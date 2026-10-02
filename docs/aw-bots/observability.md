# Observability

What meeting-api counts, and what each alert in the rule file means. The rules themselves are
aw-notetaker `deployment/base/aw-bots/alerts.yml`. That file's header is the contract for how
severity is routed. Whether a given Prometheus release has loaded the file is a cluster fact and
is not recorded here.

## Where metrics are scraped

`GET /metrics` on meeting-api (port 8080) and admin-api (port 8001) is outside the gateway.
Pods carry the `prometheus.io/*` annotations from the Helm values. The text format is
`meeting_api/metrics.py` and `admin_api/app/metrics.py`.

No metric label carries a key, a secret, a URL, or transcript text. `user_id` is the account id.

Gauges that read the database at scrape time (`aw_webhook_pending`,
`aw_webhook_outbox_unpublished`, `aw_api_token_expires_seconds`, `aw_meetings_by_status`) are
emitted by every replica. Alert expressions combine those with `max` or `min`, not a sum across
pods.

## meeting-api counters and gauges

| Metric | When it moves |
|---|---|
| `aw_intake_requests_total{route,result,user_id}` | Every `/v2` request |
| `aw_intake_request_seconds` | Every `/v2` request |
| `aw_meetings_not_sent_total{detail,user_id}` | The status writer records a `not_sent` outcome |
| `aw_meetings_failed_total{reason,user_id}` | The status writer ends a meeting `failed` after a bot was sent. Once, when the transaction commits. |
| `aw_bot_retries_total{reason,user_id}` | The status writer sends the meeting back for another bot (`bot.retry`) |
| `aw_autojoin_lag_seconds` | The auto-join sweep, per bot sent: tick time minus (`scheduled_at` minus lead) |
| `aw_webhook_deliveries_total{event_type,outcome,user_id}` | The sender, per claimed delivery |
| `aw_webhook_delivery_seconds` | The sender, per POST |
| `aw_export_total{state,user_id}` | The export route, per new result |
| `aw_sweep_items_total{sweep,result}` | An item `failed`, was `given_up`, or waited `runtime_unreachable` |
| `aw_sweep_last_run_timestamp_seconds{sweep}` | A background loop finished a tick on this replica |
| `aw_meetings_by_status{status,user_id}` | Scrape-time count of non-terminal meetings |
| `aw_webhook_pending{user_id}` | Deliveries due (`pending` or `sending`, `next_attempt_at` reached) |
| `aw_webhook_outbox_unpublished` | Outbox rows with no `published_at` |

`aw_meetings_failed_total` moves only when `write_status` sets status `failed` from a live
status and the outcome is not `not_sent` (`intake/status.py`). The label is
`data.completion_reason`. A `completed` write does not move it, so `left_alone` does not. A
`bot.retry` write sets status `requested` and moves `aw_bot_retries_total` instead. The counter
gains `awaiting_admission_rejected` or `awaiting_admission_timeout` when that meeting is later
ended `failed` with that reason. A stop that ends the row `failed` with reason `stopped` moves
it too.

admin-api adds `aw_api_token_expires_seconds` and the webhook-retention sweep stamp
`aw_sweep_last_run_timestamp_seconds{sweep="webhook-retention"}`.

calendar-dispatcher, in the other repo, exposes `aw_calendar_read_total`. The alert file reads it.
That metric is not produced by this fork.

## Alerts

Severity in `alerts.yml`: `critical` is paged, `warning` is the operator route, `info` is stored
and not sent. A 10-minute counter alert goes quiet when that window no longer contains the
increment. The `webhook_deliveries` row can still say `failed` or `dead`.

| Alert | Severity | Fires when |
|---|---|---|
| `AwBotsMeetingsNotSent` | warning | More than 1% of meetings that reached a send decision in the last hour ended `not_sent` |
| `AwBotsMeetingsFailed` | warning | A sent bot's meeting ended `failed` for a reason other than the two admission reasons |
| `AwBotsMeetingNotAdmitted` | info | `awaiting_admission_rejected` or `awaiting_admission_timeout` |
| `AwBotsBotRetrying` | info | A replacement bot was requested (`aw_bot_retries_total`) |
| `AwBotsRuntimeUnreachable` | warning | A sweep could not reach the runtime |
| `AwBotsSweepItemGivenUp` | warning | A sweep item reached `gave_up_at` |
| `AwBotsWebhookDeliveryDead` | warning | A delivery used its retries (`outcome="dead"`) |
| `AwBotsWebhookDeliveryFailed` | warning | A delivery got a terminal answer (`outcome="failed"`): 4xx other than 429, a redirect, or a URL the sender refused. Not retried. |
| `AwBotsWebhookBacklog` | warning | More than 1000 deliveries due for 5 minutes |
| `AwBotsWebhookOutboxStuck` | critical | Outbox rows unpublished for 5 minutes |
| `AwBotsAutoJoinLagHigh` | warning | Auto-join lag p95 stays above 60 seconds for 10 minutes, and at least 5 bots were sent in the hour |
| `AwBotsCalendarReadsFailing` | warning | calendar-dispatcher read failures. That service is outside this fork. |
| `AwBotsApiKeyExpiring` | warning | A named API token is inside the expiry window |
| `AwBotsSweepStale` | critical | A meeting-api sweep has not finished a tick in the stale window |
| `AwBotsRetentionSweepStale` | warning | Webhook retention has not run |
| `AwBotsSweepRunCapped` | warning | A sweep stopped because it hit its batch cap |

For an exporter subscription, `AwBotsWebhookDeliveryFailed` with HTTP 401 means
`EXPORTER_WEBHOOK_SECRET` and the subscription secret differ. That meeting is not exported.
HTTP 404 from a subscriber is the same alert: the event is not retried.
