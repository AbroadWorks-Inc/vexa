/**
 * stepAloneTimer — Zoom left-alone / empty-room timer unit tests.
 *
 * Run: npx tsx services/vexa-bot/core/src/platforms/zoom/web/alone-timer.test.ts
 *
 * Pure logic (no DOM), extracted from removal.ts so the exit-triggering
 * left-alone decision is covered: reset-on-recovery, threshold crossing,
 * no-one-joined vs everyone-left thresholds, and the unreadable (count 0) hold.
 * Matches pcm-chunker.test.ts's standalone-harness convention (no vitest).
 */

import { stepAloneTimer, AloneTimerState } from './alone-timer';

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

console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) {
  process.exit(1);
}
