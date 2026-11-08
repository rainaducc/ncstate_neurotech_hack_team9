"""Agent tests: every shown text passes check_text; LLM path, retry, fallback and audit log."""
import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent import Copilot, ReportStore, Tools  # noqa: E402
from report_adapter import HEADER, check_text, load_report  # noqa: E402

MOCKS = sorted((ROOT / "mocks").glob("*.json"))
SAMPLES = sorted((ROOT / "contract" / "samples").glob("*.json"))
QUESTIONS = ["Summarize this report.", "Why was this flagged?", "What confounds should I consider?",
             "What changed since the previous visit?", "Explain sym_mu_execute", "Draft a summary."]


@pytest.fixture
def store():
    return ReportStore.default()


def _bot(store, tmp_path, client=None):
    return Copilot(store, client=client, audit_dir=tmp_path, use_llm=client is not None)


@pytest.mark.parametrize("path", MOCKS + SAMPLES, ids=lambda p: p.name)
@pytest.mark.parametrize("question", QUESTIONS)
def test_offline_answers_pass_guardrails(store, tmp_path, path, question):
    r = load_report(path)
    store.add(r)
    reply = _bot(store, tmp_path).ask(question, r.recording_id)
    assert reply.mode == "offline"
    assert check_text(reply.text, r) == []


def test_old_model_format_still_works(store, tmp_path):
    """The model may change format; the agent must keep working through the adapter alone."""
    old = {"participant": "S1", "decision": "SUGGEST SPECIALIST", "qc": "PASS",
           "features": {"sym_mu_execute": {"value": 1.5, "z": 3.1, "label": "Hemisphere symmetry"}, "li": 0.2},
           "deviation_score": 6.1, "p_value": 0.01, "guarantee": "5% max false review", "limitations": "prototype"}
    store.add(load_report(old))
    bot = _bot(store, tmp_path)
    for q in QUESTIONS:
        reply = bot.ask(q, "S1")
        assert reply.guardrail_problems == [] and "OUTSIDE_NORMATIVE_RANGE_REVIEW" in reply.text


# ----------------------------------------------------------- fake Anthropic client
class FakeClient:
    """Plays a script of responses: ('tool', name, input) or ('text', str) or ('error',)."""

    def __init__(self, script):
        self.script, self.calls = list(script), []
        self.messages = self

    def create(self, **kw):
        self.calls.append(copy.deepcopy(kw))
        step = self.script.pop(0)
        if step[0] == "error":
            raise ConnectionError("network down")
        if step[0] == "tool":
            return NS(stop_reason="tool_use", content=[NS(type="tool_use", id=f"t{len(self.calls)}",
                                                         name=step[1], input=step[2])])
        return NS(stop_reason="end_turn", content=[NS(type="text", text=step[1])])


RID = "MOCK_OUTSIDE_WEAK_LEFT"
GOOD = (f"{HEADER}.\nOUTSIDE_NORMATIVE_RANGE_REVIEW\n- Symmetry z = 3.1; left hemisphere z = -2.9.\n"
        "- Data quality PASS. Levodopa may change beta.\n- Research prototype; at most 5% false review.")
BAD = f"{HEADER}. OUTSIDE_NORMATIVE_RANGE_REVIEW. The patient has early Parkinson's; asymmetry z = 4.7."


def test_llm_path_uses_tools_and_passes(store, tmp_path):
    client = FakeClient([("tool", "get_report", {"recording_id": RID}),
                         ("tool", "explain_flag", {"feature_name": "sym_mu_execute"}), ("text", GOOD)])
    reply = _bot(store, tmp_path, client).ask("Why flagged?", RID)
    assert reply.mode == "llm" and reply.guardrail_problems == []
    assert [c["tool"] for c in reply.tool_calls] == ["get_report", "explain_flag"]
    tool_result = json.loads(client.calls[1]["messages"][-1]["content"][0]["content"])
    assert tool_result["answer"] == "OUTSIDE_NORMATIVE_RANGE_REVIEW"


def test_llm_missing_header_is_framed(store, tmp_path):
    reply = _bot(store, tmp_path, FakeClient([("text", "Symmetry z = 3.1.")])).ask("Why?", RID)
    assert reply.mode == "llm" and reply.text.startswith(HEADER)


def test_llm_retry_after_guardrail_failure(store, tmp_path):
    reply = _bot(store, tmp_path, FakeClient([("text", BAD), ("text", GOOD)])).ask("Why?", RID)
    assert reply.mode == "llm-retry" and reply.guardrail_problems == []
    assert reply.rejected_attempts and any("diagnostic" in p for p in reply.rejected_attempts[0]["problems"])


def test_llm_fails_twice_falls_back(store, tmp_path):
    reply = _bot(store, tmp_path, FakeClient([("text", BAD), ("text", BAD)])).ask("Why?", RID)
    assert reply.mode == "fallback" and reply.guardrail_problems == []
    assert "Parkinson" not in reply.text


def test_api_error_falls_back(store, tmp_path):
    reply = _bot(store, tmp_path, FakeClient([("error",)])).ask("Why?", RID)
    assert reply.mode == "fallback" and reply.guardrail_problems == []


