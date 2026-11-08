# NeuroSym: proposed Watch-Imagine-Do diagnostic protocol (v1, for sign-off)

**Status:** This is a proposal only. Nothing below has been built, run or scored. NeuroSym is a research prototype, not a diagnosis.

Claims about the model come only from the verified brief. Claims tagged **[general EEG]** come from general EEG knowledge, not from this project's data.

---

## 1) How the current model works

- **Reference data**
  - 48 of 109 healthy adults from PhysioNet EEGMMIDB 1.0.0.
  - Runs used: 1 (rest, eyes open), 2 (rest, eyes closed), 3/7/11 (executed fist, left/right) and 4/8/12 (imagined fist, left/right).
  - All task trials are cued and done with eyes open.
  - There are no observation trials, no eyes-closed imagery, no arm or foot task, and no age, sex or handedness covariates.
- **Features (12)**
  - ERD = 10·log10(task/baseline) at C3 and C4, with baseline −1 to 0 s and task 0.5 to 2.0 s.
  - Mu: the SpecParam peak (±2 Hz, clipped to 8–13 Hz), or a fixed 8–13 Hz band. The fallback was used for 17 of 48 reference subjects. Beta is 13–30 Hz.
  - The 12 features are {execute, imagine} × {mu, beta} × {li_left, li_right, sym}.
  - Each subject needs at least 12 trials per condition and hand.
  - Beta rebound, Laplacian, rest power and the three-state profile are computed in phase2 but not used.
- **Scores**
  - Each feature gets a per-feature Gaussian z.
  - Three scores are computed from those z values:
    - Omnibus: Mahalanobis distance with Ledoit-Wolf shrinkage (0.52).
    - maxz: the largest |z|.
    - Within-subject: the max |Welch t| between right-hand and left-hand trials, over 8 tests, with 1000 permutations (seed 17).
  - Each score becomes a split-conformal p. The combined statistic is T = min p, calibrated with n_cal = 48. The smallest possible p is 1/49 = 0.0204.
- **Decision**
  - p < 0.05 gives OUTSIDE_NORMATIVE_RANGE_REVIEW.
  - p > 0.20 gives WITHIN_NORMATIVE_RANGE.
  - Anything in between gives INCONCLUSIVE_REPEAT.
  - INCONCLUSIVE_REPEAT is also returned if QC fails, if p is None, or if the full-recording, odd-half and even-half decisions disagree.
  - Each half needs at least 3 trials per hand per condition and at least 2 per hand per band.
- **Validation**
  - Healthy false-review rate at 0.05: 0.042.
  - Sensitivity to simulated one-sided weakness is low: flag rates of 0.04–0.29 across 20–60% scaling.
- **Live path**
  - ANT 24-channel cap over LSL, 18 channels used (A18), 1–40 Hz filter, amplitude rejection only.
  - No ICA, AutoReject or PyPREP. Mu is fixed at 8–13 Hz.
  - Protocol: 60 s eyes open, 60 s eyes closed, then 4 runs giving 16 trials per hand per condition.
  - Live QC passes with at least 8 clean trials per cell.
  - The live scorer uses the reference built with ICA and AutoReject, because `reports/phase3_live` has not been built. Validity on this device has not been established.

## 2) What the current protocol covers vs the spitball idea

| Element of the idea | Current protocol | Can it be scored against the current reference? |
|---|---|---|
| View the action | Not present | **No.** The reference has no observation trials. |
| Close eyes, imagine | Imagery is eyes-open only. Eyes are closed only during the 60 s rest. | **No.** Eye closure raises posterior alpha, which spreads into 8–13 Hz at C3/C4 under an average reference [general EEG]. An eyes-closed ERD compared against an eyes-open norm would mostly measure the eye state. |
| Perform the action | Present (executed fist) | **Yes**, if the cue and timing match the reference |
| Imagine (eyes open) | Present | **Yes** |
| Watch, imagine, do chained in one cycle | Not present. Reference runs are blocked by condition. | **No.** Imagery always follows observation, and the next baseline can sit inside the post-movement beta rebound [general EEG]. Neither matches the reference structure. |
| "As many examples as needed" | Fixed: 16 per hand per condition (live QC minimum 8) | **Only if stopping is blind to the outcome and the trial count is fixed (K).** Stopping when p crosses a threshold inflates false reviews. The within-subject t also depends on trial count. |

