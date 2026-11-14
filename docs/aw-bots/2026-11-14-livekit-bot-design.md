# LiveKit meetings through AW Bots: design

Status: draft for the owner's approval, 2026-11-14. Branch `feat/livekit-bot` (from `development`).
When the work is live, the lasting parts move into decision 0017,
[recording and speakers](recording-and-speakers.md) and the runbook, and this file is deleted.

## 1. What and why

Olamee is moving its calls from Jitsi to LiveKit (`talke/deployment/base/ola-live`,
`talke/deployment/docs/livekit-deployment.md`). The notetaker must record LiveKit calls the way it
records Google Meet, Teams, Zoom and Jitsi: one AW Bots bot per meeting, per-person audio, the same
export, transcript and notes.

There are two kinds of LiveKit call (trial plan decisions 11–13):

| Kind | Link | How a guest gets in | Available |
|---|---|---|---|
| Website meeting | `https://meet-next.olamee.com/<abc-defg-hij>` (later `meet.olamee.com`) | Meet's waiting room: knock, the host admits, Meet returns a LiveKit token | Now |
| Chat-app call | `https://chat.olamee.com/meet/<Matrix room ID>` | Token service notetaker endpoint, no waiting room (`livekit-deployment.md` §4.3) | After the token service (trial plan C2) |

**Phase 1 is website meetings.** Phase 2 (chat-app calls) is sketched in §9 so phase 1 does not
block it.

Goals: the bot joins, waits to be admitted, records each person on their own channel with their
name, leaves when the meeting ends or it is removed, and the meeting appears in the portal like any
other.

Non-goals: Element calls (a DM answered in Element is peer-to-peer WebRTC; no server holds the
media, so no bot can join it), LiveKit Egress, LiveKit Agents, live captions, encrypted calls.

Anti-goals: a bot that skips the host's waiting room on website meetings (owner decision 12: the
bot waits like a guest); a second recording path beside AW Bots ([0009](decisions/0009-jitsi-records-through-aw-bots.md)).

## 2. Approaches considered

| | How it joins | Verdict |
|---|---|---|
| A. Drive Meet's website like the Jitsi bot | Playwright works Meet's pre-join page and waiting room | **Rejected.** Cannot join chat-app calls (no web page). Meet's page has no global with participant names per audio stream. Every Meet upgrade can move the page |
| **B. Token over HTTP, then the bot's own LiveKit page** | Node gets a LiveKit token (Meet waiting room now, token service later); the bot's Chrome opens a small page that connects with `livekit-client` and subscribes to audio only | **Chosen.** Reuses the whole recording path (WebRTC hook, per-channel recorders, upload, exporter, worker). Names come from the SDK. Does not depend on Meet's page |
| C. `@livekit/rtc-node`, no browser | The bot process joins with LiveKit's Node SDK | Not now. Lowest CPU, but the recording, upload and speaker-activity code lives in the page and would be rewritten in Node. Revisit only if measured CPU on large calls requires it |

## 3. Facts the design relies on (checked 2026-11-14)

