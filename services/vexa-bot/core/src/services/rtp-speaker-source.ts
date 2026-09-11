/**
 * RTP-layer speaker-source instrumentation (Option 5).
 *
 * See docs-utpal/PLANS/plan-meet-audio-element-multiplexing-root-fix.md.
 */

/** One reading from an audio receiver's RTP source list. */
export interface RtpSample {
  /** The RTP source identifier (CSRC or SSRC — whichever the platform populates). */
  sourceId: number;
  /** 0.0–1.0, the audio level in the last RTP packet from this source. */
  audioLevel: number;
  /** Milliseconds since capture started. */
  tMs: number;
  /**
   * Which API supplied this sample. Recorded rather than assumed: whether Meet
   * populates CSRC or SSRC is the open question this instrumentation answers.
   */
  api?: 'csrc' | 'ssrc';
}

/** How long one source id persisted, and how often it was seen. */
export interface SourceStability {
  sourceId: number;
  firstSeenMs: number;
  lastSeenMs: number;
  spanMs: number;
  samples: number;
}

export function sourceStability(samples: RtpSample[]): SourceStability[] {
  const byId = new Map<number, SourceStability>();
  for (const sample of samples) {
    const seen = byId.get(sample.sourceId);
    if (seen === undefined) {
      byId.set(sample.sourceId, {
        sourceId: sample.sourceId,
        firstSeenMs: sample.tMs,
        lastSeenMs: sample.tMs,
        spanMs: 0,
        samples: 1,
      });
      continue;
    }
    seen.lastSeenMs = Math.max(seen.lastSeenMs, sample.tMs);
    seen.firstSeenMs = Math.min(seen.firstSeenMs, sample.tMs);
    seen.spanMs = seen.lastSeenMs - seen.firstSeenMs;
    seen.samples += 1;
  }
  return [...byId.values()];
}

/**
 * Below this level a source counts as silent.
 *
 * `audioLevel` is 0.0–1.0 from the last RTP packet. Comfort noise and an open
 * but unused microphone sit just above zero, so a bare "loudest wins" would
 * name a speaker through every pause — the same oscillation the DOM path
 * already produces. Deliberately generous: this is instrumentation, and a
 * missed quiet word costs less than a phantom speaker.
 */
export const SILENCE_LEVEL = 0.01;

export function activeSourceAt(samples: RtpSample[], tMs: number): number | null {
  let best: RtpSample | null = null;
  for (const sample of samples) {
    if (sample.tMs !== tMs) continue;
    if (sample.audioLevel < SILENCE_LEVEL) continue;
    if (best === null || sample.audioLevel > best.audioLevel) best = sample;
  }
  return best === null ? null : best.sourceId;
}

/**
 * Precompute the loudest audible source for EVERY instant in one pass.
 *
 * `activeSourceAt` answers the question for a single `tMs` by rescanning the
 * whole `samples` array; calling it once per name event is O(nameEvents ×
 * samples), which on a long meeting is 10⁸–10⁹ ops per resolve cycle on the
 * bot's single event-loop thread — a real stall. This builds the same answer
 * for all instants in O(samples), so `mapSourcesToNames` can then look each
 * event's `tMs` up in O(1).
 *
 * The winner rule is byte-identical to `activeSourceAt`:
 *   - a sample below `SILENCE_LEVEL` contributes nothing;
 *   - the loudest audible source at that instant wins;
 *   - ties keep the FIRST-seen loudest — enforced with a strict `>` update,
 *     exactly as `activeSourceAt`'s `sample.audioLevel > best.audioLevel`,
 *     and relying on the same left-to-right array order.
 *
 * Every distinct `tMs` present in the data gets an entry: an audible instant
 * maps to its winning `sourceId`, an all-quiet instant maps to `null`. So a
 * lookup can tell "quiet here" from "no data" the way `activeSourceAt` does —
 * both yield `null` — via `index.get(tMs) ?? null`.
 */
