"""
report_adapter.py - the ONLY way the agent/app reads NeuroSym model reports.

The model's JSON may change while it is being trained. The agent must never index the raw JSON
directly; it uses Report (below). When the model's format changes, update ALIASES / _normalize here,
run `python check_new_report.py <new_report.json>` and `pytest`, and the agent keeps working.

    from report_adapter import load_report, check_text
    r = load_report("report.json")
    r.answer, r.answer_reasons, r.data_quality, r.features, r.scores, r.confound_warnings, ...
    problems = check_text(agent_summary, r)    # guardrails: [] means OK
"""
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

CONTRACT_VERSION = "0.1"
ANSWERS = ("WITHIN_NORMATIVE_RANGE", "INCONCLUSIVE_REPEAT", "OUTSIDE_NORMATIVE_RANGE_REVIEW")
HEADER = "Research prototype - not a diagnosis - for clinician review"

# alternative key names the model might use -> canonical name (top level)
ALIASES = {
    "decision": "answer", "result": "answer", "classification": "answer", "label": "answer",
    "reasons": "answer_reasons", "answer_reason": "answer_reasons",
    "qc": "data_quality", "quality": "data_quality",
    "feature_table": "features", "feature_values": "features",
    "confounds": "confound_warnings", "warnings": "confound_warnings",
    "subject_id": "recording_id", "participant": "recording_id", "id": "recording_id",
    "contributors": "top_contributors", "guarantee": "guarantee_note",
}
# older answer labels -> current labels
ANSWER_ALIASES = {
    "TYPICAL": "WITHIN_NORMATIVE_RANGE", "WITHIN_RANGE": "WITHIN_NORMATIVE_RANGE",
    "REPEAT TEST": "INCONCLUSIVE_REPEAT", "REPEAT_TEST": "INCONCLUSIVE_REPEAT", "INCONCLUSIVE": "INCONCLUSIVE_REPEAT",
    "SUGGEST SPECIALIST": "OUTSIDE_NORMATIVE_RANGE_REVIEW", "SUGGEST_SPECIALIST": "OUTSIDE_NORMATIVE_RANGE_REVIEW",
    "OUTSIDE_NORMATIVE_RANGE": "OUTSIDE_NORMATIVE_RANGE_REVIEW", "REVIEW": "OUTSIDE_NORMATIVE_RANGE_REVIEW",
}
KNOWN = {"schema_version", "model_version", "recording_id", "answer", "answer_reasons", "context",
         "data_quality", "features", "symmetry", "three_state_profile", "longitudinal", "scores",
         "top_contributors", "consistency", "confound_warnings", "guarantee_note", "limitations",
         "plots", "audit"}


@dataclass
class Feature:
    name: str
    value: Optional[float]
    z: Optional[float] = None
    percentile: Optional[float] = None
    label: str = ""
    unit: str = ""
    hemisphere: str = ""
    hand: str = ""
    condition: str = ""


@dataclass
class Report:
    raw: dict
    recording_id: str
    answer: str
    answer_reasons: list
    data_quality: dict
    features: list
    scores: dict
    context: dict = field(default_factory=dict)
    symmetry: dict = field(default_factory=dict)
    three_state_profile: dict = field(default_factory=dict)
    longitudinal: Optional[dict] = None
    top_contributors: list = field(default_factory=list)
    consistency: Any = ""
    confound_warnings: list = field(default_factory=list)
    guarantee_note: str = ""
    limitations: list = field(default_factory=list)
    plots: Any = field(default_factory=list)
    schema_version: str = "unknown"
    model_version: str = "unknown"
    unknown_fields: list = field(default_factory=list)
    adapter_notes: list = field(default_factory=list)

    def feature(self, name):
        return next((f for f in self.features if f.name == name), None)

    def flagged_features(self, z_abs=1.96):
        return [f for f in self.features if f.z is not None and abs(f.z) >= z_abs]

    def numbers(self):
        """Every number that appears anywhere in the report (incl. inside text) - for guardrails."""
        out = set()

        def walk(x):
            if isinstance(x, bool) or x is None:
                return
            if isinstance(x, (int, float)):
                out.add(float(x))
            elif isinstance(x, str):
                out.update(float(m) for m in re.findall(r"-?\d+(?:\.\d+)?", x))
            elif isinstance(x, dict):
                for v in x.values():
                    walk(v)
            elif isinstance(x, (list, tuple)):
                for v in x:
                    walk(v)
        walk(self.raw)
        return out


