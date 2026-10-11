- **Bot: a Jitsi speaker who leaves no longer keeps a recorder running.** When a channel's track
  ends, or its stream is gone from the page, its recorder is stopped (its last chunk flushed) and its
  activity source and node are disconnected; the channel number is never reopened in the session.
