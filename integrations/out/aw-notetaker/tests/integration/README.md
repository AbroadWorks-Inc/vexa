# tests/integration — aw-exporter container + compose flow

Builds the `aw-exporter` Docker image and drives one `meeting.completed` webhook through the
real container against a local MinIO (S3) plus in-process stub `meeting-api` and
`notetaker-worker` servers, asserting the exported S3 keys, the `_export.json` hand-off state,
the `/process` request body, and the transcoded `audio.wav` (16 kHz mono, ~3 s).

Run with:

```bash
. .venv/bin/activate
pytest -m integration -q tests/integration
```

Prerequisites: a local Docker daemon (containers named `aw-exporter-it-<suffix>` are always
torn down by the test, even on failure) and network access to pull
`quay.io/minio/minio:latest`, plus a local `ffmpeg` on the host `PATH` to synthesize the seed
audio. Excluded from the default `pytest` run via `pyproject.toml`'s `addopts`.
