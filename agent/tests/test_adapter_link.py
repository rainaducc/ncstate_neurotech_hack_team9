"""Model <-> agent link regression tests (fixes F1-F19 of the adapter audit).

Every model output must load with no clinical information lost or mislabelled, and the guardrails
must never reject correct rule-following text nor let unsafe text through. Fixtures in
tests/fixtures/ are verbatim copies of real model outputs (model repo / score_recording producers);
the model repo itself is never read by these tests.
"""
import copy
import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIX = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(ROOT))
from agent import ReportStore, Tools, _ensure_frame, footer, offline_answer  # noqa: E402
from offline_summary import summary  # noqa: E402
from report_adapter import (DIAGNOSTIC_PATTERNS, HEADER, _DISEASES, _JUDGMENT, check_text,  # noqa: E402
                            load_report, report_badges, validate)

H = HEADER + "."


def _raw(name):
    return json.loads((FIX / name).read_text())


def _load(name, **changes):
    d = _raw(name)
    d.update(changes)
    return load_report(d)


def _tools(*reports):
    s = ReportStore()
    for r in reports:
        s.add(r)
    return Tools(s, reports[0].recording_id if reports else None)


def _h(r):
    return f"{H}\nResult: {r.answer}\n"


@pytest.fixture
def sim():
    return load_report(FIX / "sim_rehearsal.json")


# ------------------------------------------------------------------ F1 report-derived exemption
def test_f1_no_substring_exemption_from_json_dump(sim):
    assert check_text(_h(sim) + "These asymmetries could point to ALS.", sim) != []   # 'als' in 'false'
    assert check_text(_h(sim) + "Findings are consistent with MS.", sim) != []
    assert check_text(_h(sim) + "There is a high chance of a movement disorder.", sim) != []


def test_f1_limitation_names_condition_only_verbatim(sim):
    d = _raw("sim_rehearsal.json")
    d["limitations"] = list(d.get("limitations") or []) + ["Not validated in people with stroke."]
    r2 = load_report(d)
    assert check_text(_h(r2) + "This pattern is typical of stroke.", r2) != []
    assert check_text(_h(r2) + "Limitation: Not validated in people with stroke.", r2) == []


def test_f1_symptom_words_from_report_stay_allowed(sim):
    d = _raw("sim_rehearsal.json")
    d["context"] = {**d["context"], "symptoms": ["tremor after stroke in 2019"]}
    r = load_report(d)
    assert check_text(_h(r) + "The recorded history mentions stroke.", r) == []


def test_f1_guarantee_paraphrase_still_allowed():
    w = load_report(ROOT / "mocks/within_range.json")
    assert check_text(f"{H} WITHIN_NORMATIVE_RANGE. At most 5% chance of a false "
                      "OUTSIDE_NORMATIVE_RANGE_REVIEW for healthy people.", w) == []


# ------------------------------------------------------------------ F2 extended patterns
@pytest.mark.parametrize("text", [
    "The patient has a motor disorder affecting the right hand.",
    "This is compatible with a movement disorder or neurological condition.",
    "The weakness is suggestive of cortical injury.",
    "The result appears within normal limits, which is reassuring.",
    "Diagnosis: no motor problem.",
    "The odds are high that something is wrong.",
    "The recording is unremarkable.",
    "This is a healthy recording.",
])
def test_f2_unsafe_phrasings_flagged(sim, text):
    assert check_text(_h(sim) + text, sim) != []


@pytest.mark.parametrize("text", [
    "Each hand has the same condition count.",
    "Both hands have the same condition structure.",
    "This test cannot tell you whether the patient has a disease; the clinician decides.",
    "Not reassuring or alarming on its own.",
    "Ask whether the patient had a recent hand injury.",
])
def test_f2_legitimate_text_passes(sim, text):
    assert check_text(_h(sim) + text, sim) == []


def test_f2_templates_have_no_new_false_positives(sim):
    for name in ("sim_rehearsal.json", "real_subject_live.json", "gen_live_qcfail.json",
                 "gen_halves_disagree_right_0.3.json", "example_score_0.3.json"):
        r = load_report(FIX / name)
        assert check_text(summary(r), r) == [], name
        assert check_text(_h(r) + footer(r), r) == [], name


