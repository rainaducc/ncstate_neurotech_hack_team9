#!/usr/bin/env python3
"""Run this on every new report JSON the model team sends:
    python check_new_report.py path/to/report.json [more.json ...]
Checks: contract validation, adapter load, unknown/renamed fields, guardrail on the offline summary.
Exit code 0 = the agent can consume it; 1 = adapter/contract needs an update (see messages)."""
import sys
from report_adapter import CONTRACT_VERSION, check_text, load_report, validate
from offline_summary import summary

ok = True
for path in sys.argv[1:]:
    print(f"\n=== {path}")
    probs = validate(path)
    print("contract v" + CONTRACT_VERSION + ":", "valid" if not probs else f"{len(probs)} problem(s)")
    for p in probs:
        print("  -", p)
    try:
        r = load_report(path)
    except Exception as e:  # noqa: BLE001
        print("  ADAPTER FAILED:", repr(e)); ok = False; continue
    print(f"loaded: answer={r.answer} features={len(r.features)} schema={r.schema_version} model={r.model_version}")
    for n in r.adapter_notes:
        print("  adapter:", n)
    if r.unknown_fields:
        print("  new/unknown fields (agent ignores them until mapped):", ", ".join(r.unknown_fields))
    g = check_text(summary(r), r)
    print("offline summary guardrails:", "pass" if not g else g)
    if probs and not r.adapter_notes:
        ok = False
sys.exit(0 if ok else 1)
