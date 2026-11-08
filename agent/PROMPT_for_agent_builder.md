# Prompt for the teammate building the agent (Claude Code, inside this agent_kit folder)

Build the NeuroSym clinician copilot agent + clinician web app in this folder. Read README.md first.
HARD RULES:
- Read model reports ONLY through report_adapter.load_report() (Report/Feature objects). Never index
  raw JSON in agent/app code. Never touch raw EEG.
- Every text the agent shows must pass report_adapter.check_text(text, report). If it fails, regenerate
  once with the violations listed; if it still fails, fall back to offline_summary.summary(report).
- Keep tests/test_contract.py passing; add tests for agent + app.
AGENT (agent.py): Anthropic SDK tool calling (key from ANTHROPIC_API_KEY; offline_summary fallback).
Tools (all read from Report): get_report(id), explain_flag(feature_name), compare_visits(id_a, id_b),
list_confounds(id), draft_summary(id) -> editable text. System prompt: clinician copilot; answer labels
verbatim; never diagnose or name a disease as the cause; always mention data quality, confounds,
limitations and the guarantee note; for INCONCLUSIVE_REPEAT explain why and what to fix; only numbers
from the report; header "Research prototype - not a diagnosis - for clinician review". Log every
interaction (prompt, tool calls, output, check_text result) to audit/agent_log.jsonl.
APP (app.py, Streamlit): pick a report from mocks/ or contract/samples/ (or upload) -> answer banner
(color by answer), data quality panel, plots (each flagged feature's z on a normal curve with "this
recording" marker; left hemisphere (right hand) vs right hemisphere (left hand) bar chart; three-state
profile if present), longitudinal changes, confounds, copilot chat panel, editable draft summary +
Accept button (saved to audit/), export HTML report. Show adapter notes/unknown fields in a small
"model compatibility" expander.
Tests: agent outputs pass check_text on all 3 mocks; app loads each mock without errors
(streamlit.testing AppTest). README with run commands. Show me the app on the 3 mocks.