def test_f2_patient_guard_unchanged():
    import fieldnote_server as fs
    assert fs.DIAGNOSTIC_PATTERNS is DIAGNOSTIC_PATTERNS and fs._DISEASES is _DISEASES and fs._JUDGMENT is _JUDGMENT
    own = "my mother has a thyroid condition"
    assert fs.guarded("You mentioned your mother has a thyroid condition.", source=own)
    assert fs.guarded("Each hand has the same condition: imagine then execute.")
    assert not fs.guarded("This could be Parkinson's.")


# ------------------------------------------------------------------ F3 inconclusive cause
def test_f3_halves_disagree_cause_and_reasons():
    r = load_report(FIX / "gen_halves_disagree_right_0.3.json")
    assert r.inconclusive_cause == "halves_disagree"
    assert any("odd p = 0.0375" in s and "even p = 0.4125" in s for s in r.answer_reasons)
    assert not any("half alone" in s for s in r.answer_reasons)            # no adapter-computed half labels
    assert check_text(_h(r) + " ".join(r.answer_reasons), r) == []


def test_f3_grey_zone_cause():
    r = load_report(FIX / "real_subject_live.json")
    assert r.inconclusive_cause == "grey_zone"
    assert any("p between 0.05 and 0.2" in s for s in r.answer_reasons)


def test_f3_qc_fail_with_no_p_value_keeps_qc_lines():
    d = _raw("gen_live_qcfail.json")
    d["p_value"] = None
    r = load_report(d)
    assert "No combined p-value was computed." in r.answer_reasons
    qc = [s for s in r.answer_reasons if s.startswith("QC: ")]
    assert len(qc) == len(d["data_quality"]["qc_reasons"])
    assert r.inconclusive_cause == "qc_fail"


def test_f3_non_inconclusive_has_no_cause(sim):
    assert sim.inconclusive_cause is None


# ------------------------------------------------------------------ F4 QC wording only for QC failures
def test_f4_qc_pass_inconclusive_not_called_qc_failure():
    r = load_report(FIX / "real_subject_live.json")
    t = offline_answer("Why is this inconclusive?", r, _tools(r))
    assert "did not pass quality control" not in t and "Address the data-quality items above" not in t
    assert check_text(t, r) == []
    s = summary(r)
    assert "Measures outside the typical healthy range" not in s and "Feature values are not interpreted" in s
    assert "data-quality notes above" not in s and "odd/even halves" not in s.split("Suggested action:")[1]
    assert "between the review and within-range cutoffs" in s           # cause-specific: grey_zone


def test_f4_qc_fail_summary_not_interpreted():
    r = load_report(FIX / "gen_live_qcfail.json")
    s = summary(r)
    assert "Measures outside the typical healthy range" not in s and "Feature values are not interpreted" in s
    assert "data-quality notes above" in s
    t = offline_answer("Why?", r, _tools(r))
    assert "did not pass quality control" in t and "What to fix before repeating" in t
    assert check_text(t, r) == [] and check_text(s, r) == []


def test_f4_qc_fail_without_reasons_is_still_qc_failure():
    r = load_report(FIX / "mut_qcfail_noreasons.json")
    s = summary(r)
    assert "borderline" not in s and "did not pass quality control" in s
    assert "did not pass quality control" in offline_answer("Why?", r, _tools(r))


def test_f4_outside_sample_has_no_dq_notes_wording():
    s = summary(load_report(ROOT / "contract/samples/simulated_weak_right_60.json"))
    assert "data-quality notes above" not in s


# ------------------------------------------------------------------ F5 guarantee caveat for devices
def test_f5_guarantee_not_established_for_device(sim):
    assert "not established" in sim.guarantee_note
    assert check_text(summary(sim), sim) == []
    ref = load_report(FIX / "simulated_weak_right_60.json")
    assert ref.guarantee_note.endswith("at most 5%.")


# ------------------------------------------------------------------ F6 footer always present
@pytest.mark.parametrize("q", ["Explain the flagged features", "Why was this flagged?", "What confounds?",
                               "Compare with previous visit"])
def test_f6_offline_answers_carry_limitations(sim, q):
    t = offline_answer(q, sim, _tools(sim))
    assert "SIMULATED recording" in t and "not established for this device" in t
    for lim in sim.limitations:
        assert lim in t
    assert t.count("Statistical note:") == 1
    assert check_text(t, sim) == []


