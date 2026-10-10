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
(`exporter/signature.py`).

Retries follow `WEBHOOK_RETRY_SCHEDULE_S`, default `60,300,1800,7200` (1 min, 5 min, 30 min,
2 h). A 5xx, 429, timeout, or connection error waits and is then `dead`. Any other non-2xx,
including a redirect, is `failed` and is not retried. HTTP 404 is `failed`.

Secrets are stored encrypted with `WEBHOOK_SECRET_ENC_KEYS`. The active kid is
`WEBHOOK_SECRET_ENC_ACTIVE_KEY`. A subscription secret is 16 to 512 characters and is not shown
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
`{platform}_{native_meeting_id}_{start UTC as YYYYMMDDTHHMMSSmmmZ}`.
Unsafe characters in the native id become `-`. The prefix is `recordings/` in bucket
`aw-chatworks-transcribe` (`EXPORT_BUCKET`, `EXPORT_PREFIX`).

One pass writes:

| Object | Role |
|---|---|
| `master.webm` | Copy of the session master |
| `audio.wav` | Transcode the worker downloads |
| `speaker_timeline.json` | From `speaker-activity.jsonl`. See [recording](recording-and-speakers.md). Channel-tap lines are not in this timeline. |
| `participants.json` | Names from that activity |
| `speaker_activity_frames.json` | Every parsed activity frame the channel transcript needs: `{t_rel, ch, name, rms, dur_ms}`, including frames with no name, streamed to disk as they are read. `[]` when no session's activity parsed. On Jitsi, a session that has `"src":"channel"` lines writes only those lines, because the mixed-lane frames reuse channel 0. |
| `channels/ch<N>.webm` | One opus file per channel number, as its recorder made it: a server-side copy of its master, or the pieces several sessions recorded joined with the silence between them. Not decoded or padded. |
| `channels/index.json` | One row per channel: `channel`, `kind` (`gmeet` or `jitsi`), `file`, `offset_s` (where the file starts on the meeting clock), `stream_id`, and for Jitsi `participant_id` and `display_name` when the recorder stored them. Written last, after every channel file. `[]` when there are no channels or the channel step failed. |
| `meeting.json` | Meeting metadata |
| `recordings.json` | Recording list |
| `live_transcript.json` | The meeting's live transcript; `{"segments": []}` when the bot had none |
| `signal/` | Only when `EXPORT_DEBUG` is true |
| `_export.json` | The exporter's own state, including `handed_off` |

Several bot sessions on one meeting are joined into that one folder, up to
`EXPORT_MAX_RECORDINGS` (the Deployment sets 50). Past that, the export fails
`too_many_recordings` and writes nothing else. Channel pieces from those sessions
share one `channels/ch<N>.webm` per channel number. The index row keeps the earliest
piece's identity.

Every channel's identity is checked and every master asked for before any channel
file is written (`exporter/job.py` `_export_channels`). A failure while reading the
recording, fetching, copying, or joining logs `channel_export_failed`, writes the
index `[]`, and does not stop the mixed export or `POST /process`; the worker then
treats the meeting as mixed. Channel files and `channels/index.json` use the `audio`
retention class (the index expires with the files it names);
`speaker_activity_frames.json` uses `metadata`. Every run writes every file above,
so a rerun leaves nothing stale; notetaker-worker removes the channel files the
index does not name.

`POST /process` goes to `NOTETAKER_URL`. The exporter Deployment sets
`http://notetaker-api.notetaker.svc.cluster.local:8080`. The runbook's prerequisite check is
that this Service points at `notetaker-worker`. The body is `meeting_id` (the UUID), `s3_path`,
`platform`, and `idempotency_key` equal to that same UUID (`exporter/notetaker.py`,
`job.py`). The worker's behavior after that call, including how it maps speaker names and
how it uses `channels/index.json`, is `deployment/base/notetaker/worker/README.md` in the
deployment repo. This fork stops at the handoff.

A meeting already marked `handed_off` is not submitted again. Transport errors and HTTP 5xx are
retried. HTTP 4xx fails the export.
