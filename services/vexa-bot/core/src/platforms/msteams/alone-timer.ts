/**
 * Pure left-alone / empty-room timer logic for the Microsoft Teams bot — no DOM,
 * no Playwright, so it is unit-testable in isolation. Used by recording.ts's
 * in-page monitoring loop (via an exposed Node callback) to decide when a bot
 * that is alone in a meeting should leave.
 *
 * ── WHY THIS IS A COPY OF platforms/zoom/web/alone-timer.ts ──────────────────
 * `stepAloneTimer` below is a DELIBERATE, byte-for-byte duplicate of Zoom's.
 * It is NOT promoted to a shared module on purpose: Zoom is live and stable
 * (`zoom-bot:v0.1.15`) and a shared module would let a future Teams change
 * reach Zoom's exit logic. The same reasoning is recorded in
 * `aw-integration/adapter.py` for `_SPEAKER_EVENT_STREAM_MAXLEN` and in
 * `googlemeet/recording.ts` for the blob path: this codebase prefers a pinned
 * duplicate over shared code across a stability boundary. `stepAloneTimer`'s
 * test cases are ported alongside it so the duplicate cannot silently drift.
 *
 * ── WHY TEAMS NEEDS resolveTeamsAloneCount ON TOP ────────────────────────────
 * Zoom's timer is fed a real participant count from Zoom's own UI. Teams has no
 * equivalent: the count that recording.ts used before this module counted
 * `[role="menuitem"]` rows in the **Participants panel**, and nothing in
 * `msteams/` or `platforms/shared/` ever opens that panel (verified: the only
 * `.click()` sites in `msteams/` are the captions menu, the leave/hangup
 * buttons, the pre-join dialogs, and the removal "Dismiss" button). So that
 * count was pinned at whatever the closed-panel DOM happened to yield, and
 * BOTH possible values were wrong:
 *
 *   - stuck at 0  → `speakersIdentified` never became true, so the active
 *                   threshold stayed `startupAloneTimeout` (20 min by default,
 *                   because the orchestrator does not send
 *                   `noOneJoinedTimeout`). Every meeting yielded a 20-minute
 *                   artifact — which LOOKS like success, and is therefore the
 *                   worse of the two.
 *   - stuck at >=1 → the alone timer never accumulated, the bot never left, the
 *                   Job ran to `activeDeadlineSeconds` and was SIGKILLed inside
 *                   the termination grace period → artifacts lost.
 *
 * `resolveTeamsAloneCount` replaces that single unreadable source with three
 * independent ones and maps them onto Zoom's 0/1/2 contract. See its docstring
 * for what each value means and, crucially, what a closed panel means.
 */

/** State for the left-alone timer. */
export interface AloneTimerState {
  aloneMs: number;
  sawOthers: boolean;
  /**
   * Has the Participants roster been readable AT ANY POINT this session?
   *
   * This is the discriminator between the two things a `null` roster can mean.
   * `TEAMS_STALE_EVIDENCE_MS` was written on the assumption that an unreadable
   * roster is Teams' STEADY state (nothing opens the panel), so a null roster
   * says nothing and the bot must hold. A live 3-person meeting on 2026-09-07
   * disproved that for the admitted case: mid-meeting the reading was
   * `roster=1` — readable — and it became `roster=unreadable` only once
   * everyone left.
   *
   * So once the roster has been readable, its DISAPPEARANCE is evidence of
   * departure rather than blindness, and holding the full 15 minutes is wrong.
   * When it has never been readable, the original reasoning still applies and
   * the long hold is kept.
   */
  rosterEverReadable?: boolean;
}

/**
 * Advance the left-alone timer by one poll tick.
 *
 * `count` is the participant count INCLUDING the bot:
 *   - `>= 2` → at least one human present: reset, and remember we saw others.
 *   - `=== 1` → only the bot remains: accumulate toward the timeout.
 *   - `=== 0` → unreadable this tick (DOM transition): HOLD (never leave).
 *
 * The active threshold is `everyoneLeftMs` once others were ever seen, else
 * `noOneJoinedMs` (nobody ever joined). Returns the next state, whether the bot
 * should leave now, and a human-readable label (null when not alone).
 */