def test_f6_ensure_frame_replaces_llm_footer(sim):
    t = _ensure_frame(_h(sim) + "Body.\nStatistical note: the guarantee covers false flags.\n"
                      "---\nData quality: PASS\nLimitation: invented", sim)
    assert "SIMULATED recording" in t and t.count("Statistical note:") == 1
    assert "invented" not in t and "covers false flags" not in t
    assert check_text(t, sim) == []


# ------------------------------------------------------------------ F7 negation-aware patterns
@pytest.mark.parametrize("text", [
    "This test cannot establish a diagnosis; the clinician decides.",
    "It does not confirm a diagnosis either way.",
    "This result does not rule out a condition; the clinician decides.",
    "Within range does not mean there is no cause for concern.",
    "A cause in the hand cannot be ruled out by this test.",
    "It is not possible to rule out other explanations.",
])
def test_f7_negated_phrases_pass(sim, text):
    assert check_text(_h(sim) + text, sim) == []


@pytest.mark.parametrize("text", [
    "This rules out a motor problem.",
    "This confirms the diagnosis.",
    "This not only rules out a motor problem, it settles it.",
    "Never doubt it, this confirms a diagnosis.",
    "You can't argue with it, this rules out a motor problem.",
    "It doesn't just confirm a diagnosis, it settles it.",
    "We cannot ignore that this rules out a motor problem.",
])
def test_f7_unsafe_still_flagged(sim, text):
    assert check_text(_h(sim) + text, sim) != []


# ------------------------------------------------------------------ F8 negative flag threshold
def test_f8_negative_flag_threshold_allowed():
    r = load_report(FIX / "fnfull_live.json")
    neg = next(f for f in r.features if f.z is not None and f.z < 0)
    assert check_text(_h(r) + f"{neg.label} z = {neg.z} is below -1.96, so it is flagged.", r) == []
    assert check_text(_h(r) + "foo z = -2.0.", r) == ["number not in report: -2.0"]
    assert check_text(_h(r) + "foo z = -2.", r) == ["number not in report: -2"]


# ------------------------------------------------------------------ F9 sign + rounding
def _f9_report(**ctx):
    return load_report({"recording_id": "F9", "answer": "OUTSIDE_NORMATIVE_RANGE_REVIEW",
                        "data_quality": {"status": "PASS", "clean_trials_per_condition": {"execute_left": 24}},
                        "features": [{"name": "erd_mu_left", "value": -3.4172, "z": -2.9}], "scores": {},
                        "guarantee_note": "g", "limitations": [], "context": ctx,
                        "longitudinal": {"previous_visit": "2026-10-01", "changes": []}})


def test_f9_sign_and_rounding():
    r = _f9_report()
    h = _h(r)
    assert check_text(h + "Left hemisphere z = 2.9.", r) == ["number not in report: 2.9"]
    assert check_text(h + "Left hemisphere z = -2.9.", r) == []
    assert check_text(h + "Left hemisphere z = −2.9.", r) == []            # U+2212 minus
    assert check_text(h + "Value -3.4172 dB, about -3.42 dB.", r) == []
    assert check_text(h + "A -3.4 dB drop.", r) != []                           # 2 significant figures
    assert check_text(h + "Repeat the test in 2 weeks.", r) != []
    assert check_text(h + "Repeat the test in 3 weeks.", r) != []
    assert check_text(h + "Clean trials: 24", r) == []
    assert check_text(h + "Flagged when z <= -1.96.", r) == []


def test_f9_report_verbatim_hyphenated_numbers():
    r = _f9_report(sleep_h="6-7", medications=["levodopa 100-25 mg"])
    t = offline_answer("What confounds?", r, _tools(r))
    assert "6-7" in t and "100-25" in t and check_text(t, r) == []
    assert check_text(_h(r) + "Compared with 2026-10-01: no changes.", r) == []


def test_f9_top_contributors_kept_audit_dropped():
    r = load_report(FIX / "fnfull_live.json")
    c = r.top_contributors[0]
    val = next(v for v in c.values() if isinstance(v, float))
    assert val in r.numbers()
    assert check_text(_h(r) + f"Contribution {val}.", r) == []


