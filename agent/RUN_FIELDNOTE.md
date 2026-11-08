# Run Fieldnote + the NeuroSym agent

The UI is `fieldnote-ui/fieldnote/dist/`. The agent server is `neurosym-agent/fieldnote_server.py`
(it serves the UI and the three agent endpoints on one port, so no CORS setup).

## Quick start (template mode — no API key needed)

```bash
cd /Users/rainadu/WorkBuddy/Hackathon/neurosym-agent
source .venv/bin/activate
python fieldnote_server.py
# open http://localhost:8080
```

Works fully offline: the story summary, guide chat and task feedback use deterministic
templates. The badge in "My story" will say "AI template mode · no API key set".

## Live AI mode (Anthropic API key)

1. Get a key at https://console.anthropic.com/ (Settings → API keys → Create key; starts with `sk-ant-`).
2. Then either:

   **One-off (this terminal session):**
   ```bash
   cd /Users/rainadu/WorkBuddy/Hackathon/neurosym-agent
   source .venv/bin/activate
   export ANTHROPIC_API_KEY="sk-ant-...your key..."
   python fieldnote_server.py
   ```

   **Persistent (recommended):** add the key once to your shell profile, then it is always set:
   ```bash
   echo 'export ANTHROPIC_API_KEY="sk-ant-...your key..."' >> ~/.zshrc
   source ~/.zshrc
   ```
   (Only add it to `~/.zshrc` on your own machine — never commit it anywhere, never put it in the zip.)

3. Restart the server. `http://localhost:8080/api/health` should return `{"ok": true, "llm": true}`
   and the badge should say "AI guide connected".

## What the agent adds to Fieldnote

- **My story**: after the 4 guided questions, the agent drafts a pre-visit account from the
  patient's answers (patient's own words, nothing added, no diagnosis language). The patient
  still reviews/edits/confirms it — generation and confirmation stay separate. An "Ask the
  guide" chat answers process questions only (it refuses medical interpretation).
- **Movement tasks**: after each recording, the assistant computes stats from the tap timestamps
  (taps, mean interval, consistency, per-hand comparison) and gives descriptive feedback. It can
  only use numbers computed from the recording and cannot make clinical claims — both are
  enforced server-side by guardrails; anything violating them falls back to templates.

## Files changed in the UI

- `dist/agent-client.js` — new; detects the server, exposes `aiCall()`
- `dist/index.html` — loads agent-client.js before app.js
- `dist/app.js` — story summary draft + guide chat + task feedback hooks (11 small patches)
- `dist/editorial.css` — chat styles

Everything else in Fieldnote is untouched, and if the server is unreachable the UI falls back to
the original scripted behavior.

## Audit

Every agent interaction is appended to `neurosym-agent/audit/fieldnote_log.jsonl`
(input, output, source llm/template, guardrails result). Good material for the presentation.
