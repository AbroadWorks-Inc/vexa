# intake.v1 goldens

Wire-shape fixtures, one per `$def`/case. Filename `<Shape>.<case>.json`; the prefix names the
`$def` the file must conform to (validated by `../validate.mjs`, run by `gate:schema`, and again
by `meeting-api/tests/test_intake_contract.py` through the Python `jsonschema` path).

- `Entry.scheduled.json` / `Entry.join-now.json` — a `PUT /v2/entries` body with `start`/`end`,
  and an instant-join body with neither.
- `Remove.with-reason.json` / `Remove.no-reason.json` — a `POST /v2/entries/remove` body.
- `Meeting.scheduled.json` — the §2.4 meeting object.
- `Meeting.retry-pending.json` — a meeting waiting for a new bot after one failed (§6.9 F-K2):
  `requested`, `bot_joins_at` the new bot's time, no `completion_reason` yet (checked against
  `project_meeting` by meeting-api's `tests/test_bot_retry.py`).
- `EntryState.active.json` — a `GET /v2/entries` row.
- `EntryPage.first-page.json` — a `GET /v2/entries` page with a `next_cursor`.
- `MeetingPage.last-page.json` — the last page of `GET /v2/meetings` (`next_cursor: null`).
- `Erased.finished.json` — the `DELETE /v2/meetings/{id}` reply.
- `Reply.<result>.json` — one per §2.4 result: `created`, `joined_existing`, `updated`,
  `unchanged`, `not_changed_live`, `not_changed_finished`, `removed`, `entry_removed`,
  `bot_stopping`, `already_removed`.
- `Error.<code>.json` — one per §2.5 code: `invalid_request`, `unrecognized_link`,
  `platform_not_enabled`, `too_far_ahead`, `already_ended`, `unauthorized`, `forbidden`,
  `entry_not_found`, `meeting_not_found`, `meeting_not_finished`, `no_live_bot`, `rate_limited`,
  `quota_exceeded`, `unavailable`.

`content-hash-vector.json` (one level up, NOT in this directory) is a separate, non-schema-shaped
fixture — the shared `content_hash` test vector — deliberately kept out of `golden/` so
`validate.mjs` never tries to resolve it against a `$def`.
