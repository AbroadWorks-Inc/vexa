/**
 * Speaker attribution — CSS-independence and one-name-per-track tests.
 *
 * Run: npx tsx services/vexa-bot/core/src/services/speaker-attribution.test.ts
 *
 * These tests exist because Google Meet speaker attribution has broken THREE
 * times, each time because the logic was keyed on Meet's obfuscated CSS class
 * names (`Oaajhc`, `HX2H7`, …), which Meet rotates every few weeks.
 *
 * Every case below runs with `speaking: []` — i.e. the CSS speaking signal is
 * COMPLETELY DEAD, exactly as it is in production after a class rotation. Names
 * must still resolve. That is the first regression this file guards.
 *
 * The second half guards the invariant "one name per track, one track per name".
 * The 2026-08-12 live run proved names can be RIGHT while the timeline is WRONG:
 * two tracks both resolved to one participant, putting 322 of 429 timeline events
 * on someone with 7 of 13 transcript turns. Attribution reads the last timeline
 * event at or before a segment's start, so a skewed timeline misattributes speech.
 *
 * Design note for these cases: names are only "taken" via locks and LEADING VOTES,
 * so several cases deliberately resolve a track just ONCE (one vote — below
 * LOCK_THRESHOLD) to exercise the unlocked window, which is precisely where the
 * collision used to happen.
 *
 * No browser and no meeting: `page.evaluate(fn, arg)` is faked by running `fn`
 * locally against a stubbed `window`, which is all the resolver touches.
 */

import {
  resolveSpeakerName,
  clearSpeakerNameCache,
  invalidateSpeakerName,
  reportTrackAudio,
  recordTrackVote,
  isNameTaken,
} from './speaker-identity';

// ─── Test harness ────────────────────────────────────────────────────────────

let passed = 0;
let failed = 0;

function check(label: string, actual: unknown, expected: unknown): void {
  const ok = actual === expected;
  if (ok) {
    passed++;
    console.log(`  ✅ ${label}`);
  } else {
    failed++;
    console.log(`  ❌ ${label}\n       expected: ${JSON.stringify(expected)}\n       actual:   ${JSON.stringify(actual)}`);
  }
}

interface FakeWindowOpts {
  /** Roster: participantId → display name. */
  names?: Record<string, string>;
  /** CSS-derived speaking list. Deliberately [] in most tests here. */
  speaking?: string[];
}

/**
 * Build a fake Playwright Page. `evaluate` executes the callback in-process with
 * a stubbed global `window`, which is the only browser surface these code paths
 * use (no `document`, no CSS).
 *
 * NOTE: this used to stub `__vexaGetTrackTileInfo` for the old Layer 2 (tile
 * containment). That layer is gone — the 2026-08-12 live run proved Meet attaches
 * <audio> directly to <body>, so there is no tile above a media element to walk
 * up to. The cascade is now correlation-only, so the fake needs the roster and
 * the CSS speaking list and nothing else.
 */
function makeFakePage(opts: FakeWindowOpts): any {
  const win: Record<string, unknown> = {};

  win.__vexaGetAllParticipantNames = () => ({
    names: opts.names ?? {},
    speaking: opts.speaking ?? [],
  });

  return {
    async evaluate(fn: (arg?: any) => any, arg?: any) {
      const prev = (globalThis as any).window;
      (globalThis as any).window = win;
      try {
        return fn(arg);
      } finally {
        (globalThis as any).window = prev;
      }
    },
  };
}

const BOT = 'AW Notetaker';

