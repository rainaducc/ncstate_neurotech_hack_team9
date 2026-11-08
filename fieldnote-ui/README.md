# Fieldnote interface prototype

Patient and clinician workflows for preparing a patient-confirmed pre-visit profile. Fieldnote is a working name.

## Run

Open `dist/index.html` in a browser, or serve `dist/` with a local static server. No installation or build is required. Fonts load from Google Fonts, with local fallbacks.

## Included

- Dashboard with editable basic profile and personal bulletin-board reminder.
- Four-question guided conversation, preserving the patient's exact answers.
- Editable summary and explicit patient confirmation.
- Timed left/right keyboard tapping, timestamps, counts, and average intervals.
- Recording details and JSON download.
- Pre-visit profile and text report download.
- Explicit consent to sharing a snapshot with the demo clinician view, plus withdrawal.
- Separate clinician workspace with shared profile and a demo note for the patient.
- Patient account workspace with basic profile and a private editable note.

## Prototype boundaries

No EEG stream, actual AI model, authentication, external clinical sharing, database, or clinical scoring is connected. The guide is scripted. Both roles share the same in-memory state for demonstration, and switching roles is not an access-control mechanism. Refreshing clears all entered information. Use fictional data. Exported files remain on the user's device. A clinician share includes basic profile, confirmed account, and the latest task recording only.

## Integration points

`dist/app.js` contains the state and rendering functions. The guided conversation can be replaced with a backend agent that returns a proposed summary for patient review. Keep confirmation separate from generation. Replace keyboard-only task context with verified EEG collection metadata when integrated. Keep clinical interpretation distinct from patient reports and computed signals. Actual sharing needs authenticated accounts, explicit recipient selection, secure storage and transmission, and a real consent/access model.

The palette and type styles are in `dist/style.css`. The illustrated GIF is in `dist/assets/walk.gif`. The artwork is generated for this project.

## Current visual direction

The latest design removes the dog, stamp, postmark, paper grain, tape, and handwritten decorative elements. It centers the patient account in a document workspace, with profile details and a private note alongside it. `dist/editorial.css` contains the visual layer. Earlier illustration files remain in the project assets for other deliverables but the interface no longer references them.
