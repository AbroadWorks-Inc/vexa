- **meeting-api: a speaker leaving does not complete the recording.** A Jitsi or Google Meet
  channel's final chunk (`ch<N>`, sent when its speaker leaves) used to mark the whole recording
  `completed` mid-meeting and log `recording_completed`. It now finalises that channel's media file
  only; the mixed lane's final completes the recording, and a recording of only channels (the mix
  was not recorded) completes when its bot session ends. A `ch<N>` chunk with a `null` `chunk_seq`
  is refused with 422 (after auth), channel names over 200 characters are kept cut to 200 by the
  bot and meeting-api instead of dropped, and an unknown `channel_kind` is logged
  (`recording_channel_kind_unknown`).
