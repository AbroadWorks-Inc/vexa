import { Page, Locator } from 'playwright';
import { BotConfig } from '../../../types';
import { log } from '../../../utils';
import {
  zoomAudioButtonSelector,
  zoomChatButtonSelector,
  zoomVideoButtonSelector,
  zoomViewButtonSelectors,
  zoomMoreButtonSelector,
  zoomViewMenuScopeSelector,
  zoomSpeakerViewOptionSelectors,
  zoomSpeakerViewExactTexts,
  zoomSpeakerViewIconSelector,
} from './selectors';

/**
 * Post-admission setup: join computer audio, dismiss any popups, verify audio.
 */
export async function prepareZoomWebMeeting(page: Page | null, botConfig: BotConfig): Promise<void> {
  if (!page) throw new Error('[Zoom Web] Page required for prepare');

  log('[Zoom Web] Preparing meeting post-admission...');

  // Dismiss popups that overlay the meeting content
  await dismissZoomPopups(page);

  // Join computer audio — retry up to 3 times with escalating strategies.
  // (Was 8 attempts, but on current Zoom Web UI versions audio auto-joins
  // after admission so the loop most often runs through with no button to
  // click and burns ~40s before continuing — visible to the user as
  // "joining" status while the bot is actually already in the meeting.)
  // CRITICAL invariant: without joining audio, no <audio> elements are
  // created and the per-speaker capture pipeline gets zero audio data.
  let audioJoined = false;
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      // Early-exit: if Zoom auto-joined audio, <audio> elements with live
      // MediaStreams already exist. Skip the click loop entirely in that case.
      const liveAudioCount = await page.evaluate(() => {
        return Array.from(document.querySelectorAll('audio'))
          .filter((el: any) =>
            !el.paused &&
            el.srcObject instanceof MediaStream &&
            el.srcObject.getAudioTracks().length > 0 &&
            el.srcObject.getAudioTracks()[0].readyState === 'live')
          .length;
      }).catch(() => 0);
      if (liveAudioCount > 0) {
        log(`[Zoom Web] Audio already flowing (${liveAudioCount} live <audio> elements); skipping join-button retry`);
        audioJoined = true;
        break;
      }

      // Zoom's footer toolbar (which holds the Join Audio control) auto-hides
      // after a few seconds idle; nudge the pointer so the control is present
      // and clickable before we look for it.
      await revealZoomFooter(page);

      // First: check if a "Join with Computer Audio" dialog is already open (ReactModal).
      // This MUST come before clicking the footer button, because the modal blocks footer clicks.
      const computerAudioBtn = page.locator([
        'button:has-text("Join with Computer Audio")',
        'button:has-text("Join Audio by Computer")',
        'button:has-text("Computer Audio")',
      ].join(', ')).first();
      try {
        if (await computerAudioBtn.isVisible({ timeout: 1500 })) {
          await computerAudioBtn.click();
          log('[Zoom Web] Clicked "Join with Computer Audio" dialog button');
          audioJoined = true;
          break;
        }
      } catch { /* dialog not open */ }

      // Check if audio is already joined (Mute/Unmute button visible)
      const audioBtn = page.locator(zoomAudioButtonSelector).first();
      const visible = await audioBtn.isVisible({ timeout: 2000 });
      if (visible) {
        const ariaLabel = await audioBtn.getAttribute('aria-label');
        log(`[Zoom Web] Audio button aria-label: "${ariaLabel}" (attempt ${attempt + 1})`);

        // If aria-label is "Mute" or "Unmute", audio is already joined
        if (ariaLabel && (ariaLabel === 'Mute' || ariaLabel === 'Unmute')) {
          log('[Zoom Web] Audio already joined (mic toggle visible)');
          audioJoined = true;
          break;
        }

        // If aria-label contains "join audio" or is just "audio", click to open dialog
        if (ariaLabel && (ariaLabel.toLowerCase().includes('join audio') || ariaLabel.toLowerCase() === 'audio')) {
          await audioBtn.click({ timeout: 5000 });
          log('[Zoom Web] Clicked Join Audio footer button — waiting for dialog...');
          await page.waitForTimeout(1500);

          // Immediately check for dialog that just opened
          try {
            if (await computerAudioBtn.isVisible({ timeout: 3000 })) {
              await computerAudioBtn.click();
              log('[Zoom Web] Clicked "Join with Computer Audio" in dialog');
              audioJoined = true;
              break;
            }
          } catch { /* dialog didn't appear — will retry */ }
          continue;
        }
      }

      // Try the floating "Join Audio" banner (appears on some Zoom versions)
      const joinAudioBanner = page.locator('button:has-text("Join Audio")').first();
      const bannerVisible = await joinAudioBanner.isVisible({ timeout: 1000 }).catch(() => false);
      if (bannerVisible) {
        await joinAudioBanner.click();
        log(`[Zoom Web] Clicked "Join Audio" banner (attempt ${attempt + 1})`);
        await page.waitForTimeout(1500);

        // Check for dialog
        try {
          if (await computerAudioBtn.isVisible({ timeout: 3000 })) {
            await computerAudioBtn.click();
            log('[Zoom Web] Clicked "Join with Computer Audio" after banner');
            audioJoined = true;
            break;
          }
        } catch { /* no dialog */ }
        continue;
      }

      // Nothing found yet — wait and retry
      log(`[Zoom Web] No audio controls visible (attempt ${attempt + 1}), waiting...`);
      await page.waitForTimeout(2000);
    } catch (e: any) {
      log(`[Zoom Web] Audio join attempt ${attempt + 1} failed: ${e.message}`);
    }
  }

  // Final verification: check if Mute/Unmute appeared after all attempts
  if (!audioJoined) {
    try {
      const finalCheck = page.locator(zoomAudioButtonSelector).first();
      const finalLabel = await finalCheck.getAttribute('aria-label').catch(() => null);
      if (finalLabel === 'Mute' || finalLabel === 'Unmute') {
        log('[Zoom Web] Audio joined (confirmed on final check)');
        audioJoined = true;
      }
    } catch { /* ignore */ }
  }

  if (!audioJoined) {
    log('[Zoom Web] WARNING: Could not confirm audio join after all attempts — per-speaker capture may fail');
  }

  // Dismiss the "Please enable microphone/camera" notification banner if present
  try {
    const closeNotif = page.locator('button[aria-label="Close notification"], .notification-close, button:has-text("×")').first();
    if (await closeNotif.isVisible({ timeout: 1000 })) {
      await closeNotif.click();
    }
  } catch { /* no banner */ }

  // Belt-and-braces video-off after admission. join.ts already toggles the
  // pre-join preview button when it says "Stop Video", but Zoom's meeting-side
  // video state can re-enable independently of preview (observed on some
  // accounts where the preview toggle didn't carry over). Match gmeet/teams
  // behaviour: bot defaults to camera off — only opt back in when an
  // operator explicitly asks for video capture downstream.
  // Only act when aria-label === "Stop Video" (= currently broadcasting);
  // "Start Video" is already-off and would be a no-op.
  try {
    const inMeetingVideoBtn = page.locator(zoomVideoButtonSelector).first();
    if (await inMeetingVideoBtn.isVisible({ timeout: 2000 })) {
      const label = await inMeetingVideoBtn.getAttribute('aria-label');
      if (label === 'Stop Video') {
        await inMeetingVideoBtn.click();
        log('[Zoom Web] Video disabled post-admission (was on, toggled off)');
      } else {
        log(`[Zoom Web] Video already off post-admission (aria-label="${label}")`);
      }
    }
  } catch (e: any) {
    log(`[Zoom Web] Could not verify video-off post-admission: ${e.message}`);
  }

  // Incoming-video block runs at the RTCPeerConnection layer (shared
  // services/screen-content.ts → getVideoBlockInitScript). That script
  // also sets transceiver.direction so the decoder actually stops —
  // not just `track.enabled=false` which only blackens <video> output
  // while the decoder keeps pumping frames into Zoom's canvas paint.

  // NOTE: forcing Speaker View is NOT done here. prepare() runs in a Promise.all
  // race with waitForAdmission() (meetingFlow.ts), so during a real waiting-room
  // hold this would fire pre-admission — before the meeting toolbar exists — and
  // never retry. switchToZoomSpeakerView() is invoked from the zoom-web
  // `afterAdmission` strategy hook instead (post-admission, still before video
  // recording starts, so the cursor move stays out of the recording).

  // Verify audio elements exist after joining (delayed check — elements may take time to appear)
  await verifyAudioElements(page);

  log('[Zoom Web] Meeting preparation complete');
}

