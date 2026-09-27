# webhooks — outbound delivery, system + per-client (O-MTG-2)

Outbound webhook delivery behind a **`WebhookSink`** port. Derived from the parent
`services/meeting-api/meeting_api/{webhook_delivery.py, webhook_url.py, webhook_retry_worker.py,
webhooks.py}`, reimplemented clean. The wire shape is sealed in `meetings/contracts/webhook.v1`.

## What it does
- **Envelope + HMAC** (`delivery.py`) — `build_envelope` = the `{event_id, event_type, api_version,
  created_at, data}` shape; `build_headers` signs `X-Webhook-Signature: sha256=<hmac(ts.payload)>`
  with the `X-Webhook-Timestamp` it used (replay window); `verify_signature` is the symmetric
  verifier a receiver runs (recompute HMAC over `ts.payload`, constant-time compare).
- **SSRF guard** (`ssrf.py`) — `validate_webhook_url` rejects localhost / loopback / link-local
  (incl. `169.254.169.254` cloud-metadata) / private CIDRs / internal Docker hostnames / non-http
  schemes, and resolves DNS names to catch rebinding. `resolver=` is injectable for offline evals.
- **Subscription signing** (`signing.py`, §2.7) — a subscription delivery carries
  `X-Webhook-Timestamp`, exactly one `X-Webhook-Signature: sha256=<hmac(ts.body)>` and, during the
  24 h after a rotation, `X-Webhook-Signature-Previous` under the old secret. No `Authorization`.
- **Secret box** (`secret_box.py`, §2.7) — opens a subscription's stored secret at signing time:
  AES-256-GCM, 12-byte nonce, AAD `aw-webhook-secret`, `nonce ‖ ciphertext ‖ tag`, under the
  `WEBHOOK_SECRET_ENC_KEYS` / `WEBHOOK_SECRET_ENC_ACTIVE_KEY` ring. A ring that is set but wrong
  refuses to start meeting-api. Decrypt only: admin-api seals.
- **Private-host allow-list** (`ssrf.py`) — the subscription sender passes
  `WEBHOOK_PRIVATE_HOST_ALLOWLIST`; a host in it passes unresolved, at validate and at connect time.
  An IPv4-mapped IPv6 address is judged as the IPv4 address it maps. The secret box and URL rules are
  pinned by the shared vectors in `core/identity/contracts/webhook-subscriptions/`.
- **Event filter** (`delivery.py`) — `is_event_enabled`: per-client subscribers only receive the
  events in their `webhook_events` map (default: `meeting.completed`). Suppressed before any HTTP.
- **Scopes** — `WebhookSink.deliver(..., scope=)`: `per-client` applies the filter; `system`
  (billing/analytics) bypasses it.
- **Operator terminal callback** — `SystemWebhookSink` freezes one deployment-owned destination at
  boot and accepts only `meeting.completed` / `bot.failed`. In-cluster HTTP needs an explicit
  operator opt-in. It never consumes a user URL, and customer delivery retains the SSRF guard.
- **Retry** (`retry.py`) — a `RetryQueue` over a Redis list (`webhook:retry_queue`); a 5xx/429/
  transport-error enqueues; `drain_retry_queue` is one worker sweep (exponential `BACKOFF_SCHEDULE`
  = 1m·5m·30m·2h, 24h max-age). The eval drives the clock forward — no real sleeps.
- **Delivery ledger** (`ledger.py`, #841) — the per-user, queryable record of delivery outcomes.
  The lifecycle callback records each attempt's outcome (`build_delivery_record`: `event_type`,
  `event_id`, target **host only**, `outcome` ∈ #817 taxonomy `delivered|queued|suppressed|blocked|
  failed`, `status_code`, `attempt`, `created_at` — **never** the URL or secret, P14) into a per-user
  capped store, served by `GET /webhooks/deliveries` (gateway-fronted at `/user/webhook/deliveries`).
  `InMemoryDeliveryLedger` backs the app-factory/eval path; `RedisDeliveryLedger` is the prod adapter
  (`webhook:deliveries:{user_id}`, LPUSH newest-first + LTRIM to the cap). This is the user-facing
  completion of #815→#817: outcome → observable → **queryable**. Logs rotate; users need history.

The HTTP transport is **injected** (`transport(url, body, headers) -> resp`), so the eval supplies a
fake in-memory receiver — no httpx, no network, no live receiver.

## Evals
`tests/test_webhook_signing.py` · `test_webhook_delivery.py` · `test_webhook_ssrf.py` ·
`test_webhook_subscription_signing.py` (the §2.7 signing, secret box and shared vectors) ·
`test_webhook_ledger.py` (the #841 delivery-history path — a real delivery lands in
`GET /webhooks/deliveries`, host-only rows). Ride `gate:python`. `webhook.v1` goldens conform via
`gate:schema` (the contract is UNSEALED — sealing is the human `lane:contract` step).
