import json, sys
from pathlib import Path
import pytest
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from report_adapter import ANSWERS, check_text, load_report, validate
from offline_summary import summary

MOCKS = sorted((ROOT / "mocks").glob("*.json"))
# drop real model sample reports into contract/samples/ - they get tested automatically
SAMPLES = sorted((ROOT / "contract" / "samples").glob("*.json"))


@pytest.mark.parametrize("path", MOCKS + SAMPLES, ids=lambda p: p.name)
def test_report_loads_and_summary_passes_guardrails(path):
    r = load_report(path)
    assert r.answer in ANSWERS
    assert r.features, "no features"
    assert check_text(summary(r), r) == []


@pytest.mark.parametrize("path", MOCKS, ids=lambda p: p.name)
def test_mocks_match_contract(path):
    assert validate(path) == []


def test_old_format_is_adapted():
    old = {"participant": "S1", "decision": "SUGGEST SPECIALIST", "qc": "PASS",
           "features": {"sym_mu_execute": {"value": 1.5, "z": 3.1}, "li": 0.2},
           "deviation_score": 6.1, "p_value": 0.01, "guarantee": "5% max false review", "limitations": "prototype"}
    r = load_report(old)
    assert r.answer == "OUTSIDE_NORMATIVE_RANGE_REVIEW" and r.recording_id == "S1"
    assert r.feature("sym_mu_execute").z == 3.1 and r.scores["combined"]["p"] == 0.01
    assert r.adapter_notes


def test_guardrails_catch_problems():
    r = load_report(MOCKS[0])
    bad = ("Research prototype - not a diagnosis - for clinician review. "
           f"{r.answer}. The patient has early Parkinson's; asymmetry z = 4.7.")
    probs = check_text(bad, r)
    assert any("diagnostic" in p for p in probs) and any("4.7" in p for p in probs)
    assert any("label" in p for p in check_text("no label, no header", r))