/**
 * Check for <audio>/<video> elements with MediaStream srcObject.
 * Logs what was found for diagnostic purposes — does NOT block.
 */
async function verifyAudioElements(page: Page): Promise<void> {
  try {
    await page.waitForTimeout(3000); // Give Zoom time to create media elements

    const audioInfo = await page.evaluate(() => {
      const elements = Array.from(document.querySelectorAll('audio, video'));
      const withStreams = elements.filter((el: any) =>
        el.srcObject instanceof MediaStream &&
        el.srcObject.getAudioTracks().length > 0
      );
      return {
        totalElements: elements.length,
        withAudioStreams: withStreams.length,
        details: withStreams.map((el: any, i: number) => {
          const stream: MediaStream = el.srcObject;
          const tracks = stream.getAudioTracks();
          return {
            index: i,
            tag: el.tagName.toLowerCase(),
            paused: el.paused,
            trackCount: tracks.length,
            trackStates: tracks.map(t => ({ enabled: t.enabled, muted: t.muted, readyState: t.readyState })),
          };
        }),
      };
    });

    log(`[Zoom Web] Audio verification: ${audioInfo.withAudioStreams} elements with audio streams (${audioInfo.totalElements} total media elements)`);
    if (audioInfo.withAudioStreams > 0) {
      for (const d of audioInfo.details) {
        log(`[Zoom Web]   Element ${d.index} <${d.tag}>: paused=${d.paused}, tracks=${d.trackCount}, states=${JSON.stringify(d.trackStates)}`);
      }
    } else {
      log('[Zoom Web] WARNING: No audio elements found — bot may not have joined audio channel');
    }
  } catch (e: any) {
    log(`[Zoom Web] Audio verification failed: ${e.message}`);
  }
}

