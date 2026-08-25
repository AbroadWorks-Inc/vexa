import { Page } from 'playwright';
import { log } from '../../../utils';
import { BotConfig } from '../../../types';
import { zoomLeaveButtonSelector, zoomMeetingEndedModalSelector, zoomRemovalTexts, zoomParticipantsButtonSelector } from './selectors';
import { AloneTimerState, stepAloneTimer } from './alone-timer';

/**
 * Starts polling for removal/end-of-meeting events.
 * Returns a cleanup function that stops polling.
 */
// Page titles that indicate Zoom redirected away from the meeting (to sign-in or join page)
const zoomPostMeetingTitles = ['Zoom', 'Join a Meeting - Zoom', 'Join Meeting - Zoom'];

// URL patterns that are part of Zoom's normal join/audio-init redirect sequence.
// These should NOT trigger removal — they are transient navigations during the handshake.
const ZOOM_AUDIO_INIT_URL_PATTERNS = [
  /\/wc\/\d+\/join/,    // /wc/{id}/join — pre-join page revisited during audio handshake
  /\/wc\/\d+\/start/,   // /wc/{id}/start — host start page redirect
  /\/wc-loading\//,     // Web client loading screen
  /\/wc\/\d+\/videomeeting/, // Video meeting start redirect
];

function isZoomAudioInitUrl(url: string): boolean {
  return ZOOM_AUDIO_INIT_URL_PATTERNS.some(pattern => pattern.test(url));
}

