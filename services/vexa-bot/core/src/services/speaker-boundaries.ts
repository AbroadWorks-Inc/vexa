/**
 * Audio-derived speaker boundaries (CSS-INDEPENDENT).
 *
 * Emits real SPEAKER_START / SPEAKER_END pairs from per-track audio activity,
 * replacing the previous behaviour where every name resolution published a bare
 * `joined` event.
 *
 * Why this exists: `joined` maps to SPEAKER_START (segment-publisher.ts:285-290)
 * with no matching END and no repeat as speech alternates, so the stream carried
 * a ROSTER, not a timeline. The downstream worker mapper assigns each transcript
 * segment to the last event with `relative_sec <= segment.start`, so a roster
 * collapses nearly every segment onto whichever track resolved last. Real
 * boundaries are required for correct attribution.
 *
 * Silence is the ABSENCE of audio callbacks, so it cannot be detected from the
 * event stream alone — a single shared low-frequency sweep closes stale tracks.
 *
 * ── Why this lives in its own module ────────────────────────────────────────
 * This state machine previously lived inside `index.ts`, which imports
 * `playwright-extra` at module scope and therefore cannot be loaded by a plain
 * `tsx` test run. Two production defects (duplicate STARTs at a stale onset, and
 * an `END "null"` log) shipped undetected as a direct consequence. Everything
 * here is dependency-injected — no `playwright`, no `index.ts`, no browser — so
 * `speaker-boundaries.test.ts` can drive it with a fake clock.
 *
 * Do NOT add an import of `index.ts` or any Playwright package to this file.
 */

/** The two boundary kinds the publisher understands. */
export type SpeakerBoundaryType = 'started_speaking' | 'stopped_speaking';

/** Sweep cadence for closing utterances that have gone silent. */
export const SPEAKER_SWEEP_INTERVAL_MS = 200;
/** Default silence gap before an utterance is considered ended. */
export const SPEAKER_SILENCE_HANGOVER_DEFAULT_MS = 700;

/** Lowest / highest hangover accepted from the environment. */
const HANGOVER_MIN_MS = 100;
const HANGOVER_MAX_MS = 10_000;

/**
 * Names that are the string form of a nullish value rather than a real person.
 * A resolver that stringifies `null` anywhere upstream must never be able to
 * publish a boundary naming `"null"`, so these are rejected at the boundary.
 */
const STRINGIFIED_NULLISH_NAMES = new Set(['null', 'undefined']);

/**
 * Everything the state machine needs from the outside world. All injected so the
 * machine is testable in-process with no browser and no real time.
 */
export interface SpeakerBoundaryDeps {
  /** Publish one boundary. `timestampMs` is in whatever domain the caller clamps to. */
  publish(type: SpeakerBoundaryType, speaker: string, timestampMs: number): Promise<void>;
  /** Currently resolved display name for a track, or '' when still unmapped. */
  resolveName(trackIndex: number): string;
  /** Clock. Injected so tests can drive the hangover without sleeping. Defaults to `Date.now`. */
  now?: () => number;
  /** Diagnostic sink. Defaults to a no-op; production passes `utils.log`. */
  log?: (message: string) => void;
  /** Override the silence hangover. Defaults to `resolveSilenceHangoverMs()` (env-driven). */
  hangoverMs?: number;
  /** Override the sweep cadence used by `startSweep()`. Defaults to `SPEAKER_SWEEP_INTERVAL_MS`. */
  sweepIntervalMs?: number;
}

/** Public surface of the boundary state machine. */
export interface SpeakerBoundaryTracker {
  /**
   * Signal that the session time origin is aligned to the recording, so buffered
   * speaker boundaries may be published. Idempotent; safe to call more than once.
   */
  arm(): void;
  /** Whether publishing is currently armed. */
  isArmed(): boolean;
  /** Record audio activity for a track, opening a new utterance on a silent→speaking edge. */
  markTrackAudioActivity(trackIndex: number, atMs: number): void;
  /** Run one sweep pass. Exposed directly so tests can drive it without a timer. */
  sweep(): Promise<void>;
  /** Begin the periodic sweep. Idempotent. */
  startSweep(): void;
  /** Stop the sweep and close every still-open utterance so no track is left unterminated. */
  stopSweep(): Promise<void>;
  /** Number of tracks currently holding state (diagnostics / tests). */
  trackCount(): number;
  /** The hangover this instance is using, after env parsing / override. */
  hangoverMs(): number;
}

