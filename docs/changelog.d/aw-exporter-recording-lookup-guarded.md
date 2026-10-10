- **Exporter: one recording that can't be read no longer blocks the mixed export.** The full
  recording (which holds each channel's identity) is read inside the channel step, under its guard:
  a failure there costs the per-speaker files, never the mixed folder or `/process`.
