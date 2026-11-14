import { AdmissionError } from "../shared/admission";

/**
 * Meet's waiting room, over HTTP. The bot asks to enter as a guest with its configured name and
 * keeps its place by asking again with the participant id Meet signed (Meet drops a waiting entry
 * after 6 s without a call). Once the host admits it, the answer carries a LiveKit URL and token.
 *
 * Meet's answer shape stays in this file: what comes out is `{ url, token }` or a typed
 * AdmissionError. The token is never logged.
 */

/** Every 3 s: under Meet's 6 s drop, about 20 calls a minute against its 150/minute per-entry limit. */
export const KNOCK_INTERVAL_MS = 3000;
/** After a 429, a 5xx or a network error: 4 s, then 5 s while the failures last — every wait stays
 *  under Meet's 6 s drop, so the bot keeps its place in the waiting room. */
const BACKOFF_MS = [4000, 5000];
const ROOM_CODE = /^[a-z]{3}-[a-z]{4}-[a-z]{3}$/;

export interface LivekitRoom {
  host: string;
  code: string;
}

/** The host and room code of a Meet website meeting URL (`https://<host>/<abc-defg-hij>`). */
export function parseLivekitUrl(meetingUrl: string): LivekitRoom {
  let url: URL;
  try { url = new URL(meetingUrl); } catch { throw new AdmissionError("join_failure", `not a LiveKit meeting URL: ${meetingUrl}`); }
  const code = url.pathname.replace(/^\/+|\/+$/g, "").toLowerCase();
  if (url.protocol !== "https:" || url.port || url.username || url.password || !ROOM_CODE.test(code)) {
    throw new AdmissionError("join_failure", `not a LiveKit meeting URL: ${meetingUrl}`);
  }
  return { host: url.host.toLowerCase(), code };
}

/** The LiveKit server URL Meet hands back: wss or https, no credentials in it. */
function isLivekitServerUrl(raw: string): boolean {
  try {
    const u = new URL(raw);
    return (u.protocol === "wss:" || u.protocol === "https:") && !u.username && !u.password;
  } catch {
    return false;
  }
}

export interface KnockOptions {
  host: string;
  code: string;
  botName: string;
  budgetMs: number;
  /** Called once, on the first answer that says the bot is waiting. */
  onWaiting: () => void | Promise<void>;
  /** True once the bot is withdrawn: knocking stops. */
  cancelled: () => boolean;
  fetch?: typeof globalThis.fetch;
  sleep?: (ms: number) => Promise<void>;
  now?: () => number;
  log?: (message: string) => void;
}

interface EntryAnswer {
  status?: string;
  id?: string;
  livekit?: { url?: string; token?: string } | null;
}

export async function knockUntilAdmitted(o: KnockOptions): Promise<{ url: string; token: string }> {
  const doFetch = o.fetch ?? globalThis.fetch;
  const sleep = o.sleep ?? ((ms: number) => new Promise<void>((r) => setTimeout(r, ms)));
  const now = o.now ?? Date.now;
  const log = o.log ?? (() => {});
  const endpoint = `https://${o.host}/api/v1.0/rooms/${o.code}/request-entry/`;
  const deadline = now() + o.budgetMs;
  let participantId: string | undefined;
  let waiting = false;
  let failures = 0;
  let lastStatus = "";

  const backoff = () => sleep(BACKOFF_MS[Math.min(failures, BACKOFF_MS.length) - 1]);

  for (;;) {
    if (o.cancelled()) throw new AdmissionError("join_failure", "withdrawn while waiting to be admitted");
    if (now() >= deadline) {
      throw waiting
        ? new AdmissionError("lobby_timeout", `not admitted within ${Math.round(o.budgetMs / 1000)} s`)
        : new AdmissionError("join_failure", `waiting room on ${o.host} not reached within ${Math.round(o.budgetMs / 1000)} s`);
    }
    const body = participantId ? { username: o.botName, participant_id: participantId } : { username: o.botName };
    let res: Response;
    try {
      // A redirect is refused: the bot calls only the meeting's own host.
      res = await doFetch(endpoint, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), redirect: "error" });
    } catch (e) {
      failures++;
      log(`[LiveKit] knock: network error (${(e as Error)?.name ?? "error"}), backing off`);
      await backoff();
      continue;
    }
    if (res.status === 429 || res.status >= 500) {
      failures++;
      log(`[LiveKit] knock: HTTP ${res.status}, backing off`);
      await backoff();
      continue;
    }
    if (!res.ok) throw new AdmissionError("join_failure", `waiting room answered HTTP ${res.status} for room ${o.code} on ${o.host}`);
    failures = 0;
    let answer: EntryAnswer;
    try {
      answer = (await res.json()) as EntryAnswer;
    } catch {
      throw new AdmissionError("join_failure", `waiting room on ${o.host} answered HTTP ${res.status} with a body that is not JSON`);
    }
    if (typeof answer.id === "string" && answer.id) participantId = answer.id;
    if (answer.status !== lastStatus) {
      log(`[LiveKit] knock: ${answer.status}`);
      lastStatus = answer.status ?? "";
    }
    if (answer.status === "accepted") {
      const url = answer.livekit?.url;
      const token = answer.livekit?.token;
      if (!url || !token) throw new AdmissionError("join_failure", "admitted, but the answer carried no LiveKit URL or token");
      if (!isLivekitServerUrl(url)) throw new AdmissionError("join_failure", "admitted, but the LiveKit URL is not wss/https without credentials");
      return { url, token };
    }
    if (answer.status === "denied") throw new AdmissionError("denial", "the host denied entry");
    if (answer.status !== "waiting") throw new AdmissionError("join_failure", `unexpected waiting-room status '${answer.status}'`);
    if (!waiting) {
      waiting = true;
      await o.onWaiting();
    }
    await sleep(KNOCK_INTERVAL_MS);
  }
}
