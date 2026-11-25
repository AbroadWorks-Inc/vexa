/**
 * Google Meet «Continue as guest» pre-join step (seen in prod 12 Nov 2026: 12 joins failed with
 * «Could not locate name input … after 120000ms»; an outside user posted the screen the same morning).
 *
 * WHAT THIS FILE PINS
 *   1. When the guest step is on screen, the bot presses «Continue as guest» once, then finds the name field.
 *   2. When the guest step is absent, nothing is pressed (the handler is inert on today's normal lobby).
 *   3. It is pressed at most once per page, even if the button stays visible.
 *   4. A name-field miss is never blind: the thrown message carries the visible text-button labels,
 *      and a scan that failed reads "(scan failed)", never "(none)".
 *   5. A lobby that shows neither the name field nor any button 30 s in is reloaded once, inside the
 *      same 120 s budget; a lobby that shows a button is not.
 *
 * The page is a stand-in (no browser, no Google): it models only visibility by selector and a click.
 * Run: npx tsx src/googlemeet/guest-step.test.ts
 */
import { googleContinueAsGuestSelectors, googleNameInputSelectors } from './selectors';
import * as fs from 'fs';
import * as path from 'path';
import { waitForAnySelector, pressContinueAsGuestIfPresent, reloadBlankLobbyOnce, BLANK_LOBBY_RELOAD_AFTER_MS } from './join';

let passed = 0, failed = 0;
function assert(cond: boolean, msg: string): void {
  if (cond) { passed++; console.log(`  PASS  ${msg}`); } else { failed++; console.log(`  FAIL  ${msg}`); }
}

const GUEST = googleContinueAsGuestSelectors[0];
const NAME = googleNameInputSelectors[0];

function mockPage(o: { guest: boolean; nameAfterGuest: boolean; guestSticks?: boolean; labels?: string[]; scanThrows?: boolean }) {
  const state = { guestVisible: o.guest, nameVisible: !o.guest && o.nameAfterGuest };
  const el = { __element: true };
  return {
    state,
    url: () => 'https://meet.google.com/abc-defg-hij',
    locator: (sel: string) => ({
      first: () => ({
        isVisible: async () => (sel === GUEST ? state.guestVisible : sel === NAME ? state.nameVisible : false),
        elementHandle: async () => el,
      }),
    }),
    waitForTimeout: async (_ms: number) => { await new Promise((r) => setTimeout(r, 1)); },
    screenshot: async (_o: any) => {},
    evaluate: async (_fn: any) => ({ lang: 'en', nav: 'en-US' }),
    evaluateHandle: async (_fn: any, _opts: any) => { if (o.scanThrows) throw new Error('Execution context was destroyed'); return {
      getProperty: async (k: string) => ({
        jsonValue: async () => (k === 'labels' ? (o.labels || []) : null),
        asElement: () => null,
      }),
      dispose: async () => {},
    }; },
  };
}

/** A lobby on a virtual clock: blank until reloaded, then the name field renders `nameAfterReloadMs` later. */
function blankLobby(o: { labels: string[]; nameAfterReloadMs: number }) {
  let now = 1_000_000;
  let reloadedAt: number | null = null;
  const page = mockPage({ guest: false, nameAfterGuest: false, labels: o.labels }) as any;
  const name = page.locator(NAME).first();
  page.locator = (sel: string) => ({
    first: () => (sel === NAME
      ? { ...name, isVisible: async () => reloadedAt !== null && now - reloadedAt >= o.nameAfterReloadMs }
      : { isVisible: async () => false, elementHandle: async () => null }),
  });
  page.waitForTimeout = async (ms: number) => { now += ms; };
  page.reload = async () => { reloadedAt = now; };
  return { page, now: () => now, reloads: () => (reloadedAt === null ? 0 : 1), reloadedAt: () => reloadedAt };
}

