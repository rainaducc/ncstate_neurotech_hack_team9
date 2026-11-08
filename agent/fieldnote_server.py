"""
fieldnote_server.py - connects the Fieldnote UI to the NeuroSym agent.

Serves the Fieldnote dist/ plus three agent endpoints:

  GET  /api/health            -> {ok, llm}  (llm=true when ANTHROPIC_API_KEY is set)
  POST /api/story/summary     -> AI-drafted pre-visit account from the patient's answers
                                 (patient's own words; no added symptoms; no diagnosis language)
  POST /api/story/chat        -> "Ask the guide" chat on the My story page
  POST /api/task/feedback     -> descriptive feedback on a finger-tapping recording
                                 (only numbers computed from the recording; no clinical claims)

Every LLM output passes guardrails (diagnostic-language patterns; for feedback, every number
must come from the computed stats). On violation or API failure -> deterministic template
fallback, so the demo always works. All interactions are appended to audit/fieldnote_log.jsonl.

Run (from neurosym-agent/):
  source .venv/bin/activate
  export ANTHROPIC_API_KEY=sk-ant-...      # optional; without it, template mode
  python fieldnote_server.py               # -> http://localhost:8080
"""
import json
import os
import re
import sys
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from report_adapter import DIAGNOSTIC_PATTERNS  # shared guardrail patterns

DIST = Path(os.environ.get(
    "FIELDNOTE_DIST", ROOT.parent / "fieldnote-ui" / "fieldnote" / "dist")).resolve()
PORT = int(os.environ.get("FIELDNOTE_PORT", "8080"))
AUDIT = ROOT / "audit" / "fieldnote_log.jsonl"

_client = None
_model = os.environ.get("NEUROSYM_AGENT_MODEL", "claude-sonnet-4-5-20250929")


def llm():
    global _client
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic()
    return _client


def guarded(text: str, allowed_numbers=None, forbid_normative=False) -> bool:
    """No diagnostic language; if allowed_numbers given, every number must be allowed;
    forbid_normative also blocks normal/common/typical-range claims (task feedback only)."""
    extra = [
        r"\b(could|may|might)\s+(be|have|indicate|suggest)\b[^\n]{0,60}"
        r"(parkinson'?s?|stroke|multiple sclerosis|\bms\b|als|dementia|alzheimer'?s?|neurodegenerat\w*)",
        r"\b(suggest|indicates?|points? to(wards)?)\b[^\n]{0,60}\bdisease\b",
        r"\byou (have|may have|might have)\b",
    ]
    if forbid_normative:
        extra.append(r"\b(is|are|seems?|appears?)\s+(to\s+be\s+)?(very\s+|quite\s+)?"
                     r"(common|normal|typical|usual|abnormal|atypical|unusual)\b")
        extra.append(r"\bwithin\s+(the\s+)?(normal|healthy|typical)\s+(range|limits)\b")
    for pat in DIAGNOSTIC_PATTERNS + extra:
        if re.search(pat, text, flags=re.IGNORECASE):
            return False
    if allowed_numbers is not None:
        for tok in re.findall(r"(?<![\w.])-?\d+(?:\.\d+)?", text):
            val = float(tok)
            if not any(abs(val - a) < 1e-9 for a in allowed_numbers):
                return False
    return True


def ask_llm(system: str, prompt: str, max_tokens=800) -> str | None:
    c = llm()
    if c is None:
        return None
    try:
        r = c.messages.create(model=_model, max_tokens=max_tokens, system=system,
                              messages=[{"role": "user", "content": prompt}])
        return "\n".join(b.text for b in r.content if b.type == "text").strip()
    except Exception as e:
        print("[fieldnote] LLM error:", e)
        return None


def log(kind: str, payload: dict, output: str, source: str, guard_ok: bool):
    entry = {"ts": datetime.now(timezone.utc).isoformat(), "kind": kind,
             "input": payload, "output": output, "source": source, "guardrails_ok": guard_ok}
    try:
        AUDIT.parent.mkdir(exist_ok=True)
        with open(AUDIT, "a") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


