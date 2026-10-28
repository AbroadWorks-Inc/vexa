# exporter — Vexa meeting → AbroadWorks notetaker hand-off

On a finished meeting that has a recording, builds the per-meeting AbroadWorks notetaker folder in
`aw-chatworks-transcribe` (audio, speaker-activity-derived speaker attribution, meeting metadata)
and hands it off to `notetaker-worker` via `POST /process`. The meeting's UUID is its id in every
file and in the hand-off. It learns that a meeting finished from its own aw-bots `/v2/webhooks`
subscription, like the portal. It reads meeting-api and reports the export result
(`POST /v2/meetings/{uuid}/export`) through the gateway with its own key (scopes `tx` + `export`),
never with `X-User-Id` or the internal secret. The system view (events, the folder layout, the
hand-off) is the [handbook](../../../docs/aw-bots/export-and-webhooks.md).

## The trigger: a `/v2/webhooks` subscription
- **Delivery.** aw-bots posts each event (webhook.v1 `MeetingEvent`) to `POST /hooks/vexa` from
  its Postgres outbox, and retries a 5xx, 429, timeout or connection error at 1 min, 5 min,
  30 min and 2 h, then gives up (`dead`, in the subscription's delivery log). Any other answer is
  final (`failed`). The exporter answers 202 when it queues a meeting, 200 for everything it
  does not act on, 401 for a bad signature, 400 for an event it can't use, 503 when it can't
  queue (aw-bots retries).
- **Signature.** `X-Webhook-Signature` (and, for 24 h after a `rotate-secret`,
  `X-Webhook-Signature-Previous`) is `sha256=<HMAC-SHA256(secret, "<X-Webhook-Timestamp>." +
  raw body)>`. A match on either header under `EXPORTER_WEBHOOK_SECRET` is accepted, so the
  exporter keeps receiving while its secret is rotated; a timestamp more than 300 s off is
  refused.
- **The meeting.** `data.meeting` is the v2 meeting object: `id` (UUID), `upstream_id` (the integer
  the recordings and transcript reads take), `platform`, `room`, `meeting_url`, `started_at` and
  `ended_at` (the bot's actual times). Everything else the exporter needs comes from the gateway
  reads below.
- **Once per event.** Delivery is at-least-once. A queued event is recorded under
  `aw-exporter/events/<event_id>.json`; the same `event_id` again is answered 200
  (`duplicate`) and changes nothing, before and after its meeting was exported.

## Which meetings are exported
The subscription carries two events the exporter acts on, and both are exported:
- **`meeting.completed`**. One with no audio recording is a failed export (`no_audio`).
  One without a `started_at` never had its bot in the meeting (for example it was stopped in the
  lobby): the exporter answers 200, exports nothing and logs the WARNING `completed_skipped
  meeting_id=<uuid> reason=no_start_time`, the line to count. It is never a 400, which a
  subscription would record as a permanent `failed` delivery.
- **`bot.failed`**: the meeting ended `failed`, for example because its bot recorded part of the call,
  failed, and could not be replaced in time. It is exported and transcribed like a completed one,
  every session into the one folder. When it has no audio recording, it is skipped with the log line
  `bot_failed_skipped … reason=no_recording`: nothing is written, `/process` isn't called, and **no
  export result is reported** (nothing was exported, and the meeting's own `bot.failed` already says
  why). A `bot.failed` without a `started_at` never had its bot in the meeting, so the exporter
  answers 200 and skips it (`reason=no_start_time`).
- **Never exported:** any other event (answered 200, `webhook.test` included), and a `not_sent`
  meeting (`outcome.kind == "not_sent"`, no bot was ever sent), whatever event carries it.
- **One export per meeting.** The queue is keyed by the meeting id and the folder's `_export.json`
  `handed_off` ends it, so repeated or crossed events (`bot.failed` twice, or `bot.failed` then
  `meeting.completed`) export once.

## Exporting a meeting again (rerun)
One command, run in the exporter pod; Kubernetes access to the pod is the authority, so the
exporter opens no endpoint for it:

    kubectl -n aw-bots exec deploy/aw-exporter -- python -m exporter.rerun <meeting-uuid> [...]

Each meeting is read from aw-bots as it is now (`GET /v2/meetings/{id}`, the `tx` scope) and queued
with `rerun` in the same durable queue, so the running worker exports it with the usual retries,
backoff and quarantine. A rerun ignores `handed_off`: every file is written again from what aw-bots
holds now, so one an earlier export could not write (a channel whose master timed out) is filled
in, and the folder goes to `notetaker-worker`'s `/process` with `"rerun": true`, which moves the
previous outputs to `runs/<processed_at>/` and redoes the transcript. A meeting notetaker-worker
is transcribing now (`GET /status/{id}` reads `processing`) is refused with `transcription in
progress for this meeting`, as is every meeting when the worker cannot say; nothing is queued or
written for it. A meeting still in the exporter's queue is refused with `export in progress for
this meeting` while its export has not finished, or `rerun already queued` when a rerun is pending
or running; the check and the queuing are one conditional S3 write (`IfNoneMatch`), so nothing is
folded into a pending item. A meeting that has not finished, had no bot sent, or never had its bot
in the meeting is refused too (`refused <id>: <why>`, exit code 1). An id that can't be read
(aw-bots or notetaker-worker unreachable, or a `/status` body that is not a JSON object) prints
`failed <id>: <why>` and the other ids go on; the exit code is 1. A webhook redelivered while a
rerun is pending keeps it a rerun. Before it writes anything the export job asks notetaker-worker
again: a meeting being transcribed by then writes no export file and the rerun ends
`rerun_refused` with `rerun refused: transcription in progress for this meeting`, added to
`_export.json` as `rerun` (`state`, `error`, `at`) beside the marker already there, whose state
stays; it is reported `failed` only over a folder that was never handed off, so aw-bots'
`handed_off` export is never turned into `failed`. A `/status` that can't answer is retried. Once
the rerun's `/process` is accepted, its `_export.json` is kept on the pending item before the
steps after it, so a retry (the report or the marker write failed) only finishes those steps and
never refuses the rerun's own transcription. A rerun out of attempts before its `/process` is
reported `failed`, never the earlier hand-off; after it, the hand-off stands.

## Through the gateway
- **Reads.** `GET /recordings`, `GET /recordings/{id}/master`, `GET /transcripts/by-id/{id}` and,
  for a rerun, `GET /v2/meetings/{id}` go to
  `GATEWAY_URL` with `X-API-Key: <EXPORTER_API_KEY>`. The key is the `exporter` key (scopes `tx` +
  `export`, Secret `aw-bots-key-exporter`). The gateway checks the scope and tells meeting-api which
  account is calling, so the exporter reads the meetings of its key's account. meeting-api refuses a
  direct call that names a user itself, so there is no direct path.
- **UUIDs.** The meeting's UUID (`data.meeting.id` in the webhook) is the id in
  `speaker_timeline.json`, `participants.json` and the `/process` `meeting_id` and
  `idempotency_key`. `_export.json` keeps the UUID (`meeting_id`) and the integer Vexa id
  (`vexa_meeting_id`, the webhook's `upstream_id`); the integer is used only to read recordings
  and the transcript and to find the speaker-activity files. Those are under
  `signal/<owner>/<vexa_meeting_id>/<session>/`, where the owner and the session are the ones the
  recording's `storage_path` (`recordings/<owner>/<recording>/<session>/…`) names. The folder name
  is `<platform>_<room>_<startUTC>`, `<startUTC>` being `started_at`. A queued item whose meeting is
  not the v2 meeting object (no UUID `id` or no integer `upstream_id`) is moved to
  `aw-exporter/failed/` on its first attempt and writes nothing to the export bucket.
- **Export result.** After `_export.json` records the outcome, the exporter reports it with
  `POST /v2/meetings/{uuid}/export` `{"state": "handed_off" | "failed", "s3_path": "s3://…/", "error": "…"}`
  (`error` only on `failed`): `handed_off` after `/process`, `failed` when the meeting has no audio,
  a speaker's audio is missing (`audio_mismatch`: counts and levels only, no speaker's name, in
  the error, the log and `_export.json`), a rerun is refused, or its export is quarantined. meeting-api stores it on the meeting and sends `export.handed_off`
  / `export.failed`; repeating a report changes nothing. A report that isn't accepted fails the job
  step, and the queue retries it with its normal backoff.

## Running and upgrading it
The Deployment, Service, ServiceAccount and Secret templates are in aw-notetaker
`deployment/base/aw-exporter/`; the install steps are the AW Bots runbook there
(`deployment/base/aw-bots/README.md`). What the exporter needs:
- **Its gateway key.** The `exporter` key (scopes `tx` + `export`) in Secret
  `aw-bots-key-exporter`, read as `EXPORTER_API_KEY`.
- **Its webhook secret.** `EXPORTER_WEBHOOK_SECRET` in Secret `aw-exporter-secrets`, its only key;
  the same value is the subscription's secret.
- **Its host allow-listed.** `aw-exporter.aw-bots.svc.cluster.local` in
  `WEBHOOK_PRIVATE_HOST_ALLOWLIST` on admin-api and meeting-api, or aw-bots refuses the URL.
- **Its subscription**, made once with the `operator` key: `POST /v2/webhooks` `{"url":
  "http://aw-exporter.aw-bots.svc.cluster.local:8080/hooks/vexa", "secret": <the same secret>,
  "events": ["meeting.completed", "bot.failed"], "description": "aw-exporter"}`, then
  `POST /v2/webhooks/{id}/test`: its delivery shows `delivered` with 200 (`ignored`). While the
  subscription is missing or paused, nothing reaches the exporter and those events are not
  delivered later; export such meetings with the rerun command.
- **Event-marker expiry.** The bucket's lifecycle file (aw-notetaker
  `deployment/aws/s3-lifecycle/aw-bots-lifecycle.json`) holds rule `exporter-event-markers-7d`,
  which expires `aw-exporter/events/` after 7 days, well over the 48 h a redelivery can come late.
  It takes effect once the operator applies the file (runbook install step 14e).

**Upgrading.** Roll the exporter after the gateway and meeting-api it calls. A rollout is safe at
any time: on SIGTERM the running exports get `EXPORT_STOP_WAIT_SECONDS` to finish, and the rest
release their leases and resume in the new pod from the durable queue.

**Rotating the webhook secret.** The commands, how to find `{id}`, and the repair when the Secret
was already applied are in aw-notetaker `deployment/base/aw-bots/README.md`, heading "Rotating the
exporter webhook secret". Order: `POST /v2/webhooks/{id}/rotate-secret` with the new secret first,
then put the same value in `aw-exporter-secrets` and restart the exporter within 24 h. For those
24 h every delivery is signed under both secrets, and the exporter accepts a match on either
header. `{id}` is the subscription whose description is `aw-exporter` (`GET /v2/webhooks` lists
it). Applying the Secret without `rotate-secret` makes this process answer 401, and aw-bots does
not retry that.

## Several bot sessions
A meeting can have more than one bot session: a bot fails and a new one joins the same meeting.
Each session with audio has its own recording, and the exporter makes them ONE folder, so there is
one transcript with speaker names:
- **One clock.** t=0 is the first session's recording origin (`created_at −
  RECORD_CHUNK_TIMESLICE_MS`). Each later session starts at its own origin's offset from
  that; one whose origin falls inside the previous session's audio follows it directly
  (`session_overlap` warning). Sessions are ordered by `created_at` (meeting-api lists newest first).
- **Audio.** Each session's master is decoded as for one session (`aresample=async=1:first_pts=0`),
  and `audio.wav` is the sessions in order with silence in each gap: its length is the end of the
  last session minus the start of the first. `master.webm` is the same join, re-encoded as one opus
  file (48 kHz mono) by ffmpeg.
- **Speakers.** Each session's `signal/<user>/<meeting>/<session>/speaker-activity.jsonl` is read
  and shifted to where its session starts, and held inside its session's span, so no speaker event
  lands in a gap. `speaker_timeline.json` merges them; `participants.json` is the union of names.
  `_export.json.speaker_activity` is the worst session's (`missing` > `invalid` > `capped` > `ok`),
  and each session's problem is logged with its `session_uid`. The export waits
  (`ActivityNotReady`) while any session's file is not uploaded yet.
- **Every recording, bounded.** `GET /recordings?meeting_id=` is read page by page (`limit`/`offset`,
  until `has_more` is false); a recording seen on two pages is listed once. More than
  `EXPORT_MAX_RECORDINGS` fails the export and exports nothing: the ERROR
  `too_many_recordings vexa_meeting_id=… max_recordings=…`, `_export.json {state:"too_many_recordings",
  error}` and the export result `failed` with `more than <n> recordings (EXPORT_MAX_RECORDINGS)`.
- **The other files.** `meeting.json` is the webhook's meeting as sent (the intake.v1 `Meeting`,
  not ours to extend), and `recordings.json` lists every recording as meeting-api returned it.
  `live_transcript.json` is the meeting's transcript (`GET /transcripts/by-id/{id}`); only a bot
  that ran with live transcription leaves segments, otherwise it is `{"segments": []}`. Every run
  writes every file listed here, empty when there is nothing, so a rerun leaves nothing stale; a
  run that ends `no_audio` or `too_many_recordings` writes `channels/index.json` `[]`,
  `speaker_activity_frames.json` `[]` and `live_transcript.json` `{"segments": []}` beside
  `_export.json`. A recording with no audio file (the bot failed before it recorded) is skipped with
  the log line `recording_skipped … reason=no_audio`. With `EXPORT_DEBUG`, each session's signal
  files go to `signal/<session_uid>/`.
- **One session** is exported from its recording alone: `master.webm` is a server-side copy,
  `audio.wav` its transcode, `signal/*` flat.
- **Per-speaker channels.** A recording that has `ch<N>` media files also produces
  `channels/ch<N>.webm`, the channel's recorder output as it was made (opus; a server-side
  copy of its master, or one channel at a time joined when several sessions recorded it), and
  `channels/index.json`, written after every channel file; each row names its `file` and
  `offset_s`, where the file starts on the meeting clock. Nothing is decoded or padded: a
  2-hour, 19-speaker meeting's channels are ~270 MB as opus, ~3.5 GB as 16 kHz wav. Every parsed activity frame
  the channel transcript needs goes to `speaker_activity_frames.json` (`[]` when no session's
  activity parsed), streamed to disk as it is read, each with its channel (none for a
  mixed-lane frame; the worker uses only frames whose channel has a file). A meeting without channels, or whose channel step failed (logged as
  `channel_export_failed`; the mixed folder and `/process` still go out), gets the index `[]`,
  and the worker stays on the mixed path. On a rerun the worker removes the channel files the
  index does not name; the exporter deletes nothing. The worker's use of these files is
  `deployment/base/notetaker/worker/README.md` in the deployment repo.

## Config (names only)
`GATEWAY_URL`, `EXPORTER_API_KEY` (the exporter's gateway key), `EXPORTER_WEBHOOK_SECRET` (the
secret of its `/v2/webhooks` subscription),
`VEXA_BUCKET`, `EXPORT_BUCKET`, `EXPORT_PREFIX`, `NOTETAKER_URL`, `EXPORT_DEBUG`,
`EXPORT_CONCURRENCY`, `EXPORT_SWEEP_SECONDS`, `EXPORT_MAX_ATTEMPTS`, `EXPORT_RETRY_BACKOFF_SECONDS`
(default 30: a failed export waits this × 2^attempts seconds before its next try),
`EXPORT_MAX_CRASHES` (default 2: runs that stopped mid-export, the pod killed or out of memory,
before the meeting is quarantined; a crash raises nothing, so it has its own budget, checked
whether or not a lease is there; one kill charges a crash to every export the pod was running, so
a meeting with a crash runs alone, with no other export beside it, and a second crash is its
own), `EXPORT_LEASE_SECONDS` (default 120: a running export's lease, renewed while it runs and
only while it is still held; one found expired is a crash; a lease left by bookkeeping that
failed after the job is released, or recognised by the worker that holds it; a stopped worker
stops and awaits its renewals, then releases its leases, so a deploy is not one),
`EXPORT_JOB_TIMEOUT_SECONDS` (default 14400: a job running longer has hung, `/healthz` answers
503 and the liveness probe restarts the pod; that meeting's lease is left to expire, so it is
charged a crash and runs alone; ffmpeg itself has a timeout sized from its input, so a hung
ffmpeg normally fails just its export first), `EXPORT_STOP_WAIT_SECONDS` (default 100: on
SIGTERM the running exports get this long to finish and record their outcome; the Deployment's
`terminationGracePeriodSeconds` is 120),
`RMS_SPEECH_THRESHOLD`,
`SPEECH_HANGOVER_MS`, `MIN_DOMINANT_UTTERANCE_MS`, `RECORD_CHUNK_TIMESLICE_MS`,
`ACTIVITY_WAIT_SECONDS`, `EXPORT_MAX_RECORDINGS` (default 50: the meeting's recordings are read
page by page up to this many; past it the export fails, see below), `AWS_REGION`. S3 access is via
IRSA (no static keys).

## Retention tagging
`EXPORT_BUCKET` expires objects by the `retention-class` S3 object tag
(`exporter/retention.py`): `master.webm` -> `recording-mp4` (30 days), `audio.wav`,
`channels/ch<N>.webm`, `channels/index.json` (it expires with the files it names), and
`EXPORT_DEBUG`'s `signal/*` copies -> `audio` (7 days), every other JSON the exporter writes
(including `speaker_activity_frames.json`) -> `metadata` (365 days). Objects the exporter writes into `VEXA_BUCKET`
(`aw-exporter/pending/`, `failed/`, `events/`) are never tagged — that bucket has its own prefix
lifecycle (`events/` markers need to outlive the 48 h a redelivery can come late; they are a few
hundred bytes each, one per exported meeting). The exporter's IAM role needs
`s3:PutObjectTagging` on `EXPORT_BUCKET`.

## Dev setup

With [`uv`](https://docs.astral.sh/uv/) (what the repo's `gate:python` runs; it installs the `dev`
dependency group by itself):

```bash
uv run pytest -q && uv run black --check . && uv run ruff check . && uv run mypy exporter
```