(async () => {
  // 1. guest step present → pressed once → name field found
  {
    const page = mockPage({ guest: true, nameAfterGuest: true });
    const clicks: string[] = [];
    const click = async (_h: any, label: string) => {
      clicks.push(label);
      page.state.guestVisible = false; page.state.nameVisible = true;
    };
    const hit = await waitForAnySelector(page as any, googleNameInputSelectors, 2000, 'name input',
      async () => { await pressContinueAsGuestIfPresent(page as any, click); }, true);
    assert(clicks.length === 1 && clicks[0] === 'continue_as_guest', 'guest step present: «Continue as guest» pressed once');
    assert(hit.selector === NAME, 'guest step present: the name field is found after the press');
  }
  // 2. guest step absent → nothing pressed
  {
    const page = mockPage({ guest: false, nameAfterGuest: true });
    const clicks: string[] = [];
    const hit = await waitForAnySelector(page as any, googleNameInputSelectors, 2000, 'name input',
      async () => { await pressContinueAsGuestIfPresent(page as any, async (_h: any, l: string) => { clicks.push(l); }); }, true);
    assert(clicks.length === 0, 'guest step absent: nothing pressed');
    assert(hit.selector === NAME, 'guest step absent: the name field is found as before');
  }
  // 3. button stays visible → pressed at most once per page
  {
    const page = mockPage({ guest: true, nameAfterGuest: false });
    let n = 0;
    try {
      await waitForAnySelector(page as any, googleNameInputSelectors, 60, 'name input',
        async () => { await pressContinueAsGuestIfPresent(page as any, async () => { n++; }); }, true);
    } catch { /* expected miss */ }
    assert(n === 1, `sticky guest button: pressed exactly once (got ${n})`);
  }
  // 3b. a press that throws does not change how the wait ends
  {
    const page = mockPage({ guest: true, nameAfterGuest: false, labels: ['Continue as guest'] });
    let msg = '';
    try {
      await waitForAnySelector(page as any, googleNameInputSelectors, 40, 'name input',
        async () => { await pressContinueAsGuestIfPresent(page as any, async () => { throw new Error('element is detached'); }); }, true);
    } catch (e: any) { msg = String(e?.message || e); }
    assert(msg.startsWith('Could not locate name input'), 'failed press: the wait still ends as the normal name-input miss');
  }
  // 4. a name-field miss records the visible text-button labels
  {
    const page = mockPage({ guest: false, nameAfterGuest: false, labels: ['Continue as guest', 'Sign in'] });
    let msg = '';
    try { await waitForAnySelector(page as any, googleNameInputSelectors, 30, 'name input', undefined, true); }
    catch (e: any) { msg = String(e?.message || e); }
    assert(msg.startsWith('Could not locate name input'), 'name miss: keeps the prefix prod monitoring greps');
    assert(msg.includes('"Continue as guest"') && msg.includes('"Sign in"'), 'name miss: the visible button labels are in the message');
  }
  // 4b. a scan that throws is reported as such, never as an empty page
  {
    const page = mockPage({ guest: false, nameAfterGuest: false, scanThrows: true });
    let msg = '';
    try { await waitForAnySelector(page as any, googleNameInputSelectors, 30, 'name input', undefined, true); }
    catch (e: any) { msg = String(e?.message || e); }
    assert(msg.includes('visible text buttons: (scan failed)') && !msg.includes('(none)'),
      `failed scan: the message says "(scan failed)", not "(none)" (got: ${msg.slice(msg.indexOf('visible text buttons'))})`);
  }
  // 5. blank lobby → reloaded once at the 30 s mark, then the name field is found within the same 120 s
  const realNow = Date.now;
  try {
    const l = blankLobby({ labels: [], nameAfterReloadMs: 3000 });
    Date.now = l.now;
    const started = Date.now();
    const hit = await waitForAnySelector(l.page, googleNameInputSelectors, 120_000, 'name input',
      async () => { await reloadBlankLobbyOnce(l.page, started); }, true);
    const at = (l.reloadedAt() ?? 0) - started;
    assert(l.reloads() === 1 && at >= BLANK_LOBBY_RELOAD_AFTER_MS && at < BLANK_LOBBY_RELOAD_AFTER_MS + 1000,
      `blank lobby: reloaded once, at the 30 s mark (at ${at}ms)`);
    assert(hit.selector === NAME && Date.now() - started < 120_000, 'blank lobby: the name field is found after the reload, inside the 120 s budget');
  } finally { Date.now = realNow; }
  // 5b. a lobby that shows a button is not reloaded (it is not blank, something else is in the way)
  try {
    const l = blankLobby({ labels: ['Sign in'], nameAfterReloadMs: 0 });
    Date.now = l.now;
    const started = Date.now();
    try {
      await waitForAnySelector(l.page, googleNameInputSelectors, 60_000, 'name input',
        async () => { await reloadBlankLobbyOnce(l.page, started); }, true);
    } catch { /* expected miss */ }
    assert(l.reloads() === 0, 'a lobby showing a button: never reloaded');
  } finally { Date.now = realNow; }
  // 5c. the real name-field wait carries the reload in its guard
  {
    const src = fs.readFileSync(path.join(__dirname, 'join.ts'), 'utf-8');
    const call = /waitForAnySelector\(\s*page,\s*googleNameInputSelectors,[\s\S]{0,800}?\n    \);/.exec(src);
    assert(!!call && /reloadBlankLobbyOnce\(page,/.test(call[0]), 'join.ts: the name-input wait reloads a blank lobby once');
  }
  console.log(`${passed} passed, ${failed} failed`);
  if (failed) process.exit(1);
})();
