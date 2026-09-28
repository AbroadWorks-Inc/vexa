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
