/**
 * Tests for remote-camera detection — pure decision logic.
 *
 * Run: npx tsx src/services/camera-detection.test.ts
 *
 * This module has never run against a live meeting (see recording.ts —
 * headless remote-camera video reaching the bot is unverified). These
 * tests only pin the DECISION logic given synthetic per-tick observations;
 * they cannot substitute for live validation.
 */

import {
  evaluateRemoteCameraTick,
  RemoteCameraLatch,
  ObservedVideoElement,
  createOnceGuard,
} from './camera-detection';

let passed = 0;
let failed = 0;

function assert(condition: boolean, message: string): void {
  if (condition) {
    passed++;
  } else {
    failed++;
    console.error(`FAIL: ${message}`);
  }
}

function assertEqual<T>(actual: T, expected: T, message: string): void {
  assert(
    actual === expected,
    `${message} (expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)})`,
  );
}

// ─── evaluateRemoteCameraTick ────────────────────────────────────────────────

// 1. Bot's own virtual-camera track present, no remote video — must not count as camera.
{
  const videos: ObservedVideoElement[] = [{ videoWidth: 1920, isOwnTrack: true }];
  const tick = evaluateRemoteCameraTick(videos, false, true);
  assertEqual(tick.sawCamera, false, 'own track alone must not count as remote camera');
  assertEqual(tick.hadObservationOpportunity, true, 'remote participants present => opportunity');
}

// 2. Presentation active with a real, non-own video element present — must be excluded entirely.
{
  const videos: ObservedVideoElement[] = [{ videoWidth: 1280, isOwnTrack: false }];
  const tick = evaluateRemoteCameraTick(videos, true, true);
  assertEqual(tick.sawCamera, false, 'screen share must never count as video, even with a live non-own video element');
  assertEqual(tick.hadObservationOpportunity, true, 'presentation gating must not suppress opportunity tracking');
}

// 3. Remote video element exists but has no rendered frame (videoWidth 0) — camera off / not decoding.
{
  const videos: ObservedVideoElement[] = [{ videoWidth: 0, isOwnTrack: false }];
  const tick = evaluateRemoteCameraTick(videos, false, true);
  assertEqual(tick.sawCamera, false, 'videoWidth=0 must not count as an active camera');
}

// 4. No remote participants at all (bot alone) — no observation opportunity, regardless of stray video elements.
{
  const videos: ObservedVideoElement[] = [{ videoWidth: 1920, isOwnTrack: true }];
  const tick = evaluateRemoteCameraTick(videos, false, false);
  assertEqual(tick.hadObservationOpportunity, false, 'no remote participants => no opportunity to observe');
  assertEqual(tick.sawCamera, false, 'own track only, still not a camera sighting');
}

// 5. Mixed: own track + a real remote camera in the same tick — remote camera must win.
{
  const videos: ObservedVideoElement[] = [
    { videoWidth: 1920, isOwnTrack: true },
    { videoWidth: 0, isOwnTrack: false }, // another remote participant, camera off
    { videoWidth: 640, isOwnTrack: false }, // the remote participant with camera on
  ];
  const tick = evaluateRemoteCameraTick(videos, false, true);
  assertEqual(tick.sawCamera, true, 'a genuine remote camera among mixed elements must be detected');
}

// 6. No video elements at all, but remote participants present (e.g. all-audio call) — opportunity yes, camera no.
{
  const tick = evaluateRemoteCameraTick([], false, true);
  assertEqual(tick.hadObservationOpportunity, true, 'remote participants present even with zero video elements => opportunity');
  assertEqual(tick.sawCamera, false, 'no video elements => no camera seen');
}

// ─── RemoteCameraLatch ───────────────────────────────────────────────────────

// 7. Never had an opportunity across the whole meeting => unknown (null), not "audio".
{
  const latch = new RemoteCameraLatch();
  latch.observe(evaluateRemoteCameraTick([], false, false));
  latch.observe(evaluateRemoteCameraTick([{ videoWidth: 1920, isOwnTrack: true }], false, false));
  assertEqual(latch.result(), null, 'no tick ever had an observation opportunity => unknown');
}

// 8. Opportunity existed, camera never seen => definitively audio (false).
{
  const latch = new RemoteCameraLatch();
  latch.observe(evaluateRemoteCameraTick([{ videoWidth: 0, isOwnTrack: false }], false, true));
  latch.observe(evaluateRemoteCameraTick([{ videoWidth: 1920, isOwnTrack: true }], false, true));
  assertEqual(latch.result(), false, 'observation happened, camera never seen => audio');
}

