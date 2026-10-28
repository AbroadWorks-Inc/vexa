# 0010 — The per-person transcript is the one delivered

## Context

With per-speaker channels recorded ([0008](0008-per-speaker-channels.md)), notetaker-worker ran in
`compare` mode: it delivered the mixed transcript and saved the per-person one beside it. Tests on
real meetings showed the per-person transcript kept sentences whole where the mixed one split
them at a speaker change.

## Decision

`CHANNEL_TRANSCRIPT_MODE=deliver` on notetaker-worker (owner, 2026-10-10). When a meeting has
channel files, the per-person transcript is `transcript.txt`, and its non-English turns are
translated into `transcript_en.txt` on the same turns and names. The mixed recording is
transcribed only when the per-person pass fails or returns nothing.

## Consequence

A meeting is transcribed once, not twice. Teams and Zoom, which have no channels, stay on the
mixed path. This replaces the `compare` setting described in 0008.

The worker's side: deployment repo `deployment/base/notetaker/worker/README.md`.
