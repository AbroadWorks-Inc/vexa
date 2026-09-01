/**
 * Collapse repeated diagnostics into something a human can actually read.
 *
 * ── Why this module exists ──────────────────────────────────────────────────
 * The live Zoom run of 2026-09-01 emitted 278 copies of one line —
 * `[SpeakerIdentity] Element 2 → "" (platform: zoom, not yet mapped)`, 222 of
 * them for a single track — and those 278 lines said nothing about WHY the name
 * was missing. 278 identical lines is not observability: it is loud silence. It
 * also buries the handful of lines that do carry information.
 *
 * The rule implemented here: a repeated diagnostic is emitted when its
 * SIGNATURE changes (the underlying situation actually moved), and otherwise at
 * most once per heartbeat interval, carrying the count it suppressed. So a stuck
 * failure produces a slow, countable heartbeat, and a failure whose cause
 * shifts produces a line at each shift — which is the diagnostically
 * interesting moment.
 *
 * Time is injected, never read from `Date.now()` inside, so the heartbeat is
 * driven deterministically under test. No `playwright`, no browser.
 */

/** Default gap between heartbeats for an unchanged signature. */
export const REPEAT_HEARTBEAT_DEFAULT_MS = 30_000;

export interface RepeatCollapser {
  /**
   * Decide whether this occurrence should be logged.
   *
   * @param key Identity of the diagnostic stream (e.g. `zoom-miss:track2`).
   *   Streams are independent — one track's chatter never suppresses another's.
   * @param signature Fingerprint of the underlying situation. A change forces an
   *   immediate emit, because the situation moved.
   * @param message The line to log if it is emitted, WITHOUT any repeat count.
   * @param atMs Current time, injected.
   * @returns The line to log (possibly with a suppression count appended), or
   *   `null` when this occurrence should be swallowed.
   */
  consider(key: string, signature: string, message: string, atMs: number): string | null;
  /**
   * Flush a stream: if occurrences were suppressed since its last emit, return a
   * final line accounting for them, then forget the stream. Returns `null` when
   * there is nothing outstanding. Use at teardown so the last suppressed run is
   * never silently lost.
   */
  flush(key: string, atMs: number): string | null;
  /** Forget every stream (new meeting / capture restart). */
  reset(): void;
}

/** Per-stream bookkeeping. */
interface StreamState {
  signature: string;
  /** Time of the most recent EMITTED line for this stream. */
  lastEmitMs: number;
  /** Occurrences swallowed since `lastEmitMs`. */
  suppressed: number;
  /** Last message text seen, so `flush` can name what was being suppressed. */
  lastMessage: string;
}

/**
 * Build a collapser.
 *
 * @param heartbeatMs Minimum gap between emits while the signature is unchanged.
 *   Clamped to at least 1ms so a zero can never turn the collapser off silently.
 */
export function createRepeatCollapser(heartbeatMs: number = REPEAT_HEARTBEAT_DEFAULT_MS): RepeatCollapser {
  const gap = Math.max(1, Math.floor(heartbeatMs));
  const streams = new Map<string, StreamState>();

  /** Append the suppression accounting, if any occurrences were swallowed. */
  const withCount = (message: string, suppressed: number): string =>
    suppressed > 0 ? `${message} [+${suppressed} identical occurrence(s) suppressed]` : message;

  return {
    consider(key: string, signature: string, message: string, atMs: number): string | null {
      const prev = streams.get(key);

      // First occurrence for this stream — always speak up.
      if (!prev) {
        streams.set(key, { signature, lastEmitMs: atMs, suppressed: 0, lastMessage: message });
        return message;
      }

      // The situation moved. Emit immediately and account for what was hidden
      // while the previous signature stood.
      if (prev.signature !== signature) {
        const out = withCount(message, prev.suppressed);
        streams.set(key, { signature, lastEmitMs: atMs, suppressed: 0, lastMessage: message });
        return out;
      }

      // Unchanged signature: heartbeat at most once per `gap`.
      if (atMs - prev.lastEmitMs >= gap) {
        const out = withCount(message, prev.suppressed);
        streams.set(key, { signature, lastEmitMs: atMs, suppressed: 0, lastMessage: message });
        return out;
      }

      prev.suppressed += 1;
      prev.lastMessage = message;
      return null;
    },

    flush(key: string, atMs: number): string | null {
      const prev = streams.get(key);
      streams.delete(key);
      if (!prev || prev.suppressed === 0) return null;
      void atMs; // Kept in the signature for symmetry; the count is what matters.
      return withCount(prev.lastMessage, prev.suppressed);
    },

    reset(): void {
      streams.clear();
    },
  };
}
