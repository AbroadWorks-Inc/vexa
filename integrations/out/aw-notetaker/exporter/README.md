# exporter — the service code

One module per job; the flow is `app` → `queue` → `job`.

| Module | What it does |
|---|---|
| `app.py` | `POST /hooks/vexa` (verifies Vexa's webhook signature, validates, queues) and `GET /healthz` |
| `queue.py` | Durable queue in S3 (`aw-exporter/pending/`, `failed/`), retry with backoff, the worker loop |
| `job.py` | Exports one meeting: master audio → `audio.wav`, speaker timeline, JSON files, `/process` call |
| `activity.py` | Reads the bot's `speaker-activity.jsonl` (who-spoke-when, no audio) into speaker start/end events — no fallback to the debug capture tape |
| `attribution.py` | Turns those events into `speaker_timeline.json` / `participants.json` (rules ported from the old cloud bot) |
| `schemas.py` | The JSON shapes `notetaker-worker` reads |
| `storage.py`, `vexa_client.py`, `notetaker.py`, `audio.py` | S3, meeting-api, notetaker-worker and ffmpeg adapters |
| `config.py`, `naming.py`, `signature.py` | Env settings, export folder name, webhook signature check |
| `__main__.py` | Entry point: `python -m exporter` |
