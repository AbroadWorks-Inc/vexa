- **Exporter: a rerun can no longer rewrite a folder under a running transcription.**
  `exporter.rerun` also refuses a meeting still in the exporter's queue (`export in progress for
  this meeting`, `rerun already queued`) instead of printing `queued` for a request it dropped,
  and one unreachable or malformed answer no longer aborts the other ids. The export job asks
  notetaker-worker again before a rerun writes anything; if the meeting is being transcribed, no
  file is written and the reason is added to `_export.json` without touching its state; it is
  reported `failed` only over a folder never handed off. A rerun whose `/process` was accepted
  keeps its hand-off on the queue item, so a retry after a failed report finishes it instead of
  refusing its own transcription. `no_audio` and `too_many_recordings`
  exports now also write the empty index, activity frames and live transcript, and an
  `audio_mismatch` names no speaker in the log, the report or `_export.json`.
