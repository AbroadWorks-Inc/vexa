- **A speaker's identity travels on every channel chunk.** The bot sends the channel's identity with
  every chunk (the name looked up when the chunk is sent), and meeting-api fills a field only when it
  is still absent and never blanks or renames it. A lost or re-sent chunk 0 no longer costs a
  channel its name and place, and a name Jitsi resolves after the recorder started is stored. A
  channel chunk without `chunk_seq` is refused (422); a whole-recording upload keeps its default.