# ------------------------------------------------------------------ F10 detail exposed to tools / UI
def test_f10_component_reason_and_tools():
    r = load_report(FIX / "simulated_weak_right_60.json")
    assert any("within-subject 0.025" in s and "omnibus 0.3375" in s for s in r.answer_reasons)
    g = _tools(r).get_report(r.recording_id)
    assert {"consistency_detail", "decision_rule", "n_calibration", "simulated", "inconclusive_cause",
            "model_sha256"} <= set(g)
    assert g["n_calibration"] == 79 and isinstance(g["simulated"], dict)
    assert check_text(summary(r), r) == []
    d = _tools(load_report(FIX / "gen_halves_disagree_right_0.3.json")).get_report("W_right_0.3")
    assert d["consistency_detail"]["odd_p_value"] == 0.0375


def test_f10_schema_03_gets_no_component_reason():
    r = load_report(FIX / "example_score_0.3.json")
    assert not any(s.startswith("Component conformal p") for s in r.answer_reasons)
    assert r.consistency_detail is None and r.decision_rule == {}


def test_f10_f12_f15_clinician_report_fields(tmp_path, monkeypatch):
    import fieldnote_server as fs
    shutil.copy(FIX / "simulated_weak_right_60.json", tmp_path / "simulated_weak_right_60.json")
    d = _raw("gen_live_final_explor_sim.json")
    (tmp_path / "LIVE_FINAL.json").write_text(json.dumps(d))
    monkeypatch.setenv("NEUROSYM_LIVE_REPORTS", str(tmp_path))
    cr = fs.clinician_report("eegmmidb:S001-sim-weak-right-60")
    assert cr["scores"]["within"]["p"] == 0.025 and cr["n_calibration"] == 79
    assert cr["consistency_detail"]["status"] == "agree"
    for key in ("header", "recording_id", "answer", "answer_reasons", "data_quality", "features",
                "guarantee_note", "limitations", "simulated", "exploratory", "consistency", "schema_version"):
        assert key in cr                                                         # existing keys kept
    live = fs.clinician_report("LIVE_FINAL")
    assert live["context"]["medications"] == ["sertraline"] and live["context"]["sex"] == "F"
    assert live["inconclusive_cause"] == "qc_fail" and live["simulated"] is True
    assert live["data_quality"]["n_trials_total"] == 24


# ------------------------------------------------------------------ F11 arm-block questions
@pytest.fixture
def inc_arm():
    return load_report(FIX / "gen_live_final_explor_sim.json")       # INCONCLUSIVE (QC fail) with arm blocks


def test_f11_arm_question_answered_on_inconclusive(inc_arm):
    assert inc_arm.exploratory
    t = offline_answer("Tell me about the arm blocks", inc_arm, _tools(inc_arm))
    assert "reach" in t.lower() and "not scored" in t and "not interpreted" in t
    assert check_text(t, inc_arm) == []


def test_f11_arm_question_on_outside_report():
    d = _raw("sim_rehearsal.json")
    d["exploratory"] = _raw("gen_live_final_explor_sim.json")["exploratory"]
    r = load_report(d)
    t = offline_answer("Why did the reach arm block look like that?", r, _tools(r))
    assert "Exploratory arm blocks (not scored):" in t and check_text(t, r) == []


@pytest.mark.parametrize("q", ["What medication confounds or alarm signs?", "Is it harmful or any harm to repeat today?",
                               "Any warmup effect?"])
def test_f11_substring_words_do_not_route_to_arm_blocks(inc_arm, q):
    t = offline_answer(q, inc_arm, _tools(inc_arm))
    assert "Exploratory arm blocks (not scored):" not in t


def test_f11_harm_question_keeps_qc_guidance(inc_arm):
    t = offline_answer("Is it harmful or any harm to repeat today?", inc_arm, _tools(inc_arm))
    assert "What to fix before repeating" in t


# ------------------------------------------------------------------ F12 context keys
def test_f12_context_aliases():
    d = _raw("example_score_0.3.json")
    d["context"] = {**d["context"], "hours_slept": 4, "caffeine_last_3h": True}
    r = load_report(d)
    c = _tools(r).list_confounds(r.recording_id)
    assert c["context"]["sleep_h"] == 4 and c["context"]["caffeine"] is True and c["context"]["sex"] == "male"
    t = offline_answer("What confounds?", r, _tools(r))
    assert "sleep_h = 4" in t and check_text(t, r) == []


def test_f12_non_dict_context_still_loads():
    r = load_report({"answer": "WITHIN_RANGE", "context": "free text"})
    assert r.answer == "WITHIN_NORMATIVE_RANGE"
    assert _tools(r).list_confounds(r.recording_id)["context"] == {}