export function buildActiveSourceIndex(samples: RtpSample[]): Map<number, number | null> {
  const index = new Map<number, number | null>();
  // tMs -> best audible level seen so far at that instant (audible samples only).
  const bestLevel = new Map<number, number>();
  for (const sample of samples) {
    // Record the instant even when it is all-quiet, so absence-of-data and
    // silence stay distinguishable exactly as activeSourceAt keeps them.
    if (!index.has(sample.tMs)) index.set(sample.tMs, null);
    if (sample.audioLevel < SILENCE_LEVEL) continue;
    const prev = bestLevel.get(sample.tMs);
    // Strict `>` — a later sample only wins on being STRICTLY louder, so the
    // first-seen loudest keeps a tie, matching activeSourceAt.
    if (prev === undefined || sample.audioLevel > prev) {
      bestLevel.set(sample.tMs, sample.audioLevel);
      index.set(sample.tMs, sample.sourceId);
    }
  }
  return index;
}

/** Per-source audio activity — how a real speaker is told from a silent-but-listed id. */
export interface SourceActivity {
  sourceId: number;
  /** Times this id appeared at all (matches `SourceStability.samples`). */
  samples: number;
  /** Times its level reached `SILENCE_LEVEL` — i.e. it was actually speaking. */
  activeSamples: number;
  /** Highest level ever seen from this source. */
  peakLevel: number;
}

/**
 * Summarise each source's audio activity.
 *
 * `sourceStability` proves an id PERSISTS; it cannot tell a real speaker from a
 * source that merely lingers in `getContributingSources()` or a Meet-internal
 * constant echoed across receivers (measured live: id 42). audioLevel is what
 * separates them — a real speaker has `activeSamples > 0`; a sentinel has ~0.
 * Ordered by first appearance so the output lines up with `sourceStability`.
 */
export function sourceActivity(samples: RtpSample[]): SourceActivity[] {
  const byId = new Map<number, SourceActivity>();
  for (const sample of samples) {
    let entry = byId.get(sample.sourceId);
    if (entry === undefined) {
      entry = { sourceId: sample.sourceId, samples: 0, activeSamples: 0, peakLevel: 0 };
      byId.set(sample.sourceId, entry);
    }
    entry.samples += 1;
    if (sample.audioLevel >= SILENCE_LEVEL) entry.activeSamples += 1;
    if (sample.audioLevel > entry.peakLevel) entry.peakLevel = sample.audioLevel;
  }
  return [...byId.values()];
}

/** How often more than one source was audible at the same instant. */
export interface ConcurrencySummary {
  /** Distinct sampling instants present in the data. */
  distinctTicks: number;
  /** Most sources audible at any single instant. */
  maxConcurrent: number;
  /** Instants where >= 2 sources were audible at once — overlapping speech. */
  ticksWithOverlap: number;
}

/**
 * Measure concurrent audible sources across the timeline.
 *
 * The overlap-separation proof: if any instant carries two audible sources,
 * Meet forwards overlapping speakers as DISTINCT sources that can be told apart
 * by id — the thing the pooled-`<audio>`-element path cannot do. Sources below
 * `SILENCE_LEVEL` are excluded, so a source still listed after it went quiet is
 * not miscounted as a second live speaker.
 */
export function concurrentActivity(samples: RtpSample[]): ConcurrencySummary {
  // tMs -> set of source ids audible at that instant.
  const audibleAt = new Map<number, Set<number>>();
  const ticks = new Set<number>();
  for (const sample of samples) {
    ticks.add(sample.tMs);
    if (sample.audioLevel < SILENCE_LEVEL) continue;
    const ids = audibleAt.get(sample.tMs) ?? new Set<number>();
    ids.add(sample.sourceId);
    audibleAt.set(sample.tMs, ids);
  }
  let maxConcurrent = 0;
  let ticksWithOverlap = 0;
  for (const ids of audibleAt.values()) {
    if (ids.size > maxConcurrent) maxConcurrent = ids.size;
    if (ids.size >= 2) ticksWithOverlap += 1;
  }
  return { distinctTicks: ticks.size, maxConcurrent, ticksWithOverlap };
}

