"""
movement_session.py - runs the NeuroSym cued movement session from the NeuroBridge UI.

The browser shows the cues; this module supplies the trial schedule and turns each cue into an LSL
marker on the same stream the model's live scorer listens to (cue_runner.py's "NeuroSymMarkers").
So live_score.py (neurosym-agent/live) scores a NeuroBridge session exactly like a cue_runner session.

Schedule and marker strings come from live_protocol.py, a byte-identical copy of
neurosym-agent/live/protocol.py (sha256 checked in tests/test_session.py).

Marker timing: the browser timestamps each cue with performance.now() in the animation frame after the
cue is drawn. /api/session/sync estimates the offset between that clock and LSL's local_clock() from
the lowest-round-trip ping, so markers carry the time the cue appeared, not the time the HTTP request
arrived. The offset and its round trip are logged with the session.

Nothing here computes EEG features or scores. Session logs (schedule, markers, camera QC counts) are
written to sessions/<session_id>.json and sessions/<session_id>_cue_log.csv.
"""
from __future__ import annotations

import csv
import itertools
import json
import math
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import live_protocol as P

ROOT = Path(__file__).resolve().parent
SESSIONS = ROOT / "sessions"
STREAM_NAME, STREAM_TYPE, SOURCE_ID = "NeuroSymMarkers", "Markers", "neurosym-cues"   # = cue_runner.py
# A rehearsal is practice only: its markers are logged but NOT pushed to LSL. Its marker strings are
# identical to a real session's, so live_score.py would score it as FINAL and exit before the real one.
MODES = {"full": {"trials_per_hand_per_run": P.TRIALS_PER_HAND_PER_RUN, "rest": True, "push": True},
         "rehearsal": {"trials_per_hand_per_run": 3, "rest": False, "push": False}}  # = cue_runner --short
_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_lock = threading.Lock()
_outlet = None
_lsl_error = None
_sessions: dict[str, dict] = {}
_seq = itertools.count(1)

try:
    import pylsl
except Exception as e:  # pragma: no cover - depends on the machine
    pylsl, _lsl_error = None, f"pylsl unavailable: {e}"


def clock() -> float:
    return pylsl.local_clock() if pylsl else time.monotonic()


def outlet():
    """The server's marker outlet (opened by open_outlet() at server start), or None."""
    return _outlet


def open_outlet() -> None:
    """Open the marker outlet once, at server start, so live_score.py can connect before a session.
    Refuses if another NeuroSymMarkers stream (e.g. cue_runner.py) is already on the network."""
    global _outlet, _lsl_error
    if _outlet is not None or pylsl is None:
        return
    if pylsl.resolve_byprop("name", STREAM_NAME, timeout=0.5):
        _lsl_error = f"another {STREAM_NAME} stream is running (cue_runner.py?); stop it and restart this server"
        return
    try:
        _outlet = pylsl.StreamOutlet(pylsl.StreamInfo(STREAM_NAME, STREAM_TYPE, 1, 0, "string", SOURCE_ID))
        _lsl_error = None
    except Exception as e:  # pragma: no cover
        _lsl_error = f"could not open LSL outlet: {e}"


def competing_streams() -> int:
    """Number of NeuroSymMarkers streams on the network that are not this server's outlet."""
    if pylsl is None:
        return 0
    mine = _outlet.get_info().uid() if _outlet is not None else None
    return sum(1 for i in pylsl.resolve_byprop("name", STREAM_NAME, timeout=0.5) if i.uid() != mine)


# ------------------------------------------------------------------ schedule
def schedule(mode: str = "full", seed: int = 7) -> dict:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {sorted(MODES)}")
    m = MODES[mode]
    sched = P.build_schedule(seed=seed, trials_per_hand_per_run=m["trials_per_hand_per_run"])
    runs = [{"condition": c, "run": r,
             "trials": [{"hand": t.hand, "ordinal": t.ordinal, "rest_after_s": t.rest_after_s,
                         "marker": t.marker} for t in block]}
            for (c, r), block in sched]
    return {
        "mode": mode, "seed": seed, "protocol": "neurosym-agent/live/protocol.py",
        "cue_s": P.CUE_S, "run_intro_s": 3.0, "countdown_s": 5,
        "rest_blocks": ([{"key": "rest_eo", "marker": P.MARKERS["rest_eo"], "seconds": P.REST_EO_S},
                         {"key": "rest_ec", "marker": P.MARKERS["rest_ec"], "seconds": P.REST_EC_S}]
                        if m["rest"] else []),
        "markers": {"session_start": P.MARKERS["session_start"], "session_end": P.MARKERS["session_end"],
                    "cue_end": P.MARKERS["cue_end"], "run": P.MARKERS["run"], "aborted": "session_aborted"},
        "runs": runs,
        "total_s": round(P.session_seconds(sched, rest=m["rest"]), 1),
        "trials_per_hand_per_condition": m["trials_per_hand_per_run"] * 2,
    }


def allowed_markers(sched: dict) -> set[str]:
    mk = sched["markers"]
    out = {mk["session_start"], mk["session_end"], mk["cue_end"], mk["aborted"]}
    for b in sched["rest_blocks"]:
        out |= {b["marker"] + "_start", b["marker"] + "_end"}
    for run in sched["runs"]:
        base = mk["run"].format(condition=run["condition"], run=run["run"])
        out |= {base + "_start", base + "_end"} | {t["marker"] for t in run["trials"]}
    return out


