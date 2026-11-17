/**
 * The ADMISSION wait ends on a Meet refusal instead of polling a page with nothing behind it.
 *
 * Captured in production 17 Nov 2026 (pod vexa-mtg-1518-b760b929): Meet answered HTTP 403 on
 * ResolveMeetingSpace, threw `DisconnectedError, EndCause = 72, StartupCode = 200`, and redirected
 * to workspace.google.com after ~64 s. No meeting device was ever created, so the host was never
 * asked — yet the bot reported progress and sat out its budget.
 *
 * The refusal screen carries "Return to home screen", which this fork deliberately classifies as a
 * WAITING-ROOM indicator (selectors.ts:54, Vexa-ai/vexa#471 — it used to cause a false reject in
 * ~4 s). So the second case below is the one that matters: the real screen shows BOTH, and the
 * refusal must win. If it does not, the bot waits out its whole lobby budget on a dead page.
 *
 * Fabricated DOM only — no browser, no Google, no live meeting.
 *
 * Run: npx tsx src/googlemeet/refusal-admission.test.ts
 */
import { waitForGoogleMeetingAdmission } from './admission';
import { AdmissionError } from '../shared/admission';
import { googleJoinRefusedIndicators } from './selectors';

const REFUSAL_TEXT = googleJoinRefusedIndicators[0];
const RETURN_HOME = 'button:has-text("Return to home screen")';
const RECAPTCHA_IFRAME = 'iframe[src*="recaptcha"]';

let passed = 0, failed = 0;
function check(name: string, ok: boolean, detail?: string) {
  if (ok) { console.log(`  \x1b[32mPASS\x1b[0m  ${name}`); passed++; }
  else { console.log(`  \x1b[31mFAIL\x1b[0m  ${name}${detail ? ` (got ${detail})` : ''}`); failed++; }
}

/** Minimal Playwright-Page stand-in: `visible` are the selectors that resolve isVisible()===true. */
function mockPage(visible: string[]): any {
  return {
    locator: (sel: string) => sel === RECAPTCHA_IFRAME
      ? { first: () => ({ isVisible: async () => false, boundingBox: async () => null, getAttribute: async () => null }),
          nth: () => ({ isVisible: async () => false, boundingBox: async () => null, getAttribute: async () => null }),
          count: async () => 0, evaluateAll: async () => [] }
      : { first: () => ({ isVisible: async () => visible.includes(sel), getAttribute: async () => null }),
          count: async () => (visible.includes(sel) ? 1 : 0), evaluateAll: async () => [] },
    mouse: { move: async () => {} },
    url: () => 'https://meet.google.com/abc-defg-hij',
    screenshot: async () => {},
    waitForTimeout: async (_ms: number) => { await new Promise(r => setTimeout(r, 2)); },
    frames: () => [{ url: () => 'https://meet.google.com/' }],
  };
}

/** A verdict, or NO-VERDICT if the wait is still polling when the watchdog fires. */
async function outcomeOf(run: Promise<unknown>, watchdogMs = 5_000): Promise<string> {
  const watchdog = new Promise<string>(resolve => {
    const t = setTimeout(() => resolve('NO-VERDICT (still polling after watchdog)'), watchdogMs);
    (t as any).unref?.();
  });
  return Promise.race([
    run.then(() => 'resolved', (e: any) => (e instanceof AdmissionError ? `AdmissionError:${e.outcome}` : `Error:${e.message}`)),
    watchdog,
  ]);
}

(async () => {
  console.log('\n=== the admission wait ends on a Meet refusal ===');

  // 1. The refusal screen alone.
  {
    const got = await outcomeOf(waitForGoogleMeetingAdmission(mockPage([REFUSAL_TEXT]), 20_000, {} as any));
    check("refusal screen → AdmissionError('denial'), not a lobby wait", got === 'AdmissionError:denial', got);
  }

  // 2. THE REAL SCREEN: refusal copy AND "Return to home screen" (a waiting-room indicator here).
  //    Without a refusal check the bot reads this as the waiting room and polls until its budget
  //    expires — the production failure. The refusal must win.
  {
    const got = await outcomeOf(waitForGoogleMeetingAdmission(mockPage([REFUSAL_TEXT, RETURN_HOME]), 20_000, {} as any));
    check("refusal + 'Return to home screen' → denial, NOT an endless waiting-room poll", got === 'AdmissionError:denial', got);
  }

  // 3. NEGATIVE CONTROL: the plain #471 waiting screen (no refusal copy) must still be a lobby
  //    wait. If this turns into a denial we have re-broken #471 — a false reject in ~4 s.
  {
    const got = await outcomeOf(waitForGoogleMeetingAdmission(mockPage([RETURN_HOME]), 20_000, {} as any));
    check("waiting screen alone → still a lobby wait, never a denial", got !== 'AdmissionError:denial', got);
  }

  console.log(`\n=== summary: ${passed} passed, ${failed} failed ===`);
  process.exit(failed > 0 ? 1 : 0);
})();
