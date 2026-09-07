/**
 * Teams left-alone / empty-room timer + caption-provenance unit tests.
 *
 * Run: npx tsx services/vexa-bot/core/src/platforms/msteams/alone-timer.test.ts
 *
 * Pure logic (no DOM, no Playwright, no Redis), so the exit-triggering leave
 * decision and the caption event shape are both covered. Matches
 * pcm-chunker.test.ts / zoom's alone-timer.test.ts standalone-harness
 * convention (no vitest).
 *
 * Cases 1-5 are PORTED VERBATIM from platforms/zoom/web/alone-timer.test.ts so
 * that the deliberate duplication of `stepAloneTimer` cannot drift silently —
 * if someone edits the Teams copy, these fail here without touching Zoom.
 */

import { readFileSync } from 'node:fs';
import {
  AloneTimerState,
  TeamsAloneTimeouts,
  TeamsPresenceReading,
  TEAMS_ROSTER_GONE_STALE_MS,
  TEAMS_DEFAULT_EVERYONE_LEFT_MS,
  TEAMS_DEFAULT_NO_ONE_JOINED_MS,
  TEAMS_POLL_MS,
  TEAMS_PRESENCE_WINDOW_MS,
  TEAMS_STALE_EVIDENCE_MS,
  readTeamsLeaveTimeouts,
  resolveBotDisplayName,
  resolveTeamsAloneCount,
  stepAloneTimer,
  teamsAloneTick,
} from './alone-timer';
import { buildCaptionSpeakerEventFields, CAPTION_EVENT_SOURCE } from './caption-speaker-events';

let passed = 0;
let failed = 0;

function assertEqual(actual: unknown, expected: unknown, label: string): void {
  const ok = JSON.stringify(actual) === JSON.stringify(expected);
  if (ok) {
    passed++;
    console.log(`  ✅ ${label}`);
  } else {
    failed++;
    console.log(`  ❌ ${label}`);
    console.log(`     expected: ${JSON.stringify(expected)}`);
    console.log(`     actual:   ${JSON.stringify(actual)}`);
  }
}

// ═══════════════════════════════════════════════════════════════════════════
// stepAloneTimer — ported from Zoom's suite (the duplication guard)
// ═══════════════════════════════════════════════════════════════════════════

const POLL = 3000;
const EVERYONE_LEFT = 120_000; // 2 min
const NO_ONE_JOINED = 600_000; // 10 min
const step = (s: AloneTimerState, count: number) =>
  stepAloneTimer(s, count, POLL, EVERYONE_LEFT, NO_ONE_JOINED);

// 1. count>=2 resets and records that others were seen.
{
  const r = step({ aloneMs: 90_000, sawOthers: false }, 3);
  assertEqual(r.state, { aloneMs: 0, sawOthers: true }, 'count>=2 resets aloneMs and sets sawOthers');
  assertEqual([r.shouldLeave, r.label], [false, null], 'count>=2 never leaves');
}

// 2. count===0 (unreadable) holds unchanged and never leaves — even past threshold.
{
  const s: AloneTimerState = { aloneMs: NO_ONE_JOINED + 999_999, sawOthers: false };
  const r = step(s, 0);
  assertEqual(r.state, s, 'count===0 holds state unchanged');
  assertEqual(r.shouldLeave, false, 'count===0 never leaves even past threshold');
}

// 3. Never-joined path: leaves exactly at noOneJoinedTimeout, not before.
{
  let s: AloneTimerState = { aloneMs: 0, sawOthers: false };
  const need = NO_ONE_JOINED / POLL; // 200 ticks
  let leftAt = -1;
  let label = '';
  for (let i = 1; i <= need + 2; i++) {
    const r = step(s, 1);
    s = r.state;
    label = r.label || label;
    if (r.shouldLeave) { leftAt = i; break; }
  }
  assertEqual(leftAt, need, 'no-one-joined leaves exactly at noOneJoinedTimeout');
  assertEqual(label, 'no one joined', 'no-one-joined label');
}

