# aw-exporter

On Vexa's `meeting.completed` webhook, builds the per-meeting AbroadWorks notetaker folder in
`aw-chatworks-transcribe` (audio, tape-derived speaker attribution, meeting metadata) and hands it
off to `notetaker-worker` via `POST /process`.

Spec: [`docs/2026-09-23-aw-rearchitecture-design.md`](docs/2026-09-23-aw-rearchitecture-design.md)
(§4). Plan: [`docs/2026-09-23-aw-exporter-plan.md`](docs/2026-09-23-aw-exporter-plan.md).

## Config (names only — see spec §4.4)
`MEETING_API_URL`, `VEXA_WEBHOOK_SECRET`, `VEXA_BUCKET`, `EXPORT_BUCKET`, `EXPORT_PREFIX`,
`NOTETAKER_URL`, `EXPORT_DEBUG`, `EXPORT_CONCURRENCY`, `EXPORT_SWEEP_SECONDS`,
`EXPORT_MAX_ATTEMPTS`, `RMS_SPEECH_THRESHOLD`, `SPEECH_HANGOVER_MS`,
`MIN_DOMINANT_UTTERANCE_MS`, `RECORD_CHUNK_TIMESLICE_MS`, `TAPE_WAIT_SECONDS`, `TAPE_MAX_BYTES`,
`AWS_REGION`. S3 access is via IRSA (no static keys).

## Dev setup

```bash
/opt/homebrew/bin/python3.11 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest -q && black --check . && ruff check . && mypy aw_exporter
```