export function stepAloneTimer(
  state: AloneTimerState,
  count: number,
  pollMs: number,
  everyoneLeftMs: number,
  noOneJoinedMs: number
): { state: AloneTimerState; shouldLeave: boolean; label: 'everyone left' | 'no one joined' | null } {
  if (count >= 2) {
    return {
      state: { aloneMs: 0, sawOthers: true, rosterEverReadable: state.rosterEverReadable },
      shouldLeave: false,
      label: null,
    };
  }
  if (count === 1) {
    const aloneMs = state.aloneMs + pollMs;
    const threshold = state.sawOthers ? everyoneLeftMs : noOneJoinedMs;
    const label = state.sawOthers ? 'everyone left' : 'no one joined';
    return {
      state: { aloneMs, sawOthers: state.sawOthers, rosterEverReadable: state.rosterEverReadable },
      shouldLeave: aloneMs >= threshold,
      label,
    };
  }
  // count === 0 → unreadable → hold.
  return { state, shouldLeave: false, label: null };
}

/** The 0/1/2 contract `stepAloneTimer` consumes. */
export type TeamsAloneCount = 0 | 1 | 2;

/**
 * One tick's worth of DOM-derived presence evidence, read in the page by
 * `recording.ts`. Every field is deliberately nullable-or-counted rather than
 * boolean, because "the DOM says nobody" and "the DOM says nothing" are
 * different facts and only the first one means the room is empty.
 */
export interface TeamsPresenceReading {
  /**
   * Humans (the bot excluded) listed in the Participants panel, or `null` when
   * the panel yielded no rows at all — which is the NORMAL case, because
   * nothing in this bot ever opens it. `null`, not `0`: an unopened panel is
   * not an empty meeting. A non-null value means the panel was genuinely
   * readable this tick and is therefore authoritative.
   */
  rosterHumans: number | null;
  /**
   * Participant elements on the meeting stage carrying Teams' voice-level
   * signal (`[data-tid="voice-level-stream-outline"]`). Readable with the
   * Participants panel closed, which is what makes it useful here. Whether the
   * bot's OWN tile is among them is not knowable from the DOM (it depends on
   * self-view, which we never asserted), so only `>= 2` is treated as proof
   * that a human is present.
   */
  signalTiles: number;
  /**
   * Milliseconds since the last positive evidence of a human speaking — a
   * caption author from Microsoft's own diarisation, or a DOM speaking
   * transition — or `null` if there has never been any such evidence.
   */
  humanEvidenceAgeMs: number | null;
}

/** Poll cadence of the in-page monitoring loop, in ms. */
export const TEAMS_POLL_MS = 1000;

/**
 * How long a human stays "present" after their last word. Conversational gaps
 * are seconds; a minute is generous without letting a finished meeting linger.
 */
export const TEAMS_PRESENCE_WINDOW_MS = 60_000;

/**
 * How long the bot will HOLD (refuse to start the leave clock) on unreadable
 * DOM after a human was last heard.
 *
 * This bound exists because an unbounded hold is not safe here. Zoom can hold
 * forever on `count === 0` because Zoom's count recovers within a tick or two —
 * it is a genuine DOM transition. For Teams, "unreadable" can be the STEADY
 * state (closed roster panel, no stage tiles, captions never enabled), and a
 * bot that holds forever runs to `activeDeadlineSeconds` and loses its
 * artifacts to SIGKILL. So after this much total silence with nothing readable,
 * we stop holding and let the everyone-left timer run.
 *
 * 15 minutes is chosen against the two failure modes: a meeting with a genuine
 * 15-minute silent stretch is unusual, while losing a whole recording is not
 * recoverable.
 */
export const TEAMS_STALE_EVIDENCE_MS = 15 * 60_000;

