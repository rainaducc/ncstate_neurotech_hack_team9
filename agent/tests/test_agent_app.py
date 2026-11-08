"""Tests for agent.py + app.py: copilot outputs pass guardrails on all mocks; app loads each mock."""
import sys
from pathlib import Path
import pytest
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent import Copilot, available_reports, explain_flag, list_confounds, compare_visits
from report_adapter import check_text, load_report

MOCKS = sorted((ROOT / "mocks").glob("*.json"))


@pytest.mark.parametrize("path", MOCKS, ids=lambda p: p.name)
def test_copilot_answers_pass_guardrails(path):
    r = load_report(path)
    bot = Copilot(r)
    for q in ("Summarize this recording.", "What are the confounds?",
              "Explain the flagged features.", "Compare with the previous visit."):
        out = bot.ask(q)
        assert out["check_text_violations"] == [], (q, out["check_text_violations"], out["output"])


@pytest.mark.parametrize("path", MOCKS, ids=lambda p: p.name)
def test_app_loads_mock_without_errors(path):
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(ROOT / "app.py", default_timeout=60)
    at.session_state["forced_report"] = path
    at.run()
    assert not at.exception, at.exception


def test_registry_and_tools():
    reg = available_reports()
    assert reg, "no reports found in mocks/ or contract/samples/"
    r = load_report(reg[sorted(reg)[0]])
    assert check_text(explain_flag(r.features[0].name, r), r) == []
    assert check_text(list_confounds(r), r) == []
