"""
app.py - NeuroSym clinician web app (Streamlit).

Run:  .venv/bin/streamlit run app.py
Reads model reports ONLY through report_adapter.load_report(). All copilot text passes
report_adapter.check_text(). Editable draft summary + Accept button writes to audit/.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import plotly.graph_objects as go
import streamlit as st

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from agent import Copilot, available_reports
from offline_summary import summary
from report_adapter import ANSWERS, HEADER, load_report

st.set_page_config(page_title="NeuroSym Clinician Copilot", page_icon="🧠", layout="wide")

ANSWER_STYLES = {
    "WITHIN_NORMATIVE_RANGE": ("background:#e8f5e9;border:1px solid #2e7d32;color:#1b5e20;", "✅"),
    "INCONCLUSIVE_REPEAT": ("background:#fff8e1;border:1px solid #f9a825;color:#8d6e00;", "⚠️"),
    "OUTSIDE_NORMATIVE_RANGE_REVIEW": ("background:#fdecea;border:1px solid #c62828;color:#b71c1c;", "🔍"),
}

AUDIT_DIR = ROOT / "audit"


def upload_or_pick() -> Path | None:
    st.sidebar.header("1. Select a report")
    reg = available_reports()
    labels = {rid: f"{rid} — {load_report(p).answer}" for rid, p in reg.items()}
    forced = st.session_state.get("forced_report")
    if forced:  # used by tests to pin a specific report
        return Path(forced)
    choice = st.sidebar.selectbox("From mocks / samples", ["—"] + list(reg),
                                  format_func=lambda x: labels.get(x, x))
    up = st.sidebar.file_uploader("…or upload a report JSON", type=["json"])
    if up is not None:
        p = ROOT / "contract" / "samples" / f"upload_{up.name}"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(up.getvalue())
        return p
    return reg.get(choice) if choice != "—" else None


def answer_banner(report):
    style, icon = ANSWER_STYLES.get(report.answer, ("background:#eee;", "•"))
    reasons = "".join(f"<div style='font-size:.9em;margin-top:4px'>• {r}</div>" for r in report.answer_reasons)
    st.markdown(
        f"<div style='{style}padding:14px 18px;border-radius:10px'>"
        f"<div style='font-size:.8em;opacity:.8'>{HEADER}</div>"
        f"<div style='font-size:1.25em;font-weight:700'>{icon} {report.answer}</div>{reasons}</div>",
        unsafe_allow_html=True)


def dq_panel(report):
    dq = report.data_quality
    st.markdown("#### Data quality")
    c1, c2, c3 = st.columns(3)
    c1.metric("Status", dq.get("status", "n/a"))
    c2.metric("Bad channels", str(len(dq.get("bad_channels", []))))
    sync = (dq.get("sync") or {}).get("residual_ms")
    c3.metric("Sync residual (ms)", sync if sync is not None else "n/a")
    if dq.get("bad_channels"):
        st.write("Bad channels: " + ", ".join(dq["bad_channels"]))
    if dq.get("clean_trials_per_condition"):
        st.bar_chart(x="condition", y="trials",
                     data=[{"condition": k, "trials": v} for k, v in dq["clean_trials_per_condition"].items()],
                     use_container_width=True)
    for n in dq.get("notes", []):
        st.write(f"- {n}")


def z_curve(figs, feature):
    """Feature z on a standard normal curve with 'this recording' marker."""
    import numpy as np
    x = np.linspace(-4, 4, 400)
    y = np.exp(-(x**2) / 2) / (2 * np.pi) ** 0.5
    fig = go.Figure()
    fig.add_scatter(x=x, y=y, mode="lines", line=dict(color="#90a4ae"), fill="tozeroy",
                    name="healthy reference")
    z = feature.z if feature.z is not None else 0
    fig.add_vline(x=z, line=dict(color="#c62828", width=2),
                  annotation_text=f"z = {z:g}", annotation_font=dict(color="#c62828"))
    fig.update_layout(title=feature.label or feature.name, height=230,
                      margin=dict(l=10, r=10, t=40, b=10), showlegend=False)
    figs.append(fig)


def hemisphere_chart(report):
    """Left hemisphere (right hand) vs right hemisphere (left hand) bar chart."""
    left = [f for f in report.features if f.hemisphere == "left" and f.condition == "execute"]
    right = [f for f in report.features if f.hemisphere == "right" and f.condition == "execute"]
    if not left and not right:
        return None
    names, vals, colors = [], [], []
    for f in left:
        names.append(f"LH (right hand)"); vals.append(f.value or 0); colors.append("#1565c0")
    for f in right:
        names.append(f"RH (left hand)"); vals.append(f.value or 0); colors.append("#6a1b9a")
    fig = go.Figure(go.Bar(x=names, y=vals, marker_color=colors, text=[f"{v:g} dB" for v in vals]))
    fig.update_layout(title="Motor-cortex engagement (mu ERD) by hemisphere",
                      yaxis_title="ERD (dB, more negative = stronger)", height=300,
                      margin=dict(l=10, r=10, t=40, b=10))
    return fig


def three_state_chart(report):
    p = report.three_state_profile
    if not p:
        return None
    fig = go.Figure()
    for hemi, color in (("left_hemisphere", "#1565c0"), ("right_hemisphere", "#6a1b9a")):
        states = p.get(hemi, {})
        fig.add_scatter(x=list(states.keys()), y=list(states.values()), mode="lines+markers",
                        name=hemi.replace("_", " "), line=dict(color=color))
    fig.update_layout(title="Three-state profile (observe / imagine / execute)",
                      yaxis_title="ERD (dB)", height=280, margin=dict(l=10, r=10, t=40, b=10))
    return fig


def longit_panel(report):
    lg = report.longitudinal
    if not lg or not lg.get("changes"):
        return
    st.markdown(f"#### Longitudinal change (previous visit: {lg.get('previous_visit', 'n/a')})")
    for c in lg["changes"]:
        st.write(f"- **{c.get('feature')}**: {c.get('direction')} (RCI {c.get('rci'):g})")


def export_html(report, draft) -> bytes:
    flagged = report.flagged_features()
    rows = "".join(
        f"<tr><td>{f.label or f.name}</td><td>{f.value:g} {f.unit}</td>"
        f"<td>{f.z:g}</td><td>{f.percentile:g}</td><td>{'flagged' if f in flagged else ''}</td></tr>"
        for f in report.features if f.value is not None)
    confounds = "".join(f"<li>{w}</li>" for w in report.confound_warnings) or "<li>None recorded.</li>"
    lims = "".join(f"<li>{l}</li>" for l in report.limitations)
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>NeuroSym {report.recording_id}</title>
<style>body{{font-family:-apple-system,Segoe UI,sans-serif;max-width:820px;margin:40px auto;padding:0 20px;color:#222}}
h1{{font-size:1.4em}} .banner{{padding:12px 16px;border-radius:8px;background:#fdecea;border:1px solid #c62828}}
table{{border-collapse:collapse;width:100%;margin:16px 0}} td,th{{border:1px solid #ddd;padding:6px 10px;text-align:left;font-size:.9em}}
pre{{background:#f6f6f6;padding:14px;border-radius:8px;white-space:pre-wrap;font-size:.85em}}</style></head><body>
<h1>NeuroSym clinician report — {report.recording_id}</h1>
<p>{HEADER}</p><div class="banner"><b>{report.answer}</b>
{''.join(f'<div>• {r}</div>' for r in report.answer_reasons)}</div>
<h3>Data quality</h3><p>Status: {report.data_quality.get('status')} |
Bad channels: {', '.join(report.data_quality.get('bad_channels', [])) or 'none'}</p>
<h3>Features</h3><table><tr><th>Feature</th><th>Value</th><th>z</th><th>Percentile</th><th>Flag</th></tr>{rows}</table>
<h3>Confound warnings</h3><ul>{confounds}</ul>
<h3>Statistical note</h3><p>{report.guarantee_note}</p>
<h3>Limitations</h3><ul>{lims}</ul>
<h3>Clinician summary (accepted draft)</h3><pre>{draft}</pre></body></html>""".encode()


