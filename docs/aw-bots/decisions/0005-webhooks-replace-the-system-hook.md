# 0005 — Webhooks replace the system hook

## Context

Upstream can POST a single system webhook, and a per-user `webhook_url`, from a path that is easy
to skip if the process dies after the meeting is marked finished. The exporter and the portal
both need the same events, with their own secrets.

## Decision

Every status change is written to `webhook_outbox` in the same transaction as the change.
Subscriptions are `/v2/webhooks`. Each subscriber brings its own secret. AW sets neither
`VEXA_SYSTEM_WEBHOOK_URL` nor a per-user `webhook_url`.

The exporter subscribes to `meeting.completed` and `bot.failed` only.

## Consequence

A missed HTTP POST is retried from Postgres until the schedule ends. A 4xx other than 429 is
terminal. Receivers must dedupe on `event_id`.

Current behavior: [export and webhooks](../export-and-webhooks.md).