/** Default when the orchestrator sends no `noOneJoinedTimeout` (today's case). */
/**
 * The stale-evidence hold used once the roster HAS been readable this session.
 *
 * Two minutes, not fifteen. A roster that was readable and is now null is a
 * transition, so the bot does not need to assume blindness — but it still waits
 * out a short window rather than acting on one tick, because a panel can also
 * close or re-render mid-meeting. Combined with the everyone-left timer this
 * puts the total at roughly four minutes instead of nineteen.
 *
 * Measured motivation: on 2026-09-07 a 30-minute Teams meeting kept the bot
 * alive ~19 minutes past the end (15 min stale + 2 min everyone-left + poll
 * slack), uploading ~60 chunks of pure silence and padding the recording.
 */
export const TEAMS_ROSTER_GONE_STALE_MS = 2 * 60_000;

export const TEAMS_DEFAULT_NO_ONE_JOINED_MS = 20 * 60_000;

/** Default when the orchestrator sends no `everyoneLeftTimeout`. */
export const TEAMS_DEFAULT_EVERYONE_LEFT_MS = 60_000;

/** Resolved thresholds for one Teams session. All values in milliseconds. */
export interface TeamsAloneTimeouts {
  pollMs: number;
  everyoneLeftMs: number;
  noOneJoinedMs: number;
  presenceWindowMs: number;
  staleEvidenceMs: number;
}

/**
 * Map one tick of presence evidence onto `stepAloneTimer`'s 0/1/2 contract.
 *
 * Ordered most-trustworthy first. The order matters: a readable roster claiming
 * emptiness must not beat someone who spoke two seconds ago.
 *
 *   1. Someone spoke within `presenceWindowMs`  → 2 (a human is here).
 *   2. Two or more signal-bearing stage tiles   → 2 (at least one is not us).
 *   3. The roster panel was readable            → authoritative: >=1 human → 2,
 *                                                 otherwise → 1 (everyone left).
 *   4. No evidence has EVER been seen           → 1 (the no-one-joined path).
 *   5. Evidence exists but is older than
 *      `staleEvidenceMs`, nothing readable now  → 1 (stop holding; see
 *                                                 TEAMS_STALE_EVIDENCE_MS).
 *   6. Otherwise                                → 0 (recently stale and
 *                                                 unreadable → HOLD).
 *
 * Rule 6 is the point of the whole exercise: a transient DOM blip mid-meeting
 * must not start the leave clock. Rule 5 is what keeps rule 6 from becoming the
 * never-leaves failure.
 */
export function resolveTeamsAloneCount(
  reading: TeamsPresenceReading,
  presenceWindowMs: number,
  staleEvidenceMs: number,
  rosterEverReadable: boolean = false
): TeamsAloneCount {
  const { rosterHumans, signalTiles, humanEvidenceAgeMs } = reading;

  if (humanEvidenceAgeMs !== null && humanEvidenceAgeMs <= presenceWindowMs) {
    return 2;
  }
  if (signalTiles >= 2) {
    return 2;
  }
  if (rosterHumans !== null) {
    return rosterHumans >= 1 ? 2 : 1;
  }
  if (humanEvidenceAgeMs === null) {
    return 1;
  }
  // A roster that was readable earlier and is null NOW is a transition, so the
  // long blindness hold does not apply -- a much shorter window does. When the
  // roster has never been readable, `staleEvidenceMs` still governs.
  const effectiveStaleMs = rosterEverReadable
    ? Math.min(staleEvidenceMs, TEAMS_ROSTER_GONE_STALE_MS)
    : staleEvidenceMs;
  if (humanEvidenceAgeMs > effectiveStaleMs) {
    return 1;
  }
  return 0;
}

/** One tick's decision, plus the resolved count so the caller can log it. */
export interface TeamsAloneTickResult {
  state: AloneTimerState;
  count: TeamsAloneCount;
  shouldLeave: boolean;
  label: 'everyone left' | 'no one joined' | null;
}

/**
 * Resolve a presence reading to a count and advance the timer by one tick.
 * The whole Teams leave decision, as one pure function.
 */
