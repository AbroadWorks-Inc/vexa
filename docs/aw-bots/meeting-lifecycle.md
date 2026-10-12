# Meeting lifecycle

How an entry becomes a meeting, when a bot is sent, and which endings are replaced. Field-by-field
requests are in the [`/v2` API reference](../../core/meetings/services/meeting-api/V2-API.md).
The code is `meeting_api/intake/` and `meeting_api/bot_spawn/auto_join.py`.

## Entry and meeting

An **entry** is one sender's row: one calendar invite, or one paste with `join_now: true`.
Several entries in the same account can point at one **meeting**. Three colleagues with the same
link are three entries, one meeting, one bot.

A meeting is one link at overlapping times, inside one account. A second account's meeting on the
same link is a different row. The meeting's id in webhooks, exported files, and `POST /process`
is its UUID. Recordings and some internal keys still use the integer `meetings.id`.

`PUT /v2/entries` creates or updates. `POST /v2/entries/remove` removes one entry. A content hash
lets a sender push the same entry again and change nothing.

`ENTRY_BLOCKED_HOSTS` is empty (the code default and our Helm value). A Jitsi link on a host in
`VEXA_JITSI_HOSTS`, including `meet.abroadworks.com`, takes the same `PUT /v2/entries` path as
Meet, Teams, and Zoom. A host on the block list is still `platform_not_enabled`.

## When the bot goes

