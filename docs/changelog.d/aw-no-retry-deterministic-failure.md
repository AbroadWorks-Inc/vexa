- **meeting-api: a bot the host or the platform refuses is not sent again.** On a meeting that
  entries manage, a failed bot used to get a new bot for every reason but a stop, a host removal
  and nobody joining, so Zoom's "automated bots aren't allowed" (RTMS) wall got three bots, about
  90 seconds apart, before the meeting failed. `awaiting_admission_rejected`,
  `auth_session_missing`, `validation_error` and `max_bot_time_exceeded` now end the meeting at
  once with that reason (`bot.failed`); a lobby timeout, a join error and a crash are still
  retried. See [meeting lifecycle](../aw-bots/meeting-lifecycle.md).
