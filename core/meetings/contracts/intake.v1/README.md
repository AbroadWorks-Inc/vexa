# intake.v1 — the entry/meeting/webhook wire shape for the intake surface (§2, §2.4, §2.5)

The **request and reply shapes** of the `/v2` meeting routes (§2.1–§2.5): what a client sends to
`PUT /v2/entries` and `POST /v2/entries/remove`, the one Reply envelope both return, the pages of
`GET /v2/entries` and `GET /v2/meetings`, the `meeting` object every reply, read and webhook
carries, the erase reply of `DELETE /v2/meetings/{id}`, and the `error` envelope every failure
returns.

Sealed in `contracts.seal.json` (frozen under `lane:contract` review, like
[`webhook.v1`](../webhook.v1/)). `gate:schema` validates every golden in [`golden/`](golden/)
against [`intake.schema.json`](intake.schema.json), and meeting-api's route tests validate real
responses against the same shapes.

## Shapes (`$defs`)
- **`Entry`** — `PUT /v2/entries` request body (§2.2). `start`/`end` are required unless
  `join_now: true`. `start`/`end` use a loose ISO-8601 `pattern`, not `format: date-time`, so a
  **naive** timestamp (no offset) still conforms at the schema layer — rejecting it is
  `meeting_api.intake.validation.parse_entry`'s own step 2, kept deliberately separate from the
  schema step (see "Validation order" below).
- **`Remove`** — `POST /v2/entries/remove` request body (§2.3).
- **`ReplyEntry`** — the small `{external_id, user, state}` block inside `Reply` — NOT the full
  `EntryState` row. `state` is always one of `active`, `removed`, `closed`, never `null`.
- **`Reply`** — every successful call, HTTP 200 (§2.4): `result` (one of the ten outcomes),
  `previous_meeting_id`, `entry` (`ReplyEntry`), `meeting` (`Meeting`, always present —
  `project_meeting` never returns `null`, so every result carries the full projected meeting, even
  `removed`/`already_removed`).
- **`Meeting`** — the §2.4 meeting object, exactly the 16 keys
  `meeting_api.intake.projection.project_meeting` renders, in this order:
  `id, status, completion_reason, failure_stage, outcome, platform, room, meeting_url, title,
  start, end, time_zone, bot_joins_at, entries, export, sequence`. `status` /
  `completion_reason` / `failure_stage` / `outcome.kind` / `export.state` are left as open strings
  here (not enumerated): `status` mirrors the sealed `lifecycle.v1` FSM plus intake's own
  pre-lifecycle `scheduled`, which this draft does not own or duplicate.
- **`MeetingEntryRef`** — one item of `Meeting.entries` (active entries only).
- **`Outcome`** / **`Export`** — `Meeting.outcome` / `Meeting.export`, each `null` until set.
- **`EntryState`** — `GET /v2/entries` row (§2.1): the entry's own §2.2 fields, plus
  `content_hash` and `state`, so a sender can diff its view against aw-bots'.
- **`EntryPage`** — `GET /v2/entries?user=&cursor=&limit=`: `{entries: [EntryState], next_cursor}`,
  the sender's active entries for one user ordered by `external_id`; `next_cursor` is `null` on
  the last page.
- **`MeetingPage`** — `GET /v2/meetings?user=&from=&to=&status=&external_id=&cursor=&limit=`:
  `{meetings: [Meeting], next_cursor}`, newest meeting time first. `GET /v2/meetings/{id}` returns
  one `Meeting`, and so does `POST /v2/meetings/{id}/stop`.
- **`Erased`** — `DELETE /v2/meetings/{id}`: `{meeting, deleted: {objects, entries, outbox,
  deliveries}}`.
- **`Error`** — every failure (§2.5): `{ "error": { "code", "message" } }`. `message` never echoes
  `metadata` or the URL query string (see "Error messages" below). `code` is one of the fourteen
  §2.5 values.

## Validation order (`meeting_api.intake.validation`)
`parse_entry(body, *, now, max_days_ahead)` / `parse_remove(body)` are the ONLY way a request body
becomes a typed, normalised value:
1. JSON Schema (`Entry`/`Remove`) → `invalid_request`.
2. Naive time → `invalid_request`.
3. Normalise to UTC.
4. `metadata` over 16384 bytes → `invalid_request`.
5. `end <= start` → `invalid_request`.
6. `join_now` → `start = now`, `end = None` (overrides whatever was submitted).
7. `already_ended` (skipped when `join_now`: `end` is `None`).
8. `too_far_ahead` (skipped when `join_now`: `start` is `now`).
9. Lower-case `user`/`attendees`, then compute `content_hash`.

`IntakeError` carries its own `http_status`, derived from the full §2.5 code→status table (not
just the codes `parse_entry`/`parse_remove` raise) so a later route handler reuses the same class
for every other code without re-deriving the mapping. `retry_after_s` is always `None` for the
validation-order codes.

## Error messages never echo `metadata` or the URL query string
A JSON-Schema violation is turned into a message that names the failed **field path** and
**rule** (e.g. `metadata: expected type ['object']`), never the offending **value** — `e.instance`
is never read. This is deliberate: `metadata` is the sender's own arbitrary (<=16KB) blob, and a
naive "here's what you sent" message would leak it straight into an error response. The same rule
extends to any future route that builds an `Error` from request state: never build a `message`
from `metadata` or from the raw URL query string.

## `content_hash` — the shared cross-language contract
sha256 hex of the canonical JSON (`json.dumps(..., sort_keys=True, separators=(",", ":"))`) over
the entry's normalised (lower-cased + UTC) **content** fields — deliberately excluding
`external_id`/`user` (the entry's *identity* key, used to look the row up, not its content):

```json
{
  "attendees": ["a@abroadworks.com", "b@client.com"],
  "end": "2026-09-29T04:00:00Z",
  "join_now": false,
  "meeting_url": "https://meet.google.com/kxo-misr-avz",
  "metadata": null,
  "series_id": null,
  "start": "2026-09-29T03:30:00Z",
  "time_zone": "Asia/Kolkata",
  "title": "Weekly sync"
}
```

`meeting_api.intake.validation._content_hash` builds exactly this object (key set, value
normalisation, canonical form) and hashes it. **[`content-hash-vector.json`](content-hash-vector.json)**
is the shared test vector: a `raw_input` (as a sender would submit it, mixed case, a `+05:30`
offset), the `normalised` object above, and the expected `content_hash`. The calendar module
(Part B) computes the same hash from the same normalised fields and MUST match this vector
bit-for-bit — that is the point of pinning it here rather than only asserting it in a Python test.
For a `join_now` entry, `start` is `now` at call time, so `content_hash` differs on every call
(there is no stable "unchanged" for repeated instant joins — this is expected, not a bug).

## Conformance
Goldens in [`golden/`](golden/) named `<Shape>.<case>.json`; `validate.mjs` (ajv) validates each
against its `$def` (the filename prefix) — the identical convention `webhook.v1` uses. Run by
`gate:schema`. One golden per `Reply` result (ten) and per `Error` code (fourteen), plus
representative `Entry`/`Remove`/`Meeting`/`EntryState` examples. `meeting-api/tests/test_intake_contract.py`
additionally re-validates every golden through the Python `jsonschema` path (`_conforms`), so the
two runtimes are proven to agree on the same fixtures, not just on paper.
