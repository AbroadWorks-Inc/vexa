# LiveKit website meetings (phase 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** AW Bots records Olamee website meetings (`https://meet-next.olamee.com/<abc-defg-hij>`) like Jitsi: one bot knocks on Meet's waiting room, is admitted, records each person on their own named channel, and the meeting reaches the portal and the worker.

**Architecture:** Node knocks on Meet's lobby over HTTP until admitted and receives a LiveKit token. The bot's Chrome then opens a page the bot serves itself (`page.route`). That page loads a separate `livekit-page.global.js` bundle (`livekit-client` 2.21.0) and connects audio-only. The existing WebRTC hook, mixed lane, per-channel recorders, upload, exporter and worker do the rest. A new `livekit` platform value is threaded through contracts, meeting-api, the exporter, the worker, the calendar module and the portal.

**Tech Stack:** TypeScript (pnpm/turbo, tsx tests), Playwright, esbuild, `livekit-client` 2.21.0; Python 3.11 (pytest, uv), Helm; Next.js portal (vitest).

**Spec:** `vexa-fork/docs/aw-bots/2026-11-14-livekit-bot-design.md` (approach B; §3.1 holds the step-0 live results and findings A–D).

## Global Constraints

- Platform value `livekit`; website meetings only; native id = the room code, lower-case, `^[a-z]{3}-[a-z]{4}-[a-z]{3}$`.
- Hosts come from config, never hard-coded: meeting-api `VEXA_LIVEKIT_HOSTS` (Helm `meetingApi.livekitHosts`), calendar module `AW_BOTS_LIVEKIT_HOSTS`. Phase-1 value: `meet-next.olamee.com`. `meet.olamee.com` stays a Jitsi host.
- LiveKit hosts are checked before the Jitsi rules, in pasted mode and calendar-scan mode.
- Knock: `POST https://<host>/api/v1.0/rooms/<code>/request-entry/` with `{"username": botName}`, then `{"username", "participant_id"}` every **3 s**; lobby budget = the invocation's `waitingRoomTimeout` (600 s in production, decision 0012).
- The LiveKit token is never logged, written to a file or sent anywhere but `room.connect`. A participant's **identity is never sent** (it can contain an email, step 0 D): a channel carries `participant_id` = participant sid (`PA_…`) and `display_name` = `participant.name`, or no name.
- Subscribe to audio only (`autoSubscribe: false`, `setSubscribed(true)` on audio publications). The bot publishes nothing.
- Recording notice: the existing `MEET_RECORDING_NOTICE` text (`join/src/googlemeet/chat.ts:20`), sent once after connect with `localParticipant.sendText(text, { topic: 'lk.chat' })`.
- Portal label "Olamee".
- New dependency `livekit-client` **2.21.0** exactly (Apache-2.0), only in `modules/livekit-capture`; record it as a deviation in aw-notetaker `CHANGELOG.md`.
- Jitsi, Meet, Teams and Zoom behaviour must not change: their existing tests pass unmodified (except where a list of platforms is asserted).
- Branches (owner rule: `feat/livekit` in talke is for the LiveKit deployment only; any other work branches from `main`):
  - vexa-fork: `feat/livekit-bot` (exists, from `development`).
  - aw-notetaker: `feat/livekit-bot` from `origin/main`, created before the first edit.
  - talke: the worker change on `feat/livekit-bot` from `origin/main`; the one §6.3 line in `livekit-deployment.md` on `feat/livekit`. Before switching the talke checkout, run `git status`: if it holds uncommitted work this session did not write (on 2026-11-14 it held another session's `meet-frontend` and docs edits), stop and ask the owner. Never stash, commit or carry it.
- **No commits or pushes** until the owner asks: every task ends with a checkpoint, not a commit. No worktrees (the owner's rule overrides AGENTS.md here).
- Never read or print secret values.

## How the code is built: Vexa's rules, as the last customisations followed them

The work is an AW customisation of the bot. It follows the fork's P-book (`docs/docs/governance/architecture.mdx` §2) and AGENTS.md hard rules the way the exporter, speaker-activity and Jitsi work did (`docs/aw-bots/archive/`, decisions 0002–0016). For this plan that means:

- **P2/P6, one front door.** `@vexa/livekit-capture` is a page module exactly like `@vexa/jitsi-capture`: its `index.ts` is the only import surface, the bot never imports it (it reaches the browser only as a bundle), and `gate:isolation` holds.
- **P5, adapt at the boundary.** LiveKit's vocabulary (`Room`, `DisconnectReason` numbers, participant identity) stays inside `livekit-capture`; what crosses to Node is our own `VexaLivekit` shape. Meet's lobby JSON stays inside `knock.ts`; what comes out is `{ url, token }` or a typed `AdmissionError`.
- **P4/P8, contracts and goldens.** The platform enums are sealed contracts (Task 1, golden added). The recorded Meet 1.37 answers are the goldens for the knock client (Task 4) and the upgrade check in talke §6.3.
- **P7/P14, config by env.** Hosts arrive by env and are declared in `config.v1.json`; nothing host-specific is hard-coded.
- **P17, licences.** `livekit-client` and its tree pass `gate:licenses` (Category A) or the work stops.
- **P18/P21, fail loud, report from evidence.** Every failure is a typed `AdmissionError` outcome or a logged reason name. `active` is reported only once the room is `connected`, not when the token arrives. A notice that fails to send is logged, never silent, never fatal.
- **P23.** The new module is registered in `architecture.calm.json` and sealed.
- **Source states the designed present.** No history or "used to" comments; comments only for a why the code cannot show, in the surrounding file's style. Fix a defect where it is introduced, not where it shows.
- **Scope is what the bot path needs** (calendar link → aw-bots → bot → exporter → worker → portal). `notetaker-common` `BotJob.platform` belongs to the retired guest-bot path and is not changed (design §6 listed it; dropped on purpose).

## Owner decisions (2026-11-14)

1. **Connection lost mid-call:** handled as on every other platform, nothing special. Any `Disconnected` other than our own `disconnect()` → the removal monitor → `evicted` (`services/bot/src/orchestrator.ts:385`), as Jitsi's monitor does for a lost conference (`join/src/jitsi/removal.ts:12-16`). Design §4.5 updated.
2. **Branches:** as in Global Constraints.
3. **Execution:** all tasks in this session (native), then `/ponytail:ponytail-audit`, `/ponytail:ponytail-review`, then a fresh reviewer panel of subagents (Task 14), then the technical completion report (Task 15).

## Builder decisions (design §12)

- **A separate page bundle.** `livekit-client` is ~1 MB. It is built by `build-browser-utils.mjs` into its own file `dist/livekit-page.global.js` and loaded only by the LiveKit page, not into `browser-utils.global.js`, which every platform's pages run.
- **Page origin:** `https://livekit.aw-bots.invalid/` (`.invalid` never resolves; Playwright fulfils it; it is a secure context, step 0).
- **Knock backoff:** 3 s steady; on 429 or a network error wait 5 s, then 10 s, then 10 s… All waits count against the budget.
- **Roster for the leave rule:** the page emits the Teams-shaped `roster-coverage` observation (`teams-capture/src/msteams-speakers.ts:598`), which the bridge already feeds to `rosterCountForLeave` for any mixed-lane platform (`capture-bridge.ts:812`).
- **Channels:** keyed by `<participant sid>|<track id>`. When a key leaves the subscribed set, its number is forgotten, so a returning key gets a new number. This covers LiveKit reusing a receiver for another person (step 0 A).

## Review Focus

1. **A person leaves and another arrives on the same reused receiver** (step 0 A). Expect a new channel number and name; the first person's channel is released and its number is never reopened. Test in Task 2.
2. **The host never answers, or Meet rate-limits or drops the network for a while.** Expect knocking to continue inside the budget, then `lobby_timeout`, never a crash or an early give-up. Test in Task 4.
3. **The bot is stopped from the portal while knocking.** Expect the loop to end, no page connect, and no admission. Test in Task 4.
4. **A participant with an empty name, or a signed-in host whose identity holds an email.** Expect `display_name` omitted and the identity never on the wire. Tests in Tasks 2 and 3.
5. **Link variants:** upper-case code, trailing slash, query string, a second path segment, an unlisted host. Expect the first three to parse to the lower-case code; the others are not LiveKit. Tests in Tasks 7 and 10.

---

### Task 1: Contracts accept `livekit`

**Files:**
- Modify: `core/meetings/contracts/invocation.v1/invocation.schema.json:7` (Platform enum; the `perChannelRecordingEnabled` description at :44 → "Google Meet, Jitsi and LiveKit only")
- Create: `core/meetings/contracts/invocation.v1/golden/Invocation.livekit.json`
- Modify: `core/gateway/contracts/api.v1/api.schema.json` (both `"Platform"` enums), `core/gateway/contracts/ws.v1/ws.schema.json:11`
- Modify: `contracts.seal.json` (via `pnpm seal:contracts`)
- Test: `core/meetings/services/bot/src/config.test.ts` (reads goldens, :46-50)

**Interfaces:**
- Produces: `"livekit"` in `SPAWNABLE_PLATFORMS` (`meeting_api/bot_spawn/invocation.py:60` derives it from the schema).

- [ ] **Step 1:** Write the golden: the same fields as `Invocation.jitsi.json` without `passcode`: `platform: "livekit"`, `meetingUrl: "https://meet-next.olamee.com/abc-defg-hij"`, `nativeMeetingId: "abc-defg-hij"`, `botName`, `redisUrl`.
- [ ] **Step 2:** Run `node core/meetings/contracts/invocation.v1/validate.mjs`. Expected: FAIL on `Invocation.livekit.json` (enum).
- [ ] **Step 3:** Add `"livekit"` to the three enums. Run `pnpm seal:contracts`.
- [ ] **Step 4:** Run `node core/meetings/contracts/invocation.v1/validate.mjs && node scripts/gates.mjs schema && node scripts/gates.mjs contract-version`. Expected: PASS.
- [ ] **Step 5:** Checkpoint (no commit).

### Task 2: Channel selection and numbering for LiveKit (`record-chunker`)

**Files:**
- Modify: `core/meetings/modules/record-chunker/src/channel-targets.ts`, `src/index.ts` (exports)
- Test: `core/meetings/modules/record-chunker/src/channel-targets.test.ts`

**Interfaces:**
- Produces:
  ```ts
  export interface LivekitChannelCandidate { streamId: string; paused: boolean; audioTracks: number; trackId: string }
  export interface LivekitSubscribedTrack { trackId: string; sid: string; name: string }
  export interface LivekitChannelTarget { streamId: string; key: string; sid: string; name?: string }
  export function selectLivekitChannels(candidates: readonly LivekitChannelCandidate[], subscribed: readonly LivekitSubscribedTrack[]): LivekitChannelTarget[];
  export interface LivekitChannelNumbers { channelFor(key: string): number; forget(key: string): void }
  export function createLivekitChannelNumbers(): LivekitChannelNumbers;
  ```
  `key` = `` `${sid}|${trackId}` ``.

- [ ] **Step 1: Write the failing tests.**
  - `selectLivekitChannels keeps only subscribed tracks`: candidates for tracks `t1`, `t2` and a pre-allocated `t9`; subscribed `[{t1, PA_a, 'Asha'}, {t2, PA_b, 'Ben'}]` → two targets with keys `PA_a|t1`, `PA_b|t2`; `t9` absent.
  - `skips paused or trackless candidates` (same rule as `selectJitsiChannels`).
  - `empty name is omitted, never replaced`: subscribed name `''` → target has no `name` property.
  - `one target per key`: two live elements carrying `t1` → one target (the first live one).
  - `numbers start at 0 and never repeat`: `channelFor('a')`→0, `channelFor('b')`→1, `channelFor('a')`→0.
  - `a forgotten key gets a new number`: `channelFor('a')`→0, `forget('a')`, `channelFor('a')`→2 after `b`=1.
  - `a reused receiver with another person is a new channel`: `channelFor('PA_a|t1')`→0, then `channelFor('PA_c|t1')`→1.
- [ ] **Step 2:** Run `pnpm --filter @vexa/record-chunker test`. Expected: FAIL (not exported).
- [ ] **Step 3:** Implement both functions in `channel-targets.ts` and export them and the three types from `index.ts`.
- [ ] **Step 4:** Run `pnpm --filter @vexa/record-chunker test`. Expected: PASS, Jitsi cases unchanged.
- [ ] **Step 5:** Checkpoint.

### Task 3: The LiveKit page (`modules/livekit-capture`) and its bundle

**Files:**
- Create: `core/meetings/modules/livekit-capture/` mirroring `jitsi-capture` (`package.json` name `@vexa/livekit-capture`, `exports`, `files`, scripts `build`/`test`/`check:isolation`; `tsconfig.json`; `scripts/check-isolation.js` copied; `README.md`, `src/README.md`, `scripts/README.md`)
- Create: `src/livekit-speakers.ts`, `src/livekit-session.ts`, `src/page.ts` (bundle entry), `src/index.ts`, `src/livekit-capture.test.ts`
- Modify: `core/meetings/services/bot/build-browser-utils.mjs` (second esbuild output `dist/livekit-page.global.js` from `modules/livekit-capture/dist/page.js`, IIFE, `chrome120`; export the record-chunker LiveKit functions from Task 2 on `VexaBrowserUtils`)
- Modify: `core/meetings/services/bot/Dockerfile:71` (`--filter "@vexa/livekit-capture"`), add `COPY --from=builder /build/core/meetings/services/bot/dist/livekit-page.global.js /app/livekit-page.global.js` beside line 156
- Modify: `architecture.calm.json` (node `livekit-capture` like `jitsi-capture` at :389-399; add it to `meetings-composed` nodes at :1493, alphabetical), then `pnpm seal:arch`
- Modify: `pnpm-lock.yaml` (via `pnpm install`)

**Interfaces:**
- Consumes: `livekit-client` 2.21.0 (`Room`, `RoomEvent`, `Track`, `DisconnectReason`).
- Produces: the page global `window.VexaLivekit`:
  ```ts
  connect(url: string, token: string, opts: { ignoredParticipants: string[] }): Promise<{ e2ee: boolean }>
  state(): { connected: boolean; disconnectReason?: string }   // reason = DisconnectReason name, e.g. 'PARTICIPANT_REMOVED'
  subscribedTracks(): LivekitSubscribedTrack[]                  // Task 2 type; name = participant.name
  onSpeaking(cb: (name: string, isEnd: boolean, tMs: number) => void): void
  onRoster(cb: (r: { participants: number; named: number; humans: number; tMs: number }) => void): void
  sendChat(text: string): Promise<boolean>                      // never throws
  disconnect(): Promise<void>
  ```
  Module exports for tests: `createLivekitSpeakers({ onSpeaking, heartbeatMs = 2000, now })` → `{ update(names: string[]): void; tick(): void; destroy(): void }`; `countRoster(participants: { name: string }[], ignored: string[])` → `{ participants, named, humans }`; `createLivekitSession(room: RoomLike, opts)` returning the `VexaLivekit` shape (`RoomLike` = the subset of `Room` used, so tests pass a fake emitter).

- [ ] **Step 1: Write the failing tests** (`livekit-capture.test.ts`, tsx + `node:assert`, fake clock, fake room):
  - `speakers: start, heartbeat, end`: `update(['Asha'])` → `('Asha', false, t0)`; `tick()` at +2000 → `('Asha', false)` again; `update([])` → `('Asha', true)`.
  - `speakers: destroy ends the active speaker`.
  - `roster: other notetaker bots are not humans`: names `['Asha', 'Fireflies Notetaker', '']`, ignored `['notetaker']` → `{participants: 3, named: 2, humans: 2}` (an unnamed entry counts as human, Teams rule).
  - `session subscribes audio only`: fake room emits `TrackPublished` audio and video → `setSubscribed(true)` called on the audio one only; existing publications at connect are handled the same.
  - `session never exposes identity`: participant identity `f:1:asha@example.com` → no field of `subscribedTracks()` contains `@`.
  - `session records the disconnect reason name`: emit `Disconnected` with `4` → `state()` = `{connected: false, disconnectReason: 'PARTICIPANT_REMOVED'}`.
  - `sendChat uses lk.chat`: `localParticipant.sendText` called with `{ topic: 'lk.chat' }`; a rejecting `sendText` → `false`, no throw.
- [ ] **Step 2:** Run `pnpm --filter @vexa/livekit-capture test`. Expected: FAIL (module missing).
- [ ] **Step 3:** Implement. `connect` uses `new Room({ adaptiveStream: false, dynacast: false })` and `room.connect(url, token, { autoSubscribe: false })`; returns `{ e2ee: room.isE2EEEnabled }`; the token is not kept after the call. `page.ts` sets `window.VexaLivekit = createLivekitSession(new Room(...), …)`. Roster emits on every participant connect/disconnect. Add `"livekit-client": "2.21.0"` to `dependencies`; run `pnpm install`.
- [ ] **Step 4:** Run `pnpm --filter @vexa/livekit-capture test && pnpm --filter @vexa/livekit-capture build && pnpm --filter @vexa/livekit-capture check:isolation`. Expected: PASS.
- [ ] **Step 5:** Run `pnpm --filter @vexa/bot build`. Expected: `dist/browser-utils.global.js` **unchanged in size apart from the Task 2 functions** (no `livekit-client` in it: `grep -c "livekit-client" dist/browser-utils.global.js` → 0), and `dist/livekit-page.global.js` exists and defines `VexaLivekit`.
- [ ] **Step 6:** Run `pnpm seal:arch && node scripts/gates.mjs dataflow && node scripts/gates.mjs readme && node scripts/gates.mjs licenses && node scripts/gates.mjs exports`. Expected: PASS (`livekit-client` and its dependencies are Category A; if the licence gate names one that isn't, stop and report).
- [ ] **Step 7:** Checkpoint.

### Task 4: The knock client (`join/src/livekit/knock.ts`)

**Files:**
- Create: `core/meetings/modules/join/src/livekit/knock.ts`, `knock.test.ts`, `README.md`
- Create: `core/meetings/modules/join/src/livekit/fixtures/` with recorded Meet 1.37 answers: `waiting.json`, `accepted.json` (token replaced by `"TEST-TOKEN-NOT-REAL"`), `denied.json`, `404.json` (`{"detail": …}`); 429 has no body
- Modify: `core/meetings/modules/join/package.json` (append `tsx src/livekit/knock.test.ts` to the `test` chain)

**Interfaces:**
- Consumes: `AdmissionError` (`join/src/shared/admission.ts:15-24`).
- Produces:
  ```ts
  export function parseLivekitUrl(meetingUrl: string): { host: string; code: string }   // throws AdmissionError('join_failure') if not a room URL
  export interface KnockOptions {
    host: string; code: string; botName: string; budgetMs: number;
    onWaiting: () => void | Promise<void>;      // called once, on the first 'waiting'
    cancelled: () => boolean;                   // true once the bot is withdrawn
    fetch?: typeof fetch; sleep?: (ms: number) => Promise<void>; now?: () => number; log?: (m: string) => void;
  }
  export function knockUntilAdmitted(o: KnockOptions): Promise<{ url: string; token: string }>
  ```

- [ ] **Step 1: Write the failing tests** (fake fetch over the fixtures, fake clock):
  - `waiting then accepted returns url and token; second call carries participant_id; onWaiting once`.
  - `knocks every 3 s` (sleep called with 3000 between waiting answers).
  - `denied → AdmissionError('denial')`.
  - `404 → AdmissionError('join_failure')`, message contains `404`.
  - `429 then accepted`: waits 5000, continues, succeeds.
  - `network error then accepted`: continues within the budget.
  - `budget runs out while waiting → AdmissionError('lobby_timeout')`.
  - `cancelled() true → stops, throws AdmissionError('join_failure', 'withdrawn')`, no further fetch.
  - `the token never reaches the log`: collect `log` lines across a full run; none contains `TEST-TOKEN-NOT-REAL`.
  - `parseLivekitUrl`: `https://meet-next.olamee.com/ABC-DEFG-HIJ/` → `{host: 'meet-next.olamee.com', code: 'abc-defg-hij'}`; `…/abc-defg-hij?x=1` → same code; `…/abc-defg-hij/extra` → throws.
- [ ] **Step 2:** Run `pnpm --filter @vexa/join test`. Expected: FAIL.
- [ ] **Step 3:** Implement `knock.ts` (global `fetch`, `Content-Type: application/json`; log status and booleans only, as the step-0 probe did).
- [ ] **Step 4:** Run `pnpm --filter @vexa/join test`. Expected: PASS.
- [ ] **Step 5:** Checkpoint.

### Task 5: Join, leave, removal and the notice (`join/src/livekit/`)

**Files:**
- Create: `core/meetings/modules/join/src/livekit/join.ts`, `leave.ts`, `removal.ts`, `join.test.ts`
- Modify: `core/meetings/modules/join/src/index.ts` (`Platform` :37, dispatch :118-122 area, re-exports :166, unsupported-platform message :132-134), `src/_host.ts:15` (union), `src/_host.ts` (`stopLivekitRecording`, a copy of `stopJitsiRecording` at :92), `src/__tests__/unknownPlatform.test.ts` (expected message), `package.json` test chain

**Interfaces:**
- Consumes: Task 3's `window.VexaLivekit`; Task 4's `parseLivekitUrl`, `knockUntilAdmitted`; `MEET_RECORDING_NOTICE`.
- Produces:
  ```ts
  export const LIVEKIT_PAGE_ORIGIN = 'https://livekit.aw-bots.invalid';
  export function livekitPageScriptPath(): string   // process.env.VEXA_LIVEKIT_PAGE_JS || '/app/livekit-page.global.js'
  export function joinLivekitMeeting(page: Page, meetingUrl: string, botName: string, botConfig: BotConfig, waitingRoomTimeoutMs: number): Promise<void>
  export function leaveLivekitMeeting(page: Page | null, botConfig?: BotConfig, reason?: string): Promise<boolean>
  export function startLivekitRemovalMonitor(page: Page | null, onRemoval?: () => void | Promise<void>): () => void
  ```
  `joinLivekitMeeting`: parse URL → route the origin (`/` = a minimal HTML page, `/livekit.js` = the bundle file) → `goto` → knock (`onWaiting` = `callAwaitingAdmissionCallback`, `cancelled` = `page.isClosed()`) → `VexaLivekit.connect(url, token, { ignoredParticipants })` → if `e2ee`, `disconnect()` and throw `AdmissionError('join_failure', 'encrypted room')` → `VexaLivekit.sendChat(MEET_RECORDING_NOTICE)` (result logged, never fatal). In `index.ts` the livekit branch calls it and treats a return as admitted (no separate admission wait).
  `startLivekitRemovalMonitor`: poll `VexaLivekit.state()` every 1 s; `connected: false` with any reason other than `CLIENT_INITIATED` → log the reason name, then `onRemoval` once (owner decision 1: same as every platform). Returns a stop function.

- [ ] **Step 1: Write the failing tests** (`join.test.ts`; a Playwright page against a stub `VexaLivekit` served on the route, the knock stubbed with an accepted answer, the same skip-if-no-Chromium pattern as `services/bot/src/channel-recording.boundary.test.ts`):
  - `admitted: connect called with the token, notice sent on lk.chat, returns`.
  - `encrypted room → AdmissionError('join_failure', 'encrypted room') and disconnect called`.
  - `notice failure does not fail the join`.
  - `removal fires once for PARTICIPANT_REMOVED`, `once for ROOM_DELETED`, `never for CLIENT_INITIATED`.
  - `leave calls disconnect and the leave callback`.
  - `unknownPlatform` test: message now lists `livekit`.
- [ ] **Step 2:** Run `pnpm --filter @vexa/join test`. Expected: FAIL.
- [ ] **Step 3:** Implement, and wire `index.ts` / `_host.ts`.
- [ ] **Step 4:** Run `pnpm --filter @vexa/join test && node core/meetings/modules/join/scripts/check-isolation.js`. Expected: PASS; the Jitsi, Meet, Teams and Zoom tests are unchanged and green.
- [ ] **Step 5:** Checkpoint.

### Task 6: The bot runs the LiveKit lane

**Files:**
- Modify: `core/meetings/services/bot/src/config.ts:29` (`Platform`), `:36-38` (`isMixedLanePlatform` includes `'livekit'`)
- Modify: `src/join-driver.ts:55-57` (`joinPlatform` keeps `'livekit'`), `onRemoval` :165, `leave` :171, `withdraw` :188 (LiveKit branches)
- Modify: `src/capture-bridge.ts`:
  - `:739` `const livekit = inv.platform === 'livekit'`
  - `:829` `__vexaChannelActivity` exposure for `jitsi || livekit`
  - `:856/:1462` pass `isLivekit`
  - a new `if (isLivekit)` block beside `:1422`: `VexaLivekit.onSpeaking` → `__vexaSpeakerHint(name, tMs, isEnd, 'dominant')`; `VexaLivekit.onRoster` → `__vexaObservation('mixed', { type: 'roster-coverage', platform: 'livekit', ...r }, r.tMs)`
  - `:1586` `recordChannels` includes `'livekit'`
  - `mediaStreams()` adds `trackId` (the first live audio track's id)
  - a `livekit` scan branch beside `:1782` (`selectLivekitChannels(present, w.VexaLivekit.subscribedTracks())`, `numbers.channelFor(target.key)`, `openChannel(channel, stream, 'livekit', target.streamId, () => ({ participantId: target.sid, displayName: target.name }))`; a released LiveKit channel calls `numbers.forget(key)`)
  - `:1732` level tap for `jitsi` or `livekit`
- Modify: `src/recording.ts:112` (allow `channel_kind === 'livekit'`)
- Test: `src/config.test.ts`, `src/recording.test.ts`, new `src/livekit-channel-recording.boundary.test.ts` (copy of `channel-recording.boundary.test.ts` with a stub `VexaLivekit`), `package.json` test chain

**Interfaces:**
- Consumes: Tasks 2, 3 and 5.
- Produces: channel chunks with `channel_kind: 'livekit'`, `participant_id: 'PA_…'`, and `display_name` when the name is known.

- [ ] **Step 1: Write the failing tests:**
  - `config.test.ts`: `captureLane('livekit') === 'mixed'`, `recordingSource('livekit') === 'mixed-mix'`; the Jitsi golden assertions untouched; the LiveKit golden parses.
  - `recording.test.ts`: `channel_kind 'livekit'` is kept in the metadata; an unknown kind is still dropped.
  - `livekit-channel-recording.boundary.test.ts`: two subscribed tracks plus one pre-allocated → exactly two channels, kind `livekit`, ids `PA_a`/`PA_b`, names `Asha`/`Ben`; the stub moves `t1` to `PA_c` → a third channel and the first is released; no identity string in any chunk.
- [ ] **Step 2:** Run `pnpm --filter @vexa/bot test`. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `pnpm --filter @vexa/bot test && pnpm --filter @vexa/bot check:isolation`. Expected: PASS, including the unchanged `channel-recording`, `jitsi-session-restart`, `capture-bridge`, `teams-*`, `zoom-*` and `csrc-*` tests.
- [ ] **Step 5:** Checkpoint.

### Task 7: meeting-api accepts LiveKit links

**Files:**
- Modify: `core/meetings/services/meeting-api/src/meeting_api/collector/meeting_link.py` (`_configured_livekit_hosts()` like `_configured_jitsi_hosts` :65-71, reading `VEXA_LIVEKIT_HOSTS`; a LiveKit rule as the first host rule (before :94), returning `("livekit", code)` or `None`; docstring row :10-17)
- Modify: `bot_spawn/router.py` (for `platform == "livekit"`: require `meeting_url` and `parse_meeting_url(meeting_url) == ("livekit", native_meeting_id)`, else 422; message :442 lists livekit)
- Modify: `lifecycle/stop_router.py:82`, `recordings/jsonb.py:35` (+ `recordings/README.md:36`)
- Modify: `src/meeting_api/config.v1.json` (a `VEXA_LIVEKIT_HOSTS` entry shaped like :173-179; description at :190 lists livekit); `deploy/compose/docker-compose.yml` only where the config-contract gate requires it, beside the meeting-api `VEXA_JITSI_HOSTS`
- Modify: `deploy/helm/charts/vexa/values.yaml` (`livekitHosts: ""` beside :273; comment at :245-247), `templates/deployment-meeting-api.yaml` (env beside :210-211), `deploy/helm/tests/test_template.sh` (default `value: ""` beside :569; `--set meetingApi.livekitHosts=meet-next.example.org` and its `env_is` beside :597/:617), `deploy/helm/README.md:69`
- Test: `tests/test_meeting_link.py`, `tests/test_bot_spawn.py`, `tests/test_recordings.py`, `tests/test_config_contract.py`

**Interfaces:**
- Produces: `parse_meeting_url("https://meet-next.olamee.com/abc-defg-hij") == ("livekit", "abc-defg-hij")` when the host is listed.

- [ ] **Step 1: Write the failing tests** (`monkeypatch.setenv("VEXA_LIVEKIT_HOSTS", "meet-next.olamee.com")`):
  - `TestParseLivekit`: plain, upper-case code, trailing slash and query → `("livekit", "abc-defg-hij")` in both `generic_hosts` modes; `/abc-defg-hij/x`, `/notacode`, the bare host → `None`; an unlisted host → not livekit; `VEXA_LIVEKIT_HOSTS` unset → not livekit; `meet.olamee.com` (listed in `VEXA_JITSI_HOSTS`) stays Jitsi.
  - `find_meeting_link` finds a LiveKit URL inside text.
  - Spawn: `platform=livekit` without `meeting_url` → 422; with a URL on another host → 422; with a URL whose code ≠ `native_meeting_id` → 422; a matching one → accepted.
  - `_CHANNEL_KINDS` keeps `livekit`; stop accepts `livekit`.
- [ ] **Step 2:** Run `cd core/meetings/services/meeting-api && uv run pytest -q tests/test_meeting_link.py tests/test_bot_spawn.py tests/test_recordings.py`. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `uv run pytest -q` (whole meeting-api), `bash deploy/helm/tests/test_template.sh`, `node scripts/gates.mjs config-contract`. Expected: PASS; all existing Jitsi and hosted-Zoom tests unchanged.
- [ ] **Step 5:** Checkpoint.

### Task 8: The exporter keeps LiveKit channels

**Files:**
- Modify: `integrations/out/aw-notetaker/exporter/job.py:401` (and docstring :393): accept `channel_kind` `livekit`
- Test: `integrations/out/aw-notetaker/tests/test_job.py`

- [ ] **Step 1: Write the failing test** `test_livekit_channels_are_indexed`: a meeting with two channel files of kind `livekit` → `channels/index.json` rows with `kind: "livekit"`, the display names and `participant_id` `PA_…`; the folder name is `livekit_abc-defg-hij_<UTC>` (`naming.py` needs no change).
- [ ] **Step 2:** Run `cd integrations/out/aw-notetaker && uv run pytest -q tests/test_job.py`. Expected: FAIL (`ChannelIdentityMissing`).
- [ ] **Step 3:** Implement. `attribution.py:245` (the Zoom/Teams gate) stays as it is: LiveKit is per-channel like Jitsi.
- [ ] **Step 4:** Run `uv run pytest -q && uv run black --check . && uv run ruff check . && uv run mypy exporter`. Expected: PASS.
- [ ] **Step 5:** Checkpoint.

### Task 9: The worker names LiveKit channels (talke)

**Branches:** worker files on talke `feat/livekit-bot` from `origin/main`; the doc line on `feat/livekit`. Check `git status` first (Global Constraints).

**Files:**
- Modify (`feat/livekit-bot`): `talke/deployment/base/notetaker/worker/per_channel.py:255` and `:340` (`kind in ("jitsi", "livekit")`; docstring :314); worker `README.md:148,159`
- Modify (`feat/livekit`): `talke/deployment/docs/livekit-deployment.md:618` (Meet upgrade bullet: before a new Meet version goes live, re-record its lobby answers and run the bot's knock test, `join/src/livekit/knock.test.ts`)
- Test: `talke/deployment/base/notetaker/worker/tests/test_per_channel.py`

- [ ] **Step 1: Write the failing test** `test_livekit_uses_the_index_name_and_does_not_rewrite_it`: a copy of `test_jitsi_uses_the_index_name_and_does_not_rewrite_it` (:268) with `kind: "livekit"` and the same expectations.
- [ ] **Step 2:** Run `cd talke/deployment/base/notetaker/worker && python -m pytest -q tests/test_per_channel.py`. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `python -m pytest -q tests`. Expected: PASS, Jitsi and Meet tests unchanged.
- [ ] **Step 5:** Checkpoint.

### Task 10: The calendar module sends LiveKit links (aw-notetaker)

**Files:**
- Modify: `calendar-dispatcher/calendar_dispatcher/calendar_client.py` (`_livekit_url_re()` beside `_jitsi_url_re()` :264-288, reading `AW_BOTS_LIVEKIT_HOSTS`, default `""` = off, matching `https?://<host>/[a-z]{3}-[a-z]{4}-[a-z]{3}` case-insensitively and not followed by `[\w-]`; `"livekit"` added to the Literals at :110, :300, :340; patterns list :339-345 puts livekit before jitsi; docstrings :107-109, :307, :373, :644)
- Modify: `calendar-dispatcher/README.md:70,75-76,171`
- Test: `calendar-dispatcher/tests/test_calendar_client.py`, `tests/test_aw_bots_sync.py:360-364`

- [ ] **Step 1: Write the failing tests** (beside the Jitsi ones at :1782-1939):
  - `test_livekit_link_in_description_wins_over_automatic_meet` → `platform == "livekit"`, URL unchanged.
  - `test_livekit_hosts_come_from_aw_bots_livekit_hosts`; `test_livekit_is_off_when_aw_bots_livekit_hosts_is_unset`.
  - `test_livekit_needs_a_room_code` (parametrised: `/abc-defg-hij` yes; `/abc-defg-hij-x`, `/abc`, `/` no).
  - `test_livekit_link_drops_trailing_sentence_punctuation`.
  - `test_aw_bots_sync`: the parametrised event list gains a livekit case reaching `put_entry` with the URL.
- [ ] **Step 2:** Run `cd calendar-dispatcher && ../.venv/bin/python -m pytest -q -p no:cacheprovider tests/test_calendar_client.py tests/test_aw_bots_sync.py`. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `../.venv/bin/python -m pytest -q -p no:cacheprovider && ../.venv/bin/black --check . && ../.venv/bin/ruff check . && ../.venv/bin/mypy .`. Expected: PASS.
- [ ] **Step 5:** Checkpoint.

### Task 11: The external API and the portal (aw-notetaker)

**Files:**
- Modify: `notetaker-external-api/notetaker_external_api/dto.py:60-67` (`"livekit": "livekit"`); `tests/test_dto.py:152-160`; `DEVELOPER-GUIDE.md:370-381` (a `livekit` row: "Olamee website meeting")
- Modify: `portal/src/lib/types.ts:13`, `src/lib/platform.ts` (`PLATFORM_LABEL` and `PLATFORM_SHORT_LABEL` `livekit: "Olamee"`, `AW_BOTS_PLATFORMS` `["livekit", "livekit"]`), `src/components/ui/PlatformIcon.tsx:12-21` (a tint, e.g. `text-violet-600`), `src/lib/meeting-mirror.ts:62-67`, `src/lib/legacy-meetings.ts:217-223`
- Test: `portal/src/lib/platform.test.ts:103-112`, `src/lib/meetings.test.ts:873-887`

- [ ] **Step 1: Write the failing tests:** `external_platform("livekit") == "livekit"`; portal `PLATFORM_LABEL.livekit === "Olamee"`, `platformOfAwBots("livekit") === "livekit"`, `platformOf` for a `livekit` meeting.
- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3:** Implement (`tsc` lists every `Record<Platform, …>` that needs the key).
- [ ] **Step 4:** Run, in `notetaker-external-api`: `../.venv/bin/python -m pytest -q && ../.venv/bin/black --check . && ../.venv/bin/ruff check . && ../.venv/bin/mypy notetaker_external_api/`; in `portal`: `npm test && npm run typecheck && npm run lint`. Expected: PASS.
- [ ] **Step 5:** Checkpoint.

### Task 12: Deploy values, docs and changelogs

**Files:**
- Modify (aw-notetaker, applied at deploy time with the owner): `deployment/base/aw-bots/values.yaml` (`livekitHosts: "meet-next.olamee.com"` beside :136; `perChannelRecordingPlatforms: "google_meet,jitsi,livekit"` :114), `deployment/base/calendar-dispatcher/deployment.yaml` (`AW_BOTS_LIVEKIT_HOSTS` beside :81), `deployment/base/aw-bots/README.md` (a LiveKit row beside the Jitsi links at :469-472 and per-speaker at :479-485)
- Modify: aw-notetaker `CHANGELOG.md` under `## [Unreleased]`: `### Dependency deviation — \`livekit-client\` 2.21.0 in aw-bots livekit-capture (2026-11-14)`, one paragraph (Apache-2.0, page bundle only, exact pin, why); `CLAUDE.md` AW Bots paragraph: one line, "LiveKit website meetings (phase 1): built on `feat/livekit-bot`, not deployed"
- Create: `vexa-fork/docs/aw-bots/decisions/0017-livekit-meetings-through-aw-bots.md` (context, decision approach B, step-0 facts A–D, consequence); `vexa-fork/docs/changelog.d/aw-livekit-bot.md`
- Modify: `vexa-fork/docs/aw-bots/recording-and-speakers.md` (lane table :31-35, per-speaker :145-207, notice :212-220, limitations :244), `meeting-lifecycle.md:20-22`, `data-and-configuration.md:79-89`, `decisions/README.md`

- [ ] **Step 1:** Write the changes. Docs describe current behaviour only (no history), per the fork's rule.
- [ ] **Step 2:** Run `node scripts/gates.mjs readme && node scripts/gates.mjs docs-version`. Expected: PASS.
- [ ] **Step 3:** Checkpoint.

### Task 13: Verification before reporting

- [ ] **Step 1:** vexa-fork: `node scripts/gates.mjs all`. Expected: every gate green; any gate that cannot run here (Docker, compose) is reported as not run, with its output.
- [ ] **Step 2:** Package tests: `pnpm --filter @vexa/record-chunker --filter @vexa/livekit-capture --filter @vexa/join --filter @vexa/bot test`; meeting-api `uv run pytest -q`; exporter `uv run pytest -q && uv run black --check . && uv run ruff check . && uv run mypy exporter`.
- [ ] **Step 3:** talke worker `python -m pytest -q tests`.
- [ ] **Step 4:** aw-notetaker: calendar-dispatcher and notetaker-external-api (black, ruff, mypy, pytest as in Tasks 10–11); portal `npm test && npm run typecheck && npm run lint`.
- [ ] **Step 5:** Record the raw results per repo, including anything not run, for Task 15.

### Task 14: Review

- [ ] **Step 1:** Run `/ponytail:ponytail-audit` over the changed code in each repo. Fix each confirmed finding at its point of introduction, re-run the touched tests, and note what was not fixed and why.
- [ ] **Step 2:** Run `/ponytail:ponytail-review` on the whole change set (vexa-fork, talke, aw-notetaker). Same handling.
- [ ] **Step 3:** Fresh reviewer panel (subagents that have not seen this session, given the design, this plan and the diffs, not my conclusions): one each for (a) correctness and Vexa P-book compliance in the bot, join and page modules; (b) meeting-api, exporter, Helm and contracts; (c) the talke worker and the aw-notetaker calendar module, external API and portal; (d) security: token handling, identity on the wire, the host checks. Verify each finding against the code before acting; fix the confirmed ones, re-run Task 13's commands.
- [ ] **Step 4:** Checkpoint.

### Task 15: Technical completion report, then stop

**Files:**
- Create: `vexa-fork/docs/aw-bots/2026-11-14-livekit-bot-completion-report.md`, in the shape of `archive/2026-09-23-aw-exporter-completion-report.md`: 1 Summary (table: tasks, tests, gates, core changes, code size), 2 Task by task (built, verified how, differs from plan?), 3 Deviations and why, 4 Rulings made on the owner's behalf, 5 What needs the owner (deploy order, live tests), 6 How to run.

- [ ] **Step 1:** Write the report from Task 13's raw results and Task 14's findings, facts only, file:line where it matters.
- [ ] **Step 2:** Stop. Commits, pushes, deploy (worker → aw-bots image, then meeting-api values, then exporter → calendar module → portal) and the live tests (design §8, plus the step-0 items not yet checked: two speakers, denial, 429, timeout, reconnect, CPU) happen with the owner.