/** Per-track speech state. */
interface TrackSpeechState {
  /** Currently inside an utterance (audio seen within the hangover window). */
  speaking: boolean;
  /** Absolute ms when the current utterance began. */
  onsetMs: number;
  /** Absolute ms of the most recent audio for this track. */
  lastAudioMs: number;
  /** Whether a START has been published for the current utterance. */
  startEmitted: boolean;
  /** Name the open START was published under (END must match it). */
  emittedName: string | null;
}

/** A boundary decided synchronously by the sweep, awaiting publication. */
interface PendingBoundary {
  type: SpeakerBoundaryType;
  speaker: string;
  timestampMs: number;
  logLine: string;
}

/**
 * Read the silence hangover from the environment, safe-parsed and clamped.
 * Exported for the test suite and for callers that want to log the effective value.
 */
export function resolveSilenceHangoverMs(log?: (message: string) => void): number {
  const raw = process.env.SPEAKER_SILENCE_HANGOVER_MS;
  if (!raw) return SPEAKER_SILENCE_HANGOVER_DEFAULT_MS;
  const parsed = parseInt(raw, 10);
  if (!Number.isFinite(parsed) || parsed < HANGOVER_MIN_MS || parsed > HANGOVER_MAX_MS) {
    log?.(`[SpeakerBoundary] Invalid SPEAKER_SILENCE_HANGOVER_MS="${raw}" — using ${SPEAKER_SILENCE_HANGOVER_DEFAULT_MS}ms`);
    return SPEAKER_SILENCE_HANGOVER_DEFAULT_MS;
  }
  return parsed;
}

/**
 * Reject a resolved name that is really the string form of a nullish value.
 *
 * Defensive: a boundary naming `"null"` is indistinguishable downstream from a
 * genuine participant called that, and it silently mis-attributes every segment
 * in the utterance. Returns '' (the machine's "unmapped" sentinel) for junk.
 */
export function sanitizeResolvedName(raw: string | null | undefined): string {
  if (raw == null) return '';
  const trimmed = String(raw).trim();
  if (!trimmed) return '';
  if (STRINGIFIED_NULLISH_NAMES.has(trimmed.toLowerCase())) return '';
  return trimmed;
}

class SpeakerBoundaryMachine implements SpeakerBoundaryTracker {
  private readonly trackSpeech: Map<number, TrackSpeechState> = new Map();
  /** Tracks whose utterance was dropped for lack of a name — logged once each. */
  private readonly unnamedUtteranceLogged: Set<number> = new Set();
  /** Tracks whose resolver returned a stringified-nullish name — logged once each. */
  private readonly junkNameLogged: Set<number> = new Set();

  private sweepTimer: ReturnType<typeof setInterval> | null = null;
  /**
   * Re-entrancy guard. `startSweep` uses `void this.sweep()` on an interval, so a
   * publish slower than the cadence would otherwise let two passes interleave and
   * observe each other's half-applied state.
   */
  private sweeping = false;

  /**
   * Publishing must not begin until the session time origin has settled.
   *
   * Ordering hazard this guards: per-speaker capture is started fire-and-forget
   * from the post-admission callback (platforms/shared/meetingFlow.ts:180), while
   * startGoogleRecording() — which calls publisher.resetSessionStart() to move
   * sessionStartMs from bot-construction time to recording start — runs LATER
   * (meetingFlow.ts:221). A boundary published in that window is offset by the
   * entire admission wait, up to BOT_WAITING_ROOM_TIMEOUT_MS (900000ms in prod).
   *
   * Activity tracking still accrues while unarmed; only PUBLISHING waits. Because
   * onsets are stored as absolute ms and the publisher subtracts sessionStartMs at
   * publish time, deferred events get the corrected origin automatically.
   */
  private armed = false;
  /** Guards the "seen audio but never armed" warning so it is logged once, not per sweep. */
  private unarmedWarningLogged = false;

  private readonly hangover: number;
  private readonly sweepIntervalMs: number;
  private readonly now: () => number;
  private readonly log: (message: string) => void;

  constructor(private readonly deps: SpeakerBoundaryDeps) {
    this.now = deps.now ?? Date.now;
    this.log = deps.log ?? (() => {});
    this.hangover = deps.hangoverMs ?? resolveSilenceHangoverMs(this.log);
    this.sweepIntervalMs = deps.sweepIntervalMs ?? SPEAKER_SWEEP_INTERVAL_MS;
  }