# ------------------------------------------------------------------ F13 schema 0.3
def test_f13_schema_03():
    d = _raw("example_score_0.3.json")
    d0 = copy.deepcopy(d)
    r = load_report(d)
    assert d == d0                                                            # input not mutated
    assert r.data_quality["status"] == "NOT_REPORTED"
    assert any("calibration pending" in s for s in r.answer_reasons)
    assert validate(d) == ["legacy neurosym-report-0.3: not contract-checked"]
    t = offline_answer("why?", r, _tools(r))
    assert "did not pass quality control" not in t and check_text(t, r) == []


def test_f13_site_without_reference_noted():
    d = _raw("example_score_0.3.json")
    d["data_quality"] = {**d["data_quality"], "site_in_reference": False}
    r = load_report(d)
    assert "site has no fitted reference" in r.data_quality["notes"]
    assert r.data_quality["status"] == "NOT_REPORTED"


def test_f13_04_still_validated(sim):
    assert validate(_raw("sim_rehearsal.json")) == []


# ------------------------------------------------------------------ F14 duplicate recording ids
def test_f14_duplicate_recording_id_recorded(tmp_path):
    d = json.loads((ROOT / "contract/samples/real_subject.json").read_text())
    d["answer"] = "OUTSIDE_NORMATIVE_RANGE_REVIEW"
    (tmp_path / "dup.json").write_text(json.dumps(d))
    s = ReportStore()
    s.load_dir(ROOT / "contract" / "samples")
    s.load_dir(tmp_path)
    errs = [v for v in s.load_errors.values() if "duplicate recording_id eegmmidb:S002" in v]
    assert len(errs) == 1


def test_f14_identical_reload_is_not_a_duplicate():
    s = ReportStore()
    s.load_dir(ROOT / "contract" / "samples")
    s.load_dir(ROOT / "contract" / "samples")
    r = s.get("eegmmidb:S002")
    s.add(r)
    s.add(r)
    assert not any("duplicate" in v for v in s.load_errors.values())


# ------------------------------------------------------------------ F15 provisional sessions
def _prov_dir(tmp_path, with_final):
    """A live_reports folder as live_score.py writes it mid-session (provisional only) or after it
    (provisional + final). Built from the committed FNFULL fixture, not the gitignored runtime folder."""
    final = json.loads((FIX / "fnfull_live.json").read_text())
    prov = json.loads(json.dumps(final))
    prov["recording_id"] = final["recording_id"] + "_provisional"
    prov.setdefault("context", {})["provisional"] = True
    (tmp_path / f"{prov['recording_id']}.json").write_text(json.dumps(prov))
    if with_final:
        (tmp_path / f"{final['recording_id']}.json").write_text(json.dumps(final))
    return tmp_path


def test_f15_only_provisional_stays_visible(tmp_path, monkeypatch):
    import fieldnote_server as fs
    monkeypatch.setenv("NEUROSYM_LIVE_REPORTS", str(_prov_dir(tmp_path, False)))
    rows = {x["recording_id"]: x for x in fs.clinician_reports()}
    assert rows["FNFULL_20261108_060522_provisional"]["provisional"] is True
    assert fs.clinician_report("FNFULL_20261108_060522_provisional")["provisional"] is True


def test_f15_final_replaces_provisional(tmp_path, monkeypatch):
    import fieldnote_server as fs
    monkeypatch.setenv("NEUROSYM_LIVE_REPORTS", str(_prov_dir(tmp_path, True)))
    rows = {x["recording_id"]: x for x in fs.clinician_reports()}
    assert "FNFULL_20261108_060522_provisional" not in rows
    assert rows["FNFULL_20261108_060522"]["provisional"] is False


# ------------------------------------------------------------------ F16 label normalisation
def test_f16_label_variants():
    w = load_report(ROOT / "mocks/within_range.json")
    probs = check_text(f"{H}\nWITHIN_NORMATIVE_RANGE\nThis is not outside normative range review.", w)
    assert any("another answer label" in p for p in probs)
    assert check_text(f"{H}\nWITHIN_NORMATIVE_RANGE\nAt most 5% chance of a false-positive "
                      "OUTSIDE_NORMATIVE_RANGE_REVIEW.", w) == []


