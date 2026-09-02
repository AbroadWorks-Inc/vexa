import { Page } from 'playwright';
import { BotConfig } from '../../../types';
import { runMeetingFlow, PlatformStrategies } from '../../shared/meetingFlow';
import { joinZoomWebMeeting } from './join';
import { waitForZoomWebAdmission, checkZoomWebAdmissionSilent } from './admission';
import {
  prepareZoomWebMeeting,
  switchToZoomSpeakerView,
  ensureZoomMutedInMeeting,
  sweepZoomOutboundAudio,
  startZoomOutboundAudioGuard,
  describeOutboundAudioSweep,
  installZoomOutboundAudioLock,
  describeOutboundAudioLock,
  startZoomMuteWatcher,
  isZoomAudioTouchAllowed,
  probeZoomSendSide,
  describeZoomSendSide,
  zoomAudioLockMode,
} from './prepare';
import { startZoomWebRecording } from './recording';
import { startZoomWebRemovalMonitor } from './removal';
import { leaveZoomWebMeeting } from './leave';
import { log } from '../../../utils';

export async function handleZoomWeb(
  botConfig: BotConfig,
  page: Page | null,
  gracefulLeaveFunction: (page: Page | null, exitCode: number, reason: string) => Promise<void>
): Promise<void> {
  const strategies: PlatformStrategies = {
    join: joinZoomWebMeeting,
    waitForAdmission: waitForZoomWebAdmission,
    checkAdmissionSilent: checkZoomWebAdmissionSilent,
    prepare: prepareZoomWebMeeting,
    // Post-admission: force Speaker View so the DOM active-speaker containers
    // populate (Layout 1/2 in recording.ts read them). Fired NON-blocking: the
    // switch retries for ~25s, so awaiting it would delay audio recording start —
    // instead it runs in the background and exits once Speaker View is confirmed.
    // Safe mid-recording (Zoom notetaker is audio-only; no screen video capture,
    // so the cursor moves are invisible). Non-fatal.
    afterAdmission: async (page: Page | null, botConfig: BotConfig): Promise<void> => {
      if (!page) return;
      const voiceAgentEnabled = !!botConfig.voiceAgentEnabled;

      // A recorder bot must never be heard. Two independent layers, because they
      // fix different things and either can fail on its own:
      //
      //   Layer 2 (track level) guarantees SILENCE and does not read the DOM.
      //   Layer 1 (DOM mute)    makes the participant list SHOW the bot muted.
      //
      // A voice-agent bot legitimately transmits TTS and is exempt from BOTH.
      // Hands-off mode (ZOOM_AUDIO_LOCK=none): steps 1-3 all mutate tracks or
      // install patches, so they are skipped wholesale. Step 4 (the DOM mute
      // watcher) and ensureZoomMutedInMeeting still run — they touch only Zoom's
      // own UI. This is the configuration that lets a live audio-join failure be
      // isolated: 'off' still writes track.enabled = false, so it cannot tell a
      // rejected WRITE apart from a rejected unreadable getter.
      // SEND-SIDE CANARY, awaited, in EVERY mode (it patches nothing and touches
      // no track, so it is safe even in hands-off). This is the ONLY line that
      // reports whether the start.sh microphone fix is in force: the earlier
      // "AUDIO JOIN OK" line counts inbound <audio> elements and prints
      // identically whether or not a capture device exists.
      const sendSide = await probeZoomSendSide(page);
      log(`[Zoom Web] SEND-SIDE CHECK — ${describeZoomSendSide(sendSide, zoomAudioLockMode())} | (the earlier "AUDIO JOIN OK" line describes the RECEIVE side only)`);

      const touchAllowed = isZoomAudioTouchAllowed();
      if (!voiceAgentEnabled && !touchAllowed) {
        log('[Zoom Web] Outbound audio machinery SKIPPED — ZOOM_AUDIO_LOCK hands-off mode: no sweep, no lock, no guard, no track touched. Silence rests entirely on the PulseAudio source mute (start.sh); the DOM mute path below still runs');
      }
      if (!voiceAgentEnabled && touchAllowed) {
        // Step 1, AWAITED — silence NOW with the least invasive primitive, so
        // the bot is quiet before anything more elaborate is attempted.
        const sweep = await sweepZoomOutboundAudio(page, false);
        log(
          sweep
            ? `[Zoom Web] Outbound audio silenced at track level — ${describeOutboundAudioSweep(sweep)}`
            : '[Zoom Web] WARNING: outbound audio sweep could not run (page closed or context destroyed)',
        );

        // Step 2, AWAITED — RE-ASSERT the lock. The primary install happens at
        // PAGE LOAD (join.ts), before any Zoom script runs, which is what
        // guarantees the mic track cannot pre-exist the patches. This call
        // re-verifies every seal and catches anything the page-load arm missed.
        const lock = await installZoomOutboundAudioLock(page, voiceAgentEnabled);
        log(
          lock
            // Report the mechanism and its counters, not an absolute. "bot
            // cannot be unmuted" was a claim the counters do not establish on
            // their own (tracksLocked was 0 for the whole 2026-09-02 meeting);
            // describeOutboundAudioLock says what was actually patched/sealed.
            ? `[Zoom Web] Outbound audio lock re-asserted — ${describeOutboundAudioLock(lock)}`
            : '[Zoom Web] WARNING: outbound audio lock re-assert could not run — the page-load arm may still be in force',
        );
        if (lock && !lock.alreadyInstalled) {
          log('[Zoom Web] WARNING: no page-load audio lock was found in this page — the mic track may predate the patches; check the join.ts addInitScript step');
        }

        // Step 3 — backstop for anything the patches cannot see. Self-clears on
        // page close; unref'd.
        startZoomOutboundAudioGuard(page, false);
      }

      // Steps 4 and 5 are the DOM-ONLY half: they read and click Zoom's own UI
      // and never touch a track, so they run in hands-off mode too. In that mode
      // they are the ONLY thing maintaining the visual state, which is exactly
      // why the click verification and candidate discovery matter there.
      if (!voiceAgentEnabled) {
        // Step 4 — try to keep the bot LOOKING muted for the whole call (a
        // host can request an unmute). Visual only, and best-effort: on
        // 2026-09-02 two watcher clicks landed and the control still read
        // unmuted, so every click is now re-read and reported as what it was.
        startZoomMuteWatcher(page, false);

        // Layer 1 stays NON-blocking (it retries for ~30s and must not delay
        // recording), but the outcome is no longer swallowed — a failure to mute
        // is a privacy-relevant fact and gets logged as such. Not fatal, and
        // deliberately so: tearing the meeting down here would cost a recording.
        // What backs the silence depends on the mode — the track seal in 'on',
        // the enabled=false write in 'off', and in 'none' the PulseAudio source
        // mute alone. It is never this DOM path, in any mode.
        void ensureZoomMutedInMeeting(page)
          .then((outcome) => {
            if (!outcome.muted) {
              log('[Zoom Web] WARNING: in-meeting DOM mute NOT confirmed — the bot may APPEAR unmuted to participants; the track-level seal is separate and its state is in the outbound-audio guard heartbeat; re-verify zoomMicToggleSelectors against the live DOM');
            }
          })
          .catch((e: any) => log(`[Zoom Web] ensureZoomMutedInMeeting failed: ${e?.message ?? e}`));
      }

      void switchToZoomSpeakerView(page).catch(() => {});
    },
    startRecording: startZoomWebRecording,
    // Wrap to inject botConfig so the monitor can honor the left-alone /
    // no-one-joined timeouts (automaticLeave.*) — the base strategy signature
    // doesn't carry botConfig.
    startRemovalMonitor: (page: Page | null, onRemoval?: (reasonToken?: string) => void | Promise<void>) =>
      startZoomWebRemovalMonitor(page, onRemoval, botConfig),
    leave: leaveZoomWebMeeting,
  };

  await runMeetingFlow('zoom', botConfig, page, gracefulLeaveFunction, strategies);
}

export { leaveZoomWebMeeting as leaveZoomWeb };
