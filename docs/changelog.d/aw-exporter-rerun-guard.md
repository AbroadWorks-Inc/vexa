- **Exporter: a rerun is refused while the meeting is being transcribed, and is never dropped.**
  `exporter.rerun` asks notetaker-worker first and refuses with `transcription in progress for this
  meeting` before anything is queued or written. A rerun asked for while the same meeting is
  exporting stays queued after that export, and a rerun out of attempts is reported `failed`
  instead of the earlier hand-off.
