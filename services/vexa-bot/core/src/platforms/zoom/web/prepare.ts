import { Page, Locator } from 'playwright';
import { BotConfig } from '../../../types';
import { log } from '../../../utils';
// Read-only reuse of the shared repeat collapser. It is imported, never
// modified: this file must not change behaviour for Google Meet or Teams (see
// the ZOOM-LOCAL note above installOutboundAudioLockInPage), and log-throttle
// is a pure, platform-agnostic utility with its own suite.
import { createRepeatCollapser } from '../../../services/log-throttle';
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
  zoomMicLabelNodeSelector,
  zoomMicNotJoinedTextSubstrings,
  zoomMicCaretSelectors,
  zoomMicIconMutedClasses,
  zoomMicIconUnmutedClasses,
  zoomMicIconNotJoinedClasses,
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

/**
 * Classify mute vocabulary in a normalised string, distinguishing a STATE word
 * from an ACTION-OFFERED word.
 *
 * WHY THIS EXISTS — the dangerous defect it fixes: the text tier matched `mute`
 * as a bare substring and read it as "the action offered is mute, therefore the
 * mic is UNMUTED". But visible text is not always an action: Zoom and its
 * relatives also render STATE words. "Muted" and "You are muted" contain
 * `mute`, so they read as `unmuted` -> the watcher clicks -> **a muted bot
 * becomes unmuted**. That is the one direction that actively makes the reported
 * bug worse, and it came from the text tier itself.
 *
 * Order is load-bearing and is the reverse of specificity:
 *   'unmuted' (STATE: mic is live)   -- must precede 'unmute', which it contains
 *   'unmute'  (ACTION offered => currently MUTED)
 *   'muted'   (STATE: mic is muted)  -- must precede 'mute', which it contains
 *   'mute'    (ACTION offered => currently UNMUTED)
 *
 * Every ambiguity therefore resolves toward `muted`, which is the SAFE
 * direction: a wrong `muted` merely declines to click, while a wrong `unmuted`
 * triggers a click that can unmute an already-muted bot.
 *
 * Pure. Input must already be normalised by normaliseZoomMicText.
 */
export function readZoomMuteVocabulary(
  normalised: string,
): { kind: 'muted' | 'unmuted'; word: string; sense: 'state' | 'action' } | null {
  if (!normalised) return null;
  if (normalised.includes('unmuted')) return { kind: 'unmuted', word: 'unmuted', sense: 'state' };
  if (normalised.includes('unmute')) return { kind: 'muted', word: 'unmute', sense: 'action' };
  if (normalised.includes('muted')) return { kind: 'muted', word: 'muted', sense: 'state' };
  if (normalised.includes('mute')) return { kind: 'unmuted', word: 'mute', sense: 'action' };
  return null;
}

/** Attributes read off one candidate mic-control element. */
export interface ZoomMicProbe {
  ariaLabel: string | null;
  ariaPressed: string | null;
  className: string | null;
  /** class attributes of icon/span descendants — Zoom encodes state in the icon. */
  descendantClassNames: string[];
  /**
   * ELEMENT IDENTITY: the `id` when Zoom provides one, else a tag/child-index
   * path up to `body`. Prefixed `#` or `path:` so a reader can tell which.
   *
   * HONEST LIMIT — this is NOT "stable identity, independent of state", which is
   * what this comment used to claim. It is independent of `class` and
   * `aria-label` (both change with the mute state), and that is the property
   * retirement depends on. It is NOT immune to a SIBLING INSERTION within the 12
   * ancestors it walks: an inserted sibling shifts every later child index, so a
   * path can be INHERITED by a different element. selectors.ts records that
   * Zoom's split-button caret "grows only once audio is joined" — exactly such
   * an insertion. Inheritance is the dangerous direction: a fresh element could
   * occupy a retired path and never be selectable.
   *
   * What bounds that: retirement requires TWO proven failures on the same key
   * (so an inherited path must fail twice), and is cleared wholesale if it ever
   * retires every readable control. An `#id` key does not have this weakness at
   * all, which is why it is preferred.
   *
   * WHY identity and not `selector[index]`: several entries in
   * zoomMicToggleSelectors match the SAME button (`button.join-audio-container__btn`
   * and `button[class*="join-audio-container" i]` both do). Rejecting a
   * selector name would leave the identical element selectable under the next
   * name and the discovery loop would click it again forever. It also must not
   * contain the class attribute, because that CHANGES when the mic state
   * changes and a rejection keyed on it would silently stop applying.
   */
  elementKey: string;
  /** the element's own id attribute, if any (also folded into elementKey). */
  id: string | null;
  /**
   * Text of the button's label node — the state WORD a human in the room
   * actually reads off the toolbar. Harvested because in the 2026-09-02 failure
   * aria-label carried no mute vocabulary at all.
   */
  labelText: string | null;
  /** Whole-element textContent, truncated. Fallback for labelText. */
  text: string | null;
  /**
   * class attributes of VISIBLY RENDERED descendants only. A hidden state icon
   * is not what the room sees, so the precise icon-whitelist tier reads this
   * rather than descendantClassNames.
   */
  visibleDescendantClassNames: string[];
  /**
   * Whether a split-button caret was found in the same container — a structural
   * marker of "audio joined". null when it was not probed.
   *
   * REPORTED, NEVER GATED ON: zoomMicCaretSelectors is an unverified guess, and
   * a veto keyed on a selector that never matches would make the watcher
   * permanently blind. It is in the evidence and the digest so one live run can
   * settle it.
   */
  caretNearby: boolean | null;
}

/**
 * Build a complete probe from whatever fields a caller actually has.
 *
 * WHY a factory: ZoomMicProbe grew from 4 fields to 10, and two callers build
 * probes by hand (join.ts's preview read, and the unit tests). Every default is
 * "absent", so a caller that cannot supply a field can never accidentally
 * assert a state through it — and adding a field later cannot silently change
 * an existing caller's reading.
 */
export function makeZoomMicProbe(p: Partial<ZoomMicProbe> = {}): ZoomMicProbe {
  return {
    ariaLabel: p.ariaLabel ?? null,
    ariaPressed: p.ariaPressed ?? null,
    className: p.className ?? null,
    descendantClassNames: p.descendantClassNames ?? [],
    elementKey: p.elementKey ?? '',
    id: p.id ?? null,
    labelText: p.labelText ?? null,
    text: p.text ?? null,
    visibleDescendantClassNames: p.visibleDescendantClassNames ?? [],
    caretNearby: p.caretNearby ?? null,
  };
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
 * Evidence is weighed in descending order of trust. The list below is the
 * ACTUAL order of the branches; it was previously wrong in two ways — it listed
 * aria-pressed twice, and it claimed aria-pressed "outranks the guessed CSS
 * class names below it" while the text tier had been inserted above it.
 *
 *   1. NON-MIC guard over aria-label AND visible text ("Mute All",
 *      "Ask to Unmute"). Returns immediately: clicking one of these would mute
 *      every human in the meeting.
 *   2. aria-label vocabulary, via readZoomMuteVocabulary (state words before
 *      action words). First because aria-label is an explicit accessibility
 *      contract and is the one signal live-confirmed on this control
 *      ("Muted microphone in preview [aria-label \"mute\" offers mute]").
 *   3. VISIBLE-TEXT vocabulary — the label node under the button, else the
 *      element's own rendered text. This is the word a human in the room
 *      actually reads off the toolbar, and in the 2026-09-02 failure aria-label
 *      carried NO mute vocabulary while the weakest tier was left to decide a
 *      state it cannot establish.
 *   4. aria-pressed="true" -> muted. DELIBERATELY BELOW TEXT, not above:
 *      aria-pressed has never been OBSERVED on Zoom's control (the live runs
 *      logged aria-label only), and its polarity for a mic toggle is genuinely
 *      ambiguous — "engaged" can mean "mute is applied" or "microphone is on",
 *      and real apps ship both. A signal that has never been seen and whose
 *      polarity is unconfirmed does not outrank a word rendered on screen.
 *      aria-pressed="false" is NOT read as unmuted, for the same asymmetric
 *      reason throughout this file: a wrong 'unmuted' triggers a CLICK that can
 *      unmute an already-muted bot, a wrong 'muted' merely declines to click.
 *      describeZoomMicCandidates prints aria-pressed so one live run can settle
 *      whether Zoom sets it at all, and with which polarity.
 *   5. FULL icon class name matched against a whitelist, over VISIBLY RENDERED
 *      descendants only. Precise, and it can positively identify the unjoined
 *      control's glyph.
 *   6. legacy class-hint SUBSTRINGS over all descendants — the lowest-confidence
 *      tier, and the one that decided the 2026-09-02 reading on its own. Kept
 *      below the whitelist rather than deleted: its bare-word entries are what
 *      let the reader survive Zoom renaming a glyph (mutation M3), and deleting
 *      them trades a false positive for permanent blindness. The evidence string
 *      names which tier fired, so a substring-only reading is identifiable in
 *      the log as the weak reading it is.
 *   7. Only THEN is a label classified as known-non-state vocabulary ("audio",
 *      "join audio"). This check ranks LAST on purpose: it used to return early,
 *      which meant the live aria-label="audio" element was written off before
 *      aria-pressed or the class hints were ever consulted. An uninformative
 *      LABEL is not an uninformative ELEMENT.
 *
 * NOTE on `caretNearby`: it is folded into the evidence string but never
 * decides a reading. See ZoomMicProbe.caretNearby for why.
 */
