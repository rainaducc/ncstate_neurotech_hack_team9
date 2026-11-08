# NeuroSym × Fieldnote — Team 9, NC State Neurotech Hack

An EEG-based normative-model system with a guarded AI copilot on **both sides of the clinic**:

- **Patient side (Fieldnote UI)** — a pre-visit profile: the patient tells their story in 4 guided questions, an AI guide drafts the account *in their own words* (the patient confirms every word), and a **cued movement session** with the ANT eego headset runs the model's own EEG task (move / imagine, left / right) and sends the cue markers to the model's live scorer. Patient and clinician share **one thread per patient** (messages, consent-scoped profile sharing, session and report updates) synced through the server. See `agent/RUN_FIELDNOTE.md`.
- **Clinician side (Streamlit app)** — the NeuroSym model report (WITHIN_NORMATIVE_RANGE / INCONCLUSIVE_REPEAT / OUTSIDE_NORMATIVE_RANGE_REVIEW) visualized on the normative reference, plus a tool-calling copilot that can only cite report numbers.

**Neither side lets the AI practice medicine.** All agent output passes `check_text()` guardrails: mandatory research-prototype header, answer labels verbatim, no diagnostic language, only numbers that appear in the report/recording. Violating LLM output is regenerated once, then falls back to deterministic templates. Every interaction is written to an audit log.

## Repo layout

```
agent/          Clinician copilot + patient-side agent server (Python)
  agent.py            Tool-calling copilot (get_report / explain_flag / compare_visits /
                      list_confounds / draft_summary) + offline fallback + audit log
  fieldnote_server.py Serves the Fieldnote UI + 3 agent endpoints (story summary,
                      guide chat, task feedback), port 8080
  app.py              Streamlit clinician app, port 8501
  report_adapter.py   The ONLY way code reads model reports (guardrails live here)
  contract/           Report JSON schema; drop new model samples in samples/
  mocks/ tests/       3 mock reports; 15 tests (8 contract + 7 agent/app)

fieldnote-ui/   Patient-facing pre-visit profile (vanilla JS SPA, served by fieldnote_server.py)
  dist/               Built UI incl. agent-client.js (the agent bridge)
  README.md           Original Fieldnote product spec

demo/           Presentation materials
  START_HERE.md       Teammate quick-start + evidence index
  DEMO_SCRIPT.md      3-act presenter walkthrough with fallback table
  screenshots/        12 screenshots from the 2026-11-08 live rehearsal
  test_evidence.txt   Full suite result at rehearsal time (15 passed)
```

## Run it

```bash
cd agent
python3 -m venv .venv && source .venv/bin/activate
pip install jsonschema pytest anthropic streamlit plotly pylsl

pytest -q                       # 28 passed
python fieldnote_server.py      # patient UI  → http://localhost:8080
streamlit run app.py            # clinician app → http://localhost:8501
```

Live LLM mode: `export ANTHROPIC_API_KEY=sk-ant-...` before starting the servers. Without a key everything runs in deterministic template mode — the demo still works end to end.

See `demo/START_HERE.md` and `agent/RUN_FIELDNOTE.md` for details.
