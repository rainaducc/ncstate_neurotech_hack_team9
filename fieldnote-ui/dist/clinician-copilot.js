// clinician-copilot.js — NeuroSym EEG report + clinician copilot inside the NeuroBridge clinician workspace.
// Talks to fieldnote_server.py: GET /api/clinician/reports, GET /api/clinician/report?id=, POST /api/clinician/ask.
// The server runs the same Copilot as the Streamlit app (agent.py): every answer passes check_text()
// guardrails and is written to audit/agent_log.jsonl. This file only displays what the server returns.
(() => {
  const CP = window.CP = { reports: null, id: null, report: null, msgs: {}, busy: false, error: null };
  const q = s => document.querySelector(s);
  const e = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const fmt = (x, d = 1) => (x === null || x === undefined || x === '') ? '—' : (typeof x === 'number' ? String(+x.toFixed(d)) : e(x));
  // 3 significant digits so small values (z = -0.00709) are not shown as 0; integers >= 1000 stay whole
  const sig = x => (x === null || x === undefined || x === '') ? '—'
    : (typeof x === 'number' ? (Math.abs(x) >= 1000 ? String(Math.round(x)) : String(Number(x.toPrecision(3)))) : e(x));

  const ANSWER = {
    WITHIN_NORMATIVE_RANGE: { tone: 'within', plain: 'Measured motor responses fall inside the healthy reference range.' },
    INCONCLUSIVE_REPEAT: { tone: 'repeat', plain: 'The recording could not support a confident result. A repeat recording is suggested.' },
    OUTSIDE_NORMATIVE_RANGE_REVIEW: { tone: 'review', plain: 'One or more measured responses fall outside the healthy reference range. Clinician review is suggested.' },
  };
  const SUGGESTIONS = ['Explain the flagged features', 'What confounds or data-quality issues apply?', 'Compare with the previous visit', 'Draft a summary for the chart'];

  async function getJSON(url) {
    const r = await fetch(url);
    if (!r.ok) throw new Error(r.status);
    return r.json();
  }

  async function load(id) {
    CP.error = null;
    try {
      if (!CP.reports) CP.reports = (await getJSON('/api/clinician/reports')).reports;
      CP.id = id || CP.id || (CP.reports[0] && CP.reports[0].recording_id);
      CP.report = CP.id ? await getJSON('/api/clinician/report?id=' + encodeURIComponent(CP.id)) : null;
      CP.msgs[CP.id] = CP.msgs[CP.id] || [];
    } catch (err) {
      CP.error = 'The NeuroSym agent server is not reachable. Start fieldnote_server.py to load EEG reports.';
    }
    paint();
  }

  // z-scale strip: -4..+4, shaded band = typical healthy range (|z| < 1.96)
  function zStrip(z) {
    if (z === null || z === undefined) return '<span class="cp-z-na">no reference</span>';
    const clamp = Math.max(-4, Math.min(4, z));
    const pos = ((clamp + 4) / 8) * 100;
    return `<span class="cp-z" aria-label="z = ${sig(z)}"><span class="cp-z-band"></span><span class="cp-z-mid"></span><span class="cp-z-dot ${Math.abs(z) >= 1.96 ? 'out' : ''}" style="left:${pos}%"></span></span>`;
  }

  function reportHTML(r) {
    const a = ANSWER[r.answer] || { tone: 'repeat', plain: '' };
    const dq = r.data_quality || {};
    const trials = Object.entries(dq.clean_trials_per_condition || {});
    const tsp = r.three_state_profile || {};
    const tspRows = Object.entries(tsp).filter(([, v]) => v && typeof v === 'object');
    const tspCols = [...new Set(tspRows.flatMap(([, v]) => Object.keys(v)))];
    return `
      <div class="cp-verdict cp-${a.tone}">
        ${r.simulated ? '<p class="cp-sim">SIMULATED EEG · rehearsal data, not a recording</p>' : ''}
        ${r.provisional ? '<p class="cp-sim">PROVISIONAL · session still in progress, no final report yet</p>' : ''}
        <span class="small-label">MODEL ANSWER · ${e(r.recording_id)}</span>
        <h2 class="cp-label">${e(r.answer)}</h2>
        <p>${e(a.plain)}</p>
        ${(r.answer_reasons || []).length ? `<ul class="cp-reasons">${r.answer_reasons.map(x => `<li>${e(x)}</li>`).join('')}</ul>` : ''}
      </div>

      <div class="cp-block">
        <div class="cp-block-head"><span class="small-label">DATA QUALITY</span><span class="cp-pill cp-dq-${e((dq.status || '').toLowerCase())}">${e(dq.status || 'UNKNOWN')}</span></div>
        ${trials.map(([k, v]) => `<div class="task-status"><span>${e(k.replace(/_/g, ' '))}</span><span>${fmt(v, 0)} clean trials</span></div>`).join('')}
        ${dq.n_trials_total !== null && dq.n_trials_total !== undefined ? `<div class="task-status"><span>Trials scored (all conditions and hands)</span><span>${fmt(dq.n_trials_total, 0)}</span></div>` : ''}
        ${r.consistency ? `<div class="task-status"><span>Consistency</span><span>${e(r.consistency)}</span></div>` : ''}
        ${(dq.bad_channels || []).length ? `<div class="task-status"><span>Bad channels</span><span>${dq.bad_channels.map(e).join(', ')}</span></div>` : ''}
        ${(dq.notes || []).map(n => `<p class="hint cp-tight">${e(n)}</p>`).join('')}
      </div>

      <div class="cp-block">
        <div class="cp-block-head"><span class="small-label">FEATURES VS HEALTHY REFERENCE</span><span class="cp-legend"><i></i>typical range</span></div>
        <table class="cp-features">
          <thead><tr><th>Feature</th><th>Value</th><th>z</th><th>Percentile</th><th class="cp-z-col">Position</th></tr></thead>
          <tbody>${(r.features || []).map(f => `<tr class="${f.flagged ? 'flagged' : ''}"><td>${e(f.label)}<small>${e(f.name)}</small></td><td>${sig(f.value)} ${e(f.unit)}</td><td>${sig(f.z)}</td><td>${sig(f.percentile)}</td><td class="cp-z-col">${zStrip(f.z)}</td></tr>`).join('')}</tbody>
        </table>
      </div>

      ${tspRows.length ? `<div class="cp-block"><span class="small-label">THREE-STATE PROFILE</span>
        <table class="cp-features cp-tsp"><thead><tr><th></th>${tspCols.map(c => `<th>${e(c)}</th>`).join('')}</tr></thead>
        <tbody>${tspRows.map(([k, v]) => `<tr><td>${e(k.replace(/_/g, ' '))}</td>${tspCols.map(c => `<td>${fmt(v[c])}</td>`).join('')}</tr>`).join('')}</tbody></table></div>` : ''}

      ${(r.exploratory || []).length ? `<div class="cp-block"><span class="small-label">EXPLORATORY · NOT SCORED</span>
        ${r.exploratory.map(x => `<p class="hint cp-tight">${e(x)}</p>`).join('')}
        <p class="hint cp-tight">No healthy reference exists for these blocks; they do not affect the answer above.</p></div>` : ''}
      ${(r.confound_warnings || []).length ? `<div class="notice">${r.confound_warnings.map(e).join('<br>')}</div>` : ''}
      <p class="hint">${e(r.guarantee_note)}</p>
      <p class="hint cp-tight">${(r.limitations || []).map(e).join(' · ')}</p>`;
  }

  function chatHTML() {
    const msgs = CP.msgs[CP.id] || [];
    const mode = window.AI && window.AI.llm ? 'Live model · tool calling' : 'Template mode · set ANTHROPIC_API_KEY for live answers';
    return `
      <span class="small-label">NEUROSYM COPILOT</span>
      <h2 style="margin:10px 0 6px">Ask about this report.</h2>
      <p class="cp-mode">${e(mode)}</p>
      <div class="cp-suggest">${SUGGESTIONS.map(s => `<button class="cp-chip" data-cp-q="${e(s)}">${e(s)}</button>`).join('')}</div>
      <div class="guide-log cp-log" id="cp-log" aria-live="polite">
        ${msgs.length ? '' : '<p class="hint cp-tight">Answers cite only numbers from the report and never diagnose.</p>'}
        ${msgs.map(m => `<div class="guide-msg ${m.role === 'user' ? 'me' : 'cp-ans'}"><small>${m.role === 'user' ? 'CLINICIAN' : 'COPILOT'}</small>${e(m.content)}${m.meta ? `<span class="cp-meta">${e(m.meta)}</span>` : ''}</div>`).join('')}
        ${CP.busy ? '<div class="guide-msg cp-ans cp-busy"><small>COPILOT</small>Reading the report…</div>' : ''}
      </div>
      <div class="guide-input"><input id="cp-q" placeholder="Ask about features, confounds, data quality…" maxlength="1000" ${CP.busy ? 'disabled' : ''}><button class="btn" id="cp-send" ${CP.busy ? 'disabled' : ''}>Ask ↗</button></div>
      <p class="hint">Every answer passes the guardrails: research-prototype header, answer label verbatim, report numbers only, no diagnostic language. All questions and answers are written to the audit log.</p>`;
  }

  function paint() {
    const root = q('#cp-root');
    if (!root) return;
    if (CP.error) { root.innerHTML = `<section class="panel"><div class="empty"><h3>EEG report unavailable.</h3><p>${e(CP.error)}</p></div></section>`; return; }
    if (!CP.reports) { root.innerHTML = '<section class="panel"><p class="hint">Loading reports…</p></section>'; return; }
    if (!CP.report) { root.innerHTML = '<section class="panel"><div class="empty"><h3>No EEG reports found.</h3><p>Add report JSON files to agent/contract/samples/.</p></div></section>'; return; }
    root.innerHTML = `
      <div class="cp-toolbar">
        <div class="field cp-select"><label for="cp-report">Recording</label>
          <select id="cp-report">${CP.reports.map(r => `<option value="${e(r.recording_id)}" ${r.recording_id === CP.id ? 'selected' : ''}>${e(r.recording_id)} — ${e(r.answer)}${r.provisional ? ' (provisional)' : ''}</option>`).join('')}</select></div>
        <div class="cp-attach">${window.SL ? (SL.reportAttached(CP.id) ? '<span class="cp-pill cp-dq-pass">ATTACHED TO PATIENT</span>' : '<button class="btn secondary" id="cp-attach">Attach to patient ↗</button>') : ''}
        <p class="cp-header">${e(CP.report.header)}</p></div>
      </div>
      <div class="two-col">
        <section class="panel">${reportHTML(CP.report)}</section>
        <aside class="panel cp-chat">${chatHTML()}</aside>
      </div>`;
    bind();
    const log = q('#cp-log');
    if (log) log.scrollTop = log.scrollHeight;
  }

  async function ask(question) {
    question = (question || '').trim();
    if (!question || CP.busy) return;
    const id = CP.id;
    const msgs = CP.msgs[id];
    const history = msgs.map(m => ({ role: m.role, content: m.content }));
    msgs.push({ role: 'user', content: question });
    CP.busy = true; paint();
    const j = await window.aiCall('/api/clinician/ask', { recording_id: id, question, history });
    CP.busy = false;
    if (j && j.answer) {
      const src = { llm: 'live model', 'llm-retry': 'live model (rewritten after a guardrail check)', offline: 'template',
        fallback: 'template (live answer failed guardrails or the API)' }[j.source] || j.source;
      const ver = j.verified === true ? ' · claims verified' : j.verified === false ? ' · claim check failed' : '';
      const tools = (j.tools || []).length ? ` · tools: ${j.tools.join(', ')}` : '';
      const guard = (j.violations || []).length ? ` · guardrail flags: ${j.violations.join('; ')}` : ' · guardrails passed';
      msgs.push({ role: 'assistant', content: j.answer, meta: src + tools + guard + ver });
    } else {
      msgs.push({ role: 'assistant', content: 'The copilot is unavailable right now. The report on the left is unchanged.', meta: 'no answer' });
    }
    if (CP.id === id) paint();
  }

  function bind() {
    const att = q('#cp-attach');
    if (att) att.onclick = async () => {
      att.disabled = true;
      try { await SL.attachReport(CP.id); toast('Report attached. The patient is told results are ready to discuss — no numbers.'); }
      catch (err) { toast('Could not attach: ' + (err.message || err)); }
      paint();
    };
    const sel = q('#cp-report');
    if (sel) sel.onchange = () => load(sel.value);
    document.querySelectorAll('[data-cp-q]').forEach(b => b.onclick = () => ask(b.dataset.cpQ));
    const send = q('#cp-send'), input = q('#cp-q');
    if (send) send.onclick = () => ask(input.value);
    if (input) {
      input.onkeydown = ev => { if (ev.key === 'Enter') ask(input.value); };
      if (!CP.busy && document.activeElement === document.body && (CP.msgs[CP.id] || []).length) input.focus();
    }
  }

  // Hooks used by app.js
  window.copilotSection = () => '<div id="cp-root" class="cp-root"></div>';
  window.copilotBind = () => { if (!q('#cp-root')) return; CP.reports && CP.report ? paint() : load(CP.id); };
})();
