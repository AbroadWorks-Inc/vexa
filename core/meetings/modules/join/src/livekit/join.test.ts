/**
 * LiveKit join, leave and removal.
 *
 * The removal monitor runs against a scripted page (no browser). The join and leave run in headless
 * Chromium on the bot's own page origin, with a stub `window.VexaLivekit` in place of the
 * livekit-client bundle and `fetch` serving Meet's recorded answers. Where headless Chromium cannot
 * launch, that part SKIPS LOUDLY (same shape as the bot's boundary tests).
 *
 * Run: npx tsx src/livekit/join.test.ts
 */
import { mkdtempSync, readFileSync, writeFileSync } from "fs";
import { tmpdir } from "os";
import { join } from "path";
import { joinMeeting, leaveLivekitMeeting, setHooks, startLivekitRemovalMonitor, type JoinState } from "../index";
import { AdmissionError } from "../shared/admission";
import { RECORDING_NOTICE } from "../shared/recording-notice";

let passed = 0;
let failed = 0;
function check(name: string, ok: boolean, detail?: string) {
  if (ok) { console.log(`  \x1b[32mPASS\x1b[0m  ${name}`); passed++; }
  else { console.log(`  \x1b[31mFAIL\x1b[0m  ${name}${detail ? ` — ${detail}` : ""}`); failed++; }
}
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

async function removalFires(states: { connected: boolean; disconnectReason?: string }[]): Promise<number> {
  let i = 0;
  const page = {
    isClosed: () => false,
    evaluate: async () => states[Math.min(i++, states.length - 1)],
  };
  let fired = 0;
  const stop = startLivekitRemovalMonitor(page as any, () => { fired++; });
  await sleep(3500);
  stop();
  return fired;
}

const STUB = `
  window.__lk = { connects: [], chats: [], disconnects: 0 };
  window.VexaLivekit = {
    async connect(url, token) { window.__lk.connects.push([url, token]); return { e2ee: !!window.__lkE2ee }; },
    async announce(text) { window.__lk.chats.push(text); return !window.__lkChatFails; },
    async disconnect() { window.__lk.disconnects++; },
    state() { return { connected: true }; },
  };
`;

async function main() {
  check("removal: fires once when the host removes the bot",
    (await removalFires([{ connected: true }, { connected: false, disconnectReason: "PARTICIPANT_REMOVED" }])) === 1);
  check("removal: fires once when the room is deleted",
    (await removalFires([{ connected: false, disconnectReason: "ROOM_DELETED" }])) === 1);
  check("removal: fires once when the connection is lost (as every platform)",
    (await removalFires([{ connected: false, disconnectReason: "SIGNAL_CLOSE" }])) === 1);
  check("removal: never fires for our own disconnect",
    (await removalFires([{ connected: false, disconnectReason: "CLIENT_INITIATED" }])) === 0);
  check("removal: never fires while connected", (await removalFires([{ connected: true }])) === 0);

  const fixtures = join(__dirname, "fixtures");
  let answers: string[] = [];
  (globalThis as any).fetch = async () => new Response(readFileSync(join(fixtures, `${answers.shift() ?? "accepted"}.json`), "utf8"), { status: 200 });
  const stubPath = join(mkdtempSync(join(tmpdir(), "vexa-livekit-")), "stub.js");
  writeFileSync(stubPath, STUB);
  process.env.VEXA_LIVEKIT_PAGE_JS = stubPath;

  let chromium: typeof import("playwright").chromium;
  let browser: import("playwright").Browser;
  try {
    ({ chromium } = await import("playwright"));
    browser = await chromium.launch({ headless: true });
  } catch (e) {
    console.log(`  ⚠️ SKIP join/leave — headless Chromium unavailable: ${(e as Error).message?.split("\n")[0]}`);
    console.log(`\nlivekit join: ${passed} passed, ${failed} failed (browser part skipped)`);
    if (failed) process.exit(1);
    return;
  }
  const url = "https://meet-next.example.org/abc-defg-hij";
  try {
    {
      const page = await browser.newPage();
      const states: JoinState[] = [];
      answers = ["waiting", "accepted"];
      const r = await joinMeeting(page, { platform: "livekit", meetingUrl: url, botName: "Olanote", waitingRoomTimeoutMs: 60_000, hooks: { onState: (s) => { states.push(s); } } });
      const lk = await page.evaluate(() => (window as any).__lk);
      check("admitted after the host lets the bot in", r.admitted === true);
      check("the page is the bot's own origin, a secure context", page.url().startsWith("https://livekit.aw-bots.invalid/") && (await page.evaluate(() => window.isSecureContext)));
      check("connect gets the LiveKit url and token from Meet", JSON.stringify(lk.connects) === JSON.stringify([["https://livekit.example.org", "TEST-TOKEN-NOT-REAL"]]), JSON.stringify(lk.connects));
      check("awaiting_admission reported while waiting", states.includes("awaiting_admission"), states.join(","));
      check("the recording notice is posted once", JSON.stringify(lk.chats) === JSON.stringify([RECORDING_NOTICE]), JSON.stringify(lk.chats));
      setHooks({ onState: (s) => { states.push(s); } });
      await leaveLivekitMeeting(page, { platform: "livekit" }, "manual_leave");
      check("leave disconnects the room", (await page.evaluate(() => (window as any).__lk.disconnects)) === 1);
      check("leave reports leaving", states.includes("leaving"), states.join(","));
      await page.close();
    }
    {
      const page = await browser.newPage();
      await page.addInitScript(() => { (window as any).__lkE2ee = true; });
      answers = ["accepted"];
      let outcome = "resolved";
      try { await joinMeeting(page, { platform: "livekit", meetingUrl: url, botName: "Olanote", waitingRoomTimeoutMs: 60_000 }); }
      catch (e) { outcome = e instanceof AdmissionError ? `${e.outcome}: ${e.message}` : String(e); }
      check("an encrypted room is refused as join_failure", outcome.startsWith("join_failure") && outcome.includes("encrypted"), outcome);
      check("…and the bot disconnects from it", (await page.evaluate(() => (window as any).__lk.disconnects)) === 1);
      await page.close();
    }
    {
      const page = await browser.newPage();
      await page.addInitScript(() => { (window as any).__lkChatFails = true; });
      answers = ["accepted"];
      const r = await joinMeeting(page, { platform: "livekit", meetingUrl: url, botName: "Olanote", waitingRoomTimeoutMs: 60_000 });
      check("a notice that fails to send does not fail the join", r.admitted === true);
      await page.close();
    }
  } finally {
    await browser.close();
  }
  console.log(`\nlivekit join: ${passed} passed, ${failed} failed`);
  if (failed) process.exit(1);
}

main().catch((e) => { console.error(e); process.exit(1); });