/**
 * Zoom's in-meeting footer toolbar (Leave, Mute / Join Audio, etc.) auto-hides
 * after a few seconds without pointer movement, hiding the very controls the bot
 * needs to find. Nudge the pointer so the footer re-renders before a lookup.
 * Best-effort — never throws.
 *
 * WARNING: this moves the VISIBLE mouse cursor. Video recording is a literal
 * screen capture of the page, so do NOT call this after
 * startVideoRecordingIfNeeded() — the cursor would show up in the recording.
 * Both current callers (audio-join in prepareZoomWebMeeting, and
 * checkZoomWebAdmissionSilent) run strictly before recording starts.
 */
export async function revealZoomFooter(page: Page): Promise<void> {
  try {
    const vp = page.viewportSize() || { width: 1280, height: 720 };
    await page.mouse.move(Math.round(vp.width / 2), Math.round(vp.height / 2));
    await page.mouse.move(Math.round(vp.width / 2), vp.height - 4);
    await page.waitForTimeout(250);
  } catch {
    /* best-effort */
  }
}

/**
 * Surface Zoom's auto-hiding controls before probing for them — both the footer
 * toolbar (bottom) AND the top "View" widget (a full-screen overlay control that
 * hides without pointer activity). Same cursor-move caveat as revealZoomFooter:
 * callers must be pre-recording OR audio-only capture (the Zoom notetaker is
 * audio-only, so it's safe even mid-recording). Best-effort — never throws.
 */
