# Security

How a caller is authenticated, and what erasure removes. The four consumer keys and their scopes
are the table in the [README](../../README.md#keys-and-signed-identity). Secret values live in
Kubernetes Secrets and in the operator vault. This page names keys and environments only.

## Client keys

Every client calls the gateway with `X-API-Key`. One key is one account. Scopes on the key are
the only operations that account's callers can perform:

| Scope | Used for |
|---|---|
| `bot` | `PUT /v2/entries`, `POST /v2/entries/remove`, `GET /v2/entries`, `POST /v2/meetings/{id}/stop` |
| `tx` | `GET /v2/meetings` and `GET /v2/meetings/{id}` |
| `export` | `POST /v2/meetings/{id}/export` |
| `webhooks` | Subscription CRUD, rotate, test, and the delivery list |
| `erase` | `DELETE /v2/meetings/{id}` |

`/user/*` accepts the user scopes `bot`, `tx`, and `browser`. Minting a token is
`POST /admin/users/{user_id}/tokens` on admin-api with `X-Admin-API-Key`. The secret is returned
once, on `TokenResponse.token`. The list endpoint returns metadata only.

Rotation of a consumer key: mint a new token with the same name, update that consumer's Secret,
roll its Deployment, then revoke the old token by id. admin-api metrics expose time left on each
named key (`aw_api_token_expires_seconds`).

## Signed identity

Implementation: `core/gateway/services/gateway/src/gateway/identity_signature.py`. Verifiers:
meeting-api and admin-api identity guards. The shared statement of the rule is
`core/gateway/contracts/gateway-identity/`.

The gateway resolves the API key, forwards `x-user-id` and the other `x-user-*` headers, and sets:

```
x-gateway-signature: kid=<kid>,t=<unix seconds>,v2=<hex HMAC-SHA256>
```

`kid` is `GATEWAY_IDENTITY_ACTIVE_KEY`. It is a name, 1–64 characters of `[A-Za-z0-9._-]`, and
it must be a key in `GATEWAY_IDENTITY_KEYS`. That variable is a JSON object of kid to standard
base64 of exactly 32 bytes. The HMAC key is those 32 bytes.

The signed message is version `v2`, then the kid, the timestamp, `x-user-id`, and each of
`IDENTITY_HEADERS` in order (email, scopes, limits, workspaces, webhook URL, webhook secret,
webhook events; an absent header is an empty field), then the method, the SHA-256 of the body,
the raw query, and the path.

meeting-api and admin-api look the kid up in their own copy of the ring. They return 401 when
the signature is missing, malformed, under an unknown kid, wrong, carries an identity header
twice, or is more than `GATEWAY_IDENTITY_MAX_SKEW_S` seconds from their clock (default 60).

The process environment name is `GATEWAY_IDENTITY_ACTIVE_KEY` on all three services. The Helm
Secret key is `GATEWAY_IDENTITY_KEY_LABEL`. The chart templates map that Secret key into the
environment name (`deploy/helm/charts/vexa/templates/deployment-gateway.yaml` and the matching
admin-api and meeting-api templates). Renaming the environment the process reads requires a code
change and a rebuild.

Rotation of the ring: add the new kid on gateway, meeting-api, and admin-api and roll all three;
switch `GATEWAY_IDENTITY_ACTIVE_KEY` on the gateway and roll the gateway; later drop the old kid
and roll all three. The active field holds the kid. It does not hold key bytes.

`WEBHOOK_SECRET_ENC_KEYS` and `WEBHOOK_SECRET_ENC_ACTIVE_KEY` use the same JSON shape for
webhook secrets at rest. The Secret key for the active id is `WEBHOOK_SECRET_ENC_KEY_LABEL`,
mapped to `WEBHOOK_SECRET_ENC_ACTIVE_KEY`. Drop an old id only after no subscription still uses
it. Rows re-encrypt on the next read.

## Callbacks

Bot status callbacks carry the internal secret. Each bot's runtime callback URL carries that
bot's own token. `/internal/*` between admin-api and meeting-api checks the internal secret.
Those paths are how a pod inside the cluster reports progress without presenting a user API key.

## Webhook receivers

The signature a subscriber checks is in [export and webhooks](export-and-webhooks.md#delivery).
It is a different construction from `x-gateway-signature`.

Subscription URLs are checked at save, including DNS, with a 5-second cap
(`URL_CHECK_TIMEOUT_S`). A private host is accepted only when it is on
`WEBHOOK_PRIVATE_HOST_ALLOWLIST`. The check also runs at send time. A refused address ends the
delivery `failed`.

## Erasure

`DELETE /v2/meetings/{id}` requires scope `erase` and a finished meeting. The handler in
`intake/router.py`:

1. Deletes the meeting's recording objects through the recording deleter, before any row is
   removed (`collector/app.py` `delete_completed_artifacts`). A storage error returns
   `unavailable` and leaves the rows in place so the same call can be retried.
2. Removes transcript rows and recording metadata for that meeting.
3. Removes the meeting's entries, outbox rows, and webhook delivery rows
   (`intake/reads.py` `erase`).

The meeting row itself is kept. Objects in `aw-chatworks-transcribe` are written by the exporter
into a different bucket. This delete path does not list that bucket.

The response `deleted` object counts `objects`, `entries`, `outbox`, and `deliveries`.
