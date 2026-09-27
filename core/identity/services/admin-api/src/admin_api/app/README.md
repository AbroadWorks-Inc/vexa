# app — the admin-api FastAPI surface

`main.py` exposes `create_app()` with 3 auth tiers (admin `X-Admin-API-Key`, user `X-API-Key`,
internal `X-Internal-Secret`) and the gateway's fail-closed `/internal/validate` oracle. `db.py`
builds an INJECTABLE async engine so the same app runs against testcontainers-PG or prod.

Webhook subscriptions (§2.7): `webhook_subscriptions.py` serves `/v2/webhooks` (scope `webhooks`,
`x-user-id` from the gateway) and meeting-api's internal read of an account's active subscriptions;
`secret_box.py` encrypts each signing secret (AES-256-GCM under the `WEBHOOK_SECRET_ENC_*` key ring);
`url_guard.py` refuses private targets on save. The byte layout and the URL rules are pinned as
shared vectors in `core/identity/contracts/webhook-subscriptions/`.
`retention.py` is the daily single-flight sweep (§1.13) that deletes final deliveries older than
`WEBHOOK_DELIVERY_RETENTION_DAYS`, then published outbox rows with no deliveries left.
`metrics.py` (§1.13) serves `GET /metrics` from its own registry: the time left on each named key
(`aw_api_token_expires_seconds{name,user_id}`, read from `api_tokens` at scrape time) and the
retention sweep's last run (`aw_sweep_last_run_timestamp_seconds{sweep}`).
`identity_guard.py` (§1.10) answers 401 to any client request whose `x-user-id` doesn't carry the
gateway's fresh `x-gateway-signature` (`GATEWAY_IDENTITY_SECRET`); `/admin/*`, `/internal/*`,
`/health*` and `/metrics` are exempt. The signing rule is pinned as shared vectors in
`core/gateway/contracts/gateway-identity/`.

_Governed by `docs/docs/governance/architecture.mdx` (P1–P12). This folder owns one concern; its public surface is its `index`/contract; it may depend only on what the dependency-rules allow._
