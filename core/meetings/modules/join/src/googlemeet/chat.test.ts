/**
 * Google Meet recording-notice chat post.
 *
 * Drives googleMeetChatSend — the function page.evaluate serializes into the
 * page — against jsdom fixtures. No browser.
 *
 * Run: npx tsx src/googlemeet/chat.test.ts
 */

import { readFileSync } from "fs";
import { JSDOM } from "jsdom";
import {
  announceGoogleMeetAdmission,
  googleMeetChatSend,
  MEET_RECORDING_NOTICE,
  postGoogleMeetRecordingNotice,
} from "./chat";
import {
  googleChatComposerSelectors,
  googleChatOpenButtonSelectors,
  googleChatSendButtonSelectors,
} from "./selectors";

let passed = 0, failed = 0;
function check(name: string, actual: unknown, expected: unknown) {
  if (actual === expected) { console.log(`  \x1b[32mPASS\x1b[0m  ${name}`); passed++; }
  else { console.log(`  \x1b[31mFAIL\x1b[0m  ${name} (expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)})`); failed++; }
}

function mountDom(html: string) {
  const dom = new JSDOM(`<!doctype html><html><body>${html}</body></html>`);
  dom.window.Element.prototype.getBoundingClientRect = function () {
    return { width: 120, height: 40, top: 0, left: 0, right: 120, bottom: 40, x: 0, y: 0, toJSON() {} } as any;
  };
  const clicked: string[] = [];
  const keys: string[] = [];
  for (const el of Array.from(dom.window.document.querySelectorAll("button, [role='button'], textarea, [role='textbox']"))) {
    el.addEventListener("click", () => clicked.push(el.getAttribute("data-fixture-id") || el.getAttribute("aria-label") || "click"));
    el.addEventListener("keydown", (ev: any) => { if (ev.key) keys.push(ev.key); });
  }
  (globalThis as any).window = dom.window;
  (globalThis as any).document = dom.window.document;
  (globalThis as any).getComputedStyle = dom.window.getComputedStyle.bind(dom.window);
  (globalThis as any).HTMLTextAreaElement = dom.window.HTMLTextAreaElement;
  (globalThis as any).HTMLInputElement = dom.window.HTMLInputElement;
  (globalThis as any).KeyboardEvent = dom.window.KeyboardEvent;
  (globalThis as any).InputEvent = dom.window.InputEvent;
  (globalThis as any).Event = dom.window.Event;
  return { dom, clicked, keys };
}

const argsFor = (text: string) => ({
  text,
  openSelectors: googleChatOpenButtonSelectors,
  composerSelectors: googleChatComposerSelectors,
  sendSelectors: googleChatSendButtonSelectors,
});

