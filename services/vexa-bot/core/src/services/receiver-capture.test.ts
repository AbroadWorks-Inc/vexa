/**
 * Tier 1 per-receiver audio capture — pure logic tests.
 *
 * Run: npx tsx services/vexa-bot/core/src/services/receiver-capture.test.ts
 *
 * Standalone harness, no vitest — matches the convention in
 * rtp-speaker-source.test.ts, platforms/msteams/alone-timer.test.ts and
 * platforms/zoom/web/alone-timer.test.ts.
 *
 * WHY THIS EXISTS. Tier 1 captures each participant's OWN audio from their
 * RTCRtpReceiver track and tags every buffer with that receiver's loudest CSRC
 * id. The in-page half (AudioContext + ScriptProcessor + getContributingSources)
 * cannot be unit-tested in this repo, so every DECISION it makes is factored out
 * into the pure functions tested here: which receivers to capture, which CSRC id
 * a buffer belongs to, and whether a receiver already has a context.
 */

import {
  pickActiveCsrc,
  selectAudioReceivers,
  markConnected,
  sharedActiveCsrcs,
  MIRROR_SENTINEL_CSRCS,
} from "./receiver-capture";
import { SILENCE_LEVEL, RtpSourceLike, ReceiverLike } from "./rtp-speaker-source";

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

// ---------------------------------------------------------------------------
console.log("\npickActiveCsrc — which CSRC id owns this buffer?");
// ---------------------------------------------------------------------------
{
  // The loudest contributor above the silence floor is the sender this buffer
  // belongs to. This is the tag written onto every captured audio frame.
  const sources: RtpSourceLike[] = [
    { source: 1111, audioLevel: 0.8 },
    { source: 2222, audioLevel: 0.1 },
  ];
  assertEqual(pickActiveCsrc(sources), 1111, "loudest CSRC above the floor wins");
}
{
  // All quiet must report NOBODY. A buffer with no audible CSRC is dropped by
  // the caller, not mis-tagged onto the least-quiet id — the same phantom-
  // speaker failure the DOM path suffers.
  const sources: RtpSourceLike[] = [
    { source: 1111, audioLevel: 0.004 },
    { source: 2222, audioLevel: 0.002 },
  ];
  assertEqual(pickActiveCsrc(sources), null, "all-quiet CSRCs report null, not the loudest of the quiet");
}
{
  assertEqual(pickActiveCsrc([]), null, "no CSRC entries reports null");
}
{
  // Chrome omits audioLevel until a packet has arrived. Treating it as 0 keeps
  // it below the floor so an un-started source cannot be tagged as the speaker.
  const sources: RtpSourceLike[] = [{ source: 1111 }];
  assertEqual(pickActiveCsrc(sources), null, "a missing audioLevel is 0, never chosen as the active CSRC");
}
{
  // A silent-but-listed sentinel (measured live: id 42) sits in the list every
  // tick. It must never win over a real speaker.
  const sources: RtpSourceLike[] = [
    { source: 42, audioLevel: 0.002 },
    { source: 1111, audioLevel: 0.5 },
  ];
  assertEqual(pickActiveCsrc(sources), 1111, "a listed-but-silent sentinel never beats a real speaker");
}
{
  // Boundary: exactly at the floor counts as audible (>= floor), matching
  // activeSourceAt in rtp-speaker-source.ts.
  const sources: RtpSourceLike[] = [{ source: 1111, audioLevel: SILENCE_LEVEL }];
  assertEqual(pickActiveCsrc(sources), 1111, "a level exactly at the silence floor is audible");
}

// ---------------------------------------------------------------------------
console.log("\nselectAudioReceivers — capture audio senders only");
// ---------------------------------------------------------------------------
{
  // A video receiver carries no speaker audio and a receiver whose track has
  // ended (null) has nothing to capture. Building a context for either wastes an
  // AudioContext and pollutes the stream, so both are dropped.
  const receivers: ReceiverLike[] = [
    { track: { kind: "video" } },
    { track: { kind: "audio" } },
    { track: null },
    { track: { kind: "audio" } },
  ];
  assertEqual(
    selectAudioReceivers(receivers).map((r) => r.track?.kind),
    ["audio", "audio"],
    "keeps audio receivers, drops video and null-track receivers",
  );
}
{
  // A receiver with no track field at all is dropped, not crashed on.
  const receivers: ReceiverLike[] = [{}, { track: { kind: "audio" } }];
  assertEqual(
    selectAudioReceivers(receivers).map((r) => r.track?.kind),
    ["audio"],
    "a receiver with an absent track is dropped",
  );
}
{
  assertEqual(selectAudioReceivers([]), [], "no receivers yields no captures");
}

// ---------------------------------------------------------------------------
console.log("\nmarkConnected — one AudioContext per receiver, ever");
// ---------------------------------------------------------------------------
{
  // The in-page loop re-scans every few seconds to catch late joiners. Without a
  // dedup ledger it would build a second AudioContext for a receiver it already
  // captured — doubling that participant's audio, the exact duplication bug the
  // DOM path fought. The ledger is keyed by track id.
  const connected = new Set<string>();
  assertEqual(markConnected("track-abc", connected), true, "a never-seen track is newly connected");
  assertEqual(markConnected("track-abc", connected), false, "the same track is not connected twice");
  assertEqual(markConnected("track-def", connected), true, "a different track is newly connected");
  assertEqual(connected.size, 2, "the ledger holds exactly one entry per distinct track");
}

