# identity/contracts/webhook-subscriptions — shared test vectors

Webhook subscriptions (design §2.7) live in admin-api, and meeting-api signs and sends with them.
The two services may not import each other's code, so the two rules they must agree on are pinned
here as data that both test suites read:

- **`secret-box.vectors.json`** — how a subscription's signing secret is stored: AES-256-GCM,
  12-byte random nonce, AAD `aw-webhook-secret`, stored as `nonce ‖ ciphertext ‖ tag` with its key
  id in `enc_key_id`. The key ring comes from `WEBHOOK_SECRET_ENC_KEYS` (`{"<id>": "<32 bytes
  base64>"}`) and `WEBHOOK_SECRET_ENC_ACTIVE_KEY`. admin-api encrypts on save
  (`admin_api/app/secret_box.py`); meeting-api decrypts at signing time. `invalid_rings` are key-ring
  settings both must refuse.
- **`url-guard.vectors.json`** — which subscription URLs are accepted: admin-api checks on save
  (`admin_api/app/url_guard.py`), meeting-api on every send (`meeting_api/webhooks/ssrf.py`), with
  `WEBHOOK_PRIVATE_HOST_ALLOWLIST`.

Keys in these files are test-only counting sequences; no deployed key is ever written here.

## The routes' statuses and error codes

admin-api's `/v2/webhooks` routes (`admin_api/app/webhook_subscriptions.py`) answer:

| Route | Success |
|---|---|
| `POST /v2/webhooks` | 201, the subscription (with `secret` once, when aw-bots generated it) |
| `GET /v2/webhooks` | 200, `{"subscriptions": [...]}` |
| `PATCH /v2/webhooks/{id}` | 200, the subscription |
| `DELETE /v2/webhooks/{id}` | 204, no body |
| `POST /v2/webhooks/{id}/rotate-secret` | 200, the subscription (with `secret` once, when generated) |
| `POST /v2/webhooks/{id}/test` | 202, `{"subscription_id", "event_id"}`: the test send is queued |
| `GET /v2/webhooks/{id}/deliveries` | 200, `{"deliveries": [...], "next_before"}` |

Every failure is the §2.5 body, the sealed
[`intake.v1`](../../../meetings/contracts/intake.v1/) `Error`:

| HTTP | `code` | When |
|---|---|---|
| 400 | `invalid_request` | a field fails validation, or the `{id}` in the path is not a UUID |
| 401 | `unauthorized` | no caller identity from the gateway |
| 404 | `webhook_not_found` | the `{id}` is a UUID but not one of the account's subscriptions |
| 404 | `account_not_found` | `POST`: the account has no `users` row |
| 429 | `quota_exceeded` | `POST`: the account already has `WEBHOOK_MAX_SUBSCRIPTIONS` (no `Retry-After`) |
| 503 | `unavailable` | the database can't be reached, no secret key ring is configured, or the test send could not be handed to meeting-api |

admin-api's tests fail if a code it sends is not in the `intake.v1` `Error` enum or has no golden.

_Governed by `docs/docs/governance/architecture.mdx` (P1–P12). This folder owns one concern; its public surface is its `index`/contract; it may depend only on what the dependency-rules allow._
