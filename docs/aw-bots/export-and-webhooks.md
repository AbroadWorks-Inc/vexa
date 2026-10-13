# Export and webhooks

How a status change becomes a signed POST, and how the exporter turns a finished meeting into the
notetaker folder. Subscription routes and payload fields are Part 2 of the
[`/v2` API reference](../../core/meetings/services/meeting-api/V2-API.md).

## Events

admin-api `EVENT_TYPES` in `admin_api/app/webhook_subscriptions.py` is the sealed
`webhook.v1` enum. The image cannot read the schema file at runtime. A test fails if the set
drifts.

`meeting.started`, `meeting.status_change`, `meeting.completed`, `meeting.scheduled`,
`meeting.updated`, `meeting.removed`, `meeting.waiting_for_room`, `meeting.not_sent`,
`bot.failed`, `bot.retry`, `recording.ready`, `transcription.ready`, `export.handed_off`,
`export.failed`, `webhook.test`.

The status writer (`meeting_api/intake/status.py`) stores the event in `webhook_outbox` in the
same transaction as the change. `payload_text` is the bytes that will be sent. `sequence` rises
by 1 per meeting (`meeting_aw_state.event_seq`). `webhook.test` is the event with no meeting id.

A publisher copies unpublished outbox rows onto each matching subscription as
`webhook_deliveries`. Senders lease rows and POST them. State moves through `pending`,
`sending`, `delivered`, `failed`, `dead`, and `cancelled`.

## Delivery

Delivery is at least once. Order is not guaranteed. Receivers dedupe on `event_id` and ignore a
`sequence` older than one they have already applied.

Signing (`meeting_api/webhooks/signing.py`):

- `X-Webhook-Timestamp`: unix seconds
- `X-Webhook-Signature`: `sha256=<HMAC-SHA256(secret, "<timestamp>." + raw body)>`
- For 24 hours after `rotate-secret`, `X-Webhook-Signature-Previous` carries the same construction
  under the old secret

The exporter accepts either signature header and rejects a timestamp more than 300 seconds off
(`exporter/signature.py`). Changing the exporter's copy of that secret is an operator step in
aw-notetaker `deployment/base/aw-bots/README.md`, heading "Rotating the exporter webhook secret".
Applying Secret `aw-exporter-secrets` does not change the subscription meeting-api signs with.

Retries follow `WEBHOOK_RETRY_SCHEDULE_S`, default `60,300,1800,7200` (1 min, 5 min, 30 min,
2 h). A 5xx, 429, timeout, or connection error waits and is then `dead`. Any other non-2xx,
including a redirect, is `failed` and is not retried. HTTP 404 is `failed`.

Secrets are stored encrypted with `WEBHOOK_SECRET_ENC_KEYS`. The active kid is
`WEBHOOK_SECRET_ENC_ACTIVE_KEY`, which the chart fills from the Secret key
`WEBHOOK_SECRET_ENC_KEY_LABEL`. A subscription secret is 16 to 512 characters and is not shown
again. Private webhook hosts must be listed in `WEBHOOK_PRIVATE_HOST_ALLOWLIST`. The hosts we
set are in the README configuration table.

Upstream's system webhook (`VEXA_SYSTEM_WEBHOOK_URL` and `VEXA_SYSTEM_WEBHOOK_SECRET`, both set
or both absent) is unset for us. Per-user `webhook_url` is also unset. Subscribers use
`/v2/webhooks`.

## What the exporter does

Code: `integrations/out/aw-notetaker/exporter/`. Its own page is the
[package README](../../integrations/out/aw-notetaker/README.md).

`POST /hooks/vexa` acts on `meeting.completed` and `bot.failed` (`_EXPORTED_EVENTS`). Any other
event, including `webhook.test`, is HTTP 200 and is not exported. A `meeting.completed` or
`bot.failed` without `started_at` is HTTP 200 and is skipped: the bot was not in the meeting.

The exporter reads recordings through the gateway with its own key (scopes `tx` and `export`).
It reports the result with `POST /v2/meetings/{uuid}/export`.

The folder name is `exporter/naming.py` `folder_name`:
`{platform}_{native_meeting_id}_{start UTC as YYYYMMDDTHHMMSSmmmZ}`. The native id (a Meet code, a
Zoom number, a Jitsi room) is in the name on purpose, so a folder can be found by meeting in S3.
Unsafe characters in the native id become `-`. The prefix is `recordings/` in bucket
`aw-chatworks-transcribe` (`EXPORT_BUCKET`, `EXPORT_PREFIX`).