**Verdict:** keep the idea, but put it in a separate exploratory layer. The scored core stays exactly as it is, with the eyes-open blocked task run first. The watch-imagine-do sequence is added afterwards, with eyes open and unscored. Eyes-closed imagery becomes its own optional block in which the eyes stay closed throughout.

## 3) Recommended protocol

**Conflicts resolved explicitly:**

- **Order and priming.** All three proposals put the scored core before any new block. Two proposals showed action videos before the core. **Resolved:** before the core there is icon practice and verbal instruction only. Action videos are first shown at Block B, so the core keeps the reference context.
- **Eye closure.** One proposal put eye closing and opening inside each cycle. **Rejected.** The live path has no ICA, so eye-state changes would contaminate the baselines. Eyes-closed imagery gets its own block (Block C) with an eyes-closed baseline.
- **Observation control.** Options proposed were a static photo, non-biological motion, or a scrambled version of the same clip. **Chosen:** the same clip, phase- or spatially-scrambled, which roughly matches the clip's motion energy and luminance. A static photo does not control for visual motion [general EEG].
- **Session length.** Proposals ranged from 15–18 min to 30–35 min. **Resolved:** three tiers (below).
- **Marker format.** **Keep** `cue|cond|hand|ordinal`, with new `cond` codes and ordinals counted from 0 within cond × hand. The scored conditions stay byte-compatible.

| Phase | Eye state | Trial structure | Cue | Count | Approx. time | Scored? |
|---|---|---|---|---|---|---|
| Setup and impedance | — | — | — | — | not counted | — |
| P1 Rest (unchanged) | EO 60 s, then EC 60 s | — | spoken prompt | 1 each | 2.0 min | Feeds the mu peak and QC |
| P2 Familiarize | EO | Verbal instruction ("for imagine, feel the squeeze without moving") and 2 practice trials per condition per hand | same hand icons as the core | 8 trials, `cond=practice` | ~1.5 min | **No**, dropped by the filter |
| **P3 SCORED CORE** | EO, central fixation | Rest 2.5–3.5 s (jittered), then cue 4.0 s. Runs alternate Execute, Imagine, Execute, Imagine. Each run is 8 left and 8 right, pseudo-randomized with a logged seed, at most 3 same-hand trials in a row. | left/right hand icon | 16 per hand per condition (64 trials) | ~7.5 min plus 3 breaks of 30–60 s, about 9.5 min | **Yes**, with the unchanged scorer |
| After each Imagine run | EO | One tap rating: "How clearly could you feel it? 1–5" | on screen | 2 ratings | in the breaks | No (compliance only) |
| Break | — | — | — | — | ~1 min | — |
| **P4 Block B: Watch-Imagine-Do (eyes open)** | EO, fixation between phases | Rest 2.5–3.5 s, Observe 3 s, rest 2.5–3.5 s, Imagine 3 s, rest 2.5–3.5 s, Execute 3 s, rest 3.5–4.5 s. About 22 s per triplet. In control triplets, the Observe clip is scrambled. | 3 s first-person fist video (hand matches the cue), then an icon for Imagine and Execute | 10 action triplets per hand and 4 control triplets per hand, 28 in total, in 2 runs of 14 | ~10.5 min plus 1 break | **No** (exploratory) |
| **P5 Block C: eyes-closed imagery (optional)** | EC for the whole block (30 s settling at the start, discarded) | Rest 2.5–3.5 s (eyes closed), then Imagine 4 s | Tones: low = left, high = right, double beep = block end | 12 per hand | ~3.5 min | **No** (exploratory) |

