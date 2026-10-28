- **Exporter: a rerun leaves no stale file.** Every run now writes every file the exporter owns:
  `channels/index.json` is `[]` for a meeting without channels or whose channel step failed,
  `speaker_activity_frames.json` is `[]` when no activity parsed, and `live_transcript.json` has no
  segments when the bot had no live transcription. Before, those files were skipped, so an earlier
  run's copy stayed in the folder. notetaker-worker removes, on a rerun, the channel files the index
  does not name.