One pass writes:

| Object | Role |
|---|---|
| `master.webm` | Copy of the session master |
| `audio.wav` | Transcode the worker downloads |
| `speaker_timeline.json` | From `speaker-activity.jsonl`. See [recording](recording-and-speakers.md). Channel-tap lines are not in this timeline. |
| `participants.json` | Names from that activity |
| `speaker_activity_frames.json` | Every parsed activity frame the channel transcript needs: `{t_rel, ch, name, rms, dur_ms}`, including frames with no name, streamed to disk as they are read. `[]` when no session's activity parsed. Every frame carries its channel; a mixed-lane frame has none (`"ch": null`). The worker uses only frames whose channel has a file in `channels/index.json`. Activity files from a bot before 2026-10-11 wrote mixed-lane frames as `ch: 0`; no channel file is numbered 0 on Jitsi, so the worker leaves them out. |
| `channels/ch<N>.webm` | One opus file per channel number, as its recorder made it: a server-side copy of its master, or the pieces several sessions recorded joined with the silence between them. Not decoded or padded. |
| `channels/index.json` | One row per channel: `channel`, `kind` (`gmeet` or `jitsi`), `file`, `offset_s` (where the file starts on the meeting clock), `stream_id`, and for Jitsi `participant_id` and `display_name` when the recorder stored them. Written last, after every channel file. `[]` when there are no channels or the channel step failed. |
| `meeting.json` | Meeting metadata |
| `recordings.json` | Recording list |
| `live_transcript.json` | The meeting's live transcript; `{"segments": []}` when the bot had none |
| `signal/` | Only when `EXPORT_DEBUG` is true |
| `_export.json` | The exporter's own state, including `handed_off`. An `audio_mismatch` holds counts and levels, never a speaker's name. A rerun refused because the meeting is being transcribed adds `rerun` here and leaves the state as it was |

notetaker-worker writes into the same folder: `notes.json`, `transcript.txt` (the per-person
transcript when `channels/index.json` names channels, otherwise the mixed one), `transcript_en.txt`
(the English translation of non-English turns), `summary.json`, `cost.json`, and
`_transcription.json`, the state of its current job for this folder:
`{"state": "processing" | "completed" | "failed", "error", "at"}`. When nothing was transcribed
(a silent call), the worker calls no model: `notes.json` has `segment_count` 0 and `summary` `{}`,
and there is no `summary.json`. The exporter never writes
`_transcription.json`. Its rerun check asks the worker's `GET /status/{id}` instead.

Several bot sessions on one meeting are joined into that one folder, up to
`EXPORT_MAX_RECORDINGS` (the Deployment sets 50). Channel pieces from those sessions share one
`channels/ch<N>.webm` per channel number. The index row keeps the earliest piece's identity.

Past `EXPORT_MAX_RECORDINGS` the export fails `too_many_recordings`. That run, and a run that
fails `no_audio`, write `_export.json` and, empty, the files a full export owns beside it:
`channels/index.json` `[]`, `speaker_activity_frames.json` `[]` and `live_transcript.json`
`{"segments": []}` (`exporter/job.py` `_write_empty_owned_files`).

Every channel's identity is checked and every master asked for before any channel
file is written (`exporter/job.py` `_export_channels`). A failure while reading the
recording, fetching, copying, or joining logs `channel_export_failed`, writes the
index `[]`, and does not stop the mixed export or `POST /process`; the worker then
treats the meeting as mixed. Channel files and `channels/index.json` use the `audio`
retention class (the index expires with the files it names);
`speaker_activity_frames.json` uses `metadata`. A run that exports writes every file in
the table, and a run that stops at `no_audio` or `too_many_recordings` writes the empty ones
above, so a rerun leaves no file of an earlier export behind; notetaker-worker removes the
channel files the index does not name.

The exporter deletes nothing in the export bucket, and its IAM role has no delete permission
there. Removing files from a folder is notetaker-worker's job on a rerun: it moves the previous
outputs to `runs/<processed_at>/` and removes the channel files the index does not name.

Memory stays flat at any meeting length: activity frames are streamed to disk as
they are read, the timeline's named frames wait on disk in sorted runs past 50,000,
and a speaker's level is measured in wav blocks of at most 2 MiB.

The queue (`aw-exporter/pending/`, `exporter/queue.py`) is bounded in every direction:

| Case | What happens |
|---|---|
| Two writers on one item | Every change is a conditional write (`IfMatch` on the ETag read, `IfNoneMatch` to create) carrying a `write_id`, redone from a fresh read when another writer got in first. A webhook, a rerun request and the worker never undo each other. |
| A job that raises | Retried after `EXPORT_RETRY_BACKOFF_SECONDS` × 2^attempts, up to `EXPORT_MAX_ATTEMPTS` (default 5), then quarantined in `aw-exporter/failed/`. |
| A pod killed mid-export | Each export holds a lease. An expired lease is a crash, charged to every export that pod was running. A meeting with a crash then runs alone, and at `EXPORT_MAX_CRASHES` (default 2) it is quarantined. |
| A hung job | Every ffmpeg run has a timeout sized from its input (`exporter/audio.py`). A job running past `EXPORT_JOB_TIMEOUT_SECONDS` (default 14400) makes `/healthz` fail, the liveness probe restarts the pod, and that meeting is charged a crash. |
| A deploy | On SIGTERM nothing new starts, and running exports get `EXPORT_STOP_WAIT_SECONDS` (default 100) to finish; the Deployment's grace period is 120 s. The rest release their leases, so a deploy is not a crash. |
| Bookkeeping after a job keeps failing | The meeting is held in that process with a growing backoff, and after `EXPORT_MAX_ATTEMPTS` failures in a row it is not run again until the exporter restarts. |

`python -m exporter.rerun <uuid>` exports a finished meeting again. It refuses while the meeting
is still being exported, while a rerun is already queued, and while notetaker-worker is
transcribing it (`GET /status/{id}` reads `processing`); when the worker cannot answer, nothing
is queued either. The export job asks the worker again before the rerun writes anything. A rerun whose `/process` was already
accepted finishes only the steps after it on a retry, without rewriting the folder. A refused
rerun is recorded in `_export.json`'s `rerun` block and is reported `failed` only for a folder
that was never handed off, so a `handed_off` export is never turned into `failed`. Details: the
[package README](../../integrations/out/aw-notetaker/README.md). When to run it: aw-notetaker
`deployment/base/aw-bots/README.md`, heading "Re-exporting a meeting".

`POST /process` goes to `NOTETAKER_URL`. The exporter Deployment sets
`http://notetaker-api.notetaker.svc.cluster.local:8080`. The runbook's prerequisite check is
that this Service points at `notetaker-worker`. The body is `meeting_id` (the UUID), `s3_path`,
`platform`, and `idempotency_key` equal to that same UUID (`exporter/notetaker.py`,
`job.py`). The worker's behavior after that call, including how it maps speaker names and
how it uses `channels/index.json`, is `deployment/base/notetaker/worker/README.md` in the
deployment repo. This fork stops at the handoff.

A meeting already marked `handed_off` is not submitted again. Transport errors and HTTP 5xx are
retried. HTTP 4xx fails the export.

## The audio check (`audio_mismatch`)

Before `POST /process`, the exporter checks that the recording holds the speech the speaker
timeline says happened (`exporter/job.py` `_speaker_levels`). For each speaker it measures the RMS
of `audio.wav` inside that speaker's timeline coverage: the timeline's intervals, or, when it has
none, runs of points closed at the last point plus `SPEECH_HANGOVER_MS` and clipped to the next
speaker. Only speakers with more than 10 s of coverage are checked. If any of them is below
`RMS_SPEECH_THRESHOLD` (0.026), the export fails `audio_mismatch`: `_export.json` records counts
and levels (never a name), the result is reported `failed`, and `/process` is not called. A later
re-enqueue runs the check again.

The check compares our recording with the platform's own speaker signal. Jitsi's audio levels and
Teams' speaking ring come from the platform, not from our recording, so a speaker the platform
heard and our recording missed shows up as silence. That is how a silent recording is caught. An
exporter that accepted only hints our own audio backs up would be comparing the recording with
itself, and the check would catch nothing.

Zoom is the exception. Zoom keeps the last speaker lit in its active-speaker panel while nobody
talks, and the bot re-sends that name every ~2 s, so the bot sends a Zoom hint only while remote
audio reaches it ([recording and speakers](recording-and-speakers.md#zoom-hints-in-a-silent-room)).
A silent one-person Zoom call therefore exports with no speech instead of failing this check. On
Zoom the check still catches a recording that loses audio the bot received (a broken mix or
recorder: the per-track taps that gate the hints are separate from the recorder).
