# 0007 — Replace a failed bot on the same meeting

## Context

A bot can fail before it is admitted, or die during the call. Starting a second meeting would put
two bots on one link, or would drop the audio the first bot already stored.

## Decision

While entries still manage the meeting, a bot failure sends the same meeting back to `requested`
(`bot.retry`) after the failed pod is proven gone, up to `BOT_SEND_MAX_ATTEMPTS`, and only if the
next send is due before the meeting ends.

A user stop, the host removing the bot, nobody joining, and a normal end (`left_alone`) are not
replaced.

## Consequence

The exporter joins every session of that meeting into one folder. A host denial is retried until
the send budget is used. The alert for that reason is severity info: it is stored, and it is not
paged, because the bot did reach the lobby.

Note, 2026-10-12: a host denial is no longer retried. `awaiting_admission_rejected` (the host
declining the bot, the platform refusing it, or a join that ended blocked) is in `NOT_RETRIED`; a
lobby timeout is still retried. `left_alone` reported by the bot is the normal end and is not
replaced; `left_alone` that meeting-api writes for a bot lost in the call is replaced.

Current rules: [meeting lifecycle](../meeting-lifecycle.md).
