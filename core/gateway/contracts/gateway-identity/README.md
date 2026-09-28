# gateway/contracts/gateway-identity — shared test vectors

The gateway signs the identity it forwards (design §1.10, §6.9 F-E), and meeting-api and admin-api
verify it. The three services may not import each other's code, so the rule they must agree on is
pinned here as data that all three test suites read:

- **`signature.vectors.json`** — signature version **v2**:

  ```
  x-gateway-signature: t=<unix seconds>,v2=<64 lower-case hex digits>
  v2 = hex HMAC-SHA256(GATEWAY_IDENTITY_SECRET, message), message UTF-8
  message = "v2" LF <t> LF <user_id> LF <scopes> LF <limits> LF <METHOD> LF <body_sha256> LF <query> LF <path>
  ```

  - `user_id` is the exact `x-user-id` value; `METHOD` is upper-case.
  - `scopes` and `limits` are the exact `x-user-scopes` and `x-user-limits` values as forwarded;
    an absent header is an empty field. A request that carries either header more than once is
    refused.
  - `body_sha256` is the lower-case hex SHA-256 of the exact request body bytes: the bytes the
    gateway forwards, and the bytes the verifier receives before any handler reads them. An empty
    body hashes the empty string.
  - `query` is the raw query string exactly as forwarded, without the `?`, empty when there is
    none: ASGI `scope["query_string"]` on the receiving side, the query of the URL httpx sends on
    the gateway side. It is not parsed, re-ordered or re-encoded, so a request replayed with any
    other query does not verify.
  - `path` is the path the receiving service routes (ASGI `scope["path"]`: percent-decoded, no
    query string). It is the last field because it is the only one that can contain a line feed,
    so the join is unambiguous.
  - The version is in both the header label (`v2=`) and the message's first field. Verifiers
    accept only v2; a `v1=` header is refused like any malformed one.
  - A signature more than 60 s older or newer than the verifier's clock is refused.

  `vectors` pin the signer (`gateway/identity_signature.py`); `verify_cases` pin the verifiers
  (`meeting_api/identity_guard.py`, `admin_api/app/identity_guard.py`), including the changed
  scopes, limits, query and body cases and the previous key.

The secrets in the file are test-only literals; no deployed secret is ever written here.

## The verifier reads the body first

To hash the body, a verifier reads the whole request body before the route runs, then hands the
route the same bytes. It does so only once the header has parsed and is fresh, so an unsigned
request is refused without its body being read. The gateway already buffers every body it
forwards (`await request.body()`), and the one large upload, `POST /internal/recordings/upload`,
comes from the bots directly and is exempt from the signature.

## Rotating the key

The gateway always signs with `GATEWAY_IDENTITY_SECRET`. meeting-api and admin-api accept a
signature made with `GATEWAY_IDENTITY_SECRET` or, while it is set, with
`GATEWAY_IDENTITY_SECRET_PREVIOUS` (unset by default). Both comparisons are constant-time, and no
key is ever logged. Without `GATEWAY_IDENTITY_SECRET` a verifier accepts nothing, whatever the
previous key. To rotate with no downtime:

1. In meeting-api and admin-api, set `GATEWAY_IDENTITY_SECRET` to the new key and
   `GATEWAY_IDENTITY_SECRET_PREVIOUS` to the old one. Roll both services. They now accept both.
2. In the gateway, set `GATEWAY_IDENTITY_SECRET` to the new key. Roll the gateway. It now signs
   with the new key.
3. In meeting-api and admin-api, unset `GATEWAY_IDENTITY_SECRET_PREVIOUS`. Roll both services.
   The old key is no longer accepted.

_Governed by `docs/docs/governance/architecture.mdx` (P1–P12). This folder owns one concern; its public surface is its `index`/contract; it may depend only on what the dependency-rules allow._
