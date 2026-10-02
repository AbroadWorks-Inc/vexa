# 0006 — The gateway's signature is the client identity

## Context

meeting-api and admin-api used to trust an `x-user-id` header. Any pod in the cluster could set
that header and act as any account.

## Decision

The gateway signs the user it forwards. meeting-api and admin-api reject a client request whose
`x-gateway-signature` does not verify under the shared `GATEWAY_IDENTITY_KEYS` ring. The signature
version in the header is `v2`.

Bot callbacks, the runtime callback, and `/internal/*` use their own secrets. They are not client
calls.

## Consequence

Our gateway and admin-api images are required. An upstream gateway forwards `x-user-id` unsigned,
and this meeting-api refuses those requests. The ring has to be present before those processes
serve client traffic. The gateway refuses to start without a valid ring.

Current rules: [security](../security.md#signed-identity).
