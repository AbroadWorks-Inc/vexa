# exporter — Vexa meeting → AbroadWorks notetaker hand-off

On Vexa's `meeting.completed` webhook, builds the per-meeting AbroadWorks notetaker folder in
`aw-chatworks-transcribe` (audio, speaker-activity-derived speaker attribution, meeting metadata)
and hands it off to `notetaker-worker` via `POST /process`. The meeting's UUID is its id in every
file and in the hand-off. It reads meeting-api and reports the export result
(`POST /v2/meetings/{uuid}/export`) through the gateway with its own key (scopes `tx` + `export`),
never with `X-User-Id` or the internal secret (design
[`docs/2026-09-25-meeting-intake-and-webhooks-design.md`](docs/2026-09-25-meeting-intake-and-webhooks-design.md) §1.9).

Spec: [`docs/2026-09-23-aw-rearchitecture-design.md`](docs/2026-09-23-aw-rearchitecture-design.md)
(§4), [`docs/2026-09-23-speaker-activity-design.md`](docs/2026-09-23-speaker-activity-design.md).
Plan: [`docs/2026-09-23-aw-exporter-plan.md`](docs/2026-09-23-aw-exporter-plan.md).

## Through the gateway (design §1.9, §1.10)
- **Reads.** `GET /recordings`, `GET /recordings/{id}/master` and `GET /transcripts/by-id/{id}` go to
  `GATEWAY_URL` with `X-API-Key: <EXPORTER_API_KEY>`. The key is the `exporter` key (scopes `tx` +
  `export`, Secret `aw-bots-key-exporter`). The gateway checks the scope and tells meeting-api which
  account is calling, so the exporter reads the meetings of its key's account. meeting-api refuses a
  direct call that names a user itself, so there is no direct path.
- **UUIDs.** The meeting's UUID (`data.meeting.uuid` in the webhook) is the id in
  `speaker_timeline.json`, `participants.json` and the `/process` `meeting_id` and
  `idempotency_key`. `_export.json` keeps the UUID (`meeting_id`) and the integer Vexa id
  (`vexa_meeting_id`); the integer is used only to read recordings and the transcript. The folder
  name `<platform>_<room>_<startUTC>` is unchanged. A webhook without a UUID is moved to
  `aw-exporter/failed/` on its first attempt and writes nothing to the export bucket.
- **Export result.** After `_export.json` records the outcome, the exporter reports it with
  `POST /v2/meetings/{uuid}/export` `{"state": "handed_off" | "failed", "s3_path": "s3://…/", "error": "…"}`
  (`error` only on `failed`): `handed_off` after `/process`, `failed` when the meeting has no audio
  or its export is quarantined. meeting-api stores it on the meeting and sends `export.handed_off`
  / `export.failed`; repeating a report changes nothing. A report that isn't accepted fails the job
  step, and the queue retries it with its normal backoff.

## Rollout (design Part 5)
1. **Drain the queue with the old exporter** before anything else changes: `aw-exporter/pending/`
   must be empty. Queued items from before the rollout have no meeting UUID.
2. **Deploy meeting-api and the gateway, and mint the `exporter` key into `aw-bots-key-exporter`,
   before the new exporter.** The new exporter needs `GATEWAY_URL`, `EXPORTER_API_KEY` and the new
   export route.
3. **Don't re-enqueue items in `aw-exporter/failed/` from before the rollout as they are.** They
   have no UUID, and `notetaker-worker` would see a new `idempotency_key` (the UUID instead of
   `vexa-<n>`) for a meeting it may already have.

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
- **Unchanged files.** `meeting.json` stays the webhook's meeting row as sent (a copy of
  `webhook.v1`, not ours to extend), and `recordings.json` lists every recording as meeting-api
  returned it. A recording with no audio file (the bot failed before it recorded) is skipped with
  the log line `recording_skipped … reason=no_audio`. With `EXPORT_DEBUG`, each session's signal
  files go to `signal/<session_uid>/`.
- **One session** is exported from its recording exactly as before: `master.webm` is a server-side
  copy, `audio.wav` its transcode, `signal/*` flat.

## Config (names only — see spec §4.4)
`GATEWAY_URL`, `EXPORTER_API_KEY` (the exporter's gateway key), `VEXA_WEBHOOK_SECRET`,
`VEXA_BUCKET`, `EXPORT_BUCKET`, `EXPORT_PREFIX`, `NOTETAKER_URL`, `EXPORT_DEBUG`,
`EXPORT_CONCURRENCY`, `EXPORT_SWEEP_SECONDS`, `EXPORT_MAX_ATTEMPTS`, `RMS_SPEECH_THRESHOLD`,
`SPEECH_HANGOVER_MS`, `MIN_DOMINANT_UTTERANCE_MS`, `RECORD_CHUNK_TIMESLICE_MS`,
`ACTIVITY_WAIT_SECONDS`, `AWS_REGION`. S3 access is via IRSA (no static keys).

## Retention tagging (see spec §3/§7)
`EXPORT_BUCKET` expires objects by the `retention-class` S3 object tag
(`exporter/retention.py`): `master.webm` -> `recording-mp4` (30 days), `audio.wav` and
`EXPORT_DEBUG`'s `signal/*` copies -> `audio` (7 days), every JSON the exporter writes ->
`metadata` (365 days). Objects the exporter writes into `VEXA_BUCKET`
(`aw-exporter/pending/`, `failed/`) are never tagged — that bucket has its own prefix
lifecycle. The exporter's IAM role needs `s3:PutObjectTagging` on `EXPORT_BUCKET`.

## Dev setup

With [`uv`](https://docs.astral.sh/uv/) (what the repo's `gate:python` runs; it installs the `dev`
dependency group by itself):

```bash
uv run pytest -q && uv run black --check . && uv run ruff check . && uv run mypy exporter
```
