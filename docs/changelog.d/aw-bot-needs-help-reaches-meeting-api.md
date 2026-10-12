- **Bot: a blocker met before the waiting room reaches meeting-api.** The bot's own transition table
  refused `joining → needs_help`, so its report for Zoom's "automated bots aren't allowed" wall was
  logged as an illegal transition and never sent (meetings 272, 273 and 352). Both sides now also
  allow `needs_help → awaiting_admission`, for a Google Meet consent gate that clears before the
  lobby. The bot names `failure_stage: awaiting_admission` only when it actually reported the lobby,
  as meeting-api already did; a join that ends `blocked` is `awaiting_admission_rejected`, so it is
  not sent again. A test now holds the bot, meeting-api and the lifecycle.v1 README to one table.
  meeting-api treats `needs_help` as pre-active everywhere (the bot never got in): a stop keeps its
  stage and deletes the workload, a lost or destroyed bot ends `failed` (not `completed`/
  `left_alone`), it counts against the concurrency cap, and a replica that rehydrates learns the
  lobby from the stored trail, so the failure stage stays truthful across replicas.
