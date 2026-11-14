- **aw-bots: a LiveKit waiting room that never answers no longer holds the bot.** Each request to
  the waiting room now has a 3-second time limit; an unanswered one is retried after 4 s, like a
  network error. Before, Node's `fetch` could wait up to 300 s, so the bot lost its place in the
  waiting room, overran the lobby budget and ignored a stop for that long.
