// shared-layer.js — the shared communication layer: one thread per patient, synced through the server.
//
// Patient: a Messages page, sharing/withdrawing the profile is stored on the server, the dashboard shows
//          the clinician's latest message. The patient never receives EEG numbers or report labels.
// Clinician: the Overview is an inbox (patient card + thread + reply); the shared profile comes from the
//          server and only while the patient's share is active; EEG reports can be attached to the patient.
// Both views poll every 4 s. Identity is a demo stand-in: one demo patient and the role switch.
(() => {
  const q = s => document.querySelector(s);
  const e = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const SL = window.SL = { pid: 'P-0001', online: false, msgs: { patient: [], clinician: [] }, lastId: { patient: 0, clinician: 0 },
    st: { patient: null, clinician: null }, overview: null, sending: false, timer: null, shareKey: null };
  const role = () => (state.role === 'clinician' ? 'clinician' : 'patient');

  async function get(path) { const r = await fetch(path); if (!r.ok) throw new Error(r.status); return r.json(); }
  async function post(path, body) {
    const r = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || r.status);
    return j;
  }
  const P = (what, qs = '') => `/api/patients/${SL.pid}/${what}${qs}`;

  // ------------------------------------------------------------ sync
  // Single-flight: one poll at a time; calls that arrive meanwhile schedule exactly one more run.
  let inflight = null, rerun = false;
  function poll() {
    if (inflight) { rerun = true; return inflight; }
    inflight = pollOnce().finally(() => { inflight = null; if (rerun) { rerun = false; poll(); } });
    return inflight;
  }

  async function pollOnce() {
    const r = role();
    const wasOnline = SL.online;
    try {
      const [st, m] = await Promise.all([get(P('state', `?role=${r}`)), get(P('messages', `?role=${r}&after=${SL.lastId[r]}`))]);
      if (r === 'clinician') SL.overview = (await get('/api/patients')).patients;
      if (r !== role()) return;                                   // role switched mid-poll: drop it
      SL.online = true;
      const prev = SL.st[r];
      const before = JSON.stringify([prev && prev.shared, prev && prev.share && prev.share.shared_utc]);
      const mismatch = r === 'patient' && !!st.shared !== !!state.shared;   // UI disagrees with server
      const lost = r === 'clinician' && prev && prev.share && !st.share;      // patient withdrew
      SL.st[r] = st;
      const seen = new Set(SL.msgs[r].map(x => x.id));
      const fresh = m.messages.filter(x => !seen.has(x.id));
      if (fresh.length) { SL.msgs[r].push(...fresh); SL.lastId[r] = Math.max(SL.lastId[r], ...fresh.map(x => x.id)); }
      const changed = mismatch || before !== JSON.stringify([st.shared, st.share && st.share.shared_utc]);
      syncAppState(r, st);
      if (fresh.length) paintThread();
      markReadIfVisible();
      paintBadges();
      if (changed || fresh.some(x => x.author !== r) || !wasOnline) SL.pendingRender = true;
      if (lost && current === 'profile' && state.role === 'clinician') { SL.pendingRender = false; render(); return; }  // never defer a withdrawal
      flush();
    } catch (err) {
      SL.online = false;
      if (wasOnline) { SL.pendingRender = true; flush(); }
    }
  }

  function flush() { if (SL.pendingRender && safeToRender()) { SL.pendingRender = false; render(); } }
  document.addEventListener('focusout', () => setTimeout(flush, 0));

  // Mirror the server into the app's in-memory state so existing screens keep working.
  function syncAppState(r, st) {
    if (r === 'clinician') {
      state.shared = st.share ? { ...st.share.snapshot, records: st.share.snapshot.records || [] } : null;
    } else {
      if (!st.shared) state.shared = null;
      else if (!state.shared) state.shared = { restored: true };
      state.checks[2] = !!st.shared;
      const lastClin = [...SL.msgs.patient].reverse().find(x => x.author === 'clinician');
      state.clinicianNote = lastClin ? lastClin.body : '';
    }
  }

  function safeToRender() {
    const a = document.activeElement;
    if (a && ['INPUT', 'TEXTAREA', 'SELECT'].includes(a.tagName)) return false;
    if ((q('#sl-body') && q('#sl-body').value.trim()) || (q('#cp-q') && q('#cp-q').value.trim())) return false;  // keep drafts
    if (q('#modal') && q('#modal').open) return false;
    if (window.MS && MS.view === 'running') return false;
    return ['dashboard', 'profile', 'messages'].includes(current);
  }

  function threadVisible() {
    return !document.hidden && ((role() === 'patient' && current === 'messages') || (role() === 'clinician' && current === 'dashboard'));
  }
  function markReadIfVisible() {
    const r = role();
    if (threadVisible() && SL.lastId[r] && SL.st[r] && SL.st[r].unread) {
      post(P('read'), { role: r, upto: SL.lastId[r] }).then(() => { if (SL.st[r]) SL.st[r].unread = 0; paintBadges(); }).catch(() => {});
    }
  }

  function paintBadges() {
    const r = role(), n = SL.st[r] ? SL.st[r].unread : 0;
    const link = document.querySelector(`.nav-item[href="#${r === 'patient' ? 'messages' : 'dashboard'}"]`);
    if (!link) return;
    let b = link.querySelector('.sl-badge');
    if (!n) { b && b.remove(); return; }
    if (!b) { b = document.createElement('span'); b.className = 'sl-badge'; link.appendChild(b); }
    b.textContent = n;
  }

  // ------------------------------------------------------------ thread
  const WHO = { patient: { patient: 'YOU', clinician: 'YOUR CLINICIAN' }, clinician: { patient: 'PATIENT', clinician: 'YOU' } };
  function when(iso) {
    const d = new Date(iso);
    return isNaN(d) ? '' : d.toLocaleString('en-US', { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
  }
  function attachmentHTML(a, r) {
    if (!a) return '';
    if (a.type === 'session') return `<span class="sl-att">${a.mode === 'full' ? 'Movement session' : 'Rehearsal'} · ${a.cues ?? 0} cues · ${a.completed ? 'complete' : 'stopped early'}</span>`;
    if (a.type === 'report' && r === 'clinician') return `<button class="sl-att sl-open-report" data-report="${e(a.recording_id)}">EEG report · ${e(a.answer)}${a.simulated ? ' · simulated' : ''} — open ↗</button>`;
    return '';
  }
  function threadHTML() {
    const r = role(), list = SL.msgs[r];
    const off = SL.online ? '' : '<p class="hint sl-offline">Offline — new messages will appear when the connection is back.</p>';
    if (!SL.online && !list.length) return off;
    if (!list.length) return `<p class="hint">${r === 'patient' ? 'No messages yet. You can write to your care team here.' : 'No messages yet.'}</p>`;
    return off + list.map(m => m.author === 'system'
      ? `<div class="sl-event"><span>${e(m.body)}</span>${attachmentHTML(m.attachment, r)}<small>${when(m.created_utc)}</small></div>`
      : `<div class="sl-msg ${m.author === r ? 'mine' : ''}"><small>${WHO[r][m.author]} · ${when(m.created_utc)}</small><p>${e(m.body)}</p></div>`).join('');
  }
  function paintThread() {
    const el = q('#sl-thread'); if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
    el.innerHTML = threadHTML();
    if (atBottom || !el.dataset.painted) el.scrollTop = el.scrollHeight;
    el.dataset.painted = '1';
    bindThread();
  }
  function composerHTML() {
    const r = role();
    return `<div class="sl-compose"><textarea id="sl-body" rows="3" maxlength="4000" placeholder="${r === 'patient' ? 'Write to your care team…' : 'Write to the patient…'}"></textarea>
      <div class="sl-compose-row"><span class="hint sl-tight">${r === 'patient' ? 'For anything urgent, call your clinic or emergency services.' : 'Messages are visible to the patient. Do not paste EEG numbers here.'}</span><button class="btn" id="sl-send">Send ↗</button></div></div>`;
  }
  async function send() {
    const box = q('#sl-body'); if (!box || SL.sending) return;
    const body = box.value.trim(); if (!body) return;
    SL.sending = true; q('#sl-send').disabled = true;
    try { await post(P('messages'), { role: role(), body }); box.value = ''; await poll(); }
    catch (err) { toast('Message not sent: ' + (err.message || err)); }
    SL.sending = false; if (q('#sl-send')) q('#sl-send').disabled = false;
  }
  function bindThread() {
    document.querySelectorAll('.sl-open-report').forEach(b => b.onclick = () => {
      if (window.CP) { CP.id = b.dataset.report; CP.report = null; }
      go('eeg');
    });
  }
  function bindComposer() {
    if (q('#sl-send')) q('#sl-send').onclick = send;
    if (q('#sl-body')) q('#sl-body').onkeydown = ev => { if (ev.key === 'Enter' && (ev.metaKey || ev.ctrlKey)) { ev.preventDefault(); send(); } };
  }

  // ------------------------------------------------------------ pages
  window.messagesPage = () => {
    const st = SL.st.patient || {};
    return header('MESSAGES', 'Your care team, in one place.', 'Messages and updates between you and your clinician.') +
      `<div class="two-col"><section class="panel sl-panel"><span class="small-label">CONVERSATION</span><div class="sl-thread" id="sl-thread">${threadHTML()}</div>${composerHTML()}</section>
      <aside class="panel"><span class="small-label">WHAT YOUR CLINICIAN CAN SEE</span><h2 style="margin:10px 0 16px">You decide.</h2>
        <div class="task-status"><span>Visit profile</span><span>${st.shared ? 'Shared' : 'Not shared'}</span></div>
        <div class="task-status"><span>Movement sessions</span><span>${(st.sessions || []).length} recorded</span></div>
        <div class="task-status"><span>EEG results</span><span>Reviewed by your care team</span></div>
        <p class="hint">Your story is shared only after you confirm it and choose to share. You can withdraw access at any time from your visit profile.</p>
        ${action(st.shared ? 'Review what you shared' : 'Prepare your visit profile', 'profile', 'secondary')}</aside></div>`;
  };

  window.inboxSection = () => {
    const ov = (SL.overview || []).find(p => p.id === SL.pid) || {};
    const st = SL.st.clinician || {};
    return `<div class="two-col sl-inbox"><section class="panel sl-panel"><div class="sl-head"><span class="small-label">CONVERSATION · ${e(ov.name || 'Patient')}</span>${SL.online ? '' : '<span class="hint sl-tight">Offline</span>'}</div>
        <div class="sl-thread" id="sl-thread">${threadHTML()}</div>${composerHTML()}</section>
      <aside class="panel"><span class="small-label">PATIENT</span><h2 style="margin:10px 0 16px">${e(ov.name || 'Jordan M.')}</h2>
        <div class="task-status"><span>Visit profile</span><span>${st.share ? 'Shared ' + when(st.share.shared_utc) : 'Not shared'}</span></div>
        <div class="task-status"><span>Movement sessions</span><span>${(st.sessions || []).length}</span></div>
        <div class="task-status"><span>EEG reports attached</span><span>${(st.reports || []).length}</span></div>
        <div class="actions">${st.share ? action('Open shared profile', 'profile') : ''}${action('EEG report & copilot', 'eeg', 'secondary')}</div>
        <p class="hint">The profile appears only while the patient's share is active.</p></aside></div>`;
  };

  if (q('#modal')) q('#modal').addEventListener('close', () => setTimeout(flush, 0));

  // Called from app.js bind() after each render.
  window.sharedBind = () => {
    paintThread(); bindComposer(); paintBadges(); markReadIfVisible();
    if (!SL.timer) { SL.timer = setInterval(poll, 4000); }
  };

  // Hooks for app.js actions (share, withdraw, clinician note) and the copilot (attach report).
  SL.share = (snap, rollback) => post(P('share'), { snapshot: snap })
    .then(() => { toast('Shared with your clinician'); return poll(); })
    .catch(err => { rollback && rollback(); toast('Sharing failed, nothing was shared: ' + (err.message || err)); });
  SL.withdraw = rollback => post(P('withdraw'), {})
    .then(() => { toast('Clinician access withdrawn'); return poll(); })
    .catch(err => { rollback && rollback(); toast('Withdrawing failed — your profile is still shared. Try again: ' + (err.message || err)); });
  SL.note = body => post(P('messages'), { role: 'clinician', body }).then(poll).catch(err => toast('Note not sent: ' + (err.message || err)));
  SL.attachReport = id => post(P('reports'), { recording_id: id }).then(poll);
  SL.reportAttached = id => !!(SL.st.clinician && (SL.st.clinician.reports || []).includes(id));
  SL.poll = poll;
  // Clinician empty state: date of the withdrawal if the most recent share event was a withdrawal, else ''.
  SL.withdrawn = () => {
    const st = SL.st.clinician;
    if (role() !== 'clinician' || !st || st.share) return '';
    const last = [...SL.msgs.clinician].reverse().find(m => m.kind === 'profile_shared' || m.kind === 'profile_withdrawn');
    return last && last.kind === 'profile_withdrawn' ? when(last.created_utc) : '';
  };

  // A role switch in this tab changes whose thread we show.
  // Loaded after app.js, so state/render exist. First sync, then one render to show server state.
  let lastRole = role();
  setInterval(() => { if (lastRole !== role()) { lastRole = role(); state.shared = null; SL.pendingRender = true; poll(); } }, 300);
  poll().then(() => { if (safeToRender()) render(); else window.sharedBind(); });
})();
