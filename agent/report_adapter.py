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
import copy
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

CONTRACT_VERSION = "0.1 + model neurosym-report-0.4"
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
KNOWN = {"exploratory", "header", "decision_rule", "n_calibration", "deviation_score", "p_value", "simulated",
         "within_subject_permutation_p", "consistency_detail", "schema_version", "model_version", "recording_id", "answer", "answer_reasons", "context",
         "data_quality", "features", "symmetry", "three_state_profile", "longitudinal", "scores",
         "top_contributors", "consistency", "confound_warnings", "guarantee_note", "limitations",
         "plots", "audit", "inconclusive_cause"}
# model context key names -> the names the agent reads (F12); originals are kept
CONTEXT_ALIASES = {"hours_slept": "sleep_h", "caffeine_last_3h": "caffeine"}


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
    exploratory: list = field(default_factory=list)     # plain-English lines about unscored blocks
    unknown_fields: list = field(default_factory=list)
    adapter_notes: list = field(default_factory=list)
    # why an INCONCLUSIVE_REPEAT: 'qc_fail' | 'halves_disagree' | 'grey_zone' | None (derived from model fields)
    inconclusive_cause: Optional[str] = None
    consistency_detail: Optional[dict] = None       # odd/even half p-values as the model wrote them
    decision_rule: dict = field(default_factory=dict)
    n_calibration: Optional[int] = None
    simulated: Optional[dict] = None                # the model's simulated block (dict) when present
    is_simulated: bool = False                      # any truthy 'simulated' value from the model
    audit: dict = field(default_factory=dict)

    @property
    def is_provisional(self) -> bool:
        return bool(isinstance(self.context, dict) and self.context.get("provisional"))

    @property
    def calibration_pending(self) -> bool:
        """Schema-0.3 report whose answer the model fixes at INCONCLUSIVE_REPEAT (no decision computed):
        a repeat recording cannot change it."""
        return (self.answer == "INCONCLUSIVE_REPEAT" and self.inconclusive_cause is None
                and self.schema_version == "neurosym-report-0.3" and self.raw.get("p_value") is None)

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
                out.update(float(m) for m in re.findall(_NUM_RE, x.replace("\u2212", "-")))
            elif isinstance(x, dict):
                for v in x.values():
                    walk(v)
            elif isinstance(x, (list, tuple)):
                for v in x:
                    walk(v)
        walk({k: v for k, v in self.raw.items() if k != "audit"})   # audit = version/sha256, not findings
        return out


_NUM_RE = r"(?<![\w.])-?\d+(?:\.\d+)?"


_COND = {"execute": "real movement", "imagine": "imagined movement", "observe": "watching movement"}


def _describe_feature(name):
    """Plain-English label and metadata from model feature names like 'execute_mu_li_left'."""
    parts = name.split("_")
    if len(parts) >= 3 and parts[0] in _COND and parts[1] in ("mu", "beta"):
        cond, band, kind = parts[0], parts[1], parts[2]
        hand = parts[3] if len(parts) > 3 and parts[3] in ("left", "right") else ""
        if kind == "li" and hand:
            return {"label": f"Lateralization, {hand} hand, {_COND[cond]} ({band})", "unit": "dB",
                    "hand": hand, "condition": cond}
        if kind == "sym":
            return {"label": f"Hemisphere symmetry, {_COND[cond]} ({band})", "unit": "dB", "condition": cond}
    return {}


