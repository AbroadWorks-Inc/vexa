- **aw-bots: meeting intake API (`/v2`).** Any app sends meetings through the gateway with
  `PUT /v2/entries` and `POST /v2/entries/remove`, reads them with `GET /v2/entries` and
  `GET /v2/meetings`, stops the bot in the call with `POST /v2/meetings/{id}/stop`, and erases a
  finished meeting's aw-bots data with `DELETE /v2/meetings/{id}`. An entry is one person's invite
  or click. The same link at overlapping times is one meeting, however many people have the
  invite, and a link has at most one live bot: the next meeting on it waits until the link is
  free. Every reply and webhook names the meeting by its UUID. The contract is sealed as
  `intake.v1`. Details are in the fork's `README.md`.
- **aw-bots: one status writer and signed webhooks.** Every meeting status change goes through one
  writer, which records the event in a webhook outbox in the same transaction. Apps subscribe with
  `/v2/webhooks` (scope `webhooks`); each event is delivered to each subscriber at least once,
  signed with the subscriber's own secret, by leased senders that keep their state in Postgres
  and retry at 1 min, 5 min, 30 min and 2 h. Receivers dedupe on `event_id` and order by
  `sequence`. The new events are sealed in `webhook.v1`, which describes both deliveries: a
  subscriber gets one event per step carrying the meeting and `data.change`, signed with no
  `Authorization` header (`MeetingEvent`, `TestEvent`), while the system and per-user URLs keep
  their legacy shape.
- **aw-bots: keys and signed identity.** New scopes `webhooks`, `erase` and `export` (sealed in
  `identity.v1`), for one least-privilege key per consumer. The gateway signs the user it forwards
  (a key ring, `GATEWAY_IDENTITY_KEYS`, with the active key named by `kid`; the signature covers the query, the body and the forwarded scope and limit headers); meeting-api and admin-api refuse a client request without a valid
  signature, so every client goes through the gateway. Bot status callbacks must carry the internal
  secret, and runtime callbacks a per-bot token in their URL. The gateway limits entry writes per
  account (600 a minute, `rate_limited`).
- **aw-bots: metrics and the exporter.** meeting-api and admin-api serve Prometheus metrics on
  `/metrics` (not routed through the gateway). The exporter reads meetings through the gateway with
  its own key, names every file and `/process` call by the meeting's UUID, and reports its result
  with `POST /v2/meetings/{id}/export`. The database schema, `webhook.v1`, `intake.v1`,
  `identity.v1` and the architecture model are re-sealed.
- **aw-bots: a bot that fails mid-meeting is retried on the same meeting (F-K2).** The row holds
  its link and gets a new bot session on the same meeting once the failed pod is proven gone
  (a runtime `gone`, a confirmed delete, a terminal callback, or a 404 that has lasted
  `MEETING_UNTRACKED_GRACE_SEC`), bounded by `BOT_SEND_MAX_ATTEMPTS`/`BOT_SEND_RETRY_BACKOFF_S` and
  the meeting's planned end; past that it ends `failed`. A user's stop, a calendar removal, the
  host removing the bot, and nobody joining are never retried.
- **aw-bots: bounded work everywhere (F-K, F-I, F-D).** Sending a bot for an entry-managed meeting
  is tried a bounded number of times with a fixed gap, then the meeting ends `not_sent`
  (`BOT_SEND_MAX_ATTEMPTS`, `BOT_SEND_RETRY_BACKOFF_S`). Every intake sweep and background job reads
  its work in bounded, paged batches, and an item that keeps failing is logged, counted and dropped
  rather than retried forever (`SWEEP_BATCH_SIZE`, `SWEEP_MAX_ITEM_FAILURES`). An entry write that
  loses a database-constraint race is retried a bounded number of times inside aw-bots; still
  losing, it answers `500 internal_error`, never `503` (`INTAKE_CONFLICT_RETRIES`).
- **aw-bots: the exporter is a `/v2` webhook subscriber (F-X).** It moves off upstream's
  Redis-backed system webhook onto its own `/v2/webhooks` subscription (Postgres delivery, bounded
  retries), and the system webhook is switched off for AW. The meeting gains its actual
  `started_at`/`ended_at`, and the legacy second meeting projection is gone.
- **aw-bots: the identity key ring and signature v2 (F-E); webhook sender settings
  (F-A/C/H).** The gateway's signature over a forwarded request now also covers the query string,
  a body hash and the forwarded scope/limit headers, signed with an active key from a rotatable key
  ring (`GATEWAY_IDENTITY_KEYS`, `GATEWAY_IDENTITY_ACTIVE_KEY`) instead of one static secret. The
  webhook sender's retry schedule, DNS pool size and per-lookup timeout are settings
  (`WEBHOOK_RETRY_SCHEDULE_S`, `WEBHOOK_DNS_THREADS`, `WEBHOOK_DNS_TIMEOUT_S`) instead of constants,
  and it always signs with the subscription's current secret, never a stale cached one.
- **aw-bots: one finish path (F-FIN); the cleanup wave (F-L).** Every way a meeting ends — a stop,
  a calendar removal, a normal end, a failure — runs the same finish steps: transcript finalize,
  provenance, the per-user webhook, flows, and copilot reap. A redundancy audit and cleanup wave
  removed in-service duplicates (the paged-sweep loop, key-ring parsers, the gone-proof check, the
  finished-meeting lock and others) and brought code, contracts and docs into agreement.
