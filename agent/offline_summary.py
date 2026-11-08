"""Template clinician summary used when no LLM/API key is available, and as a reference for the
LLM agent's style. Uses only numbers present in the report, so it always passes check_text()."""
from report_adapter import HEADER, load_report

SIDE = {"left": "left hemisphere", "right": "right hemisphere"}


def _fmt(x):
    return "n/a" if x is None else f"{x:g}"


def inconclusive_action(r):
    """Suggested action for an INCONCLUSIVE_REPEAT, chosen from the model's own cause (r.inconclusive_cause)."""
    dq = r.data_quality
    cause = r.inconclusive_cause
    if cause == "qc_fail" or dq.get("status") == "FAIL":
        if dq.get("notes"):
            return ("the recording did not pass quality control; address the data-quality notes above and "
                    "repeat the recording.")
        return "the recording did not pass quality control; repeat the recording."
    if cause == "halves_disagree":
        return "the odd/even halves of the recording disagreed (see reasons above); repeat the recording."
    if cause == "grey_zone":
        return ("the result was borderline: the combined p fell between the review and within-range cutoffs "
                "(see reasons above); repeat the recording.")
    if r.calibration_pending:
        return ("the model fixes this answer at INCONCLUSIVE_REPEAT while its decision calibration is pending "
                "(see reasons above); repeating the recording will not change it. Clinician review of the "
                "reasons and limitations applies.")
    if dq.get("notes"):
        return "address the data-quality notes above and repeat the recording."
    return ("the report gives no specific cause (see reasons above); review them before deciding whether to "
            "repeat the recording.")


def not_interpreted(r):
    """Why feature values are not interpreted on an INCONCLUSIVE_REPEAT / QC-failed report."""
    if r.data_quality.get("status") == "FAIL" or r.inconclusive_cause == "qc_fail":
        return "Feature values from this recording are not interpreted because the data did not pass quality control."
    if r.calibration_pending:
        return ("Feature values from this recording are not interpreted: the model fixes the answer at "
                "INCONCLUSIVE_REPEAT while its decision calibration is pending (see reasons above), so repeating "
                "the recording will not change it.")
    if r.inconclusive_cause == "halves_disagree":
        return ("Feature values from this recording are not interpreted because the odd/even halves disagreed "
                "(see reasons above); repeat the recording.")
    if r.inconclusive_cause == "grey_zone":
        return ("Feature values from this recording are not interpreted because the result is borderline "
                "(see reasons above); repeat the recording.")
    return "Feature values from this recording are not interpreted because the result is inconclusive (see reasons above)."


def summary(report):
    r = report
    lines = [HEADER + ".", "", f"Result: {r.answer}"]
    lines += [f"- {reason}" for reason in r.answer_reasons]
    dq = r.data_quality
    lines += ["", f"Data quality: {dq.get('status')}"]
    if dq.get("bad_channels"):
        lines.append("Bad channels: " + ", ".join(dq["bad_channels"]))
    if dq.get("n_trials_total") is not None:
        lines.append(f"Trials scored (all conditions and hands): {_fmt(dq['n_trials_total'])}")
    lines += [f"- {n}" for n in dq.get("notes", [])]
    flagged = r.flagged_features()
    if r.answer == "INCONCLUSIVE_REPEAT" or dq.get("status") == "FAIL":
        lines += ["", "Feature values are not interpreted for this result."]
    elif flagged:
        lines += ["", "Measures outside the typical healthy range:"]
        for f in flagged:
            lines.append(f"- {f.label}: {_fmt(f.value)} {f.unit} (z = {_fmt(f.z)}, percentile {_fmt(f.percentile)})")
    if r.longitudinal and r.longitudinal.get("changes"):
        lines += ["", f"Change since previous visit ({r.longitudinal.get('previous_visit')}):"]
        lines += [f"- {c.get('feature')}: {c.get('direction')} (RCI {_fmt(c.get('rci'))})" for c in r.longitudinal["changes"]]
    if r.confound_warnings:
        lines += ["", "Possible confounds:"] + [f"- {w}" for w in r.confound_warnings]
    if r.answer == "INCONCLUSIVE_REPEAT":
        lines += ["", "Suggested action: " + inconclusive_action(r)]
    elif r.answer == "OUTSIDE_NORMATIVE_RANGE_REVIEW":
        lines += ["", "Suggested action: clinician review alongside examination and history. "
                      "This flag is a deviation from healthy reference data, not a diagnosis."]
    if getattr(r, "exploratory", None):
        lines += ["", "Exploratory arm blocks (not scored):"] + [f"- {x}" for x in r.exploratory]
    lines += ["", "Statistical note: " + r.guarantee_note, "Limitations:"] + [f"- {l}" for l in r.limitations]
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    print(summary(load_report(sys.argv[1])))
