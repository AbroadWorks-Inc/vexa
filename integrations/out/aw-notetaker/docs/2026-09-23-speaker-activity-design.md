# Speaker activity file — design (addendum to the rearchitecture design)

Date: 2026-09-23 · Branch: `feat/aw-rearchitecture` · Parent: [`2026-09-23-aw-rearchitecture-design.md`](2026-09-23-aw-rearchitecture-design.md) §4.3

## In plain words

A named transcript needs two inputs, exactly as the Jitsi pipeline does:

1. **The audio** (`master.webm` → `audio.wav`). Whisper turns it into text with timestamps. No size limit; always recorded.
2. **Who was talking when.** `notetaker-worker` matches each text line's time against it and writes the speaker's name. Jitsi gets it from Prosody's speaker timeline; aw-bots must produce it for Meet, Zoom and Teams.

Today aw-bots only has (2) inside its **debug tape** (`captured-signal.jsonl`), which also stores a full copy of everyone's audio. That makes it about 5 MB per minute, and aw-bots caps it at 250 MB. On a 2–3 hour meeting the tape stops after about 50 minutes, and everyone after that loses their name.

The fix: aw-bots writes **(2) on its own**, in a small file with **no audio**: `speaker-activity.jsonl`. It is always written, needs about 1–40 MB for a 3-hour meeting, and is the only source the exporter reads for names. The debug tape goes back to being a debugging tool: off by default, switched on only to investigate a meeting.

There is **no fallback** to the old tape. If `speaker-activity.jsonl` is missing, that is an incident: logged as an error with a fixed event name for alerting. The transcript is still produced so the meeting isn't lost. Transcription itself never depends on this file; if transcription fails, `/process` is re-run from the audio kept in S3.

## The file

Path in the bot pod: `<VEXA_CAPTURE_SIGNAL_DIR>/<session>.speaker-activity.jsonl` (default dir `/tmp/captured-signal`).
In S3 after upload: `aw-bots/signal/<user_id>/<meeting_id>/<session_uid>/speaker-activity.jsonl`.

One JSON object per line:

| Line | Shape | When |
|---|---|---|
| header (first line) | `{"type":"speaker_activity_header","v":1,"session_uid","platform","lane":"gmeet"\|"mixed","native_meeting_id","started_at","image_version"}` | at bot start |
| frame | `{"t":<epoch ms>,"ch":<channel>,"name":"<display name>"?,"rms":<0..1, 5 dp>,"dur_ms":<int>}` | every captured audio frame (Meet names it; Zoom/Teams frames are unnamed) |
| hint | `{"type":"hint","t":<epoch ms>,"name":"<display name>","isEnd":true?}` | Zoom/Teams active-speaker signal |
| capped | `{"type":"capped","t":<epoch ms>,"bytes":<n>}` | once, if the safety ceiling is ever reached; nothing is written after it |

- **No audio samples**, ever.
- Same clock as the recording: `t` is capture epoch ms, the same domain the tape uses, so the measured origin rule (spec §4.3) still holds.
- Safety ceiling: `VEXA_SPEAKER_ACTIVITY_MAX_BYTES`, default 1 GiB, about 25× a 3-hour meeting. It exists only so a runaway bug cannot fill the pod's disk and kill the recording. It is never expected to be reached.

## Changes

| Where (aw-bots) | Change |
|---|---|
| `core/meetings/services/bot/src/speaker-activity.ts` (new) | The writer. Buffered, serialized appends in order (same pattern as the tape recorder); never throws into capture. |
| `core/meetings/services/bot/src/capture-bridge.ts` | Feed every frame and hint to the writer at the two existing tap points. This runs **independently of the debug tape**. |
| `core/meetings/services/bot/src/index.ts` | Always create the writer. At teardown: close it, then upload it **before** the debug tape files (smallest and most important first, inside the SIGTERM grace). |
| `core/meetings/services/bot/src/signal-upload.ts` | New part `speaker-activity`, uploaded whether or not the debug tape exists. |
| `core/meetings/services/meeting-api/…/recordings/jsonb.py` | Add `"speaker-activity"` to the accepted signal parts (today unknown parts get 422). |
| `integrations/out/aw-notetaker/exporter/` | Read `speaker-activity.jsonl` instead of `captured-signal.jsonl`. Wait for it like today's tape wait. States `ok`, `missing` (error log `speaker_activity_missing`), `invalid`, `capped`. The old tape reader is removed. |
| Deployment (later) | Turn the debug tape off by default (`capture_signal=false` platform setting in admin-api). |

## Out of scope

Teams caption authors as an extra naming source (a later improvement), the calendar-dispatcher decision, and deployment manifests.
