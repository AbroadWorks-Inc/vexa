/**
 * Zoom display-name candidacy + accumulated roster (DOM-INDEPENDENT).
 *
 * ── Why this module exists ──────────────────────────────────────────────────
 * The live 2-human Zoom meeting of 2026-09-01 produced a transcript in which
 * BOTH humans' speech was attributed to one of them. Two measured causes sat in
 * `speaker-identity.ts`, and both are fixed by moving decisions OUT of the
 * browser and into pure, testable functions here.
 *
 * 1. `traverseZoomDOM`'s in-page `looksLikeName` rejected any candidate whose
 *    first character was a lowercase Latin letter, as an anti-chat-sentence
 *    heuristic. The second participant's Zoom display name was literally
 *    `"sujoy sarkar"` — lowercase 's'. Their name was therefore UNRETURNABLE by
 *    construction, while `"Utpalendu Sarkar"` sailed through. That asymmetry is
 *    the whole observed outcome: 1 track locked, 2 tracks never mapped, 278
 *    "not yet mapped" log lines. The heuristic is dropped here: candidates are
 *    read from `.video-avatar__avatar-footer` (and the participants panel) —
 *    class-scoped, name-only elements, so the case of the first letter carries no
 *    information about junk. The old rule was not baseless — the code it came
 *    from cited a real observed chat sentence ("it's a we thing"), so some read
 *    did once surface prose. The claim here is NOT that prose is impossible; it
 *    is that length, multi-line and UI-label rejection catch prose without also
 *    catching a lowercase NAME, which is the one thing case-matching cannot do.
 *
 * 2. Zoom exposed exactly ONE participant name at any single instant. The live
 *    log read `roster=[Utpalendu Sarkar]` twenty times, then later
 *    `roster=[sujoy sarkar]`, and not once both together across 21 observations.
 *    Whether that is inherent to Zoom or specific to that situation is UNKNOWN —
 *    one run, gallery view, participants panel closed. Either way a point-in-time
 *    roster could not support elimination there, while the union OVER TIME does
 *    contain both names, which is what `ZoomRosterObservatory` accumulates.
 *
 * ── Why the filtering lives in Node, not in `page.evaluate` ─────────────────
 * A function passed to `page.evaluate` is serialised and re-parsed in the page,
 * so it cannot close over anything imported. Every predicate written inline
 * there is consequently untestable without a browser — which is exactly how a
 * one-line heuristic silently deleted a participant for weeks. The browser side
 * is now dumb (return raw strings); every judgement happens here, under test.
 *
 * Do NOT add a `playwright` import to this file — that is what keeps
 * `zoom-roster.test.ts` runnable under a plain `tsx` invocation.
 */

/**
 * Zoom toolbar / panel strings that sit in the same containers as names.
 * Compared case-insensitively against the WHOLE trimmed candidate, never as a
 * substring, so a participant called "View" is rejected but "Viewfinder Vince"
 * is not.
 */
export const ZOOM_UI_LABELS: ReadonlySet<string> = new Set([
  'mute all',
  'unmute all',
  'raise hand',
  'lower hand',
  'participants',
  'invite',
  'more',
  'chat',
  'share screen',
  'record',
  'leave',
  'ask to unmute',
  'reactions',
  'view',
  'you',
  'me',
  'host',
  'co-host',
  'muted',
  'unmuted',
]);

/** Longest string still plausible as a display name. */
export const ZOOM_NAME_MAX_LENGTH = 60;

/**
 * Is this text plausibly a Zoom participant's display name?
 *
 * Deliberately permissive about CASE — see cause 1 in the module header. A
 * name's first letter being lowercase is the single check that broke live
 * attribution. It was aimed at a real problem — chat prose reaching a name read —
 * but it is the wrong discriminator, because "starts lowercase" is equally true of
 * a chat sentence and of an ordinary display name. The rules below separate the
 * two without deleting a participant.
 *
 * Rejected: empty/whitespace, over-long, multi-line (a paragraph of chat or a
 * concatenated `innerText` of several tiles), and exact UI labels.
 */
