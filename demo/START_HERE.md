# Team 9 — NeuroSym × Fieldnote demo package

Everything needed to understand, run, and present the prototype.

## Contents
- `DEMO_SCRIPT.md` — the 3-act presenter walkthrough (patient story → movement tasks → clinician app), with talking points and an emergency-fallback table.
- `README.md` — what the clinician copilot agent is and the hard rules it enforces.
- `RUN_FIELDNOTE.md` — how to run the patient-facing Fieldnote UI + agent server (API key setup included).
- `screenshots/` — 12 screenshots from the live rehearsal (2026-11-08), in demo order:
  | File | Stage |
  |---|---|
  | demo_01_intro.png | Fieldnote landing |
  | demo_02_story_q1.png | Story Q1 |
  | demo_03_story_complete.png | AI-drafted summary (after 4 questions) |
  | demo_04_guide_chat.png | "Ask the guide" live chat |
  | demo_05_confirmed.png | Patient confirms summary |
  | demo_06_tasks_start.png | Movement task start |
  | demo_07/08_*_feedback.png | Left/right hand recording |
  | demo_09_task_feedback.png | Live-LLM recording-assistant feedback |
  | demo_10_recordings.png / demo_11_share.png | Recordings + share views |
  | demo_12_clinician.png | Streamlit clinician app |
- `audit_evidence/` — real audit logs from the rehearsal:
  - `fieldnote_log.jsonl` — every patient-side agent call (story summary, guide chat, task feedback) with timestamps, source (llm/template), and guardrail result.
  - `agent_log.jsonl` — clinician-side copilot calls with tool use and guardrail checks.
- `test_evidence.txt` — full test suite result at rehearsal time: **15 passed in 1.07s** (8 contract tests from the handoff + 7 agent/app tests).

## Quick start for teammates
```bash
git clone https://github.com/rainaducc/ncstate_neurotech_hack_team9
cd ncstate_neurotech_hack_team9/agent
python3 -m venv .venv && source .venv/bin/activate
pip install jsonschema pytest anthropic streamlit plotly
pytest -q                                        # 15 tests
python fieldnote_server.py                       # Fieldnote UI → localhost:8080
streamlit run app.py                             # clinician app → localhost:8501
```
Optional live LLM: `export ANTHROPIC_API_KEY=sk-ant-...` (starts with sk-ant-, from console.anthropic.com). Without a key, everything runs in deterministic template mode — the demo still works.
