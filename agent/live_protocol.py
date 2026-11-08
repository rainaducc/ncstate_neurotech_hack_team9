"""
protocol.py - the NeuroSym demo experiment, matched to the model's training task (PhysioNet EEGMMIDB).

Model input = one row per trial and band: condition (execute|imagine), hand (left|right), ordinal,
half (odd|even), band (mu|beta), c3_erd_db, c4_erd_db. So the experiment must contain real and imagined
LEFT and RIGHT hand movements with a clean 1 s pre-cue baseline and a cue of at least 2 s.

Session (about 11 min of recording):
  rest eyes open 60 s, rest eyes closed 60 s,
  4 runs: execute A, imagine A, execute B, imagine B; each run = 8 left + 8 right trials, shuffled
  (max 3 same hand in a row), cue 4 s (open/close the fist until the cue disappears), rest 4.0-4.5 s.
=> 16 trials per hand per condition (PhysioNet has about 22).

Marker strings (sent over LSL and logged): see MARKERS below.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

CUE_S = 4.0
REST_S = (4.0, 4.5)          # jittered rest; the model's baseline is the 1 s before each cue
REST_EO_S = 60.0
REST_EC_S = 60.0
RUNS = (("execute", "A"), ("imagine", "A"), ("execute", "B"), ("imagine", "B"))
SCORED = ("execute", "imagine")          # the only conditions the model has a healthy reference for
# Optional exploratory arm blocks: same C3/C4 measurement, shown live and summarized in the report,
# but NOT scored by the model (no healthy reference exists for them).
ARM_TASKS = {
    "reach": "Reach forward with the arm shown, grasp the cup, bring it to your mouth, put it back.",
    "wrist": "Bend the wrist shown up and down steadily, forearm resting on the table.",
    "elbow": "Bend the elbow shown to bring the hand to the shoulder and back, slowly, upper arm still.",
}
TRIALS_PER_HAND_PER_RUN = 8
MAX_SAME_IN_A_ROW = 3

MARKERS = {
    "session_start": "session_start",
    "rest_eo": "rest_eyes_open",          # + _start / _end
    "rest_ec": "rest_eyes_closed",
    "run": "run_{condition}_{run}",        # + _start / _end
    "cue": "cue|{condition}|{hand}|{ordinal}",   # trial onset; ordinal counts per condition x hand
    "cue_end": "cue_end",
    "session_end": "session_end",
}


@dataclass
class Trial:
    condition: str
    run: str
    hand: str
    ordinal: int          # 0-based presentation order within condition x hand, across both runs
    rest_after_s: float

    @property
    def half(self):
        return "even" if self.ordinal % 2 == 0 else "odd"

    @property
    def marker(self):
        return MARKERS["cue"].format(condition=self.condition, hand=self.hand, ordinal=self.ordinal)


def _hand_order(n_per_hand, rng):
    while True:
        seq = ["left"] * n_per_hand + ["right"] * n_per_hand
        rng.shuffle(seq)
        run, ok = 1, True
        for a, b in zip(seq, seq[1:]):
            run = run + 1 if a == b else 1
            ok &= run <= MAX_SAME_IN_A_ROW
        if ok:
            return seq


def build_schedule(seed=7, trials_per_hand_per_run=TRIALS_PER_HAND_PER_RUN, runs=RUNS, arm=()):
    """arm: names from ARM_TASKS appended as one exploratory run each (after the scored runs)."""
    runs = tuple(runs) + tuple((task, "X") for task in arm)
    rng = random.Random(seed)
    counters, out = {}, []
    for condition, run in runs:
        block = []
        for hand in _hand_order(trials_per_hand_per_run, rng):
            k = (condition, hand)
            counters[k] = counters.get(k, 0)
            block.append(Trial(condition, run, hand, counters[k], round(rng.uniform(*REST_S), 3)))
            counters[k] += 1
        out.append(((condition, run), block))
    return out


def parse_cue(marker: str):
    """'cue|execute|left|3' -> ('execute', 'left', 3) or None."""
    parts = marker.split("|")
    if len(parts) == 4 and parts[0] == "cue":
        return parts[1], parts[2], int(parts[3])
    return None


def session_seconds(schedule, rest=True):
    t = (REST_EO_S + REST_EC_S) if rest else 0.0
    for _, block in schedule:
        t += 3.0 + sum(CUE_S + tr.rest_after_s for tr in block)   # 3 s run intro
    return t