export function looksLikeZoomDisplayName(text: string): boolean {
  const trimmed = text.trim();
  if (trimmed.length === 0) return false;
  if (trimmed.length > ZOOM_NAME_MAX_LENGTH) return false;
  // A real display name is single-line. A multi-line candidate means the read
  // picked up a container, not a name element.
  if (/[\n\r\t]/.test(trimmed)) return false;
  if (ZOOM_UI_LABELS.has(trimmed.toLowerCase())) return false;
  return true;
}

/**
 * Does `candidate` refer to the bot itself?
 *
 * Two-way containment, matching the pre-existing convention everywhere else in
 * `speaker-identity.ts` — Zoom sometimes decorates the bot's own tile ("AW
 * Notetaker (Me)") and sometimes truncates it. An empty `selfName` disables the
 * test rather than matching everything.
 */
export function isZoomSelfName(candidate: string, selfName?: string): boolean {
  const selfLower = (selfName ?? '').trim().toLowerCase();
  if (!selfLower) return false;
  const lower = candidate.trim().toLowerCase();
  if (!lower) return false;
  return lower.includes(selfLower) || selfLower.includes(lower);
}

/**
 * Turn a raw browser read into accepted display names: trim, drop junk, drop the
 * bot, de-duplicate while preserving first-seen order.
 */
export function normalizeZoomCandidates(
  raw: readonly (string | null | undefined)[],
  selfName?: string,
): string[] {
  const out: string[] = [];
  const seen = new Set<string>();
  for (const entry of raw) {
    if (entry == null) continue;
    const trimmed = String(entry).trim();
    if (!looksLikeZoomDisplayName(trimmed)) continue;
    if (isZoomSelfName(trimmed, selfName)) continue;
    if (seen.has(trimmed)) continue;
    seen.add(trimmed);
    out.push(trimmed);
  }
  return out;
}

/**
 * The union, over the session, of every participant name Zoom has ever shown us
 * — because Zoom shows us at most one at a time (cause 2 in the module header).
 *
 * Each name carries the time it was LAST observed, so a caller can eliminate
 * among only recently-seen names. That freshness bound exists for one specific
 * hazard: a participant who LEAVES cannot be detected from a DOM that never
 * lists more than one person, so without it their name would stay eliminable
 * forever and could eventually be handed to a different human's track — a wrong
 * REAL name, which is the one outcome worse than an honest unknown.
 */
export interface ZoomRosterObservatory {
  /**
   * Fold one point-in-time read into the accumulated roster.
   *
   * @param names Accepted display names from this read (see `normalizeZoomCandidates`).
   * @param atMs Observation time, injected.
   * @returns Only the names that were not already known, for once-each logging.
   */
  observe(names: readonly string[], atMs: number): string[];
  /**
   * Names observed so far, in first-seen order.
   *
   * @param freshSinceMs When given, only names last observed at or after this
   *   time are returned. Omit for the unfiltered union.
   */
  known(freshSinceMs?: number): string[];
  /** When a name was last observed, or `null` if never. */
  lastSeenMs(name: string): number | null;
  /** How many names have been observed, ever. */
  size(): number;
  /** Forget everything. */
  clear(): void;
}

export function createZoomRosterObservatory(): ZoomRosterObservatory {
  // A Map preserves insertion order in JS, which is the "first-seen" order the
  // interface promises, independently of the last-seen times stored as values.
  const seen = new Map<string, number>();
  return {
    observe(names: readonly string[], atMs: number): string[] {
      const fresh: string[] = [];
      for (const name of names) {
        if (!seen.has(name)) fresh.push(name);
        seen.set(name, atMs);
      }
      return fresh;
    },
    known(freshSinceMs?: number): string[] {
      const out: string[] = [];
      for (const [name, at] of seen) {
        if (freshSinceMs !== undefined && at < freshSinceMs) continue;
        out.push(name);
      }
      return out;
    },
    lastSeenMs(name: string): number | null {
      return seen.has(name) ? seen.get(name)! : null;
    },
    size(): number {
      return seen.size;
    },
    clear(): void {
      seen.clear();
    },
  };
}

/** Outcome of an elimination attempt — a name, or precisely why there is none. */
export type EliminationResult =
  | { name: string; unclaimed: string[] }
  | { name: null; reason: 'empty-roster' | 'all-claimed' | 'ambiguous'; unclaimed: string[] };

