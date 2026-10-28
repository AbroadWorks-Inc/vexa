- **Bot: a Jitsi channel survives a brief detach.** A channel's recorder was released the first
  time a 2 s scan missed its stream, and a released channel is never reopened, so a stream detached
  for a few seconds lost that speaker for the rest of the meeting (shown in real Chromium: a 3 s
  detach). A channel is now released when its track ends or its stream has been gone for 30 s
  (`VEXA_CHANNEL_GONE_MS`). New Chromium boundary test: two speakers, identity on every chunk,
  worklet loudness, a 3 s detach survives, a leaving speaker is released.
