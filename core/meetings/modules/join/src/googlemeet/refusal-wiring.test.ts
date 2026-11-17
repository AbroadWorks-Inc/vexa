/**
 * The refusal detector is WIRED INTO the real Google Meet join flow — not merely present.
 *
 * `refusal.test.ts` proves the module's behaviour (the knock loop, the config, the guard ending a
 * wait). It cannot prove `joinGoogleMeeting` actually uses it: `watchGoogleJoinRefusal` is imported
 * directly rather than injected, so there is no seam to stub without a browser.
 *
 * These are therefore SOURCE assertions on join.ts, the same technique humanized.test.ts (test 8)
 * already uses on this file. They are weaker than behavioural tests — they prove the call is
 * written, not that it fires — and they exist to stop the one regression that would silently
 * re-open the bug: someone removing the listener or a guard and leaving the module in place,
 * which no other test would catch.
 *
 * Ordering matters and is asserted: ResolveMeetingSpace answers its 403 DURING the lobby load, so
 * the listener must be attached BEFORE page.goto or the refusal is missed entirely.
 *
 * Run: npx tsx src/googlemeet/refusal-wiring.test.ts
 */
import * as fs from "fs";
import * as path from "path";

let passed = 0, failed = 0;
function check(name: string, ok: boolean, detail?: string) {
  if (ok) { console.log(`  \x1b[32mPASS\x1b[0m  ${name}`); passed++; }
  else { console.log(`  \x1b[31mFAIL\x1b[0m  ${name}${detail ? ` (${detail})` : ""}`); failed++; }
}

const src = fs.readFileSync(path.join(__dirname, "join.ts"), "utf-8");

// The body of joinGoogleMeeting only — a call anywhere else in the file would not wire the flow.
const bodyStart = src.indexOf("export async function joinGoogleMeeting");
const body = bodyStart === -1 ? "" : src.slice(bodyStart);

console.log("\n=== refusal detection is wired into joinGoogleMeeting ===");

check(
  "join.ts imports the refusal detector",
  /import\s*\{[^}]*watchGoogleJoinRefusal[^}]*\}\s*from\s*["']\.\/refusal["']/.test(src),
  "no import of watchGoogleJoinRefusal from ./refusal",
);

check(
  "join.ts imports throwIfGoogleJoinRefused",
  /import\s*\{[^}]*throwIfGoogleJoinRefused[^}]*\}\s*from\s*["']\.\/refusal["']/.test(src),
  "no import of throwIfGoogleJoinRefused from ./refusal",
);

check(
  "joinGoogleMeeting attaches the listener",
  /watchGoogleJoinRefusal\s*\(\s*page\s*\)/.test(body),
  "watchGoogleJoinRefusal(page) is not called in joinGoogleMeeting",
);

// THE ordering assertion: attached before navigation, or ResolveMeetingSpace's 403 is never seen.
{
  const watchAt = body.indexOf("watchGoogleJoinRefusal(page)");
  const gotoAt = body.indexOf("page.goto(");
  check(
    "the listener is attached BEFORE page.goto",
    watchAt !== -1 && gotoAt !== -1 && watchAt < gotoAt,
    `watchAt=${watchAt} gotoAt=${gotoAt}`,
  );
}

// Both guest-path waits must receive a guard: the 120 s name-input wait and the 60 s join-button
// wait are the two places a refused bot used to sit out its whole budget.
{
  // Window is generous on purpose: this call carries a comment and a multi-line guard, and the
  // assertion is about WHAT is passed, not how tightly it is written.
  const nameCall = /waitForAnySelector\(\s*page,\s*googleNameInputSelectors,[\s\S]{0,800}?\n    \);/.exec(body);
  check(
    "the name-input wait is given a refusal guard",
    !!nameCall && /refused\s*\(/.test(nameCall[0]),
    nameCall ? "call found but no refused(...) argument" : "name-input wait call not found",
  );
  // PR #2058: the guest step is pressed from inside that same guard, so it runs on every poll of
  // the 120 s wait rather than once before it.
  check(
    "the name-input wait also presses the «Continue as guest» step",
    !!nameCall && /pressContinueAsGuestIfPresent\s*\(/.test(nameCall[0]),
    nameCall ? "guard does not press the guest step" : "name-input wait call not found",
  );
}

{
  const ctaCall = /waitForLobbyCta\(\s*page,\s*googleJoinButtonSelectors,[\s\S]{0,200}?\)/.exec(body);
  check(
    "the join-button wait is given a refusal guard",
    !!ctaCall && /refused\s*\(/.test(ctaCall[0]),
    ctaCall ? "call found but no refused(...) argument" : "join-button wait call not found",
  );
}

// ── The re-knock loop in index.ts ───────────────────────────────────────────────────────────────
// Meet's 403 is flaky per knock (~64% in prod, upstream's measurement; our own three attempts on
// meeting 1518 on 17 Nov went fail, fail, succeed on one node). One refusal must therefore not end
// the join: the loop re-knocks inside the SAME lobby budget the control plane issued.
console.log("\n=== the re-knock loop is wired into the google_meet branch ===");

const idx = fs.readFileSync(path.join(__dirname, "..", "index.ts"), "utf-8");

check(
  "index.ts imports the knock loop",
  /import\s*\{[^}]*knockUntilGoogleAccepts[^}]*\}\s*from\s*["']\.\/googlemeet\/refusal["']/.test(idx),
  "no import of knockUntilGoogleAccepts from ./googlemeet/refusal",
);

// The google_meet branch only — the loop must not wrap another platform.
const gm = (() => {
  const at = idx.indexOf('platform === "google_meet"');
  if (at === -1) return "";
  const rest = idx.slice(at);
  const end = rest.indexOf("} else {");
  return end === -1 ? rest : rest.slice(0, end);
})();

check(
  "the google_meet branch knocks through the loop",
  /knockUntilGoogleAccepts\s*\(/.test(gm),
  "google_meet branch does not call knockUntilGoogleAccepts",
);

check(
  "the knock callback performs the join AND the admission wait",
  /joinGoogleMeeting\s*\(/.test(gm) && /waitForGoogleMeetingAdmission\s*\(/.test(gm),
  "the branch no longer calls joinGoogleMeeting + waitForGoogleMeetingAdmission",
);

check(
  "the loop is given the deployment's knock config",
  /googleKnockConfig\s*\(/.test(gm),
  "googleKnockConfig() is not read in the google_meet branch (knocks would not be configurable)",
);

// NEGATIVE CONTROL — the loop is Meet-only. LiveKit is a separate branch and must be untouched:
// taking upstream's index.ts wholesale would silently delete our LiveKit platform.
{
  const lk = (() => {
    const at = idx.indexOf('platform === "livekit"');
    if (at === -1) return "";
    const rest = idx.slice(at);
    const end = rest.indexOf('} else if');
    return end === -1 ? rest : rest.slice(0, end);
  })();
  check(
    "LiveKit still joins directly — the Meet knock loop did not leak into it",
    /joinLivekitMeeting\s*\(/.test(lk) && !/knockUntilGoogleAccepts/.test(lk),
    lk ? "livekit branch changed" : "livekit branch not found AT ALL",
  );
}

console.log(`\n=== summary: ${passed} passed, ${failed} failed ===`);
process.exit(failed > 0 ? 1 : 0);