/**
 * Elimination: if exactly one roster name is still unclaimed by another track,
 * it must belong to the asking track.
 *
 * FAIL-SAFE by design. With zero unclaimed names, or two or more, this returns
 * `null` and the caller must leave the track unnamed — a wrong name is strictly
 * worse than an honest unknown, because the honest unknown is visible in the
 * transcript and a wrong name is not. This is the same rule Google Meet's
 * Layer 3 uses; Zoom simply never had it.
 *
 * @param roster Accumulated names (see `ZoomRosterObservatory`).
 * @param isTaken Whether a name is already claimed by a DIFFERENT track. The
 *   caller supplies this so the module needs no knowledge of the vote store.
 */
export function pickSoleUnclaimed(
  roster: readonly string[],
  isTaken: (name: string) => boolean,
): EliminationResult {
  if (roster.length === 0) return { name: null, reason: 'empty-roster', unclaimed: [] };
  const unclaimed = roster.filter((name) => !isTaken(name));
  if (unclaimed.length === 1) return { name: unclaimed[0], unclaimed };
  if (unclaimed.length === 0) return { name: null, reason: 'all-claimed', unclaimed };
  return { name: null, reason: 'ambiguous', unclaimed };
}

// ─── Active-speaker selection (shared with the Zoom Web recording poll) ──────
//
// `platforms/zoom/web/recording.ts`'s `startSpeakerPolling` is a SECOND,
// independent source of SPEAKER_START/SPEAKER_END events, and it carried a
// HAND-COPIED duplicate of the name predicate — its own comment said "Name-quality
// guards mirrored from services/speaker-identity.ts". Deleting the leading-lowercase
// case rule from the original therefore did not reach the copy, and `"sujoy sarkar"`
// stayed unreturnable on that path. A mirrored predicate that silently drifts from
// its source is the real defect; the types and function below exist so there is
// exactly ONE definition of what a Zoom display name is, shared by both paths, and
// so the selection logic is testable without a browser (it previously was not).

/** One point-in-time read of Zoom's active-speaker DOM, unjudged. */
export interface ZoomActiveSpeakerRead {
  /** Raw text from the speaker-view active container (Layout 1). */
  layout1: string | null;
  /** Raw text from the screen-share filmstrip's active tile (Layout 2). */
  layout2: string | null;
  /**
   * Layout 3 (gallery view): one entry per candidate "is speaking" selector, in
   * the order they should be tried, each with every raw candidate it matched.
   */
  gallery: Array<{ selector: string; raw: Array<string | null> }>;
}

/** Which layout produced the name — for logging, so a live run is diagnosable. */
export type ZoomActiveSpeakerSource = 'layout1' | 'layout2' | `gallery:${string}`;

/**
 * Pick the active speaker from a raw DOM read.
 *
 * Preserves the pre-existing precedence exactly — Layout 1, then Layout 2, then
 * each gallery selector in order — and the pre-existing gallery rule that a
 * selector is usable ONLY if it yields exactly ONE distinct accepted name (two or
 * more means the selector is too broad on this Zoom UI version, and guessing
 * between them would be the wrong-confident-bind failure this whole change is
 * about). What it no longer does is reject a name for starting with a lowercase
 * letter.
 *
 * @returns The name and which layout found it, or `null` when nothing is usable.
 */
export function pickZoomActiveSpeaker(
  read: ZoomActiveSpeakerRead,
  botName?: string,
): { name: string; source: ZoomActiveSpeakerSource } | null {
  const layout1 = normalizeZoomCandidates([read.layout1], botName);
  if (layout1.length > 0) return { name: layout1[0], source: 'layout1' };

  const layout2 = normalizeZoomCandidates([read.layout2], botName);
  if (layout2.length > 0) return { name: layout2[0], source: 'layout2' };

  for (const { selector, raw } of read.gallery) {
    const names = normalizeZoomCandidates(raw, botName);
    // `normalizeZoomCandidates` already de-duplicates, so `length === 1` IS
    // "exactly one distinct accepted name".
    if (names.length === 1) return { name: names[0], source: `gallery:${selector}` };
  }

  return null;
}
