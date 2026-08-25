import { Page } from 'playwright';
import { BotConfig } from '../../../types';
import { AdmissionDecision } from '../../shared/meetingFlow';
import { log, callAwaitingAdmissionCallback } from '../../../utils';
import { checkEscalation, triggerEscalation, getEscalationExtensionMs } from '../../shared/escalation';
import {
  zoomLeaveButtonSelector,
  zoomMeetingAppSelector,
  zoomWaitingRoomTexts,
  zoomRemovalTexts,
  zoomInvalidMeetingText,
  zoomInvalidMeetingTitle,
} from './selectors';
import { revealZoomFooter } from './prepare';

/**
 * Check if the bot is confirmed inside the meeting.
 * Primary:   Leave button visible (footer is showing). Strong positive —
 *            this control never renders in the waiting room.
 * Fallback1: .meeting-app container present (footer may be auto-hidden).
 * Fallback2: live <audio> elements AND no pre-join-page indicators —
 *            Zoom Web preloads audio streams on the pre-join page itself
 *            (local mic preview), so audio presence alone is NOT enough.
 *            Require the pre-join name input AND join button to be absent.
 *            (Observed 2026-04-26 meeting_id=31: bot was at
 *            "Enter Meeting Info"/passcode-entry screen with 3 live audio
 *            elements; an earlier audio-only fallback falsely reported
 *            admitted, status=active appeared on the dashboard while the
 *            bot was actually still pre-join.)
 *
 * IMPORTANT — waiting-room exclusion runs before BOTH fallbacks:
 * Zoom renders the waiting room INSIDE `.meeting-app` (so fallback 1
 * fires false-positive there), and the bot's mic-preview audio stays live
 * across the pre-join → waiting-room transition while pre-join DOM
 * indicators are already gone (so fallback 2 fires false-positive too).
 * Without the exclusion, the bot reports admitted and the dashboard skips
 * the `awaiting_admission` state entirely. Observed 2026-04-26
 * meeting_id=36: screenshot showed "Host has joined. We've let them know
 * you're here." while the bot reported admitted=true.
 */
async function isAdmitted(page: Page): Promise<boolean> {
  try {
    // Strong positive: Leave button is footer-only, never appears in
    // pre-join or waiting room. Trust it without further checks.
    const leaveBtn = page.locator(zoomLeaveButtonSelector).first();
    if (await leaveBtn.isVisible({ timeout: 500 })) return true;

    // Before the weaker fallbacks, rule out the waiting room. The
    // waiting-room text is the most reliable disambiguator — it appears
    // ONLY in the waiting room.
    const inWaitingRoom = await page.evaluate((texts: string[]) => {
      const bodyText = document.body?.innerText || '';
      return texts.some(t => bodyText.includes(t));
    }, zoomWaitingRoomTexts).catch(() => false);
    if (inWaitingRoom) return false;

    // Fallback 1: footer may be auto-hidden — check for the meeting app shell
    const meetingApp = page.locator(zoomMeetingAppSelector).first();
    if (await meetingApp.isVisible({ timeout: 500 })) return true;

    // Fallback 2: live <audio> elements AND not on a pre-join page.
    // Distinguishes "in meeting, audio routing" from "pre-join page with
    // mic preview audio". Pre-join detection is shared via isOnPreJoinPage()
    // so it stays in sync with the post-admission silent check.
    const liveAudioCount = await page.evaluate(() => {
      return Array.from(document.querySelectorAll('audio'))
        .filter((el: any) =>
          !el.paused &&
          el.srcObject instanceof MediaStream &&
          el.srcObject.getAudioTracks().length > 0 &&
          el.srcObject.getAudioTracks()[0].readyState === 'live')
        .length;
    }).catch(() => 0);
    return liveAudioCount > 0 && !(await isOnPreJoinPage(page));
  } catch {
    return false;
  }
}

/**
 * Check if the bot is currently in the waiting room.
 * Zoom waiting room shows specific text strings — no unique CSS class.
 */
