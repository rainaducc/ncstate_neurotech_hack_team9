"""
agent.py - NeuroSym clinician copilot agent.

Reads model reports ONLY through report_adapter.load_report(). Every user-visible text
passes report_adapter.check_text(): answer label verbatim, mandatory header, no diagnostic
language, only numbers that appear in the report. If the LLM output fails guardrails it is
regenerated once; if it still fails, we fall back to offline_summary.summary(report).

Works fully offline: without ANTHROPIC_API_KEY (or on any API error) ask() returns the
template summary plus rule-based answers to common questions. All interactions are logged
to audit/agent_log.jsonl.

CLI:  python agent.py                      # interactive REPL over the mocks
      python agent.py mocks/within_range.json "summarize this recording"
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from offline_summary import summary
from report_adapter import HEADER, Report, check_text, load_report

AUDIT_DIR = ROOT / "audit"
AUDIT_LOG = AUDIT_DIR / "agent_log.jsonl"
MODEL = os.environ.get("NEUROSYM_AGENT_MODEL", "claude-sonnet-4-5-20250929")

REPORT_DIRS = [ROOT / "mocks", ROOT / "contract" / "samples"]


# ------------------------------------------------------------------ report registry
def available_reports() -> dict[str, Path]:
    """All report files visible to the agent, keyed by recording_id."""
    out: dict[str, Path] = {}
    for d in REPORT_DIRS:
        if d.exists():
            for p in sorted(d.glob("*.json")):
                try:
                    out[load_report(p).recording_id] = p
                except Exception:
                    continue
    return out


def get_report(recording_id: str, registry: dict[str, Path] | None = None) -> Report:
    reg = registry if registry is not None else available_reports()
    if recording_id in reg:
        return load_report(reg[recording_id])
    raise KeyError(f"no report with recording_id {recording_id!r}; known: {sorted(reg)}")


# ------------------------------------------------------------------ feature knowledge
FEATURE_EXPLAIN = {
    "erd_mu": "mu-band event-related desynchronization over one motor cortex while the hand moves - a measure of motor-cortex engagement. Stronger mu suppression generally means stronger engagement.",
    "erd_beta": "beta-band event-related desynchronization over one motor cortex during movement - another engagement measure, sensitive to different cortical populations.",
    "sym_": "hemisphere symmetry: compares the response of the left vs right motor cortex for the same hand. Large values mean one hemisphere responded much more strongly than the other.",
    "li_": "lateralization index: how dominant the correct (contralateral) hemisphere is for a given hand. Near zero means weak or reversed lateralization.",
    "pmbr_": "post-movement beta rebound: the beta 'rebound' after movement ends, thought to reflect cortical reset. Reduced rebound is a common atypical pattern.",
    "resting_": "resting (baseline) power with no movement task - the background activity the movement responses are compared against.",
}


def explain_flag(feature_name: str, report: Report) -> str:
    f = report.feature(feature_name)
    if f is None:
        f = next((x for x in report.features if feature_name.lower() in x.name.lower()), None)
    if f is None:
        return (f"{HEADER}. {report.answer}. No feature named {feature_name!r} in this report. "
                "Available features: " + ", ".join(x.name for x in report.features) + ".")
    meaning = next((v for k, v in FEATURE_EXPLAIN.items() if f.name.startswith(k)),
                   "A quantitative EEG feature from the normative model.")
    parts = [f"{HEADER}.", f"Result for this recording: {report.answer}.",
             f"Feature {f.name}: {f.label or f.name}.", meaning]
    if f.z is not None:
        flag = ("outside the typical healthy range for this feature"
                if abs(f.z) >= 1.96 else "within the typical healthy range for this feature")
        parts.append(f"Value: {_n(f.value)} {f.unit} (z = {_n(f.z)}, percentile {_n(f.percentile)}) - {flag}.")
    if report.data_quality.get("status"):
        parts.append(f"Data quality for this recording: {report.data_quality.get('status')}.")
    parts.append("This describes deviation from healthy reference data, not a diagnosis.")
    return "\n".join(parts)


def _n(x) -> str:
    return "n/a" if x is None else f"{x:g}"


def list_confounds(report: Report) -> str:
    lines = [f"{HEADER}. Result: {report.answer}.", "", "Possible confounds and context:"]
    if report.confound_warnings:
        lines += [f"- {w}" for w in report.confound_warnings]
    else:
        lines.append("- None recorded in the report.")
    if report.context:
        c = report.context
        ctx = ", ".join(f"{k} = {_n(v) if isinstance(v, (int, float)) else v}"
                        for k, v in c.items() if v not in (None, "", []))
        lines.append(f"- Session context: {ctx}.")
    dq = report.data_quality
    lines.append(f"- Data quality: {dq.get('status')}"
                 + (f" (bad channels: {', '.join(dq['bad_channels'])})" if dq.get("bad_channels") else "") + ".")
    if dq.get("notes"):
        lines += [f"- {note}" for note in dq["notes"]]
    lines.append(f"- Statistical note: {report.guarantee_note}")
    return "\n".join(lines)


def compare_visits(recording_id_a: str, recording_id_b: str, registry=None) -> str:
    ra, rb = get_report(recording_id_a, registry), get_report(recording_id_b, registry)
    lines = [f"{HEADER}.", f"Current result: {ra.answer}. Comparison with {rb.recording_id} ({rb.answer})."]
    by_name_b = {f.name: f for f in rb.features}
    for f in ra.features:
        g = by_name_b.get(f.name)
        if g is None or f.z is None or g.z is None:
            continue
        diff = f.z - g.z
        if abs(diff) < 0.5:
            continue
        direction = "higher" if diff > 0 else "lower"
        lines.append(f"- {f.label or f.name}: {direction} than the comparison visit (z = {_n(f.z)} vs {_n(g.z)}).")
    if ra.longitudinal and ra.longitudinal.get("changes"):
        lines.append(f"Longitudinal analysis in this report (previous visit {ra.longitudinal.get('previous_visit')}):")
        for c in ra.longitudinal["changes"]:
            lines.append(f"- {c.get('feature')}: {c.get('direction')} (RCI {_n(c.get('rci'))}).")
    lines.append("Changes describe deviation from healthy reference data, not a diagnosis.")
    return "\n".join(lines)


def draft_summary(report: Report) -> str:
    return summary(report)


TOOLS_SPEC = [
    {"name": "get_report", "description": "Load a NeuroSym report by recording_id and return its answer, data quality, flagged features, scores, confounds and limitations.",
     "input_schema": {"type": "object", "properties": {"recording_id": {"type": "string"}}, "required": ["recording_id"]}},
    {"name": "explain_flag", "description": "Explain what one EEG feature means and whether it is flagged in the current report.",
     "input_schema": {"type": "object", "properties": {"feature_name": {"type": "string"}}, "required": ["feature_name"]}},
    {"name": "compare_visits", "description": "Compare the current recording with another visit by recording_id.",
     "input_schema": {"type": "object", "properties": {"recording_id_a": {"type": "string"}, "recording_id_b": {"type": "string"}}, "required": ["recording_id_a", "recording_id_b"]}},
    {"name": "list_confounds", "description": "List confound warnings, session context and data-quality notes for the current report.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "draft_summary", "description": "Produce the editable template clinician summary for the current report.",
     "input_schema": {"type": "object", "properties": {}}},
]


SYSTEM_PROMPT = """You are the NeuroSym clinician copilot, a research prototype for clinicians reviewing EEG normative-model reports.

