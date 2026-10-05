# Recording and speakers

Where audio and speaker names are written, how Meet, Zoom, and Teams differ, and what happens to
the bot pod. The exporter's handoff of that material is
[export and webhooks](export-and-webhooks.md).

## Two objects in bucket `aw-bots`

Audio and the speaker file are different objects. Keys are built in
`meeting_api/recordings/jsonb.py`.

| Object | Key |
|---|---|
| One audio chunk | `recordings/{user}/{recording}/{session}/audio/{seq:06d}.webm` |
| The session master | `recordings/{user}/{recording}/{session}/audio/master.webm` |
| Who spoke when | `signal/{user}/{meeting_id}/{session}/speaker-activity.jsonl` |

`meeting_id` in the signal key is the integer meeting id. `recording` is the recording id.
`session` is the bot's connection id. The speaker file is written for every meeting. It does not
depend on the debug tape (`capture_signal`), which stays off in production because that tape also
stores audio and is capped.

The bot writer is `core/meetings/services/bot/src/speaker-activity.ts`. meeting-api accepts the
part name `speaker-activity` in `SIGNAL_TAPE_PARTS`.

## Lanes

`captureLane` in `core/meetings/services/bot/src/config.ts` is the one function the header, the
capture bridge, and the pipeline share.

| Platform | Lane | What the activity file holds |
|---|---|---|
| Google Meet | `gmeet` | Named per-participant frames |
| Zoom | `pertrack` | Named frames, one track per participant |
| Teams, Jitsi | `mixed` | The server mix. Frames are unnamed. Hints carry the active speaker. |

Zoom is the only `pertrack` platform (`isPerTrackLanePlatform`). Teams stays on the mix.

The exporter (`exporter/activity.py`) keeps a named frame on every lane. Hints become the
timeline only when the file stored no named frame. That is the Teams path, and a Zoom file that
never named a channel.

`build_speaker_timeline` in `exporter/attribution.py` then:

- Builds intervals from those named frames when the frames paired.
- When nothing paired and at least two speakers are named, derives intervals from the point runs.
  Each run ends at its last point plus `SPEECH_HANGOVER_MS` (default 700) and is clipped so it
  does not overlap the next speaker or the recording.
- Sets `speaker_intervals_source` to `audio`, `points`, or leaves it unset when there are no
  intervals.
- For Zoom and Teams with at least two named speakers, anchors the earliest point to the start
  of the recording. Meet's timeline is left as captured.

A frame counts as speech at or above `RMS_SPEECH_THRESHOLD` (default 0.026).

`notetaker-worker` reads `speaker_timeline.json` from the export bucket, including
`speaker_intervals`. This repo does not change that worker.

## Google Meet recording notice

After Meet admits the bot, join posts a chat message
(`core/meetings/modules/join/src/googlemeet/chat.ts`, `MEET_RECORDING_NOTICE`):

> AW Notetaker is recording this meeting for transcription. See https://abroadworks.com/notetaker-privacy for details. If you'd prefer the meeting not be recorded, ask the host to remove this bot.

A missing chat control is logged and the join continues. This is not the `chat_send` act, and
`voiceAgentEnabled` does not turn it on. Teams, Zoom, and Jitsi are unchanged.

## The runtime pod

`RUNTIME_BACKEND=k8s` on EKS (`deploy/helm/charts/vexa/templates/deployment-runtime.yaml` sets it
from `runtime.backend`). Meeting-api asks the runtime whether a bot is running and to delete it.
Meeting-api does not talk to Kubernetes itself.

A meeting-bot pod (env contains `VEXA_BOT_CONFIG`) gets `activeDeadlineSeconds`:
`BOT_MAX_ACTIVE_MS` when that is set, otherwise 4 hours, plus 15 minutes
(`k8s_backend.py` `_meeting_bot_deadline_seconds`). With the default cap that is 15300 seconds,
measured from pod start. Agent pods do not get this deadline.

The reaper in `runtime_kernel` stores the exit code, then deletes `Succeeded` and `Failed` bot
pods. `kubectl` shows exit code 1 as `Error` and exit code 0 as `Completed`. The container is
not restarted (`restartPolicy: Never`). Each send is its own pod, so one meeting can leave
several exited pods. The reaper removes them after the exit is stored, which lets the meetings
pool scale down. A non-zero exit copies `kubectl logs --tail=200` into the runtime log first
(clipped to 4096 characters). It reaps once at boot, after adopt, and then every 30 seconds.
Docker and process backends have no reaper.

A failed `kubectl get` is not exit code 0. Only a NotFound answer means the pod is gone. A failed
delete raises, so meeting-api does not treat a still-running pod as removed
(`k8s_backend.py`, commit `f819b40a` and the reaper on `a6c69ad7`, both in `development`).

## Earlier notes

The 1 Oct investigation is in the [archive](archive/2026-10-01-zoom-teams-recording-and-speaker-intervals.md).
Its opening line says the fixes had not started. The lane, the named-frame rule, the point
intervals, and the Meet notice on `development` are the sections above.
