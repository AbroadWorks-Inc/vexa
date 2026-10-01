/**
 * Post the recording notice into Google Meet chat once the bot is admitted.
 *
 * This is not the acts.v1 `chat_send` command, and `voiceAgentEnabled` does
 * not turn it on. Those only name a voice-agent command this bot does not
 * run. The notice is sent from the join, on every admitted Meet. A missing
 * chat control is reported and does not fail the join.
 */
import { Page } from "playwright";
import { log } from "../_host";
import {
  googleChatComposerSelectors,
  googleChatOpenButtonSelectors,
  googleChatSendButtonSelectors,
} from "./selectors";

// Design §5.2 notice, plus the §14 #4 line that the host removes the bot.
// The unsigned legal draft that says the bot leaves and deletes the recording
// is not used: the bot does not do that.
export const MEET_RECORDING_NOTICE =
  "AW Notetaker is recording this meeting for transcription. " +
  "See https://abroadworks.com/notetaker-privacy for details. " +
  "If you'd prefer the meeting not be recorded, ask the host to remove this bot.";

export type GoogleMeetChatSendArgs = {
  text: string;
  openSelectors: string[];
  composerSelectors: string[];
  sendSelectors: string[];
};

export type GoogleMeetChatSendResult = { ok: boolean; detail: string };

// Runs INSIDE the page via page.evaluate. Self-contained: DOM globals and
// `args` only. Selectors are plain CSS (document.querySelector).
export async function googleMeetChatSend(
  args: GoogleMeetChatSendArgs,
): Promise<GoogleMeetChatSendResult> {
  (globalThis as any).__name = (globalThis as any).__name || ((f: unknown) => f);
  const text = String(args?.text ?? "").replace(/\s+/g, " ").trim();
  if (!text) return { ok: false, detail: "empty text" };

  const isVisible = (el: Element) => {
    const rect = el.getBoundingClientRect();
    const cs = getComputedStyle(el as HTMLElement);
    return rect.width > 0 && rect.height > 0
      && cs.display !== "none" && cs.visibility !== "hidden" && cs.opacity !== "0";
  };

  const firstVisible = (selectors: string[]): HTMLElement | null => {
    for (const sel of selectors) {
      let nodes: Element[];
      try {
        nodes = Array.from(document.querySelectorAll(sel));
      } catch {
        continue;
      }
      const hit = nodes.find((el) => isVisible(el)) as HTMLElement | undefined;
      if (hit) return hit;
    }
    return null;
  };

  const wait = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

  let composer = firstVisible(args.composerSelectors || []);
  let opener: HTMLElement | null = null;
  if (!composer) {
    opener = firstVisible(args.openSelectors || []);
    if (!opener) return { ok: false, detail: "no chat control" };
    opener.click();
    const deadline = Date.now() + 2000;
    for (;;) {
      composer = firstVisible(args.composerSelectors || []);
      if (composer || Date.now() >= deadline) break;
      await wait(50);
    }
  }
  if (!composer) return { ok: false, detail: "chat panel did not open" };

  if (composer instanceof HTMLTextAreaElement || composer instanceof HTMLInputElement) {
    const proto = composer instanceof HTMLTextAreaElement
      ? HTMLTextAreaElement.prototype
      : HTMLInputElement.prototype;
    const desc = Object.getOwnPropertyDescriptor(proto, "value");
    if (desc && desc.set) desc.set.call(composer, text);
    else composer.value = text;
    composer.dispatchEvent(new Event("input", { bubbles: true }));
  } else {
    composer.focus();
    composer.textContent = text;
    composer.dispatchEvent(new InputEvent("input", { bubbles: true, data: text }));
  }

  const send = firstVisible(args.sendSelectors || []);
  if (send && send !== opener) {
    send.removeAttribute("disabled");
    send.removeAttribute("aria-disabled");
    send.click();
    return { ok: true, detail: "clicked send" };
  }

  composer.focus();
  const key: KeyboardEventInit = {
    key: "Enter", code: "Enter", bubbles: true, cancelable: true,
  };
  composer.dispatchEvent(new KeyboardEvent("keydown", key));
  composer.dispatchEvent(new KeyboardEvent("keyup", key));
  return { ok: true, detail: "pressed enter" };
}

export async function postGoogleMeetRecordingNotice(
  page: Page,
  text: string = MEET_RECORDING_NOTICE,
): Promise<boolean> {
  const result = await page.evaluate(googleMeetChatSend, {
    text,
    openSelectors: googleChatOpenButtonSelectors,
    composerSelectors: googleChatComposerSelectors,
    sendSelectors: googleChatSendButtonSelectors,
  }) as GoogleMeetChatSendResult;
  if (result?.ok) {
    log(`[chat] posted recording notice (${result.detail})`);
    return true;
  }
  log(`[chat] recording notice was not posted: ${result?.detail ?? "no result"}`);
  return false;
}

// Called only after Google Meet admission. Never throws: the bot is already
// in the meeting, and a chat miss must not turn that into a failed join.
export async function announceGoogleMeetAdmission(page: Page): Promise<void> {
  try {
    await postGoogleMeetRecordingNotice(page);
  } catch (err: any) {
    log(`[chat] recording notice failed: ${err?.message ?? err}`);
  }
}
