- **aw-bots: LiveKit website meetings (`livekit`).** A link `https://<host>/<abc-defg-hij>` on a
  `VEXA_LIVEKIT_HOSTS` host (Helm `meetingApi.livekitHosts`) is a `livekit` meeting. The bot asks
  Meet's waiting room to enter, waits for the host, then joins the LiveKit room audio-only from its
  own page (`@vexa/livekit-capture`, `livekit-client` 2.21.0) and records each person on a named
  channel (`channel_kind: livekit`). It posts the recording notice in the meeting's chat and leaves
  when removed or when everyone has left. `POST /bots` sends a `livekit` bot only to a listed host.
  Decision [0017](../aw-bots/decisions/0017-livekit-meetings-through-aw-bots.md).
