# AW Bots meeting intake and webhooks — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development (the owner's chosen method). Each task gets a fresh implementer and then a fresh reviewer, and the whole branch gets a review at the end. Every unit is test-first (superpowers:test-driven-development). Nothing is reported done without superpowers:verification-before-completion. Steps use checkbox (`- [ ]`) syntax.

**Goal:** any client (our calendar module, our portal, a third party) sends meeting *entries* to aw-bots through a sealed `/v2` API. aw-bots owns scheduling, dedup (R1–R8), bots and results. It reports every result to any number of signed webhook subscribers.

**Architecture:**

- **Intake module.** A new `meeting_api/intake/` module sits beside the existing spawn and stop paths and calls them; it never duplicates them. It has pure rules and a narrow `IntakeStore` port with two implementations: an in-memory fake and a SQLAlchemy adapter.
- **Our state lives in typed tables:**
  - `meeting_entries`: one row per entry;
  - `meeting_aw_state`: our per-meeting state;
  - `webhook_outbox`: every event, written in the same transaction as the change it describes.
- **Webhooks.** admin-api owns the subscriptions, with secrets encrypted under a rotatable key ring. meeting-api delivers events through a Redis Stream consumer group.
- **Calendar module.** calendar-dispatcher gains a per-user `bot_backend` switch that sends entries instead of bots.
- **Portal.** It reads meetings from `/v2`, receives webhooks, and pushes them to browsers over SSE.

**Tech stack:**

- aw-bots: Python 3.11, FastAPI, SQLAlchemy 2 async + asyncpg, redis-py 5 (Streams), `cryptography==42.0.5`, `prometheus-client==0.19.0`, pytest + fakeredis, JSON Schema contracts, the pnpm gate suite.
- aw-notetaker: Python 3.11, SQLAlchemy/Alembic, httpx, google-api-python-client. The portal is Next.js 14 + vitest + `ioredis`.

**Spec:** `integrations/out/aw-notetaker/docs/2026-09-25-meeting-intake-and-webhooks-design.md`, **revision V10 (final for build)**. Every task cites the § it implements; read that § before starting the task. The design wins over this plan. If the two disagree, stop and report.

**Worktrees (created 2026-09-26):**

| Repo | Worktree | Branch | Base |
|---|---|---|---|
| aw-bots (`AbroadWorks-Inc/vexa`) | `/Applications/XAMPP/xamppfiles/htdocs/mike/vexa-meeting-intake` (`$V`) | `feat/meeting-intake` | `development` @ `5c9e500f` + V9 restore (`aafeb633`) + V10 (`936a7155`) |
| aw-notetaker | `/Applications/XAMPP/xamppfiles/htdocs/mike/aw-notetaker-calendar-aw-bots` (`$N`) | `feat/calendar-aw-bots` | `feat/aw-bots-deployment` @ `814b639` |

Path shorthands in aw-bots:

| Shorthand | Path |
|---|---|
| `MA` | `core/meetings/services/meeting-api` |
| `MAS` | `$MA/src/meeting_api` |
| `AA` | `core/identity/services/admin-api` |
| `AAS` | `$AA/src/admin_api` |
| `GW` | `core/gateway/services/gateway` |
| `EX` | `integrations/out/aw-notetaker` |

## Baseline (measured 2026-09-26, before any change)

| Package | Command | Result |
|---|---|---|
| meeting-api | `cd $V/$MA && PYTHONDONTWRITEBYTECODE=1 uv run pytest -q -p no:cacheprovider` | 1424 passed, 5 skipped |
| admin-api | same, in `$V/$AA` | 67 passed, 87 skipped |
| gateway | same, in `$V/$GW` | 367 passed, 1 xfailed |
| exporter | same, in `$V/$EX` | 197 passed, 1 deselected |
| calendar-dispatcher | `cd $N/calendar-dispatcher && ../.venv/bin/python -m pytest -q -p no:cacheprovider` | **524** passed (the floor; the design was first written at 519) |
| notetaker-postgres | same, in `$N/notetaker-postgres` | 88 passed |
| portal | `cd $N/portal && npx vitest run && npx tsc --noEmit && npx next lint` | 1759 passed / 12 skipped; tsc 0 errors; eslint clean |
| ruff findings per package | `uvx ruff@0.15.3 check src` | meeting-api 12, admin-api 0, gateway 3 |
| mypy findings per package | `uvx mypy==1.17.1 --ignore-missing-imports src` | meeting-api 132, admin-api 17, gateway 6 |
| black | `uvx black==25.1.0 --check src` | upstream is not black-formatted: 70 / 11 / 9 files would reformat |

`$N/.venv` is a fresh Python 3.11 venv with `notetaker-common`, `notetaker-postgres[dev]` and `calendar-dispatcher[dev]` installed editable from `$N`.

## Global constraints

**Secrets**

- Never read, print, decode or paste a secret value.
- Test secrets are literal dummies, such as `"test-secret"` and the key ring `{"k1": base64(32 zero bytes)}`.
- Never open a live `*-secrets.yaml`, `.env*` or `vexa-fork/.env.local`.
- No log line carries a key, a secret, a URL query string or transcript text (§8.11).
- Secret templates carry `"<REPLACE_ME>"` only.

**No new S3 deletes anywhere** (§8.7, §8.11)

- The bot and the exporter delete no meeting data.
- Nothing deletes from `aw-chatworks-transcribe`.
- The only S3 delete that `/v2` reaches is upstream's existing completed-artifact deletion of meeting-api's raw copies in the `aw-bots` bucket, reused unchanged.
- No IAM change.

**Sealed contracts**

- `lifecycle.v1` `CompletionReason` keeps its ten values; our cause is `meeting_aw_state.outcome_*` (§3). A test asserts that no code path writes an eleventh value.
- Each seal is its own commit, never folded into a code commit: `pnpm seal:schema`, `pnpm seal:contracts`, `pnpm seal:arch` (§8.9).

**Code rules**

- **One link parser:** `collector/meeting_link.py` in aw-bots. In aw-notetaker, `meeting_key.py` stays the old path's canonicaliser and is not used on the aw-bots path (§9.3).
- **Upstream surfaces stay as they are:** `/meetings`, `/bots` and `/transcripts` keep their shapes. The only exceptions are the changes §8.4 names: the room resolver, the live-only stop and `managed_by_entries`.
- **Settings:** every new setting goes in its service's `config.v1.json` (gate `config-contract`) and in the chart's `values.yaml`, with the design's default (§8.8). Never in code.
- **Dependencies:** exactly the ones in §8.8.
  - `cryptography==42.0.5` and `prometheus-client==0.19.0` in meeting-api and admin-api, in both `pyproject.toml` and the Dockerfile.
  - `ioredis` in the portal.
- **No history in code** (AGENTS.md): source states the designed present.
- **Logs** (§8.11): structured JSON through each service's `obs.log_event`. Every line about a meeting carries `meeting_uuid`, `external_id`, `user` and `account`.
- **Advisory locks:** use the single-bigint form, `pg_advisory_xact_lock(hashtextextended(:key, 0))`. That is the convention `tests/test_single_flight.py` pins; the same test bans the two-int form.

**Lint (owner-accepted)**

- Every new Python file is clean under black 25.1.0, ruff 0.15.3 and mypy 1.17.1.
- Touched upstream files are not reformatted, and their ruff and mypy findings must not rise.
- The report lists per-file finding counts before and after for every touched upstream file.

**Commits and pushes**

- Conventional Commits, one concern per commit, with the § in the body.
- Trailer: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Stage with explicit `git add <paths>` only. Never stash, reset, amend or force-push.
- Owner's push policy: push both feature branches after each green milestone (M1–M8 below). No PRs, no merges.

**Gates (owner)**

- Run every gate that doesn't need the compose env. The compose gates are `stack`, `compose`, `compose-stress`, `compose-chaos` and `eval*`.
- Record exactly which gates ran and which didn't.
- Never create `deploy/compose/.env`.

**Real-Postgres tests (owner)**

These run against a throwaway local database. It is used for testing only: it touches no cluster and no real data, and it needs Docker Desktop running. The password `test` is a dummy for this local container only.

1. Start it: `docker run -d --rm --name aw-intake-pg -e POSTGRES_PASSWORD=test -p 55432:5432 postgres:17`.
2. Point the tests at it: `MEETING_API_TEST_DATABASE_URL=postgresql+asyncpg://postgres:test@localhost:55432/postgres`.
3. Remove it afterwards: `docker stop aw-intake-pg`.

Without the URL, these tests skip.

**Out of scope for a session**

No deploy, no image build, no `kubectl apply`, and no token minting or revoking. The human runs every live step.

## Milestones (push both branches after each is green)

| M | Tasks | Green means |
|---|---|---|
| M1 | A1–A4 | schema + seal, parser, projection, contracts + seals; all four aw-bots suites ≥ baseline; `db-schema` and `contract-version` green |
| M2 | A5–A7 | intake core, store (real-PG tests green), `/v2` routes |
| M3 | A8–A12 | exact-row spawn, auto-join, resolver + `managed_by_entries`, stop, events |
| M4 | A13–A14 | subscriptions (admin-api), outbox and stream sender |
| M5 | A15–A21 | callback secret, exporter, gateway limit, metrics, Helm, arch seal, aw-bots docs; full local gate run recorded |
| M6 | B1–B6 | calendar module (≥ 524 tests, old path unchanged), deploy manifests, alerts |
| M7 | C1–C5 | portal |
| M8 | D1–D2 | aw-notetaker docs, whole-branch review, final report |

## Review focus (inputs the design implies but no §6 row exercises)

1. **Two rooms locked at once.** A link change moves an entry between rooms. The locks are taken in sorted key order, so two opposite moves never deadlock. Tested in A5 (the fake records the order) and A6 (real PG).
2. **A duplicate `PUT` racing itself.** The calendar module retries after a timeout while the first request is still running. The second request waits on the room lock and replies `unchanged`; there are never two meetings. Tested in A6 (real PG).
3. **Non-UTC and naive timestamps.** `+05:30` is normalised to UTC; a naive timestamp is `invalid_request`. Tested in A4.
4. **A meeting created through upstream routes** (`POST /bots`, `POST /meetings`) with no `meeting_aw_state` row. The projection, `sequence` and the resolver still work, and the row is created lazily under the row lock. Tested in A3, A10 and A12.
5. **A subscriber that accepts the connection and never answers.** The 10 s timeout covers the whole request. The stream item is acknowledged only after its attempt row is written. If the replica dies, `XAUTOCLAIM` reclaims the item. Tested in A14.

---

## Part A — aw-bots (`$V`, branch `feat/meeting-intake`)