# ------------------------------------------------------------------ story summary
SUMMARY_SYSTEM = """You are Fieldnote's conversation guide, helping a patient prepare a short
pre-visit account of a movement concern for their clinician. Draft a brief first-person account
from their four answers.
Rules:
- Keep the patient's own words and phrasing wherever possible; you may smooth the flow.
- Group by topic: what feels different, when it started, which side/body area, effect on daily life.
- Never add symptoms, causes, medical terms, or details the patient did not mention.
- Never diagnose, speculate, or mention any disease.
- Plain, warm language. At most ~140 words. Output only the account text."""

SUMMARY_FALLBACK = "What feels different\n{0}\n\nWhen it started\n{1}\n\nSide or body area\n{2}\n\nEffect on daily life\n{3}"


def story_summary(answers: list) -> tuple[str, str, bool]:
    answers = [str(a)[:3000] for a in (answers or [])]
    prompt = "Patient answers:\n" + "\n".join(f"- {a}" for a in answers)
    out = ask_llm(SUMMARY_SYSTEM, prompt)
    if out and guarded(out):
        return out, "llm", True
    if len(answers) >= 4:
        fb = SUMMARY_FALLBACK.format(*[a for a in (answers + ["", "", "", ""])[:4]])
    else:
        fb = "\n\n".join(f"- {a}" for a in answers)
    return fb, "template", out is None or guarded(out or "")


# ------------------------------------------------------------------ guide chat
CHAT_SYSTEM = """You are Fieldnote's conversation guide inside a patient-facing pre-visit tool.
You help the patient describe a movement concern in their own words before a first appointment.
You can: explain what the questions are for, suggest what kind of everyday detail is useful,
encourage them to check and correct the drafted summary.
You must never: diagnose, mention any disease or condition, interpret symptoms clinically,
add symptoms they did not mention, or give medical advice. If asked for medical judgment,
explain that this is a preparation tool and questions belong with the clinician.
Keep replies short (1-4 sentences), warm, plain language."""

CHAT_FALLBACK = ("In this prototype I can help you prepare your account: add detail to any answer, "
                 "check the drafted summary beside you, and edit anything that is not your wording. "
                 "Questions about what something means medically are best saved for your clinician.")


def story_chat(answers: list, messages: list) -> tuple[str, str, bool]:
    c = llm()
    hist = []
    for m in (messages or [])[-8:]:
        role = "user" if m.get("role") == "user" else "assistant"
        hist.append({"role": role, "content": str(m.get("text", ""))[:2000]})
    if c is not None and hist:
        try:
            r = c.messages.create(
                model=_model, max_tokens=400, system=CHAT_SYSTEM,
                messages=[{"role": "user", "content": "Patient answers so far:\n" +
                           "\n".join(f"- {a}" for a in (answers or []))}] + hist)
            out = "\n".join(b.text for b in r.content if b.type == "text").strip()
            if out and guarded(out):
                return out, "llm", True
        except Exception as e:
            print("[fieldnote] LLM error:", e)
    return CHAT_FALLBACK, "template", True


# ------------------------------------------------------------------ task feedback
FEEDBACK_SYSTEM = """You are the recording assistant in a patient-facing movement-tracking tool.
You describe a finger-tapping recording back to the person who did it.
You receive JSON stats computed from their key-press timestamps.
Rules:
- Write 3-4 short sentences of plain, encouraging, concrete feedback.
- Use ONLY the numbers in the JSON. Do not compute or invent other numbers.
- Describe pace (mean_interval_ms lower = faster; cv_pct lower = steadier rhythm), compare hands
  if both are present, and suggest one repeat recording.
- Never diagnose, never mention diseases, never compare with a healthy range, and never call a
  pattern common, normal, typical, abnormal or unusual. Counts describe this recording only."""


