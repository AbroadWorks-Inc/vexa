import { Page } from 'playwright-core';
import { log } from '../utils';
import {
  createZoomRosterObservatory,
  normalizeZoomCandidates,
  pickSoleUnclaimed,
  type EliminationResult,
} from './zoom-roster';
import { createRepeatCollapser } from './log-throttle';

/**
 * Speaker Identity — discover track→speaker mapping once, lock forever.
 *
 * Google Meet assigns each participant a fixed audio track for the duration
 * of the meeting. The mapping never changes (unless someone leaves and
 * rejoins). We discover it by correlating audio activity with speaking
 * indicators, then lock it permanently.
 *
 * Strategy:
 * 1. When audio arrives on track N and exactly one speaking indicator is active,
 *    record a vote: track N = that speaker.
 * 2. After LOCK_THRESHOLD consistent votes → lock permanently.
 * 3. Locked mappings are never re-evaluated (the mapping is static).
 * 4. If a name is already taken by another track (locked OR leading votes),
 *    don't return it — enforce one-name-per-track, one-track-per-name always.
 *
 * "Taken" deliberately includes LEADING VOTES, not just locks. Locks alone are
 * not enough: `clearSpeakerNameCache()` fires on every participant-count change,
 * and in the window after it nothing is locked yet, so a locks-only test lets two
 * tracks resolve to the same person. That happened in production on 2026-08-12
 * (tracks 0 and 2 both bound to one participant), which did not corrupt the names
 * but badly skewed `speaker_timeline.json` and therefore misattributed speech.
 * See `claimedNameForTrack` for the exact definition and `speaker-attribution.test.ts`
 * for the regression guard.
 */

// ─── Track→Speaker Mapping ───────────────────────────────────────────────────

/** Votes per track: trackIndex → { speakerName → voteCount } */
const trackVotes = new Map<number, Map<string, number>>();

/** Locked mappings: trackIndex → speakerName. Once set, permanent. */
const lockedMappings = new Map<number, string>();

/** Minimum votes to lock (reduced from 3 for faster locking with human participants) */
const LOCK_THRESHOLD = 2;

/** Minimum vote ratio to lock (70%) */
const LOCK_RATIO = 0.7;

/** Track last audio activity time per track (for Zoom active-speaker disambiguation) */
const trackLastAudioMs = new Map<number, number>();

/**
 * `trackIndex:name` pairs already reported as refused, so a refusal is logged once
 * and never per audio chunk. Bounded by tracks × roster size.
 */
const claimRefusalLogged = new Set<string>();

/**
 * The name a track currently believes itself to be: its locked name, or — if not
 * yet locked — the top of its vote tally.
 *
 * The sort expression here is deliberately IDENTICAL to the one every resolver
 * return path uses (`Array.from(votes.entries()).sort((a, b) => b[1] - a[1])`), so
 * "the name this track would hand out" and "the name this track claims" can never
 * disagree — including on ties, where V8's stable sort makes both pick the
 * first-voted name. Each track therefore claims at most ONE name at a time, which
 * is what keeps the guard from over-blocking.
 *
 * @param trackIndex Audio track index.
 * @returns The claimed name, or null if the track has neither a lock nor any vote.
 */
function claimedNameForTrack(trackIndex: number): string | null {
  const locked = lockedMappings.get(trackIndex);
  if (locked) return locked;

  const votes = trackVotes.get(trackIndex);
  if (!votes || votes.size === 0) return null;

  return Array.from(votes.entries()).sort((a, b) => b[1] - a[1])[0][0];
}

/**
 * Check if a name is already taken by another track.
 *
 * "Taken" means another track has CLAIMED it — either locked to it permanently, or
 * currently leading that track's vote tally. The leading-vote half is essential:
 * `clearSpeakerNameCache()` wipes all locks on any participant-count change, so a
 * locks-only test is blind precisely in the window where collisions happen.
 *
 * Because votes are wiped alongside locks, claims rebuild from scratch after a
 * clear — which is what allows Google Meet's genuine track reassignment to take
 * effect — but the FIRST vote a track casts re-establishes its claim immediately,
 * so no second track can pile onto the same name inside that window.
 *
 * @param name Candidate display name.
 * @param excludeTrackIndex Track asking the question; its own claim never blocks it.
 */
export function isNameTaken(name: string, excludeTrackIndex?: number): boolean {
  for (const [idx, lockedName] of lockedMappings) {
    if (idx !== excludeTrackIndex && lockedName === name) return true;
  }
  for (const idx of trackVotes.keys()) {
    if (idx === excludeTrackIndex) continue;
    if (lockedMappings.has(idx)) continue; // its locked name was already checked above
    if (claimedNameForTrack(idx) === name) return true;
  }
  return false;
}

/**
 * Record a vote: track N was active while speaker X was speaking.
 * Supports fractional weights (0.5 for overlapping speech, 1.0 for exclusive).
 * Once locked, votes are ignored for that track.
 */
export function recordTrackVote(trackIndex: number, speakerName: string, weight: number = 1.0): void {
  // Already locked — nothing to do
  if (lockedMappings.has(trackIndex)) return;

  // Don't vote for a name another track has already claimed (locked OR leading).
  // Logged once per track+name so a wrongly-held claim is diagnosable from the bot
  // log without ever logging per audio chunk.
  if (isNameTaken(speakerName, trackIndex)) {
    const key = `${trackIndex}:${speakerName}`;
    if (!claimRefusalLogged.has(key)) {
      claimRefusalLogged.add(key);
      log(`[SpeakerIdentity] Track ${trackIndex} vote for "${speakerName}" refused — claimed by another track`);
    }
    return;
  }

  if (!trackVotes.has(trackIndex)) {
    trackVotes.set(trackIndex, new Map());
  }
  const votes = trackVotes.get(trackIndex)!;
  votes.set(speakerName, (votes.get(speakerName) || 0) + weight);

  // Check if we can lock
  const totalVotes = Array.from(votes.values()).reduce((a, b) => a + b, 0);
  const topEntry = Array.from(votes.entries()).sort((a, b) => b[1] - a[1])[0];

  if (topEntry && topEntry[1] >= LOCK_THRESHOLD && topEntry[1] / totalVotes >= LOCK_RATIO) {
    // Final check: don't lock if the name is taken
    if (isNameTaken(topEntry[0], trackIndex)) {
      log(`[SpeakerIdentity] Track ${trackIndex} would lock to "${topEntry[0]}" but name is taken by another track — skipping`);
      return;
    }
    lockedMappings.set(trackIndex, topEntry[0]);
    log(`[SpeakerIdentity] Track ${trackIndex} → "${topEntry[0]}" LOCKED PERMANENTLY (${topEntry[1]}/${totalVotes} votes, ${(topEntry[1] / totalVotes * 100).toFixed(0)}%)`);
  }
}

