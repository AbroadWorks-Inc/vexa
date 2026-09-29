# webhook.v1 — outbound delivery envelope + signed-header scheme

The **outbound webhook wire shape**: what meeting-api POSTs to a receiver's URL, and how it
authenticates the delivery. Two producers share it, and they differ on the wire:

- **Subscription deliveries** (§2.7) go to the subscribers managed with `/v2/webhooks`. The status
  writer (`meeting_api.intake.status`) stores each event in `webhook_outbox` in the same
  transaction as the change; the publisher (`intake/outbox.py`) fans it out to the account's
  subscribers; the sender (`webhooks/sender.py`) posts the stored body, signed
  (`webhooks/signing.py`). Shapes `MeetingEvent`, `TestEvent`, `SignatureHeaders`.
- **Legacy deliveries** go to the system hook and to a meeting's per-user `webhook_url`. The
  lifecycle callback builds them (`lifecycle/webhook.py` around `app.legacy_meeting_projection`)
  and `webhooks/delivery.py` posts them. Shapes `Envelope`, `SignatureHeaders`.

Every delivery of either producer is an `Envelope`.

> **SEALED** — pinned in `contracts.seal.json`; changes ride the human `lane:contract` review
> (`pnpm seal:contracts` re-pins the hash).

## Subscription deliveries (§2.7)

### Events
A subscriber receives the events it listed (`events: []` means all). Every meeting event carries
the full meeting (`data.meeting`); `webhook.test` carries none.

| Event | When |
|---|---|
| `meeting.scheduled` | a meeting was created by an entry |
| `meeting.updated` | its time, link, title or entries changed |
| `meeting.removed` | removed before its bot was sent, or merged into a live meeting (`data.merged_into`) |
| `meeting.waiting_for_room` | due, but another bot is on the link |
| `meeting.not_sent` | ended with no bot; `data.meeting.outcome` carries the typed code and message |
| `meeting.status_change` | every bot step without a typed event (`requested`, `joining`, `awaiting_admission`, `needs_help`, `stopping`) |
| `meeting.started` | the step to `active` |
| `meeting.completed` | the step to `completed` |
| `bot.failed` | the step to `failed` (`meeting.not_sent` instead when the outcome is `not_sent`) |
| `bot.retry` | a bot failed while the meeting is on and a new bot will be sent on the same meeting: the step back to `requested`; `data.change.reason` carries the failure reason and `data.meeting.bot_joins_at` when the new bot goes |
| `export.handed_off` / `export.failed` | the exporter's result (`data.meeting.export`) |
| `webhook.test` | a test send (`POST /v2/webhooks/{id}/test`) |

`recording.ready` and `transcription.ready` are in `EventType` but are not sent to subscribers.

### Envelope (`MeetingEvent`)
```json
{
  "event_id": "evt_<64 hex>",
  "event_type": "meeting.completed",
  "api_version": "2026-09-25",
  "created_at": "2026-09-29T05:12:41Z",
  "data": {
    "meeting": { "id": "5f0c2b7e-8d1a-4c3e-9b6f-2a7d1e4c8b90", "status": "completed", "sequence": 9, "...": "intake.v1 Meeting" },
    "change": { "from": "stopping", "to": "completed", "reason": "stopped", "at": "2026-09-29T05:12:41Z" }
  }
}
```
- **`data.meeting`** is the §2.4 meeting, exactly the `intake.v1` `Meeting` every `/v2` reply
  carries: `id` is the meeting's UUID, `upstream_id` its integer id for the upstream reads,
  `started_at`/`ended_at` the bot's actual times, `sequence` its event counter. There is no
  `user_id` and no raw `data` blob.
- **`data.change`** (`Change`: `from`, `to`, `reason`, `at`) is on every status change, whatever
  the event type, and on `meeting.scheduled` (`from: null`). Events that change no status
  (`meeting.updated`, `meeting.waiting_for_room`, `export.*`) carry no `change`.
- **`data.merged_into`**: only on a `meeting.removed` that merged the meeting into a live one; the
  live meeting's UUID.
- **One event per step.** A status change is exactly one event, never two: `meeting.removed` or
  `meeting.not_sent` when that is what happened, else the typed `meeting.started`,
  `meeting.completed` or `bot.failed`, else `meeting.status_change`. A receiver that follows every
  step listens to the typed events as well as `meeting.status_change`.
- **`event_id`** = `evt_` + the full sha256 hex of `<meeting id>|<event_type>|<sequence>`
  (`intake.status.derive_event_id_v2`), with the meeting's UUID and the event's `sequence`.
- **`sequence`** (`data.meeting.sequence`) rises by 1 with every event of a meeting. A receiver
  ignores an event older than one it has already applied.

### `webhook.test` (`TestEvent`)
```json
{ "event_id": "evt_test_<32 hex>", "event_type": "webhook.test", "api_version": "2026-09-25",
  "created_at": "…", "data": { "subscription_id": "<the subscription's UUID>" } }
```
Sent to that one subscriber, signed like every other delivery. It carries only
`data.subscription_id`: no meeting, no `sequence`. A receiver answers a verified test with a 2xx
and does nothing else.

### Signing (`SignatureHeaders`)
- `Content-Type: application/json`
- `X-Webhook-Timestamp: <unix seconds>`
- `X-Webhook-Signature: sha256=<hex HMAC-SHA256(secret, "<timestamp>." + raw body)>`: always
  exactly one value.
- `X-Webhook-Signature-Previous: sha256=<… under the previous secret>`: only during the 24 h after
  `rotate-secret`. A receiver accepts a match on either header.
- There is **no `Authorization` header**: the secret never crosses the wire.