// 4. Everyone-left path: shorter threshold once others were seen.
{
  let s: AloneTimerState = { aloneMs: 0, sawOthers: true };
  const need = EVERYONE_LEFT / POLL; // 40 ticks
  let leftAt = -1;
  let label = '';
  for (let i = 1; i <= need + 2; i++) {
    const r = step(s, 1);
    s = r.state;
    label = r.label || label;
    if (r.shouldLeave) { leftAt = i; break; }
  }
  assertEqual(leftAt, need, 'everyone-left leaves exactly at everyoneLeftTimeout');
  assertEqual(label, 'everyone left', 'everyone-left label');
}

// 5. Flapping: a single count>=2 tick fully resets near-threshold accumulation.
{
  const rejoin = step({ aloneMs: EVERYONE_LEFT - POLL, sawOthers: true }, 2);
  assertEqual([rejoin.state.aloneMs, rejoin.shouldLeave], [0, false], 'count>=2 resets near-threshold accumulation');
  const stay = step({ aloneMs: EVERYONE_LEFT - POLL, sawOthers: true }, 1);
  assertEqual(stay.shouldLeave, true, 'without rejoin, the next alone tick crosses threshold');
}

// ═══════════════════════════════════════════════════════════════════════════
// resolveTeamsAloneCount — the Teams-specific presence mapping
// ═══════════════════════════════════════════════════════════════════════════

const WINDOW = 60_000;
const STALE = 900_000;
const reading = (r: Partial<TeamsPresenceReading> = {}): TeamsPresenceReading => ({
  rosterHumans: null,
  signalTiles: 0,
  humanEvidenceAgeMs: null,
  ...r,
});
const resolve = (r: Partial<TeamsPresenceReading>) => resolveTeamsAloneCount(reading(r), WINDOW, STALE);

// 6. THE defect this module exists for: the closed Participants panel.
//    A closed panel yields no rows, so recording.ts reports rosterHumans: null.
//    That must NOT read as an empty meeting.
{
  assertEqual(
    resolve({ rosterHumans: null, signalTiles: 0, humanEvidenceAgeMs: 5_000 }),
    2,
    'closed panel + recent speech → present (a human spoke), NOT alone'
  );
  assertEqual(
    resolve({ rosterHumans: 0, signalTiles: 0, humanEvidenceAgeMs: 5_000 }),
    2,
    'an open panel claiming 0 humans loses to someone who spoke 5s ago'
  );
  assertEqual(
    resolve({ rosterHumans: 0, signalTiles: 0, humanEvidenceAgeMs: null }),
    1,
    'an OPEN panel with 0 humans and no speech ever → alone (authoritative)'
  );
}

// 7. Roster readability is a distinct fact from roster emptiness.
{
  assertEqual(resolve({ rosterHumans: 1 }), 2, 'readable panel with 1 human → present');
  assertEqual(resolve({ rosterHumans: 4 }), 2, 'readable panel with 4 humans → present');
  assertEqual(resolve({ rosterHumans: 0 }), 1, 'readable panel with 0 humans → alone');
  assertEqual(
    resolve({ rosterHumans: null, humanEvidenceAgeMs: WINDOW + 1 }),
    0,
    'unreadable panel + stale-but-not-old evidence → HOLD (0), not alone'
  );
}

// 8. Stage tiles corroborate presence with the panel closed. Only >= 2 counts,
//    because whether the bot's own tile is included is unknown.
{
  assertEqual(resolve({ signalTiles: 2 }), 2, '2 signal tiles → present');
  assertEqual(resolve({ signalTiles: 9 }), 2, '9 signal tiles → present');
  assertEqual(
    resolve({ signalTiles: 1, humanEvidenceAgeMs: null }),
    1,
    '1 signal tile with no speech ever is NOT proof of a human → no-one-joined'
  );
  assertEqual(
    resolve({ signalTiles: 1, humanEvidenceAgeMs: WINDOW + 1 }),
    0,
    '1 signal tile after a human was heard → HOLD, not a leave'
  );
}

// 9. The presence window boundary.
{
  assertEqual(resolve({ humanEvidenceAgeMs: WINDOW }), 2, 'evidence exactly at the window is still present');
  assertEqual(resolve({ humanEvidenceAgeMs: WINDOW + 1 }), 0, 'one ms past the window falls through to HOLD');
}

