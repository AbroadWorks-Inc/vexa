# intake — the meeting projection and request validation (§2, §2.4, §1.1)

`project_meeting(meeting, aw, entries, *, lead_s)` renders one aw-bots `meeting` object — the
exact §2.4 shape every reply, read and webhook uses. Pure: no DB, no clock, no network; every
value comes from the `meetings` row, the `meeting_aw_state` row (or `None`), and the meeting's
active `meeting_entries` rows, all passed in as mappings.

`parse_entry(body, *, now, max_days_ahead)` / `parse_remove(body)` turn a `PUT /v2/entries` /
`POST /v2/entries/remove` body into a normalised `EntryIn` / `RemoveIn`, or raise `IntakeError`
(§2.5) — validated against the DRAFT `intake.v1` contract
(`core/meetings/contracts/intake.v1/`, unsealed until task A8).

## Front door
- `project_meeting` — `projection.py`.
- `parse_entry`, `parse_remove`, `EntryIn`, `RemoveIn`, `IntakeError` — `validation.py`.
