"""
agent.py - NeuroSym clinician copilot.

Hard rules (from PROMPT_for_agent_builder.md):
- Model reports are read ONLY through report_adapter.load_report() -> Report. This file never indexes
  raw report JSON and never touches EEG. When the model's output format changes, only
  report_adapter.py (ALIASES / _normalize) and the contract change; this file stays the same.
- Every text shown to a clinician passes report_adapter.check_text(text, report). On failure the LLM
  gets one retry with the violations listed; then we fall back to the deterministic offline answer.
- Every interaction is logged to audit/agent_log.jsonl.

Usage:
    from agent import Copilot, ReportStore
    store = ReportStore.default()               # mocks/ + contract/samples/
    bot = Copilot(store)                        # uses ANTHROPIC_API_KEY if set, else offline mode
    reply = bot.ask("Why was this flagged?", "MOCK_OUTSIDE_WEAK_LEFT")
    reply.text, reply.mode, reply.guardrail_problems

CLI:
    python agent.py mocks/outside_weak_left.json "What should I check before acting on this?"
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from offline_summary import not_interpreted, summary as offline_summary
from report_adapter import HEADER, Report, check_text, load_report

ROOT = Path(__file__).resolve().parent
def audit_dir() -> Path:
    """Read at call time so tests and deployments can redirect the audit folder."""
    return Path(os.environ.get("NEUROSYM_AUDIT_DIR", ROOT / "audit"))
DEFAULT_MODEL = os.environ.get("NEUROSYM_AGENT_MODEL", "claude-sonnet-5-5")
MAX_TOOL_ROUNDS = 6

# Plain-English meaning of each feature family, keyed by name prefix. No numbers here on purpose:
# guardrails only allow numbers that appear in the report.
FEATURE_GLOSSARY = [
    ("li", "Lateralization for one hand: how much more the opposite hemisphere's power drops than the "
           "same-side one. Positive means the expected opposite-side dominance."),
    ("sym", "Cross-hemisphere symmetry: left hemisphere during right-hand movement minus right hemisphere "
            "during left-hand movement. Healthy people are roughly, not perfectly, symmetric."),
    ("erd_mu", "Mu-rhythm power drop during movement over one motor cortex. A larger "
               "negative value means that side engaged more strongly."),
    ("erd_beta", "Beta-rhythm power drop during movement over one motor cortex."),
    ("erd", "Event-related desynchronization: the power drop over a motor cortex during the task."),
    ("sym", "Cross-hemisphere symmetry: left hemisphere during right-hand movement compared with right "
            "hemisphere during left-hand movement. Healthy people are roughly, not perfectly, symmetric."),
    ("li", "Lateralization for one hand: how much more the opposite hemisphere responds than the same-side one."),
    ("pmbr", "Post-movement beta rebound: the power overshoot after movement stops (the 'reset')."),
    ("rebound", "Post-movement beta rebound: the power overshoot after movement stops (the 'reset')."),
    ("rest", "Resting rhythm power, a baseline measure rather than a task response."),
]

SYSTEM_PROMPT = f"""You are the NeuroSym clinician copilot. You help a clinician read ONE model report
about a standardized EEG hand-movement test. You only see the report through your tools.

Rules you must follow:
- Begin with the line "{HEADER}." and state the report's answer label verbatim
  (WITHIN_NORMATIVE_RANGE, INCONCLUSIVE_REPEAT or OUTSIDE_NORMATIVE_RANGE_REVIEW).
- Answer the question asked, directly, in about 150 words or fewer. The app appends a standard footer
  with data quality, confound warnings, limitations and the guarantee note, so do not repeat those
  in full; mention one only when it changes the answer (for example a FAIL, or a confound that
  affects the measures you discuss).
- Never diagnose. Never name a disease as the cause of a finding, and never say the patient "has" a
  condition. A flag means the measures differ from healthy reference data, nothing more.
- Never name a disease or condition (not even hedged: "could point to", "compatible with"), and do not
  use judgment words such as impaired, abnormal, deficit, damage or dysfunction. Say "outside the
  healthy reference range" and describe which side and which measure.
