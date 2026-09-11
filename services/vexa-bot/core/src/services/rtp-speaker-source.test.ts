/**
 * RTP-layer speaker-source instrumentation — pure logic tests.
 *
 * Run: npx tsx services/vexa-bot/core/src/services/rtp-speaker-source.test.ts
 *
 * Standalone harness, no vitest — matches the convention in
 * platforms/msteams/alone-timer.test.ts and platforms/zoom/web/alone-timer.test.ts.
 *
 * WHY THIS EXISTS. Meet pools ~3 <audio> elements across N participants and
 * re-points them, so "element #2" is not a stable person and attribution
 * oscillates (measured live: 7 people, 3 elements). Option 5 of
 * docs-utpal/PLANS/plan-meet-audio-element-multiplexing-root-fix.md proposes
 * keying on the RTP source id instead, because that identifies the SENDER
 * rather than a browser-local attachment.
 *
 * Whether that id is actually stable is UNMEASURED. These functions are the
 * measurement: the browser collects raw samples, and every decision about what
 * they mean lives here, where it can be tested without a browser.
 */

import {
  sourceStability,
  activeSourceAt,
  mapSourcesToNames,
  namesClaimedByMultipleSources,
  sampleReceivers,
  sourceActivity,
  concurrentActivity,
  RtpSample,
  NameEvent,
} from './rtp-speaker-source';

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

function s(sourceId: number, tMs: number, audioLevel = 0.5): RtpSample {
  return { sourceId, audioLevel, tMs };
}

// ---------------------------------------------------------------------------
console.log('\nsourceStability — does an id persist across the session?');
// ---------------------------------------------------------------------------
{
  // Two sources, both seen from early to late. This is the SHAPE that would
  // prove Option 5 viable: an id that spans the meeting is a person, not an
  // attachment.
  const samples = [
    s(1111, 0), s(2222, 100), s(1111, 5_000), s(2222, 5_100), s(1111, 60_000),
  ];

  assertEqual(
    sourceStability(samples),
    [
      { sourceId: 1111, firstSeenMs: 0, lastSeenMs: 60_000, spanMs: 60_000, samples: 3 },
      { sourceId: 2222, firstSeenMs: 100, lastSeenMs: 5_100, spanMs: 5_000, samples: 2 },
    ],
    'reports first/last/span/count per source, ordered by first appearance',
  );
}

// ---------------------------------------------------------------------------
console.log('\nactiveSourceAt — who is emitting at an instant?');
// ---------------------------------------------------------------------------
{
  // The loudest contributor at that moment wins. This is the signal that
  // replaces "which CSS class is highlighted".
  const samples = [s(1111, 1_000, 0.8), s(2222, 1_000, 0.1)];
  assertEqual(activeSourceAt(samples, 1_000), 1111, 'loudest source at the instant wins');
}
{
  // Silence must report NOBODY, not the least-quiet source. Without this the
  // timeline would name a speaker through every pause — which is exactly the
  // oscillation the DOM path already suffers.
  const samples = [s(1111, 1_000, 0.004), s(2222, 1_000, 0.002)];
  assertEqual(activeSourceAt(samples, 1_000), null, 'all-quiet reports null, not the loudest of the quiet');
}
{
  // Samples at other instants must not leak into this one.
  const samples = [s(1111, 0, 0.9), s(2222, 5_000, 0.6)];
  assertEqual(activeSourceAt(samples, 5_000), 2222, 'only samples at that instant are considered');
}
{
  assertEqual(activeSourceAt([], 1_000), null, 'no samples reports null');
}

// ---------------------------------------------------------------------------
console.log('\nmapSourcesToNames — learn which id belongs to whom');
// ---------------------------------------------------------------------------
{
  // THE CORE IDEA. The DOM knows the NAME but its timing is late and noisy;
  // the RTP layer knows the TIMING precisely but only by number. Each DOM
  // observation casts one vote for whichever source was actually emitting at
  // that instant. After a few votes the id->name mapping is known, and the
  // noisy signal is never needed for timing again.
  const samples = [
    s(1111, 1_000, 0.9), s(2222, 1_000, 0.0),
    s(1111, 2_000, 0.9), s(2222, 2_000, 0.0),
    s(1111, 3_000, 0.0), s(2222, 3_000, 0.9),
  ];
  const names: NameEvent[] = [
    { name: 'Alice Chen', tMs: 1_000 },
    { name: 'Alice Chen', tMs: 2_000 },
    { name: 'Bob Müller', tMs: 3_000 },
  ];

  assertEqual(
    mapSourcesToNames(samples, names),
    [
      { sourceId: 1111, name: 'Alice Chen', votes: 2, total: 2, confidence: 1 },
      { sourceId: 2222, name: 'Bob Müller', votes: 1, total: 1, confidence: 1 },
    ],
    'each source takes the name it was emitting under',
  );
}

