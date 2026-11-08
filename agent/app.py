"""
app.py - NeuroSym clinician app (Streamlit).  Run:  streamlit run app.py

Reads reports ONLY through report_adapter.load_report() (via agent.ReportStore). Never reads EEG.
"""
from __future__ import annotations

import html
import json
import os
import math
import time
from pathlib import Path

import plotly.graph_objects as go
import streamlit as st

from agent import Copilot, ReportStore, audit_dir
from report_adapter import HEADER, check_text, load_report, report_badges, validate

ANSWER_STYLE = {
    "WITHIN_NORMATIVE_RANGE": ("#E7F0E4", "#4E6E4A", "Within the healthy reference range"),
    "INCONCLUSIVE_REPEAT": ("#F6ECD6", "#8A5A0C", "Inconclusive - repeat the recording"),
    "OUTSIDE_NORMATIVE_RANGE_REVIEW": ("#F3E1DB", "#A8503B", "Outside the healthy reference range - clinician review"),
}
HEMI_COLOR = {"left": "#3F7A86", "right": "#E58C72"}

st.set_page_config(page_title="NeuroSym - clinician review", layout="wide")


@st.cache_resource
def _store():
    return ReportStore.default()


LIVE_DIR = Path(os.environ.get("NEUROSYM_LIVE_REPORTS", Path(__file__).resolve().parent / "live_reports"))


def _copilot(store):
    if "copilot" not in st.session_state:
        st.session_state.copilot = Copilot(store)
    return st.session_state.copilot


def _g(x):
    return "n/a" if x is None else f"{x:g}"


# ------------------------------------------------------------------ sidebar: choose report
store = _store()
bot = _copilot(store)
with st.sidebar:
    st.markdown("### NeuroSym")
    up = st.file_uploader("Load a model report (JSON)", type="json")
    if up is not None:
        try:
            raw_up = json.loads(up.getvalue())
            rep_up = store.add(load_report(raw_up), f"upload:{up.name}")
            st.session_state.setdefault("uploaded_raw", {})[rep_up.recording_id] = raw_up
            st.success(f"Loaded {rep_up.recording_id}")
        except Exception as e:  # noqa: BLE001
            st.error(f"Could not load this report: {e}")
    if st.button("Load new live reports", key="reload"):
        store.load_dir(LIVE_DIR)
    status_file = LIVE_DIR / "live_status.json"
    if status_file.exists():
        try:
            live = json.loads(status_file.read_text())
            with st.expander(f"Live session {live.get('recording_id', '')}", expanded=True):
                st.caption(f"updated {live.get('updated')} · {live.get('n_trials', 0)} clean trials · "
                           f"{len(live.get('rejected', []))} rejected {('· ' + live['note']) if live.get('note') else ''}")
                mu = [t for t in live.get("trials", []) if t.get("band") == "mu"][-8:]
                for t in reversed(mu):
                    contra = t["c4_erd_db"] if t["hand"] == "left" else t["c3_erd_db"]
                    ipsi = t["c3_erd_db"] if t["hand"] == "left" else t["c4_erd_db"]
                    st.caption(f"{t['condition']} {t['hand']} #{t['ordinal']}: opposite side {contra:+.1f} dB, "
                               f"same side {ipsi:+.1f} dB")
        except Exception as e:  # noqa: BLE001
            st.caption(f"Live status unreadable: {e}")
    ids = store.visible_ids()        # a provisional report is hidden once its final report is loaded
    if not ids:
        st.warning("No reports found in mocks/ or contract/samples/.")
        st.stop()
    qp = st.query_params.get("report")
    rid = st.selectbox("Report", ids, index=ids.index(qp) if qp in ids else 0,
                       format_func=lambda i: f"{i} (provisional)" if store.is_provisional(i) else i)
    st.caption(f"Copilot mode: **{bot.mode}**" + ("" if bot.mode == "llm" else
               " (set ANTHROPIC_API_KEY for the LLM copilot)"))
    for path, err in store.load_errors.items():
        st.error(f"{Path(path).name}: {err}")

r = store.get(rid)

# ------------------------------------------------------------------ header + answer banner
st.caption(HEADER)
bg, fg, plain = ANSWER_STYLE[r.answer]
badges = report_badges(r) + (["PROVISIONAL"] if store.is_provisional(rid) and not r.is_provisional else [])
badge_html = "".join(f"<span style='background:#2A221C;color:#fff;font-size:11px;font-weight:700;"
                     f"padding:2px 8px;border-radius:4px;margin-right:6px'>{html.escape(b)}</span>" for b in badges)
badge_div = f"<div style='margin-bottom:6px'>{badge_html}</div>" if badge_html else ""
st.markdown(
    f"<div style='background:{bg};border-left:6px solid {fg};padding:14px 18px;border-radius:8px'>"
    f"{badge_div}"
    f"<div style='font-size:13px;color:{fg};font-weight:600;letter-spacing:.04em'>{html.escape(r.answer)}</div>"
    f"<div style='font-size:20px;margin-top:2px'>{html.escape(plain)}</div>"
    f"<div style='font-size:13px;color:#6B5F55;margin-top:4px'>Recording {html.escape(r.recording_id)}"
    f" &middot; model {html.escape(r.model_version)} &middot; schema {html.escape(r.schema_version)}</div></div>",
    unsafe_allow_html=True)