  arm(): void {
    if (this.armed) return;
    this.armed = true;
    this.log('[SpeakerBoundary] Time origin aligned to recording — boundary publishing armed');
  }

  isArmed(): boolean {
    return this.armed;
  }

  trackCount(): number {
    return this.trackSpeech.size;
  }

  hangoverMs(): number {
    return this.hangover;
  }

  markTrackAudioActivity(trackIndex: number, atMs: number): void {
    let st = this.trackSpeech.get(trackIndex);
    if (!st) {
      st = { speaking: false, onsetMs: atMs, lastAudioMs: atMs, startEmitted: false, emittedName: null };
      this.trackSpeech.set(trackIndex, st);
    }
    st.lastAudioMs = atMs;
    if (!st.speaking) {
      st.speaking = true;
      st.onsetMs = atMs;
      st.startEmitted = false;
      st.emittedName = null;
    }
  }

  /** Resolved name for a track with stringified-nullish junk rejected. */
  private nameFor(trackIndex: number): string {
    let raw: string;
    try {
      raw = this.deps.resolveName(trackIndex);
    } catch (err: unknown) {
      this.log(`[SpeakerBoundary] Track ${trackIndex} name lookup failed: ${errMessage(err)}`);
      return '';
    }
    const clean = sanitizeResolvedName(raw);
    // Only shout about junk, not about the ordinary "still unmapped" case.
    if (!clean && String(raw ?? '').trim() && !this.junkNameLogged.has(trackIndex)) {
      this.junkNameLogged.add(trackIndex);
      this.log(`[SpeakerBoundary] Track ${trackIndex} resolver returned stringified-nullish name — treating as unmapped`);
    }
    return clean;
  }

  private async emit(boundary: PendingBoundary): Promise<void> {
    try {
      await this.deps.publish(boundary.type, boundary.speaker, boundary.timestampMs);
    } catch (err: unknown) {
      this.log(`[SpeakerBoundary] Failed to publish ${boundary.type} for "${boundary.speaker}": ${errMessage(err)}`);
      return;
    }
    this.log(boundary.logLine);
  }

  /**
   * Close utterances that have gone silent, and retroactively emit a START for any
   * OPEN utterance whose name resolved only after it began — using the ORIGINAL
   * onset, never the resolution moment, so the boundary is not back-dated.
   *
   * Every state transition happens synchronously in the decision loop; publishing
   * happens afterwards from immutable snapshots. Reading track state after an
   * `await` is what produced the `END "null"` log line in production.
   */
  async sweep(): Promise<void> {
    // Hold publishing until the time origin is settled (see `armed`).
    // State keeps accruing meanwhile, so nothing is lost — only deferred.
    if (!this.armed) {
      // Fail LOUDLY rather than silently. arm() has exactly one production caller
      // (googlemeet/recording.ts, right after resetSessionStart). If that call is
      // ever lost — partial revert, cherry-pick, a new platform wiring up capture
      // without it — every meeting silently reverts to an empty speaker timeline
      // and SPEAKER_NN, with no error anywhere. This warning makes it diagnosable.
      if (this.trackSpeech.size > 0 && !this.unarmedWarningLogged) {
        this.unarmedWarningLogged = true;
        this.log('[SpeakerBoundary] WARNING: audio activity seen but boundaries are NOT armed — armSpeakerBoundaries() has not been called. Speaker timeline will stay EMPTY until it is.');
      }
      return;
    }

    if (this.sweeping) return;
    this.sweeping = true;
    try {
      const pending = this.decide();
      for (const boundary of pending) {
        await this.emit(boundary);
      }
    } finally {
      this.sweeping = false;
    }
  }

