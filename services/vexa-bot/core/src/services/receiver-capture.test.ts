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

import { pickActiveCsrc, selectAudioReceivers, markConnected } from "./receiver-capture";
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

console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) {
  process.exit(1);
}
