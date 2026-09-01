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
      if (!voiceAgentEnabled) {
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
            ? `[Zoom Web] Outbound audio lock re-asserted (bot cannot be unmuted) — ${describeOutboundAudioLock(lock)}`
            : '[Zoom Web] WARNING: outbound audio lock re-assert could not run — the page-load arm may still be in force',
        );
        if (lock && !lock.alreadyInstalled) {
          log('[Zoom Web] WARNING: no page-load audio lock was found in this page — the mic track may predate the patches; check the join.ts addInitScript step');
        }

        // Step 3 — backstop for anything the patches cannot see. Self-clears on
        // page close; unref'd.
        startZoomOutboundAudioGuard(page, false);

        // Step 4 — keep the bot LOOKING muted for the whole call (a host can
        // request an unmute). Visual only; silence is already guaranteed above.
        startZoomMuteWatcher(page, false);

        // Layer 1 stays NON-blocking (it retries for ~30s and must not delay
        // recording), but the outcome is no longer swallowed — a failure to mute
        // is a privacy-relevant fact and gets logged as such. Not fatal: layer 2
        // already guarantees no audio leaves, so tearing the meeting down here
        // would cost a recording to fix a cosmetic-only residue.
        void ensureZoomMutedInMeeting(page)
          .then((outcome) => {
            if (!outcome.muted) {
              log('[Zoom Web] WARNING: in-meeting DOM mute NOT confirmed — bot is silent (track-level) but may still APPEAR unmuted to participants; re-verify zoomMicToggleSelectors against the live DOM');
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
