import { Page } from 'playwright';
import { BotConfig } from '../../../types';
import { log, callJoiningCallback } from '../../../utils';
import { installOutboundAudioLockInPage, isZoomAudioSealEnabled, isZoomAudioTouchAllowed, readZoomMicState, makeZoomMicProbe } from './prepare';
import {
  zoomNameInputSelector,
  zoomJoinButtonSelector,
  zoomPreviewMuteSelector,
  zoomPreviewVideoSelector,
  zoomPermissionDismissSelector,
  zoomMeetingAppSelector,
} from './selectors';

/**
 * Build the Zoom Web Client URL from a meeting invite URL.
 * Input:  https://us05web.zoom.us/j/84335626851?pwd=...
 * Output: https://app.zoom.us/wc/84335626851/join?pwd=...
 *
 * For Zoom Events URLs (events.zoom.us/ejl/...) the URL is returned as-is
 * because the events page handles its own redirect to the web client.
 */
export function buildZoomWebClientUrl(meetingUrl: string): string {
  try {
    const url = new URL(meetingUrl);

    // Zoom Events URLs — return as-is; the events page redirects to the web client
    if (url.hostname === 'events.zoom.us') {
      return meetingUrl;
    }

    // Already a web client URL — return as-is
    if (meetingUrl.includes('/wc/')) return meetingUrl;

    // Extract meeting ID from path: /j/84335626851
    const pathMatch = url.pathname.match(/\/j\/(\d+)/);
    const meetingId = pathMatch?.[1];
    if (!meetingId) {
      throw new Error(`Cannot extract meeting ID from Zoom URL: ${meetingUrl}`);
    }

    const pwd = url.searchParams.get('pwd') || '';
    const wcUrl = new URL(`https://app.zoom.us/wc/${meetingId}/join`);
    if (pwd) wcUrl.searchParams.set('pwd', pwd);

    return wcUrl.toString();
  } catch (err: any) {
    // If already a web client URL or unrecognised format, return as-is
    if (meetingUrl.includes('/wc/')) return meetingUrl;
    throw new Error(`Invalid Zoom meeting URL: ${meetingUrl} — ${err.message}`);
  }
}

const HOST_NOT_STARTED_RETRY_INTERVAL_MS = 15000;
const HOST_NOT_STARTED_MAX_WAIT_MS = 10 * 60 * 1000; // 10 minutes

