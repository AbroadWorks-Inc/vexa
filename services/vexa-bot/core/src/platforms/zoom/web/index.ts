import { Page } from 'playwright';
import { BotConfig } from '../../../types';
import { runMeetingFlow, PlatformStrategies } from '../../shared/meetingFlow';
import { joinZoomWebMeeting } from './join';
import { waitForZoomWebAdmission, checkZoomWebAdmissionSilent } from './admission';
import { prepareZoomWebMeeting, switchToZoomSpeakerView, ensureZoomMutedInMeeting } from './prepare';
import { startZoomWebRecording } from './recording';
import { startZoomWebRemovalMonitor } from './removal';
import { leaveZoomWebMeeting } from './leave';

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
      // Mute the bot in-meeting (preview mute doesn't persist through audio-join).
      // Recorder bots are receive-only; voice-agent bots must transmit TTS so they
      // are exempt. Non-blocking, best-effort.
      if (!botConfig.voiceAgentEnabled) void ensureZoomMutedInMeeting(page).catch(() => {});
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
