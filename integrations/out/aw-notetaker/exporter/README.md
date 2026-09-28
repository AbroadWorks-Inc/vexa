# exporter — the service code

One module per job; the flow is `app` → `queue` → `job`.

| Module | What it does |
|---|---|
| `app.py` | `POST /hooks/vexa` (verifies Vexa's webhook signature, validates, queues) and `GET /healthz` |
| `queue.py` | Durable queue in S3 (`aw-exporter/pending/`, `failed/`), retry with backoff, the worker loop |
| `job.py` | Exports one meeting: every bot session's master audio → one `audio.wav`/`master.webm` on one clock, speaker timeline, JSON files, `/process` call |
| `activity.py` | Reads the bot's `speaker-activity.jsonl` (who-spoke-when, no audio) into speaker start/end events — no fallback to the debug capture tape |
| `attribution.py` | Turns those events into `speaker_timeline.json` / `participants.json` (rules ported from the old cloud bot) |
| `schemas.py` | The JSON shapes `notetaker-worker` reads |
| `export_result.py` | Reports the export result through the gateway (`POST /v2/meetings/{uuid}/export`); the queue retries it |
| `audio.py` | ffmpeg webm → wav transcode, and the join of several sessions (wav in Python, webm with ffmpeg) with silence in the gaps |
| `storage.py`, `vexa_client.py`, `notetaker.py` | S3, meeting-api (through the gateway) and notetaker-worker adapters |
| `config.py`, `naming.py`, `signature.py` | Env settings, export folder name, webhook signature check |
| `__main__.py` | Entry point: `python -m exporter` |