// 10. The stale-evidence bound: HOLD is finite, or the Job runs to its 4h
//     deadline and the artifacts are SIGKILLed away.
{
  assertEqual(resolve({ humanEvidenceAgeMs: STALE }), 0, 'evidence exactly at the stale bound still holds');
  assertEqual(resolve({ humanEvidenceAgeMs: STALE + 1 }), 1, 'past the stale bound, stop holding → alone');
  assertEqual(
    resolve({ humanEvidenceAgeMs: STALE + 1, signalTiles: 2 }),
    2,
    'stale evidence does not override live stage tiles'
  );
  assertEqual(
    resolve({ humanEvidenceAgeMs: STALE + 1, rosterHumans: 3 }),
    2,
    'stale evidence does not override a readable, populated roster'
  );
}

// 11. teamsAloneTick end-to-end: the reading the old code produced (roster
//     always empty) no longer leaves at the 20-minute startup timeout while a
//     human is speaking.
{
  const timeouts = readTeamsLeaveTimeouts({ noOneJoinedTimeout: 600_000, everyoneLeftTimeout: 120_000 });
  let state: AloneTimerState = { aloneMs: 0, sawOthers: false };
  // 30 minutes of a real meeting with the panel shut and captions flowing.
  for (let i = 0; i < 1800; i++) {
    const t = teamsAloneTick(state, reading({ humanEvidenceAgeMs: 2_000 }), timeouts);
    state = t.state;
    if (t.shouldLeave) break;
  }
  // `rosterEverReadable: false` because this scenario keeps the panel SHUT for
  // the whole 30 minutes (`reading()` defaults rosterHumans to null), so the
  // latch never fires. That is the case the long stale hold still protects.
  assertEqual(
    state,
    { aloneMs: 0, sawOthers: true, rosterEverReadable: false },
    '30 min of captions never accumulates alone time'
  );

  // Then everyone leaves: no more evidence, and the roster becomes readable and
  // empty (Teams shows the roster when you are the last one).
  let leftAt = -1;
  let label: string | null = null;
  for (let i = 1; i <= 200; i++) {
    const t = teamsAloneTick(state, reading({ rosterHumans: 0, humanEvidenceAgeMs: 60_000 + i * 1000 }), timeouts);
    state = t.state;
    label = t.label ?? label;
    if (t.shouldLeave) { leftAt = i; break; }
  }
  assertEqual(leftAt, 120, 'after everyone leaves, it leaves at everyoneLeftTimeout (120 ticks of 1s)');
  assertEqual(label, 'everyone left', 'and reports the everyone-left reason, not the startup one');
}

// 12. The other half of the defect: an unreadable DOM for a whole meeting must
//     still terminate, or the Job hits activeDeadlineSeconds.
{
  const timeouts = readTeamsLeaveTimeouts({});
  let state: AloneTimerState = { aloneMs: 0, sawOthers: true };
  let leftAt = -1;
  // Evidence ages past the stale bound while nothing is readable.
  for (let i = 1; i <= 200; i++) {
    const age = TEAMS_STALE_EVIDENCE_MS + i * 1000;
    const t = teamsAloneTick(state, reading({ humanEvidenceAgeMs: age }), timeouts);
    state = t.state;
    if (t.shouldLeave) { leftAt = i; break; }
  }
  assertEqual(
    leftAt,
    TEAMS_DEFAULT_EVERYONE_LEFT_MS / TEAMS_POLL_MS,
    'unreadable DOM past the stale bound still terminates (never-leaves failure closed)'
  );
}

// ═══════════════════════════════════════════════════════════════════════════
// readTeamsLeaveTimeouts — the field names are the bug class
// ═══════════════════════════════════════════════════════════════════════════

// 13. The orchestrator's field names, in ms, are honoured verbatim.
{
  const t = readTeamsLeaveTimeouts({ noOneJoinedTimeout: 300_000, everyoneLeftTimeout: 45_000 });
  assertEqual(t.noOneJoinedMs, 300_000, 'noOneJoinedTimeout is read as milliseconds');
  assertEqual(t.everyoneLeftMs, 45_000, 'everyoneLeftTimeout is read as milliseconds');
}

