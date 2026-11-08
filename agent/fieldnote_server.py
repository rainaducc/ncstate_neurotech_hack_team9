"""
fieldnote_server.py - connects the NeuroBridge UI to the NeuroSym agent.

Serves the NeuroBridge UI (fieldnote-ui/dist/) plus three agent endpoints:

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
import random
import re
import sys
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from report_adapter import DIAGNOSTIC_PATTERNS, _DISEASES, _JUDGMENT  # shared guardrail patterns
import movement_session  # cued movement session: schedule + LSL marker bridge
import shared_store  # shared communication layer: one thread per patient

_DIST_CANDIDATES = [ROOT.parent / "fieldnote-ui" / "dist", ROOT.parent / "fieldnote-ui" / "fieldnote" / "dist"]
DIST = Path(os.environ.get(
    "FIELDNOTE_DIST", next((p for p in _DIST_CANDIDATES if p.exists()), _DIST_CANDIDATES[0]))).resolve()
PORT = int(os.environ.get("FIELDNOTE_PORT", "8080"))
AUDIT = ROOT / "audit" / "fieldnote_log.jsonl"

_client = None
_model = os.environ.get("NEUROSYM_AGENT_MODEL", "claude-sonnet-5-5")  # same default as agent.py


def llm():
    global _client
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic()
    return _client


def guarded(text: str, allowed_numbers=None, forbid_normative=False, source: str = "") -> bool:
    """No diagnostic language; if allowed_numbers given, every number must be allowed;
    forbid_normative also blocks normal/common/typical-range claims (task feedback only).
    Condition names and clinical judgment words (the same lists the copilot guardrails use) are
    blocked unless the patient wrote them in `source` themselves: we keep their words, never add ours."""
    own = (source or "").lower()

    def theirs(phrase):          # the patient wrote this exact phrase as whole words ('ms' in 'arms' is not 'MS')
        phrase = phrase.strip().lower()
        return bool(phrase) and re.search(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", own) is not None

    for pat in (_DISEASES, _JUDGMENT):
        for m in re.finditer(pat, text, flags=re.IGNORECASE):
            if not theirs(m.group(0)):
                return False
    extra = [
        r"\b(has|have|likely has|probably has|consistent with|indicative of|confirms?|suggestive of|evidence of)\s+"
        r"(early\s+)?(parkinson'?s?|stroke|multiple sclerosis|\bms\b|als|dementia|alzheimer'?s?|neurodegenerat\w*)",
        r"\b(could|may|might)\s+(be|have|indicate|suggest)\b[^\n]{0,60}"
        r"(parkinson'?s?|stroke|multiple sclerosis|\bms\b|als|dementia|alzheimer'?s?|neurodegenerat\w*)",
        r"\b(suggest|indicates?|points? to(wards)?)\b[^\n]{0,60}\bdisease\b",
        r"\byou\s+((may|might|could|likely|probably|possibly|also)\s+)?have\b",
        r"\b(may|might|could|would|likely|probably)\s+(explain|cause|account for)\b",   # no causal guesses
    ]
    if forbid_normative:
        extra.append(r"\b(is|are|seems?|appears?)\s+(to\s+be\s+)?(very\s+|quite\s+)?"
                     r"(common|normal|typical|usual|abnormal|atypical|unusual)\b")
        extra.append(r"\bwithin\s+(the\s+)?(normal|healthy|typical)\s+(range|limits)\b")
    for pat in DIAGNOSTIC_PATTERNS + extra:
        for m in re.finditer(pat, text, flags=re.IGNORECASE):
            # the match plus the rest of its clause must be the patient's own words: "you said your mother
            # has Parkinson's disease" passes, "you were diagnosed with <something they never wrote>" does not
            clause = re.split(r"[.,;:!?\n]|\s(?:and|but|or|so)\s", text[m.start():])[0]
            if not theirs(text[m.start():m.start() + max(len(clause), len(m.group(0)))]):
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
        print("[neurobridge] LLM error:", e)
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
SUMMARY_SYSTEM = """You are NeuroBridge's conversation guide, helping a patient prepare a short
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
    if out and guarded(out, source=" ".join(answers)):
        return out, "llm", True
    if len(answers) >= 4:
        fb = SUMMARY_FALLBACK.format(*[a for a in (answers + ["", "", "", ""])[:4]])
    else:
        fb = "\n\n".join(f"- {a}" for a in answers)
    return fb, "template", out is None or guarded(out or "", source=" ".join(answers))


