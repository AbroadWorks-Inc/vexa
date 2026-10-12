- **Bot: stopping per-speaker recorders is parallel and complete.** Stop flushed each channel's
  recorder one after another (up to 10 s each); they now stop together. A recorder a scan opened
  while stop ran is no longer left running, a channel released while its activity tap was being
  made no longer keeps that tap, and stop waits for a leaving speaker's last chunk, so no final
  chunk arrives after the upload sink closes (the duplicate seq behind
  `recording_chunk_count_mismatch`).