async function isInWaitingRoom(page: Page): Promise<boolean> {
  try {
    for (const text of zoomWaitingRoomTexts) {
      const el = page.locator(`text=${text}`).first();
      const visible = await el.isVisible({ timeout: 300 }).catch(() => false);
      if (visible) return true;
    }
    // Also check via JS text scan (more reliable for partial matches)
    return await page.evaluate((texts: string[]) => {
      const bodyText = document.body.innerText || '';
      return texts.some(t => bodyText.includes(t));
    }, zoomWaitingRoomTexts);
  } catch {
    return false;
  }
}

/**
 * Check if the bot was rejected / meeting ended.
 */
async function isRejectedOrEnded(page: Page): Promise<boolean> {
  try {
    return await page.evaluate((texts: string[]) => {
      const bodyText = document.body.innerText || '';
      return texts.some(t => bodyText.includes(t));
    }, zoomRemovalTexts);
  } catch {
    return false;
  }
}

export async function waitForZoomWebAdmission(
  page: Page | null,
  timeoutMs: number,
  botConfig: BotConfig
): Promise<boolean | AdmissionDecision> {
  if (!page) throw new Error('[Zoom Web] Page required for admission check');

  log('[Zoom Web] Checking admission state...');

  // Fast path: already admitted (host was present and let us in immediately).
  // isAdmitted() rules out the waiting room before its weaker fallbacks fire,
  // so a true here means the bot is genuinely in the meeting.
  if (await isAdmitted(page)) {
    log('[Zoom Web] Bot immediately admitted (no waiting room detected)');
    return true;
  }

  // Check if in waiting room
  const inWaiting = await isInWaitingRoom(page);
  if (inWaiting) {
    log('[Zoom Web] Bot is in waiting room — waiting for host admission');
    try {
      await callAwaitingAdmissionCallback(botConfig);
    } catch (e: any) {
      log(`[Zoom Web] Warning: awaiting_admission callback failed: ${e.message}`);
    }
  }

  // Poll loop
  const startTime = Date.now();
  const pollInterval = 2000;
  let unknownStateDuration = 0;
  const effectiveTimeout = () => timeoutMs + getEscalationExtensionMs();

  while (Date.now() - startTime < effectiveTimeout()) {
    await page.waitForTimeout(pollInterval);

    if (await isRejectedOrEnded(page)) {
      log('[Zoom Web] Bot was rejected or meeting ended during admission wait');
      throw new Error('Bot was rejected from the Zoom meeting or meeting ended');
    }

    if (await isAdmitted(page)) {
      log('[Zoom Web] Bot admitted — Leave button now visible');
      return true;
    }

    // Track unknown state (neither admitted, nor waiting room, nor rejected)
    const inWaitingNow = await isInWaitingRoom(page);
    if (!inWaitingNow) {
      unknownStateDuration += pollInterval;
    } else {
      unknownStateDuration = 0;
    }

    // Escalation check
    const elapsedMs = Date.now() - startTime;
    const escalation = checkEscalation(elapsedMs, timeoutMs, unknownStateDuration);
    if (escalation) {
      await triggerEscalation(botConfig, escalation.reason);
    }

    const elapsed = Math.round(elapsedMs / 1000);
    log(`[Zoom Web] Still waiting for admission... ${elapsed}s elapsed`);
  }

  throw new Error(`[Zoom Web] Bot not admitted within ${effectiveTimeout()}ms timeout`);
}

/**
 * True if the page is showing Zoom's pre-join / passcode-entry screen (bot not
 * yet in the meeting). Combines DOM-selector detection with body-text hints —
 * the text hints matter because selectors alone missed a real pre-join screen
 * once (see isAdmitted() history, meeting_id=31). Shared by isAdmitted()'s audio
 * fallback and checkZoomWebAdmissionSilent() so the two can't drift apart.
 * Fails safe to `true` (assume pre-join / NOT admitted) on error.
 */
async function isOnPreJoinPage(page: Page): Promise<boolean> {
  return page.evaluate(() => {
    const domPresent = !!(
      document.querySelector('#input-for-name') ||
      document.querySelector('button.preview-join-button') ||
      document.querySelector('input[placeholder*="passcode" i], input[placeholder*="password" i]')
    );
    const bodyText = (document.body?.innerText || '').toLowerCase();
    const textHint = ['enter meeting info', 'meeting passcode'].some(t => bodyText.includes(t));
    return domPresent || textHint;
  }).catch(() => true);
}

