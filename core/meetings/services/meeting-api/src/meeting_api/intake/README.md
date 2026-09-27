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
entries on `completed`/`failed` (returning a future, non-overlapping entry's id for re-run, R7), and
inserts one `webhook_outbox` row whose `payload_text` is the exact §2.7 envelope that gets sent.
`write_event(db, meeting_id, event_type, change)` records a non-status event the same way.
`write_status(..., event_data=...)` adds keys to the envelope's `data` next to `meeting` and
`change` (`merged_into` on a merge's `meeting.removed`, §2.7), never replacing those two.

`rules.py` holds the R1 matching rules and the meeting windows (§1.1, R7, R10), pure: `overlaps`
(half-open, a missing end is unbounded), `meeting_start`, `meeting_window`, `match_entry`,
`join_now_target`, `recompute`, `finished_window` / `is_rerun` / `is_future_move`, and `is_overdue`
(R6, the not-sent sweep's end). It is the one
definition of these windows; the status writer's re-run rule uses it.

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
an entry to the meeting R1 matches or creates one, keeps a live meeting as it is
(`not_changed_live`), treats a finished meeting as history unless the entry points to a new future
time, removes a meeting that lost its last entry (R8) or stops its bot when live (R5), and checks
the active-entry quota only when a write adds an entry. Events are published after the commit;
a `join_now` entry's meeting is then spawned on that exact row (a failure ends it `not_sent`,
unless it was adopted with other entries on it: then only the pasted entry goes, Ruling R12). `merge_into_live` (R2's exception)
and `rerun_entries` (R7) are for the scheduler and the status writer's callers. The service reaches
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
`POST /v2/meetings/{id}/stop` and `DELETE /v2/meetings/{id}`. Every success body is an `intake.v1`
shape and every failure the §2.5 body; the route class scopes that error handling to these routes,
so a validation failure is 400 `invalid_request` here while upstream routes keep 422, and a
database that can't be reached (or a write that lost a unique-key race) is 503 `unavailable`. A
`user=` read sees a meeting when one of its entries, in any state, names the user as its `user` or
an attendee. Stop calls the `StopPort` for a live meeting and answers 409 `no_live_bot` otherwise.
Erasure runs upstream's `delete_completed_artifacts` (objects first, then transcripts), then
`IntakeReads.erase` removes the meeting's delivery, outbox and entry rows in one transaction; the
meeting row and `meeting_aw_state` stay and no event is written. The reads and erasure go through
`IntakeReads` (`ports.py`): `PostgresIntakeReads` (`reads.py`) over Postgres, `InMemoryIntakeReads`
in `fakes.py`. `GET /v2/entries` pages by `external_id` (served by
`uq_meeting_entries_user_source_external`); `GET /v2/meetings` pages by `(meeting_event_time, id)`,
newest first. meeting-api's production app mounts the router once the real spawn and stop ports
exist (A12).
`ExactRowSpawn` (`spawn.py`) is the production `SpawnPort` (§1.5): `spawn_exact(user_id, meeting_id)`
runs `bot_spawn.request_bot` with `claim_meeting_id`, so the spawn claims exactly that row
(`scheduled` → `requested` through `write_status`, under the link lock, then the per-user lock, then
the row) and stamps `data.auto_join_last_attempt` with the send time, which `bot_joins_at` shows.
It answers `sent`, `already_live` (a bot already owns the link) or `failed` with the code and message
of `spawn_failure(exc)`, the one table from a spawn exception to its §1.13 code (`account_limit`,
`already_live`, `meeting_stopped`, `spawn_error`, `authority_denied`, `authority_unavailable`,
`auth_session`, `transcription_config`, else `internal_error`, logged with its stack); it never
raises. A failure after the claim ends the claimed meeting `not_sent` with that code and message,
under the link lock (Ruling R17): a row still `requested` goes `failed` through `write_status`, a row
the spawn flow already wrote `failed` gets the outcome on `meeting_aw_state` and a `meeting.not_sent`
event; the entry service then replies with that meeting (R12 applies only to a failure before the
claim). The per-user bot limit comes from `fetch_bot_context`, as for the auto-join sweep, and is
never guessed: without it, or without `max_concurrent` in it, the spawn fails `internal_error`
(the one exception is the sweep's `AUTO_JOIN_ALLOW_UNCAPPED` opt-in with no identity edge).
`IntakeStop` (`stop.py`) is the production `StopPort` (§1.7), behind `POST /v2/meetings/{id}/stop`
(no outcome: the meeting ends with upstream's `stopped`) and R5 (outcome `cancelled_by_calendar`
with the remove reason). Under the meeting's link lock it locks the meeting row, then
`meeting_aw_state`: a bot that reached the meeting goes `stopping` through `write_status` with
`data.stop_requested` and the outcome, so every later event carries it; a bot still booting
(`requested`, `joining`, `awaiting_admission`) keeps the stage it reached and gets only the flag and
the outcome (`IntakeTx.mark_stop_requested`). After the commit, `lifecycle.stop_router.stop_meeting_row`
publishes the leave command on `bot_commands:meeting:{id}` and deletes the workload of a bot still
booting. A meeting with no live bot, or already stop-requested, is left as it is; a command bus
that can't be reached is 503 `unavailable`, with the stop already recorded.
`sweeps.py` is the scheduler's intake side (§1.5, R2, R6). `check_room` reads a due entry-managed
meeting and its link again under the link lock: `free`, `gone`, `merge` (the live meeting is an
open-ended meeting with an active `join_now` entry whose bot isn't `stopping`, `is_merge_target`,
the same predicate `merge_into_live` re-checks: `merge_into_live`), or `waiting` (any other live
bot, a leaving one included) — `meeting.waiting_for_room` goes out
once (`meeting_aw_state.waiting_for_room_sent_at`) and no retry pause is stamped, so the bot goes on
the first tick after the link is free. `not_sent_tick` (every `NOT_SENT_SWEEP_INTERVAL_S`,
single-flight) ends every `scheduled` entry-managed meeting past its end (`IntakeStore.overdue_meetings`,
`rules.is_overdue`; an open-ended one's end is its start plus `JOIN_NOW_ADOPT_AHEAD_S`) `failed`,
outcome `not_sent`, under its link lock, with detail `last_error_code` (message `last_error_message`),
else `room_busy` ("another bot was still on this meeting link when the meeting ended"), else
`ended_before_sent` ("the meeting ended before a bot was sent"), and re-runs the entries it kept (R7).
`IntakeSettings.from_env()` (`settings.py`) reads `ENTRY_MAX_DAYS_AHEAD`, `JOIN_NOW_ADOPT_AHEAD_S`,
`AUTO_JOIN_LEAD_S`, `ENTRY_BLOCKED_HOSTS` and `INTAKE_MAX_ACTIVE_ENTRIES`, all declared in
`config.v1.json`.

## Front door
- `project_meeting` — `projection.py`.
- `parse_entry`, `parse_remove`, `EntryIn`, `RemoveIn`, `IntakeError` — `validation.py`.
- `write_status`, `write_event`, `StatusConflict`, `Outcome`, `WrittenEvent`, `derive_event_id_v2`,
  `row_mapping` — `status.py`.
- `IntakeService` — `service.py`; `IntakeSettings` — `settings.py`.
- `ExactRowSpawn`, `spawn_failure` — `spawn.py`.
- `IntakeStop` — `stop.py`.
- `IntakeStore`, `IntakeTx`, `EntryView`, `MeetingView`, `Room`, `SpawnOutcome`, `SpawnPort`,
  `StopPort`, `EventPublisher` — `ports.py`; the in-memory fakes — `fakes.py`.
- `PostgresIntakeStore` — `adapters.py`.
- `build_intake_router` — `router.py`; `IntakeReads`, `MeetingQuery`, `ErasedRows` — `ports.py`;
  `PostgresIntakeReads` — `reads.py`.
- The matching rules — `rules.py` (used inside the package).