export function readZoomMicState(probe: ZoomMicProbe): ZoomMicReading {
  const label = normaliseZoomMicText(probe.ariaLabel);
  // Prefer the dedicated label node; fall back to the element's own text.
  const labelText = normaliseZoomMicText(probe.labelText);
  const text = normaliseZoomMicText(probe.text);
  const visibleText = labelText || text;
  const visibleTextSource = labelText ? 'label node' : 'text';
  // Appended to every evidence string so the joined/unjoined structural marker
  // is always in the log, without ever deciding the reading.
  const caret =
    probe.caretNearby === null ? '' : probe.caretNearby ? ' [caret: yes]' : ' [caret: no]';

  // FIRST, and returning immediately: a control that merely CONTAINS mute
  // vocabulary but is not the mic toggle. Must precede the mute branches below
  // (otherwise "Ask to unmute" reads as muted and "Mute All" as unmuted) AND
  // must not fall through to the class hints (a "Mute All" button can carry its
  // own muted-looking icon class). Checked against the VISIBLE text as well as
  // aria-label — "Mute All" is a text label on some Zoom builds, and a wrapper
  // that swallows several buttons' text is correctly rejected here too.
  for (const [field, value] of [['aria-label', label], [visibleTextSource, visibleText]] as const) {
    if (!value) continue;
    const nonMic = zoomNonMicLabelSubstrings.find((s) => value.includes(s));
    if (nonMic) {
      return { kind: 'not-mic-control', evidence: `${field} "${value}" matches non-mic control "${nonMic}"${caret}` };
    }
  }

  // aria-label, through the shared polarity-correct reader. A label of "Muted"
  // is a STATE, not an action offered, and used to read as unmuted here too.
  const labelVocab = readZoomMuteVocabulary(label);
  if (labelVocab) {
    return {
      kind: labelVocab.kind,
      evidence: `aria-label "${label}" ${labelVocab.sense === 'action' ? `offers ${labelVocab.word}` : `states "${labelVocab.word}"`}${caret}`,
    };
  }

  // VISIBLE TEXT. "Join Audio" identifies the UNJOINED control positively —
  // returned as not-mute-toggle so the watcher never clicks it, and named
  // explicitly rather than lumped in with an unreadable element.
  if (visibleText) {
    const notJoined = zoomMicNotJoinedTextSubstrings.find((s) => visibleText.includes(s));
    if (notJoined) {
      return {
        kind: 'not-mute-toggle',
        evidence: `${visibleTextSource} "${visibleText}" is the UNJOINED audio control ("${notJoined}") — not a mute toggle${caret}`,
      };
    }
    const textVocab = readZoomMuteVocabulary(visibleText);
    if (textVocab) {
      return {
        kind: textVocab.kind,
        evidence: `${visibleTextSource} "${visibleText}" ${textVocab.sense === 'action' ? `offers ${textVocab.word}` : `states "${textVocab.word}"`}${caret}`,
      };
    }
  }

  // A real ARIA state attribute outranks the guessed class names below.
  if (probe.ariaPressed === 'true') {
    return { kind: 'muted', evidence: `aria-pressed="true"${caret}` };
  }

  // PRECISE ICON TIER: full class-name tokens off VISIBLY RENDERED descendants,
  // matched against a whitelist. Unmuted before muted for the same reason the
  // substring tier is ordered that way, and 'not joined' before both because a
  // headset glyph is not a mic state at all.
  const visibleTokens = new Set(
    probe.visibleDescendantClassNames
      .concat(probe.className ? [probe.className] : [])
      .flatMap((c) => normaliseZoomMicText(c).split(' '))
      .filter((t) => t.length > 0),
  );
  const notJoinedIcon = zoomMicIconNotJoinedClasses.find((c) => visibleTokens.has(c));
  if (notJoinedIcon) {
    return {
      kind: 'not-mute-toggle',
      evidence: `visible icon class "${notJoinedIcon}" is the UNJOINED audio control — not a mute toggle${caret}`,
    };
  }
  // BOTH lookups happen BEFORE either return, so the ORDER OF THESE TWO LINES
  // CANNOT AFFECT THE RESULT. That is deliberate: with exact-token matching
  // there is no containment problem to order around, so swapping the lookups
  // was a behaviour-preserving edit for every single-glyph fixture and left the
  // suite green — while, with both glyphs rendered, the order alone decided
  // whether a MUTED bot read `unmuted` and got clicked. Removing the ordering
  // dependency kills that whole mutation class instead of pinning one order.
  const unmutedIcon = zoomMicIconUnmutedClasses.find((c) => visibleTokens.has(c));
  const mutedIcon = zoomMicIconMutedClasses.find((c) => visibleTokens.has(c));
  if (unmutedIcon && mutedIcon) {
    // Genuinely ambiguous: two contradicting state glyphs are both RENDERED.
    // Resolve toward `muted`, on the asymmetric-cost rule used throughout this
    // file — a wrong `unmuted` triggers a click that can unmute an
    // already-muted bot, a wrong `muted` merely declines to click.
    return {
      kind: 'muted',
      evidence: `CONFLICTING visible icon classes "${mutedIcon}" and "${unmutedIcon}" — resolving to muted, the safe direction (a wrong unmuted would trigger a click)${caret}`,
    };
  }
  if (unmutedIcon) {
    return { kind: 'unmuted', evidence: `visible icon class "${unmutedIcon}"${caret}` };
  }
  if (mutedIcon) {
    return { kind: 'muted', evidence: `visible icon class "${mutedIcon}"${caret}` };
  }

  // WEAKEST TIER, kept for rename resilience: substring hints over ALL
  // descendants, visible or not. The evidence says "class hint", which is how a
  // reader tells this apart from the whitelist tier above.
  const classes = normaliseZoomMicText([probe.className, ...probe.descendantClassNames].join(' '));
  const unmutedHint = zoomMicUnmutedClassHints.find((h) => classes.includes(h));
  const mutedHint = zoomMicMutedClassHints.find((h) => classes.includes(h));
  // CONFLICT DETECTION, PER TOKEN — and this tier CANNOT be reordered the way
  // the whitelist above can. 'svgaudiounmuted' CONTAINS 'muted', so a blob-level
  // "both matched" test is true for every unmuted-only control and would make
  // the bot never mute itself (the M3 trap). So a token counts as muted only if
  // that same token is not itself an unmuted token; two DIFFERENT glyph tokens
  // are then a real conflict, resolved toward muted like the tier above.
  const classTokens = classes.split(' ').filter((t) => t.length > 0);
  const unmutedToken = classTokens.find((t) => zoomMicUnmutedClassHints.some((h) => t.includes(h)));
  const mutedToken = classTokens.find(
    (t) => zoomMicMutedClassHints.some((h) => t.includes(h)) && !zoomMicUnmutedClassHints.some((h) => t.includes(h)),
  );
  if (unmutedToken && mutedToken) {
    return {
      kind: 'muted',
      evidence: `CONFLICTING class hints "${mutedToken}" and "${unmutedToken}" — resolving to muted, the safe direction${caret}`,
    };
  }
  if (unmutedHint) {
    return { kind: 'unmuted', evidence: `class hint "${unmutedHint}"${caret}` };
  }
  if (mutedHint) {
    return { kind: 'muted', evidence: `class hint "${mutedHint}"${caret}` };
  }

  // LAST, not first: an uninformative label is not an uninformative element.
  for (const [field, value] of [['aria-label', label], [visibleTextSource, visibleText]] as const) {
    if (!value) continue;
    if (zoomMicNonToggleExactLabels.includes(value) || zoomMicNonToggleSubstrings.some((s) => value.includes(s))) {
      return { kind: 'not-mute-toggle', evidence: `${field} "${value}" carries no mute state${caret}` };
    }
  }

  if (label) return { kind: 'not-mute-toggle', evidence: `aria-label "${label}" unrecognised${caret}` };
  if (visibleText) return { kind: 'not-mute-toggle', evidence: `${visibleTextSource} "${visibleText}" unrecognised${caret}` };
  return { kind: 'unknown', evidence: `no aria-label, no text, no icon class, no aria-pressed${caret}` };
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
 * Pick the first candidate that yields a CONFIDENT reading (muted / unmuted),
 * skipping any element the caller has REJECTED.
 *
 * Candidates that read `not-mute-toggle` or `unknown` are skipped rather than
 * acted on — this is what stops the bot from clicking a control it has not
 * actually identified (the aria-label="audio" element observed live).
 *
 * @param rejected `elementKey`s proven not to be mute toggles — a click landed
 *   on them and the state did not move. This is the CANDIDATE-DISCOVERY half of
 *   the fix: without it the watcher re-clicks the same wrong element forever
 *   (twice in 30s on 2026-09-02, and it would have continued for the whole
 *   call). Keyed on `elementKey`, not `selector[index]`, because several
 *   selectors match the same button — see ZoomMicProbe.elementKey.
 */
export function selectZoomMicToggle(
  candidates: ZoomMicCandidate[],
  rejected: ReadonlySet<string> = new Set(),
): ZoomMicSelection | null {
  for (const candidate of candidates) {
    if (candidate.probe.elementKey && rejected.has(candidate.probe.elementKey)) continue;
    const reading = readZoomMicState(candidate.probe);
    if (reading.kind === 'muted' || reading.kind === 'unmuted') {
      return { candidate, reading };
    }
  }
  return null;
}

/**
 * Read ONE SPECIFIC element out of a probe set, by its stable identity.
 *
 * WHY THIS EXISTS — the defect it fixes: the post-click re-read used
 * selectZoomMicToggle, which returns THE FIRST CANDIDATE THAT READS
 * CONFIDENTLY, not the element that was clicked. probeZoomMicCandidates walks
 * seven selectors and pushes up to four matches each, so the re-probe is a
 * fresh, re-ordered set of up to 28 entries, and the verdict was computed from
 * whichever element happened to sort first and then ATTRIBUTED TO THE CLICK.
 * Two concrete failures followed:
 *
 *   - FALSE 'muted': the click lands on A and does nothing; on re-probe A's
 *     class has changed enough that it no longer reads confidently, and an
 *     earlier-ranked B reads muted. The log then said "CONFIRMED the bot muted
 *     after clicking" — the exact class of untrue claim this change set exists
 *     to delete, now with the word CONFIRMED attached — and A was never
 *     retired, so the loop would repeat forever.
 *   - FALSE 'still-unmuted': the mirror case. The click WORKS on A, but some
 *     other element reads unmuted first, so a WORKING control gets retired.
 *     That is the worse direction: retirement lasts the whole session and could
 *     discard the only candidate that works.
 *
 * Returns the matched candidate with WHATEVER it reads — confident or not — so
 * the caller can report an honest "unreadable" rather than silently
 * substituting a different element. Returns null when the key is empty or the
 * element is absent from the probe set; both are unknowns, never verdicts.
 *
 * Identity is `probe.elementKey`, which excludes `class` and `aria-label`
 * precisely because both CHANGE when the mute state changes — so a successful
 * mute (the common case, and the one that flips the icon class) still matches.
 *
 * Pure — no DOM, no clock, no I/O.
 */
export function readZoomMicCandidateByKey(
  candidates: ZoomMicCandidate[],
  elementKey: string,
): ZoomMicSelection | null {
  if (!elementKey) return null;
  for (const candidate of candidates) {
    if (candidate.probe.elementKey !== elementKey) continue;
    return { candidate, reading: readZoomMicState(candidate.probe) };
  }
  return null;
}

/**
 * One-line digest of everything probed — the diagnostic missing from the failed
 * run. Carries each candidate's EVIDENCE as well as its kind: on 2026-09-02 the
 * live reading came from a descendant class hint ("svgaudiounmuted") rather than
 * from aria-label, and the log did not say so, which left the reading's basis
 * unknowable after the fact.
 */
export function describeZoomMicCandidates(
  candidates: ZoomMicCandidate[],
  rejected: ReadonlySet<string> = new Set(),
): string {
  if (candidates.length === 0) return 'no mic-control candidates matched any selector';
  return candidates
    .map((c) => {
      const reading = readZoomMicState(c.probe);
      const skipped = c.probe.elementKey && rejected.has(c.probe.elementKey) ? ' REJECTED' : '';
      return `${c.selector}[${c.index}]${skipped} key=${c.probe.elementKey || 'none'} aria-label="${c.probe.ariaLabel ?? ''}" label="${c.probe.labelText ?? ''}" aria-pressed=${c.probe.ariaPressed ?? 'absent'} -> ${reading.kind} (${reading.evidence})`;
    })
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
  return page.evaluate(
    (args: { sels: string[]; labelSel: string; caretSels: string[] }) => {
      const { sels, labelSel, caretSels } = args;
      const MAX_PER_SELECTOR = 4;
      const MAX_DESCENDANTS = 8;
      // Scan more than we keep, so the RENDERED filter has something to choose
      // from. Slicing to 8 BEFORE filtering starved the precise whitelist tier
      // whenever a button's first eight [class] descendants were hidden, while
      // the demoted substring tier still saw them — the weaker signal winning
      // by accident of DOM order.
      const MAX_DESCENDANT_SCAN = 40;
      const MAX_TEXT = 120;
      // KNOWN DEBT, recorded deliberately rather than fixed now: 12 ancestors is
      // not necessarily enough to reach `body` for a real Zoom footer button, so
      // two different elements can produce the SAME path key and retiring one
      // control could retire an unrelated one. What bounds the damage: a
      // retirement needs TWO proven failures on that key, and the set is cleared
      // wholesale if it ever retires every readable control — so a collision
      // costs extra cycles, not a permanently blind watcher. An `#id` key has no
      // such weakness, which is why it is preferred. Raising the depth or
      // asserting the path is rooted at body is the real fix.
      const MAX_PATH_DEPTH = 12;

      /**
       * State-INDEPENDENT element identity. The class attribute is deliberately
       * excluded: it changes when the mic state changes, and a rejection keyed
       * on it would silently stop applying to the element it was meant for.
       */
      const identify = (el: Element): string => {
        const id = el.getAttribute('id');
        // Prefixed so the log shows WHICH kind of identity backs a retirement:
        // an id is immune to sibling insertion, a path is not.
        if (id) return `#${id}`;
        const parts: string[] = [];
        let node: Element | null = el;
        let depth = 0;
        while (node && node !== document.body && depth < MAX_PATH_DEPTH) {
          const parent: Element | null = node.parentElement;
          if (!parent) break;
          parts.unshift(`${node.tagName.toLowerCase()}:${Array.from(parent.children).indexOf(node)}`);
          node = parent;
          depth++;
        }
        return parts.length > 0 ? `path:${parts.join('/')}` : '';
      };

      const isRendered = (el: Element): boolean => {
        const r = el.getBoundingClientRect();
        return r.width > 0 || r.height > 0;
      };

      /**
       * Text of an element EXCLUDING descendants that are not rendered.
       *
       * `el.textContent` includes `display:none` subtrees, so a button carrying
       * a hidden "Unmute" span alongside a visible "Mute" one would feed both
       * words to the text tier — which outranks the icon whitelist and feeds the
       * non-mic guard. "Hidden is not what the room sees" was already applied to
       * icon classes; it has to apply to the HIGHER-ranked signal too.
       *
       * HONEST LIMIT: this uses layout boxes, so `visibility:hidden` and
       * `opacity:0` text still counts (both keep a box). Only display:none /
       * zero-area subtrees are excluded.
       */
      const renderedTextOf = (root: Element): string => {
        if (!isRendered(root)) return '';
        let out = '';
        const walk = (node: Node): void => {
          const children = Array.from(node.childNodes);
          for (const child of children) {
            if (out.length >= MAX_TEXT) return;
            if (child.nodeType === 3) {
              out += child.nodeValue ?? '';
              continue;
            }
            if (child.nodeType !== 1) continue;
            const el = child as Element;
            if (!isRendered(el)) continue; // display:none / zero-area subtree
            walk(el);
          }
        };
        walk(root);
        return out.slice(0, MAX_TEXT);
      };

      const out: ZoomMicCandidate[] = [];
      for (const selector of sels) {
        let nodes: Element[] = [];
        try {
          nodes = Array.from(document.querySelectorAll(selector));
        } catch {
          continue; // selector not supported by this browser — try the next
        }
        nodes.slice(0, MAX_PER_SELECTOR).forEach((el, index) => {
          if (!isRendered(el)) return; // hidden — not clickable
          const scanned = Array.from(el.querySelectorAll('[class]')).slice(0, MAX_DESCENDANT_SCAN);
          const descendantClassNames = scanned.slice(0, MAX_DESCENDANTS).map((d) => d.getAttribute('class') ?? '');
          // Visible-only, for the precise icon-whitelist tier: a hidden state
          // icon is not what the room sees. FILTER THEN SLICE (see
          // MAX_DESCENDANT_SCAN) — the reverse order starved this tier.
          const visibleDescendantClassNames = scanned
            .filter(isRendered)
            .slice(0, MAX_DESCENDANTS)
            .map((d) => d.getAttribute('class') ?? '');

          let labelText: string | null = null;
          try {
            const labelNode = el.querySelector(labelSel);
            if (labelNode) labelText = renderedTextOf(labelNode);
          } catch {
            /* unsupported selector — fall back to the element's own text */
          }

          // Split-button caret in the SAME container. Harvested only; nothing
          // in readZoomMicState gates on it (the selectors are unverified).
          let caretNearby: boolean | null = null;
          const container = el.parentElement;
          if (container) {
            caretNearby = false;
            for (const cs of caretSels) {
              try {
                const hit = container.querySelector(cs);
                if (hit && isRendered(hit)) {
                  caretNearby = true;
                  break;
                }
              } catch {
                /* unsupported selector — try the next */
              }
            }
          }

          out.push({
            selector,
            index,
            probe: {
              ariaLabel: el.getAttribute('aria-label'),
              ariaPressed: el.getAttribute('aria-pressed'),
              className: el.getAttribute('class'),
              descendantClassNames,
              elementKey: identify(el),
              id: el.getAttribute('id'),
              labelText,
              text: renderedTextOf(el),
              visibleDescendantClassNames,
              caretNearby,
            },
          });
        });
      }
      return out;
    },
    { sels: selectors, labelSel: zoomMicLabelNodeSelector, caretSels: zoomMicCaretSelectors },
  );
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
 * This is best-effort by nature — it depends on Zoom's DOM. What keeps the bot
 * silent is installOutboundAudioLockInPage, armed at PAGE LOAD from join.ts and
 * re-asserted by startZoomOutboundAudioGuard (layer 2); neither reads the DOM.
 * The guard is the backstop, not the primary mechanism — the earlier wording
 * here credited it with the whole guarantee, which is wrong. Never throws.
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

        // SAME DEFECT, SAME FIX. This re-read also used selectZoomMicToggle
        // over a fresh probe, so it could report "Muted microphone in meeting"
        // and return muted:true on the strength of a DIFFERENT element than the
        // one it clicked. Pin it to the clicked element's identity.
        const afterProbe = await probeZoomMicCandidates(page, zoomMicToggleSelectors);
        const after = readZoomMicCandidateByKey(afterProbe, selection.candidate.probe.elementKey);
        if (after && after.reading.kind === 'muted') {
          log(`[Zoom Web] Muted microphone in meeting (recorder bot — receive-only) [same element key=${selection.candidate.probe.elementKey}: ${after.reading.evidence}]`);
          return { muted: true, clicked: true, attempts: attempt + 1, detail: after.reading.evidence };
        }
        log(`[Zoom Web] Mute click not yet confirmed — the CLICKED element (key=${selection.candidate.probe.elementKey || 'none'}) reads ${after ? after.reading.kind : 'absent from the re-probe'} — re-checking`);
      }
    } catch (e: any) {
      log(`[Zoom Web] ensureZoomMutedInMeeting attempt ${attempt + 1} error: ${e?.message ?? e}`);
    }
    await page.waitForTimeout(5000);
  }

  log(`[Zoom Web] WARNING: could not confirm in-meeting mute after retries — the participant list may show this bot as unmuted. The track-level seal is unaffected by this; read the outbound-audio guard heartbeat for its actual state. Last probe: [${lastDetail}]`);
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
 * than trusting Zoom's DOM, disable every OUTBOUND audio track directly — so a
 * rotted selector in this file cannot make the bot audible. The honest limit:
 * this sweep can only act on peers it can SEE, so a false `registryPresent` is
 * reported rather than treated as "nothing to do", and the caller must read it.
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

