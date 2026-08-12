/**
 * Audio-derived speaker boundaries — state-machine tests.
 *
 * Run: npx tsx services/vexa-bot/core/src/services/speaker-boundaries.test.ts
 *
 * This state machine used to live inside `index.ts`, which imports
 * `playwright-extra` at module scope and so cannot be loaded by a plain `tsx`
 * run. It was therefore completely untested, and two defects reached production
 * and were only caught by eyeballing a live meeting's logs:
 *
 *   1. Duplicate STARTs at a STALE onset. The END branch cleared `startEmitted`
 *      but left `onsetMs`, and the retroactive-START block did not check
 *      `speaking` — so the next 200ms tick re-published START at the previous
 *      utterance's onset. 48 of 429 events in the live timeline were duplicates.
 *   2. `END "null"` — the END log line read `st.emittedName` AFTER awaiting the
 *      publish, so a concurrent `markTrackAudioActivity` that reset the state
 *      mid-publish made the log stringify a `null`.
 *
 * Both are regression-tested below (cases 1, 2 and 6). Time is injected, so the
 * hangover is driven with a fake clock — no sleeps, fully deterministic.
 */

import {
  createSpeakerBoundaryTracker,
  sanitizeResolvedName,
  SpeakerBoundaryType,
  SpeakerBoundaryTracker,
} from './speaker-boundaries';

// ─── Test harness ────────────────────────────────────────────────────────────

let passed = 0;
let failed = 0;

function check(label: string, actual: unknown, expected: unknown): void {
  const ok = JSON.stringify(actual) === JSON.stringify(expected);
  if (ok) {
    passed++;
    console.log(`  ✅ ${label}`);
  } else {
    failed++;
    console.log(`  ❌ ${label}\n       expected: ${JSON.stringify(expected)}\n       actual:   ${JSON.stringify(actual)}`);
  }
}

function checkTrue(label: string, actual: boolean): void {
  check(label, actual, true);
}

/** One published boundary, captured. */
interface Published {
  type: SpeakerBoundaryType;
  speaker: string;
  timestampMs: number;
}

interface Rig {
  tracker: SpeakerBoundaryTracker;
  /** Everything the machine published, in publish order. */
  events: Published[];
  /** Every diagnostic line the machine logged. */
  logs: string[];
  /** Advance the fake clock. */
  advance(ms: number): void;
  /** Current fake time. */
  clock(): number;
  /** Set the name `resolveName` will return for a track ('' = unmapped). */
  setName(trackIndex: number, name: string): void;
  /** Make the next `publish` calls resolve only when `releasePublish()` is called. */
  blockPublish(): void;
  releasePublish(): void;
  /** Convenience: `${type}@${timestampMs}` per event, for compact assertions. */
  shape(): string[];
}

const T0 = 1_000_000; // arbitrary fake epoch

function makeRig(opts: { hangoverMs?: number } = {}): Rig {
  let nowMs = T0;
  const events: Published[] = [];
  const logs: string[] = [];
  const names = new Map<number, string>();

  let gate: Promise<void> | null = null;
  let openGate: (() => void) | null = null;

  const rig: Rig = {
    tracker: createSpeakerBoundaryTracker({
      hangoverMs: opts.hangoverMs ?? 700,
      now: () => nowMs,
      log: (m) => { logs.push(m); },
      resolveName: (idx) => names.get(idx) ?? '',
      publish: async (type, speaker, timestampMs) => {
        if (gate) await gate;
        events.push({ type, speaker, timestampMs });
      },
    }),
    events,
    logs,
    advance: (ms) => { nowMs += ms; },
    clock: () => nowMs,
    setName: (idx, name) => { names.set(idx, name); },
    blockPublish: () => {
      gate = new Promise<void>((resolve) => { openGate = resolve; });
    },
    releasePublish: () => {
      gate = null;
      openGate?.();
      openGate = null;
    },
    shape: () => events.map((e) => `${e.type}@${e.timestampMs}`),
  };
  return rig;
}

/** Speak on `track` for `durationMs`, feeding activity every 100ms. */
async function speak(rig: Rig, track: number, durationMs: number): Promise<void> {
  const step = 100;
  for (let elapsed = 0; elapsed < durationMs; elapsed += step) {
    rig.tracker.markTrackAudioActivity(track, rig.clock());
    await rig.tracker.sweep();
    rig.advance(step);
  }
}

/** Run sweeps across `durationMs` of silence at the production 200ms cadence. */
async function stayQuiet(rig: Rig, durationMs: number): Promise<void> {
  const step = 200;
  for (let elapsed = 0; elapsed < durationMs; elapsed += step) {
    rig.advance(step);
    await rig.tracker.sweep();
  }
}

// ─── Tests ───────────────────────────────────────────────────────────────────

