# app — the admin-api FastAPI surface

`main.py` exposes `create_app()` with 3 auth tiers (admin `X-Admin-API-Key`, user `X-API-Key`,
internal `X-Internal-Secret`) and the gateway's fail-closed `/internal/validate` oracle. `db.py`
builds an INJECTABLE async engine so the same app runs against testcontainers-PG or prod.

Webhook subscriptions (§2.7): `webhook_subscriptions.py` serves `/v2/webhooks` (scope `webhooks`,
`x-user-id` from the gateway) and meeting-api's internal read of an account's active subscriptions;
`secret_box.py` encrypts each signing secret (AES-256-GCM under the `WEBHOOK_SECRET_ENC_*` key ring);
`url_guard.py` refuses private targets on save. The byte layout and the URL rules are pinned as
shared vectors in `core/identity/contracts/webhook-subscriptions/`.

_Governed by `docs/docs/governance/architecture.mdx` (P1–P12). This folder owns one concern; its public surface is its `index`/contract; it may depend only on what the dependency-rules allow._