// 14. The legacy seconds fields still work, and are converted.
{
  const t = readTeamsLeaveTimeouts({ startupAloneTimeoutSeconds: 90, everyoneLeftTimeoutSeconds: 30 });
  assertEqual(t.noOneJoinedMs, 90_000, 'startupAloneTimeoutSeconds converts s → ms');
  assertEqual(t.everyoneLeftMs, 30_000, 'everyoneLeftTimeoutSeconds converts s → ms');
}

// 15. ms wins over s when both are present (today's precedence, preserved).
{
  const t = readTeamsLeaveTimeouts({
    noOneJoinedTimeout: 111_000,
    startupAloneTimeoutSeconds: 999,
    everyoneLeftTimeout: 22_000,
    everyoneLeftTimeoutSeconds: 999,
  });
  assertEqual([t.noOneJoinedMs, t.everyoneLeftMs], [111_000, 22_000], 'ms fields take precedence over s fields');
}

// 16. Absent / unusable config falls back to the documented defaults. This is
//     the live case today: _build_bot_config does not send noOneJoinedTimeout.
{
  for (const [label, cfg] of [
    ['undefined', undefined],
    ['null', null],
    ['empty object', {}],
    ['zeroes', { noOneJoinedTimeout: 0, everyoneLeftTimeout: 0 }],
    ['NaN', { noOneJoinedTimeout: NaN, everyoneLeftTimeout: NaN }],
    ['negative', { noOneJoinedTimeout: -5, everyoneLeftTimeout: -5 }],
    ['wrong types', { noOneJoinedTimeout: 'soon', everyoneLeftTimeout: {} }],
  ] as [string, unknown][]) {
    const t = readTeamsLeaveTimeouts(cfg);
    assertEqual(
      [t.noOneJoinedMs, t.everyoneLeftMs],
      [TEAMS_DEFAULT_NO_ONE_JOINED_MS, TEAMS_DEFAULT_EVERYONE_LEFT_MS],
      `unusable automaticLeave (${label}) → documented defaults`
    );
  }
}

// 17. Numeric strings are accepted (JSON config has produced these before).
{
  const t = readTeamsLeaveTimeouts({ noOneJoinedTimeout: '300000', everyoneLeftTimeoutSeconds: '30' });
  assertEqual([t.noOneJoinedMs, t.everyoneLeftMs], [300_000, 30_000], 'numeric strings are coerced');
}

// 18. The constants the page's decision depends on.
{
  assertEqual(
    [TEAMS_POLL_MS, TEAMS_PRESENCE_WINDOW_MS, TEAMS_STALE_EVIDENCE_MS],
    [1000, 60_000, 900_000],
    'poll / presence-window / stale-bound constants'
  );
  const t = readTeamsLeaveTimeouts({});
  assertEqual(
    [t.pollMs, t.presenceWindowMs, t.staleEvidenceMs],
    [TEAMS_POLL_MS, TEAMS_PRESENCE_WINDOW_MS, TEAMS_STALE_EVIDENCE_MS],
    'the resolved timeouts carry those constants through'
  );
}

// ═══════════════════════════════════════════════════════════════════════════
// resolveBotDisplayName — botName, not name
// ═══════════════════════════════════════════════════════════════════════════

// 19. The orchestrator sets `botName`. `botConfigData?.name` — what recording.ts
//     read before — is undefined on a real BotConfig.
{
  assertEqual(resolveBotDisplayName({ botName: 'AW Notetaker' }), 'AW Notetaker', 'botName is read');
  assertEqual(
    resolveBotDisplayName({ name: 'AW Notetaker' }),
    'AW Notetaker',
    'the legacy `name` still works as a fallback'
  );
  assertEqual(
    resolveBotDisplayName({ botName: 'Real', name: 'Legacy' }),
    'Real',
    'botName wins over name'
  );
  assertEqual(resolveBotDisplayName({ botName: '  Padded  ' }), 'Padded', 'the value is trimmed');
  for (const [label, cfg] of [
    ['undefined', undefined],
    ['null', null],
    ['empty object', {}],
    ['blank botName', { botName: '   ' }],
    ['non-string', { botName: 42 }],
  ] as [string, unknown][]) {
    assertEqual(resolveBotDisplayName(cfg), '', `no usable name (${label}) → '' (caller falls back to 'vexa')`);
  }
  assertEqual(
    resolveBotDisplayName({ botName: '   ', name: 'Legacy' }),
    'Legacy',
    'a blank botName does not shadow a usable name'
  );
}