(async () => {
  console.log("\n=== the notice text is the recording disclosure, not a leave promise ===");
  check("names AW Notetaker and transcription", MEET_RECORDING_NOTICE.includes("AW Notetaker is recording this meeting for transcription"), true);
  check("links the privacy notice", MEET_RECORDING_NOTICE.includes("https://abroadworks.com/notetaker-privacy"), true);
  check("tells people to ask the host", MEET_RECORDING_NOTICE.includes("ask the host to remove this bot"), true);
  check("does not promise the bot will leave", MEET_RECORDING_NOTICE.includes("stop recording"), false);

  console.log("\n=== closed chat: open the panel, type the notice, click send once ===");
  {
    const { dom, clicked } = mountDom(`
      <button aria-label="Chat with everyone" data-fixture-id="open"></button>
      <textarea aria-label="Send a message" data-fixture-id="box" style="display:none"></textarea>
      <button aria-label="Send a message" data-fixture-id="send" style="display:none" aria-disabled="true"></button>
    `);
    const open = dom.window.document.querySelector("[data-fixture-id='open']") as HTMLElement;
    open.addEventListener("click", () => {
      for (const id of ["box", "send"]) {
        (dom.window.document.querySelector(`[data-fixture-id='${id}']`) as HTMLElement).style.display = "";
      }
    });
    const result = await googleMeetChatSend(argsFor("recording notice"));
    const box = dom.window.document.querySelector("textarea") as HTMLTextAreaElement;
    check("returns clicked send", result.ok && result.detail, true && "clicked send");
    check("opened the chat once", clicked.filter((id) => id === "open").length, 1);
    check("clicked send once", clicked.filter((id) => id === "send").length, 1);
    check("the composer holds the notice", box.value, "recording notice");
    check("send is no longer aria-disabled", (dom.window.document.querySelector("[data-fixture-id='send']") as HTMLElement).hasAttribute("aria-disabled"), false);
  }

  console.log("\n=== chat already open: do not click the opener again ===");
  {
    const { clicked } = mountDom(`
      <button aria-label="Chat with everyone" data-fixture-id="open"></button>
      <textarea aria-label="Send a message to everyone" data-fixture-id="box"></textarea>
      <button aria-label="Send a message" data-fixture-id="send" disabled></button>
    `);
    const result = await googleMeetChatSend(argsFor("already open"));
    check("returns clicked send", result.detail, "clicked send");
    check("opener was not clicked", clicked.includes("open"), false);
    check("send was clicked", clicked.includes("send"), true);
  }

  console.log("\n=== no send button: Enter submits the composer ===");
  {
    const { dom, keys } = mountDom(`<textarea aria-label="Send a message" data-fixture-id="box"></textarea>`);
    const result = await googleMeetChatSend(argsFor("enter path"));
    const box = dom.window.document.querySelector("textarea") as HTMLTextAreaElement;
    check("returns pressed enter", result.detail, "pressed enter");
    check("Enter was dispatched", keys.includes("Enter"), true);
    check("the composer holds the text", box.value, "enter path");
  }

  console.log("\n=== contenteditable composer ===");
  {
    const { dom, clicked } = mountDom(`
      <div role="textbox" aria-label="Send a message" contenteditable="true" data-fixture-id="box"></div>
      <button aria-label="Send" data-fixture-id="send"></button>
    `);
    const result = await googleMeetChatSend(argsFor("rich text"));
    const box = dom.window.document.querySelector("[role='textbox']") as HTMLElement;
    check("returns clicked send", result.detail, "clicked send");
    check("textbox holds the text", box.textContent, "rich text");
    check("send was clicked", clicked.includes("send"), true);
  }

  console.log("\n=== chat disabled, and an empty notice ===");
  {
    const { clicked } = mountDom(`<button aria-label="Leave call" data-fixture-id="leave"></button>`);
    const missing = await googleMeetChatSend(argsFor("nobody can chat"));
    check("no chat control", `${missing.ok}:${missing.detail}`, "false:no chat control");
    check("nothing was clicked", clicked.length, 0);
    const empty = await googleMeetChatSend(argsFor("   "));
    check("empty text is not sent", empty.detail, "empty text");
  }

  console.log("\n=== the wrapper posts the real notice and a chat failure stays a joined meeting ===");
  {
    let seen = "";
    const page = {
      async evaluate(fn: (arg: any) => Promise<any>, arg: any) {
        seen = arg.text;
        return fn(arg);
      },
    } as any;
    mountDom(`
      <textarea aria-label="Send a message"></textarea>
      <button aria-label="Send a message" data-fixture-id="send"></button>
    `);
    const posted = await postGoogleMeetRecordingNotice(page);
    check("wrapper reports the post", posted, true);
    check("wrapper sends the recording notice", seen, MEET_RECORDING_NOTICE);
  }
  {
    const page = { async evaluate() { throw new Error("page closed"); } } as any;
    let threw = false;
    try { await announceGoogleMeetAdmission(page); } catch { threw = true; }
    check("a chat exception does not fail the join", threw, false);
  }

  console.log("\n=== joinMeeting calls the notice only for an admitted Google Meet ===");
  {
    const indexSrc = readFileSync(require("path").join(__dirname, "../index.ts"), "utf8");
    check(
      "admission hook is present",
      /if \(admitted && platform === "google_meet"\) \{\s*await announceGoogleMeetAdmission\(page\);\s*\}/.test(indexSrc),
      true,
    );
  }

  console.log(`\n=== summary: ${passed} passed, ${failed} failed ===`);
  process.exit(failed > 0 ? 1 : 0);
})();