{
  // Found by mutation testing: deleting the "nobody was emitting" guard left
  // every test green. The DOM's speaker indicator LAGS — it commonly still
  // names someone through a pause. Attributing that to whatever the Map
  // happens to key on would invent a source that never spoke, which is the
  // phantom-participant failure all over again.
  const samples = [s(1111, 1_000, 0.0), s(2222, 1_000, 0.0)];
  const names: NameEvent[] = [{ name: 'Alice Chen', tMs: 1_000 }];

  assertEqual(
    mapSourcesToNames(samples, names),
    [],
    'a name claimed during silence casts no vote and invents no source',
  );
}

// ---------------------------------------------------------------------------
console.log('\nnamesClaimedByMultipleSources — the instability signature');
// ---------------------------------------------------------------------------
{
  // THE WHOLE POINT OF THE INSTRUMENTATION. If the RTP id identifies the
  // SENDER, one person yields exactly one id for the session. If the same
  // person turns up under two ids, the id is NOT stable across re-pointing or
  // renegotiation — and Option 5 does not hold. That must be reported loudly,
  // not averaged away.
  const mapping = [
    { sourceId: 1111, name: 'Alice Chen', votes: 3, total: 3, confidence: 1 },
    { sourceId: 3333, name: 'Alice Chen', votes: 2, total: 2, confidence: 1 },
    { sourceId: 2222, name: 'Bob Müller', votes: 4, total: 4, confidence: 1 },
  ];
  assertEqual(
    namesClaimedByMultipleSources(mapping),
    ['Alice Chen'],
    'a name held by two ids is reported — that is the id being unstable',
  );
}
{
  // The clean case must stay quiet, or the signal is useless.
  const mapping = [
    { sourceId: 1111, name: 'Alice Chen', votes: 3, total: 3, confidence: 1 },
    { sourceId: 2222, name: 'Bob Müller', votes: 4, total: 4, confidence: 1 },
  ];
  assertEqual(namesClaimedByMultipleSources(mapping), [], 'one id per person reports nothing');
}

// ---------------------------------------------------------------------------
console.log('\nsampleReceivers — read the RTP layer, and record WHICH API had the data');
// ---------------------------------------------------------------------------
{
  // THE OPEN QUESTION THIS INSTRUMENTATION EXISTS TO ANSWER. CSRC lists
  // contributors to a MIXED stream; SSRC identifies ONE sender's stream. Meet's
  // SFU forwards separate streams, which points at SSRC — but Recall.ai
  // publicly describes Meet attribution as "speaker detection using CSRC
  // identifiers". We do not know which Meet populates, so we read both and
  // record the answer rather than betting on it.
  const csrcOnly = [{
    track: { kind: 'audio' },
    getContributingSources: () => [{ source: 1111, audioLevel: 0.7 }],
    getSynchronizationSources: () => [],
  }];
  assertEqual(
    sampleReceivers(csrcOnly, 500),
    [{ sourceId: 1111, audioLevel: 0.7, tMs: 500, api: 'csrc' }],
    'reads CSRC when present and records that it came from csrc',
  );
}
{
  const ssrcOnly = [{
    track: { kind: 'audio' },
    getContributingSources: () => [],
    getSynchronizationSources: () => [{ source: 2222, audioLevel: 0.4 }],
  }];
  assertEqual(
    sampleReceivers(ssrcOnly, 500),
    [{ sourceId: 2222, audioLevel: 0.4, tMs: 500, api: 'ssrc' }],
    'falls back to SSRC and records that it came from ssrc',
  );
}
{
  // A video receiver has no speaker information and must not pollute the data.
  const mixed = [
    { track: { kind: 'video' }, getContributingSources: () => [{ source: 9999, audioLevel: 0.9 }], getSynchronizationSources: () => [] },
    { track: { kind: 'audio' }, getContributingSources: () => [{ source: 1111, audioLevel: 0.6 }], getSynchronizationSources: () => [] },
  ];
  assertEqual(
    sampleReceivers(mixed, 500).map((x) => x.sourceId),
    [1111],
    'video receivers are ignored',
  );
}
{
  // Chrome omits audioLevel when no packet has arrived yet. Treating a missing
  // level as 0 keeps it below SILENCE_LEVEL, so it cannot masquerade as speech.
  const noLevel = [{
    track: { kind: 'audio' },
    getContributingSources: () => [{ source: 1111 }],
    getSynchronizationSources: () => [],
  }];
  assertEqual(
    sampleReceivers(noLevel, 500),
    [{ sourceId: 1111, audioLevel: 0, tMs: 500, api: 'csrc' }],
    'a missing audioLevel becomes 0, never treated as speech',
  );
}

