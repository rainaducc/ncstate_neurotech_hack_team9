"""Cued movement session: protocol parity with the model's live experiment, and the marker bridge."""
import hashlib
import os
import sys
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import live_protocol as P  # noqa: E402
import movement_session as S  # noqa: E402

UPSTREAM = Path(os.environ.get("NEUROSYM_AGENT_DIR",
                               Path.home() / "Downloads/neurosym/agent-latest/neurosym-agent")) / "live/protocol.py"


@pytest.mark.skipif(not UPSTREAM.exists(), reason="neurosym-agent checkout not present")
def test_vendored_protocol_is_byte_identical_to_upstream():
    sha = lambda p: hashlib.sha256(Path(p).read_bytes()).hexdigest()
    assert sha(ROOT / "live_protocol.py") == sha(UPSTREAM), "re-copy neurosym-agent/live/protocol.py"


@pytest.mark.parametrize("mode,per_cell", [("full", 16), ("rehearsal", 6)])
def test_schedule_matches_model_task(mode, per_cell):
    s = S.schedule(mode, seed=7)
    cells = Counter((r["condition"], t["hand"]) for r in s["runs"] for t in r["trials"])
    assert set(cells) == {(c, h) for c in ("execute", "imagine") for h in ("left", "right")}
    assert set(cells.values()) == {per_cell}
    assert s["cue_s"] >= 2.0                                   # model needs >= 2 s of cue
    assert all(t["rest_after_s"] >= 1.0 for r in s["runs"] for t in r["trials"])  # >= 1 s baseline
    for r in s["runs"]:                                        # max 3 same hand in a row
        hands, run = [t["hand"] for t in r["trials"]], 1
        for a, b in zip(hands, hands[1:]):
            run = run + 1 if a == b else 1
            assert run <= P.MAX_SAME_IN_A_ROW
    for (c, h), n in cells.items():                            # ordinals 0..n-1 per condition x hand
        ords = [t["ordinal"] for r in s["runs"] if r["condition"] == c for t in r["trials"] if t["hand"] == h]
        assert ords == list(range(n))
    assert bool(s["rest_blocks"]) == (mode == "full")


def test_schedule_is_identical_to_cue_runner_for_same_seed():
    ours = [(r["condition"], t["hand"], t["ordinal"], t["rest_after_s"], t["marker"])
            for r in S.schedule("full", 11)["runs"] for t in r["trials"]]
    theirs = [(c, t.hand, t.ordinal, t.rest_after_s, t.marker)
              for (c, _), block in P.build_schedule(seed=11) for t in block]
    assert ours == theirs


def test_markers_are_parsed_by_the_live_scorer_format():
    for r in S.schedule("rehearsal", 3)["runs"]:
        for t in r["trials"]:
            assert P.parse_cue(t["marker"]) == (r["condition"], t["hand"], t["ordinal"])


def test_session_rejects_unknown_markers_and_stamps_at_paint_time(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "SESSIONS", tmp_path)
    monkeypatch.setattr(S, "outlet", lambda: None)             # never open a real marker stream in tests
    j = S.start("rehearsal", 5)
    sid = j["session_id"]
    with pytest.raises(ValueError):
        S.marker(sid, "cue|observe|left|0", None)              # not in the schedule
    with pytest.raises(KeyError):
        S.marker("../etc", "session_start", None)
    now = S.clock()
    S.set_sync(sid, offset_s=now - 100.0, rtt_ms=1.0)          # client clock = LSL - offset
    first = j["schedule"]["runs"][0]["trials"][0]["marker"]
    out = S.marker(sid, first, client_ms=99_950.0)             # painted 50 ms before "now"
    assert out["lsl_time"] == pytest.approx(now - 0.05, abs=0.02)
    future = S.marker(sid, S.schedule("rehearsal", 5)["markers"]["cue_end"], client_ms=200_000.0)
    assert future["lsl_time"] <= S.clock()                     # never stamped in the future
    S.finish(sid, {"completed": False, "aborted_reason": "test", "cues_shown": 1, "camera": None,
                   "ignored": "x"})
    saved = (tmp_path / f"{sid}.json").read_text()
    assert '"ignored"' not in saved and first in saved
    assert (tmp_path / f"{sid}_cue_log.csv").read_text().startswith("marker,lsl_time")


def test_full_session_refuses_without_marker_stream_and_rehearsal_never_pushes(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "SESSIONS", tmp_path)
    monkeypatch.setattr(S, "outlet", lambda: None)
    with pytest.raises(ValueError):
        S.start("full", 1)                                     # a full session must reach the scorer
    j = S.start("rehearsal", 1)
    assert j["pushes_markers"] is False
    out = S.marker(j["session_id"], j["schedule"]["markers"]["session_start"], None)
    assert out["pushed"] is False


def test_finish_is_idempotent_and_rejects_later_markers(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "SESSIONS", tmp_path)
    monkeypatch.setattr(S, "outlet", lambda: None)
    sid = S.start("rehearsal", 2)["session_id"]
    S.finish(sid, {"completed": True, "cues_shown": 24})
    S.finish(sid, {"completed": False, "aborted_reason": "late"})
    assert S._sessions[sid]["result"]["completed"] is True
    with pytest.raises(ValueError):
        S.marker(sid, "session_aborted", None)
    with pytest.raises(ValueError):
        S.set_sync(sid, float("nan"), 1.0)