async function revealZoomControls(page: Page): Promise<void> {
  try {
    const vp = page.viewportSize() || { width: 1280, height: 720 };
    const cx = Math.round(vp.width / 2);
    await page.mouse.move(cx, Math.round(vp.height / 2)); // center — wake overlays
    await page.mouse.move(vp.width - 8, 8);               // top-right — View widget
    await page.mouse.move(cx, vp.height - 4);             // bottom — footer
    await page.waitForTimeout(250);
  } catch {
    /* best-effort */
  }
}

/**
 * Ensure the bot is MUTED once it is in the meeting.
 *
 * WHY: the pre-join preview mute (join.ts) does NOT reliably persist into the
 * meeting — Zoom re-enables the mic when audio is joined, so a recorder bot can
 * end up transmitting live audio (user-observed 2026-08-25: bot joined unmuted).
 * The reliable place to mute is the IN-MEETING mic toggle after admission
 * (mirrors how meet-bot mutes post-join). Retries because the footer auto-hides
 * and the audio button can appear a beat after join, and to re-mute if Zoom
 * auto-unmutes at audio-join. The in-meeting audio button's aria-label is
 * "Mute" when currently UNMUTED (click to mute) and "Unmute" when already muted.
 *
 * Audio-only capture, so the cursor moves from revealZoomFooter are invisible in
 * output. Best-effort — never throws. Recorder bots only; voice-agent bots must
 * transmit TTS and are NOT muted (gated by the caller).
 */
export async function ensureZoomMutedInMeeting(page: Page): Promise<void> {
  // One-shot latch: click the toggle AT MOST ONCE. If a click registers but the
  // aria-label lags (broken/slow DOM), clicking again on a later pass could apply
  // an EVEN number of toggles and leave the bot UNMUTED — the opposite of intent.
  // After clicking once we only re-read, letting a lagging label flip to "Unmute".
  let clickedMute = false;
  for (let attempt = 0; attempt < 6; attempt++) {
    try {
      await revealZoomFooter(page);
      const audioBtn = page.locator(zoomAudioButtonSelector).first();
      const visible = await audioBtn.isVisible({ timeout: 2000 }).catch(() => false);
      if (visible) {
        const label = await audioBtn.getAttribute('aria-label').catch(() => null);
        if (label === 'Unmute') {
          // "Unmute" == currently muted → done.
          log(
            clickedMute
              ? '[Zoom Web] Muted microphone in meeting (recorder bot — receive-only)'
              : attempt === 0
                ? '[Zoom Web] Mic already muted in meeting'
                : '[Zoom Web] Mic muted in meeting (confirmed)',
          );
          return;
        }
        if (label === 'Mute') {
          // "Mute" == currently unmuted → click once to mute.
          if (clickedMute) {
            log('[Zoom Web] Mute clicked but aria-label still "Mute" — not re-clicking (avoids toggling back); re-checking');
          } else {
            await audioBtn.click({ timeout: 3000 });
            clickedMute = true;
            await page.waitForTimeout(500);
            const after = await audioBtn.getAttribute('aria-label').catch(() => null);
            if (after === 'Unmute') {
              log('[Zoom Web] Muted microphone in meeting (recorder bot — receive-only)');
              return;
            }
            log(`[Zoom Web] Mute click not yet confirmed (aria-label now "${after}") — re-checking`);
          }
        } else {
          // Audio not joined yet (e.g. "Join Audio"); prepare() handles joining.
          log(`[Zoom Web] Mic toggle not mute-ready (aria-label "${label}") — retry ${attempt + 1}/6`);
        }
      }
    } catch (e: any) {
      log(`[Zoom Web] ensureZoomMutedInMeeting attempt ${attempt + 1} error: ${e?.message ?? e}`);
    }
    await page.waitForTimeout(5000);
  }
  log('[Zoom Web] WARNING: could not confirm in-meeting mute after retries — bot may be transmitting; verify zoomAudioButtonSelector against live DOM');
}

