# NeuroSym × Fieldnote — Demo Rehearsal Script

**Setup before presenting:**
- Fieldnote (patient side): http://localhost:8080 — check the My-story badge says "AI guide connected"
- Clinician app: http://localhost:8501 (second tab, don't switch to it until Act 3)
- Quick health check: `curl localhost:8080/api/health` → `{"ok": true, "llm": true}`
- If the API hiccups mid-demo: don't panic — every agent call silently falls back to deterministic templates. The demo keeps working; only the badge wording changes.

**Framing line (Act 0, ~30s):**
> "NeuroSym is a normative-model EEG system. The clinical side tells a clinician whether a recording falls inside or outside a normative range. But the patient side of the story was missing — that's Fieldnote, with an AI copilot that prepares the patient *before* the visit and never practices medicine."

---

## Act 1 — The patient's story (Fieldnote → http://localhost:8080)

1. **Intro page** (`demo_01_intro.png`): point out "Research prototype / Information stays in this tab" — trust framing is part of the design.
2. **My story → 4 guided questions** (`demo_02_story_q1.png`): answer them live (or click "Use a fictional example"). Type answers like *"Typing feels less fluid than usual."* — the AI uses the patient's OWN words.
3. **AI drafts the summary** (`demo_03_story_complete.png`): after question 4, the GUIDE says it drafted an account in the right panel. Key line to read aloud:
   > "Drafted by your AI guide from your own answers — check every word before confirming. You confirm it, not the AI."
   This is generation/confirmation separation — the AI proposes, the human owns it.
4. **Ask the guide** (`demo_04_guide_chat.png`): type *"What happens to this after I confirm it?"* — it answers process questions only. Mention: if you ask it to interpret symptoms, it refuses. Guardrails, not vibes.
5. **Confirm** (`demo_05_confirmed.png`): click "Yes, this sounds right."

**Talking point:** every interaction is logged to `neurosym-agent/audit/fieldnote_log.jsonl` — auditable by design.

## Act 2 — Movement tasks (same tab)

6. **Movement tasks tab** (`demo_06_tasks_start.png`): start the keyboard/tapping task. Note the disclaimer: "EEG is not connected in this prototype" — the keyboard stands in for the sensor.
7. **Record left hand, then right** (`demo_07_left_feedback.png`, `demo_08_right_feedback.png`): tap for the full duration.
8. **Recording assistant feedback** (`demo_09_task_feedback.png`): live LLM output using ONLY computed numbers from this recording — tap counts, average interval, rhythm consistency. Never says "normal" or "abnormal," never compares to a healthy range. If the model drifts (it did once in testing — said "which is common"), a guardrail rejects it and falls back to a template. **This is the same check_text discipline as the clinician-side contract.**
9. **Recordings / share views** (`demo_10_recordings.png`, `demo_11_share.png`): the confirmed story + task summary assemble into a pre-visit profile.

## Act 3 — The clinician side (switch tab → http://localhost:8501)

10. **Streamlit clinician app** (`demo_12_clinician.png`): pick a report (mock `MOCK_OUTSIDE_WEAK_LEFT`, or a real one), show the color-coded answer banner, flagged features on the normative curve, hemisphere comparison, confounds panel.
11. **Copilot chat**: ask *"Explain the flagged features"* — it tool-calls `get_report`/`explain_flag`, and can only cite numbers from the report. Ask it to diagnose → guardrail forces a refusal.
12. **Draft summary + Accept** → saved to `audit/` — clinician-facing generation also keeps a human in the loop.

**Closing line:**
> "One guarded agent core, two sides of the clinic: the patient walks in prepared and in control of their own words; the clinician gets a normative answer with data-quality honesty. Neither side lets the AI practice medicine."

---

## Emergency fallbacks
| Problem | Fix |
|---|---|
| API down / timeout | Everything falls back to templates automatically — keep going |
| Server not running | `cd ~/WorkBuddy/Hackathon/neurosym-agent && source .venv/bin/activate && python fieldnote_server.py` (key auto-loads from ~/.zshrc in Terminal) |
| Want to reset the story | "Start these questions again" button on the story page |
| Clinician app blank | Refresh; pick a report in the left sidebar |
