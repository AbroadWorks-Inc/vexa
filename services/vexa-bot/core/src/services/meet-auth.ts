/**
 * meet-auth — the boot-time decision for how the Meet bot authenticates.
 *
 * Extracted from the inline block in `index.ts` (the storage_state loader) so
 * the decision is unit-testable in isolation, matching how every other piece
 * of index.ts logic lives in `services/*.ts` (see log-throttle, speaker-*).
 *
 * Design §5.2.4: the Meet bot could boot signed-in as a Google account via a
 * Playwright storage_state injected through GOOGLE_NOTETAKER_STORAGE_STATE.
 * Google revokes that session roughly every 14 days, which took the bot down
 * repeatedly. Phase 1 "force guest" drops that path when the dispatcher marks a
 * job joinMode="guest": the bot then joins anonymously EXACTLY like Zoom/Teams,
 * removing the revocation dependency. The secret, its key, and the refresh
 * CronJob all stay in place — only this decision changes.
 *
 * Meet-only by construction: Zoom/Teams never read this and already join as
 * guests.
 */

export type MeetAuthDecision =
  | { authenticated: true; storageStateJson: string }
  | {
      authenticated: false;
      reason: "guest_forced" | "no_secret" | "invalid_secret";
    };

/**
 * Decide whether to load the saved Google session and join authenticated.
 *
 * @param joinMode      botConfig.joinMode — "guest" forces anonymous join even
 *                      when a valid secret is present. Any other value (or
 *                      undefined, for legacy jobs) keeps the old behaviour.
 * @param rawStorageState  the GOOGLE_NOTETAKER_STORAGE_STATE env value, in
 *                      either accepted seeding format (raw JSON or base64 JSON).
 * @returns a decision the caller acts on: write the file + set authenticated,
 *          or fall through to the anonymous path. The `reason` lets the caller
 *          log the same distinctions the inline code used to.
 */
export function decideMeetStorageState(
  joinMode: string | undefined,
  rawStorageState: string | undefined,
): MeetAuthDecision {
  // Force guest: skip the storage_state entirely, EVEN IF a valid secret is
  // present. This is the whole point of the ticket — a revoked Google session
  // can no longer take the Meet bot down because it is never consulted.
  if (joinMode === "guest") {
    return { authenticated: false, reason: "guest_forced" };
  }

  // Unchanged legacy behaviour for every non-guest join.
  if (rawStorageState && rawStorageState.trim().length > 8) {
    try {
      // Accept both seeding formats: raw storage_state JSON (--from-file) and
      // base64-encoded JSON (--from-literal). '{' is not in the base64 alphabet.
      const trimmed = rawStorageState.trim();
      const decoded = trimmed.startsWith("{")
        ? trimmed
        : Buffer.from(trimmed, "base64").toString("utf8");
      JSON.parse(decoded); // validate real storage_state JSON before trusting it
      return { authenticated: true, storageStateJson: decoded };
    } catch {
      return { authenticated: false, reason: "invalid_secret" };
    }
  }

  return { authenticated: false, reason: "no_secret" };
}
