- **Recordings: a long meeting's master builds in seconds and in flat memory.** meeting-api built a
  master by reading every 15-second chunk one after another and holding the whole file in memory,
  inside the `GET /recordings/{id}/master` request. A 2-hour track (about 450 chunks) took 38 s, past
  the gateway's 30 s, so the caller got a 504; several long meetings finishing together could exhaust
  the pod's memory. Finalize now reads 8 chunks at a time and writes the master in 8 MiB multipart
  parts as it goes: the same track builds in about 3 s and the master never sits in memory. The
  bytes are unchanged (the recording.v1 goldens pin them).