/**
 * POSITIVE-liveness gate: the page is still a live Zoom meeting-client page.
 * Guards the silent check's negative-confirmation path so a torn-down /
 * about:blank / error / network-interstitial page (where the waiting-room /
 * removal / pre-join text checks can all legitimately return false) is never
 * mistaken for "still admitted" (which would silently record nothing). Fails
 * safe to `false` (NOT a live meeting) on error.
 *
 * URL check accepts every legitimate meeting-client URL — `/wc/`, `/wc/{id}/join`
 * AND `/wc-loading/` — because Zoom sits on `/wc-loading/` for 10-15s during the
 * post-admission audio-init handshake; a narrower `/wc/` match wrongly evicted a
 * genuinely-live bot during that window. Excludes, semantically: Chromium's
 * network-error interstitial (`#main-frame-error`, which keeps the target URL)
 * and Zoom's "host hasn't started / invalid link" error screen.
 */
async function hasMeetingLiveness(page: Page): Promise<boolean> {
  if (page.isClosed()) return false;
  if (!page.url().includes('zoom.us/wc')) return false;
  return page.evaluate(
    ({ invalidText, invalidTitle }: { invalidText: string; invalidTitle: string }) => {
      if (document.readyState !== 'complete') return false;
      // Chromium net-error interstitial keeps the target URL but is not a Zoom page.
      if (document.querySelector('#main-frame-error')) return false;
      const bodyText = document.body?.innerText || '';
      if (bodyText.trim().length <= 20) return false;
      // Zoom "Error - Zoom" / "This meeting link is invalid" screen is not a live meeting.
      if (document.title.includes(invalidTitle)) return false;
      if (bodyText.toLowerCase().includes(invalidText.toLowerCase())) return false;
      return true;
    },
    { invalidText: zoomInvalidMeetingText, invalidTitle: zoomInvalidMeetingTitle }
  ).catch(() => false);
}

/**
 * Post-admission "is the bot STILL in the meeting?" check, used by the shared
 * meeting flow to catch genuine false positives (bot bounced back to pre-join /
 * waiting room / was removed) right after a confirmed admission.
 *
 * Two Zoom-Web races made the old naive re-check wrongly EVICT a genuinely
 * admitted bot (observed live 2026-08-21 on the waiting-room admit path — the
 * bot was admitted, then self-declared `admission_false_positive` and left
 * without recording):
 *   1. The in-meeting footer auto-hides within seconds, so the Leave button that
 *      isAdmitted() keys off vanishes.
 *   2. Audio-join (prepare) runs AFTER this check, so the live-<audio> fallback
 *      is always 0 here.
 * So: reveal the footer first; and if no positive control is visible yet, accept
 * "still admitted" ONLY when a coarse positive-liveness signal holds AND none of
 * the negative states (waiting room / removed / pre-join) do. The liveness gate
 * (hasMeetingLiveness) is what makes this safe — without it, a blank/errored/
 * torn-down page would slip through all three negative checks and be misread as
 * admitted, silently recording nothing for the whole meeting. A real bounce
 * still returns false. Bounded to 3 attempts so a genuine eviction is still
 * detected within a few seconds.
 */
export async function checkZoomWebAdmissionSilent(page: Page | null): Promise<boolean> {
  if (!page) return false;
  for (let attempt = 0; attempt < 3; attempt++) {
    await revealZoomFooter(page);
    if (await isAdmitted(page)) return true;

    const [waiting, gone, preJoin, live] = await Promise.all([
      isInWaitingRoom(page).catch(() => false),
      isRejectedOrEnded(page).catch(() => false),
      isOnPreJoinPage(page),
      hasMeetingLiveness(page).catch(() => false),
    ]);
    if (live && !waiting && !gone && !preJoin) {
      log('[Zoom Web] Silent admission check: controls momentarily hidden but page is a live meeting (not waiting room / removed / pre-join) — treating as still admitted');
      return true;
    }
    if (attempt < 2) {
      await page.waitForTimeout(1500);
    }
  }
  return false;
}
