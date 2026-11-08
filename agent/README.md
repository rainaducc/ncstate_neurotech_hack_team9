# NeuroSym Clinician Copilot — agent kit (installed)

Built from `PROMPT_for_agent_builder.md` inside the NeuroSym handoff (Team 9 neurotech hackathon).

## What's here
- `report_adapter.py` — the ONLY way agent/app read model reports (Report/Feature objects, ALIASES normalization, `check_text()` guardrails, `validate()` vs contract schema).
- `offline_summary.py` — template clinician summary, always passes guardrails.
- `agent.py` — clinician copilot. Anthropic tool calling (`get_report`, `explain_flag`, `compare_visits`, `list_confounds`, `draft_summary`) when `ANTHROPIC_API_KEY` is set; otherwise a rule-based offline mode. Every output passes `check_text()` (header + answer label verbatim, no diagnostic language, only report numbers); LLM output failing guardrails is regenerated once, then falls back to offline. All interactions logged to `audit/agent_log.jsonl`.
- `app.py` — Streamlit clinician app: answer banner (color by answer), data-quality panel, flagged-feature z-curves on the healthy reference, hemisphere bar chart, three-state profile, longitudinal changes, confounds, copilot chat, editable draft summary + Accept (saves to `audit/accepted_summaries.jsonl`), HTML export, model-compatibility expander.
- `tests/` — contract tests (8, from the handoff) + agent/app tests (7): 15 total, all passing.

## Run
```bash
source .venv/bin/activate        # venv already created with jsonschema pytest anthropic streamlit plotly
streamlit run app.py             # clinician app
python agent.py                  # interactive copilot REPL over the mocks
python agent.py mocks/outside_weak_left.json "Explain the flagged features"
pytest -q                        # 15 passed
```

## When the model team sends new reports
1. Copy new samples into `contract/samples/` (new schema → `contract/`).
2. `python check_new_report.py contract/samples/*.json` — add mappings to ALIASES/_normalize in `report_adapter.py` if needed.
3. `pytest -q` must pass. Agent + app code unchanged.

## Hard rules (enforced in code)
- Never index raw model JSON in agent/app code; never touch raw EEG.
- Never diagnose or name a disease as the cause; header "Research prototype - not a diagnosis - for clinician review" is mandatory.
- Answer labels verbatim: WITHIN_NORMATIVE_RANGE / INCONCLUSIVE_REPEAT / OUTSIDE_NORMATIVE_RANGE_REVIEW.
