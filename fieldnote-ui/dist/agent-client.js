// agent-client.js — connects Fieldnote to the NeuroSym agent server (fieldnote_server.py).
// Loaded before app.js. If the server is unreachable (e.g. opened as file://),
// window.AI.connected stays false and the UI keeps its original scripted behavior.
window.AI = { connected: false, llm: false };

window.aiCall = async (path, body) => {
  try {
    const r = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {})
    });
    if (!r.ok) throw new Error(r.status);
    return await r.json();
  } catch (e) {
    return null;
  }
};

(async () => {
  try {
    const r = await fetch('/api/health');
    const j = await r.json();
    window.AI.connected = !!j.ok;
    window.AI.llm = !!j.llm;
    if (window.AI.connected && typeof render === 'function') render();
  } catch (e) { /* keep scripted mode */ }
})();
