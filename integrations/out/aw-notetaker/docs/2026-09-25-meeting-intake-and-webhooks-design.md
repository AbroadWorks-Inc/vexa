# AW Bots: meeting intake and webhooks (design)

- **Date:** 2026-09-25
- **Status:** draft for owner review — **revised 2026-09-25 after a code check** (see the revision log below).
- **Scope:** handoff §6 A (`aw-notetaker/docs/handoffs/2026-09-24-aw-bots-handoff.md`): how any calendar or app sends meetings to aw-bots, and how aw-bots reports every result back.
- **Repos touched:** aw-bots (this repo: meeting-api, admin-api, gateway route table, exporter) and aw-notetaker (calendar-dispatcher, notetaker-postgres, portal).

**Revision log (2026-09-25 review, every claim re-checked against `development` and `feat/aw-bots-deployment`):**

- **V1 — reasons must not touch the sealed enum.** `completion_reason` is the sealed `lifecycle.v1` `CompletionReason` enum (ten values; `contracts/lifecycle.v1/README.md:41-44` says it is "deliberately untouched" because it feeds the retry classifier). The first draft added `cancelled_by_calendar` and `not_sent` to it. Now: the sealed values stay, and AW's cause rides an **additive `outcome` block** (§3 "Reasons", §5.4).
- **V2 — instant join adopted the wrong window.** §6.3 used `start − 5 min … end`, which re-creates the 09:45-paste-against-10:00 case the old path's `_room_row_to_adopt` (`calendar-dispatcher/main.py:482-520`) was written to stop. Now: R1 has an explicit `join_now` rule, and a due meeting merges into a live open-ended one instead of waiting until it fails (R2).
- **V3 — a removed entry that comes back was stuck.** An event that leaves the 14-day window (moved later) and returns, or is re-added with the same id, would reply `not_changed_finished` for ever. Now: a `PUT` for a `removed` entry re-activates it (§8.2), and the calendar module confirms every vanish with `events().get` so the reason is right (§9.3).
- **V4 — a meeting already under way was dropped.** With the due window `[start − lead, start + grace]`, an entry whose start is past but end ahead (calendar connected mid-meeting) ended `not_sent` at once. Now: due until `end` (R6, §8.3).
- **V5 — seals and gates were missing.** The DB schema (`schema.seal.json`, gate `db-schema`), the contracts (`contracts.seal.json`, gate `contract-version`, incl. the webhook `EventType` enum) and the architecture model (P23) are all sealed; each change needs its seal step and its review lane (§8.9).
- **V6 — citations corrected**: live statuses (`bot_spawn/auto_join.py:133-136`, not `lifecycle/machine.py`), `NO_ORG` (`admin_api/app/events.py:45`; admin-api lives under `core/identity/`), the gateway file path, auto-join env lines, the drain interval line, the calendar-sync range, Jitsi ids on `meet.jit.si`, portal lines and in-cluster hosts.
- **V7 — `event_id` defined** for the new events (today's is per bot run, `lifecycle/webhook.py:67-80`), the stop payload corrected, the exporter's internal call routed past the gateway, the portal's v1 refresh made polling, and the existing `data.metadata` / annotate machinery noted.

---

## 1. Summary

- **aw-bots is the meeting service.** It holds every upcoming meeting it has been sent, sends the bot 5 minutes before each one, runs the bot, and reports every result by webhook. It never reads a calendar.
- **The calendar module (`calendar-dispatcher`) reports what the calendars say.** Every 60 s it reads each user's Google calendar for the next 14 days. It sends aw-bots each new meeting, each change and each cancellation. It never sends a bot.
- **The portal is the UI.** It handles sign-in, connecting a calendar, the meeting list, transcripts and the instant-join button. It reads meetings from aw-bots.
- **Any other client** (another company's calendar system, a CRM, a script) uses the same API and the same webhooks. Nothing in aw-bots is specific to our calendar module or our portal.

```
Google Calendar ──(read every 60 s)──> Calendar module ──PUT/remove entries──> aw-bots meeting service
                                                                         │  (holds meetings, scheduler, bots)
Portal ──instant join / stop / read meetings────────────────────────────┤
  ^                                                                      │
  └────────────── webhooks (every result, to every subscriber) ──────────┘
                                                                         │ meeting.completed
                                                                         v
                                                          exporter ──> notetaker-worker /process
```

---

## 2. Words used in this document

| Word | Meaning |
|---|---|
| **Account** | One aw-bots user that a client's systems act as. AbroadWorks' account is the service user `notetaker@abroadworks.com` (id 1). aw-bots has no organisation level above the user (`NO_ORG = ""` at `admin_api/app/events.py:45`, used at `admin_api/app/main.py:510`; admin-api lives under `core/identity/services/admin-api/`; `admin_api/schema/models.py` has no org or tenant column), so **one client = one account**. Everything below (dedup, `external_id`, webhooks) is scoped to one account. |
| **User** | A person whose calendar or click produced a request, identified by email (for example `a@abroadworks.com`). Users are data inside an account, not aw-bots logins. |
| **Entry** | One calendar's (or one click's) view of a meeting: "user A's calendar has event X on this link at this time". Entries are what clients send. |
| **Meeting** | What aw-bots records and sends a bot to. One meeting can have **several entries**, for example the same call on A's and B's calendars. |
| **external_id** | The client's own ID for an entry, for example `google:<Google event id>` or `manual:<id>`. Together with `user`, it's the key for later updates and cancellations. The client never needs the UUID to change an entry. |
| **UUID** (`meeting.id`) | aw-bots' ID for a meeting, used in every reply, webhook, exported file and `/process` call. The integer primary key stays inside the database. |
| **Room** | platform + room code, as parsed from the link (`google_meet` + `kxo-misr-avz`, `zoom` + `12345678901`). |
| **Live** | A meeting whose bot has been sent and hasn't finished: status `requested`, `joining`, `awaiting_admission`, `needs_help`, `active` or `stopping`. The code's list is `LIVE_STATUSES` (`bot_spawn/auto_join.py:133-136`), which also carries the legacy spelling `needs_human_help` (§14.4). `collector/app.py:49` holds the planned set (`idle`, `scheduled`); `:52-55` holds the FSM-owned set, which includes the two terminal statuses. `lifecycle/machine.py:26-34` (`BotStatus`) has neither `requested` nor `stopping` (`:141-150` maps them), so it is not the live set. |

---

## 3. Rules

**R1: The same room at overlapping times is one meeting (within one account).**
- On Meet, Zoom, Teams and Jitsi, a room code is one call. Anyone who opens that link at that moment is in the same call.
- So when an entry arrives for a room, and a non-finished meeting in the same account has the same room and a time range that **overlaps** it (`entry.start < meeting.end` and `entry.end > meeting.start`), the entry joins that meeting.
- Otherwise the entry creates a new meeting.
- Back-to-back meetings (one ends at 15:00, the next starts at 15:00) don't overlap, so they stay separate.
- A merged meeting's time is the earliest start and the latest end of its current entries. Once the meeting is live its time is not recomputed (R7); a new entry may still attach to it.
- **`join_now` entries** (a pasted link) carry no time of their own, so overlap is defined for them: the entry joins the **earliest** non-finished meeting on the room whose `end > now` and whose `start ≤ now + JOIN_NOW_ADOPT_AHEAD_S` (setting, default 3600). This is the old path's proven rule (`_room_row_to_adopt`, `calendar-dispatcher/main.py:482-520`: earliest row, end still ahead) plus a start bound, which the old path did not need at a 24-hour horizon but a 14-day one does (a paste at 10:20, after today's 10:00 standup has ended, must not adopt tomorrow's). No match → a new open-ended meeting (§6.2).
- **An open-ended live meeting** (a `join_now` meeting still running) has no `end`, so for R1 its window is `[start, now]`: it matches entries that have already started or are due (`start ≤ now + lead`), never tomorrow's occurrence on the same room.

**R2: One live bot per room, and the next meeting waits for the room.**
- Many *scheduled* meetings may share a room (a daily standup has one per day).
- Only one meeting per room may be *live*.
- If a meeting is due while another bot is still in that room, it waits. It stays `scheduled`, webhook `meeting.waiting_for_room` is sent once, and its bot is sent as soon as the first bot leaves.
- It keeps waiting until its own end time. If the room is still busy then, it ends `failed` with outcome `not_sent`, detail `room_busy`.
- **Exception — the room is held by an open-ended `join_now` meeting** (someone pasted the link early). The due meeting does not wait: its entries move onto the live meeting (R1, open-ended window), the live meeting takes the due meeting's `title` if it has none, and the due row is removed with outcome `merged_into_live` (webhook `meeting.removed` carrying `merged_into`). One bot, one recording, and the calendar users see it. Waiting would instead end the calendar meeting `room_busy` while the recording sat under a `manual:` meeting only the paster can see.
- Known effect: if the first call runs straight into the second with no gap, the first bot's recording contains the start of the second.

**R3: How far ahead.**
- The calendar module sends meetings starting in the **next 14 days** (setting `AW_BOTS_HORIZON_DAYS`, default 14).
- aw-bots refuses any entry starting more than **30 days** ahead (setting `ENTRY_MAX_DAYS_AHEAD`, default 30), with error `too_far_ahead`.
- A series with no end date therefore never has more than 14 days of occurrences in aw-bots. A weekday standup has about 10 scheduled at a time, about 260 over a year, each with its own UUID and recording.

**R4: Every request gets a definite answer.** Each reply has a `result` (§5.4), or an error `code` (§5.5), plus the meeting as saved.

**R5: A cancel while the bot is in the meeting stops the bot.**
- When the **last** entry of a live meeting is removed, the bot leaves at once.
- What was recorded so far is kept and processed. The meeting ends with the sealed `completion_reason: "stopped"` and outcome `cancelled_by_calendar` (see "Reasons" below).
- If other entries remain (for example only B declined), nothing changes for the bot.

**R6: A meeting never stays `scheduled` forever.**
- A meeting is **due from `start − lead` until its `end`**, not until `start + grace` as today (`bot_spawn/auto_join.py:118`): a bot that arrives late is better than none, and a meeting already under way when its entry arrives (calendar connected mid-call) gets a bot at once. The calendar module removes stale entries, so the "stale plan" the grace protects upstream cannot occur on this path.
- If `end` passes without a bot (account limit reached, spawn error, room busy), it ends as `failed` with outcome `not_sent` and the detail (`ended_before_sent` when no attempt ever ran, else the last `auto_join_error`), and a webhook goes out.
- Today such a row stays `scheduled` for ever (`bot_spawn/auto_join.py:78-127`: it simply stops being due). That holds for rows created through the API, which is what this design uses; rows from Vexa's own calendar sync are instead hard-deleted when their UID leaves the feed window (`calendar_sync/service.py:716-756`).

**R7: Changes while live or after the end.**
- While a meeting is live, updates to its entries are stored on the entry but don't change the meeting. The reply is `not_changed_live`. Removing the last entry stops the bot (R5).
- After a meeting has finished, updates and removals reply `not_changed_finished`.

**R8: Removed meetings are kept as history.**
- A meeting removed before its bot was sent ends `failed` with `completion_reason: "stopped"` (what a planned-row cancel writes today, `lifecycle/stop_router.py:267-284`) and outcome `cancelled_by_calendar`, with webhook `meeting.removed`.
- The row is kept, not hard-deleted, so every outcome stays visible.

**Reasons: the sealed enum stays, AW's cause is additive.**
- `completion_reason` is the sealed `lifecycle.v1` enum: `stopped`, `left_alone`, `startup_alone`, `evicted`, `awaiting_admission_timeout`, `awaiting_admission_rejected`, `join_failure`, `auth_session_missing`, `validation_error`, `max_bot_time_exceeded` — and `failure_stage` is `requested`/`joining`/`awaiting_admission`/`active`. Neither has room for "the calendar cancelled it" or "no bot was ever sent", and the contract README says the enum is deliberately untouched because `lifecycle/retry.py` classifies on it. Upstream's own pattern for extra facts is an additive block (`join_evidence`, same README).
- So every meeting object carries **`outcome`** (stored at `data.outcome`, `null` until set):

```json
"outcome": { "kind": "cancelled_by_calendar", "detail": "declined", "at": "2026-09-29T04:20:00Z" }
```

| `outcome.kind` | Set when | `status` / `completion_reason` |
|---|---|---|
| `cancelled_by_calendar` | last entry removed (R5, R8); `detail` = the remove `reason` | live → `completed` (or `failed`) / `stopped`; planned → `failed` / `stopped` |
| `not_sent` | `end` passed with no bot (R6); `detail` = `ended_before_sent`, `account_limit`, `spawn_error` or `room_busy` | `failed` / absent (no bot ran, so no stage either) |
| `merged_into_live` | R2 exception; `detail` = the UUID it merged into | `failed` / absent |

- A stop through `POST /v2/meetings/{id}/stop` sets no outcome: it is the user's `stopped`, exactly as today.

---

## 4. Who does what

| Module | Does | Holds | Never does |
|---|---|---|---|
| **aw-bots meeting service** (gateway, admin-api, meeting-api, runtime) | Accepts entries, applies R1–R8, sends bots (scheduler), runs them, sends webhooks, serves meeting reads | Entries and meetings from 30 days ahead to all history; webhook subscriptions and delivery log | Read a calendar; know anything about the portal |
| **Calendar module** (`calendar-dispatcher`) | Reads each connected user's Google calendar every 60 s; applies the eligibility rules; sends entries and removals | Users' calendar tokens; what it last sent per (user, event) | Send or stop a bot directly |
| **Portal** | Sign-in, connect calendar, meeting list and detail, instant join, stop bot, webhook receiver | Nothing about meetings of its own | Read calendars; talk to bots |
| **Exporter** (part of aw-bots) | On `meeting.completed`: builds the S3 folder, calls `/process`, reports the export result to meeting-api | Its S3 queue | Transcribe |

---

## 5. The API (aw-bots, new `/v2` routes)

The new routes sit under `/v2` so upstream Vexa's `/meetings`, `/bots` and `/transcripts` routes stay as they are. That keeps later upstream merges simple.

### 5.1 Endpoints

| Method + path | Who calls it | Does | Gateway scope |
|---|---|---|---|
| `PUT /v2/entries` | calendar module, portal, any client | Create or update one entry (upsert by `user` + `external_id`); instant join with `join_now: true` | `bot` |
| `POST /v2/entries/remove` | calendar module, portal, any client | Remove one entry | `bot` |
| `GET /v2/meetings/{id}` | portal, any client | One meeting by UUID | `tx` |
| `GET /v2/meetings` | portal, any client | List meetings. Filters: `user`, `from`, `to`, `status`, `external_id`; cursor paging | `tx` |
| `POST /v2/meetings/{id}/stop` | portal ("stop bot") | Stop the bot in that one meeting now | `bot` |
| `/v2/webhooks…` | client admin | Manage webhook subscriptions (§7.2) | `bot` |

- **Auth:** the account's API key, exactly as today (runbook step 8; AbroadWorks' key is in Secret `aw-bots-portal-api-key`, namespace `notetaker`). The gateway adds `x-user-id` (`gateway/app.py:412`).
- **Route table:** new rows in `core/meetings/routes.v1.json`. The account key must carry both `bot` and `tx` (see open item O2).

