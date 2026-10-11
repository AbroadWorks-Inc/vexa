- **aw-exporter: every activity frame is written with its channel.** The exporter no longer reads
  `"src":"channel"` to keep only Jitsi channel lines: since the bot stopped giving mixed-audio
  lines a channel number, `speaker_activity_frames.json` holds every frame with its channel (none
  for a mixed-lane frame) and the worker uses the frames whose channel has a file. On the mixed
  lane the speaker timeline reads only the mix's own frames. A Jitsi activity file from a bot before
  2026-10-11 (mixed lines on `ch: 0`) falls back to its level hints for the timeline on a rerun.
