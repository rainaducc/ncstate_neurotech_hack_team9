# Run Fieldnote + the NeuroSym agent

The UI is `fieldnote-ui/dist/`. `agent/fieldnote_server.py` serves the UI and every endpoint on one
port (8080), so no CORS setup is needed.

## Quick start (template mode, no API key needed)

```bash
cd agent
python3 -m venv .venv && .venv/bin/pip install jsonschema pytest anthropic streamlit plotly pylsl
.venv/bin/python fieldnote_server.py      # open http://localhost:8080
```

Live AI mode: `export ANTHROPIC_API_KEY=sk-ant-...` before starting the server. Never commit the key.

## What the server does

- **My story:** the agent drafts a pre-visit account from the patient's four answers, in their own
  words, with nothing added and no diagnosis language. The patient reviews and confirms it. The
  "Ask the guide" chat answers process questions only.
- **Movement session:** the cued EEG task. See below.
- **Clinician view → EEG report & copilot:** the NeuroSym report and the tool-calling copilot
  (`agent.py`). Every answer passes `check_text()` and is logged to `audit/agent_log.jsonl`.

## Movement session (replaces the keyboard tapping task)

The patient wears the ANT eego headset and follows on-screen cues. It is the same task the model's
healthy reference uses, and the same schedule as `neurosym-agent/live/cue_runner.py`:

| Block | Time | What the screen says |
|---|---|---|
| Rest, eyes open | 60 s | Look at the cross |
| Rest, eyes closed | 60 s | Eyes closed until the tone |
| 4 runs (MOVE, IMAGINE, MOVE, IMAGINE) | ~2 min each | `MOVE`/`IMAGINE` + `← LEFT`/`RIGHT →` for 4 s, then a cross for 4–4.5 s |

That gives 16 cues per hand per condition, about 11 minutes. **Rehearsal** mode has 3 cues per hand
per run and no rest blocks (about 3½ min). It is practice only: its markers are logged but **not sent to
LSL**, because they are identical to a real session's and `live_score.py` would score the rehearsal as
final and exit before the real session.

- **Schedule:** `agent/live_protocol.py` is a byte-identical copy of `neurosym-agent/live/protocol.py`
  (a test checks the sha256). The trial order uses a new random seed per session, logged with it.
- **Markers:** each cue is pushed to the LSL stream `NeuroSymMarkers` (type `Markers`, source
  `neurosym-cues`), exactly like `cue_runner.py`, so `live_score.py` scores a Fieldnote session
  unchanged. Markers carry the time the cue was drawn (browser frame timestamp converted to LSL time
  via a clock offset measured at the start), not the time the HTTP request arrived.
- **Headset check:** before starting, the page lists the EEG stream, whether C3/C4 exist, whether data
  is flowing, and whether a scorer is listening. A full session needs all of these, and is re-checked
  the moment Begin is pressed; a rehearsal needs none of them.
- **Camera check (optional):** frame-to-frame motion on the device. It counts MOVE cues with little hand
  movement and IMAGINE cues with movement. Video never leaves the browser and is not stored. It is a
  compliance check only; it does not touch the EEG or the result.
- **What the patient sees:** progress and the camera counts. No EEG values and no result.
- **Logs:** `agent/sessions/<id>.json` and `<id>_cue_log.csv` (same columns as cue_runner).
- **Stopping:** Esc, leaving the page, or hiding the window stops the session and sends `session_aborted`.
- **Marker stream lifetime:** the server opens `NeuroSymMarkers` once at start-up, so `live_score.py` can
  be started before or after the server. If another `NeuroSymMarkers` stream (e.g. `cue_runner.py`) is
  already running, the server does not open its own, and a full session refuses to start while a second
  source exists.
- **Stopped sessions:** Esc or leaving sends `session_aborted`. `live_score.py` treats that like
  `session_end` and still writes a final report if enough clean trials exist (same as with cue_runner).

### Scoring a session (model team's live scorer, unchanged)

```bash
cd ~/Downloads/neurosym/agent-latest/neurosym-agent
NEUROSYM_MODEL_REPO=~/Downloads/neurosym ~/Downloads/neurosym/.venv/bin/python live/live_score.py \
  --participant P01 --age 68 --handedness right \
  --out /path/to/this/repo/agent/live_reports
```

Start it after the Fieldnote server is up and before pressing Begin. The final report appears under **Clinician view → EEG report &
copilot** (provisional mid-session reports are not listed). To test the whole pipeline without the cap, run
`live/sim_eeg_stream.py` and a **full** session. Reports from the simulated stream are flagged as
simulated, and the clinician view labels them.

## Shared communication layer (patient ↔ clinician)

One thread per patient, stored on the server (`agent/shared_store.py`, SQLite at `agent/data/fieldnote.db`),
so the patient and the clinician can use separate browsers or devices.

- **Patient → Messages:** write to the care team; see clinician replies and updates. The dashboard shows the
  clinician's latest message.
- **Clinician → Inbox:** the patient's thread with a reply box, plus what is shared (profile, sessions,
  attached reports). **EEG report & copilot → Attach to patient** links a report to the patient.
- **Thread events:** profile shared / withdrawn, movement session completed (sessions started from
  Fieldnote are linked to the patient automatically), EEG report attached.
- **Consent:** the clinician sees the profile only while the patient's share is active. Withdrawing removes
  it from the clinician's screen on the next sync (within ~4 s), even mid-typing.
- **What the patient never gets:** EEG numbers, answer labels or report IDs. Attaching a report tells the
  patient only that the care team has the results to go through with them.
- **Sync:** both sides poll every 4 s.
- **Not yet:** accounts and sign-in (one demo patient `P-0001` and the role switch stand in for identity —
  use fictional data only), notifications, AI-drafted replies. To reset the thread, stop the server and
  delete `agent/data/fieldnote.db`.

## Audit

Story and guide interactions: `audit/fieldnote_log.jsonl`. Copilot: `audit/agent_log.jsonl`.
