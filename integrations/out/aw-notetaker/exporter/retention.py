"""Retention-class values for the `EXPORT_BUCKET`'s S3 lifecycle rules (design doc §3/§7):
the bucket expires an object by its `retention-class` tag, so anything the exporter writes
untagged never expires. `exporter.storage.Storage`'s write methods take an optional
`retention` argument that sets this tag.

Vexa-bucket writes (`aw-exporter/pending/`, `aw-exporter/failed/`) are deliberately never
tagged — that bucket has its own prefix lifecycle.

A fourth class, "summary" (7 years), applies to `summary.json`, which `notetaker-worker`
writes, not the exporter — no constant for it here.
"""

from __future__ import annotations

# 7 days: audio.wav, channels/ch<N>.webm with the channels/index.json that names them
# (the index expires with its files), EXPORT_DEBUG signal/* copies (can contain audio)
AUDIO = "audio"
METADATA = (
    "metadata"  # 365 days: every other JSON the exporter writes into EXPORT_BUCKET
)
RECORDING_MP4 = "recording-mp4"  # 30 days — master.webm
