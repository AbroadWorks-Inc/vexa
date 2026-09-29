# intake — entry handling, the meeting projection, request validation and the status writer (§1.1, §1.3, §1.4, §2, §2.4)

`project_meeting(meeting, aw, entries, *, lead_s)` renders one aw-bots `meeting` object — the
exact §2.4 shape every reply, read and webhook uses. Pure: no DB, no clock, no network; every
value comes from the `meetings` row, the `meeting_aw_state` row (or `None`), and the meeting's
`meeting_entries` rows, all passed in as mappings. A finished (`completed`/`failed`) meeting lists
its closed entries, any other meeting its active ones; removed entries are never listed (R9).

`parse_entry(body, *, now, max_days_ahead)` / `parse_remove(body)` turn a `PUT /v2/entries` /
`POST /v2/entries/remove` body into a normalised `EntryIn` / `RemoveIn`, or raise `IntakeError`
(§2.5) — validated against the `intake.v1` contract (`core/meetings/contracts/intake.v1/`).

`write_status(db, meeting_id, to_status, *, expected_from, ...)` is the one writer of
`meetings.status` (§1.4). In the caller's transaction it locks the meeting row (`StatusConflict`,
writing nothing, if the status isn't expected), writes the status and data patch, locks
`meeting_aw_state` (created if missing) to set the outcome and bump `event_seq`, closes the active
entries on `completed`/`failed` (an entry moved to a new time while live has already left, R7), and
inserts one `webhook_outbox` row whose `payload_text` is the exact §2.7 envelope that gets sent.
Its event type is the caller's, else `typed_event` of the change (Ruling R25): `meeting.started`
on `active`, `meeting.completed` on `completed`, `meeting.not_sent` / `bot.failed` on `failed`
(by whether the outcome is `not_sent`), `meeting.status_change` otherwise; `data.change` is always
there, a meeting's first event included (`creation_change`).
`write_event(db, meeting_id, event_type, change)` records a non-status event the same way.
`write_status(..., event_data=...)` adds keys to the envelope's `data` next to `meeting` and
`change` (`merged_into` on a merge's `meeting.removed`, §2.7), never replacing those two.
`insert_meeting(db, ...)` creates a `meetings` row with its `meeting_aw_state` row and its first
event (a new row's status is its first change). Every writer of `meetings.status` in meeting-api
goes through these two (intake, the bot-spawn repo's claim, reopen, `fail_meeting`, lifecycle and
service-authority writes, the collector's planned create, `set_intent` and planned edit), holding
the meeting's link lock first. The lifecycle write changes a status only from its caller's
predecessors (the edge's `from` for the callback, the live statuses otherwise) and never off
`completed`/`failed` (`adapters.take_link_lock`, or `lock_meeting_on_its_link` for a
writer that knows the meeting by id); `tests/test_status_writers_all.py` fails on any other write.
A `data` patch whose `completion_reason` is outside the sealed `lifecycle.v1` set is refused before
anything is written (`check_completion_reason`; upstream's `start_failed` on a `failed` row is the
one exception). `project_stored(db, meeting_id)` is the one projection of a stored meeting, which
the legacy system and per-user webhooks' meeting block takes `uuid`, `entries`, `outcome` and
`sequence` from (§1.8).

`rules.py` holds the R1 matching rules and the meeting windows (§1.1, R7, R10), pure: `overlaps`
(half-open, a missing end is unbounded), `meeting_start`, `meeting_window`, `match_entry`,
`join_now_target`, `recompute`, `finished_window` / `is_future_move` (R7: an update that moves the
entry away from a live or finished meeting to a new future time), and `is_overdue` (R6, the
not-sent sweep's end). It is the one definition of these windows.

`resolver.py` is the one link resolver (§1.6): which meeting an upstream route that takes a link
(platform + room code) means. `resolve(rows, kind, *, now)` over the link's rows as `LinkRow`
(status, `meeting_start`, `created_at`, whether entries manage it): `READ` (transcript,
participants, `/ws/authorize-subscribe`, chat, annotate, docs, `continue_meeting`) is the live
meeting, else the most recent that has started (left planning, or its start is past), never a
future one; `PLANNED_EDIT` (`PATCH`/`DELETE /meetings/{p}/{n}`, intent, workspace, share) is the
live meeting, else the single planned (`scheduled`/`idle`) one that isn't stale (a timed,
entry-less `scheduled` plan past its `data.scheduled_at` plus `AUTO_JOIN_GRACE_S` is ignored; idle
and untimed plans never go stale, Ruling R22), several raising
`AmbiguousRoom`, and with neither the `READ` answer; `STOP`
(`DELETE /bots/{p}/{n}`) is the live meeting only and never a plan. `adapters.link_rows(db, user_id,
room)` reads the rows from Postgres and `fakes.link_rows_in` is its twin; the collector store, the
bot-spawn repo and the stop route all resolve through them. `ManagedByEntries` is the refusal of an
upstream edit (`PATCH`/`DELETE /meetings/…`, `PUT …/intent`) to a meeting with a `meeting_entries`
row; the upstream routes answer both with 409 and the code as `detail`. The collector store's
`update_planned_meeting` / `delete_planned_meeting` refuse such a row under the row lock (Ruling
R21), so upstream calendar sync skips it.

`IntakeService` (`service.py`) is the behaviour of `PUT /v2/entries` and `POST /v2/entries/remove`
(§1.3, every §2.6 case): under the entry's link lock (both links, sorted, when the link changes;
restarted once when the entry's link changed before the read), it answers `unchanged`, attaches
an entry to the meeting R1 matches or creates one, moves an entry of a live meeting that the
update points to a new future time (from the later of now and the planned end) to its R1 meeting
at once, leaving its row `closed` on the live meeting as history and the bot in the call (R7),
otherwise keeps a live meeting as it is (`not_changed_live`), treats a finished meeting as history
unless the entry points to a new future time, removes a meeting that lost its last entry (R8) or stops its bot when live (R5), and checks
the active-entry quota only when a write adds an entry. Events are published after the commit;
a `join_now` entry's meeting is then spawned on that exact row (a failure ends it `not_sent`,
unless it was adopted with other entries on it: then only the pasted entry goes, Ruling R12). `merge_into_live` (R2's exception)
is for the scheduler: the auto-join tick merges. An entry has at most one row that isn't `closed`
(the partial unique index `uq_meeting_entries_user_source_external`); closed rows are history, and
the projection lists each entry of a meeting once, its active row first (R9). The service reaches
storage, spawn, stop and publishing only through `ports.py` (`IntakeStore`/`IntakeTx`,
`SpawnPort`, `StopPort`, `EventPublisher`); `fakes.py` holds the in-memory implementations.
`PostgresIntakeStore(session_factory)` (`adapters.py`) is the `IntakeStore` over Postgres: each
`room_lock` is one transaction holding the links' advisory locks, taken in sorted order
(`pg_advisory_xact_lock(hashtextextended('aw-intake:'||user_id||':'||platform||':'||native, 0))`,
§1.4) and released at commit or rollback; inside it the lock order is the link, then the meeting
row, then `meeting_aw_state`, and status changes and events go through `write_status` /
`write_event` in the same transaction. `room_meetings` returns the link's live meetings and the
non-finished meetings entries manage, never an entry-less upstream-planned row (Ruling R15);
`count_active_entries` is an index-only count on `ix_meeting_entries_active_user`.
`build_intake_router(...)` (`router.py`) is the `/v2` meeting routes of §2.1: `PUT /v2/entries`,
`POST /v2/entries/remove`, `GET /v2/entries`, `GET /v2/meetings`, `GET /v2/meetings/{id}`,
`POST /v2/meetings/{id}/stop`, `DELETE /v2/meetings/{id}` and `POST /v2/meetings/{id}/export`. Every success body is an `intake.v1`
shape and every failure the §2.5 body; the route class scopes that error handling to these routes,
so a validation failure is 400 `invalid_request` here while upstream routes keep 422, and a
database that can't be reached (or a write that lost a unique-key race) is 503 `unavailable`. A
`user=` read sees a meeting when one of its entries, in any state, names the user as its `user` or
an attendee. Stop calls the `StopPort` for a live meeting and answers 409 `no_live_bot` otherwise.
Erasure runs upstream's `delete_completed_artifacts` (objects first, then transcripts), then
`IntakeReads.erase` removes the meeting's delivery, outbox and entry rows in one transaction; the
meeting row and `meeting_aw_state` stay and no event is written. The export result (§1.9,
`{state: "handed_off"|"failed", s3_path, error?}`, validated by `export.parse_export`) is taken only for
a finished meeting of the account (`meeting_not_found` otherwise, `meeting_not_finished` for a
scheduled or live one): `IntakeReads.record_export` stores it on `meeting_aw_state.export_*` and
writes `export.handed_off` / `export.failed` through `write_event`, under the link lock, the meeting
row and then `meeting_aw_state`; the stored state and path again write nothing. The reply is the
meeting. The reads, erasure and export result go through
`IntakeReads` (`ports.py`): `PostgresIntakeReads` (`reads.py`) over Postgres, `InMemoryIntakeReads`
in `fakes.py`. `GET /v2/entries` pages by `external_id` (served by
`uq_meeting_entries_user_source_external`); `GET /v2/meetings` pages by `(meeting_event_time, id)`,
newest first. meeting-api's production app (`__main__.build_production_app` → `create_app`) mounts
the router over `PostgresIntakeStore`, `PostgresIntakeReads`, `ExactRowSpawn`, `IntakeStop` and the
outbox-only publisher; the same entry service is the scheduler's. The routes are reached only
through the gateway, which checks the scope and sets `x-user-id`.
`ExactRowSpawn` (`spawn.py`) is the production `SpawnPort` (§1.5): `spawn_exact(user_id, meeting_id)`
runs `bot_spawn.request_bot` with `claim_meeting_id`, so the spawn claims exactly that row
(`scheduled` → `requested` through `write_status`, under the link lock, then the per-user lock, then
the row) and stamps `data.auto_join_last_attempt` with the send time, which `bot_joins_at` shows.
It answers `sent`, `already_live` (a bot already owns the link) or `failed` with the code and message
of `spawn_failure(exc)`, the one table from a spawn exception to its §1.13 code (`account_limit`,
`already_live`, `meeting_stopped`, `spawn_error`, `authority_denied`, `authority_unavailable`,
`auth_session`, `transcription_config`, else `internal_error`, logged with its stack); it never
raises. A failure after the claim ends the claimed meeting `not_sent` with that code and message,
under the link lock (Ruling R17): a row still `requested` goes `failed` through `write_status`; a row
the spawn flow already ended (a runtime spawn failure, the stop fence) carries that outcome in the
flow's own `meeting.not_sent` event, so nothing more is written; a row still `requested` whose
workload exists (`bot_container_id` set) is left to its bot and logged. The entry service then
replies with that meeting (R12 applies only to a failure before the claim). The per-user bot limit comes from `fetch_bot_context`, as for the auto-join sweep, and is
never guessed: without it, or without `max_concurrent` in it, the spawn fails `internal_error`
(the one exception is the sweep's `AUTO_JOIN_ALLOW_UNCAPPED` opt-in with no identity edge).
`retry.py` is §6.9 F-K2: a bot that fails while its meeting is on gets a new bot on the SAME
meeting. `due_at` is the decision: a bot failure (a `failed` session whatever its reason but
`stopped`, `evicted` and `startup_alone`, or a `completed` the runtime drove, a lost bot) on a
meeting entries manage that nobody ended (no `stop_requested`, no outcome, live and not
`stopping`), within `BOT_SEND_MAX_ATTEMPTS` (`send_attempts`, shared with F-K) and with the next bot
`BOT_SEND_RETRY_BACKOFF_S` from now before the planned end. `retry(tx, …)` is the one writer: one
of the meeting's sends (`record_send_failure`), then `requested` through `write_status` with
`bot.retry` and the marker `data.bot_retry` (`reason`, `stage`, `message`, `after_session`,
`workload`, `at`, `due_at`, `proven_gone`); the row's `completion_reason` stays empty while it
waits. Three writers call it: the session-keyed lifecycle write (`update_meeting_status`; a lost bot
on the last attempt ends `failed`; a session with no recorded workload is known by
`workload_id_for`), `fail_meeting` and `ExactRowSpawn`'s post-claim ending. A workload is recorded
proven gone only on evidence: the runtime refused it (429, 4xx, a dead body), its teardown was
confirmed, or the failure came before the create was called (the token, the invocation, the spec:
no workload is named). A create the runtime did not answer (a timeout, a transport error, a 5xx,
the request cancelled mid-create) or a post-spawn write failure with an unconfirmed teardown names
the workload unproven; when that failure ends the meeting instead of retrying it, the workload is
deleted through the reconcile sweeps' teardown, and a delete not confirmed stays on the finished
row (`data.unproven_teardown`) for the reconcile sweep to retry each pass, bounded by §6.9 F-I
(`retry_unproven_teardowns`, sweep `unproven-teardown`: cleared on a confirmed delete, the runtime
reporting it gone, or a 404 past `MEETING_UNTRACKED_GRACE_SEC`; given up and counted after
`SWEEP_MAX_ITEM_FAILURES`). The spawn port's own ending (which
knows no workload) never records one gone. The last failure ends `failed` with
`bot.failed` and `aw_meetings_failed_total`; on a meeting that already had a bot session its
`not_sent` outcome is dropped. A row waiting for its next bot takes no status write from a session
(data-only writes still land), a session that isn't the meeting's newest writes nothing, and the
entry service answers a new instant join sent back this way `created`. The auto-join tick drives
the waiting meetings (`list_retry_meetings`): a stop (`stop_requested`) or a passed planned end
ends one `failed` (`retry.end`, the kept reason, or `stopped`); after `due_at` the failed workload
is proven gone (`lifecycle.reconcile.prove_workload_gone`: reported terminal, a confirmed delete,
or untracked for `MEETING_UNTRACKED_GRACE_SEC` since the failure; a runtime destroy callback proves
it too) and a new session is spawned on the row through `ExactRowSpawn`. The claim takes a
`requested` row whose marker is proven, with no status change (`retry.claimed` moves the failure
into `completion_history`), leaving the row out of its own duplicate check, bot limit and signed-in
session check. A new bot that fails before its claim is one more bounded send. A stop of a
waiting meeting (`record_stop`: the `/v2` stop, R5) ends it at once, `failed` with `stopped` and
the outcome, and sends no leave command; a removal of its last entry answers `removed`. Its
`bot_joins_at` is the retry's `due_at`, and the reconcile sweep's listing leaves it out. A waiting
meeting is bounded: at `due_at` + `MEETING_UNTRACKED_GRACE_SEC` (`retry.deadline`) one still
unproven, or still without its new bot, ends `failed` (`workload_not_proven` / `retry_not_sent`),
by the retry driver or, if the driver gave the item up, by the reconcile sweep
(`end_overdue_retries`, `repo.end_retry`). A meeting the retry ends this way (its planned end, a
stop, its deadline, its last send) gets the same meeting-level finish as a lifecycle end: the app's
`finish_meeting` (transcript finalized, service provenance, `bot.failed` to the system hook, the
copilot reap), keyed by its last bot session. Every
`bot.retry` moves `aw_bot_retries_total{reason,user_id}` once its transaction commits.
`IntakeStop` (`stop.py`) is the production `StopPort` (§1.7), behind `POST /v2/meetings/{id}/stop`
(no outcome: the meeting ends with upstream's `stopped`) and R5 (outcome `cancelled_by_calendar`
with the remove reason). Under the meeting's link lock it locks the meeting row, then
`meeting_aw_state`: a bot that reached the meeting goes `stopping` through `write_status` with
`data.stop_requested` and the outcome, so every later event carries it; a bot still booting
(`requested`, `joining`, `awaiting_admission`) keeps the stage it reached and gets only the flag and
the outcome (`IntakeTx.mark_stop_requested`). Those steps are `record_stop(tx, meeting_id, outcome)`,
the one implementation: R5 calls it inside the removal's own transaction, so the removed last entry
and its stop commit together, and the `/v2` stop opens a transaction for it. After the commit,
`IntakeStop.leave` (`lifecycle.stop_router.stop_meeting_row`) publishes the leave command on
`bot_commands:meeting:{id}` and deletes the workload of a bot still booting; when R5's leave fails,
the removal still answers `bot_stopping` and the stale-stopping reconcile sweep ends the bot. A meeting with no live bot, or already stop-requested, is left as it is; a command bus
that can't be reached is 503 `unavailable`, with the stop already recorded.
`sweeps.py` is the scheduler's intake side (§1.5, R2, R6). `check_room` reads a due entry-managed
meeting and its link again under the link lock: `free`, `gone`, `merge` (the live meeting is an
open-ended meeting with an active `join_now` entry whose bot isn't `stopping`, `is_merge_target`,
the same predicate `merge_into_live` re-checks: `merge_into_live`), or `waiting` (any other live
bot, a leaving one included) — `meeting.waiting_for_room` goes out
once (`meeting_aw_state.waiting_for_room_sent_at`) and no retry pause is stamped, so the bot goes on
the first tick after the link is free. `not_sent_tick` (every `NOT_SENT_SWEEP_INTERVAL_S`,
single-flight) ends every `scheduled` entry-managed meeting past its end (`IntakeStore.overdue_meetings`,
`rules.is_overdue`; an open-ended one has no end to pass) `failed`,
outcome `not_sent`, under its link lock, with detail `last_error_code` (message `last_error_message`),
else `room_busy` ("another bot was still on this meeting link when the meeting ended"), else
`ended_before_sent` ("the meeting ended before a bot was sent"). A bot send for an entry-managed
meeting (the scheduler's, or a `join_now` entry's before the claim) is bounded (§6.9 F-K,
`IntakeService.send_failed`): each failure adds 1 to `meeting_aw_state.send_attempts` with its typed
code and exact message and holds the next send `BOT_SEND_RETRY_BACKOFF_S`, and the
`BOT_SEND_MAX_ATTEMPTS`-th ends the meeting `not_sent` (`meeting.not_sent`); the meeting's end stays
the outer bound. `JOIN_NOW_ADOPT_AHEAD_S` is only the look-ahead a pasted link adopts by.
`IntakeSettings.from_env()` (`settings.py`) reads `ENTRY_MAX_DAYS_AHEAD`, `JOIN_NOW_ADOPT_AHEAD_S`,
`AUTO_JOIN_LEAD_S`, `ENTRY_BLOCKED_HOSTS`, `INTAKE_MAX_ACTIVE_ENTRIES`, `BOT_SEND_MAX_ATTEMPTS` and
`BOT_SEND_RETRY_BACKOFF_S`, all declared in `config.v1.json`.

`OutboxPublisher` (`outbox.py`, §1.8) turns unpublished `webhook_outbox` rows into
`webhook_deliveries` rows: single-flight, every `WEBHOOK_PUBLISH_INTERVAL_S`, a page of
`SWEEP_BATCH_SIZE` rows at a time, oldest first, every page each tick. Each account's subscriptions
come from admin-api's internal read (cached 30 s; an account whose read fails waits for the next
tick). In one transaction per page it re-reads
`webhook_subscriptions.active` under `FOR SHARE`, inserts one `pending` delivery per matching active
subscriber (`ON CONFLICT (event_id, subscription_id) DO NOTHING`) and sets `published_at`, so a
crash before the commit is redone without duplicates and a concurrent pause in admin-api is either
seen or cancels what was inserted. A page whose transaction fails is published row by row, and a
row that keeps failing is given up (below). `webhook.test` rows are never fanned out.

The three intake sweeps (the auto-join tick, the not-sent sweep, the publisher) are bounded the same
way (§6.9 F-I, `sweeps/item_failures.py`): pages of at most `SWEEP_BATCH_SIZE` in a stable order,
and each item through `run_item`, so one item's failure is logged with its id and stack, counted in
`aw_sweep_items_total{sweep,result}`, and never stops the rest; after `SWEEP_MAX_ITEM_FAILURES`
(counted per sweep in `sweep_item_failures`, shared by the replicas) the item is given up.
`PostgresWebhookTests` backs `POST /internal/webhooks/test` (the route is
`webhooks/internal_router.py`, internal secret): one `webhook.test` outbox row (sequence 0, `evt_test_<uuid4 hex>`, already published) and
one delivery for that subscription, in one transaction; the reply is `{event_id}`.

## Front door
- `project_meeting` — `projection.py`.
- `parse_entry`, `parse_remove`, `EntryIn`, `RemoveIn`, `IntakeError` — `validation.py`.
- `write_status`, `write_event`, `StatusConflict`, `Outcome`, `WrittenEvent`, `derive_event_id_v2`,
  `row_mapping` — `status.py` (with `insert_meeting`, `project_stored`, `check_completion_reason`).
- `IntakeService` — `service.py`; `IntakeSettings` — `settings.py`.
- `ExactRowSpawn`, `spawn_failure` — `spawn.py`.
- `IntakeStop`, `record_stop` — `stop.py`.
- `retry`, `due_at`, `Failure`, `marker` (§6.9 F-K2) — `retry.py`.
- `IntakeStore`, `IntakeTx`, `EntryView`, `MeetingView`, `Room`, `SpawnOutcome`, `SpawnPort`,
  `StopPort`, `EventPublisher` — `ports.py`; the in-memory fakes — `fakes.py`.
- `PostgresIntakeStore` — `adapters.py`.
- `build_intake_router` — `router.py`; `IntakeReads`, `MeetingQuery`, `ErasedRows`, `ExportReport` —
  `ports.py`; `parse_export`, `ExportIn`, `store_export` (the export result's body and Postgres
  write) — `export.py`;
  `PostgresIntakeReads` — `reads.py`.
- `OutboxPublisher`, `fan_out`, `PostgresWebhookTests`, `SubscriptionNotFound`, `WebhookTests` —
  `outbox.py`.
- The matching rules — `rules.py` (used inside the package).