### 5.2 Entry fields (`PUT /v2/entries`)

| Field | Type | Required | Notes |
|---|---|---|---|
| `external_id` | string ≤ 255 | yes | Client's ID. Unique per (account, `user`). |
| `user` | email | yes | Whose calendar or click this is. Lower-cased by aw-bots. |
| `meeting_url` | string | yes | Parsed by aw-bots (`collector/meeting_link.py:74-168`). Unknown → `unrecognized_link`. |
| `start` | ISO 8601 UTC | yes, unless `join_now` | |
| `end` | ISO 8601 UTC | yes, unless `join_now` | Must be after `start`. |
| `time_zone` | IANA name | no | Host's zone, for display only (`Asia/Kolkata`). Never used to compute times. |
| `title` | string ≤ 512 | no | |
| `series_id` | string ≤ 255 | no | Groups occurrences of one recurring event (Google `recurringEventId`). Display and filtering only. aw-bots never computes occurrences. |
| `join_now` | bool | no, default `false` | Instant join: the bot is sent at once. `start` = now; `end` is open until the bot finishes. |
| `metadata` | object ≤ 16 KB | no | The client's own data, stored on the **entry** and echoed in every webhook. Not read by aw-bots. Distinct from the meeting's `data.metadata`, which `POST /meetings/{id}/annotate` writes (64 keys / 16 KB, `collector/adapters.py:1405-1467`) and `GET /meetings?metadata=` filters on (`collector/app.py:261-283`); that machinery stays as it is. |

### 5.3 Remove fields (`POST /v2/entries/remove`)

| Field | Required | Notes |
|---|---|---|
| `external_id` | yes | |
| `user` | yes | |
| `reason` | no | `cancelled`, `declined`, `deleted` or free text; recorded and sent in the webhook. |