**New `cond` codes:** `practice`, `observe`, `observe_ctrl`, `imagine_oie`, `execute_oie`, `imagine_ec`. The scorer accepts only `execute` and `imagine`.

**Session tiers (recording time, excluding setup):**

| Tier | Phases | Time | Use |
|---|---|---|---|
| Minimum | P1–P3 | ~13 min | Fatigued or impaired patients |
| Standard | P1–P4 | ~25 min | Default when the patient tolerates it |
| Extended | P1–P5 | ~29 min | Patients who tolerate it, and **every recording in a local healthy reference cohort** |

Blocks B and C can be dropped at any point without changing the scored result.

**Trial timing.** The brief does not state the current live trial timing. The values above (cue at least 2 s, rest at least 1 s, window inside the run) meet the eligibility rule. Replace them with the existing live timing if that already meets the rule and matches the reference more closely; this needs to be checked.

## 4) New measurements and what each adds

| # | Measurement | Source | What it adds | Reference status |
|---|---|---|---|---|
| a | Imagine − execute ERD contrast, per hemisphere, band and hand. Use a difference, not a ratio, because a ratio is unstable when execution ERD is near 0. | P3 | Could separate "motor plan preserved, output reduced" from a global reduction. The 12 features contain both conditions but never contrast them directly. | Can be normed from the existing 48 (see §5) |
| b | Per-trial consistency: share of trials with contralateral ERD < 0, trial-level SD, split-half reliability | P3 | Separates "consistently weak" from "noisy or non-compliant". The model currently sees only means. | Normable **only if** trial-level reference rows exist (unknown) |
| c | Post-movement beta rebound and Laplacian ERD | P3 | Execution information beyond ERD [general EEG]. Already computed in phase2. | Normable in principle (see §5) |
| d | Observation mu/beta suppression at C3/C4: observe minus observe_ctrl, plus an observation LI | P4 | Motor-system engagement that needs neither movement nor imagery ability. Observation-related suppression is generally weaker and less hand-specific than execution ERD [general EEG]. | **None.** Within-subject descriptive only. |
| e | Ordering of ERD magnitude across observe, imagine and execute within a triplet | P4 | Whether engagement increases across the three phases. The phase2 "three-state profile" may be a different construct; do not assume they are the same. | **None** |
| f | Priming index: imagery after an action clip vs after a control clip, and imagine_oie vs core imagine | P4 vs P3 | Whether imagery is associated with facilitation after observation. Confounded with time-on-task, because Block B always runs later. | **None** |
| g | Eyes-closed imagery LI, against an eyes-closed baseline | P5 | Whether eye closure is associated with stronger lateralized imagery for this patient | **None** |
| h | QC only: posterior-alpha index (if O/P channels are in A18; membership not stated in the brief), blink rate per phase, vividness ratings, movement or EMG during imagery (if the ANT has an aux input; unknown) | all | Flags mu contamination and imagery non-compliance | Never scored |

Measurements d–g are reported as within-subject effect sizes with CIs and permutation p (fixed seed). They carry the label "EXPLORATORY: no healthy reference, not part of the decision". They never get a z or a conformal p.

## 5) Model-side changes required, and what can or cannot be normed

**Required before any session uses the new protocol:**

1. **Condition whitelist.** The scorer reads only `cond ∈ {execute, imagine}` from P3. Add a unit test that injects every new `cond` code and asserts that T, the per-score p values and the decision are unchanged.
2. **Fixed-K trial selector.** Score exactly the first K clean trials per cell, in presentation order, with halves split by the original ordinal as now.
   - Why: the within-subject max |Welch t| depends on trial count, and its calibration used the healthy subjects' own counts.
   - Status: the healthy per-subject trial counts are not in the brief and must be read from the reference data.
3. **Live QC minimum.** Raise it from 8 to K. Today's 8 is below the reference rule of 12. This conflict exists in the current system as well.
4. **Prerequisite power study** (needs trial-level reference rows). Subsample healthy trials to K = 8, 12, 16 and 20 per cell (seed fixed, only K values the reference supports). At each K:
   - recompute the healthy false-review rate;
   - recompute the simulated-weakness flag rates;
   - re-check the within-subject calibration.

   This also fills the missing power analysis of trial count.