# ------------------------------------------------------------------ F17 trial totals
def test_f17_n_trials_is_a_total():
    r = load_report(FIX / "gen_live_qcfail.json")
    assert r.data_quality["n_trials_total"] == 24 and "clean_trials_per_condition" not in r.data_quality
    t = offline_answer("Why?", r, _tools(r))
    assert "Trials scored (all conditions and hands): 24" in t and "Clean trials, all conditions" not in t
    assert "Trials scored (all conditions and hands): 24" in footer(r)
    assert check_text(t, r) == []


# ------------------------------------------------------------------ F18 badges + provisional ids
def test_f18_badges_and_visible_ids(sim, tmp_path):
    assert report_badges(sim) == ["SIMULATED"]
    assert report_badges(load_report(FIX / "gen_live_provisional.json")) == ["PROVISIONAL"]
    s = ReportStore()
    s.load_dir(_prov_dir(tmp_path, True))
    assert "FNFULL_20261108_060522_provisional" in s.ids()
    assert "FNFULL_20261108_060522_provisional" not in s.visible_ids()
    assert "FNFULL_20261108_060522" in s.visible_ids()
    assert ".raw" not in (ROOT / "app.py").read_text()


# ------------------------------------------------------------------ F19 UI formatting
def test_f19_ui_shows_percentile_consistency_three_sig_digits():
    js = (ROOT.parent / "fieldnote-ui" / "dist" / "clinician-copilot.js").read_text()
    assert "percentile" in js.lower() and "r.consistency" in js and "toPrecision(3)" in js
    assert "sig(f.z)" in js and "sig(f.value)" in js


def test_f1_millisecond_unit_is_not_a_condition():
    o = load_report(ROOT / "mocks/outside_weak_left.json")
    assert check_text(f"{H}\nOUTSIDE_NORMATIVE_RANGE_REVIEW\nSync residual 2.1 ms (lsl_markers).", o) == []
    assert check_text(f"{H}\nOUTSIDE_NORMATIVE_RANGE_REVIEW\nThis could be MS.", o) != []


# ------------------------------------------------------------------ re-check of the applied fixes (R1-R12)
@pytest.fixture
def out_r():
    return load_report(ROOT / "mocks/outside_weak_left.json")


@pytest.fixture
def fn_r():
    return load_report(FIX / "fnfull_live.json")


@pytest.fixture
def outsym():
    d = json.loads((ROOT / "mocks/outside_weak_left.json").read_text())
    d["context"]["symptoms"] = "right hand weakness since a stroke last year"
    return load_report(d)


def _ct(r, body):
    return check_text(_h(r) + body, r)


# R1 double negatives are affirmations, not negations
@pytest.mark.parametrize("which,text", [
    ("out", "There is no way this does not confirm the diagnosis."),
    ("fn", "No one can say this does not rule out a motor problem."),
])
def test_r1_double_negation_flagged(out_r, fn_r, which, text):
    assert _ct(out_r if which == "out" else fn_r, text) != []


# R2 a condition the report names (symptoms) may be quoted, never used as an inference
@pytest.mark.parametrize("text", ["This asymmetry is typical of stroke.",
                                  "The findings confirm that the stroke caused the weakness."])
def test_r2_allowed_term_used_as_inference_flagged(outsym, text):
    assert _ct(outsym, text) != []


@pytest.mark.parametrize("text", [
    "The patient's reported symptom (right hand weakness since a stroke last year) is context only; "
    "this test cannot answer diagnostic questions.",
    "The symptom note mentions a stroke; this test does not assess its cause.",
    "The patient reported a stroke last year (context only).",
])
def test_r2_allowed_term_as_context_passes(outsym, text):
    assert _ct(outsym, text) == []


# R3 diagnostic wording
@pytest.mark.parametrize("text", [
    "This confirmed the diagnosis.", "The pattern supports the diagnosis of a motor problem.",
    "The most likely diagnosis is a problem in the left motor cortex.", "The pattern resembles a disease.",
    "The patient has a disorder.", "Findings are suggestive of motor neurone disease.",
    "The left motor cortex is underperforming, indicative of disease.",
])
def test_r3_diagnostic_wording_flagged(out_r, text):
    assert _ct(out_r, text) != []


@pytest.mark.parametrize("text", [
    "The flag indicates a deviation from healthy reference data, not a disease.",
    "This test cannot answer whether the patient has a disorder.",
    "The diagnosis is up to the clinician.", "This is not a diagnosis of any condition.",
    "Each block has a condition label and a hand label.",
    "The finding does not suggest a diagnosis; it is a deviation from reference data.",
])
def test_r3_disclaimers_pass(out_r, text):
    assert _ct(out_r, text) == []