# ------------------------------------------------------------------ layout
st.title("🧠 NeuroSym — Clinician Copilot")
st.caption(HEADER)

path = upload_or_pick()
if path is None:
    st.info("Pick a report from the sidebar (mocks / contract samples) or upload a JSON report.")
    st.stop()

report = load_report(path)
answer_banner(report)

left, right = st.columns([2, 3])
with left:
    dq_panel(report)
    longit_panel(report)
    if report.confound_warnings:
        st.markdown("#### Confound warnings")
        for w in report.confound_warnings:
            st.warning(w)

with right:
    figs: list = []
    flagged = report.flagged_features()
    st.markdown("#### Flagged features on the healthy reference curve" if flagged
                else "#### Features on the healthy reference curve")
    cols = st.columns(2 if flagged else 1)
    for i, f in enumerate((flagged or report.features)[:4]):
        with cols[i % len(cols)]:
            figs.append(None)
            z_curve(figs, f)
            st.plotly_chart(figs.pop(), use_container_width=True)
    h = hemisphere_chart(report)
    if h:
        st.plotly_chart(h, use_container_width=True)
    t = three_state_chart(report)
    if t:
        st.plotly_chart(t, use_container_width=True)
    if report.scores:
        st.markdown("#### Scores")
        s1, s2, s3 = st.columns(3)
        s1.metric("Omnibus", (report.scores.get("omnibus") or {}).get("score", "n/a"),
                  f"p = {(report.scores.get('omnibus') or {}).get('p', 'n/a')}")
        s2.metric("Asymmetry", (report.scores.get("asymmetry") or {}).get("score", "n/a"),
                  f"p = {(report.scores.get('asymmetry') or {}).get('p', 'n/a')}")
        s3.metric("Within-subject p", (report.scores.get("within_subject") or {}).get("p", "n/a"))

