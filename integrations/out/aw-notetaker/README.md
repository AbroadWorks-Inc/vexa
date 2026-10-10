# exporter — Vexa meeting → AbroadWorks notetaker hand-off

On a finished meeting that has a recording, builds the per-meeting AbroadWorks notetaker folder in
`aw-chatworks-transcribe` (audio, speaker-activity-derived speaker attribution, meeting metadata)
and hands it off to `notetaker-worker` via `POST /process`. The meeting's UUID is its id in every
file and in the hand-off. It learns that a meeting finished from its own aw-bots `/v2/webhooks`
subscription, like the portal. It reads meeting-api and reports the export result
(`POST /v2/meetings/{uuid}/export`) through the gateway with its own key (scopes `tx` + `export`),
never with `X-User-Id` or the internal secret. The living description is the
[handbook](../../../docs/aw-bots/export-and-webhooks.md). The dated designs are in the
[archive](../../../docs/aw-bots/archive/README.md).

## The trigger: a `/v2/webhooks` subscription (design §2.7, §6.9 F-X)
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
- **The meeting.** `data.meeting` is the §2.4 meeting: `id` (UUID), `upstream_id` (the integer
  the recordings and transcript reads take), `platform`, `room`, `meeting_url`, `started_at` and
  `ended_at` (the bot's actual times). Everything else the exporter needs comes from the gateway
  reads below.
- **Once per event.** Delivery is at-least-once. A queued event is recorded under
  `aw-exporter/events/<event_id>.json`; the same `event_id` again is answered 200
  (`duplicate`) and changes nothing, before and after its meeting was exported.

## Which meetings are exported (design §6.9 F-K2)
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
previous outputs to `runs/<processed_at>/` and redoes the transcript. While the meeting's previous
job is still running the worker answers 409, and the queue retries the rerun with its backoff. A meeting that has
not finished, had no bot sent, or never had its bot in the meeting is refused (`refused <id>:
<why>`, exit code 1) and nothing is queued for it. A webhook redelivered while a rerun is pending
keeps it a rerun.

## Through the gateway (design §1.9, §1.10)
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
  is `<platform>_<room>_<startUTC>`, `<startUTC>` being `started_at`. A queued meeting that is not
  the §2.4 meeting (no UUID `id` or no integer `upstream_id`, such as an item the old exporter
  queued from the system hook) is moved to `aw-exporter/failed/` on its first attempt and writes
  nothing to the export bucket.
- **Export result.** After `_export.json` records the outcome, the exporter reports it with
  `POST /v2/meetings/{uuid}/export` `{"state": "handed_off" | "failed", "s3_path": "s3://…/", "error": "…"}`
  (`error` only on `failed`): `handed_off` after `/process`, `failed` when the meeting has no audio
  or its export is quarantined. meeting-api stores it on the meeting and sends `export.handed_off`
  / `export.failed`; repeating a report changes nothing. A report that isn't accepted fails the job
  step, and the queue retries it with its normal backoff.

## Rollout (design Part 5; aw-notetaker runbook steps 11–14e)
In a window with no meeting in progress: from the step that turns the system webhook off until the
subscription exists, no webhook reaches the exporter, so a meeting that finished in between would
be exported by nobody.
1. **The system webhook goes off** with the `helm upgrade` that brings our gateway and admin-api
   (step 11): the values set no `VEXA_SYSTEM_WEBHOOK_URL`, and `VEXA_SYSTEM_WEBHOOK_SECRET` leaves
   `aw-bots-secrets` right before it (meeting-api refuses to start with only one of the two). The
   same values allow-list `aw-exporter.aw-bots.svc.cluster.local` in
   `WEBHOOK_PRIVATE_HOST_ALLOWLIST` on admin-api and meeting-api.
2. **Mint the `exporter` key** into `aw-bots-key-exporter` (step 12).
3. **Drain the queue with the old exporter** (step 13): `aw-exporter/pending/` must be empty; its
   items are the system hook's meeting, not the §2.4 one. **Don't re-enqueue items in
   `aw-exporter/failed/` from before the rollout as they are:** `notetaker-worker` would see a new
   `idempotency_key` (the UUID instead of `vexa-<n>`) for a meeting it may already have.
4. **meeting-api and the bot** (step 14a). The new exporter needs its export route and the gateway.
5. **`EXPORTER_WEBHOOK_SECRET`** (step 14b): a fresh random value in Secret `aw-exporter-secrets`,
   its only key.
6. **This exporter** (step 14c), once meeting-api is ready, with `GATEWAY_URL`, `EXPORTER_API_KEY`,
   `EXPORTER_WEBHOOK_SECRET` and `EXPORT_MAX_RECORDINGS`.
7. **The subscription** (step 14d), with the `operator` key: `POST /v2/webhooks` `{"url":
   "http://aw-exporter.aw-bots.svc.cluster.local:8080/hooks/vexa", "secret": <the same secret>,
   "events": ["meeting.completed", "bot.failed"], "description": "aw-exporter"}`, then
   `POST /v2/webhooks/{id}/test`: its delivery shows `delivered` with 200 (`ignored`).
8. **Optional** (step 14e): an S3 lifecycle rule expiring `aw-exporter/events/` (for example 30
   days; it must be over 48 h).

## Several bot sessions (design §6.9 F-K2)
A meeting can have more than one bot session: a bot fails and a new one joins the same meeting.
Each session with audio has its own recording, and the exporter makes them ONE folder, so there is
one transcript with speaker names:
- **One clock.** t=0 is the first session's recording origin (`created_at −
  RECORD_CHUNK_TIMESLICE_MS`, spec §4.3). Each later session starts at its own origin's offset from
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
  `live_transcript.json` is the meeting's transcript (`GET /transcripts/by-id/{id}`) when it has
  segments, which only a bot that ran with live transcription leaves. A recording with no audio file (the bot failed before it recorded) is skipped with
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
  the channel transcript needs goes to `speaker_activity_frames.json`. On Jitsi, a session
  with `"src":"channel"` lines writes only those. A failure logs `channel_export_failed`
  and the mixed folder and `/process` still go out. No index means the worker stays on the
  mixed path. The worker's use of these files is
  `deployment/base/notetaker/worker/README.md` in the deployment repo.

## Config (names only — see spec §4.4)
`GATEWAY_URL`, `EXPORTER_API_KEY` (the exporter's gateway key), `EXPORTER_WEBHOOK_SECRET` (the
secret of its `/v2/webhooks` subscription),
`VEXA_BUCKET`, `EXPORT_BUCKET`, `EXPORT_PREFIX`, `NOTETAKER_URL`, `EXPORT_DEBUG`,
`EXPORT_CONCURRENCY`, `EXPORT_SWEEP_SECONDS`, `EXPORT_MAX_ATTEMPTS`, `EXPORT_RETRY_BACKOFF_SECONDS`
(default 30: a failed export waits this × 2^attempts seconds before its next try),
`EXPORT_MAX_CRASHES` (default 2: runs that stopped mid-export, the pod killed or out of memory,
before the meeting is quarantined; a crash raises nothing, so it has its own budget),
`EXPORT_LEASE_SECONDS` (default 120: a running export's lease, renewed while it runs; one found
expired is a crash; a stopped worker releases its leases, so a deploy is not one),
`RMS_SPEECH_THRESHOLD`,
`SPEECH_HANGOVER_MS`, `MIN_DOMINANT_UTTERANCE_MS`, `RECORD_CHUNK_TIMESLICE_MS`,
`ACTIVITY_WAIT_SECONDS`, `EXPORT_MAX_RECORDINGS` (default 50: the meeting's recordings are read
page by page up to this many; past it the export fails, see below), `AWS_REGION`. S3 access is via
IRSA (no static keys).

## Retention tagging (see spec §3/§7)
`EXPORT_BUCKET` expires objects by the `retention-class` S3 object tag
(`exporter/retention.py`): `master.webm` -> `recording-mp4` (30 days), `audio.wav`,
`channels/ch<N>.webm`, and `EXPORT_DEBUG`'s `signal/*` copies -> `audio` (7 days), every JSON
the exporter writes (including `channels/index.json` and `speaker_activity_frames.json`) ->
`metadata` (365 days). Objects the exporter writes into `VEXA_BUCKET`
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
