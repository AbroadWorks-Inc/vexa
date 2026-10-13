# 0012 — A bot waits 10 minutes in a lobby, on every platform

## Context

The code's lobby budget is 900 seconds. Google Meet withdraws an unanswered "Ask to join" after
about 10 minutes, so on Meet a bot kept waiting on a lobby that no longer existed.

## Decision

`VEXA_LOBBY_BUDGET_S=600` on every platform (owner, 2026-10-12). Send attempts stay at 3.

## Consequence

A bot nobody admits gives up after about 10 minutes. On Meet that matches the knock's expiry,
which ends `awaiting_admission_timeout` and is retried within the send budget; a host's denial is
not retried. The reconcile sweep's pre-active grace follows the budget (plus 60 s).

Current behavior: [meeting lifecycle](../meeting-lifecycle.md).
