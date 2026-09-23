# exporter — Vexa meeting → AbroadWorks notetaker hand-off

On Vexa's `meeting.completed` webhook, builds the per-meeting AbroadWorks notetaker folder in
`aw-chatworks-transcribe` (audio, speaker-activity-derived speaker attribution, meeting metadata)
and hands it off to `notetaker-worker` via `POST /process`.

Spec: [`docs/2026-09-23-aw-rearchitecture-design.md`](docs/2026-09-23-aw-rearchitecture-design.md)
(§4), [`docs/2026-09-23-speaker-activity-design.md`](docs/2026-09-23-speaker-activity-design.md).
Plan: [`docs/2026-09-23-aw-exporter-plan.md`](docs/2026-09-23-aw-exporter-plan.md).

## Config (names only — see spec §4.4)
`MEETING_API_URL`, `VEXA_WEBHOOK_SECRET`, `VEXA_BUCKET`, `EXPORT_BUCKET`, `EXPORT_PREFIX`,
`NOTETAKER_URL`, `EXPORT_DEBUG`, `EXPORT_CONCURRENCY`, `EXPORT_SWEEP_SECONDS`,
`EXPORT_MAX_ATTEMPTS`, `RMS_SPEECH_THRESHOLD`, `SPEECH_HANGOVER_MS`,
`MIN_DOMINANT_UTTERANCE_MS`, `RECORD_CHUNK_TIMESLICE_MS`, `ACTIVITY_WAIT_SECONDS`,
`AWS_REGION`. S3 access is via IRSA (no static keys).

## Retention tagging (see spec §3/§7)
`EXPORT_BUCKET` expires objects by the `retention-class` S3 object tag
(`exporter/retention.py`): `master.webm` -> `recording-mp4` (30 days), `audio.wav` and
`EXPORT_DEBUG`'s `signal/*` copies -> `audio` (7 days), every JSON the exporter writes ->
`metadata` (365 days). Objects the exporter writes into `VEXA_BUCKET`
(`aw-exporter/pending/`, `failed/`) are never tagged — that bucket has its own prefix
lifecycle. The exporter's IAM role needs `s3:PutObjectTagging` on `EXPORT_BUCKET`.

## Dev setup

```bash
/opt/homebrew/bin/python3.11 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest -q && black --check . && ruff check . && mypy exporter
```