for reason in r.answer_reasons:
    st.markdown(f"- {reason}")

with st.expander("Model compatibility"):
    src = store.sources.get(rid, "")
    raw_for_validation = st.session_state.get("uploaded_raw", {}).get(rid) or (
        src if src and not src.startswith("upload:") and Path(src).exists() else None)
    problems = validate(raw_for_validation) if raw_for_validation is not None else ["(not checked)"]
    for x in [x for x in problems if x.startswith("legacy ")]:
        st.info(x)                                      # informational (old schema), not a contract failure
    problems = [x for x in problems if not x.startswith("legacy ")]
    st.write(f"Source: `{src}`")
    st.write("Contract validation:", "valid" if not problems else problems)
    st.write("Adapter notes:", r.adapter_notes or "none")
    st.write("Unknown fields (ignored until mapped in report_adapter.py):", r.unknown_fields or "none")

left, right = st.columns([3, 2], gap="large")

# ------------------------------------------------------------------ left column: evidence
with left:
    st.subheader("Data quality")
    dq = r.data_quality
    c1, c2, c3 = st.columns(3)
    c1.metric("Status", dq.get("status", "n/a"))
    c2.metric("Bad channels", ", ".join(dq.get("bad_channels") or []) or "none")
    sync = dq.get("sync") or {}
    c3.metric("Sync", sync.get("method", "n/a"),
              help=f"residual {sync.get('residual_ms')} ms" if sync.get("residual_ms") is not None else None)
    trials = dq.get("clean_trials_per_condition") or {}
    if trials:
        st.caption("Clean trials per condition")
        st.dataframe([{"condition": k, "clean trials": v} for k, v in trials.items()],
                     hide_index=True, width='stretch')
    if dq.get("n_trials_total") is not None:
        st.caption(f"Trials scored (all conditions and hands): {_g(dq['n_trials_total'])}")
    for n in dq.get("notes") or []:
        st.warning(n)
    if r.consistency:
        st.caption(f"Consistency: {r.consistency}")

    flagged = r.flagged_features()
    st.subheader("Measures outside the typical range" if flagged else "Measures")
    if flagged:
        xs = [x / 20 for x in range(-90, 91)]
        for f in flagged:
            fig = go.Figure()
            fig.add_trace(go.Scatter(x=xs, y=[math.exp(-x * x / 2) / math.sqrt(2 * math.pi) for x in xs],
                                     mode="lines", line=dict(color="#CDBFB1"), fill="tozeroy",
                                     fillcolor="rgba(205,191,177,0.25)", name="healthy reference",
                                     hoverinfo="skip"))
            fig.add_vrect(x0=-1.96, x1=1.96, fillcolor="rgba(78,110,74,0.08)", line_width=0)
            fig.add_vline(x=f.z, line=dict(color=HEMI_COLOR.get(f.hemisphere, "#A8503B"), width=3),
                          annotation_text=f"this recording (z = {_g(f.z)})", annotation_position="top")
            fig.update_layout(title=f"{f.label}: {_g(f.value)} {f.unit}, percentile {_g(f.percentile)}",
                              height=220, margin=dict(l=10, r=10, t=40, b=10), showlegend=False,
                              xaxis_title="z (healthy reference)", yaxis_visible=False)
            st.plotly_chart(fig, width='stretch', key=f"z-{rid}-{f.name}")
    else:
        st.caption("No feature is outside the typical range (|z| < 1.96).")

    # left hemisphere (right hand) vs right hemisphere (left hand), per condition
    hemi = [f for f in r.features if f.hemisphere in ("left", "right") and f.value is not None]
    if hemi:
        conds = sorted({f.condition or "all" for f in hemi})
        fig = go.Figure()
        for side, hand in (("left", "right"), ("right", "left")):
            vals = [next((f.value for f in hemi if f.hemisphere == side and (f.condition or "all") == c), None)
                    for c in conds]
            fig.add_bar(x=conds, y=vals, name=f"{side} hemisphere ({hand} hand)", marker_color=HEMI_COLOR[side])
        unit = hemi[0].unit
        fig.update_layout(title="Left vs right hemisphere response", barmode="group", height=280,
                          yaxis_title=unit, margin=dict(l=10, r=10, t=40, b=10))
        st.plotly_chart(fig, width='stretch', key=f"hemi-{rid}")

    tsp = r.three_state_profile
    if isinstance(tsp, dict) and tsp and all(isinstance(v, dict) for v in tsp.values()):
        states = [s for s in ("observe", "imagine", "execute") if any(s in v for v in tsp.values())]
        fig = go.Figure()
        for name, vals in tsp.items():
            color = HEMI_COLOR["left"] if "left" in name else HEMI_COLOR["right"] if "right" in name else None
            fig.add_bar(x=states, y=[vals.get(s) for s in states], name=name.replace("_", " "), marker_color=color)
        fig.update_layout(title="Three-state profile", barmode="group", height=260,
                          margin=dict(l=10, r=10, t=40, b=10))
        st.plotly_chart(fig, width='stretch', key=f"tsp-{rid}")

    with st.expander("All features"):
        st.dataframe([{"feature": f.label, "value": f.value, "unit": f.unit, "z": f.z,
                       "percentile": f.percentile} for f in r.features], hide_index=True,
                     width='stretch')

    st.subheader("Change since previous visit")
    lon = r.longitudinal
    if lon and lon.get("changes"):
        st.caption(f"Previous visit: {lon.get('previous_visit')}")
        st.dataframe([{"feature": c.get("feature"), "direction": c.get("direction"), "RCI": c.get("rci")}
                      for c in lon["changes"]], hide_index=True, width='stretch')
    else:
        st.caption("No earlier visit in this report.")

    st.subheader("Possible confounds")
    for w in r.confound_warnings or ["None listed in the report."]:
        st.markdown(f"- {w}")
    st.caption("Statistical note: " + r.guarantee_note)
    for lim in r.limitations:
        st.caption("Limitation: " + lim)