- Never reassure ("healthy", "normal", "nothing to worry about", "rules out") and never give a chance,
  probability or risk of any condition. Percentiles compare with healthy reference data only.
- Use only numbers that appear in tool results, written exactly as given. Do not compute new numbers
  (no differences, ratios, rounding or percentages of your own) and do not quote statistical cutoffs
  unless a tool result contains them.
- If asked whether the person has a specific disease, or for its probability, say that this test
  cannot answer diagnostic questions and that the clinician decides, WITHOUT repeating the disease name.
  Then give what the report does show.
- Mention only this report's own answer label, never the other two.
- Exploratory (unscored) arm blocks are descriptive only: never combine them with the answer label or
  call them typical or atypical; say the model has no healthy reference for them.
- If data quality is FAIL or the answer is INCONCLUSIVE_REPEAT, do not interpret feature values as
  findings: explain what went wrong and what to fix before repeating.
- Left hemisphere = C3 = responds to the RIGHT hand; right hemisphere = C4 = responds to the LEFT hand.
  Check sides against the tool results before writing.
- For INCONCLUSIVE_REPEAT, explain why and what to fix before repeating.
- Be brief and concrete. Plain text, short bullets. The clinician decides; you inform.
"""

TOOL_SPECS = [
    {"name": "get_report", "description": "Overview of a report: answer, reasons, data quality, scores, "
     "flagged features, context, limitations, guarantee note.",
     "input_schema": {"type": "object", "properties": {"recording_id": {"type": "string"}},
                      "required": ["recording_id"]}},
    {"name": "explain_flag", "description": "Details and plain-English meaning of one feature.",
     "input_schema": {"type": "object", "properties": {"feature_name": {"type": "string"},
                                                       "recording_id": {"type": "string"}},
                      "required": ["feature_name"]}},
    {"name": "compare_visits", "description": "Side-by-side feature values of two recordings of the same "
     "person, plus any reliable-change results the newer report contains.",
     "input_schema": {"type": "object", "properties": {"id_a": {"type": "string"}, "id_b": {"type": "string"}},
                      "required": ["id_a", "id_b"]}},
    {"name": "list_confounds", "description": "Confound warnings, relevant context and data-quality notes.",
     "input_schema": {"type": "object", "properties": {"recording_id": {"type": "string"}},
                      "required": ["recording_id"]}},
    {"name": "draft_summary", "description": "Template clinician summary to start an editable draft from.",
     "input_schema": {"type": "object", "properties": {"recording_id": {"type": "string"}},
                      "required": ["recording_id"]}},
]


# ------------------------------------------------------------------ report store
class ReportStore:
    """Holds Report objects by recording_id. Loading always goes through load_report()."""

    def __init__(self):
        self.reports: dict[str, Report] = {}
        self.sources: dict[str, str] = {}
        self.load_errors: dict[str, str] = {}

    @classmethod
    def default(cls, dirs=None):
        s = cls()
        live = Path(os.environ.get("NEUROSYM_LIVE_REPORTS", ROOT / "live_reports"))
        for d in dirs or [ROOT / "mocks", ROOT / "contract" / "samples", live]:
            s.load_dir(d)
        return s

    def load_dir(self, d):
        for p in sorted(Path(d).glob("*.json")):
            if p.name != "live_status.json":
                self.load_file(p)

    def load_file(self, path) -> Optional[Report]:
        try:
            r = load_report(path)
        except Exception as e:  # noqa: BLE001 - one bad file must not break the app
            self.load_errors[str(path)] = repr(e)
            return None
        return self.add(r, str(path))

    def add(self, report: Report, source="(memory)") -> Report:
        rid = report.recording_id
        if rid in self.reports and self.sources.get(rid) != source and self.reports[rid] != report:
            self.load_errors[source] = (f"duplicate recording_id {rid} (already loaded from "
                                        f"{self.sources[rid]}); kept the later file")
        self.reports[rid] = report
        self.sources[rid] = source
        return report

    def get(self, recording_id) -> Optional[Report]:
        return self.reports.get(recording_id)

    def ids(self):
        return sorted(self.reports)

    def is_provisional(self, rid) -> bool:
        r = self.reports.get(rid)
        return bool(self.sources.get(rid, "").endswith("_provisional.json") or (r is not None and r.is_provisional))

    def superseded(self, rid) -> bool:
        """A provisional report whose final report (same id without '_provisional') is loaded."""
        return rid.endswith("_provisional") and rid[:-len("_provisional")] in self.reports

    def visible_ids(self):
        """ids without provisional reports that a final report already replaces."""
        return [rid for rid in self.ids() if not self.superseded(rid)]


# ------------------------------------------------------------------ tools (read only from Report)
def _feature_dict(f):
    return {k: v for k, v in dict(name=f.name, label=f.label, value=f.value, unit=f.unit, z=f.z,
                                  percentile=f.percentile, hemisphere=f.hemisphere, hand=f.hand,
                                  condition=f.condition).items() if v not in ("", None)}


def glossary(name: str) -> str:
    tokens = name.lower().split("_")
    for key, text in FEATURE_GLOSSARY:
        k = key.split("_")
        if any(tokens[i:i + len(k)] == k for i in range(len(tokens))):
            return text
    return "No plain-English description is available for this feature yet."


class Tools:
    def __init__(self, store: ReportStore, default_id: Optional[str] = None):
        self.store, self.default_id = store, default_id

    def _report(self, rid=None) -> Report:
        rid = rid or self.default_id
        r = self.store.get(rid)
        if r is None:
            raise KeyError(f"report '{rid}' is not loaded")
        return r

    def get_report(self, recording_id=None):
        r = self._report(recording_id)
        return {
            "recording_id": r.recording_id, "answer": r.answer, "answer_reasons": r.answer_reasons,
            "data_quality": r.data_quality, "scores": r.scores, "consistency": r.consistency,
            "flagged_features": [_feature_dict(f) for f in r.flagged_features()],
            "all_feature_names": [f.name for f in r.features],
            "top_contributors": r.top_contributors, "context": r.context,
            "has_longitudinal": bool(r.longitudinal), "confound_warnings": r.confound_warnings,
            "guarantee_note": r.guarantee_note, "limitations": r.limitations,
            "exploratory_unscored_blocks": r.exploratory,
            "schema_version": r.schema_version, "model_version": r.model_version,
            "consistency_detail": r.consistency_detail, "decision_rule": r.decision_rule,
            "n_calibration": r.n_calibration, "simulated": r.simulated,
            "inconclusive_cause": r.inconclusive_cause, "model_sha256": (r.audit or {}).get("model_sha256"),
        }

    def explain_flag(self, feature_name, recording_id=None):
        r = self._report(recording_id)
        f = r.feature(feature_name)
        if f is None:
            return {"error": f"feature '{feature_name}' not in report",
                    "available": [x.name for x in r.features]}
        return {"feature": _feature_dict(f), "meaning": glossary(f.name),
                "outside_typical_range": f in r.flagged_features(),
                "is_top_contributor": any(feature_name in json.dumps(c) for c in r.top_contributors)}

    def compare_visits(self, id_a, id_b):
        a, b = self.store.get(id_a), self.store.get(id_b)
        out = {"id_a": id_a, "id_b": id_b, "loaded": {id_a: a is not None, id_b: b is not None}}
        for r in (a, b):
            if r and r.longitudinal:
                out[f"longitudinal_in_{r.recording_id}"] = r.longitudinal
        if a and b:
            out["answers"] = {id_a: a.answer, id_b: b.answer}
            out["features"] = [{"name": fa.name, "label": fa.label, "unit": fa.unit,
                                id_a: {"value": fa.value, "z": fa.z},
                                id_b: {"value": fb.value, "z": fb.z}}
                               for fa in a.features if (fb := b.feature(fa.name)) is not None]
        return out

    def list_confounds(self, recording_id=None):
        r = self._report(recording_id)
        ctx_keys = ("medications", "sleep_h", "caffeine", "symptoms", "affected_side", "handedness", "age",
                    "sex", "mu_band_source", "preprocessing")
        c = r.context if isinstance(r.context, dict) else {}
        return {"confound_warnings": r.confound_warnings,
                "context": {k: c[k] for k in ctx_keys if k in c},
                "data_quality_status": r.data_quality.get("status"),
                "data_quality_notes": r.data_quality.get("notes", []),
                "bad_channels": r.data_quality.get("bad_channels", []),
                "consistency": r.consistency}

    def draft_summary(self, recording_id=None):
        return {"draft": offline_summary(self._report(recording_id))}

    def call(self, name, args):
        fn: Callable = getattr(self, name, None)
        if name not in {t["name"] for t in TOOL_SPECS} or fn is None:
            return {"error": f"unknown tool {name}"}
        try:
            return fn(**args)
        except Exception as e:  # noqa: BLE001 - report errors back to the model
            return {"error": repr(e)}


# ------------------------------------------------------------------ offline (no-LLM) answers
def _g(x):
    return "n/a" if x is None else f"{x:g}"


def _frame(r: Report, body_lines):
    return "\n".join([HEADER + ".", f"Result: {r.answer}", ""] + body_lines)


def offline_answer(question: str, r: Report, tools: Tools) -> str:
    """Deterministic answer built only from report values. Used without an API key and as fallback."""
    q = (question or "").lower()
    dq = r.data_quality
    failed = dq.get("status") == "FAIL"
    if r.exploratory and re.search(r"\b(arms?|reach\w*|wrists?|elbows?|exploratory)\b", q):
        lines = ["Exploratory arm blocks (not scored):"] + [f"- {x}" for x in r.exploratory]
        lines += ["", f"Data quality: {dq.get('status')}"]
        if failed:
            lines.append("This recording did not pass quality control; these descriptive arm-block values "
                         "are not interpreted.")
        elif r.answer == "INCONCLUSIVE_REPEAT":
            lines.append("The result is inconclusive; these descriptive arm-block values are not interpreted.")
        return _frame(r, lines + ["", footer(r)])
    if r.answer == "INCONCLUSIVE_REPEAT" or failed:
        if not any(k in q for k in ("confound", "medication", "previous", "compare", "visit")):
            lines = [f"- {x}" for x in r.answer_reasons]
            lines += ["", f"Data quality: {dq.get('status')}"]
            if dq.get("bad_channels"):
                lines.append("Bad channels: " + ", ".join(dq["bad_channels"]))
            for k, v in (dq.get("clean_trials_per_condition") or {}).items():
                lines.append(f"Clean trials, {k.replace('_', ' ')}: {_g(v)}")
            if dq.get("n_trials_total") is not None:
                lines.append(f"Trials scored (all conditions and hands): {_g(dq['n_trials_total'])}")
            if r.consistency:
                lines.append(f"Consistency: {r.consistency}")
            if dq.get("notes"):
                head = "Data-quality notes:" if r.calibration_pending else "What to fix before repeating:"
                lines += ["", head] + [f"- {n}" for n in dq["notes"]]
            lines += ["", not_interpreted(r)]
            return _frame(r, lines + ["", footer(r)])       # footer carries confounds, limitations, guarantee
    named = [f for f in r.features if f.name.lower() in q]
    if named or "explain" in q or "why" in q or "flag" in q:
        feats = named or r.flagged_features()
        lines = [f"- {x}" for x in r.answer_reasons] or ["- No answer reasons were given in the report."]
        if feats:
            lines.append("")
        for f in feats:
            lines.append(f"{f.label}: {_g(f.value)} {f.unit} (z = {_g(f.z)}, percentile {_g(f.percentile)})")
            lines.append(f"  {glossary(f.name)}")
        lines += ["", f"Data quality: {r.data_quality.get('status')}"]
        lines += [f"Possible confound: {w}" for w in r.confound_warnings]
        return _frame(r, lines + ["", footer(r)])
    if "confound" in q or "medication" in q or "sleep" in q or "caffeine" in q:
        c = tools.list_confounds(r.recording_id)
        lines = [f"- {w}" for w in c["confound_warnings"]] or ["- The report lists no confound warnings."]
        lines += [f"Context recorded: {k} = {v}" for k, v in c["context"].items() if v not in (None, [], "")]
        lines += [f"Data quality: {c['data_quality_status']}"] + [f"- {n}" for n in c["data_quality_notes"]]
        return _frame(r, lines + ["", footer(r)])
    if "previous" in q or "compare" in q or "change" in q or "visit" in q:
        lon = r.longitudinal
        if not lon:
            return _frame(r, ["This report has no earlier visit to compare with.", "", footer(r)])
        lines = [f"Compared with {lon.get('previous_visit')}:"]
        lines += [f"- {c.get('feature')}: {c.get('direction')} (RCI {_g(c.get('rci'))})" for c in lon.get("changes", [])]
        lines += ["", f"Data quality this visit: {r.data_quality.get('status')}"]
        return _frame(r, lines + ["", footer(r)])
    return offline_summary(r)


# ------------------------------------------------------------------ copilot
VERIFIER_MODEL = os.environ.get("NEUROSYM_VERIFIER_MODEL", "claude-haiku-5-5")
VERIFIER_PROMPT = """You audit an answer written for a clinician about one EEG test report.
REPORT FACTS (JSON):
{facts}