/**
 * Get locked speaker name for a track. Returns null if not yet locked.
 */
export function getLockedMapping(trackIndex: number): string | null {
  return lockedMappings.get(trackIndex) ?? null;
}

/**
 * Check if a track is locked.
 */
export function isTrackLocked(trackIndex: number): boolean {
  return lockedMappings.has(trackIndex);
}

/**
 * Report that a track just received audio data.
 * Called from the audio pipeline so Zoom active-speaker voting
 * can disambiguate which track the highlighted name belongs to.
 */
export function reportTrackAudio(trackIndex: number): void {
  trackLastAudioMs.set(trackIndex, Date.now());
}

/**
 * Is this track the most recently active one? (within 500ms tolerance)
 * Used by Zoom to vote active speaker name only on the loudest/most-recent track.
 */
function isMostRecentlyActiveTrack(trackIndex: number): boolean {
  const myTime = trackLastAudioMs.get(trackIndex) || 0;
  if (myTime === 0) return false;
  for (const [idx, time] of trackLastAudioMs) {
    if (idx !== trackIndex && time > myTime + 500) return false;
  }
  return true;
}

// ─── Browser State Query ─────────────────────────────────────────────────────

// (A module-level `isJunkName` lived here purely for the removed Layer 2. The
// equivalent check that is still needed runs INSIDE the page context — see the
// `isJunk` closure in queryBrowserState below — because it has to execute in the
// browser, not in Node.)

/**
 * Query browser for participant names and who's currently speaking.
 */