/** The DOM/roster's claim that `name` was speaking at `tMs`. */
export interface NameEvent {
  name: string;
  tMs: number;
}

/** One learned id→name mapping, with the evidence behind it. */
export interface SourceName {
  sourceId: number;
  name: string;
  /** Votes for the winning name. */
  votes: number;
  /** Votes cast for this source in total. */
  total: number;
  /** `votes / total` — 1 means every observation agreed. */
  confidence: number;
}

export function mapSourcesToNames(
  samples: RtpSample[],
  nameEvents: NameEvent[],
): SourceName[] {
  // sourceId -> (name -> votes), in first-seen order so the output is stable.
  const tally = new Map<number, Map<string, number>>();

  // Precompute the winner for every instant ONCE (O(samples)), then look each
  // event up in O(1) instead of rescanning samples per event (O(nameEvents ×
  // samples)). The index applies activeSourceAt's exact rule, so the result is
  // byte-identical to calling activeSourceAt in the loop.
  const activeIndex = buildActiveSourceIndex(samples);

  for (const event of nameEvents) {
    const sourceId = activeIndex.get(event.tMs) ?? null;
    // No source was audibly emitting when the DOM claimed a speaker. That is
    // evidence about the DOM's lag, not about this source — discard the vote
    // rather than guess.
    if (sourceId === null) continue;
    let names = tally.get(sourceId);
    if (names === undefined) {
      names = new Map<string, number>();
      tally.set(sourceId, names);
    }
    names.set(event.name, (names.get(event.name) ?? 0) + 1);
  }

  const out: SourceName[] = [];
  for (const [sourceId, names] of tally) {
    let winner = '';
    let votes = 0;
    let total = 0;
    for (const [name, count] of names) {
      total += count;
      if (count > votes) {
        winner = name;
        votes = count;
      }
    }
    out.push({ sourceId, name: winner, votes, total, confidence: votes / total });
  }
  return out;
}

/**
 * Names that more than one source id claims.
 *
 * Empty is the result Option 5 needs: one sender, one id, for the whole
 * session. A non-empty list means the id changed under a person — through
 * element re-pointing or SFU renegotiation — and the id cannot be the
 * attribution key on its own.
 */
export function namesClaimedByMultipleSources(mapping: SourceName[]): string[] {
  const idsPerName = new Map<string, Set<number>>();
  for (const entry of mapping) {
    const ids = idsPerName.get(entry.name) ?? new Set<number>();
    ids.add(entry.sourceId);
    idsPerName.set(entry.name, ids);
  }
  const out: string[] = [];
  for (const [name, ids] of idsPerName) {
    if (ids.size > 1) out.push(name);
  }
  return out;
}

/**
 * Below this confidence a learned id→name mapping is not trustworthy enough to
 * be a person.
 *
 * A stable sender votes for ONE name every time, so its confidence sits at 1.0.
 * The live probe found id 42 mirrors whoever is loudest, so across the session
 * it is attributed to MANY names — its votes split and its confidence collapses.
 * Dropping anything below this floor removes that mirror without needing to know
 * its id in advance. Deliberately loose: a genuine speaker occasionally
 * mis-voted by DOM lag still clears it, while a source spread across three or
 * more names cannot.
 */
export const MIN_IDENTITY_CONFIDENCE = 0.5;