  /** Synchronous decision pass: mutate state, return the boundaries to publish. */
  private decide(): PendingBoundary[] {
    const nowMs = this.now();
    const pending: PendingBoundary[] = [];

    for (const [idx, st] of this.trackSpeech) {
      // Retroactive START — ONLY for a currently open utterance.
      //
      // The `st.speaking` guard is the fix for the duplicate-START defect: the END
      // branch below clears `startEmitted`/`emittedName` but deliberately leaves
      // `onsetMs` alone (it is only refreshed on the next silent→speaking edge).
      // Without this guard the very next tick saw `!startEmitted` plus a resolvable
      // name and re-published START at the now-STALE onset, once per utterance —
      // 48 duplicate-timestamp events in a 429-event live timeline.
      if (st.speaking && !st.startEmitted) {
        const name = this.nameFor(idx);
        if (name) {
          st.startEmitted = true;
          st.emittedName = name;
          pending.push({
            type: 'started_speaking',
            speaker: name,
            timestampMs: st.onsetMs,
            logLine: `[SpeakerBoundary] Track ${idx} START "${name}" @onset=${st.onsetMs}`,
          });
        }
      }

      if (st.speaking && nowMs - st.lastAudioMs > this.hangover) {
        // Snapshot before mutating, so the published event and its log line agree
        // even if audio for this track arrives while the publish is in flight.
        const closingName = st.emittedName;
        const closingAtMs = st.lastAudioMs;
        const hadStart = st.startEmitted;

        st.speaking = false;
        st.startEmitted = false;
        st.emittedName = null;

        if (hadStart && closingName) {
          pending.push({
            type: 'stopped_speaking',
            speaker: closingName,
            timestampMs: closingAtMs,
            logLine: `[SpeakerBoundary] Track ${idx} END "${closingName}" @${closingAtMs}`,
          });
        } else if (!this.unnamedUtteranceLogged.has(idx)) {
          this.unnamedUtteranceLogged.add(idx);
          this.log(`[SpeakerBoundary] Track ${idx} utterance dropped — no name resolved before it ended`);
        }
      }
    }

    return pending;
  }

  startSweep(): void {
    if (this.sweepTimer) return;
    this.sweepTimer = setInterval(() => {
      void this.sweep();
    }, this.sweepIntervalMs);
    this.log(`[SpeakerBoundary] Sweep started (hangover=${this.hangover}ms, cadence=${this.sweepIntervalMs}ms)`);
  }

  async stopSweep(): Promise<void> {
    if (this.sweepTimer) {
      clearInterval(this.sweepTimer);
      this.sweepTimer = null;
    }

    // If recording never started the origin was never aligned, so anything published
    // here would carry the pre-admission offset. Discard rather than emit garbage.
    if (!this.armed) {
      if (this.trackSpeech.size > 0) {
        this.log(`[SpeakerBoundary] Discarding ${this.trackSpeech.size} track state(s) — recording never started, time origin never aligned`);
      }
      this.reset();
      return;
    }

    // Decide the whole flush synchronously, then publish — same rule as `sweep()`.
    const pending: PendingBoundary[] = [];
    for (const [idx, st] of this.trackSpeech) {
      if (st.speaking && !st.startEmitted) {
        const name = this.nameFor(idx);
        if (name) {
          st.startEmitted = true;
          st.emittedName = name;
          pending.push({
            type: 'started_speaking',
            speaker: name,
            timestampMs: st.onsetMs,
            logLine: `[SpeakerBoundary] Track ${idx} START "${name}" @onset=${st.onsetMs} (finalize flush)`,
          });
        }
      }
      if (st.startEmitted && st.emittedName) {
        pending.push({
          type: 'stopped_speaking',
          speaker: st.emittedName,
          timestampMs: st.lastAudioMs,
          logLine: `[SpeakerBoundary] Track ${idx} END "${st.emittedName}" @${st.lastAudioMs} (finalize flush)`,
        });
      }
      st.speaking = false;
      st.startEmitted = false;
      st.emittedName = null;
    }

    for (const boundary of pending) {
      await this.emit(boundary);
    }

    this.reset();
    this.armed = false;
  }

  /**
   * Clear per-meeting state. `unarmedWarningLogged` is reset here — i.e. on BOTH
   * teardown paths — because a capture restart that never armed would otherwise
   * suppress the one warning that diagnoses that exact failure.
   */
  private reset(): void {
    this.trackSpeech.clear();
    this.unnamedUtteranceLogged.clear();
    this.junkNameLogged.clear();
    this.unarmedWarningLogged = false;
  }
}

/** Extract a message from an unknown thrown value without assuming `Error`. */
function errMessage(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

/**
 * Build a speaker-boundary tracker. One instance per bot process in production;
 * tests create a fresh instance per case so no state leaks between them.
 */
export function createSpeakerBoundaryTracker(deps: SpeakerBoundaryDeps): SpeakerBoundaryTracker {
  return new SpeakerBoundaryMachine(deps);
}