async function queryBrowserState(
  page: Page,
  botName?: string,
): Promise<{ filteredNames: string[]; speaking: string[] } | null> {
  try {
    return await page.evaluate((selfName: string) => {
      const isJunk = (name: string): boolean => {
        return /^Google Participant \(/.test(name) ||
               /spaces\//.test(name) ||
               /devices\//.test(name);
      };

      // Google Meet CHROME (control / menu / panel labels) that the roster
      // scraper can pick up as if they were participant names. Observed live
      // 2026-09-03: "Backgrounds and effects" (the visual-effects control) was
      // accepted as a speaker and reached BOTH the transcript and the participant
      // list. Matched as a lower-cased substring; every entry is multi-word Meet
      // chrome, so it cannot collide with a real display name. This is a denylist
      // of UI text, NOT of names — only add control/menu strings here.
      const uiLabelPatterns = [
        'let participants', 'send messages', 'turn on captions', 'turn off captions',
        'backgrounds and effects', 'visual effects', 'apply visual effects',
        'more options', 'raise hand', 'lower hand', 'present now', 'stop presenting',
        'turn on microphone', 'turn off microphone', 'turn on camera', 'turn off camera',
        'leave call', 'meeting details', 'chat with everyone', 'add people',
        'call controls', 'host controls',
      ];
      const isUiLabel = (name: string): boolean => {
        const lower = name.toLowerCase();
        return uiLabelPatterns.some(p => lower.includes(p));
      };

      const getNames = (window as any).__vexaGetAllParticipantNames;
      if (typeof getNames !== 'function') return null;

      const data = getNames() as { names: Record<string, string>; speaking: string[] };
      const selfLower = selfName.toLowerCase();

      // A name is rejected if it is the bot itself, a Meet UI label, or structural
      // junk. Applied to BOTH the roster and the speaking[] list so a UI label can
      // never enter voting from either source (previously `speaking` was only
      // isJunk-filtered, which let a scraped control label through).
      const reject = (n: string): boolean => {
        const lower = n.toLowerCase();
        if (lower.includes(selfLower) || selfLower.includes(lower)) return true;
        if (isUiLabel(n)) return true;
        if (isJunk(n)) return true;
        return false;
      };

      const filteredNames = Object.values(data.names).filter(n => !reject(n));
      const speaking = data.speaking.filter(n => !reject(n));

      return { filteredNames, speaking };
    }, botName || 'Vexa Bot');
  } catch (err: any) {
    log(`[SpeakerIdentity] Browser query failed: ${err.message}`);
    return null;
  }
}

/** Which layer resolved each track — logged once per track, never per audio chunk. */
const resolutionLogged = new Set<number>();
/** Tracks whose full-cascade failure has been reported once. */
const allLayersFailedLogged = new Set<number>();

function logResolution(trackIndex: number, name: string, layer: string): void {
  if (resolutionLogged.has(trackIndex)) return;
  resolutionLogged.add(trackIndex);
  log(`[NameResolve] Track ${trackIndex} → "${name}" via layer=${layer}`);
}

// NOTE — the former "Layer 2" (tile containment) has been REMOVED.
//
// It walked up from a track's bound HTMLMediaElement looking for an enclosing
// participant tile to read a name from. The live run of 2026-08-12 settled the
// question: `[TrackTileDebug]` logged the identical ancestor chain for every
// track — `<audio>` (zero attributes) -> `<body>` -> `<html>`. Google Meet
// attaches its audio elements as direct children of `<body>`, nowhere near the
// visual tile DOM, so there is nothing to walk up TO. The strategy was
// architecturally impossible rather than merely broken by a selector change, and
// it resolved 0 of 3 tracks in production while running on every resolution.
//
// Attribution is therefore correlation-based only: audio-activity elimination,
// then the legacy CSS vote, then top-vote. Do not reintroduce a containment
// layer without new DOM evidence — see docs-utpal/CHANGE_LEDGER.md (v8).

// ─── Main Resolution ─────────────────────────────────────────────────────────

/**
 * Resolve speaker name for a Google Meet audio track.
 *
 * If locked → return immediately (permanent).
 * If not locked → query browser, vote if single speaker.
 * Never return a name that's already taken by another track.
 */
async function resolveGoogleMeetSpeakerName(
  page: Page,
  elementIndex: number,
  botName?: string,
): Promise<string | null> {
  // ── Layer 1: locked → permanent, instant return ───────────────────────────
  const locked = getLockedMapping(elementIndex);
  if (locked) return locked;

  // (Layer 2, tile containment, was removed — see the note above this function.)

  // Query browser
  const state = await queryBrowserState(page, botName);
  if (!state) return null;

  const { speaking, filteredNames } = state;

  // ── Layer 3: audio-activity elimination (CSS-INDEPENDENT) ─────────────────
  // The CSS `speaking[]` signal below is empty whenever Meet rotates its
  // obfuscated class names, which is the recurring root cause of SPEAKER_NN.
  // This layer needs no visual speaking indicator at all: if this track is the
  // only one currently emitting audio, and exactly one roster name is not yet
  // locked to another track, then by elimination that name is this track.
  if (isMostRecentlyActiveTrack(elementIndex)) {
    const unclaimed = filteredNames.filter(n => !isNameTaken(n, elementIndex));
    if (unclaimed.length === 1) {
      const candidate = unclaimed[0];
      recordTrackVote(elementIndex, candidate, 1.0);
      const lockedNow = getLockedMapping(elementIndex) || candidate;
      logResolution(elementIndex, lockedNow, 'audio-elimination');
      return lockedNow;
    }
  }

  // ── Layer 4: legacy CSS speaking[] vote (DEMOTED, kept as a bonus signal) ──
  // Non-functional while Meet's classes are rotated, but costs nothing and
  // silently starts helping again if they ever match. No longer the only path.

  // Single speaker → full vote (high confidence)
  if (speaking.length === 1) {
    const candidate = speaking[0];
    if (!isNameTaken(candidate, elementIndex)) {
      recordTrackVote(elementIndex, candidate, 1.0);
      logResolution(elementIndex, candidate, 'css-speaking');
      return getLockedMapping(elementIndex) || candidate;
    }
  }

  // Two speakers overlapping → half vote for each (common in real conversation)
  if (speaking.length === 2) {
    for (const candidate of speaking) {
      if (!isNameTaken(candidate, elementIndex)) {
        recordTrackVote(elementIndex, candidate, 0.5);
      }
    }
    // Return locked name if just locked, or top voted
    const justLocked = getLockedMapping(elementIndex);
    if (justLocked) {
      logResolution(elementIndex, justLocked, 'css-speaking-overlap');
      return justLocked;
    }
  }

  // ── Layer 5: existing top-vote fallback ───────────────────────────────────
  // Zero or 3+ speaking — can't vote.
  // Return top voted name only if it's not taken by another track.
  const votes = trackVotes.get(elementIndex);
  if (votes && votes.size > 0) {
    const sorted = Array.from(votes.entries()).sort((a, b) => b[1] - a[1]);
    for (const [name] of sorted) {
      if (!isNameTaken(name, elementIndex)) {
        logResolution(elementIndex, name, 'top-vote');
        return name;
      }
    }
  }

  // All layers failed. index.ts has an independent 15s roster-order fallback that
  // triggers off this null — do not duplicate it here.
  if (!resolutionLogged.has(elementIndex) && !allLayersFailedLogged.has(elementIndex)) {
    allLayersFailedLogged.add(elementIndex);
    log(`[NameResolve] Track ${elementIndex} UNRESOLVED — all layers failed (tile=null, activity-elimination=no, css-speaking=${speaking.length}, votes=${votes?.size ?? 0}); roster-order fallback will apply after 15s`);
  }
  return null;
}

// ─── Teams DOM Traversal ─────────────────────────────────────────────────────

const TEAMS_SELECTORS = {
  participantContainer: [
    '[data-tid*="video-tile"]',
    '[data-tid*="videoTile"]',
    '[data-tid*="participant"]',
    '[data-tid*="roster-item"]',
    '.participant-tile',
    '.video-tile',
  ],
  nameElement: [
    '[data-tid*="display-name"]',
    '[data-tid*="participant-name"]',
    '.participant-name',
    '.display-name',
    '.user-name',
    '.roster-item-name',
    '.video-tile-name',
    'span[title]',
    '.ms-Persona-primaryText',
  ],
};

/**
 * DOM traversal for Teams: walk up from a media element to find a name.
 */
async function traverseTeamsDOM(page: Page, elementIndex: number): Promise<string | null> {
  return await page.evaluate(
    ({ idx, containerSelectors, nameSelectors }) => {
      const mediaElements = Array.from(
        document.querySelectorAll('audio, video')
      ).filter((el: any) =>
        !el.paused &&
        el.srcObject instanceof MediaStream &&
        (el.srcObject as MediaStream).getAudioTracks().length > 0
      );

      const targetElement = mediaElements[idx] as HTMLElement | undefined;
      if (!targetElement) return null;

      let current: HTMLElement | null = targetElement;
      while (current && current !== document.body) {
        for (const cs of containerSelectors) {
          if (current.matches(cs)) {
            for (const ns of nameSelectors) {
              const nameEl = current.querySelector(ns);
              if (nameEl) {
                const text = (nameEl.textContent || '').trim();
                if (text.length > 0) return text;
                const title = nameEl.getAttribute('title');
                if (title && title.trim().length > 0) return title.trim();
              }
            }
          }
        }
        current = current.parentElement;
      }

      const ariaLabel = targetElement.getAttribute('aria-label');
      if (ariaLabel && ariaLabel.trim().length > 0) return ariaLabel.trim();

      const titled = targetElement.closest('[title]');
      if (titled) {
        const title = titled.getAttribute('title');
        if (title && title.trim().length > 0) return title.trim();
      }

      return null;
    },
    {
      idx: elementIndex,
      containerSelectors: TEAMS_SELECTORS.participantContainer,
      nameSelectors: TEAMS_SELECTORS.nameElement,
    }
  );
}

/**
 * Teams speaker resolution: DOM traversal + voting/locking (same system as Google Meet).
 * DOM traversal provides the name candidate, voting provides consistency and uniqueness.
 */
async function resolveTeamsSpeakerName(
  page: Page,
  elementIndex: number,
): Promise<string | null> {
  // Locked → permanent, instant return
  const locked = getLockedMapping(elementIndex);
  if (locked) return locked;

  // Try DOM traversal
  const domName = await traverseTeamsDOM(page, elementIndex);

  if (domName) {
    // Don't return a name already taken by another track
    if (isNameTaken(domName, elementIndex)) return null;

    recordTrackVote(elementIndex, domName);
    return getLockedMapping(elementIndex) || domName;
  }

  // No DOM name — return top voted if not taken
  const votes = trackVotes.get(elementIndex);
  if (votes && votes.size > 0) {
    const sorted = Array.from(votes.entries()).sort((a, b) => b[1] - a[1]);
    for (const [name] of sorted) {
      if (!isNameTaken(name, elementIndex)) return name;
    }
  }

  return null;
}

// ─── Zoom Speaker Resolution ─────────────────────────────────────────────────
//
// ── The 2026-09-01 misattribution, and what changed ─────────────────────────
// A live 2-human Zoom meeting attributed ALL speech to one person: 66 timeline
// intervals, every one of them the same participant, and one speaker label in
// `transcript.txt`. Three defects in this section produced that, and all three
// are addressed below. See `zoom-roster.ts` for the full evidence trail.
//
//   1. The in-page `looksLikeName` predicate rejected any candidate starting
//      with a lowercase Latin letter (an anti-chat-sentence heuristic). The
//      second participant's display name was `"sujoy sarkar"`, so their name was
//      UNRETURNABLE by construction while `"Utpalendu Sarkar"` resolved on its
//      first two votes. Fixed by deleting the case rule and moving every
//      judgement into `looksLikeZoomDisplayName` (Node-side, unit-tested).
//
//   2. Only two hardcoded layouts were consulted for the active speaker
//      (`.speaker-active-container__video-frame`, and the screen-share
//      filmstrip's `--active` tile). The live log recorded `Could not confirm
//      Speaker View after retries`, i.e. the bot sat in GALLERY view, where
//      neither selector exists — so Path 2 could never fire. Fixed by widening
//      to a candidate list that includes gallery view and two
//      rename-tolerant `[class*=...]` patterns.
//
//   3. Zoom exposed exactly ONE participant name at any instant — the live log
//      read `roster=[Utpalendu Sarkar]` twenty times, then later
//      `roster=[sujoy sarkar]`, not once both together in 21 observations (one
//      run, so whether that is inherent to Zoom is UNKNOWN) — and Zoom had no
//      elimination layer
//      at all (Google Meet's Layer 3 has one). Fixed by accumulating the roster
//      OVER TIME in `zoomRoster` and eliminating against that union.
//
// The browser side of this section is now deliberately DUMB: it returns raw
// strings and a DOM census, and every decision happens in Node where it can be
// tested. A predicate written inline in `page.evaluate` cannot close over an
// import, so it can only be exercised by a real browser — which is precisely how
// defect 1 survived.

/**
 * Union, over the session, of every Zoom display name we have observed.
 *
 * Deliberately NOT cleared by `clearSpeakerNameCache()`: that fires on every
 * participant-count change, and the accumulation this depends on takes many
 * polls to build precisely because Zoom reveals one name at a time. Names do not
 * stop being real when tracks are reassigned. Staleness is handled by the
 * freshness bound below instead of by wiping.
 */
const zoomRoster = createZoomRosterObservatory();

/**
 * How recently a roster name must have been observed to be eliminable.
 *
 * UNTUNED — a judgement call, not a measurement. It guards one hazard: a
 * participant who LEAVES cannot be detected from a DOM that never lists more
 * than one person, so an unbounded union would keep their name eliminable and
 * could eventually hand it to a different human — a wrong REAL name, the one
 * outcome worse than an honest unknown. Generous on purpose: a present but quiet
 * participant may go a long time without Zoom rendering their tile footer.
 */
export const ZOOM_ROSTER_FRESHNESS_MS = 5 * 60_000;

/**
 * `trackIndex:name` pairs already claimed from an UNCOVERED elimination, so such a
 * claim is cast exactly ONCE and can never accumulate to a lock. See Layer Z4.
 */
const zoomAssumedClaims = new Set<string>();

/**
 * Vote weight for an elimination that rests on an assumption rather than on real
 * elimination information.
 *
 * Must stay below `LOCK_THRESHOLD`, but the weight ALONE is not the safeguard —
 * votes accumulate, so a repeated 0.5 reaches 2.0 and LOCKS on the fourth poll
 * (measured, not assumed). The safeguard is that the claim is cast at most once
 * per track+name (`zoomAssumedClaims`), which caps a track's assumed evidence at
 * this value forever. A lock therefore REQUIRES a covered reading.
 */
const ZOOM_ASSUMED_VOTE_WEIGHT = 0.5;

/** Collapses the per-track "still unmapped" diagnostic (was 278 identical lines). */
const zoomMissCollapser = createRepeatCollapser();

/**
 * Tile name-footer selectors. The verified one first; the `[class*=...]` variant
 * is a deliberate hedge against Zoom renaming the class, which is what has
 * broken this path before.
 */
const ZOOM_FOOTER_SELECTORS: readonly string[] = [
  '.video-avatar__avatar-footer',
  '[class*="avatar-footer"]',
];

/**
 * Active-speaker tile selectors, most specific first.
 *
 * The first two are the pre-existing pair and only exist in Speaker View or
 * during a screen share. The rest were added after the live gallery-view failure
 * (defect 2): a gallery active tile, then two rename-tolerant patterns. Order
 * matters — the first selector that yields an accepted name wins, so the
 * specific ones are consulted before the loose ones.
 */
const ZOOM_ACTIVE_TILE_SELECTORS: readonly string[] = [
  '.speaker-active-container__video-frame',
  '.speaker-bar-container__video-frame--active',
  '.gallery-video-container__video-frame--active',
  '[class*="video-frame--active"]',
  '[class*="video-avatar"][class*="--active"]',
  '[class*="active-speaker"]',
];

/**
 * Ancestors that may be a participant TILE.
 *
 * A hard requirement for Layer Z2, not a hint: without it the walk-up below
 * accepts ANY ancestor, up to and including Zoom's app root. Substring matching
 * throughout, because Zoom renames these classes between releases and an exact
 * class would silently disable the layer instead of failing loudly.
 */
const ZOOM_TILE_SELECTOR =
  '[class*="video-avatar"], [class*="video-container"], [class*="gallery-video"], [class*="video-frame"]';

/**
 * How many ancestors Layer Z2 may climb before giving up.
 *
 * A real participant tile is a handful of levels above its media element. The
 * cap exists so that when the shape and single-media guards BOTH somehow pass on
 * a container far above the tile, the walk still cannot reach the app root.
 */
const ZOOM_TILE_MAX_DEPTH = 6;

/** Participants-panel row selectors — a name source that survives any view mode. */
const ZOOM_PANEL_NAME_SELECTORS: readonly string[] = [
  '.participants-item__display-name',
  '[class*="participants-item"] [class*="display-name"]',
  '[class*="participants-list"] [class*="display-name"]',
];

/** One page read: raw candidate strings plus the DOM census that explains a miss. */
interface ZoomDomRead {
  /** Raw footer text(s) reachable by walking up from THIS track's media element. */
  trackCandidates: string[];
  /** Raw footer / panel text from anywhere on the page. */
  rosterCandidates: string[];
  /** Raw text from whichever active-speaker selector matched first. */
  activeCandidates: string[];
  /** Diagnostics — the DOM state that actually caused a miss. */
  census: {
    mediaElements: number;
    footers: number;
    panelRows: number;
    /** Which active-speaker selector matched, or null when none did. */
    activeSelector: string | null;
    /** Whether a media element existed at this track's index. */
    trackElementFound: boolean;
    /** Ancestors walked before a single-footer tile was found (-1 = never found). */
    tileDepth: number;
    /** Why the walk-up produced nothing — the actual cause, for a live log. */
    tileReject: string;
  };
}

/**
 * Read every Zoom name source in ONE round trip, without judging any of them.
 *
 * The "exactly one footer under this ancestor" rule in the walk-up matters: the
 * previous implementation accepted the FIRST footer found under any ancestor, so
 * a broad ancestor containing several tiles would hand an arbitrary
 * participant's name to this track. An ancestor holding two or more footers is
 * by definition not this participant's tile, so it is skipped.
 *
 * UNVERIFIED without a live browser: whether Zoom's per-participant `<audio>`
 * elements sit inside their own tile subtree at all. Google Meet's do not (they
 * hang off `<body>`), which is why Meet's containment layer was deleted. If
 * Zoom's are the same, `trackCandidates` will always be empty and elimination is
 * the only path that can work — which is exactly why elimination was added.
 */
async function readZoomDom(
  page: Page,
  elementIndex: number,
): Promise<ZoomDomRead | null> {
  try {
    return await page.evaluate(
      ({ idx, footerSels, activeSels, panelSels, tileSelector, maxDepth }) => {
        const textOf = (el: Element | null): string | null => {
          if (!el) return null;
          const span = el.querySelector('span');
          const raw = span?.textContent ?? (el as HTMLElement).innerText ?? el.textContent;
          return raw == null ? null : String(raw);
        };

        const mediaElements = Array.from(document.querySelectorAll('audio, video')).filter(
          (el: Element) => {
            const m = el as HTMLMediaElement;
            return (
              !m.paused &&
              m.srcObject instanceof MediaStream &&
              (m.srcObject as MediaStream).getAudioTracks().length > 0
            );
          },
        );

        // ── This track's own tile, by walking up ──────────────────────────────
        //
        // THREE guards, all required. The previous version had only the
        // footer-count rule, and that rule is sound in one direction only:
        // "two or more footers ⇒ not this tile" is true, but the converse it
        // relied on is FALSE. When only ONE footer exists on the whole page —
        // exactly the state measured live, where the roster read
        // `roster=[Utpalendu Sarkar]` twenty times — then EVERY ancestor up to
        // the app root contains exactly one, so the walk ran to the root and
        // bound whichever single name happened to be rendered. Reproduced: a
        // track whose media element is nested in one branch, with the page's only
        // footer in a SIBLING branch, resolved to that sibling's name. Because
        // `LOCK_THRESHOLD` is 2 and this layer has no most-recently-active guard,
        // the wrong name then LOCKS — and `clearSpeakerNameCache()` never fires on
        // Zoom (see the note on `clearZoomRoster`), so it stands for the whole
        // meeting. That is the very bug this file exists to fix, via another layer.
        //
        //   1. SHAPE     — the ancestor must look like a participant tile.
        //   2. EXCLUSIVE — it must contain exactly ONE live media element, i.e.
        //                  this track's. A container holding several is shared
        //                  infrastructure (the app root holds them all), never a
        //                  tile. This is the guard that kills the reproduction.
        //   3. DEPTH     — bounded climb, as a backstop if 1 and 2 both pass high up.
        //
        // Failing all three is SAFE: Layer Z2 yields nothing and resolution falls
        // through to Z4's elimination, which is audio-derived. Yielding nothing is
        // always better than yielding somebody else's name.
        const trackCandidates: string[] = [];
        const target = mediaElements[idx] as HTMLElement | undefined;
        let tileDepth = -1;
        let tileReject = 'no-media-element';
        if (target) {
          tileReject = 'reached-body';
          let current: HTMLElement | null = target;
          let depth = 0;
          walk: while (current && current !== document.body) {
            if (depth > maxDepth) {
              tileReject = 'depth-cap';
              break;
            }
            // The media element itself is never a tile, so skip depth 0's shape test.
            if (current !== target && current.matches(tileSelector)) {
              const liveMediaHere = Array.from(current.querySelectorAll('audio, video')).filter(
                (el: Element) => {
                  const m = el as HTMLMediaElement;
                  return (
                    !m.paused &&
                    m.srcObject instanceof MediaStream &&
                    (m.srcObject as MediaStream).getAudioTracks().length > 0
                  );
                },
              ).length;
              if (liveMediaHere > 1) {
                // Shared container, not a tile. Keep climbing only to record the
                // cause; a container above this one can only be more shared.
                tileReject = 'shared-container';
              } else {
                for (const sel of footerSels) {
                  const found = current.querySelectorAll(sel);
                  if (found.length !== 1) {
                    tileReject = found.length === 0 ? 'no-footer-in-tile' : 'multi-footer';
                    continue;
                  }
                  const text = textOf(found[0]);
                  if (text != null) {
                    trackCandidates.push(text);
                    tileDepth = depth;
                    tileReject = 'none';
                    break walk;
                  }
                }
              }
            }
            current = current.parentElement;
            depth++;
          }
        }

        // ── Page-wide roster: every tile footer + every panel row ─────────────
        const rosterCandidates: string[] = [];
        let footers = 0;
        for (const sel of footerSels) {
          const all = document.querySelectorAll(sel);
          if (sel === footerSels[0]) footers = all.length;
          all.forEach((el) => {
            const text = textOf(el);
            if (text != null) rosterCandidates.push(text);
          });
        }
        let panelRows = 0;
        for (const sel of panelSels) {
          const all = document.querySelectorAll(sel);
          panelRows += all.length;
          all.forEach((el) => {
            const raw = (el as HTMLElement).textContent;
            if (raw != null) rosterCandidates.push(String(raw));
          });
        }

        // ── Active speaker: first selector that yields any text wins ──────────
        const activeCandidates: string[] = [];
        let activeSelector: string | null = null;
        for (const sel of activeSels) {
          const container = document.querySelector(sel);
          if (!container) continue;
          let text: string | null = null;
          for (const fSel of footerSels) {
            text = textOf(container.querySelector(fSel));
            if (text != null && String(text).trim().length > 0) break;
            text = null;
          }
          // A tile that IS the footer (loose selectors can match it directly).
          if (text == null) text = textOf(container);
          if (text != null && String(text).trim().length > 0) {
            activeCandidates.push(text);
            activeSelector = sel;
            break;
          }
        }

        return {
          trackCandidates,
          rosterCandidates,
          activeCandidates,
          census: {
            mediaElements: mediaElements.length,
            footers,
            panelRows,
            activeSelector,
            trackElementFound: !!target,
            tileDepth,
            tileReject,
          },
        };
      },
      {
        idx: elementIndex,
        footerSels: ZOOM_FOOTER_SELECTORS as string[],
        activeSels: ZOOM_ACTIVE_TILE_SELECTORS as string[],
        panelSels: ZOOM_PANEL_NAME_SELECTORS as string[],
        tileSelector: ZOOM_TILE_SELECTOR,
        maxDepth: ZOOM_TILE_MAX_DEPTH,
      },
    );
  } catch (err: unknown) {
    log(`[SpeakerIdentity] Zoom DOM read failed: ${err instanceof Error ? err.message : String(err)}`);
    return null;
  }
}

/**
 * Clear the accumulated Zoom roster.
 *
 * Test seam for this module's session-scoped state, and the hook a meeting
 * teardown in `index.ts` should call so a capture restart does not inherit the
 * previous meeting's names. It is deliberately NOT called by
 * `clearSpeakerNameCache()` — see `zoomRoster`.
 *
 * ── KNOWN DEBT: a wrong lock is PERMANENT on Zoom ───────────────────────────
 * `clearSpeakerNameCache()` is the only thing that can undo a wrong
 * `lockedMappings` entry, and on Zoom it NEVER RUNS. Its sole caller
 * (`index.ts:1721-1734`) probes the participant count with a page evaluate that
 * understands only Google Meet's `getGoogleMeetActiveParticipantsCount()` and
 * Teams' `[data-tid*="video-tile"]` tiles, and otherwise `return 0`. Zoom matches
 * neither, so `currentCount` is always 0, `lastParticipantCount` stays 0, and the
 * invalidation gate `lastParticipantCount > 0 && currentCount !== lastParticipantCount`
 * is never satisfied.
 *
 * Consequence: `LOCK_THRESHOLD` is 2, so any layer that votes twice for a wrong
 * name locks it for the entire meeting, `isNameTaken` then denies that name to its
 * real owner forever, and nothing recovers. That is why every Zoom layer here is
 * built to REFUSE rather than to guess and self-correct later — there is no
 * later. Fixing the probe would need a Zoom branch in `index.ts`, which is
 * outside this change's allowlist; recorded here so it is not rediscovered.
 */
export function clearZoomRoster(): void {
  zoomRoster.clear();
  zoomMissCollapser.reset();
  // Audio telemetry is per-meeting. `clearSpeakerNameCache()` deliberately keeps
  // it (a participant join/leave does not invalidate who is currently talking —
  // see that function), but a meeting teardown must drop it: a stale entry makes
  // a track that has not existed for an hour still look "recently active", which
  // is the one input Layer Z4 trusts.
  trackLastAudioMs.clear();
  zoomAssumedClaims.clear();
}

/** How many tracks have ever produced audio this meeting. */
function audioActiveTrackCount(): number {
  return trackLastAudioMs.size;
}

/** Snapshot of the accumulated roster, for diagnostics and tests. */
export function getZoomRosterNames(): string[] {
  return zoomRoster.known();
}

/**
 * Zoom speaker resolution.
 *
 * Layer Z1 — locked mapping (permanent, instant).
 * Layer Z2 — this track's own tile footer, by walking up from its media element.
 * Layer Z3 — active-speaker tile, credited only to the most recently active track.
 * Layer Z4 — elimination over the ACCUMULATED roster (the fix for defect 3).
 * Layer Z5 — top existing vote.
 *
 * Every layer honours one-name-per-track / one-track-per-name via `isNameTaken`,
 * and every layer that assigns does so through `recordTrackVote`, so the
 * lock/threshold machinery is identical to Google Meet's.
 */
async function resolveZoomSpeakerName(
  page: Page,
  elementIndex: number,
  botName?: string,
  nowMs: number = Date.now(),
): Promise<string | null> {
  // ── Layer Z1: locked → permanent, instant return ───────────────────────────
  const locked = getLockedMapping(elementIndex);
  if (locked) return locked;

  const read = await readZoomDom(page, elementIndex);
  if (!read) return null;

  const trackNames = normalizeZoomCandidates(read.trackCandidates, botName);
  const activeNames = normalizeZoomCandidates(read.activeCandidates, botName);
  const rosterNames = normalizeZoomCandidates(read.rosterCandidates, botName);

  // Everything we saw this poll feeds the union — including the active-speaker
  // and per-track reads, which in Speaker View are often the ONLY name on screen.
  const fresh = zoomRoster.observe(
    normalizeZoomCandidates([...rosterNames, ...activeNames, ...trackNames], botName),
    nowMs,
  );
  if (fresh.length > 0) {
    log(`[SpeakerIdentity] Zoom roster grew by ${fresh.length} name(s) — ${zoomRoster.size()} known this session`);
  }

  // ── Layer Z2: this track's own tile ────────────────────────────────────────
  for (const candidate of trackNames) {
    if (isNameTaken(candidate, elementIndex)) continue;
    recordTrackVote(elementIndex, candidate);
    const resolved = getLockedMapping(elementIndex) || candidate;
    // `tileDepth` is included because a WRONG Z2 bind is otherwise invisible in the
    // logs: it appeared only in the miss census, i.e. only when Z2 found nothing.
    // The depth is what tells you whether the name came from the immediate tile or
    // from something several levels up.
    logResolution(elementIndex, resolved, `zoom-tile(depth=${read.census.tileDepth})`);
    return resolved;
  }

  // ── Layer Z3: active-speaker correlation ───────────────────────────────────
  // Credited ONLY to the most recently active track, otherwise every track votes
  // for the same highlighted name at once and they all collide in `isNameTaken`.
  if (activeNames.length > 0 && isMostRecentlyActiveTrack(elementIndex)) {
    for (const candidate of activeNames) {
      if (isNameTaken(candidate, elementIndex)) continue;
      recordTrackVote(elementIndex, candidate, 1.0);
      const resolved = getLockedMapping(elementIndex) || candidate;
      logResolution(elementIndex, resolved, `zoom-active(${read.census.activeSelector ?? '?'})`);
      return resolved;
    }
  }

  // ── Layer Z4: elimination over the accumulated roster ──────────────────────
  // If this track is the one currently emitting audio and exactly one
  // recently-seen roster name is still unclaimed, that name is this track by
  // elimination. FAIL-SAFE: with zero or two-or-more unclaimed names nothing is
  // assigned and the track stays honestly unknown.
  //
  // The `isMostRecentlyActiveTrack` half of this is audio-derived and therefore
  // camera-independent — it is fed by `reportTrackAudio` from the audio pipeline,
  // not by any DOM read. The roster half reads `.video-avatar__avatar-footer`,
  // which is the AVATAR tile's name label (the tile Zoom renders when a
  // participant's video is OFF) and which the failing live run proves was
  // present throughout. So this layer does not depend on anyone having a camera
  // on — unlike Layer Z3, whose selectors are all `…video-frame…` shaped.
  //
  // UNPROVEN, and the residual risk in this layer: that the single footer Zoom
  // renders belongs to the SPEAKING participant. If it is arbitrary instead,
  // a one-name roster can bind to the wrong track. See the `covered` check
  // below, which labels exactly that case instead of hiding it. Closing it
  // properly needs a per-track audio LEVEL — see
  // `platforms/zoom/web/observe.ts`, which already collects
  // `inbound-rtp.audioLevel` per receiver and per-element AnalyserNode RMS, but
  // only as a `ZOOM_OBSERVE=true` diagnostic dump that nothing consumes.
  // `null` means the layer was never entered — a distinct diagnostic state from
  // "entered and found nothing", and reporting them as the same thing sent an
  // earlier reading of this code looking for a roster bug that did not exist.
  let elimination: EliminationResult | null = null;
  if (isMostRecentlyActiveTrack(elementIndex)) {
    elimination = pickSoleUnclaimed(
      zoomRoster.known(nowMs - ZOOM_ROSTER_FRESHNESS_MS),
      (name) => isNameTaken(name, elementIndex),
    );
    if (elimination.name !== null) {
      // ── Is this REAL elimination, or an assumption wearing its clothes? ─────
      //
      // Elimination only carries information when the roster can account for every
      // track that has produced audio AND there is more than one such track. The
      // second half was missing and it is the common case: early in a 2-person
      // meeting only one track has spoken, so one known name gave
      // `1 >= 1 === true` and the bind was logged as a TRUE elimination — while a
      // one-name/one-track reading carries ZERO elimination information. It rests
      // entirely on the unproven assumption that the single footer Zoom renders
      // belongs to the SPEAKING participant. If that is wrong, the names are
      // swapped for the whole meeting.
      const knownNames = zoomRoster.known(nowMs - ZOOM_ROSTER_FRESHNESS_MS).length;
      const audioTracks = audioActiveTrackCount();
      const covered = audioTracks >= 2 && knownNames >= audioTracks;

      if (covered) {
        recordTrackVote(elementIndex, elimination.name, 1.0);
        const resolved = getLockedMapping(elementIndex) || elimination.name;
        logResolution(elementIndex, resolved, 'zoom-elimination');
        return resolved;
      }

      // ── Uncovered: may CLAIM, may never LOCK ───────────────────────────────
      //
      // Three options were on the table and only this one works.
      //
      //   Full weight — what this code did — locks on the second poll, and on Zoom
      //   a lock is FOREVER (`clearSpeakerNameCache()` never fires there; see
      //   `clearZoomRoster`). Unacceptable.
      //
      //   A repeated half weight does NOT fix it: votes accumulate, so 0.5 reaches
      //   `LOCK_THRESHOLD` (2) and locks on the FOURTH poll — measured. At a
      //   1-2s poll cadence that converts a permanent wrong lock into a permanent
      //   wrong lock a few seconds later.
      //
      //   Hard refusal is honest — `syntheticSpeakerLabel` now guarantees the track
      //   a distinct, visibly-unresolved identity, so refusing costs a placeholder
      //   rather than silence, which is why the old "refusing would leave the
      //   meeting with no names" justification no longer holds. But it DEADLOCKS
      //   this layer: with two tracks, two known names and nothing yet claimed,
      //   every elimination is ambiguous forever, so nobody ever gets a real name.
      //
      // So: cast the claim exactly ONCE, at a weight that alone can never lock.
      // The single vote is enough to make `isNameTaken` treat the name as claimed
      // (a leading vote is a claim), which is what BOOTSTRAPS elimination for the
      // other track — and because it never repeats, it stays displaceable by any
      // later confident signal and can never become permanent on its own. A lock
      // now REQUIRES a covered reading.
      const claimKey = `${elementIndex}:${elimination.name}`;
      if (!zoomAssumedClaims.has(claimKey)) {
        zoomAssumedClaims.add(claimKey);
        recordTrackVote(elementIndex, elimination.name, ZOOM_ASSUMED_VOTE_WEIGHT);
        log(`[SpeakerIdentity] Track ${elementIndex} LOW CONFIDENCE: assumed "${elimination.name}" — ${knownNames} known name(s) for ${audioTracks} audio-active track(s), so this is not a true elimination. Claimed once at weight ${ZOOM_ASSUMED_VOTE_WEIGHT}; it can never lock on its own.`);
      }
      return elimination.name;
    }
  }

  // ── Layer Z5: top existing vote ────────────────────────────────────────────
  const votes = trackVotes.get(elementIndex);
  if (votes && votes.size > 0) {
    const sorted = Array.from(votes.entries()).sort((a, b) => b[1] - a[1]);
    for (const [name] of sorted) {
      if (!isNameTaken(name, elementIndex)) {
        logResolution(elementIndex, name, 'zoom-top-vote');
        return name;
      }
    }
  }

  // ── Every layer missed. Report the DOM state that caused it, once ──────────
  // Not the miss itself — the CAUSE. The live run logged 278 content-free
  // "not yet mapped" lines; this logs the census, collapses identical repeats to
  // a heartbeat, and speaks up immediately whenever the situation changes.
  const c = read.census;
  const signature = [
    `media=${c.mediaElements}`,
    `el=${c.trackElementFound ? 'yes' : 'no'}`,
    `tileDepth=${c.tileDepth}`,
    `tileReject=${c.tileReject}`,
    `footers=${c.footers}`,
    `panel=${c.panelRows}`,
    `active=${c.activeSelector ?? 'none'}`,
    `roster=${zoomRoster.size()}`,
    `recent=${isMostRecentlyActiveTrack(elementIndex) ? 'yes' : 'no'}`,
    `elim=${elimination === null ? 'not-attempted' : elimination.name === null ? elimination.reason : 'hit'}`,
    `unclaimed=${elimination?.unclaimed.length ?? 0}`,
    `votes=${votes?.size ?? 0}`,
  ].join(' ');
  const line = zoomMissCollapser.consider(
    `zoom-miss:${elementIndex}`,
    signature,
    `[SpeakerIdentity] Zoom track ${elementIndex} UNRESOLVED — ${signature}`,
    nowMs,
  );
  if (line) log(line);

  return null;
}

// ─── Main Resolution ─────────────────────────────────────────────────────────

/**
 * Resolve speaker name for any platform.
 * Google Meet: speaking-indicator correlation → voting → permanent lock.
 * Teams: DOM traversal → voting → permanent lock.
 * Zoom: DOM traversal + active speaker correlation → voting → permanent lock.
 * All enforce one-name-per-track, one-track-per-name.
 */
export async function resolveSpeakerName(
  page: Page,
  elementIndex: number,
  platform: string,
  botName?: string,
  nowMs: number = Date.now(),
): Promise<string> {
  let name: string | null = null;

  if (platform === 'googlemeet') {
    name = await resolveGoogleMeetSpeakerName(page, elementIndex, botName);
  } else if (platform === 'msteams') {
    name = await resolveTeamsSpeakerName(page, elementIndex);
  } else if (platform === 'zoom') {
    name = await resolveZoomSpeakerName(page, elementIndex, botName, nowMs);
  } else {
    log(`[SpeakerIdentity] Unknown platform "${platform}" — returning empty`);
    return '';
  }

  if (name) {
    log(`[SpeakerIdentity] Element ${elementIndex} → "${name}" (platform: ${platform})`);
    return name;
  }

  // A miss is reported by the platform resolver, WITH the DOM state that caused
  // it and with identical repeats collapsed. This used to log an unconditional
  // content-free line per poll per track — 278 of them in the 2026-09-01 live
  // run, 222 for one track — which buried every line that carried information.
  // Zoom reports through `zoomMissCollapser`; Meet through `allLayersFailedLogged`.
  const missReporter = missReporterFor(platform);
  const line = unresolvedMissCollapser.consider(
    `unresolved:${platform}:${elementIndex}`,
    missReporter,
    `[SpeakerIdentity] Element ${elementIndex} → "" (platform: ${platform}, not yet mapped)`,
    nowMs,
  );
  if (line) log(line);
  return '';
}

/**
 * Collapses the platform-agnostic "not yet mapped" line. Its signature is just
 * the platform, so the line appears once per track immediately and thereafter
 * only on the heartbeat — the diagnostic detail lives in the per-platform miss
 * report, which is signature-driven on the actual DOM census.
 */
const unresolvedMissCollapser = createRepeatCollapser();

/** Signature for the generic miss line: the platform, which never changes mid-session. */
function missReporterFor(platform: string): string {
  return `platform=${platform}`;
}

// ─── Lifecycle ───────────────────────────────────────────────────────────────

/**
 * Clear all track→speaker mappings. Called on every participant-count change,
 * because Google Meet genuinely reassigns audio tracks when someone joins or
 * leaves, which can make an existing lock wrong.
 *
 * Votes and locks are both wiped — that is the point, and it is what lets a
 * reassigned track acquire its new identity. The uniqueness invariant survives the
 * wipe not by retaining state but because `isNameTaken` counts a track's LEADING
 * VOTE as a claim: the first vote cast after the wipe re-establishes the claim
 * before any other track can resolve to the same name.
 *
 * `trackLastAudioMs` is deliberately NOT cleared. It is audio telemetry, not
 * identity, it is repopulated by the audio pipeline on the very next chunk, and
 * wiping it makes `isMostRecentlyActiveTrack` — hence Layer 3's elimination — more
 * trigger-happy at exactly the moment identity is least certain.
 */
export function clearSpeakerNameCache(): void {
  trackVotes.clear();
  lockedMappings.clear();
  // Re-arm the once-per-track diagnostics so the post-invalidation re-resolution
  // is visible in the logs — otherwise which-layer-won is silently lost after the
  // first participant join/leave.
  resolutionLogged.clear();
  allLayersFailedLogged.clear();
  claimRefusalLogged.clear();
  // Re-arm the collapsed miss diagnostics: after a track reassignment the very
  // next miss is the interesting one, and a stale heartbeat window would swallow
  // it. The accumulated Zoom roster is deliberately NOT cleared here — see
  // `zoomRoster` for why.
  zoomMissCollapser.reset();
  unresolvedMissCollapser.reset();
  // Votes are wiped above, so the assumed claims they represent are gone too; not
  // clearing this would permanently prevent the claim from being re-cast and
  // deadlock Layer Z4 after any cache clear.
  zoomAssumedClaims.clear();
  log('[SpeakerIdentity] All track mappings cleared.');
}

/**
 * Remove mapping for a single track (participant left).
 *
 * Deleting this track's votes and lock is what releases its claim on a name, so
 * the name becomes available to whichever track the participant's audio moves to
 * — that is what keeps the stricter `isNameTaken` from deadlocking a legitimate
 * re-resolution. Every other per-track bookkeeping set must be cleared alongside
 * them, for the same reason `clearSpeakerNameCache()` clears them wholesale:
 * a once-per-track record that outlives the votes it describes silently prevents
 * the thing it was recording from ever happening again. `zoomAssumedClaims` is
 * the one that bites hardest — an assumed elimination claim is cast once per
 * track+name, so leaving it behind would make Layer Z4 permanently dead for this
 * track.
 */
export function invalidateSpeakerName(platform: string, elementIndex: number): void {
  trackVotes.delete(elementIndex);
  lockedMappings.delete(elementIndex);
  resolutionLogged.delete(elementIndex);
  allLayersFailedLogged.delete(elementIndex);
  // Re-arm the per-track, per-name records. Same prefix scan for both sets; the
  // trailing colon makes the match unambiguous (track 1 never matches track 11).
  for (const key of claimRefusalLogged) {
    if (key.startsWith(`${elementIndex}:`)) claimRefusalLogged.delete(key);
  }
  for (const key of zoomAssumedClaims) {
    if (key.startsWith(`${elementIndex}:`)) zoomAssumedClaims.delete(key);
  }
  log(`[SpeakerIdentity] Track ${elementIndex} mapping invalidated.`);
}

/** Debug: get current mapping state. */
export function getTrackMappingState(): Record<number, { name: string; locked: boolean; votes: Record<string, number> }> {
  const state: Record<number, { name: string; locked: boolean; votes: Record<string, number> }> = {};
  for (const [idx, votes] of trackVotes) {
    const locked = lockedMappings.get(idx);
    state[idx] = {
      name: locked || '',
      locked: !!locked,
      votes: Object.fromEntries(votes),
    };
  }
  return state;
}
