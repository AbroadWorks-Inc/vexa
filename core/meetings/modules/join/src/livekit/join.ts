import { readFileSync } from "fs";
import { Page } from "playwright";
import { log, callAwaitingAdmissionCallback, BotConfig } from "../_host";
import { AdmissionError } from "../shared/admission";
import { RECORDING_NOTICE } from "../shared/recording-notice";
import { knockUntilAdmitted, parseLivekitUrl } from "./knock";

/**
 * Join a LiveKit website meeting: open the bot's own page, wait in Meet's waiting room until the
 * host admits the bot, then connect the page to the room. Admission is the room reaching
 * `connected`, not the token arriving.
 *
 * The page is served by the bot itself on a fixed HTTPS origin (Playwright fulfils every request,
 * so nothing is fetched from the network and the page is a secure context). It loads the LiveKit
 * page bundle, which puts the session on `window.VexaLivekit`.
 */

export const LIVEKIT_PAGE_ORIGIN = "https://livekit.aw-bots.invalid";

/** The LiveKit page bundle the bot image ships (`services/bot/Dockerfile`). */
export function livekitPageScriptPath(): string {
  return process.env.VEXA_LIVEKIT_PAGE_JS || "/app/livekit-page.global.js";
}

const PAGE_HTML =
  '<!doctype html><html><head><meta charset="utf-8"><title>LiveKit</title></head>' +
  '<body><script src="/livekit.js"></script></body></html>';

async function openLivekitPage(page: Page): Promise<void> {
  const script = readFileSync(livekitPageScriptPath(), "utf8");
  await page.route(`${LIVEKIT_PAGE_ORIGIN}/**`, (route) => {
    const path = new URL(route.request().url()).pathname;
    return path === "/livekit.js"
      ? route.fulfill({ status: 200, contentType: "text/javascript", body: script })
      : route.fulfill({ status: 200, contentType: "text/html", body: PAGE_HTML });
  });
  await page.goto(`${LIVEKIT_PAGE_ORIGIN}/`);
  await page.waitForFunction(() => !!(window as any).VexaLivekit, undefined, { timeout: 15_000 });
}

export async function joinLivekitMeeting(
  page: Page,
  meetingUrl: string,
  botName: string,
  botConfig: BotConfig,
  waitingRoomTimeoutMs: number,
): Promise<void> {
  const room = parseLivekitUrl(meetingUrl);
  await openLivekitPage(page);
  log(`[LiveKit] Asking to enter room ${room.code} on ${room.host}`);
  const { url, token } = await knockUntilAdmitted({
    host: room.host,
    code: room.code,
    botName,
    budgetMs: waitingRoomTimeoutMs,
    onWaiting: () => callAwaitingAdmissionCallback(botConfig),
    cancelled: () => page.isClosed(),
    log,
  });
  const { e2ee } = await page.evaluate(
    ([u, t]) => (window as any).VexaLivekit.connect(u, t) as Promise<{ e2ee: boolean }>,
    [url, token],
  );
  if (e2ee) {
    await page.evaluate(() => (window as any).VexaLivekit.disconnect()).catch(() => { /* leaving anyway */ });
    throw new AdmissionError("join_failure", "encrypted room: the bot cannot record an end-to-end encrypted meeting");
  }
  log(`[LiveKit] Connected to room ${room.code}`);
}

/** Post the recording notice into the meeting's chat, now and to each later joiner. Never throws. */
export async function announceLivekitAdmission(page: Page): Promise<void> {
  try {
    const posted = await page.evaluate((text) => (window as any).VexaLivekit.announce(text) as Promise<boolean>, RECORDING_NOTICE);
    log(posted ? "[LiveKit] Recording notice posted in chat" : "[LiveKit] Recording notice NOT posted in chat");
  } catch (e: any) {
    log(`[LiveKit] Recording notice NOT posted in chat: ${e?.message ?? e}`);
  }
}
