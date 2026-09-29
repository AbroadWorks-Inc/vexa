# AW Bots `/v2` API reference

- **Date:** 2026-09-28. Served by meeting-api (`src/meeting_api/intake/`, `src/meeting_api/webhooks/`) through the gateway; webhook subscriptions are stored by admin-api.
- **For:** anyone who writes a client of aw-bots: the calendar module, the portal, or any other app.
- **Where the rules come from:** the [meeting intake and webhooks design](../../../../integrations/out/aw-notetaker/docs/2026-09-25-meeting-intake-and-webhooks-design.md) Part 2, and the sealed contracts [`intake.v1`](../../../../core/meetings/contracts/intake.v1/) and [`webhook.v1`](../../../../core/meetings/contracts/webhook.v1/). When this page and a sealed contract disagree, the contract wins.

Contents: [Part 1 — Meeting intake API](#part-1--meeting-intake-api) · [Part 2 — Webhooks API](#part-2--webhooks-api)

## Part 1 — Meeting intake API

The `/v2` API is how any app hands meetings to aw-bots: a calendar sync, a portal, a CRM, a
script. The app sends **entries**; aw-bots turns them into **meetings**, decides when to send a
bot, never sends two bots to one meeting, and reports every change by signed webhook
([Webhooks API (/v2)](#part-2--webhooks-api)).

aw-bots never reads a calendar and knows nothing about the app's users beyond the email address
the app sends. Duplicates, overlapping times, one bot per meeting link, limits and auth are still
checked by aw-bots, whatever a sender does.

### Base URL & keys

Every call goes through the gateway:

| | |
|---|---|
| In-cluster (AW) | `http://aw-bots-vexa-gateway.aw-bots.svc.cluster.local:8000` |
| Self-hosted | `http://localhost:18056` (the gateway) |

The examples use two shell variables:

```bash
export AW_BOTS_BASE_URL=http://aw-bots-vexa-gateway.aw-bots.svc.cluster.local:8000
export AW_BOTS_API_KEY=vxa_bot_…   # placeholder; your own key, kept server-side
```

Every request carries the key as `X-API-Key`. One key is one **account**: everything a key writes
and reads belongs to that account, and two accounts never see each other's meetings, even on the
same link at the same time. Give each client its own least-privilege key
([scopes](../../../../docs/docs/authentication.mdx)):

| Route | Scope |
|---|---|
| `PUT /v2/entries` | `bot` |
| `POST /v2/entries/remove` | `bot` |
| `GET /v2/entries` | `bot` |
| `GET /v2/meetings` | `tx` |
| `GET /v2/meetings/{id}` | `tx` |
| `POST /v2/meetings/{id}/stop` | `bot` |
| `DELETE /v2/meetings/{id}` | `erase` |
| `POST /v2/meetings/{id}/export` | `export` (the exporter only) |
| `/v2/webhooks…` | `webhooks` ([Webhooks API](#part-2--webhooks-api)) |

A calendar-style sender needs `bot`; a UI that also reads meetings needs `bot` + `tx`. `erase`,
`webhooks` and `export` are separate keys: a `bot` + `tx` key doesn't reach their routes.

### Entries and meetings

An **entry** is one person's invite or click: "user `a@abroadworks.com` has this meeting link at
this time". A **meeting** is what the bot joins. aw-bots groups entries into meetings:

- **The same link at overlapping times is one meeting** (within one account). Five people with
  the same invite send five entries; they all land on one meeting, with one UUID and one bot.
  Back-to-back times (15:00 end, 15:00 start) don't overlap, so they stay two meetings.
- **One live bot per link.** Many meetings may share a link (a daily standup), but only one bot is
  on it at a time. A meeting due while another bot is still on the link waits
  (`meeting.waiting_for_room`) and goes the moment the link is free.
- **An entry is keyed by `user` + `external_id`.** `external_id` is the sender's own id, any
  format (`google:<event id>`, `manual:<uuid>`, `crm:deal-4711-call-2`); aw-bots never parses it.
  Sending the same `user` + `external_id` again updates that entry. `user` is an email address,
  lower-cased by aw-bots; it is who owns the entry, and `GET /v2/meetings?user=` shows a user the
  meetings they own or are invited to (through `attendees`).
- **A meeting is named by its UUID** (`meeting.id`) in every reply, read and webhook.
- **A recurring series is one entry per occurrence**, each with its own `external_id`; aw-bots
  never expands a series. `series_id` is for display and filtering only.
- A meeting's time runs from the earliest start to the latest end of its active entries. Once its
  bot is live, the time is no longer recomputed.

The bot is sent `AUTO_JOIN_LEAD_S` before the meeting starts (300 s in AW's deployment; 120 s by
default) and a meeting still without a bot when its `end` passes ends `failed` with outcome
`not_sent` and the reason. A meeting never stays `scheduled` forever.

### Conventions

- Request and reply bodies are JSON. Send `Content-Type: application/json`.
- Times are ISO 8601 **with an offset** (`2026-09-29T09:00:00Z` or `…+05:30`). A time without an
  offset is refused. aw-bots stores and returns UTC with a trailing `Z`.
- Every successful meeting route answers **200**. Every failure has one body shape:

```json
{ "error": { "code": "invalid_request", "message": "start: naive timestamps are not allowed (an explicit UTC offset is required)" } }
```

  Branch on `code`, never on `message`: the message is for people and may change. It never echoes
  `metadata` or a URL query string. See [Errors](#errors) for every code.

---

### Create or update an entry

```bash PUT /v2/entries
curl -X PUT "$AW_BOTS_BASE_URL/v2/entries" \
  -H "X-API-Key: $AW_BOTS_API_KEY" -H "Content-Type: application/json" \
  -d '{
    "external_id": "google:3n5kq8example",
    "user": "a@abroadworks.com",
    "meeting_url": "https://meet.google.com/kxo-misr-avz",
    "start": "2026-09-29T09:00:00Z",
    "end": "2026-09-29T09:30:00Z",
    "time_zone": "Asia/Kolkata",
    "title": "Weekly sync",
    "attendees": ["a@abroadworks.com", "b@abroadworks.com", "c@client.com"]
  }'
```

Scope `bot`. Creates the entry, or updates it when this `user` already has an entry with this
`external_id` (an upsert). Unknown fields are refused.

| Field | Type | Required | Limits and meaning |
|---|---|---|---|
| `external_id` | string | yes | 1–255 characters. The sender's own id, any format; aw-bots never parses it. Unique per account and `user`. |
| `user` | string (email) | yes | The entry's owner. Lower-cased. |
| `meeting_url` | string | yes | **One** link, chosen by the sender. aw-bots parses it for the platform and room (Google Meet, Zoom, Teams, Jitsi); a link it can't parse → `unrecognized_link`, a host the deployment blocks → `platform_not_enabled`. |
| `start` | string (ISO 8601 with offset) | yes, unless `join_now` | Normalised to UTC. At most `ENTRY_MAX_DAYS_AHEAD` (30) days ahead, else `too_far_ahead`. |
| `end` | string (ISO 8601 with offset) | yes, unless `join_now` | Must be after `start`, and after now (else `already_ended`). |
| `time_zone` | string or null | no | IANA name (`Asia/Kolkata`). Display only. |
| `title` | string or null | no | At most 512 characters. |
| `attendees` | array of emails, or null | no | At most 100. Lower-cased. Used only so `user=` reads match invited people. |
| `series_id` | string or null | no | At most 255 characters. The sender's own series id; display and filtering only. |
| `join_now` | boolean | no | `true` = instant join: send a bot now. `start` becomes now and `end` open; any `start`/`end` sent is ignored. Instant join is decided by this flag only, never by the `external_id`. |
| `metadata` | object or null | no | At most 16 384 bytes as compact JSON. The sender's own data, stored on the entry and echoed in reads and webhooks. |

#### Reply

Always **200** with the reply envelope, for every result:

```json Response — 200 (created)
{
  "result": "created",
  "previous_meeting_id": null,
  "entry": { "external_id": "google:3n5kq8example", "user": "a@abroadworks.com", "state": "active" },
  "meeting": {
    "id": "5f0c2b7e-8d1a-4c3e-9b6f-2a7d1e4c8b90",
    "status": "scheduled",
    "completion_reason": null,
    "failure_stage": null,
    "outcome": null,
    "platform": "google_meet",
    "room": "kxo-misr-avz",
    "meeting_url": "https://meet.google.com/kxo-misr-avz",
    "title": "Weekly sync",
    "start": "2026-09-29T09:00:00Z",
    "end": "2026-09-29T09:30:00Z",
    "time_zone": "Asia/Kolkata",
    "bot_joins_at": "2026-09-29T08:55:00Z",
    "entries": [
      { "external_id": "google:3n5kq8example", "user": "a@abroadworks.com",
        "attendees": ["a@abroadworks.com", "b@abroadworks.com", "c@client.com"],
        "series_id": null, "metadata": null }
    ],
    "export": null,
    "sequence": 1
  }
}
```

| Field | Meaning |
|---|---|
| `result` | What happened (table below). |
| `previous_meeting_id` | The UUID of the meeting the entry left, when it moved to another meeting (`updated`; or `created` / `joined_existing` after a move off a live or finished meeting); otherwise `null`. |
| `entry` | `{external_id, user, state}`: this entry's state after the call, `active`, `removed` or `closed` (its meeting finished). |
| `meeting` | The [meeting object](#the-meeting-object) as saved, always present. |

| `result` | Meaning |
|---|---|
| `created` | A new meeting was made for this entry. |
| `joined_existing` | The entry joined a meeting already there (same link, overlapping time): same UUID, no second bot. |
| `updated` | The entry changed and its meeting was updated. If the entry moved to another meeting, `meeting` is the new one and `previous_meeting_id` the one it left. |
| `unchanged` | Same content as stored (same `content_hash`); nothing done. Safe to send as often as you like. |
| `not_changed_live` | The meeting's bot is live and the change doesn't move the entry to a future, non-overlapping time (a new title or attendees, a later end, a start that still overlaps). The change is stored on the entry; the meeting is left as it is. |
| `not_changed_finished` | The meeting has finished and the change doesn't point to a new future time. The finished meeting is history. |

What each case sends and gets back:

| Case | Send | Reply |
|---|---|---|
| A new meeting | `PUT` with its times | `created` |
| The same invite for another user (`b@…`) | `PUT` with `user: "b@…"`, same link and time | `joined_existing`, **the same UUID**; `entries` lists A and B |
| One occurrence moved | same `external_id`, new `start`/`end` | `updated`, same UUID, new `bot_joins_at` |
| Link changed | same `external_id`, new `meeting_url` | `updated`; if the new link matches another meeting, its UUID and `previous_meeting_id`. The old meeting ends `cancelled_by_calendar` / `entry_moved` if it has no entries left |
| Title changed | same `external_id`, new `title` | `updated`, or `not_changed_live` while the bot is live |
| Moved to a later time while the bot is live | same `external_id`, a `start` at or after both now and the live meeting's planned end | at once `created` or `joined_existing` for the new time, with `previous_meeting_id` = the live meeting. The live meeting keeps its bot, and keeps the entry as closed history |
| Changed after the meeting finished | same time → | `not_changed_finished` |
| | a future time that doesn't overlap → | `created`, a new meeting, `previous_meeting_id` = the finished one |
| Removed entry sent again | same `external_id` | it becomes active again (`created` or `joined_existing`) |
| Back-to-back meetings on one link | two entries | two meetings; the second waits for the link if the first bot is still there |

#### Instant join (`join_now`)

```bash PUT /v2/entries — instant join
curl -X PUT "$AW_BOTS_BASE_URL/v2/entries" \
  -H "X-API-Key: $AW_BOTS_API_KEY" -H "Content-Type: application/json" \
  -d '{
    "external_id": "manual:0f9d1c2a-6b7e-4f3a-9c1d-8e2b5a7f4c61",
    "user": "a@abroadworks.com",
    "meeting_url": "https://us02web.zoom.us/j/12345678901?pwd=abc",
    "join_now": true
  }'
```

Generate the `manual:` id **once per click** (a retry after a timeout reuses it, so it lands on the
same entry). The bot is sent inside the call, so the reply already says what happened:

| Reply | Meaning |
|---|---|
| `created`, `meeting.status: "requested"` | The bot is on its way. |
| `joined_existing` | The link already had a meeting (below): its UUID. A scheduled one's bot is sent now; a live one is left as it is. **Never a second bot.** |
| `created`, `meeting.status: "failed"`, `outcome.kind: "not_sent"` | The bot couldn't be sent; `outcome.detail` is the typed code (`account_limit`, `spawn_error`, …) and `outcome.message` the exact reason. |
| `400 unrecognized_link` | Not a meeting link aw-bots knows. |

A paste adopts the **earliest** unfinished meeting on the link whose `end` is after now and whose
`start` is within `JOIN_NOW_ADOPT_AHEAD_S` (1 hour) from now: a 09:45 paste adopts the 10:00
meeting; a live meeting counts however long it runs past its planned end. With no match it makes
a new open-ended meeting (`end: null`). If a paste adopts a scheduled meeting that other people's
entries share and the bot can't be sent, only the pasted entry is removed (reason `not_sent`), the
error is recorded, and the meeting stays scheduled for the others (`joined_existing`).

`content_hash` includes `start`, which is "now" for an instant join, so a `join_now` entry is never
`unchanged`.

#### Errors

`400 invalid_request`, `400 unrecognized_link`, `400 platform_not_enabled`, `400 too_far_ahead`,
`400 already_ended`, `401 unauthorized`, `403 forbidden`, `429 rate_limited`,
`429 quota_exceeded`, `503 unavailable`. What to do about each is in [Errors](#errors).

### Remove an entry

```bash POST /v2/entries/remove
curl -X POST "$AW_BOTS_BASE_URL/v2/entries/remove" \
  -H "X-API-Key: $AW_BOTS_API_KEY" -H "Content-Type: application/json" \
  -d '{"external_id":"google:3n5kq8example","user":"a@abroadworks.com","reason":"cancelled"}'
```

Scope `bot`.

| Field | Type | Required | Meaning |
|---|---|---|---|
| `external_id` | string | yes | 1–255 characters. |
| `user` | string (email) | yes | Lower-cased. |
| `reason` | string or null | no | `cancelled`, `declined`, `deleted`, `moved_out_of_window`, `not_eligible`, or free text. Recorded, put in the meeting's `outcome.detail` when it was the last entry, and sent in the webhook. |

The reply is the same envelope as `PUT`, with `entry.state: "removed"` and one of:

| `result` | Meaning |
|---|---|
| `removed` | It was the meeting's last entry and no bot had been sent: the meeting ends `failed`, `completion_reason: "stopped"`, outcome `cancelled_by_calendar` (`meeting.removed` webhook). The row is kept as history. Also the answer for a meeting waiting for a new bot after one failed: it ends the same way at once (`bot.failed` webhook). |
| `entry_removed` | Other entries remain, so the meeting stays (re-planned around them) and the bot still goes for them. Also the answer when the meeting had already finished: only the entry goes. |
| `bot_stopping` | It was the last entry of a live meeting: the bot is leaving. What was recorded so far is kept and processed; the meeting ends `stopped`, outcome `cancelled_by_calendar`. |
| `already_removed` | The entry was already removed. Nothing done. |

Errors: `400 invalid_request`, `404 entry_not_found` (this entry was never sent — drop it),
`401`, `403`, `429 rate_limited`, `503 unavailable`.

To cancel a future meeting, remove its entries. Stop (below) is only for a bot that is live.

### List a user's entries

```bash GET /v2/entries
curl "$AW_BOTS_BASE_URL/v2/entries?user=a@abroadworks.com&limit=200" \
  -H "X-API-Key: $AW_BOTS_API_KEY"
```

Scope `bot`. The **active** entries this account holds for one user, ordered by `external_id`,
each with the `content_hash` aw-bots computed. A sender uses it to compare its own view with
aw-bots' and send only the differences.

| Query | Required | Meaning |
|---|---|---|
| `user` | yes | Email address. |
| `limit` | no | 1–200, default 100. |
| `cursor` | no | `next_cursor` from the previous page. Opaque; an invalid one → `invalid_request`. |

```json Response — 200
{
  "entries": [
    {
      "external_id": "google:3n5kq8example",
      "user": "a@abroadworks.com",
      "meeting_url": "https://meet.google.com/kxo-misr-avz",
      "start": "2026-09-29T03:30:00Z",
      "end": "2026-09-29T04:00:00Z",
      "time_zone": "Asia/Kolkata",
      "title": "Weekly sync",
      "attendees": ["a@abroadworks.com", "b@client.com"],
      "series_id": null,
      "join_now": false,
      "metadata": null,
      "content_hash": "fd7a005d05177e2d33b75cdb001856a8e7720391abc66118984a8cdfe636a03f",
      "state": "active"
    }
  ],
  "next_cursor": "Imdvb2dsZTozbjVrcThleGFtcGxlIg=="
}
```

`next_cursor` is `null` on the last page.

#### `content_hash`

The sha256 hex of the canonical JSON of the entry's normalised **content** fields: `attendees`
(lower-cased, in the order sent), `end`, `join_now`, `meeting_url` (as sent), `metadata`,
`series_id`, `start`, `time_zone`, `title`. `external_id` and `user` are the entry's key, not its
content, so they are left out. Times are UTC with a trailing `Z`; the JSON has sorted keys, no
spaces, and non-ASCII characters escaped as `\uXXXX` (Python's `json.dumps` default):

```python content_hash.py
import hashlib
import json
from datetime import datetime, timezone


def _utc(value):
    if value is None:
        return None
    dt = datetime.fromisoformat(value)  # must carry an offset
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def content_hash(entry: dict) -> str:
    fields = {
        "attendees": [a.lower() for a in entry.get("attendees") or []],
        "end": _utc(entry.get("end")),
        "join_now": bool(entry.get("join_now", False)),
        "meeting_url": entry["meeting_url"],
        "metadata": entry.get("metadata"),
        "series_id": entry.get("series_id"),
        "start": _utc(entry.get("start")),
        "time_zone": entry.get("time_zone"),
        "title": entry.get("title"),
    }
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
```

The shared test vector is `core/meetings/contracts/intake.v1/content-hash-vector.json`: its
`raw_input` must hash to `fd7a005d…6a03f`. A sender in another language must match that vector
bit for bit (watch the ASCII escaping and fractional seconds, which Python writes as six digits).

### List a user's meetings

```bash GET /v2/meetings
curl "$AW_BOTS_BASE_URL/v2/meetings?user=a@abroadworks.com&from=2026-09-28T00:00:00Z&limit=50" \
  -H "X-API-Key: $AW_BOTS_API_KEY"
```

Scope `tx`. The meetings a user may see: those where one of the meeting's entries, **in any
state**, has the user as its `user` or among its `attendees` (so a declined guest still sees the
meeting). Newest meeting time first.

| Query | Required | Meaning |
|---|---|---|
| `user` | yes | Email address. |
| `from` | no | ISO 8601 with offset; meetings whose time is at or after it. |
| `to` | no | ISO 8601 with offset; meetings whose time is before it. |
| `status` | no | One status: `scheduled`, `idle`, `requested`, `joining`, `awaiting_admission`, `active`, `needs_help`, `needs_human_help`, `stopping`, `completed`, `failed`. Anything else → `invalid_request`. |
| `external_id` | no | Only meetings holding an entry of this account with this `external_id` (any user, any state). |
| `limit` | no | 1–200, default 100. |
| `cursor` | no | `next_cursor` from the previous page. |

The meeting time is the planned start, else the time the bot started, else when the meeting was
made.

```json Response — 200
{
  "meetings": [
    { "id": "5f0c2b7e-8d1a-4c3e-9b6f-2a7d1e4c8b90", "status": "scheduled", "…": "the meeting object" }
  ],
  "next_cursor": null
}
```

Each item is a full [meeting object](#the-meeting-object). `next_cursor` is `null` on the last page.

### Get one meeting

```bash GET /v2/meetings/{id}
curl "$AW_BOTS_BASE_URL/v2/meetings/5f0c2b7e-8d1a-4c3e-9b6f-2a7d1e4c8b90?user=a@abroadworks.com" \
  -H "X-API-Key: $AW_BOTS_API_KEY"
```

Scope `tx`. Returns one [meeting object](#the-meeting-object).

- **With `user=`** (a UI acting for a signed-in person — always pass it): only a meeting that user
  owns or is invited to, through any of its entries in any state.
- **Without `user=`**: any meeting of the account.

Anything else, another account's meeting, or an id that isn't a UUID is `404 meeting_not_found`;
the answer is the same in every case, so it tells nothing about meetings the caller can't see.

### Stop the bot

```bash POST /v2/meetings/{id}/stop
curl -X POST "$AW_BOTS_BASE_URL/v2/meetings/5f0c2b7e-8d1a-4c3e-9b6f-2a7d1e4c8b90/stop" \
  -H "X-API-Key: $AW_BOTS_API_KEY"
```

Scope `bot`, no body. The live bot leaves now; the reply is the meeting (**200**).

- A bot **in the call** goes `stopping`, then `completed` with `completion_reason: "stopped"`.
  What was recorded is processed as usual.
- A bot **still joining** (`requested`, `joining`, `awaiting_admission`) keeps its status until it
  ends; it is told to stop and its workload is removed.
- A meeting **waiting for a new bot** after one failed (`requested`, `bot_joins_at` in the future)
  has no bot to tell: it ends `failed` with `completion_reason: "stopped"` at once.
- A user's stop sets no `outcome`: it is the user's own `stopped`.
- A meeting with **no live bot** (still `scheduled`, or finished) → `409 no_live_bot`. To cancel a
  future meeting, remove its entries.

aw-bots doesn't decide *who* may press stop; that is the client's rule (the portal allows it only
to a user who owns an active entry on the meeting). Errors: `404 meeting_not_found`,
`409 no_live_bot`, `503 unavailable` (the stop is recorded but the leave command couldn't be sent
yet; it reconciles on its own, and a retry is safe).

### Erase a finished meeting

```bash DELETE /v2/meetings/{id}
curl -X DELETE "$AW_BOTS_BASE_URL/v2/meetings/5f0c2b7e-8d1a-4c3e-9b6f-2a7d1e4c8b90" \
  -H "X-API-Key: $AW_BOTS_ERASE_KEY"
```

Scope `erase`. Works on **finished** meetings only (`completed` or `failed`); otherwise
`409 meeting_not_finished` (remove its entries or stop it first). It removes aw-bots' own
recording copies and transcript rows for the meeting, then its entries, webhook outbox rows and
delivery rows. The meeting row stays as evidence. **No webhook is sent.** Transcripts and audio the
notetaker keeps in its own bucket are not aw-bots' and are not touched.

```json Response — 200
{
  "meeting": { "id": "5f0c2b7e-8d1a-4c3e-9b6f-2a7d1e4c8b90", "status": "completed", "completion_reason": "stopped", "entries": [], "…": "the meeting object" },
  "deleted": { "objects": 2, "entries": 1, "outbox": 6, "deliveries": 6 }
}
```

Errors: `404 meeting_not_found`, `409 meeting_not_finished`, `503 unavailable` (storage failed
before any row was removed; retry the same call).

### Report an export result (exporter only)

```bash POST /v2/meetings/{id}/export
curl -X POST "$AW_BOTS_BASE_URL/v2/meetings/5f0c2b7e-8d1a-4c3e-9b6f-2a7d1e4c8b90/export" \
  -H "X-API-Key: $AW_BOTS_EXPORT_KEY" -H "Content-Type: application/json" \
  -d '{"state":"handed_off","s3_path":"s3://aw-chatworks-transcribe/recordings/google_meet_kxo-misr-avz_20260929T090000Z/"}'
```

Scope `export`. Only the exporter calls this; other clients read the result from the meeting's
`export` field and the `export.handed_off` / `export.failed` webhooks.

| Field | Type | Required | Meaning |
|---|---|---|---|
| `state` | `"handed_off"` or `"failed"` | yes | The export's result. |
| `s3_path` | string | yes | 1–1024 characters. The export folder. |
| `error` | string or null | no | At most 2000 characters. Why it failed. |

Taken only for a finished meeting of the account (`404 meeting_not_found`, `409
meeting_not_finished`). The reply is the meeting, whose `export` shows the result. The same
`state` and `s3_path` again changes nothing and sends no event, so the exporter may repeat a report
until it is accepted.

---

### The meeting object

Every reply, read and webhook carries the meeting in this one shape (sealed as `intake.v1`
`Meeting`): exactly these 16 keys.

| Field | Type | Meaning |
|---|---|---|
| `id` | string (UUID) | The meeting's id. Use it in every `/v2/meetings/{id}` path. |
| `status` | string | See the status table below. |
| `completion_reason` | string or null | Set when the meeting finished: `stopped`, `left_alone`, `startup_alone`, `evicted`, `awaiting_admission_timeout`, `awaiting_admission_rejected`, `join_failure`, `auth_session_missing`, `validation_error`, `max_bot_time_exceeded` (and upstream's `start_failed` on some `failed` rows). |
| `failure_stage` | string or null | On a `failed` meeting, the stage it failed at: `requested`, `joining`, `awaiting_admission`, `active`. |
| `outcome` | object or null | aw-bots' own cause, below. `null` until set. |
| `platform` | string | `google_meet`, `zoom`, `teams` or `jitsi`, from parsing `meeting_url`. |
| `room` | string | The platform's room id: the Meet code, the Zoom numeric id, the Teams id, the lower-cased Jitsi room. |
| `meeting_url` | string | The link the bot joins. |
| `title` | string or null | The first title among the entries, in start order. |
| `start`, `end` | string or null | UTC. `end` is `null` for an open-ended instant join. |
| `time_zone` | string or null | Display only. |
| `bot_joins_at` | string or null | While `scheduled`: when the bot will be sent (`start` − the lead). Waiting for a new bot after one failed (`requested`, after `bot.retry`): when that bot will be sent. Once sent: when it was sent. Otherwise `null`. |
| `entries` | array | A finished meeting lists its closed entries; any other meeting its active ones. Removed entries are never listed. Each is `{external_id, user, attendees, series_id, metadata}`. |
| `export` | object or null | `{state, s3_path, error, at}` once the exporter reported: `state` is `handed_off` or `failed`. |
| `sequence` | integer | The meeting's event counter: +1 with every webhook event of this meeting. Keep the highest you applied and ignore anything older. |

| `status` | Meaning |
|---|---|
| `scheduled` | Planned; no bot sent yet. |
| `requested` | The bot is being started, or a new bot is on its way after one failed (`bot.retry`). |
| `joining` | The bot is opening the meeting. |
| `awaiting_admission` | The bot is in the lobby, waiting to be let in. |
| `active` | The bot is in the call and recording. |
| `needs_help`, `needs_human_help` | The bot is stuck and needs a person. |
| `stopping` | The bot is leaving. |
| `completed` | Finished normally. |
| `failed` | Finished without a normal recording: see `completion_reason`, `failure_stage` and `outcome`. |
| `idle` | Upstream's planned status for meetings made outside `/v2`; entries never make it. |

`requested` through `stopping` are **live**: the meeting has a bot and stop works.
`completed` and `failed` are **finished**.

```json "outcome"
{ "kind": "not_sent", "detail": "account_limit", "message": "bot limit reached (45 of 45)", "at": "2026-09-29T04:20:00Z" }
```

| `outcome.kind` | Set when | `detail` |
|---|---|---|
| `not_sent` | The meeting ended with no bot: its end passed, or an instant join failed. `status: "failed"`. | A typed code: `account_limit`, `already_live`, `meeting_stopped`, `spawn_error`, `authority_denied`, `authority_unavailable`, `auth_session`, `transcription_config`, `internal_error`, `room_busy` (the link was still busy at the meeting's end), `ended_before_sent`. `message` is the exact reason. |
| `cancelled_by_calendar` | The last entry was removed, or moved to another meeting. | The remove `reason`, or `entry_moved`. |
| `merged_into_live` | The meeting was due while an open-ended instant-join bot was on its link: its entries moved onto that live meeting, so there is one bot and one recording. `status: "failed"`, webhook `meeting.removed` with `data.merged_into`. | The live meeting's UUID. |

---

### Errors

Every error body is `{ "error": { "code", "message" } }`, from aw-bots and from the gateway alike.

| HTTP | `code` | When | The client should |
|---|---|---|---|
| 400 | `invalid_request` | A missing or wrong field, an unknown field, a time without an offset, `end` ≤ `start`, `metadata` over 16 KB, a bad query value or cursor | Fix the request and resend. |
| 400 | `unrecognized_link` | aw-bots can't parse `meeting_url` | Not retry until the link changes. |
| 400 | `platform_not_enabled` | The link's host is on the deployment's blocked list (`ENTRY_BLOCKED_HOSTS`) | Not retry until that changes. |
| 400 | `too_far_ahead` | `start` is more than 30 days ahead | Send it later. |
| 400 | `already_ended` | `end` is not after now (without `join_now`) | Drop it. |
| 401 | `unauthorized` | No key, or an unknown, revoked or expired key | Stop and fix the configuration; never mark entries rejected. |
| 403 | `forbidden` | The key lacks the route's scope | Same as 401. |
| 404 | `entry_not_found` | Remove of an entry never sent | Drop it. |
| 404 | `meeting_not_found` | Unknown UUID, another account's meeting, or not visible to `user=` | Treat as "not found". |
| 409 | `meeting_not_finished` | `DELETE` or export on a scheduled or live meeting | Remove its entries or stop it first. |
| 409 | `no_live_bot` | Stop on a meeting with no live bot | To cancel a future meeting, remove its entry. |
| 429 | `rate_limited` | The account's write rate was hit (`Retry-After` set) | Wait `Retry-After` seconds, then resend. |
| 429 | `quota_exceeded` | A standing quota is full (no `Retry-After`) | Stop; free entries, or ask for a higher quota. |
| 503 | `unavailable` | The database is down, a write raced another on the same entry, the gateway can't check the key, or a service behind it failed | Retry with backoff. |

A 500 is a bug in aw-bots, not in the request: report it.

### Rate limits and quotas

A rate says "slow down" and comes with `Retry-After`; a quota says "stop" and comes without it.

| Limit | Value | Refusal |
|---|---|---|
| Entry writes (`PUT /v2/entries` + `POST /v2/entries/remove` together), per account | 600 a minute (`INTAKE_RATE_LIMIT_PER_MIN`), in wall-clock minute windows shared by every gateway replica | `429 rate_limited`, `Retry-After` = seconds left in the window |
| Requests per key (every route) | a burst of 120, refilled at 40 a second | `429 rate_limited`, `Retry-After: 1` |
| Requests per address, from outside the deployment's own network | 600 a minute | `429` |
| Active entries (entries of unfinished meetings), per account | 100 000 (`INTAKE_MAX_ACTIVE_ENTRIES`), checked only when a write adds an active entry; concurrent writes can pass it by a few | `429 quota_exceeded` |
| Webhook subscriptions, per account | 20 | `429 quota_exceeded` |
| `metadata` per entry | 16 KB | `400 invalid_request` |

Always honour `Retry-After`. Pace writes a little below the limit (the calendar module keeps to 500
a minute).

---

### How to integrate

#### A calendar-style sender

1. Get a `bot` key. Choose an `external_id` scheme that is stable per invite and per occurrence
   (`google:<event id>`); send `series_id` for recurring meetings.
2. For each user and each invite in your window: `PUT /v2/entries` when it is new or its content
   changed. Pick **one** link per invite. Store the reply's `meeting.id` and the `content_hash` you
   compute.
3. When an invite goes away before it ends: `POST /v2/entries/remove` with the reason
   (`cancelled`, `declined`, `deleted`, `moved_out_of_window`, `not_eligible`). When it simply
   ended, send nothing.
4. Reconcile now and then: page `GET /v2/entries?user=`, compare each `content_hash` with yours,
   re-send what differs or is missing, and remove what aw-bots holds but you don't. Never resend
   everything blindly.
5. Handle replies and errors: every `result` is success; `400`/`404`/`409`/`quota_exceeded` → mark
   the entry rejected and retry only when it changes; `rate_limited`, `503` or a timeout → back off,
   honouring `Retry-After`; `401`/`403` → stop and alert.

#### A UI-style client (a portal)

1. Keep the key (`bot` + `tx`) on the server; it never reaches the browser or the logs.
2. **Instant join:** on each click, `PUT /v2/entries` with `join_now: true`, `external_id =
   "manual:" + a new UUID`, the signed-in user, and the pasted link. Show the reply's meeting and
   status (`requested`, `joined_existing`, or `failed` with `outcome.message`).
3. **Lists and pages:** `GET /v2/meetings?user=<signed-in user>` for the list and
   `GET /v2/meetings/{id}?user=<signed-in user>` for one meeting. Always pass `user=`; a
   `meeting_not_found` means "not found" to that user.
4. **Stop:** show the button while the meeting is live (`requested` … `stopping`) and call
   `POST /v2/meetings/{id}/stop`. `no_live_bot` means there is nothing to stop.
5. **Live updates:** subscribe to webhooks ([Webhooks API](#part-2--webhooks-api)), verify each delivery,
   dedupe on `event_id`, apply by `sequence`, and re-read the meeting when in doubt.
6. **Transcripts:** read from `meeting.export.s3_path` once `export.state` is `handed_off`; until
   then, show "processing".

## Part 2 — Webhooks API

aw-bots reports every change to a meeting by signed webhook to every subscriber of the account
that made it. Subscriptions are managed through the gateway with a key holding the `webhooks`
scope; the events carry the same [meeting object](#the-meeting-object) every
`/v2` read returns. Base URL, keys and the error shape are as in the
[Meeting intake API (/v2)](#base-url--keys).

```bash
export AW_BOTS_WEBHOOKS_KEY=vxa_webhooks_…   # placeholder; a key with the webhooks scope
```

These subscriptions are separate from the per-account webhook in [Settings](../../../../docs/docs/webhooks.mdx)
(`PUT /user/webhook`), which keeps its own older envelope.

### Subscriptions

A subscription object:

```json
{
  "id": "2d9f6c1e-4b7a-4e3d-8c5f-1a2b3c4d5e6f",
  "url": "https://portal.example.com/api/webhooks/aw-bots",
  "events": [],
  "active": true,
  "description": "portal live updates",
  "secret_last4": "x9Qa",
  "previous_secret_expires_at": null,
  "created_at": "2026-09-28T10:00:00Z",
  "updated_at": "2026-09-28T10:00:00Z"
}
```

| Field | Meaning |
|---|---|
| `id` | The subscription's UUID. |
| `url` | Where deliveries are posted. |
| `events` | The event types it receives; `[]` means all. |
| `active` | `false` = paused: nothing is delivered. |
| `description` | Free text. |
| `secret_last4` | The last four characters of the current secret, to recognise it. A secret is never returned in full after the call that set it. |
| `previous_secret_expires_at` | After a rotation, when the old secret stops signing; otherwise `null`. |

**URL rules.** `http` or `https` with a host. The URL is checked when saved and again before
every send: a host that is, or resolves to, a private, loopback, link-local or otherwise reserved
address is refused (`400 invalid_request`), unless the deployment allow-lists it
(`WEBHOOK_PRIVATE_HOST_ALLOWLIST`, which holds the portal's in-cluster host).

#### Add a subscription

```bash POST /v2/webhooks
curl -X POST "$AW_BOTS_BASE_URL/v2/webhooks" \
  -H "X-API-Key: $AW_BOTS_WEBHOOKS_KEY" -H "Content-Type: application/json" \
  -d '{
    "url": "https://portal.example.com/api/webhooks/aw-bots",
    "secret": "<RECEIVER_SECRET>",
    "events": [],
    "description": "portal live updates"
  }'
```

| Field | Type | Required | Meaning |
|---|---|---|---|
| `url` | string | yes | 1–2048 characters; see the URL rules. |
| `secret` | string | no | 16–512 characters. The receiver normally supplies its own. If omitted, aw-bots generates one and returns it **once**, as `secret` in this response. A supplied secret is never echoed. |
| `events` | array of strings | yes | `[]` = all events; otherwise each must be an event type below. |
| `description` | string or null | no | At most 500 characters. |

**201 Created** with the subscription object (plus `secret` when generated). At most 20 per
account: the 21st is `429 quota_exceeded`. A new subscription, or a change to its events, reaches
the sender within about 30 s.

#### List subscriptions

```bash GET /v2/webhooks
curl "$AW_BOTS_BASE_URL/v2/webhooks" -H "X-API-Key: $AW_BOTS_WEBHOOKS_KEY"
```

**200** `{"subscriptions": [ …subscription objects, oldest first… ]}`. Secrets are never shown.

#### Change a subscription

```bash PATCH /v2/webhooks/{id}
curl -X PATCH "$AW_BOTS_BASE_URL/v2/webhooks/2d9f6c1e-4b7a-4e3d-8c5f-1a2b3c4d5e6f" \
  -H "X-API-Key: $AW_BOTS_WEBHOOKS_KEY" -H "Content-Type: application/json" \
  -d '{"active": false}'
```

Send any of `url`, `events`, `active` (none of them `null`) and `description`; an empty body is
`invalid_request`. **200** with the subscription. Pausing (`active: false`) cancels its pending
deliveries; events that happen while paused are not delivered later.

#### Remove a subscription

```bash DELETE /v2/webhooks/{id}
curl -X DELETE "$AW_BOTS_BASE_URL/v2/webhooks/2d9f6c1e-4b7a-4e3d-8c5f-1a2b3c4d5e6f" \
  -H "X-API-Key: $AW_BOTS_WEBHOOKS_KEY"
```

**204 No Content**. Its pending deliveries are cancelled.

#### Rotate the secret

```bash POST /v2/webhooks/{id}/rotate-secret
curl -X POST "$AW_BOTS_BASE_URL/v2/webhooks/2d9f6c1e-4b7a-4e3d-8c5f-1a2b3c4d5e6f/rotate-secret" \
  -H "X-API-Key: $AW_BOTS_WEBHOOKS_KEY" -H "Content-Type: application/json" \
  -d '{"secret": "<NEW_RECEIVER_SECRET>"}'
```

Body `{"secret"?}` (16–512 characters; omit it to have one generated and returned once as
`secret`). **200** with the subscription, `previous_secret_expires_at` set to now + 24 h. For the
next 24 h every delivery carries two signatures, one under each secret (see
[Signing](#signing)), so the receiver can switch to the new secret whenever it likes.

#### Send a test

```bash POST /v2/webhooks/{id}/test
curl -X POST "$AW_BOTS_BASE_URL/v2/webhooks/2d9f6c1e-4b7a-4e3d-8c5f-1a2b3c4d5e6f/test" \
  -H "X-API-Key: $AW_BOTS_WEBHOOKS_KEY"
```

**202 Accepted** `{"subscription_id": "…", "event_id": "evt_test_…"}`. A signed `webhook.test` is
queued to that one subscriber now, whatever its `events` list, and shows in its delivery log like
any other delivery. A paused subscription's test is cancelled, not sent.

#### Read the delivery log

```bash GET /v2/webhooks/{id}/deliveries
curl "$AW_BOTS_BASE_URL/v2/webhooks/2d9f6c1e-4b7a-4e3d-8c5f-1a2b3c4d5e6f/deliveries?limit=50" \
  -H "X-API-Key: $AW_BOTS_WEBHOOKS_KEY"
```

`limit` 1–200 (default 50); `before` = the `next_before` of the previous page. Newest first.

```json Response — 200
{
  "deliveries": [
    {
      "id": 1042,
      "event_id": "evt_ef69c30038906cb161306e9e12261b439e1f66a7c5426a943b43f6ed880cf5a9",
      "event_type": "meeting.completed",
      "state": "delivered",
      "attempts": 2,
      "next_attempt_at": null,
      "last_status_code": 200,
      "last_error": null,
      "created_at": "2026-09-29T05:12:42Z",
      "updated_at": "2026-09-29T05:13:45Z",
      "attempt_log": [
        { "attempt": 1, "outcome": "retry", "status_code": 503, "error": null, "duration_ms": 41, "at": "2026-09-29T05:12:43Z" },
        { "attempt": 2, "outcome": "delivered", "status_code": 200, "error": null, "duration_ms": 38, "at": "2026-09-29T05:13:45Z" }
      ]
    }
  ],
  "next_before": null
}
```

`state` is `pending`, `sending`, `delivered`, `failed`, `dead` or `cancelled`. Delivery rows in a
final state are kept for 30 days (`WEBHOOK_DELIVERY_RETENTION_DAYS`).

#### Errors

The `/v2` error shape. `400 invalid_request` (a bad field, an unknown event type, a refused URL),
`401 unauthorized`, `403 forbidden`, `404 webhook_not_found` (not one of the account's
subscriptions), `404 account_not_found`, `429 quota_exceeded`, `503 unavailable` (storage down, the
deployment's secret key ring not configured, or the test couldn't be queued; retry).

---

### Events

| `event_type` | When |
|---|---|
| `meeting.scheduled` | A meeting was created by an entry. |
| `meeting.updated` | Its time, link, title or entries changed. |
| `meeting.removed` | Removed before its bot was sent (its last entry was removed), or merged into a live meeting (`data.merged_into`). |
| `meeting.waiting_for_room` | Due, but another bot is still on the link. Sent once; the bot goes when the link is free. |
| `meeting.not_sent` | Ended with no bot; `outcome` carries the typed code and exact message. |
| `meeting.status_change` | Every bot step without a typed event: to `requested`, `joining`, `awaiting_admission`, `needs_help`, `stopping`. |
| `meeting.started` | The bot reached `active`. |
| `meeting.completed` | The meeting reached `completed`. |
| `bot.failed` | The meeting reached `failed` (except `not_sent`, which is `meeting.not_sent`). |
| `bot.retry` | A bot failed while the meeting is on; a new bot will be sent to the same meeting. The meeting is back to `requested`, `change.reason` is the failure reason and `bot_joins_at` is when the new bot goes. |
| `export.handed_off` / `export.failed` | The exporter's result (`meeting.export`). |
| `webhook.test` | A test send. |

A status change is **exactly one event**, never two: the typed one when there is one, else
`meeting.status_change`. A receiver that follows every step listens to the typed events as well as
`meeting.status_change`. `recording.ready` and `transcription.ready` are accepted in `events` but
not sent to subscribers. Erasing a meeting sends nothing. aw-bots' part ends at
`export.handed_off`: transcription is not an aw-bots event.

#### The envelope

Every delivery is one JSON object:

```json
{
  "api_version": "2026-09-25",
  "created_at": "2026-09-29T05:12:41Z",
  "data": {
    "change": { "at": "2026-09-29T05:12:41Z", "from": "stopping", "reason": "stopped", "to": "completed" },
    "meeting": {
      "bot_joins_at": "2026-09-29T04:25:00Z",
      "completion_reason": "stopped",
      "end": "2026-09-29T05:00:00Z",
      "entries": [
        { "attendees": ["a@abroadworks.com", "b@example.com"], "external_id": "google:3n5kq8example",
          "metadata": { "crm_id": "42" }, "series_id": "google:series-weekly", "user": "a@abroadworks.com" }
      ],
      "export": null,
      "failure_stage": null,
      "id": "5f0c2b7e-8d1a-4c3e-9b6f-2a7d1e4c8b90",
      "meeting_url": "https://meet.google.com/abc-defg-hij",
      "outcome": null,
      "platform": "google_meet",
      "room": "abc-defg-hij",
      "sequence": 9,
      "start": "2026-09-29T04:30:00Z",
      "status": "completed",
      "time_zone": "Asia/Kolkata",
      "title": "Weekly sync"
    }
  },
  "event_id": "evt_ef69c30038906cb161306e9e12261b439e1f66a7c5426a943b43f6ed880cf5a9",
  "event_type": "meeting.completed"
}
```

| Field | Meaning |
|---|---|
| `event_id` | `evt_` + the sha256 hex of the text `<meeting id>`, `<event_type>` and `<sequence>` joined by `\|` (for the example: `5f0c2b7e-8d1a-4c3e-9b6f-2a7d1e4c8b90\|meeting.completed\|9`). The same on every redelivery of the event, different for every event. |
| `event_type` | One of the events above. |
| `api_version` | `2026-09-25`. |
| `created_at` | When the event was recorded (UTC, whole seconds). |
| `data.meeting` | The full [meeting object](#the-meeting-object) as it was at this event, with `id` and `sequence`. |
| `data.change` | `{from, to, reason, at}` on every status change, whatever the event type, and on `meeting.scheduled` (`from: null`). Absent on events that change no status (`meeting.updated`, `meeting.waiting_for_room`, `export.*`). `reason` is the step's reason when it has one. |
| `data.merged_into` | Only on a `meeting.removed` that merged the meeting into a live one: the live meeting's UUID. |

`webhook.test` carries no meeting, only the subscription:

```json
{ "api_version": "2026-09-25", "created_at": "2026-09-29T05:12:41Z",
  "data": { "subscription_id": "2d9f6c1e-4b7a-4e3d-8c5f-1a2b3c4d5e6f" },
  "event_id": "evt_test_0c4d8e2f6a1b4c3d9e8f7a6b5c4d3e2f", "event_type": "webhook.test" }
```

Answer a verified test with a 2xx and do nothing else.

#### Ordering and duplicates

Delivery is **at least once and not in order**:

- **Dedupe on `event_id`.** Keep the ids you have processed for at least **48 hours**, and mark an
  id seen only after you have processed it. Never key on the body or the signature: the
  signature's timestamp differs between redeliveries.
- **Order by `data.meeting.sequence`.** It rises by 1 with every event of a meeting. Keep the
  highest `sequence` applied per meeting `id` and ignore an event with a lower one; it is older
  than what you have. A gap means an event is still on its way; the meeting in the newest event is
  complete on its own, so applying it is safe.

### Signing

Every delivery carries:

| Header | Value |
|---|---|
| `Content-Type` | `application/json` |
| `X-Webhook-Timestamp` | Unix seconds when it was signed. |
| `X-Webhook-Signature` | `sha256=` + hex HMAC-SHA256 with the secret over `"<X-Webhook-Timestamp>." + <raw body>`. Always exactly one value. |
| `X-Webhook-Signature-Previous` | The same under the previous secret, only during the 24 h after a rotation. |

There is **no `Authorization` header**: the secret never crosses the wire. To verify:

1. Take the **raw body bytes** as received. Don't parse and re-serialise first.
2. Reject a timestamp more than **300 s** from your clock.
3. Compute `sha256=` + hex HMAC-SHA256(secret, `timestamp + "." + raw body`) and compare it in
   constant time with `X-Webhook-Signature` and, if present, `X-Webhook-Signature-Previous`.
   Accept a match on either.
4. On failure answer 401 (it is not retried); on success process, then answer 2xx.

```python verify.py
import hashlib
import hmac
import time


def verify(raw_body: bytes, headers: dict, secret: str, tolerance_s: int = 300) -> bool:
    ts = headers.get("X-Webhook-Timestamp", "")
    if not ts.isdigit() or abs(time.time() - int(ts)) > tolerance_s:
        return False
    expected = "sha256=" + hmac.new(
        secret.encode(), ts.encode() + b"." + raw_body, hashlib.sha256
    ).hexdigest()
    return any(
        hmac.compare_digest(expected, headers.get(name, ""))
        for name in ("X-Webhook-Signature", "X-Webhook-Signature-Previous")
    )
```

```javascript verify.mjs
import { createHmac, timingSafeEqual } from "node:crypto";

// rawBody: the request body as a Buffer; headers: lower-cased names (as Node gives them)
export function verify(rawBody, headers, secret, toleranceS = 300) {
  const ts = headers["x-webhook-timestamp"] ?? "";
  if (!/^[0-9]+$/.test(ts) || Math.abs(Date.now() / 1000 - Number(ts)) > toleranceS) return false;
  const expected = Buffer.from(
    "sha256=" + createHmac("sha256", secret).update(ts + ".").update(rawBody).digest("hex"));
  return ["x-webhook-signature", "x-webhook-signature-previous"].some((name) => {
    const got = Buffer.from(headers[name] ?? "");
    return got.length === expected.length && timingSafeEqual(got, expected);
  });
}
```

Both snippets check out against the sealed `webhook.v1` goldens: the body is
`MeetingEvent.meeting-completed.json` written compactly with sorted keys (exactly how aw-bots posts
it), the secret `whsec_demo_secret`, the timestamp `1790658761`, giving
`sha256=d4b79a595be48b0769c0e9a685672032fc9bb64275ae2ee05ec9ce237aeb98a4`
(`SignatureHeaders.subscription.json`). With `whsec_demo_previous_secret`, the match is on
`X-Webhook-Signature-Previous` (`SignatureHeaders.rotated.json`). Test with the golden's own
timestamp as "now", or the 300 s check refuses it.

**Rotation, step by step:** call `rotate-secret`; give the receiver the new secret alongside the
old one (it accepts a match on either header); after 24 h the previous header stops and the old
secret can go.

### Delivery and retries

| Your answer | Result |
|---|---|
| 2xx | `delivered` |
| 5xx, 429, a timeout (10 s per attempt) or a connection error | retried after 1 min, 5 min, 30 min and 2 h, then `dead` |
| anything else: 4xx, a redirect (redirects are never followed) | `failed`, not retried |

- The retry waits are the operator's setting `WEBHOOK_RETRY_SCHEDULE_S` (default
  `60,300,1800,7200`, in seconds); the number of entries is the number of retries.
- **Answer 2xx fast.** Verify, store or queue the event, answer, and do slow work afterwards. An
  answer later than 10 s counts as a timeout and is sent again.
- Answer 5xx (or 503) when you can't take the event right now, so it comes back; answer 4xx only for
  a request you will never accept.
- A URL the guard refuses at send time is `failed`. A delivery aw-bots can't sign or resolve yet
  (DNS failure, a subscription it can't read) is retried like any other.
- Every attempt is in the [delivery log](#read-the-delivery-log).

### Receiver checklist

1. Subscribe once with a `webhooks` key and your own secret; keep the secret out of logs.
2. Read the raw body; verify either signature header; reject more than 300 s of skew.
3. Skip an `event_id` already seen (kept 48 h); skip an event whose `sequence` is lower than the
   one you applied for that meeting.
4. Apply the event (or queue it), then mark the `event_id` seen, then answer 2xx within 10 s.
   Answer 503 if your store is down, so aw-bots retries.
5. Answer a verified `webhook.test` with 200 and do nothing else. Use `POST …/test` after setup
   and check the delivery log.
