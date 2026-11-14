# @vexa/livekit-capture — the bot's LiveKit session (browser)

_meetings/ · module · LiveKit room → subscribed tracks, `mixed-capture.v1` hints, roster, chat._

Runs **inside the bot's own LiveKit page** (served by the bot, never a meeting website). The join
layer gets a LiveKit token (Meet's waiting room) and calls `connect`; the session subscribes to
**audio only** and publishes nothing. Audio is not captured here: the shared WebRTC hook
([`@vexa/mixed-capture-core`](../mixed-capture-core/)) mirrors every received track into its own
`<audio>` element, and the capture bridge records the ones this session reports as subscribed.

- `createLivekitSession(room)` — the session the bundle puts on `window.VexaLivekit`: `connect`,
  `state` (connected, and the `DisconnectReason` name once disconnected), `subscribedTracks`
  (track id, participant sid, name — never the identity, which can hold an email), `onSpeaking`,
  `onRoster`, `sendChat` (a text stream on `lk.chat`, which Meet's chat reads), `disconnect`.
- `createLivekitSpeakers` — LiveKit's active speakers → start/end hints with a ~2 s heartbeat.
- `countRoster` — participants, named, and humans (names without another notetaker bot's fragment).

## Surface
`createLivekitSession` · `createLivekitSpeakers` · `countRoster` (+ types `LivekitSession`,
`LivekitSessionOptions`, `LivekitSubscribedTrack`, `LivekitRoster`, `LivekitSpeakers`,
`LivekitSpeakersOptions`, `RoomLike`). Front door: [`src/index.ts`](src/index.ts). Bundle entry:
[`src/page.ts`](src/page.ts) → the bot's `dist/livekit-page.global.js`.

## Dependencies
`livekit-client` 2.21.0 (Apache-2.0), pinned exactly. Only this page loads it.

## Verify
`pnpm --filter @vexa/livekit-capture test` — the L2 unit drives the session against a fake room
that emits livekit-client's real event names and reason numbers (no browser). Connecting to a real
room is validated live. Covered by `gate:node`, `gate:isolation`, `gate:exports`, `gate:readme`.