// ═══════════════════════════════════════════════════════════════════════════
// buildCaptionSpeakerEventFields — caption provenance on the wire
// ═══════════════════════════════════════════════════════════════════════════

// 20. The tag is what the adapter's Teams branch keys on.
{
  const f = buildCaptionSpeakerEventFields({
    uid: 'conn-1',
    meetingId: '77',
    participantName: 'Alice Smith',
    relativeMs: 12_345.6,
  });
  assertEqual(f, {
    uid: 'conn-1',
    relative_client_timestamp_ms: '12346',
    event_type: 'SPEAKER_START',
    participant_name: 'Alice Smith',
    meeting_id: '77',
    source: 'caption',
  }, 'caption boundary fields, source-tagged and rounded');
  assertEqual(CAPTION_EVENT_SOURCE, 'caption', 'the provenance literal the adapter matches');
}

// 21. It emits SPEAKER_START only — never an END, never source 'audio'. Tagging
//     these pairable would have the adapter open an interval that only closes
//     at the session boundary, erasing every other speaker.
{
  const f = buildCaptionSpeakerEventFields({ uid: 'u', meetingId: '1', participantName: 'Bob', relativeMs: 0 })!;
  assertEqual(f.event_type, 'SPEAKER_START', 'always SPEAKER_START');
  assertEqual(f.source !== 'audio', true, "never claims source 'audio'");
}

// 22. Unusable input yields null rather than a degraded row: the adapter reads
//     participant_name straight into speaker_timeline.json.
{
  const cases: [string, Parameters<typeof buildCaptionSpeakerEventFields>[0]][] = [
    ['empty name', { uid: 'u', meetingId: '1', participantName: '', relativeMs: 5 }],
    ['blank name', { uid: 'u', meetingId: '1', participantName: '   ', relativeMs: 5 }],
    ['missing uid', { uid: '', meetingId: '1', participantName: 'Alice', relativeMs: 5 }],
    ['NaN offset', { uid: 'u', meetingId: '1', participantName: 'Alice', relativeMs: NaN }],
    ['Infinity offset', { uid: 'u', meetingId: '1', participantName: 'Alice', relativeMs: Infinity }],
  ];
  for (const [label, input] of cases) {
    assertEqual(buildCaptionSpeakerEventFields(input), null, `unusable input (${label}) → null`);
  }
}

// 23. Negative offsets clamp to 0. A negative would sort ahead of every
//     transcript segment and capture the opening of the meeting.
{
  const f = buildCaptionSpeakerEventFields({ uid: 'u', meetingId: '1', participantName: 'Alice', relativeMs: -4_000 })!;
  assertEqual(f.relative_client_timestamp_ms, '0', 'a negative offset clamps to 0');
}

// 24. The name is trimmed but otherwise verbatim — the adapter slugs it, we
//     must not normalise it here or two producers disagree about one person.
{
  const f = buildCaptionSpeakerEventFields({ uid: 'u', meetingId: '1', participantName: '  José Núñez ', relativeMs: 1 })!;
  assertEqual(f.participant_name, 'José Núñez', 'name is trimmed, not otherwise altered');
}