/** What Chromium is actually offered on the SEND side. */
export interface ZoomSendSideProbe {
  /** number of audioinput devices enumerateDevices() reports. */
  audioInputDevices: number;
  /** enumerateDevices threw / was unavailable. */
  error: string | null;
}

/**
 * Report whether Chromium was offered a MICROPHONE at all.
 *
 * WHY THIS EXISTS: `AUDIO JOIN OK` counts `document.querySelectorAll('audio')`
 * elements with live MediaStreams — those are INBOUND. It printed identically on
 * the run that failed and will print identically whether or not the capture
 * device exists, so it is a canary for the RECORDING and for nothing else. The
 * start.sh microphone fix has no observable signal without this.
 *
 * Runs as a plain page.evaluate: no patches, no track access, nothing installed.
 * That is deliberate — it must work in mode 'none' too, and it must not be the
 * thing an operator is trying to rule out.
 *
 * Never throws.
 */
export async function probeZoomSendSide(page: Page): Promise<ZoomSendSideProbe | null> {
  if (page.isClosed()) return null;
  try {
    return await page.evaluate(async () => {
      try {
        const md = navigator.mediaDevices;
        if (!md || typeof md.enumerateDevices !== 'function') {
          return { audioInputDevices: 0, error: 'navigator.mediaDevices.enumerateDevices unavailable' };
        }
        const devices = await md.enumerateDevices();
        return {
          audioInputDevices: devices.filter((d) => d.kind === 'audioinput').length,
          error: null,
        };
      } catch (e) {
        return { audioInputDevices: 0, error: String((e as Error)?.message ?? e) };
      }
    });
  } catch {
    return null;
  }
}

