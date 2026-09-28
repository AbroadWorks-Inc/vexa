# gateway/contracts/gateway-identity — shared test vectors

The gateway signs the identity it forwards (design §1.10, §6.9 F-E), and meeting-api and admin-api
verify it. The three services may not import each other's code, so the rule they must agree on is
pinned here as data that all three test suites read.

## The signature (v2)

```
x-gateway-signature: kid=<key id>,t=<unix seconds>,v2=<64 lower-case hex digits>
v2 = hex HMAC-SHA256(key, message), message UTF-8
message = "v2" LF <kid> LF <t> LF <user_id> LF <scopes> LF <limits> LF <METHOD> LF <body_sha256> LF <query> LF <path>
```

- `kid` names the ring key the signature is made with. The HMAC key is that key's 32 raw bytes
  (the base64 decoded, not its text). The kid is in the header, so a verifier knows which key to
  use, and in the message, so it can't be relabelled.
- `user_id` is the exact `x-user-id` value; `METHOD` is upper-case.
- `scopes` and `limits` are the exact `x-user-scopes` and `x-user-limits` values as forwarded;
  an absent header is an empty field. A request that carries either header more than once is
  refused.
- `body_sha256` is the lower-case hex SHA-256 of the exact request body bytes: the bytes the
  gateway forwards, and the bytes the verifier receives before any handler reads them. An empty
  body hashes the empty string.
- `query` is the raw query string exactly as forwarded, without the `?`, empty when there is
  none: ASGI `scope["query_string"]` on the receiving side, the query of the URL httpx sends on
  the gateway side. It is not parsed, re-ordered or re-encoded.
- `path` is the path the receiving service routes (ASGI `scope["path"]`: percent-decoded, no
  query string). It is the last field because it is the only one that can contain a line feed,
  so the join is unambiguous.
- The version is in both the header label (`v2=`) and the message's first field. Only v2 is
  accepted.
- A signature more than 60 s older or newer than the verifier's clock is refused.

## The key ring

The ring has the webhook secret encryption ring's format (`WEBHOOK_SECRET_ENC_KEYS`):

- **`GATEWAY_IDENTITY_KEYS`** (gateway, meeting-api, admin-api; the same value on all three): a
  non-empty JSON object `{"<kid>": "<exactly 32 bytes, standard base64>"}`. A kid is 1-64
  characters of `A-Z a-z 0-9 . _ -`, a narrower set than the webhook ring's because it travels in
  the header.
- **`GATEWAY_IDENTITY_ACTIVE_KEY`** (gateway only): the kid the gateway signs with. It must be in
  the ring.

The gateway refuses to start without both, or with a ring that is malformed or doesn't hold the
active kid. meeting-api and admin-api refuse every client request with 401 when the ring is unset
or malformed, and their rejection log names the fault. A verifier checks a signature only under the
key its `kid` names; a kid not in its ring is refused. No message ever carries key material.

## Rotating a key

1. **Add** the new key to `GATEWAY_IDENTITY_KEYS` on all three services, keeping the old one and
   leaving `GATEWAY_IDENTITY_ACTIVE_KEY` as it is. Roll all three. Every service now accepts both
   keys, and the gateway still signs with the old one.
2. **Switch** `GATEWAY_IDENTITY_ACTIVE_KEY` on the gateway to the new kid. Roll the gateway. It now
   signs with the new key, which every verifier already holds.
3. **Later**, drop the old key from the ring on all three services. Roll all three. The old key
   is no longer accepted anywhere.

## The files

- **`signature.vectors.json`**:
  - `keys` / `active_key`: a test ring.
  - `vectors` pin the signer (`gateway/identity_signature.py`), under the active key and an older one.
  - `verify_cases` pin the verifiers (`meeting_api/identity_guard.py`, `admin_api/app/identity_guard.py`). Each case names the kids its verifier holds (`verifier_kids`). The cases cover:
    - changed scopes, limits, query and body;
    - the active key, and an older key still in the ring;
    - a key dropped from the ring, and a kid not in the ring;
    - a relabelled kid, and the header format.
  - `ring_cases` pin the ring parser in all three services.

The keys in the file are test-only literals; no deployed key is ever written here.

## The verifier reads the body first

To hash the body, a verifier reads the whole request body before the route runs, then hands the
route the same bytes. It does so only once the header has parsed, names a known kid and is fresh,
so an unsigned request is refused without its body being read. The gateway already buffers every
body it forwards (`await request.body()`), and the one large upload,
`POST /internal/recordings/upload`, comes from the bots directly and is exempt from the signature.

_Governed by `docs/docs/governance/architecture.mdx` (P1–P12). This folder owns one concern; its public surface is its `index`/contract; it may depend only on what the dependency-rules allow._
