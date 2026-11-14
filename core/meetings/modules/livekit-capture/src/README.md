# livekit-capture/src

Front door [`index.ts`](index.ts). [`livekit-session.ts`](livekit-session.ts) (`createLivekitSession`,
`countRoster` — audio-only subscription, subscribed tracks, roster, chat, disconnect reason; keeps
LiveKit's vocabulary inside), [`livekit-speakers.ts`](livekit-speakers.ts) (`createLivekitSpeakers` —
start/end hints with a heartbeat), [`page.ts`](page.ts) (bundle entry: the session on
`window.VexaLivekit`).

[`livekit-capture.test.ts`](livekit-capture.test.ts) (`npm test`) is the L2 unit against a fake room.