/**
 * Format the send-side probe. Pure, so it is unit-testable.
 *
 * States plainly which counters are and are not meaningful in the current mode,
 * because two of them are STRUCTURALLY ZERO in mode 'off' — the mode the first
 * live run uses — and an operator staring at a zero that can never be anything
 * else is worse off than one told it is unavailable.
 */
export function describeZoomSendSide(probe: ZoomSendSideProbe | null, mode: ZoomAudioLockMode): string {
  const modeNote =
    mode === 'on'
      ? 'mode=on (tracksLocked, audioSenders and blockedUnmutes are all meaningful)'
      : mode === 'off'
        // WAS FACTUALLY WRONG, and this file contradicted itself: the old text
        // claimed "the peer registry is not installed, so audioSenders reads
        // unreadable". The RTCPeerConnection ctor patch sits ABOVE the
        // kill-switch boundary and is deliberately NOT gated (see the KILL
        // SWITCH BOUNDARY comment), so in mode off the registry IS populated,
        // registryPresent is true, and audioSenders is a live number. The note
        // therefore told the operator to discard the informative signal and
        // rely on the weaker one — worse than an uninformative line, because it
        // sat in the same sentence as a true statement about tracksLocked.
        ? 'mode=off — NOTE: tracksLocked is STRUCTURALLY 0 here (lockTrack returns before the counter), but the peer registry IS installed (the ctor patch sits above the kill-switch boundary), so audioSenders and sweepDisabled are LIVE and meaningful — they are what shows Zoom ATTACHED the mic to a sender, as against merely being offered a device'
        : 'mode=none — NOTE: the guard is not armed, so there is NO heartbeat at all; this one-shot line is the only send-side evidence you will get';
  if (!probe) return `audioInputDevices=unreadable (probe could not run) | ${modeNote}`;
  if (probe.error) return `audioInputDevices=unreadable (${probe.error}) | ${modeNote}`;
  return probe.audioInputDevices > 0
    ? `audioInputDevices=${probe.audioInputDevices} — a CAPTURE DEVICE IS OFFERED to Chromium (the start.sh microphone fix is in force) | ${modeNote}`
    : `audioInputDevices=0 — NO CAPTURE DEVICE: Chromium has no microphone, so Zoom cannot join audio for SENDING and will have no mute state to describe. This is the 2026-09-02 root cause; check the start.sh remap-source block | ${modeNote}`;
}