/**
 * Resolve stable per-person identities from CSRC/SSRC samples.
 *
 * The identity brain of Tier 1 per-speaker audio. `mapSourcesToNames` learns a
 * raw id→name mapping; this turns that into a clean, one-entry-per-person list
 * fit to key audio on, by stripping the two ways a mapping lies:
 *
 *  1. LOW CONFIDENCE — a "mirror" source (live probe: id 42) follows whoever is
 *     loudest, so its votes scatter across names and its confidence falls below
 *     `MIN_IDENTITY_CONFIDENCE`. Such sources are dropped.
 *  2. A NAME CLAIMED BY MULTIPLE IDS — when the same person surfaces under a
 *     real id AND a mirror id, `namesClaimedByMultipleSources` flags the name;
 *     only the highest-confidence source for that name is kept (tie → most
 *     votes), the rest dropped. This catches a mirror that sat above the floor.
 *
 * Output is one `{ sourceId, name }` per surviving person, in first-appearance
 * order (inherited from `mapSourcesToNames`, which keys by first vote).
 */
export function resolveSpeakerIdentities(
  samples: RtpSample[],
  nameEvents: NameEvent[],
): { sourceId: number; name: string }[] {
  const mapping = mapSourcesToNames(samples, nameEvents);
  const contested = new Set(namesClaimedByMultipleSources(mapping));

  // For each contested name, decide the single source that keeps it: highest
  // confidence, tie broken by raw votes. A mirror splits its votes, so a real
  // sender almost always wins here even when the mirror cleared the floor.
  const keptSourceForName = new Map<string, number>();
  for (const name of contested) {
    let best: SourceName | null = null;
    for (const entry of mapping) {
      if (entry.name !== name) continue;
      if (
        best === null ||
        entry.confidence > best.confidence ||
        (entry.confidence === best.confidence && entry.votes > best.votes)
      ) {
        best = entry;
      }
    }
    if (best !== null) keptSourceForName.set(name, best.sourceId);
  }

  const out: { sourceId: number; name: string }[] = [];
  for (const entry of mapping) {
    // Guard 1 — confidence floor: drop mirrors whose votes scattered.
    if (entry.confidence < MIN_IDENTITY_CONFIDENCE) continue;
    // Guard 2 — one id per name: for a contested name keep only the winner.
    if (contested.has(entry.name) && keptSourceForName.get(entry.name) !== entry.sourceId) {
      continue;
    }
    out.push({ sourceId: entry.sourceId, name: entry.name });
  }
  return out;
}

/** Minimal shape of one entry from `getContributingSources()` / `getSynchronizationSources()`. */
export interface RtpSourceLike {
  source: number;
  audioLevel?: number;
}

/** Minimal shape of an `RTCRtpReceiver`, so this is testable without a browser. */
export interface ReceiverLike {
  track?: { kind: string } | null;
  getContributingSources?: () => RtpSourceLike[];
  getSynchronizationSources?: () => RtpSourceLike[];
}

/**
 * One reading across every audio receiver, at instant `tMs`.
 *
 * Tries CSRC first and falls back to SSRC, tagging each sample with which one
 * produced it — the point being to LEARN which Meet uses, not to guess. Takes
 * plain objects rather than real `RTCRtpReceiver`s so the logic is testable
 * outside a browser; the caller passes `pc.getReceivers()`.
 */
export function sampleReceivers(receivers: ReceiverLike[], tMs: number): RtpSample[] {
  const out: RtpSample[] = [];
  for (const receiver of receivers) {
    if (receiver.track?.kind !== 'audio') continue;

    const csrc = receiver.getContributingSources?.() ?? [];
    const entries = csrc.length > 0 ? csrc : (receiver.getSynchronizationSources?.() ?? []);
    const api: 'csrc' | 'ssrc' = csrc.length > 0 ? 'csrc' : 'ssrc';

    for (const entry of entries) {
      out.push({
        sourceId: entry.source,
        // Chrome omits audioLevel until a packet has arrived. 0 keeps it below
        // SILENCE_LEVEL so an un-started source cannot look like speech.
        audioLevel: entry.audioLevel ?? 0,
        tMs,
        api,
      });
    }
  }
  return out;
}
