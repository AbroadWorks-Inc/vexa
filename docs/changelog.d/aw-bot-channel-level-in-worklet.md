- **Bot: a Jitsi channel's loudness is measured in its audio worklet.** For every speaking channel,
  about four times a second, the page copied a 4,096-sample block into a JSON array and sent it to
  Node, which kept one loudness value. A level worklet now computes each block's RMS and peak on the
  audio thread and the page forwards one value per block past the silence gate; the
  `speaker-activity.jsonl` line is unchanged.