# ------------------------------------------------------------------ right column: copilot + draft
with right:
    st.subheader("Copilot")
    chats = st.session_state.setdefault("chats", {})
    chat = chats.setdefault(rid, [])
    q_buttons = st.columns(3)
    preset = None
    if q_buttons[0].button("Why this result?", key=f"b1-{rid}"):
        preset = "Why did this recording get this result?"
    if q_buttons[1].button("Confounds", key=f"b2-{rid}"):
        preset = "What confounds should I consider?"
    if q_buttons[2].button("Previous visit", key=f"b3-{rid}"):
        preset = "What changed since the previous visit?"
    typed = st.chat_input("Ask about this report")
    question = typed or preset
    if question:
        history = [(chat[i][1], chat[i + 1][1]) for i in range(0, len(chat) - 1, 2)]
        reply = bot.ask(question, rid, history=history)
        chat.append(("user", question))
        chat.append(("assistant", reply.text, reply.mode, reply.guardrail_problems, reply.verified))
    for item in chat:
        with st.chat_message(item[0]):
            st.markdown(item[1])
            if item[0] == "assistant":
                ver = {True: "claims verified", False: "verifier objected", None: "claims not verified"}[item[4]]
                st.caption(f"{item[2]} · guardrails {'pass' if not item[3] else item[3]}"
                           + ("" if item[2] == "offline" else f" · {ver}"))

    st.subheader("Draft summary")
    drafts = st.session_state.setdefault("drafts", {})
    if rid not in drafts:
        drafts[rid] = bot.draft(rid).text
    edited = st.text_area("Edit before accepting", drafts[rid], height=360, key=f"draft-{rid}")
    issues = check_text(edited, r)
    if issues:
        st.warning("Guardrail check on the edited text: " + "; ".join(issues))
    if st.button("Accept summary", type="primary", key=f"accept-{rid}"):
        out = audit_dir() / "accepted"
        out.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        rec = {"ts_utc": ts, "recording_id": rid, "model_version": r.model_version,
               "schema_version": r.schema_version, "agent_draft": drafts[rid], "accepted_text": edited,
               "edited_by_clinician": edited != drafts[rid], "check_text": issues}
        (out / f"{rid}_{ts}.json").write_text(json.dumps(rec, indent=2))
        st.session_state.setdefault("accepted", {})[rid] = edited
        st.success("Accepted and saved to the audit log.")

    final = st.session_state.get("accepted", {}).get(rid, edited)
    feats = "".join(f"<tr><td>{html.escape(f.label)}</td><td>{_g(f.value)} {html.escape(f.unit)}</td>"
                    f"<td>{_g(f.z)}</td><td>{_g(f.percentile)}</td></tr>" for f in r.features)
    report_html = (
        f"<!DOCTYPE html><html><head><meta charset='utf-8'><title>NeuroSym {html.escape(rid)}</title>"
        "<style>body{font-family:system-ui,sans-serif;max-width:760px;margin:32px auto;color:#2A221C}"
        "td,th{border-bottom:1px solid #DDD3C8;padding:4px 8px;text-align:left}"
        f".b{{background:{bg};border-left:6px solid {fg};padding:12px 16px}}</style></head><body>"
        f"<p><em>{html.escape(HEADER)}</em></p><div class='b'><b>{html.escape(r.answer)}</b><br>"
        f"Recording {html.escape(rid)} &middot; model {html.escape(r.model_version)}</div>"
        f"<h3>Clinician summary</h3><pre style='white-space:pre-wrap'>{html.escape(final)}</pre>"
        f"<h3>Features</h3><table><tr><th>Feature</th><th>Value</th><th>z</th><th>Percentile</th></tr>{feats}"
        "</table></body></html>")
    st.download_button("Export HTML report", report_html, file_name=f"neurosym_{rid}.html",
                       mime="text/html", key=f"export-{rid}")
