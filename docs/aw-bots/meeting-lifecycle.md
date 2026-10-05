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

`intake/retry.py` is the replacement decision. `lifecycle/retry.py` is not that path.

A failure is retried when the meeting still has active entries, nobody has asked it to stop, and
`is_bot_failure` is true:

- status `failed` (a failed start, a join failure, a lobby rejection, a lobby timeout, a crash), or
- status `completed` and the bot was lost.

These completion reasons are not retried (`NOT_RETRIED`):

| Reason | Meaning |
|---|---|
| `stopped` | A user called `POST /v2/meetings/{id}/stop`. |
| `evicted` | The host removed the bot after it was in the call. |
| `startup_alone` | Nobody joined. |

`left_alone` is a normal completion. The bot was in the call and hung up after the room emptied
or the silence window expired. It is status `completed`. It is not in `NOT_RETRIED` because a
normal completion is already outside `is_bot_failure`. It does not increment
`aw_meetings_failed_total`. See [observability](observability.md).

A replacement is sent on the same meeting, from the same send count, only after the failed pod
is proven gone. The workload id is chosen and stored before the pod is created
(`data.spawn_session`). A still-running bot is not replaced. Alive statuses include `starting`,
`running`, and `stopping`.

If the failed workload is not proven gone by `due_at` plus `MEETING_UNTRACKED_GRACE_SEC`
(default 600 seconds), the meeting ends with `workload_not_proven` or `retry_not_sent`. A spawn
that recorded a session and never wrote it ends `start_failed` after the same grace.

Google Meet admission (`core/meetings/modules/join/src/googlemeet/admission.ts`):

- The host's "denied your request" is `awaiting_admission_rejected`. The bot exits and does not post a chat into the meeting.
- Google Meet withdraws an unanswered "Ask to join" about 10 minutes after the knock. The waiting room disappears, with no denial and no in-call controls. The page logs `DisconnectedError`, `EndCause = 72`. A lobby that has been gone for `KNOCK_LOST_GRACE_MS` (20 seconds) with neither an admission nor a denial is `awaiting_admission_timeout`. The retry sends a new knock. The bot does not keep polling that page until the lobby budget ends.
- A page with no meeting and no lobby at the end of the budget is `join_failure`.

`awaiting_admission_rejected` and `awaiting_admission_timeout` are still bot failures, so an
entry-managed meeting retries them until the send budget is used. The alerts treat both as the
host's answer and store them at severity info.

## Stop and erase

`POST /v2/meetings/{id}/stop` (scope `bot`) asks the live bot to leave. The completion reason is
`stopped`. If no bot is live, the response code is `no_live_bot`.

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