// ---------------------------------------------------------------------------
console.log("\nsharedActiveCsrcs — the cross-receiver mirror signature");
// ---------------------------------------------------------------------------
// Sort a Set into an array so the JSON-based assert can compare it. Ordering is
// not part of the contract; membership is.
function ids(set: Set<number>): number[] {
  return Array.from(set).sort((a, b) => a - b);
}
{
  // THE MIRROR'S DEFINING PROPERTY. Meet echoes an "active-speaker channel"
  // mirror CSRC across MULTIPLE receivers at once (live-measured id 42), loud
  // because it follows whoever is loudest. A real participant is on exactly ONE
  // receiver, so an id audible in two or more receivers' contributing-source
  // lists is the mirror — the thing that must not be allowed to tag every
  // receiver's buffer.
  const perReceiver: RtpSourceLike[][] = [
    [{ source: 42, audioLevel: 0.8 }, { source: 1111, audioLevel: 0.5 }],
    [{ source: 42, audioLevel: 0.7 }, { source: 2222, audioLevel: 0.6 }],
  ];
  assertEqual(ids(sharedActiveCsrcs(perReceiver)), [42], "an id audible in two receivers is a mirror");
}
{
  // A real speaker sits in one receiver only → never flagged as a mirror, so its
  // own buffers keep their own CSRC.
  const perReceiver: RtpSourceLike[][] = [
    [{ source: 1111, audioLevel: 0.8 }],
    [{ source: 2222, audioLevel: 0.6 }],
  ];
  assertEqual(ids(sharedActiveCsrcs(perReceiver)), [], "an id in a single receiver is not a mirror");
}
{
  // Present in two receivers but AUDIBLE (>= floor) in only one is not the mirror
  // signature: the mirror is loud wherever it appears because it mirrors the
  // loudest speaker. A source that has merely gone quiet in one receiver's list
  // (comfort-noise residue) must not be excluded.
  const perReceiver: RtpSourceLike[][] = [
    [{ source: 42, audioLevel: 0.8 }],
    [{ source: 42, audioLevel: 0.004 }], // below SILENCE_LEVEL (0.01)
  ];
  assertEqual(ids(sharedActiveCsrcs(perReceiver)), [], "present in two but audible in only one is not shared");
}
{
  // Boundary: exactly at the floor counts as audible (>=), matching pickActiveCsrc
  // and activeSourceAt. Two receivers each carrying id 42 at the floor → shared.
  const perReceiver: RtpSourceLike[][] = [
    [{ source: 42, audioLevel: SILENCE_LEVEL }],
    [{ source: 42, audioLevel: SILENCE_LEVEL }],
  ];
  assertEqual(ids(sharedActiveCsrcs(perReceiver)), [42], "audible at exactly the floor in two receivers is shared");
}
{
  // Within one receiver an id listed twice must not count as two receivers — the
  // signal is cross-RECEIVER, not cross-entry.
  const perReceiver: RtpSourceLike[][] = [
    [{ source: 42, audioLevel: 0.8 }, { source: 42, audioLevel: 0.7 }],
  ];
  assertEqual(ids(sharedActiveCsrcs(perReceiver)), [], "the same id twice in ONE receiver is not cross-receiver");
}
{
  assertEqual(ids(sharedActiveCsrcs([])), [], "no receivers → no shared ids");
  assertEqual(ids(sharedActiveCsrcs([[], []])), [], "two empty receivers → no shared ids");
}
{
  // A missing audioLevel is 0 (Chrome omits it until a packet arrives) → below
  // the floor → not audible → not shared even across two receivers.
  const perReceiver: RtpSourceLike[][] = [[{ source: 42 }], [{ source: 42 }]];
  assertEqual(ids(sharedActiveCsrcs(perReceiver)), [], "a missing audioLevel is not audible, so not shared");
}

// ---------------------------------------------------------------------------
console.log("\npickActiveCsrc — honour an exclusion set (mirror suppression)");
// ---------------------------------------------------------------------------
{
  // The mirror is the loudest id on this receiver, but it is excluded, so the
  // buffer keeps this receiver's OWN real CSRC instead of being stolen.
  const sources: RtpSourceLike[] = [
    { source: 42, audioLevel: 0.9 },
    { source: 1111, audioLevel: 0.5 },
  ];
  assertEqual(pickActiveCsrc(sources, new Set([42])), 1111, "an excluded loudest mirror yields the next real id");
}
{
  // Every audible candidate excluded → null, dropped like all-silence rather than
  // mis-tagged onto an excluded id.
  const sources: RtpSourceLike[] = [{ source: 42, audioLevel: 0.9 }];
  assertEqual(pickActiveCsrc(sources, new Set([42])), null, "all candidates excluded reports null");
}
{
  // Backward compatible: no exclusion set = today's behaviour, mirror wins.
  const sources: RtpSourceLike[] = [
    { source: 42, audioLevel: 0.9 },
    { source: 1111, audioLevel: 0.5 },
  ];
  assertEqual(pickActiveCsrc(sources), 42, "an undefined exclude leaves behaviour unchanged");
  assertEqual(pickActiveCsrc(sources, new Set()), 42, "an empty exclude leaves behaviour unchanged");
}

// ---------------------------------------------------------------------------
console.log("\nMIRROR_SENTINEL_CSRCS — the known live-measured sentinel constant");
// ---------------------------------------------------------------------------
{
  // The live-measured Meet active-speaker sentinel. Kept as a constant fallback
  // for the case cross-receiver detection cannot see it (a single mixed receiver).
  assertEqual(MIRROR_SENTINEL_CSRCS.has(42), true, "42 is a known sentinel mirror id");
  assertEqual(MIRROR_SENTINEL_CSRCS.has(1111), false, "a real id is not a sentinel");
}

console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) {
  process.exit(1);
}
