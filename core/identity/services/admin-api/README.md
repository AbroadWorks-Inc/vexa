# admin-api — users + API tokens (Python)

The identity control plane: the `User` + `APIToken` source-of-truth and the HTTP surface that
mints, resolves, and **validates** scoped tokens. Its one job is to be the **gateway's authz
oracle** — `/internal/validate` turns a raw token into `{user_id, scopes, email, …}` so every other
service stays out of the identity business. Python because it carves the parent admin-api
(`libs/admin-models` + FastAPI) clean onto the v0.12 backing stack.

## Seams

| Direction | Neighbour | Via | What crosses |
|---|---|---|---|
| **calls** | terminal / dashboard login | `GET /admin/users/email/{email}` | resolve a returning user by email (find-or-create) |
| **calls** | terminal / dashboard login | `POST /admin/users` · `POST /admin/users/{id}/tokens` | create user · mint a scoped session token |
| **consumes** | the gateway | `POST /internal/validate` | a raw token → `{user_id, scopes, max_concurrent, email, webhook_*}` (fail-closed) |
| **calls** | bot/worker clients | `X-API-Key` on `/user/*` | user-tier self-serve (webhook config in `user.data`) |
| **calls** | the gateway (scope `webhooks`) | `/v2/webhooks…` + `x-user-id` | webhook subscriptions: add, list, change, delete, rotate the secret, test, delivery log (§2.7) |
| **consumes** | meeting-api | `GET /internal/users/{id}/webhook-subscriptions` (internal secret) | an account's active subscriptions, secrets as ciphertext + key id only |
| **outbound** | meeting-api | `POST /internal/webhooks/test` (internal secret, `MEETING_API_URL`) | queue one `webhook.test` send |
| **produces** | Postgres (backing stack) | daily retention sweep (`app/retention.py`) | deletes final webhook deliveries older than `WEBHOOK_DELIVERY_RETENTION_DAYS` (30), then published outbox rows with none left (§1.13) |
| **scraped-by** | the cluster's Prometheus | `GET /metrics` on the pod (in no gateway route table) | key expiry and the retention sweep's last run (§1.13, `app/metrics.py`) |
| **produces** | Postgres (backing stack) | SQLAlchemy `users` · `api_tokens` | the identity tables (one `Base`, FK `api_tokens.user_id → users.id`) |

## Contracts

**Owns:** [`core/identity/contracts/identity.v1`](../../contracts/identity.v1) — `ScopedToken`
(`subject`, `scopes[]` ∈ `{bot,tx,browser,webhooks,erase,export}`, `expires_at`), `AccessDecision` (default-deny verdict),
`ResourceKind`. Sealed in [`contracts.seal.json`](../../../../contracts.seal.json).
Token prefix/scope rules live in `src/admin_api/token_scope.py` (`VALID_SCOPES`, `vxa_<scope>_…`).

**Consumes:** none — this is the root of the identity domain; it produces the token others validate.

## Isolated evaluation

`tests/` are the Group-1 backing-stack evals — ephemeral testcontainers Postgres + Redis, no live
stack (`conftest.py` skips if Docker is absent). `test_stack_admin_api.py` drives the full surface;
`test_health.py` is the pure-liveness probe.

```bash
uv run pytest -q     # L3 integration (testcontainers Postgres) · L1 health
```

## Status

- ✅ delivered — `User` + `APIToken` tables (one `Base`, v0.12 carve)
- ✅ delivered — admin tier: `POST /admin/users`, `GET /admin/users/email/{email}`, `POST /admin/users/{id}/tokens`, `DELETE /admin/tokens/{id}`
- ✅ delivered — `/internal/validate` authz oracle → `{user_id, scopes, max_concurrent, email, webhook_*}`, fail-closed, expiry-rejecting, `last_used_at` bump
- ✅ delivered — scoped/multi-scope/expiring token mint (`vxa_<scope>_…`, `VALID_SCOPES`)
- 🟡 partial — user tier: `PUT /user/webhook` self-serve (other `/user/*` surfaces deferred)
- ⬜ planned — `/internal/validate` also returns the canonical `subject` (`u_<user_id>`)
- ⬜ planned — the find-or-create-user + mint-token flow backs the terminal login (Google + dev type-any-email)
