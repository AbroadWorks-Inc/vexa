# 0015 — Per-speaker Jitsi has no channel cap

## Context

Only Jitsi's channel count grows with attendance: Meet sends at most 3 remote voices, Zoom and
Teams one mix. The bot runs in one Chrome with a 1-CPU limit and records each Jitsi speaker with
its own recorder. Load test 2026-10-11 (audio only): 0.61, 0.73 and 0.88 cores at 10, 20 and 30
participants, throttled 47 % of the time at 30.

## Decision

No cap on channels. Optimise inside the bot: measure first, then remove waste (loudness measured
in the audio worklet, a speaker's recorder and nodes released when their track ends; both done),
then stop re-encoding by recording each stream's original Opus packets, once that is shown to work
in the bot's Jitsi page. A mixed-only fallback above a measured size is a later option.

## Consequence

Large Jitsi calls run near the bot's CPU limit today. Video receive is not measured (the bot sets
no `channelLastN`). The next step is an owner decision.
