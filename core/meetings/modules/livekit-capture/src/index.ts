/**
 * @vexa/livekit-capture — the bot's LiveKit session in the page (browser). Front door.
 * `page.ts` is the bundle entry that builds the session on `window.VexaLivekit`.
 */
export { createLivekitSession, countRoster } from "./livekit-session.js";
export type {
  LivekitRoster,
  LivekitSession,
  LivekitSessionOptions,
  LivekitSubscribedTrack,
  RoomLike,
} from "./livekit-session.js";
export { createLivekitSpeakers } from "./livekit-speakers.js";
export type { LivekitSpeaker, LivekitSpeakers, LivekitSpeakersOptions } from "./livekit-speakers.js";
