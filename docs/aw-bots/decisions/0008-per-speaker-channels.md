# 0008 — Per-speaker channels beside the mix

## Context

After the call, one mixed recording is transcribed and names are applied from
`speaker-activity.jsonl`. Two people talking at once become one line. On Google Meet the
glow is one lit tile at frame delivery, so a name can land on the wrong channel. Vexa can
transcribe per channel during the call. We run with `TRANSCRIBE_ENABLED` false and use the
shared transcriber after the call. The debug tape holds channel audio and is capped, so it
is not the recording.

## Decision

On Google Meet and Jitsi, when `PER_CHANNEL_RECORDING_PLATFORMS` lists the platform, the bot
records each remote channel beside the mixed master. The mixed master, the mixed timeline,
and `POST /process` are unchanged. A channel that fails to export does not stop that handoff.
Teams stays the server mix. Zoom stays `pertrack`.

The worker's `CHANNEL_TRANSCRIPT_MODE` decides what users receive. `compare`, which is set,
delivers the mixed transcript and saves the channel transcript beside it. `deliver` makes
the channel transcript the delivered one and falls back to the mix when that path fails.

## Consequence

Meetings recorded before the switch have no channel files and stay on the mixed path.
Turning recording on or off is the Helm value. Replacing the delivered transcript is the
worker setting. Neither requires a new image by itself. The bot that is already in a call
keeps the invocation it started with.

Current behavior: [recording and speakers](../recording-and-speakers.md).
The folder: [export and webhooks](../export-and-webhooks.md).