export function teamsAloneTick(
  state: AloneTimerState,
  reading: TeamsPresenceReading,
  timeouts: TeamsAloneTimeouts
): TeamsAloneTickResult {
  // Latch BEFORE resolving: a readable roster this tick is remembered for the
  // rest of the session, so a later null roster reads as departure rather than
  // blindness. Latch-only (never cleared) -- the fact being recorded is "this
  // bot CAN see the roster in this meeting", which does not stop being true.
  const rosterEverReadable = state.rosterEverReadable === true || reading.rosterHumans !== null;

  const count = resolveTeamsAloneCount(
    reading,
    timeouts.presenceWindowMs,
    timeouts.staleEvidenceMs,
    rosterEverReadable
  );
  const stepped = stepAloneTimer(
    state,
    count,
    timeouts.pollMs,
    timeouts.everyoneLeftMs,
    timeouts.noOneJoinedMs
  );
  return {
    state: { ...stepped.state, rosterEverReadable },
    count,
    shouldLeave: stepped.shouldLeave,
    label: stepped.label,
  };
}

/** True for a value usable as a positive millisecond threshold. */
function positiveMs(value: unknown): number | null {
  const n = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(n) && n > 0 ? n : null;
}

/**
 * Read the leave thresholds out of `botConfig.automaticLeave`.
 *
 * Field names are load-bearing and are pinned by tests. Upstream has already
 * shipped one field-name mismatch here (commit `7010bd15`, "Fix
 * everyoneLeftTimeout field name mismatch"), and a mismatch is SILENT: the
 * value simply falls through to the default and the bot leaves at the wrong
 * time. Precedence matches what recording.ts did before this module, so the
 * behaviour of an existing config is unchanged:
 *
 *   - `noOneJoinedTimeout` (ms) → `startupAloneTimeoutSeconds` (s) → 20 min
 *   - `everyoneLeftTimeout` (ms) → `everyoneLeftTimeoutSeconds` (s) → 60 s
 */
export function readTeamsLeaveTimeouts(automaticLeave: unknown): TeamsAloneTimeouts {
  const cfg = (automaticLeave ?? {}) as Record<string, unknown>;

  const noOneJoinedMs =
    positiveMs(cfg.noOneJoinedTimeout) ??
    (positiveMs(cfg.startupAloneTimeoutSeconds) !== null
      ? positiveMs(cfg.startupAloneTimeoutSeconds)! * 1000
      : TEAMS_DEFAULT_NO_ONE_JOINED_MS);

  const everyoneLeftMs =
    positiveMs(cfg.everyoneLeftTimeout) ??
    (positiveMs(cfg.everyoneLeftTimeoutSeconds) !== null
      ? positiveMs(cfg.everyoneLeftTimeoutSeconds)! * 1000
      : TEAMS_DEFAULT_EVERYONE_LEFT_MS);

  return {
    pollMs: TEAMS_POLL_MS,
    everyoneLeftMs,
    noOneJoinedMs,
    presenceWindowMs: TEAMS_PRESENCE_WINDOW_MS,
    staleEvidenceMs: TEAMS_STALE_EVIDENCE_MS,
  };
}

/**
 * The bot's own display name, resolved ONCE on the Node side and passed into
 * the page as a plain string.
 *
 * `recording.ts` read `botConfigData?.name` in two places while the
 * orchestrator sets **`botName`** (`job_launcher.py`), so the bot's own row was
 * never excluded from the participant count and the bot's own captions were
 * never filtered by name. Resolving it here — outside the page, in a tested
 * function — means the page never does a field lookup at all, which is what
 * removes the bug class rather than fixing one instance of it.
 *
 * `botName` wins over the legacy `name`; both are trimmed; `''` when neither is
 * a non-empty string (callers treat that as "fall back to the 'vexa' literal",
 * matching the pre-existing caption filter).
 */
export function resolveBotDisplayName(botConfigData: unknown): string {
  const cfg = (botConfigData ?? {}) as Record<string, unknown>;
  for (const key of ['botName', 'name']) {
    const value = cfg[key];
    if (typeof value === 'string' && value.trim()) {
      return value.trim();
    }
  }
  return '';
}