def _normalize_model_v04(raw, notes):
    """Map the model repo's report format (schema 'neurosym-report-0.4') onto the agent contract."""
    dq = raw.get("data_quality")
    if isinstance(dq, dict) and "status" not in dq and "qc_passed" in dq:
        dq = dict(dq)
        dq["status"] = "PASS" if dq.get("qc_passed") else "FAIL"
        dq.setdefault("notes", list(dq.get("qc_reasons") or []))
        if dq.get("n_trials") is not None:     # a total over all conditions and hands, not per condition
            dq.setdefault("n_trials_total", dq["n_trials"])
        raw["data_quality"] = dq
        notes.append("data_quality.qc_passed -> status")
    scores = raw.get("scores")
    if isinstance(scores, dict):
        out = {}
        for k, v in scores.items():
            if isinstance(v, dict) and ("conformal_p" in v or "value" in v):
                v = {"score": v.get("value"), "p": v.get("conformal_p"), **v}
                notes.append(f"scores.{k}: value/conformal_p -> score/p")
            out["asymmetry" if k == "maxz" else k] = v
        if "within_subject_permutation_p" in raw and isinstance(out.get("within"), dict):
            out["within"]["raw_permutation_p"] = raw["within_subject_permutation_p"]
        raw["scores"] = out
    rule = raw.get("decision_rule") if isinstance(raw.get("decision_rule"), dict) else {}
    alpha = rule.get("review_if_p_below")
    if not raw.get("guarantee_note") and alpha is not None:
        raw["guarantee_note"] = (f"Calibrated on {raw.get('n_calibration', 'the')} healthy reference recordings: for "
                                 f"healthy people recorded like the calibration set, the chance of a false "
                                 f"OUTSIDE_NORMATIVE_RANGE_REVIEW is at most {alpha * 100:g}%.")
        if any(isinstance(l, str) and "guarantee is not established" in l.lower()
               for l in (raw.get("limitations") or [])):
            raw["guarantee_note"] += (" This bound was established on the reference dataset only; for this "
                                      "recording device it is not established (see limitations).")
        notes.append("guarantee_note built from decision_rule and n_calibration")
    if not raw.get("answer_reasons") and rule:
        p = raw.get("p_value")
        if _is_num(p):
            reasons = [f"Combined conformal p = {p:.3g} (review if below {rule.get('review_if_p_below')}, "
                       f"within range if above {rule.get('within_if_p_above')})."]
        else:
            reasons = ["No combined p-value was computed."]
        cons = raw.get("consistency")
        if isinstance(cons, dict) and cons.get("status") == "disagree":
            reasons.append(f"Odd/even halves disagree (odd p = {cons.get('odd_p_value')}, even p = "
                           f"{cons.get('even_p_value')}); decision rule: qc_fail_or_halves_disagree -> "
                           f"INCONCLUSIVE_REPEAT.")
        elif isinstance(cons, dict) and cons.get("status"):
            reasons.append(f"Odd/even halves: {cons['status']}.")
        reasons += [f"QC: {r}" for r in (raw.get("data_quality") or {}).get("notes", [])]
        raw["answer_reasons"] = reasons
        notes.append("answer_reasons built from p_value, decision_rule and consistency")
    sc = raw.get("scores") if isinstance(raw.get("scores"), dict) else {}
    if any(k in sc for k in ("within", "asymmetry", "omnibus")):
        def _p(k):
            v = sc.get(k)
            return v.get("p") if isinstance(v, dict) and _is_num(v.get("p")) else None
        parts = [f"within-subject {_p('within'):g}" if _p("within") is not None else "within-subject test not run"]
        if _p("asymmetry") is not None:
            parts.append(f"max |z| asymmetry {_p('asymmetry'):g}")
        if _p("omnibus") is not None:
            parts.append(f"omnibus {_p('omnibus'):g}")
        raw["answer_reasons"] = list(raw.get("answer_reasons") or []) + [
            "Component conformal p: " + ", ".join(parts) + "; the smallest drives the combined p."]
        notes.append("component conformal p-values added to answer_reasons")
    cons = raw.get("consistency")
    if isinstance(cons, dict):
        raw["consistency_detail"] = cons
        raw["consistency"] = f"odd/even halves {cons.get('status', 'unknown')}"
    audit = raw.get("audit") or {}
    if "model_version" not in raw and isinstance(audit, dict) and audit.get("model_version"):
        raw["model_version"] = f"{audit['model_version']} ({audit.get('backend', '')})".strip()
        notes.append("audit.model_version -> model_version")
    ctx = raw.get("context") or {}
    if isinstance(ctx, dict) and ctx.get("provisional"):
        notes.append("provisional report (session in progress)")
    sim = raw.get("simulated")
    if sim:
        lim = list(raw.get("limitations") or [])
        desc = ", ".join(f"{k} {v}" for k, v in sim.items()) if isinstance(sim, dict) else str(sim)
        lim.insert(0, f"SIMULATED recording, not a real person ({desc}).")
        raw["limitations"] = lim
        notes.append("simulated recording flagged in limitations")
    feats = raw.get("features")
    if isinstance(feats, list):
        raw["features"] = [{**_describe_feature(str(f.get("name", ""))), **f} if isinstance(f, dict) else f
                           for f in feats]


