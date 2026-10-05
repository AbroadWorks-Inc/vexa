- **Bot: Teams and Jitsi recordings now carry the meeting.** The mixed-lane recorder records the
  same live audio graph the speaker capture reads, built at admission, so a participant track that
  arrives after the bot joined is in the recording. Until now the recorder snapshotted the page's
  media elements once at admission; on Jitsi that snapshot held the page's preloaded sound effects or
  the bridge's silent placeholder track and no participant, so every Jitsi recording was silent and
  the exporter refused it as `audio_mismatch`. One audio graph, two sinks, closed by whichever of
  capture or recording stops last. The graph renders silence from admission (a constant-zero
  source), so the recording's clock starts when the bot joins, not when the first participant's
  track arrives; the Zoom per-track graph gets the same. Pinned by
  `recording-source.boundary.test.ts`.
