# livekit-capture/scripts

[`check-isolation.js`](check-isolation.js) — the brick's `gate:isolation` (P2) check.
`@vexa/livekit-capture` is page code whose only external import is the declared `livekit-client` —
never another brick's internals, never node/Playwright.
