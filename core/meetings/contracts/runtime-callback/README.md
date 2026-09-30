# meetings/contracts/runtime-callback — shared test vectors

The runtime posts workload events to the `callbackUrl` meeting-api gives it, verbatim and with no
headers of its own (`core/runtime/src/runtime_kernel/callbacks.py`). meeting-api therefore protects
`POST /runtime/callback` with a per-bot token in that URL (design §1.10):

- **`token.vectors.json`** — the callback URL is `<MEETING_API_URL>/runtime/callback?t=<token>`,
  where `token` is the hex HMAC-SHA256 of `"aw-runtime-callback.<workloadId>"` keyed by
  `INTERNAL_API_SECRET`. meeting-api names the workload when it spawns the bot, and the runtime
  echoes `workloadId` in every event, so the route recomputes the token from the event body.
  Pinned by `meeting_api/callback_auth.py` and its tests.

The secret in the file is a test-only literal; no deployed secret is ever written here.

_Governed by `docs/docs/governance/architecture.mdx` (P1–P12). This folder owns one concern; its public surface is its `index`/contract; it may depend only on what the dependency-rules allow._