// 9. Camera seen once, then latch must stay true even after the camera turns off (latching semantics).
{
  const latch = new RemoteCameraLatch();
  latch.observe(evaluateRemoteCameraTick([{ videoWidth: 640, isOwnTrack: false }], false, true)); // camera on
  latch.observe(evaluateRemoteCameraTick([{ videoWidth: 0, isOwnTrack: false }], false, true)); // camera off later
  latch.observe(evaluateRemoteCameraTick([], false, true)); // participant leaves
  assertEqual(latch.result(), true, 'a single sighting must latch true for the rest of the meeting');
}

// 10. Screen-share-only meeting: camera never counted even though a video element was live throughout.
{
  const latch = new RemoteCameraLatch();
  for (let i = 0; i < 5; i++) {
    latch.observe(evaluateRemoteCameraTick([{ videoWidth: 1280, isOwnTrack: false }], true, true));
  }
  assertEqual(latch.result(), false, 'screen-share-only meeting must resolve to audio, not video');
}

// 11. Mixed meeting lifecycle: starts alone (unknown-leaning), participant joins with camera off, then camera on.
{
  const latch = new RemoteCameraLatch();
  latch.observe(evaluateRemoteCameraTick([], false, false)); // bot alone
  latch.observe(evaluateRemoteCameraTick([{ videoWidth: 0, isOwnTrack: false }], false, true)); // joined, camera off
  latch.observe(evaluateRemoteCameraTick([{ videoWidth: 720, isOwnTrack: false }], false, true)); // camera on
  assertEqual(latch.result(), true, 'latch must resolve true once any tick sees a real remote camera');
}

// 12. Presentation active for the only tick that had participants => still unknown-safe fallback is false, not null,
//     because the tick DID have an observation opportunity (participants were present); presentation only
//     suppresses the camera reading for that tick, not the opportunity flag.
{
  const latch = new RemoteCameraLatch();
  latch.observe(evaluateRemoteCameraTick([{ videoWidth: 1280, isOwnTrack: false }], true, true));
  assertEqual(latch.result(), false, 'presentation-only tick with participants present resolves to audio, not unknown');
}

// ─── createOnceGuard ─────────────────────────────────────────────────────────
// Pins the idempotency guard used to make __vexaFinalizeCameraDetection safe
// to call from every exit path (in-page timers AND the Node-side
// graceful-leave path) without ever double-publishing.
//
// These need `await`, so they're wrapped in an async function rather than
// using top-level await — this file is compiled by the project's shared
// tsconfig (CommonJS/es2016-ish target), which doesn't allow top-level await.
async function runAsyncTests(): Promise<void> {
  // 13. Calling the guarded function twice must only run the wrapped action once.
  {
    let calls = 0;
    const guarded = createOnceGuard(async () => {
      calls++;
      return 'ok';
    });
    const first = await guarded();
    const second = await guarded();
    assertEqual(calls, 1, 'wrapped action must run exactly once across two invocations');
    assertEqual(first, 'ok', 'first call must return the action result');
    assertEqual(second, undefined, 'second call must be a no-op returning undefined');
  }

  // 14. Concurrent (racing) calls — not just sequential — must still only run the action once.
  // This is the realistic shape of the real bug: the in-page "everyone left" timer and the
  // Node-side graceful-leave path can both reach the finalize function nearly simultaneously.
  {
    let calls = 0;
    const guarded = createOnceGuard(async () => {
      calls++;
      await new Promise((resolve) => setTimeout(resolve, 5));
      return calls;
    });
    const [a, b] = await Promise.all([guarded(), guarded()]);
    assertEqual(calls, 1, 'racing concurrent calls must still only run the action once');
    const results = [a, b].filter((r) => r !== undefined);
    assertEqual(results.length, 1, 'exactly one racing call must observe the action result');
  }

  // 15. A first attempt that throws must still permanently block later attempts (at-most-one
  // ATTEMPT semantics, not retry-until-success) — a torn-down page will not become reachable
  // on a second try, so retrying would just mask the same failure again.
  {
    let calls = 0;
    const guarded = createOnceGuard(async () => {
      calls++;
      throw new Error('page already gone');
    });
    let firstThrew = false;
    try {
      await guarded();
    } catch {
      firstThrew = true;
    }
    const second = await guarded();
    assertEqual(firstThrew, true, 'the first call must propagate the action error to its caller');
    assertEqual(calls, 1, 'a failed first attempt must NOT be retried by a later call');
    assertEqual(second, undefined, 'a call after a failed first attempt must be a silent no-op');
  }
}

runAsyncTests().then(() => {
  console.log(`\n${passed} passed, ${failed} failed`);
  if (failed > 0) {
    process.exit(1);
  }
});
