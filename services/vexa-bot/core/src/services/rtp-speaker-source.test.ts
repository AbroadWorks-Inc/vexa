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
  buildActiveSourceIndex,
  mapSourcesToNames,
  namesClaimedByMultipleSources,
  sampleReceivers,
  sourceActivity,
  concurrentActivity,
  resolveSpeakerIdentities,
  MIN_IDENTITY_CONFIDENCE,
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

// ---------------------------------------------------------------------------
console.log('\nresolveSpeakerIdentities — the identity brain: one clean id per person');
// ---------------------------------------------------------------------------
{
  // THE HAPPY PATH. One sender, one id, every DOM observation agreeing → a
  // clean, high-confidence identity that survives every filter.
  const samples = [
    s(1111, 1_000, 0.9), s(1111, 2_000, 0.9), s(1111, 3_000, 0.9),
  ];
  const names: NameEvent[] = [
    { name: 'Alice Chen', tMs: 1_000 },
    { name: 'Alice Chen', tMs: 2_000 },
    { name: 'Alice Chen', tMs: 3_000 },
  ];
  assertEqual(
    resolveSpeakerIdentities(samples, names),
    [{ sourceId: 1111, name: 'Alice Chen' }],
    'a single high-confidence id → one person, kept',
  );
}

{
  // THE MIRROR, THRESHOLD BRANCH. Live probe: id 42 mirrors whoever is loudest,
  // so across time it is attributed to MANY names — its votes split and its
  // confidence collapses below MIN_IDENTITY_CONFIDENCE. Here 42's names are held
  // by NOBODY else, so nothing but the confidence floor can catch it — this
  // isolates the threshold guard. The two real ids must survive.
  const samples = [
    s(1111, 1_000, 0.9), s(1111, 2_000, 0.9),            // Alice
    s(2222, 3_000, 0.9), s(2222, 4_000, 0.9),            // Bob
    s(42, 5_000, 0.9), s(42, 6_000, 0.9), s(42, 7_000, 0.9), // mirror: 3 names
  ];
  const names: NameEvent[] = [
    { name: 'Alice Chen', tMs: 1_000 },
    { name: 'Alice Chen', tMs: 2_000 },
    { name: 'Bob Müller', tMs: 3_000 },
    { name: 'Bob Müller', tMs: 4_000 },
    { name: 'Carol Diaz', tMs: 5_000 },
    { name: 'Dave Okoro', tMs: 6_000 },
    { name: 'Eve Petrov', tMs: 7_000 },
  ];
  assertEqual(
    resolveSpeakerIdentities(samples, names),
    [
      { sourceId: 1111, name: 'Alice Chen' },
      { sourceId: 2222, name: 'Bob Müller' },
    ],
    'a mirror id split across many names falls below the confidence floor and is dropped; real ids kept',
  );
}

{
  // THE MIRROR, DEDUPE BRANCH. Here the mirror (42) mostly mirrors Alice, so it
  // claims her NAME too — at confidence 0.6, ABOVE the floor. The threshold
  // alone would keep it; only namesClaimedByMultipleSources can catch that two
  // ids hold one name, keeping the HIGHER-confidence real id and dropping the
  // mirror. This isolates the dedupe guard from the threshold guard.
  const samples = [
    s(1111, 1_000, 0.9), s(1111, 2_000, 0.9),            // real Alice: 2/2 → 1.0
    s(42, 3_000, 0.9), s(42, 4_000, 0.9), s(42, 5_000, 0.9), // mirror → Alice x3
    s(42, 6_000, 0.9), s(42, 7_000, 0.9),                // mirror → Frank x2
  ];
  const names: NameEvent[] = [
    { name: 'Alice Chen', tMs: 1_000 },
    { name: 'Alice Chen', tMs: 2_000 },
    { name: 'Alice Chen', tMs: 3_000 },
    { name: 'Alice Chen', tMs: 4_000 },
    { name: 'Alice Chen', tMs: 5_000 },
    { name: 'Frank Ito', tMs: 6_000 },
    { name: 'Frank Ito', tMs: 7_000 },
  ];
  // Sanity: the mirror sits at 0.6, ABOVE the floor — so only the dedupe rule
  // can drop it, not the threshold.
  const mirror = mapSourcesToNames(samples, names).find((m) => m.sourceId === 42);
  assertEqual(
    mirror !== undefined && mirror.confidence >= MIN_IDENTITY_CONFIDENCE,
    true,
    'the mirror is above the confidence floor (so the dedupe rule, not the floor, must catch it)',
  );
  assertEqual(
    resolveSpeakerIdentities(samples, names),
    [{ sourceId: 1111, name: 'Alice Chen' }],
    'two ids claim one name → keep only the higher-confidence source, drop the mirror',
  );
}