def _is_num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _inconclusive_cause(raw):
    """Why the answer is INCONCLUSIVE_REPEAT, from the model's own fields only (no new numbers)."""
    if raw.get("answer") != "INCONCLUSIVE_REPEAT":
        return None
    dq = raw.get("data_quality") if isinstance(raw.get("data_quality"), dict) else {}
    cons = raw.get("consistency_detail") if isinstance(raw.get("consistency_detail"), dict) else {}
    rule = raw.get("decision_rule") if isinstance(raw.get("decision_rule"), dict) else {}
    p, rv, wv = raw.get("p_value"), rule.get("review_if_p_below"), rule.get("within_if_p_above")
    if dq.get("qc_passed") is False or dq.get("status") == "FAIL":
        return "qc_fail"
    if cons.get("status") == "disagree":
        return "halves_disagree"
    if _is_num(p) and _is_num(rv) and _is_num(wv) and rv <= p <= wv:
        raw["answer_reasons"] = list(raw.get("answer_reasons") or []) + [
            f"p between {rv} and {wv} -> INCONCLUSIVE_REPEAT."]
        return "grey_zone"
    return None


def _normalize(d):
    notes, raw = [], copy.deepcopy(d)          # never mutate the caller's dict
    if str(raw.get("schema_version", "")).startswith("neurosym-report"):
        _normalize_model_v04(raw, notes)
    if raw.get("schema_version") == "neurosym-report-0.3":
        if not raw.get("answer_reasons") and raw.get("p_value") is None and raw.get("answer") == "INCONCLUSIVE_REPEAT":
            reasons = ["Decision calibration pending in this model version (schema 0.3); answer fixed at "
                       "INCONCLUSIVE_REPEAT."]
            if raw.get("guarantee_note"):
                reasons.append(f"Model note: {raw['guarantee_note']}")
            raw["answer_reasons"] = reasons
            notes.append("schema 0.3: answer_reasons built (calibration pending)")
        dq0 = raw.get("data_quality")
        if isinstance(dq0, dict) and dq0.get("site_in_reference") is False:
            dq0["notes"] = list(dq0.get("notes") or []) + ["site has no fitted reference"]
    ctx = raw.get("context")
    if isinstance(ctx, dict):
        ctx = dict(ctx)
        for k, v in CONTEXT_ALIASES.items():
            if k in ctx and v not in ctx:
                ctx[v] = ctx[k]
                notes.append(f"context '{k}' -> '{v}'")
        raw["context"] = ctx
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
    if "status" not in dq:                     # do not invent PASS/FAIL/WARN the model never reported
        dq["status"] = "NOT_REPORTED"
        notes.append("data_quality.status not reported by model")
    raw["data_quality"] = dq
    raw["inconclusive_cause"] = _inconclusive_cause(raw)
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
        exploratory=_exploratory_lines(raw.get("exploratory")),
        unknown_fields=sorted(set(raw) - KNOWN), adapter_notes=notes,
        inconclusive_cause=raw.get("inconclusive_cause"),
        consistency_detail=raw.get("consistency_detail") if isinstance(raw.get("consistency_detail"), dict) else None,
        decision_rule=raw.get("decision_rule") if isinstance(raw.get("decision_rule"), dict) else {},
        n_calibration=raw.get("n_calibration") if _is_num(raw.get("n_calibration")) and isinstance(raw.get("n_calibration"), int) else None,
        simulated=raw.get("simulated") if isinstance(raw.get("simulated"), dict) else None,
        is_simulated=bool(raw.get("simulated")),
        audit=raw.get("audit") if isinstance(raw.get("audit"), dict) else {})


def report_badges(r):
    """Status badges every viewer must show next to the answer (F18)."""
    out = []
    if r.is_simulated:
        out.append("SIMULATED")
    if r.is_provisional:
        out.append("PROVISIONAL")
    return out


def _exploratory_lines(ex):
    if not isinstance(ex, dict):
        return []
    lines = [ex["note"]] if ex.get("note") else []
    for b in ex.get("blocks") or []:
        lines.append(f"{b.get('task')} ({b.get('hand')} arm): opposite hemisphere responded more in "
                     f"{b.get('n_opposite_side_stronger')} of {b.get('n_trials')} trials "
                     f"(mean {b.get('mean_opposite_side_erd_db')} dB opposite vs {b.get('mean_same_side_erd_db')} dB same side).")
    return lines


