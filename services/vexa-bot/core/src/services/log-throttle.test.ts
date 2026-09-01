/**
 * log-throttle — collapse repeated diagnostics without losing information.
 *
 * Run: npx tsx services/vexa-bot/core/src/services/log-throttle.test.ts
 *
 * Exists because the live Zoom run of 2026-09-01 emitted 278 copies of one
 * content-free line (222 of them for a single track). The requirement is not
 * "log less" — it is "a stuck failure produces a countable heartbeat, a failure
 * whose CAUSE changes speaks up immediately, and nothing is silently lost".
 */

import { REPEAT_HEARTBEAT_DEFAULT_MS, createRepeatCollapser } from './log-throttle';

let passed = 0;
let failed = 0;

function check(label: string, actual: unknown, expected: unknown): void {
  if (JSON.stringify(actual) === JSON.stringify(expected)) {
    passed++;
    console.log(`  ✅ ${label}`);
  } else {
    failed++;
    console.log(`  ❌ ${label}\n       expected: ${JSON.stringify(expected)}\n       actual:   ${JSON.stringify(actual)}`);
  }
}

function run(): void {
  console.log('\n=== log-throttle ===');

  console.log('\nThe live shape — 278 identical polls:');
  {
    // Reproduce the real cadence: the bot polled roughly every 500ms for ~2min.
    const c = createRepeatCollapser(30_000);
    const emitted: string[] = [];
    for (let i = 0; i < 278; i++) {
      const line = c.consider('miss:2', 'sig-A', 'track 2 UNRESOLVED', i * 500);
      if (line) emitted.push(line);
    }
    // 278 polls over 139s at a 30s heartbeat: the first plus one per 30s window.
    check('278 identical occurrences collapse to a handful of lines', emitted.length, 5);
    const emittedBeforeFlush = emitted.length;
    check('the first occurrence is emitted verbatim, unadorned', emitted[0], 'track 2 UNRESOLVED');
    check(
      'every heartbeat after the first accounts for what it hid',
      emitted.slice(1).every((l) => /\[\+\d+ identical occurrence\(s\) suppressed\]$/.test(l)),
      true,
    );
    // Nothing is lost: after the final flush, the emitted lines plus every
    // suppression count must add up to exactly the occurrences considered. The
    // flush is what closes the books on the run still outstanding at the end —
    // without it the tail would vanish silently, which is the failure mode this
    // whole module exists to avoid.
    const tail = c.flush('miss:2', 278 * 500);
    if (tail) emitted.push(tail);
    const counted = emitted.reduce((sum, l) => {
      const m = l.match(/\[\+(\d+) identical/);
      return sum + (m ? Number(m[1]) : 0);
    }, 0);
    // `emittedBeforeFlush` and not `emitted.length`: the flush line is a closing
    // report, not itself an occurrence.
    check('emitted lines + suppressed counts account for all 278', counted + emittedBeforeFlush, 278);
    check('the flush added exactly one closing line', emitted.length - emittedBeforeFlush, 1);
  }

  console.log('\nA changed cause speaks up immediately:');
  {
    const c = createRepeatCollapser(30_000);
    check('first occurrence emits', c.consider('k', 'sig-A', 'msg A', 0), 'msg A');
    check('same signature inside the window is swallowed', c.consider('k', 'sig-A', 'msg A', 100), null);
    check('same signature again is swallowed', c.consider('k', 'sig-A', 'msg A', 200), null);
    // The situation moved — this is the diagnostically interesting moment, and it
    // must NOT wait for the heartbeat.
    check(
      'a changed signature emits at once, carrying the suppressed count',
      c.consider('k', 'sig-B', 'msg B', 300),
      'msg B [+2 identical occurrence(s) suppressed]',
    );
    check('the new signature then throttles in its own right', c.consider('k', 'sig-B', 'msg B', 400), null);
  }

  console.log('\nHeartbeat boundary:');
  {
    const c = createRepeatCollapser(1_000);
    c.consider('k', 's', 'm', 0);
    check('just before the gap: swallowed', c.consider('k', 's', 'm', 999), null);
    check('exactly at the gap: emitted', c.consider('k', 's', 'm', 1_000), 'm [+1 identical occurrence(s) suppressed]');
    check('the window restarts from the emit, not from time zero', c.consider('k', 's', 'm', 1_999), null);
    check('and closes again a full gap later', c.consider('k', 's', 'm', 2_000), 'm [+1 identical occurrence(s) suppressed]');
  }

  console.log('\nStreams are independent:');
  {
    const c = createRepeatCollapser(30_000);
    check('track 0 first occurrence emits', c.consider('miss:0', 's', 'track 0', 0), 'track 0');
    // The real bug this guards: one chatty track must not silence another. Live,
    // track 2 out-logged track 0 four to one.
    check('track 2 first occurrence still emits', c.consider('miss:2', 's', 'track 2', 0), 'track 2');
    check('track 0 then throttles', c.consider('miss:0', 's', 'track 0', 10), null);
    check('track 2 throttles separately', c.consider('miss:2', 's', 'track 2', 10), null);
  }

  console.log('\nFlush and reset:');
  {
    const c = createRepeatCollapser(30_000);
    c.consider('k', 's', 'm', 0);
    c.consider('k', 's', 'm', 1);
    c.consider('k', 's', 'm', 2);
    check('flush surfaces the outstanding suppressed run', c.flush('k', 3), 'm [+2 identical occurrence(s) suppressed]');
    check('flushing again yields nothing (the stream is gone)', c.flush('k', 4), null);
    check('after a flush the stream starts over', c.consider('k', 's', 'm', 5), 'm');
  }
  {
    const c = createRepeatCollapser(30_000);
    c.consider('k', 's', 'm', 0);
    check('nothing suppressed ⇒ flush is silent, not a duplicate line', c.flush('k', 1), null);
  }
  {
    const c = createRepeatCollapser(30_000);
    c.consider('k', 's', 'm', 0);
    check('after reset the next occurrence is treated as the first', (c.reset(), c.consider('k', 's', 'm', 1)), 'm');
  }

  console.log('\nDegenerate configuration cannot silently disable throttling:');
  {
    // A zero or negative gap must not make every occurrence emit (that is the
    // 278-line behaviour) — it is clamped to 1ms.
    const zero = createRepeatCollapser(0);
    zero.consider('k', 's', 'm', 0);
    check('a 0ms gap still swallows a same-millisecond repeat', zero.consider('k', 's', 'm', 0), null);
    check('a 0ms gap emits 1ms later', zero.consider('k', 's', 'm', 1), 'm [+1 identical occurrence(s) suppressed]');
    const neg = createRepeatCollapser(-5_000);
    neg.consider('k', 's', 'm', 0);
    check('a negative gap swallows a same-millisecond repeat too', neg.consider('k', 's', 'm', 0), null);
  }
  check('the default heartbeat is 30s', REPEAT_HEARTBEAT_DEFAULT_MS, 30_000);

  console.log(`\n=== ${passed} passed, ${failed} failed ===\n`);
  if (failed > 0) process.exit(1);
}

run();