ANSWER:
{answer}

List every problem of these kinds:
- a claim not supported by the facts, or a number attached to the wrong measure
- left/right hemisphere or hand swapped (left hemisphere = C3 = right hand; right hemisphere = C4 = left hand)
- feature values interpreted as findings although data quality is FAIL or the answer is INCONCLUSIVE_REPEAT
- anything implying a diagnosis, a cause, a prognosis, or reassurance
- missing mention of data quality, of confound warnings (if any), or of the limitations
Wording and style are not problems. Reply with JSON only:
{{"supported": true or false, "problems": ["short description", ...]}}"""


@dataclass
class Reply:
    text: str
    mode: str                       # "llm" | "llm-retry" | "offline" | "fallback"
    recording_id: str
    tool_calls: list = field(default_factory=list)
    guardrail_problems: list = field(default_factory=list)   # problems of the text actually shown ([] expected)
    rejected_attempts: list = field(default_factory=list)    # LLM drafts that failed guardrails
    verified: Optional[bool] = None                           # claim verifier result (LLM mode only)
    usage: dict = field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0, "api_calls": 0})
    latency_s: float = 0.0


def footer(r: Report) -> str:
    """Standard footer, built only from report values, appended to every LLM answer."""
    lines = ["---", f"Data quality: {r.data_quality.get('status')}"
             + (f" (bad channels: {', '.join(r.data_quality['bad_channels'])})" if r.data_quality.get("bad_channels") else "")]
    if r.data_quality.get("n_trials_total") is not None:
        lines.append(f"Trials scored (all conditions and hands): {_g(r.data_quality['n_trials_total'])}")
    lines += [f"Confound: {w}" for w in r.confound_warnings] or ["Confounds: none listed in the report."]
    lines += [f"Limitation: {x}" for x in r.limitations]
    if r.guarantee_note:
        lines.append("Statistical note: " + r.guarantee_note)
    return "\n".join(lines)


def _ensure_frame(text: str, r: Report) -> str:
    """Header, answer label and footer are fixed framing, added deterministically."""
    text = text.strip()
    cut = text.find("---\nData quality:")          # drop any footer the LLM wrote itself
    if cut >= 0:
        text = text[:cut].rstrip()
    text = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("Statistical note:")).strip()
    text = f"{text}\n\n{footer(r)}"               # the real footer is always appended
    if r.answer not in text:
        text = f"Result: {r.answer}\n\n{text}"
    if HEADER.lower() not in text.lower():
        text = f"{HEADER}.\n{text}"
    return text


class Copilot:
    def __init__(self, store: ReportStore, client: Any = None, model: str = DEFAULT_MODEL,
                 audit_dir: Optional[Path] = None, use_llm: Optional[bool] = None,
                 verify: bool = True, verifier_model: str = VERIFIER_MODEL):
        self.store, self.model, self._audit_dir = store, model, audit_dir
        self.verify, self.verifier_model = verify, verifier_model
        if client is None and (use_llm is None or use_llm) and os.environ.get("ANTHROPIC_API_KEY"):
            try:
                import anthropic
                client = anthropic.Anthropic()
            except Exception:  # noqa: BLE001 - no SDK -> offline mode
                client = None
        self.client = client if use_llm is not False else None

    @property
    def mode(self):
        return "llm" if self.client else "offline"

    # -- public
    def ask(self, question: str, recording_id: str, history: Optional[list] = None) -> Reply:
        r = self.store.get(recording_id)
        if r is None:
            raise KeyError(f"report '{recording_id}' is not loaded")
        tools = Tools(self.store, recording_id)
        t0 = time.time()
        if not self.client:
            reply = self._checked_offline(question, r, tools, mode="offline")
        else:
            reply = self._ask_llm(question, r, tools, history or [])
        reply.latency_s = round(time.time() - t0, 2)
        self._log(question, reply, r)
        return reply

    def draft(self, recording_id: str) -> Reply:
        return self.ask("Draft an editable clinician summary of this report.", recording_id)

    # -- internals
    def _checked_offline(self, question, r, tools, mode, tool_calls=None, rejected=None):
        text = offline_answer(question, r, tools)
        probs = check_text(text, r)
        if probs:  # the template should never fail, but never show unchecked text
            text, probs = offline_summary(r), check_text(offline_summary(r), r)
        return Reply(text, mode, r.recording_id, tool_calls or [], probs, rejected or [])

    def _ask_llm(self, question, r, tools, history):
        calls, rejected = [], []
        usage = {"input_tokens": 0, "output_tokens": 0, "api_calls": 0}
        user = (f"Report under review: {r.recording_id}. Use the tools to read it.\n\n"
                f"Clinician question: {question}")
        messages = _history_messages(history) + [{"role": "user", "content": user}]
        for attempt in range(2):
            try:
                text = self._run_tool_loop(messages, tools, calls, usage)
            except Exception as e:  # noqa: BLE001 - API/network failure -> offline
                rejected.append({"error": repr(e)})
                break
            text = _ensure_frame(text, r)
            probs = check_text(text, r)
            verified = None
            if not probs and self.verify:
                verified, vprobs = self._verify(text, r, tools, usage)
                probs = [f"verifier: {p}" for p in vprobs]
            if not probs:
                rep = Reply(text, "llm" if attempt == 0 else "llm-retry", r.recording_id, calls, [], rejected)
                rep.verified, rep.usage = verified, usage
                return rep
            rejected.append({"text": text, "problems": probs})
            messages = messages + [
                {"role": "assistant", "content": text},
                {"role": "user", "content": "Your answer failed the guardrails: " + "; ".join(probs)
                 + ". Rewrite it fixing every item. Use only numbers from the tool results, exactly as given."}]
        rep = self._checked_offline(question, r, tools, "fallback", calls, rejected)
        rep.usage = usage
        return rep

    def _verify(self, text, r, tools, usage):
        """Second, independent LLM pass that checks claims against the report. Fails open (verified=None)
        on API or parse errors, which is logged and shown in the app."""
        facts = tools.get_report(r.recording_id)
        facts["features"] = [_feature_dict(f) for f in r.features]
        facts["longitudinal"] = r.longitudinal
        try:
            resp = self.client.messages.create(
                model=self.verifier_model, max_tokens=1500,
                messages=[{"role": "user", "content": VERIFIER_PROMPT.format(
                    facts=json.dumps(facts, default=str), answer=text)}])
            _add_usage(usage, resp)
            raw = "".join(getattr(b, "text", "") for b in resp.content)
            data = json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
            problems = [str(p) for p in data.get("problems", [])]
            ok = bool(data.get("supported", not problems)) and not problems
            return ok, ([] if ok else problems or ["verifier marked the answer unsupported"])
        except Exception as e:  # noqa: BLE001
            usage.setdefault("verifier_errors", []).append(repr(e)[:300])
            return None, []

    def _run_tool_loop(self, messages, tools, calls, usage=None):
        msgs = list(messages)
        for _ in range(MAX_TOOL_ROUNDS):
            resp = self.client.messages.create(model=self.model, max_tokens=1200, system=SYSTEM_PROMPT,
                                               tools=TOOL_SPECS, messages=msgs)
            if usage is not None:
                _add_usage(usage, resp)
            blocks = list(resp.content)
            if resp.stop_reason != "tool_use":
                return "\n".join(getattr(b, "text", "") for b in blocks if getattr(b, "type", "") == "text")
            msgs.append({"role": "assistant", "content": _assistant_content(blocks)})
            results = []
            for b in blocks:
                if getattr(b, "type", "") == "tool_use":
                    out = tools.call(b.name, dict(b.input or {}))
                    calls.append({"tool": b.name, "input": dict(b.input or {})})
                    results.append({"type": "tool_result", "tool_use_id": b.id,
                                    "content": json.dumps(out, default=str)})
            msgs.append({"role": "user", "content": results})
        raise RuntimeError("tool loop did not finish")

    def _log(self, question, reply: Reply, r: Report):
        d = Path(self._audit_dir) if self._audit_dir else audit_dir()
        d.mkdir(parents=True, exist_ok=True)
        entry = {"ts_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "recording_id": r.recording_id,
                 "model_version": r.model_version, "schema_version": r.schema_version,
                 "agent_mode": reply.mode, "llm_model": self.model if self.client else None,
                 "prompt": question, "tool_calls": reply.tool_calls, "output": reply.text,
                 "check_text": reply.guardrail_problems, "rejected_attempts": reply.rejected_attempts,
                 "verified": reply.verified, "usage": reply.usage, "latency_s": reply.latency_s}
        with open(d / "agent_log.jsonl", "a") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")


def _add_usage(usage, resp):
    u = getattr(resp, "usage", None)
    usage["api_calls"] += 1
    if u is not None:
        usage["input_tokens"] += int(getattr(u, "input_tokens", 0) or 0)
        usage["output_tokens"] += int(getattr(u, "output_tokens", 0) or 0)


def _history_messages(history, max_turns=4):
    """history = [(question, answer_text), ...] from earlier turns on the same report (plain text only)."""
    out = []
    for q, a in list(history)[-max_turns:]:
        out += [{"role": "user", "content": q}, {"role": "assistant", "content": a}]
    return out


def _block_to_dict(b):
    """Send the model's own blocks back unchanged. Converting unknown blocks (e.g. thinking blocks)
    to empty text blocks makes the next API call fail with a 400."""
    if hasattr(b, "model_dump"):
        return b.model_dump(exclude_none=True)
    t = getattr(b, "type", "")
    if t == "tool_use":
        return {"type": "tool_use", "id": b.id, "name": b.name, "input": dict(b.input or {})}
    if t == "text":
        return {"type": "text", "text": getattr(b, "text", "")}
    return {k: v for k, v in vars(b).items() if v is not None}


def _assistant_content(blocks):
    out = [_block_to_dict(b) for b in blocks]
    return [d for d in out if not (d.get("type") == "text" and not str(d.get("text", "")).strip())]


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        sys.exit("usage: python agent.py report.json [question]")
    store = ReportStore.default()
    rep = store.load_file(sys.argv[1]) or sys.exit(f"could not load {sys.argv[1]}: {store.load_errors}")
    bot = Copilot(store)
    out = bot.ask(sys.argv[2] if len(sys.argv) > 2 else "Summarize this report.", rep.recording_id)
    print(f"[mode: {out.mode}; guardrails: {'pass' if not out.guardrail_problems else out.guardrail_problems}]\n")
    print(out.text)