st.markdown("#### Statistical note & limitations")
st.caption(report.guarantee_note)
for l in report.limitations:
    st.caption("• " + l)

# -- copilot chat ---------------------------------------------------------
st.markdown("---")
st.markdown("#### 💬 Copilot chat")
if "chat" not in st.session_state or st.session_state.get("chat_report") != report.recording_id:
    st.session_state.chat = []
    st.session_state.chat_report = report.recording_id
    st.session_state.copilot = Copilot(report)

for msg in st.session_state.chat:
    with st.chat_message(msg["role"]):
        st.markdown(msg["text"].replace("\n", "  \n"))

if q := st.chat_input("Ask about this recording…"):
    st.session_state.chat.append({"role": "user", "text": q})
    with st.chat_message("user"):
        st.markdown(q)
    result = st.session_state.copilot.ask(q)
    st.session_state.chat.append({"role": "assistant", "text": result["output"]})
    st.rerun()

# -- draft summary --------------------------------------------------------
st.markdown("---")
st.markdown("#### 📝 Draft clinician summary")
default = st.session_state.get("draft_text") or summary(report)
draft = st.text_area("Editable (uses only numbers from the report; passes guardrails)",
                     value=default, height=320, key="draft_area")
b1, b2 = st.columns([1, 3])
if b1.button("✅ Accept summary"):
    AUDIT_DIR.mkdir(exist_ok=True)
    accepted = {"ts": dt.datetime.now(dt.timezone.utc).isoformat(),
                "recording_id": report.recording_id, "answer": report.answer, "draft": draft,
                "check_text_violations": __import__("report_adapter").check_text(draft, report)}
    (AUDIT_DIR / "accepted_summaries.jsonl").open("a").write(json.dumps(accepted) + "\n")
    st.session_state.draft_text = draft
    st.success("Accepted draft saved to audit/accepted_summaries.jsonl")
b2.download_button("⬇️ Export HTML report", data=export_html(report, draft),
                   file_name=f"neurosym_{report.recording_id}.html", mime="text/html")

# -- model compatibility ---------------------------------------------------
with st.expander("🔧 Model compatibility (adapter notes / unknown fields)"):
    st.write(f"Contract version: adapter 0.1 · schema_version {report.schema_version} · "
             f"model_version {report.model_version}")
    if report.adapter_notes:
        st.write("Adapter notes:")
        for n in report.adapter_notes:
            st.write(f"- {n}")
    else:
        st.write("Adapter notes: none — report matched the current contract directly.")
    st.write("Unknown fields: " + (", ".join(report.unknown_fields) if report.unknown_fields else "none"))