/**
 * Probe selector candidates in order and return the first one whose locator
 * becomes visible within its share of `timeoutMs`. Returns null if none do —
 * callers treat that as "control not found" rather than an error.
 */
async function findFirstVisibleLocator(
  page: Page,
  selectors: string[],
  timeoutMs: number,
): Promise<Locator | null> {
  const perSelectorTimeout = Math.max(150, Math.floor(timeoutMs / Math.max(1, selectors.length)));
  for (const selector of selectors) {
    try {
      const locator = page.locator(selector).first();
      // waitFor() actually polls until visible; Locator.isVisible({timeout}) is a
      // deprecated no-wait (returns immediately), so a candidate that renders a
      // beat later would be missed. Throwing on timeout = "not this one, try next".
      await locator.waitFor({ state: 'visible', timeout: perSelectorTimeout });
      return locator;
    } catch {
      /* not visible within budget — try next candidate */
    }
  }
  return null;
}

/**
 * Force Zoom Web into Speaker View so the active-speaker DOM containers
 * (`.speaker-active-container__video-frame` / `.speaker-bar-container__video-frame--active`)
 * populate. Zoom Web defaults to Gallery View, where neither renders — which is
 * why `startSpeakerPolling`'s Layout 1/2 detection found nothing on the bot
 * (confirmed live 2026-08-24: the same DOM shows those containers only in
 * Speaker View, and the names resolve from `.video-avatar__avatar-footer span`).
 *
 * RETRIES for ~25s. The View control renders a few seconds after admission (and
 * auto-hides), so the previous single attempt missed it. Each pass reveals the
 * controls, clicks the View button, then clicks the Speaker-View option —
 * anchored on the unambiguous `svg.SvgSpeakerView` icon (safe page-wide, unlike
 * the bare word "Speaker" which also appears in Zoom's audio-device menu), with
 * the menu-scoped text candidates as fallback. Exits as soon as Speaker View is
 * confirmed. NEVER throws.
 *
 * Intended to be fired NON-blocking from the afterAdmission hook so it never
 * delays audio recording; cursor moves are safe for the Zoom notetaker
 * (audio-only capture — no screen video recording).
 */