Hard rules:
- Always state the report's answer label verbatim (e.g. WITHIN_NORMATIVE_RANGE).
- Start every user-visible answer with the header: "Research prototype - not a diagnosis - for clinician review".
- Never diagnose or name a disease as the cause of any finding. You describe deviation from healthy reference data.
- Always mention data quality, confounds, limitations and the guarantee note when they are relevant.
- If the answer is INCONCLUSIVE_REPEAT, explain why and what to fix before repeating.
- Only use numbers that appear in the report. Never invent or round numbers.
- Use the provided tools to read report data; never guess report contents."""


def _report_brief(report: Report) -> str:
    """Compact, read-only digest of the Report given to the LLM as context."""
    return json.dumps({
        "recording_id": report.recording_id, "answer": report.answer,
        "answer_reasons": report.answer_reasons,
        "data_quality": report.data_quality,
        "features": [{"name": f.name, "value": f.value, "z": f.z, "percentile": f.percentile,
                      "unit": f.unit, "hemisphere": f.hemisphere, "hand": f.hand,
                      "condition": f.condition} for f in report.features],
        "scores": report.scores, "confound_warnings": report.confound_warnings,
        "guarantee_note": report.guarantee_note, "limitations": report.limitations,
    }, default=str)


class Copilot:
    """Clinician copilot over one report. LLM via Anthropic tool calling, offline fallback."""

    def __init__(self, report: Report):
        self.report = report
        self.client = None
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if api_key:
            try:
                import anthropic
                self.client = anthropic.Anthropic(api_key=api_key)
            except Exception:
                self.client = None

    # -- tool dispatch -------------------------------------------------
    def _run_tool(self, name: str, inp: dict) -> str:
        r = self.report
        if name == "get_report":
            target = get_report(inp["recording_id"])
            return _report_brief(target)
        if name == "explain_flag":
            return explain_flag(inp["feature_name"], r)
        if name == "list_confounds":
            return list_confounds(r)
        if name == "compare_visits":
            return compare_visits(inp["recording_id_a"], inp["recording_id_b"])
        if name == "draft_summary":
            return draft_summary(r)
        return f"unknown tool {name}"

    # -- offline answers -------------------------------------------------
    def _offline_answer(self, question: str) -> str:
        q = question.lower()
        if any(w in q for w in ("confound", "medication", "quality", "artifact")):
            return list_confounds(self.report)
        if "compare" in q or "visit" in q or "previous" in q:
            if self.report.longitudinal and self.report.longitudinal.get("previous_visit"):
                try:
                    return compare_visits(self.report.recording_id, self.report.longitudinal["previous_visit"])
                except KeyError:
                    lg = self.report.longitudinal
                    lines = [f"{HEADER}. Result: {self.report.answer}.",
                             f"Comparison visit {lg.get('previous_visit')} is not available in this "
                             "workspace, but this report includes its own longitudinal analysis:"]
                    lines += [f"- {c.get('feature')}: {c.get('direction')} (RCI {_n(c.get('rci'))})."
                              for c in lg.get("changes", [])]
                    lines.append("Changes describe deviation from healthy reference data, not a diagnosis.")
                    return "\n".join(lines)
            return (f"{HEADER}. Result: {self.report.answer}. No previous-visit data in this report; "
                    "no comparison is possible.")
        if any(w in q for w in ("feature", "flag", "z", "what does", "explain")):
            flagged = self.report.flagged_features()
            if flagged:
                return "\n\n".join(explain_flag(f.name, self.report) for f in flagged)
            return (f"{HEADER}. Result: {self.report.answer}. No features are flagged in "
                    "this report.")
        return draft_summary(self.report)

    # -- main entry ------------------------------------------------------
    def ask(self, question: str, history: list | None = None) -> dict:
        answer, tool_calls_log, source, violations = None, [], "offline", []
        attempted_llm = False
        if self.client is not None:
            attempted_llm = True
            try:
                answer, tool_calls_log, violations = self._ask_llm(question, history or [])
                source = "llm"
            except Exception as e:  # API failure -> fallback
                tool_calls_log.append({"error": str(e)})
                answer, violations = None, []
        if answer is None or violations:  # missing/failed/guardrail-violating LLM output
            answer = self._offline_answer(question)
            source = "offline_fallback" if attempted_llm else "offline"
            violations = check_text(answer, self.report)
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "recording_id": self.report.recording_id,
            "question": question,
            "tool_calls": tool_calls_log,
            "output": answer,
            "source": source,
            "check_text_violations": violations,
        }
        self._log(entry)
        return entry

    def _ask_llm(self, question: str, history: list):
        messages = ([*history, {"role": "user", "content":
                    f"Report context (read-only):\n{_report_brief(self.report)}\n\nQuestion: {question}"}])
        tool_log, final_text, violations = [], None, []
        for _ in range(4):  # max 4 round trips
            resp = self.client.messages.create(
                model=MODEL, max_tokens=1500, system=SYSTEM_PROMPT,
                tools=TOOLS_SPEC, messages=messages)
            tool_uses = [b for b in resp.content if b.type == "tool_use"]
            texts = [b.text for b in resp.content if b.type == "text"]
            tool_log += [{"tool": t.name, "input": t.input} for t in tool_uses]
            if resp.stop_reason == "tool_use":
                messages = [*messages, {"role": "assistant", "content": resp.content},
                            {"role": "user", "content": [
                                {"type": "tool_result", "tool_use_id": t.id, "content": self._run_tool(t.name, t.input)}
                                for t in tool_uses]}]
                continue
            final_text = "\n".join(texts).strip()
            break
        if final_text:
            violations = check_text(final_text, self.report)
            if violations:  # regenerate once with violations listed
                messages.append({"role": "assistant", "content": final_text})
                messages.append({"role": "user", "content":
                                 "Your answer violated guardrails: " + "; ".join(violations) +
                                 ". Rewrite it fully compliant: keep the header, state the answer label "
                                 "verbatim, use only numbers from the report, no diagnostic language."})
                resp = self.client.messages.create(model=MODEL, max_tokens=1500, system=SYSTEM_PROMPT,
                                                   tools=TOOLS_SPEC, messages=messages)
                final_text = "\n".join(b.text for b in resp.content if b.type == "text").strip()
                violations = check_text(final_text, self.report)
                if violations:
                    return None, tool_log, violations  # -> offline fallback
        else:
            return None, tool_log, []
        return final_text, tool_log, violations

    def _log(self, entry: dict):
        try:
            AUDIT_DIR.mkdir(exist_ok=True)
            with open(AUDIT_LOG, "a") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass


if __name__ == "__main__":
    args = [a for a in sys.argv[1:]]
    report_path = ROOT / "mocks" / "outside_weak_left.json"
    if args and args[0].endswith(".json"):
        report_path = ROOT / args.pop(0) if not Path(args[0]).is_absolute() else Path(args.pop(0))
    rep = load_report(report_path)
    bot = Copilot(rep)
    if args:
        print(bot.ask(" ".join(args))["output"])
    else:
        print(f"NeuroSym copilot over {rep.recording_id} ({rep.answer}). "
              f"Mode: {'LLM' if bot.client else 'offline'}. Type 'quit' to exit.")
        while True:
            try:
                q = input("clinician> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if q.lower() in {"quit", "exit"}:
                break
            if q:
                print(bot.ask(q)["output"], "\n")
