# gateway/contracts/gateway-identity — shared test vectors

The gateway signs the identity it forwards (design §1.10), and meeting-api and admin-api verify it.
The three services may not import each other's code, so the rule they must agree on is pinned here
as data that all three test suites read:

- **`signature.vectors.json`** — `x-gateway-signature: t=<unix>,v1=<hex HMAC-SHA256>` over
  `"<t>.<user_id>.<METHOD>.<path>"`, keyed by `GATEWAY_IDENTITY_SECRET`. `path` is the path the
  receiving service routes (percent-decoded, no query string); `user_id` is the exact `x-user-id`
  value. A signature more than 60 s older or newer than the verifier's clock is refused.
  `vectors` pin the signer (`gateway/identity_signature.py`); `verify_cases` pin the verifiers
  (`meeting_api/identity_guard.py`, `admin_api/app/identity_guard.py`).

The secret in the file is a test-only literal; no deployed secret is ever written here.

_Governed by `docs/docs/governance/architecture.mdx` (P1–P12). This folder owns one concern; its public surface is its `index`/contract; it may depend only on what the dependency-rules allow._
