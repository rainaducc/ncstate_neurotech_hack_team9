"""Template clinician summary used when no LLM/API key is available, and as a reference for the
LLM agent's style. Uses only numbers present in the report, so it always passes check_text()."""
from report_adapter import HEADER, load_report

SIDE = {"left": "left hemisphere", "right": "right hemisphere"}


def _fmt(x):
    return "n/a" if x is None else f"{x:g}"


def summary(report):
    r = report
    lines = [HEADER + ".", "", f"Result: {r.answer}"]
    lines += [f"- {reason}" for reason in r.answer_reasons]
    dq = r.data_quality
    lines += ["", f"Data quality: {dq.get('status')}"]
    if dq.get("bad_channels"):
        lines.append("Bad channels: " + ", ".join(dq["bad_channels"]))
    lines += [f"- {n}" for n in dq.get("notes", [])]
    flagged = r.flagged_features()
    if flagged:
        lines += ["", "Measures outside the typical healthy range:"]
        for f in flagged:
            lines.append(f"- {f.label}: {_fmt(f.value)} {f.unit} (z = {_fmt(f.z)}, percentile {_fmt(f.percentile)})")
    if r.longitudinal and r.longitudinal.get("changes"):
        lines += ["", f"Change since previous visit ({r.longitudinal.get('previous_visit')}):"]
        lines += [f"- {c.get('feature')}: {c.get('direction')} (RCI {_fmt(c.get('rci'))})" for c in r.longitudinal["changes"]]
    if r.confound_warnings:
        lines += ["", "Possible confounds:"] + [f"- {w}" for w in r.confound_warnings]
    if r.answer == "INCONCLUSIVE_REPEAT":
        lines += ["", "Suggested action: address the data-quality notes above and repeat the recording."]
    elif r.answer == "OUTSIDE_NORMATIVE_RANGE_REVIEW":
        lines += ["", "Suggested action: clinician review alongside examination and history. "
                      "This flag is a deviation from healthy reference data, not a diagnosis."]
    lines += ["", "Statistical note: " + r.guarantee_note, "Limitations:"] + [f"- {l}" for l in r.limitations]
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    print(summary(load_report(sys.argv[1])))
