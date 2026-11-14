import { Page } from "playwright";
import { log } from "../_host";

const POLL_MS = 1000;

/**
 * Watch the room until it disconnects. Returns a cleanup fn that stops polling.
 *
 * livekit-client has already tried to reconnect before it reports `Disconnected`, so any
 * disconnect we did not ask for ends the meeting for the bot: the host removed it
 * (PARTICIPANT_REMOVED), the room was deleted (ROOM_DELETED), or the connection is gone. The
 * reason name is logged; the bot handles all of them as on every other platform.
 */
export function startLivekitRemovalMonitor(
  page: Page | null,
  onRemoval?: () => void | Promise<void>,
): () => void {
  if (!page) return () => {};
  let stopped = false;
  const timer = setInterval(async () => {
    if (stopped || page.isClosed()) return;
    let state: { connected: boolean; disconnectReason?: string } | undefined;
    try {
      state = await page.evaluate(() => (window as any).VexaLivekit?.state());
    } catch { return; /* a page mid-navigation answers on the next poll */ }
    if (stopped || !state || state.connected || state.disconnectReason === "CLIENT_INITIATED") return;
    if (!state.disconnectReason) return;
    stopped = true;
    clearInterval(timer);
    log(`[LiveKit] REMOVAL TRIGGERED: disconnected (${state.disconnectReason})`);
    try { await onRemoval?.(); } catch (e: any) { log(`[LiveKit] removal callback failed: ${e?.message ?? e}`); }
  }, POLL_MS);
  return () => { stopped = true; clearInterval(timer); };
}
