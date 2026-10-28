- **Bot: mixed-audio activity lines carry no channel number.** On the mixed lanes (Jitsi, Teams) a
  line was written with `ch: 0`, the same number as a real speaker channel 0. A frame now carries a
  channel number only when it belongs to a channel.