// ---------------------------------------------------------------------------
console.log('\nsourceActivity — separate a real speaker from a listed-but-silent source');
// ---------------------------------------------------------------------------
{
  // THE PROBLEM THIS SOLVES. A source can sit in getContributingSources() for
  // ~10s after it last contributed, and Meet also echoes an internal constant
  // id (measured live: id 42, present every tick across MULTIPLE receivers).
  // "present in the list" is therefore NOT "speaking". audioLevel is what tells
  // them apart: a real speaker has samples above the silence floor; a
  // listed-but-silent source does not. Without this, the stability report calls
  // a constant sentinel a "person".
  const samples = [
    // 1111: a genuine speaker — two audible samples, one quiet.
    s(1111, 1_000, 0.8), s(1111, 2_000, 0.6), s(1111, 3_000, 0.004),
    // 42: always listed, never above the floor — the sentinel shape.
    s(42, 1_000, 0.0), s(42, 2_000, 0.002), s(42, 3_000, 0.001),
  ];
  assertEqual(
    sourceActivity(samples),
    [
      { sourceId: 1111, samples: 3, activeSamples: 2, peakLevel: 0.8 },
      { sourceId: 42, samples: 3, activeSamples: 0, peakLevel: 0.002 },
    ],
    'counts audible samples and peak per source, ordered by first appearance',
  );
}

// ---------------------------------------------------------------------------
console.log('\nconcurrentActivity — the overlap-separation proof');
// ---------------------------------------------------------------------------
{
  // THE OVERLAP QUESTION. When two people talk at once, does Meet forward two
  // distinct sources audible AT THE SAME INSTANT, or collapse them to one? If
  // any tick has >=2 audible sources, overlapping speech is separable by id —
  // which the DOM-element path cannot do.
  const samples = [
    s(1111, 1_000, 0.7), s(2222, 1_000, 0.6), // both audible at t=1000 → overlap
    s(1111, 2_000, 0.7), s(2222, 2_000, 0.6), // and again at t=2000
  ];
  assertEqual(
    concurrentActivity(samples),
    { distinctTicks: 2, maxConcurrent: 2, ticksWithOverlap: 2 },
    'two audible sources at one instant is reported as overlap',
  );
}
{
  // Turn-taking (one at a time) must NOT read as overlap, or the proof is
  // worthless. Different speakers at different ticks → never 2 at once.
  const samples = [
    s(1111, 1_000, 0.7), s(2222, 1_000, 0.0), // only 1111 audible
    s(1111, 2_000, 0.0), s(2222, 2_000, 0.6), // only 2222 audible
  ];
  assertEqual(
    concurrentActivity(samples),
    { distinctTicks: 2, maxConcurrent: 1, ticksWithOverlap: 0 },
    'turn-taking is not overlap — max one audible source per instant',
  );
}
{
  // Mutation guard: a source below the silence floor must not count toward
  // concurrency. Deleting the floor check would call a still-listed silent
  // source a concurrent speaker — inventing overlap that never happened.
  const samples = [s(1111, 1_000, 0.7), s(42, 1_000, 0.002)];
  assertEqual(
    concurrentActivity(samples),
    { distinctTicks: 1, maxConcurrent: 1, ticksWithOverlap: 0 },
    'a silent listed source is not counted as a concurrent speaker',
  );
}

console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) {
  process.exit(1);
}