A receiver verifies over the raw body bytes it received and rejects a timestamp more than 300 s
off. The body is the envelope serialised once, compactly with sorted keys, and every retry posts
the same bytes.

### Delivery
- 10 s timeout per attempt; redirects are not followed.
- A 2xx is `delivered`. A 5xx, 429, timeout or connection error is retried at 1 min, 5 min, 30 min
  and 2 h, then `dead`. Any other answer, a redirect included, is `failed`, with no retry.
- **At-least-once, not in order.** The same event may arrive more than once, and events of one
  meeting may arrive out of order: dedupe on `event_id`, order by `sequence`.
- **Retention.** Retain seen `event_id`s ≥ 48h to dedupe a late redelivery.

## Legacy deliveries (system hook, per-user `webhook_url`)
- **Envelope.** `api_version` `2026-03-01`. `data` is `{ meeting, status_change? }`. `meeting` is
  upstream's meeting block: the integer `id`, `user_id`, `native_meeting_id`, `start_time`,
  `end_time` and the cleaned `data` (meeting-api's own bookkeeping keys stripped). It is not the
  §2.4 meeting: a receiver that wants that (the UUID, `entries`, `outcome`, `sequence`) subscribes
  on `/v2/webhooks`, as the AW exporter and the portal do. Every event except
  `meeting.completed` also carries the `status_change` block (`from`, `to`, `reason`, `timestamp`,
  `transition_source`).
- **Two events per FSM advance.** One advance (e.g. → `active`) emits both `meeting.status_change`
  and the typed `meeting.started`; these are DISTINCT logical events with DISTINCT `event_id`s.
  The per-user URL receives the event types its `webhook_events` enables; the system hook receives
  `meeting.completed` and `bot.failed`.
- **`event_id`** = `evt_` + the first 32 hex of sha256 of `<connection_id>|<event_type>|<new_status>`
  (`lifecycle.webhook.derive_event_id`), the same across every (re)delivery (#519; this closes the
  #330 4×-billing class, where a per-emission `uuid4` made redeliveries look like distinct events).
- **Signing.** With a per-user `webhook_secret`: `X-Webhook-Timestamp` and `X-Webhook-Signature` as
  above, plus `Authorization: Bearer <secret>` alongside. Without one: `Content-Type` only.
- **Delivery.** At-least-once (the initial send, a retry-queue drain, a restart replay or a
  cross-replica race can re-emit it); retried at 1 min, 5 min, 30 min and 2 h, for at most 24 h
  (`retry.py`). Receivers MUST dedupe on `event_id` and retain seen ids ≥ 48h.
- **Completion carries frozen service facts.** `meeting.completed.data.meeting.service_provenance`
  states admitted/departed time, bot outcome, transcription provider/outcome, and lifecycle
  contract version. It contains no endpoint URL, token, credential, meeting title, or transcript.
  Absence means provenance is unresolved; consumers must not infer it from legacy intent flags.

**Do NOT key on the body or the signature**, for either producer. `created_at` (legacy) and the
`X-Webhook-Timestamp` (hence the signature) differ across redeliveries; only `event_id` is stable.

## Shapes (`$defs`)
- **`Envelope`** — `event_id · event_type · api_version · created_at · data`, `data` open. Every
  delivery of either producer.
- **`MeetingEvent`** — a subscription meeting event: `event_id` `^evt_[0-9a-f]{64}$`, any
  `EventType` except `webhook.test`, `api_version` `2026-09-25`, `data` exactly `meeting`
  (`intake.v1#/$defs/Meeting`, referenced by `$id`), optional `change`, optional `merged_into`.
- **`Change`** — `{from, to, reason, at}`.
- **`TestEvent`** — a subscription `webhook.test`: `event_id` `^evt_test_[0-9a-f]{32}$`, `data`
  exactly `subscription_id`.
- **`EventType`** — the delivered event vocabulary (`meeting.started · meeting.status_change ·
  meeting.completed · meeting.scheduled · meeting.updated · meeting.removed ·
  meeting.waiting_for_room · meeting.not_sent · bot.failed · bot.retry · recording.ready ·
  transcription.ready · export.handed_off · export.failed · webhook.test`). `bot.retry` is sent
  when a bot fails while its meeting is on and a new bot will be sent on the same meeting. The
  legacy per-user `webhook_url` delivery gets it too, with the `status_change` block of the failed
  bot's session; the system hook never does: it gets only `meeting.completed` and `bot.failed`, at
  the meeting's end.
- **`SignatureHeaders`** — the headers a verifier recomputes: `sha256=<hmac_sha256(secret,
  "<X-Webhook-Timestamp>." + raw_body)>`, timestamp-then-payload, bounding replay. `Authorization`
  appears on legacy deliveries only; `X-Webhook-Signature-Previous` on subscription deliveries
  only.

## Deliberately **not** in this contract
- **The secret never crosses the wire** on a subscription delivery. Only the HMAC of `ts.payload`
  does. Verification is symmetric: the receiver recomputes with its shared secret (ADR-0001 — data,
  not credentials).
- **SSRF policy and the retry mechanics are service-side**; the schedule above is what a receiver
  can expect, not a schema field. They live in `services/meeting-api/src/meeting_api/webhooks/`.

## Conformance
Goldens in [`golden/`](golden/) named `<Shape>.<case>.json`; `validate.mjs` (ajv, with
`intake.v1` registered for `MeetingEvent`'s `$ref`) validates each against its `$def` (the filename
prefix). Run by `gate:schema`. meeting-api's `tests/test_webhook_goldens.py` regenerates every
envelope golden and the subscription header goldens from the real emitters and fails when they
differ; `tests/test_intake_contract.py` re-validates every golden in Python.
