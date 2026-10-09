- **Exporter: per-speaker channels are exported as their recorders made them.** Each channel was
  decoded to 16 kHz wav and padded with silence back to the meeting's start, and every channel was
  built on local disk before any was uploaded: meeting 195 (2 h, 19 speakers) turned ~270 MB of opus
  into ~3.5 GB of wav and needed more than the exporter's 8 GiB, so the exporter was evicted on every
  retry and no other meeting was exported meanwhile. A channel is now `channels/ch<N>.webm`, a
  server-side copy of its master (several bot sessions: joined into one opus file, one channel at a
  time), and its `channels/index.json` row names the `file` and its `offset_s`. notetaker-worker
  sends each file with its offset and the transcriber decodes one channel at a time.