def validate(src, schema_path=None):
    """Raw JSON vs its contract: the model's own schema for its reports, v0 for the agent mocks.
    Returns a list of problems ([] = valid)."""
    import jsonschema
    d = json.loads(Path(src).read_text()) if not isinstance(src, dict) else src
    if schema_path is None:
        sv = d.get("schema_version")
        if sv == "neurosym-report-0.3":       # informational, not a contract failure (app.py shows it as info)
            return ["legacy neurosym-report-0.3: not contract-checked"]
        schema_path = Path(__file__).parent / "contract" / (
            "report_schema_model.json" if sv == "neurosym-report-0.4" else "report_schema_v0.json")
    schema = json.loads(Path(schema_path).read_text())
    v = jsonschema.Draft202012Validator(schema)
    return [f"{'/'.join(map(str, e.path)) or '(root)'}: {e.message}" for e in v.iter_errors(d)]


# ---------------------------------------------------------------- guardrails
# Lexical guardrails. They are a floor, not a proof of correctness: the LLM agent also runs a
# claim verifier (agent.py). Tested against good and bad example answers in tests/test_guardrails.py
# and tests/test_adapter_link.py.

FLAG_Z = 1.96   # threshold used by Report.flagged_features(); the agent may cite it (as +1.96 or -1.96)
# Numbers that are part of how things are written, not claims about the recording.
_MASKS = [r"(?m)^\s*\d+[.)]\s", r"\b10-20\b", r"\bC[34]\b|\b[A-Z]{1,2}\d{1,2}\b"]

# NOTE: fieldnote_server.guarded() imports DIAGNOSTIC_PATTERNS, _DISEASES and _JUDGMENT for the
# patient-side guard. Only ever make these stricter; clinician-only patterns go in _CLINICIAN_DIAGNOSTIC.
_DISEASES = (r"parkinson\w*|\bPD\b|stroke|multiple sclerosis|\bMS\b|\bALS\b|amyotrophic|dementia|"
             r"alzheimer\w*|neurodegenerat\w*|hemipar\w*|hemipleg\w*|lesion\w*|ataxi\w*|dystoni\w*|"
             r"huntington\w*|epilep\w*|seizure\w*|neuropath\w*|\bTIA\b|tumou?r\w*|brain injury|\bTBI\b|"
             r"cerebral palsy|essential tremor|myelopath\w*|radiculopath\w*|motor neurone? disease|\bMND\b"
             r"|\b(movement|motor|neurolog\w*|neuromuscular) (disorder|disease|condition)s?\b"
             r"|\b(brain|cortical|cerebral|nerve|spinal|spinal cord|head|neurolog\w*) injur(y|ies)\b")
_JUDGMENT = r"\b(impair\w*|abnormal\w*|deficit\w*|damage\w*|dysfunction\w*|patholog\w*|diseased|degenerat\w*)\b"
# 'well' is reassurance ("is well"), not the intensifier in "is well below -1.96"
_WELL = r"well\b(?!\s+(below|above|within|outside|beyond|under|over|short|past|known|documented|established|defined))"
_RULE_OUT = (r"\brul(e|es|ed|ing) out\b"
             r"|\bexclud(e|es|ed|ing)\s+(a|an|the|any)\s+([\w-]+\s+)?(problem|disorder|condition|disease|"
             r"diagnos\w*|abnormalit\w*|patholog\w*|cause)s?\b"
             r"|\b(problem|disorder|condition|disease|cause|diagnos\w*)s?\b[^.;\n]{0,25}\bexcluded\b")
_REASSURE = (r"\b(is|are|looks?|appears?|seems?)\s+(\w+ly\s+)?(healthy|normal|fine|ok(ay)?|intact|" + _WELL + r")\b|"
             + _RULE_OUT + r"|"
             r"\bnothing to worry\b|\bno (need|reason) (for|to) (follow|worry|repeat|review)\w*|"
             r"\bno further (testing|follow-?up|review)\b|\bno (cause for )?concern\b"
             r"|\bwithin normal limits\b|(?<!not )(?<!n't )\breassur(ing|ed|ance)\b|\bunremarkable\b"
             r"|\bhealthy (result|recording|brain)\b|\b(all|everything)( is)? (fine|ok|okay|normal)\b"
             r"|\bnothing\s+(is\s+)?wrong\b|\bworking normally\b"
             r"|\b(does not|doesn't|do not|don't)\s+have\s+an?\s+([\w-]+\s+)?(problem|disorder|condition|disease)\b")