{
  // A plain below-threshold source is dropped even when its name is unique. A
  // real speaker (1111) stands beside a source (42) whose votes split evenly
  // across names it alone claims → confidence 0.4 < 0.5 → dropped.
  const samples = [
    s(1111, 1_000, 0.9), s(1111, 2_000, 0.9),
    s(42, 3_000, 0.9), s(42, 4_000, 0.9), s(42, 5_000, 0.9),
    s(42, 6_000, 0.9), s(42, 7_000, 0.9),
  ];
  const names: NameEvent[] = [
    { name: 'Alice Chen', tMs: 1_000 },
    { name: 'Alice Chen', tMs: 2_000 },
    { name: 'Xavier One', tMs: 3_000 },
    { name: 'Xavier One', tMs: 4_000 },
    { name: 'Yuki Two', tMs: 5_000 },
    { name: 'Yuki Two', tMs: 6_000 },
    { name: 'Zed Three', tMs: 7_000 },
  ];
  assertEqual(
    resolveSpeakerIdentities(samples, names),
    [{ sourceId: 1111, name: 'Alice Chen' }],
    'a below-threshold source is dropped even with a name no one else claims',
  );
}

// ---------------------------------------------------------------------------
console.log('\nbuildActiveSourceIndex — the O(1) lookup must match activeSourceAt exactly');
// ---------------------------------------------------------------------------
{
  // The index is the performance fix: mapSourcesToNames used to rescan the
  // whole samples array once PER name event (O(nameEvents × samples)); it now
  // precomputes the winner for every instant in one pass and looks each event
  // up in O(1). This test pins that the index's winner-per-instant is
  // byte-identical to activeSourceAt across the tricky cases: several events at
  // one instant, an instant with no audible source (no vote), and a tie at one
  // instant (first-seen loudest wins).
  const samples = [
    // t=1000: 1111 (0.9) beats 2222 (0.3) → winner 1111.
    s(1111, 1_000, 0.9), s(2222, 1_000, 0.3),
    // t=2000: TIE at 0.7 between 3333 (seen first) and 4444 → strict `>` keeps
    // the first-seen loudest → winner 3333.
    s(3333, 2_000, 0.7), s(4444, 2_000, 0.7),
    // t=3000: both below the silence floor → all-quiet → no winner, no vote.
    s(5555, 3_000, 0.004), s(6666, 3_000, 0.002),
  ];

  // The index winner at each instant equals activeSourceAt at that instant.
  const index = buildActiveSourceIndex(samples);
  assertEqual(index.get(1_000) ?? null, activeSourceAt(samples, 1_000), 'index[1000] == activeSourceAt(1000)');
  assertEqual(index.get(2_000) ?? null, activeSourceAt(samples, 2_000), 'index[2000] == activeSourceAt(2000) (tie → first-seen loudest)');
  assertEqual(index.get(3_000) ?? null, activeSourceAt(samples, 3_000), 'index[3000] == activeSourceAt(3000) (all-quiet → null)');

  // And the end-to-end mapSourcesToNames output over the same data matches the
  // pre-optimization expectation: multiple events at the tie instant and at the
  // winner instant all attribute to the right ids, and the silent instant casts
  // no vote (so 5555/6666 never appear).
  const names: NameEvent[] = [
    { name: 'Alice Chen', tMs: 1_000 },   // → 1111
    { name: 'Alice Chen', tMs: 1_000 },   // → 1111 again (same instant, multiple events)
    { name: 'Bob Müller', tMs: 2_000 },   // → 3333 (tie winner)
    { name: 'Bob Müller', tMs: 2_000 },   // → 3333 again
    { name: 'Ghost Name', tMs: 3_000 },   // all-quiet → discarded, invents no source
  ];
  assertEqual(
    mapSourcesToNames(samples, names),
    [
      { sourceId: 1111, name: 'Alice Chen', votes: 2, total: 2, confidence: 1 },
      { sourceId: 3333, name: 'Bob Müller', votes: 2, total: 2, confidence: 1 },
    ],
    'index-path mapSourcesToNames matches activeSourceAt semantics: multi-event/tie/silent-instant all correct',
  );
}

// ---------------------------------------------------------------------------
console.log('\nMIN_IDENTITY_CONFIDENCE — the 0.5 boundary is INCLUSIVE (F1)');
// ---------------------------------------------------------------------------
{
  // F1: pin the confidence floor at EXACTLY 0.5. The guard in
  // resolveSpeakerIdentities is `entry.confidence < MIN_IDENTITY_CONFIDENCE` —
  // so a source sitting EXACTLY on the floor must be KEPT. Construct source 100
  // as the sole audible source at t=0 and t=500, with name events splitting its
  // votes Alice:1 / Bob:1 → winner votes 1 of total 2 → confidence 0.5, and no
  // other source claims either name (uncontested), so only the threshold guard
  // can act on it. Mutating `<` → `<=` drops it and turns this test red.
  const samples = [s(100, 0, 0.9), s(100, 500, 0.9)];
  const names: NameEvent[] = [
    { name: 'Alice Chen', tMs: 0 },
    { name: 'Bob Müller', tMs: 500 },
  ];

  // Sanity: the confidence really is exactly the boundary value.
  const mapped = mapSourcesToNames(samples, names).find((m) => m.sourceId === 100);
  assertEqual(mapped?.confidence, MIN_IDENTITY_CONFIDENCE, 'source 100 sits at confidence == 0.5 (the boundary)');

  assertEqual(
    resolveSpeakerIdentities(samples, names),
    [{ sourceId: 100, name: 'Alice Chen' }],
    'a source EXACTLY at the confidence floor is kept (< is exclusive; <= would drop it)',
  );
}

console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) {
  process.exit(1);
}