# ------------------------------------------------------------------ guide chat
CHAT_SYSTEM = """You are NeuroBridge's conversation guide inside a patient-facing pre-visit tool.
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
            own = " ".join([str(a) for a in (answers or [])] + [h["content"] for h in hist if h["role"] == "user"])
            if out and guarded(out, source=own):
                return out, "llm", True
        except Exception as e:
            print("[neurobridge] LLM error:", e)
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


# ------------------------------------------------------------------ clinician copilot
# The same Copilot as the Streamlit app (agent.py): reports are read only through
# report_adapter, every answer passes check_text(), and Copilot.ask() writes audit/agent_log.jsonl.
def _store():
    """Reports the clinician can open: agent mocks, contract samples and live reports.
    Loaded through agent.ReportStore (so through report_adapter.load_report) on every call, so a report
    written by live_score.py appears without a restart. A provisional mid-session report is dropped only
    once its final report (same id without '_provisional') is loaded; until then it stays visible,
    marked provisional, so a session without a final report never vanishes."""
    from agent import ReportStore
    s = ReportStore.default()
    for rid in list(s.reports):
        if s.superseded(rid) and s.is_provisional(rid):
            del s.reports[rid]
            s.sources.pop(rid, None)
    return s


def _get(recording_id: str, store=None):
    r = (store or _store()).get(recording_id)
    if r is None:
        raise KeyError(recording_id)
    return r


def clinician_reports() -> list:
    s = _store()
    return [{"recording_id": rid, "answer": s.get(rid).answer, "model_version": s.get(rid).model_version,
             "provisional": s.is_provisional(rid)}
            for rid in s.ids()]


def clinician_report(recording_id: str) -> dict:
    from report_adapter import HEADER
    s = _store()
    r = _get(recording_id, s)
    c = r.context if isinstance(r.context, dict) else {}
    feats = sorted(r.features, key=lambda f: -abs(f.z) if f.z is not None else 0)
    flagged = {f.name for f in r.flagged_features()}
    return {
        "header": HEADER,
        "recording_id": r.recording_id,
        "model_version": r.model_version,
        "answer": r.answer,
        "answer_reasons": r.answer_reasons,
        "data_quality": {
            "status": r.data_quality.get("status"),
            "bad_channels": r.data_quality.get("bad_channels", []),
            "clean_trials_per_condition": r.data_quality.get("clean_trials_per_condition", {}),
            "notes": r.data_quality.get("notes", []),
            "n_trials_total": r.data_quality.get("n_trials_total"),
        },
        "features": [{"name": f.name, "label": f.label or f.name, "value": f.value, "unit": f.unit,
                      "z": f.z, "percentile": f.percentile, "flagged": f.name in flagged}
                     for f in feats],
        "three_state_profile": r.three_state_profile,
        "confound_warnings": r.confound_warnings,
        "guarantee_note": r.guarantee_note,
        "limitations": r.limitations,
        "has_longitudinal": bool(r.longitudinal),
        "simulated": r.is_simulated,
        "exploratory": r.exploratory,
        "consistency": r.consistency if isinstance(r.consistency, str) else "",
        "schema_version": r.schema_version,
        # added keys (the UI renders them only when present)
        "provisional": s.is_provisional(recording_id),
        "consistency_detail": r.consistency_detail,
        "n_calibration": r.n_calibration,
        "inconclusive_cause": r.inconclusive_cause,
        "scores": {k: {"score": v.get("score"), "p": v.get("p")} for k, v in (r.scores or {}).items()
                   if isinstance(v, dict)},
        "context": {k: c[k] for k in ("age", "sex", "handedness", "medications", "sleep_h", "caffeine",
                                      "symptoms", "affected_side") if k in c},
    }


def _clean_history(history) -> list:
    """Client chat history [{role, content}, ...] -> [(question, answer), ...] pairs, the format
    agent.Copilot.ask() expects. Unpaired or malformed turns are dropped; at most 4 pairs are kept."""
    pairs, q = [], None
    for m in (history or [])[-8:]:
        if not isinstance(m, dict):
            continue
        role, content = m.get("role"), m.get("content")
        if not (isinstance(content, str) and content.strip()):
            continue
        if role == "user":
            q = content[:1000]
        elif role == "assistant" and q is not None:
            pairs.append((q, content[:4000]))
            q = None
    return pairs[-4:]


def clinician_ask(recording_id: str, question: str, history) -> dict:
    from agent import Copilot
    s = _store()
    _get(recording_id, s)
    reply = Copilot(s).ask(question[:1000], recording_id, _clean_history(history))
    return {"answer": reply.text, "source": reply.mode,
            "tools": [t.get("tool") for t in reply.tool_calls if isinstance(t, dict) and t.get("tool")],
            "violations": reply.guardrail_problems, "verified": reply.verified}


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
        if self.path.split("?")[0] == "/api/patients" or self.path.startswith("/api/patients/"):
            return self._patients_get()
        if self.path == "/api/session/devices":
            return self._json(movement_session.devices())
        if self.path.startswith("/api/session/schedule"):
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query)
            try:
                return self._json(movement_session.schedule((q.get("mode") or ["full"])[0],
                                                             int((q.get("seed") or ["7"])[0])))
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
        if self.path == "/api/clinician/reports":
            return self._json({"reports": clinician_reports()})
        if self.path.startswith("/api/clinician/report?"):
            from urllib.parse import parse_qs, urlparse
            rid = (parse_qs(urlparse(self.path).query).get("id") or [""])[0]
            try:
                return self._json(clinician_report(rid))
            except KeyError:
                return self._json({"error": "unknown recording_id"}, 404)
        return super().do_GET()

    # -------------------------------------------------- shared communication layer
    def _patients_get(self):
        from urllib.parse import parse_qs, urlparse
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        parts = u.path.strip("/").split("/")          # api, patients, [pid], [what]
        if len(parts) > 4 or parts[:2] != ["api", "patients"]:
            return self._json({"error": "not found"}, 404)
        try:
            if len(parts) == 2:
                return self._json({"patients": shared_store.patients_overview()})
            pid, what = parts[2], (parts[3] if len(parts) > 3 else "state")
            role = q.get("role", "patient")
            if what == "state":
                return self._json(shared_store.patient_state(pid, role))
            if what == "messages":
                return self._json({"messages": shared_store.messages(pid, role, int(q.get("after") or 0))})
        except KeyError:
            return self._json({"error": "unknown patient"}, 404)
        except (ValueError, TypeError, OverflowError) as e:
            return self._json({"error": str(e)}, 400)
        return self._json({"error": "not found"}, 404)

    def _patients_post(self, data):
        parts = self.path.split("?")[0].strip("/").split("/")
        if len(parts) != 4:
            return self._json({"error": "not found"}, 404)
        pid, what = parts[2], parts[3]
        st = shared_store
        try:
            if what == "messages":
                return self._json(st.post_message(pid, data.get("role"), data.get("body")))
            if what == "read":
                st.mark_read(pid, data.get("role"), int(data.get("upto") or 0))
                return self._json({"ok": True})
            if what == "share":
                return self._json(st.share_profile(pid, data.get("snapshot")))
            if what == "withdraw":
                st.withdraw_profile(pid)
                return self._json({"ok": True})
            if what == "reports":
                st.check_pid(pid)
                r = clinician_report(str(data.get("recording_id") or ""))
                st.link_report(pid, r["recording_id"], r["answer"], r["simulated"])
                return self._json({"ok": True, "reports": st.links(pid, "report")})
        except KeyError:
            return self._json({"error": "unknown patient or report"}, 404)
        except (ValueError, TypeError, OverflowError) as e:
            return self._json({"error": str(e)}, 400)
        return self._json({"error": "not found"}, 404)

    def _session_post(self, action, data):
        S = movement_session
        try:
            if action == "start":
                seed = data.get("seed")
                seed = int(seed) if seed is not None else random.SystemRandom().randrange(1, 10**6)
                pid = data.get("patient_id")
                if pid is not None:
                    try:
                        shared_store.check_pid(pid)
                    except KeyError:
                        return self._json({"error": "unknown patient"}, 404)
                return self._json(S.start(str(data.get("mode") or "full"), seed,
                                          {"camera_check": bool(data.get("camera_check")), "patient_id": pid}))
            if action == "sync":
                return self._json(S.sync_ping())
            if action == "sync_set":
                S.set_sync(data.get("session_id"), float(data["offset_s"]), float(data["rtt_ms"]))
                return self._json({"ok": True})
            if action == "marker":
                cm = data.get("client_ms")
                return self._json(S.marker(data.get("session_id"), str(data.get("marker")),
                                           float(cm) if cm is not None else None))
            if action == "finish":
                out = S.finish(data.get("session_id"), data.get("result") or {})
                pid = S._get(data.get("session_id"))["meta"].get("patient_id")
                if pid:
                    shared_store.link_session(pid, out["session_id"], out)
                return self._json(out)
        except KeyError:
            return self._json({"error": "unknown session"}, 404)
        except (ValueError, TypeError, OverflowError) as e:
            return self._json({"error": str(e)}, 400)
        return self._json({"error": "not found"}, 404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        try:
            data = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return self._json({"error": "bad json"}, 400)
        if not isinstance(data, dict):
            return self._json({"error": "body must be a JSON object"}, 400)
        try:
            return self._route_post(data)
        except Exception as e:  # backstop: never leave a request without a response
            return self._json({"error": f"server error: {type(e).__name__}"}, 500)

    def _route_post(self, data):
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
        if self.path.startswith("/api/session/"):
            return self._session_post(self.path.rsplit("/", 1)[-1], data)
        if self.path.startswith("/api/patients/"):
            return self._patients_post(data)
        if self.path == "/api/clinician/ask":
            q = str(data.get("question") or "").strip()
            if not q:
                return self._json({"error": "empty question"}, 400)
            try:
                return self._json(clinician_ask(str(data.get("recording_id") or ""), q,
                                                data.get("history")))
            except KeyError:
                return self._json({"error": "unknown recording_id"}, 404)
        return self._json({"error": "not found"}, 404)


if __name__ == "__main__":
    if not DIST.exists():
        sys.exit(f"NeuroBridge UI (dist) not found at {DIST}")
    print(f"NeuroBridge + NeuroSym agent -> http://localhost:{PORT}  "
          f"(LLM {'ON' if llm() else 'OFF — template mode, set ANTHROPIC_API_KEY'})")
    movement_session.open_outlet()
    print(f"Marker stream {movement_session.STREAM_NAME}: "
          f"{'open' if movement_session.outlet() else movement_session._lsl_error}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