| Fact | Source |
|---|---|
| Join time on our LiveKit, 2026-11-12 call `qya-qawv-fyw`: 19 joins, median 1.33 s from `starting RTC session` to `participant active`, all direct UDP, mostly India | LiveKit logs in Loki |
| Request entry: `POST /api/v1.0/rooms/<code or id>/request-entry/` `{"username"}`, then the same with `participant_id` (the signed `id` from the first answer). Answer: participant dict with `status` `waiting`/`accepted`/`denied`, `id`, and `livekit` (`url`, `room`, `token`) once accepted | Meet v1.37.0 `core/api/viewsets.py:585`, `core/services/lobby.py:160–229` |
| A waiting entry is dropped after 6 s without a call; a denial is remembered 5 s; an acceptance 6 h (`LOBBY_WAITING_TIMEOUT`, `LOBBY_DENIED_TIMEOUT`, `LOBBY_ACCEPTED_TIMEOUT`) | Meet settings in the running pod |
| The waiting-room throttle is 150/minute **per waiting participant**, not per address. Any number of bots behind one NAT is fine | Meet `core/api/throttling.py:76` (`RequestEntryAnonRateThrottle.get_cache_key`) |
| meet-backend is reachable in-cluster (`http://meet-backend.ola-live.svc.cluster.local`, headers `Host: meet-next.olamee.com`, `X-Forwarded-Proto: https`): 200 from the meeting-api pod. Not used (the public URL is enough), kept as a fallback | Tested 2026-11-14 |
| The token Meet returns for an admitted guest: identity = the lobby participant id (UUID), name = the username sent, room = the Meet room's UUID, `canPublish`/`canSubscribe` true | Meet `core/utils.py` `generate_livekit_config`; live join log of 2026-11-12 |
| `livekit-client` 2.21.0 (Apache-2.0) labels a received stream `<participant sid>\|<stream or track id>` | `livekit-client/src/room/Room.ts:1619`, `utils.ts:34` |
| Meet is pinned: backend image `lasuite/meet-backend:v1.37.0`, chart 0.0.29, frontend is our own build. An upgrade is our own change in talke | `talke/deployment/base/ola-live/meet-values.yaml` |
| Meet 1.37 rooms have no end-to-end encryption: no backend setting; the frontend only displays `room.isE2EEEnabled` in its devtools | Meet settings in the running pod; `meet-frontend/src/features/devtools/roomConfig.ts:39` |
| The bot's display name is configured, not fixed: `defaultBotName: "Olanote"`, overridden by the service account's bot name | `aw-notetaker/deployment/base/aw-bots/values.yaml:111` |
| `meet.olamee.com` is listed as a **Jitsi** host today | `aw-notetaker/deployment/base/aw-bots/values.yaml:136`, `deployment/base/calendar-dispatcher/deployment.yaml:81` |
| A pasted link whose host has a `meet` label is read as Jitsi | `meeting_link.py:150` |

### 3.1 Step 0, live check (2026-11-14)

Room `ypr-ymtf-fmk` on `meet-next`, throwaway pod with bot image `1d6f1306` on the `aw-bots-meetings`
pool, a test page with `livekit-client` 2.21.0 and the bot's own init scripts (WebRTC audio hook).
One speaker (the host). Pod deleted afterwards.

| Check | Result |
|---|---|
| Knock | `waiting` twice, then `accepted` with `livekit.url` `https://livekit.olamee.com`, `livekit.room` = room UUID; an unknown room answers 404 `{detail}` |
| UDP from bot nodes | Selected pair local `host`/`udp` → remote `prflx`/`udp`, port 7882. No TURN |
| Audio only | `audio/microphone` subscribed, `video/camera` not; inbound-rtp video 0 |
| WebRTC hook | Sees livekit-client's peer connection (1) |
| Audio into Web Audio | The hook's own `<audio>` element carries the track: RMS 0.018–0.053 while speaking, with or without the page attaching its own element |
| Removed by host | `Disconnected` reason 4 `PARTICIPANT_REMOVED` |
| Room ended | Not reachable: Meet 1.37 has no "end for everyone", only Leave. When the host left, the bot stayed `connected` with an empty roster |
| Chat | A text stream on topic `lk.chat` from the bot appeared in Meet's chat; the host's reply arrived on `lk.chat` (and as a legacy packet on `lk-chat-topic`) |
| Server | LiveKit 1.13.11, `isE2EEEnabled` false |

What it changed in this design:

- **A.** LiveKit pre-allocates receivers: at connect the hook mirrored 3 audio tracks that belonged to
  nobody, and the host's microphone later arrived on one of them (same track id). A channel is
  therefore chosen from livekit-client's subscribed tracks, matched to the hook's element by track
  id, never from the page's elements alone (§4.3, §4.4).
- **B.** The page does not attach elements: the hook's element is enough (§4.3).
- **C.** A meeting ends when everyone leaves; the roster count drives the leave. `ROOM_DELETED` is
  still handled as an end, but nothing relies on it (§4.5).
- **D.** A signed-in person's identity is `f:<uuid>:<email>`. The identity is never sent: the
  channel's `participant_id` is the LiveKit participant sid (`PA_…`), its name `participant.name`
  (§4.4).
- Not checked live: two people speaking, denial, 429, the 10-minute timeout, reconnects, CPU. The
  first real-call test after the initial build covers them (§8).

## 4. Design

### 4.1 Platform and meeting identity

