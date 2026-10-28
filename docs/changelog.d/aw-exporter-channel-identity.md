- **Exporter: per-speaker channels keep their speaker and their place in the meeting.** The
  exporter read channel identity (`channel_kind`, display name, recorder start) from the
  `GET /recordings` list, which leaves out each media file's `metadata`, so every channel was
  exported as a Meet channel starting at the meeting's start. On Jitsi that put late joiners'
  audio at the wrong time and left every speaker unnamed. It now reads each recording in full
  (`GET /recordings/{id}`), and a channel without its recorder identity is not exported (logged as
  `channel_export_failed … ChannelIdentityMissing`) instead of being guessed.