For a planned meeting, the auto-join sweep sends the bot `AUTO_JOIN_LEAD_S` seconds before
`scheduled_at`. The code default is 120 (`bot_spawn/auto_join.py` `DEFAULT_LEAD_S`). Our Helm
value `meetingApi.autoJoinLeadSeconds` is 300, so the deployed process uses 300. The full table
is in the [README](../../README.md#configuration).

A `join_now` entry adopts a meeting on that link whose start is within
`JOIN_NOW_ADOPT_AHEAD_S` (default 3600 seconds) instead of opening a second meeting. Upstream
`POST /bots` uses the same adopt window.

One live bot per link. A meeting that becomes due while another bot is still on its link waits,
and its bot goes when the link is free.

## Send budget

Meetings that entries manage get `BOT_SEND_MAX_ATTEMPTS` sends (default 3),
`BOT_SEND_RETRY_BACKOFF_S` apart (default 60 seconds). The count is
`meeting_aw_state.send_attempts`. The last failed send ends the meeting `not_sent` with a typed
code and message, and emits `meeting.not_sent`.

`due_at` in `intake/retry.py` refuses another send when the next attempt would be at or after the
meeting's end, or when the attempt count is already at the maximum.

`AUTO_JOIN_RETRY_BACKOFF_S` (default 300 seconds) is a different clock. It belongs to the older
path for a scheduled meeting that has no entries. Calendar sync and `join_now` use the 60-second
budget above.

## A failed bot on a live meeting

`intake/retry.py` is the replacement decision, and its `NOT_RETRIED` is the one list of reasons
that are not retried (the calendar occurrence rule, `lifecycle/occurrence.py`, reads it too).

A failure is retried when the meeting still has active entries, nobody has asked it to stop, and
`is_bot_failure` is true:

- the bot session ended `failed` (a failed start, a join failure, a lobby timeout, a crash), or
- the session ended `completed` and the bot was **lost**: the runtime saw its workload gone and
  the bot never reported an end of its own (`bot_spawn/adapters.py`, `transition_source`
  `runtime_destroy`, and the meeting was not `stopping`).

These completion reasons are not retried (`NOT_RETRIED`), because a new bot would get the same
answer. The meeting ends with that reason: `failed` with `bot.failed`, or `completed` with
`meeting.completed` when the bot reported it that way (an eviction):

| Reason | Meaning |
|---|---|
| `stopped` | A user called `POST /v2/meetings/{id}/stop`. |
| `evicted` | The host removed the bot after it was in the call. |
| `startup_alone` | Nobody joined. |
| `awaiting_admission_rejected` | The host denied the bot, or the platform refused it (Zoom: "automated bots aren't allowed", which needs Zoom RTMS), or the join ended blocked (`needs_help`) without admission. The bot maps a `blocked` join to this reason (`bot/src/orchestrator.ts` `OUTCOME_FAIL`). |
| `auth_session_missing` | The bot's signed-in profile is signed out. |
| `validation_error` | The link is not a meeting the bot can join. |
| `max_bot_time_exceeded` | The bot reached its time cap. |

### `left_alone`: two writers, two answers

`left_alone` is not in `NOT_RETRIED`, and the same reason is retried or not depending on who wrote it:

| Who wrote `left_alone` | What happened | Retried |
|---|---|---|
| The bot itself, status `completed` | The bot was in the call and hung up after everyone left or the silence window expired. This is the normal end. | No: a `completed` the bot reported is not a bot failure, so `is_bot_failure` is false. The meeting ends `completed` and `meeting.completed` is sent. It does not move `aw_meetings_failed_total` ([observability](observability.md)). |
| meeting-api's reconcile (`lifecycle/reconcile.py`), status `completed` or `failed` with `failure_stage: active` | The bot was in the call (`active`) and its workload disappeared without the bot reporting an end: a lost bot. | Yes, like any bot failure: `bot.retry` with `change.reason: "left_alone"`. If the send budget is spent, the lost bot's `completed` is written as `failed`. |

A lost bot that had not reached the call is filed by its stage instead: `awaiting_admission_timeout`
from the lobby, `join_failure` from `requested`, `joining` or `needs_help`, or `stopped` when a
stop was under way. Both stage reasons are retried.

A replacement is sent on the same meeting, from the same send count, only after the failed pod
is proven gone. The workload id is chosen and stored before the pod is created
(`data.spawn_session`). A still-running bot is not replaced. The runtime states that count as
still running include `starting`, `running`, and `stopping`.

If the failed workload is not proven gone by `due_at` plus `MEETING_UNTRACKED_GRACE_SEC`
(default 600 seconds), the meeting ends with `workload_not_proven` or `retry_not_sent`. A spawn
that recorded a session and never wrote it ends `start_failed` after the same grace.

Google Meet admission (`core/meetings/modules/join/src/googlemeet/admission.ts`):

- The host's "denied your request" is `awaiting_admission_rejected`. The bot exits and does not post a chat into the meeting.
- Google Meet withdraws an unanswered "Ask to join" about 10 minutes after the knock. The waiting room disappears, with no denial and no in-call controls. The page logs `DisconnectedError`, `EndCause = 72`. A lobby that has been gone for `KNOCK_LOST_GRACE_MS` (20 seconds) with neither an admission nor a denial is `awaiting_admission_timeout`. The retry sends a new knock. The bot does not keep polling that page until the lobby budget ends.
- A page with no meeting and no lobby at the end of the budget is `join_failure`.
- Google Meet's "take notes for me" consent prompt is a blocker: the bot reports `needs_help`, does
  not click it, and keeps polling. Admission continues once someone answers it.

`awaiting_admission_timeout` is a bot failure, so an entry-managed meeting retries it until the
send budget is used. `awaiting_admission_rejected` is not retried. The alerts treat both as the
host's answer and store them at severity info.

## Blockers: `needs_help`

A bot reports `needs_help` when something stands between it and the call. Zoom's
automated-bot wall and the Meet consent prompt can appear before any lobby, so `needs_help` is
legal from `joining` as well as from `awaiting_admission`. From `needs_help` the bot goes to
`awaiting_admission` (the blocker cleared and it reached the lobby), `active`, or `failed`. The
table is the same in the bot (`bot/src/contracts.ts`), in meeting-api (`lifecycle/machine.py`
`LEGAL_TRANSITIONS`) and in `core/meetings/contracts/lifecycle.v1/README.md`.

`failure_stage` names `awaiting_admission` only when the bot really stood in the lobby. The bot
tracks whether it ever reported `awaiting_admission` (`orchestrator.ts` `preActiveStage`);
meeting-api derives the same from the session's own status history, not from the payload. When a
replica reloads a session it rebuilds that history from the stored `status_transition` trail, so
a lobby another replica recorded still counts. A Zoom wall met right after joining fails at
`joining`.

`needs_help` is a pre-active status everywhere: the reconcile sweep treats a lost `needs_help` bot
like one still joining, the account's bot limit (`count_active_bots`) counts it, and a stop leaves
its status and stage as they are and deletes its workload directly.

The failed bot's evidence (`data.join_evidence`) is what the v2 meeting shows as `join_failure`:
its `reason`, `attribution` and `detail`, only on a `failed` meeting not ended by a user stop. The
exact rule is in the [`/v2` API reference](../../core/meetings/services/meeting-api/V2-API.md#the-meeting-object).

## Stop and erase

`POST /v2/meetings/{id}/stop` (scope `bot`) asks the live bot to leave. The completion reason is
`stopped`. A bot in the call (`active`) goes `stopping`. A bot not yet in the call (`requested`,
`joining`, `awaiting_admission`, `needs_help`) keeps its status and stage, gets the leave command,
and has its workload deleted (`intake/stop.py`). A meeting waiting for a replacement bot ends
`failed` with `stopped` at once. If no bot is live, the response code is `no_live_bot`.

`DELETE /v2/meetings/{id}` (scope `erase`) runs only when the meeting status is finished.
What it deletes is in [security](security.md#erasure).

## Sweeps

Intake sweeps and the reconcile loops read in pages of `SWEEP_BATCH_SIZE` (default 200). An item
that fails `SWEEP_MAX_ITEM_FAILURES` times (default 5) gets `gave_up_at` on
`sweep_item_failures` and is skipped while that stamp is set (`sweeps/item_failures.py`).
Untouched give-up rows are pruned after `SWEEP_ITEM_FAILURES_RETENTION_S` (README default
604800 seconds).

A runtime that does not answer is counted `runtime_unreachable` and is not that item's failure.
A teardown in that state is chased until `UNPROVEN_TEARDOWN_MAX_AGE_S` (default 21600 seconds).

An entry write that loses a database race is run again `INTAKE_CONFLICT_RETRIES` times (default
3), then answered 500.
