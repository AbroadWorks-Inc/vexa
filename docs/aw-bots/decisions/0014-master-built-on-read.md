# 0014 — The master recording is built when it is read

## Context

meeting-api assembles a session's master from its chunks when it is read
(`GET /recordings/{id}/master`). On 2026-10-09 a 2-hour channel took 38 s, past the gateway's
30 s timeout. Building each master once, when the bot session ends, was considered.

## Decision

Keep building on read, and make it fast: 8 chunks at a time, uploaded in multipart parts (2.7 s
for a 2-hour channel). Building at session end was dropped (owner, 2026-10-12).

## Consequence

The only reader is the exporter, once, after the meeting, so concurrent builds of one master do
not happen. A 4-hour channel builds in about 5 to 6 s. Build on read exists for upstream's
mid-meeting playback, which AW does not use.
