# 0003 — The exporter hands off to the notetaker

## Context

The notetaker worker expects one folder per meeting: `audio.wav`, a speaker timeline, and
metadata. Vexa stores chunked webm and a signal file in a different bucket, keyed by user,
recording, and session.

## Decision

A small service in `integrations/out/aw-notetaker` does that translation. It is a webhook
subscriber. It reads through the gateway with its own key. It writes the folder and calls
`POST /process`, then reports the result on `POST /v2/meetings/{id}/export`.

The bot does not call the worker. meeting-api does not write the transcribe bucket.

## Consequence

The worker stays shared with Jitsi and stays in the deployment repo. Several bot sessions on one
meeting become one folder. Erasure of the Vexa recording does not delete the exported copy. See
[security](../security.md#erasure).

Design:
[archive/2026-09-23-aw-rearchitecture-design.md](../archive/2026-09-23-aw-rearchitecture-design.md).
