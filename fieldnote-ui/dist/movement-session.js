// movement-session.js — the cued movement session (replaces the keyboard tapping task).
//
// Same task the NeuroSym model is calibrated on: cued LEFT / RIGHT fist open-close, real (MOVE) and
// imagined (IMAGINE), after eyes-open and eyes-closed rest. The schedule and marker strings come from the
// server (agent/live_protocol.py = neurosym-agent/live/protocol.py). Each cue is sent to the LSL marker
// stream "NeuroSymMarkers", so neurosym-agent/live/live_score.py scores the session unchanged.
//
// Timing: steps run against absolute performance.now() deadlines (no drift). A cue's marker carries the
// timestamp of the animation frame after the cue was drawn, converted to LSL time with an offset measured
// at session start (lowest-round-trip ping).
//
// Optional camera check: frame-to-frame motion (64x48 grayscale, on this device only, nothing stored or
// sent except per-trial counts). MOVE trials with little motion and IMAGINE trials with motion are counted.
// It is a compliance check only — it never touches the EEG or the model's result.
(() => {
  const q = s => document.querySelector(s);
  const e = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const MS = window.MS = { view: 'setup', mode: 'full', camera: false, devices: null, checking: false, preview: null,
    sid: null, sched: null, offset: null, rtt: null, steps: [], idx: 0, t0: 0, timer: null, raf: null,
    shown: 0, total: 0, label: '', last: null, abortReason: null, markerChain: Promise.resolve(), lsl: false };
  const WORD = { execute: 'MOVE', imagine: 'IMAGINE' };
  const INSTRUCT = {
    execute: 'Open and close the fist on the side shown, steadily, until the word disappears.',
    imagine: 'Imagine opening and closing that fist. Keep both hands relaxed and still.',
  };
  const CAM = { stream: null, video: null, canvas: null, ctx: null, prev: null, samples: [], loop: null };
  const MOTION_RATIO = 1.5;   // trial motion vs this person's own resting motion (heuristic QC threshold)

  async function api(path, body) {
    const r = await fetch(path, body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), keepalive: true });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || r.status);
    return j;
  }

  // ------------------------------------------------------------ setup view
  async function checkDevices() {
    MS.checking = true; paint();
    try { MS.devices = await api('/api/session/devices'); } catch (err) { MS.devices = { error: String(err.message || err) }; }
    MS.checking = false; paint();
  }

  function deviceRows() {
    const d = MS.devices;
    if (MS.checking) return '<div class="task-status"><span>Headset</span><span>Checking…</span></div>';
    if (!d) return '<div class="task-status"><span>Headset</span><span>Not checked</span></div>';
    if (d.error) return `<div class="task-status"><span>Headset</span><span class="ms-bad">Server unreachable</span></div>`;
    const eeg = d.eeg || [];
    const best = eeg.find(x => x.data_flowing && x.has_c3_c4) || eeg[0];
    return `
      <div class="task-status"><span>Headset stream</span><span class="${best && best.data_flowing ? 'ms-ok' : 'ms-bad'}">${best ? `${e(best.name)} · ${best.channels} ch · ${Math.round(best.srate)} Hz` : 'Not found'}</span></div>
      <div class="task-status"><span>C3 / C4 channels</span><span class="${best && best.has_c3_c4 ? 'ms-ok' : 'ms-bad'}">${best ? (best.has_c3_c4 ? 'Present' : 'Missing') : '—'}</span></div>
      <div class="task-status"><span>Signal</span><span class="${best && best.data_flowing ? 'ms-ok' : 'ms-bad'}">${best ? (best.data_flowing ? 'Data flowing' : 'No data') : '—'}</span></div>
      <div class="task-status"><span>Live scoring</span><span class="${d.scorer_listening ? 'ms-ok' : 'ms-warn'}">${d.scorer_listening ? 'Listening' : 'Not listening yet'}</span></div>
      ${d.lsl ? '' : `<div class="task-status"><span>Marker stream</span><span class="ms-bad">${e(d.lsl_error || 'unavailable')}</span></div>`}
      ${d.competing_marker_streams ? '<div class="task-status"><span>Other cue source</span><span class="ms-bad">Running — stop cue_runner.py</span></div>' : ''}
      ${eeg.some(x => /simulat/i.test(x.name)) ? '<p class="hint ms-tight">This headset stream is simulated. Use it to rehearse only.</p>' : ''}`;
  }

  function ready() {
    const d = MS.devices;
    return !!(d && d.lsl && d.scorer_listening && !d.competing_marker_streams && (d.eeg || []).some(x => x.data_flowing && x.has_c3_c4));
  }
  // A full session needs the headset; a rehearsal can run without one (markers are still logged).
  const canBegin = () => MS.mode === 'rehearsal' ? !!(MS.devices && !MS.devices.error) : ready();

  function setupHTML() {
    const full = MS.mode === 'full';
    return `
      <span class="small-label">CUED HAND MOVEMENT · WITH HEADSET</span>
      <h2>${full ? 'About 11 minutes.' : 'Rehearsal · about 3½ minutes.'}</h2>
      <p>Words on the screen tell you which hand to use, and whether to <b>move</b> it or only <b>imagine</b> moving it.</p>
      <div class="ms-modes">
        <button class="ms-mode ${full ? 'on' : ''}" data-ms-mode="full"><b>Full session</b><small>Rest 2 min · 4 runs · 16 cues per hand per task</small></button>
        <button class="ms-mode ${full ? '' : 'on'}" data-ms-mode="rehearsal"><b>Rehearsal</b><small>Practice only · 3 cues per hand per run · not sent to the scorer</small></button>
      </div>
      <label class="ms-check"><input type="checkbox" id="ms-camera" ${MS.camera ? 'checked' : ''}><span>Use the camera to check hand movement<small>Stays on this device. Only counts of trials are kept.</small></span></label>
      ${MS.camera ? '<div class="ms-preview"><video id="ms-video" autoplay muted playsinline></video><span>Keep both hands in view</span></div>' : ''}
      <div class="actions">
        <button class="btn blue" id="ms-begin" ${canBegin() ? '' : 'disabled'}>${full ? 'Begin session' : 'Begin rehearsal'} ↗</button>
        <button class="btn secondary" id="ms-check">${MS.devices ? 'Check again' : 'Check headset'}</button>
      </div>
      ${MS.devices && !ready() ? `<p class="hint">${full ? 'A full session starts once the headset stream has C3 and C4 with data flowing, and the live scorer is listening.' : 'No headset found. A rehearsal is practice only and works without one.'}</p>` : ''}`;
  }

  function doneHTML() {
    const r = MS.last || {};
    const cam = r.camera;
    const aborted = r.completed === false;
    return `
      <span class="small-label">${aborted ? 'SESSION STOPPED' : 'SESSION COMPLETE'}</span>
      <h2 style="margin-top:14px">${aborted ? 'The session stopped early.' : 'Thank you. Your session is recorded.'}</h2>
      <p>${aborted ? e(r.aborted_reason || 'Stopped.') + ' Your care team will see that it stopped early.' : `${r.cues_shown} cues shown. Your care team reviews the recording.`}</p>
      ${cam ? `<div class="notice"><b>Camera check</b> · MOVE cues with little hand movement: ${cam.move_low} of ${cam.move_n}. IMAGINE cues with hand movement: ${cam.imagine_moved} of ${cam.imagine_n}.${cam.imagine_moved ? ' During IMAGINE, try to keep both hands completely still.' : ''}</div>` : ''}
      ${action('See your recordings', 'recordings')}
      <button class="text-btn" id="ms-again" style="margin-top:18px">Set up another session</button>`;
  }

  function pageAside() {
    return `<aside class="panel"><span class="small-label">BEFORE YOU START</span><h2 style="margin:10px 0 20px">Settle in.</h2>
      <div class="milestone"><b>Headset on, signal checked</b><p>Wear the headset as fitted. The headset check confirms the signal reaches this computer.</p></div>
      <div class="milestone"><b>Forearms on the table, palms up</b><p>Hands relaxed and in view of the camera. Sit so you can see the screen without moving your head.</p></div>
      <div class="milestone"><b>Blink and swallow during the cross</b><p>While a word is showing, keep your face and body still. The screen goes full size; press Esc to stop at any time.</p></div>
      ${deviceRows()}</aside>`;
  }

  window.sessionPage = () => header('MOVEMENT SESSION', 'Move, imagine, rest.', 'Follow the words on the screen with each hand. It is the same task your clinician’s EEG reference uses.') +
    `<div class="two-col"><section><div class="task-stage ms-stage" id="ms-stage">${MS.view === 'done' ? doneHTML() : setupHTML()}</div>
      <div class="notice">The session records your EEG for your care team. You will not see EEG results here, and nothing on this page is a diagnosis.</div></section>
      ${pageAside()}</div>`;

  function paint() {
    if (current !== 'tasks' || state.role !== 'patient') return;
    const main = q('#main'); if (!main) return;
    main.innerHTML = window.sessionPage();
    window.sessionBind();
  }

  window.sessionBind = () => {
    document.querySelectorAll('[data-ms-mode]').forEach(b => b.onclick = () => { MS.mode = b.dataset.msMode; paint(); });
    const cam = q('#ms-camera');
    if (cam) cam.onchange = async () => { MS.camera = cam.checked; if (MS.camera) await camOn(); else camOff(); paint(); };
    if (q('#ms-video') && CAM.stream) { q('#ms-video').srcObject = CAM.stream; }
    if (q('#ms-check')) q('#ms-check').onclick = checkDevices;
    if (q('#ms-begin')) q('#ms-begin').onclick = begin;
    if (q('#ms-again')) q('#ms-again').onclick = () => { MS.view = 'setup'; MS.last = null; MS.devices = null; paint(); };
    document.querySelectorAll('[data-go]').forEach(b => b.onclick = () => go(b.dataset.go));
    if (!MS.devices && !MS.checking) checkDevices();
  };

  // Leaving the page or switching role mid-session aborts it (called from app.js stopTask()).
  window.sessionLeave = () => { if (MS.view === 'running') abort('You left the session page.'); camOff(); MS.camera = false; };

  // ------------------------------------------------------------ camera check
  async function camOn() {
    try {
      CAM.stream = await navigator.mediaDevices.getUserMedia({ video: { width: 320, height: 240 }, audio: false });
      CAM.video = document.createElement('video'); CAM.video.muted = true; CAM.video.playsInline = true;
      CAM.video.srcObject = CAM.stream; await CAM.video.play();
      CAM.canvas = document.createElement('canvas'); CAM.canvas.width = 64; CAM.canvas.height = 48;
      CAM.ctx = CAM.canvas.getContext('2d', { willReadFrequently: true });
    } catch (err) {
      if (CAM.stream) CAM.stream.getTracks().forEach(t => t.stop());
      MS.camera = false; CAM.stream = null; toast('Camera unavailable — the session runs without the camera check');
    }
  }
  function camOff() { camStop(); if (CAM.stream) CAM.stream.getTracks().forEach(t => t.stop()); CAM.stream = null; }
  function camStart() {
    CAM.samples = []; CAM.prev = null;
    if (!CAM.stream) return;
    CAM.loop = setInterval(() => {
      if (!CAM.video || CAM.video.readyState < 2) return;
      CAM.ctx.drawImage(CAM.video, 0, 0, 64, 48);
      const px = CAM.ctx.getImageData(0, 0, 64, 48).data;
      const g = new Float32Array(64 * 48);
      for (let i = 0; i < g.length; i++) g[i] = 0.299 * px[4 * i] + 0.587 * px[4 * i + 1] + 0.114 * px[4 * i + 2];
      if (CAM.prev) { let s = 0; for (let i = 0; i < g.length; i++) s += Math.abs(g[i] - CAM.prev[i]); CAM.samples.push([performance.now(), s / g.length]); }
      CAM.prev = g;
    }, 66);
  }
  function camStop() { if (CAM.loop) clearInterval(CAM.loop); CAM.loop = null; }
  function meanMotion(a, b) {
    const v = CAM.samples.filter(([t]) => t >= a && t <= b).map(([, m]) => m);
    return v.length ? v.reduce((x, y) => x + y, 0) / v.length : null;
  }
  function cameraSummary(cues) {
    if (!CAM.samples.length || !cues.length) return null;
    const rest = cues.map(c => meanMotion(c.t - 1000, c.t)).filter(x => x !== null).sort((a, b) => a - b);
    if (!rest.length) return null;
    const base = Math.max(rest[Math.floor(rest.length / 2)], 0.05);
    const out = { move_n: 0, move_low: 0, imagine_n: 0, imagine_moved: 0, threshold_ratio: MOTION_RATIO };
    cues.forEach(c => {
      const m = meanMotion(c.t + 500, c.t + MS.sched.cue_s * 1000);
      if (m === null) return;
      if (c.condition === 'execute') { out.move_n++; if (m / base < MOTION_RATIO) out.move_low++; }
      else { out.imagine_n++; if (m / base >= MOTION_RATIO) out.imagine_moved++; }
    });
    return out;
  }

  // ------------------------------------------------------------ session engine
  async function syncClock() {
    let best = null;
    for (let i = 0; i < 7; i++) {
      const a = performance.now(); const j = await api('/api/session/sync', {}); const b = performance.now();
      const rtt = b - a, off = j.lsl_time - (a + b) / 2000;
      if (!best || rtt < best.rtt) best = { rtt, off };
    }
    MS.offset = best.off; MS.rtt = best.rtt;
    await api('/api/session/sync_set', { session_id: MS.sid, offset_s: best.off, rtt_ms: best.rtt });
  }

  function sendMarker(marker, clientMs) {
    MS.markerChain = MS.markerChain.then(() => api('/api/session/marker', { session_id: MS.sid, marker, client_ms: clientMs })).catch(err => {
      if (MS.view === 'running') abort('The marker connection failed: ' + (err.message || err));
    });
    return MS.markerChain;
  }

  // A step shows a screen, optionally marks it (at paint time), then waits `dur` seconds.
  function buildSteps(s) {
    const st = [], mk = s.markers;
    for (let i = s.countdown_s; i >= 1; i--) st.push({ screen: { kind: 'count', n: i }, dur: 1 });
    st.push({ mark: mk.session_start, dur: 0 });
    s.rest_blocks.forEach(b => {
      st.push({ screen: { kind: 'rest', eyes: b.key === 'rest_ec' ? 'closed' : 'open' }, mark: b.marker + '_start', dur: b.seconds });
      st.push({ mark: b.marker + '_end', dur: 0, tone: b.key === 'rest_ec' });
    });
    s.runs.forEach((r, ri) => {
      const base = mk.run.replace('{condition}', r.condition).replace('{run}', r.run);
      st.push({ screen: { kind: 'intro', condition: r.condition, run: r.run, n: ri + 1, of: s.runs.length }, dur: s.run_intro_s });
      st.push({ mark: base + '_start', dur: 0 });
      r.trials.forEach(t => {
        st.push({ screen: { kind: 'cue', condition: r.condition, hand: t.hand }, mark: t.marker, dur: s.cue_s, cue: { condition: r.condition, hand: t.hand } });
        st.push({ screen: { kind: 'fix' }, mark: mk.cue_end, dur: t.rest_after_s });
      });
      st.push({ mark: base + '_end', dur: 0 });
    });
    st.push({ mark: mk.session_end, dur: 0, final: true });
    st.push({ screen: { kind: 'thanks' }, dur: 2 });
    return st;
  }

  function overlayHTML(sc) {
    const pct = Math.round(100 * MS.shown / Math.max(1, MS.total));
    let body = '';
    if (sc.kind === 'count') body = `<p class="ms-o-small">Starting in</p><p class="ms-o-big">${sc.n}</p>`;
    else if (sc.kind === 'rest') body = sc.eyes === 'open' ? `<p class="ms-o-big ms-o-fix">+</p><p class="ms-o-small">Rest. Look at the cross.</p>` : `<p class="ms-o-big ms-o-fix">+</p><p class="ms-o-small">Close your eyes until you hear the tone.</p>`;
    else if (sc.kind === 'intro') body = `<p class="ms-o-small">Run ${sc.n} of ${sc.of}</p><p class="ms-o-word ms-${sc.condition}">${WORD[sc.condition]}</p><p class="ms-o-small ms-o-wide">${INSTRUCT[sc.condition]}</p>`;
    else if (sc.kind === 'cue') body = `<p class="ms-o-word ms-${sc.condition}">${WORD[sc.condition]}</p><p class="ms-o-big ms-o-hand">${sc.hand === 'left' ? '←&nbsp;LEFT' : 'RIGHT&nbsp;→'}</p>`;
    else if (sc.kind === 'fix') body = `<p class="ms-o-big ms-o-fix">+</p>`;
    else if (sc.kind === 'thanks') body = `<p class="ms-o-word">Done</p><p class="ms-o-small">Thank you.</p>`;
    return `<div class="ms-o-center">${body}</div><div class="ms-o-foot"><span>${MS.shown} / ${MS.total} cues</span><span class="ms-o-bar"><i style="width:${pct}%"></i></span><span>Esc to stop</span></div>`;
  }

  function tone() {
    try {
      const ac = MS.ac; if (!ac) return;
      if (ac.state !== 'running') ac.resume();
      const o = ac.createOscillator(), g = ac.createGain();
      o.frequency.value = 660; g.gain.value = 0.15; o.connect(g); g.connect(ac.destination);
      o.start(); o.stop(ac.currentTime + 0.35);
    } catch (err) { /* no audio */ }
  }

  async function begin() {
    if (MS.view !== 'setup' || MS.starting || !canBegin()) return;
    MS.starting = true;
    const btn = q('#ms-begin'); if (btn) { btn.disabled = true; btn.textContent = 'Starting…'; }
    try {   // created inside the click so the browser allows sound later (the eyes-closed tone)
      if (!MS.ac) MS.ac = new (window.AudioContext || window.webkitAudioContext)();
      MS.ac.resume();
    } catch (err) { MS.ac = null; }
    const stillHere = () => current === 'tasks' && state.role === 'patient' && MS.view === 'setup';
    try {
      MS.devices = await api('/api/session/devices');              // fresh check right before starting
      if (!canBegin()) throw new Error(MS.mode === 'full' ? 'the headset or live scorer is not ready' : 'the server is not reachable');
      if (!stillHere()) return;
      const j = await api('/api/session/start', { mode: MS.mode, camera_check: MS.camera && !!CAM.stream, patient_id: window.SL ? SL.pid : undefined });
      MS.sid = j.session_id; MS.sched = j.schedule; MS.lsl = j.lsl; MS.headset = ready();
      await syncClock();
      if (!stillHere()) { api('/api/session/finish', { session_id: MS.sid, result: { completed: false, aborted_reason: 'left before the start', cues_shown: 0 } }).catch(() => {}); return; }
    } catch (err) { toast('Could not start the session: ' + (err.message || err)); return; }
    finally { MS.starting = false; if (MS.view === 'setup') paint(); }
    MS.ended = false;
    MS.steps = buildSteps(MS.sched); MS.idx = 0; MS.shown = 0; MS.cues = []; MS.abortReason = null;
    MS.total = MS.sched.runs.reduce((n, r) => n + r.trials.length, 0);
    MS.view = 'running';
    const ov = document.createElement('div'); ov.id = 'ms-overlay'; ov.className = 'ms-overlay'; ov.tabIndex = -1;
    document.body.appendChild(ov); ov.focus();
    // Fire and forget: some browsers never settle this promise (embedded views), and windowed is fine.
    try { ov.requestFullscreen?.().catch(() => {}); } catch (err) { /* windowed is fine */ }
    document.addEventListener('keydown', onKey, true);
    document.addEventListener('visibilitychange', onHidden);
    document.addEventListener('fullscreenchange', onFs);
    camStart();
    MS.t0 = performance.now();
    MS.deadline = MS.t0;
    runStep();
  }

  function runStep() {
    if (MS.view !== 'running') return;
    if (MS.idx >= MS.steps.length) return complete();
    const step = MS.steps[MS.idx++];
    const fire = () => {
      if (MS.view !== 'running') return;
      MS.raf = requestAnimationFrame(() => {
        if (step.screen) q('#ms-overlay').innerHTML = overlayHTML(step.screen);
        if (step.tone) tone();
        // the next frame's timestamp ≈ when the screen above was presented
        MS.raf = requestAnimationFrame(ts => {
          if (step.cue) { MS.shown++; MS.cues.push({ ...step.cue, t: ts }); const f = q('.ms-o-foot span'); if (f) f.textContent = `${MS.shown} / ${MS.total} cues`; }
          if (step.mark) sendMarker(step.mark, ts);
          if (step.final) MS.ended = true;
          MS.deadline = (step.screen || step.mark ? ts : MS.deadline) + step.dur * 1000;
          MS.timer = setTimeout(runStep, Math.max(0, MS.deadline - performance.now()));
        });
      });
    };
    fire();
  }

  function onKey(ev) { if (MS.view === 'running' && ev.key === 'Escape') { ev.preventDefault(); ev.stopPropagation(); abort('You stopped the session.'); } }
  // Brief visibility blips (fullscreen or focus transitions) are tolerated; hidden for > 1 s stops the
  // session, because hidden pages pause animation frames and throttle timers.
  function onHidden() {
    clearTimeout(MS.hiddenTimer);
    if (MS.view !== 'running' || !document.hidden) return;
    MS.hiddenTimer = setTimeout(() => {
      if (MS.view === 'running' && document.hidden) abort('The session window was hidden, so cue timing could not be kept.');
    }, 1000);
  }
  function onFs() { /* leaving fullscreen is allowed; Esc first exits fullscreen in most browsers, a second Esc stops */ }

  function teardown() {
    clearTimeout(MS.timer); clearTimeout(MS.hiddenTimer); cancelAnimationFrame(MS.raf); camStop();
    document.removeEventListener('keydown', onKey, true);
    document.removeEventListener('visibilitychange', onHidden);
    document.removeEventListener('fullscreenchange', onFs);
    if (document.fullscreenElement) document.exitFullscreen?.().catch(() => {});
    q('#ms-overlay')?.remove();
  }

  async function finish(result) {
    MS.last = result;
    camOff(); MS.camera = false;
    let summary = null;
    try { await MS.markerChain; summary = await api('/api/session/finish', { session_id: MS.sid, result }); } catch (err) { /* logged server-side as far as it got */ }
    state.records.unshift({ id: Date.now(), kind: 'session', session_id: MS.sid, mode: MS.mode,
      label: new Date().toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' }),
      completed: result.completed, aborted_reason: result.aborted_reason || null, cues_shown: result.cues_shown,
      total: MS.total, camera: result.camera || null, lsl: MS.lsl, headset: MS.headset, delivery_ms: summary && summary.marker_delivery_ms_max });
    if (result.completed && MS.mode === 'full') state.checks[1] = true;
    MS.view = 'done';
    if (window.SL) SL.poll();
    if (current === 'tasks' && state.role === 'patient') paint(); else render();
  }

  function complete() {
    if (MS.view !== 'running') return;
    MS.view = 'stopping';
    teardown();
    finish({ completed: true, cues_shown: MS.shown, camera: MS.camera ? cameraSummary(MS.cues) : null });
  }

  function abort(reason) {
    if (MS.view !== 'running') return;
    if (MS.ended) return complete();       // session_end already sent: the session is complete
    MS.view = 'stopping';
    teardown();
    sendMarker(MS.sched.markers.aborted, null);
    finish({ completed: false, aborted_reason: reason, cues_shown: MS.shown, camera: null });
  }

  // ------------------------------------------------------------ text used by recordings / profile / download
  window.sessionText = r => {
    if (!r || r.kind !== 'session') return 'No session included.';
    const what = r.mode === 'full' ? 'Cued hand movement session (move and imagine, left and right)' : 'Rehearsal session (too short to score)';
    const status = r.completed ? `${r.cues_shown} of ${r.total} cues shown` : `Stopped after ${r.cues_shown} of ${r.total} cues — ${r.aborted_reason || 'incomplete'}`;
    const cam = r.camera ? `\nCamera check: MOVE cues with little movement ${r.camera.move_low}/${r.camera.move_n}; IMAGINE cues with movement ${r.camera.imagine_moved}/${r.camera.imagine_n}` : '';
    return `${what}\n${status}\nSession ID: ${r.session_id}${cam}\nEEG results are reviewed by the care team.`;
  };
})();
