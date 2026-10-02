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

Current rules: [meeting lifecycle](../meeting-lifecycle.md).