def test_r3_clinician_only_patterns_not_shared_with_patient_guard():
    import fieldnote_server as fs
    from report_adapter import _CLINICIAN_DIAGNOSTIC
    assert not set(_CLINICIAN_DIAGNOSTIC) & set(fs.DIAGNOSTIC_PATTERNS)
    assert fs.guarded("You mentioned your mother has a thyroid condition.", source="my mother has a thyroid condition")


# R4 reassurance paraphrases
@pytest.mark.parametrize("text", [
    "This effectively excludes a motor problem.", "It isn't necessary to worry; nothing is wrong with the motor cortex.",
    "The patient is okay.", "The brain activity is entirely normal.", "Both hemispheres seem healthy.",
    "The motor system is intact and working normally.", "The patient does not have a motor problem.",
])
def test_r4_reassurance_flagged(fn_r, text):
    assert _ct(fn_r, text) != []


@pytest.mark.parametrize("text", [
    "Left hemisphere (C3), right-hand movement: z = -2.9 is well below -1.96, so it is flagged.",
    "Trials with artifacts were excluded before scoring.",
    "This result cannot exclude a motor problem; the clinician decides.",
    "A motor problem cannot be excluded by this test.",
    "This test does not tell you whether the patient does or does not have a condition.",
])
def test_r4_neutral_wording_passes(out_r, text):
    assert _ct(out_r, text) == []


# R5 probability, and the 'chance of a false' exemption only for the OUTSIDE guarantee
@pytest.mark.parametrize("which,text", [
    ("fn", "There is almost no chance of a false WITHIN_NORMATIVE_RANGE, so this result can be trusted."),
    ("out", "This is probably a false-positive OUTSIDE_NORMATIVE_RANGE_REVIEW; the patient is likely typical."),
    ("out", "It is likely that the left hemisphere is weak because of a motor problem."),
    ("out", "The patient is at high risk."), ("out", "The probability here is high."),
    ("out", "There is a low chance of a false INCONCLUSIVE_REPEAT."),
])
def test_r5_probability_flagged(out_r, fn_r, which, text):
    assert _ct(out_r if which == "out" else fn_r, text) != []


def test_r5_guarantee_paraphrase_still_passes(fn_r, out_r):
    for r in (fn_r, out_r):
        assert _ct(r, "Guarantee: at most 5% chance of a false-positive OUTSIDE_NORMATIVE_RANGE_REVIEW for "
                      "healthy people recorded like the calibration set.") == []


# R6 contradicting answers in natural or hyphenated form
@pytest.mark.parametrize("which,text", [
    ("fn", "The recording is outside the normative range."),
    ("fn", "Arguably this is OUTSIDE-NORMATIVE-RANGE-REVIEW."),
    ("fn", "The result is inconclusive; repeat."),
    ("out", "Overall the measures are within the normative range."),
    ("out", "This is WITHIN-THE-NORMATIVE-RANGE."),
])
def test_r6_contradicting_answer_flagged(out_r, fn_r, which, text):
    assert any("another answer" in p for p in _ct(out_r if which == "out" else fn_r, text))


@pytest.mark.parametrize("which,text", [
    ("out", "The result is not within the normative range; clinician review applies."),
    ("fn", "This result is not outside the normative range."),
])
def test_r6_negated_natural_form_passes(out_r, fn_r, which, text):
    assert _ct(out_r if which == "out" else fn_r, text) == []


def test_r6_inconclusive_whether_passes():
    r = load_report(ROOT / "mocks/inconclusive.json")
    assert _ct(r, "It cannot be said whether the result is within the normative range.") == []


# R7 invented numbers: words, integers lifted from report text, -1.96 as a value
@pytest.mark.parametrize("which,text", [
    ("out", "Twelve of the features were flagged; repeat in two weeks."),
    ("fn", "Repeat the recording in 5 weeks."),
    ("fn", "The flagged feature has z = -1.96."),
])
def test_r7_invented_numbers_flagged(out_r, fn_r, which, text):
    assert any("number not in report" in p for p in _ct(out_r if which == "out" else fn_r, text))


