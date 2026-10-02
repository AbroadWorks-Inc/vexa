# 0001 — Vexa is the meeting platform

## Context

AW already had a notetaker worker that turns a folder in `aw-chatworks-transcribe` into a named
transcript and a summary, fed by Jitsi. The previous cloud bot was a separate join path per
platform and did not scale to a self-serve guest bot.

## Decision

Run upstream Vexa v0.12 as the meeting platform for Google Meet, Microsoft Teams, and Zoom.
Keep our changes in this fork, on `development`. Leave Jitsi on its own recorder. Both paths call
the same `notetaker-worker`.

`main` in this repo stays a mirror of upstream. We do not commit product work to it.

## Consequence

A meeting's audio and speaker file are Vexa's, in bucket `aw-bots`. The transcript is still the
worker's, in `aw-chatworks-transcribe`. The exporter is the only bridge. See
[0003](0003-exporter-hands-off-to-notetaker.md).

The long design is
[archive/2026-09-23-aw-rearchitecture-design.md](../archive/2026-09-23-aw-rearchitecture-design.md).
