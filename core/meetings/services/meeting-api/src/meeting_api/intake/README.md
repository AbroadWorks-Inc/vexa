# intake — the one meeting projection (§2.4, §1.1)

`project_meeting(meeting, aw, entries, *, lead_s)` renders one aw-bots `meeting` object — the
exact §2.4 shape every reply, read and webhook uses. Pure: no DB, no clock, no network; every
value comes from the `meetings` row, the `meeting_aw_state` row (or `None`), and the meeting's
active `meeting_entries` rows, all passed in as mappings.

## Front door
- `project_meeting` — `projection.py`.