def _normalize(d):
    notes, raw = [], dict(d)
    for k in list(raw):
        if k in ALIASES and ALIASES[k] not in raw:
            raw[ALIASES[k]] = raw.pop(k)
            notes.append(f"renamed '{k}' -> '{ALIASES[k]}'")
    ans = str(raw.get("answer", "")).strip()
    if ans.upper() in ANSWER_ALIASES:
        notes.append(f"answer '{ans}' mapped to {ANSWER_ALIASES[ans.upper()]}")
        ans = ANSWER_ALIASES[ans.upper()]
    if ans not in ANSWERS:
        notes.append(f"unknown answer '{ans}' -> treated as INCONCLUSIVE_REPEAT")
        ans = "INCONCLUSIVE_REPEAT"
    raw["answer"] = ans
    feats = raw.get("features", [])
    if isinstance(feats, dict):  # {name: value} or {name: {...}}
        feats = [dict(name=k, **(v if isinstance(v, dict) else {"value": v})) for k, v in feats.items()]
        notes.append("features given as dict -> converted to list")
    raw["features"] = feats
    scores = dict(raw.get("scores") or {})
    if "deviation_score" in raw and "omnibus" not in scores:
        scores["omnibus"] = {"score": raw["deviation_score"]}
        notes.append("top-level deviation_score -> scores.omnibus.score")
    if "p_value" in raw and "combined" not in scores:
        scores["combined"] = {"p": raw["p_value"]}
        notes.append("top-level p_value -> scores.combined.p")
    raw["scores"] = scores
    dq = raw.get("data_quality") or {}
    if isinstance(dq, str):
        dq = {"status": dq}
    dq.setdefault("status", "WARN")
    raw["data_quality"] = dq
    return raw, notes


def load_report(src):
    d = json.loads(Path(src).read_text()) if not isinstance(src, dict) else src
    raw, notes = _normalize(d)
    feats = []
    for f in raw["features"]:
        feats.append(Feature(name=str(f.get("name", "")), value=f.get("value"), z=f.get("z"),
                             percentile=f.get("percentile"), label=f.get("label", "") or f.get("name", ""),
                             unit=f.get("unit", ""), hemisphere=f.get("hemisphere", ""),
                             hand=f.get("hand", ""), condition=f.get("condition", "")))
    lim = raw.get("limitations") or []
    if isinstance(lim, str):
        lim = [lim]
    return Report(
        raw=raw, recording_id=str(raw.get("recording_id", "unknown")), answer=raw["answer"],
        answer_reasons=list(raw.get("answer_reasons") or []), data_quality=raw["data_quality"],
        features=feats, scores=raw["scores"], context=raw.get("context") or {},
        symmetry=raw.get("symmetry") or {}, three_state_profile=raw.get("three_state_profile") or {},
        longitudinal=raw.get("longitudinal"), top_contributors=list(raw.get("top_contributors") or []),
        consistency=raw.get("consistency", ""), confound_warnings=list(raw.get("confound_warnings") or []),
        guarantee_note=raw.get("guarantee_note", ""), limitations=lim, plots=raw.get("plots", []),
        schema_version=str(raw.get("schema_version", "unknown")),
        model_version=str(raw.get("model_version", "unknown")),
        unknown_fields=sorted(set(raw) - KNOWN), adapter_notes=notes)


def validate(src, schema_path=Path(__file__).parent / "contract" / "report_schema_v0.json"):
    """Raw JSON vs the current contract. Returns a list of problems ([] = valid)."""
    import jsonschema
    d = json.loads(Path(src).read_text()) if not isinstance(src, dict) else src
    schema = json.loads(Path(schema_path).read_text())
    v = jsonschema.Draft202012Validator(schema)
    return [f"{'/'.join(map(str, e.path)) or '(root)'}: {e.message}" for e in v.iter_errors(d)]


# ---------------------------------------------------------------- guardrails
DIAGNOSTIC_PATTERNS = [
    r"\bdiagnosed with\b", r"\byou have\b", r"\bthe patient has\b(?! been)",
    r"\b(has|have|likely has|probably has|consistent with|indicative of|confirms?|suggestive of|evidence of)\s+"
    r"(early\s+)?(parkinson'?s?|stroke|multiple sclerosis|\bms\b|als|dementia|alzheimer'?s?|neurodegenerat\w*)",
    r"\b(parkinson'?s?|stroke|dementia)\s+(is|was)\s+(detected|present|confirmed)",
]


def check_text(text, report, require_header=True):
    """Guardrails for anything the agent writes. Returns a list of violations ([] = OK)."""
    problems = []
    if report.answer not in text:
        problems.append(f"answer label {report.answer} not stated verbatim")
    if require_header and HEADER.lower() not in text.lower():
        problems.append("missing header: " + HEADER)
    for pat in DIAGNOSTIC_PATTERNS:
        m = re.search(pat, text, flags=re.IGNORECASE)
        if m:
            problems.append(f"diagnostic language: '{m.group(0)}'")
    allowed = report.numbers()
    for tok in re.findall(r"(?<![\w.])-?\d+(?:\.\d+)?", text):
        val = float(tok)
        dec = len(tok.split(".")[1]) if "." in tok else 0
        if any(abs(round(c, dec) - val) < 1e-9 for a in allowed for c in (a, abs(a))):
            continue
        problems.append(f"number not in report: {tok}")
    return problems
