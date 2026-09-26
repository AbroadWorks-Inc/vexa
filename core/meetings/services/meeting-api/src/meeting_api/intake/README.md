# intake — the meeting projection, request validation and the status writer (§2, §2.4, §1.1, §1.4)

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

## Front door
- `project_meeting` — `projection.py`.
- `parse_entry`, `parse_remove`, `EntryIn`, `RemoveIn`, `IntakeError` — `validation.py`.
- `write_status`, `write_event`, `StatusConflict`, `Outcome`, `WrittenEvent`, `derive_event_id_v2`,
  `row_mapping` — `status.py`.