# the model's only guarantee is about a false OUTSIDE_NORMATIVE_RANGE_REVIEW; nothing else is exempt
_FALSE_OUT = (r"\s+a\s+false[\s-]+(positive[\s-]+|alarm[\s-]+)?OUTSIDE[\s_-]+(THE[\s_-]+)?NORMATIVE[\s_-]+RANGE"
              r"[\s_-]+REVIEW")
_PROBABILITY = (r"\b(chance|probability|likelihood|odds|risk)\s+(of|that)\b(?!" + _FALSE_OUT + r")|"
                r"\b\d+(\.\d+)?\s*%?\s+(chance|probability|likelihood)\b(?!\s+of" + _FALSE_OUT + r")|\bodds (are|is)\b"
                r"|\b(probably|likely|unlikely|probable|improbable)\b"
                r"|\b(high|low|elevated|increased|reduced)[\s-]+risk\b|\bprobability\s+(is|here|was)\b")
DIAGNOSTIC_PATTERNS = [r"\bdiagnosed with\b",
                       r"\b(confirm(s|ed|ing)?|establish\w*|support(s|ed|ing)?|suggest(s|ed|ing)?)\s+"
                       r"(a |an |the )?diagnos\w*\b"]
_CONFIRM_DIAGNOSIS = DIAGNOSTIC_PATTERNS[1]
# clinician text only; NOT shared with fieldnote_server.guarded (patients' own words must survive there)
_HAS_DISORDER = (r"\b(has|have|had|suffers? from)\s+an?\s+(?!(same|different|single|separate|second|first)\b)"
                 r"(([\w-]+\s+)(disorder|disease|condition)|disorder|disease)s?\b")
_DIAG_OF = r"\bdiagnosis\s+(of\b|is\b(?!\s+(up to|for|made by|the clinician|a clinical|not|beyond|outside)\b))"
_INFER_DISEASE = (r"\b(suggest\w*|indicat\w*|consistent with|compatible with|resembl\w*|signs? of|typical of|"
                  r"characteristic of|evidence of)\b[^.;\n]{0,40}?\b(disease|disorder)s?\b")
_CLINICIAN_DIAGNOSTIC = [_HAS_DISORDER, r"\bdiagnosis\s*:",
                         r"\b(likely|probable|possible|differential|working|presumptive|provisional)\s+diagnos\w*\b",
                         _DIAG_OF, _INFER_DISEASE]
# words that turn a symptom the report names into an inference (F1 allow-set)
_INFER = (r"\b(typical of|consistent with|suggest\w*|indicat\w*|compatible|signs? of|caus\w*|confirm\w*|"
          r"points? to|pointing to|due to|because of|explain\w*|result(s|ed)? (of|from)|reflect\w*|resembl\w*|"
          r"likely|probabl\w*|evidence of|characteristic|attribut\w*|related to|secondary to|diagnos\w*)\b")

# a negation directly before the matched verb ("cannot establish a diagnosis", "does not rule out")
_NEG = re.compile(r"\b(not|nor|neither|cannot|\w+n't|never|unable to|not possible to|not able to)\s+(be\s+)?$", re.I)
# one negation covering two joined verbs ("cannot confirm or rule out", "neither confirm nor rule out")
_NEG_COORD = re.compile(r"\b(not|nor|neither|cannot|\w+n't|never|unable to|not possible to|not able to)\s+"
                        r"(\w+\s+){1,2}(or|nor)\s+$", re.I)
_NEG_PURPOSE = re.compile(r"\b(not|never|\w+n't)\s+(be\s+)?(used|meant|intended|able|designed|enough|sufficient)"
                          r"\s+to\s+$", re.I)
_NEG_NOTHING = re.compile(r"\b(not|nothing)\s+(something\s+|anything\s+)?(that\s+)?(this|the|a|one)\s+(\w+\s+){0,2}"
                          r"(can|could|will|is able to)\s+$", re.I)