def test_audit_log_written(store, tmp_path):
    bot = _bot(store, tmp_path, FakeClient([("text", BAD), ("text", GOOD)]))
    bot.ask("Why?", RID)
    entry = json.loads((tmp_path / "agent_log.jsonl").read_text().splitlines()[-1])
    for k in ("prompt", "tool_calls", "output", "check_text", "rejected_attempts", "model_version", "agent_mode"):
        assert k in entry
    assert entry["check_text"] == [] and entry["agent_mode"] == "llm-retry"


def test_compare_visits_uses_only_report_values(store):
    a = load_report(MOCKS[0])
    b_raw = {"recording_id": "VISIT_B", "answer": "WITHIN_NORMATIVE_RANGE", "data_quality": {"status": "PASS"},
             "features": [{"name": a.features[0].name, "value": 9.9, "z": 0.5}], "scores": {},
             "guarantee_note": "g", "limitations": []}
    store.add(load_report(b_raw))
    out = Tools(store).compare_visits(a.recording_id, "VISIT_B")
    assert out["features"][0]["VISIT_B"]["value"] == 9.9
    assert Tools(store).compare_visits("NOPE", "VISIT_B")["loaded"]["NOPE"] is False


def test_unknown_tool_and_feature(store):
    t = Tools(store, RID)
    assert "error" in t.call("delete_everything", {})
    assert "available" in t.explain_flag("not_a_feature")


def test_agent_and_app_never_index_raw_json():
    """Model format may change: only report_adapter.py may touch the raw dict."""
    for name in ("agent.py", "app.py"):
        src = (ROOT / name).read_text()
        assert ".raw" not in src and "json.load(open" not in src, name


VER_OK = ("text", '{"supported": true, "problems": []}')
VER_BAD = ("text", '{"supported": false, "problems": ["left and right hemisphere swapped"]}')


def test_verifier_pass_marks_verified(store, tmp_path):
    reply = _bot(store, tmp_path, FakeClient([("text", GOOD), VER_OK])).ask("Why?", RID)
    assert reply.mode == "llm" and reply.verified is True
    assert reply.usage["api_calls"] == 2


def test_verifier_objection_triggers_retry(store, tmp_path):
    client = FakeClient([("text", GOOD), VER_BAD, ("text", GOOD), VER_OK])
    reply = _bot(store, tmp_path, client).ask("Why?", RID)
    assert reply.mode == "llm-retry" and reply.verified is True
    assert "swapped" in reply.rejected_attempts[0]["problems"][0]
    assert "swapped" in client.calls[2]["messages"][-1]["content"]      # the retry is told why


def test_verifier_error_fails_open_but_is_recorded(store, tmp_path):
    reply = _bot(store, tmp_path, FakeClient([("text", GOOD), ("text", "not json")])).ask("Why?", RID)
    assert reply.mode == "llm" and reply.verified is None


def test_history_is_sent(store, tmp_path):
    client = FakeClient([("text", GOOD), VER_OK])
    _bot(store, tmp_path, client).ask("And the left hand?", RID, history=[("Why?", "Earlier answer")])
    msgs = client.calls[0]["messages"]
    assert msgs[0]["content"] == "Why?" and msgs[1]["content"] == "Earlier answer"


def test_inconclusive_offline_says_what_to_fix(store, tmp_path):
    reply = _bot(store, tmp_path).ask("Why this result?", "MOCK_INCONCLUSIVE")
    assert "What to fix" in reply.text and "C4" in reply.text and "z =" not in reply.text


def test_thinking_blocks_are_passed_back_unchanged(store, tmp_path):
    class ThinkingClient(FakeClient):
        def create(self, **kw):
            self.calls.append(copy.deepcopy(kw))
            if len(self.calls) == 1:
                return NS(stop_reason="tool_use", content=[
                    NS(type="thinking", thinking="plan", signature="sig"),
                    NS(type="text", text=""),
                    NS(type="tool_use", id="t1", name="get_report", input={"recording_id": RID})])
            if len(self.calls) == 2:
                return NS(stop_reason="end_turn", content=[NS(type="text", text=GOOD)])
            return NS(stop_reason="end_turn", content=[NS(type="text", text='{"supported": true, "problems": []}')])
    client = ThinkingClient([])
    reply = _bot(store, tmp_path, client).ask("Why?", RID)
    sent = client.calls[1]["messages"][1]["content"]
    assert sent[0] == {"type": "thinking", "thinking": "plan", "signature": "sig"}
    assert all(not (b["type"] == "text" and not b["text"]) for b in sent)
    assert reply.mode == "llm" and reply.verified is True


def test_footer_added_and_guarantee_paraphrase_allowed(store, tmp_path):
    text = f"{HEADER}.\nOUTSIDE_NORMATIVE_RANGE_REVIEW\nSymmetry z = 3.1."
    reply = _bot(store, tmp_path, FakeClient([("text", text), VER_OK])).ask("Why?", RID)
    assert "Statistical note:" in reply.text and "Confound:" in reply.text
    w = load_report(ROOT / "mocks/within_range.json")
    assert check_text(f"{HEADER}. WITHIN_NORMATIVE_RANGE. At most 5% chance of a false "
                      "OUTSIDE_NORMATIVE_RANGE_REVIEW for healthy people.", w) == []