5. **Separate exploratory module** (for example `neurosym/exploratory_oie.py`).
   - It reads only the new codes and uses its own baselines: fixation before observe, eyes-closed rest before imagine_ec, eyes-open rest before the triplet phases.
   - It never writes into p or the decision.
6. **Report schema `neurosym-report-0.5`.** New fields:
   - `protocol_version`, `tier_run`, `blocks_run` with an eye state per block;
   - per-cell `target_K`, `clean_used`, `attempted`, `rejected`, `stop_reason`;
   - `patient_capability` (execute or attempted);
   - `imagery_vividness[]` and `compliance_flags`;
   - `exploratory{…, normed:false, in_decision:false}`;
   - `reference_id` and `device`.

   Also reconcile agent contract v0 (PASS/WARN/FAIL plus guarantee_note) with the `qc_passed` boolean.
7. **Existing blocker, not fixed by this protocol:** `reports/phase3_live` is still not built. Live scoring still uses the ICA+AutoReject reference.

**What can and cannot be normed:**

| Can be normed with existing EEGMMIDB (n=48), subject to checks | Cannot be normed with existing data |
|---|---|
| The 12 core features (current) | Observation and observation control (d) |
| Contrast (a). It is a linear combination of existing features, so a healthy distribution exists. Adding it to the **omnibus adds no new information**, because Mahalanobis distance already uses those dimensions. Adding it to **maxz** changes the test family, so T must be recalibrated. | The triplet-embedded imagery and execution, and the priming index (e, f) |
| (b), only if `reports/phase3/trials/reference_features.csv` holds trial-level rows. The brief does not say whether it does. | Eyes-closed imagery (g) |
| (c). Computed in phase2. Check whether the beta rebound window fits within the reference cues and runs, and whether the Laplacian neighbours exist on the ANT A18 layout. | Attempted (not executed) movement. The reference subjects executed. |
| Within-subject statistic at a fixed K, via subsampling | Any age, sex or handedness adjustment. The reference has no covariates. |

**Adoption rule for (a)–(c) in the scored set:**

- Re-estimate the Gaussians and the Ledoit-Wolf matrix (the 0.52 shrinkage will change).
- Re-run the leave-one-out and leave-two-out calibration of all scores and of T.
- Redo the weakness simulation.
- Adopt only if sensitivity improves while the healthy false-review rate stays at or below 0.05. Pre-register this criterion before looking at any patient data.

**Norming d–g needs a local healthy cohort** recorded on the ANT device with the Extended tier.

- Cohort size: n_cal ≥ 20 is the minimum for p < 0.05 to be reachable at all, because 1/(n+1) < 0.05 requires n ≥ 20. One proposal said 19; that is wrong, since 1/20 = 0.05 is not below 0.05. Matching today's floor needs at least 48 after exclusions. p < 0.01 needs n ≥ 100.
- Exclusion rate: the current pipeline excluded 61 of 109. That came from the offline ICA/PyPREP/AutoReject path and may not carry over to the ANT live pipeline, but plan to record well over 48.
- Record age, sex and handedness.
- The same cohort would also build `phase3_live`.
- No public healthy dataset with observation or eyes-closed imagery is confirmed. None is named here.

## 6) Adaptive stopping rule

"As many as needed" means **stop when there is enough clean data, never when the result looks settled.**

1. **Pre-register K** per condition × hand. Default K = 16; it must be at least 12 and within the range of the healthy reference counts. The final value comes from the power study.
2. **The dashboard is blind to the outcome while recording.** The operator sees only clean-trial counts per cell and per half. z, p and the decision stay hidden until recording ends.
3. **QC-driven top-ups.**
   - After P3, a top-up run is queued if any cell has fewer than K clean trials, or fails the half minimums (at least 3 per hand per condition per half, at least 2 per hand per band).
   - A top-up contains only the deficient condition, both hands, randomized.
   - Cap: 2 top-up runs per condition.
   - If the cap is reached without K clean trials, the result is INCONCLUSIVE_REPEAT (QC fail).
