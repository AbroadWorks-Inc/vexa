# Data and configuration

Tables AW Bots added, and the settings whose code default and deployed value differ. Every other
setting we rely on is the table in the [README](../../README.md#configuration). Each meeting-api,
admin-api, and gateway setting is also declared in that service's `config.v1.json`.

Schema models: `core/identity/services/admin-api/src/admin_api/schema/models.py`. Postgres in the
cluster is database `vexa` on the release's Postgres pod. This page describes the model. It does
not describe a live migration snapshot.

## Tables we added

Upstream `meetings` remains the meeting row. AW state that does not belong in `meetings.data` is
a separate table.

| Table | One row is |
|---|---|
| `meeting_entries` | One invite or one `join_now`, bound to `meetings.id`. `state` is `active`, `removed`, or `closed`. |
| `meeting_aw_state` | The scheduled end, the outcome, `send_attempts`, `event_seq`, and the export result. Primary key `meeting_id`. |
| `sweep_item_failures` | Failure count and `gave_up_at` for one sweep item. |
| `webhook_subscriptions` | A receiver URL and its encrypted secret. `events` empty means all event types. |
| `webhook_outbox` | One stored event. `payload_text` is the body that is sent. |
| `webhook_deliveries` | One event for one subscription. Unique on `(event_id, subscription_id)`. |
| `webhook_delivery_attempts` | One HTTP attempt, kept after the delivery reaches a terminal state. |

`meeting_entries` is unique on `(user_id, source_user, external_id)` while `state` is not
`closed`. Closed rows are history and may repeat that key.

`meeting_aw_state.outcome_kind`, `outcome_detail`, and `outcome_message` are the typed outcome
on the webhook. `export_state`, `export_s3_path`, `export_error`, and `export_at` are what
`POST /v2/meetings/{id}/export` records.

`meetings.data` still holds bot fields the upstream row already had, including
`completion_reason`, `failure_stage`, `failure_reason`, `join_evidence`, and `bot_retry` while a
replacement is waiting (`intake/retry.py`). While it waits, the failed bot's reason, stage and
`join_evidence` are on `bot_retry`, not the row; a new bot moves them into `completion_history`.
The v2 meeting's `join_failure` is `join_evidence`'s `reason`, `attribution` and `detail` (detail
cut to 500 characters), on a `failed` meeting whose `completion_reason` is not `stopped`; otherwise
`null` (`intake/projection.py`).

## Statuses that matter to operators

| Status or reason | What it means |
|---|---|
| `scheduled` | The meeting exists and a bot has not been sent. |
| `requested`, `joining`, `awaiting_admission`, `needs_help`, `active`, `stopping` | A bot is in flight or in the call. `stopping` is not replaced. |
| `needs_help` | The bot met a blocker before it got in (Zoom's automated-bot wall, the Meet consent prompt, or one in the lobby). Pre-active: it counts against the bot limit, and a stop deletes its workload. |
| `completed` + `left_alone`, reported by the bot | The bot was in the call and hung up after the room emptied. A successful end, not replaced. |
| `left_alone` written by reconcile | The bot was in the call and its workload vanished without an end of its own: a lost bot. Replaced (`bot.retry`). |
| `completed` + `evicted` | The host removed the bot. The bot reports status `completed` with this reason. |
| `stopping`, then `completed` or `failed`, reason `stopped` | A client called stop. The bot leaves and the callback ends the meeting. A meeting that was only waiting for a replacement ends `failed` with `stopped` immediately. |
| `failed` + `awaiting_admission_rejected` | The host denied the bot, the platform refused it, or the join ended blocked. Not replaced. |
| `failed` + `awaiting_admission_timeout` | The knock was not answered. |
| `failed` + `join_failure` | The join died without a host denial and without a lobby still on screen. |
| `failed` + outcome `not_sent` | The send budget was used up, or the meeting became overdue before a bot was in the call. |

Replacement rules for these reasons are in [meeting lifecycle](meeting-lifecycle.md).

## Settings that are easy to misread

**Two lead times.** `AUTO_JOIN_LEAD_S` defaults to 120 in `bot_spawn/auto_join.py`. Helm
`meetingApi.autoJoinLeadSeconds` is 300 in our values, and that is what the process reads on
EKS. A local run that does not set the variable uses 120.

**Two retry clocks.** `BOT_SEND_RETRY_BACKOFF_S` (default 60) and `BOT_SEND_MAX_ATTEMPTS`
(default 3) bound entry-managed meetings, which is the calendar and `join_now` path.
`AUTO_JOIN_RETRY_BACKOFF_S` (default 300) applies to a scheduled meeting that has no entries.
Changing one does not change the other.

**Identity ring versus the Helm label.** The process reads `GATEWAY_IDENTITY_KEYS` and
`GATEWAY_IDENTITY_ACTIVE_KEY`. The Secret stores the active kid under
`GATEWAY_IDENTITY_KEY_LABEL`, and the Deployment template copies it into
`GATEWAY_IDENTITY_ACTIVE_KEY`. The same split exists for `WEBHOOK_SECRET_ENC_KEY_LABEL` and
`WEBHOOK_SECRET_ENC_ACTIVE_KEY`. Details are in [security](security.md#signed-identity).

**`NOTETAKER_URL`.** The exporter Deployment sets the host `notetaker-api` in namespace
`notetaker`. The runbook treats that name as the Service in front of `notetaker-worker`.

**Per-speaker recording.** `PER_CHANNEL_RECORDING_PLATFORMS` is a comma-separated list of
platform names on meeting-api. Helm `meetingApi.perChannelRecordingPlatforms` sets it.
The chart default is empty, which is off for every platform. Our values set
`google_meet,jitsi`. Whitespace around a name is ignored. A platform not in the list,
and Teams and Zoom, do not get channel files. The bot learns the decision from
`perChannelRecordingEnabled` on its invocation, and only when the value is true.
Changing the list and running `helm upgrade` changes the next bot. A bot already in a
call keeps the invocation it started with. The worker's `CHANNEL_TRANSCRIPT_MODE` is a
different setting, on the notetaker Deployment in the deployment repo, set to `deliver`: when a
meeting has channel files, the per-person transcript is the one delivered, and the mixed
recording is transcribed only when that path fails or returns nothing.

**Lobby budget.** `VEXA_LOBBY_BUDGET_S` is set to 600 in `meetingApi.extraEnv`; the code default
in `bot_spawn/service.py` is 900. The reconcile sweep's pre-active grace is derived from it
(budget plus 60 s), so raising one raises the other.

**Image tags.** `global.imageTag` is empty, so each image uses the tag in the values file or,
for the exporter, in its Deployment. There is no `:latest`.

Pins and the values file are the intended release. What is running in the cluster is read from
the cluster, not from this page.
