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

Zoom is the only `pertrack` platform (`isPerTrackLanePlatform`). Teams stays on the mix. On Jitsi
the hints are each participant's audio level in the mix (`kind: "levels"`; bot log
`[JitsiSpeakers] level speaker → …`), so a Jitsi timeline's `speaker_intervals_source` is `levels`.
Jitsi's lane for the mixed timeline stays `mixed`. Per-speaker lines on Jitsi are extra; they
are described under [Per-speaker channels](#per-speaker-channels).

How a Jitsi level becomes a turn (`modules/jitsi-capture/src/jitsi-speakers.ts`), polled every
400 ms: the loudest participant strictly above `JITSI_SPEECH_LEVEL` (0.1) is the speaker; a level
sample older than 1 s is ignored; a new speaker, or silence, must hold for `CONFIRM_POLLS` (2)
polls before a hint is sent, so a turn starts about 0.8 s late. The bot's own id, ids missing
from the participant map, and an exact tie name nobody. The dominant-speaker flag is read only
on a poll with no fresh level sample (hint `source` `dominant`).

The exporter (`exporter/activity.py`) keeps a named frame on every lane. On the mixed lane only
the mix's own frames (no channel) are the timeline; a channel's frames stay out of it. Hints
become the timeline only when the file stored no named frame. That is the Teams path, a Zoom
file that never named a channel, and a Jitsi file from a bot before 2026-10-11 (its mixed-lane
frames carry `ch: 0`).

`build_speaker_timeline` in `exporter/attribution.py` then:

- Builds intervals from those named frames when the frames paired.
- When nothing paired and at least two speakers are named, derives intervals from the point runs.
  Each run ends at its last point plus `SPEECH_HANGOVER_MS` (default 700) and is clipped so it
  does not overlap the next speaker or the recording.
- Sets `speaker_intervals_source` to `audio`, `points`, or `levels` (point runs whose every
  hint is kind `levels`), or leaves it unset when there are no intervals.
- For Zoom and Teams with at least two named speakers, anchors the earliest point to the start
  of the recording. Meet's timeline is left as captured.

A frame counts as speech at or above `RMS_SPEECH_THRESHOLD` (default 0.026).

`notetaker-worker` reads `speaker_timeline.json` from the export bucket, including
`speaker_intervals`. It splits a transcribed segment between words at a speaker change only when
`speaker_intervals_source` is `audio` or `levels`, never for `points` (Teams' 2-second indicator
cut segments into pieces). When the export folder also has `channels/index.json`, the worker can
transcribe each channel. That choice is `CHANNEL_TRANSCRIPT_MODE` in the deployment repo.
This repo does not change that worker. The handoff of the files is
[export and webhooks](export-and-webhooks.md).

## Zoom hints in a silent room

Zoom keeps the last person in its active-speaker panel while nobody talks, and
`zoom-speakers.ts` (upstream) re-sends the lit name every ~2 s. Upstream uses that only to name
tracks. Our `speaker-activity.jsonl` records every hint, and the exporter builds the timeline from
hints when a file has no named audio frame. A silent room has no frames at all, because the bot
writes no block whose peak is at or below 0.005. Without a gate, a host alone in a no-show call
would get minutes of "speech" over a silent recording, and the export would fail its audio check
([export and webhooks](export-and-webhooks.md#the-audio-check-audio_mismatch)).

So the Zoom wiring in `capture-bridge.ts` sends a hint only while remote audio arrives: a block
above that same 0.005 floor on any track within the last 2.5 s (one heartbeat plus one
ScriptProcessor block). An end hint is sent only for a turn whose start was sent. The track namer
still gets every change, so naming is unchanged. Teams needs no gate: it re-sends a name only
while the speaking ring is on (`msteams-speakers.ts`). This wiring differs from upstream's
`capture-bridge.ts`; keep ours when syncing upstream.

Two thresholds answer two questions, and they cannot disagree:

| Value | Where | Question |
|---|---|---|
| peak 0.005 | the bot, upstream's capture gate (`aloneness.ts` calls it the single silence oracle) | Is any sound arriving? Below it the bot writes no frame, counts the room as silent and, on Zoom, sends no hint. |
| RMS 0.026 (`RMS_SPEECH_THRESHOLD`) | the exporter | Is this sound speech? |

A block below the floor is also below the speech threshold.

## What the recorder records

The recorder (`record-chunker`, started after admission) records one audio mix per bot session
(`recordingSource` in `capture-bridge.ts`):

- **Zoom, Teams, Jitsi**: the live Web Audio graph the capture lane already reads. A track that
  joins later is connected to the same destination, so it is in the recording. A constant-zero
  source keeps the mix rendering from the moment it is created, so the recording's clock starts at
  admission. Bot log: `[pertrack] recording the live mix (N track(s) connected)` (Zoom) or
  `[mixed] recording the live mix (N stream(s) connected)` (Teams, Jitsi). If the mixed lane has no
  graph, the master is not recorded and the log says `recording not started: the live mix graph is
  absent`; channel recorders still run. The mix graphs run at 16 kHz, so these masters carry no
  audio above 8 kHz. Whichever of capture and recording stops last closes the graph: stopping
  capture while the recording tap is live leaves it open, because closing it would end the
  recorder's stream and drop the open timeslice. On Jitsi the audio hook tells tracks apart by
  object, not id, because each bridge session numbers `remote-audio-N` from 1 again; after a
  reconnect the new tracks join the same mix and the same recording (bot log `[Audio Hook]
  mirrored … re-used id: a new peer connection`), and audio during the cut itself is lost.
- **Google Meet**: the page's remote audio elements.

Each chunk uploads as it is made (`VEXA_RECORDING_TIMESLICE_MS`, default 15000). A bot session has
one recording id, derived from its session id (`recordings/jsonb.py`
`session_recording_numeric_id`), so the first chunks of several media types arriving at the same
moment land under one recording. The master is assembled on read
(`GET /recordings/{id}/master`): chunks are read 8 at a time and uploaded as one object, in 8 MiB
multipart parts above 8 MiB (`recordings/README.md`). A 2-hour channel builds in about 3 s, well
inside the gateway's 30 s timeout for buffered requests.

## When the bot leaves

The bot leaves when no remote audio above the capture floor (peak 0.005) has reached it for
`BOT_ALONE_SILENCE_WINDOW_MS` (default 10 minutes, `aloneness.ts`), and ends `completed` with
reason `left_alone`; that ending is not retried. A bot whose streams are connected but deliver no
frames is treated as deaf, not alone, and does not leave on that rule.
meeting-api's own `left_alone`, for a bot whose pod vanished mid-meeting (`reconcile.py`), is a
lost bot and is retried.

## Per-speaker channels

Google Meet and Jitsi record each remote channel beside the mixed master. The mixed master
and `speaker-activity.jsonl` are written on every meeting. Teams stays the server mix. Zoom
stays `pertrack`.

The switch is `PER_CHANNEL_RECORDING_PLATFORMS` on meeting-api
(`bot_spawn/service.py` `per_channel_recording_enabled`): a comma-separated list of platform
names. Unset or empty is off. A channel is recorded only when this bot is recording at all.
Helm `meetingApi.perChannelRecordingPlatforms` sets the variable. The chart default is `""`.
Our values set `google_meet,jitsi`.

meeting-api sends `perChannelRecordingEnabled: true` on the invocation only when the platform
is listed (`bot_spawn/service.py`, `invocation.py`). Off omits the field, so a bot image that
does not know the field still accepts the invocation. The bot reads
`inv.perChannelRecordingEnabled` (`capture-bridge.ts` `startRecording`). It does not read an
environment variable of its own.

| Platform | What is recorded | What the first chunk stores |
|---|---|---|
| Google Meet | Each remote audio element `gmeet-capture` is connected to. The channel number is the index that capture assigned (`gmeet-capture.ts` `channels()`). Meet keeps a small rotating pool of those elements. | `channel_kind: gmeet`, `stream_id`. Glow names stay on the ordinary activity frames, which already carry `ch`. |
| Jitsi | A live remote stream whose id is `remote-audio-<n>` (`record-chunker/src/channel-targets.ts`). The first stream with an id gets its digits as the channel number. A later stream object under an id already used (a new bridge session numbers its streams from `remote-audio-1` again) gets the next number never used, so `stream_id` and the channel number can differ. `mixedmslabel` is not a channel. | `channel_kind: jitsi`, `stream_id`, and, when the page store has them, `participant_id` and `display_name` (`jitsiNameForStream`). |

The recorder is the same `record-chunker` tap as the master, one continuous recorder per
channel, same timeslice (`VEXA_RECORDING_TIMESLICE_MS`, default 15000 ms). The page looks for
new elements every 2 seconds. Each channel uploads as media type `ch<N>`
(`bot/src/recording.ts`). meeting-api stores that as its own media file under
`recordings/{user}/{recording}/{session}/ch<N>/` (`recordings/jsonb.py` `chunk_storage_key`)
and finalizes it with the same `finalize_master` as `audio`. A channel's final chunk (sent when
its speaker leaves, or at stop) marks only that media file final; the recording turns `completed`
on the mixed lane's final chunk, or, when the mix was not recorded, when the bot session's
terminal status is written (`recordings/jsonb.py` `finish_session_recordings`).

meeting-api checks each channel upload (`recordings/router.py`, `recordings/jsonb.py`):

- A `ch<N>` chunk without a `chunk_seq` (absent or `null`) is refused with 422, after the upload
  is authenticated.
- `stream_id`, `participant_id` and `display_name` over 200 characters are stored cut to 200. The
  bot cuts them to 200 before it sends them.
- A `channel_kind` other than `gmeet` or `jitsi` is not stored, and the upload still succeeds.
  meeting-api logs `recording_channel_kind_unknown` once per media file, naming the kind.
- The list prefix includes a trailing slash (`recordings/service.py`), so one media type is not
  concatenated into another whose name starts the same way.

A channel's recorder is released when its audio track ends, or when its stream has been missing
from the page for `VEXA_CHANNEL_GONE_MS` (default 30000 ms). Missing means that stream object: a
new stream under the same id does not keep the old channel open. The release flushes the recorder's
final chunk and frees its audio nodes. A released channel number is never reopened in that session;
a speaker who comes back on a new stream, or after a bridge session restart, records on a new
channel, and the per-chunk identity names them. At stop the bot stops every channel recorder in
parallel and waits for releases still flushing, so no channel chunk arrives after the stop returns.
A channel that opens while a stop or a release takes it leaves no recorder running
(`capture-bridge.ts` `startRecording`).

Jitsi also appends activity for those channels to `speaker-activity.jsonl`, with
`"src":"channel"` on the line. One shared `AudioContext` feeds every channel tap. A block
whose peak is at or below 0.005 is not written. The recorder itself is continuous: that gate
is only on the activity line. Meet does not add `"src":"channel"` lines. Its existing frames
already name the channel.

The bot log line when a recorder starts is `[channels] recording ch<N>`.

A Jitsi channel exists only once Jitsi forwards that participant's audio. A client sending
digital silence never gets one; a real microphone does, through its noise floor. Meet moves
speakers between its few remote audio elements, so a Meet channel is not one person; the
activity frames' glow names say who was on it when.

## Google Meet recording notice

After Meet admits the bot, join posts a chat message
(`core/meetings/modules/join/src/googlemeet/chat.ts`, `MEET_RECORDING_NOTICE`):

> Olanote by Olamee Corp is recording this meeting for transcription. See https://olamee.com/legal#privacy for details. If you'd prefer the meeting not be recorded, ask the host to remove this bot.

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
delete raises, so meeting-api does not treat a still-running pod as removed (`k8s_backend.py`).

## Known limitations

| Limit | Effect | How to spot it |
|---|---|---|
| Zoom's web-client bot protection | Zoom refused the bot on 8 of 12 Zoom meetings between 2026-10-01 and 2026-10-12. It cannot be turned off from our side; the sanctioned path is Zoom RTMS (aw-notetaker `docs/superpowers/plans/2026-10-12-zoom-authorised-join.md`). | The meeting ends `failed`, `awaiting_admission_rejected`; the portal says Zoom blocked the notetaker |
| A Zoom host alone who says two short things minutes apart | If the track namer never binds the host's track, both remarks pass the hint gate, the exporter joins them into one long run (point runs have no gap limit), and the audio check fails. Not observed. Fixing it means changing how point runs join, which changes Teams and Jitsi turns too. | Zoom `_export.json`: `audio_mismatch` with `speakers_checked` 1 |
| A Jitsi bridge session restart while the bot reports `ended` | A speaker who comes back may not get a channel | Fewer channels than speakers after a reconnect |
| A channel's empty final chunk can reach meeting-api before its last data chunk | Seen in tests and live (0.1 s apart). Whether assembly handles that order is not checked | A channel master shorter than its recorder ran |
| Google Meet reconnect | A second recorder can open for the same stream under a new channel number | Two channels with the same `stream_id` |
| Channels uploaded when the mixed master was skipped | The exporter's handling of that case is not checked | `[mixed] recording not started` in the bot log |
| A quiet Jitsi speaker | A participant whose level never goes above 0.1 gets no turn on the mixed timeline (their channel still records them) | Speech in the per-person transcript with no matching turn in the mixed one |
| Teams joins only as an anonymous guest | A tenant that sends the anonymous join to Microsoft sign-in cannot be joined; the bot ends with `join_failure` (a transient reason, so a new bot is tried) and the reason text `teams_auth_redirect` (`join/src/msteams/auth-redirect.ts`) | `teams_auth_redirect` in the meeting's `last_error` |
| Zoom through the web client only | The native Zoom SDK is proprietary and excluded by the licence gate (`join/src/zoom/README.md`), so the bot meets Zoom's web-client protection (row above) | — |
| Large Jitsi calls | At 30 participants the bot (1 CPU) averages 0.88 core and is throttled 47 % of the time, audio only; video receive is not measured | CPU throttling on the bot pod |