4. **Score once**, on the first K clean trials per cell. Surplus trials are logged and not scored.
5. **No extension within a session after p is seen.** One proposal allowed one pre-registered extra run after an INCONCLUSIVE result, subject to a two-stage healthy simulation. **Rejected for v1**, for two reasons:
   - p moves in steps of 1/49.
   - Splitting alpha over 3 looks is impossible (0.05/3 = 0.0167 < 0.0204), and the two-stage simulation has not been done.

   This can be revisited after the power study.
6. **INCONCLUSIVE_REPEAT means a new session on another day**, scored independently.
   - There is no test-retest data, so there is no calibrated rule for combining sessions.
   - Policy is to be chosen before data collection (see §8):
     - Option A: report each session separately and note the union bound. Up to about 0.084 of healthy people could get at least one OUTSIDE across two sessions.
     - Option B: use α = 0.025 per session. It is reachable, but only the minimum p of 1/49 would flag.
7. **Exploratory blocks use fixed counts** (Block B: 10 action and 4 control triplets per hand; Block C: 12 per hand), with no stopping rule. Fixed counts keep a future healthy reference comparable. A precision-based stop was rejected for this reason.

## 7) Risks and confounds

1. **Eyes-closed alpha leaking into mu** [general EEG]. This is the main flaw in the original idea. Mitigation: Block C is self-contained, eyes-closed throughout and unscored, plus the posterior-alpha QC index. Residual risk: Block C ERD is not comparable to P3.
2. **Eye-state transitions and blinks.** The live path has amplitude rejection only. Mitigation: no transitions inside a trial, and a 30 s settling period that is discarded. Expect higher rejection in Block C.
3. **Visual activity during the videos.** Posterior alpha suppression from the video may spread to C3/C4 under the average reference and look like mu suppression [general EEG]. The scrambled-clip control reduces this but does not remove it.
4. **Carryover.** Beta rebound and lingering ERD can leak into the next baseline [general EEG]. Mitigation: jittered rests of 2.5–4.5 s. The time course in patients is unknown.
5. **Order and time-on-task confound.** The triplet order is fixed, and Block B always follows P3. Effects of observation, priming and fatigue cannot be separated from condition. This is acceptable for exploratory work only.
6. **Fatigue and burden.** The Standard tier roughly doubles recording time. Drowsiness-related alpha increase can mimic reduced ERD [general EEG]. Mitigation: core first, tiers, and blocks that can be dropped.
7. **Imagery compliance.** Covert movement or absent effort cannot be verified without EMG (availability unknown). Vividness is self-report.
8. **Attempted vs executed movement.** Patients who cannot clench are not exchangeable with the reference subjects. Record this. It may invalidate the execute features for that patient.
9. **Marker and timing integrity.** Video playback can add marker latency over LSL. Measure the latency before any cohort recording. Leakage of new codes into the scored stream would invalidate p; this is covered by the whitelist test.
10. **Ways this could be worse than today:**
    - stopping on p, or scoring all trials instead of K;
    - adding features without re-validation, which dilutes power or enlarges the maxz family;
    - a local cohort smaller than 20, which cannot produce p < 0.05 at all;
    - exploratory numbers shown next to scored ones and read as diagnostic.
11. **Inherited limits, not fixed by this protocol:**
    - one public site with n = 48;
    - weakness simulated, not real;
    - sensitivity of 0.04–0.29;
    - Mac/Linux feature differences up to 0.37 dB;
    - device validity not established;
    - no test-retest data, no RCI, no covariates.

## 8) Open questions to decide before anything is built

