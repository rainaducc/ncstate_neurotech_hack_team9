"""Shared communication layer: consent-scoped sharing, role-scoped thread, unread counts."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import shared_store as S  # noqa: E402

PID = S.DEMO_PATIENT["id"]


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    S.use_db(tmp_path / "t.db")
    yield
    S.use_db(S.ROOT / "data" / "fieldnote.db")


def test_profile_visible_to_clinician_only_while_shared():
    assert S.patient_state(PID, "clinician")["share"] is None
    with pytest.raises(ValueError):
        S.share_profile(PID, {"summary": "  "})                 # nothing confirmed, nothing shared
    S.share_profile(PID, {"name": "Jordan M.", "summary": "My right hand feels slower.", "extra": "dropped"})
    share = S.patient_state(PID, "clinician")["share"]
    assert share["snapshot"]["summary"] == "My right hand feels slower."
    assert "extra" not in share["snapshot"]
    S.withdraw_profile(PID)
    assert S.patient_state(PID, "clinician")["share"] is None
    kinds = [m["kind"] for m in S.messages(PID, "patient")]
    assert kinds == ["profile_shared", "profile_withdrawn"]


def test_patient_never_sees_report_label_or_ids():
    S.link_report(PID, "FNFULL_1", "OUTSIDE_NORMATIVE_RANGE_REVIEW", simulated=True)
    S.link_report(PID, "FNFULL_1", "OUTSIDE_NORMATIVE_RANGE_REVIEW", simulated=True)   # idempotent
    patient = S.messages(PID, "patient")
    clinician = S.messages(PID, "clinician")
    assert len(patient) == 1 and len(clinician) == 2
    text = " ".join(m["body"] + str(m["attachment"]) for m in patient)
    assert "OUTSIDE" not in text and "FNFULL_1" not in text
    assert "reports" not in S.patient_state(PID, "patient")
    assert S.patient_state(PID, "clinician")["reports"] == ["FNFULL_1"]


def test_unread_counts_other_side_and_system_only():
    S.post_message(PID, "patient", "Hello, is Thursday still on?")
    assert S.unread(PID, "clinician") == 1 and S.unread(PID, "patient") == 0
    m = S.post_message(PID, "clinician", "Yes, see you at 10.")
    assert S.unread(PID, "patient") == 1
    S.mark_read(PID, "patient", m["id"])
    S.mark_read(PID, "patient", 0)                              # read pointer never moves backwards
    assert S.unread(PID, "patient") == 0
    assert S.messages(PID, "patient", after_id=m["id"]) == []


def test_session_link_posts_once_and_validates_input():
    summary = {"session_id": "FN_x", "mode": "full", "cues_marked": 64, "result": {"completed": True}}
    S.link_session(PID, "FN_x", summary)
    S.link_session(PID, "FN_x", summary)
    assert [m["kind"] for m in S.messages(PID, "patient")] == ["session"]
    with pytest.raises(KeyError):
        S.post_message("P-9999", "patient", "hi")
    with pytest.raises(KeyError):
        S.post_message("../x", "patient", "hi")
    with pytest.raises(ValueError):
        S.post_message(PID, "admin", "hi")
    with pytest.raises(ValueError):
        S.post_message(PID, "patient", "   ")
    assert len(S.post_message(PID, "patient", "x" * 10_000)["body"]) == S.MAX_BODY


def test_own_system_events_are_not_unread_and_oversized_share_is_rejected_cleanly():
    S.share_profile(PID, {"summary": "ok"})
    assert S.unread(PID, "patient") == 0 and S.unread(PID, "clinician") == 1
    S.link_report(PID, "R1", "WITHIN_NORMATIVE_RANGE", simulated=False)
    assert S.unread(PID, "clinician") == 1 and S.unread(PID, "patient") == 1
    with pytest.raises(ValueError):
        S.share_profile(PID, {"summary": "x" * 300_000})
    assert S.active_share(PID)["snapshot"]["summary"] == "ok"   # the earlier share is untouched
    with pytest.raises(ValueError):
        S.messages(PID, "patient", after_id=2**70)