/** One guard tick reduced to a loggable line. Pure, so it is unit-testable. */
export interface ZoomAudioGuardTickReport {
  /**
   * Fingerprint of the situation. The collapser emits IMMEDIATELY when this
   * changes, so a state transition is never delayed by the heartbeat.
   */
  signature: string;
  /** The line to log, with no platform prefix and no repeat accounting. */
  message: string;
  /** true when the tick found something that should not be happening. */
  warn: boolean;
}

/**
 * Reduce one guard tick (a lock re-assert plus an observability sweep) to the
 * line the log should carry.
 *
 * WHY this exists: the previous tick logged only when `tracksLocked` or
 * `blockedUnmutes` had grown, so a tick where nothing changed was completely
 * SILENT. Over the live 5-minute meeting of 2026-09-02 the guard therefore
 * logged exactly once — the install at 06:45:19 — and nothing whatsoever could
 * be said about the ~30 ticks that followed. A silent mechanism on a
 * privacy-critical path cannot be trusted or debugged, so every tick now
 * produces a line, which `createRepeatCollapser` throttles to a heartbeat while
 * the situation is unchanged and emits at once when it moves.
 *
 * `audioSenders` comes from the sweep because `OutboundAudioLockResult` does not
 * carry it, and it is the single most load-bearing number here: on 2026-09-02 it
 * was 0 for the whole meeting. This function reports that count as a FACT and
 * deliberately does NOT classify it — whether a bot with no outbound audio
 * sender at all can be muted via Zoom's toolbar is under separate investigation,
 * and prejudging it here is exactly the kind of claim this change exists to stop.
 *
 * Pure — no clock, no DOM, no I/O.
 */