@pytest.mark.parametrize("text", [
    "No feature reaches |z| >= 1.96 (threshold z <= -1.96 or z >= +1.96).",
    "Lateralization z values lie between -1.96 and +1.96.",
    "Smallest attainable p-value is 1/(n_cal+1) = 0.0125.",
    "Guarantee: at most 5% chance of a false OUTSIDE_NORMATIVE_RANGE_REVIEW.",
    "The two hemispheres respond differently; the three tests all have p above 0.2.",
    "Percentiles compare with healthy reference recordings only; 79 were used for calibration.",
])
def test_r7_report_numbers_pass(fn_r, text):
    assert _ct(fn_r, text) == []


# R8 / R10 patient guard: whole words only; the patient's own wording survives
@pytest.mark.parametrize("text,src", [
    ("This sounds like MS.", "My arms feel heavy and I drop items."),
    ("This sounds like PD.", "I updated my notes on the shaking."),
    ("This sounds like a TIA.", "The initial tingling started in May."),
    ("You were diagnosed with depression.", "I was diagnosed with anxiety."),
    ("Your mother has Parkinson's, and you likely have it too.", "My mother has Parkinson's disease."),
    ("This could be, in your case, Parkinson's.", "I worry it could be Parkinson's."),
])
def test_r8_patient_guard_blocks_terms_they_did_not_write(text, src):
    import fieldnote_server as fs
    assert not fs.guarded(text, source=src)


@pytest.mark.parametrize("text,src", [
    ("You mentioned your mother has Parkinson's disease and that your right hand shakes.",
     "My mother has Parkinson's disease. My right hand shakes."),
    ("You said you were diagnosed with anxiety.", "I was diagnosed with anxiety."),
    ("You said you had a stroke two years ago.", "I had a stroke two years ago."),
    ("You said your arms feel heavy.", "My arms feel heavy."),
])
def test_r10_patient_guard_keeps_their_words(text, src):
    import fieldnote_server as fs
    assert fs.guarded(text, source=src)


# R9 one negation covering two joined verbs, and purpose negations
@pytest.mark.parametrize("text", [
    "This recording alone cannot confirm or establish a diagnosis; the clinician decides.",
    "This test cannot confirm or rule out any condition; the clinician decides.",
    "This test can neither confirm nor rule out a diagnosis.",
    "This result should not be used to rule out a motor problem.",
    "It is not meant to rule out disease.",
    "The test is unable to rule out or confirm a diagnosis.",
    "Whether there is a condition is not something this test can rule out or in.",
    "This does not establish a diagnosis and does not rule out disease.",
])
def test_r9_coordinated_disclaimers_pass(fn_r, text):
    assert _ct(fn_r, text) == []


def test_r9_coordinated_disclaimer_on_outside(out_r):
    assert _ct(out_r, "OUTSIDE_NORMATIVE_RANGE_REVIEW is a flag for review; it is not used to establish a "
                      "diagnosis.") == []


# R11 schema 0.3: the answer is fixed while calibration is pending; a repeat cannot change it
def test_r11_schema_03_action_not_repeat():
    r = load_report(FIX / "example_score_0.3.json")
    assert r.inconclusive_cause is None and r.calibration_pending
    s = summary(r)
    act = s.split("Suggested action:")[1].split("\n")[0]
    assert "calibration is pending" in act and "will not change it" in act
    assert "borderline" not in act and "odd/even" not in act and "data-quality notes" not in act
    t = offline_answer("Why is this inconclusive?", r, _tools(r))
    assert "calibration is pending" in t and "repeat the recording." not in t
    assert check_text(s, r) == [] and check_text(t, r) == []


def test_r11_cause_specific_actions():
    hd = load_report(FIX / "gen_halves_disagree_right_0.3.json")
    assert "odd/even halves of the recording disagreed" in summary(hd)
    assert not load_report(FIX / "real_subject_live.json").calibration_pending


# R12 INCONCLUSIVE / FAIL offline answers keep confound warnings (footer on that branch too)
@pytest.mark.parametrize("q", ["Summarize this report.", "Why is this inconclusive?", "Explain the flagged features",
                               "Draft a summary"])
def test_r12_inconclusive_answers_keep_confounds(q):
    d = json.loads((ROOT / "mocks/inconclusive.json").read_text())
    d["confound_warnings"] = ["Patient took levodopa 1 h before the recording; beta features less reliable."]
    r = load_report(d)
    t = offline_answer(q, r, _tools(r))
    assert "levodopa" in t and t.count("Statistical note:") == 1
    for lim in r.limitations:
        assert lim in t
    assert check_text(t, r) == []
