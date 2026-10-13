# 0009 — Jitsi records through AW Bots

## Context

Jitsi meetings on `meet.abroadworks.com` were recorded by Jibri, with live captions from the
transcriber, while Meet, Teams and Zoom went through the AW Bots bot. Two recording paths fed one
notetaker pipeline, and only the bot path had per-speaker channels.

## Decision

The AW Bots bot records Jitsi meetings like the other platforms (`meetingApi.jitsiHosts` lists
`meet.abroadworks.com`). Jibri and live captions are removed. Deployed 2026-10-11, with no
conference running.

## Consequence

One recording path. Jitsi gets per-speaker channels ([0008](0008-per-speaker-channels.md)) and
speaker names from each participant's audio level in the mix. The transcriber runs batch jobs
only. This replaces "Leave Jitsi on its own recorder" in
[0001](0001-vexa-as-the-meeting-platform.md).

Current behavior: [recording and speakers](../recording-and-speakers.md).
