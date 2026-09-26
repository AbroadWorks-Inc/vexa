# AW Bots: meeting intake and webhooks — design and implementation plan

- **Date:** 2026-09-25, revised 2026-09-26.
- **Status:** **final for build, revision V11.** This is the only document for this work: the design (Parts 0–4), the rollout order (Part 5) and the step-by-step build plan (Part 6).
- **Scope:** handoff §6 A (`aw-notetaker/docs/handoffs/2026-09-24-aw-bots-handoff.md`). It covers how any app sends meetings to aw-bots, and how aw-bots reports every result back.
- **Where the work happens:** in the existing checkouts, with no extra folders or worktrees.
  - aw-bots: `aw-notetaker/vexa-fork`, branch `feat/meeting-intake` (from `development`).
  - aw-notetaker: `aw-notetaker`, branch `feat/calendar-aw-bots`, created from `feat/aw-bots-deployment` when Part B starts.
- **For an implementing agent:** use superpowers:subagent-driven-development (the owner's chosen method). Each task gets a fresh implementer and a fresh reviewer, and the whole branch is reviewed at the end. Every unit is built test-first (superpowers:test-driven-development), and nothing is reported done without superpowers:verification-before-completion.

## Revision log

- **V1 — the sealed enum stays.** `completion_reason` is the sealed `lifecycle.v1` enum (ten values, `contracts/lifecycle.v1/README.md:41-44`). AW's cause is an additive `outcome` block instead.
- **V2 — instant join adopts by an explicit rule** (R1 `join_now`), and a due meeting merges into a live open-ended one (R2).
- **V3 — a removed entry that comes back is re-activated,** and the calendar module confirms every disappearance with Google.
- **V4 — a meeting is due until its end,** so a meeting joined mid-call still gets a bot (R6).
- **V5 — the seals and gates are named** (schema, contracts, architecture).
- **V6 — citations corrected.**
- **V7 — `event_id` defined for the new events.**
- **V8 — owner answers.** Visibility stays owner-or-invited, carried by the `attendees` field. Webhook secrets are supplied by the receiver and encrypted at rest. Zoom takes numeric links only. O1, O2, O3, O7 checked live.
- **V9 — production grade (2026-09-26).** Named keys per consumer; our state in a typed side table; a replica-safe webhook sender; limits, metrics, alerts and retention; a sealed `intake.v1`. V9 was reverted on `development` (`60b358b7`) and restored by owner decision the same day.
- **V10 — corrected after the implementation check (2026-09-26, ~160 citations re-checked).**
  - The index-swap runbook is `MIGRATION-0008` (0001–0007 are taken).
  - No aw-bots service exposes `/metrics` today.
  - Only admin-api's model has the "swallows a failed unique index" comment.
  - A two-value `X-Webhook-Signature` would break sealed `webhook.v1`, so the old signature rides its own optional header.
  - Owner decisions: a transactional outbox; erasure deletes nothing in `aw-chatworks-transcribe`; `POST /bots` claims by the R1 `join_now` rule; alerts as a Prometheus rule group; `quota_exceeded` distinct from `rate_limited`.
- **V11 — owner decisions after the second review (2026-09-26).** One document; aw-bots is a service; each sending app owns its data.
  1. The document has four parts: service, client contract, calendar-module notes and portal notes.
  2. The old cloud bot is decommissioned and out of scope. There is no per-user switch, and the calendar module's old-bot code is removed.
  3. Every client goes through the gateway, which signs the identity it forwards. The bots alone talk to meeting-api directly, as Vexa does, checked with the internal secret.
  4. The exporter reads through the gateway and reports its export result through the gateway.
  5. A finished meeting's entries are closed in the same transaction, and only unfinished meetings count toward the entry limit.
  6. A future time is always a new meeting. A change stored while a meeting was live is applied when it finishes.
  7. Every status change goes through one writer, which also writes the outbox.
  8. Delivery state lives in Postgres (leased workers, `SKIP LOCKED`). Redis is not used for webhooks.
  9. There is one lock order, and intake status changes are conditional.
  10. The scheduler reads only due meetings, through a partial index.
  11. Every failure carries a typed code and the exact message. Waiting for the meeting link imposes no 5-minute pause.
  12. Stop is for a bot in the call only; otherwise `no_live_bot`.
  13. Reading one meeting takes `user=`.
  14. Least-privilege keys and new scopes: `webhooks`, `erase`, `export`.
  15. Traffic limits are sized to our numbers. In-cluster callers skip the per-address guard, and the calendar module reconciles by comparing, never by blind resend.
  16. A deleted or paused subscription cancels its waiting deliveries.
  17. Erasure covers aw-bots' own data only.
  18. Event ids are the full sha256, and payloads are stored as exact text.
  19. `meetings.uuid` is added in three steps.
  20. `intake.v1` is sealed after the routes are built.
  21. `cryptography==50.0.1` (owner-approved deviation from the §10A.2 pin 42.0.5) and `prometheus-client==0.19.0`.
  22. Alerts live in aw-notetaker `deployment/base/aw-bots/alerts.yml`, with per-service scrape annotations.
  23. The bot limit stays at 45.
  24. Link choice is the sender's job.

---

# Part 0 — Overview

## 0.1 Summary

- **aw-bots is a service.** It does four things: it takes meetings from any app through one API (`/v2`); it decides when to send a bot, never sending two to one meeting; it runs the bot; and it reports every result by signed webhook to every app that subscribed. It never reads a calendar, and it knows nothing about any app's users.
- **Each sending app owns its own data.** The calendar module (`calendar-dispatcher`) keeps aw-bots up to date: new meetings, changes, removals, and invites that no longer qualify. The portal decides which employee may see which meeting and handles its own webhooks. Any other client uses the same API and webhooks.
- **aw-bots still checks everything itself.** Duplicates, overlapping times, one bot per meeting link, limits and auth hold whatever a sender does.

```
Google Calendar ──(read)──> Calendar module ──PUT/remove entries──> gateway ──> aw-bots (meetings, scheduler, bots)
Portal ──instant join / stop / read (always "for whom")──────────> gateway ──┘      │
  ^                                                                                  │ signed webhooks (delivery state in Postgres)
  └──────────────────────────────────────────────────────────────────────────────────┘
                                                                                     │ meeting.completed (system URL)
                                                                                     v
                                                          exporter ──(gateway)──> reads + export result
                                                             └──> notetaker-worker /process
```

## 0.2 Words used in this document

| Word | Meaning |
|---|---|
| **Account** | One aw-bots user that a client's systems act as. AbroadWorks' account is the service user `notetaker@abroadworks.com` (id 1). aw-bots has no organisation level above the user (`NO_ORG = ""`, `admin_api/app/events.py:45`), so **one client = one account**. |
| **User** | A person whose calendar or click produced a request, identified by email. Users are data inside an account, not aw-bots logins. |
| **Entry** | One person's calendar invite (or one click) for a meeting. Three colleagues with the same invite means 3 entries, 1 meeting, 1 bot. |
| **Meeting** | What aw-bots records and sends a bot to. One meeting can have several entries. |
| **external_id** | The sender's own id for an entry, **any string up to 255 characters, chosen by the sender**. aw-bots never parses it, never checks a prefix and never infers anything from it; it only stores it and uses it with `user` as the key for later updates and removals. Examples: `google:3n5kq8example` (a Google Calendar event), `outlook:AAMkAGI2…` (an Outlook event), `crm:deal-4711` (a CRM), `manual:0f9d1c2a-…` (a click). The id says where the *entry* came from, not which meeting platform it uses: a Google event can carry a Zoom, Teams or Jitsi link. |
| **UUID** (`meeting.id`) | aw-bots' id for a meeting, used in every reply, webhook, exported file and `/process` call. The integer key stays inside the database. |
| **Meeting link** ("room" in code) | Platform plus room code, as parsed from the URL (`google_meet` + `kxo-misr-avz`, `zoom` + `12345678901`). The same link at the same time is one call. |
| **Live** | A bot has been sent and hasn't finished: `requested`, `joining`, `awaiting_admission`, `needs_help`, `active` or `stopping` (`LIVE_STATUSES`, `bot_spawn/auto_join.py:133-136`, which also carries the legacy spelling `needs_human_help`). |
| **Finished** | `completed` or `failed`. A finished meeting is history. |

## 0.3 Who does what

| Module | Does | Holds | Never does |
|---|---|---|---|
| **aw-bots** (gateway, admin-api, meeting-api, runtime) | Accepts entries, applies R1–R8, sends and runs bots, sends webhooks, serves reads | Meetings and entries up to 30 days ahead plus all history; subscriptions and delivery state | Read a calendar; know an app's users |
| **Calendar module** (`calendar-dispatcher`) | Reads each connected user's Google calendar; decides which invites qualify and which link to send; sends entries and removals | Users' calendar tokens; what it last sent per (user, event) | Send or stop a bot |
| **Portal** | Sign-in, meeting list and detail, instant join, stop, webhook receiver, live page updates | Nothing about meetings of its own | Read calendars; talk to bots |
| **Exporter** (part of aw-bots) | On `meeting.completed`: builds the S3 folder, calls `/process`, reports the export result | Its S3 queue | Transcribe; delete meeting data |

---

# Part 1 — The aw-bots service

## 1.1 Rules

**R1: The same meeting link at overlapping times is one meeting, within one account.**
- When an entry arrives, and a non-finished meeting in the account has the same link and an **overlapping** time (`entry.start < meeting.end` and `entry.end > meeting.start`), the entry joins that meeting. Otherwise it creates a new meeting.
- Back-to-back meetings (15:00 end, 15:00 start) don't overlap, so they stay separate.
- A merged meeting's time runs from the earliest start to the latest end of its active entries. Once the meeting is live, its time isn't recomputed.
- **`join_now` entries** (a pasted link) join the **earliest** non-finished meeting on the link whose `end > now` and `start ≤ now + JOIN_NOW_ADOPT_AHEAD_S` (default 3600). A paste at 09:45 adopts the 10:00 meeting. A paste at 10:20, after today's standup ended, doesn't adopt tomorrow's. No match → a new open-ended meeting.
- **An open-ended live meeting** has the window `[start, now]`. It matches only entries already started or due (`start ≤ now + lead`).

**R2: One live bot per meeting link. The next meeting waits for the link.**
- Many scheduled meetings may share a link; only one may be live.
- A meeting due while another bot is still on its link waits. It stays `scheduled`, `meeting.waiting_for_room` is sent once, and its bot goes **on the first scheduler tick after the link is free**, with no retry pause.
- If the link is still busy at the meeting's own end, it ends `failed`, outcome `not_sent`, detail `room_busy`.
- **Exception:** if the link is held by an open-ended `join_now` meeting, the due meeting doesn't wait. Its entries move onto the live meeting, which takes its title if it has none. The due meeting ends with outcome `merged_into_live` (`meeting.removed` with `merged_into`). The result is one bot and one recording, visible to the calendar users.

**R3: How far ahead.** aw-bots refuses entries starting more than `ENTRY_MAX_DAYS_AHEAD` (30) days ahead, with `too_far_ahead`. The calendar module sends 14 days ahead (Part 3).

**R4: Every request gets a definite answer:** a `result` (§2.4) or an error `code` (§2.5), plus the meeting as saved.

**R5: Removing the last entry of a live meeting stops the bot.** What was recorded is kept and processed. The meeting ends with the sealed `completion_reason: "stopped"` and outcome `cancelled_by_calendar`. If other entries remain, the bot stays.

**R6: A meeting never stays `scheduled` forever.**
- It is due from `start − lead` until its `end`, so a late bot is better than none.
- If `end` passes without a bot, it ends `failed`, outcome `not_sent`, with the typed reason and exact message (§1.13), and a webhook goes out.

**R7: Changes while live or after the finish.**
- **While live:** an update is stored on the entry, but doesn't change the meeting (`not_changed_live`). When the meeting finishes, any stored entry whose time is now in the future and doesn't overlap the finished meeting becomes a new meeting automatically. Removing the last entry stops the bot (R5).
- **After the finish:** the meeting is history. An update about that meeting's time replies `not_changed_finished`. An update that moves the entry to a **future time that doesn't overlap** the finished meeting is treated as a new entry. It gets a new meeting, and `previous_meeting_id` names the old one.

**R8: Removed meetings are kept as history.** A meeting whose last entry is removed before its bot was sent ends `failed`, `completion_reason: "stopped"`, outcome `cancelled_by_calendar`, with `meeting.removed`. The row is kept.

**Reasons: the sealed enum stays, and AW's cause is additive.**
- `completion_reason` is the sealed `lifecycle.v1` set: `stopped`, `left_alone`, `startup_alone`, `evicted`, `awaiting_admission_timeout`, `awaiting_admission_rejected`, `join_failure`, `auth_session_missing`, `validation_error`, `max_bot_time_exceeded`. `lifecycle/retry.py` classifies on it, so it is never extended.
- Every meeting object carries an additive **`outcome`**, `null` until set:

```json
"outcome": { "kind": "not_sent", "detail": "account_limit", "message": "bot limit reached (45 of 45)", "at": "2026-09-29T04:20:00Z" }
```

| `outcome.kind` | Set when | `detail` | `status` / `completion_reason` |
|---|---|---|---|
| `cancelled_by_calendar` | last entry removed (R5, R8), or its last entry moved to another meeting | the remove `reason`, or `entry_moved` | live → `completed`/`failed` + `stopped`; planned → `failed` + `stopped` |
| `not_sent` | `end` passed, or an instant join failed, with no bot | typed code (§1.13), for example `account_limit`, `spawn_error`, `room_busy`, `ended_before_sent` | `failed` / absent |
| `merged_into_live` | R2 exception | the UUID it merged into | `failed` / absent |

A user's stop (§1.7) sets no outcome: it is the user's `stopped`.

## 1.2 Data

admin-api owns the schema. `ensure_schema` creates missing tables, columns and indexes by name (`admin_api/schema/sync.py`), and meeting-api mirrors every table it uses (`meeting_api/sessions/models.py`). Upstream's `meetings` table gains only `uuid` and two indexes. Everything else is ours, in typed tables.

```python
# meetings (upstream): additions only
uuid = Column(UUID(as_uuid=True), nullable=False, unique=True, server_default=text("gen_random_uuid()"))
Index("uq_meeting_live_user_platform_native", "user_id", "platform", "platform_specific_id", unique=True,
      postgresql_where=text("status IN ('requested','joining','awaiting_admission','needs_help','active','stopping')"))
Index("ix_meeting_scheduled_due", text("meeting_event_time(data, start_time, created_at)"),
      postgresql_where=text("status = 'scheduled'"))
# dropped: uq_meeting_active_user_platform_native (covered every non-finished status incl. 'scheduled')

class MeetingEntry:        # "meeting_entries"
    id BigInteger PK; user_id Integer not null; source_user Text not null; external_id String(255) not null
    meeting_id Integer FK meetings.id ON DELETE RESTRICT not null
    meeting_url Text not null; platform String(100) not null; native_meeting_id String(255) not null
    title String(512) null; start_at DateTime(tz) not null; end_at DateTime(tz) null; time_zone Text null
    series_id String(255) null; attendees ARRAY(Text) null; join_now Boolean not null default false
    metadata_ JSONB null (column "metadata"); content_hash String(64) not null
    state String(16) not null  # 'active' | 'removed' | 'closed'
    removed_reason Text null; created_at/updated_at DateTime(tz) server_default now(); removed_at/closed_at DateTime(tz) null
    UniqueConstraint(user_id, source_user, external_id, name="uq_meeting_entries_user_source_external")
    Index(meeting_id); Index(user_id, platform, native_meeting_id, state); Index(attendees, postgresql_using="gin")
    Index("ix_meeting_entries_active_user", user_id, postgresql_where=text("state = 'active'"))
class MeetingAwState:      # "meeting_aw_state"; meeting_id Integer PK FK meetings.id ON DELETE CASCADE
    scheduled_end_at DateTime(tz) null; time_zone Text null; event_seq BigInteger not null server_default "0"
    outcome_kind/outcome_detail/outcome_message Text null; outcome_at DateTime(tz) null
    last_error_code/last_error_message Text null; waiting_for_room_sent_at DateTime(tz) null
    export_state/export_s3_path/export_error Text null; export_at DateTime(tz) null; updated_at DateTime(tz)
class WebhookSubscription: # "webhook_subscriptions"
    id UUID PK default gen_random_uuid(); user_id Integer not null index; url Text not null
    secret_enc LargeBinary not null; enc_key_id String(64) not null; secret_last4 String(4) not null
    previous_secret_enc LargeBinary null; previous_enc_key_id String(64) null; previous_secret_expires_at DateTime(tz) null
    events ARRAY(Text) not null server_default '{}'; active Boolean not null default true; description Text null
    created_at/updated_at DateTime(tz)
class WebhookOutbox:       # "webhook_outbox": the durable record of every event
    event_id String(80) PK; meeting_id Integer FK meetings.id ON DELETE CASCADE null   # null only for webhook.test
    event_type String(64) not null; sequence BigInteger not null; payload_text Text not null   # exact bytes sent
    created_at DateTime(tz) server_default now(); published_at DateTime(tz) null
    Index("ix_webhook_outbox_unpublished", created_at, postgresql_where=text("published_at IS NULL"))
class WebhookDelivery:     # "webhook_deliveries": one row per (event, subscription) = the delivery state
    id BigInteger PK; event_id String(80) FK webhook_outbox.event_id ON DELETE CASCADE not null
    subscription_id UUID not null; user_id Integer not null
    state String(16) not null   # 'pending'|'sending'|'delivered'|'failed'|'dead'|'cancelled'
    attempts Integer not null default 0; next_attempt_at DateTime(tz) not null; lease_until DateTime(tz) null
    last_status_code Integer null; last_error Text null; created_at/updated_at DateTime(tz)
    UniqueConstraint(event_id, subscription_id)
    Index("ix_webhook_deliveries_due", next_attempt_at, postgresql_where=text("state IN ('pending','sending')"))
    Index(subscription_id, created_at)
class WebhookDeliveryAttempt:  # "webhook_delivery_attempts": the log, one row per attempt
    id BigInteger PK; delivery_id BigInteger FK webhook_deliveries.id ON DELETE CASCADE not null
    attempt Integer not null; outcome String(16) not null; status_code Integer null; error Text null
    duration_ms Integer null; created_at DateTime(tz) server_default now(); Index(delivery_id); Index(created_at)
```

- **Meeting times.** `data.scheduled_at` stays the join time that auto-join reads (upstream's field): the earliest start of the active entries. `meeting_aw_state.scheduled_end_at` holds the latest end, or null for an open-ended `join_now` meeting.
- **Why the index swap.** The old unique index allowed only one non-finished row per link. With many scheduled occurrences per link, only **live** rows may be unique. `ensure_schema` matches indexes by name and never alters one, and a failed unique index stops admin-api (`sync.py:93-142` raises). The comment at `admin_api/schema/models.py:160-162` saying otherwise is stale and is corrected. So the swap is a manual runbook, `admin_api/schema/MIGRATION-0008-meeting-live-dedup-index.md`, run **before** the deploy (Part 5).
- **The due index.** It uses upstream's IMMUTABLE `meeting_event_time()` (created by admin-api's `_sync_functions`) restricted to `scheduled`. It holds only meetings that are still scheduled, so the scheduler never touches history.
- **`meetings.uuid` without locking the live table.** Add it nullable, backfill in batches, build its unique index `CONCURRENTLY`, then set `NOT NULL` through a validated check constraint.

## 1.3 Entry handling (`meeting_api/intake/`)

The module talks to storage through one narrow port, `IntakeStore`, with an in-memory fake beside the Postgres adapter (the pattern of `collector/fakes.py`). It calls the existing spawn and stop paths; it never duplicates them.

**`PUT /v2/entries`**, under the lock of the entry's meeting link (both links, in sorted order, when the link changes):
1. Validate the fields and parse the link (`collector/meeting_link.py`, the one parser). An unknown link → `unrecognized_link`. A host in `ENTRY_BLOCKED_HOSTS` → `platform_not_enabled`.
2. Read the entry by (`user_id`, `source_user`, `external_id`) **under the lock**. If its link changed since the lock was chosen, restart once.
3. **An unchanged entry** (same `content_hash`) → `unchanged`.
4. **A `removed` or `closed` entry comes back** → it becomes active and takes the R1 path (`created`/`joined_existing`, with `previous_meeting_id`). A `closed` entry does this only if R7's future-time rule applies. Otherwise → `not_changed_finished`.
5. **Its meeting is live** → store the change on the entry and reply `not_changed_live` (applied at finish, R7).
6. Otherwise, match R1. Found → attach. Not found → create a meeting (`scheduled`, `data.auto_join=true`).
7. A meeting left with no active entries is removed (R8, detail `entry_moved`).
8. Recompute the affected meetings' times.
9. Write the events (§1.4). Commit, then publish.

**Quota.** Only a write that adds an active entry checks it. The count is an index-only count on the partial index. At `INTAKE_MAX_ACTIVE_ENTRIES` → 429 `quota_exceeded`.

**`join_now`.** After commit, the handler calls the spawn path for that exact row (§1.5):
- sent → `requested`;
- the scheduler got there first, or a bot is already live → `joined_existing`, never `not_sent`;
- a real failure → the meeting ends `not_sent` with the typed code and exact message, and the reply shows it.

**`POST /v2/entries/remove`.** Mark the entry `removed`. If it was the last active entry:
- scheduled → R8;
- live → stop that meeting (§1.7) with outcome `cancelled_by_calendar`.

All status changes are conditional (§1.4). If a scheduled meeting went live meanwhile, the live branch is taken.

## 1.4 One status writer, and the outbox

Every status change, from every code path, goes through one function, `write_status`:
- intake;
- spawn claim and reopen;
- stop;
- pre-session failure;
- planned cancel;
- reconcile sweeps;
- `set_intent`;
- the bot lifecycle callback.

In the caller's transaction, `write_status` does five things in order:
1. Lock the meeting row (`FOR UPDATE`). If its status isn't one the caller expects, raise `StatusConflict` and write nothing. This is what makes every change conditional ("only if still `scheduled`").
2. Write the status (and any data patch).
3. Lock `meeting_aw_state` (creating the row if missing), set the outcome if given, and increment `event_seq`.
4. **On a finished status:** close the active entries. The exception is an entry whose stored time is in the future and doesn't overlap: it is returned for automatic re-run (R7).
5. Insert the event into `webhook_outbox`. The id is `evt_` + sha256(uuid | event_type | sequence), full 64 hex. The payload is the §2.7 envelope built with the one meeting projection, serialized once and stored as the exact text that will be sent.

A guard test fails if anything else in meeting-api writes `meetings.status`.

**Lock order everywhere:** the meeting link's advisory lock (`pg_advisory_xact_lock(hashtextextended('aw-intake:'||user_id||':'||platform||':'||native, 0))`, the single-bigint form upstream pins), then the meeting row, then `meeting_aw_state`.

## 1.5 Scheduler (auto-join)

- **Reads only what is due.** Every 30 s it asks the database only for `status='scheduled'` meetings whose join time is within the next `AUTO_JOIN_LEAD_S` (300 s) or already past. The partial index `ix_meeting_scheduled_due` serves this. Meetings with entries are due until `scheduled_end_at`. Entry-less rows (planned through Vexa's own routes) keep upstream's grace window.
- **Spawns the exact row.** It passes the row id, and the spawn claims exactly that row under the link lock.
- **Upstream `POST /bots`** has no row id. It claims by the R1 `join_now` rule, else inserts: never a future occurrence.
- **Dedup** checks the full live set.
- **Link busy.** If the busy link holds an open-ended `join_now` meeting → merge (R2 exception). Otherwise → `meeting.waiting_for_room` once, with no retry pause: the bot goes on the first tick after the link is free.
- **A real spawn failure** stores `last_error_code` and `last_error_message` and backs off as upstream does (`AUTO_JOIN_RETRY_BACKOFF_S`).
- **Not-sent sweep** (every 30 s, single-flight). Scheduled meetings whose `scheduled_end_at` has passed end `not_sent` with detail = `last_error_code`, else `room_busy` if waiting, else `ended_before_sent`, plus the last message.
- **Settings:** sweep 30 s (`AUTO_JOIN_SWEEP_INTERVAL_S`), lead 300 s (`AUTO_JOIN_LEAD_S`), backoff 300 s, grace `AUTO_JOIN_GRACE_S` (entry-less rows only).

**Spawn failures map to typed codes, never a bare 500:**

| Exception | Code |
|---|---|
| `MaxBotsExceeded`, `QuotaExceeded` | `account_limit` |
| `DuplicateMeeting` | `already_live` |
| `MeetingStopped` | `meeting_stopped` |
| `SpawnFailed` | `spawn_error` |
| `ServiceAuthorityDenied` | `authority_denied` |
| `ServiceAuthorityUnavailable` | `authority_unavailable` |
| `AuthSessionNotConfigured`, `AuthSessionBusy` | `auth_session` |
| `TranscriptionNotConfigured` | `transcription_config` |
| anything else | `internal_error` (logged with its stack) |

## 1.6 Meeting-link lookups on upstream routes

Upstream routes that take a link (platform + room code) used to pick the newest row, which with many scheduled rows is often a future one. One resolver replaces that:

| Route kind | Resolves to |
|---|---|
| Reads (`GET /transcripts/{p}/{n}`, participants, `POST /ws/authorize-subscribe`) and `annotate` | the live meeting, else the most recent that has started; never a future one |
| Planned edits (`PATCH`/`DELETE /meetings/{p}/{n}`, `PUT …/intent`, `POST …/workspace`, `POST …/share`) | the live meeting, or the single scheduled one; several scheduled → 409 `ambiguous_room` |
| Stop (`DELETE /bots/{p}/{n}`) | the live meeting only; it never cancels future plans |

**Entry-managed meetings are edited only through `/v2/entries`.** Upstream `PATCH`/`DELETE /meetings/{id}`, their native forms and `PUT …/intent` answer 409 `managed_by_entries`. A direct edit would otherwise be undone by recomputation, and a hard delete would orphan entries (hence `ON DELETE RESTRICT`). Reads, `annotate` and `share` stay open. The bot's callbacks and recordings are keyed by session and don't change.

## 1.7 Stopping

`POST /v2/meetings/{id}/stop` and R5 stop **the bot that is in the call**:
1. link lock;
2. meeting row;
3. if given, the outcome (on `meeting_aw_state`, so the final webhook carries it);
4. `write_status(stopping)`;
5. the leave command on `bot_commands:meeting:{id}`;
6. a workload delete while the bot is still booting.

A meeting with no live bot → 409 `no_live_bot` ("no bot in this meeting; to cancel it, remove the entry"). A user's stop keeps upstream's `stopped` reason.

## 1.8 Webhook delivery

- **Subscriptions** live in admin-api (§2.7). meeting-api reads an account's active subscriptions through admin-api's internal read, cached for 30 s.
- **Publisher** (single-flight, every 1 s). In one transaction per batch of ≤ 500 unpublished outbox rows, it inserts one `webhook_deliveries` row per matching subscriber (`pending`, due now, `ON CONFLICT DO NOTHING`) and sets `published_at`. A crash before commit means the next tick redoes it: nothing lost, nothing duplicated.
- **Senders** (one loop per meeting-api replica, every 1 s):
  1. Claim due rows with `FOR UPDATE SKIP LOCKED`, set `sending` with a 60 s lease, and commit.
  2. Re-check that the subscription is active (else `cancelled`) and that its URL passes the SSRF guard.
  3. Sign the stored payload text and post it with a 10 s total timeout.
  4. Write an attempt row, then:
     - 2xx → `delivered`;
     - 5xx, 429, timeout or connection error → retry at +60 s, +300 s, +1800 s, +7200 s, then `dead`;
     - any other 4xx → `failed`.

  A crashed replica's lease expires and another replica takes the row.
- **Redis is not used for webhooks.** Its eviction policy (`allkeys-lru`, 1 GB) makes it unsafe for anything that must not be lost.
- **Deleting or pausing a subscription** cancels its pending deliveries in the same transaction.
- **Ordering:** at-least-once, not in order. Receivers order by `sequence` and dedupe on `event_id`.
- **`webhook.test`** (from admin-api through meeting-api's internal route) writes an outbox row (sequence 0, id `evt_test_<uuid4>`) plus one delivery row for that single subscription, both at once. It is never replayed and never counts as stuck.
- **The legacy system URL** (the exporter's trigger) and the per-user URL keep working unchanged, and now carry `uuid`.

## 1.9 Exporter

- **IDs.** The meeting UUID replaces `vexa-<n>` (`exporter/job.py:98-100`) in `speaker_timeline.json`, `participants.json`, and the `/process` `meeting_id` and `idempotency_key`. `_export.json` keeps the integer too. The S3 folder naming `<platform>_<room>_<startUTC>` is unchanged.
- **Everything goes through the gateway** with the exporter's own key (§1.10):
  - reads: `GET /recordings`, `/recordings/{id}/master`, the transcript;
  - after `/process`, the result: `POST /v2/meetings/{id}/export` `{state: "handed_off"|"failed", s3_path, error?}`, scope `export`. meeting-api stores it in `meeting_aw_state.export_*` and emits `export.handed_off` / `export.failed`.

  The exporter no longer uses `X-User-Id` or `INTERNAL_API_SECRET`.
- **The exporter deletes no meeting data, and neither does the bot** (it has no S3 delete code). The exporter never deletes from `aw-chatworks-transcribe`; its IAM role has no `s3:DeleteObject` there. Its only deletes are its own queue markers under `aw-bots/aw-exporter/` (`exporter/queue.py:107, 113`), unchanged. No IAM change.
- **A webhook without `uuid` fails the job loudly.** The rollout drains the exporter queue first (Part 5).

## 1.10 Security, identity and keys

- **Clients go through the gateway only.** The gateway checks the key's scope, sets `x-user-id`, and **signs** it: `x-gateway-signature: t=<unix>,v1=<hex HMAC-SHA256(GATEWAY_IDENTITY_SECRET, "<t>.<user_id>.<METHOD>.<path>")>`. meeting-api and admin-api reject any client request whose signature is missing, wrong, for a different user, or older than 60 s. A pod calling meeting-api directly with `x-user-id: 1` gets 401. (Network policies are not enforced in this cluster: runbook "Security", checked 2026-09-24.)
- **The one direct path is the bots**, as Vexa does. They send status callbacks (`POST /bots/internal/callback/lifecycle`) and upload recordings straight to meeting-api, which now checks `x-internal-secret` in constant time and rejects a mismatch with 401 (the bot already sends it: `services/bot/src/adapters/lifecycle-http.ts:68`). `/runtime/callback` gets the same check.
- **Service-to-service calls** (meeting-api ↔ admin-api internal routes) keep the internal secret, as Vexa has them.
- **One key per consumer, least privilege.** Keys are created, rotated and revoked by name and id. New scopes: `webhooks`, `erase`, `export` (`admin_api/token_scope.py`).

| Key name | Scopes | Where it lives |
|---|---|---|
| `calendar-dispatcher` | `bot` | Secret `aw-bots-key-calendar-dispatcher` (namespace `notetaker`) |
| `portal` | `bot`, `tx` | Secret `aw-bots-key-portal` (namespace `notetaker`) |
| `exporter` | `tx`, `export` | Secret `aw-bots-key-exporter` (namespace `aw-bots`) |
| `operator` | `webhooks`, `erase` | the operator vault only; used by people to create subscriptions and to erase |

- **Rotation:** mint a new key with the same name (`expires_in` 365 days), update the Secret, roll the Deployment, then revoke the old key by id. Expiry is monitored (§1.13).
- **Today:** user 1 has two tokens, ids 1 and 2, both named `portal`, neither used. Both are revoked and the four keys above are minted (Part 5).
- No key is ever logged, echoed in an error or written to a meeting row.

## 1.11 Settings and dependencies

| Setting | Default | Service |
|---|---|---|
| `ENTRY_MAX_DAYS_AHEAD` | 30 | meeting-api |
| `JOIN_NOW_ADOPT_AHEAD_S` | 3600 | meeting-api |
| `ENTRY_BLOCKED_HOSTS` | `meet.abroadworks.com` (until the Jitsi cutover, Part 5) | meeting-api |
| `INTAKE_MAX_ACTIVE_ENTRIES` | 100000 | meeting-api |
| `WEBHOOK_PRIVATE_HOST_ALLOWLIST` | `portal.notetaker.svc.cluster.local` | meeting-api, admin-api |
| `WEBHOOK_MAX_SUBSCRIPTIONS` | 20 | admin-api |
| `WEBHOOK_DELIVERY_RETENTION_DAYS` | 30 | admin-api |
| `WEBHOOK_SECRET_ENC_KEYS`, `WEBHOOK_SECRET_ENC_ACTIVE_KEY` | Secret: key ring `{"<id>": "<32 bytes base64>"}` + active id | meeting-api, admin-api |
| `GATEWAY_IDENTITY_SECRET` | Secret | gateway, meeting-api, admin-api |
| `INTAKE_RATE_LIMIT_PER_MIN` | 600 | gateway |
| `GUARD_IP_WHITELIST` (chart `gateway.guard.ipWhitelist`) | our VPC's address range, so our own apps are never blocked by the gateway's per-IP limit (600 a minute per address). Today every caller is inside the VPC, so the per-IP limit has nothing to act on; it starts acting on outside callers once an entrance exists | gateway |
| `GUARD_TRUSTED_PROXIES` (chart `gateway.guard.trustedProxies`) | empty today (no load balancer in front of the gateway). **Set to the load balancer's addresses in the same change that adds any entrance** (Cloudflare or a private link); otherwise outside traffic arrives from an address inside the VPC and skips the per-IP limit | gateway |
| `VEXA_JITSI_HOSTS` | add `meet.abroadworks.com` | meeting-api |

- Every setting lives in its service's `config.v1.json` and the chart's `values.yaml`; none is in code.
- **Dependencies:** `cryptography==50.0.1` (owner-approved deviation from the §10A.2 pin 42.0.5, whose bundled OpenSSL later releases patched) and `prometheus-client==0.19.0`, in meeting-api and admin-api (`pyproject.toml` and the Dockerfile `uv pip install` line). Nothing else.
- **Jitsi room names are lower-cased at parse time.** The jitsi-meet web client on `meet.abroadworks.com` (`stable-10888`) lower-cases every room name before joining, so `Standup` and `standup` are the same call; lower-casing at parse time is lossless.

## 1.12 Seals and gates

| Sealed artefact | Change | Step |
|---|---|---|
| `schema.seal.json` (gate `db-schema`) | `meetings.uuid`; tables `meeting_entries`, `meeting_aw_state`, `webhook_subscriptions`, `webhook_outbox`, `webhook_deliveries`, `webhook_delivery_attempts` | `pnpm seal:schema`, own commit |
| `contracts.seal.json` (gate `contract-version`) | new `intake.v1`; `webhook.v1`: new `EventType` values and the optional `X-Webhook-Signature-Previous` header (back-compatible: every existing golden still validates). `lifecycle.v1` untouched | `pnpm seal:contracts` after the routes are built, own commit |
| `architecture.calm.json` / `architecture.seal.json` (P23) | node `meeting-api-intake`; flows calendar module → gateway, portal → gateway, exporter → gateway, meeting-api → subscribers, admin-api → meeting-api (`webhook.test`) | `pnpm seal:arch`, own commit |

`contract-conformance` drives the golden webhook examples, which are regenerated from the real builders, never hand-edited.

## 1.13 Limits, observability, retention, scale

**Limits (per account).** A rate says "slow down"; a quota says "stop".

| Limit | Value | Enforced by | Refusal |
|---|---|---|---|
| entry writes (`PUT` + remove) | 600 a minute | gateway, one counter per account in Redis (shared by all gateway replicas) | 429 `rate_limited` + `Retry-After` |
| active entries (unfinished meetings only) | 100 000 | meeting-api, index-only count, only when a write adds an entry | 429 `quota_exceeded` |
| webhook subscriptions | 20 | admin-api | 429 `quota_exceeded` |
| `metadata` per entry | 16 KB | meeting-api | 400 `invalid_request` |

The gateway's per-address guard (`edge_guard.py`, 600 a minute per address) skips our VPC's range (`GUARD_IP_WHITELIST`), so our own apps are never blocked; they are still bound by their keys and the per-account limits. The chart already carries both guard settings (`templates/deployment-gateway.yaml:86-91`); only our values file sets them.

**Typed failure codes** (`outcome.detail`, `last_error_code`): `account_limit`, `already_live`, `meeting_stopped`, `spawn_error`, `authority_denied`, `authority_unavailable`, `auth_session`, `transcription_config`, `internal_error`, `room_busy` and `ended_before_sent`. There is always an exact message too. The same code and message go on the meeting, into the webhook, into the logs and into the metrics.

**Metrics.** Nothing in aw-bots exposes `/metrics` today. meeting-api and admin-api each gain one, and it is in no gateway route table, so it is never public.

| Metric | What it measures |
|---|---|
| `aw_intake_requests_total{route,result}` | intake requests by route and result |
| `aw_intake_request_seconds` | intake request latency |
| `aw_meetings_by_status` | gauge; non-terminal statuses only |
| `aw_meetings_not_sent_total{detail}` | meetings ended `not_sent`, by reason |
| `aw_autojoin_lag_seconds` | bot sent minus (`scheduled_at` − lead) |
| `aw_webhook_deliveries_total{event_type,outcome}` | delivery results |
| `aw_webhook_delivery_seconds` | delivery latency |
| `aw_webhook_pending` | gauge; deliveries due |
| `aw_webhook_outbox_unpublished` | gauge; outbox rows not yet published |
| `aw_export_total{state}` | counted at the export route |
| `aw_api_token_expires_seconds{name}` | time left on each named key |
| `aw_sweep_last_run_timestamp_seconds{sweep}` | when each sweep last ran |

Metrics are labelled by `user_id`.

**Scraping.** The cluster runs plain `prom/prometheus` v2.47.0 (talke `deployment/monitoring-stack/`), which discovers pods by the `prometheus.io/*` annotations. The chart gains per-service `meetingApi.podAnnotations` and `adminApi.podAnnotations`, empty by default.

**Alerts** live in aw-notetaker `deployment/base/aw-bots/alerts.yml`, as one rule group in the `serverFiles.alerting_rules.yml` format. They fire when:
- `not_sent` exceeds 1 % of meetings over 1 h;
- any delivery goes `dead`;
- more than 1 000 deliveries are due for 5 min;
- an outbox row stays unpublished for more than 5 min;
- auto-join lag p95 exceeds 60 s;
- calendar reads for one user fail for more than 30 min;
- a key expires within 30 days;
- a sweep hasn't run for 5 min.

**Logs.** Structured JSON (`obs.log_event`). Every line about a meeting carries `meeting_uuid`, `external_id`, `user` and `account`. Logs never carry a key, secret, URL query string or transcript text.

**Retention and erasure.**
- Meetings, entries and `meeting_aw_state` are kept as history.
- **`DELETE /v2/meetings/{id}`** (scope `erase`) works on **finished** meetings only; otherwise 409 `meeting_not_finished`. It runs upstream's completed-artifact deletion, which removes meeting-api's own raw recording copies in bucket `aw-bots` and then its transcript rows (`collector/app.py:588-628`). It also removes that meeting's entries, outbox and delivery rows. The meeting row and `meeting_aw_state` stay as evidence, and no webhook is sent.
- Transcripts and audio in `aw-chatworks-transcribe` belong to the notetaker and are out of aw-bots' scope. **Nothing in aw-bots deletes from that bucket.**
- **Daily sweep** (admin-api, single-flight): delete delivery rows in a final state older than `WEBHOOK_DELIVERY_RETENTION_DAYS`, then published outbox rows with no deliveries left. Unpublished rows are never pruned; they alert instead.

**Scale (our numbers).** 600 employees and ~200 000 meeting minutes a month give about 230 meetings a working day and ~15 bots at once on average, ~45 at peak (the account's `max_concurrent_bots` is 45).

| Quantity | Value | Cost |
|---|---|---|
| upcoming meetings held (14 days) | ~3 200 | rows; the scheduler reads only the few due ones |
| entry writes per calendar cycle | only changes: tens | trivial |
| reconciliation (6-hourly) | compares ~6 000–10 000 entries by hash; sends only the differences | one paged list per user |
| webhook events per day | ~2 400 outbox rows × subscribers | trivial for Postgres |
| scheduler tick (30 s) | one indexed query | constant |

---

# Part 2 — The contract for any client

## 2.1 Endpoints (all through the gateway)

| Method + path | Scope | Does |
|---|---|---|
| `PUT /v2/entries` | `bot` | Create or update one entry (upsert by `user` + `external_id`); instant join with `join_now: true` |
| `POST /v2/entries/remove` | `bot` | Remove one entry |
| `GET /v2/entries?user=&cursor=&limit=` | `bot` | The sender's active entries for one user, with `content_hash`, so a sender can compare its view with aw-bots' and send only the differences |
| `GET /v2/meetings?user=&from=&to=&status=&external_id=&cursor=&limit=` | `tx` | Meetings a user may see (owner or invited, over entries in any state); cursor paging, `limit` ≤ 200 |
| `GET /v2/meetings/{id}?user=` | `tx` | One meeting by UUID. With `user=`, the same owner-or-invited check; otherwise `meeting_not_found` |
| `POST /v2/meetings/{id}/stop` | `bot` | The bot in the call leaves now; no live bot → `no_live_bot` |
| `DELETE /v2/meetings/{id}` | `erase` | Erase a finished meeting's aw-bots data (§1.13) |
| `POST /v2/meetings/{id}/export` | `export` | The exporter reports its result (§1.9) |
| `/v2/webhooks…` | `webhooks` | Manage webhook subscriptions (§2.7) |

`routes.v1.json` (meetings) and `core/identity/routes.v1.json` (webhooks) carry these rows. The contract is sealed as `core/meetings/contracts/intake.v1/`.

## 2.2 Entry fields (`PUT /v2/entries`)

| Field | Type | Required | Notes |
|---|---|---|---|
| `external_id` | string ≤ 255 | yes | The sender's own id, any format; aw-bots never parses it. Unique per (account, `user`) |
| `user` | email | yes | Lower-cased by aw-bots |
| `meeting_url` | string | yes | **One** link, chosen by the sender. Parsed by aw-bots; unknown → `unrecognized_link` |
| `start`, `end` | ISO 8601 with offset | yes, unless `join_now` | Normalised to UTC; a naive timestamp is refused; `end` must be after `start` |
| `time_zone` | IANA name | no | Display only |
| `title` | string ≤ 512 | no | |
| `attendees` | ≤ 100 emails | no | Lower-cased; used only so `user=` reads match invited people |
| `series_id` | string ≤ 255 | no | The sender's own series id, any format; display and filtering only; aw-bots never expands a series |
| `join_now` | bool | no | Instant join: `start` = now, `end` open. Instant join is decided by this flag only, never by the `external_id` |
| `metadata` | object ≤ 16 KB | no | The sender's own data, stored on the entry and echoed in webhooks |

## 2.3 Remove fields (`POST /v2/entries/remove`)

`external_id` and `user` are required. `reason` is optional: `cancelled`, `declined`, `deleted`, `moved_out_of_window`, `not_eligible` or free text. It is recorded and sent in the webhook.

## 2.4 Reply (every successful call: HTTP 200)

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
      { "external_id": "google:3n5kq8example", "user": "a@abroadworks.com", "attendees": ["a@abroadworks.com", "b@abroadworks.com", "c@client.com"], "series_id": null, "metadata": null }
    ],
    "export": null,
    "sequence": 1
  }
}
```

| `result` | Meaning |
|---|---|
| `created` | A new meeting was made for this entry |
| `joined_existing` | The entry joined a meeting already there (R1): same UUID, no second bot |
| `updated` | The entry changed and the meeting was updated; `previous_meeting_id` names the meeting it left, if it moved |
| `unchanged` | Same data as before; nothing done. Safe to send as often as you like |
| `not_changed_live` | The meeting is live; the change is stored and applied when it finishes if it points to a future time (R7) |
| `not_changed_finished` | The meeting has finished and the change doesn't point to a new future time (R7) |
| `removed` | Remove: it was the last entry; the meeting is removed (R8) |
| `entry_removed` | Remove: other entries remain |
| `bot_stopping` | Remove: it was the last entry of a live meeting; the bot is leaving (R5) |
| `already_removed` | Remove of an entry already removed |

## 2.5 Errors

Every error has the body `{ "error": { "code": "...", "message": "..." } }`. The gateway uses the same shape for `/v2` auth and rate errors.

| HTTP | `code` | When | Client should |
|---|---|---|---|
| 400 | `invalid_request` | missing or wrong field; naive time; `end` ≤ `start`; metadata too big | fix and resend |
| 400 | `unrecognized_link` | aw-bots can't parse `meeting_url` | not retry until the link changes |
| 400 | `platform_not_enabled` | the host is blocked (`ENTRY_BLOCKED_HOSTS`) | not retry until it changes |
| 400 | `too_far_ahead` | `start` > now + 30 days | send later |
| 400 | `already_ended` | `end` ≤ now (not `join_now`) | drop it |
| 401 / 403 | `unauthorized` / `forbidden` | bad key / missing scope | stop and fix the configuration; never mark entries rejected |
| 404 | `entry_not_found` | remove of an entry never sent | drop it |
| 404 | `meeting_not_found` | unknown UUID, another account's, or not visible to `user=` | — |
| 409 | `meeting_not_finished` | `DELETE` on a scheduled or live meeting | remove the entries or stop it first |
| 409 | `no_live_bot` | stop on a meeting with no bot in it | to cancel a future meeting, remove its entry |
| 429 | `rate_limited` | the account's write rate was hit; `Retry-After` set | wait, then resend |
| 429 | `quota_exceeded` | a standing quota is full (entries, subscriptions); no `Retry-After` | stop; free entries or ask for a higher quota |
| 503 | `unavailable` | database down | retry with backoff |

## 2.6 Every use case: what is sent and the reply

These rules hold for any client. "When" is the sender's business (the calendar module's rules are in Part 3).

**2.6.1 One-off meeting.** Send:

```json
{ "external_id": "google:3n5kq8example", "user": "a@abroadworks.com", "meeting_url": "https://meet.google.com/kxo-misr-avz",
  "start": "2026-09-29T09:00:00Z", "end": "2026-09-29T09:30:00Z", "time_zone": "Asia/Kolkata", "title": "Weekly sync",
  "attendees": ["a@abroadworks.com", "b@abroadworks.com", "c@client.com"] }
```
Reply: `created`, as in §2.4.

**2.6.2 Instant join (a pasted link).**

```json
{ "external_id": "manual:0f9d1c2a-6b7e-4f3a-9c1d-8e2b5a7f4c61", "user": "a@abroadworks.com",
  "meeting_url": "https://us02web.zoom.us/j/12345678901?pwd=abc", "join_now": true }
```

The sender generates the `manual:` id once per click. Possible replies:
- `created` with status `requested`: the bot is on its way;
- `joined_existing` (2.6.3);
- `created` with status `failed` and outcome `not_sent`, carrying the exact reason;
- `unrecognized_link`.

**2.6.3 Instant join for a meeting already scheduled or live.** Same request. aw-bots adopts the earliest non-finished meeting on the link with `end > now` and `start ≤ now + JOIN_NOW_ADOPT_AHEAD_S`. A 09:45 paste adopts the 10:00 meeting. The reply is `joined_existing` with that UUID: a scheduled meeting's bot is sent now, and a live one is left as it is. **Never a second bot.** The reverse order (paste first, calendar meeting due later) is the R2 exception.

**2.6.4 Recurring series.** One `PUT` per occurrence, each with its own `external_id`. aw-bots never expands a series.

```json
{ "external_id": "google:9dstandupexample_20260929T043000Z", "user": "a@abroadworks.com", "meeting_url": "https://meet.google.com/abc-defg-hij",
  "start": "2026-09-29T04:30:00Z", "end": "2026-09-29T04:45:00Z", "time_zone": "Asia/Kolkata", "title": "Daily standup",
  "series_id": "google:9dstandupexample" }
```
Reply: `created`, with a new UUID for each occurrence. All occurrences share the link; R2 allows it because their times differ.

**2.6.5 One occurrence moved.** Same `external_id`, new `start`/`end` → `updated`, same UUID, new `bot_joins_at`.

**2.6.6 One occurrence cancelled.**

```json
{ "external_id": "google:9dstandupexample_20260930T043000Z", "user": "a@abroadworks.com", "reason": "cancelled" }
```

Reply:
- `removed`;
- `entry_removed` if another user's entry remains;
- `bot_stopping` if the meeting is live.

**2.6.7 Whole series moved or changed.** New ids: removes for the old ones plus creates for the new (`removed` + `created`). Same ids: updates (`updated`). If an occurrence moved beyond the sender's window, it is removed with `moved_out_of_window` and re-sent when it comes back, which gives a new meeting with `previous_meeting_id`.

**2.6.8 Whole series cancelled.** A remove per occurrence → `removed` each.

**2.6.9 Link changed.** Same `external_id`, new `meeting_url` → `updated`: the meeting moves to the new link. If the new link matches an existing meeting (R1), the reply is `updated` with the new UUID and `previous_meeting_id`. The old meeting is removed if it has no entries left (outcome `cancelled_by_calendar` / `entry_moved`).

**2.6.10 Title changed.** → `updated`, or `not_changed_live` while live.

**2.6.11 A user declined, or an invite stopped qualifying.** A remove with `"reason": "declined"` (or `deleted`, `not_eligible`):
- `entry_removed` if others still have it (the bot still goes for them);
- `removed` if it was the last entry.

**2.6.12 The same meeting on several users' calendars.** One `PUT` per user. B's request:

```json
{ "external_id": "google:3n5kq8example", "user": "b@abroadworks.com", "meeting_url": "https://meet.google.com/kxo-misr-avz",
  "start": "2026-09-29T09:00:00Z", "end": "2026-09-29T09:30:00Z", "title": "Weekly sync" }
```
Reply: `joined_existing`, **the same UUID**, with `entries` listing A and B. There is one bot.

**2.6.13 Back-to-back meetings on one link.** Two creates → two meetings. If the first bot is still there when the second is due, the second waits (R2) and goes the moment the link is free.

**2.6.14 Two accounts, same link, same time.** Each account gets its own meeting and bot. Data never mixes between accounts.

**2.6.15 Cancelled while the bot is in the meeting.** A remove of the last entry → `bot_stopping`. The recording so far is processed. The meeting ends `completed`, `stopped`, outcome `cancelled_by_calendar`.

**2.6.16 Changed after the meeting finished.** The same time → `not_changed_finished`. A future, non-overlapping time (for example, `not_sent` at 10:00, moved at 11:00 to 15:00) → `created`, a new meeting, with `previous_meeting_id` = the finished one.

**2.6.17 Too far ahead, unknown link, blocked host.** → `too_far_ahead`, `unrecognized_link`, `platform_not_enabled`.

**2.6.18 The owner's recurring test meeting.** "Test recurring meeting", yearly, first occurrence Mon 28 Sep 17:00 IST (11:30 UTC), link `kxo-misr-avz`, then moved to Fri 25 Sep 17:00 IST.
- Created: `external_id: "google:6ktestrecurring_20260928T113000Z"`, start `2026-09-28T11:30:00Z` → `created`.
- Moved "this event only": same id, start `2026-09-25T11:30:00Z` → `updated`.
- Moved "all events": as 2.6.7.
- Either way, the bot joins on 25 Sep at 11:25 UTC.

**2.6.19 Stop.** `POST /v2/meetings/{id}/stop`:
- live → 200, the bot leaves, status `stopping`, then `completed` with `stopped`;
- scheduled or finished → 409 `no_live_bot`. A future meeting is cancelled with a remove (2.6.6).

**2.6.20 Other senders and platforms.** Nothing above depends on the sender or the platform. Examples:

```json
{ "external_id": "outlook:AAMkAGI2TG93AAA=", "user": "a@abroadworks.com", "meeting_url": "https://teams.microsoft.com/l/meetup-join/19%3ameeting_Yjk4ZTM2%40thread.v2/0",
  "start": "2026-09-30T10:00:00Z", "end": "2026-09-30T10:30:00Z", "title": "Client review" }
{ "external_id": "crm:deal-4711-call-2", "user": "b@abroadworks.com", "meeting_url": "https://meet.abroadworks.com/Deal4711",
  "start": "2026-10-01T06:00:00Z", "end": "2026-10-01T06:45:00Z" }
{ "external_id": "google:7hzoomexample", "user": "a@abroadworks.com", "meeting_url": "https://abroadworks.zoom.us/j/12345678901?pwd=abc",
  "start": "2026-10-02T09:00:00Z", "end": "2026-10-02T10:00:00Z" }
```
Each is handled by the same rules; the platform comes from parsing `meeting_url` (Teams, Jitsi — lower-cased room, Zoom — the numeric id).

**2.6.21 Reconciling.** The sender pages `GET /v2/entries?user=` and compares each `content_hash` with its own view. It re-sends only the entries that differ or are missing, and removes the ones aw-bots holds but it doesn't.

## 2.7 Webhooks

**Subscriptions.** Managed with the `operator` key, through the gateway, by admin-api.

| Method + path | Does |
|---|---|
| `POST /v2/webhooks` `{url, secret?, events, description}` | Add a subscriber. The receiver normally supplies its secret; if omitted, aw-bots generates one and returns it once. `events: []` means all; each listed event must be a `webhook.v1` `EventType`. At most 20 per account (`quota_exceeded`) |
| `GET /v2/webhooks` | List; secrets never shown (`secret_last4` only) |
| `PATCH /v2/webhooks/{id}` `{url?, events?, active?, description?}` | Change; pausing cancels pending deliveries |
| `DELETE /v2/webhooks/{id}` | Remove; its pending deliveries are cancelled |
| `POST /v2/webhooks/{id}/rotate-secret` `{secret?}` | New secret, shown once; the old one stays valid for 24 h |
| `POST /v2/webhooks/{id}/test` | Sends `webhook.test` to that subscriber now |
| `GET /v2/webhooks/{id}/deliveries?limit=&before=` | The delivery log |

- The URL is checked on save and again on every send by the SSRF guard (`webhooks/ssrf.py`). Private addresses are refused, except hosts in `WEBHOOK_PRIVATE_HOST_ALLOWLIST`.
- Secrets are stored AES-256-GCM encrypted under a key ring (`WEBHOOK_SECRET_ENC_KEYS`, `WEBHOOK_SECRET_ENC_ACTIVE_KEY`). Each ciphertext stores its key id. Rotating the encryption key is: add a key, switch the active id, rows re-encrypt on next read, drop the old key once no row uses it. Secrets are decrypted only at signing time and never returned.

**Events.** Every event carries the full meeting object (§2.4), including `id`, `entries` and `sequence`.

| Event | When |
|---|---|
| `meeting.scheduled` | a meeting was created by an entry |
| `meeting.updated` | time, link, title or entries changed |
| `meeting.removed` | removed before its bot was sent (R8), or merged into a live meeting (`data.merged_into`) |
| `meeting.waiting_for_room` | due, but another bot is on the link (R2) |
| `meeting.not_sent` | ended with no bot; `outcome` carries the typed code and exact message |
| `meeting.status_change` | every bot step (`requested`, `joining`, `awaiting_admission`, `active`, `needs_help`, `stopping`, `completed`, `failed`), with `from`, `to`, `reason` |
| `meeting.started` / `meeting.completed` / `bot.failed` | the existing typed events, enriched |
| `export.handed_off` / `export.failed` | the exporter's result |
| `webhook.test` | a test send |
| `bot.retry` | reserved for the lobby-timeout retry (handoff §6 B9); not emitted by this work |

`DELETE /v2/meetings/{id}` sends no event. Transcription is not an aw-bots event: aw-bots' job ends at `export.handed_off`.

**Envelope:**

```json
{
  "event_id": "evt_7c1e0b0a4d2f4b8e9a3c5d6e7f8a9b0c1d2e3f405162738495a6b7c8d9e0f1a2",
  "event_type": "meeting.status_change",
  "api_version": "2026-09-25",
  "created_at": "2026-09-29T04:26:12Z",
  "data": {
    "meeting": { "id": "5f0c2b7e-8d1a-4c3e-9b6f-2a7d1e4c8b90", "status": "active", "sequence": 7, "...": "as §2.4" },
    "change": { "from": "awaiting_admission", "to": "active", "reason": null, "at": "2026-09-29T04:26:11Z" }
  }
}
```

- `event_id` is unique per event and identical across retries; receivers dedupe on it.
- `sequence` rises by 1 with every event of a meeting; receivers ignore an event older than one they have already applied.

**Signing.** This is the scheme the exporter already verifies (`exporter/signature.py`).
- `X-Webhook-Timestamp: <unix seconds>`
- `X-Webhook-Signature: sha256=<hex HMAC-SHA256(secret, "<timestamp>." + raw body)>`: always exactly one value, as sealed `webhook.v1` requires.
- `X-Webhook-Signature-Previous: sha256=<…old secret…>`: only during the 24 h after a rotation. A receiver accepts a match on either header.
- A receiver rejects a timestamp more than 300 s off.
- There is **no `Authorization` header** on subscription deliveries.

**Delivery.**
- 10 s timeout.
- Retried on 5xx, 429, timeout or connection error at 1 min, 5 min, 30 min and 2 h, then `dead`. Any other 4xx is `failed`.
- Every attempt is logged (`GET …/deliveries`).
- Delivery is at-least-once and not in order.

---

# Part 3 — Notes for the calendar module (`calendar-dispatcher`)

The calendar module owns the correctness of what it sends. aw-bots never asks Google anything.

- **One path.** The old cloud bot is decommissioned. The calendar module sends every connected user's meetings to aw-bots. Its old-bot code (job publish, cancel, liveness, done/prune, `POST /dispatch`, `tracked_meetings` writes, `meeting_key.py`) is removed. The `tracked_meetings` table is kept as history.
- **Its ids.** It sends `external_id = "google:" + <Google event id>` and `series_id = "google:" + recurringEventId`. That prefix is the calendar module's own convention (it keeps ids unique if it later reads Outlook too); aw-bots attaches no meaning to it.
- **Reading.** Unchanged:
  - `singleEvents=True`, so Google lists each occurrence of a series separately, each with its own id;
  - token refresh, rotation and deactivation.

  The window is `AW_BOTS_HORIZON_DAYS` (14). `recurringEventId` gives `series_id`.
- **Which invites qualify.** One function, `is_eligible(event, owner)`. It skips an invite that:
  - the user declined;
  - is all-day;
  - is `outOfOffice`, `focusTime` or `workingLocation`;
  - has no meeting link.
- **Which link to send.** Exactly one per invite. A link the organiser added (a Zoom, Teams or Jitsi link in `conferenceData.entryPoints`, `location` or `description`; Jitsi hosts in `AW_BOTS_JITSI_HOSTS` = `meet.abroadworks.com,meet.jit.si`) wins over Google's automatically added Meet link (`hangoutLink`). The owner may change this rule.
- **Local state.** Table `aw_entries` (notetaker-postgres migration `0011`), primary key (`owner_email`, `event_id`), holding:
  - `meeting_uuid` and `sent_hash` (equal to aw-bots' `content_hash`: the same canonical JSON);
  - `state` (`synced` / `rejected` / `error`), `last_error`, `attempts`, `next_attempt_at`;
  - `last_seen_at`, `end_at`, `updated_at`.
- **Each cycle, for each user:**
  - **In the read and changed** (new, different hash, or an `error` row that is due) → `PUT /v2/entries`.
  - **Gone from the read, and its end has passed** → delete the local row and send nothing: the meeting simply ended.
  - **Gone from the read, end still ahead, not seen for 5 min** → ask Google once (`events().get`) why:

    | Google says | Reason sent |
    |---|---|
    | `status == "cancelled"` | `cancelled` |
    | the owner's own response is declined | `declined` |
    | 404 / 410 | `deleted` |
    | exists, starts beyond the window | `moved_out_of_window` |
    | exists, but `is_eligible` is false (link removed, turned all-day, out of office) | `not_eligible` |
    | exists, eligible, in the window | a partial read: send nothing |

    For any reason except the last, send `POST /v2/entries/remove` with that reason, then delete the local row.
  - **Every 6 hours** (`AW_BOTS_RECONCILE_HOURS`): page `GET /v2/entries?user=`, compare hashes, and send only the differences. Never a blind resend of everything.
  - **Google push notifications** (`POST /webhooks/google-calendar`) trigger an immediate cycle for that user.
- **Pacing and errors.**
  - A token bucket keeps writes below `AW_BOTS_MAX_WRITES_PER_MIN` (500, under the server's 600).
  - `rate_limited`, 5xx or a timeout → back off, honouring `Retry-After` for the whole cycle, from 60 s doubling up to 1 h.
  - `400`/`404`/`409`/`quota_exceeded` → `rejected`, retried only when the event changes or at the next reconciliation.
  - `401`/`403` → stop the cycle and alert; never mark entries rejected.
- **Key:** `calendar-dispatcher` (`bot` scope), from Secret `aw-bots-key-calendar-dispatcher`, sent as `X-API-Key` to `AW_BOTS_BASE_URL` = `http://aw-bots-vexa-gateway.aw-bots.svc.cluster.local:8000`.
- **Metrics:** `aw_calendar_read_total{outcome}`, `aw_calendar_entries_sent_total{result}`, `aw_calendar_cycle_seconds`.

---

# Part 4 — Notes for the portal

The portal decides what its users see and how its pages update. aw-bots only sends webhooks and answers reads.

- **Key:** `portal` (`bot`, `tx`), from Secret `aw-bots-key-portal`, server-side only. It never reaches the browser or the logs.
- **Instant join:** `PUT /v2/entries` with `join_now: true` and `external_id = "manual:" + randomUUID()`, generated once per click.
- **Who sees what.** Every read passes the signed-in user: `GET /v2/meetings?user=` and `GET /v2/meetings/{id}?user=`. aw-bots applies owner-or-invited, including declined guests (today's rule). Anything else is "not found".
- **Transcripts:** read from `meeting.export.s3_path`. With no export yet, the page shows "processing". The transcript files belong to the notetaker.
- **Join progress:** from `GET /v2/meetings/{id}?user=` and the pushed updates.
- **Stop:** the button is shown only while a bot is in the call.
- **Webhook receiver** (`POST /api/webhooks/aw-bots`):
  1. Verify either signature header with the secret the portal supplied when subscribing (Secret `aw-bots-portal-webhook`), and reject more than 300 s of skew.
  2. Publish the event to the portal's Redis (`notetaker-redis`), channel `aw:meeting:<uuid>`.
  3. Only then mark `event_id` as seen (24 h).
  4. If Redis is down, answer 503 so aw-bots retries.
- **Live pages:** Server-Sent Events. One Redis subscriber per portal server, shared by all open tabs. Each meeting id is authorised through `GET /v2/meetings/{id}?user=`. While disconnected, the page refreshes the meeting every 30 s until it reconnects.
- **`tracked_meetings`:** the portal stops reading it. Meetings come only from aw-bots.
- **Dependency:** `ioredis` (a Node dependency outside §10A.2, like `pg`), recorded in aw-notetaker `CHANGELOG.md`.

---

# Part 5 — Rollout order (the human runs these; no session does)

Run it in a window with no meeting in progress. The step numbers continue the runbook (`deployment/base/aw-bots/README.md`, steps 1–8 done).

| Step | Action |
|---|---|
| 9 | **MIGRATION-0008 step 1:** the six new tables, the three-step `uuid`, the live-link index and the due index, all `CONCURRENTLY`. |
| 10 | **Secrets:** add `WEBHOOK_SECRET_ENC_KEYS`, `WEBHOOK_SECRET_ENC_ACTIVE_KEY` and `GATEWAY_IDENTITY_SECRET` to `aw-bots-secrets`, generated without display (the step 4d pattern). |
| 11 | **Deploy admin-api and gateway** (new scopes, signing). The old meeting-api ignores the signature header, so nothing breaks. |
| 12 | **Keys:** revoke tokens 1 and 2. Mint `calendar-dispatcher`, `portal`, `exporter` and `operator` with the scopes in §1.10. The first three go into their Secrets; the operator key goes into the operator vault. |
| 13 | **Drain the exporter queue:** `aw-exporter/pending/` must be empty. |
| 14 | **Deploy meeting-api, bot and exporter together:** one `helm upgrade`, plus `kubectl apply -k deployment/base/aw-exporter`. |
| 15 | **MIGRATION-0008 step 3:** drop `uq_meeting_active_user_platform_native` `CONCURRENTLY`. |
| 16 | **Alerts:** load `alerts.yml` into talke's Prometheus values, then `helm upgrade prometheus`. |
| 17 | **Subscription:** create the portal's webhook subscription with the operator key and a portal-supplied secret (Secret `aw-bots-portal-webhook`). |
| 18 | **notetaker-postgres migration `0011`;** deploy the calendar module and the portal; run the live tests (Part 7). |
| — | **Any future entrance** (Cloudflare hostname or a private link for another VPC): set `gateway.guard.trustedProxies` to the load balancer's addresses in the same change (§1.11). |
| — | **Jitsi cutover** (later): turn off Jibri's recording and remove `meet.abroadworks.com` from `ENTRY_BLOCKED_HOSTS` in the same change. |

---

# Part 6 — Implementation plan

## 6.1 Baseline (measured 2026-09-26)

`$V` = `aw-notetaker/vexa-fork`. `$N` = `aw-notetaker`. Within `$V`:

| Short name | Path |
|---|---|
| `MA` | `core/meetings/services/meeting-api` |
| `MAS` | `$MA/src/meeting_api` |
| `AA` | `core/identity/services/admin-api` |
| `AAS` | `$AA/src/admin_api` |
| `GW` | `core/gateway/services/gateway` |
| `EX` | `integrations/out/aw-notetaker` |

| Package | Command | Result |
|---|---|---|
| meeting-api | `cd $V/$MA && PYTHONDONTWRITEBYTECODE=1 uv run pytest -q -p no:cacheprovider` | 1424 passed, 5 skipped |
| admin-api | same, in `$V/$AA` | 67 passed, 87 skipped |
| gateway | same, in `$V/$GW` | 367 passed, 1 xfailed |
| exporter | same, in `$V/$EX` | 197 passed, 1 deselected |
| calendar-dispatcher | pytest (Python 3.11) | 524 passed |
| notetaker-postgres | pytest (Python 3.11) | 88 passed |
| portal | `cd $N/portal && npx vitest run && npx tsc --noEmit && npx next lint` | 1759 passed / 12 skipped; tsc and eslint clean |
| ruff (per package) | `uvx ruff@0.15.3 check src` | meeting-api 12, admin-api 0, gateway 3 |
| mypy (per package) | `uvx mypy==1.17.1 --ignore-missing-imports src` | meeting-api 132, admin-api 17, gateway 6 |

- `$N/.venv` currently has a broken Python link (it points to a Python 3.14 that no longer exists). Part B needs it rebuilt as Python 3.11, with the owner's OK.
- calendar-dispatcher and the portal remove the old-system code and its tests; the report lists each removed test by name. In every other package counts only go up.

## 6.2 Rules for every task

- **Secrets.** Never read or print a secret value. Tests use dummies. Never open live `*-secrets.yaml`, `.env*` or `vexa-fork/.env.local`. Templates hold `"<REPLACE_ME>"` only.
- **No new S3 deletes.** No IAM change.
- **`lifecycle.v1` stays sealed.** Each seal gets its own commit.
- **One link parser.** Upstream routes keep their shapes except where §1.6/§1.7/§1.10 change them. Every setting lives in `config.v1.json` and the chart. No history in code comments (AGENTS.md).
- **Lint.** New files are clean under black 25.1.0, ruff 0.15.3 and mypy 1.17.1. Touched upstream files aren't reformatted, and their ruff/mypy counts don't rise. The report gives per-file counts before and after.
- **Commits.** Conventional Commits, one concern per commit, with the section in the body. Trailer: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Explicit `git add <paths>`. Never stash, reset, amend or force-push.
- **Pushes.** Push both branches after each green milestone. No PRs, no merges.
- **Gates.** Run every gate that doesn't need the compose env (`stack`, `compose*` and `eval*` need it), and record which ran. Never create `deploy/compose/.env`.
- **Real-Postgres tests (testing only).**
  - Start a throwaway container: `docker run -d --rm --name aw-intake-pg -e POSTGRES_PASSWORD=test -p 55432:5432 postgres:17`.
  - Point the tests at it: `MEETING_API_TEST_DATABASE_URL=postgresql+asyncpg://postgres:test@localhost:55432/postgres`.
  - Remove it afterwards: `docker stop aw-intake-pg`.
  - Ask the owner to open Docker Desktop when it's first needed.
- **Never:** deploy, build images, `kubectl apply`, mint or revoke tokens.

## 6.3 Milestones (push both branches after each is green)

| Milestone | Tasks |
|---|---|
| M1 | A1–A4 |
| M2 | A5–A8 |
| M3 | A9–A13 |
| M4 | A14–A15 |
| M5 | A16–A22 and the full local gate run |
| M6 | B1–B7 |
| M7 | C1–C6 |
| M8 | D1–D2 |

## 6.4 Part A — aw-bots (`$V`, branch `feat/meeting-intake`)

### A1 — Schema and MIGRATION-0008 (§1.2)

**Files:**
- `$AAS/schema/models.py` and its mirror `$MAS/sessions/models.py`. Correct the stale comment at admin-api `:160-162`.
- New: `$AAS/schema/MIGRATION-0008-meeting-live-dedup-index.md`.
- Tests: `$AA/tests/test_schema_intake_tables.py`, `$MA/tests/test_intake_models_mirror.py`, `$MA/tests/test_intake_pg_schema.py` (real PG).

- [ ] **Failing tests:**
  - the six tables with exactly §1.2's columns, nullability, FKs and `ondelete`;
  - both new `meetings` indexes with their predicates, and the old one gone;
  - the mirror matches (if the test-isolation gate forbids a cross-service import, compare against `schema.seal.json`).
- [ ] **Run and expect FAIL:** `cd $V/$AA && PYTHONDONTWRITEBYTECODE=1 uv run pytest -q tests/test_schema_intake_tables.py`.
- [ ] **Implement.** Confirm that `sync.py` renders `gen_random_uuid()` and the ARRAY default, and that `_sync_functions` creates `meeting_event_time` before any index.
- [ ] **Real-PG proofs:**
  - `ensure_schema` works on an empty DB;
  - two `scheduled` rows plus one `active` row on one link are fine; a second live row raises `IntegrityError`;
  - MIGRATION-0008's SQL, run on a DB with the old index and three rows, ends with the new indexes, `uuid` filled and `indisvalid` true;
  - RESTRICT on an entry-managed meeting; CASCADE on an entry-less one;
  - `EXPLAIN` of the due query uses `ix_meeting_scheduled_due`.
- [ ] **Full suites** at or above baseline.
- [ ] **Write MIGRATION-0008** (MIGRATION-0002 style):
  - **Why.**
  - **Pre-check:** no link has two live rows (`… GROUP BY user_id, platform, platform_specific_id HAVING count(*) > 1` returns 0 rows).
  - **Step 1:**
    1. the new tables (SQL generated from the models in the test, pasted verbatim);
    2. `ADD COLUMN uuid uuid` (nullable);
    3. backfill in batches of 1000;
    4. `SET DEFAULT gen_random_uuid()`;
    5. `CREATE UNIQUE INDEX CONCURRENTLY`, with the exact name SQLAlchemy generates;
    6. a `NOT VALID` check, then `VALIDATE`, then `SET NOT NULL`;
    7. the live-link index `CONCURRENTLY`;
    8. the due index `CONCURRENTLY`.
  - **Step 2:** the deploy.
  - **Step 3:** drop the old index `CONCURRENTLY`.
  - **Verify:** `\d meetings`, `indisvalid`.
  - **Rollback:** free before step 3.
  - All commands go through `kubectl -n aw-bots exec -it <postgres pod> -- psql -U postgres -d vexa`.
- [ ] **Commit** `feat(schema): meeting uuid, intake and delivery tables, live-only link index, due index (§1.2)`.
- [ ] **Seal:** `pnpm seal:schema`, then `node scripts/gates.mjs db-schema` green. Commit `chore(seal): schema seal for intake tables (lane:schema)`.

### A2 — Jitsi room names lower-cased (§1.11)

**Files:** `$MAS/collector/meeting_link.py:152-161`; `$MA/tests/test_meeting_link.py`.

- [ ] Tests:
  - `https://meet.jit.si/Standup` → `standup`;
  - `https://meet.abroadworks.com/Team-Sync` (host in `VEXA_JITSI_HOSTS`) → `team-sync@meet.abroadworks.com`;
  - the host is unchanged;
  - all existing cases still pass.
- [ ] Commit `fix(meeting-link): lower-case Jitsi room names (§1.11)`.

### A3 — One meeting projection (§2.4, §1.1)

**Files:** new `$MAS/intake/__init__.py`, `$MAS/intake/README.md`, `$MAS/intake/projection.py`; test `$MA/tests/test_intake_projection.py`.

```python
def project_meeting(meeting: Mapping[str, Any], aw: Optional[Mapping[str, Any]],
                    entries: Sequence[Mapping[str, Any]], *, lead_s: int) -> dict[str, Any]
# exactly the §2.4 "meeting" keys; outcome = {kind, detail, message, at} | None; entries = active only
```

- `completion_reason` and `failure_stage` come from `data`, where upstream writes them.
- `bot_joins_at`: `scheduled_at − lead` while scheduled; the spawn time once sent; `null` for an unsent instant join.
- The projection never contains `user_id`, the integer id, secrets or tokens.
- [ ] Tests:
  - every key;
  - `aw=None`: outcome null, sequence 0;
  - each outcome kind;
  - the forbidden keys.
- [ ] Commit `feat(intake): the meeting projection (§2.4)`.

### A4 — Validation and draft contracts (§2, §2.7)

**Files:**
- New `core/meetings/contracts/intake.v1/{README.md,intake.schema.json,validate.mjs,golden/*.json}` (shaped like `webhook.v1/`).
- `webhook.v1/webhook.schema.json`: the new `EventType` values, and the optional `X-Webhook-Signature-Previous` (`^sha256=[0-9a-f]{64}$`).
- New `$MAS/intake/validation.py`.
- Tests: `$MA/tests/test_intake_validation.py`, `test_intake_contract.py`.

`intake.v1` defines:
- `Entry`, `Remove` and `Reply` (the ten results);
- `Meeting`;
- `EntryState`;
- `Error`, with the §2.5 codes.

There is one golden file per result and per error code.

```python
@dataclass(frozen=True)
class EntryIn:
    external_id: str; user: str; meeting_url: str; start: datetime; end: Optional[datetime]; time_zone: Optional[str]
    title: Optional[str]; attendees: tuple[str, ...]; series_id: Optional[str]; join_now: bool; metadata: Optional[dict[str, Any]]
    content_hash: str
@dataclass(frozen=True)
class RemoveIn: external_id: str; user: str; reason: Optional[str]
class IntakeError(Exception): code: str; message: str; http_status: int; retry_after_s: Optional[int]
def parse_entry(body: Any, *, now: datetime, max_days_ahead: int) -> EntryIn
def parse_remove(body: Any) -> RemoveIn
```

Validation order:
1. JSON Schema → `invalid_request` (never echoing `metadata` or the URL query).
2. Naive time → `invalid_request`.
3. Normalise to UTC.
4. Metadata over 16 384 bytes → `invalid_request`.
5. `end ≤ start` → `invalid_request`.
6. `join_now` → start = now, end = None.
7. `already_ended`.
8. `too_far_ahead`.
9. Lower-case, then compute `content_hash` (the canonical JSON).

- [ ] Tests:
  - the goldens validate;
  - every error path;
  - `+05:30` is normalised;
  - naive times are refused;
  - the 16 384 / 16 385 byte boundary;
  - `content_hash` is stable under key order and changes when any field changes;
  - the existing webhook goldens still validate.
- [ ] Commit `feat(contracts): intake.v1 draft, webhook.v1 additions, request validation (§2)`. Seal later, in A8.

**M1:** verify, then push both branches.

### A5 — The status writer and the outbox (§1.4)

**Files:** new `$MAS/intake/status.py`; test `$MA/tests/test_status_writer.py`.

```python
class StatusConflict(Exception): ...
@dataclass(frozen=True)
class Outcome: kind: str; detail: Optional[str]; message: Optional[str]
@dataclass(frozen=True)
class WrittenEvent: event_id: str; sequence: int; rerun_entry_ids: tuple[int, ...]
async def write_status(db: AsyncSession, meeting_id: int, to_status: str, *, expected_from: Collection[str],
                       data_patch: Optional[Mapping[str, Any]] = None, outcome: Optional[Outcome] = None,
                       change_reason: Optional[str] = None, event_type: Optional[str] = None) -> WrittenEvent
async def write_event(db: AsyncSession, meeting_id: int, event_type: str, change: Optional[Mapping[str, Any]] = None) -> WrittenEvent
def derive_event_id_v2(meeting_uuid: str, event_type: str, sequence: int) -> str   # "evt_" + full sha256 hex
```

- Steps 1–5 are exactly as in §1.4, in the caller's transaction and in the §1.4 lock order.
- The payload is `json.dumps(envelope, separators=(",", ":"), sort_keys=True)`, stored as `payload_text`.
- The legacy `lifecycle/webhook.py` `derive_event_id` (the system URL) is unchanged.
- [ ] Tests (fake session plus real PG):
  - a conflict writes nothing;
  - sequence goes up by exactly 1;
  - a terminal write closes active entries in the same transaction, and a rollback leaves nothing;
  - a future non-overlapping entry comes back in `rerun_entry_ids` and isn't closed;
  - the payload round-trips byte for byte;
  - the lock order holds.
- [ ] Commit `feat(intake): single status writer with outbox and entry closing (§1.4)`.

### A6 — Intake core: rules, port, fake, service; every §2.6 use case (§1.1, §1.3)

**Files:** new `$MAS/intake/{rules.py,ports.py,fakes.py,service.py,settings.py}`; tests `$MA/tests/test_intake_rules.py`, `test_intake_use_cases.py`, `test_intake_service.py`.

```python
# rules.py (pure; LIVE_STATUSES imported from bot_spawn.auto_join)
def overlaps(a_start, a_end, b_start, b_end) -> bool
def meeting_window(m: MeetingView, *, now) -> tuple[datetime, Optional[datetime]]
def match_entry(entry: EntryIn, candidates, *, now, lead_s) -> Optional[MeetingView]
def join_now_target(candidates, *, now, adopt_ahead_s) -> Optional[MeetingView]
def recompute(entries: Sequence[EntryView]) -> Plan
def is_future_move(entry: EntryIn, finished: MeetingView, *, now) -> bool
# ports.py
@dataclass(frozen=True)
class Room: platform: str; native_meeting_id: str
class IntakeStore(Protocol):
    def room_lock(self, user_id: int, rooms: Sequence[Room]) -> AsyncContextManager["IntakeTx"]
class IntakeTx(Protocol):
    async def find_entry(self, user_id, source_user, external_id) -> Optional[EntryView]
    async def room_meetings(self, user_id, room) -> list[MeetingView]
    async def meeting(self, meeting_id) -> MeetingView
    async def count_active_entries(self, user_id) -> int
    async def create_meeting(self, user_id, room, plan: Plan, *, join_now: bool) -> MeetingView
    async def save_entry(self, user_id, entry: EntryIn, room, meeting_id) -> EntryView
    async def mark_entry_removed(self, entry_id, reason) -> None
    async def active_entries(self, meeting_id) -> list[EntryView]
    async def apply_plan(self, meeting_id, room, plan) -> None
    async def move_active_entries(self, from_meeting_id, to_meeting_id) -> None
    async def status(self, meeting_id, to_status, *, expected_from, outcome=None, event_type=None) -> WrittenEvent
    async def event(self, meeting_id, event_type, change=None) -> WrittenEvent
@dataclass(frozen=True)
class SpawnOutcome: result: Literal["sent", "already_live", "failed"]; code: Optional[str]; message: Optional[str]
class SpawnPort(Protocol):
    async def spawn_exact(self, user_id: int, meeting_id: int) -> SpawnOutcome
class StopPort(Protocol):
    async def stop_live(self, user_id: int, meeting_id: int, *, outcome: Optional[Outcome]) -> None
class EventPublisher(Protocol):
    async def publish(self, event_ids: Sequence[str]) -> None
# service.py
class IntakeService:
    async def put_entry(self, user_id, body) -> dict
    async def remove_entry(self, user_id, body) -> dict
    async def merge_into_live(self, user_id, due_meeting_id, live_meeting_id) -> None
    async def rerun_entries(self, user_id, entry_ids: Sequence[int]) -> None
# settings.py: IntakeSettings.from_env() reads ENTRY_MAX_DAYS_AHEAD, JOIN_NOW_ADOPT_AHEAD_S, AUTO_JOIN_LEAD_S, ENTRY_BLOCKED_HOSTS, INTAKE_MAX_ACTIVE_ENTRIES
```

The behaviour is exactly §1.3. `test_intake_use_cases.py` has one test per §2.6 case. Each asserts the reply, the status, the outcome (including message), the entries, the ordered events, and the spawns/stops.

| Test | Expect |
|---|---|
| `test_2_6_1_one_off_created` | `created`, `scheduled`, `bot_joins_at` = start − 300 s, `[meeting.scheduled]` |
| `test_2_6_2_instant_join_new` / `_unrecognized_link` / `_spawn_fails` / `_scheduler_won` | `created`+`requested` / 400 / `failed` with `not_sent`/`account_limit` + "bot limit reached (45 of 45)" / `joined_existing` |
| `test_2_6_3_adopts_0945_for_1000` / `_live_no_second_bot` / `_paste_1020_not_tomorrow` | `joined_existing` / no spawn / `created` |
| `test_2_6_4_series_one_meeting_per_occurrence` | 10 × `created` |
| `test_2_6_5_occurrence_moved` | `updated`, same uuid |
| `test_2_6_6_cancelled` / `_other_entry_remains` / `_live_last_entry` | `removed` / `entry_removed` / `bot_stopping` |
| `test_2_6_7_new_ids` / `_same_ids` / `_moved_out_and_back` | as §2.6.7 |
| `test_2_6_8_series_cancelled` | `removed` each |
| `test_2_6_9_link_changed_moves` / `_joins_other` | `updated`; old meeting `cancelled_by_calendar`/`entry_moved` |
| `test_2_6_10_title_changed` | `updated`; `not_changed_live` while live |
| `test_2_6_11_declined_one_of_two` / `_not_eligible_last_entry` | `entry_removed` / `removed` |
| `test_2_6_12_same_meeting_two_users` | `joined_existing`, same uuid |
| `test_2_6_13_back_to_back` / `test_2_6_14_two_accounts` | two meetings each |
| `test_2_6_15_cancel_while_live` | `bot_stopping` |
| `test_2_6_16_same_time_after_finish` / `_future_time_after_finish` | `not_changed_finished` / `created` with `previous_meeting_id` |
| `test_r7_moved_while_live_reruns_at_finish` | a new meeting created automatically at finish, `meeting.scheduled` |
| `test_2_6_17_too_far_unknown_blocked` | three errors |
| `test_2_6_18_owner_recurring_moved` | `bot_joins_at` 2026-09-25T11:25:00Z |
| `test_finished_meetings_free_the_quota` | 100 000 finished meetings; a new entry is accepted |

- [ ] **Rules tests:** touching times, the join-now bound, the open-ended window, tie-breaks.
- [ ] **Service tests:** `unchanged` emits nothing; removed and closed entries come back; the quota counts active entries only; lock order; restart when the link changed; `completion_reason` stays in the sealed set.
- [ ] Commits: `feat(intake): R1 matching rules (§1.1)`, `feat(intake): entry service with every use case (§1.3)`.

### A7 — The Postgres store (§1.3, §1.4)

**Files:** new `$MAS/intake/adapters.py`; test `$MA/tests/test_intake_adapter_pg.py`.

- [ ] Real-PG tests:
  - two concurrent PUTs for one link and time → one meeting;
  - a duplicate PUT racing itself → one meeting, the second reply `unchanged`;
  - opposite link moves finish within 5 s;
  - two scheduled plus one live row on one link;
  - a forced exception rolls back both the state and the outbox;
  - instant join racing the scheduler → one bot, reply `joined_existing`.
- [ ] Commit `feat(intake): Postgres store with link locks (§1.3)`.

### A8 — `/v2` routes, `GET /v2/entries`, erasure; seal the contracts (§2.1, §1.13)

**Files:**
- New `$MAS/intake/router.py` and `$MAS/intake/reads.py`.
- `$MAS/collector/app.py`: extract `_apply_meeting_delete`'s terminal branch (`:588-628`) into `delete_completed_artifacts(store, deleter, user_id, meeting_id) -> dict`, used by both routes.
- `$MAS/app.py`, `core/meetings/routes.v1.json`.
- Tests: `test_intake_routes.py`, `test_intake_delete.py`.

**Behaviour:**
- Every route and scope is as §2.1.
- **Cursor:** base64 `(meeting_event_time, id)`, the order `ix_meeting_user_event_order` serves.
- **Errors:** the §2.5 shape. Validation failures on `/v2` are 400 (a route-scoped handler; upstream keeps 422). DB down → 503. Another account's uuid → 404.
- **DELETE:** the upstream function first, then one transaction deleting deliveries (attempts cascade), outbox and entries. Reply `{meeting, deleted: {objects, entries, outbox, deliveries}}`.

- [ ] Tests:
  - every code;
  - bodies validate against `intake.v1`;
  - visibility (attendee, removed entry, stranger → 404 on the single read);
  - `GET /v2/entries` paging;
  - stop on a scheduled meeting → `no_live_bot`;
  - DELETE 409s;
  - a storage failure aborts before any row is removed;
  - no call to the exporter or `aw-chatworks-transcribe`.
- [ ] Commits: `refactor(meetings): completed-artifact deletion as one callable` (upstream tests unchanged), `feat(intake): /v2 routes, entry reconciliation list, erasure (§2.1)`.
- [ ] `pnpm seal:contracts` → `chore(seal): intake.v1 and webhook.v1 (lane:contract)`; `contract-version` green.

**M2:** verify, then push both branches.

### A9 — Spawn the exact row; every failure mapped (§1.5)

**Files:**
- `$MAS/bot_spawn/ports.py`: `claim_meeting_id: Optional[int] = None` on `create_meeting_guarded` and `request_bot`.
- `adapters.py:459-573`, `fakes.py`, `service.py`.
- New `$MAS/intake/spawn.py`.
- Test: `$MA/tests/test_spawn_exact_row.py`.

The dedup list at `adapters.py:485` becomes `LIVE_STATUSES`. The claim rules and the error codes are exactly §1.5.

- [ ] Tests:
  - the id claim;
  - `POST /bots` at 09:55 claims today's row, and at 10:40 inserts;
  - a live `needs_help` row blocks;
  - every exception maps to its code and message;
  - the fake and the adapter agree.
- [ ] Commit `fix(bot-spawn): claim the exact row; full live set; every failure mapped (§1.5)`.

### A10 — Scheduler: only what is due; waiting; merge; not-sent (§1.5)

**Files:** `$MAS/bot_spawn/auto_join.py`, `adapters.py` (`list_due_meetings(now, lead_s)`), `$MAS/__main__.py`, new `$MAS/intake/sweeps.py`; tests `test_auto_join_intake.py`, `test_main_auto_join_wiring.py`.

```sql
SELECT m.*, a.scheduled_end_at, a.waiting_for_room_sent_at FROM meetings m LEFT JOIN meeting_aw_state a ON a.meeting_id = m.id
WHERE m.status = 'scheduled' AND meeting_event_time(m.data, m.start_time, m.created_at) <= :now + :lead
```

- [ ] Tests:
  - `EXPLAIN` uses the partial index;
  - 3 200 future rows are not read;
  - a late entry is due at once;
  - a waiting meeting goes on the first tick after its sibling finishes;
  - the merge;
  - each `not_sent` code with its exact message;
  - entry-less rows behave as before;
  - two replicas run each sweep once.
- [ ] Commit `feat(auto-join): read only due meetings; wait without backoff; not-sent with exact cause (§1.5)`.

### A11 — Link resolver and entry-managed meetings (§1.6)

**Files:** new `$MAS/intake/resolver.py`; every site in §8.2 below; the row-id `PATCH`/`DELETE` in `collector/app.py`; `lifecycle/stop_router.py:152-158`; tests `test_room_resolver.py`, `test_managed_by_entries.py`.

- [ ] Tests:
  - each route kind with 1 live + 2 future rows, and with 0 live + 1 past + 2 future;
  - `ambiguous_room`;
  - `managed_by_entries`;
  - `test_stop_route.py` updated with §1.6 cited.
- [ ] Commit `feat(meetings): one link resolver; stop never cancels plans; entry-managed rows (§1.6)`.

### A12 — Stop the bot in the call (§1.7)

**Files:** `lifecycle/stop_router.py` (extract `stop_meeting_row`), new `$MAS/intake/stop.py`; test `test_intake_stop.py`.

- [ ] Tests:
  - booting vs active;
  - `no_live_bot` on scheduled and on finished meetings;
  - the R5 outcome appears on the final webhook;
  - the lock order holds.
- [ ] Commit `feat(intake): stop the bot in the call (§1.7)`.

### A13 — Every status write goes through the writer; enriched events (§1.4, §1.8)

**Files:**
- Route every status writer through `write_status`: `bot_spawn/adapters.py` `:549`, `:196`, `:714`, `:918`; `lifecycle/stop_router.py:319`; `lifecycle/reconcile.py:274, 593`; `collector/adapters.py:1145`; the callback path `app.py:409-546`.
- The legacy projection (`app.py:384-404`) gains `uuid`, `entries`, `outcome` and `sequence`.
- Regenerate the `webhook.v1` goldens from the builders.
- Test: `test_status_writers_all.py`.

- [ ] A guard test fails if anything outside `intake/status.py` writes `meetings.status`.
- [ ] Tests:
  - every writer produces one outbox row and sequence +1;
  - `requested` and `stopping` reach subscribers;
  - the system URL still delivers `meeting.completed`, now with `uuid`.
- [ ] Commit `refactor(meetings): every status change through the status writer (§1.4)`.

**M3:** verify, then push both branches.

### A14 — admin-api: subscriptions, key ring, scopes, retention (§2.7, §1.10, §1.13)

**Files:**
- New `$AAS/app/webhook_subscriptions.py`, `secret_box.py`, `url_guard.py`, `retention.py`.
- `$AAS/token_scope.py` (`VALID_SCOPES` + `webhooks`, `erase`, `export`), `$AAS/app/main.py`, `core/identity/routes.v1.json`, `$AAS/config.v1.json`.
- `$AA/pyproject.toml` + Dockerfile: `cryptography==50.0.1`.
- Tests: `test_webhook_subscriptions.py`, `test_secret_box.py`, `test_webhook_retention.py`, `test_token_scopes.py`.

**Behaviour:**
- **`SecretBox`:** AES-256-GCM, 12-byte nonce, AAD `b"aw-webhook-secret"`. It fails closed if the key ring is bad.
- **Routes:** as §2.7. The internal `GET /internal/users/{id}/webhook-subscriptions` returns ciphertext only and re-wraps lazily.
- **Delete or pause** cancels pending deliveries in the same transaction.
- **The URL guard** follows the same rules as meeting-api's `ssrf.py`, with shared test vectors (the isolation gate forbids importing it).
- **Retention** is as §1.13.

- [ ] Tests:
  - a row encrypted under key A is readable after B becomes active, and re-wrapped;
  - a wrong key id errors;
  - no secret in any response or log;
  - the 21st subscription → `quota_exceeded`;
  - private URLs are refused unless allow-listed;
  - a rotation keeps the old secret for 24 h;
  - delete or pause cancels pending deliveries;
  - the new scopes work;
  - retention runs in order.
- [ ] Commits: `feat(admin-api): webhook subscriptions with key-ring encryption (§2.7)`, `feat(admin-api): webhooks, erase and export scopes (§1.10)`, `feat(admin-api): delivery retention (§1.13)`.

### A15 — meeting-api: publisher, sender, signing (§1.8, §2.7)

**Files:**
- New `$MAS/webhooks/{subscriptions.py,secret_box.py,signing.py,sender.py}`, `$MAS/intake/outbox.py`.
- `$MAS/__main__.py`: the publisher is single-flight, every 1 s; one sender per replica, every 1 s.
- `webhooks/ssrf.py` (allow-list), `app.py` (`POST /internal/webhooks/test`), `config.v1.json`.
- `$MA/pyproject.toml` + Dockerfile: `cryptography==50.0.1`.
- Tests: `test_webhook_publisher.py`, `test_webhook_sender.py`, `test_webhook_signing.py`.

- [ ] Tests (real PG where marked):
  - two senders never send the same delivery (real PG);
  - an expired lease is reclaimed;
  - a publisher crash and redo creates no duplicates;
  - retries post byte-identical payloads;
  - the retry schedule, then `dead`;
  - 400 → `failed`;
  - a subscription deleted or paused mid-retry → `cancelled`;
  - the signature verifies against a test-local copy of the exporter's algorithm;
  - the previous header appears only during a rotation;
  - the allow-list lets the portal host through and blocks `10.0.0.1`;
  - no secret in logs;
  - one attempt row per attempt;
  - `webhook.test` goes to one subscriber and is never replayed;
  - no Redis in this path.
- [ ] Commits: `feat(webhooks): delivery state in Postgres with leased senders (§1.8)`, `feat(intake): outbox publisher (§1.8)`.

**M4:** verify, then push both branches.

### A16 — Signed gateway identity; bot and runtime callbacks (§1.10)

**Files:**
- `$GW/src/gateway/app.py:408-412`: the signature header.
- New `$MAS/identity_guard.py` and `$AAS/app/identity_guard.py`: all routes except `/internal/*`, the bot callbacks, `/health*` and `/metrics`.
- `$MAS/app.py:932-938` and `/runtime/callback`: the internal secret check.
- Config files and the secret template gain `GATEWAY_IDENTITY_SECRET`.
- Tests: `test_identity_signature.py` (gateway), `test_identity_guard.py` (meeting-api and admin-api), `test_callback_secret.py`.

- [ ] **Check first** that:
  - the bot sends `x-internal-secret` on every callback;
  - meeting-api hands the bot its secret;
  - the runtime sends a secret on `/runtime/callback`.

  If any is missing, stop and report.
- [ ] Tests:
  - unsigned, forged, stale and wrong-user requests → 401;
  - a signed request passes;
  - a direct `x-user-id: 1` → 401;
  - callbacks use their secret;
  - every existing route test passes (fixtures sign).
- [ ] Commits: `feat(gateway,meeting-api,admin-api): signed gateway identity (§1.10)`, `fix(meeting-api): require the internal secret on bot and runtime callbacks (§1.10)`.

### A17 — Exporter through the gateway; the export route (§1.9)

**Files:**
- Exporter: `job.py:98-100`, `notetaker.py:37-42`, the id fields in `attribution.py`/`schemas.py`, `vexa_client.py` (`GATEWAY_URL` + `EXPORTER_API_KEY`), new `export_result.py`, `config.py`.
- meeting-api: `POST /v2/meetings/{id}/export` (scope `export`).
- Tests: `$EX/tests/test_job.py`, `test_export_result.py`, `$MA/tests/test_export_route.py`.

- [ ] Tests:
  - UUIDs are used everywhere;
  - a missing `uuid` fails loudly;
  - the export result is retried until accepted;
  - `storage.delete` is called only from `queue.py:107` and `:113`;
  - the exporter's strict lint passes.
- [ ] Commits: `feat(exporter): UUID everywhere; reads and export result through the gateway (§1.9)`, `feat(meeting-api): export result route (§1.9)`.

### A18 — Gateway: per-account write limit and `/v2` error shape (§1.13)

**Files:**
- New `$GW/src/gateway/intake_limit.py`: an `IntakeLimiter` port with two implementations:
  - `RedisIntakeLimiter`: a 60 s window, `INCR` + `EXPIRE` in `MULTI`, on the existing `REDIS_URL`;
  - `InMemoryIntakeLimiter`, for tests.
- `app.py`, `config.v1.json`.
- Test: `test_intake_rate_limit.py`.

- [ ] Tests:
  - the 601st write in a minute → 429 + `Retry-After`;
  - two gateway instances share the count;
  - the count is per account;
  - other routes are unaffected;
  - upstream routes keep `{"detail":…}`;
  - storage down → 503.
- [ ] Commit `feat(gateway): per-account intake write limit and /v2 error shape (§1.13)`.

### A19 — Metrics (§1.13)

**Files:** new `$MAS/metrics.py`, `$AAS/app/metrics.py`; `GET /metrics` on both; instrumentation at each site; `prometheus-client==0.19.0` in both pyprojects and Dockerfiles; tests `test_metrics.py` (both).

- [ ] Commit `feat(meeting-api,admin-api): Prometheus metrics (§1.13)`.

### A20 — Settings and Helm (§1.11)

**Files:**
- Every `config.v1.json`.
- `deploy/helm/charts/vexa/values.yaml` + templates:
  - env wiring;
  - secrets from `secrets.existingSecretName`;
  - `meetingApi.podAnnotations` and `adminApi.podAnnotations`, merged over `global.podAnnotations`, empty by default.
- `deploy/helm/tests/test_template.sh` (a hot file: sequence it).

- [ ] `config-contract` and `test_template.sh` green.
- [ ] Commit `feat(helm): intake settings, identity secret, per-service pod annotations (§1.11)`.

### A21 — Architecture model (§1.12)

- [ ] Update `architecture.calm.json`; run `pnpm arch:dsl`; commit `docs(arch): intake flows (§1.12)`.
- [ ] Run `pnpm seal:arch`; commit `chore(seal): architecture seal (P23)`.
- [ ] Run `pnpm gate:calm` if the network allows; otherwise note it.

### A22 — aw-bots docs

- [ ] Add `docs/changelog.d/aw-meeting-intake.md`, update `$EX/README.md` and the fork `README.md` (`/v2`, webhooks, keys, signed identity).
- [ ] If a task found something new, add V12 to the revision log.
- [ ] Commit `docs(aw-bots): intake and webhooks`.
- [ ] Run the full local gate set and record it.

**M5:** verify, then push both branches.

## 6.5 Part B — calendar module (`$N`, branch `feat/calendar-aw-bots`; Part 3)

### B1 — Migration 0011 `aw_entries`

- [ ] Add `notetaker-postgres/.../versions/0011_add_aw_entries.py` and the `AwEntry` model (Part 3 columns).
- [ ] Tests: upgrade, downgrade and re-upgrade 0010 → 0011.
- [ ] Commit `feat(notetaker-postgres): migration 0011 aw_entries (Part 3)`.

### B2 — aw-bots client with pacing

**File:** new `calendar_dispatcher/aw_bots_client.py`.

```python
class AwBotsResult(NamedTuple):
    kind: Literal["ok","rejected","retry","auth_failed"]; result: str | None; meeting_uuid: str | None
    code: str | None; retry_after_s: int | None
class AwBotsClient:
    def __init__(self, base_url: str, api_key: str, *, max_writes_per_min: int = 500, timeout_s: float = 10.0,
                 transport: httpx.BaseTransport | None = None, clock: Callable[[], float] = time.monotonic) -> None
    def put_entry(self, body: Mapping[str, Any]) -> AwBotsResult
    def remove_entry(self, body: Mapping[str, Any]) -> AwBotsResult
    def list_entries(self, user: str) -> Iterator[EntryState]
```

- [ ] Tests:
  - the Part 3 error mapping;
  - pacing;
  - `Retry-After` honoured for the whole cycle;
  - 401 → `auth_failed`;
  - the key never appears in logs or `repr`.
- [ ] Commit `feat(calendar-dispatcher): aw-bots client with pacing (Part 3)`.

### B3 — Reading: 14 days, series id, one link, eligibility in one function

- [ ] Change `calendar_client.py`.
- [ ] Tests:
  - an auto-Meet invite plus a Jitsi link in the description sends the Jitsi link;
  - a Meet-only invite sends Meet;
  - every eligibility reason.
- [ ] Commit `feat(calendar-dispatcher): 14-day read, series id, one link per invite (Part 3)`.

### B4 — Why an invite left, including `not_eligible`

- [ ] Add `vanish_reason(creds, event_id, owner_email, horizon_end)` covering the six outcomes in Part 3.
- [ ] Tests for each, including: link removed → `not_eligible` → remove sent.
- [ ] Commit `feat(calendar-dispatcher): name why an invite left (Part 3)`.

### B5 — The sync cycle

**Files:** new `calendar_dispatcher/aw_bots_sync.py`; `main.py` (every active connection; the push notification triggers `sync_user`).

- [ ] The cycle follows Part 3. `sent_hash` equals `content_hash`, with a shared test vector in both repos.
- [ ] Tests:
  - an unchanged event makes no call;
  - ended vs vanished;
  - every reason;
  - a partial read sends nothing;
  - reconciliation sends only differences (1 000 entries, 3 different → 3 calls);
  - pacing stays under 500 a minute;
  - 401 stops the cycle;
  - Teams and Jitsi events reach `put_entry`.
- [ ] Commit `feat(calendar-dispatcher): keep aw-bots up to date (Part 3)`.

### B6 — Remove the old-bot code

- [ ] Remove `job_publisher.py`, `cancel_client.py`, `liveness_client.py`, and the old paths in `main.py` (publish, cancel reconciliation, `_duplicate_bot`, room adoption, done/prune, `POST /dispatch`, `_process_pending_cancels`, the `tracked_meetings` writes), all with their tests.
- [ ] Remove `meeting_key.py` if nothing imports it (check with grep).
- [ ] Remove the unused settings in `deployment/base/calendar-dispatcher/*` (`DISPATCH_AUTH_TOKEN`, orchestrator URLs, `DISPATCH_PLATFORMS`). Keep the `tracked_meetings` table.
- [ ] Every remaining test passes; the report lists every removed test by name.
- [ ] Commit `refactor(calendar-dispatcher): remove the decommissioned old-bot path (Part 3)`.

### B7 — Manifests, alerts, CHANGELOG

- [ ] `deployment/base/aw-bots/values.yaml`:
  - the §1.11 settings;
  - `gateway.guard.ipWhitelist: "192.168.0.0/16"` (the cluster VPC `vpc-0923c7a3c64820d17`, read 2026-09-26; the live gateway has it empty today); `gateway.guard.trustedProxies: ""` (§1.11);
  - pod annotations: meeting-api 8080 and admin-api 8001 (confirm against the chart Services), path `/metrics`.
- [ ] `aw-bots-secrets.yaml.template`: the three new keys as `"<REPLACE_ME>"`.
- [ ] Exporter Deployment and template: `GATEWAY_URL`, `EXPORTER_API_KEY` from `aw-bots-key-exporter`. Remove `MEETING_API_URL` if unused.
- [ ] Calendar-dispatcher Deployment: `AW_BOTS_BASE_URL`, and `AW_BOTS_API_KEY` from `aw-bots-key-calendar-dispatcher`.
- [ ] New `deployment/base/aw-bots/alerts.yml` with the §1.13 alerts, in talke's format.
- [ ] `CHANGELOG.md`: the old path removed; `meeting_key.py`; the `cryptography==50.0.1` deviation; `ioredis`; V11.
- [ ] The YAML parses. Run `promtool check rules` if available.
- [ ] Commit `feat(deploy): aw-bots settings, keys, alerts (§1.11, §1.13)`.

**M6:** verify, then push both branches.

## 6.6 Part C — portal (`$N/portal`; Part 4)

| Task | Build | Tests | Commit |
|---|---|---|---|
| C1 | `lib/aw-bots.ts` (server-only: `putEntry`, `removeEntry`, `getMeeting(id, user)`, `listMeetings({user,…})`, `stopMeeting`); `api/dispatch/route.ts` → `PUT /v2/entries` with `join_now` | client + route | `feat(portal): instant join through aw-bots (Part 4)` |
| C2 | `meetings.ts`, `meeting-detail.ts`: every read passes the signed-in user | B opening A's id → not found; an invited colleague without a calendar sees it | `feat(portal): meetings from aw-bots, scoped to the signed-in user (Part 4)` |
| C3 | the transcript prefix only from `export.s3_path`; "processing" before that | prefix source; processing state | `feat(portal): transcripts from the export path (Part 4)` |
| C4 | progress from `GET /v2/meetings/{id}?user=` and pushes; the stop button only when a bot is live | both | `feat(portal): join progress and stop through aw-bots (Part 4)` |
| C5 | `api/webhooks/aw-bots/route.ts` (verify either header → publish → then dedupe; 503 when Redis is down); `api/meetings/stream/route.ts` (SSE, one subscriber per server, ids authorised with `user=`); `lib/redis.ts` (`ioredis`); `ui-k8s` env and Secrets by name | signatures; a failed publish isn't marked seen; a stranger's id refused; shared subscriber | `feat(portal): aw-bots webhooks and live updates (Part 4)` |
| C6 | remove the remaining `tracked_meetings` reads | removed tests listed by name | `refactor(portal): meetings come only from aw-bots (Part 4)` |

**M7:** verify, then push both branches.

## 6.7 Part D — docs, verification, report

- [ ] **D1.** Update aw-notetaker's `CLAUDE.md` (phase awareness; the old system decommissioned), `docs/phases/README.md`, the AW Bots playbook, `docs/SYSTEM_BRAIN.md` and `CHANGELOG.md`. Add runbook steps 9–18 (Part 5) to `deployment/base/aw-bots/README.md`, with commands that print no secret. Commit `docs: AW Bots intake — runbook, system brain, phases, changelog`.
- [ ] **D2.**
  - Record every gate that ran (and the ones that didn't, with why).
  - Record test counts per package, before and after, with removed tests by name.
  - Record ruff/mypy per touched upstream file, before and after.
  - Record the real-PG results.
  - Run `superpowers:requesting-code-review` on each branch and fix what it finds.
  - Write the final report: built, counts, gates, numbered deviations, next steps for the human, not verified.

**M8:** push both branches.

## 6.8 Verification commands

```bash
V=/Applications/XAMPP/xamppfiles/htdocs/mike/aw-notetaker/vexa-fork; N=/Applications/XAMPP/xamppfiles/htdocs/mike/aw-notetaker
export PYTHONDONTWRITEBYTECODE=1
for d in core/meetings/services/meeting-api core/identity/services/admin-api core/gateway/services/gateway integrations/out/aw-notetaker; do (cd $V/$d && uv run pytest -q -p no:cacheprovider | tail -1); done
docker run -d --rm --name aw-intake-pg -e POSTGRES_PASSWORD=test -p 55432:5432 postgres:17     # testing only
(cd $V/core/meetings/services/meeting-api && MEETING_API_TEST_DATABASE_URL=postgresql+asyncpg://postgres:test@localhost:55432/postgres uv run pytest -q -p no:cacheprovider | tail -1)
docker stop aw-intake-pg
for g in readme docs-version dataflow isolation isolation-py exports graph graph-py schema contract-version config-contract db-schema db-budget python node health access tracing replay telemetry licenses image-licenses runtime-parity execution-env test-isolation arch-report parity contract-conformance lite-makefile; do (cd $V && node scripts/gates.mjs $g >/dev/null 2>&1 && echo "PASS $g" || echo "FAIL $g"); done
for d in calendar-dispatcher notetaker-postgres; do (cd $N/$d && ../.venv/bin/python -m pytest -q -p no:cacheprovider | tail -1 && ../.venv/bin/black --check . && ../.venv/bin/ruff check . && ../.venv/bin/mypy .); done
cd $N/portal && npx vitest run && npx tsc --noEmit && npx next lint
```

---

# Part 7 — Live tests (after rollout, with the owner's meetings)

1. A one-off Meet.
2. The yearly test meeting, moved (§2.6.18).
3. A daily standup over 3 days.
4. A and B on one meeting: one bot, both see it.
5. Back-to-back on one Zoom link: the second bot goes the moment the first leaves.
6. Cancel while the bot is in the meeting.
7. Instant join of an already scheduled meeting.
8. Decline by one of two users.
9. Remove the link from an invite: the calendar module sends `not_eligible`, and no bot joins.

---

# Part 8 — Facts from the code (checked 2026-09-25/26)

## 8.1 aw-bots

- **Unique index today:** `uq_meeting_active_user_platform_native` on `(user_id, platform, platform_specific_id)` `WHERE status NOT IN ('completed','failed')` (`meeting_api/sessions/models.py:111-116`; `admin_api/schema/models.py:163-168`).
- **Schema:** no alembic. `ensure_schema` = `create_all` plus missing columns and indexes by name (`admin_api/schema/sync.py`). A failed unique index raises `SchemaInvariantError` (`sync.py:93-142`). Live unique indexes are built by hand `CONCURRENTLY` (MIGRATION-0002). MIGRATION-0001 to 0007 exist.
- **Statuses:** planned `idle`, `scheduled`; bot `requested, joining, awaiting_admission, needs_help, active, stopping, completed, failed` (`collector/app.py:49-55`).
- **`POST /meetings`:** 409 on any non-finished row for the link (`collector/adapters.py:1234-1244`).
- **Auto-join:**
  - it reads **every** `scheduled` row every tick (`bot_spawn/adapters.py:575-590`), and filters by time in code (`auto_join.py:78-127`, window at `:118`);
  - a live sibling stamps a 300 s retry (`auto_join.py:246-279`);
  - `auto_join_error` is free text;
  - `LIVE_STATUSES` is at `auto_join.py:133-136`.
- **Spawn:** the dedup list (`bot_spawn/adapters.py:485`) omits `needs_help`/`stopping`; `pg_advisory_xact_lock(user_id)` is at `:490`; the claim picks the newest planned row (`:533-556`).
- **Status writers:** `bot_spawn/adapters.py:196, 549, 714, 918`; `lifecycle/stop_router.py:319`; `lifecycle/reconcile.py:274, 593`; `collector/adapters.py:1145`; the callback path (`app.py:409-546`).
- **Upstream `DELETE /meetings/{id}`** (`collector/app.py:588-628`): planned → row deleted; finished → artifact deletion (its recording objects in `aw-bots` first, then transcripts; the row is kept); live → 409.
- **Webhooks today:**
  - one system URL and one per-user URL, snapshotted at spawn;
  - signed with `X-Webhook-Signature`, plus `Authorization: Bearer <secret>`;
  - retries at 60/300/1800/7200 s (`retry.py:56-58`);
  - non-429 4xx is never retried (`delivery.py:170-178`);
  - `event_id` = `sha256(connection_id|event_type|new_status)` truncated (`lifecycle/webhook.py:67-80`);
  - sealed `webhook.v1` `SignatureHeaders` pins one `sha256=<64 hex>`;
  - `exporter/signature.py:37-38` compares the whole header.
- **Redis** (the chart): `maxmemory 1gb`, `allkeys-lru` (`values.yaml:440-441`).
- **Gateway:**
  - it strips authority headers (`:408-410`), sets `x-user-id` (`:412`), checks scope any-of (`:383-384`), and refuses undeclared routes (`:372-382`);
  - its per-user token bucket is per process (`ratelimit.py`);
  - its per-address guard is 600 rpm (`edge_guard.py:48`);
  - it uses Redis already (`adapters.py:212`).
- **Scopes:** `VALID_SCOPES = {"bot","tx","browser"}` (`admin_api/token_scope.py:15`).
- **Bot callback:** it accepts any caller (`app.py:932-938`), although the bot sends `x-internal-secret` (`lifecycle-http.ts:68`).
- **Exporter:**
  - `vexa-<n>` ids (`exporter/job.py:98-100`);
  - reads meeting-api with `X-User-Id` (`vexa_client.py`);
  - its only S3 deletes are queue markers (`queue.py:107, 113`);
  - its IAM role has no `s3:DeleteObject` on `aw-chatworks-transcribe`.
- **Metrics:** no Prometheus client and no `/metrics` anywhere in aw-bots.
- **Tests:** meeting-api uses in-memory fakes plus fakeredis. The real-PG hook is `MEETING_API_TEST_DATABASE_URL` (`tests/test_single_flight.py:205-218`), which also pins the single-bigint advisory lock.

## 8.2 Meeting-link lookups today (all pick the newest row)

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

## 8.3 aw-notetaker

- **calendar-dispatcher:**
  - it reads `events().list(calendarId="primary", singleEvents=True, orderBy="startTime")` (`calendar_client.py:519-531`) with pagination 250/page, capped at 20 pages (it raises at the cap);
  - eligibility is at `calendar_client.py:294-302, 568-585`;
  - `hangoutLink` wins today (`:563`);
  - `DISPATCH_PLATFORMS` (default `meet,zoom`; the live manifest has `meet,zoom,teams`) filters events at `main.py:1865`.
- **notetaker-postgres:** the latest Alembic revision is `0010`.
- **portal:** visibility is `TENANT_FILTER` (`portal/src/lib/meetings.ts:103`). The portal has no S3 delete.

---

# Part 9 — Found while checking (separate decisions)

1. `PUT /meetings/{p}/{n}/intent` never checks the status (`collector/adapters.py:1117-1162`). Covered here for entry-managed meetings (§1.6); otherwise unchanged.
2. `PUT /user/webhook` doesn't validate the URL on save (`admin_api/app/main.py:633-649`). Unchanged.
3. `needs_human_help` appears in some status lists (`auto_join.py:135`, `collector/app.py:246`), but the state machine emits only `needs_help`. So `GET /meetings?exclude_planned=true` hides `needs_help` rows. Unchanged.

(The unchecked bot callback and the incomplete spawn dedup list, listed here in V10, are fixed by this design: §1.10 and §1.5.)
