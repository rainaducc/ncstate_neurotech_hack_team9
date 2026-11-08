"""Guardrails must accept normal good answers and reject harmful ones (both directions matter:
false rejections push the LLM into template fallbacks)."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from report_adapter import HEADER, check_text, load_report  # noqa: E402

H = HEADER + "."
O = load_report(ROOT / "mocks/outside_weak_left.json")
I = load_report(ROOT / "mocks/inconclusive.json")
W = load_report(ROOT / "mocks/within_range.json")

GOOD = [
    (O, f"{H}\nOUTSIDE_NORMATIVE_RANGE_REVIEW\n\n1. Symmetry z = 3.1.\n2. Left hemisphere z = -2.9.\n3. Data quality PASS.\n4. Levodopa noted.\n5. Done."),
    (O, f"{H}\nOUTSIDE_NORMATIVE_RANGE_REVIEW\nFeatures beyond |z| 1.96 are shown as outside the typical range."),
    (O, f"{H}\nOUTSIDE_NORMATIVE_RANGE_REVIEW\nThe C3 response (left hemisphere) is weaker than C4; electrodes follow the 10-20 system."),
    (O, f"{H}\nOUTSIDE_NORMATIVE_RANGE_REVIEW\nIf you have time, consider repeating at the same time relative to the levodopa dose."),
    (W, f"{H}\nWITHIN_NORMATIVE_RANGE\n{W.guarantee_note}"),
    (I, f"{H}\nINCONCLUSIVE_REPEAT\nReseat C4 and P8, then repeat. Too few clean trials for left-hand imagery."),
]
BAD = [
    (O, f"{H}\nOUTSIDE_NORMATIVE_RANGE_REVIEW\nThis pattern could point to early PD.", "condition"),
    (O, f"{H}\nOUTSIDE_NORMATIVE_RANGE_REVIEW\nThe asymmetry suggests Parkinson disease.", "condition"),
    (O, f"{H}\nOUTSIDE_NORMATIVE_RANGE_REVIEW\nFindings are compatible with parkinsonism.", "condition"),
    (O, f"{H}\nOUTSIDE_NORMATIVE_RANGE_REVIEW\nOverall this is WITHIN_NORMATIVE_RANGE.", "another answer label"),
    (O, f"{H}\nOUTSIDE_NORMATIVE_RANGE_REVIEW\nPercentile 99.9 means a 99.9 chance of illness.", "probability"),
    (I, f"{H}\nINCONCLUSIVE_REPEAT\nThe right hemisphere is clearly impaired.", "judgment"),
    (W, f"{H}\nWITHIN_NORMATIVE_RANGE\nThe patient is healthy and there is no need for follow-up.", "reassurance"),
    (O, f"{H}\nOUTSIDE_NORMATIVE_RANGE_REVIEW\nAsymmetry z = 4.7.", "number not in report"),
    (O, f"{H}\nOUTSIDE_NORMATIVE_RANGE_REVIEW\nThe review cutoff is p < 0.05.", "number not in report"),
    (O, "OUTSIDE_NORMATIVE_RANGE_REVIEW only", "header"),
]


@pytest.mark.parametrize("report,text", GOOD)
def test_good_answers_pass(report, text):
    assert check_text(text, report) == []


@pytest.mark.parametrize("report,text,kind", BAD)
def test_bad_answers_fail(report, text, kind):
    probs = check_text(text, report)
    assert any(kind in p for p in probs), probs