### 5.4 Reply (every successful call: HTTP 200)

```json
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
      { "external_id": "google:3n5kq8example", "user": "a@abroadworks.com", "series_id": null, "metadata": null }
    ],
    "export": null,
    "sequence": 1
  }
}
```

| `result` | Meaning |
|---|---|
| `created` | A new meeting was made for this entry. |
| `joined_existing` | The entry joined a meeting already there (R1): same UUID, no second bot. |
| `updated` | The entry changed and the meeting was updated. If the change moved the entry to another meeting (new time or link), `previous_meeting_id` names the meeting it left. |
| `unchanged` | Same data as before; nothing done. Safe to send as often as you like. |
| `not_changed_live` | The meeting is live; the change is stored on the entry only (R7). |
| `not_changed_finished` | The meeting has finished (R7). |
| `removed` | Remove call: it was the last entry, so the meeting is removed and its bot won't be sent (R8). |
| `entry_removed` | Remove call: other entries remain, so the meeting stays. |
| `bot_stopping` | Remove call: it was the last entry of a live meeting, so the bot is leaving (R5). |
| `already_removed` | Remove call for an entry already removed. Safe to repeat. |

### 5.5 Errors

Every error has the body `{ "error": { "code": "...", "message": "..." } }`.

| HTTP | `code` | When | Client should |
|---|---|---|---|
| 400 | `invalid_request` | missing/wrong field; `end` ≤ `start` | fix and resend |
| 400 | `unrecognized_link` | aw-bots can't parse `meeting_url` | not retry until the link changes |
| 400 | `platform_not_enabled` | the host is blocked (setting `ENTRY_BLOCKED_HOSTS`; §10.2) | not retry until the next full resync |
| 400 | `too_far_ahead` | `start` > now + `ENTRY_MAX_DAYS_AHEAD` | send again later |
| 400 | `already_ended` | `end` ≤ now (and not `join_now`) | drop it |
| 401 / 403 | `unauthorized` / `forbidden` | bad key / missing scope | fix config |
| 404 | `entry_not_found` | remove of an entry never sent | drop it |
| 404 | `meeting_not_found` | unknown UUID | — |
| 503 | `unavailable` | database/Redis down | retry with backoff |

---

## 6. Every use case: what is sent, when, and the reply

These are the calendar module's rules. Any other client follows the same ones.
- **"When"** means the first 60-second read where the calendar module notices the change.
- The calendar module sends only when something changed. It compares with what it last sent (§9.3).
- Every 6 hours it also re-sends everything as a full resync; unchanged entries reply `unchanged`.

Users in the examples: `a@abroadworks.com`, `b@abroadworks.com`. Times are UTC; 10:00 IST = 04:30 UTC.

### 6.1 One-off meeting

- **When:** the event is created, or first comes within 14 days.
- **Send:** `PUT /v2/entries`

```json
{
  "external_id": "google:3n5kq8example",
  "user": "a@abroadworks.com",
  "meeting_url": "https://meet.google.com/kxo-misr-avz",
  "start": "2026-09-29T09:00:00Z",
  "end": "2026-09-29T09:30:00Z",
  "time_zone": "Asia/Kolkata",
  "title": "Weekly sync"
}
```
- **Reply:** `result: "created"`, the meeting as in §5.4. The calendar module stores `meeting.id`.

### 6.2 Instant join (a pasted link in the portal)

- **When:** the user clicks "Add to meeting".
- **Send:** `PUT /v2/entries` (from the portal)

```json
{
  "external_id": "manual:0f9d1c2a-6b7e-4f3a-9c1d-8e2b5a7f4c61",
  "user": "a@abroadworks.com",
  "meeting_url": "https://us02web.zoom.us/j/12345678901?pwd=abc",
  "join_now": true
}
```
The portal generates the `manual:` ID once per click.
- **Reply:**
  - `result: "created"`, `status: "requested"`: the bot is on its way.
  - `result: "joined_existing"` (§6.3).
  - Error `unrecognized_link`.

### 6.3 Instant join for a meeting that's already scheduled or live

Same request as §6.2.
- aw-bots finds the earliest non-finished meeting on the same room with `end > now` and `start ≤ now + JOIN_NOW_ADOPT_AHEAD_S` (R1, `join_now` rule). A paste at 09:45 for a 10:00 meeting therefore adopts it, as the old path does today.
- **Reply:** `result: "joined_existing"` with that meeting's UUID.
  - If it was `scheduled`, the bot is sent now (its `bot_joins_at` becomes now).
  - If it's already live, nothing more happens.
- **Never a second bot.**
- The reverse order — the paste first, then the calendar meeting comes due on the same room — is the R2 exception: the due meeting merges into the running one.

### 6.4 Recurring series: daily, weekly, monthly, custom, with or without an end date

- **When:** each occurrence as it comes within 14 days. The calendar module reads Google with `singleEvents=True` (`calendar_client.py:519-531`), so Google itself lists each occurrence with its own ID. aw-bots never expands a series.
- **Send:** one `PUT /v2/entries` per occurrence. The daily standup at 10:00 IST, on Tuesday 29 Sep:

```json
{
  "external_id": "google:9dstandupexample_20260929T043000Z",
  "user": "a@abroadworks.com",
  "meeting_url": "https://meet.google.com/abc-defg-hij",
  "start": "2026-09-29T04:30:00Z",
  "end": "2026-09-29T04:45:00Z",
  "time_zone": "Asia/Kolkata",
  "title": "Daily standup",
  "series_id": "google:9dstandupexample"
}
```
- **Reply:** `result: "created"`, a new UUID per occurrence.
  - On the first read, the calendar module sends the next 14 days of occurrences (about 10 on weekdays).
  - After that it sends one a day, as each new day comes within the window.
  - All share the room `abc-defg-hij`; R2 allows that because they're at different times.

### 6.5 One occurrence moved ("this event only")

- **When:** Google shows the occurrence at its new time. **Its ID doesn't change** (it keeps the original start in the ID).
- **Send:** `PUT /v2/entries`, same `external_id`, new `start`/`end`.
- **Reply:** `result: "updated"`, same UUID, new `bot_joins_at`.

### 6.6 One occurrence cancelled

- **When:** the occurrence disappears from the read, after 5 minutes' grace, as today (`main.py:890-940`).
- **Send:** `POST /v2/entries/remove`

```json
{ "external_id": "google:9dstandupexample_20260930T043000Z", "user": "a@abroadworks.com", "reason": "cancelled" }
```
- **Reply:** `removed`, or `entry_removed` if another user's entry remains, or `bot_stopping` if the meeting is live.
- Before sending, the calendar module confirms the vanish with one `events().get` (§9.3), so `reason` is `cancelled`, `declined`, `deleted` or `moved_out_of_window` — not a guess.

### 6.7 Whole series moved or changed ("all events")

- **When:** Google shows the series at its new time.
- If Google gives the occurrences **new IDs**, the old IDs disappear from the read and the new ones appear. The calendar module sends a remove for each old ID and a create for each new one. aw-bots replies `removed` + `created`.
- If the IDs **stay the same**, the calendar module sends updates and aw-bots replies `updated`.
- The calendar module needs no logic for which of the two Google did: it compares by `external_id`, and both give the correct result.
- If a move takes an occurrence **beyond the 14-day window**, it vanishes from the read; the module confirms it with `events().get`, sends a remove with `reason: "moved_out_of_window"`, and re-sends the same `external_id` when it comes back into the window. aw-bots re-activates the removed entry as a new meeting (§8.2), so this is safe.

### 6.8 Whole series cancelled

