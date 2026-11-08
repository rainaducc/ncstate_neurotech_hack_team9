# Paste into the MODEL Claude Code session (~/brain-network-chart/neurosym) so its output matches the agent kit

Make the report JSON compatible with the agent team's contract (agent_kit/contract/report_schema_v0.json;
copy it into docs/ if not present):
- Top-level fields: schema_version, model_version, recording_id, answer (WITHIN_NORMATIVE_RANGE |
  INCONCLUSIVE_REPEAT | OUTSIDE_NORMATIVE_RANGE_REVIEW), answer_reasons[list of short strings that
  explain the answer, incl. p-values], context{...}, data_quality{status PASS|WARN|FAIL, bad_channels,
  clean_trials_per_condition, sync, notes}, features[{name, label (plain English), value, unit, z,
  percentile, hemisphere, hand, condition}], symmetry, three_state_profile, longitudinal (or null),
  scores{omnibus{score,p}, asymmetry{score,p}, within_subject{p}, combined{p}}, top_contributors,
  consistency, confound_warnings[], guarantee_note, limitations[], plots, audit.
- Write docs/report_schema.json (JSON Schema) and keep it in sync with the code; bump schema_version on
  changes; additive changes preferred.
- Generate sample reports in docs/samples/: one real held-out subject per answer type if possible, and
  one simulated weak-hemisphere case. Validate them against the schema in a test.