- New platform value **`livekit`** everywhere a platform is listed (§6).
- Website meeting native id = the room code, lower-case (`abc-defg-hij`). One Meet deployment
  serves all configured hosts, and the code survives the `meet-next` → `meet` move, so the host is
  not part of the id (unlike Jitsi's `room@host`).
- Hosts come from configuration: meeting-api `VEXA_LIVEKIT_HOSTS` (Helm `meetingApi.livekitHosts`),
  calendar module `AW_BOTS_LIVEKIT_HOSTS`. Phase 1 value: `meet-next.olamee.com`.
- `meeting_link.parse_meeting_url` checks LiveKit hosts **before** the Jitsi rules, in every mode
  (pasted and calendar scan). Path must be a single segment matching `^[a-z]{3}-[a-z]{4}-[a-z]{3}$`;
  anything else on a LiveKit host returns `None`.
- The raw URL rides along as `meeting_url`, as for Jitsi; the bot needs its host.

### 4.2 Join (new `join/src/livekit/`)

1. **Knock** (Node, no page yet): `POST https://<host>/api/v1.0/rooms/<code>/request-entry/`
   with `{"username": <the invocation's botName>}` (the configured name; it is what the host sees in the waiting room). Report `awaiting_admission` on the first `waiting` answer.
2. **Hold the place**: repeat with `participant_id` every 3 s (under the 6 s drop, ≈20/min, well
   under 150/min).
3. **Outcomes**:
   - `accepted` with `livekit` → continue to 4.
   - `denied` → `AdmissionError('denial')` → `awaiting_admission_rejected`, not retried.
   - Waiting longer than the lobby budget (600 s, [0012](decisions/0012-lobby-wait-ten-minutes.md)) → `AdmissionError('lobby_timeout')`.
   - 404 (no such room) → `AdmissionError('join_failure')` with the status in the message.
   - 429 → back off and continue; network errors → retry with backoff inside the budget.
4. **Connect**: open the bot page (4.3) and `room.connect(livekit.url, livekit.token, { autoSubscribe: false })`.
   Admission = the room reaches `connected`. Report `active`.
   If a remote participant publishes end-to-end encrypted media (a publication's `isEncrypted`;
   impossible on Meet 1.37, possible if Meet or the apps add encryption later), leave at once with
   `join_failure` reason `encrypted room`, and never subscribe to an encrypted track that appears
   later, so the bot never records encrypted noise as if it were a meeting. (`room.isE2EEEnabled`
   only describes the bot's own encryption.)
5. **Withdraw** before admission: stop knocking (the entry expires in 6 s). After connect:
   `room.disconnect()`.

The token is never logged, never written to a file and never sent anywhere but LiveKit.

### 4.3 The bot's page (new `modules/livekit-capture/`)

- A small HTML page served by the bot itself on a fixed HTTPS origin through Playwright request
  interception (`page.route`), so it is a secure context and no external page is loaded.
  `livekit-client` is bundled into the page-side bundle (`build-browser-utils.mjs`), pinned.
- The existing init scripts (WebRTC audio hook, `VexaBrowserUtils`) are installed as on every
  platform, before navigation.
- Subscribe to **audio tracks only** (`TrackPublished` / existing publications of kind audio →
  `setSubscribed(true)`). No video is received, so CPU per person is below Jitsi's
  ([0015](decisions/0015-jitsi-channels-have-no-cap.md)).
- The page attaches no elements: the WebRTC hook already mirrors every received audio track into
  its own `<audio>` element, and that element delivers the audio to Web Audio (step 0, B).
- The hook also mirrors receivers LiveKit pre-allocates, before they carry anyone (step 0, A). The
  page keeps the subscribed audio tracks (track id → participant sid and name); the channel scan
  takes only the hook elements whose track is in that list.
- Exposes to the capture bridge: the subscribed audio tracks with their participant (sid, name,
  whether another bot), the remote roster, active-speaker changes, and connection events
  (`Disconnected` with its reason, `Reconnecting`, `Reconnected`). Never the identity (step 0, D).
- The bot publishes nothing (no microphone, no camera).

### 4.4 Recording and speakers

- **Per-channel recording**, like Jitsi: one channel per remote audio track, beside the mixed
  master. `perChannelRecordingPlatforms` gains `livekit`. `channel_kind: 'livekit'`.
- Channel selection and numbering: new `selectLivekitChannels` / `createLivekitChannelNumbers` in
  `record-chunker/src/channel-targets.ts`. A channel is one track object; a number is never reused
  in a session (same rule as Jitsi).
- **Names come from the SDK** (`participant.name`; no fallback to the identity, which can hold an
  email), sent with every chunk as Jitsi does through `nameOf`; `participant_id` is the participant
  sid. Each channel is one person, so the worker uses the channel's
  display name directly (§6, talke).
- **Speaker hints** for the mixed lane: LiveKit `ActiveSpeakersChanged` → the same `dom-active`
  hints Jitsi sends, including the 2 s heartbeat (`jitsi-speakers.ts` header explains why).
- Chat capture: not in phase 1.

### 4.5 Leaving

| Event | Result |
|---|---|
| `Disconnected` reason `PARTICIPANT_REMOVED` | Removed by the host (same path as Jitsi removal) |
| `Disconnected` reason `ROOM_DELETED` | Meeting ended. Meet 1.37 cannot cause it (no "end for everyone"); an admin delete can |
| Everyone else left | How a Meet 1.37 meeting ends (step 0, C). The silence window, as on every platform; while LiveKit's exact roster still holds a human the bot stays, however long the room is silent (owner, 2026-11-14, rule `exact-roster`). LiveKit gives an exact roster: remote participants that are not bots (`ignoredParticipants`). Use the roster count where the bot already supports it (`rosterCountForLeave`); otherwise the silence rule as for Jitsi |
| `Disconnected` for any other reason after reconnect attempts fail | Connection lost: handled as on every other platform (the removal monitor ends the bot as `evicted`, as Jitsi's does for a lost conference). Owner, 2026-11-14: nothing special for LiveKit |
| Stop from the portal | `room.disconnect()`, then the normal upload drain |

### 4.6 Recording notice

Google Meet posts the Olanote notice in chat; Teams, Zoom and Jitsi do not. Owner (2026-11-14): the
bot posts the same notice into Meet's chat once connected, as a text stream on topic `lk.chat`
(`localParticipant.sendText`), which Meet 1.37's chat reads (step 0), and to each later joiner on
their own, because a text stream is not replayed to people who join after it.

## 5. Security

- The bot calls only hosts in `VEXA_LIVEKIT_HOSTS`; meeting-api rejects other hosts at intake (same
  SSRF check as Jitsi's `meeting_url`).
- The bot receives a LiveKit token only from Meet, scoped to one room, `roomAdmin` false.
- Phase 1 needs no new secret. Phase 2 needs the token service's notetaker secret in `aw-bots`.

## 6. Where the change lands

**aw-bots (`vexa-fork`, this branch)**

| Area | Files |
|---|---|
| Contracts | `contracts/invocation.v1/invocation.schema.json` platform enum + golden `Invocation.livekit.json` |
| Join | `modules/join/src/index.ts` (`Platform`, dispatch), new `modules/join/src/livekit/` (`join.ts`, `admission.ts`, `leave.ts`, `removal.ts`, tests) |
| Page bundle | new `modules/livekit-capture/`; `services/bot/build-browser-utils.mjs`; `services/bot/Dockerfile` build filter; `services/bot/package.json` |
| Bot | `services/bot/src/config.ts` (`Platform`, mixed/per-channel predicates), `join-driver.ts` (`joinPlatform`, removal, leave, withdraw), `capture-bridge.ts` (LiveKit lane, channels, names, hints), `recording.ts` (`channel_kind` `livekit`) |
| Chunker | `modules/record-chunker/src/channel-targets.ts` + test, `index.ts` exports |
| meeting-api | `collector/meeting_link.py` (+ tests), `bot_spawn/router.py` and `service.py` (meeting URL required, host check), `lifecycle/stop_router.py` `_SUPPORTED_PLATFORMS`, `recordings/jsonb.py` `_CHANNEL_KINDS`, config `VEXA_LIVEKIT_HOSTS` in `core/flows/src/config.v1.json` |
| Exporter | `integrations/out/aw-notetaker/exporter/job.py:401`, `activity.py` (+ tests) |
| Helm | `meetingApi.livekitHosts`; `perChannelRecordingPlatforms` |
| Docs | decision 0017, `recording-and-speakers.md`, `meeting-lifecycle.md`, `data-and-configuration.md`, a fragment in `docs/changelog.d/` |
| Architecture model | the new module in `architecture.calm.json`, then `pnpm seal:arch` (P23) |

**talke**: `deployment/base/notetaker/worker/per_channel.py:255` — a `livekit` channel names its
person like a `jitsi` channel (+ test). Project rule: confirm no Jitsi behaviour changes.

**aw-notetaker**

| Area | Files |
|---|---|
| Calendar module | `calendar_client.py` (`AW_BOTS_LIVEKIT_HOSTS`, LiveKit link pattern, platform literal, `choose_link`), deployment env, tests |
| Shared types | `notetaker-common/notetaker_common/schemas.py` platform literal, `notetaker-external-api/notetaker_external_api/dto.py` platform map |
| Portal | `portal/src/lib/platform.ts`, `types.ts`, `components/ui/PlatformIcon.tsx` (label "Olamee", icon) |
| Deploy | `deployment/base/aw-bots/values.yaml`, `deployment/base/calendar-dispatcher/deployment.yaml` |

New dependency: `livekit-client` (Apache-2.0, page bundle only). Recorded as a deviation in
`CHANGELOG.md`, as `ioredis` was.

## 7. Order of work and deploy

0. **Live check (throwaway)** on `meet-next`, from a pod on the **bot node pool** (so UDP from bot
   nodes to LiveKit is proven): knock, admit, connect a page with the token; confirm audio-only
   subscription, that the WebRTC audio hook sees `livekit-client`'s peer connection, per-track audio
   via `<audio>` elements reaching Web Audio, and the `Disconnected` reasons for remove and end.
   **If the hook or the elements do not deliver audio**, the page hands each subscribed
   `MediaStreamTrack` to the capture bridge directly (a page and bridge change; the recorders,
   upload and exporter stay as they are). Approach C is not the fallback for this.
1. aw-bots: contracts → join + page bundle → capture bridge + chunker → meeting-api → exporter, with
   unit tests at each step.
   Add a contract test of the knock client against recorded Meet 1.37 answers (waiting, accepted,
   denied, 404, 429).
2. talke worker change. Add one line to the Meet upgrade steps in
   `talke/deployment/docs/livekit-deployment.md` §6.3: re-run the bot knock test before a new Meet
   version goes live.
3. aw-notetaker: calendar module, shared types, portal.
4. Deploy: worker → aw-bots (image, then meeting-api values, then exporter) → calendar module →
   portal. Shipping the calendar module first makes aw-bots reject LiveKit entries.
5. Live tests on real meetings (§8).

## 8. Done when (live)

- A scheduled website meeting from Google Calendar gets the bot; the host admits it; three people
  speak; the portal shows the meeting; the transcript names each person from their own channel.
- A pasted link with instant join works the same.
- Host denies → `awaiting_admission_rejected`, no retry. Nobody admits for 10 min → timeout.
- Host removes the bot → it leaves and the recording up to then is exported.
- Everyone leaves → the bot leaves.
- A 15-person meeting: bot CPU measured and recorded in the runbook.
- Jitsi, Meet, Teams and Zoom meetings unchanged (their existing tests and one live Jitsi meeting).

## 9. Phase 2: chat-app calls (after the token service)

- Link `https://chat.olamee.com/meet/<URL-encoded Matrix room ID>`; native id = the Matrix room ID.
  Check the exporter folder name with `!` and `:` before choosing an encoding.
- Token source: the token service's notetaker endpoint with its shared secret (new Secret in
  `aw-bots`). No waiting room. Everything after the token is phase 1's code.
- The LiveKit room of a chat-app call is the call's `livekit_room` (a UUID carried in the
  `m.call.livekit.*` events, unchanged when a person is added), not the Matrix room ID
  (`talke/deployment/docs/2026-11-14-livekit-chat-app-calls-plan.md` §3, §8). The bot is sent to
  that room; it stays in the call when the call moves to a new Matrix room.
- Participant identity is the Matrix user ID: names map to ChatWorks users exactly.
- **Add notetaker** button: the ChatWorks backend creates an aw-bots entry with `join_now`; needs
  its own aw-bots key (one key per consumer).

## 10. Before the move of `meet.olamee.com` to LiveKit (trial plan workstream H)

Remove `meet.olamee.com` from `jitsiHosts` and `AW_BOTS_JITSI_HOSTS` and add it to both LiveKit
lists **in the same release**, the day the site moves. Until then it stays Jitsi.

## 11. Owner decisions (2026-11-14)

1. Recording notice: posted in Meet's chat (§4.6).
2. Portal label: "Olamee".

## 12. Left to the builder

Module layout inside `livekit-capture`, the page origin, knock retry/backoff constants, test
structure, and how the roster count plugs into the aloneness adapter.
