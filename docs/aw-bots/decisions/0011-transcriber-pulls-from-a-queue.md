# 0011 — The transcriber takes work from a queue

## Context

Each transcriber pod kept its tasks in memory, and the worker polled through the Service, so with
two pods a poll could reach the wrong pod and the task was sent again. KEDA asked for at most one
pod. The worker's time limit also counted the time a task waited behind another meeting, so a
long queue dropped work that never reached the GPU.

## Decision

Approved 2026-10-10. The worker puts each task in a queue in notetaker-redis (keys prefixed
`transcriber:`); a free pod claims the oldest, reads the audio from S3 itself and writes the
result back. A task held by a pod that dies is taken again. KEDA runs one pod per meeting with
work outstanding, up to 3 (GPU node pool limit 4). The transcriber owns the time limit: it counts
only a job's own GPU time and stops it past `ASR_BATCH_GPU_LIMIT_SECONDS` (3600), reporting
`failed`. Waiting in the queue never counts, and the worker has no clock of its own.

## Consequence

Several meetings can transcribe at once, and none is dropped for waiting. The transcriber needs
Secret `transcriber-redis` (`REDIS_URL`, the same value as the worker's). Details: deployment repo
`deployment/base/transcriber/README.md`.