# a second negator earlier in the same clause cancels the first ("no way this does not confirm")
_SECOND_NEG = re.compile(r"\b(no way|no one|no-one|nobody|nothing|not|never|cannot|\w+n't|impossible|false that|"
                         r"untrue)\b", re.I)
# answer labels; '_' matches any run of spaces/underscores/hyphens ("outside normative range review")
_LABEL_RX = [a.replace("_", r"[\s_-]+") for a in ANSWERS]
# natural-language forms of each answer: flagged when they contradict the report's own answer
_LABEL_RX_THE = {a: r"\b" + a.replace("_", r"[\s_-]+(the[\s_-]+)?") + r"\b" for a in ANSWERS}
_NATURAL = {"OUTSIDE_NORMATIVE_RANGE_REVIEW": r"\boutside\s+(of\s+)?(the\s+)?normative\s+range\b",
            "WITHIN_NORMATIVE_RANGE": r"\bwithin\s+(the\s+)?normative\s+range\b",
            "INCONCLUSIVE_REPEAT": r"\binconclusive\b"}
_NUM_WORDS = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve "
                                         "thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty"
                                         .split())}
_NUM_WORDS.update({"thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80,
                   "ninety": 90, "hundred": 100, "thousand": 1000})
_NUM_WORDS.pop("one")            # "no one", "each one", "the one on the left": too often not a count
_NUM_WORDS.pop("zero")
_NUM_WORD_RX = r"\b(" + "|".join(_NUM_WORDS) + r")\b"
# a number word that counts the test's own structure, not a finding ("the two hemispheres")
_STRUCT_NOUN = (r"\s+(hemispheres?|hands?|halves|half|sides?|arms?|conditions?|electrodes?|channels?|states?|"
                r"tasks?|blocks?|visits?|recordings?|answers?|labels?|tests?|components?)\b")
# a comparison or cutoff word right before +/-1.96 (the flag threshold, never a feature's value)
_CUTOFF = r"(<=|>=|<|>|≤|≥|±|\+/-|\bbelow\b|\babove\b|\bbeyond\b|\bexceed\w*|\bthreshold\b|\bcut-?off\b|\|z\||\bthan\b|\bbetween\b|\bat (least|most)\b|\bunder\b|\bover\b|\breach\w*)"


def _clause_before(text, start, back=200):
    return re.split(r"[.;:!?\n]", text[max(0, start - back):start])[-1]


def _negated(text, start, kind="neg"):
    pre = _clause_before(text, start)
    if kind == "concern":
        return re.search(r"not mean (that )?(there is )?$", pre[-30:], re.I) is not None
    for rx in (_NEG, _NEG_COORD, _NEG_PURPOSE, _NEG_NOTHING):
        m = rx.search(pre)
        if m:
            before = re.split(r",|\b(?:and|but|or|nor|while|whereas|although|though)\b", pre[:m.start()])[-1]
            return _SECOND_NEG.search(before) is None
    return False


def _sentence(text, start, end):
    a = max([text.rfind(c, 0, start) for c in ".;!?\n"]) + 1
    ends = [i for i in (text.find(c, end) for c in ".;!?\n") if i >= 0]
    return text[a:min(ends) if ends else len(text)]


def _infers(sentence):
    """True if the sentence draws an inference (unnegated 'typical of', 'caused', 'confirms', ...)."""
    for m in re.finditer(_INFER, sentence, re.IGNORECASE):
        if not _negated(sentence, m.start()) and not re.search(r"\b(not|no|never|cannot|\w+n't)\s+(\w+\s+){0,2}$",
                                                               sentence[:m.start()], re.I):
            return True
    return False


def _tok_ctx(s, a, b):
    """(previous token, next token) around s[a:b]; a token is a word or one punctuation mark."""
    p = re.search(r"(\w+|[^\w\s])\s*$", s[:a])
    n = re.match(r"\s*(\w+|[^\w\s])", s[b:])
    return (p.group(1).lower() if p else ""), (n.group(1).lower() if n else "")


