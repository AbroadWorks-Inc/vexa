/**
 * Pure left-alone / empty-room timer logic for the Zoom Web bot — no DOM, no
 * Playwright, so it is unit-testable in isolation. Used by removal.ts's poll
 * loop to decide when a bot that is alone in a meeting should leave.
 */

/** State for the left-alone timer. */
export interface AloneTimerState {
  aloneMs: number;
  sawOthers: boolean;
}

/**
 * Advance the left-alone timer by one poll tick.
 *
 * `count` is Zoom's participant count INCLUDING the bot:
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
    return { state: { aloneMs: 0, sawOthers: true }, shouldLeave: false, label: null };
  }
  if (count === 1) {
    const aloneMs = state.aloneMs + pollMs;
    const threshold = state.sawOthers ? everyoneLeftMs : noOneJoinedMs;
    const label = state.sawOthers ? 'everyone left' : 'no one joined';
    return { state: { aloneMs, sawOthers: state.sawOthers }, shouldLeave: aloneMs >= threshold, label };
  }
  // count === 0 → unreadable → hold.
  return { state, shouldLeave: false, label: null };
}
