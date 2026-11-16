/**
 * Ask again when Google withdraws the join request.
 *
 * Google keeps an unauthenticated guest's "Ask to join" visible to the host for
 * roughly 65 seconds, then removes it with neither an admission nor a denial. A
 * bot dispatched ahead of the meeting therefore spends its one visible minute
 * before anyone has arrived to answer, and exits.
 *
 * This loop re-asks on that outcome — and ONLY that outcome. A denial, an auth
 * failure, or any other error propagates untouched: re-asking after a host has
 * said no is pestering them, and after an auth failure it can never succeed.
 *
 * The caller injects the join and admission calls, the clock and the predicate,
 * so the decision logic is exercised without a browser or a live meeting.
 */

/** The calls this loop drives. Injected so the logic is testable in isolation. */
export interface ReknockDeps {
  /** Navigate and click "Ask to join". Re-run in full on each ask: a fresh page
   *  load is also a fresh chance at not being refused. */
  join: () => Promise<void>;
  /** Wait for admission with the time that remains. Resolves true on admission. */
  waitForAdmission: (remainingMs: number) => Promise<boolean>;
  /** True only for "the request expired", never for a denial or auth failure. */
  isExpiredKnock: (e: unknown) => boolean;
  now: () => number;
  log: (message: string) => void;
}

export interface ReknockOptions {
  /** Budget for the WHOLE sequence, not per ask. */
  totalBudgetMs: number;
  /** Hard cap on asks, so a pathological expiry loop still terminates. */
  maxKnocks: number;
}

/**
 * Returns whatever the admission wait returned once it does not expire.
 * Rethrows the LAST error when the budget or the knock cap runs out — the real
 * one, never a synthesised summary, so the reason reaching the operator is the
 * reason that actually occurred.
 */
export async function joinWithReknock(
  deps: ReknockDeps,
  opts: ReknockOptions
): Promise<boolean> {
  const deadline = deps.now() + opts.totalBudgetMs;
  let knocks = 0;
  let lastError: unknown = null;

  while (knocks < opts.maxKnocks) {
    knocks++;
    await deps.join();

    const remainingMs = deadline - deps.now();
    if (remainingMs <= 0) break;

    try {
      return await deps.waitForAdmission(remainingMs);
    } catch (e) {
      lastError = e;
      if (!deps.isExpiredKnock(e)) throw e;
      if (knocks >= opts.maxKnocks) break;
      if (deps.now() >= deadline) break;
      deps.log(
        `Google withdrew the join request; re-knocking (ask ${knocks + 1} of ${opts.maxKnocks}, ` +
          `${Math.round((deadline - deps.now()) / 1000)}s of budget left)`
      );
    }
  }

  throw lastError ?? new Error("Google Meet admission ended without a verdict");
}