### Task A1: Schema — `meetings.uuid`, five tables, live-index swap, MIGRATION-0008 (§8.1, §8.9)

**Files:**

- Modify `$AAS/schema/models.py`:
  - add `Meeting.uuid`;
  - add the classes `MeetingEntry`, `MeetingAwState`, `WebhookSubscription`, `WebhookDelivery` and `WebhookOutbox`;
  - replace `uq_meeting_active_user_platform_native` with `uq_meeting_live_user_platform_native`;
  - add `ix_meeting_status_scheduled_at`;
  - correct the stale comment at `:160-162`, which says `_sync_indexes` swallows a failure (`sync.py:93-142` in fact raises).
- Modify `$MAS/sessions/models.py`: mirror all of the above. There is no comment to fix in this copy.
- Create `$AAS/schema/MIGRATION-0008-meeting-live-dedup-index.md`.
- Tests:
  - `$AA/tests/test_schema_intake_tables.py`;
  - `$MA/tests/test_intake_models_mirror.py`;
  - `$MA/tests/test_intake_pg_schema.py` (real PG).

**Interfaces it produces.** These column names are the contract for every later task:

```python
# meetings
uuid = Column(UUID(as_uuid=True), nullable=False, unique=True, server_default=text("gen_random_uuid()"))
Index("uq_meeting_live_user_platform_native", "user_id", "platform", "platform_specific_id", unique=True,
      postgresql_where=text("status IN ('requested','joining','awaiting_admission','needs_help','active','stopping')"))
Index("ix_meeting_status_scheduled_at", "status", text("(data ->> 'scheduled_at')"))

class MeetingEntry:        # "meeting_entries"
    id BigInteger PK; user_id Integer not null; source_user Text not null; external_id String(255) not null
    meeting_id Integer FK meetings.id ON DELETE RESTRICT not null
    meeting_url Text not null; platform String(100) not null; native_meeting_id String(255) not null
    title String(512) null; start_at DateTime(tz) not null; end_at DateTime(tz) null; time_zone Text null
    series_id String(255) null; attendees ARRAY(Text) null; join_now Boolean not null default false
    metadata_ JSONB null (column "metadata"); state String(16) not null ('active'|'removed')
    removed_reason Text null; created_at/updated_at DateTime(tz) server_default now(); removed_at DateTime(tz) null
    UniqueConstraint(user_id, source_user, external_id, name="uq_meeting_entries_user_source_external")
    Index(meeting_id); Index(user_id, platform, native_meeting_id, state)
    Index(attendees, postgresql_using="gin"); Index("ix_meeting_entries_active_user", user_id, postgresql_where=text("state = 'active'"))
class MeetingAwState:      # "meeting_aw_state"; meeting_id Integer PK FK meetings.id ON DELETE CASCADE
    scheduled_end_at DateTime(tz) null; time_zone Text null; event_seq BigInteger not null server_default "0"
    outcome_kind Text null ('cancelled_by_calendar'|'not_sent'|'merged_into_live'); outcome_detail Text null; outcome_at DateTime(tz) null
    waiting_for_room_sent_at DateTime(tz) null
    export_state Text null ('handed_off'|'failed'); export_s3_path Text null; export_error Text null; export_at DateTime(tz) null
    updated_at DateTime(tz); Index(scheduled_end_at)
class WebhookSubscription: # "webhook_subscriptions"
    id UUID PK default gen_random_uuid(); user_id Integer not null index; url Text not null
    secret_enc LargeBinary not null; enc_key_id String(64) not null; secret_last4 String(4) not null
    previous_secret_enc LargeBinary null; previous_enc_key_id String(64) null; previous_secret_expires_at DateTime(tz) null
    events ARRAY(Text) not null server_default '{}'; active Boolean not null default true; description Text null
    created_at/updated_at DateTime(tz)
class WebhookOutbox:       # "webhook_outbox" (owner decision D1)
    event_id String(80) PK; meeting_id Integer FK meetings.id ON DELETE CASCADE null (null only for webhook.test)
    event_type String(64) not null; sequence BigInteger not null; payload JSONB not null
    created_at DateTime(tz) server_default now(); published_at DateTime(tz) null
    Index("ix_webhook_outbox_unpublished", created_at, postgresql_where=text("published_at IS NULL"))
class WebhookDelivery:     # "webhook_deliveries", one row per attempt
    id BigInteger PK; event_id String(80) FK webhook_outbox.event_id ON DELETE CASCADE not null
    subscription_id UUID not null; event_type String(64) not null; meeting_id UUID null; attempt Integer not null
    outcome String(16) not null ('delivered'|'retrying'|'failed'|'dead'); status_code Integer null; error Text null
    created_at DateTime(tz) server_default now(); Index(subscription_id, created_at); Index(event_id); Index(created_at)
```

