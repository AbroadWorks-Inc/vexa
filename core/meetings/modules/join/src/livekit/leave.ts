import { Page } from "playwright";
import { log, logJSON, callLeaveCallback, stopLivekitRecording, BotConfig } from "../_host";

export async function leaveLivekitMeeting(
  page: Page | null,
  botConfig?: BotConfig,
  reason: string = "manual_leave",
): Promise<boolean> {
  log(`[LiveKit] Leaving meeting (reason: ${reason})`);

  // Notify the host first so it records the leave intent even if the disconnect fails.
  if (botConfig) {
    try {
      await callLeaveCallback(botConfig, reason);
    } catch (callbackError: any) {
      logJSON({
        level: "warn",
        msg: "[LiveKit] Leave callback failed; continuing with leave attempt",
        error_message: callbackError?.message,
        leave_reason: reason,
      });
    }
  }

  if (page && !page.isClosed()) {
    try {
      await page.evaluate(() => (window as any).VexaLivekit?.disconnect());
      log("[LiveKit] Disconnected from the room");
    } catch (e: any) {
      logJSON({ level: "error", msg: "[LiveKit] Error during leave", error_message: e?.message, leave_reason: reason });
    }
  }

  // Recording is a HOST concern — give the embedder a chance to drain its pipeline.
  try {
    await stopLivekitRecording(page ?? undefined, botConfig);
  } catch (e: any) {
    logJSON({ level: "error", msg: "[LiveKit] Error stopping recording during leave", error_message: e?.message, leave_reason: reason });
  }
  return true;
}