def _report_strings(report):
    """Human-readable string values (never keys, never JSON literals such as 'false') of the report
    fields that may legitimately name a condition: context.symptoms, context.medications, data_quality.notes."""
    ctx = report.context if isinstance(report.context, dict) else {}
    dq = report.data_quality if isinstance(report.data_quality, dict) else {}

    def vals(x):
        if isinstance(x, str):
            yield x
        elif isinstance(x, (list, tuple)):
            for y in x:
                yield from vals(y)
        elif isinstance(x, dict):
            for y in x.values():
                yield from vals(y)
    for src in (ctx.get("symptoms"), ctx.get("medications"), dq.get("notes")):
        yield from vals(src)


def _number_sources(report):
    """(numbers stored as numbers, decimals written inside report text, {integer written inside report text:
    [(prev, next) token contexts]}). An integer that only appears inside text ('at most 5%', '1/(n_cal+1)')
    is allowed only in the same context, never as a free-standing new claim ('repeat in 5 weeks')."""
    struct, dec, ints = set(), set(), {}

    def text(s):
        s = s.replace("−", "-")
        for m in re.finditer(_NUM_RE, s):
            if "." in m.group(0):
                dec.add(float(m.group(0)))
            else:
                ints.setdefault(float(m.group(0)), []).append(_tok_ctx(s, m.start(), m.end()))

    def walk(x):
        if isinstance(x, bool) or x is None:
            return
        if isinstance(x, (int, float)):
            struct.add(float(x))
        elif isinstance(x, str):
            text(x)
        elif isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, (list, tuple)):
            for v in x:
                walk(v)
    walk({k: v for k, v in report.raw.items() if k != "audit"})   # audit = version/sha256, not findings
    for s in [report.guarantee_note, *report.answer_reasons, *report.limitations, *report.confound_warnings,
              *report.exploratory]:
        if isinstance(s, str):
            text(s)
    return struct, dec, ints