export function startZoomWebRemovalMonitor(
  page: Page | null,
  onRemoval?: (reasonToken?: string) => void | Promise<void>,
  botConfig?: BotConfig
): () => void {
  if (!page) return () => {};

  let stopped = false;
  let consecutiveLeaveButtonMisses = 0;
  const LEAVE_BUTTON_MISS_THRESHOLD = 3; // Require 3 consecutive misses (9s) before acting
  // A /wc/{id}/join redirect after presence is usually meeting-end, but Zoom can
  // also bounce there for a mid-meeting audio reconnect that recovers in ~10-15s
  // (see GRACE_PERIOD_MS note). Require a longer miss streak (~18s) for that URL
  // shape specifically, so a reconnect can recover before we declare the meeting
  // ended. Unambiguous end states (error/blank/post-meeting title) keep the fast 3.
  const WC_REDIRECT_MISS_THRESHOLD = 6;
  const joinedAtMs = Date.now();
  const GRACE_PERIOD_MS = 20_000; // 20s grace — Zoom audio init can take 10-15s with slow networks
  const POLL_INTERVAL_MS = 3000;

  // Left-alone / empty-room auto-leave. Zoom Web (unlike meet-bot) has no
  // built-in "everyone left" exit, so an admitted bot in a meeting that is never
  // formally Ended lingers until the 4h Job hard-deadline. Mirror meet-bot's two
  // timeouts: noOneJoined (admitted but nobody else ever joined) and everyoneLeft
  // (others were present, then all left). Timers only run once the bot is
  // confirmed in the meeting (Leave button visible) and past the join grace.
  // Real system defaults are asymmetric (docker.ts / meeting-api SYSTEM_DEFAULTS):
  // noOneJoined 10min, everyoneLeft 2min. The `??` only applies if botConfig is
  // absent (index.ts always injects it). Floor at 60s so a mis-set 0/tiny value
  // can't evict a slightly-late participant on the first post-grace poll.
  const everyoneLeftMs = Math.max(60_000, botConfig?.automaticLeave?.everyoneLeftTimeout ?? 120_000);
  const noOneJoinedMs = Math.max(60_000, botConfig?.automaticLeave?.noOneJoinedTimeout ?? 600_000);
  let aloneState: AloneTimerState = { aloneMs: 0, sawOthers: false };
  let warnedUnreadable = false;

  // Once the bot has confirmed it is actually in the meeting (Leave button seen
  // at least once), a later redirect to /wc/{id}/join is NOT the audio-init
  // handshake — it is Zoom bouncing the bot back to the pre-join page because the
  // meeting ENDED. So the audio-init URL exemption must apply ONLY before presence
  // is confirmed; afterwards such a redirect is treated as end-of-meeting (still
  // gated by the consecutive-miss threshold, so a transient reconnect can recover).
  let confirmedPresence = false;

  const triggerRemoval = async (reason: string, signalToken?: string) => {
    if (stopped) return;
    stopped = true;
    const elapsed = ((Date.now() - joinedAtMs) / 1000).toFixed(1);
    log(`[Zoom Web] REMOVAL TRIGGERED (${elapsed}s after join): ${reason}`);
    log(`[Zoom Web] Current URL at removal: ${page.url()}`);
    const title = await page.title().catch(() => '<unknown>');
    log(`[Zoom Web] Current title at removal: "${title}"`);
    // signalToken lets the alone-timer report left_alone_timeout /
    // startup_alone_timeout instead of the generic removed_by_admin.
    onRemoval && await onRemoval(signalToken);
  };

  // Fast path: detect navigation away from the meeting page immediately via framenavigated.
  // Zoom redirects to /wc/{id}/join or zoom.us/signin when the meeting ends without a modal.
  const onNavigated = (frame: any) => {
    if (stopped || frame !== page.mainFrame()) return;
    const url: string = frame.url();
    if (!url || url.startsWith('about:')) return;
    const elapsed = ((Date.now() - joinedAtMs) / 1000).toFixed(1);

    // Grace period: Zoom performs internal redirects during audio init handshake
    // right after join. Ignore navigations during this window to avoid false ejection.
    if (Date.now() - joinedAtMs < GRACE_PERIOD_MS) {
      log(`[Zoom Web] Ignoring navigation during grace period (${elapsed}s after join): ${url}`);
      return;
    }

    // Ignore known audio-init redirect URLs (they can extend beyond the grace
    // window) — but ONLY before the bot has confirmed presence. After the bot has
    // been in the meeting, the same /wc/{id}/join redirect means the meeting ended;
    // fall through so the /wc/ handling below defers it to the polling loop.
    if (isZoomAudioInitUrl(url) && !confirmedPresence) {
      log(`[Zoom Web] Ignoring audio-init redirect URL (${elapsed}s after join): ${url}`);
      return;
    }

    // Any navigation away from the zoom.us domain means the meeting ended
    // (covers company SSO redirects, homepages, sign-in pages, etc.)
    if (!/zoom\.(us|com|eu|com\.cn|com\.br|com\.au|de|fr|jp|ca|co\.uk)\b/.test(url)) {
      triggerRemoval(`Navigation away from Zoom domain: ${url}`);
    } else if (url.includes('/signin') || url.includes('/login')) {
      // Explicit sign-in redirect — meeting definitely ended
      triggerRemoval(`Navigation to Zoom sign-in: ${url}`);
    } else if (url.includes('/wc/') && !url.includes('/meeting')) {
      // Non-meeting /wc/ URL — but only if it's not a known init pattern
      log(`[Zoom Web] Suspicious non-meeting /wc/ URL (${elapsed}s after join): ${url} — deferring to polling`);
      // Don't trigger immediately — let the polling loop confirm via Leave button absence
    }
  };
  page.on('framenavigated', onNavigated);

  const poll = async () => {
    if (stopped || !page || page.isClosed()) return;

    try {
      // Check for end-of-meeting modal (zm-modal-body-title)
      const modalEl = page.locator(zoomMeetingEndedModalSelector).first();
      const modalVisible = await modalEl.isVisible({ timeout: 300 }).catch(() => false);
      if (modalVisible) {
        const modalText = await modalEl.textContent() ?? '';
        const trimmed = modalText.trim();
        const isRemoval = zoomRemovalTexts.some(t => trimmed.includes(t));
        if (isRemoval) {
          await triggerRemoval(`Removal/end modal detected: "${trimmed}"`);
          return;
        } else {
          log(`[Zoom Web] Ignoring non-removal modal: "${trimmed}"`);
        }
      }

      // Check via body text for removal phrases
      const detected = await page.evaluate((texts: string[]) => {
        const bodyText = document.body.innerText || '';
        return texts.find(t => bodyText.includes(t)) || null;
      }, zoomRemovalTexts).catch(() => null);

      if (detected) {
        await triggerRemoval(`Removal detected via text: "${detected}"`);
        return;
      }

      // Check if Leave button disappeared — require consecutive misses to avoid
      // false positives from Zoom UI transitions (popups, tooltips, feature tips).
      const leaveVisible = await page.locator(zoomLeaveButtonSelector).first()
        .isVisible({ timeout: 300 }).catch(() => false);
      if (!leaveVisible) {
        consecutiveLeaveButtonMisses++;
        const url = page.url();
        const title = await page.title().catch(() => '');
        const elapsed = ((Date.now() - joinedAtMs) / 1000).toFixed(1);
        log(`[Zoom Web] Leave button miss #${consecutiveLeaveButtonMisses} (${elapsed}s after join) — URL: ${url}, title: "${title}"`);

        // During grace period or on audio-init URLs, don't act on Leave button absence.
        // Zoom's UI hasn't fully loaded yet — Leave button simply doesn't exist.
        // But once presence was confirmed, an audio-init URL is really the meeting
        // ending (Zoom bounced us to the pre-join page), so stop suppressing then —
        // the consecutive-miss threshold below still guards against a transient blip.
        if (Date.now() - joinedAtMs < GRACE_PERIOD_MS || (isZoomAudioInitUrl(url) && !confirmedPresence)) {
          const why = Date.now() - joinedAtMs < GRACE_PERIOD_MS ? 'grace period' : 'pre-presence audio-init URL';
          log(`[Zoom Web] Suppressing Leave button miss — ${why}`);
        } else {
          // Navigated off Zoom entirely — immediate exit (no counter needed)
          if (url && !url.startsWith('about:') && !/zoom\.(us|com|eu|com\.cn|com\.br|com\.au|de|fr|jp|ca|co\.uk)\b/.test(url)) {
            await triggerRemoval(`Leave button gone and URL left Zoom domain: ${url}`);
            return;
          }
          // Redirected to sign-in — immediate exit
          if (url.includes('/signin') || url.includes('/login')) {
            await triggerRemoval(`Leave button gone and redirected to sign-in: ${url}`);
            return;
          }
          // For other conditions, only act after consecutive misses
          if (consecutiveLeaveButtonMisses >= LEAVE_BUTTON_MISS_THRESHOLD) {
            // Redirected away from meeting page within Zoom (e.g. /wc/{id}/join).
            // Handle this URL shape EXCLUSIVELY on the longer reconnect-tolerant
            // threshold — and skip the title branches for it, since the pre-join
            // page's own title ("Join Meeting - Zoom") would otherwise trip the
            // post-meeting-title branch at the fast 3 and defeat the longer window.
            if (url.includes('/wc/') && !url.includes('/meeting')) {
              if (consecutiveLeaveButtonMisses >= WC_REDIRECT_MISS_THRESHOLD) {
                await triggerRemoval(`Leave button gone ${consecutiveLeaveButtonMisses}x and URL is non-meeting: ${url}`);
                return;
              }
            // Error page or blank
            } else if (title === 'Error - Zoom' || title === '') {
              await triggerRemoval(`Leave button gone ${consecutiveLeaveButtonMisses}x and page shows error (title="${title}")`);
              return;
            // Generic post-meeting title
            } else if (zoomPostMeetingTitles.includes(title)) {
              await triggerRemoval(`Leave button gone ${consecutiveLeaveButtonMisses}x and post-meeting title: "${title}"`);
              return;
            }
          }
        }
      } else {
        if (!confirmedPresence) {
          confirmedPresence = true;
          log('[Zoom Web] Presence confirmed (Leave button visible) — audio-init URL exemption now disabled; a later /wc/join redirect will be treated as meeting end');
        }
        if (consecutiveLeaveButtonMisses > 0) {
          log(`[Zoom Web] Leave button recovered after ${consecutiveLeaveButtonMisses} miss(es)`);
        }
        consecutiveLeaveButtonMisses = 0;

        // Left-alone / empty-room check. The bot is confirmed in the meeting
        // (Leave button visible); only run past the join grace.
        if (Date.now() - joinedAtMs >= GRACE_PERIOD_MS) {
          // Zoom's OWN participant count (includes the bot) from the Participants
          // toolbar button — the one count source that is view-mode-independent
          // (survives Speaker View, which the bot is forced into, AND screen
          // share). Deliberately NO DOM-tile fallback: `.video-avatar__*` is the
          // Gallery-View family, so in Speaker View it would see only the bot's
          // self-view and report a false "alone". If the button can't be parsed
          // we return 0 = "unreadable" → hold (never leave), so a selector drift
          // degrades to "bot lingers" (safe), never "bot evicts a live meeting".
          const info = await page
            .evaluate((btnSel: string) => {
              const btn = document.querySelector(btnSel);
              if (btn) {
                const s = (btn.getAttribute('aria-label') || '') + ' ' + (btn.textContent || '');
                // Prefer the number anchored to "participant(s)" (live label:
                // "…pane,3 particpants" — note Zoom's own misspelling) so an
                // unrelated badge/count elsewhere in the label can't be misread;
                // fall back to the first digit-run.
                const m = s.match(/(\d+)\s*partic/i) || s.match(/(\d+)/);
                if (m) return { count: parseInt(m[1], 10), source: 'button' };
              }
              return { count: 0, source: 'unreadable' };
            }, zoomParticipantsButtonSelector)
            .catch(() => ({ count: 0, source: 'unreadable' }));

          // Observability breadcrumb: if the count is unreadable, the alone-timer
          // HOLDS (never leaves) — surface it once so a Participants-button
          // selector drift is visible in logs instead of silently reverting to
          // the pre-fix "bot lingers up to 4h" behavior.
          if (info.source === 'unreadable' && !warnedUnreadable) {
            warnedUnreadable = true;
            log('[Zoom Web] WARNING: participant count unreadable (Participants button not matched) — left-alone timer HOLDING; verify zoomParticipantsButtonSelector against live DOM.');
          }

          const hadSeenOthers = aloneState.sawOthers;
          const res = stepAloneTimer(aloneState, info.count, POLL_INTERVAL_MS, everyoneLeftMs, noOneJoinedMs);
          aloneState = res.state;

          if (info.count >= 2 && !hadSeenOthers) {
            log('[Zoom Web] Other participant(s) present — alone-timer armed for "everyone left"');
          } else if (info.count === 1) {
            const threshold = aloneState.sawOthers ? everyoneLeftMs : noOneJoinedMs;
            log(`[Zoom Web] Bot alone (${res.label}) ${(aloneState.aloneMs / 1000).toFixed(0)}s / ${(threshold / 1000).toFixed(0)}s [count source=${info.source}]`);
          }

          if (res.shouldLeave) {
            const token = aloneState.sawOthers ? 'ZOOM_BOT_LEFT_ALONE_TIMEOUT' : 'ZOOM_BOT_STARTUP_ALONE_TIMEOUT';
            await triggerRemoval(
              `Alone in meeting (${res.label}) for ${(aloneState.aloneMs / 1000).toFixed(0)}s`,
              token
            );
            return;
          }
        }
      }
    } catch {
      // Page navigated away or context destroyed
      await triggerRemoval('Exception in removal poll — page likely navigated away');
      return;
    }

    if (!stopped) {
      setTimeout(poll, POLL_INTERVAL_MS);
    }
  };

  setTimeout(poll, 3000);

  return () => {
    stopped = true;
    page.off('framenavigated', onNavigated);
    log('[Zoom Web] Removal monitor stopped');
  };
}