async function run(): Promise<void> {
  console.log('\n=== Speaker attribution: CSS-independence ===\n');

  // ── Resolution with the CSS signal dead ───────────────────────────────────
  // These used to exercise Layer 2 (tile containment). That layer is removed, so
  // the same guarantees are now asserted through the correlation layers, which
  // is what actually carries attribution in production.
  console.log('Name resolution with a dead CSS signal:');
  {
    clearSpeakerNameCache();
    reportTrackAudio(0); // this track is the one emitting audio
    const page = makeFakePage({
      names: { p1: 'Speaker B', p2: BOT },
      speaking: [], // ← rotated classes: nothing detected
    });
    const name = await resolveSpeakerName(page, 0, 'googlemeet', BOT);
    check('resolves a real name with zero CSS speaking signal', name, 'Speaker B');
  }

  {
    clearSpeakerNameCache();
    reportTrackAudio(0);
    const page = makeFakePage({
      names: { p1: BOT }, // the bot is the only roster entry
      speaking: [],
    });
    const name = await resolveSpeakerName(page, 0, 'googlemeet', BOT);
    check('never attributes a track to the bot itself', name, '');
  }

  {
    clearSpeakerNameCache();
    // 'Speaker C' is already locked to track 0 (2 votes = LOCK_THRESHOLD).
    recordTrackVote(0, 'Speaker C', 1.0);
    recordTrackVote(0, 'Speaker C', 1.0);
    check('precondition: name is locked to track 0', isNameTaken('Speaker C', 1), true);

    reportTrackAudio(1);
    const page = makeFakePage({
      // Only the already-claimed name is unclaimed-looking to a naive resolver.
      names: { p1: 'Speaker C', p2: 'Speaker A' },
      speaking: ['Speaker C'], // CSS even points at the taken name
    });
    const name = await resolveSpeakerName(page, 1, 'googlemeet', BOT);
    check('upholds one-name-per-track (does not steal a locked name)', name !== 'Speaker C', true);
  }

  // ── Layer 3: audio-activity elimination ──────────────────────────────────
  console.log('\nLayer 3 — audio-activity elimination (CSS signal dead):');
  {
    clearSpeakerNameCache();
    reportTrackAudio(3); // only this track is emitting audio
    const page = makeFakePage({
      names: { p1: 'Speaker A', p2: BOT },
      speaking: [],
    });
    const name = await resolveSpeakerName(page, 3, 'googlemeet', BOT);
    check('binds the sole active track to the sole unclaimed roster name', name, 'Speaker A');
  }

  {
    clearSpeakerNameCache();
    reportTrackAudio(4);
    const page = makeFakePage({
      names: { p1: 'Alice', p2: 'Bob', p3: BOT }, // two candidates → ambiguous
      speaking: [],
    });
    const name = await resolveSpeakerName(page, 4, 'googlemeet', BOT);
    check('refuses to guess when 2+ roster names are unclaimed', name, '');
  }

  // ── Graceful degradation ─────────────────────────────────────────────────
  console.log('\nGraceful degradation:');
  {
    clearSpeakerNameCache();
    reportTrackAudio(5);
    const page = makeFakePage({
      names: { p1: 'Solo Speaker', p2: BOT },
      speaking: [],
    });
    const name = await resolveSpeakerName(page, 5, 'googlemeet', BOT);
    check('a sole active track with a sole unclaimed name still resolves', name, 'Solo Speaker');
  }

  {
    clearSpeakerNameCache();
    const page = makeFakePage({ names: {}, speaking: [] });
    const name = await resolveSpeakerName(page, 9, 'googlemeet', BOT);
    check('empty roster + dead CSS yields "" rather than a wrong name', name, '');
  }

  // ── Legacy CSS path still honoured when it happens to work ───────────────
  console.log('\nLayer 4 — legacy CSS path (regression guard, must still function):');
  {
    clearSpeakerNameCache();
    // NOTE: deliberately no reportTrackAudio(7) here. That makes
    // isMostRecentlyActiveTrack(7) false, so Layer 3 is skipped and Layer 4 is
    // genuinely the layer under test. Adding a reportTrackAudio call would make
    // Layer 3 resolve first and this test would silently stop covering the CSS path.
    const page = makeFakePage({
      names: { p1: 'Alice', p2: 'Bob', p3: BOT },
      speaking: ['Bob'], // classes DID match this time
    });
    const name = await resolveSpeakerName(page, 7, 'googlemeet', BOT);
    check('single CSS speaker still votes correctly when classes match', name, 'Bob');
  }

  // ── One name per track, one track per name ───────────────────────────────
  // Regression for the 2026-08-12 defect: with nothing locked yet, two tracks both
  // resolved to the same participant. Fails against the locks-only isNameTaken().
  console.log('\nCollision invariant — two tracks must never share one name:');
  {
    clearSpeakerNameCache();
    const page = makeFakePage({
      names: { p1: 'Speaker C', p2: BOT }, // exactly one real participant
      speaking: [],
    });

    reportTrackAudio(0);
    const first = await resolveSpeakerName(page, 0, 'googlemeet', BOT);
    check('first active track binds the sole unclaimed roster name', first, 'Speaker C');

    // Track 0 has ONE vote — under LOCK_THRESHOLD, so nothing is locked. Pre-fix
    // this window let track 1 claim the same person (bot.log.final L825/L832).
    reportTrackAudio(1);
    const second = await resolveSpeakerName(page, 1, 'googlemeet', BOT);
    check('a second track cannot take a name another track already leads', second !== 'Speaker C', true);
    check('...and yields "" rather than a confidently wrong name', second, '');
  }

  {
    // Same collision through Layer 4 — the exact shape of L1038/L1040, where two
    // tracks were handed one name by the CSS `speaking` signal.
    // NOTE: the roster carries TWO real names on purpose. With only one, Layer 3
    // would resolve first and this case would stop covering the CSS path.
    clearSpeakerNameCache();
    const page = makeFakePage({
      names: { p1: 'Speaker A', p2: 'Alice', p3: BOT },
      speaking: ['Speaker A'], // one highlighted name, several live tracks
    });
    const t0 = await resolveSpeakerName(page, 0, 'googlemeet', BOT);
    const t2 = await resolveSpeakerName(page, 2, 'googlemeet', BOT);
    check('CSS path: first track takes the highlighted name', t0, 'Speaker A');
    check('CSS path: a second track is refused the same name', t2 !== 'Speaker A', true);
  }

  {
    // The contract stated in the module docstring, asserted directly.
    clearSpeakerNameCache();
    recordTrackVote(0, 'Alice', 1.0); // one vote → leading but NOT locked
    check('a leading vote alone makes a name taken (pre-fix: false)', isNameTaken('Alice', 1), true);
    check('a claim never blocks the track that holds it', isNameTaken('Alice', 0), false);
    check('an unclaimed name stays free', isNameTaken('Bob', 1), false);
  }

  // ── Invariant survives a mid-session cache wipe ──────────────────────────
  console.log('\nMid-session wipe (participant joined/left → clearSpeakerNameCache):');
  {
    clearSpeakerNameCache();
    const page = makeFakePage({
      names: { p1: 'Speaker B', p2: BOT },
      speaking: [],
    });
    await resolveSpeakerName(page, 0, 'googlemeet', BOT); // vote 1
    const locked = await resolveSpeakerName(page, 0, 'googlemeet', BOT); // vote 2 → LOCKED
    check('track 0 locks its own name', locked, 'Speaker B');

    clearSpeakerNameCache(); // Meet reassigns tracks on join/leave: nothing is locked now

    const after = await resolveSpeakerName(page, 0, 'googlemeet', BOT);
    check('the owning track re-acquires its name after the wipe', after, 'Speaker B');

    reportTrackAudio(1);
    const other = await resolveSpeakerName(page, 1, 'googlemeet', BOT);
    check('post-wipe, a second track still cannot collide with it', other !== 'Speaker B', true);
  }

  // ── Happy path + no deadlock ─────────────────────────────────────────────
  console.log('\nNo deadlock — legitimate owners still get, and keep, their names:');
  {
    // Two tracks, two people. With Layer 2 removed, one signal has to break the
    // initial symmetry — either the CSS list or a lock. Here CSS names track 0's
    // speaker; that claim then leaves exactly one unclaimed roster name, so
    // Layer 3's elimination can resolve track 1 with NO CSS signal at all. This
    // is the two layers cooperating, which is how production behaves.
    clearSpeakerNameCache();
    const withCss = makeFakePage({
      names: { p1: 'Alice', p2: 'Bob', p3: BOT },
      speaking: ['Alice'],
    });
    check('CSS names the first track', await resolveSpeakerName(withCss, 0, 'googlemeet', BOT), 'Alice');

    const cssDead = makeFakePage({
      names: { p1: 'Alice', p2: 'Bob', p3: BOT },
      speaking: [],
    });
    reportTrackAudio(1);
    check('elimination then resolves the second, CSS dead', await resolveSpeakerName(cssDead, 1, 'googlemeet', BOT), 'Bob');

    // Re-resolving must be stable, not oscillate or fall back to "".
    check('track 0 keeps its name on re-resolution', await resolveSpeakerName(cssDead, 0, 'googlemeet', BOT), 'Alice');
    check('track 1 keeps its name on re-resolution', await resolveSpeakerName(cssDead, 1, 'googlemeet', BOT), 'Bob');
  }

  {
    // A refused track must not be stuck at "" forever: when the claim holder goes
    // away, the name is released and the remaining track can take it.
    clearSpeakerNameCache();
    const page = makeFakePage({
      names: { p1: 'Solo Speaker', p2: BOT },
      speaking: [],
    });
    reportTrackAudio(10);
    check('track 10 claims the only name', await resolveSpeakerName(page, 10, 'googlemeet', BOT), 'Solo Speaker');
    reportTrackAudio(11);
    check('track 11 is refused while the claim stands', await resolveSpeakerName(page, 11, 'googlemeet', BOT), '');

    invalidateSpeakerName('googlemeet', 10); // that participant's track went away

    reportTrackAudio(11);
    check('the released name becomes available again (no deadlock)', await resolveSpeakerName(page, 11, 'googlemeet', BOT), 'Solo Speaker');
  }

  // ── Other platforms must not regress ─────────────────────────────────────
  // Teams and Zoom share the lock/vote store. Their DOM traversal needs a real
  // `document`, so these cases cover the shared Layer-1 path only — enough to
  // prove the stricter isNameTaken() has not broken the dispatcher for them.
  console.log('\nTeams / Zoom — shared lock path intact:');
  {
    clearSpeakerNameCache();
    recordTrackVote(20, 'Teams Person', 1.0);
    recordTrackVote(20, 'Teams Person', 1.0); // → locked
    check('Teams: locked mapping returns without touching the DOM', await resolveSpeakerName(makeFakePage({}), 20, 'msteams', BOT), 'Teams Person');
  }

  {
    clearSpeakerNameCache();
    recordTrackVote(21, 'Zoom Person', 1.0);
    recordTrackVote(21, 'Zoom Person', 1.0); // → locked
    check('Zoom: locked mapping returns without touching the DOM', await resolveSpeakerName(makeFakePage({}), 21, 'zoom', BOT), 'Zoom Person');
  }

  console.log(`\n=== ${passed} passed, ${failed} failed ===\n`);
  if (failed > 0) process.exit(1);
}

run().catch(err => {
  console.error('Test harness crashed:', err);
  process.exit(1);
});
