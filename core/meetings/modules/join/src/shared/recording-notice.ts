/**
 * The recording notice the bot posts into a meeting's chat once admitted (Google Meet, LiveKit).
 *
 * Design §5.2 notice, plus the §14 #4 line that the host removes the bot. The unsigned legal draft
 * that says the bot leaves and deletes the recording is not used: the bot does not do that.
 */
export const RECORDING_NOTICE =
  "Olanote by Olamee Corp is recording this meeting for transcription. " +
  "See https://olamee.com/legal?tab=privacy for details. " +
  "If you'd prefer the meeting not be recorded, ask the host to remove this bot.";
