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
 *
 * A third defect — measured live, not hypothetical — is tested in section 12:
 * an utterance that ends with NO name ever resolved used to be silently
 * DISCARDED, so a track that never resolves hands all of its speech to
 * whichever other track's name is known, asserting a false attribution rather
 * than an honest unknown. The fix emits it under a stable per-track synthetic
 * identity (`syntheticSpeakerLabel`) instead of dropping it.
 */

import {
  createSpeakerBoundaryTracker,
  sanitizeResolvedName,
  syntheticSpeakerLabel,
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

function makeRig(opts: { hangoverMs?: number; synthesizeUnresolved?: boolean } = {}): Rig {
  let nowMs = T0;
  const events: Published[] = [];
  const logs: string[] = [];
  const names = new Map<number, string>();

  let gate: Promise<void> | null = null;
  let openGate: (() => void) | null = null;

  const rig: Rig = {
    tracker: createSpeakerBoundaryTracker({
      hangoverMs: opts.hangoverMs ?? 700,
      // Defaults to OFF, mirroring production: Google Meet and Teams share this
      // tracker and must keep the pre-existing drop behaviour. Only cases that
      // explicitly opt in exercise the synthetic identity.
      synthesizeUnresolvedSpeakers: () => opts.synthesizeUnresolved ?? false,
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

    // A stringified-nullish resolveName() is sanitized to '' (unmapped) by
    // sanitizeResolvedName, so it takes the SAME "never resolved" path as a
    // track that never resolves at all — it must publish under the synthetic
    // identity, never under the literal junk string.
    for (const junk of ['null', 'undefined']) {
      const rig = makeRig({ synthesizeUnresolved: true });
      rig.tracker.arm();
      rig.setName(0, junk);

      const onset = rig.clock();
      await speak(rig, 0, 600);
      const lastAudio = rig.clock() - 100;
      await stayQuiet(rig, 2_000);

      const synthetic = syntheticSpeakerLabel(0);
      check(`resolveName()="${junk}" is emitted under the synthetic identity, not dropped`, rig.shape(), [
        `started_speaking@${onset}`,
        `stopped_speaking@${lastAudio}`,
      ]);
      check(
        `both boundaries name the synthetic identity, not "${junk}"`,
        rig.events.map((e) => e.speaker),
        [synthetic, synthetic],
      );
      check(
        `no event is named "${junk}"`,
        rig.events.filter((e) => e.speaker === junk).length,
        0,
      );
      // Asserts on the ONCE-PER-TRACK summary line specifically — not on any
      // phrase that a per-boundary log line also carries, which would make this
      // pass for the wrong reason.
      check(
        `the fallback is summarised exactly once for the track`,
        rig.logs.filter((l) => l.includes('NEVER RESOLVED a name')).length,
        1,
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

  // ── 8b. Teardown must not race an in-flight sweep ────────────────────────
  // Regression guard. `stopSweep()` used to flush without checking `sweeping`,
  // so teardown could run concurrently with a tick stuck inside publish. The
  // caller (cleanupPerSpeakerPipeline) nulls `segmentPublisher` the moment
  // stopSweep returns, so a still-in-flight publish was silently dropped.
  // Previously harmless — aw-integration discarded every SPEAKER_END. Now that
  // ENDs close speaker intervals, a dropped END runs that interval to the
  // session boundary and hands the speaker the rest of the meeting.
  console.log('\nTeardown vs in-flight sweep:');
  {
    const rig = makeRig();
    rig.tracker.arm();
    rig.setName(0, 'Judith');

    const onset = rig.clock();
    rig.tracker.markTrackAudioActivity(0, onset);

    rig.blockPublish();
    const inflightSweep = rig.tracker.sweep();   // parks inside publish
    const teardown = rig.tracker.stopSweep();    // must WAIT, not barge in

    let teardownFinishedEarly = false;
    await Promise.race([
      teardown.then(() => { teardownFinishedEarly = true; }),
      new Promise((r) => setTimeout(r, 60)),
    ]);
    checkTrue('stopSweep waits while a sweep is in flight', !teardownFinishedEarly);

    rig.releasePublish();
    await inflightSweep;
    await teardown;

    // Exactly one START (from the parked sweep) and exactly one END (from the
    // flush) — nothing duplicated, nothing lost.
    check('teardown yields one clean START/END pair', rig.shape(), [
      `started_speaking@${onset}`,
      `stopped_speaking@${onset}`,
    ]);
    checkTrue(
      'every START published by teardown has a matching END',
      rig.events.filter((e) => e.type === 'started_speaking').length ===
        rig.events.filter((e) => e.type === 'stopped_speaking').length,
    );
  }

  // ── 9. Multi-track independence (the live 3-speaker case) ────────────────
  // ── 12. Live regression: two humans must never collapse into one speaker ──
  //
  // The 2026-09-01 Zoom meeting, reproduced. Track 1's name resolved; tracks 0
  // and 2 never did (222 and 57 "not yet mapped" polls). Pre-fix, an utterance
  // that ended with no name emitted NO boundary at all, so the downstream mapper
  // — which assigns each transcript segment to the last boundary at or before
  // its start — handed every word of tracks 0 and 2 to track 1's name. Result:
  // 66 timeline intervals, all one person, and one speaker label in the
  // transcript for a two-person conversation.
  //
  // The assertions below are about DISTINCTNESS, which is the property the
  // transcript reader actually cares about, not about the placeholder's spelling.
  console.log('\nDefect 3 — an unresolved speaker must never be published as a resolved one:');
  {
    const rig = makeRig({ synthesizeUnresolved: true }); // Zoom opts in
    rig.tracker.arm();
    rig.setName(1, 'Utpalendu Sarkar'); // resolved
    rig.setName(0, '');                 // never resolves
    rig.setName(2, '');                 // never resolves

    // Turn-taking: unresolved, resolved, unresolved.
    const onset0 = rig.clock();
    await speak(rig, 0, 500);
    const last0 = rig.clock() - 100;
    await stayQuiet(rig, 2_000);

    const onset1 = rig.clock();
    await speak(rig, 1, 500);
    const last1 = rig.clock() - 100;
    await stayQuiet(rig, 2_000);

    const onset2 = rig.clock();
    await speak(rig, 2, 500);
    const last2 = rig.clock() - 100;
    await stayQuiet(rig, 2_000);

    const speakers = Array.from(new Set(rig.events.map((e) => e.speaker)));
    check('three speaking tracks yield THREE distinct speakers, not one', speakers.length, 3);
    check(
      'the resolved human is credited with exactly their own utterance',
      rig.events.filter((e) => e.speaker === 'Utpalendu Sarkar').map((e) => `${e.type}@${e.timestampMs}`),
      [`started_speaking@${onset1}`, `stopped_speaking@${last1}`],
    );
    // The core lie: pre-fix, the resolved name owned the whole meeting because
    // the other two tracks published nothing. Two events is one utterance.
    check(
      'the resolved human is NOT handed the other tracks\' speech',
      rig.events.filter((e) => e.speaker === 'Utpalendu Sarkar').length,
      2,
    );
    check(
      'the unresolved tracks each publish their own real onset and offset',
      rig.events
        .filter((e) => e.speaker !== 'Utpalendu Sarkar')
        .map((e) => `${e.type}@${e.timestampMs}`),
      [
        `started_speaking@${onset0}`,
        `stopped_speaking@${last0}`,
        `started_speaking@${onset2}`,
        `stopped_speaking@${last2}`,
      ],
    );
    check(
      'the two unresolved tracks get DIFFERENT placeholders',
      syntheticSpeakerLabel(0) === syntheticSpeakerLabel(2),
      false,
    );
    // "Impossible to confuse with a real resolved name" — asserted as a property,
    // not as a spelling: no placeholder may equal any resolved name, and every
    // placeholder must be recognisable as one.
    checkTrue(
      'no placeholder collides with the resolved name',
      !speakers.filter((n) => n !== 'Utpalendu Sarkar').includes('Utpalendu Sarkar'),
    );
    checkTrue(
      'every placeholder is marked unresolved',
      speakers.filter((n) => n !== 'Utpalendu Sarkar').every((n) => n.includes('(unresolved)')),
    );
    check('each unresolved track is summarised once, not per sweep', rig.logs.filter((l) => l.includes('NEVER RESOLVED a name')).length, 2);
  }
  {
    // Stability across the session: the same track keeps the same placeholder
    // over several separate utterances, so a reader sees ONE unknown person, not
    // one per turn.
    const rig = makeRig({ synthesizeUnresolved: true }); // Zoom opts in
    rig.tracker.arm();
    for (let turn = 0; turn < 3; turn++) {
      await speak(rig, 4, 400);
      await stayQuiet(rig, 2_000);
    }
    check('three utterances from one unresolved track → 6 events', rig.events.length, 6);
    check(
      'all six carry the SAME placeholder',
      Array.from(new Set(rig.events.map((e) => e.speaker))),
      [syntheticSpeakerLabel(4)],
    );
    check(
      'every placeholder START has a matching END',
      rig.events.filter((e) => e.type === 'started_speaking').length,
      rig.events.filter((e) => e.type === 'stopped_speaking').length,
    );
  }
  {
    // A track that resolves LATE must switch to its real name, and its earlier
    // speech must stay under the placeholder — an honest "unknown then known",
    // never a retroactive rewrite of history we did not observe.
    const rig = makeRig({ synthesizeUnresolved: true }); // Zoom opts in
    rig.tracker.arm();

    await speak(rig, 0, 400);
    await stayQuiet(rig, 2_000);
    check('first utterance is a placeholder', rig.events.map((e) => e.speaker), [
      syntheticSpeakerLabel(0),
      syntheticSpeakerLabel(0),
    ]);

    rig.setName(0, 'sujoy sarkar'); // the live lowercase display name
    const onset = rig.clock();
    await speak(rig, 0, 400);
    const last = rig.clock() - 100;
    await stayQuiet(rig, 2_000);

    check(
      'the later utterance carries the real name',
      rig.events.slice(2).map((e) => `${e.speaker}|${e.type}@${e.timestampMs}`),
      [`sujoy sarkar|started_speaking@${onset}`, `sujoy sarkar|stopped_speaking@${last}`],
    );
    check('the earlier placeholder is NOT rewritten', rig.events[0].speaker, syntheticSpeakerLabel(0));
  }
  {
    // Teardown path: an utterance still OPEN and still nameless when the meeting
    // ends. The sweep fallback cannot help — the sweep never runs again — so
    // stopSweep() must synthesize the pair itself, or the meeting's last
    // utterance is dropped and donated to whoever else resolved.
    const rig = makeRig({ synthesizeUnresolved: true }); // Zoom opts in
    rig.tracker.arm();
    const onset = rig.clock();
    await speak(rig, 7, 500);
    const last = rig.clock() - 100;
    // No silence: the utterance is still open.
    check('precondition: nothing published yet', rig.events.length, 0);

    await rig.tracker.stopSweep();

    check('teardown publishes the nameless open utterance', rig.shape(), [
      `started_speaking@${onset}`,
      `stopped_speaking@${last}`,
    ]);
    check(
      'both teardown boundaries use the track placeholder',
      Array.from(new Set(rig.events.map((e) => e.speaker))),
      [syntheticSpeakerLabel(7)],
    );
    check('teardown clears state', rig.tracker.trackCount(), 0);
  }
  {
    // Teardown must NOT invent an utterance for a track that is not speaking:
    // a track whose utterance already closed has nothing left to publish.
    const rig = makeRig({ synthesizeUnresolved: true }); // Zoom opts in
    rig.tracker.arm();
    await speak(rig, 8, 400);
    await stayQuiet(rig, 2_000);
    const beforeTeardown = rig.events.length;
    await rig.tracker.stopSweep();
    check('teardown adds nothing for an already-closed utterance', rig.events.length, beforeTeardown);
  }

  // ── 13. HARD CONSTRAINT: Google Meet's behaviour must not change ─────────
  //
  // There is ONE tracker instance (`index.ts`) and BOTH
  // `platforms/googlemeet/recording.ts:230` and `platforms/zoom/web/recording.ts:199`
  // call `armSpeakerBoundaries()`, so the synthetic-identity change above would
  // otherwise alter Google Meet's output — introducing a speaker label the
  // worker, adapter and portal have never seen — in a pipeline the user has
  // stated is tested and working at its best.
  //
  // These cases lock the DEFAULT (opted-out) path to the pre-existing behaviour:
  // no boundary at all, and the original log line, verbatim. Every case below
  // uses a plain `makeRig()`, i.e. exactly what a caller that does not opt in
  // gets — which is the whole point of the default being OFF.
  console.log('\nGoogle Meet regression lock (synthesis NOT opted in):');
  {
    const rig = makeRig(); // no opt-in — the Google Meet / Teams path
    rig.tracker.arm();

    await speak(rig, 0, 600);
    await stayQuiet(rig, 2_000);

    check('an unresolved utterance publishes NOTHING (pre-existing behaviour)', rig.events.length, 0);
    // The exact aw/main string, asserted verbatim: downstream log tooling and any
    // human reading a Meet bot log must see the line they have always seen.
    checkTrue(
      'and logs the original "utterance dropped" line, verbatim',
      rig.logs.some((l) => l === '[SpeakerBoundary] Track 0 utterance dropped — no name resolved before it ended'),
    );
    check(
      'no placeholder label appears anywhere in the log',
      rig.logs.filter((l) => l.includes('Unknown Speaker')).length,
      0,
    );
    check('the drop is logged once per track, not per sweep', rig.logs.filter((l) => l.includes('utterance dropped')).length, 1);
  }
  {
    const rig = makeRig(); // no opt-in
    rig.tracker.arm();
    // Teardown with an utterance still OPEN and nameless — the other call site.
    await speak(rig, 3, 500);
    await rig.tracker.stopSweep();
    check('teardown publishes NOTHING for a nameless open utterance either', rig.events.length, 0);
    check(
      'and invents no placeholder at teardown',
      rig.events.filter((e) => e.speaker.includes('Unknown Speaker')).length,
      0,
    );
  }
  {
    // The opt-in must not disturb the RESOLVED path on either platform: a named
    // utterance behaves identically whether or not synthesis is enabled. Without
    // this, the two cases above could pass simply because the tracker was broken.
    for (const synthesize of [false, true]) {
      const rig = makeRig({ synthesizeUnresolved: synthesize });
      rig.tracker.arm();
      rig.setName(0, 'Named Person');
      const onset = rig.clock();
      await speak(rig, 0, 600);
      const lastAudio = rig.clock() - 100;
      await stayQuiet(rig, 2_000);
      check(`CONTROL: a RESOLVED utterance is identical with synthesis=${synthesize}`, rig.shape(), [
        `started_speaking@${onset}`,
        `stopped_speaking@${lastAudio}`,
      ]);
      check(`CONTROL: and names the real speaker with synthesis=${synthesize}`, Array.from(new Set(rig.events.map((e) => e.speaker))), ['Named Person']);
    }
  }

  {
    // THE default-off lock. Every case above goes through `makeRig`, which always
    // passes `synthesizeUnresolvedSpeakers` explicitly — so none of them exercises
    // the MACHINE's own default, and flipping that default to `true` left all of
    // them green (verified by mutation). This case builds a tracker with the option
    // ABSENT, which is what a caller that has not opted in actually looks like.
    const events: Published[] = [];
    const logs: string[] = [];
    let nowMs = T0;
    const tracker = createSpeakerBoundaryTracker({
      hangoverMs: 700,
      now: () => nowMs,
      log: (m) => { logs.push(m); },
      resolveName: () => '', // never resolves
      publish: async (type, speaker, timestampMs) => { events.push({ type, speaker, timestampMs }); },
      // synthesizeUnresolvedSpeakers deliberately OMITTED — this is the Meet shape.
    });
    tracker.arm();
    for (let i = 0; i < 6; i++) {
      tracker.markTrackAudioActivity(0, nowMs);
      await tracker.sweep();
      nowMs += 100;
    }
    for (let i = 0; i < 10; i++) {
      nowMs += 200;
      await tracker.sweep();
    }
    check('with the option OMITTED, an unresolved utterance publishes nothing', events.length, 0);
    checkTrue(
      'with the option OMITTED, the original drop line is logged',
      logs.some((l) => l === '[SpeakerBoundary] Track 0 utterance dropped — no name resolved before it ended'),
    );
    check(
      'with the option OMITTED, no placeholder is ever published',
      events.filter((e) => e.speaker.includes('Unknown Speaker')).length,
      0,
    );
    // Teardown, same tracker, same omission.
    tracker.markTrackAudioActivity(0, nowMs);
    await tracker.stopSweep();
    check('with the option OMITTED, teardown publishes nothing either', events.length, 0);
  }

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