export function reportZoomAudioGuardTick(
  lock: OutboundAudioLockResult | null,
  sweep: OutboundAudioSweepResult | null,
): ZoomAudioGuardTickReport {
  if (!lock) {
    return {
      signature: 'lock-unavailable',
      message: 'Outbound audio guard tick could not read the lock (page closed or execution context destroyed)',
      warn: true,
    };
  }
  if (lock.skippedVoiceAgent) {
    // Not reachable from the armed guard (it returns early for a voice agent),
    // but reported honestly rather than mislabelled if it ever is.
    return {
      signature: 'voice-agent',
      message: `Outbound audio guard tick — ${describeOutboundAudioLock(lock)}`,
      warn: false,
    };
  }

  // Sweep detail. A sweep that DISABLED something means a live outbound audio
  // track existed which the lock had not sealed — the one case this backstop
  // exists for, and never silent again.
  const sweepDetail = !sweep
    ? 'audioSenders=unreadable (sweep could not run)'
    : !sweep.registryPresent
      ? 'audioSenders=unreadable (peer-connection registry ABSENT)'
      : `audioSenders=${sweep.audioSendersFound} sweepDisabled=${sweep.tracksDisabled} sweepAlreadyDisabled=${sweep.alreadyDisabled} sweepErrors=${sweep.errors}`;

  const warn =
    !lock.sealEnabled ||
    !lock.registryPresent ||
    lock.errors > 0 ||
    !sweep ||
    !sweep.registryPresent ||
    sweep.tracksDisabled > 0 ||
    sweep.errors > 0;

  return {
    // Every counter that can move is in the signature, so any real change emits
    // on the very next tick instead of waiting up to a heartbeat.
    signature: [
      lock.sealEnabled,
      lock.registryPresent,
      lock.tracksLocked,
      lock.tracksVerified,
      lock.tracksResealed,
      lock.blockedUnmutes,
      lock.errors,
      lock.patchedConstructor,
      lock.patchedAddTrack,
      lock.patchedAddTransceiver,
      lock.patchedReplaceTrack,
      lock.patchedGetUserMedia,
      sweep ? sweep.registryPresent : 'no-sweep',
      sweep ? sweep.audioSendersFound : -1,
      sweep ? sweep.tracksDisabled : -1,
      sweep ? sweep.errors : -1,
    ].join('|'),
    message: `Outbound audio guard heartbeat — ${describeOutboundAudioLock(lock)} ${sweepDetail}`,
    warn,
  };
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
 * OBSERVABILITY (fixed 2026-09-02): every tick now produces a line via
 * reportZoomAudioGuardTick, throttled by a repeat collapser to roughly one
 * heartbeat per `heartbeatMs` while the counters are unchanged and emitted
 * immediately when they move. The previous "only log a change" rule made a
 * healthy guard indistinguishable from a dead one — across a real 5-minute
 * meeting it logged once. `stop()` flushes the collapser so the final suppressed
 * run is accounted for rather than lost.
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
  heartbeatMs = 60_000,
): () => void {
  if (voiceAgentEnabled) {
    log('[Zoom Web] Outbound audio guard NOT armed — voice agent must transmit TTS');
    return () => { /* nothing armed */ };
  }
  // Hands-off mode: the guard re-installs the lock and sweeps tracks, both of
  // which touch tracks, so it must not run. Checked HERE as well as at the call
  // site — 'none' has to be a property of the machinery, not of one caller.
  if (!isZoomAudioTouchAllowed()) {
    log('[Zoom Web] Outbound audio guard NOT armed — ZOOM_AUDIO_LOCK hands-off mode (no track is touched; no heartbeat will be emitted)');
    return () => { /* nothing armed */ };
  }

  let stopped = false;
  // One stream, keyed below; the collapser turns "every tick" into "a heartbeat
  // plus every transition", which is what makes a healthy guard visible.
  const heartbeat = createRepeatCollapser(heartbeatMs);
  const emit = (line: string, warn: boolean): void =>
    log(warn ? `[Zoom Web] WARNING: ${line}` : `[Zoom Web] ${line}`);

  // The warn level of the most recent tick, so the shutdown flush does not
  // downgrade a suppressed WARNING to informational on the very last line
  // before teardown — which is the line most likely to be read.
  let lastWarn = false;
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
      // Sweep EVERY tick, not just when lock.errors > 0. Two reasons: it is the
      // belt-and-braces disable for a track that could not be sealed, and it is
      // the only source of audioSenders — the number that was missing from the
      // 2026-09-02 log and the one a reader most needs.
      const sweep = await sweepZoomOutboundAudio(page, voiceAgentEnabled);
      const report = reportZoomAudioGuardTick(lock, sweep);
      lastWarn = report.warn;
      const line = heartbeat.consider('zoom-audio-guard', report.signature, report.message, Date.now());
      if (line) emit(line, report.warn);
    })();
  }, intervalMs);

  // Never keep the process alive for this guard.
  if (typeof timer.unref === 'function') timer.unref();

  const stop = (): void => {
    if (stopped) return;
    stopped = true;
    clearInterval(timer);
    // Account for whatever the heartbeat was still suppressing, so the last run
    // of ticks before shutdown is not silently discarded.
    const tail = heartbeat.flush('zoom-audio-guard', Date.now());
    if (tail) emit(tail, lastWarn);
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

/**
 * The three operating modes of the outbound-audio machinery.
 *
 *   'on'   — default. Tracks are disabled AND sealed; the at-birth patches are
 *            installed; the guard is armed.
 *   'off'  — the original kill switch, behaviour UNCHANGED: no seal, no
 *            at-birth patches, but tracks are still written `enabled = false`
 *            (lockTrack does that write BEFORE consulting the switch) and the
 *            guard still runs. An unmute CAN succeed.
 *   'none' — HANDS OFF. Nothing is installed and no track is ever touched, in
 *            the page or from Node. Only the DOM mute path remains.
 */
export type ZoomAudioLockMode = 'on' | 'off' | 'none';

/** Spellings that select hands-off mode. Normalised (trimmed, lowercased). */
const zoomAudioLockNoneValues = ['none', 'hands-off', 'handsoff', 'no-touch', 'notouch'];

/**
 * Parse the env var into a MODE.
 *
 * WHY this is separate from parseZoomAudioLockEnv rather than replacing it:
 * that function's boolean contract is depended on by the seal itself and is
 * pinned by mutations M37/M37b/M37c, and 'off' must keep behaving EXACTLY as it
 * does today. So hands-off is recognised FIRST and everything else delegates —
 * which also means `parseZoomAudioLockEnv('none')` still returns true on its
 * own. That is a trap: read the MODE, never the boolean, when deciding whether
 * to touch a track.
 */
export function parseZoomAudioLockMode(raw: string | undefined): ZoomAudioLockMode {
  const v = (raw ?? '').trim().toLowerCase();
  if (zoomAudioLockNoneValues.includes(v)) return 'none';
  return parseZoomAudioLockEnv(raw) ? 'on' : 'off';
}

/** Memoised so the decision is read once per process and logged exactly once. */
let zoomAudioSealDecision: ZoomAudioLockMode | null = null;

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
 * WHY 'none' EXISTS AS WELL: 'off' removes the lying getter and the at-birth
 * patches, but lockTrack writes `track.enabled = false` BEFORE it consults the
 * switch — so 'off' still MUTATES the track. If Zoom's audio join turns out to
 * be upset by the write rather than by the unreadable getter, 'off' cannot
 * isolate that, and `voiceAgentEnabled` is far too blunt a substitute (it also
 * changes preview mute and arms neither guard nor watcher). 'none' is the
 * genuine touch-nothing configuration that makes a live incident isolable
 * instead of guessable. It leaves the bot's silence resting on the start.sh
 * microphone topology — the mic is fed by a null sink nothing ever plays into,
 * so it is silent BY CONSTRUCTION, with the source mute as defence in depth.
 *
 * LIVE-RUN ORDER: 'off' FIRST, then 'on'. NOT 'none' — 'none' refuses to arm the
 * outbound-audio guard, so it emits no heartbeat at all and blinds the very
 * instrument added to observe this. What 'none' gives up, explicitly: the guard
 * heartbeat, audioSenders, blockedUnmutes, and every at-birth patch. Use it only
 * to answer "does OUR code break Zoom's audio join?", and read the one-shot
 * send-side line (describeZoomSendSide) for evidence in that mode.
 *
 * Defaults to ON. Logged once, plainly, at arm time: a silent kill switch is
 * worse than none — if anyone later asks "was the lock on during that meeting?",
 * the log has to answer.
 */
export function zoomAudioLockMode(): ZoomAudioLockMode {
  if (zoomAudioSealDecision !== null) return zoomAudioSealDecision;
  const raw = process.env.ZOOM_AUDIO_LOCK;
  zoomAudioSealDecision = parseZoomAudioLockMode(raw);
  log(
    zoomAudioSealDecision === 'on'
      ? `[Zoom Web] Outbound audio SEAL: ENABLED (default) [ZOOM_AUDIO_LOCK=${raw ?? '<unset>'}]`
      : zoomAudioSealDecision === 'off'
        ? `[Zoom Web] Outbound audio SEAL: DISABLED by operator [ZOOM_AUDIO_LOCK=${raw ?? '<unset>'}] — tracks are still set enabled=false and the DOM mute path is unchanged, but an unmute CAN succeed`
        : `[Zoom Web] Outbound audio SEAL: HANDS OFF by operator [ZOOM_AUDIO_LOCK=${raw ?? '<unset>'}] — NOTHING is installed and no track is touched; silence rests on the start.sh mic topology (a null sink nothing plays into, plus its source mute), and an unmute CAN succeed. GIVES UP: the guard is not armed, so there is NO heartbeat, no audioSenders and no blockedUnmutes. For a first live run prefer ZOOM_AUDIO_LOCK=off, which keeps the heartbeat`,
  );
  return zoomAudioSealDecision;
}

/** True only in mode 'on'. This is what the in-page seal is gated on. */
export function isZoomAudioSealEnabled(): boolean {
  return zoomAudioLockMode() === 'on';
}

/**
 * False ONLY in hands-off mode. Every site that installs a patch or writes to a
 * track must consult this first, so 'none' is a property of the whole machinery
 * rather than of one function that could be bypassed.
 */
export function isZoomAudioTouchAllowed(): boolean {
  return zoomAudioLockMode() !== 'none';
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
    // `tracksLocked` is STRUCTURALLY 0 in this mode: lockTrack returns before
    // the counter increments, even though it has already written
    // enabled = false. Labelling it "tracksDisabled" was simply wrong — it
    // reported 0 for tracks that WERE disabled.
    return `SEAL DISABLED by ZOOM_AUDIO_LOCK — tracksLocked=${result.tracksLocked} (STRUCTURALLY 0 in this mode; tracks ARE still written enabled=false, the counter just never runs) registry=${result.registryPresent ? 'present' : 'ABSENT'} errors=${result.errors} (an unmute CAN succeed)`;
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
 * THE SHIPPED watcher config. Named and exported so the values that actually
 * run can be asserted.
 *
 * WHY: stepZoomMuteWatcher is tested exhaustively with INJECTED configs, so
 * every anti-oscillator property was proven — for configs that never ship.
 * Replacing this default with `{ confirmations: 1, cooldownMs: 0 }` survived
 * the whole suite while restoring the toggle oscillator the state machine
 * exists to prevent.
 *
 * It also carries a load-bearing relationship: ZOOM_RETIREMENT_REQUIRED_STRIKES
 * is justified by "each strike costs a full watcher cycle (~15s poll +
 * cooldown)". That rationale rests on `cooldownMs` being large, so a pinned
 * constant was resting on an unpinned one. Both are pinned now.
 */
export const zoomMuteWatcherDefaultConfig: ZoomMuteWatcherConfig = {
  confirmations: 2,
  cooldownMs: 30_000,
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

/** Timing for the post-click settle poll. All injected, so it is testable. */
export interface ZoomMutePollConfig {
  /** wait before the FIRST re-read (Zoom needs a beat to repaint). */
  firstDelayMs: number;
  /** gap between subsequent re-reads. */
  intervalMs: number;
  /** total budget from the click; a reading still unmuted at this point is a proven failure. */
  deadlineMs: number;
}

export const zoomMutePollDefaults: ZoomMutePollConfig = {
  firstDelayMs: 500,
  intervalMs: 500,
  deadlineMs: 4_000,
};

export interface ZoomMutePollResult {
  /** the CLICKED element's reading, or null if it was never readable. */
  after: ZoomMicSelection | null;
  /** how many re-probes were performed. */
  polls: number;
  /** ms after the click at which `muted` was first observed, else null. */
  settledAtMs: number | null;
  /** true when the budget ran out without ever reading `muted`. */
  timedOut: boolean;
  /** the candidate set from the LAST probe, for the diagnostic digest. */
  lastCandidates: ZoomMicCandidate[];
}

/**
 * Poll for the mute to SETTLE, instead of sampling once and calling it.
 *
 * WHY THIS EXISTS — the defect it fixes: verification took ONE reading at a
 * fixed +750ms after the click, and a reading of `unmuted` there was treated as
 * a proven failure that RETIRED the candidate permanently. But Zoom's mute is an
 * audio-session operation acknowledged by the server, not a local CSS toggle; on
 * a loaded pod or a busy meeting the toolbar reflects it later than 750ms. The
 * re-read then sampled the PRE-CLICK state and the only control that actually
 * mutes the bot was retired for the whole call. Before retirement existed that
 * same timing produced a useless-but-harmless re-click loop; with retirement it
 * became terminal. A state that has not arrived is not a state that will not
 * arrive.
 *
 * So: re-probe until the clicked element reads `muted` (return at once), or the
 * budget expires. Only a reading still `unmuted` AT THE DEADLINE is a proven
 * failure — and even then it is only ONE strike (see stepZoomRetirement).
 *
 * The prober, the clock and the sleep are all INJECTED, which is what makes the
 * timing behaviour unit-testable rather than a source assertion. `probeOnce`
 * must never throw; the caller wraps the Playwright call.
 */
export async function pollZoomMuteSettled(
  probeOnce: () => Promise<ZoomMicCandidate[]>,
  clickedKey: string,
  config: ZoomMutePollConfig,
  now: () => number,
  sleep: (ms: number) => Promise<void>,
): Promise<ZoomMutePollResult> {
  const startedAt = now();
  let polls = 0;
  let after: ZoomMicSelection | null = null;
  let lastCandidates: ZoomMicCandidate[] = [];
  // HARD ITERATION CAP, independent of the clock. The deadline check is the
  // normal exit; this is the backstop for the case where it cannot fire — a
  // clock that does not advance, or a future edit that breaks the comparison.
  // This loop runs inside a setInterval callback in a live bot, so "loops
  // forever" is not an acceptable failure mode for any edit to reach.
  const maxPolls = Math.max(1, Math.ceil(config.deadlineMs / Math.max(1, config.intervalMs)) + 2);

  await sleep(config.firstDelayMs);
  for (;;) {
    lastCandidates = await probeOnce();
    polls++;
    const reading = readZoomMicCandidateByKey(lastCandidates, clickedKey);
    // Keep the most informative reading seen: a later "absent" must not erase
    // an earlier confident one.
    if (reading) after = reading;
    if (reading && reading.reading.kind === 'muted') {
      return { after: reading, polls, settledAtMs: now() - startedAt, timedOut: false, lastCandidates };
    }
    if (now() - startedAt >= config.deadlineMs || polls >= maxPolls) {
      return { after, polls, settledAtMs: null, timedOut: true, lastCandidates };
    }
    await sleep(config.intervalMs);
  }
}

/** What a post-click re-read actually established. */
export type ZoomMuteClickVerdict = 'muted' | 'still-unmuted' | 'unreadable';

export interface ZoomMuteClickReport {
  verdict: ZoomMuteClickVerdict;
  /** true for every verdict except a confirmed mute. */
  warn: boolean;
  /** The line to log, with no platform prefix. */
  message: string;
  /**
   * `elementKey` to stop selecting for the rest of the session, or null.
   *
   * Set ONLY for a `still-unmuted` verdict — a click that landed and provably
   * did not change the state. `unreadable` is unknown, not a proven failure,
   * and must never blacklist a candidate that might be the right one. Null when
   * the probe could not produce an identity, since rejecting an empty key would
   * match every unidentified element.
   */
  rejectKey: string | null;
}

/** Everything the post-click report needs. `after` is the RE-READ, not the trigger. */
export interface ZoomMuteClickInput {
  /** stepZoomMuteWatcher's reason for clicking (carries the click number). */
  reason: string;
  /** The candidate + reading that triggered the click. */
  before: ZoomMicSelection;
  /** The confident reading taken AFTER the click, or null if none was available. */
  after: ZoomMicSelection | null;
  /** Full probe digest of the post-click pass — only used when something is wrong. */
  afterDetail: string;
  /** Clicks already proven ineffective before this one. */
  priorIneffectiveClicks: number;
}

/**
 * Turn a click plus its re-read into an HONEST log line.
 *
 * WHY this exists: the watcher used to log `Mute watcher re-muted the bot` from
 * the line immediately after `.click()`, with no re-read at all. That string is
 * a claim about the meeting, not an observation of it, and on 2026-09-02 it was
 * emitted twice (06:49:35 and 06:50:05) while the control kept reading
 * `svgaudiounmuted` and the user ended up muting the bot by hand from the
 * participant list. Click #1 demonstrably did not mute, and nothing in the
 * system noticed — the log asserted the opposite.
 *
 * The clicked candidate's selector and index are now in the line too. Previously
 * only `reading.evidence` was logged, so which of the seven candidate selectors
 * was actually clicked could not be recovered from the log.
 *
 * Pure — no DOM, no clock, no I/O.
 */
export function reportZoomMuteClick(input: ZoomMuteClickInput): ZoomMuteClickReport {
  const target = `${input.before.candidate.selector}[${input.before.candidate.index}]`;
  const clickedKey = input.before.candidate.probe.elementKey || 'none';
  const trigger = `${input.reason}; clicked ${target} key=${clickedKey} on [${input.before.reading.evidence}]`;
  // Every verdict says WHICH element the after-reading describes. Without this a
  // reader cannot tell whether the verdict refers to the clicked element at all
  // — which is exactly how a reading taken from a different element was able to
  // masquerade as a confirmed mute.
  const sameElement = `re-read of the SAME element (key=${clickedKey})`;

  if (input.after && input.after.reading.kind === 'muted') {
    return {
      verdict: 'muted',
      warn: false,
      rejectKey: null,
      message: `Mute watcher CONFIRMED the bot muted after clicking — ${trigger}; ${sameElement} [${input.after.reading.evidence}]`,
    };
  }

  if (input.after && input.after.reading.kind === 'unmuted') {
    const ineffective = input.priorIneffectiveClicks + 1;
    const key = input.before.candidate.probe.elementKey;
    // CANDIDATE DISCOVERY. A click landed and the state did not move, so this
    // element is not the mute toggle in this state. Rejecting it converts the
    // futile re-click loop (twice in 30s on 2026-09-02, and it would have run
    // for the whole call) into a fall-through to the NEXT candidate.
    return {
      verdict: 'still-unmuted',
      warn: true,
      rejectKey: key || null,
      message:
        `Mute watcher clicked mute and the control STILL reads unmuted — the click had NO EFFECT ` +
        `(${ineffective} ineffective click(s) so far) — ${trigger}; ${sameElement} [${input.after.reading.evidence}]; ` +
        (key
          ? `this element is now REJECTED for the session (key=${key}) — the next pass falls through to the next candidate; `
          : `it could NOT be rejected (the probe produced no element identity), so the next pass may pick it again; `) +
        `post-click probe [${input.afterDetail}]`,
    };
  }

  // UNREADABLE. Two distinct causes, and a reader needs to know which:
  //   - `after` is null: the clicked element was ABSENT from the re-probe (or
  //     the verification itself failed, which afterDetail spells out).
  //   - `after` is present but reads neither muted nor unmuted: the element WAS
  //     found and says nothing useful.
  // Both are honest unknowns. Neither may retire a candidate — the clicked
  // element might well be the only one that works.
  const why = input.after
    ? `the clicked element re-read as "${input.after.reading.kind}" [${input.after.reading.evidence}]`
    : `the clicked element (key=${clickedKey}) was NOT FOUND in the post-click probe`;
  return {
    verdict: 'unreadable',
    warn: true,
    rejectKey: null,
    message:
      `Mute watcher clicked mute but could NOT verify the result — ${why}, ` +
      `so whether the bot is muted is UNKNOWN and the candidate is NOT rejected — ${trigger}; ` +
      `post-click probe [${input.afterDetail}]`,
  };
}

/** Retirement bookkeeping. Pure data, stepped by stepZoomRetirement. */
export interface ZoomRetirementState {
  /** proven-ineffective clicks per element key, since the last reset. */
  strikes: Record<string, number>;
  /** keys currently retired from selection. */
  retired: string[];
  /** how many times the whole set has been cleared (an absorbing state broken). */
  resets: number;
}

export const zoomRetirementInitialState: ZoomRetirementState = { strikes: {}, retired: [], resets: 0 };

export interface ZoomRetirementStep {
  state: ZoomRetirementState;
  action: 'none' | 'strike' | 'retire';
  reason: string;
}

/**
 * Default corroboration. TWO proven-ineffective clicks on the SAME element
 * before it is retired.
 *
 * WHY 2 and not 1: retirement is the only irreversible-ish decision in this
 * file, and a single proven failure can still be a timing artefact that the
 * settle poll did not outlast (a pod paused long enough for a 4s budget to
 * expire, a mid-meeting re-render). One flake must not be terminal. WHY not
 * higher: each strike costs a full watcher cycle (~15s poll + cooldown 30s), so
 * 3 strikes would leave a genuinely wrong candidate in play for well over a
 * minute of a call that is often only a few minutes long.
 */
export const ZOOM_RETIREMENT_REQUIRED_STRIKES = 2;

/**
 * Record a PROVEN-ineffective click and decide whether to retire the element.
 *
 * Fail-safes, both deliberate and both previously present:
 *   - an EMPTY key is never recorded and never retired: '' is the ambient
 *     elementKey, so putting it in the set would match every unidentified
 *     element and silence the watcher wholesale.
 *   - only a PROVEN failure reaches here. `unreadable` is an unknown and the
 *     caller must not call this for it.
 *
 * Pure.
 */
export function stepZoomRetirement(
  state: ZoomRetirementState,
  key: string,
  requiredStrikes: number = ZOOM_RETIREMENT_REQUIRED_STRIKES,
): ZoomRetirementStep {
  if (!key) {
    return { state, action: 'none', reason: 'no element identity — nothing can be recorded or retired' };
  }
  if (state.retired.includes(key)) {
    return { state, action: 'none', reason: `already retired (key=${key})` };
  }
  const strikes = (state.strikes[key] ?? 0) + 1;
  const next: ZoomRetirementState = { ...state, strikes: { ...state.strikes, [key]: strikes } };
  if (strikes < requiredStrikes) {
    return {
      state: next,
      action: 'strike',
      reason: `strike ${strikes}/${requiredStrikes} on key=${key} — NOT retired yet (one proven failure can still be a timing artefact)`,
    };
  }
  return {
    state: { ...next, retired: [...state.retired, key] },
    action: 'retire',
    reason: `strike ${strikes}/${requiredStrikes} on key=${key} — RETIRED for the session; the next pass falls through to the next candidate`,
  };
}

/**
 * Is retirement now an ABSORBING state — i.e. does the DOM still offer a
 * confidently-readable control, but every such control has been retired?
 *
 * "Every readable control retired" must never be terminal. Without this the
 * watcher logs `N candidate(s) rejected` for the rest of the call with nothing
 * left to try, which is strictly worse than the re-click loop it replaced.
 *
 * Deliberately NOT time-based expiry as well: two mechanisms (corroboration +
 * this reset) already cover both the single-flake case and the
 * everything-retired case, and a third tuning knob with its own clock would add
 * surface without covering a case these two miss.
 *
 * Pure.
 */
export function zoomRetirementIsAbsorbing(
  candidates: ZoomMicCandidate[],
  retired: ReadonlySet<string>,
): boolean {
  if (retired.size === 0) return false;
  let anyConfident = false;
  for (const candidate of candidates) {
    const kind = readZoomMicState(candidate.probe).kind;
    if (kind !== 'muted' && kind !== 'unmuted') continue;
    anyConfident = true;
    // A confidently-readable candidate that is NOT retired: still have options.
    if (!candidate.probe.elementKey || !retired.has(candidate.probe.elementKey)) return false;
  }
  return anyConfident;
}

/** Clear every retirement and every strike, keeping a count of how often. */
export function resetZoomRetirement(state: ZoomRetirementState): ZoomRetirementState {
  return { strikes: {}, retired: [], resets: state.resets + 1 };
}

/**
 * Try to keep the bot LOOKING muted for the whole call: poll the mic control and
 * re-click mute if it is confidently unmuted (a host-requested unmute, say).
 *
 * Best-effort, and known to be capable of failing: on 2026-09-02 two clicks
 * landed (Playwright reported no actionability error) and the control still read
 * unmuted afterwards. Every click is therefore re-read and reported through
 * reportZoomMuteClick — a click is never logged as a mute.
 *
 * CANDIDATE DISCOVERY is what makes that verification worth having. A click that
 * provably did not move the state retires that ELEMENT for the rest of the
 * session, so the next pass falls through to the next candidate. The failure it
 * fixes was not a missing selector: the element clicked was visible, enabled and
 * hit-testable, and simply is not a mute toggle in that state. Walking the
 * candidate list on evidence therefore survives whatever Zoom renames next,
 * which no amount of selector guessing does.
 *
 * This maintains only the VISUAL half. Silence is guaranteed independently by
 * installOutboundAudioLockInPage, which is why this watcher can afford to be
 * conservative and slow rather than aggressive.
 *
 * Unref'd and self-clearing on page close, so it can never delay bot shutdown.
 * Never throws. @returns a stop function (idempotent).
 */
export function startZoomMuteWatcher(
  page: Page,
  voiceAgentEnabled: boolean,
  intervalMs = 15_000,
  config: ZoomMuteWatcherConfig = zoomMuteWatcherDefaultConfig,
  pollConfig: ZoomMutePollConfig = zoomMutePollDefaults,
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
  // Clicks that were re-read afterwards and PROVEN not to have muted the bot.
  // Reported in the warning so a repeat failure reads as a pattern, not as a
  // fresh surprise each time. With rejection in place this also counts DISTINCT
  // wrong candidates, since each can only fail once.
  let ineffectiveClicks = 0;
  // Candidate-discovery memory. `retirement` holds the strikes and the retired
  // keys; `rejected` is the Set the selection consults, kept in step with it.
  //
  // Retirement is CORROBORATED (two proven failures on the same key) and
  // REVERSIBLE (cleared wholesale if it ever becomes absorbing). It used to be
  // one timing-sensitive observation, permanently — which turned a
  // useless-but-harmless re-click loop into a terminal failure whenever Zoom's
  // toolbar reflected the mute later than the single re-read.
  let retirement = zoomRetirementInitialState;
  const rejected = new Set<string>();
  const syncRejected = (): void => {
    rejected.clear();
    for (const key of retirement.retired) rejected.add(key);
  };

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
        // "Every readable control retired" must never be an absorbing state:
        // without this the watcher would log `N candidate(s) rejected` for the
        // rest of the call with nothing left to try — strictly worse than the
        // re-click loop retirement replaced.
        if (zoomRetirementIsAbsorbing(candidates, rejected)) {
          const cleared = retirement.retired.length;
          retirement = resetZoomRetirement(retirement);
          syncRejected();
          log(`[Zoom Web] WARNING: Mute watcher had retired EVERY readable mic control (${cleared}) — clearing the retirement set and starting discovery over (reset #${retirement.resets}). Retirement is corroborated and reversible precisely so this is not terminal`);
        }
        const selection = selectZoomMicToggle(candidates, rejected);
        if (!selection) {
          noCandidatePasses++;
          // Log the first occurrence, then every 8th pass (~2 min), so a
          // permanently blind watcher is visible without flooding the log.
          // The rejected count is named: "no readable control" and "every
          // readable control has been proven wrong" are different situations
          // and only one of them means the selector list needs work.
          if (noCandidatePasses === 1 || noCandidatePasses % 8 === 0) {
            log(`[Zoom Web] Mute watcher found no selectable mic control (pass ${noCandidatePasses}, ${rejected.size} candidate(s) rejected so far) — visual re-mute is INACTIVE; track-level silence unaffected [${describeZoomMicCandidates(candidates, rejected)}]`);
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
          // VERIFY. The click is not the outcome: re-reveal the footer (the
          // click may have moved focus or the toolbar may have auto-hidden
          // again) and re-probe before saying anything about the mute state.
          //
          // Its OWN try/catch, so a click is reported even when the
          // verification itself dies (a navigation destroys the execution
          // context). Falling through to the outer catch would log a generic
          // "pass failed" and leave the click entirely unaccounted for — which
          // is the same invisibility this change exists to remove.
          let poll: ZoomMutePollResult | null = null;
          let verifyError: string | null = null;
          try {
            // POLL, do not sample once. Every probe re-reveals the footer first
            // (the click can move focus and the toolbar auto-hides), and the
            // reading is always taken from the CLICKED element by key.
            poll = await pollZoomMuteSettled(
              async () => {
                await revealZoomFooter(page);
                return probeZoomMicCandidates(page, zoomMicToggleSelectors);
              },
              selection.candidate.probe.elementKey,
              pollConfig,
              () => Date.now(),
              (ms) => page.waitForTimeout(ms),
            );
          } catch (ve: any) {
            verifyError = ve?.message ?? String(ve);
          }
          const settled = poll && poll.settledAtMs !== null ? ` settled after ${poll.settledAtMs}ms in ${poll.polls} poll(s)` : '';
          const timing = poll
            ? `polls=${poll.polls} budget=${pollConfig.deadlineMs}ms${poll.timedOut ? ' TIMED OUT' : settled}`
            : 'poll did not run';
          const report = reportZoomMuteClick({
            reason: `${step.reason}; ${timing}`,
            before: selection,
            // PINNED TO THE CLICKED ELEMENT, never a fresh selection: see
            // readZoomMicCandidateByKey for the false-muted / false-retirement
            // failures a fresh selectZoomMicToggle produced here.
            //
            // `rejected` is deliberately NOT threaded here, and that asymmetry
            // with describeZoomMicCandidates below is intentional: this read is
            // keyed to ONE element, so filtering the set it is drawn from cannot
            // change the answer. Threading it would be dead weight that implied
            // the read is a selection.
            after: verifyError || !poll ? null : poll.after,
            afterDetail: verifyError
              ? `post-click verification failed: ${verifyError}`
              : describeZoomMicCandidates(poll ? poll.lastCandidates : [], rejected),
            priorIneffectiveClicks: ineffectiveClicks,
          });
          // Only a PROVEN-ineffective click counts; an unverifiable one is
          // unknown, not a failure, and must not be tallied as one.
          if (report.verdict === 'still-unmuted') ineffectiveClicks++;
          log(report.warn ? `[Zoom Web] WARNING: ${report.message}` : `[Zoom Web] ${report.message}`);
          // CORROBORATION. A proven failure is a STRIKE; only the second strike
          // on the same element retires it. One proven failure can still be a
          // timing artefact the settle poll did not outlast.
          if (report.rejectKey) {
            const rs = stepZoomRetirement(retirement, report.rejectKey);
            retirement = rs.state;
            syncRejected();
            log(`[Zoom Web] ${rs.action === 'retire' ? 'WARNING: ' : ''}Mute watcher candidate discovery — ${rs.reason}`);
          }
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