- **When:** all its occurrences disappear from the read.
- **Send:** `POST /v2/entries/remove` for each occurrence it had sent (at most 14 days' worth).
- **Reply:** `removed` for each.

### 6.9 Link changed

- **Send:** `PUT /v2/entries`, same `external_id`, new `meeting_url`.
- **Reply:**
  - `updated`: the meeting moves to the new room.
  - Or, if the new room matches an existing meeting (R1), `updated` with a new `meeting.id` and `previous_meeting_id` set. The old meeting is removed if no entries remain on it.

### 6.10 Title changed

- **Send:** `PUT /v2/entries` with the new `title`.
- **Reply:** `updated` (or `not_changed_live` during the meeting).

### 6.11 User declined, or deleted the event from their calendar

- **When:** the calendar module's eligibility rule skips declined events (`calendar_client.py:294-298`), so the event disappears from that user's read.
- **Send:** `POST /v2/entries/remove` with `"reason": "declined"` (or `"deleted"`).
- **Reply:** `entry_removed` if others still have it (the bot still goes for them); otherwise `removed`.

### 6.12 The same meeting on several users' calendars

- **When:** each user's read reaches the event.
- **Send:** one `PUT /v2/entries` per user. The `external_id` may be the same (Google shares event IDs across attendees in the same Google system) or different (other calendar systems).
- A's request is §6.1. B's request:

```json
{
  "external_id": "google:3n5kq8example",
  "user": "b@abroadworks.com",
  "meeting_url": "https://meet.google.com/kxo-misr-avz",
  "start": "2026-09-29T09:00:00Z",
  "end": "2026-09-29T09:30:00Z",
  "title": "Weekly sync"
}
```
- **Reply:** `result: "joined_existing"`, **the same UUID**, and `entries` now lists A and B. One bot; both users see the meeting (§10.3).

### 6.13 Back-to-back meetings on one room

- **Send:** two ordinary creates (14:00–15:00 and 15:00–16:00, same personal Zoom room).
- **Reply:** two `created`, two UUIDs; they don't overlap, so they stay separate.
- If the first bot is still in the room at 14:55, the second meeting waits (R2): webhook `meeting.waiting_for_room`, then its bot is sent when the first leaves.

### 6.14 Two different accounts, same room, same time

For example, a call between an AbroadWorks user and a user of another client.
- **Result:** each account gets its own meeting and its own bot. Data never mixes between accounts. This is how dedup works in Vexa already: it's per user (`sessions/models.py:111-116`).

### 6.15 Cancelled while the bot is in the meeting

- **Send:** the remove, as in §6.6.
- **Reply:** `bot_stopping` (if it was the last entry). The bot leaves; the recording so far is processed; the meeting ends `completed` with `completion_reason: "stopped"` and outcome `cancelled_by_calendar` (R5).

### 6.16 Changed after the meeting finished

- **Reply:** `not_changed_finished`. The calendar module stops tracking entries whose `end` has passed (§9.3), so this happens only with a client that sends late.

### 6.17 Too far ahead, unknown link, blocked host

- **Reply:** errors `too_far_ahead`, `unrecognized_link` and `platform_not_enabled` (§5.5).
- The calendar module never sends further out than 14 days, so `too_far_ahead` protects aw-bots from other clients.

### 6.18 The owner's recurring test meeting

"Test recurring meeting", yearly, first occurrence Mon 28 Sep 17:00 IST (11:30 UTC), link `kxo-misr-avz`, then moved to Fri 25 Sep 17:00 IST.
- **On creation:** 28 Sep is within 14 days, so the calendar module sends `external_id: "google:6ktestrecurring_20260928T113000Z"`, start `2026-09-28T11:30:00Z` → `created`.
- **Moved as "this event only":** same ID, start `2026-09-25T11:30:00Z` → `updated` (§6.5).
- **Moved as "all events":** the handling in §6.7 applies.
- **Either way:** the bot joins on 25 Sep at 11:25 UTC. The 2027 occurrence is sent in mid-September 2027.

---

## 7. Webhooks

### 7.1 What changes

- **Today:** one system URL (used by the exporter; `webhooks/system.py:138-175`) and one URL per user (`PUT /user/webhook`, stored in `users.data`, `admin_api/schema/models.py:43-44`). The per-user URL is **copied onto each meeting when its bot starts** (`bot_spawn/service.py:706-711`), so a later change doesn't reach meetings already running.
- **New:** any number of **subscriptions per account**.
  - They are read at **send time**, not copied onto meetings.
  - Deliveries are queued, so a slow subscriber never delays the bot lifecycle.
  - Every attempt is logged.
- The exporter stays on the system URL, unchanged. The old per-user webhook is left as it is (unused by us).

### 7.2 Managing subscriptions (the account's key, via the gateway → admin-api)

| Method + path | Does |
|---|---|
| `POST /v2/webhooks` `{url, events, description}` | Add a subscriber. Returns `{id, secret}`; the secret is shown **once**. `events: []` means all events. |
| `GET /v2/webhooks` | List (secrets never shown; `secret_last4` only) |
| `PATCH /v2/webhooks/{id}` `{url?, events?, active?, description?}` | Change |
| `DELETE /v2/webhooks/{id}` | Remove |
| `POST /v2/webhooks/{id}/rotate-secret` | New secret, shown once. The old one keeps being accepted for 24 h (both signatures are sent, §7.4). |
| `POST /v2/webhooks/{id}/test` | Sends a `webhook.test` event now |
| `GET /v2/webhooks/{id}/deliveries?limit=&before=` | The delivery log (§7.5) |

- **URL check:** on save and again on every send, by the existing SSRF guard (`webhooks/ssrf.py:149-261`).
  - The guard blocks private IPs, and the portal is reached inside the cluster. So a setting `WEBHOOK_PRIVATE_HOST_ALLOWLIST` (for example `portal.notetaker.svc.cluster.local`) lets named in-cluster hosts through.
  - Nothing else private is allowed.
- For AbroadWorks, the portal is the first subscriber.

### 7.3 Events

Every event carries the full meeting object (as in §5.4) with `id` (UUID), `entries` (each with `external_id`, `user`, `metadata`) and `sequence`.

| Event | When | Built in |
|---|---|---|
| `meeting.scheduled` | a new meeting was created by an entry | A |
| `meeting.updated` | time, link, title or entries changed | A |
| `meeting.removed` | removed before its bot was sent (R8), or merged into a live open-ended meeting (R2; `data.merged_into`) | A |
| `meeting.waiting_for_room` | due, but another bot is in the room (R2) | A |
| `meeting.not_sent` | `end` passed without a bot (R6); `outcome.kind` = `not_sent`, `outcome.detail` = `ended_before_sent`, `account_limit`, `spawn_error` or `room_busy` | A |
| `meeting.status_change` | every bot step: `requested` (bot sent), `joining`, `awaiting_admission` (lobby), `active` (admitted, recording), `needs_help`, `stopping`, `completed`, `failed`; with `from`, `to`, `reason` | A (enriches the existing event, `lifecycle/webhook.py:85-114`) |
| `meeting.started` / `meeting.completed` / `bot.failed` | the existing typed events (`lifecycle/webhook.py:39-43`) | A (enriched) |
| `export.handed_off` / `export.failed` | the exporter's `/process` call succeeded / failed for good | A (§8.7) |
| `bot.retry` | a lobby-timeout retry, with `attempt` | B (handoff §6 B9) |

- `completion_reason` stays the sealed set (`lifecycle/machine.py:37-49`: `stopped`, `left_alone`, `startup_alone`, `evicted`, `awaiting_admission_timeout`, `awaiting_admission_rejected`, `join_failure`, `auth_session_missing`, `validation_error`, `max_bot_time_exceeded`). Ours is the additive `outcome` block (§3).
- **New event types touch a sealed enum.** `event_type` is `$ref EventType` in `contracts/webhook.v1/webhook.schema.json`; adding `meeting.scheduled`, `meeting.updated`, `meeting.removed`, `meeting.waiting_for_room`, `meeting.not_sent`, `export.*`, `bot.retry` and `webhook.test` changes the sealed schema (§8.9). `data` is open (`additionalProperties` not locked), so adding `uuid`, `entries`, `outcome`, `sequence` to the meeting object is back-compatible.
- **Transcription is not an aw-bots event.** aw-bots' job ends at `export.handed_off`.

**Envelope:**

```json
{
  "event_id": "evt_7c1e0b0a4d2f4b8e9a3c5d6e7f8a9b0c",
  "event_type": "meeting.status_change",
  "api_version": "2026-09-25",
  "created_at": "2026-09-29T04:26:12Z",
  "data": {
    "meeting": { "id": "5f0c2b7e-8d1a-4c3e-9b6f-2a7d1e4c8b90", "status": "active", "sequence": 7, "...": "as §5.4" },
    "change": { "from": "awaiting_admission", "to": "active", "reason": null, "at": "2026-09-29T04:26:11Z" }
  }
}
```

- `event_id` is unique per event and stays the same across retries. Receivers dedupe on it. Today's id is `sha256(connection_id | event_type | new_status)` (`lifecycle/webhook.py:67-80`), which needs a bot run; the new events have none, so **every** event's id becomes `evt_` + `sha256(meeting uuid | event_type | sequence)`, computed under the same row lock that increments `sequence`.
- `sequence` goes up by one with every event of that meeting. Receivers ignore an event whose `sequence` is lower than one they have already applied, because deliveries can arrive out of order after retries.

### 7.4 Signing

This is the same scheme the exporter already verifies (`webhooks/delivery.py:73-99`, `exporter/signature.py:18-38`), so there is one scheme across aw-bots.
- `X-Webhook-Timestamp: <unix seconds>`
- `X-Webhook-Signature: sha256=<hex HMAC-SHA256(secret, "<timestamp>." + raw body)>`
- During the 24 h after a rotation: `sha256=<new>,sha256=<old>`. A receiver accepts either.
- A receiver rejects a timestamp more than 300 s off.
- **No `Authorization: Bearer <secret>` header** on subscription deliveries. The legacy sender adds one (`delivery.py:80-99`); it isn't needed and it exposes the secret itself.

### 7.5 Delivery, retries and the log

- **Timeout** 10 s.
- **Retried on:** 5xx, 429, timeout or connection error. The schedule is 1 min, 5 min, 30 min, 2 h (Vexa's existing backoff, `webhooks/retry.py:56-58`); after the last attempt the delivery is marked `dead`.
- **Other 4xx:** `failed`, not retried.
- The queue item stores the **subscription id, not the secret**. The existing queue stores the secret in Redis (`retry.py:95-106`).
- **Log:** table `webhook_deliveries`, one row **per attempt**: `event_id`, `event_type`, `meeting_id` (UUID), `attempt`, `outcome`, `status_code`, `error`, `created_at`. Kept 30 days. The existing ledger keeps only first attempts, 100 per user, in Redis (`webhooks/ledger.py:32-111`).

---

## 8. aw-bots changes (implementation detail)

### 8.1 Schema

admin-api owns the schema. `ensure_schema` creates missing tables, columns and indexes **by name** (`admin_api/schema/sync.py:1-14, 227-232`).

- **`meetings.uuid`:** UUID, not null, unique, default `gen_random_uuid()`. Model in both copies (`admin_api/schema/models.py`, `meeting_api/sessions/models.py`).
- **New table `meeting_entries`:**

| Column | Type | Notes |
|---|---|---|
| `id` | bigserial PK | internal |
| `user_id` | int | the account |
| `source_user` | text | lower-cased email |
| `external_id` | text | |
| `meeting_id` | int FK → meetings.id | the meeting it belongs to now |
| `meeting_url`, `platform`, `native_meeting_id` | text | as parsed |
| `title` | text null | |
| `start_at`, `end_at` | timestamptz (end null for `join_now`) | |
| `time_zone`, `series_id` | text null | |
| `join_now` | bool | |
| `metadata` | jsonb null | |
| `state` | text | `active` / `removed` |
| `removed_reason` | text null | |
| `created_at`, `updated_at`, `removed_at` | timestamptz | |

  - Unique on (`user_id`, `source_user`, `external_id`).
  - Indexes on (`meeting_id`) and (`user_id`, `platform`, `native_meeting_id`, `state`).
- **New tables `webhook_subscriptions`** (`id` uuid, `user_id`, `url`, `secret`, `previous_secret`, `previous_secret_expires_at`, `events` text[], `active`, `description`, timestamps) **and `webhook_deliveries`** (§7.5).
- **The one-live-bot-per-room index (R2):**
  - Add `uq_meeting_live_user_platform_native` on `meetings (user_id, platform, platform_specific_id)` `WHERE status IN ('requested','joining','awaiting_admission','needs_help','active','stopping')`.
  - Drop `uq_meeting_active_user_platform_native`, which today covers every non-finished status including `scheduled` (`sessions/models.py:111-116`).
  - `ensure_schema` matches by name and won't alter an existing index, so this is a **manual step on the live database**, like MIGRATION-0002: create the new index `CONCURRENTLY`, then drop the old one `CONCURRENTLY`. It gets a `MIGRATION-0003-…md` next to `admin_api/schema/MIGRATION-0002-meeting-active-dedup-index.md`.
  - Order: new index first, then deploy the new code, then drop the old one.
  - **Everything that assumes one non-finished row per room must change or be checked in the same change**, because the old index was what made that assumption true: the claim-in-place branch (`bot_spawn/adapters.py:533-556`, §8.3), the auto-join live-sibling guard (`auto_join.py:246-279`, §8.3), the spawn dedup list at `bot_spawn/adapters.py:485` (which omits `needs_help` and `stopping` and relied on the index to catch them — it becomes the full live set), and Vexa's own calendar-sync adoption (`calendar_sync/service.py:572-578`, unused here, O7). Upstream's `POST /meetings` keeps its 409 on any non-finished row for the link because that check is in code (`collector/adapters.py:1234-1244`), not the index; the Vexa terminal therefore cannot plan a second occurrence on a room that already has one, which is acceptable.
  - Both model copies carry comments saying `_sync_indexes` swallows a failed unique index (`admin_api/schema/models.py:160-162`, `sessions/models.py:105`); in fact `sync.py:93-142` raises `SchemaInvariantError` and admin-api does not start. The migration must therefore run **before** the deploy, not be left for `ensure_schema` to retry.
- **Meeting times:** `data.scheduled_at` stays the join time that auto-join reads. It is set to the earliest start of the meeting's active entries. New: `data.scheduled_end_at` and `data.time_zone`.

### 8.2 Entry handling (new module `meeting_api/intake/`)

**`PUT /v2/entries`**, under a Postgres advisory lock on (account, platform, room):
1. Validate the fields and parse the link.
2. Find the entry by (`user_id`, `source_user`, `external_id`). **An entry in state `removed` is treated as new**: it goes back to `active` and takes the R1 path below (reply `created` or `joined_existing`, and `previous_meeting_id` names the meeting it was removed from). Without this, an event that leaves the 14-day window and returns, or is deleted and re-added with the same id, would answer `not_changed_finished` for ever, because its old meeting is `failed`.
3. Otherwise, if the meeting it belongs to is live or finished, store the change on the entry and reply `not_changed_live` / `not_changed_finished` (R7).
4. Otherwise, match R1: a non-finished meeting in the account with the same room and an overlapping time.
   - Match found → attach the entry.
   - No match → create a meeting (`status='scheduled'`, `data.auto_join=true`).
5. If the entry left a meeting that has no active entries left, that meeting is removed (R8).
6. Recompute each affected meeting's start and end from its entries (R1).
7. Emit the webhooks and reply.

**`join_now`:** the handler calls the spawn path directly for that exact meeting row, the same way `POST /bots` does, so the reply already shows `requested`. If the room has a live bot, the reply is `joined_existing` and nothing is spawned. If the spawn fails (for example, the account limit), the meeting ends `failed` with outcome `not_sent` at once, with the detail (R6), and the reply shows it.

**`POST /v2/entries/remove`:**
- Mark the entry `removed`.
- If it was the meeting's last active entry:
  - `scheduled` → meeting `failed` / `stopped`, outcome `cancelled_by_calendar`, webhook `meeting.removed`;
  - live → stop that one meeting (§8.5) with outcome `cancelled_by_calendar`.

### 8.3 Auto-join (`bot_spawn/auto_join.py`)

- **Spawn the exact meeting.** Today the spawn path (`bot_spawn/adapters.py:459-573`, `create_meeting_guarded`) claims the **newest** `idle`/`scheduled` row for the room (`:533-556`). With many scheduled rows per room, that would be the wrong one (a standup two weeks out). Auto-join passes the row id and the spawn claims exactly that row.
- **Due window (R6):** `[scheduled_at − lead, scheduled_end_at]` for meetings that have entries; `AUTO_JOIN_GRACE_S` no longer bounds them (`auto_join.py:118`). Rows without entries (planned through Vexa's own routes) keep today's window.
- **Room busy (R2):** today a live sibling gives `skipped_live` with a retry time (`auto_join.py:246-279`). New:
  - if the sibling is an open-ended `join_now` meeting, merge into it (R2 exception) instead of waiting;
  - otherwise send `meeting.waiting_for_room` once and keep the meeting due until its `scheduled_end_at`.
- **Not sent (R6):** a new step in the same sweep. A `scheduled` meeting whose `scheduled_end_at` has passed becomes `failed` with outcome `not_sent` (detail = the last `auto_join_error`, or `ended_before_sent`) and webhook `meeting.not_sent`.
- Settings unchanged: sweep 30 s (`AUTO_JOIN_SWEEP_INTERVAL_S`, `__main__.py:542`), lead 300 s (ours; code default `DEFAULT_LEAD_S = 120` at `auto_join.py:63`, read at `__main__.py:545`), backoff 300 s (`:547`). Grace (`:546`, hard-coded 600) still applies to entry-less rows only.

### 8.4 Room-code lookups

Today these routes pick the **newest** row for a room (full list in §13.2). Once many scheduled rows share a room, "newest" would often be a **future** occurrence. One shared resolver replaces the newest-row pick:

| Route kind | Resolves to |
|---|---|
| Reads (`GET /transcripts/{p}/{n}`, `GET /meetings/{p}/{n}/participants`, `POST /ws/authorize-subscribe`) and descriptions (`POST /meetings/{p}/{n}/annotate`) | the live meeting; else the most recent meeting that has started. **Never a future scheduled one.** |
| Planned-edit routes (`PATCH`/`DELETE /meetings/{p}/{n}`, `PUT /meetings/{p}/{n}/intent`, `POST …/workspace`, `POST …/share`) | the live meeting, or the single scheduled one. If there are several scheduled, the reply is `409 ambiguous_room`: "several scheduled meetings on this room; use the meeting id". |
| Stop (`DELETE /bots/{p}/{n}`) | **the live meeting only.** Today it also cancels every planned row on the room (`lifecycle/stop_router.py:152-158`), which would wipe every future standup. |

- The bot's own status callbacks are keyed by session (`POST /bots/internal/callback/lifecycle` → `connection_id` = `session_uid`, `app.py:409-546`), and recordings by session too (`recordings/router.py:182-266`). Neither changes.
- Vexa's own dashboard (the terminal) uses the room-code edit routes. For a room with several scheduled meetings, it gets the `ambiguous_room` error rather than a wrong edit.

### 8.5 Stopping one meeting

`POST /v2/meetings/{id}/stop`, and R5, reuse the existing stop mechanism for **one row**:
1. `stop_requested`, status `stopping`;
2. `{"action":"leave","meeting_id":<row id>}` on Redis `bot_commands:meeting:{row id}` (`lifecycle/stop.py:43-50`);
3. workload delete if the bot is still booting (`stop_router.py:207-215`, statuses `requested`/`joining`/`awaiting_admission`, `:74`).

A user stop overrides the bot's reason with `stopped` (`lifecycle/machine.py:56-76`); that stays. A calendar stop is the same `stopped` plus `data.outcome = cancelled_by_calendar`, written when the stop is requested so the terminal webhook carries it.

### 8.6 Webhook sending (`meeting_api/webhooks/subscriptions.py`, new)

- **On every event**, meeting-api:
  1. loads the account's active subscriptions from admin-api `GET /internal/users/{id}/webhook-subscriptions` (cached 30 s) — the same `ADMIN_API_URL` + `INTERNAL_API_SECRET` edge auto-join already uses for bot context (`__main__.py:771-795`);
  2. filters them by event;
  3. puts one delivery item per subscriber on a Redis queue;
  4. a sender loop (in the existing drain tick, `_webhook_drain_loop` at `__main__.py:417-443`, interval `WEBHOOK_DRAIN_INTERVAL` default 5 s at `:296`) signs and posts, and writes `webhook_deliveries`.
- **SSRF:** the existing guard, plus the allow-list (§7.2).
- **`sequence`:** kept on the meeting row (`data.event_seq`), increased under the same row lock as the change.

### 8.7 Exporter

- **IDs:** use the meeting **UUID** instead of `vexa-<n>` (`exporter/job.py:98-100`) in:
  - `speaker_timeline.json` and `participants.json`;
  - the `/process` body's `meeting_id` and `idempotency_key` (`notetaker.py:37-42`);
  - `_export.json`, which keeps the integer too, for our own logs.
- The UUID comes from the webhook: the meeting projection (`app.py:384-404`) gains `uuid`. The contract `core/meetings/contracts/webhook.v1` and its golden files are updated to match.
- The S3 folder naming `<platform>_<room>_<startUTC>` is unchanged.
- **New, after `/process`:** `POST /internal/meetings/{id}/export` to meeting-api, with `{state: "handed_off" | "failed", s3_path, error?}`. The exporter calls meeting-api **directly in-cluster** (as it already does for reads, rearchitecture design D12) with `Authorization: Bearer <INTERNAL_API_SECRET>`, the auth `/internal/recordings/upload` uses (`recordings/router.py:241-252`); `/internal/*` is not in the gateway route table and the gateway refuses undeclared routes (`gateway/app.py:372-382`). meeting-api stores it as `data.export` (shown in the meeting object as `export`) and emits `export.handed_off` / `export.failed`.
- The `idempotency_key` stays the meeting UUID: one processing per meeting. A `continue_meeting` rerun (`find_latest`, `bot_spawn/adapters.py:140-156`) would be deduped by the worker for 24 h; we don't use that route.
- The portal reads transcripts from `export.s3_path`.

### 8.8 Settings (all new ones in `values.yaml`, never in code)

| Setting | Default | Where |
|---|---|---|
| `ENTRY_MAX_DAYS_AHEAD` | 30 | meeting-api |
| `JOIN_NOW_ADOPT_AHEAD_S` | 3600 | meeting-api |
| `ENTRY_BLOCKED_HOSTS` | `meet.abroadworks.com` (until the Jitsi cutover, §10.2) | meeting-api |
| `WEBHOOK_PRIVATE_HOST_ALLOWLIST` | `portal.notetaker.svc.cluster.local` (Service `portal`, namespace `notetaker`, port 80 → 3000, `portal/ui-k8s/service.yaml`) | meeting-api, admin-api |
| `WEBHOOK_DELIVERY_RETENTION_DAYS` | 30 | admin-api |
| `VEXA_JITSI_HOSTS` | add `meet.abroadworks.com` (handoff §6 B13) | meeting-api |

### 8.9 Seals and gates (`node scripts/gates.mjs all`, also on pre-push)

This work trips three sealed artefacts; each has its own step and review lane, and a red gate blocks the push.

| Sealed artefact | What this design changes | Step |
|---|---|---|
| `schema.seal.json` (gate `db-schema`: every table + column of both model copies) | `meetings.uuid`; tables `meeting_entries`, `webhook_subscriptions`, `webhook_deliveries` | `pnpm seal:schema`, `lane:schema` review. Note the index swap itself is not in the seal (columns only) but the new index name is in both models. |
| `contracts.seal.json` (gate `contract-version`) | `webhook.v1`: new `EventType` values (§7.3). `lifecycle.v1`: **untouched** by design (§3 "Reasons") | Back-compatible → `pnpm seal:contracts` in a `lane:contract` PR; the gate's own rule is that a breaking change needs `webhook.v2` instead — adding enum values is treated as back-compatible here because the existing golden files still validate, and that call is made in the PR |
| `architecture.calm.json` / `architecture.seal.json` (P23) | new module `meeting_api/intake/`, new flows calendar-dispatcher → gateway, meeting-api → subscribers, exporter → meeting-api | update the model in the same change, `pnpm seal:arch` |

`contract-conformance` also drives the golden webhook examples against real responses, so the enriched meeting projection (§8.7) needs its golden files regenerated, not hand-edited.

---

## 9. Calendar module changes (`calendar-dispatcher`, aw-notetaker repo)

### 9.1 Per-user switch to aw-bots

- New column `calendar_connections.bot_backend`: `old` (default) or `aw-bots`, via notetaker-postgres migration `0011`.
- `old` users keep today's path exactly: publish to the old bot, cancel, liveness, done/prune.
- `aw-bots` users take the new path below. This lets the owner's account move first, without two bots in any meeting, and the rest follow with one update each.

### 9.2 Reading

- The read itself is unchanged: `singleEvents=True`, the eligibility rules (declined, `outOfOffice`/`focusTime`/`workingLocation`, all-day, no link; `calendar_client.py:294-302, 568-585`), and token refresh, rotation and deactivation.
- For `aw-bots` users:
  - the window is `AW_BOTS_HORIZON_DAYS` (14) instead of `CALENDAR_HORIZON_MINUTES` (1440);
  - `recurringEventId` is read for `series_id`; it isn't read today;
  - Jitsi links (`meet.abroadworks.com`, `meet.jit.si`) are detected; today detection covers Meet, Zoom and Teams only (`calendar_client.py:563-569`). Meet is found only through `hangoutLink` (`:563`); Zoom and Teams are scanned from `conferenceData.entryPoints[].uri`, `location` and `description`. Jitsi detection scans the same three fields for the configured hosts.

### 9.3 Sending

- **New table `aw_entries`** (migration `0011`), primary key (`owner_email`, `event_id`): `meeting_uuid`, `sent_hash` (hash of the fields sent), `state` (`synced` / `rejected` / `error`), `last_error`, `attempts`, `next_attempt_at`, `last_seen_at`, `end_at`, `updated_at`.
- **Each cycle, per `aw-bots` user:**
  - **Event in the read**, and its hash differs from `sent_hash` (or it's new): `PUT /v2/entries`, then store `meeting_uuid` and `sent_hash`.
  - **Row not in the read, and its `end_at` has passed:** the meeting simply ended. Delete the local row and **send nothing**.
  - **Row not in the read, its `end_at` still ahead, and not seen for 5 min** (the same grace as `_reconcile_cancellations`, `main.py:890-940`, `_CANCEL_GRACE` at `:285`): one `events().get(calendarId="primary", eventId=…)` to name the reason — `status == "cancelled"` → `cancelled`; the user's own attendee entry `responseStatus == "declined"` → `declined`; 404/410 → `deleted`; the event still exists with a start beyond the horizon → `moved_out_of_window`; the event still exists in the window → it was a partial read, send nothing. Then `POST /v2/entries/remove` with that reason, and delete the local row. Today's path never looks and calls everything a cancellation; with a 14-day window a move beyond the horizon is common, and it must not show as "cancelled" in the portal.
  - **Google push notifications** (`POST /webhooks/google-calendar`, `main.py:2425`, using `calendar_connections.watch_*`) keep working for `aw-bots` users as a trigger for an immediate read of that user; the no-grace cancel path `_apply_webhook_cancellations` (`:943-983`) is not used for them — every removal goes through the confirmed vanish rule above.
  - **Every 6 hours** (`AW_BOTS_FULL_RESYNC_HOURS`): re-send everything in the read; unchanged entries reply `unchanged`.
- **Errors:**
  - 400: `state='rejected'` with the code; not retried until the event changes or the next full resync.
  - 5xx or timeout: retried with backoff (60 s doubling to 1 h, as the read backoff, `main.py:241-273`).
- **Replaced for `aw-bots` users:** the publish (`main.py:876`), the cancel (`main.py:1007-1009`), liveness (`main.py:821, 1310`), `_duplicate_bot` (`main.py:325-370`) and `_complete_finished_meetings`. aw-bots does all of that now.
- The calendar module **doesn't write `tracked_meetings`** for `aw-bots` users; the portal reads their meetings from aw-bots (§10.3).
- **Auth:** the account's API key from Secret `aw-bots-portal-api-key` (namespace `notetaker`, key `VEXA_API_KEY`; the same namespace as calendar-dispatcher; nothing references it yet), plus setting `AW_BOTS_BASE_URL` = `http://aw-bots-vexa-gateway.aw-bots.svc.cluster.local:8000` (`deployment/base/aw-bots/README.md:663-664`), header `X-API-Key`.
- **`meeting_key.py`:** not used on this path. aw-bots parses and matches links itself (R1). `meeting_key.py` stays the canonicaliser for the old path. This is a change of responsibility and gets a CHANGELOG line.

---

## 10. Portal changes and rollout

### 10.1 Portal (for `aw-bots` users)

- **Instant join:** the server route (`portal/src/app/api/dispatch/route.ts:96-189`, today `POST {DISPATCHER_BASE_URL}/dispatch` with `X-Dispatch-Token` at `:132-145`) calls `PUT /v2/entries` with `join_now` (§6.2) instead. Settings: `AW_BOTS_BASE_URL`, and the API key from Secret `aw-bots-portal-api-key`.
- **Lists and detail:** `GET /v2/meetings?user=<signed-in email>&from=&to=` instead of the `tracked_meetings` queries (dashboard `meetings.ts:159-164`, `:195-200`; paged/count/search around `:659`, `:825`, `:910`, `:959`; detail `meeting-detail.ts:79-105`; all on `TENANT_FILTER`, `meetings.ts:103`).
- **Transcript and audio:** from `meeting.export.s3_path` instead of `s3PrefixFor` = `recordings/{platform}_{event_id}_{job_id}/` (`meetings.ts:283-289`). Under that prefix the portal reads `notes.json`, `participants.json`, `transcript.txt` and `full_session.m4a` (fallback `.wav`) (`lib/s3.ts:47-70, 194-220, 406-454`); the exporter's folder holds the same files, so only the prefix source changes. Note the list/detail pages derive `platform` from the URL with a `"meet"` fallback (`lib/platform.ts:50-64`); with aw-bots the object's `platform` is authoritative.
- **Join progress:** `GET /v2/meetings/{id}` every 2.5 s (the current cadence, `JOIN_POLL_INTERVAL_MS`, `join-progress.ts:54`; 90 s timeout at `:63`), instead of the SQL + S3 probes of `audio_chunks/` and `lobby_state.json` (`join-state.ts:133-184`).
- **Stop bot:** `POST /v2/meetings/{id}/stop`.
- **Refresh:** in v1 open pages **poll** `GET /v2/meetings/{id}` (the join-progress cadence while live, slower otherwise). A webhook receiver `POST /api/webhooks/aw-bots` (verify §7.4, dedupe on `event_id`) is added only once the portal has a push channel to browsers (SSE or a websocket); a stateless route that "stores nothing" cannot refresh a page by itself. Until then the portal is not a webhook subscriber, and the first subscriber is whichever client is built next.

### 10.2 Jitsi cutover switch (handoff §6 A1)

- `meet.abroadworks.com` is recorded by Jibri today. Until the cutover, aw-bots refuses that host (`ENTRY_BLOCKED_HOSTS`, error `platform_not_enabled`), for both the calendar module and the portal.
- This check is needed because aw-bots' parser would otherwise accept a pasted `meet.abroadworks.com` link even without `VEXA_JITSI_HOSTS`: pasted links with a `meet` label are accepted (`meeting_link.py:141-151`).
- **Cutover:** turn Jibri's recording off and remove the host from `ENTRY_BLOCKED_HOSTS` in the same change, then restart the calendar module (its first cycle is a full resync).

### 10.3 Who sees a meeting

A user sees a meeting when they are the `user` of one of its entries, meaning it's on their own connected calendar or they started it. That's a change from today's rule (owner or listed attendee, `meetings.ts:103`): an attendee who hasn't connected a calendar no longer sees it.

### 10.4 Rollout order

1. **aw-bots.** Build and deploy:
   - create the new index;
   - deploy;
   - drop the old index;
   - create the portal's webhook subscription.
2. **Calendar module + portal.** Deploy with every user on `old`; nothing changes for anyone.
3. **Pilot.** Set the owner's connection to `aw-bots`, then run the live tests (§11.3).
4. **Everyone else.** Move all users to `aw-bots`.
5. **Jitsi cutover** (§10.2), then retire the old bot (handoff §6 item 16).

---

## 11. Testing

### 11.1 aw-bots unit tests

Every row of §6. In addition:
- R1 overlap edges (touching times don't merge); the `join_now` adoption bound (09:45 adopts 10:00; 10:20 does not adopt tomorrow's); an open-ended live meeting matches only due entries;
- R2 waiting and release; the merge into an open-ended live meeting;
- R6 `not_sent` for each detail, and a late entry (start past, end ahead) gets a bot at once;
- R7 while live / finished; a `removed` entry re-sent is re-activated (`created` / `joined_existing`);
- the sealed `completion_reason` is never given a value outside the ten, and `outcome` is set for each kind;
- remove → the last-entry stop;
- the room-code resolver for each route kind in §8.4;
- auto-join spawns the exact row;
- webhook signing incl. rotation, retries, `dead`, the log, `sequence`, the SSRF allow-list;
- the exporter uses the UUID.

### 11.2 Against real Postgres

The index swap (MIGRATION-0003 steps), and two scheduled rows plus one live row on the same room. The sqlite fakes can't prove unique partial indexes or advisory locks.

### 11.3 Live, with the owner's account on `aw-bots`

1. A one-off Meet.
2. The yearly test meeting, moved (§6.18).
3. A daily standup over 3 days.
4. A and B on one meeting (one bot, both see it).
5. Back-to-back on one Zoom room.
6. Cancel while the bot is in the meeting.
7. Instant join of an already scheduled meeting.
8. Decline by one of two users.

### 11.4 Calendar module

- Hash-based sending; ended vs vanished; the `events().get` confirmation gives each of the four reasons and sends nothing on a partial read;
- rejected vs retried;
- full resync;
- the `old`/`aw-bots` switch leaves `old` users byte-for-byte unchanged (the existing 519 tests stay green).

---

## 12. Open items

| # | Item | Needs |
|---|---|---|
| O1 | `notetaker-worker` `/process` must accept a UUID `meeting_id` (today `vexa-<n>`). The canonical contract types it as a free string (`§4`, `meeting_id: str`, idempotency by `meeting_id`/`idempotency_key`), so only the worker's implementation can object. The worker is in the talke repo; Jitsi compatibility must hold (CLAUDE.md hard constraint 2). | code check in talke |
| O2 | The account's API key must carry both `bot` and `tx` scopes (gateway scope check is any-of per route, `gateway/app.py:383-384`; `/bots/*` = `bot`, `/meetings/*` and `/transcripts/*` = `tx` in `core/meetings/routes.v1.json`). | check user 1's key |
| O3 | Zoom vanity links `zoom.us/my/<name>`: the calendar module understands them (`meeting_key.py`), but aw-bots' parser needs digits (`meeting_link.py:103-105`), so they would be `unrecognized_link`. Whether the bot can join them is untested. | decide: support or reject |
| O4 | Jitsi room names keep their case in aw-bots (`meeting_link.py:152-161`). If Jitsi treats `Standup` and `standup` as one room, R1 must compare lower-cased. | check on `meet.abroadworks.com` |
| O5 | Webhook secrets are stored in the database as today's per-user secret is. Encrypt the column at rest? | owner |
| O6 | Record in `CHANGELOG.md`: calendar reading **stays in calendar-dispatcher** (reverses handoff §4, "moves into the portal"); `meeting_key.py` isn't used on the aw-bots path. | with the implementation |
| O7 | Vexa's own calendar sync assumes the old index (`calendar_sync/service.py:572-674`, `by_native` adoption). It is not used, but user 1 has calendar settings (`PUT /user/calendar` set the bot name). Confirm no ICS feed is configured, so the sync never runs. | check user 1 |
| O8 | Handoff §4 (2026-09-25 lines) says calendar reading "moves into the portal" and recommends sending a series once for aw-bots to expand; this design keeps reading in `calendar-dispatcher` and sends one entry per occurrence. Both go in `CHANGELOG.md` (with O6) and the handoff lines get a "superseded by this design" note. | owner confirms, then edit handoff |

---

## 13. Facts from the code this design rests on (checked 2026-09-25)

### 13.1 aw-bots

- **Unique index:** `uq_meeting_active_user_platform_native` on `(user_id, platform, platform_specific_id)` `WHERE status NOT IN ('completed','failed')` (`meeting_api/sessions/models.py:111-116`; `admin_api/schema/models.py:163-169`). It covers `scheduled`/`idle` too (`bot_spawn/adapters.py:524-527`).
- **Schema:** no alembic; `ensure_schema` = `create_all` + add missing columns and indexes by name (`admin_api/schema/sync.py`); unique indexes are built by hand `CONCURRENTLY` on live databases (MIGRATION-0002).
- **Statuses:** planned `idle`, `scheduled`; bot `requested, joining, awaiting_admission, needs_help, active, stopping, completed, failed` (`collector/app.py:49-55`). Terminal: `completed`, `failed`.
- **Meeting table:** integer PK, no UUID or external-id column (`sessions/models.py:45-59`). The only UUID is per bot session (`bot_spawn/service.py:618`).
- **`POST /meetings`:** 409 on any non-finished row for the room (`collector/adapters.py:1234-1244`, `collector/app.py:474-478`).
- **Auto-join:** selects `status='scheduled'` rows with a room (`bot_spawn/adapters.py:575-590`); due in `[start − lead, start + grace]` (`auto_join.py:118`); a live sibling gives `skipped_live` (`auto_join.py:246-279`); after grace an API-created row stays `scheduled` (`auto_join.py:78-127`). Live set `LIVE_STATUSES` at `auto_join.py:133-136`. Spawn dedup (`bot_spawn/adapters.py:485`) checks only `requested`/`joining`/`awaiting_admission`/`active` under `pg_advisory_xact_lock(user_id)` (`:490`); the index catches the rest.
- **Stop:** `DELETE /bots/{p}/{n}` cancels planned rows and stops live ones on the room (`lifecycle/stop_router.py:106-232`).
- **Bot callback:** keyed by `connection_id` = `session_uid` (`app.py:409-546`).
- **Link parser:**
  - Meet is lower-cased;
  - Zoom takes the first 9–11 digit run on any host containing "zoom", so `us02web.zoom.us/j/…?pwd=` = `zoom.us/j/…`;
  - Teams takes the thread id or `/meet/<id>`;
  - Jitsi is the bare room on `meet.jit.si` and `room@host` elsewhere (`collector/meeting_link.py:161`); the `meet` label heuristic (`:150`) and the non-Zoom-host Zoom branch (`:129-132`) apply only to pasted links (`generic_hosts=True`), and bare 9–11 digits are Zoom (`:165-166`).
- **Webhooks:** one system URL + one per-user URL, snapshotted at spawn; signing `X-Webhook-Signature` HMAC-SHA256 over `ts.body` plus `Authorization: Bearer <secret>`; retries 60/300/1800/7200 s (`retry.py:56`, max age 86 400 s at `:58`); non-429 4xx never retried (`delivery.py:170-178`); only `meeting.completed` on by default (`delivery.py:32`); ledger first attempt only (`webhooks/*`, `bot_spawn/service.py:706-711`; `app.py:798-812` is the one `record` caller). `event_id` = `sha256(connection_id|event_type|new_status)` (`lifecycle/webhook.py:67-80`).
- **Gateway:** `core/gateway/services/gateway/src/gateway/app.py` — strips authority headers (`:408-410`), sets `x-user-id` (`:412`), any-of scope check (`:383-384`), undeclared routes refused (`:372-382`).
- **Tests:** meeting-api has no sqlite; fakes are in-memory (`bot_spawn/fakes.py`, `collector/fakes.py`) plus fakeredis; one real-Postgres test runs only with `MEETING_API_TEST_DATABASE_URL` (`tests/test_single_flight.py:205-218`). §11.2 uses that hook.
- **Exporter:** `vexa-<n>` from the integer id (`exporter/job.py:98-100`); folder `<platform>_<room>_<start>` (`exporter/naming.py:19-24`).

### 13.2 Room-code lookups today (all pick the newest row)

| Route or code path | Location |
|---|---|
| `get_transcript` | `collector/adapters.py:468-494` |
| `authorize_subscribe` | `:698-732` |
| participants | `:734-772` |
| workspace | `:774-799` |
| share | `:819-840` |
| docs | `:1075-1115` |
| `set_intent` | `:1117-1162` |
| `_resolve_owned_native` (native `PATCH`/`DELETE`, annotate, chat) | `collector/app.py:499-506` |
| `find_latest` (`continue_meeting`) | `bot_spawn/adapters.py:140-156` |

### 13.3 calendar-dispatcher

- **Read:** `events().list(calendarId="primary", singleEvents=True, orderBy="startTime")`, window `CALENDAR_HORIZON_MINUTES` (1440) (`calendar_client.py:519-531`). `recurringEventId` and `originalStartTime` aren't read anywhere.
- **Hand-offs to the old bot:** publish (`main.py:876`, `:2338-2344`), cancel (`:1007-1009`), liveness (`:821`, `:1310`, `:2139`).
- **Tables:** `tracked_meetings` PK is the Google instance id (`notetaker_postgres/models.py:146`); latest Alembic revision `0010` (`0010_add_tracked_meetings_display_names`). `calendar_connections` (`models.py:26-75`) has no column that could act as the §9.1 switch (`provider` ≠ `google` makes the dispatcher skip the user, `connections.py:82`), hence the new `bot_backend` column.
- **Reads:** `load_active_connections` (`connections.py:41-97`) = `status == 'active'` and `provider == 'google'`. Pagination is 250/page, capped at 20 pages, and a hit on the cap raises rather than returning a partial list (`calendar_client.py:30, 536-541`) — so a 14-day window on a very busy calendar fails loudly, never silently drops events.

---

## 14. Found while checking, not part of this work

These are listed for a separate decision. None is changed by this design.

1. The bot's lifecycle callback sends `x-internal-secret`, but meeting-api doesn't check it (`meeting_api/app.py:932-938`).
2. `PUT /meetings/{p}/{n}/intent` never checks the status, so it can overwrite a row the bot owns (`collector/adapters.py:1117-1162`).
3. `PUT /user/webhook` doesn't validate the URL when it's saved; the SSRF guard runs only at send time (`admin_api/app/main.py:633-649`).
4. `needs_human_help` appears in some status lists (`auto_join.py:135`, `collector/app.py:246`), but the state machine emits only `needs_help`. At `collector/app.py:246` (`_NON_PLANNED_STATUSES`) the misspelling means `GET /meetings?exclude_planned=true` hides rows in `needs_help`.
5. The comments at `admin_api/schema/models.py:160-162` and `sessions/models.py:105` say a failed unique index is swallowed; `admin_api/schema/sync.py:93-142` raises and stops admin-api.
6. The spawn dedup at `bot_spawn/adapters.py:485` omits `needs_help` and `stopping`; only the unique index stops a second spawn during those.
