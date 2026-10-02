# 0002 — A speaker-activity file on every meeting

## Context

Vexa kept who-spoke-when inside its debug tape. That tape also stores audio and stops at a size
cap, so a long meeting lost the names the transcript needs. Production runs with the tape off.

## Decision

The bot always writes `speaker-activity.jsonl`: time, channel, display name, loudness, duration,
and active-speaker hints. No audio. meeting-api stores it under `signal/` as its own part, so it
exists when the tape does not.

## Consequence

The exporter builds `speaker_timeline.json` from this file only. Turning the debug tape on is a
debugging action. It is not how names are produced.

Design:
[archive/2026-09-23-speaker-activity-design.md](../archive/2026-09-23-speaker-activity-design.md).
Current layout: [recording and speakers](../recording-and-speakers.md).
