/**
 * Google Meet guest refusal — detecting that the tab has LEFT the lobby.
 *
 * THE BUG (measured 2026-11-16, meetings 1515 and 1516, 7 bot runs):
 * Google refuses an unauthenticated guest with an early HTTP 403 (~3.1 s after
 * the join page opens; a 0.01 s spread across three runs — a server-side
 * decision, not a race). The tab is then moved off the lobby, and the bot —
 * which has no idea this happened — keeps hunting for a name input for up to
 * 120 s (join.ts:457). It eventually matches the GENERIC selector
 * `input[type="text"]:not([aria-hidden="true"])` on whatever page it has landed
 * on, types the bot name into it, finds no join button, and exits 1 reporting
 * "join button not found" — which is not what went wrong.
 *
 * Measured on 2026-11-16:
 *   46987e0f  403 at +3.10 s  -> generic selector at 64 s -> exit 1
 *   3a51d257  403 at +3.10 s  -> generic selector at 64 s -> exit 1
 *   f367e0a0  403 at +3.09 s  -> generic selector at 64 s -> exit 1
 *   dac77447  403 at +11.15 s -> input[jsname] at 3 s -> knocked successfully
 *   f3910f87  403 at +12.24 s -> input[jsname] at 3 s -> knocked successfully
 *
 * WHAT THIS FILE PINS
 *   1. A URL on the lobby host is NOT a refusal, whatever its path or query.
 *   2. A URL on any OTHER host IS a refusal.
 *   3. The `/meet/<code>` path form is NOT a refusal. This matters: 11 of 1368
 *      production meetings carry it (aw-bots-vexa-postgres, 2026-11-16), because
 *      senders paste the post-redirect URL out of their address bar. Five of
 *      those eleven COMPLETED. Treating them as refusals would break working
 *      meetings — the single most expensive mistake this detector could make.
 *   4. An unparseable URL is NOT evidence of refusal (fail safe, never fail shut).
 *   5. Host comparison is case-insensitive.
 *
 * NOT PINNED: the timing of the redirect. Only one `url=` observation exists per
 * production run (the +1 s locale log), so WHEN the tab leaves the lobby is
 * unmeasured. This detector deliberately does not depend on timing — it reports
 * the refusal whenever the move happens.
 *
 * No browser, no live meeting, no Google.
 *
 * Run: npx tsx src/googlemeet/guest-refusal.test.ts
 */

import { isGuestRefusalUrl, pollForGuestRefusal } from './join';

let failures = 0;
function assert(cond: boolean, msg: string): void {
  if (cond) {
    console.log(`  ok   ${msg}`);
  } else {
    failures++;
    console.error(`  FAIL ${msg}`);
  }
}

console.log('isGuestRefusalUrl — the lobby host is the whole test');

// 1. The real lobby, exactly as the bot navigates to it.
assert(
  isGuestRefusalUrl('https://meet.google.com/eni-huhs-tfb?hl=en') === false,
  'canonical lobby URL is not a refusal'
);
assert(
  isGuestRefusalUrl('https://meet.google.com/wvu-hskv-aba?hl=en') === false,
  'second canonical lobby URL is not a refusal'
);

// 2. Where a refused guest actually ends up.
assert(
  isGuestRefusalUrl('https://workspace.google.com/products/meet/') === true,
  'workspace.google.com product page IS a refusal'
);
assert(
  isGuestRefusalUrl('https://www.google.com/') === true,
  'any other Google host IS a refusal'
);

// 3. The /meet/ path form — production reality, must NOT trip the detector.
assert(
  isGuestRefusalUrl('https://meet.google.com/meet/rwn-zswo-fgf?authuser=0') === false,
  '/meet/<code> form is NOT a refusal (11 live meetings carry it; 5 completed)'
);
assert(
  isGuestRefusalUrl('https://meet.google.com/meet/dov-ifgr-uhd') === false,
  '/meet/<code> without query is NOT a refusal'
);

// 4. Unparseable input must fail SAFE — never strand a healthy run.
assert(
  isGuestRefusalUrl('not a url') === false,
  'unparseable URL is not evidence of refusal'
);
assert(
  isGuestRefusalUrl('') === false,
  'empty URL is not evidence of refusal'
);
assert(
  isGuestRefusalUrl('about:blank') === false,
  'about:blank is not evidence of refusal (page not navigated yet)'
);

// 5. Host case must not matter.
assert(
  isGuestRefusalUrl('https://MEET.GOOGLE.COM/eni-huhs-tfb') === false,
  'upper-case lobby host is not a refusal'
);

// Negative control: a host that merely CONTAINS the lobby host must still be a
// refusal — substring matching would be the obvious wrong implementation.
assert(
  isGuestRefusalUrl('https://meet.google.com.evil.example/xyz') === true,
  'a host merely containing meet.google.com IS a refusal (no substring matching)'
);

/* ------------------------------------------------------------------------- *
 * pollForGuestRefusal — the watcher raced against the name-input wait.
 *
 * Deterministic: injected clock and sleep, no real timers, no browser.
 * ------------------------------------------------------------------------- */

function fakePage(urls: string[]) {
  let i = 0;
  return {
    url: () => urls[Math.min(i++, urls.length - 1)],
    calls: () => i,
  };
}

async function run(): Promise<void> {
  console.log('\npollForGuestRefusal — races the name-input wait');

  // Refusal appears on the 3rd poll: detected, and reported as true.
  {
    const lobby = 'https://meet.google.com/eni-huhs-tfb?hl=en';
    const page = fakePage([lobby, lobby, 'https://workspace.google.com/products/meet/']);
    let slept = 0;
    const refused = await pollForGuestRefusal(page, {
      timeoutMs: 120000,
      pollMs: 1000,
      sleep: async () => { slept++; },
      now: () => slept * 1000,
    });
    assert(refused === true, 'detects a refusal that appears mid-wait');
    assert(slept <= 3, `stops polling as soon as it sees the refusal (slept ${slept})`);
  }

  // Lobby holds for the whole budget: no refusal, and it does NOT invent one.
  {
    const lobby = 'https://meet.google.com/eni-huhs-tfb?hl=en';
    let slept = 0;
    const refused = await pollForGuestRefusal(fakePage([lobby]), {
      timeoutMs: 5000,
      pollMs: 1000,
      sleep: async () => { slept++; },
      now: () => slept * 1000,
    });
    assert(refused === false, 'a lobby that never moves is not a refusal');
    assert(slept >= 4, `polled until the budget elapsed (slept ${slept})`);
  }

  // A run that stays on the /meet/ form must never be reported as refused —
  // this is the production case that would break working meetings.
  {
    const meetForm = 'https://meet.google.com/meet/rwn-zswo-fgf?authuser=0';
    let slept = 0;
    const refused = await pollForGuestRefusal(fakePage([meetForm]), {
      timeoutMs: 3000,
      pollMs: 1000,
      sleep: async () => { slept++; },
      now: () => slept * 1000,
    });
    assert(refused === false, '/meet/ form is never reported as a refusal');
  }

  // A page whose url() throws must not crash the watcher or strand the run.
  {
    const page = { url: () => { throw new Error('page closed'); } };
    let slept = 0;
    const refused = await pollForGuestRefusal(page, {
      timeoutMs: 2000,
      pollMs: 1000,
      sleep: async () => { slept++; },
      now: () => slept * 1000,
    });
    assert(refused === false, 'a throwing page.url() is not evidence of refusal');
  }
}

run().then(() => {
  console.log(failures === 0 ? '\nPASS' : `\nFAIL — ${failures} assertion(s)`);
  process.exit(failures === 0 ? 0 : 1);
});
