# 0017 — LiveKit website meetings through AW Bots

## Context

Olamee is moving its calls from Jitsi to LiveKit with La Suite Meet as the website
(`meet-next.olamee.com`, later `meet.olamee.com`). A guest enters through Meet's waiting room; once
the host admits it, Meet returns a LiveKit token. The notetaker must record these meetings the way
it records Meet, Teams, Zoom and Jitsi: one bot, per-person audio, the same export and notes.

## Decision

Platform `livekit`, website meetings only. The bot does not drive Meet's website: Node asks Meet's
waiting room over HTTP with the configured bot name every 3 s until the host admits it, then the
bot's Chrome opens a page the bot serves itself and joins the room audio-only with `livekit-client`
2.21.0 (`@vexa/livekit-capture`, its own page bundle). The shared WebRTC hook, the mixed lane, the
per-channel recorders, the export and the worker are unchanged. A channel is a subscribed track
named from the LiveKit participant (sid and name, never the identity). Hosts come from
`VEXA_LIVEKIT_HOSTS` / `AW_BOTS_LIVEKIT_HOSTS` and are checked before the Jitsi rules; a bot is only
sent to a listed host. The recording notice is posted in Meet's chat (`lk.chat`). A disconnect the
bot did not ask for ends it as `evicted`, as on every platform (owner, 2026-11-14). While LiveKit's
exact roster holds a human the bot stays through any silence; an empty room leaves on the silence
window. A LiveKit link carries no port.

## Consequence

Driving Meet's page (it has no per-stream names) and joining without a browser (it would rewrite
the recording path in Node) were not chosen. Chat-app calls (`chat.olamee.com/meet/…`) are phase
2: only the token source changes. A Meet upgrade re-runs the knock test against its new answers.
Facts the design rests on, checked live on 2026-11-14: direct UDP from bot nodes, audio-only
subscription, the hook's element carrying the audio, receivers LiveKit pre-allocates, reason
`PARTICIPANT_REMOVED` on removal, no "end for everyone" in Meet 1.37, chat on `lk.chat`.

Meet is pinned (`lasuite/meet-backend:v1.37.0`, talke `deployment/base/ola-live/meet-values.yaml`);
the knock relies on its waiting-room timings (a waiting entry dropped after 6 s) and its throttle
counting per waiting participant. Deployed 2026-11-14 (aw-bots `48571c60`).

Decided and left as is: meeting-api is the only host check (the bot does not re-check the list);
spawn does not re-check a host removed from `VEXA_LIVEKIT_HOSTS`, so a host is removed only when
nothing on it is planned; the MCP link parser is not taught LiveKit (not on AW's path).

Moving `meet.olamee.com` from Jitsi to LiveKit: take it off `jitsiHosts` / `AW_BOTS_JITSI_HOSTS`
and add it to both LiveKit lists in the same release, the day the site moves. Listed in both at
once, the calendar module labels the link Jitsi while aw-bots reads it as LiveKit.

Phase 2, chat-app calls (after the token service): link
`https://chat.olamee.com/meet/<URL-encoded Matrix room ID>`; the token comes from the token
service's notetaker endpoint with its shared secret (a new Secret in `aw-bots`), with no waiting
room; the bot joins the call's `livekit_room` (a UUID in the `m.call.livekit.*` events), not the
Matrix room ID; names are Matrix user IDs; ChatWorks' **Add notetaker** creates an entry with
`join_now` with its own aw-bots key. Check the exporter folder name with `!` and `:` before
choosing the native id's encoding. Everything after the token is phase 1's code.

Current behavior: [recording and speakers](../recording-and-speakers.md),
[meeting lifecycle](../meeting-lifecycle.md) (LiveKit admission), [security](../security.md)
(LiveKit meeting tokens).
