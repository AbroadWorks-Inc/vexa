# join/src/livekit — LiveKit website meetings (Meet waiting room → the bot's own LiveKit page)

The bot does not drive Meet's website. [`knock.ts`](knock.ts) asks Meet's waiting room over HTTP
(`POST /api/v1.0/rooms/<code>/request-entry/`) until the host admits it and Meet returns a LiveKit
URL and token (`parseLivekitUrl`, `knockUntilAdmitted`; the token is never logged). Every request
has a 3 s time limit, so a waiting room that never answers cannot hold the bot past Meet's 6 s
drop, the lobby budget or a withdraw. The bot's Chrome
then opens a page the bot serves itself and connects through `window.VexaLivekit`
(`@vexa/livekit-capture`, loaded from the LiveKit page bundle).
[`fixtures/`](fixtures/) holds Meet's recorded answers, the goldens for
[`knock.test.ts`](knock.test.ts). Imports `../shared/admission` and Node builtins only.