- [ ] **Step 1 — write the failing tests.**
  - `test_schema_intake_tables.py`, against admin-api's metadata, asserts:
    - the five tables have exactly these columns, types, nullability and FKs (including each FK's `ondelete`);
    - the new index exists with the six-status predicate, and the old index is gone;
    - `meetings.uuid` is non-null and unique, with default `gen_random_uuid()`.
  - `test_intake_models_mirror.py` asserts that meeting-api's mirror has the same columns. If gate `test-isolation` forbids importing admin-api from meeting-api tests, compare against `schema.seal.json` instead; that is the file gate `db-schema` compares both copies with.
- [ ] **Step 2 — run and expect FAIL:** `cd $V/$AA && PYTHONDONTWRITEBYTECODE=1 uv run pytest -q tests/test_schema_intake_tables.py`.
- [ ] **Step 3 — implement both model files.** Then check two things in `sync.py`:
  - `_col_default_sql` renders `gen_random_uuid()` and the `ARRAY(Text)` default correctly;
  - `_sync_columns` adds `uuid` as `NOT NULL DEFAULT gen_random_uuid()`, which fills existing rows.

  If either is wrong, fix the renderer in `sync.py` and give the fix its own test.
- [ ] **Step 4 — real-PG tests** in `test_intake_pg_schema.py` (skipped without the env). Run `ensure_schema` on an empty database, then prove:
  - two `scheduled` rows and one `active` row on one room insert fine;
  - a second live row on that room raises `IntegrityError`;
  - on a database with the *old* index and three existing meetings, running the MIGRATION-0008 SQL in order leaves only the new index, fills `uuid` on every row, and leaves `pg_index.indisvalid` true;
  - deleting an entry-managed meeting raises (RESTRICT);
  - deleting an entry-less meeting cascades its aw_state and outbox rows.
- [ ] **Step 5 — full suites.** Both packages stay at or above the baseline.
- [ ] **Step 6 — write `MIGRATION-0008-meeting-live-dedup-index.md`** in the style of MIGRATION-0002:
  - **Why** the change is needed.
  - **Pre-check:** no room may hold two live rows. This query must return 0 rows: `SELECT user_id, platform, platform_specific_id, count(*) FROM meetings WHERE status IN (<six>) GROUP BY 1,2,3 HAVING count(*) > 1`.
  - **Step 1, before the deploy:**
    - `ALTER TABLE meetings ADD COLUMN IF NOT EXISTS uuid uuid NOT NULL DEFAULT gen_random_uuid()`;
    - `CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS` for the uuid index, using the exact name SQLAlchemy generates. Derive that name in the test; never guess it;
    - `CREATE UNIQUE INDEX CONCURRENTLY uq_meeting_live_user_platform_native …`;
    - `CREATE INDEX CONCURRENTLY ix_meeting_status_scheduled_at …`.
  - **Step 2:** the deploy.
  - **Step 3, once the deploy is healthy:** `DROP INDEX CONCURRENTLY uq_meeting_active_user_platform_native`.
  - **Verify:** `\d meetings` and `indisvalid`.
  - **Rollback:** free before step 3. After step 3, the old index can only be rebuilt while no room holds two non-finished rows, so roll back before step 3.
  - Operators type every command through `kubectl -n aw-bots exec -it <postgres pod> -- psql -U postgres -d vexa`. No password appears on any command line.
- [ ] **Step 7 — commit:** `feat(schema): meeting uuid, intake and webhook tables, live-only room index (§8.1)`.
- [ ] **Step 8 — seal:** run `cd $V && pnpm seal:schema && node scripts/gates.mjs db-schema` and expect green. Commit only `schema.seal.json`, as `chore(seal): schema seal for intake tables (§8.9, lane:schema)`.

### Task A2: Jitsi room names lower-cased at parse time (§12 O4)

**Files:** modify `$MAS/collector/meeting_link.py:152-161`. Test: `$MA/tests/test_meeting_link.py`.

- [ ] Write the failing tests:
  - `https://meet.jit.si/Standup` → room `standup`;
  - `https://meet.abroadworks.com/Team-Sync` (host in `VEXA_JITSI_HOSTS`) → `team-sync@meet.abroadworks.com`;
  - the host part stays unchanged;
  - every existing Meet, Zoom and Teams case still passes.
- [ ] Run and expect FAIL. Lower-case the room component only. Run the file and the whole suite, and expect PASS.
- [ ] Commit `fix(meeting-link): lower-case Jitsi room names (design O4)`.

### Task A3: One meeting projection (§5.4, §8.1)

**Files:** create `$MAS/intake/__init__.py`, `$MAS/intake/README.md` (needed by gate `readme`) and `$MAS/intake/projection.py`. Test: `$MA/tests/test_intake_projection.py`.

```python
def project_meeting(meeting: Mapping[str, Any], aw: Optional[Mapping[str, Any]],
                    entries: Sequence[Mapping[str, Any]], *, lead_s: int) -> dict[str, Any]:
    """The §5.4 meeting object — the ONE projection every /v2 route and every webhook uses."""
# keys exactly: id (uuid str), status, completion_reason, failure_stage, outcome ({kind,detail,at}|None),
# platform, room, meeting_url, title, start, end, time_zone, bot_joins_at, entries, export ({state,s3_path,error,at}|None), sequence
```

Field rules:

- `completion_reason` and `failure_stage` are read from `data`, where upstream writes them (`lifecycle/machine.py`, `app.py:384-404`).
- `bot_joins_at`:
  - while the meeting is `scheduled`: `scheduled_at − lead_s`;
  - once a bot was sent: the time the spawn recorded;
  - for a `join_now` meeting not yet sent: `null`.
- Times are ISO 8601 with a `Z` suffix.
- The projection never contains `user_id`, the integer id, a webhook secret or a bot token. The test for this mirrors `test_response_secret_projection.py`.

- [ ] Write the failing tests:
  - every field;
  - `aw=None`, i.e. a meeting created upstream: `outcome` is null, `sequence` is 0, and `end` comes from `end_time`;
  - each outcome kind, including `cancelled_by_calendar`/`entry_moved`;
  - the forbidden keys never appear.
- [ ] Implement, expect PASS, and commit `feat(intake): the meeting projection (§5.4)`.

### Task A4: Contracts — new sealed `intake.v1`; webhook.v1 event types and previous-signature header (§8.9, §7.3, §7.4)

**Files:**

- Create `core/meetings/contracts/intake.v1/{README.md,intake.schema.json,validate.mjs,golden/*.json}`, shaped like `webhook.v1/`.
- Modify `core/meetings/contracts/webhook.v1/webhook.schema.json`:
  - add to the `EventType` enum: `meeting.scheduled`, `meeting.updated`, `meeting.removed`, `meeting.waiting_for_room`, `meeting.not_sent`, `export.handed_off`, `export.failed`, `bot.retry` and `webhook.test`;
  - add an optional `X-Webhook-Signature-Previous` to `SignatureHeaders`, with the same pattern, `^sha256=[0-9a-f]{64}$`.
- Modify the webhook.v1 README.
- Create `$MAS/intake/validation.py`.
- Tests: `$MA/tests/test_intake_contract.py` and `$MA/tests/test_intake_validation.py`.

The `$defs` in `intake.schema.json`:

| Definition | Contents |
|---|---|
| `Entry` | §5.2: `external_id` ≤ 255, `title` ≤ 512, `attendees` ≤ 100 emails, `series_id` ≤ 255, `metadata` an object, `start`/`end` as `date-time` |
| `Remove` | §5.3 |
| `Reply` | §5.4, with the ten `result` values |
| `Meeting` | A3's keys |
| `Error` | `{error:{code,message}}`; `code` is exactly §5.5's list: `invalid_request`, `unrecognized_link`, `platform_not_enabled`, `too_far_ahead`, `already_ended`, `unauthorized`, `forbidden`, `rate_limited`, `quota_exceeded`, `entry_not_found`, `meeting_not_found`, `meeting_not_finished`, `unavailable` |

Golden files: one reply for each `result`, and one error for each `code`.

```python
@dataclass(frozen=True)
class EntryIn:
    external_id: str; user: str; meeting_url: str; start: datetime; end: Optional[datetime]
    time_zone: Optional[str]; title: Optional[str]; attendees: tuple[str, ...]; series_id: Optional[str]
    join_now: bool; metadata: Optional[dict[str, Any]]
@dataclass(frozen=True)
class RemoveIn:
    external_id: str; user: str; reason: Optional[str]
class IntakeError(Exception):
    code: str; message: str; http_status: int; retry_after_s: Optional[int]
def parse_entry(body: Any, *, now: datetime, max_days_ahead: int) -> EntryIn
def parse_remove(body: Any) -> RemoveIn
```

Validation runs in this order:

1. JSON Schema → `invalid_request`. The message names the field and never echoes `metadata` or the URL's query string.
2. A naive datetime → `invalid_request`. Otherwise, normalise to UTC.
3. `metadata` over 16 384 bytes serialized → `invalid_request`.
4. `end ≤ start` → `invalid_request`.
5. `join_now`: set `start = now` and `end = None`, ignoring any times sent.
6. Not `join_now` and `end ≤ now` → `already_ended`.
7. `start > now + max_days_ahead` → `too_far_ahead`.
8. Lower-case `user` and `attendees`.

Link parsing and `ENTRY_BLOCKED_HOSTS` belong to the service (A5), not here.

- [ ] Write the failing tests:
  - the golden files validate against the schema;
  - every error path;
  - `+05:30` is normalised to UTC;
  - a naive timestamp is refused;
  - metadata of exactly 16 384 bytes is accepted, and 16 385 bytes is refused.
- [ ] Implement, expect PASS, and commit `feat(contracts): intake.v1 contract and request validation (§8.9)`.
- [ ] Run `pnpm seal:contracts` and commit `chore(seal): seal intake.v1 (§8.9, lane:contract)`.
- [ ] Make the webhook.v1 edits. Prove that every existing webhook golden file still validates (`node core/meetings/contracts/webhook.v1/validate.mjs`). Commit `feat(contracts): webhook.v1 intake event types and previous-signature header (§7.3, §7.4)`, with the back-compatibility call (§8.9) in the body.
- [ ] Run `pnpm seal:contracts` and commit `chore(seal): webhook.v1 additions (lane:contract)`.
- [ ] `node scripts/gates.mjs contract-version` must be green after each seal.

**M1 ends here: verify, then push both branches.**

### Task A5: Intake core — rules, `IntakeStore` port, fake and service; every §6 use case (§3, §6, §8.2)

**Files:** create `$MAS/intake/{rules.py,ports.py,fakes.py,service.py,settings.py}`. Tests: `$MA/tests/test_intake_rules.py`, `$MA/tests/test_intake_use_cases.py` and `$MA/tests/test_intake_service.py`.

```python
# rules.py — pure. LIVE_STATUSES is imported from bot_spawn.auto_join, never redefined.
FINISHED = ("completed", "failed")
def overlaps(a_start: datetime, a_end: Optional[datetime], b_start: datetime, b_end: Optional[datetime]) -> bool
def meeting_window(m: MeetingView, *, now: datetime) -> tuple[datetime, Optional[datetime]]   # open-ended live → (start, now)
def match_entry(entry: EntryIn, candidates: Sequence[MeetingView], *, now: datetime, lead_s: int) -> Optional[MeetingView]
def join_now_target(candidates: Sequence[MeetingView], *, now: datetime, adopt_ahead_s: int) -> Optional[MeetingView]
def recompute(entries: Sequence[EntryView]) -> Plan   # Plan(start, end|None, title|None, time_zone|None, meeting_url)

# ports.py
@dataclass(frozen=True)
class Room: platform: str; native_meeting_id: str
class IntakeStore(Protocol):
    def room_lock(self, user_id: int, rooms: Sequence[Room]) -> AsyncContextManager["IntakeTx"]   # one txn; rooms locked in sorted order
class IntakeTx(Protocol):
    async def find_entry(self, user_id: int, source_user: str, external_id: str) -> Optional[EntryView]
    async def room_meetings(self, user_id: int, room: Room) -> list[MeetingView]            # non-finished only
    async def meeting(self, meeting_id: int) -> MeetingView
    async def count_active_entries(self, user_id: int) -> int                             # index-only (partial index)
    async def create_meeting(self, user_id: int, room: Room, plan: Plan, *, join_now: bool) -> MeetingView  # 'scheduled', data.auto_join=true, data.scheduled_at=start, aw_state row
    async def save_entry(self, user_id: int, entry: EntryIn, room: Room, meeting_id: int) -> EntryView     # upsert; 'removed' → 'active'
    async def mark_entry_removed(self, entry_id: int, reason: Optional[str]) -> None
    async def active_entries(self, meeting_id: int) -> list[EntryView]
    async def apply_plan(self, meeting_id: int, room: Room, plan: Plan) -> None
    async def move_active_entries(self, from_meeting_id: int, to_meeting_id: int) -> None
    async def finish_unsent(self, meeting_id: int, *, completion_reason: Optional[str], outcome_kind: str, outcome_detail: Optional[str]) -> None  # status 'failed'
    async def set_outcome(self, meeting_id: int, kind: str, detail: Optional[str]) -> None
    async def emit(self, meeting_id: int, event_type: str, change: Optional[Mapping[str, Any]] = None) -> str  # bump event_seq + insert webhook_outbox row; returns event_id
@dataclass(frozen=True)
class SpawnOutcome: ok: bool; detail: Optional[str]      # detail ∈ {account_limit, spawn_error, room_busy}
class SpawnPort(Protocol):
    async def spawn_exact(self, user_id: int, meeting_id: int) -> SpawnOutcome
class StopPort(Protocol):
    async def stop_meeting(self, user_id: int, meeting_id: int, *, outcome_kind: Optional[str], outcome_detail: Optional[str]) -> None
class EventPublisher(Protocol):
    async def publish(self, event_ids: Sequence[str]) -> None   # after commit (A14)

# service.py
class IntakeService:
    def __init__(self, store: IntakeStore, spawn: SpawnPort, stop: StopPort, publisher: EventPublisher,
                 settings: IntakeSettings, clock: Callable[[], datetime]) -> None
    async def put_entry(self, user_id: int, body: Any) -> dict[str, Any]
    async def remove_entry(self, user_id: int, body: Any) -> dict[str, Any]
    async def merge_into_live(self, user_id: int, due_meeting_id: int, live_meeting_id: int) -> None   # R2 exception (A9)

# settings.py
@dataclass(frozen=True)
class IntakeSettings:
    max_days_ahead: int; join_now_adopt_ahead_s: int; lead_s: int; blocked_hosts: frozenset[str]; max_active_entries: int
    @classmethod
    def from_env(cls) -> "IntakeSettings"
    # reads ENTRY_MAX_DAYS_AHEAD=30, JOIN_NOW_ADOPT_AHEAD_S=3600, AUTO_JOIN_LEAD_S, ENTRY_BLOCKED_HOSTS, INTAKE_MAX_ACTIVE_ENTRIES=100000
```

**`put_entry`** follows §8.2 steps 1–7 under `room_lock`. When an entry moves rooms, it locks both the old room and the new one.

- **Quota.** It is checked only when the write adds an active entry, i.e. a new entry or a `removed` one coming back. If `count_active_entries ≥ max_active_entries`, the reply is 429 `quota_exceeded`.
- **Events** are emitted inside the transaction and published after commit:
  - a new meeting → `meeting.scheduled`;
  - a changed meeting → `meeting.updated`;
  - a meeting left with no entries → `finish_unsent(completion_reason='stopped', outcome_kind='cancelled_by_calendar', detail=reason or 'entry_moved')` and `meeting.removed`.
- **`join_now`.** After commit, call `spawn_exact`. If the spawn fails, a second `room_lock` transaction runs `finish_unsent(outcome_kind='not_sent', detail=…)` and emits `meeting.not_sent`. The reply shows the meeting as it ended.

**`remove_entry`** replies with one of:

- `entry_not_found`;
- `already_removed`;
- `entry_removed`;
- `removed` (R8);
- `bot_stopping` (R5). After commit, it calls `stop.stop_meeting(outcome_kind='cancelled_by_calendar', detail=reason)`.

**Visibility** (stated here, tested in A7): `GET /v2/meetings?user=` matches a meeting through any of its entries, active or removed (§10.3, R8).

**`test_intake_use_cases.py`** has one test per §6 row. Each test asserts the reply's `result`, the meeting's status and outcome, its entries, the events emitted **in order**, and the spawns and stops called.

| Test | Setup → action | Expect |
|---|---|---|
| `test_6_1_one_off_created` | PUT the §6.1 body | `created`, `scheduled`, `bot_joins_at` = start − 300 s, events `[meeting.scheduled]` |
| `test_6_2_instant_join_new` | PUT the §6.2 body; spawn succeeds | `created`, `requested`, one `spawn_exact` on that row |
| `test_6_2_instant_join_unrecognized_link` | PUT `https://example.com/x` | 400 `unrecognized_link` |
| `test_6_2_instant_join_spawn_fails` | spawn fails with `account_limit` | reply shows `failed`, outcome `not_sent`/`account_limit`, events `[scheduled, not_sent]` |
| `test_6_3_adopts_scheduled_0945_for_1000` | scheduled 10:00–10:30; now 09:45; PUT `join_now` | `joined_existing`, same uuid, `spawn_exact` on it |
| `test_6_3_live_no_second_bot` | a live meeting on the room | `joined_existing`, no spawn |
| `test_6_3_paste_1020_does_not_adopt_tomorrow` | today 10:00–10:15 finished, tomorrow scheduled; now 10:20 | `created`, open-ended |
| `test_6_4_series_one_meeting_per_occurrence` | 10 occurrences on one room | 10 × `created`, 10 uuids |
| `test_6_5_occurrence_moved_same_id` | PUT, then PUT with a new start | `updated`, same uuid, new `bot_joins_at`, events `[scheduled, updated]` |
| `test_6_6_occurrence_cancelled` | remove with reason `cancelled` | `removed`; status `failed`, reason `stopped`; outcome `cancelled_by_calendar`/`cancelled` |
| `test_6_6_other_user_entry_remains` | entries A and B; remove A | `entry_removed`, still `scheduled` |
| `test_6_6_live_last_entry` | live meeting; remove its last entry | `bot_stopping`, `stop_meeting(outcome_kind='cancelled_by_calendar')` |
| `test_6_7_series_new_ids` | remove the old ids, create the new ones | n × `removed`, n × `created` |
| `test_6_7_series_same_ids` | same ids, new times | n × `updated` |
| `test_6_7_moved_out_of_window_and_back` | remove with `moved_out_of_window`, then re-PUT the same id | `created`, `previous_meeting_id` = the old uuid; the old meeting stays `failed` |
| `test_6_8_series_cancelled` | remove each occurrence | `removed` for each |
| `test_6_9_link_changed_moves_room` | PUT a new `meeting_url` (the only entry) | `updated`, new room, same uuid |
| `test_6_9_link_changed_joins_other_meeting` | the new link matches another meeting | `updated`, new uuid, `previous_meeting_id` = the old one; the old meeting ends `failed`/`stopped`/`cancelled_by_calendar`/`entry_moved`, with `meeting.removed` |
| `test_6_10_title_changed` | PUT a new title | `updated`; while live, `not_changed_live` |
| `test_6_11_declined_one_of_two` | remove B with `declined` | `entry_removed`; the bot still goes |
| `test_6_12_same_meeting_two_users` | A, then B, same room and time | B gets `joined_existing`, same uuid, entries A+B |
| `test_6_13_back_to_back_separate` | 14–15 and 15–16 on one room | two × `created`, two uuids |
| `test_6_14_two_accounts_separate` | user_id 1 and user_id 2, same room and time | two meetings |
| `test_6_15_cancel_while_live` | as in 6.6, live | `bot_stopping` (A11 proves the terminal webhook carries the outcome) |
| `test_6_16_changed_after_finish` | meeting `completed`; PUT or remove | `not_changed_finished` |
| `test_6_17_too_far_unknown_blocked` | start at now + 31 d; an unknown link; a blocked host | `too_far_ahead`, `unrecognized_link`, `platform_not_enabled` |
| `test_6_18_owner_recurring_moved` | create for 28 Sep, update to 25 Sep | `created`, then `updated`; `bot_joins_at` 2026-09-25T11:25:00Z |

**`test_intake_rules.py`** (§11.1):

- touching times don't merge;
- the `join_now` bound: a 09:45 paste adopts the 10:00 meeting, and a 10:20 paste doesn't adopt tomorrow's;
- an open-ended live meeting matches only due entries (`start ≤ now + lead`);
- on a tie, the earliest start wins.

**`test_intake_service.py`:**

- an identical re-PUT replies `unchanged` and emits no event;
- re-PUTting a `removed` entry replies `created` or `joined_existing`;
- R7: while live, the reply is `not_changed_live`, and the change is stored on the entry only;
- quota: a new entry gets `quota_exceeded` once `max_active_entries` is reached, but an update to an existing entry still passes;
- locks are taken in sorted order (checked through the fake's lock log);
- `test_completion_reason_sealed_set`: every value written is one of the ten, or null.

- [ ] Write `test_intake_rules.py` and see it FAIL. Implement `rules.py` and see it PASS. Commit `feat(intake): R1 matching rules (§3)`.
- [ ] Write the fake in the style of `collector/fakes.py`: dict tables, a lock-order log, and no `await` between a check and its write. Write the use-case and service tests and see them FAIL. Implement `service.py` and `settings.py`, see them PASS, run the full suite, and commit `feat(intake): entry service with every §6 use case (§8.2)`.

### Task A6: SQLAlchemy `IntakeStore` and a real-Postgres proof (§8.2, §11.2)

**Files:** create `$MAS/intake/adapters.py`. Test: `$MA/tests/test_intake_adapter_pg.py`.

- **`room_lock`:** one `AsyncSession` transaction. For each room, in sorted `(platform, native)` order, run `SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))`, with `k = 'aw-intake:' || user_id || ':' || platform || ':' || native`.
- **`emit`:**
  1. `INSERT … ON CONFLICT DO NOTHING` into `meeting_aw_state`, then `SELECT … FOR UPDATE`.
  2. `event_seq += 1`.
  3. `event_id = "evt_" + sha256(f"{uuid}|{event_type}|{seq}")`.
  4. Build the envelope with A3's projection.
  5. `INSERT INTO webhook_outbox (event_id, meeting_id, event_type, sequence, payload)`.
- **`count_active_entries`:** `SELECT count(*) FROM meeting_entries WHERE user_id=:u AND state='active'`, served by the partial index.
- **Real-PG tests:**
  - the §6.12 race: two concurrent PUTs produce one meeting;
  - a duplicate PUT racing itself produces one meeting, and the second reply is `unchanged`;
  - two opposite room moves run in parallel and both finish within 5 s;
  - two scheduled rows and one live row coexist on one room;
  - a forced exception after `emit` leaves neither the state change nor the outbox row.

- [ ] Write the tests. They must collect everywhere and skip without the env. Implement, run against the throwaway container (see Global constraints), and commit `feat(intake): Postgres store with room advisory locks and the outbox (§8.2)`.

### Task A7: `/v2` routes, `DELETE` and the route table (§5, §8.11)

**Files:**

- Create `$MAS/intake/router.py` and `$MAS/intake/reads.py`. The reads:
  - filter on `user`, `from`, `to`, `status` and `external_id`;
  - use a cursor that is base64 of `(COALESCE(data.scheduled_at, start_time), id)`;
  - take `limit` with a default of 50 and a maximum of 200;
  - apply visibility `source_user = :u OR :u = ANY(attendees)` over entries in any state.
- Modify `$MAS/collector/app.py`. Extract upstream's completed-artifact deletion (the terminal branch of `_apply_meeting_delete`, `:588-628`) into `delete_completed_artifacts(store, deleter, user_id, meeting_id) -> dict`. Both the upstream route and the new route call it; the logic is never duplicated.
- Modify `$MAS/app.py` to mount the router.
- Modify `core/meetings/routes.v1.json`.
- Tests: `$MA/tests/test_intake_routes.py` and `$MA/tests/test_intake_delete.py`.

| Method + path | Scope |
|---|---|
| `PUT /v2/entries` | `bot` |
| `POST /v2/entries/remove` | `bot` |
| `GET /v2/meetings` | `tx` |
| `GET /v2/meetings/{meeting_id}` | `tx` |
| `POST /v2/meetings/{meeting_id}/stop` | `bot` |
| `DELETE /v2/meetings/{meeting_id}` | `tx` |

Error handling:

- An `IntakeError` becomes `{error:{code,message}}` with its status, plus `Retry-After` when set.
- On these routes only, a request-validation failure becomes 400 `invalid_request`. The handler is route-scoped, so upstream routes keep their 422.
- The database or Redis being down → 503 `unavailable`.
- Another account's uuid → 404 `meeting_not_found`. Never 403, so the reply doesn't reveal that the meeting exists.

`DELETE`:

- a scheduled or live meeting → 409 `meeting_not_finished`;
- a finished meeting:
  1. run `delete_completed_artifacts(...)`;
  2. in one transaction, delete that meeting's `webhook_deliveries`, `webhook_outbox` and `meeting_entries` rows. The meeting row and `meeting_aw_state` stay;
  3. send no event;
  4. reply 200 with `{meeting: <projection>, deleted: {objects, entries, outbox, deliveries}}`.

- [ ] Write the route tests against the fake store:
  - every §5.5 code the route layer produces;
  - response bodies validate against `intake.v1`;
  - `x-user-id` is required;
  - `user=` visibility, including an attendee and a removed entry;
  - cursor paging stays stable across inserts;
  - `DELETE`:
    - 409 on a scheduled meeting and on a live one;
    - on a finished meeting, it removes exactly our three row kinds and calls the extracted upstream function once;
    - a storage failure aborts before any row is removed (upstream's ordering);
    - no call reaches the exporter or `aw-chatworks-transcribe`.
- [ ] Implement and expect PASS. The gateway's manifest tests must be green: `cd $V/$GW && uv run pytest -q`.
- [ ] Commit the extraction first, with the upstream tests unchanged and green: `refactor(meetings): completed-artifact deletion as one callable`. Then commit `feat(intake): /v2 entry and meeting routes, erasure (§5, §8.11)`.

**M2 ends here: verify, then push both branches.**

### Task A8: Spawn the exact row; full live set in dedup; `POST /bots` claim rule (§8.1, §8.3)

**Files:**

- Modify `$MAS/bot_spawn/ports.py`: `create_meeting_guarded` and `request_bot` gain `claim_meeting_id: Optional[int] = None`.
- Modify `$MAS/bot_spawn/adapters.py:459-573`, `$MAS/bot_spawn/fakes.py` and `$MAS/bot_spawn/service.py`.
- Create `$MAS/intake/spawn.py`, the `SpawnPort`. It calls `request_bot(claim_meeting_id=…)` and maps exceptions:
  - `MaxBotsExceeded` or `QuotaExceeded` → `account_limit`;
  - `DuplicateMeeting` → `room_busy`;
  - `SpawnFailed` → `spawn_error`.
- Test: `$MA/tests/test_spawn_exact_row.py`.

Behaviour:

- **Dedup list.** The list at `adapters.py:485` becomes `auto_join.LIVE_STATUSES` (§14.6).
- **With `claim_meeting_id`.** Lock that exact row (`FOR UPDATE`) and claim it if it is `idle` or `scheduled`. Otherwise raise `MeetingStopped` or `DuplicateMeeting`.
- **Without it** (upstream `POST /bots`), apply the R1 `join_now` rule. Claim the earliest `idle`/`scheduled` row on the room that meets both conditions:
  - its end is still ahead. The end is `meeting_aw_state.scheduled_end_at`, or `scheduled_at + AUTO_JOIN_GRACE_S` for entry-less rows;
  - `scheduled_at ≤ now + JOIN_NOW_ADOPT_AHEAD_S`.

  If no row qualifies, insert a new one.

- [ ] Write the tests:
  - with two scheduled rows (today 10:00, and 7 days out), auto-join claims today's by id;
  - `POST /bots` at 09:55 claims today's;
  - `POST /bots` at 10:40 inserts a new row;
  - a live `needs_help` row blocks a spawn;
  - the fake and the adapter agree (one shared, parametrised test).
- [ ] Commit `fix(bot-spawn): claim the exact planned row; dedup on the full live set (§8.3, §14.6)`.

### Task A9: Auto-join — due until end, waiting for the room, merge, not-sent sweep (R2, R6, §8.3)

**Files:**

- Modify `$MAS/bot_spawn/auto_join.py`:
  - `due_rows` takes `scheduled_end_at`;
  - the live-sibling branch;
  - the claim passes `claim_meeting_id=row["id"]`.
- Modify `$MAS/bot_spawn/adapters.py`: `list_scheduled_meetings` joins `meeting_aw_state` and returns `scheduled_end_at` and `waiting_for_room_sent_at`. It is served by `ix_meeting_status_scheduled_at`.
- Modify `$MAS/__main__.py`: add the not-sent sweep, every 30 s, under `run_single_flight("intake-not-sent", …)`.
- Create `$MAS/intake/sweeps.py`.
- Tests: `$MA/tests/test_auto_join_intake.py`; extend `test_main_auto_join_wiring.py`.

Behaviour:

- **Due window.** A row that has entries is due in `[scheduled_at − lead, scheduled_end_at]`. Entry-less rows keep `[start − lead, start + grace]`.
- **Live sibling is an open-ended `join_now` meeting.** Call `IntakeService.merge_into_live`, which:
  1. moves the entries;
  2. copies the title over if the live meeting has none;
  3. runs `finish_unsent(outcome_kind='merged_into_live', detail=<live uuid>)`;
  4. emits `meeting.removed` with `data.merged_into`, and `meeting.updated` on the live meeting.
- **Any other live sibling.** On the first occurrence only, set `waiting_for_room_sent_at` and emit `meeting.waiting_for_room`.
- **Not-sent sweep.** A `scheduled` row with `scheduled_end_at < now` gets `finish_unsent(completion_reason=None, outcome_kind='not_sent', detail=…)` and emits `meeting.not_sent`. The detail comes from `data.auto_join_error` (`account_limit`, `spawn_error` or `room_busy`), else `ended_before_sent`.
- **Metrics.** The sweep refreshes `aw_meetings_by_status` (non-terminal statuses only) and `aw_sweep_last_run_timestamp_seconds{sweep}` (A18).

- [ ] Write the tests (§11.1):
  - a late entry (start past, end ahead) is due at once;
  - `waiting_for_room` is sent once across three ticks, and the meeting is released when the sibling finishes;
  - the merge into an open-ended meeting;
  - each `not_sent` detail;
  - entry-less rows behave as before: the existing `test_auto_join_*` stay green;
  - two "replicas" sharing the fake single-flight lock run the sweep once.
- [ ] Commit `feat(auto-join): intake due window, waiting for room, not-sent sweep (§8.3)`.

### Task A10: Room resolver and entry-managed meetings (§8.4)

**Files:**

- Create `$MAS/intake/resolver.py`.
- Modify every §13.2 site:
  - in `collector/adapters.py`: `get_transcript`, `authorize_subscribe`, participants, workspace, share, docs, `set_intent`;
  - in `collector/app.py`: `_resolve_owned_native`, and the row-id `PATCH`/`DELETE` (for `managed_by_entries`);
  - in `bot_spawn/adapters.py`: `find_latest`;
  - `lifecycle/stop_router.py:152-158`.
- Tests: `$MA/tests/test_room_resolver.py` and `$MA/tests/test_managed_by_entries.py`, plus the existing suites.

```python
class AmbiguousRoom(Exception): ...        # → 409 {"detail": "several scheduled meetings on this room; use the meeting id", "code": "ambiguous_room"}
class ManagedByEntries(Exception): ...     # → 409 {"detail": "this meeting is managed through /v2/entries", "code": "managed_by_entries"}
def pick_for_read(rows: Sequence[Mapping[str, Any]], *, now: datetime) -> Optional[Mapping[str, Any]]   # live, else most recent started; never future scheduled
def pick_for_planned_edit(rows: Sequence[Mapping[str, Any]]) -> Optional[Mapping[str, Any]]           # live, else the single scheduled; >1 → AmbiguousRoom
def pick_for_stop(rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]                        # live only
```

- Upstream routes keep their `{"detail": …}` error shape.
- On a meeting that has entries, these routes refuse with `managed_by_entries`:
  - `PATCH`/`DELETE /meetings/{id}`;
  - their native-keyed forms;
  - `PUT …/intent`.

  `annotate`, reads and `share` stay open.

- [ ] Write the tests for each route kind, using two room setups: (a) 1 live + 2 future scheduled; (b) 0 live + 1 past completed + 2 future scheduled.
  - Reads return the live meeting in (a) and the completed one in (b).
  - A planned edit with 2 scheduled rows → 409 `ambiguous_room`.
  - `DELETE /bots/{p}/{n}` stops only the live meeting and leaves both scheduled rows. Update the `test_stop_route.py` expectations that encoded "cancel every planned row", citing §8.4 in the test.
  - `managed_by_entries`: upstream `DELETE /meetings/{id}` on an entry-managed scheduled row → 409; on an entry-less row, the route still deletes it.
- [ ] Commit `feat(meetings): one room resolver; stop never cancels plans; entry-managed rows (§8.4)`.

### Task A11: Stop exactly one meeting (§8.5, R5)

**Files:**

- Modify `$MAS/lifecycle/stop_router.py`: extract `stop_meeting_row(repo, publisher, runtime, row) -> dict` from the existing handler. Both the old route and the new port use it.
- Create `$MAS/intake/stop.py`, the `StopPort`. It writes the outcome to `meeting_aw_state` **at the moment the stop is requested**, then calls `stop_meeting_row`.
- Test: `$MA/tests/test_intake_stop.py`.

Behaviour by status:

| Status | Action |
|---|---|
| `requested`, `joining`, `awaiting_admission` | leave command, then workload delete |
| `active` | leave command only |
| `scheduled` | terminalised like upstream's `_cancel_planned` (`failed` / `stopped`, no outcome); reply 200 |
| finished | 200, no-op, with the meeting |

- A user's stop keeps the reason `stopped` (`machine.py:56-76`).
- The R5 path also sets `cancelled_by_calendar`, and the terminal webhook carries it. This is asserted through the A12 envelope builder.

- [ ] Commit `feat(intake): stop exactly one meeting (§8.5)`.

### Task A12: Events — `sequence`, stable ids, enriched lifecycle events, an outbox row for every event (§7.3)

**Files:**

- Modify `$MAS/lifecycle/webhook.py`:
  - `derive_event_id(meeting_uuid, event_type, sequence) -> str` returns `evt_` + sha256(uuid|type|seq);
  - envelopes carry A3's projection in `data.meeting`, and `data.change`.
- Modify `$MAS/app.py:384-404`: the projection gains `uuid`, `entries`, `outcome` and `sequence`.
- Modify the lifecycle callback's status write: the status change, the `event_seq` bump and the outbox row share one transaction.
- Regenerate `core/meetings/contracts/webhook.v1/golden/*` **from the real builders, never by hand**. A test fails whenever the golden files drift from the builders.
- Tests: `$MA/tests/test_webhook_intake_events.py`. `test_contract_conformance.py` stays green.

The legacy system URL and per-user URL keep delivering. The exporter keeps receiving `meeting.completed`, which now carries `uuid` (§8.7).

- [ ] Write the tests:
  - `sequence` rises by exactly 1 per event per meeting, across both intake and lifecycle events;
  - `event_id` stays the same across a retry and differs between events;
  - `status_change` carries `from`, `to` and `reason`;
  - an upstream-created meeting's first event gets sequence 1.
- [ ] Commit `feat(webhooks): per-meeting sequence, stable ids, outbox for every event (§7.3)`.

**M3 ends here: verify, then push both branches.**

### Task A13: admin-api — subscriptions, key ring, internal read, retention (§7.2, §8.11)

**Files:**

- Create `$AAS/app/webhook_subscriptions.py` (the router), `$AAS/app/secret_box.py`, `$AAS/app/url_guard.py` and `$AAS/app/retention.py`.
- Modify:
  - `$AAS/app/main.py`: mount the router, and start the daily prune under a Postgres single-flight lock;
  - `core/identity/routes.v1.json`: the `/v2/webhooks…` rows, scope `bot`;
  - `$AAS/config.v1.json`;
  - `$AA/pyproject.toml` and `$AA/Dockerfile`: add `cryptography==42.0.5`.
- Tests: `$AA/tests/test_webhook_subscriptions.py`, `$AA/tests/test_secret_box.py` and `$AA/tests/test_webhook_retention.py`.

```python
class SecretBox:
    def __init__(self, keys: Mapping[str, bytes], active_key_id: str) -> None   # WEBHOOK_SECRET_ENC_KEYS (JSON id→base64 32 B), WEBHOOK_SECRET_ENC_ACTIVE_KEY; fail closed: missing active id or key length ≠ 32
    def encrypt(self, plaintext: str) -> tuple[bytes, str]                      # nonce(12)‖ciphertext‖tag, key id; AAD b"aw-webhook-secret"
    def decrypt(self, blob: bytes, key_id: str) -> str
    def needs_rewrap(self, key_id: str) -> bool
```

**Routes (§7.2):**

- **`POST /v2/webhooks`**
  - At most `WEBHOOK_MAX_SUBSCRIPTIONS` (20) per account; beyond that, 429 `quota_exceeded`.
  - `events` must be a subset of the webhook.v1 `EventType` values.
  - The URL is checked on save.
  - The caller may supply the secret. Otherwise aw-bots generates `secrets.token_urlsafe(32)` and returns it once.
- **`GET`** returns `secret_last4` only, never the secret.
- **`PATCH` and `DELETE`.**
- **`POST …/rotate-secret`** keeps the previous secret valid for 24 h.
- **`POST …/test`** calls meeting-api's `POST /internal/webhooks/test` with `INTERNAL_API_SECRET`. admin-api never writes meeting-api's outbox itself.
- **`GET …/deliveries?limit=&before=`.**
- **Internal `GET /internal/users/{id}/webhook-subscriptions`**
  - Checks `X-Internal-Secret` in constant time.
  - Returns active subscriptions with **ciphertext and key ids only**.
  - Re-wraps any row that `needs_rewrap`, in the same transaction.

**URL guard.** admin-api may not import meeting-api (gate `isolation-py`), so `url_guard.py` implements the same rules as meeting-api's `webhooks/ssrf.py`: resolve the host, then reject private, loopback, link-local and metadata ranges unless the host is in `WEBHOOK_PRIVATE_HOST_ALLOWLIST`. There is no shared home for a common vector file (`core/identity/contracts/…` doesn't fit). So each service's tests carry the **same parametrised test vectors**. Upstream already uses this self-contained-per-service pattern for `obs.py`, and the PR says so.

**Retention**, run daily:

1. Delete `webhook_deliveries` rows older than `WEBHOOK_DELIVERY_RETENTION_DAYS`.
2. Then delete **published** `webhook_outbox` rows older than that which have no deliveries left.

Unpublished outbox rows are never pruned.

- [ ] Write the tests (§11.1):
  - a row written under key A is still readable after key B becomes active, and is re-encrypted under B on the internal read;
  - a wrong key id raises an error and never returns plaintext;
  - no response and no log line contains a secret (checked against captured logs);
  - the 21st subscription → 429 `quota_exceeded`;
  - a private URL is refused unless allow-listed;
  - a rotation keeps the previous secret for 24 h;
  - delivery-log paging;
  - retention deletes in the order above and never touches unpublished rows.
- [ ] Commit `feat(admin-api): webhook subscriptions with key-ring-encrypted secrets (§7.2)`, then `feat(admin-api): delivery and outbox retention sweep (§8.11)`.

### Task A14: meeting-api — publisher, stream sender, signing, repair sweep (§7.4, §7.5, §8.6)

**Files:**

- Create:
  - `$MAS/webhooks/subscriptions.py`: admin-api's internal read, cached for 30 s;
  - `$MAS/webhooks/secret_box.py`: the same algorithm, env and test vectors as A13;
  - `$MAS/webhooks/signing.py` and `$MAS/webhooks/stream_sender.py`;
  - `$MAS/intake/outbox.py`: the `EventPublisher` and the repair sweep.
- Modify:
  - `$MAS/__main__.py`: one consumer per replica, and the repair sweep every 60 s under `run_single_flight("webhook-outbox", …)`;
  - `$MAS/webhooks/ssrf.py`: an allow-list parameter;
  - `$MAS/app.py`: `POST /internal/webhooks/test`;
  - `$MAS/config.v1.json`;
  - `$MA/pyproject.toml` and `$MA/Dockerfile`: add `cryptography==42.0.5`.
- Tests: `$MA/tests/test_stream_sender.py`, `test_webhook_signing.py` and `test_outbox_publish.py`.

**Publishing one outbox row:**

1. Load the account's subscriptions; the account comes from the meeting.
2. Keep the ones whose `events` list matches; an empty list means all events.
3. `XADD aw:webhook-deliveries` one item per subscriber, with `event_id`, `subscription_id`, `attempt=1` and `not_before=0`.
4. `UPDATE webhook_outbox SET published_at=now()`.

**The worker loop** (group `aw-senders`, consumer name = pod name):

1. `XREADGROUP COUNT 10 BLOCK 5000`.
2. For an item whose `not_before` is in the future, re-`XADD` it and `XACK` the original; there is no busy wait.
3. Otherwise:
   1. load the payload from `webhook_outbox`, and the subscription from the cache;
   2. re-check the URL;
   3. sign **the stored payload bytes**;
   4. POST with a 10 s total timeout;
   5. insert the `webhook_deliveries` row;
   6. `XACK`.
4. Run `XAUTOCLAIM min-idle 60000` on every turn.

**Retries:**

- 5xx, 429, a timeout or a connection error: retry after 60, 300, 1800 and then 7200 s (the constants in `webhooks/retry.py`). After the last retry, the delivery is `dead`.
- Any other 4xx: `failed`, with no retry.

**Signing:**

- `X-Webhook-Timestamp`;
- `X-Webhook-Signature: sha256=<hex>`;
- `X-Webhook-Signature-Previous: sha256=<hex>`, only while `previous_secret_expires_at > now`;
- `Content-Type: application/json`;
- **no `Authorization` header.**

**Repair sweep:** publish rows where `published_at IS NULL AND created_at < now() - 60 s`, oldest first, up to 500 per turn.

- [ ] Write the tests (§11.1):
  - two consumers on one fakeredis stream deliver each item exactly once;
  - an unacknowledged item is reclaimed after 60 s (fake clock);
  - the sweep publishes old unpublished rows and leaves young ones;
  - a retry posts a byte-identical payload;
  - the retry schedule ends in `dead`;
  - a 400 is `failed` with no retry;
  - the signature verifies against a test-local copy of the exporter's algorithm (copied, not imported, because of gate `test-isolation`);
  - the previous header appears during a rotation, then disappears;
  - `portal.notetaker.svc.cluster.local` is allowed and `10.0.0.1` is blocked;
  - no secret appears in logs;
  - one delivery row per attempt;
  - `webhook.test` reaches its one subscriber and is never replayed by the sweep.
- [ ] Commit `feat(webhooks): signed subscription deliveries over a Redis Stream (§8.6)`, then `feat(intake): outbox publisher and repair sweep (§8.6)`.

**M4 ends here: verify, then push both branches.**

### Task A15: Enforce the bot's lifecycle callback secret (§8.10, §14.1)

**Files:** modify `$MAS/app.py:932-938`. Test: `$MA/tests/test_callback_secret.py`. Update any test fixtures that call the callback without the header.

- **Check first.**
  - The bot must send `x-internal-secret` on every callback path: `core/meetings/services/bot/src/adapters/lifecycle-http.ts:68` and any other sender in the bot.
  - meeting-api must hand `internal_secret` to the bot at spawn (`request_bot(internal_secret=…)`).
  - If either is missing, **stop and report**. Enforcing the check would break live bots, so the producer is fixed first.
- **Behaviour.**
  - The header is compared with `hmac.compare_digest`. Missing or wrong → 401.
  - An unset `INTERNAL_API_SECRET` → 401 on every call (fail closed), and `config_preflight` reports it.

- [ ] Commit `fix(meeting-api): require the internal secret on bot lifecycle callbacks (§8.10)`, as a commit of its own.

### Task A16: Exporter — UUID everywhere, its own key through the gateway, export result (§8.7, §8.10)

**Files:**

- Modify:
  - `$EX/exporter/job.py:98-100`: ids become `meeting.uuid`. `_export.json` keeps `vexa_meeting_id` as well;
  - `$EX/exporter/notetaker.py:37-42`;
  - the id fields that `attribution.py` and `schemas.py` write into `speaker_timeline.json` and `participants.json`;
  - `$EX/exporter/vexa_client.py`: reads go through `GATEWAY_URL` with `X-API-Key` from `EXPORTER_API_KEY`. No more `X-User-Id`;
  - `$EX/exporter/config.py`.
- Create `$EX/exporter/export_result.py`: `POST {MEETING_API_URL}/internal/meetings/{uuid}/export` with `Authorization: Bearer <INTERNAL_API_SECRET>`.
- On the meeting-api side, add the route `POST /internal/meetings/{meeting_uuid}/export`. It checks the Bearer token the way `recordings/router.py:241-252` does, writes `meeting_aw_state.export_*`, emits `export.handed_off` or `export.failed`, and counts `aw_export_total{state}`.
- Tests: `$EX/tests/test_job.py`, `$EX/tests/test_export_result.py` and `$MA/tests/test_export_result_route.py`.

Rules:

- A webhook without `uuid` fails the job loudly. There is no fallback to `vexa-<n>`.
- **The exporter gains no delete.** A test asserts that the storage adapter's `delete` is called only from the two queue-marker sites (`queue.py:107, 113`).
- The exporter's own strict black, ruff and mypy stay green.

- [ ] Commit `feat(exporter): meeting UUID everywhere, own key, report export result (§8.7)`, then `feat(meeting-api): export result route and export events (§8.7)`.

### Task A17: Gateway — per-account intake write limit in Redis; `/v2` error shape (§8.11, §5.5)

**Files:**

- Create `$GW/src/gateway/intake_limit.py`: a fixed 60 s window per account in Redis. The key is `aw:intake-rate:<user_id>:<window>`, updated with `INCR` + `EXPIRE` in one `MULTI`. The limit is `INTAKE_RATE_LIMIT_PER_MIN`, 600 by default.
- Modify `$GW/src/gateway/app.py`:
  - apply the limit to `PUT /v2/entries` and `POST /v2/entries/remove` only;
  - over the limit: 429 `{"error":{"code":"rate_limited","message":…}}`, with `Retry-After` set to the seconds left in the window;
  - `/v2` paths return 401 and 403 in the §5.5 shape;
  - if Redis is unavailable: 503 `unavailable` (fail closed).
- Modify `$GW/src/gateway/config.v1.json`.
- Test: `$GW/tests/test_intake_rate_limit.py`, using fakeredis with two app instances sharing one Redis.

- [ ] Write the tests:
  - 600 writes pass, and the 601st gets 429 with `Retry-After`;
  - **the count is shared across the two gateway instances**;
  - the limit is per account;
  - other routes are unaffected, and upstream routes keep `{"detail":…}`.
- [ ] Commit `feat(gateway): per-account intake write limit shared through Redis (§8.11)`.

### Task A18: Metrics (§8.11)

**Files:**

- Create `$MAS/metrics.py` and `$AAS/app/metrics.py`.
- Modify `$MAS/app.py` and `$AAS/app/main.py`: add `GET /metrics`, which is in no gateway route table.
- Instrument these sites: the intake routes, the sweeps, the spawn lag, the sender, the stream-pending gauge, the export route, and token expiry in admin-api.
- Add `prometheus-client==0.19.0` to both `pyproject.toml` files and both Dockerfiles.
- Tests: `$MA/tests/test_metrics.py` and `$AA/tests/test_metrics.py`.

The metric names are exactly §8.11's, plus `aw_sweep_last_run_timestamp_seconds{sweep}`. Each is labelled `user_id` where §8.11 says so. `aw_api_token_expires_seconds{name}` comes from `api_tokens.expires_at`.

- [ ] Commit `feat(meeting-api,admin-api): Prometheus metrics for intake and webhooks (§8.11)`.

### Task A19: Settings and Helm (§8.8, §8.11)

**Files:**

- Modify the three `config.v1.json` files to add every §8.8 setting.
- Modify `deploy/helm/charts/vexa/values.yaml` and its templates:
  - wire the new env vars;
  - read `WEBHOOK_SECRET_ENC_KEYS` and `WEBHOOK_SECRET_ENC_ACTIVE_KEY` from `secrets.existingSecretName`;
  - **add per-service `meetingApi.podAnnotations` and `adminApi.podAnnotations`**. They merge over `global.podAnnotations` and are empty by default, so the default render doesn't change.
- Modify `deploy/helm/tests/test_template.sh` to add render assertions. This is a hot file, so sequence it (AGENTS.md).
- Modify `deploy/db-budget.json` only if a new database engine appears; none is planned.

- [ ] Run `node scripts/gates.mjs config-contract` and `bash deploy/helm/tests/test_template.sh`; both must be green. Commit `feat(helm): intake and webhook settings, per-service pod annotations (§8.8, §8.11)`.

### Task A20: Architecture model (§8.9, P23)

**Files:**

- Modify `architecture.calm.json`: add the node `meeting-api-intake` and the flows §8.9 lists.
- Regenerate with `pnpm arch:dsl`.
- Run `pnpm seal:arch` in its own commit.
- Run `pnpm gate:calm`, which needs network access for `npx`. If it can't run, the report says so.

- [ ] Commit `docs(arch): intake module and flows in the CALM model (§8.9)`, then `chore(seal): architecture seal (P23)`.

### Task A21: aw-bots docs

**Files:**

- Create `docs/changelog.d/aw-meeting-intake.md` (a changelog fragment).
- Modify `$EX/README.md`: add the env names `GATEWAY_URL` and `EXPORTER_API_KEY`.
- Modify `README.md` (the fork's README): `/v2`, webhooks and keys.
- Revise the design only if a task found something new, and then add a V11 line to its revision log.

- [ ] Commit `docs(aw-bots): intake and webhooks — changelog fragment and READMEs`. Run the full local gate set and record every gate's output line.

**M5 ends here: verify, then push both branches.**

---

## Part B — calendar module (`$N`, branch `feat/calendar-aw-bots`) (§9)

**Old-path rule:** every existing test passes unchanged, so the count stays ≥ 524. For an `old` user, the cycle calls the same helpers in the same order; a spy test asserts that the call sequences are identical.

### Task B1: Migration 0011 — `bot_backend` and `aw_entries` (§9.1, §9.3)

**Files:**

- Create `notetaker-postgres/notetaker_postgres/migrations/versions/0011_add_bot_backend_and_aw_entries.py`.
- Modify `notetaker-postgres/notetaker_postgres/models.py`:
  - `CalendarConnection.bot_backend`: String(16), not null, server default `'old'`, CHECK in (`old`, `aw-bots`);
  - `AwEntry`, with primary key (`owner_email`, `event_id`) and these columns:

    | Column | Type |
    |---|---|
    | `meeting_uuid` | UUID, null |
    | `sent_hash` | Text, null |
    | `state` | String(16), not null (`synced` / `rejected` / `error`) |
    | `last_error` | Text, null |
    | `attempts` | Integer, not null, default 0 |
    | `next_attempt_at` | timestamptz, null |
    | `last_seen_at` | timestamptz, not null |
    | `end_at` | timestamptz, not null |
    | `updated_at` | timestamptz |

- Tests: `tests/test_migration.py` (0010 → 0011: upgrade, downgrade, re-upgrade) and `tests/test_models.py`.

- [ ] Commit `feat(notetaker-postgres): migration 0011 bot_backend and aw_entries (design §9.1)`.

### Task B2: The aw-bots client (§9.3, §5)

**Files:** create `calendar-dispatcher/calendar_dispatcher/aw_bots_client.py`. Test: `tests/test_aw_bots_client.py`.

```python
class AwBotsResult(NamedTuple):
    kind: Literal["ok", "rejected", "retry"]; result: str | None; meeting_uuid: str | None; code: str | None
    retry_after_s: int | None
class AwBotsClient:
    def __init__(self, base_url: str, api_key: str, *, timeout_s: float = 10.0,
                 transport: httpx.BaseTransport | None = None) -> None
    def put_entry(self, body: Mapping[str, Any]) -> AwBotsResult
    def remove_entry(self, body: Mapping[str, Any]) -> AwBotsResult
```

How responses map to a result:

| Response | Result |
|---|---|
| 200 | `ok` |
| 400 or 404 | `rejected`, with the error code |
| 429 `rate_limited`, 5xx, a timeout or a connection error | `retry`, honouring `Retry-After` |
| 429 `quota_exceeded` | `rejected` |

- Env: `AW_BOTS_BASE_URL`; `AW_BOTS_API_KEY`, from Secret `aw-bots-key-calendar-dispatcher`, key `VEXA_API_KEY`.
- The key is never logged and never appears in a `repr` or an exception message. Tests check `caplog` and `repr`.

- [ ] Commit `feat(calendar-dispatcher): aw-bots intake client (§9.3)`.

### Task B3: Reading for aw-bots users — 14 days, `series_id`, Jitsi (§9.2)

**Files:** modify `calendar-dispatcher/calendar_dispatcher/calendar_client.py`. Test: `tests/test_calendar_client.py`.

- A `horizon: timedelta` parameter. Only the aw-bots path passes 14 days.
- `CalendarEvent.series_id`, from `recurringEventId`.
- A `jitsi_hosts: tuple[str, ...] = ()` parameter, which scans `conferenceData.entryPoints[].uri`, `location` and `description`. It is empty by default, so the old path is unchanged.
- Env: `AW_BOTS_HORIZON_DAYS=14` and `AW_BOTS_JITSI_HOSTS=meet.abroadworks.com,meet.jit.si`.

- [ ] Commit `feat(calendar-dispatcher): 14-day read, series id and Jitsi links for aw-bots users (§9.2)`.

### Task B4: Name the vanish with `events().get` (§9.3)

**Files:** modify `calendar_client.py` to add `confirm_vanished(creds, event_id, owner_email, horizon_end) -> Literal["cancelled","declined","deleted","moved_out_of_window","present"]`.

Tests cover each of the four reasons, plus `present` (a partial read), which sends nothing.

- [ ] Commit `feat(calendar-dispatcher): name the reason an event left the calendar (§9.3)`.

### Task B5: The aw-bots cycle and the per-user switch (§9.1, §9.2, §9.3)

**Files:**

- Create `calendar-dispatcher/calendar_dispatcher/aw_bots_sync.py`.
- Modify `main.py`:
  - `run_poll_cycle` branches on `connection.bot_backend` **before** the `DISPATCH_PLATFORMS` filter at `:1865`, so that filter keeps governing the old path, unchanged;
  - for aw-bots users, the Google push handler triggers an immediate `sync_user` and never calls `_apply_webhook_cancellations`.
- Modify `connections.py` to return `bot_backend`.
- Tests: `tests/test_aw_bots_sync.py` and `tests/test_main.py`.

**The entry body.** `entry_body(event, owner_email)` produces the §6.1 shape:

- `external_id = "google:" + event_id`;
- `series_id = "google:" + recurringEventId`;
- `attendees` = the same list `tracked_meetings.attendee_emails` gets today.

`sent_hash` is the sha256 of the body's canonical JSON.

**Each cycle, for each aw-bots user:**

- **Event present, and it needs sending** → `put_entry`. It needs sending when:
  - it's new;
  - its hash changed;
  - its row is in `error` and past `next_attempt_at`;
  - or a full resync is due (every `AW_BOTS_FULL_RESYNC_HOURS`, default 6).
- **Event absent and its end has passed** → delete the row; send nothing.
- **Event absent, end still ahead, unseen for 5 min:**
  1. `confirm_vanished`;
  2. `remove_entry(reason)`, skipped if the result is `present`;
  3. delete the row.
- **After a `rejected` reply**, the entry is retried only when the event changes, or at the next resync.
- **After a `retry` reply**, back off from 60 s, doubling up to 1 h (`_backoff_after_failure`).

The cycle writes no `tracked_meetings` row for aw-bots users. It adds the metrics `aw_calendar_read_total{outcome}`, `aw_calendar_entries_sent_total{result}` and `aw_calendar_cycle_seconds`.

- [ ] Write the tests (§11.4):
  - an unchanged event makes no call;
  - ended vs vanished;
  - each of the four reasons;
  - a partial read sends nothing;
  - rejected vs retried;
  - a full resync re-sends everything;
  - for an aw-bots user, a Teams and a Jitsi event both reach `put_entry`, while for an `old` user the Jitsi event stays filtered;
  - the old-path spy shows identical calls.
- [ ] Commit `feat(calendar-dispatcher): send entries to aw-bots per user (§9.3)`, then `feat(calendar-dispatcher): per-user bot_backend switch (§9.1)`.

### Task B6: Manifests, alerts, scrape annotations (§8.8, §8.10, §8.11, §9.3)

**Files — modify:**

- **`deployment/base/aw-bots/values.yaml`:**
  - the §8.8 settings;
  - `VEXA_JITSI_HOSTS` gains `meet.abroadworks.com`;
  - `ENTRY_BLOCKED_HOSTS=meet.abroadworks.com`;
  - `WEBHOOK_PRIVATE_HOST_ALLOWLIST=portal.notetaker.svc.cluster.local`;
  - `meetingApi.podAnnotations` and `adminApi.podAnnotations`, each with `prometheus.io/scrape: "true"`, the port (meeting-api 8080, admin-api 8001; confirm against the chart's Services) and `prometheus.io/path: /metrics`.
- **`deployment/base/aw-bots/aw-bots-secrets.yaml.template`:** `WEBHOOK_SECRET_ENC_KEYS` and `WEBHOOK_SECRET_ENC_ACTIVE_KEY`, both as `"<REPLACE_ME>"`.
- **`deployment/base/aw-exporter/deployment.yaml` and its secret template:**
  - `GATEWAY_URL`;
  - `EXPORTER_API_KEY`, from `aw-bots-key-exporter`;
  - `INTERNAL_API_SECRET`, by `secretKeyRef` to `aw-bots-secrets`.
- **The calendar-dispatcher Deployment:**
  - `AW_BOTS_BASE_URL=http://aw-bots-vexa-gateway.aw-bots.svc.cluster.local:8000`;
  - `AW_BOTS_API_KEY`, from `aw-bots-key-calendar-dispatcher/VEXA_API_KEY`, with `optional: true` so an `old`-only deploy starts without it. An aw-bots user without a key logs an error and sends nothing. That is not a fallback: nothing is sent anywhere else.

**Files — create `deployment/base/aw-bots/alerts.yml`.** It holds one rule group, `aw-bots`, with §8.11's eight alerts. It uses the exact format of talke's `deployment/monitoring-stack/prometheus-alerting-values.yaml`, under `serverFiles.alerting_rules.yml`. Each rule has:

- `alert`;
- `expr`, folded with `>-`;
- `for`;
- `labels.severity` (`critical` or `warning`) and `labels.service: aw-bots`;
- `annotations.summary` and `annotations.description`, with a pointer to the runbook.

**`CHANGELOG.md`** records:

- the change of responsibility for `meeting_key.py` (§9.3);
- the portal's `ioredis` dependency (§8.8);
- the V10 decisions.

- [ ] Validate with `python3 -c 'import yaml; yaml.safe_load(open(...))'`. If `promtool` is available, also run `promtool check rules` on the extracted group; if not, the report says so.
- [ ] Commit `feat(deploy): aw-bots intake settings, exporter key, dispatcher key, alerts (§8.8, §8.11)`.

**M6 ends here: verify, then push both branches.**

---

## Part C — portal (`$N/portal`) (§10.1, §10.3)

The switch is the signed-in user's `calendar_connections.bot_backend` (`lib/calendar-connections.ts`). Nothing changes for anyone else, and every existing vitest test passes unchanged.

### Task C1: aw-bots server client and instant join (§10.1, §6.2)

**Files:**

- Create `portal/src/lib/aw-bots.ts`, server-only, with `putEntry`, `removeEntry`, `getMeeting`, `listMeetings`, `stopMeeting` and `deleteMeeting`. The `X-API-Key` comes from `AW_BOTS_API_KEY` (Secret `aw-bots-key-portal`). The key is never sent to the browser and never logged.
- Modify `portal/src/app/api/dispatch/route.ts`: for aw-bots users, send `PUT /v2/entries` with `join_now: true` and `external_id = "manual:" + crypto.randomUUID()`, generated once per click.
- Tests: `aw-bots.test.ts` and `route.test.ts`.

- [ ] Commit `feat(portal): instant join through aw-bots /v2 (§10.1)`.

### Task C2: Lists and detail from `/v2` (§10.1, §10.3)

**Files:** modify `portal/src/lib/meetings.ts` and `meeting-detail.ts`. aw-bots users go through `listMeetings` and `getMeeting`, mapped onto the existing view types. `platform` comes from the meeting object.

Tests: visibility includes an attendee who never connected a calendar.

- [ ] Commit `feat(portal): meeting lists and detail from aw-bots (§10.1)`.

### Task C3: Transcript and audio from `export.s3_path` (§10.1)

**Files:** modify the callers of `portal/src/lib/s3.ts`. For aw-bots meetings, the prefix comes only from `meeting.export.s3_path`. With no export yet, the page shows "processing"; it never reads the old prefix.

- [ ] Commit `feat(portal): transcripts from the aw-bots export path (§10.1)`.

### Task C4: Join progress and stop (§10.1)

**Files:** modify `join-progress.ts` and `join-state.ts`.

- aw-bots users get join progress from `GET /v2/meetings/{id}`. The 2.5 s poll is kept only as the SSE fallback (C5).
- Stop calls `POST /v2/meetings/{id}/stop`.

- [ ] Commit `feat(portal): join progress and stop through aw-bots (§10.1)`.

### Task C5: Webhook receiver and SSE (§10.1, §7.4)

**Files — create:**

- **`portal/src/app/api/webhooks/aw-bots/route.ts`**, which, in order:
  1. verifies the signature over the raw body with `AW_BOTS_WEBHOOK_SECRET` (Secret `aw-bots-portal-webhook`), accepting a match on either `X-Webhook-Signature` or `X-Webhook-Signature-Previous`;
  2. rejects more than 300 s of clock skew;
  3. dedupes with `SET aw:evt:<event_id> 1 NX EX 86400`;
  4. runs `PUBLISH aw:meeting:<uuid> <envelope>`;
  5. answers 2xx quickly.
- **`portal/src/app/api/meetings/stream/route.ts`**, the SSE endpoint:
  - every id in `ids` is authorised through `getMeeting` visibility for the signed-in user, never trusted from the query;
  - one Redis subscription per connection, cleaned up on close.
- **`portal/src/lib/redis.ts`**, using `ioredis`.
- **A client hook `useMeetingStream(ids)`**: while disconnected it polls `GET /v2/meetings/{id}` every 30 s, and it reconnects with backoff.

**Files — modify:**

- `portal/package.json`: add `ioredis`.
- `portal/ui-k8s/*`: env `REDIS_URL=redis://notetaker-redis-master.notetaker.svc.cluster.local:6379/0` and `AW_BOTS_BASE_URL`, plus the Secrets by name.

**Tests:**

- signatures: good, bad, stale, and a rotation matching on either header;
- dedupe;
- the SSE endpoint refuses another user's meeting id;
- the fallback while disconnected.

- [ ] Commit `feat(portal): aw-bots webhook receiver and live updates over SSE (§10.1)`.

**M7 ends here: verify, then push both branches.**

---

## Part D — docs, verification, report

### Task D1: aw-notetaker docs (CLAUDE.md "definition of done")

Update:

- `CLAUDE.md`: phase awareness (intake built, not deployed) and the repository section;
- `docs/phases/README.md`: the phase index row;
- the phase playbook that the README index names for AW Bots;
- `docs/SYSTEM_BRAIN.md`: module inventory, flows, data flow;
- `CHANGELOG.md`;
- `deployment/base/aw-bots/README.md`, with new runbook steps:

| Step | What it does |
|---|---|
| 9 | MIGRATION-0008 step 1. |
| 10 | Add the key ring to `aw-bots-secrets`, generated without display in the step 4d pattern. Then `helm upgrade`, in the order meeting-api → bot → exporter. |
| 11 | MIGRATION-0008 step 3. |
| 12 | Revoke tokens 1 and 2 by id. Mint the `calendar-dispatcher`, `portal` and `exporter` keys into `aw-bots-key-calendar-dispatcher` and `aw-bots-key-portal` (namespace `notetaker`) and `aw-bots-key-exporter` (namespace `aw-bots`). The commands print no key, following step 8's pattern. Roll the exporter. |
| 13 | Load `alerts.yml` into talke's Prometheus values and run `helm upgrade prometheus`, as talke's header describes. |
| 14 | Create the portal's webhook subscription with a secret the portal supplies. The secret goes into Secret `aw-bots-portal-webhook` without being displayed. |
| 15 | notetaker-postgres migration 0011. |
| 16 | Deploy the calendar module and the portal with every user on `old`. |
| 17 | Pilot: switch the owner to `aw-bots`. |

- [ ] Commit `docs: AW Bots intake — runbook steps, system brain, phases, changelog`.

### Task D2: Whole-branch verification and report

- Run `cd $V && PYTHONDONTWRITEBYTECODE=1 node scripts/gates.mjs <gate>` for **every gate that doesn't need the compose env**, and record each result line. List the gates not run, with the reason: `stack`, `compose`, `compose-stress`, `compose-chaos`, `eval`, `eval-baseline`, and any others that fail because `deploy/compose/.env` is absent.
- For every package, record:
  - pytest counts vs the baseline;
  - ruff and mypy findings for each touched upstream file, before and after;
  - black on the new files.
- Run the real-PG tests against the throwaway container and record the pass counts.
- Run `superpowers:requesting-code-review` over each branch, fix what it finds, and re-run.
- Write the final report:
  - what was built, per task;
  - test counts before and after;
  - the gate output;
  - the deviations, numbered;
  - what the human runs next: runbook steps 9–17, in order;
  - what was not verified.

**M8: push both branches.**

## Verification commands

```bash
V=/Applications/XAMPP/xamppfiles/htdocs/mike/vexa-meeting-intake; N=/Applications/XAMPP/xamppfiles/htdocs/mike/aw-notetaker-calendar-aw-bots
export PYTHONDONTWRITEBYTECODE=1
for d in core/meetings/services/meeting-api core/identity/services/admin-api core/gateway/services/gateway integrations/out/aw-notetaker; do (cd $V/$d && uv run pytest -q -p no:cacheprovider | tail -1); done
# real-Postgres tests (testing only; needs Docker Desktop running)
docker run -d --rm --name aw-intake-pg -e POSTGRES_PASSWORD=test -p 55432:5432 postgres:17
(cd $V/core/meetings/services/meeting-api && MEETING_API_TEST_DATABASE_URL=postgresql+asyncpg://postgres:test@localhost:55432/postgres uv run pytest -q -p no:cacheprovider | tail -1)
docker stop aw-intake-pg
for g in readme docs-version dataflow isolation isolation-py exports graph graph-py schema contract-version config-contract db-schema db-budget python node health access tracing replay telemetry licenses image-licenses runtime-parity execution-env test-isolation arch-report parity contract-conformance lite-makefile; do (cd $V && node scripts/gates.mjs $g >/dev/null 2>&1 && echo "PASS $g" || echo "FAIL $g"); done
for d in calendar-dispatcher notetaker-postgres; do (cd $N/$d && ../.venv/bin/python -m pytest -q -p no:cacheprovider | tail -1 && ../.venv/bin/black --check . && ../.venv/bin/ruff check . && ../.venv/bin/mypy .); done
cd $N/portal && npx vitest run && npx tsc --noEmit && npx next lint
```

## What the human runs, in order (after review; no session runs any of it)

1. MIGRATION-0008 step 1 on the live aw-bots Postgres (runbook step 9).
2. Build the images in CI and pin their tags. Add the key ring to `aw-bots-secrets`. Run `helm upgrade`, in the order meeting-api → bot → exporter (step 10).
3. MIGRATION-0008 step 3 (step 11).
4. Revoke tokens 1 and 2, mint the three named keys into their Secrets, and roll the exporter (step 12).
5. Load `alerts.yml` into Prometheus (step 13).
6. Create the portal's webhook subscription (step 14).
7. notetaker-postgres migration 0011 (step 15).
8. Deploy the calendar module and the portal with everyone on `old` (step 16).
9. Pilot: set the owner's `bot_backend='aw-bots'`, then run the §11.3 live tests (step 17).
