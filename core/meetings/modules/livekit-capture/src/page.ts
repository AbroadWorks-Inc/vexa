/**
 * Bundle entry for the bot's LiveKit page (`livekit-page.global.js`): one session on
 * `window.VexaLivekit`, logging through the bot's `window.logBot`.
 */
import { Room } from "livekit-client";
import { createLivekitSession, type RoomLike } from "./index.js";

const w = window as unknown as { VexaLivekit?: unknown; logBot?: (m: string) => void };
// Room's event-typed `on` is wider than RoomLike's; the session uses only the RoomLike part.
const room = new Room({ adaptiveStream: false, dynacast: false }) as unknown as RoomLike;
w.VexaLivekit = createLivekitSession(room, { log: (m) => w.logBot?.(`[livekit] ${m}`) });