async function run(): Promise<void> {
  console.log('\n=== speaker-boundaries ===');

  // ── 1. Defect 1 regression: no spurious duplicate START at a stale onset ──
  //
  // THE load-bearing test. Pre-fix, the retroactive-START block did not check
  // `st.speaking`, so the tick after an END re-published START at the ORIGINAL
  // (now stale) onset — and kept the track "started" forever while silent.
  console.log('\nDefect 1 — duplicate START at a stale onset:');
  {
    const rig = makeRig();
    rig.tracker.arm();
    rig.setName(0, 'Speaker A');

    const onset = rig.clock();
    await speak(rig, 0, 1_000);
    const lastAudio = rig.clock() - 100; // last markTrackAudioActivity timestamp

    // Long silence: 3s at the production cadence = 15 sweeps after the END.
    await stayQuiet(rig, 3_000);

    check('exactly one START and one END for a single utterance', rig.shape(), [
      `started_speaking@${onset}`,
      `stopped_speaking@${lastAudio}`,
    ]);
    check('no event repeats the stale onset', rig.events.filter((e) => e.timestampMs === onset).length, 1);
    check(
      'no START is re-published across 15 silent sweeps',
      rig.events.filter((e) => e.type === 'started_speaking').length,
      1,
    );
  }

  // ── 2. A new utterance after silence uses the NEW onset ──────────────────
  console.log('\nNew utterance after silence:');
  {
    const rig = makeRig();
    rig.tracker.arm();
    rig.setName(0, 'Alice');

    const onset1 = rig.clock();
    await speak(rig, 0, 500);
    const lastAudio1 = rig.clock() - 100;
    await stayQuiet(rig, 2_000);

    const onset2 = rig.clock();
    await speak(rig, 0, 500);
    const lastAudio2 = rig.clock() - 100;
    await stayQuiet(rig, 2_000);

    check('two utterances yield START/END/START/END', rig.shape(), [
      `started_speaking@${onset1}`,
      `stopped_speaking@${lastAudio1}`,
      `started_speaking@${onset2}`,
      `stopped_speaking@${lastAudio2}`,
    ]);
    checkTrue('the second START uses the new onset, not the stale one', onset2 > lastAudio1);
    check('every event names the same speaker', [...new Set(rig.events.map((e) => e.speaker))], ['Alice']);
  }

  // ── 3. Retroactive START preserves the TRUE onset ────────────────────────
  console.log('\nRetroactive START (name resolves late):');
  {
    const rig = makeRig();
    rig.tracker.arm();
    // Deliberately unmapped at onset — this is the normal GMeet case, where
    // vote-and-lock needs a few seconds to settle.
    rig.setName(0, '');

    const trueOnset = rig.clock();
    await speak(rig, 0, 600); // 6 sweeps, no name → nothing published
    check('nothing published while the track is unmapped', rig.events.length, 0);

    const resolutionTime = rig.clock();
    rig.setName(0, 'Bob');
    rig.tracker.markTrackAudioActivity(0, rig.clock());
    await rig.tracker.sweep();

    check('START published once the name resolves', rig.events.length, 1);
    check('START carries the ORIGINAL onset, not the resolution time', rig.events[0].timestampMs, trueOnset);
    checkTrue('resolution happened strictly after the onset', resolutionTime > trueOnset);
  }

  // ── 4. Arming gate ───────────────────────────────────────────────────────
  console.log('\nArming gate:');
  {
    const rig = makeRig();
    rig.setName(0, 'Carol');

    const onset = rig.clock();
    await speak(rig, 0, 600); // unarmed
    check('nothing published before arm()', rig.events.length, 0);
    checkTrue(
      'the unarmed warning is logged',
      rig.logs.some((l) => l.includes('audio activity seen but boundaries are NOT armed')),
    );
    check(
      'the unarmed warning is logged exactly once, not per sweep',
      rig.logs.filter((l) => l.includes('NOT armed')).length,
      1,
    );
    check('state still accrued while unarmed', rig.tracker.trackCount(), 1);

    rig.tracker.arm();
    await rig.tracker.sweep();
    check('the buffered onset flushes after arming', rig.events.length, 1);
    check('the flushed START keeps the pre-arm onset', rig.events[0].timestampMs, onset);
  }

  // ── 5. Teardown ──────────────────────────────────────────────────────────
  console.log('\nTeardown:');
  {
    const rig = makeRig();
    rig.tracker.arm();
    rig.setName(0, 'Dave');

    const onset = rig.clock();
    await speak(rig, 0, 400);
    const lastAudio = rig.clock() - 100;
    await rig.tracker.stopSweep(); // meeting ends mid-utterance

    check('armed teardown flushes START then END', rig.shape(), [
      `started_speaking@${onset}`,
      `stopped_speaking@${lastAudio}`,
    ]);
    check('END uses lastAudioMs, not teardown time', rig.events[1].timestampMs, lastAudio);
    check('teardown clears all track state', rig.tracker.trackCount(), 0);
    check('teardown disarms', rig.tracker.isArmed(), false);
  }
  {
    const rig = makeRig();
    rig.setName(0, 'Erin'); // never armed — bot never admitted
    await speak(rig, 0, 400);
    await rig.tracker.stopSweep();

    check('never-armed teardown publishes NOTHING', rig.events.length, 0);
    checkTrue(
      'never-armed teardown says it discarded state',
      rig.logs.some((l) => l.includes('Discarding') && l.includes('recording never started')),
    );
    check('never-armed teardown clears state', rig.tracker.trackCount(), 0);
  }

  // ── 6. Defect 2 regression: a stringified-nullish name never ships ───────
  console.log('\nDefect 2 — stringified-nullish names:');
  {
    check('sanitizeResolvedName rejects "null"', sanitizeResolvedName('null'), '');
    check('sanitizeResolvedName rejects "undefined"', sanitizeResolvedName('undefined'), '');
    check('sanitizeResolvedName rejects a real null', sanitizeResolvedName(null), '');
    check('sanitizeResolvedName rejects " NULL "', sanitizeResolvedName(' NULL '), '');
    check('sanitizeResolvedName keeps a real name', sanitizeResolvedName('  Nullson  '), 'Nullson');

    for (const junk of ['null', 'undefined']) {
      const rig = makeRig();
      rig.tracker.arm();
      rig.setName(0, junk);
      await speak(rig, 0, 600);
      await stayQuiet(rig, 2_000);

      check(`resolveName()="${junk}" publishes no event at all`, rig.events.length, 0);
      check(
        `no event is named "${junk}"`,
        rig.events.filter((e) => e.speaker === junk).length,
        0,
      );
      checkTrue(
        `the "${junk}" utterance is reported as dropped, not published`,
        rig.logs.some((l) => l.includes('no name resolved before it ended')),
      );
      check(
        `no log line renders END "${junk}"`,
        rig.logs.filter((l) => l.includes(`END "${junk}"`)).length,
        0,
      );
    }
  }
  {
    // The production mechanism: state is reset by a concurrent
    // markTrackAudioActivity WHILE the END publish is in flight. Pre-fix the log
    // line read st.emittedName after that await and rendered `END "null"`.
    const rig = makeRig();
    rig.tracker.arm();
    rig.setName(0, 'Frank');

    const onset = rig.clock();
    await speak(rig, 0, 400);
    const lastAudio = rig.clock() - 100;

    rig.blockPublish();
    rig.advance(1_000);
    const sweeping = rig.tracker.sweep(); // decides END, then blocks in publish
    await Promise.resolve();
    // Audio arrives mid-publish, exactly as it does in production.
    const newOnset = rig.clock();
    rig.tracker.markTrackAudioActivity(0, newOnset);
    rig.releasePublish();
    await sweeping;

    check('END still names the speaker its START used', rig.events.map((e) => e.speaker), ['Frank', 'Frank']);
    check(
      'no log line renders a nullish name',
      rig.logs.filter((l) => l.includes('"null"') || l.includes('"undefined"')).length,
      0,
    );
    checkTrue('the END log names the real speaker', rig.logs.some((l) => l.includes('END "Frank"')));
    check('END still carries lastAudioMs', rig.events[1].timestampMs, lastAudio);
    checkTrue('the mid-publish audio opened a fresh onset', newOnset > onset);
  }

  // ── 7. END matches the name its START was published under ────────────────
  console.log('\nEND/START name agreement:');
  {
    const rig = makeRig();
    rig.tracker.arm();
    rig.setName(0, 'Grace');

    await speak(rig, 0, 300);
    // Vote-and-lock changes its mind mid-utterance (participant count changed).
    rig.setName(0, 'Heidi');
    await speak(rig, 0, 300);
    await stayQuiet(rig, 2_000);

    check('both boundaries use the START-time name', rig.events.map((e) => e.speaker), ['Grace', 'Grace']);
    check('one pair, not two', rig.shape().length, 2);
    // The NEXT utterance is free to pick up the new name.
    await speak(rig, 0, 300);
    await stayQuiet(rig, 2_000);
    check('the next utterance adopts the new name', rig.events.slice(2).map((e) => e.speaker), ['Heidi', 'Heidi']);
  }

  // ── 8. Re-entrancy: overlapping sweeps must not double-emit ──────────────
  console.log('\nRe-entrancy (slow publish, sweep driven concurrently):');
  {
    const rig = makeRig();
    rig.tracker.arm();
    rig.setName(0, 'Ivan');

    const onset = rig.clock();
    rig.tracker.markTrackAudioActivity(0, onset);

    rig.blockPublish();
    // Fire four sweeps while the first is still stuck inside publish — exactly
    // what `setInterval(() => void sweep(), 200)` does when Redis is slow.
    const inflight = [
      rig.tracker.sweep(),
      rig.tracker.sweep(),
      rig.tracker.sweep(),
      rig.tracker.sweep(),
    ];
    rig.releasePublish();
    await Promise.all(inflight);

    check('a single START despite four overlapping sweeps', rig.shape(), [`started_speaking@${onset}`]);

    // And the END side, likewise overlapped.
    rig.advance(1_000);
    rig.blockPublish();
    const closing = [rig.tracker.sweep(), rig.tracker.sweep(), rig.tracker.sweep()];
    rig.releasePublish();
    await Promise.all(closing);

    check('a single END despite three overlapping sweeps', rig.shape(), [
      `started_speaking@${onset}`,
      `stopped_speaking@${onset}`,
    ]);
  }

  // ── 9. Multi-track independence (the live 3-speaker case) ────────────────
  console.log('\nMulti-track:');
  {
    const rig = makeRig();
    rig.tracker.arm();
    rig.setName(0, 'Alice');
    rig.setName(1, 'Bob');

    const onsetA = rig.clock();
    await speak(rig, 0, 400);
    const lastA = rig.clock() - 100;
    const onsetB = rig.clock();
    await speak(rig, 1, 400);
    const lastB = rig.clock() - 100;
    await stayQuiet(rig, 2_000);

    check('each track gets its own pair', rig.events.length, 4);
    check(
      'track 0 pair is correct',
      rig.events.filter((e) => e.speaker === 'Alice').map((e) => `${e.type}@${e.timestampMs}`),
      [`started_speaking@${onsetA}`, `stopped_speaking@${lastA}`],
    );
    check(
      'track 1 pair is correct',
      rig.events.filter((e) => e.speaker === 'Bob').map((e) => `${e.type}@${e.timestampMs}`),
      [`started_speaking@${onsetB}`, `stopped_speaking@${lastB}`],
    );
    check(
      'no duplicate timestamps anywhere',
      rig.events.length - new Set(rig.events.map((e) => `${e.speaker}|${e.type}|${e.timestampMs}`)).size,
      0,
    );
  }

  // ── 10. A failing publish must not silence the machine ───────────────────
  console.log('\nPublish failure:');
  {
    const events: Published[] = [];
    const logs: string[] = [];
    let nowMs = T0;
    let failNext = true;
    const tracker = createSpeakerBoundaryTracker({
      hangoverMs: 700,
      now: () => nowMs,
      log: (m) => { logs.push(m); },
      resolveName: () => 'Judy',
      publish: async (type, speaker, timestampMs) => {
        if (failNext) { failNext = false; throw new Error('redis down'); }
        events.push({ type, speaker, timestampMs });
      },
    });
    tracker.arm();
    tracker.markTrackAudioActivity(0, nowMs);
    await tracker.sweep();               // START publish throws
    nowMs += 1_000;
    await tracker.sweep();               // END must still be attempted

    check('the failed START is not recorded', events.length, 1);
    check('the END still published', events[0].type, 'stopped_speaking');
    checkTrue('the failure is logged', logs.some((l) => l.includes('Failed to publish started_speaking')));
    checkTrue(
      'no success line is logged for the failed publish',
      !logs.some((l) => l.includes('START "Judy"')),
    );
  }

  // ── 11. A throwing resolveName must not crash the sweep ──────────────────
  console.log('\nResolver failure:');
  {
    const logs: string[] = [];
    let nowMs = T0;
    let boom = true;
    const events: Published[] = [];
    const tracker = createSpeakerBoundaryTracker({
      hangoverMs: 700,
      now: () => nowMs,
      log: (m) => { logs.push(m); },
      resolveName: () => {
        if (boom) throw new Error('speakerManager torn down');
        return 'Karl';
      },
      publish: async (type, speaker, timestampMs) => { events.push({ type, speaker, timestampMs }); },
    });
    tracker.arm();
    tracker.markTrackAudioActivity(0, nowMs);
    await tracker.sweep(); // resolver throws — must be swallowed
    check('nothing published when the resolver throws', events.length, 0);
    checkTrue('the resolver failure is logged', logs.some((l) => l.includes('name lookup failed')));

    boom = false;
    nowMs += 100;
    tracker.markTrackAudioActivity(0, nowMs);
    await tracker.sweep();
    check('the machine recovers on the next sweep', events.length, 1);
  }

  console.log(`\n=== ${passed} passed, ${failed} failed ===\n`);
  if (failed > 0) process.exit(1);
}

run().catch((err) => {
  console.error('Test harness crashed:', err);
  process.exit(1);
});
