# 0016 — A Zoom speaker hint is sent only while audio arrives

## Context

Zoom keeps the last speaker lit in its active-speaker panel while nobody talks, and the bot
re-sends the lit name every ~2 s. The speaker-activity file ([0002](0002-speaker-activity-file.md))
records each hint, and the exporter builds the timeline from hints when no audio frame carries a
name. On 2026-10-12 two one-person Zoom calls where the host said nothing exported minutes of
"speech" over a silent recording and failed the audio check (`audio_mismatch`).

## Decision

The bot's Zoom wiring sends a hint only while remote audio above the capture floor (peak 0.005)
arrives on any track within the last 2.5 s, and an end hint only for a turn whose start was sent.
The fix is where the false claim is made, in the bot. The exporter's audio check is unchanged:
an exporter that accepted only hints backed by our own audio would compare the recording with
itself and stop catching silent recordings on Jitsi and Teams. Merged and deployed 2026-10-13
(PR #67, Helm revision 23).

## Consequence

A silent Zoom call exports with no speech, and notetaker-worker calls no model for it. On Zoom the
audio check still catches a recording that loses audio the bot received. Our `capture-bridge.ts`
Zoom wiring differs from upstream; keep ours when syncing.

Current behavior: [recording and speakers](../recording-and-speakers.md#zoom-hints-in-a-silent-room).