1. **Order:** do you approve core-first, with no action videos before P3 (icon practice only)?
2. **Default tier for patients:** Minimum (~13 min) or Standard (~25 min)?
3. **K and the top-up cap:** do you approve running the subsampling power study first, and choosing K from it (default 16)?
4. **Repeat-session policy:** Option A (separate reports, about 0.084 bound) or Option B (α = 0.025 per session)?
5. **Local healthy cohort:** do you commit to one, and at what size (at least 20 so p < 0.05 is reachable; at least 48 to match today's floor)? This also needs ethics approval and consent arrangements, which are outside the brief.
6. **Block C:** keep the eyes-closed imagery block at all, or drop it until there is a reason to norm it?
7. **Control stimulus:** scrambled clip (recommended), static photo, or both?
8. **Attempted movement:** allowed in P3 for patients who cannot execute, with the result flagged as not exchangeable? Or does that send the patient to the exploratory path only?
9. **Tier 1 features (a)–(c):** pursue full re-validation into T now, or keep them descriptive for v1?
10. **Dashboard:** show exploratory numbers to clinicians at all, or only in a research view?
11. **Data checks to run first:**
    - Is `reference_features.csv` trial-level, and what are the healthy per-subject trial counts?
    - What is the current live trial timing?
    - Which O/P channels are in A18?
    - Does the ANT system have an aux EMG input?
12. **Markers:** keep the 4-field format with new `cond` codes (recommended), or add a `block` field?

Per your instruction, nothing here will be built or run until you confirm how it works and how well it works with the model.

---

## Addendum: checked by hand on 2026-11-08, after the synthesis

These checks were made directly against `~/Downloads/neurosym` after the study finished. Where they conflict with the sections above, the addendum wins.

1. **`reports/phase3_live` is now built.** It was written at 05:18 today, while the study was running, so §1, §5.7 and §7 are out of date on this point. `build_summary.json` says:
   - **Reference size:** n_reference = n_cal = **79**, so the smallest p is **1/80 = 0.0125**. It was 1/49 = 0.0204.
   - **Healthy false-review rate at 0.05 (leave-one-out):**

     | Score | Rate |
     |---|---|
     | omnibus | 0.038 |
     | maxz | 0.025 |
     | within | 0.038 |
     | **combined** | **0.051** |
     | within, uncalibrated | 0.165 |

     **The combined rate is slightly above 0.05.** It is within finite-sample noise for n = 79, but the model team should look at it before the guarantee note is shown on live reports.
   - **Healthy answer counts:** WITHIN 49, **INCONCLUSIVE 26 (33%)**, OUTSIDE 4. A third of healthy recordings already come back INCONCLUSIVE_REPEAT on the live path. That makes the repeat-session policy (§6.6, §8.4) more urgent.
   - **Live vs offline features** (`parity.csv`, 35 subjects): r = 0.70–0.91, mean |diff| = 0.32–0.79 dB per feature. The live pipeline is not interchangeable with the offline one.
   - **Consequence for §6.5:** with n_cal = 79, 0.05/3 = 0.0167 > 1/80, so an alpha-split multi-look design is no longer impossible in principle. It still requires the two-stage healthy simulation first. The v1 recommendation (no in-session extension) stands.
   - **Consequence for §6.6 Option B:** α = 0.025 per session now allows p ∈ {1/80, 2/80} to flag.
2. **Trial-level reference rows exist (offline reference).** `reports/phase3/trials/subjects/S*.json` holds a `trials` list (condition, hand, ordinal, half, band, c3/c4 ERD, LI) for all 48 subjects. This answers §8.11a. The power study (§5.4) and the trial-consistency feature (§4b) are feasible.
   - **Trials per cell:** the smallest cell (condition × hand × band) per subject ranges from 12 to 22.
   - **Subjects that support each K:**

     | K | Subjects |
     |---|---|
     | 8 | 48 |
     | 12 | 48 |
     | 14 | 47 |
     | 16 | 42 |
     | 20 | 31 |

     So the default K = 16 drops 6 reference subjects from a fixed-K recalibration. K = 12 keeps all 48.
   - I did not check whether `phase3_live` has trial-level rows.