# ------------------------------------------------------------------ devices
def devices(timeout: float = 1.0) -> dict:
    """EEG streams visible on the network, whether data is flowing, and whether a scorer is listening."""
    o = outlet()
    out = {"lsl": o is not None, "lsl_error": _lsl_error, "eeg": [], "marker_stream": STREAM_NAME,
           "scorer_listening": bool(o and o.have_consumers()), "competing_marker_streams": 0}
    if pylsl is None:
        return out
    out["competing_marker_streams"] = competing_streams()
    for info in pylsl.resolve_byprop("type", "EEG", timeout=timeout):
        labels, flowing = [], False
        try:
            inlet = pylsl.StreamInlet(info, max_buflen=2)
            ch = inlet.info(timeout=1.0).desc().child("channels").child("channel")  # full info has labels
            while not ch.empty() and len(labels) < 512:
                labels.append(ch.child_value("label"))
                ch = ch.next_sibling()
            inlet.open_stream(timeout=1.0)
            deadline = time.monotonic() + 0.6
            while time.monotonic() < deadline and not flowing:
                _, ts = inlet.pull_chunk(timeout=0.2)
                flowing = bool(ts)
            inlet.close_stream()
        except Exception:
            flowing = False
        out["eeg"].append({"name": info.name(), "channels": info.channel_count(),
                           "srate": info.nominal_srate(), "has_c3_c4": {"C3", "C4"} <= set(labels),
                           "data_flowing": flowing})
    return out


# ------------------------------------------------------------------ session lifecycle
def start(mode: str, seed: int, meta: dict | None = None) -> dict:
    sched = schedule(mode, seed)
    push = MODES[mode]["push"]
    if push:  # a full session must reach the scorer, through this server's stream only
        if outlet() is None:
            raise ValueError(_lsl_error or "LSL marker stream unavailable")
        if competing_streams():
            raise ValueError(f"another {STREAM_NAME} stream is running (cue_runner.py?); stop it first")
        if not outlet().have_consumers():
            raise ValueError("no live scorer is listening; start live_score.py, then check the headset again")
    sid = f"FN_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{next(_seq)}"
    s = {"session_id": sid, "started_utc": datetime.now(timezone.utc).isoformat(), "mode": mode,
         "seed": seed, "schedule_total_s": sched["total_s"], "lsl": push and outlet() is not None,
         "lsl_error": _lsl_error, "meta": meta or {}, "sync": None, "markers": [], "result": None,
         "_allowed": allowed_markers(sched)}
    with _lock:
        _sessions[sid] = s
    _save(s)
    return {"session_id": sid, "schedule": sched, "lsl": s["lsl"], "lsl_error": _lsl_error,
            "pushes_markers": s["lsl"]}


def sync_ping() -> dict:
    return {"lsl_time": clock()}


def set_sync(sid: str, offset_s: float, rtt_ms: float) -> None:
    s = _get(sid)
    if not (math.isfinite(offset_s) and math.isfinite(rtt_ms)) or rtt_ms < 0:
        raise ValueError("sync values must be finite")
    s["sync"] = {"offset_s": float(offset_s), "rtt_ms": float(rtt_ms)}
    _save(s)


def marker(sid: str, m: str, client_ms: float | None) -> dict:
    s = _get(sid)
    if m not in s["_allowed"]:
        raise ValueError(f"unknown marker {m!r} for this session")
    if s["result"] is not None:
        raise ValueError("session already finished")
    now = clock()
    if s["sync"] and client_ms is not None and math.isfinite(client_ms):
        t, how = float(client_ms) / 1000.0 + s["sync"]["offset_s"], "client_paint"
        if not (now - 5.0 <= t <= now):       # never in the future, never implausibly old
            t, how = now, "receipt_clamped"
    else:
        t, how = now, "receipt"
    o = outlet() if s["lsl"] else None
    if o is not None:
        o.push_sample([m], t)
    with _lock:
        s["markers"].append({"marker": m, "lsl_time": round(t, 6), "stamp": how,
                             "received_lsl_time": round(clock(), 6)})
    if m in ("session_end", "session_aborted"):
        _save(s)
    return {"ok": True, "lsl_time": t, "pushed": o is not None}


def finish(sid: str, result: dict) -> dict:
    s = _get(sid)
    if s["result"] is not None:          # first finish wins; a late abort cannot overwrite a completion
        return summary(s)
    keep = ("completed", "aborted_reason", "cues_shown", "camera")
    s["result"] = {k: result.get(k) for k in keep if k in result}
    s["finished_utc"] = datetime.now(timezone.utc).isoformat()
    _save(s)
    return summary(s)


def summary(s: dict) -> dict:
    cues = [m for m in s["markers"] if m["marker"].startswith("cue|")]
    lag = [m["received_lsl_time"] - m["lsl_time"] for m in cues if m["stamp"] == "client_paint"]
    return {"session_id": s["session_id"], "mode": s["mode"], "cues_marked": len(cues),
            "lsl": s["lsl"], "sync_rtt_ms": (s["sync"] or {}).get("rtt_ms"),
            "marker_delivery_ms_max": round(1000 * max(lag), 1) if lag else None,
            "result": s["result"]}


def _get(sid: str) -> dict:
    if not isinstance(sid, str) or not _ID.match(sid) or sid not in _sessions:
        raise KeyError(sid)
    return _sessions[sid]


def _save(s: dict) -> None:
    SESSIONS.mkdir(exist_ok=True)
    with _lock:
        data = {k: v for k, v in s.items() if not k.startswith("_")}
        (SESSIONS / f"{s['session_id']}.json").write_text(json.dumps(data, indent=1))
        with open(SESSIONS / f"{s['session_id']}_cue_log.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["marker", "lsl_time"])                        # same columns as cue_runner.py
            w.writerows([m["marker"], f"{m['lsl_time']:.6f}"] for m in s["markers"])
