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

_Governed by `docs/docs/governance/architecture.mdx` (P1–P12). This folder owns one concern; its public surface is its `index`/contract; it may depend only on what the dependency-rules allow._
