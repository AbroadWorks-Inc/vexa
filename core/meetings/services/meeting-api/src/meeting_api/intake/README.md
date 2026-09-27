# intake — entry handling, the meeting projection, request validation and the status writer (§1.1, §1.3, §1.4, §2, §2.4)

`project_meeting(meeting, aw, entries, *, lead_s)` renders one aw-bots `meeting` object — the
exact §2.4 shape every reply, read and webhook uses. Pure: no DB, no clock, no network; every
value comes from the `meetings` row, the `meeting_aw_state` row (or `None`), and the meeting's
`meeting_entries` rows, all passed in as mappings. A finished (`completed`/`failed`) meeting lists
its closed entries, any other meeting its active ones; removed entries are never listed (R9).

`parse_entry(body, *, now, max_days_ahead)` / `parse_remove(body)` turn a `PUT /v2/entries` /
`POST /v2/entries/remove` body into a normalised `EntryIn` / `RemoveIn`, or raise `IntakeError`
(§2.5) — validated against the DRAFT `intake.v1` contract
(`core/meetings/contracts/intake.v1/`, unsealed until task A8).

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
`join_now_target`, `recompute`, and `finished_window` / `is_rerun` / `is_future_move`. It is the one
definition of these windows; the status writer's re-run rule uses it.

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
`IntakeSettings.from_env()` (`settings.py`) reads `ENTRY_MAX_DAYS_AHEAD`, `JOIN_NOW_ADOPT_AHEAD_S`,
`AUTO_JOIN_LEAD_S`, `ENTRY_BLOCKED_HOSTS` and `INTAKE_MAX_ACTIVE_ENTRIES`, all declared in
`config.v1.json`.

## Front door
- `project_meeting` — `projection.py`.
- `parse_entry`, `parse_remove`, `EntryIn`, `RemoveIn`, `IntakeError` — `validation.py`.
- `write_status`, `write_event`, `StatusConflict`, `Outcome`, `WrittenEvent`, `derive_event_id_v2`,
  `row_mapping` — `status.py`.
- `IntakeService` — `service.py`; `IntakeSettings` — `settings.py`.
- `IntakeStore`, `IntakeTx`, `EntryView`, `MeetingView`, `Room`, `SpawnOutcome`, `SpawnPort`,
  `StopPort`, `EventPublisher` — `ports.py`; the in-memory fakes — `fakes.py`.
- `PostgresIntakeStore` — `adapters.py`.
- The matching rules — `rules.py` (used inside the package).