export async function switchToZoomSpeakerView(page: Page): Promise<void> {
  const deadline = Date.now() + 25_000;
  let attempt = 0;
  let warnedNoButton = false;
  while (Date.now() < deadline) {
    attempt++;
    try {
      if (page.isClosed()) return;

      // Success = we are in Speaker View (the active-speaker frame exists).
      const inSpeakerView = await page
        .evaluate(() => !!document.querySelector('.speaker-active-container__video-frame'))
        .catch(() => false);
      if (inSpeakerView) {
        if (attempt > 1) log('[Zoom Web] Speaker View active');
        return;
      }

      await revealZoomControls(page);

      // 1) Click the View/Layout control (or open the overflow "More" menu first).
      let viewButton = await findFirstVisibleLocator(page, zoomViewButtonSelectors, 1200);
      if (!viewButton) {
        const moreButton = await findFirstVisibleLocator(page, [zoomMoreButtonSelector], 400);
        if (moreButton) {
          await moreButton.click().catch(() => {});
          await page.waitForTimeout(300);
          viewButton = await findFirstVisibleLocator(page, zoomViewButtonSelectors, 1200);
        }
      }
      if (!viewButton) {
        if (!warnedNoButton) {
          log('[Zoom Web] View control not visible yet — retrying…');
          warnedNoButton = true;
        }
        await page.waitForTimeout(2000);
        continue;
      }
      await viewButton.click().catch(() => {});
      await page.waitForTimeout(350);

      // 2) Click the Speaker-View option. Primary: the unambiguous SvgSpeakerView
      // icon (click its nearest clickable ancestor) — safe page-wide.
      let clicked = await page
        .evaluate((iconSel: string) => {
          const icon = document.querySelector(iconSel);
          if (!icon) return false;
          const target =
            (icon.closest(
              '[role="menuitemradio"],[role="menuitem"],li,button,[class*="menu-item"],[class*="dropdown-item"]'
            ) as HTMLElement | null) || (icon.parentElement as HTMLElement | null);
          if (target) {
            target.click();
            return true;
          }
          return false;
        }, zoomSpeakerViewIconSelector)
        .catch(() => false);

      // Fallback: menu-scoped text candidates, then an exact-text scan.
      if (!clicked) {
        const menu = page.locator(zoomViewMenuScopeSelector).first();
        const menuOpen = await menu
          .waitFor({ state: 'visible', timeout: 800 })
          .then(() => true)
          .catch(() => false);
        if (menuOpen) {
          for (const sel of zoomSpeakerViewOptionSelectors) {
            try {
              const o = menu.locator(sel).first();
              await o.waitFor({ state: 'visible', timeout: 500 });
              await o.click();
              clicked = true;
              break;
            } catch {
              /* next candidate */
            }
          }
          if (!clicked) {
            clicked = await page
              .evaluate(
                ({ scopeSelector, exactTexts }: { scopeSelector: string; exactTexts: string[] }) => {
                  const scope = document.querySelector(scopeSelector);
                  if (!scope) return false;
                  const wanted = exactTexts.map((t) => t.toLowerCase());
                  const el = Array.from(scope.querySelectorAll<HTMLElement>('*')).find((e) =>
                    wanted.includes((e.textContent || '').trim().toLowerCase())
                  );
                  if (el) {
                    el.click();
                    return true;
                  }
                  return false;
                },
                { scopeSelector: zoomViewMenuScopeSelector, exactTexts: zoomSpeakerViewExactTexts }
              )
              .catch(() => false);
          }
        }
      }

      if (!clicked) {
        // Close any stray open menu before the next pass.
        await page.keyboard.press('Escape').catch(() => {});
      }
      await page.waitForTimeout(1500); // let the layout switch; loop re-checks inSpeakerView
    } catch (e: any) {
      log(`[Zoom Web] switchToZoomSpeakerView attempt ${attempt} error (non-fatal): ${e?.message || e}`);
      await page.waitForTimeout(1500);
    }
  }
  log('[Zoom Web] Could not confirm Speaker View after retries — speaker names may be limited');
}

/**
 * Dismiss known Zoom Web popups/modals that overlay meeting content.
 * Safe to call repeatedly — each check is short-circuited if the popup isn't visible.
 */
export async function dismissZoomPopups(page: Page): Promise<void> {
  // All checks use timeout:0 — instant visibility check, no waiting.
  // This function is polled every 2s so there's no need to wait for elements to appear.
  const dismissTargets = [
    { selector: '.zm-modal button:has-text("OK")', label: 'AI Companion' },
    { selector: '.relative-tooltip button:has-text("Got it")', label: 'chatting as guest' },
    { selector: '.settings-feature-tips button:has-text("OK")', label: 'feature tip' },
    { selector: '.ReactModal__Content button:has-text("OK")', label: 'modal OK' },
    { selector: '.ReactModal__Content button:has-text("Got it")', label: 'modal Got it' },
    { selector: '[role="presentation"] button:has-text("OK")', label: 'presentation OK' },
    // Zoom advisory modal: "Your mic is muted in system or browser settings."
    // Doesn't block joining/capture but spams logs and remains on screen
    // until manually dismissed. Click any of OK / Dismiss / Got it / Continue.
    { selector: '.zm-modal:has-text("mic is muted") button:has-text("OK"), .zm-modal:has-text("mic is muted") button:has-text("Got it"), .zm-modal:has-text("mic is muted") button:has-text("Dismiss"), .zm-modal:has-text("mic is muted") button:has-text("Continue")', label: 'mic-muted advisory' },
  ];

  for (const { selector, label } of dismissTargets) {
    try {
      const btn = page.locator(selector).first();
      if (await btn.isVisible({ timeout: 0 })) {
        await btn.click();
        log(`[Zoom Web] Dismissed "${label}" popup`);
      }
    } catch { /* not present or already gone */ }
  }
}
