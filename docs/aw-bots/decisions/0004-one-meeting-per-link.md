# 0004 — One meeting per link, one live bot

## Context

Calendar sync and a pasted link can both describe the same room. Comparing meeting URLs as raw
strings treated three spellings as three meetings. Two bots in one call record over each other.

## Decision

Inside one account, entries with the same link at overlapping times are one meeting. A `join_now`
adopts the meeting already there when its start is inside `JOIN_NOW_ADOPT_AHEAD_S`. A second
meeting on that link waits until the live bot has left.

The meeting's public id is a UUID. Callers do not invent a second key from the URL.

## Consequence

Dedup is AW Bots' job. A client that sends the same invite twice gets one bot. Two accounts are
two meetings even on the same link, because each account has its own key.

The intake design is
[archive/2026-09-25-meeting-intake-and-webhooks-design.md](../archive/2026-09-25-meeting-intake-and-webhooks-design.md).
Current rules: [meeting lifecycle](../meeting-lifecycle.md).