def check_text(text, report, require_header=True, extra_numbers=()):
    """Guardrails for anything the agent writes. Returns a list of violations ([] = OK)."""
    problems = []
    if report.answer not in text:
        problems.append(f"answer label {report.answer} not stated verbatim")
    quoted = text
    for s_ in [report.guarantee_note, *report.answer_reasons, *report.limitations, *report.confound_warnings]:
        if isinstance(s_, str) and s_:
            quoted = quoted.replace(s_, " ")       # labels inside quoted report text are fine
    verbatim = quoted                              # report sentences removed, wording otherwise intact
    lbl = re.sub(r"false[\s-]+(positive[\s-]+|alarm[\s-]+)?" + _LABEL_RX[2], " ", quoted,
                 flags=re.IGNORECASE)              # paraphrased guarantee note (only the OUTSIDE guarantee exists)
    hdr_free = re.sub(re.escape(HEADER), " ", lbl, flags=re.IGNORECASE)
    for other in ANSWERS:
        if other == report.answer:
            continue
        hit = False
        for rx in (_LABEL_RX_THE[other], _NATURAL[other]):
            for m in re.finditer(rx, hdr_free, flags=re.IGNORECASE):
                g = m.group(0)
                if rx is _LABEL_RX_THE[other] and (g.isupper() or "_" in g or "-" in g
                                                   or other != "WITHIN_NORMATIVE_RANGE"):
                    hit = True                     # the label itself (any spacing): never another answer's
                    break                          # (lower-case 'within the normative range' is plain English)
                if _negated(hdr_free, m.start()) or re.search(r"\bwhether\s+(or not\s+)?(\w+\s+){0,3}$",
                                                              _clause_before(hdr_free, m.start()), re.I):
                    continue                       # "not within the normative range" on an OUTSIDE report
                hit = True
                break
            if hit:
                break
        if hit:
            problems.append(f"mentions another answer label: {other}")
    if require_header and HEADER.lower() not in text.lower():
        problems.append("missing header: " + HEADER)
    body = re.sub(re.escape(HEADER), "", text, flags=re.IGNORECASE)
    quoted_body = re.sub(re.escape(HEADER), "", verbatim, flags=re.IGNORECASE)
    for pat in DIAGNOSTIC_PATTERNS + _CLINICIAN_DIAGNOSTIC:
        for m in re.finditer(pat, body, flags=re.IGNORECASE):
            if pat is _CONFIRM_DIAGNOSIS and _negated(body, m.start()):
                continue
            pre = _clause_before(body, m.start())
            if pat is _DIAG_OF and re.search(r"\b(not|no|never|nor|cannot|\w+n't|unable to|without)\s+(\w+\s+){0,2}$",
                                             pre, re.I):
                continue                           # "this is not a diagnosis of any condition"
            if pat is _HAS_DISORDER and re.search(r"\b(whether|if)\b", pre, re.I):
                continue                           # "cannot answer whether the patient has a disease"
            if pat is _INFER_DISEASE:
                if _negated(body, m.start()) or re.search(r"\b(not|no|nor|never)\s+(a\s+|an\s+|any\s+)?([\w-]+\s+)?$",
                                                          body[m.start():m.start(m.lastindex)], re.I):
                    continue                       # "indicates a deviation, not a disease"
            problems.append(f"diagnostic language: '{m.group(0)}'")
            break
    allowed_terms = {t.group(0).lower() for t in re.finditer(_DISEASES, " ".join(_report_strings(report)),
                                                               flags=re.IGNORECASE)}
    for m in re.finditer(_DISEASES, quoted_body, flags=re.IGNORECASE):
        if m.group(0) == "ms" and re.search(r"\d\s*$", quoted_body[:m.start()]):
            continue                                    # the unit "2.1 ms" (milliseconds), not a condition
        if m.group(0).lower() in allowed_terms and not _infers(_sentence(quoted_body, m.start(), m.end())):
            continue                                    # the report names it (e.g. symptoms) and no inference is drawn
        problems.append(f"diagnostic language, names a condition: '{m.group(0)}'")
    for label, pat in (("clinical judgment word", _JUDGMENT), ("reassurance", _REASSURE),
                       ("probability claim", _PROBABILITY)):
        for m in re.finditer(pat, quoted_body, flags=re.IGNORECASE):
            g = m.group(0).lower()
            if label == "reassurance" and re.fullmatch(_RULE_OUT, g, re.IGNORECASE):
                pos = m.start() + (g.rfind("excluded") if g.endswith("excluded") else 0)
                if _negated(quoted_body, pos):
                    continue
            if label == "reassurance" and re.match(r"(does not|doesn't|do not|don't) have", g) and \
                    re.search(r"\b(whether|if)\b", _clause_before(quoted_body, m.start()), re.I):
                continue                                # "cannot tell whether the patient does or does not have a ..."
            if label == "reassurance" and re.fullmatch(r"no (cause for )?concern", g) and \
                    _negated(quoted_body, m.start(), "concern"):
                continue
            problems.append(f"{label}: '{m.group(0)}'")
    masked = quoted_body.replace("−", "-")         # verbatim report sentences are not re-checked
    for pat in _MASKS:
        masked = re.sub(pat, " ", masked)
    struct, dec, ints = _number_sources(report)
    allowed = struct | dec | {float(x) for x in extra_numbers}
    for m in re.finditer(_NUM_RE, masked):
        tok, val = m.group(0), float(m.group(0))
        dec_n = len(tok.split(".")[1]) if "." in tok else 0
        if any(abs(a - val) < 1e-9 for a in allowed):
            continue                                   # exact value (sign must match)
        if val in ints:
            p, n = _tok_ctx(masked, m.start(), m.end())
            if any((p and p == p2) or (n and n == n2) for p2, n2 in ints[val]):
                continue                               # same integer in the same context as the report's own text
        if abs(abs(val) - FLAG_Z) < 1e-9:
            w = re.split(r"(?<![<>!=≤≥])=(?!=)|[,;:]", _clause_before(masked, m.start(), 25))[-1]
            if re.search(_CUTOFF, w, re.IGNORECASE):
                continue                               # the flag threshold cited as a cutoff
        sig = len(tok.lstrip("-").replace(".", "").lstrip("0"))
        if dec_n > 0 and sig >= 3 and any(abs(round(a, dec_n) - val) < 1e-9 for a in allowed):
            continue                                   # signed rounding, at least 3 significant figures
        problems.append(f"number not in report: {tok}")
    int_vals = struct | set(ints)
    for m in re.finditer(_NUM_WORD_RX, masked, flags=re.IGNORECASE):
        if re.match(_STRUCT_NOUN, masked[m.end():], re.IGNORECASE):
            continue                                   # "the two hemispheres", "both halves"
        if _NUM_WORDS[m.group(0).lower()] in int_vals:
            continue
        problems.append(f"number not in report: '{m.group(0)}'")
    return problems