// ---------------------------------------------------------------------------
// rosterEverReadable — the 2026-09-07 live finding
//
// A 30-min Teams meeting kept the bot alive ~19 min past the end (15 min stale
// + 2 min everyone-left), uploading ~60 silent chunks. Mid-meeting the reading
// was `roster=1` (READABLE); it became `roster=unreadable` only once everyone
// left. So a null roster after a readable one is DEPARTURE, not blindness.
// ---------------------------------------------------------------------------
{
  const timeouts: TeamsAloneTimeouts = {
    pollMs: 1_000,
    everyoneLeftMs: 120_000,
    noOneJoinedMs: 600_000,
    presenceWindowMs: 60_000,
    staleEvidenceMs: 900_000,
  };
  const rd = (o: Partial<TeamsPresenceReading> = {}): TeamsPresenceReading => ({
    rosterHumans: null, signalTiles: 0, humanEvidenceAgeMs: null, ...o,
  });

  // 1. the latch sets when the roster is readable, and never clears
  let st: AloneTimerState = { aloneMs: 0, sawOthers: false };
  st = teamsAloneTick(st, rd({ rosterHumans: 2, humanEvidenceAgeMs: 1_000 }), timeouts).state;
  assertEqual(st.rosterEverReadable, true, 'latch sets on a readable roster');
  st = teamsAloneTick(st, rd({ humanEvidenceAgeMs: 200_000 }), timeouts).state;
  assertEqual(st.rosterEverReadable, true, 'latch survives a later null roster');

  // 2. THE FIX: readable-then-gone leaves in ~4 min, not ~17
  let s2: AloneTimerState = { aloneMs: 0, sawOthers: false };
  s2 = teamsAloneTick(s2, rd({ rosterHumans: 2, humanEvidenceAgeMs: 1_000 }), timeouts).state;
  let leftAt = -1;
  for (let i = 1; i <= 1200; i++) {
    // roster gone, no tiles, evidence ageing past the SHORT bound
    const t = teamsAloneTick(s2, rd({ humanEvidenceAgeMs: 60_000 + i * 1_000 }), timeouts);
    s2 = t.state;
    if (t.shouldLeave) { leftAt = i; break; }
  }
  assertEqual(leftAt > 0 && leftAt <= 200, true, `leaves within ~200s of roster loss (got ${leftAt})`);

  // 3. REGRESSION GUARD: never-readable roster keeps the LONG hold
  let s3: AloneTimerState = { aloneMs: 0, sawOthers: true };
  let left3 = -1;
  for (let i = 1; i <= 400; i++) {
    const t = teamsAloneTick(s3, rd({ humanEvidenceAgeMs: 60_000 + i * 1_000 }), timeouts);
    s3 = t.state;
    if (t.shouldLeave) { left3 = i; break; }
  }
  assertEqual(s3.rosterEverReadable, false, 'never-readable roster does not latch');
  assertEqual(left3, -1, 'never-readable roster still holds past 400s (long bound intact)');

  // 4. live evidence still wins over the short bound - a quiet-but-present
  //    meeting must not be abandoned
  let s4: AloneTimerState = { aloneMs: 0, sawOthers: false };
  s4 = teamsAloneTick(s4, rd({ rosterHumans: 3, humanEvidenceAgeMs: 1_000 }), timeouts).state;
  let left4 = -1;
  for (let i = 1; i <= 600; i++) {
    const t = teamsAloneTick(s4, rd({ humanEvidenceAgeMs: 5_000 }), timeouts);
    s4 = t.state;
    if (t.shouldLeave) { left4 = i; break; }
  }
  assertEqual(left4, -1, 'recent captions keep the bot in, latch or no latch');
}

// ---------------------------------------------------------------------------
// SOURCE GUARD: the Teams caption publish must carry source: 'caption'
//
// `handleTeamsCaptionData` lives in index.ts, a ~2,600-line orchestration file
// with no unit-test seam, so this is a static assertion over the source text --
// the same approach teams-bot's suite uses for its Dockerfile.
//
// Why it is worth pinning: on 2026-09-07 a live 3-person meeting put 707 named
// speaker events into Redis and only 10 carried the tag, because this call site
// published untagged. aw-integration's Teams preference selects on
// `source == "caption"`, so it saw 10 of 707 and was effectively inert, while
// the DOM fallback was emitting ONE anonymous `Teams Participant (<uuid>)` for
// all three humans. Untagged is fail-safe, never correct.
// ---------------------------------------------------------------------------
{
  // Resolved from the repo layout rather than `import.meta.url`: this package's
  // tsconfig `module` target predates import.meta, and tsc rejects it (tsx does
  // not, which is exactly how a type error can hide behind a green suite).
  const src = readFileSync('src/index.ts', 'utf8');
  const i = src.indexOf("type: 'started_speaking'");
  assertEqual(i > 0, true, "index.ts still has the 'started_speaking' caption publish");
  // the source field must appear inside this call, before the closing brace
  const call = src.slice(Math.max(0, i - 400), i + 200);
  assertEqual(
    /source:\s*'caption'/.test(call),
    true,
    "the Teams caption publish carries source: 'caption'"
  );
}

console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) {
  process.exit(1);
}
