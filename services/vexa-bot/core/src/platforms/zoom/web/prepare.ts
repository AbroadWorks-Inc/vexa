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
  zoomMicToggleSelectors,
  zoomMicNonToggleExactLabels,
  zoomMicNonToggleSubstrings,
  zoomMicUnmutedClassHints,
  zoomMicMutedClassHints,
  zoomNonMicLabelSubstrings,
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
 *
 * THIS IS THE CANARY for the outbound-audio seal. No <audio> element with a live
 * stream means the bot never joined audio, which means no recording — and the
 * seal (armed pre-navigation, so it is in force during Zoom's audio-join) is the
 * one new mechanism that could plausibly cause that. So the headline count is
 * logged on SUCCESS as well as failure: an operator scanning a live log must be
 * able to answer "did audio join?" without reading the per-element detail. If it
 * reports zero, try ZOOM_AUDIO_LOCK=off before looking anywhere else.
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

    // One scannable verdict line, logged in BOTH outcomes — this is the canary an
    // operator should read first when a run produces no transcript.
    if (audioInfo.withAudioStreams > 0) {
      log(`[Zoom Web] ✅ AUDIO JOIN OK — ${audioInfo.withAudioStreams} element(s) with live audio streams (${audioInfo.totalElements} total media elements); recording can proceed`);
      for (const d of audioInfo.details) {
        log(`[Zoom Web]   Element ${d.index} <${d.tag}>: paused=${d.paused}, tracks=${d.trackCount}, states=${JSON.stringify(d.trackStates)}`);
      }
    } else {
      log(`[Zoom Web] ❌ AUDIO JOIN FAILED — 0 elements with audio streams (${audioInfo.totalElements} total media elements). THERE WILL BE NO RECORDING. If the outbound-audio seal is enabled, retry with ZOOM_AUDIO_LOCK=off to rule it out before investigating anything else.`);
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
 * Normalise an aria-label / class string for vocabulary matching: lowercase,
 * whitespace-collapsed, trimmed. `null` becomes ''.
 */
export function normaliseZoomMicText(raw: string | null | undefined): string {
  return (raw ?? '').toLowerCase().replace(/\s+/g, ' ').trim();
}

/** Attributes read off one candidate mic-control element. */
export interface ZoomMicProbe {
  ariaLabel: string | null;
  ariaPressed: string | null;
  className: string | null;
  /** class attributes of icon/span descendants — Zoom encodes state in the icon. */
  descendantClassNames: string[];
}

/**
 * What a probe tells us about the mic.
 *
 * `not-mute-toggle` and `unknown` are deliberately distinct from `unmuted`: a
 * control we cannot read is NOT evidence that the mic is live, and must never
 * trigger a click. Clicking on a bad read is the one truly harmful outcome
 * here — it can UNMUTE a bot that was already muted.
 */
export type ZoomMicReading =
  | { kind: 'muted'; evidence: string }
  | { kind: 'unmuted'; evidence: string }
  /** a DIFFERENT control that merely contains mute vocabulary ("Mute All"). */
  | { kind: 'not-mic-control'; evidence: string }
  | { kind: 'not-mute-toggle'; evidence: string }
  | { kind: 'unknown'; evidence: string };

/**
 * Interpret one candidate control's attributes into a mic reading.
 *
 * Evidence is weighed in descending order of trust:
 *   1. aria-label mute vocabulary — 'unmute' BEFORE 'mute', because the string
 *      "Unmute" contains "mute" and a naive substring test inverts the reading.
 *      This ranks first because it needs NO polarity assumption: the label names
 *      the action OFFERED, which fixes the current state unambiguously.
 *   2. aria-pressed="true" -> muted. A real ARIA state attribute, so it
 *      outranks the guessed CSS class names below it.
 *      aria-pressed="false" is deliberately NOT read as unmuted. ARIA convention
 *      says a toggle button's pressed state is its engaged state, but for a mic
 *      control "engaged" is genuinely ambiguous — it can mean "mute is applied"
 *      or "microphone is on", and real apps ship both. Zoom's polarity is
 *      unconfirmed (see below), and the two possible mistakes do NOT cost the
 *      same: a wrong 'unmuted' read triggers a CLICK that unmutes a
 *      already-muted bot — making the user-visible complaint worse — whereas a
 *      wrong 'muted' read merely declines to click and leaves layer 2
 *      (track-level silence) in charge. Asymmetric cost, asymmetric rule.
 *      NOTE: aria-pressed has never been OBSERVED on Zoom's control. The live
 *      2026-09-01 run logged aria-label only, so this branch is untested against
 *      the real DOM; describeZoomMicCandidates now prints aria-pressed so a
 *      single live run settles whether Zoom sets it (and with which polarity).
 *   3. class hints on the button and its icon descendants (unverified live) —
 *      again unmuted-before-muted, since 'audio-unmuted' contains 'muted'.
 *   4. Only THEN is a label classified as known-non-state vocabulary ("audio",
 *      "join audio"). This check ranks LAST on purpose: it used to return early,
 *      which meant the live aria-label="audio" element was written off before
 *      aria-pressed or the class hints were ever consulted. An uninformative
 *      LABEL is not an uninformative ELEMENT.
 */
export function readZoomMicState(probe: ZoomMicProbe): ZoomMicReading {
  const label = normaliseZoomMicText(probe.ariaLabel);

  // FIRST, and returning immediately: a control that merely CONTAINS mute
  // vocabulary but is not the mic toggle. Must precede the mute branches below
  // (otherwise "Ask to unmute" reads as muted and "Mute All" as unmuted) AND
  // must not fall through to the class hints (a "Mute All" button can carry its
  // own muted-looking icon class).
  const nonMic = zoomNonMicLabelSubstrings.find((s) => label.includes(s));
  if (nonMic) {
    return { kind: 'not-mic-control', evidence: `aria-label "${label}" matches non-mic control "${nonMic}"` };
  }

  if (label.includes('unmute')) {
    // "Unmute" == the action offered == currently MUTED.
    return { kind: 'muted', evidence: `aria-label "${label}" offers unmute` };
  }
  if (label.includes('mute')) {
    // "Mute" == the action offered == currently UNMUTED.
    return { kind: 'unmuted', evidence: `aria-label "${label}" offers mute` };
  }
  // A real ARIA state attribute outranks the guessed class names below.
  if (probe.ariaPressed === 'true') {
    return { kind: 'muted', evidence: 'aria-pressed="true"' };
  }

  const classes = normaliseZoomMicText([probe.className, ...probe.descendantClassNames].join(' '));
  const unmutedHint = zoomMicUnmutedClassHints.find((h) => classes.includes(h));
  if (unmutedHint) {
    return { kind: 'unmuted', evidence: `class hint "${unmutedHint}"` };
  }
  const mutedHint = zoomMicMutedClassHints.find((h) => classes.includes(h));
  if (mutedHint) {
    return { kind: 'muted', evidence: `class hint "${mutedHint}"` };
  }

  // LAST, not first: an uninformative label is not an uninformative element.
  if (label && (zoomMicNonToggleExactLabels.includes(label)
    || zoomMicNonToggleSubstrings.some((s) => label.includes(s)))) {
    return { kind: 'not-mute-toggle', evidence: `aria-label "${label}" carries no mute state` };
  }

  return label
    ? { kind: 'not-mute-toggle', evidence: `aria-label "${label}" unrecognised` }
    : { kind: 'unknown', evidence: 'no aria-label, no class hint, no aria-pressed' };
}

/** One probed DOM candidate, addressable again via selector + nth index. */
export interface ZoomMicCandidate {
  selector: string;
  index: number;
  probe: ZoomMicProbe;
}

export interface ZoomMicSelection {
  candidate: ZoomMicCandidate;
  reading: ZoomMicReading;
}

/**
 * Pick the first candidate that yields a CONFIDENT reading (muted / unmuted).
 * Candidates that read `not-mute-toggle` or `unknown` are skipped rather than
 * acted on — this is what stops the bot from clicking a control it has not
 * actually identified (the aria-label="audio" element observed live).
 */
export function selectZoomMicToggle(candidates: ZoomMicCandidate[]): ZoomMicSelection | null {
  for (const candidate of candidates) {
    const reading = readZoomMicState(candidate.probe);
    if (reading.kind === 'muted' || reading.kind === 'unmuted') {
      return { candidate, reading };
    }
  }
  return null;
}

/** One-line digest of everything probed — the diagnostic missing from the failed run. */
export function describeZoomMicCandidates(candidates: ZoomMicCandidate[]): string {
  if (candidates.length === 0) return 'no mic-control candidates matched any selector';
  return candidates
    .map((c) => `${c.selector}[${c.index}] aria-label="${c.probe.ariaLabel ?? ''}" aria-pressed=${c.probe.ariaPressed ?? 'absent'} -> ${readZoomMicState(c.probe).kind}`)
    .join('; ');
}

/**
 * Read every match of every candidate selector out of the live DOM.
 *
 * Runs as ONE page.evaluate so a footer that auto-hides mid-probe cannot give a
 * torn read across selectors. Zero-area (hidden) elements are skipped, but the
 * reported `index` stays the index within the full querySelectorAll order so
 * `page.locator(selector).nth(index)` addresses the same element.
 */
async function probeZoomMicCandidates(page: Page, selectors: string[]): Promise<ZoomMicCandidate[]> {
  return page.evaluate((sels: string[]) => {
    const MAX_PER_SELECTOR = 4;
    const MAX_DESCENDANTS = 8;
    const out: ZoomMicCandidate[] = [];
    for (const selector of sels) {
      let nodes: Element[] = [];
      try {
        nodes = Array.from(document.querySelectorAll(selector));
      } catch {
        continue; // selector not supported by this browser — try the next
      }
      nodes.slice(0, MAX_PER_SELECTOR).forEach((el, index) => {
        const rect = el.getBoundingClientRect();
        if (rect.width === 0 && rect.height === 0) return; // hidden — not clickable
        const descendantClassNames = Array.from(el.querySelectorAll('[class]'))
          .slice(0, MAX_DESCENDANTS)
          .map((d) => d.getAttribute('class') ?? '');
        out.push({
          selector,
          index,
          probe: {
            ariaLabel: el.getAttribute('aria-label'),
            ariaPressed: el.getAttribute('aria-pressed'),
            className: el.getAttribute('class'),
            descendantClassNames,
          },
        });
      });
    }
    return out;
  }, selectors);
}

export interface ZoomMuteOutcome {
  /** true only when a control was positively read as muted. */
  muted: boolean;
  clicked: boolean;
  attempts: number;
  detail: string;
}

/**
 * LAYER 1 — make the DOM mute actually take, so other participants SEE the bot
 * muted in the participant list.
 *
 * WHY: the pre-join preview mute (join.ts) does NOT reliably persist into the
 * meeting — Zoom re-enables the mic when audio is joined, so a recorder bot can
 * end up shown as an unmuted participant (user-observed 2026-08-25, and again
 * 2026-09-01 with the preview mute confirmed successful in the same run).
 *
 * The previous implementation compared aria-label with EXACT string equality
 * against 'Mute'/'Unmute'. The live DOM returned aria-label="audio", which
 * matched neither, so all 6 attempts fell through to the "audio not joined"
 * branch and the bot stayed unmuted for the whole meeting. Reading is now
 * vocabulary-tolerant (readZoomMicState) and probes EVERY match of several
 * selectors rather than `.first()` of one.
 *
 * This is best-effort by nature — it depends on Zoom's DOM. The deterministic
 * guarantee that no audio leaves the bot is startZoomOutboundAudioGuard
 * (layer 2), which does not read the DOM at all. Never throws.
 *
 * Recorder bots only; voice-agent bots must transmit TTS and are gated out by
 * the caller.
 */
export async function ensureZoomMutedInMeeting(page: Page): Promise<ZoomMuteOutcome> {
  // One-shot latch: click the toggle AT MOST ONCE. If a click registers but the
  // aria-label lags (broken/slow DOM), clicking again on a later pass could apply
  // an EVEN number of toggles and leave the bot UNMUTED — the opposite of intent.
  // After clicking once we only re-read, letting a lagging label flip to "Unmute".
  let clickedMute = false;
  let lastDetail = 'no probe completed';

  for (let attempt = 0; attempt < 6; attempt++) {
    try {
      await revealZoomFooter(page);
      const candidates = await probeZoomMicCandidates(page, zoomMicToggleSelectors);
      const selection = selectZoomMicToggle(candidates);
      lastDetail = describeZoomMicCandidates(candidates);

      if (!selection) {
        // Log EVERY candidate seen. The 2026-09-01 failure logged only one
        // label and repeated it 6 times, which hid that other matches existed.
        log(`[Zoom Web] No readable mic toggle — retry ${attempt + 1}/6 [${lastDetail}]`);
      } else if (selection.reading.kind === 'muted') {
        log(
          clickedMute
            ? `[Zoom Web] Muted microphone in meeting (recorder bot — receive-only) [${selection.reading.evidence}]`
            : attempt === 0
              ? `[Zoom Web] Mic already muted in meeting [${selection.reading.evidence}]`
              : `[Zoom Web] Mic muted in meeting (confirmed) [${selection.reading.evidence}]`,
        );
        return { muted: true, clicked: clickedMute, attempts: attempt + 1, detail: selection.reading.evidence };
      } else if (clickedMute) {
        log(`[Zoom Web] Mute clicked but control still reads unmuted — not re-clicking (avoids toggling back); re-checking [${selection.reading.evidence}]`);
      } else {
        const target = page.locator(selection.candidate.selector).nth(selection.candidate.index);
        await target.click({ timeout: 3000 });
        clickedMute = true;
        await page.waitForTimeout(500);

        const after = selectZoomMicToggle(await probeZoomMicCandidates(page, zoomMicToggleSelectors));
        if (after && after.reading.kind === 'muted') {
          log(`[Zoom Web] Muted microphone in meeting (recorder bot — receive-only) [${after.reading.evidence}]`);
          return { muted: true, clicked: true, attempts: attempt + 1, detail: after.reading.evidence };
        }
        log(`[Zoom Web] Mute click not yet confirmed (${after ? after.reading.kind : 'unreadable'}) — re-checking`);
      }
    } catch (e: any) {
      log(`[Zoom Web] ensureZoomMutedInMeeting attempt ${attempt + 1} error: ${e?.message ?? e}`);
    }
    await page.waitForTimeout(5000);
  }

  log(`[Zoom Web] WARNING: could not confirm in-meeting mute after retries — the participant list may show this bot as unmuted. Outbound audio is still silenced at track level by the outbound-audio guard. Last probe: [${lastDetail}]`);
  return { muted: false, clicked: clickedMute, attempts: 6, detail: lastDetail };
}

/** Result of one outbound-audio sweep. All counters are per-sweep, not cumulative. */
export interface OutboundAudioSweepResult {
  skippedVoiceAgent: boolean;
  /** false when the RTCPeerConnection registry was never installed (video-block script off). */
  registryPresent: boolean;
  peerConnections: number;
  audioSendersFound: number;
  tracksDisabled: number;
  alreadyDisabled: number;
  errors: number;
}

// Minimal structural types for the in-page sweep. Types are erased at compile
// time, so referencing them does NOT break page.evaluate serialisation — only
// runtime VALUES from module scope would.
interface OutboundAudioTrackLike {
  kind?: string;
  enabled?: boolean;
}
interface OutboundAudioSenderLike {
  track?: OutboundAudioTrackLike | null;
}
interface OutboundAudioPeerLike {
  connectionState?: string;
  getSenders?: () => OutboundAudioSenderLike[];
}
interface VexaAudioWindow {
  __vexa_peer_connections?: unknown;
}

/**
 * LAYER 2 — the deterministic guarantee, executed IN THE PAGE.
 *
 * A recorder bot is receive-only: it never legitimately transmits. So rather
 * than trusting Zoom's DOM, disable every OUTBOUND audio track directly. Even
 * if every selector in this file rots, no audio leaves the bot.
 *
 * Mirrors the in-repo precedent in services/screen-content.ts (~line 1228),
 * which disables incoming VIDEO tracks with `track.enabled = false` — and
 * deliberately keeps to that primitive: the same file records (v0.10.6, #291)
 * that mutating `transceiver.direction` here produces a malformed offer SDP and
 * degrades the peer connection across three platforms. So: senders only,
 * `enabled = false` only, no SDP munging, no transceiver mutation.
 *
 * Touches `getSenders()` exclusively — never `getReceivers()` — so the incoming
 * audio the transcription pipeline depends on is untouched.
 *
 * MUST stay self-contained (no module-scope values, no imports): it is
 * serialised into the browser by page.evaluate. That self-containment is also
 * what makes it directly unit-testable against a fake registry.
 */
export function silenceOutboundAudioTracks(voiceAgentEnabled: boolean): OutboundAudioSweepResult {
  const result: OutboundAudioSweepResult = {
    skippedVoiceAgent: false,
    registryPresent: false,
    peerConnections: 0,
    audioSendersFound: 0,
    tracksDisabled: 0,
    alreadyDisabled: 0,
    errors: 0,
  };

  // A voice agent legitimately transmits TTS — force-muting it would break it.
  if (voiceAgentEnabled) {
    result.skippedVoiceAgent = true;
    return result;
  }

  // BOTH registries, deduplicated — the Zoom-local one first.
  //
  // This read used to be `__vexa_peer_connections` alone: Meet's registry, whose
  // only writers live in services/screen-content.ts's getVirtualCameraInitScript
  // and are therefore installed solely when `cameraEnabled` is true (default
  // false). In the default recorder configuration it does not exist, so this
  // sweep found no peers and disabled nothing — the same defect as the original
  // registry bug, surviving in the fallback path that was supposed to be the
  // cover for an unsealable track. Dedup matters because a peer created while
  // cameraEnabled is on lands in BOTH arrays.
  const g = globalThis as unknown as VexaAudioWindow & { __vexa_zoom_peer_connections?: unknown };
  const peers: OutboundAudioPeerLike[] = [];
  for (const candidate of [g.__vexa_zoom_peer_connections, g.__vexa_peer_connections]) {
    if (!Array.isArray(candidate)) continue;
    result.registryPresent = true;
    for (const peer of candidate as OutboundAudioPeerLike[]) {
      if (!peers.includes(peer)) peers.push(peer);
    }
  }
  if (!result.registryPresent) return result; // reported, not assumed

  for (const peer of peers) {
    try {
      if (!peer || typeof peer.getSenders !== 'function') {
        result.errors++;
        continue;
      }
      if (peer.connectionState === 'closed') continue;
      result.peerConnections++;
      for (const sender of peer.getSenders()) {
        const track = sender && sender.track;
        if (!track || track.kind !== 'audio') continue;
        result.audioSendersFound++;
        if (track.enabled === false) {
          result.alreadyDisabled++;
          continue;
        }
        track.enabled = false;
        result.tracksDisabled++;
      }
    } catch {
      result.errors++; // one bad peer must never abort the sweep
    }
  }

  return result;
}

/**
 * Run one outbound-audio sweep in the page. Returns null if the page is gone or
 * the evaluate failed (navigation destroys the execution context). Never throws.
 */
export async function sweepZoomOutboundAudio(
  page: Page,
  voiceAgentEnabled: boolean,
): Promise<OutboundAudioSweepResult | null> {
  if (page.isClosed()) return null;
  try {
    return await page.evaluate(silenceOutboundAudioTracks, voiceAgentEnabled);
  } catch {
    return null;
  }
}

/** Format a sweep result for the log. Pure, so it is unit-testable. */
export function describeOutboundAudioSweep(result: OutboundAudioSweepResult): string {
  if (result.skippedVoiceAgent) return 'voice agent enabled — outbound audio intentionally left live';
  if (!result.registryPresent) {
    return 'RTCPeerConnection registry absent (videoReceiveEnabled?) — track-level mute could NOT be applied';
  }
  return `pcs=${result.peerConnections} audioSenders=${result.audioSendersFound} disabled=${result.tracksDisabled} alreadyDisabled=${result.alreadyDisabled} errors=${result.errors}`;
}

/**
 * BACKSTOP for the outbound-audio lock — not the primary mechanism.
 *
 * The patches installed at page load (see join.ts) are what deliver the
 * guarantee; this interval is the backstop for the residual cases they cannot
 * cover: a track that appeared through a path we did not patch, one whose sealing
 * failed (defineProperty threw), and — the case that actually motivated the
 * re-verification — a track whose seal was REMOVED after the fact, since the
 * descriptor is deliberately configurable.
 *
 * On navigation: a document navigation creates fresh track objects and re-runs
 * the page-load init script, so the patches come back on their own. This tick is
 * not what recovers them, and the previous claim that it was has been removed.
 *
 * Logs the first pass, then only passes that actually locked something or
 * refused an unmute — a 10s heartbeat would bury everything else in the log.
 *
 * The interval is unref()'d and self-clears when the page closes, so it can
 * never hold the Node event loop open and delay bot shutdown.
 *
 * @returns a stop function (idempotent).
 */
export function startZoomOutboundAudioGuard(
  page: Page,
  voiceAgentEnabled: boolean,
  intervalMs = 10_000,
): () => void {
  if (voiceAgentEnabled) {
    log('[Zoom Web] Outbound audio guard NOT armed — voice agent must transmit TTS');
    return () => { /* nothing armed */ };
  }

  let stopped = false;
  let lastLockedCount = -1;
  let lastBlockedCount = 0;
  const timer = setInterval(() => {
    void (async () => {
      if (stopped) return;
      if (page.isClosed()) {
        stop();
        return;
      }
      // Re-install rather than merely sweep: this re-seals new tracks AND
      // re-applies the patches if a navigation dropped them.
      const lock = await installZoomOutboundAudioLock(page, voiceAgentEnabled);
      if (!lock) return;
      const newlyLocked = lastLockedCount >= 0 && lock.tracksLocked > lastLockedCount;
      const newlyBlocked = lock.blockedUnmutes > lastBlockedCount;
      if (newlyLocked || newlyBlocked) {
        log(`[Zoom Web] Outbound audio lock refreshed — ${describeOutboundAudioLock(lock)}`);
      }
      lastLockedCount = lock.tracksLocked;
      lastBlockedCount = lock.blockedUnmutes;
      // Belt and braces: if any track could not be sealed, at least keep it
      // disabled with the least invasive primitive.
      if (lock.errors > 0) {
        const sweep = await sweepZoomOutboundAudio(page, voiceAgentEnabled);
        if (sweep && sweep.tracksDisabled > 0) {
          log(`[Zoom Web] Unsealed track re-silenced by sweep — ${describeOutboundAudioSweep(sweep)}`);
        }
      }
    })();
  }, intervalMs);

  // Never keep the process alive for this guard.
  if (typeof timer.unref === 'function') timer.unref();

  const stop = (): void => {
    if (stopped) return;
    stopped = true;
    clearInterval(timer);
  };

  page.once('close', stop);
  return stop;
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

/**
 * Parse the `ZOOM_AUDIO_LOCK` operational kill switch. Pure, so it is testable.
 *
 * Returns true (seal ACTIVE) for anything that is not an explicit opt-out —
 * including unset, empty and unrecognised values. Defaulting to ON is the whole
 * point: a typo must not silently ship an unprotected bot.
 *
 * Recognised opt-outs (case/whitespace-insensitive): off, 0, false, no, disabled.
 */
export function parseZoomAudioLockEnv(raw: string | undefined): boolean {
  const v = (raw ?? '').trim().toLowerCase();
  return !(v === 'off' || v === '0' || v === 'false' || v === 'no' || v === 'disabled');
}

/** Memoised so the decision is read once per process and logged exactly once. */
let zoomAudioSealDecision: boolean | null = null;

/**
 * THE OPERATIONAL ESCAPE HATCH — read this before deleting the env var.
 *
 * This is NOT a feature flag. It exists because the seal is an UNVERIFIED
 * mechanism sitting on the RECORDING critical path. Since the lock is now armed
 * before Zoom's own scripts run (join.ts), the mic track is sealed DURING Zoom's
 * audio-join rather than after it. If Zoom writes `enabled = true`, reads back
 * `false` and treats that as a failure, the plausible worst case is that the bot
 * never joins audio at all — costing the transcript, which is the product, and a
 * strictly worse outcome than the unmuted-bot bug this all fixes.
 *
 * So an operator who sees audio-join breakage in a live meeting can set
 * ZOOM_AUDIO_LOCK=off and keep recording, with no rebuild and no revert.
 *
 * Defaults to ON. Logged once, plainly, at arm time: a silent kill switch is
 * worse than none — if anyone later asks "was the lock on during that meeting?",
 * the log has to answer.
 */
export function isZoomAudioSealEnabled(): boolean {
  if (zoomAudioSealDecision !== null) return zoomAudioSealDecision;
  const raw = process.env.ZOOM_AUDIO_LOCK;
  zoomAudioSealDecision = parseZoomAudioLockEnv(raw);
  log(
    zoomAudioSealDecision
      ? `[Zoom Web] Outbound audio SEAL: ENABLED (default) [ZOOM_AUDIO_LOCK=${raw ?? '<unset>'}]`
      : `[Zoom Web] Outbound audio SEAL: DISABLED by operator [ZOOM_AUDIO_LOCK=${raw ?? '<unset>'}] — tracks are still set enabled=false and the DOM mute path is unchanged, but an unmute CAN succeed`,
  );
  return zoomAudioSealDecision;
}

// ---------------------------------------------------------------------------
// DELIBERATELY ZOOM-LOCAL — DO NOT GENERALISE INTO src/services/.
//
// Everything from here to the end of the mute machinery (mic-state reading, the
// outbound-audio lock, the mute watcher) is intentionally duplicated inside
// platforms/zoom/web/ instead of being factored into a shared helper. Extracting
// it is normally the right instinct and here it is the WRONG one: Google Meet and
// the transcription pipeline are tested and in production, and a shared module is
// exactly the path by which a Zoom-only fix reaches Meet. If you are tempted to
// hoist this, don't — copy it, or raise it first.
//
// The same rule fixes the blast radius: these functions are reachable ONLY from
// platforms/zoom/web/index.ts's afterAdmission hook, so no other platform can
// execute them even by accident.
// ---------------------------------------------------------------------------

/** Result of installing the outbound-audio lock. Counters are CUMULATIVE per page. */
export interface OutboundAudioLockResult {
  /** false when the ZOOM_AUDIO_LOCK kill switch disabled the seal. */
  sealEnabled: boolean;
  skippedVoiceAgent: boolean;
  /** true when a previous install already ran in this page (the call is idempotent). */
  alreadyInstalled: boolean;
  registryPresent: boolean;
  tracksLocked: number;
  /** how many times something tried to set enabled=true and was refused. */
  blockedUnmutes: number;
  /** previously-sealed tracks whose seal was verified still intact this pass. */
  tracksVerified: number;
  /** tracks whose seal had been REMOVED and was re-applied (see BLOCKER-1 note). */
  tracksResealed: number;
  patchedConstructor: boolean;
  patchedAddTrack: boolean;
  patchedAddTransceiver: boolean;
  patchedReplaceTrack: boolean;
  patchedGetUserMedia: boolean;
  errors: number;
}

type AnyFn = (...args: unknown[]) => unknown;

interface LockableTrack {
  kind?: string;
  enabled?: boolean;
}

interface AudioLockState {
  tracksLocked: number;
  tracksResealed: number;
  blockedUnmutes: number;
  errors: number;
  patchedConstructor: boolean;
  patchedAddTrack: boolean;
  patchedAddTransceiver: boolean;
  patchedReplaceTrack: boolean;
  patchedGetUserMedia: boolean;
}

interface LockGlobal {
  __vexa_zoom_audio_lock?: AudioLockState;
  /** Meet's shared registry — READ ONLY here, and only present when cameraEnabled. */
  __vexa_peer_connections?: unknown;
  /** Zoom-local registry, written by this file's own constructor patch. */
  __vexa_zoom_peer_connections?: unknown;
  RTCPeerConnection?: unknown;
  RTCRtpSender?: unknown;
  navigator?: { mediaDevices?: Record<string, unknown> };
}

/** Marker placed on the lock's own `enabled` getter so a seal can be recognised. */
interface LockGetter {
  __vexaAudioLockGetter?: boolean;
}

interface LockPeer {
  connectionState?: string;
  getSenders?: () => { track?: LockableTrack | null }[];
}

/**
 * LAYER 2 (primary) — make the recorder bot's mute IRREVOCABLE, executed IN THE PAGE.
 *
 * The periodic sweep alone only satisfies "is muted": it is a corrective, so
 * audio can be live for up to one interval and nothing PREVENTS an unmute. This
 * function closes both holes:
 *
 *   a) Every existing outbound audio track is disabled and then SEALED — an own
 *      `enabled` accessor shadows the prototype's, and its setter refuses `true`.
 *      This is the only thing that stops an in-call unmute, because Zoom's mute
 *      button sets `enabled = true` on the EXISTING track: no addTrack, no
 *      replaceTrack, no getUserMedia. Patching creation points cannot catch it.
 *   b) The three points where a NEW outbound audio track can appear are patched
 *      so the track is disabled AT BIRTH, before Zoom can transmit on it —
 *      `RTCPeerConnection.prototype.addTrack`, `RTCRtpSender.prototype.replaceTrack`
 *      and `navigator.mediaDevices.getUserMedia`. Zero window, no interval needed.
 *
 * ORDER IS CRITICAL inside lockTrack: `enabled = false` FIRST, seal SECOND.
 * Sealing first would leave a getter reporting `false` over a track that is
 * still really transmitting — a lie instead of a guarantee.
 *
 * RISK, stated honestly — AND NOTE WHERE THIS NOW RUNS. The lock is armed
 * PRE-NAVIGATION from join.ts, not at afterAdmission as it originally was, so it
 * covers the PREVIEW page too and the mic track is sealed DURING Zoom's
 * audio-join sequence rather than after it.
 *
 * TRANSCRIPT-LEVEL WORST CASE: if Zoom writes `enabled = true`, reads back
 * `false` and treats that as a failure, the bot may never join audio at all. No
 * audio join means no <audio> elements (see the liveAudioCount probe in
 * prepareZoomWebMeeting) which means NO RECORDING — the transcript, not merely the
 * outbound path. That is strictly worse than the unmuted-bot bug this fixes,
 * which is why ZOOM_AUDIO_LOCK exists: an operator can back the seal out
 * without building or redeploying an image. verifyAudioElements is the canary
 * that distinguishes this failure from every other one.
 *
 * Countervailing evidence that this is low-probability: the shipping flow ALREADY
 * joins audio with the mic muted (join.ts mutes in preview), and the live run of
 * 2026-09-01 logged "Muted microphone in preview" followed by "Audio already
 * flowing (2 live <audio> elements)". So a disabled local mic at join time is
 * proven tolerated. The untested delta is narrow: only that a WRITE of `true`
 * silently fails instead of taking effect.
 *
 * Judged
 * acceptable because (1) the getter is FACTUALLY correct — the track genuinely
 * is disabled, so this lies only about whether a write took effect, not about
 * whether audio flows; (2) the descriptor is left `configurable: true` so a
 * future caller COULD redefine or delete it — note there is deliberately NO
 * runtime escape hatch and no env flag today, so nothing in this codebase can
 * currently unlock a sealed track; (3) it touches OUTBOUND senders only, never `getReceivers()`,
 * so the inbound audio the transcript is built from cannot be affected. It is
 * unverified against Zoom's real audio subsystem — needs a live run.
 *
 * Safe to call repeatedly, and NOT idempotent in the naive sense — that
 * distinction matters. Each patch carries a marker so it is never double-wrapped,
 * but a track is NOT skipped merely because it was sealed once before: every call
 * re-checks the live property descriptor and RE-SEALS anything that lost its seal.
 * An earlier version short-circuited on WeakSet membership, which meant "already
 * seen" was treated as "still sealed" — so a de-sealed track was never recovered
 * and audio could transmit indefinitely. Verification, not memoisation.
 *
 * MUST stay self-contained (no module-scope values): it is serialised into the
 * browser by page.evaluate — which is also what makes it unit-testable here.
 */
export interface OutboundAudioLockOptions {
  voiceAgentEnabled: boolean;
  /** false = kill switch engaged: no seal, no at-birth patches (see isZoomAudioSealEnabled). */
  sealEnabled: boolean;
}

export function installOutboundAudioLockInPage(options: OutboundAudioLockOptions): OutboundAudioLockResult {
  const voiceAgentEnabled = options.voiceAgentEnabled;
  const sealEnabled = options.sealEnabled;
  const result: OutboundAudioLockResult = {
    sealEnabled,
    skippedVoiceAgent: false,
    alreadyInstalled: false,
    registryPresent: false,
    tracksLocked: 0,
    blockedUnmutes: 0,
    tracksVerified: 0,
    tracksResealed: 0,
    patchedConstructor: false,
    patchedAddTrack: false,
    patchedAddTransceiver: false,
    patchedReplaceTrack: false,
    patchedGetUserMedia: false,
    errors: 0,
  };

  // A voice agent legitimately transmits TTS. It must bypass EVERY layer,
  // prototype patching included — it could not undo a sealed track.
  if (voiceAgentEnabled) {
    result.skippedVoiceAgent = true;
    return result;
  }

  const g = globalThis as unknown as LockGlobal;
  const existing = g.__vexa_zoom_audio_lock;
  result.alreadyInstalled = !!existing;
  const state: AudioLockState = existing ?? {
    tracksLocked: 0,
    tracksResealed: 0,
    blockedUnmutes: 0,
    errors: 0,
    patchedConstructor: false,
    patchedAddTrack: false,
    patchedAddTransceiver: false,
    patchedReplaceTrack: false,
    patchedGetUserMedia: false,
  };
  g.__vexa_zoom_audio_lock = state;

  let verified = 0;

  /**
   * Is this track's `enabled` CURRENTLY our own seal?
   *
   * This replaced a `WeakSet.has(track)` short-circuit, which was a real defect:
   * the seal is `configurable: true`, so it can be redefined or deleted. Once it
   * was, the WeakSet still contained the track, so every subsequent pass returned
   * early WITHOUT re-checking — the track stayed de-sealed, `enabled` could be set
   * back to true, and audio transmitted indefinitely while the guard reported
   * `errors: 0` so even its fallback sweep never fired. Membership in a set is not
   * evidence that a property is still what you left it as; only the descriptor is.
   *
   * LIMIT: the marker is a plain writable property on a function object, so this
   * check is TAMPER-EVIDENT, NOT TAMPER-PROOF — a foreign getter that sets
   * `__vexaAudioLockGetter = true` would pass, leaving audio live with errors=0.
   * That needs a page deliberately forging it, which is not the threat model
   * here (Zoom is not adversarial), so the limit is documented rather than
   * hardened. Do not add crypto or a closure token for this.
   */
  const isSealed = (track: LockableTrack): boolean => {
    try {
      const d = Object.getOwnPropertyDescriptor(track, 'enabled');
      return !!(d && d.get && (d.get as LockGetter).__vexaAudioLockGetter === true);
    } catch {
      return false;
    }
  };

  const lockTrack = (track: LockableTrack | null | undefined): void => {
    if (!track || track.kind !== 'audio') return;
    if (isSealed(track)) {
      // Still sealed: the setter cannot have let `true` through, so there is
      // nothing to re-assert. Counted so a live run can show the seal holding.
      verified++;
      return;
    }
    const wasSealedBefore = state.tracksLocked > 0 || state.tracksResealed > 0;
    try {
      // 1. Actually stop the audio. NEVER reorder these two steps.
      track.enabled = false;
      // KILL SWITCH: with the seal disabled we stop here. The track is silent but
      // an unmute CAN succeed — deliberately the pre-seal behaviour, which is the
      // configuration proven not to disturb audio-join.
      if (!sealEnabled) return;
      // 2. Then refuse to let it come back.
      const getter = (): boolean => false;
      (getter as LockGetter).__vexaAudioLockGetter = true;
      Object.defineProperty(track, 'enabled', {
        configurable: true, // redefinable by a future caller; re-verified every guard tick
        enumerable: false,
        get: getter,
        set: (value: boolean) => {
          if (value) state.blockedUnmutes++;
        },
      });
      if (wasSealedBefore) state.tracksResealed++;
      state.tracksLocked++;
    } catch {
      // Sealing failed (a non-configurable `enabled`, say). The disable in step 1
      // has ALREADY been applied, so the track is silent — it is merely not
      // sealed, i.e. still revocable. Counting the error routes it to the guard's
      // periodic re-sweep, which is the only cover such a track gets.
      //
      // That cover is real but BOUNDED, and the bound is the honest part: the
      // sweep re-asserts `enabled = false` at most once per guard interval, so an
      // unsealable track that something re-enables can transmit for up to one
      // interval before being silenced again. It is not prevention. (The sweep
      // reaches such a track only because it now enumerates the Zoom-local
      // registry as well; reading Meet's alone, it found nothing in the default
      // configuration and this comment promised cover that did not exist.)
      //
      // (There was a `track.enabled = false` retry here; it was dead code — step 1
      // always precedes this catch — and mutation testing proved removing it
      // changed no observable behaviour.)
      state.errors++;
    }
  };

  // (a) Seal every outbound audio track that already exists.
  //
  // TWO registries are read. `__vexa_zoom_peer_connections` is OUR OWN, written
  // by the constructor patch below, and is the one that works in the default bot
  // configuration. `__vexa_peer_connections` is Meet's shared registry — read
  // opportunistically, never written here: its only writers live in
  // services/screen-content.ts's getVirtualCameraInitScript, which is installed
  // solely when `cameraEnabled` is true (default false). Relying on it was a real
  // defect: with the standard recorder config it does not exist, so this whole
  // step silently sealed NOTHING. Making screen-content.ts write it
  // unconditionally would alter Meet's init path and is forbidden.
  for (const candidate of [g.__vexa_zoom_peer_connections, g.__vexa_peer_connections]) {
    if (!Array.isArray(candidate)) continue;
    result.registryPresent = true;
    for (const peer of candidate as LockPeer[]) {
      try {
        if (!peer || typeof peer.getSenders !== 'function') {
          state.errors++;
          continue;
        }
        if (peer.connectionState === 'closed') continue;
        for (const sender of peer.getSenders()) lockTrack(sender && sender.track);
      } catch {
        state.errors++;
      }
    }
  }

  // (b) Disable any FUTURE outbound audio track at birth.
  const markerOf = (fn: unknown): boolean => !!(fn as { __vexaAudioLock?: boolean }).__vexaAudioLock;
  const mark = (fn: unknown): void => {
    (fn as { __vexaAudioLock?: boolean }).__vexaAudioLock = true;
  };

  // Zoom-local peer registry, so existing/attached senders can be enumerated and
  // their seals RE-VERIFIED without depending on Meet's cameraEnabled-gated one.
  // Deliberately a Zoom-specific global: writing Meet's name could change Meet's
  // behaviour, and Meet is off-limits.
  try {
    const OrigPC = g.RTCPeerConnection;
    if (typeof OrigPC === 'function' && !markerOf(OrigPC)) {
      if (!Array.isArray(g.__vexa_zoom_peer_connections)) g.__vexa_zoom_peer_connections = [];
      const zoomRegistry = g.__vexa_zoom_peer_connections as unknown[];
      const Ctor = OrigPC as unknown as new (...a: unknown[]) => object;
      const PatchedPC = function (this: unknown, ...args: unknown[]): object {
        const pc = new Ctor(...args);
        try {
          zoomRegistry.push(pc);
        } catch {
          /* registry push must never break peer creation */
        }
        return pc;
      };
      // Preserve prototype + statics so instanceof and RTCPeerConnection.generateCertificate still work.
      (PatchedPC as unknown as { prototype: unknown }).prototype = (OrigPC as { prototype?: unknown }).prototype;
      try {
        for (const k of Object.keys(OrigPC as object)) {
          (PatchedPC as unknown as Record<string, unknown>)[k] = (OrigPC as unknown as Record<string, unknown>)[k];
        }
      } catch {
        /* statics are best-effort */
      }
      mark(PatchedPC);
      g.RTCPeerConnection = PatchedPC;
      state.patchedConstructor = true;
    }
  } catch {
    state.errors++;
  }

  // KILL SWITCH BOUNDARY.
  //
  // What follows is gated on the seal being enabled: the three at-birth patches
  // that disable an outbound audio track the moment it appears. With the switch
  // off we install none of them, leaving Zoom's audio-join path exactly as it was
  // before this change set.
  //
  // The RTCPeerConnection CONSTRUCTOR patch above is deliberately NOT gated. It
  // only appends the peer to an array — it returns the same object and preserves
  // prototype and statics, so nothing Zoom can observe changes; it is the same
  // technique services/screen-content.ts already runs in production for Meet. And
  // it is REQUIRED for the switch-off behaviour to mean anything: with no registry
  // there are no senders to enumerate, so `enabled = false` would have nothing to
  // act on in the default configuration and the bot would fall back to DOM mute
  // alone. Keeping it makes OFF strictly safer than the pre-change state while
  // removing every mechanism that could plausibly interfere with audio-join.
  if (!sealEnabled) {
    result.tracksLocked = state.tracksLocked;
    result.tracksVerified = verified;
    result.errors = state.errors;
    result.patchedConstructor = state.patchedConstructor;
    return result;
  }

  try {
    const pcProto = (g.RTCPeerConnection as { prototype?: Record<string, unknown> } | undefined)?.prototype;
    if (pcProto && typeof pcProto.addTransceiver === 'function' && !markerOf(pcProto.addTransceiver)) {
      const orig = pcProto.addTransceiver as AnyFn;
      const patched = function (this: unknown, ...args: unknown[]): unknown {
        // addTransceiver(trackOrKind, init) — a fourth way an outbound track attaches.
        lockTrack(args[0] as LockableTrack);
        return orig.apply(this, args);
      };
      mark(patched);
      pcProto.addTransceiver = patched;
      state.patchedAddTransceiver = true;
    }
  } catch {
    state.errors++;
  }

  try {
    const pcProto = (g.RTCPeerConnection as { prototype?: Record<string, unknown> } | undefined)?.prototype;
    if (pcProto && typeof pcProto.addTrack === 'function' && !markerOf(pcProto.addTrack)) {
      const orig = pcProto.addTrack as AnyFn;
      const patched = function (this: unknown, ...args: unknown[]): unknown {
        lockTrack(args[0] as LockableTrack); // BEFORE attaching — no transmit window
        return orig.apply(this, args);
      };
      mark(patched);
      pcProto.addTrack = patched;
      state.patchedAddTrack = true;
    }
  } catch {
    state.errors++;
  }

  try {
    const senderProto = (g.RTCRtpSender as { prototype?: Record<string, unknown> } | undefined)?.prototype;
    if (senderProto && typeof senderProto.replaceTrack === 'function' && !markerOf(senderProto.replaceTrack)) {
      const orig = senderProto.replaceTrack as AnyFn;
      const patched = function (this: unknown, ...args: unknown[]): unknown {
        lockTrack(args[0] as LockableTrack);
        return orig.apply(this, args);
      };
      mark(patched);
      senderProto.replaceTrack = patched;
      state.patchedReplaceTrack = true;
    }
  } catch {
    state.errors++;
  }

  try {
    const md = g.navigator && g.navigator.mediaDevices;
    if (md && typeof md.getUserMedia === 'function' && !markerOf(md.getUserMedia)) {
      const orig = md.getUserMedia as AnyFn;
      // getUserMedia returns LOCAL CAPTURE only — a microphone/camera stream the
      // page itself requested. It NEVER yields a remote participant's audio, so
      // locking its audio tracks cannot touch the inbound path the transcript is
      // built from. Verified on this platform: nothing in the Zoom recording path
      // calls getUserMedia (capture is PulseAudio plus the inbound <audio>
      // elements). HAZARD IF THAT CHANGES: if anything is ever added that
      // acquires audio through getUserMedia *in order to record*, this patch
      // would disable that track and silence the recording. Re-check this before
      // introducing any getUserMedia-based capture on the Zoom path.
      const patched = function (this: unknown, ...args: unknown[]): unknown {
        return Promise.resolve(orig.apply(md, args)).then((stream) => {
          try {
            const s = stream as { getAudioTracks?: () => LockableTrack[] } | null;
            if (s && typeof s.getAudioTracks === 'function') {
              for (const t of s.getAudioTracks()) lockTrack(t);
            }
          } catch {
            state.errors++;
          }
          return stream;
        });
      };
      mark(patched);
      md.getUserMedia = patched;
      state.patchedGetUserMedia = true;
    }
  } catch {
    state.errors++;
  }

  result.tracksLocked = state.tracksLocked;
  result.tracksResealed = state.tracksResealed;
  result.tracksVerified = verified;
  result.blockedUnmutes = state.blockedUnmutes;
  result.errors = state.errors;
  result.patchedConstructor = state.patchedConstructor;
  result.patchedAddTrack = state.patchedAddTrack;
  result.patchedAddTransceiver = state.patchedAddTransceiver;
  result.patchedReplaceTrack = state.patchedReplaceTrack;
  result.patchedGetUserMedia = state.patchedGetUserMedia;
  return result;
}

/** Install (or refresh) the outbound-audio lock in the page. Never throws. */
export async function installZoomOutboundAudioLock(
  page: Page,
  voiceAgentEnabled: boolean,
): Promise<OutboundAudioLockResult | null> {
  if (page.isClosed()) return null;
  try {
    return await page.evaluate(installOutboundAudioLockInPage, {
      voiceAgentEnabled,
      sealEnabled: isZoomAudioSealEnabled(),
    });
  } catch {
    return null;
  }
}

/** Format a lock result for the log. Pure, so it is unit-testable. */
export function describeOutboundAudioLock(result: OutboundAudioLockResult): string {
  if (result.skippedVoiceAgent) return 'voice agent enabled — outbound audio intentionally left live and UNLOCKED';
  if (!result.sealEnabled) {
    return `SEAL DISABLED by ZOOM_AUDIO_LOCK — tracksDisabled=${result.tracksLocked} registry=${result.registryPresent ? 'present' : 'ABSENT'} errors=${result.errors} (an unmute CAN succeed)`;
  }
  const patches = [
    result.patchedConstructor ? 'ctor' : null,
    result.patchedAddTrack ? 'addTrack' : null,
    result.patchedAddTransceiver ? 'addTransceiver' : null,
    result.patchedReplaceTrack ? 'replaceTrack' : null,
    result.patchedGetUserMedia ? 'getUserMedia' : null,
  ].filter((p) => p !== null);
  return `tracksLocked=${result.tracksLocked} verified=${result.tracksVerified} resealed=${result.tracksResealed} blockedUnmutes=${result.blockedUnmutes} patched=[${patches.join(',')}] registry=${result.registryPresent ? 'present' : 'ABSENT'} errors=${result.errors}`;
}

/** Persistent-mute-watcher state. Pure data, stepped by stepZoomMuteWatcher. */
export interface ZoomMuteWatcherState {
  /** consecutive CONFIDENT 'unmuted' readings seen so far. */
  consecutiveUnmuted: number;
  lastClickAtMs: number | null;
  clicks: number;
}

export interface ZoomMuteWatcherConfig {
  /** consecutive confident 'unmuted' readings required before clicking. */
  confirmations: number;
  /** minimum gap between two clicks. */
  cooldownMs: number;
}

export interface ZoomMuteWatcherStep {
  state: ZoomMuteWatcherState;
  action: 'click' | 'none';
  reason: string;
}

export const zoomMuteWatcherInitialState: ZoomMuteWatcherState = {
  consecutiveUnmuted: 0,
  lastClickAtMs: null,
  clicks: 0,
};

/**
 * LAYER 3 — decide whether the persistent watcher should re-click mute.
 *
 * WHY a watcher at all: ensureZoomMutedInMeeting runs once at join, but a host
 * can ask a participant to unmute (Zoom even has a pre-approval setting), so the
 * VISUAL half of the guarantee has to be maintained for the whole call.
 *
 * WHY a state machine: a naive "if it looks unmuted, click" watcher becomes a
 * toggle OSCILLATOR — if a click lands but the aria-label lags, the next pass
 * still reads 'unmuted' and clicks again, applying an even number of toggles and
 * leaving the bot unmuted. Two guards prevent that:
 *
 *   1. `confirmations` consecutive CONFIDENT 'unmuted' readings are required.
 *      A lagging label resolves within one or two passes, so it never reaches
 *      the threshold. Any other reading RESETS the counter.
 *   2. `cooldownMs` between clicks, so even sustained misreading cannot produce
 *      a rapid toggle train.
 *
 * 'unknown' and 'not-mute-toggle' NEVER count toward a click and never trigger
 * one — the same asymmetry readZoomMicState uses, for the same reason: clicking
 * on an unidentified control can UNMUTE a bot that was already muted.
 *
 * Pure and synchronous — no DOM, no clock, no I/O. `nowMs` is injected.
 */
export function stepZoomMuteWatcher(
  state: ZoomMuteWatcherState,
  reading: ZoomMicReading['kind'] | 'none',
  nowMs: number,
  config: ZoomMuteWatcherConfig,
): ZoomMuteWatcherStep {
  if (reading !== 'unmuted') {
    // Anything that is not a confident 'unmuted' resets the streak.
    return {
      state: { ...state, consecutiveUnmuted: 0 },
      action: 'none',
      reason: reading === 'muted' ? 'reads muted — nothing to do' : `reading "${reading}" is not actionable`,
    };
  }

  const consecutiveUnmuted = state.consecutiveUnmuted + 1;
  if (consecutiveUnmuted < config.confirmations) {
    return {
      state: { ...state, consecutiveUnmuted },
      action: 'none',
      reason: `unmuted ${consecutiveUnmuted}/${config.confirmations} confirmations`,
    };
  }

  if (state.lastClickAtMs !== null && nowMs - state.lastClickAtMs < config.cooldownMs) {
    return {
      state: { ...state, consecutiveUnmuted },
      action: 'none',
      reason: `cooldown (${nowMs - state.lastClickAtMs}ms < ${config.cooldownMs}ms since last click)`,
    };
  }

  return {
    state: { consecutiveUnmuted: 0, lastClickAtMs: nowMs, clicks: state.clicks + 1 },
    action: 'click',
    reason: `confirmed unmuted ${consecutiveUnmuted}x — re-muting (click #${state.clicks + 1})`,
  };
}

/**
 * Keep the bot LOOKING muted for the whole call: poll the mic control and
 * re-click mute if it is confidently unmuted (a host-requested unmute, say).
 *
 * This maintains only the VISUAL half. Silence is already guaranteed
 * irrevocably by installOutboundAudioLockInPage, which is why this watcher can
 * afford to be conservative and slow rather than aggressive.
 *
 * Unref'd and self-clearing on page close, so it can never delay bot shutdown.
 * Never throws. @returns a stop function (idempotent).
 */
export function startZoomMuteWatcher(
  page: Page,
  voiceAgentEnabled: boolean,
  intervalMs = 15_000,
  config: ZoomMuteWatcherConfig = { confirmations: 2, cooldownMs: 30_000 },
): () => void {
  if (voiceAgentEnabled) {
    log('[Zoom Web] Mute watcher NOT armed — voice agent must be able to transmit');
    return () => { /* nothing armed */ };
  }

  let stopped = false;
  let state = zoomMuteWatcherInitialState;
  // Consecutive passes that found NO readable candidate. An invisible no-op is
  // the worst outcome for a watcher, so this is logged rather than swallowed.
  let noCandidatePasses = 0;

  const stop = (): void => {
    if (stopped) return;
    stopped = true;
    clearInterval(timer);
  };

  const timer = setInterval(() => {
    void (async () => {
      if (stopped) return;
      if (page.isClosed()) {
        stop();
        return;
      }
      try {
        // MUST reveal the footer first. Zoom's toolbar auto-hides, and
        // probeZoomMicCandidates skips zero-area elements — so without this the
        // watcher finds nothing on every pass and silently never fires.
        await revealZoomFooter(page);
        const candidates = await probeZoomMicCandidates(page, zoomMicToggleSelectors);
        const selection = selectZoomMicToggle(candidates);
        if (!selection) {
          noCandidatePasses++;
          // Log the first occurrence, then every 8th pass (~2 min), so a
          // permanently blind watcher is visible without flooding the log.
          if (noCandidatePasses === 1 || noCandidatePasses % 8 === 0) {
            log(`[Zoom Web] Mute watcher found no readable mic control (pass ${noCandidatePasses}) — visual re-mute is INACTIVE; track-level silence unaffected [${describeZoomMicCandidates(candidates)}]`);
          }
        } else if (noCandidatePasses > 0) {
          log(`[Zoom Web] Mute watcher recovered a readable mic control after ${noCandidatePasses} blind pass(es)`);
          noCandidatePasses = 0;
        }
        const step = stepZoomMuteWatcher(state, selection ? selection.reading.kind : 'none', Date.now(), config);
        state = step.state;
        if (step.action === 'click' && selection) {
          await page
            .locator(selection.candidate.selector)
            .nth(selection.candidate.index)
            .click({ timeout: 3000 });
          log(`[Zoom Web] Mute watcher re-muted the bot — ${step.reason} [${selection.reading.evidence}]`);
        }
      } catch (e: any) {
        log(`[Zoom Web] Mute watcher pass failed: ${e?.message ?? e}`);
      }
    })();
  }, intervalMs);

  if (typeof timer.unref === 'function') timer.unref();
  page.once('close', stop);
  return stop;
}