def tap_stats(left: list, right: list, seconds: int) -> dict:
    def one(ts):
        if len(ts) < 2:
            return {"taps": len(ts)}
        ivs = [b - a for a, b in zip(ts, ts[1:])]
        m = sum(ivs) / len(ivs)
        var = sum((x - m) ** 2 for x in ivs) / len(ivs)
        halves = [ivs[:len(ivs) // 2], ivs[len(ivs) // 2:]]
        half_means = [round(sum(h) / len(h), 1) for h in halves if h]
        return {"taps": len(ts), "mean_interval_ms": round(m, 1),
                "cv_pct": round(100 * (var ** 0.5) / m, 1), "half_means_ms": half_means}
    s = {"seconds": seconds, "left": one(left or []), "right": one(right or [])}
    return s


def allowed_numbers(stats: dict) -> set:
    out = {float(stats["seconds"])}
    for hand in ("left", "right"):
        for k, v in stats[hand].items():
            if isinstance(v, (int, float)):
                out.add(float(v))
            elif isinstance(v, list):
                out.update(float(x) for x in v)
    return out


def tap_feedback(left: list, right: list, seconds: int) -> tuple[str, str, bool]:
    s = tap_stats(left, right, seconds)
    allowed = allowed_numbers(s)
    out = ask_llm(FEEDBACK_SYSTEM, json.dumps(s), max_tokens=400)
    if out and guarded(out, allowed, forbid_normative=True):
        return out, "llm", True
    l, r = s["left"], s["right"]
    parts = [f"You recorded {l.get('taps', 0)} taps with your left hand and {r.get('taps', 0)} with your right hand in {seconds} seconds."]
    if "mean_interval_ms" in l and "mean_interval_ms" in r:
        steadier = ("left" if l.get("cv_pct", 99) <= r.get("cv_pct", 99) else "right")
        parts.append(f"Average interval was {l['mean_interval_ms']} ms for the left hand and {r['mean_interval_ms']} ms for the right; your {steadier} hand had the steadier rhythm this time.")
        parts.append(f"Consistency (lower is steadier): left {l.get('cv_pct')}%, right {r.get('cv_pct')}%.")
    parts.append("These counts describe this recording only — doing one more recording later is a good way to see how much they vary.")
    return " ".join(parts), "template", out is None or guarded(out or "", allowed, forbid_normative=True)


# ------------------------------------------------------------------ http server
class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(DIST), **kw)

    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/health":
            return self._json({"ok": True, "llm": bool(os.environ.get("ANTHROPIC_API_KEY"))})
        return super().do_GET()

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        try:
            data = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return self._json({"error": "bad json"}, 400)
        if self.path == "/api/story/summary":
            out, src, ok = story_summary(data.get("answers"))
            log("story_summary", {"answers": data.get("answers")}, out, src, ok)
            return self._json({"summary": out, "source": src})
        if self.path == "/api/story/chat":
            out, src, ok = story_chat(data.get("answers"), data.get("messages"))
            log("story_chat", {"answers": data.get("answers"), "messages": data.get("messages")},
                out, src, ok)
            return self._json({"reply": out, "source": src})
        if self.path == "/api/task/feedback":
            out, src, ok = tap_feedback(data.get("left"), data.get("right"),
                                        int(data.get("seconds") or 10))
            log("task_feedback", {"left": len(data.get("left") or []),
                                  "right": len(data.get("right") or [])}, out, src, ok)
            return self._json({"feedback": out, "source": src})
        return self._json({"error": "not found"}, 404)


if __name__ == "__main__":
    if not DIST.exists():
        sys.exit(f"Fieldnote dist not found at {DIST}")
    print(f"Fieldnote + NeuroSym agent -> http://localhost:{PORT}  "
          f"(LLM {'ON' if llm() else 'OFF — template mode, set ANTHROPIC_API_KEY'})")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