export async function joinZoomWebMeeting(page: Page | null, botConfig: BotConfig): Promise<void> {
  if (!page) throw new Error('[Zoom Web] Page is required for web-based Zoom join');

  const rawUrl = botConfig.meetingUrl!;
  const webClientUrl = buildZoomWebClientUrl(rawUrl);
  log(`[Zoom Web] Navigating to web client: ${webClientUrl}`);

  // Arm the outbound-audio lock BEFORE the first navigation, so the patches are
  // in place before any Zoom script runs.
  //
  // WHY HERE and not at afterAdmission: the bot joins audio during preview/join,
  // i.e. BEFORE admission, so by the time afterAdmission fires the real mic track
  // ALREADY EXISTS. An existing track can only be found by enumerating peer
  // connections, and the registry that made that possible
  // (`__vexa_peer_connections`) is written only by Meet's virtual-camera init
  // script, which is installed solely when `cameraEnabled` is true — false for a
  // recorder bot. Net effect of the old placement: in the default configuration
  // the bot's actual microphone track was never sealed at all.
  //
  // Installing at page load dissolves that: no outbound audio track can pre-exist
  // the patches, so the mic track is caught at birth by the getUserMedia patch —
  // the only way a real microphone track can come into being — regardless of any
  // registry. The afterAdmission install remains, as a re-assert.
  //
  // Zoom-only by construction: this runs from platforms/zoom/web/, so Meet and
  // Teams init paths are untouched.
  if (botConfig.voiceAgentEnabled) {
    log('[Zoom Web] Outbound audio lock NOT armed at page load — voice agent must transmit TTS');
  } else if (!isZoomAudioTouchAllowed()) {
    // Hands-off mode installs NOTHING. Gated here rather than inside the in-page
    // function so the init script is never even registered — "installs nothing"
    // has to mean nothing runs, not that something runs and returns early.
    log('[Zoom Web] Outbound audio lock NOT armed at page load — ZOOM_AUDIO_LOCK hands-off mode; no patch is installed and no track is touched. The bot\'s silence rests entirely on the PulseAudio source mute from start.sh');
  } else {
    try {
      await page.addInitScript(installOutboundAudioLockInPage, {
        voiceAgentEnabled: false,
        sealEnabled: isZoomAudioSealEnabled(),
      });
      log('[Zoom Web] Outbound audio lock armed at page load (before any Zoom script runs)');
    } catch (e: any) {
      log(`[Zoom Web] WARNING: could not arm the page-load audio lock (${e?.message ?? e}) — falling back to the post-admission install, which cannot seal a pre-existing mic track`);
    }
  }

  // Retry loop: if host hasn't started the meeting yet, page title = "Error - Zoom"
  // and body contains "This meeting link is invalid". Poll until the pre-join page appears.
  const startTime = Date.now();
  while (true) {
    await page.goto(webClientUrl, { waitUntil: 'domcontentloaded', timeout: 60000 });
    await page.waitForTimeout(2000);

    const title = await page.title();
    const isError = title === 'Error - Zoom' || title === 'error - Zoom';

    // Auth-required gate: meetings with "Only authenticated users can join"
    // enabled show a sign-in page where #input-for-name never renders, and
    // the bot would otherwise wait the full name-input timeout (5 min) for
    // a field that never appears. Detect early and fail fast with a
    // structured reason so the meeting-api receives auth_required, not a
    // generic timeout.
    const authRequired = await page.evaluate(() => {
      const body = (document.body?.innerText || '').toLowerCase();
      const signInIndicators = [
        'sign in to join this meeting',
        'sign in to join',
        'authentication is required',
        'only authenticated users can join',
        'this meeting requires authentication',
      ];
      return signInIndicators.some(s => body.includes(s));
    }).catch(() => false);
    if (authRequired) {
      log('[Zoom Web] Sign-in page detected — meeting requires authenticated users');
      throw new Error('[Zoom Web] auth_required: meeting host has restricted entry to authenticated Zoom users; bot cannot join without a Zoom account session');
    }

    if (!isError) break; // Pre-join page loaded

    const elapsed = Date.now() - startTime;
    if (elapsed >= HOST_NOT_STARTED_MAX_WAIT_MS) {
      throw new Error('[Zoom Web] Host did not start the meeting within the wait timeout');
    }
    log(`[Zoom Web] Host not started yet (title="${title}"). Retrying in ${HOST_NOT_STARTED_RETRY_INTERVAL_MS / 1000}s...`);
    await page.waitForTimeout(HOST_NOT_STARTED_RETRY_INTERVAL_MS);
  }

  // Notify meeting-api: joining
  // Fix 2: Propagate JOINING callback failure — bot must NOT proceed if server rejected
  await callJoiningCallback(botConfig);

  // Handle the "Use microphone and camera" permission dialog(s).
  // Zoom shows this dialog up to twice (camera+mic, then mic-only).
  // ALL bots must click "Allow" to join the audio channel — without it, Zoom
  // never creates <audio> elements for other participants and the per-speaker
  // capture pipeline gets no audio data. Recorder bots mute their mic in preview
  // (below) so they don't transmit, but they still need to join audio to RECEIVE.
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      // Click "Allow" to grant audio permission (needed to receive meeting audio)
      const allowBtn = page.locator('button:has-text("Allow")').first();
      const allowVisible = await allowBtn.isVisible({ timeout: 4000 });
      if (allowVisible) {
        await allowBtn.click();
        log(`[Zoom Web] Granted audio permission (attempt ${attempt + 1})`);
        await page.waitForTimeout(600);
        continue;
      }
      // Fallback: if "Allow" not found, check for dismiss button — but log a warning
      // since skipping audio permission means no audio capture
      const dismissBtn = page.locator(zoomPermissionDismissSelector).first();
      const visible = await dismissBtn.isVisible({ timeout: 1000 });
      if (visible) {
        log(`[Zoom Web] WARNING: No "Allow" button found, falling back to dismiss — audio capture may not work (attempt ${attempt + 1})`);
        await dismissBtn.click();
        await page.waitForTimeout(600);
      } else {
        break;
      }
    } catch {
      break;
    }
  }

  // Wait for the pre-join name input to appear
  log('[Zoom Web] Waiting for pre-join name input...');
  await page.waitForSelector(zoomNameInputSelector, { timeout: 30000 });

  // Some meetings show a passcode-entry pre-join page that includes a
  // passcode input ABOVE the name input. If a passcode field is visible
  // and we have a passcode in botConfig, fill it. If a passcode field is
  // visible but we have NO passcode, fail fast with a structured reason —
  // the join button stays disabled forever and the bot would otherwise
  // sit on the pre-join page indefinitely.
  const passcodeInputSelector = 'input[placeholder*="passcode" i], input[placeholder*="password" i], input[type="password"]';
  const hasPasscodeField = await page.locator(passcodeInputSelector).first().isVisible({ timeout: 1000 }).catch(() => false);
  if (hasPasscodeField) {
    const passcode = (botConfig as any).passcode || '';
    if (passcode) {
      await page.locator(passcodeInputSelector).first().fill(passcode);
      log(`[Zoom Web] Filled passcode field`);
    } else {
      throw new Error('[Zoom Web] passcode_required: meeting requires a passcode but botConfig.passcode is empty; pass passcode in the POST /bots payload or include ?pwd=... in the meeting_url');
    }
  }

  // Fill name using REAL keyboard events.
  //
  // Earlier versions used a "React-compatible native setter" trick that
  // synthetically dispatched input/change events. On the current Zoom Web
  // UI version (observed 2026-04-26 in meeting_id=29), that doesn't fully
  // satisfy Zoom's React form validation — the Join button stays disabled
  // (class="zm-btn preview-join-button disabled ..."), and Playwright's
  // 30s click retry loop times out with the failure mode:
  //   "<div class="preview-meeting-info">…</div> intercepts pointer events".
  //
  // Real keyboard events (focus + type) trigger Zoom's full input pipeline
  // including the validation that enables the Join button.
  await page.locator(zoomNameInputSelector).first().click({ timeout: 5000 }).catch(() => {});
  await page.locator(zoomNameInputSelector).first().fill('');
  await page.keyboard.type(botConfig.botName, { delay: 30 });
  log(`[Zoom Web] Name typed: "${botConfig.botName}"`);

  // Wait for Zoom's React state to enable the Join button (or proceed if
  // it never enables — the click attempt below will surface the issue).
  await page.waitForFunction(
    (sel: string) => {
      const btn = document.querySelector(sel) as HTMLButtonElement | null;
      return !!btn && !btn.classList.contains('disabled') && !btn.disabled;
    },
    zoomJoinButtonSelector,
    { timeout: 8000 },
  ).catch(() => log('[Zoom Web] WARNING: Join button still disabled after typing name; will attempt click anyway'));

  // Ensure mic is muted in preview for recorder bots (they only need to receive audio).
  // Voice agent bots keep mic unmuted so Zoom grants audio access for TTS output.
  // PulseAudio starts muted (entrypoint.sh), so no audio leaks before TTS.
  const isVoiceAgent = !!botConfig.voiceAgentEnabled;
  if (!isVoiceAgent) {
    try {
      const muteBtn = page.locator(zoomPreviewMuteSelector);
      // Built through the factory so every field this preview read cannot
      // supply defaults to "absent" rather than to a value that could assert a
      // state. `id` IS supplied: zoomPreviewMuteSelector is an id selector, so
      // it is the element identity the reader would otherwise have to guess.
      const probe = makeZoomMicProbe({
        ariaLabel: await muteBtn.getAttribute('aria-label').catch(() => null),
        ariaPressed: await muteBtn.getAttribute('aria-pressed').catch(() => null),
        className: await muteBtn.getAttribute('class').catch(() => null),
        id: 'preview-audio-control-button',
        elementKey: '#preview-audio-control-button',
      });
      const reading = readZoomMicState(probe);

      // WHY this is no longer `ariaLabel === 'Mute'`:
      //
      // Two independent problems with that test. First, it is the same brittle
      // exact-match that shipped the in-meeting bug — the live DOM there returned
      // aria-label="audio", matching neither spelling. Second, and new: the
      // outbound-audio seal is now armed PRE-NAVIGATION, so it covers this preview
      // page and the mic track is already `enabled = false` when this runs. If
      // Zoom renders this label from the track's `enabled` state, the label reads
      // "Unmute" (already muted), the click is skipped, and Zoom's own mute state
      // is never set — leaving the bot silent but MORE likely to APPEAR unmuted,
      // which is the user's original complaint.
      //
      // What is NOT done here, and why: we do not click when the control reads
      // muted. Clicking a control that is already muted UNMUTES it — the exact
      // toggle hazard the in-meeting one-shot latch exists to prevent — and it
      // would turn a cosmetic uncertainty into a real unmute. Nor is the seal
      // deferred until after this step: that would let the mic track pre-exist the
      // patches and reopen the defect the pre-navigation arming fixed.
      //
      // So the reading is logged either way. A live run resolves whether Zoom's
      // preview label tracks `enabled` (reads muted before we ever click) or its
      // own internal state (reads unmuted, and we click as before). If it is the
      // former, the corrective is post-admission: ensureZoomMutedInMeeting and the
      // persistent mute watcher both re-read the real control and re-mute on a
      // confident unmuted reading. Those cosmetic layers are LOAD-BEARING for the
      // visual half now, not nice-to-have.
      if (reading.kind === 'unmuted') {
        await muteBtn.click();
        log(`[Zoom Web] Muted microphone in preview (recorder bot — receive-only audio) [${reading.evidence}]`);
      } else if (reading.kind === 'muted') {
        log(`[Zoom Web] Preview mic already reads muted — NOT clicking (a click here would unmute) [${reading.evidence}]. If the seal is armed this may be the track state rather than Zoom's own; the in-meeting mute watcher is the corrective.`);
      } else {
        log(`[Zoom Web] Preview mic state unreadable (${reading.kind}) — NOT clicking [${reading.evidence}]; relying on the in-meeting mute path`);
      }
    } catch {
      log('[Zoom Web] Could not read/toggle preview mic — relying on the in-meeting mute path');
    }
  } else {
    log('[Zoom Web] Voice agent: keeping mic enabled in preview for TTS');
  }

  try {
    const videoBtn = page.locator(zoomPreviewVideoSelector);
    const videoAriaLabel = await videoBtn.getAttribute('aria-label');
    // "Stop Video" means video is on → click to stop. "Start Video" means already off → skip.
    if (videoAriaLabel === 'Stop Video') {
      await videoBtn.click();
      log('[Zoom Web] Stopped video in preview');
    }
  } catch {
    log('[Zoom Web] Could not toggle preview video (may already be off)');
  }

  // Click Join via DOM, bypassing Playwright's pointer-event interception
  // checks. Zoom's preview screen sometimes overlays a `.preview-meeting-info`
  // div on top of the Join button — Playwright's `.click()` waits for the
  // element to become hit-testable (no overlapping z-index intercepting
  // pointer events) and times out after 30s. Calling `.click()` programmatically
  // via the DOM bypasses that hit-test entirely; the underlying React handler
  // fires regardless of overlapping elements.
  log('[Zoom Web] Clicking Join (DOM-direct)...');
  const clicked = await page.evaluate((sel: string) => {
    const btn = document.querySelector(sel) as HTMLButtonElement | null;
    if (!btn) return false;
    if (btn.classList.contains('disabled') || btn.disabled) return false;
    btn.click();
    return true;
  }, zoomJoinButtonSelector);
  if (!clicked) {
    log('[Zoom Web] WARNING: Join button not clickable via DOM (still disabled?); falling back to Playwright click...');
    const joinBtn = page.locator(zoomJoinButtonSelector);
    await joinBtn.waitFor({ state: 'visible', timeout: 10000 });
    await joinBtn.click({ force: true, timeout: 10000 });
  }
  log('[Zoom Web] Join clicked — waiting for meeting to load...');

  // Wait a moment for page transition
  await page.waitForTimeout(3000);
}
