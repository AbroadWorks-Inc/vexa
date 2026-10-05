/**
 * @vexa/jitsi-capture — Jitsi Meet's contribution to the mixed lane.
 *
 * Like Zoom/Teams, Jitsi delivers one mixed audio stream (captured by
 * @vexa/mixed-capture-core); this module provides the WHO + chat signals:
 *   - createJitsiSpeakers: prefers each participant's audio level (two-poll hold,
 *     ends the turn when nobody stays above the threshold). The dominant-speaker
 *     flag is the fallback when no level has arrived.
 *   - createJitsiChat: reads conference chat (redux primary — the panel need
 *     not be open; DOM fallback) + sendJitsiChatMessage over the app's own API.
 */
export {
  createJitsiSpeakers,
  selectJitsiSpeaker,
  JITSI_SPEECH_LEVEL,
  CONFIRM_POLLS,
  jitsiDominantTileSelectors,
  jitsiTileNameSelectors,
} from "./jitsi-speakers.js";
export type {
  JitsiSpeakers,
  JitsiSpeakersOptions,
  JitsiHintSource,
  JitsiLevelSample,
  JitsiNamedParticipant,
} from "./jitsi-speakers.js";
export {
  createJitsiChat,
  sendJitsiChatMessage,
  jitsiChatContainerSelectors,
  jitsiChatMessageSelectors,
  jitsiChatSenderSelectors,
  jitsiChatTextSelectors,
} from "./jitsi-chat.js";
export type { JitsiChat, JitsiChatMessage, JitsiChatOptions } from "./jitsi-chat.js";
