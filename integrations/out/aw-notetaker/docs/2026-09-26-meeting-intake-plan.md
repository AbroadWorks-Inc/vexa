# AW Bots meeting intake and webhooks — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Every unit is built test-first (superpowers:test-driven-development); no task is reported done without superpowers:verification-before-completion.

**Goal:** Any client (our calendar module, our portal, a third party) sends meeting *entries* to aw-bots through a sealed `/v2` API; aw-bots owns scheduling, dedup (R1–R8), bots and results, and reports every result to any number of signed webhook subscribers.

**Architecture:** A new `meeting_api/intake/` module (pure rules + a narrow `IntakeStore` port with an in-memory fake and a SQLAlchemy adapter) sits beside the existing spawn and stop paths and calls them; it never duplicates them. Our meeting state lives in a typed side table `meeting_aw_state`; entries in `meeting_entries`. admin-api owns subscriptions (encrypted secrets); meeting-api sends deliveries from a Redis Stream consumer group. calendar-dispatcher gains a per-user `bot_backend` switch that sends entries instead of bots; the portal reads meetings from `/v2` and receives webhooks, pushing them to browsers over SSE.

**Tech stack:** aw-bots — Python 3.11, FastAPI, SQLAlchemy 2 async + asyncpg, redis-py 5 (Streams), pytest + fakeredis, JSON Schema contracts, pnpm gate suite. aw-notetaker — Python 3.11, SQLAlchemy/Alembic, httpx, google-api-python-client; portal Next.js 14 + vitest.

**Spec:** `integrations/out/aw-notetaker/docs/2026-09-25-meeting-intake-and-webhooks-design.md` (revision V9). Every task cites the § it implements. Read the § before the task; the design wins over this plan.

**Worktrees (created 2026-09-26):**

| Repo | Worktree | Branch | From |
|---|---|---|---|
| aw-bots (`AbroadWorks-Inc/vexa`) | `/Applications/XAMPP/xamppfiles/htdocs/mike/vexa-meeting-intake` (`$V`) | `feat/meeting-intake` | `development` @ `5c58fd99` |
| aw-notetaker | `/Applications/XAMPP/xamppfiles/htdocs/mike/aw-notetaker-calendar-aw-bots` (`$N`) | `feat/calendar-aw-bots` | `feat/aw-bots-deployment` @ `814b639` |

Path shorthands in aw-bots: `MA` = `core/meetings/services/meeting-api`, `MAS` = `$MA/src/meeting_api`, `AA` = `core/identity/services/admin-api`, `AAS` = `$AA/src/admin_api`, `GW` = `core/gateway/services/gateway`, `EX` = `integrations/out/aw-notetaker`.

## Baseline (measured 2026-09-26, before any change)

| Package | Command | Result |
|---|---|---|
| meeting-api | `cd $V/$MA && PYTHONDONTWRITEBYTECODE=1 uv run pytest -q -p no:cacheprovider` | 1424 passed, 5 skipped |
| admin-api | same in `$V/$AA` | 67 passed, 87 skipped |
| gateway | same in `$V/$GW` | 367 passed, 1 xfailed |
| exporter | same in `$V/$EX` | 197 passed, 1 deselected |
| calendar-dispatcher | `cd $N/calendar-dispatcher && ../.venv/bin/python -m pytest -q -p no:cacheprovider` | **524** passed (the brief says 519; 524 is the floor) |
| notetaker-postgres | same in `$N/notetaker-postgres` | 88 passed |
| portal | `cd $N/portal && npx vitest run && npx tsc --noEmit && npx next lint` | 1759 passed / 12 skipped; tsc 0 errors; eslint clean |
| ruff (existing findings) | `uvx ruff@0.15.3 check src` | meeting-api 12, admin-api 0, gateway 3 |
| mypy (existing findings) | `uvx mypy==1.17.1 --ignore-missing-imports src` | meeting-api 132, admin-api 17, gateway 6 |
| black | `uvx black==25.1.0 --check src` | upstream is NOT black-formatted: 70 / 11 / 9 files would reformat |

`$N/.venv` is a fresh Python 3.11 venv with `notetaker-common`, `notetaker-postgres[dev]`, `calendar-dispatcher[dev]` installed editable from `$N`.

## Global constraints

- **Secrets:** never read, print, decode or paste a secret value. Test secrets are literal dummies (`"test-secret"`, `"k1"` → 32 zero bytes base64). Never open any live `*-secrets.yaml`, `.env*`, `vexa-fork/.env.local`. No key, secret, URL query string or transcript text in any log line (§8.11).
- **Sealed enum untouched:** `lifecycle.v1` `CompletionReason` keeps exactly its ten values; AW's cause is `meeting_aw_state.outcome_*` (§3 "Reasons"). A test asserts no code path writes a value outside the ten.
- **Seals are explicit steps:** `pnpm seal:schema`, `pnpm seal:contracts`, `pnpm seal:arch` each run in their own commit, never folded into a code commit (§8.9).
- **Link parsing:** the one parser is `collector/meeting_link.py` (aw-bots); never a second one. In aw-notetaker, `meeting_key.py` stays the old path's canonicaliser and is NOT used on the aw-bots path (§9.3).
- **Upstream routes unchanged** except where §8.4 says so: `/meetings`, `/bots`, `/transcripts` keep their shapes; new surface is `/v2` (§5).
- **Settings in config, never code:** every new setting is in `config.v1.json` of its service (gate `config-contract`) and in `deploy/helm/charts/vexa/values.yaml`, with the design's default (§8.8).
- **Deps:** no new Python dependency except the ones surfaced in "Decisions needed" D3. FINOS Category A only.
- **No history in code** (AGENTS.md): source states the designed present; no "used to" comments.
- **Lint scope (deviation DV3):** every NEW Python file is black 25.1.0 / ruff 0.15.3 / mypy 1.17.1 clean. In EXISTING upstream files, changed hunks follow the file's own style and the package's ruff and mypy finding counts must not rise above the baseline table. Upstream files are never bulk-reformatted.
- **Commits:** Conventional Commits, one concern per commit, body cites the §. aw-bots and aw-notetaker trailer: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Explicit `git add <paths>` only; never `git add -A`, stash, reset or amend. Never commit `docs/call_analysis_22.09.2026.txt`.
- **Logs (§8.11):** structured JSON through each service's existing `obs.log_event`; every line about a meeting carries `meeting_uuid`, `external_id`, `user`, `account` (user_id).
- **`bot.retry`** gets its `EventType` value now (A4); its emitter is handoff §6 B9, not this plan.
- **Tests:** every §6 use case is a named unit test in `$MA/tests/test_intake_use_cases.py` (`test_6_1_…` … `test_6_18_…`). Real-Postgres tests use `MEETING_API_TEST_DATABASE_URL` and `pytest.mark.skipif` without it, the same hook as `tests/test_single_flight.py`.
- **Gates before any push:** `node scripts/gates.mjs all` in `$V` (see "Verification" for what runs locally); black/ruff/mypy/pytest per touched package; portal tsc+eslint+vitest. Counts never drop below the baseline.
- **No deploy, no image build, no `kubectl apply`, no token minting or revoking.** The human runs every live step.

## Review focus (inputs the design implies but no §6 row exercises)

1. **Two rooms locked at once** (a link change moves an entry between rooms): the two advisory locks are taken in a fixed order (sorted key), so two opposite moves never deadlock. Test in Task A6 (real PG) and A5 (fake asserts order).
2. **Duplicate `PUT` racing itself** (the calendar module retries after a timeout while the first request is still running): the second waits on the room lock and replies `unchanged`; never two meetings. Real-PG test in A6.
3. **Clock skew and time zones in input**: `start`/`end` with a non-UTC offset (`+05:30`) are normalised to UTC; a naive timestamp is `invalid_request`. Tests in A4.
4. **A meeting created through upstream routes** (`POST /bots`, `POST /meetings`) that has no `meeting_aw_state` row: projections, webhooks (`sequence`) and the room resolver all work; the aw_state row is created lazily under the row lock. Tests in A3, A10, A12.
5. **Subscriber that answers 200 with a slow body / never closes**: the 10 s timeout covers the whole request; the stream item is not acknowledged until the attempt row is written; `XAUTOCLAIM` reclaims it if the replica dies. Tests in A14.

## Decisions needed before building (surfaced, not assumed)

| # | Question | Recommendation |
|---|---|---|
| D1 | §8.6 outbox: "a sweep re-enqueues any event whose `webhook_deliveries` row is missing" can't work as written. No table stores the event, so its payload can't be rebuilt, and an event with no matching subscriber never has a delivery row. | Add table `webhook_outbox` (`event_id` PK, `meeting_id`, `user_id`, `event_type`, `envelope` jsonb, `created_at`, `enqueued_at` null), written **in the same transaction** as the state change. After commit the request enqueues it and sets `enqueued_at`. The 60 s sweep enqueues rows with `enqueued_at IS NULL AND created_at < now() − 60 s`. Pruned with `webhook_deliveries` (30 d). One more sealed table (`seal:schema`). |
| D2 | §8.11 says `DELETE /v2/meetings/{id}` is the per-meeting erasure path, but §5.1 doesn't list it and §5.5 gives it no errors. | Build it in A7 (scope `tx`, like upstream `DELETE /meetings/{id}`), reusing upstream's artifact deletion; the S3 folder deletion "through the exporter's `export_s3_path`" is **deferred**: the exporter has no delete route, and adding one is a new cross-service write path the design doesn't specify. |
| D3 | New Python deps: `cryptography` (AES-256-GCM, §7.2) in admin-api and meeting-api; `prometheus-client` (§8.11) in meeting-api and admin-api. Both are in the canonical §10A.2 list and Category A. | Approve, as range pins in upstream style (`cryptography>=44,<46`, `prometheus-client>=0.19,<1`), added to the Dockerfile `uv pip install` line and `pyproject.toml`. Portal: `ioredis` (MIT) for §10.1 dedupe + pub/sub, outside §10A.2 like `pg`. |
| D4 | §8.1 says the claim-in-place branch "must change", and §8.3 covers auto-join (it passes the row id), but nothing says which row upstream `POST /bots` (no id) claims when a room has several `scheduled` rows. | Use the R1 `join_now` rule: the earliest non-finished row on the room with `end > now` and `start ≤ now + JOIN_NOW_ADOPT_AHEAD_S`; if none, insert a new row. A future standup is never claimed. |
| D6 | §8.11 sets an "active entries 100 000" limit per account, but §5.5 has no error code for it. | 429 `rate_limited`, message `active entry limit reached`, no `Retry-After` (a quota, the same class as upstream's 429 for `MaxBotsExceeded`). |
| D7 | W5: how the old signature rides during the 24 h after a rotation without breaking sealed `webhook.v1`. | Keep `X-Webhook-Signature` a single `sha256=<new>` (unchanged, matches the seal) and add `X-Webhook-Signature-Previous: sha256=<old>` only while the old secret is valid. The header is additive: webhook.v1 gains an optional property in `SignatureHeaders`, then `seal:contracts`. The exporter (system URL, never rotated this way) is unaffected. The portal verifier accepts either header. §7.4's text gets corrected in A21. |
| D8 | W6: which platforms an aw-bots user's events carry. | The aw-bots branch splits off in `run_poll_cycle` **before** the `DISPATCH_PLATFORMS` filter, so that filter keeps governing only the old path. aw-bots decides what it accepts (`unrecognized_link`, `ENTRY_BLOCKED_HOSTS` → `platform_not_enabled`, cached as `rejected` until the event changes or the 6-hourly resync). |
| D5 | Where alerts live (§8.11). No Prometheus scrape exists for aw-bots today, and I can't see whether the cluster has the prometheus-operator CRDs. | Ship `/metrics` + the metric names; write the alert rules into the runbook as PromQL. Add a `PrometheusRule` manifest only if the owner confirms the CRD exists. |

## Design claims found wrong while checking (reported, not built on)

| # | Design says | Code says | Plan does |
|---|---|---|---|
| W1 | §8.1/§10.4/brief: the index swap is `MIGRATION-0003-…md` | `admin_api/schema/MIGRATION-0003-default-max-concurrent-bots-3.md` exists; 0001–0007 are all used | Name it `MIGRATION-0008-meeting-live-dedup-index.md` |
| W2 | §8.11: "the exporter already exposes `/metrics`" | No Prometheus client or `/metrics` route anywhere in aw-bots (`grep -rniE 'prometheus|/metrics'` over `core/`, `deploy/helm`, `integrations/`) | meeting-api and admin-api gain `/metrics` (A18). `aw_export_total` is counted in meeting-api at the export-result route, so the exporter needs no metrics dependency |
| W3 | §14 heading: "None is changed by this design" | §8.10 (V9) fixes §14.1 (callback secret) and §8.1 fixes §14.6 (dedup list) | Follow §8 (the later revision); fix the §14 wording in Task A21 |
| W4 | §8.1 last bullet, §14.5: both model copies say `_sync_indexes` swallows a failed unique index (`sessions/models.py:105`) | Only `admin_api/schema/models.py:160` says it; `sessions/models.py:105-110` has no such comment | A1 fixes the admin-api comment only |
| W5 | §7.4: during rotation `X-Webhook-Signature: sha256=<new>,sha256=<old>` | Sealed `webhook.v1` `SignatureHeaders` pins `^sha256=[0-9a-f]{64}$`; `exporter/signature.py:37-38` compares the whole header, so a two-value header fails there too | Blocked on D7 |
| W6 | §9.2: aw-bots users get Meet, Zoom, Teams and Jitsi | `run_poll_cycle` drops every event whose platform isn't in `DISPATCH_PLATFORMS` (default `meet,zoom`, `main.py:1427, 1454-1456`, applied at `:1865`) **before** `_process_event` | Blocked on D8 |

Verifier sweep 2026-09-26: ~160 citations checked; every other one holds (largest drift 2 lines). `ENABLED_PLATFORMS` is bot-orchestrator's variable, not the dispatcher's; this work doesn't touch it, so the separate-commit rule never triggers.

---

## Part A — aw-bots (`$V`, branch `feat/meeting-intake`)

Order follows the design's §10.4 step 1 and the module dependencies: schema → parser → projection → contracts → intake core → store adapter → routes → spawn/auto-join → resolver → stop → events → subscriptions (admin) → sender (meeting-api) → callback secret → exporter → gateway limit → metrics → settings/helm → architecture seal → docs.

### Task A1: Schema — `meetings.uuid`, intake tables, live-index swap (§8.1, §8.9, D1)

**Files:**
- Modify: `$AAS/schema/models.py` (Meeting: `uuid`; new `MeetingEntry`, `MeetingAwState`, `WebhookSubscription`, `WebhookDelivery`, `WebhookOutbox`; replace `uq_meeting_active_user_platform_native` with `uq_meeting_live_user_platform_native`; add `ix_meeting_status_scheduled_at`)
- Modify: `$MAS/sessions/models.py` (mirror of every table meeting-api reads or writes: all five; same index change)
- Modify: `$AAS/schema/models.py:160-162` (the stale "swallows silently" comment: `sync.py:93-142` raises `SchemaInvariantError`; W4)
- Create: `$AAS/schema/MIGRATION-0008-meeting-live-dedup-index.md` (W1)
- Test: `$AA/tests/test_schema_intake_tables.py`, `$MA/tests/test_intake_models_mirror.py`, `$MA/tests/test_intake_pg_index.py` (real PG, skipped without the env)

**Interfaces — produces (column names are the contract for every later task):**

```python
# meetings
uuid = Column(UUID(as_uuid=True), nullable=False, unique=True, server_default=text("gen_random_uuid()"))
# new index (both copies), replacing uq_meeting_active_user_platform_native
Index("uq_meeting_live_user_platform_native", "user_id", "platform", "platform_specific_id", unique=True,
      postgresql_where=text("status IN ('requested','joining','awaiting_admission','needs_help','active','stopping')"))
Index("ix_meeting_status_scheduled_at", "status", text("(data ->> 'scheduled_at')"))

class MeetingEntry:      # __tablename__ = "meeting_entries"
    id BigInteger PK; user_id Integer not null; source_user Text not null; external_id String(255) not null
    meeting_id Integer FK meetings.id not null; meeting_url Text not null; platform String(100) not null
    native_meeting_id String(255) not null; title String(512) null; start_at DateTime(tz) not null
    end_at DateTime(tz) null; time_zone Text null; series_id String(255) null; attendees ARRAY(Text) null
    join_now Boolean not null default false; metadata_ JSONB null (column name "metadata"); state String(16) not null
    removed_reason Text null; created_at/updated_at DateTime(tz) server_default now(); removed_at DateTime(tz) null
    UniqueConstraint(user_id, source_user, external_id, name="uq_meeting_entries_user_source_external")
    Index(meeting_id); Index(user_id, platform, native_meeting_id, state); Index(attendees, postgresql_using="gin")
class MeetingAwState:    # "meeting_aw_state", meeting_id Integer PK FK meetings.id
    scheduled_end_at DateTime(tz) null; time_zone Text null; event_seq BigInteger not null server_default "0"
    outcome_kind/outcome_detail Text null; outcome_at DateTime(tz) null; waiting_for_room_sent_at DateTime(tz) null
    export_state/export_s3_path/export_error Text null; export_at DateTime(tz) null; updated_at DateTime(tz)
    Index(scheduled_end_at)
class WebhookSubscription:  # "webhook_subscriptions"
    id UUID PK default gen_random_uuid(); user_id Integer not null index; url Text not null
    secret_enc LargeBinary not null; enc_key_id String(64) not null; secret_last4 String(4) not null
    previous_secret_enc LargeBinary null; previous_enc_key_id String(64) null; previous_secret_expires_at DateTime(tz) null
    events ARRAY(Text) not null server_default '{}'; active Boolean not null default true; description Text null
    created_at/updated_at DateTime(tz)
class WebhookDelivery:   # "webhook_deliveries", one row per attempt
    id BigInteger PK; subscription_id UUID not null; user_id Integer not null; event_id String(80) not null
    event_type String(64) not null; meeting_id UUID null; attempt Integer not null; outcome String(16) not null
    status_code Integer null; error Text null; created_at DateTime(tz) server_default now()
    Index(subscription_id, created_at); Index(event_id); Index(created_at)
class WebhookOutbox:     # "webhook_outbox" (D1)
    event_id String(80) PK; user_id Integer not null; meeting_id Integer null; event_type String(64) not null
    envelope JSONB not null; created_at DateTime(tz) server_default now(); enqueued_at DateTime(tz) null
    Index("ix_webhook_outbox_pending", created_at, postgresql_where=text("enqueued_at IS NULL"))
```

`outcome` values (`outcome_kind`): `cancelled_by_calendar`, `not_sent`, `merged_into_live`. `state` values: `active`, `removed`. `export_state`: `handed_off`, `failed`. Delivery `outcome`: `delivered`, `retrying`, `failed`, `dead`.

- [ ] **Step 1:** Write failing tests. `test_schema_intake_tables.py`: the metadata has the five tables with the exact columns above; `uq_meeting_live_user_platform_native` exists with the six-status predicate; `uq_meeting_active_user_platform_native` does not; `meetings.uuid` is non-null, unique, server default `gen_random_uuid()`. `test_intake_models_mirror.py`: for every table meeting-api mirrors, the column names and types match admin-api's (import both via their `Base.metadata` — check the isolation gate allows the test-only edge; if it doesn't, compare against `schema.seal.json` instead, which is how `db-schema` already compares both copies).
- [ ] **Step 2:** Run: `cd $V/$AA && PYTHONDONTWRITEBYTECODE=1 uv run pytest -q tests/test_schema_intake_tables.py` → FAIL (tables missing).
- [ ] **Step 3:** Add the models in both files. Check `sync.py` `_col_default_sql` renders `gen_random_uuid()` and the `ARRAY(Text)` default; if `_sync_columns` can't add `uuid` safely (it can: `NOT NULL DEFAULT gen_random_uuid()` is legal on a populated table and fills every row), no sync change is needed.
- [ ] **Step 4:** Real-PG test `test_intake_pg_index.py` (skip without `MEETING_API_TEST_DATABASE_URL`): run `ensure_schema`, then prove (a) two `scheduled` rows + one `active` row on one (user, platform, native) insert fine; (b) a second live row raises `IntegrityError`; (c) the MIGRATION-0008 SQL, run in order against a DB that still has the old index, ends with only the new index; (d) `uuid` is filled on pre-existing rows.
- [ ] **Step 5:** Run both packages' full suites; counts ≥ baseline.
- [ ] **Step 6:** Write `MIGRATION-0008-meeting-live-dedup-index.md` in the style of MIGRATION-0002: why; pre-check (`SELECT user_id, platform, platform_specific_id, count(*) … WHERE status IN (<six>) GROUP BY 1,2,3 HAVING count(*) > 1` must return 0 rows); **step 1 before deploy** `CREATE UNIQUE INDEX CONCURRENTLY uq_meeting_live_user_platform_native …` and `CREATE INDEX CONCURRENTLY ix_meeting_status_scheduled_at …`, and add `meetings.uuid` + `CREATE UNIQUE INDEX CONCURRENTLY` on it ahead of `ensure_schema`, so no in-band lock is taken; **step 2** deploy; **step 3 after deploy** `DROP INDEX CONCURRENTLY uq_meeting_active_user_platform_native`; verification queries (`\d meetings`, `pg_index.indisvalid`); rollback (the old index is still there until step 3; rolling back after step 3 needs the old index re-created CONCURRENTLY and fails if a room already holds two scheduled rows, so the doc says roll back before step 3). Commands run with `kubectl -n aw-bots exec -it <postgres pod> -- psql -U postgres -d vexa` and the human types them; no password on the command line.
- [ ] **Step 7:** Commit `feat(schema): meeting uuid, intake tables, live-only room index (§8.1)`.
- [ ] **Step 8:** `cd $V && pnpm seal:schema && node scripts/gates.mjs db-schema` → green; commit `chore(seal): schema seal for intake tables (§8.9, lane:schema)` with only `schema.seal.json`.

### Task A2: Jitsi room names lower-cased at parse time (§12 O4)

**Files:** Modify `$MAS/collector/meeting_link.py:152-161`; Test `$MA/tests/test_meeting_link.py` (add cases).

- [ ] **Step 1:** Failing tests: `https://meet.jit.si/Standup` → room `standup`; `https://meet.abroadworks.com/Team-Sync` (host in `VEXA_JITSI_HOSTS`) → `team-sync@meet.abroadworks.com`; host part unchanged; Meet and Zoom results unchanged (existing tests).
- [ ] **Step 2–4:** run → fail; lower-case the room component only; run the whole file + suite → pass.
- [ ] **Step 5:** Commit `fix(meeting-link): lower-case Jitsi room names (design O4)`.

### Task A3: The meeting projection and `outcome` (§5.4, §8.1 "one function")

**Files:** Create `$MAS/intake/__init__.py`, `$MAS/intake/README.md` (gate `readme`), `$MAS/intake/projection.py`; Test `$MA/tests/test_intake_projection.py`.

**Interfaces — produces:**

```python
def project_meeting(meeting: dict, aw: Optional[dict], entries: list[dict], *, lead_s: int) -> dict:
    """The §5.4 meeting object. `meeting` = a meetings row dict (id, uuid, user_id, status, platform,
    platform_specific_id, data, start_time, end_time); `aw` = meeting_aw_state row dict or None;
    `entries` = active entries (dicts with external_id, user, attendees, series_id, metadata)."""
# keys, exactly: id (str uuid), status, completion_reason, failure_stage, outcome ({kind,detail,at}|None),
# platform, room, meeting_url, title, start, end, time_zone, bot_joins_at, entries, export ({state,s3_path,
# error,at}|None), sequence (int)
```

`completion_reason`/`failure_stage` come from `data` exactly where upstream stores them (read `lifecycle/machine.py` and `app.py:384-404`); `bot_joins_at` = `start − lead_s` while `scheduled`, the spawn time once a bot was sent (`data` key the spawn writes), `null` for `join_now` meetings not yet sent; times ISO 8601 with `Z`. No `user_id`, integer id, webhook secret or bot token ever appears (test asserts, mirroring `test_response_secret_projection.py`).

- [ ] Steps: failing tests for each §5.4 field, for `aw=None` (upstream-created meeting: `outcome null`, `sequence 0`, `end` from `end_time`), for each outcome kind, and for the forbidden-keys list → implement → pass → commit `feat(intake): meeting projection (§5.4)`.

### Task A4: Contracts — `intake.v1` (new, sealed) and `webhook.v1` event types (§8.9, §7.3)

**Files:**
- Create: `core/meetings/contracts/intake.v1/{README.md,intake.schema.json,validate.mjs,golden/*.json}` (copy the shape of `webhook.v1/`: its README sections, its `validate.mjs`)
- Modify: `core/meetings/contracts/webhook.v1/webhook.schema.json` (optional `X-Webhook-Signature-Previous` in `SignatureHeaders`, D7; `EventType` enum + `meeting.scheduled`, `meeting.updated`, `meeting.removed`, `meeting.waiting_for_room`, `meeting.not_sent`, `export.handed_off`, `export.failed`, `bot.retry`, `webhook.test`), its README
- Create: `$MAS/intake/validation.py`
- Test: `$MA/tests/test_intake_contract.py`, `$MA/tests/test_intake_validation.py`

`intake.schema.json` `$defs`: `Entry` (§5.2, incl. bounds: `external_id` ≤255, `title` ≤512, `attendees` ≤100 emails, `series_id` ≤255, `metadata` object, serialized size ≤16 384 bytes checked in code, `start`/`end` `date-time`), `Remove` (§5.3), `Reply` (§5.4 with `result` enum of the ten values), `Meeting` (A3's keys), `Error` (`{error:{code,message}}`, `code` enum of §5.5). Golden files: one reply per `result` value and one per error `code`.

**Interfaces — produces:**

```python
@dataclass(frozen=True)
class EntryIn:
    external_id: str; user: str; meeting_url: str; start: Optional[datetime]; end: Optional[datetime]
    time_zone: Optional[str]; title: Optional[str]; attendees: tuple[str, ...]; series_id: Optional[str]
    join_now: bool; metadata: Optional[dict]
@dataclass(frozen=True)
class RemoveIn:
    external_id: str; user: str; reason: Optional[str]
class IntakeError(Exception):
    def __init__(self, code: str, message: str, http_status: int): ...
def parse_entry(body: dict, *, now: datetime, max_days_ahead: int) -> EntryIn      # 400 invalid_request / too_far_ahead / already_ended
def parse_remove(body: dict) -> RemoveIn
```

Validation order: JSON Schema (→ `invalid_request`, message names the field, never echoes `metadata` or the URL query) → naive datetime → `invalid_request`; normalise to UTC; `end ≤ start` → `invalid_request`; `join_now` ignores `start`/`end` if sent (sets `start=now`, `end=None`); not `join_now` and `end ≤ now` → `already_ended`; `start > now + max_days_ahead` → `too_far_ahead`; lower-case `user` and `attendees`. Link parsing + `ENTRY_BLOCKED_HOSTS` happen in the service (A5), not here.

- [ ] Steps: failing tests (goldens validate against the schema; every error path; `+05:30` normalised; naive rejected; 16 KB metadata boundary 16 384 ok / 16 385 rejected) → implement → pass → commit `feat(contracts): intake.v1 contract and request validation (§8.9)`.
- [ ] `pnpm seal:contracts` for `intake.v1` → commit `chore(seal): seal intake.v1 (§8.9, lane:contract)`.
- [ ] Add the webhook `EventType` values; prove every existing webhook golden still validates (`node core/meetings/contracts/webhook.v1/validate.mjs`) → commit `feat(contracts): webhook.v1 event types for intake (§7.3)`; the PR body states the back-compat call (§8.9); `pnpm seal:contracts` → commit `chore(seal): webhook.v1 event types (lane:contract)`.
- [ ] `node scripts/gates.mjs contract-version` green after each seal.

### Task A5: Intake core — rules, `IntakeStore` port, fake, service; every §6 use case (§3, §6, §8.2)

**Files:**
- Create: `$MAS/intake/rules.py`, `$MAS/intake/ports.py`, `$MAS/intake/fakes.py`, `$MAS/intake/service.py`, `$MAS/intake/settings.py`
- Test: `$MA/tests/test_intake_rules.py`, `$MA/tests/test_intake_use_cases.py`, `$MA/tests/test_intake_service.py`

**Interfaces — produces:**

```python
# rules.py (pure, no I/O)
LIVE_STATUSES = ("requested","joining","awaiting_admission","needs_help","needs_human_help","stopping")  # imported from bot_spawn.auto_join, not redefined
FINISHED = ("completed","failed")
def overlaps(a_start, a_end, b_start, b_end) -> bool                      # a_start < b_end and a_end > b_start; None end = open
def meeting_window(m: MeetingView, *, now: datetime) -> tuple[datetime, Optional[datetime]]  # open-ended live → (start, now)
def match_entry(entry: EntryIn, candidates: list[MeetingView], *, now, lead_s) -> Optional[MeetingView]   # R1 (earliest start wins on ties)
def join_now_target(candidates: list[MeetingView], *, now, adopt_ahead_s) -> Optional[MeetingView]       # R1 join_now rule
def recompute(entries: list[EntryView]) -> tuple[datetime, Optional[datetime], Optional[str], Optional[str]]  # (earliest start, latest end, title, time_zone)

# ports.py
class IntakeStore(Protocol):
    def room_lock(self, user_id: int, rooms: list[Room]) -> AsyncContextManager["IntakeTx"]  # locks sorted rooms, one txn
class IntakeTx(Protocol):
    async def find_entry(self, user_id: int, source_user: str, external_id: str) -> Optional[EntryView]
    async def room_meetings(self, user_id: int, room: Room) -> list[MeetingView]        # non-finished only
    async def meeting(self, meeting_id: int) -> MeetingView
    async def create_meeting(self, user_id: int, room: Room, *, meeting_url: str, title, start, end, time_zone, join_now: bool) -> MeetingView  # status 'scheduled', data.auto_join=true, data.scheduled_at=start
    async def save_entry(self, user_id: int, entry: EntryIn, room: Room, meeting_id: int, *, reactivate: bool) -> EntryView
    async def mark_entry_removed(self, entry_id: int, reason: Optional[str]) -> None
    async def active_entries(self, meeting_id: int) -> list[EntryView]
    async def update_meeting_plan(self, meeting_id: int, *, start, end, title, meeting_url, time_zone, room: Room) -> None
    async def move_active_entries(self, from_meeting_id: int, to_meeting_id: int) -> None
    async def finish_unsent(self, meeting_id: int, *, completion_reason: Optional[str], outcome_kind: str, outcome_detail: Optional[str]) -> None  # status 'failed'
    async def set_outcome(self, meeting_id: int, kind: str, detail: Optional[str]) -> None
    async def emit(self, meeting_id: int, event_type: str, change: Optional[dict] = None) -> str  # bumps event_seq, writes webhook_outbox row, returns event_id
class SpawnPort(Protocol):
    async def spawn_exact(self, user_id: int, meeting_id: int) -> SpawnOutcome   # SpawnOutcome(ok: bool, detail: Optional[str]) detail ∈ {account_limit, spawn_error, room_busy}
class StopPort(Protocol):
    async def stop_meeting(self, user_id: int, meeting_id: int, *, outcome_kind: Optional[str], outcome_detail: Optional[str]) -> None
class EventPublisher(Protocol):
    async def publish(self, event_ids: list[str]) -> None     # after commit; enqueue outbox rows (A14)

# service.py
class IntakeService:
    def __init__(self, store: IntakeStore, spawn: SpawnPort, stop: StopPort, publisher: EventPublisher,
                 settings: IntakeSettings, clock: Callable[[], datetime]) -> None
    async def put_entry(self, user_id: int, body: dict) -> dict        # reply §5.4
    async def remove_entry(self, user_id: int, body: dict) -> dict
# settings.py
@dataclass(frozen=True)
class IntakeSettings:
    max_days_ahead: int = 30; join_now_adopt_ahead_s: int = 3600; lead_s: int = 300
    blocked_hosts: frozenset[str] = frozenset(); max_active_entries: int = 100_000
    @classmethod
    def from_env(cls) -> "IntakeSettings"   # ENTRY_MAX_DAYS_AHEAD, JOIN_NOW_ADOPT_AHEAD_S, AUTO_JOIN_LEAD_S, ENTRY_BLOCKED_HOSTS (comma list), INTAKE_MAX_ACTIVE_ENTRIES
```

`put_entry` follows §8.2 steps 1–7 exactly, under `room_lock` (both old and new rooms when the entry exists on a different room). Events: new meeting → `meeting.scheduled`; changed meeting → `meeting.updated`; last entry left a scheduled meeting → `meeting.removed` (R8). `join_now`: after commit, `spawn_exact`; on failure, a second `room_lock` txn runs `finish_unsent(outcome_kind='not_sent', detail=…)` + `meeting.not_sent`, and the reply shows it (§8.2). `remove_entry`: `entry_not_found`, `already_removed`, `entry_removed`, `removed` (R8: `finish_unsent(completion_reason='stopped', outcome_kind='cancelled_by_calendar', detail=reason)` + `meeting.removed`), `bot_stopping` (R5: after commit, `stop.stop_meeting(outcome_kind='cancelled_by_calendar', detail=reason)`).

Visibility assumption (stated here): `GET /v2/meetings?user=` (A7) matches a meeting through **any** of its entries, active or removed, so a cancelled meeting stays visible as history (R8).

`test_intake_use_cases.py` — one test per row; each asserts the reply `result`, the meeting status/outcome, the entries list, the events emitted in order, and spawns/stops called:

| Test | Setup → action | Expect |
|---|---|---|
| `test_6_1_one_off_created` | PUT §6.1 body | `created`, status `scheduled`, `bot_joins_at` = start − 300 s, events `[meeting.scheduled]` |
| `test_6_2_instant_join_new` | PUT §6.2 body, spawn ok | `created`, status `requested`, `spawn_exact` called once with that row |
| `test_6_2_instant_join_unrecognized_link` | PUT with `https://example.com/x` | 400 `unrecognized_link` |
| `test_6_3_instant_join_adopts_scheduled_0945_for_1000` | scheduled 10:00–10:30; now 09:45; PUT join_now | `joined_existing`, same uuid, spawn_exact on it |
| `test_6_3_instant_join_live_no_second_bot` | live meeting on room | `joined_existing`, no spawn |
| `test_6_3_paste_1020_does_not_adopt_tomorrow` | today's 10:00–10:15 finished, tomorrow's scheduled; now 10:20 | `created` (new open-ended meeting) |
| `test_6_4_series_one_meeting_per_occurrence` | 10 occurrences same room | 10 `created`, 10 uuids |
| `test_6_5_occurrence_moved_same_id` | PUT then PUT with new start | `updated`, same uuid, new `bot_joins_at`, events `[scheduled, updated]` |
| `test_6_6_occurrence_cancelled` | PUT then remove reason `cancelled` | `removed`, status `failed`, `completion_reason` `stopped`, outcome `cancelled_by_calendar`/`cancelled` |
| `test_6_6_other_user_entry_remains` | A and B entries, remove A | `entry_removed`, meeting still `scheduled` |
| `test_6_6_live_last_entry` | live meeting, remove last | `bot_stopping`, `stop_meeting` called with `cancelled_by_calendar` |
| `test_6_7_series_new_ids_remove_and_create` | old ids removed, new ids created | `removed` ×n + `created` ×n |
| `test_6_7_series_same_ids_updated` | same ids new times | `updated` ×n |
| `test_6_7_moved_out_of_window_then_back` | remove `moved_out_of_window`, re-PUT same id | `created`, `previous_meeting_id` = old uuid, old meeting stays `failed` |
| `test_6_8_series_cancelled` | remove each | `removed` each |
| `test_6_9_link_changed_moves_room` | PUT new `meeting_url` | `updated`, room changed, same uuid (only entry) |
| `test_6_9_link_changed_joins_existing_meeting` | new link matches another meeting | `updated`, new uuid, `previous_meeting_id` = old; the old meeting (no active entries left) is removed per §8.2 step 5: `failed` / `stopped` / `cancelled_by_calendar`, event `meeting.removed` |
| `test_6_10_title_changed` | PUT new title | `updated`; during live → `not_changed_live` |
| `test_6_11_declined_one_of_two` | remove B `declined` | `entry_removed`; bot still goes |
| `test_6_12_same_meeting_two_users` | A then B, same room/time | B `joined_existing`, same uuid, entries A+B, one meeting |
| `test_6_13_back_to_back_separate` | 14–15 and 15–16 same room | two `created`, two uuids |
| `test_6_14_two_accounts_separate` | user_id 1 and 2 same room/time | two meetings |
| `test_6_15_cancel_while_live` | as 6.6 live | `bot_stopping`; (A11 proves the terminal webhook carries the outcome) |
| `test_6_16_changed_after_finish` | meeting `completed`, PUT/remove | `not_changed_finished` |
| `test_6_17_too_far_unknown_blocked` | start now+31 d; unknown link; `meet.abroadworks.com` blocked | `too_far_ahead`, `unrecognized_link`, `platform_not_enabled` |
| `test_6_18_owner_recurring_moved_this_event` | §6.18 create 28 Sep, update to 25 Sep | `created` then `updated`, `bot_joins_at` 2026-09-25T11:25:00Z |

Plus `test_intake_rules.py` (§11.1): touching times don't merge; join_now bound (09:45 adopts 10:00; 10:20 doesn't adopt tomorrow's); open-ended live meeting matches only due entries (`start ≤ now + lead`); earliest-start tie-break. `test_intake_service.py`: `unchanged` on identical re-PUT (no event); a `removed` entry re-PUT → `created`/`joined_existing`; R7 live `not_changed_live` stores on entry only; join_now spawn failure → `failed`/`not_sent`/`account_limit`; `max_active_entries` reached → per D6; the rooms of an entry moving between rooms are locked in sorted order (fake records lock order); the completion_reason written is always one of the sealed ten or null (`test_completion_reason_sealed_set`).


- [ ] Steps: write `test_intake_rules.py` → fail → `rules.py` → pass → commit `feat(intake): R1 matching rules (§3)`. Write the fake (`fakes.py`, in the style of `collector/fakes.py`: dict tables, a lock-order log, no `await` between check and write) + `test_intake_use_cases.py` + `test_intake_service.py` → fail → `service.py` → pass → full suite → commit `feat(intake): entry service with every §6 use case (§8.2)`.

### Task A6: SQLAlchemy `IntakeStore` adapter + real-Postgres proof (§8.2, §11.2)

**Files:** Create `$MAS/intake/adapters.py`; Test `$MA/tests/test_intake_adapter_pg.py` (skipped without env).

- `room_lock`: one `AsyncSession` transaction; for each room in sorted `(platform, native)` order `SELECT pg_advisory_xact_lock(hashtext('aw-intake'), hashtext(:user_id || ':' || :platform || ':' || :native))` (two-int form: a separate key space from the spawn path's single-bigint `pg_advisory_xact_lock(user_id)`).
- `emit`: `SELECT … FROM meeting_aw_state WHERE meeting_id=:id FOR UPDATE` (insert the row first with `ON CONFLICT DO NOTHING` so upstream-created meetings work), `event_seq += 1`, `event_id = "evt_" + sha256(f"{uuid}|{event_type}|{seq}")`, envelope built with A3's projection, insert into `webhook_outbox`.
- Real-PG tests: the §6.12 race (two concurrent PUTs for A and B, same room/time → one meeting); duplicate PUT racing itself → one meeting, second reply `unchanged`; two opposite room moves in parallel → both finish (no deadlock, 5 s timeout); two scheduled + one live row on one room (§11.2); the outbox row commits atomically with the state change (a forced exception after `emit` leaves neither).
- [ ] Steps: tests → fail (skip locally if no env, but they must at least import and collect) → implement → run with a local Postgres if the human provides `MEETING_API_TEST_DATABASE_URL` (a throwaway `docker run postgres:17-alpine` URL is fine; no production value) → commit `feat(intake): Postgres store with room advisory locks (§8.2)`.

### Task A7: `/v2` routes + `routes.v1.json` (§5, D2)

**Files:** Create `$MAS/intake/router.py`, `$MAS/intake/reads.py` (list/get queries: filters `user`, `from`, `to`, `status`, `external_id`; cursor = base64 of `(scheduled_at_or_start, id)`; `limit` default 50, max 200; visibility = entry `source_user = :user OR :user = ANY(attendees)`, any entry state); Modify `$MAS/app.py` (mount router in `create_app`), `core/meetings/routes.v1.json` (rows below); Test `$MA/tests/test_intake_routes.py`, `$GW/tests/` gateway manifest test if one enumerates routes.

| Method + path | Scope |
|---|---|
| `PUT /v2/entries` | `bot` |
| `POST /v2/entries/remove` | `bot` |
| `GET /v2/meetings` | `tx` |
| `GET /v2/meetings/{meeting_id}` | `tx` |
| `POST /v2/meetings/{meeting_id}/stop` | `bot` |
| `DELETE /v2/meetings/{meeting_id}` | `tx` (D2) |

Error mapping: `IntakeError` → `{error:{code,message}}` with its status; FastAPI validation errors on these routes → 400 `invalid_request` in the same shape (a route-scoped exception handler, so upstream routes keep their 422); DB/Redis down → 503 `unavailable`; a UUID from another account → 404 `meeting_not_found` (never 403, no existence leak).
- [ ] Steps: route tests with the fake store (every §5.5 code produced by the route layer, body shape validated against `intake.v1`, `x-user-id` required, `user=` visibility incl. attendee and removed-entry cases, cursor paging stable across inserts) → fail → implement → pass → gateway route-manifest test green (`cd $V/$GW && uv run pytest -q`) → commit `feat(intake): /v2 entry and meeting routes (§5)`.

### Task A8: Spawn the exact row; live-set dedup; `POST /bots` claim rule (§8.3, §8.1, D4)

**Files:** Modify `$MAS/bot_spawn/ports.py` (`create_meeting_guarded(..., claim_meeting_id: Optional[int] = None)`, `request_bot(..., claim_meeting_id=None)`), `$MAS/bot_spawn/adapters.py:459-573`, `$MAS/bot_spawn/fakes.py`, `$MAS/bot_spawn/service.py`; Create `$MAS/intake/spawn.py` (`SpawnPort` implementation calling `request_bot` with `claim_meeting_id`, mapping `MaxBotsExceeded`/`QuotaExceeded` → `account_limit`, `DuplicateMeeting` → `room_busy`, `SpawnFailed` → `spawn_error`); Test `$MA/tests/test_spawn_exact_row.py`.

- dedup list at `adapters.py:485` becomes `auto_join.LIVE_STATUSES` (all seven incl. `needs_help`, `stopping`, `needs_human_help`) — §14.6.
- `claim_meeting_id` given → lock and claim exactly that row if it is `idle`/`scheduled`, else `MeetingStopped`/`DuplicateMeeting` as today's semantics.
- `claim_meeting_id` None → D4 rule: earliest `idle`/`scheduled` row on the room with `scheduled_end_at > now` (or, entry-less rows, `scheduled_at + AUTO_JOIN_GRACE_S > now`) and `scheduled_at ≤ now + JOIN_NOW_ADOPT_AHEAD_S`; else insert.
- [ ] Tests: two scheduled rows (today 10:00, in 7 days) → auto-join claims today's by id; `POST /bots` at 09:55 claims today's; at 10:40 (after end) inserts new; a `needs_help` live row blocks a spawn; fake and adapter agree. Commit `fix(bot-spawn): claim the exact planned row; dedup on the full live set (§8.3, §14.6)`.

### Task A9: Auto-join — due until end, waiting for room, merge into open-ended, not-sent sweep (§3 R2 R6, §8.3)

**Files:** Modify `$MAS/bot_spawn/auto_join.py` (`due_rows` gains `scheduled_end_at`; live-sibling branch), `$MAS/bot_spawn/adapters.py` (`list_scheduled_meetings` joins `meeting_aw_state` and returns `scheduled_end_at`, `waiting_for_room_sent_at`; uses `ix_meeting_status_scheduled_at`), `$MAS/__main__.py` (new not-sent sweep under `run_single_flight("intake-not-sent", …)`); Create `$MAS/intake/sweeps.py`; Test `$MA/tests/test_auto_join_intake.py`, extend `test_main_auto_join_wiring.py`.

- Rows with an entry (`scheduled_end_at` present or `data.intake=true` marker written by `create_meeting`): due in `[scheduled_at − lead, scheduled_end_at]`; entry-less rows keep today's `[start − lead, start + grace]`.
- Live sibling on the room: if the sibling is open-ended `join_now` (`meeting_aw_state.scheduled_end_at IS NULL` and it has a `join_now` entry) → merge (R2 exception) through `IntakeService.merge_into_live(due_id, live_id)` (move entries, adopt title if none, `finish_unsent(outcome_kind='merged_into_live', detail=live uuid)`, `meeting.removed` with `data.merged_into`, `meeting.updated` on the live one); else if `waiting_for_room_sent_at` is null → set it + `meeting.waiting_for_room`, keep due.
- Not-sent sweep (30 s, single-flight): `scheduled` rows with `scheduled_end_at < now` → `finish_unsent(completion_reason=None, outcome_kind='not_sent', detail=last auto_join_error mapped to account_limit|spawn_error|room_busy, else ended_before_sent)` + `meeting.not_sent`; metric `aw_meetings_not_sent_total{detail}` (A18).
- auto-join passes `claim_meeting_id=row["id"]` (A8).
- [ ] Tests (§11.1): late entry (start past, end ahead) is due at once; R2 wait → `waiting_for_room` sent once across three ticks → released when the sibling finishes; merge into open-ended; each `not_sent` detail; entry-less row unchanged behaviour (existing `test_auto_join_*` stay green); two sweeps on two "replicas" with the fake single-flight lock run once. Commit `feat(auto-join): intake due window, waiting for room, not-sent sweep (§8.3)`.

### Task A10: Room-code resolver (§8.4)

**Files:** Create `$MAS/intake/resolver.py`; Modify every site in §13.2 (`collector/adapters.py` `get_transcript`, `authorize_subscribe`, participants, workspace, share, docs, `set_intent`; `collector/app.py` `_resolve_owned_native`; `bot_spawn/adapters.py` `find_latest`), `lifecycle/stop_router.py:152-158`; Test `$MA/tests/test_room_resolver.py` + existing suites.

```python
class AmbiguousRoom(Exception): ...   # → 409 {"detail": "several scheduled meetings on this room; use the meeting id", "code": "ambiguous_room"}
def pick_for_read(rows: list[dict], *, now: datetime) -> Optional[dict]        # live, else most recent started (start_time or scheduled_at ≤ now); never future scheduled
def pick_for_planned_edit(rows: list[dict]) -> Optional[dict]                  # live, else the single scheduled; >1 scheduled → AmbiguousRoom
def pick_for_stop(rows: list[dict]) -> list[dict]                               # live rows only
```

Upstream error body shape for these routes stays `{"detail": …}` (they are upstream routes); `find_latest` (continue_meeting) keeps "most recent terminal" semantics but never returns a future scheduled row.
- [ ] Tests per route kind (§11.1 "the room-code resolver for each route kind"): room with 1 live + 2 future scheduled → reads return live; 0 live + 1 past completed + 2 future → reads return the completed; planned edit with 2 scheduled → 409; `DELETE /bots/{p}/{n}` with 2 future scheduled + 1 live stops only the live one and leaves both scheduled (update `test_stop_route.py` expectations that encoded "cancel every planned row", citing §8.4). Commit `feat(meetings): one room-code resolver; stop never cancels future plans (§8.4)`.

### Task A11: Stop one meeting (§8.5, R5)

**Files:** Create `$MAS/intake/stop.py` (`StopPort` impl) reusing `lifecycle/stop_router.py` helpers `_mark_stop_requested`, `leave_command_*`, runtime delete for booting statuses (refactor them into a callable `stop_meeting_row(repo, publisher, runtime, row)` inside `stop_router.py`, used by both the old route and the new port — no duplicate logic); route `POST /v2/meetings/{id}/stop` (A7) calls it with no outcome; Test `$MA/tests/test_intake_stop.py`.
- Outcome written to `meeting_aw_state` **when the stop is requested**, so the terminal `status_change`/`meeting.completed` webhook carries it; the user stop keeps `completion_reason='stopped'` (`machine.py:56-76`).
- [ ] Tests: `requested`/`joining`/`awaiting_admission` → workload delete + leave command; `active` → leave command only; `scheduled` → terminalised like upstream `_cancel_planned` (status `failed`, `completion_reason` `stopped`, no outcome), reply 200; finished → 200 no-op with the meeting. R5 path carries `cancelled_by_calendar`. Commit `feat(intake): stop exactly one meeting (§8.5)`.

### Task A12: Event model — `sequence`, stable `event_id`, enriched lifecycle events (§7.3)

**Files:** Modify `$MAS/lifecycle/webhook.py` (`derive_event_id` → `evt_` + sha256(uuid|type|seq); envelopes carry the A3 projection under `data.meeting` and `data.change`), `$MAS/app.py:384-404` (projection gains `uuid`, `entries`, `outcome`, `sequence`), the callback path that emits status changes (bump `event_seq` + outbox row in the same transaction as the status write); regenerate `core/meetings/contracts/webhook.v1/golden/*` by running the real builders (§8.9: regenerated, not hand-edited) — add a `scripts/`-free generator test that fails when goldens drift; Test `$MA/tests/test_webhook_intake_events.py`, `test_contract_conformance.py` stays green.
- Legacy system URL and per-user URL deliveries keep working (exporter keeps receiving `meeting.completed`) and now carry `uuid` (§8.7).
- [ ] Tests: `sequence` strictly +1 per event per meeting across intake and lifecycle events; `event_id` identical on retry, different across events; `meeting.status_change` carries `from`/`to`/`reason`; an upstream-created meeting (no aw_state) gets sequence 1 on its first event. Commit `feat(webhooks): per-meeting sequence and stable event ids (§7.3)`.

### Task A13: admin-api — webhook subscriptions, key ring, internal read, retention (§7.2, §7.5, §8.10)

**Files:** Create `$AAS/app/webhook_subscriptions.py` (router), `$AAS/app/secret_box.py` (AES-256-GCM key ring), `$AAS/app/ssrf.py` (see isolation note), `$AAS/app/retention.py`; Modify `$AAS/app/main.py` (mount; daily prune loop under a Postgres advisory single-flight), `core/identity/routes.v1.json` (the `/v2/webhooks…` rows, scope `bot`), `$AA/pyproject.toml` + `$AA/Dockerfile` (`cryptography`, D3); Test `$AA/tests/test_webhook_subscriptions.py`, `$AA/tests/test_secret_box.py`.

```python
class SecretBox:
    def __init__(self, keys: dict[str, bytes], active_key_id: str) -> None     # from WEBHOOK_SECRET_ENC_KEYS (JSON id→base64 32 B) + WEBHOOK_SECRET_ENC_ACTIVE_KEY; fail closed if active missing or key ≠ 32 B
    def encrypt(self, plaintext: str) -> tuple[bytes, str]                     # (nonce‖ciphertext‖tag, key_id); 12-byte random nonce; AAD = b"aw-webhook-secret"
    def decrypt(self, blob: bytes, key_id: str) -> str
    def needs_rewrap(self, key_id: str) -> bool
```

Routes (§7.2): `POST /v2/webhooks` (≤ 20 per account → 429 `rate_limited`; `events` ⊆ webhook.v1 `EventType`; URL SSRF-checked on save with `WEBHOOK_PRIVATE_HOST_ALLOWLIST`; secret supplied or `secrets.token_urlsafe(32)` returned once), `GET` (never a secret; `secret_last4`), `PATCH`, `DELETE`, `POST …/rotate-secret` (previous kept 24 h), `POST …/test` (admin-api never writes meeting-api's outbox: it calls meeting-api `POST /internal/webhooks/test` with `INTERNAL_API_SECRET`, which A14 adds), `GET …/deliveries?limit=&before=`. Internal: `GET /internal/users/{id}/webhook-subscriptions` (auth `X-Internal-Secret`, constant-time), returns active subscriptions **with** `secret_enc`/`enc_key_id`/previous (ciphertext only; meeting-api decrypts with the same key ring at signing time). Lazy re-encrypt: on internal read, rows with `needs_rewrap` are re-encrypted under the active key in the same transaction.

Isolation note: admin-api may not import meeting-api (gate `isolation-py`). Check `scripts/check-isolation-py.mjs` allowed edges; if no shared package can hold `ssrf.py`, admin-api gets its own save-time check that calls the same algorithm (resolve host, reject private/loopback/link-local/metadata ranges unless the host is in the allow-list). Duplicating ~60 lines of a guard across two services is the upstream pattern already used for `obs.py` ("self-contained per-service"); the plan accepts it and says so in the PR.

- [ ] Tests (§11.1 encryption): row written under key A is readable after B becomes active and is re-encrypted under B on read; wrong key id → error, never plaintext; no route response or log contains the secret (assert against captured logs); 21st subscription → 429; private URL refused unless allow-listed; rotate keeps previous for 24 h; deliveries paging. Commit `feat(admin-api): webhook subscriptions with encrypted secrets (§7.2)`; retention prune → commit `feat(admin-api): prune webhook deliveries after 30 days (§8.11)`.

### Task A14: meeting-api — Redis Stream sender, retries, log, outbox sweep (§7.4, §7.5, §8.6, D1)

**Files:** Create `$MAS/webhooks/subscriptions.py` (subscription cache 30 s via admin-api internal route), `$MAS/webhooks/stream_sender.py`, `$MAS/webhooks/signing.py`, `$MAS/webhooks/secret_box.py` (same algorithm as A13, same env), `$MAS/intake/outbox.py` (`EventPublisher` impl + sweep); Modify `$MAS/__main__.py` (start one consumer per replica; outbox sweep 60 s under `run_single_flight("webhook-outbox", …)`), `$MAS/webhooks/ssrf.py` (allow-list param), `$MA/pyproject.toml` + `$MA/Dockerfile` (`cryptography`, D3), `$MAS/app.py` (`POST /internal/webhooks/test`); Test `$MA/tests/test_stream_sender.py`, `test_webhook_signing.py`, `test_outbox.py`.

- Stream `aw:webhook-deliveries`, group `aw-senders`, consumer name = pod hostname. Item fields: `event_id`, `subscription_id`, `attempt`, `not_before` (epoch s). Publish = per outbox row: load subscriptions, filter by `events` (empty = all), `XADD` one item each, set `outbox.enqueued_at`.
- Worker loop: `XREADGROUP COUNT 10 BLOCK 5000`; item with `not_before` in the future → re-`XADD` with same fields + `XACK` (never busy-wait); else load envelope from `webhook_outbox`, load subscription (cache), SSRF check again, sign, POST (httpx timeout 10 s total), insert `webhook_deliveries` row, then `XACK`. Retry on 5xx/429/timeout/connection error at +60 s, +300 s, +1800 s, +7200 s (reuse `webhooks/retry.py` constants); after the 4th retry → row `dead`. Other 4xx → `failed`. `XAUTOCLAIM min-idle 60000` every loop turn.
- Signing (per D7): `X-Webhook-Timestamp`, `X-Webhook-Signature: sha256=<new>`, plus `X-Webhook-Signature-Previous: sha256=<old>` while `previous_secret_expires_at > now`; body = the exact bytes posted; **no `Authorization` header**; `Content-Type: application/json`.
- [ ] Tests (§11.1): two consumers on one fakeredis stream deliver each item once; an unacked item is reclaimed by the other consumer after 60 s (fake clock); outbox sweep enqueues a committed row with `enqueued_at NULL` older than 60 s and ignores younger ones; retry schedule + `dead` after the last; 400 → `failed` no retry; signature verifies with the exporter's `exporter/signature.py` algorithm (re-implemented in the test, not imported: test-isolation gate); both signatures during rotation; SSRF allow-list lets `portal.notetaker.svc.cluster.local` through and blocks `10.0.0.1`; no secret in logs; delivery rows written per attempt. Commits: `feat(webhooks): signed subscription deliveries over a Redis Stream (§8.6)`, `feat(intake): transactional outbox and repair sweep (§8.6, D1)`.

### Task A15: Enforce the bot's lifecycle callback secret (§8.10)

**Files:** Modify `$MAS/app.py:932-938` (compare `x-internal-secret` with `INTERNAL_API_SECRET` via `hmac.compare_digest`; missing/mismatch → 401; unset secret → 401 for every call, fail closed, and `config_preflight` reports it); Test `$MA/tests/test_callback_secret.py`; update test fixtures that call the callback without the header.
- Check first that the bot image sends the header on **every** callback (`core/meetings/services/bot/src/adapters/lifecycle-http.ts:68`) and that meeting-api's spawn passes `internal_secret` into the bot env (`request_bot(internal_secret=…)`). If any bot callback path lacks it, STOP and report: enforcing would break live bots (fix at the producer first).
- [ ] Commit `fix(meeting-api): require the internal secret on bot lifecycle callbacks (§8.10)` — its own commit.

### Task A16: Exporter — UUID ids, own key through the gateway, export result route (§8.7, §8.10)

**Files:** Modify `$EX/exporter/job.py:98-100` (ids = `meeting.uuid`; `_export.json` keeps `vexa_meeting_id` integer too), `$EX/exporter/notetaker.py:37-42`, `$EX/exporter/attribution.py`/`schemas.py` (the id field in `speaker_timeline.json`/`participants.json`), `$EX/exporter/vexa_client.py` (reads via `GATEWAY_URL` + `X-API-Key` from `EXPORTER_API_KEY`; no `X-User-Id`), `$EX/exporter/config.py`; Create `$EX/exporter/export_result.py` (`POST {MEETING_API_URL}/internal/meetings/{uuid}/export` with `Authorization: Bearer <INTERNAL_API_SECRET>`); meeting-api side: Create route in `$MAS/intake/router.py` or `$MAS/app.py` — `POST /internal/meetings/{meeting_uuid}/export` (Bearer check like `recordings/router.py:241-252`), writes `meeting_aw_state.export_*`, emits `export.handed_off`/`export.failed`; Tests `$EX/tests/test_job.py`, `$EX/tests/test_export_result.py`, `$MA/tests/test_export_result_route.py`.
- A webhook without `uuid` (an old meeting-api) → the job fails loudly (no fallback to `vexa-<n>`; owner preference "no fallbacks").
- The exporter's own tests, black/ruff/mypy strict (its pyproject pins) stay green.
- [ ] Commit `feat(exporter): meeting UUID everywhere, own key, report export result (§8.7)`; `feat(meeting-api): internal export result route and export events (§8.7)`.

### Task A17: Gateway — intake write rate limit (§8.11, §5.5)

**Files:** Modify `$GW/src/gateway/ratelimit.py` (a second limiter keyed `intake:<user_id>`, window 60 s, `INTAKE_RATE_LIMIT_PER_MIN` default 600), `$GW/src/gateway/app.py` (apply to `PUT /v2/entries` and `POST /v2/entries/remove` only; 429 body `{"error":{"code":"rate_limited","message":"…"}}` + `Retry-After: <seconds until window reset>`), `$GW/src/gateway/config.v1.json`; Test `$GW/tests/test_intake_rate_limit.py`.
- [ ] Tests: 600 allowed, 601st → 429 with `Retry-After`; other routes unaffected; per account. Commit `feat(gateway): per-account intake write limit (§8.11)`.

### Task A18: Metrics (§8.11, W2, D3)

**Files:** Create `$MAS/metrics.py`, `$AAS/app/metrics.py`; Modify `$MAS/app.py` and `$AAS/app/main.py` (`GET /metrics`, not in any gateway route table, so not public), counters/histograms at their sites (intake routes, not-sent sweep, auto-join lag at spawn, sender, stream pending gauge, export route, token expiry gauge in admin-api), `pyproject.toml` + `Dockerfile` of both (`prometheus-client`); Test `$MA/tests/test_metrics.py`, `$AA/tests/test_metrics.py`.
- Metric names exactly as §8.11, each labelled `user_id`. `aw_api_token_expires_seconds{name}` from `api_tokens.expires_at`.
- [ ] Commit `feat(meeting-api,admin-api): Prometheus metrics for intake and webhooks (§8.11)`.

### Task A19: Settings and Helm (§8.8)

**Files:** Modify `$MAS/config.v1.json`, `$AAS/config.v1.json`, `$GW/src/gateway/config.v1.json` (every new env: `ENTRY_MAX_DAYS_AHEAD`, `JOIN_NOW_ADOPT_AHEAD_S`, `ENTRY_BLOCKED_HOSTS`, `INTAKE_MAX_ACTIVE_ENTRIES`, `WEBHOOK_PRIVATE_HOST_ALLOWLIST`, `WEBHOOK_DELIVERY_RETENTION_DAYS`, `WEBHOOK_SECRET_ENC_KEYS` (secret), `WEBHOOK_SECRET_ENC_ACTIVE_KEY`, `INTAKE_RATE_LIMIT_PER_MIN`; plus `VEXA_JITSI_HOSTS` gains `meet.abroadworks.com` in our values, handoff §6 B13), `deploy/helm/charts/vexa/values.yaml` + templates (env wiring; `WEBHOOK_SECRET_ENC_KEYS` from the existing secret `secrets.existingSecretName`), `deploy/helm/tests/test_template.sh` (render assertions — hot file: sequence, AGENTS.md), `deploy/db-budget.json` if a new engine is built (none planned: reuse the existing engines).
- [ ] `node scripts/gates.mjs config-contract` green; `bash deploy/helm/tests/test_template.sh` green. Commit `feat(helm): intake and webhook settings (§8.8)`.

### Task A20: Architecture model (§8.9, P23)

**Files:** Modify `architecture.calm.json` (node `meeting-api-intake` inside meeting-api; flows calendar-dispatcher → gateway `/v2/entries`, meeting-api → webhook subscribers, exporter → meeting-api `/internal/meetings/{id}/export`, exporter → gateway reads), regenerate `docs/views/architecture.dsl` (`pnpm arch:dsl`), `pnpm seal:arch` in its own commit; `pnpm gate:calm` (needs network for `npx`; if unavailable, say so).
- [ ] Commits `docs(arch): intake module and new flows in the CALM model (§8.9)`, `chore(seal): architecture seal (P23)`.

### Task A21: aw-bots docs (definition of done)

**Files:** Create `docs/changelog.d/<n>-aw-meeting-intake.md` (fragment; `<n>` = PR number once known, else `aw-meeting-intake`), Modify `$EX/README.md` (env names), `README.md` (fork developer README: `/v2`, webhooks), the design §12/§14 (W3 wording; D1–D6 answers; W1 name).
- [ ] Commit `docs(aw-bots): intake and webhooks — changelog fragment, README, design notes`.

---

## Part B — calendar module (`$N`, branch `feat/calendar-aw-bots`) (§9)

Old path rule: every existing test passes unchanged (≥ 524), and the `old` branch of the cycle calls exactly the same functions in the same order (a test spies the old helpers and asserts identical calls for an `old` user before and after).

### Task B1: Migration 0011 — `bot_backend` + `aw_entries` (§9.1, §9.3)

**Files:** Create `notetaker-postgres/notetaker_postgres/migrations/versions/0011_add_bot_backend_and_aw_entries.py`; Modify `notetaker-postgres/notetaker_postgres/models.py` (`CalendarConnection.bot_backend` String(16) not null server_default `'old'`, CHECK in (`old`,`aw-bots`); `AwEntry` table PK (`owner_email`, `event_id`): `meeting_uuid` UUID null, `sent_hash` Text null, `state` String(16) not null (`synced`/`rejected`/`error`), `last_error` Text null, `attempts` Integer not null default 0, `next_attempt_at` timestamptz null, `last_seen_at` timestamptz not null, `end_at` timestamptz not null, `updated_at` timestamptz); Test `notetaker-postgres/tests/test_migration.py` (0010→0011 upgrade/downgrade/re-upgrade on the existing harness), `test_models.py`.
- [ ] Commit `feat(notetaker-postgres): migration 0011 bot_backend and aw_entries (design §9.1)`.

### Task B2: aw-bots client (§9.3 auth, §5)

**Files:** Create `calendar-dispatcher/calendar_dispatcher/aw_bots_client.py`; Test `calendar-dispatcher/tests/test_aw_bots_client.py`.

```python
class AwBotsResult(NamedTuple): kind: Literal["ok","rejected","retry"]; result: str | None; meeting_uuid: str | None; code: str | None
class AwBotsClient:
    def __init__(self, base_url: str, api_key: str, *, timeout_s: float = 10.0, transport: httpx.BaseTransport | None = None) -> None
    def put_entry(self, body: dict[str, Any]) -> AwBotsResult       # 200 → ok; 400/404 → rejected(code); 429/5xx/timeout/conn → retry
    def remove_entry(self, body: dict[str, Any]) -> AwBotsResult
```
Env: `AW_BOTS_BASE_URL`, `AW_BOTS_API_KEY` (from Secret `aw-bots-key-calendar-dispatcher`, key `VEXA_API_KEY`; §8.10 supersedes §9.3's `aw-bots-portal-api-key`). The key is never logged or put in an exception message (test asserts on `caplog` and `repr`).
- [ ] Commit `feat(calendar-dispatcher): aw-bots intake client (§9.3)`.

### Task B3: Read changes for aw-bots users — horizon, `series_id`, Jitsi detection (§9.2)

**Files:** Modify `calendar-dispatcher/calendar_dispatcher/calendar_client.py` (a `list_upcoming_events(..., horizon: timedelta)` parameter used only by the aw-bots path; `CalendarEvent` gains `series_id: str | None` from `recurringEventId`; Jitsi detection behind a flag `detect_jitsi_hosts: tuple[str, ...] = ()`, scanning `conferenceData.entryPoints[].uri`, `location`, `description` — default empty, so the old path is unchanged); Test `calendar-dispatcher/tests/test_calendar_client.py` (new cases).
- Env: `AW_BOTS_HORIZON_DAYS` (14), `AW_BOTS_JITSI_HOSTS` (`meet.abroadworks.com,meet.jit.si`).
- [ ] Commit `feat(calendar-dispatcher): 14-day read, series id and Jitsi links for aw-bots users (§9.2)`.

### Task B4: Vanish confirmation with `events().get` (§9.3)

**Files:** Modify `calendar_client.py` (`confirm_vanished(creds, event_id, owner_email, horizon_end) -> Literal["cancelled","declined","deleted","moved_out_of_window","present"]`); Test (four reasons + partial read → `present` → nothing sent).
- [ ] Commit `feat(calendar-dispatcher): name the reason an event left the calendar (§9.3)`.

### Task B5: The aw-bots cycle and the per-user switch (§9.1, §9.3)

**Files:** Create `calendar-dispatcher/calendar_dispatcher/aw_bots_sync.py`; Modify `main.py` (`run_poll_cycle`: branch on `connection.bot_backend`; `old` → untouched code; `aw-bots` → `sync_user(...)`), `connections.py` (`load_active_connections` returns `bot_backend`), the Google push webhook handler (aw-bots users → trigger an immediate `sync_user`, never `_apply_webhook_cancellations`); Test `calendar-dispatcher/tests/test_aw_bots_sync.py`, `tests/test_main.py` (switch + old-path spy).
- Per D8 the aw-bots branch leaves `run_poll_cycle` before the `DISPATCH_PLATFORMS` filter (`main.py:1865`); a test proves a Teams and a Jitsi event reach `put_entry` for an aw-bots user while the same events stay filtered for an `old` user.
- `entry_body(event, owner_email) -> dict` = §6.1 shape (`external_id = "google:" + event_id`, `series_id = "google:" + recurringEventId`, `attendees` = the same list `tracked_meetings.attendee_emails` gets today); `sent_hash = sha256(canonical JSON of entry_body)`.
- Per cycle, per aw-bots user: in read & hash differs (or new, or `state='error'` past `next_attempt_at`, or full resync due every `AW_BOTS_FULL_RESYNC_HOURS`=6) → `put_entry`; ended & absent → delete row, send nothing; absent & `end_at` ahead & not seen for 5 min → `confirm_vanished` → `remove_entry(reason)` (skip on `present`), delete row. 400 → `rejected` (retried on change or resync); retry → backoff 60 s doubling to 1 h (reuse `_backoff_after_failure`).
- Writes **no** `tracked_meetings` row for aw-bots users (§9.3).
- Metrics `aw_calendar_read_total{outcome}`, `aw_calendar_entries_sent_total{result}`, `aw_calendar_cycle_seconds` (prometheus-client is already a dependency).
- [ ] Tests (§11.4): hash-based sending (unchanged event → no call); ended vs vanished; each of four reasons; partial read sends nothing; rejected vs retried; full resync re-sends all; `old` users byte-for-byte unchanged (spy). Commits `feat(calendar-dispatcher): send entries to aw-bots per user (§9.3)`, `feat(calendar-dispatcher): per-user bot_backend switch (§9.1)`.

### Task B6: Deployment manifests + docs (§8.8, §8.10, §9.3)

**Files:** Modify `deployment/base/aw-bots/values.yaml` (the §8.8 settings with their defaults, `VEXA_JITSI_HOSTS` + `meet.abroadworks.com`, `ENTRY_BLOCKED_HOSTS=meet.abroadworks.com`, `WEBHOOK_PRIVATE_HOST_ALLOWLIST=portal.notetaker.svc.cluster.local`), `deployment/base/aw-bots/aw-bots-secrets.yaml.template` (`WEBHOOK_SECRET_ENC_KEYS`, `WEBHOOK_SECRET_ENC_ACTIVE_KEY` as `"<REPLACE_ME>"` placeholders only), `deployment/base/aw-exporter/deployment.yaml` + its secret template (`GATEWAY_URL`, `EXPORTER_API_KEY` from `aw-bots-key-exporter`, `INTERNAL_API_SECRET` by `secretKeyRef` to `aw-bots-secrets`), the calendar-dispatcher Deployment (env `AW_BOTS_BASE_URL=http://aw-bots-vexa-gateway.aw-bots.svc.cluster.local:8000`, `AW_BOTS_API_KEY` from `secretKeyRef aw-bots-key-calendar-dispatcher/VEXA_API_KEY`, optional: false only once rollout step 1 is done → `optional: true` so an `old`-only deploy starts without it), `CHANGELOG.md` (meeting_key.py responsibility change §9.3; D1–D6; W1–W3).
- [ ] Commit `feat(deploy): calendar-dispatcher aw-bots settings (§9.3)`.

---

## Part C — portal (`$N/portal`) (§10.1, §10.3)

Per-user switch: the portal reads `calendar_connections.bot_backend` for the signed-in user (`lib/calendar-connections.ts`); `aw-bots` users take the new paths, everyone else is unchanged (every existing vitest test passes unchanged).

### Task C1: aw-bots server client + instant join (§10.1, §6.2)
**Files:** Create `portal/src/lib/aw-bots.ts` (server-only: `putEntry`, `removeEntry`, `getMeeting`, `listMeetings`, `stopMeeting`; `X-API-Key` from `AW_BOTS_API_KEY` = Secret `aw-bots-key-portal`), Modify `portal/src/app/api/dispatch/route.ts` (aw-bots users → `PUT /v2/entries` with `external_id = "manual:" + crypto.randomUUID()` generated once per click, `join_now: true`); Tests `portal/src/lib/aw-bots.test.ts`, `portal/src/app/api/dispatch/route.test.ts`. Commit `feat(portal): instant join through aw-bots /v2 (§10.1)`.

### Task C2: Lists and detail from `/v2` (§10.1, §10.3)
**Files:** Modify `portal/src/lib/meetings.ts`, `portal/src/lib/meeting-detail.ts` (aw-bots users → `listMeetings({user,from,to,status,cursor})`, `getMeeting(uuid)`; `platform` from the object), mapping to the existing view types; Tests. Commit `feat(portal): meeting lists and detail from aw-bots (§10.1)`.

### Task C3: Transcript and audio from `export.s3_path` (§10.1)
**Files:** Modify `portal/src/lib/s3.ts` callers (`s3PrefixFor` gets the prefix from `meeting.export.s3_path` for aw-bots meetings; no fallback to the old prefix); Tests. Commit `feat(portal): read transcripts from the aw-bots export path (§10.1)`.

### Task C4: Join progress + stop (§10.1)
**Files:** Modify `portal/src/lib/join-progress.ts`, `join-state.ts` (aw-bots → `GET /v2/meetings/{id}`; 2.5 s poll kept only as the SSE fallback, C5), stop action → `POST /v2/meetings/{id}/stop`; Tests. Commit `feat(portal): join progress and stop through aw-bots (§10.1)`.

### Task C5: Webhook receiver + SSE (§10.1, §7.4)
**Files:** Create `portal/src/app/api/webhooks/aw-bots/route.ts` (verify `X-Webhook-Signature` over the raw body with `AW_BOTS_WEBHOOK_SECRET`, accept either signature, reject > 300 s skew, dedupe `SET NX EX 86400` on `aw:evt:<event_id>`, `PUBLISH aw:meeting:<uuid>`), `portal/src/app/api/meetings/stream/route.ts` (SSE; `ids` query limited to meetings the signed-in user can see — checked through `getMeeting` visibility, never trusted from the query), `portal/src/lib/redis.ts` (`ioredis`, D3), a client hook `useMeetingStream(ids)` with 30 s `GET /v2/meetings/{id}` fallback while disconnected; Modify `portal/package.json` (`ioredis`), `portal/ui-k8s/*` (env `REDIS_URL=redis://notetaker-redis-master.notetaker.svc.cluster.local:6379/0`, `AW_BOTS_BASE_URL`, secrets by name); Tests (signature good/bad/stale/rotation pair; dedupe; SSE authorisation refuses another user's id; fallback on disconnect). Commit `feat(portal): aw-bots webhook receiver and live updates over SSE (§10.1)`.

---

## Part D — docs, verification, report

### Task D1: aw-notetaker docs (CLAUDE.md "definition of done")
Update `CLAUDE.md` (Phase awareness: intake built, not deployed; repository section), `docs/phases/README.md` row, the current phase playbook (AW Bots lives in the productization track — update the playbook the README index names for it), `docs/SYSTEM_BRAIN.md` (module inventory, flows, data flow), `CHANGELOG.md`, `deployment/base/aw-bots/README.md` (new runbook steps: **9** MIGRATION-0008 step 1; **10** deploy; **11** MIGRATION-0008 step 3; **12** revoke tokens 1 and 2 and mint `calendar-dispatcher`, `portal`, `exporter` keys into `aw-bots-key-calendar-dispatcher`, `aw-bots-key-portal` (namespace `notetaker`), `aw-bots-key-exporter` (namespace `aw-bots`) — commands print no key, same pattern as step 8; **13** `WEBHOOK_SECRET_ENC_KEYS`/`_ACTIVE_KEY` generated into `aw-bots-secrets` without display; **14** portal webhook subscription; **15** migration 0011 on notetaker-postgres). Commit `docs: AW Bots intake — runbook steps, system brain, phases, changelog`.

### Task D2: Whole-branch verification
- `cd $V && PYTHONDONTWRITEBYTECODE=1 node scripts/gates.mjs all` — record every gate's line. The `stack`, `compose*`, `eval*` gates need Docker and `deploy/compose/.env` (minted by `deploy/compose/mint-dev-env.sh`, which the human runs; the file is never read by the session). If those can't run, the report says which gates ran and which didn't, and why.
- Per package: pytest counts vs baseline; ruff/mypy counts vs baseline; black on new files.
- `superpowers:requesting-code-review` over each branch; fix; re-run.

### Verification commands (copy-paste)

```bash
V=/Applications/XAMPP/xamppfiles/htdocs/mike/vexa-meeting-intake; N=/Applications/XAMPP/xamppfiles/htdocs/mike/aw-notetaker-calendar-aw-bots
export PYTHONDONTWRITEBYTECODE=1
for d in core/meetings/services/meeting-api core/identity/services/admin-api core/gateway/services/gateway integrations/out/aw-notetaker; do (cd $V/$d && uv run pytest -q -p no:cacheprovider | tail -1); done
cd $V && node scripts/gates.mjs all
for d in calendar-dispatcher notetaker-postgres; do (cd $N/$d && ../.venv/bin/python -m pytest -q -p no:cacheprovider | tail -1 && ../.venv/bin/black --check . && ../.venv/bin/ruff check . && ../.venv/bin/mypy .); done
cd $N/portal && npx vitest run && npx tsc --noEmit && npx next lint
```

## What the human runs, in order (after review and merge; nothing here is run by a session)

1. aw-bots: MIGRATION-0008 **step 1** (new indexes + `uuid`, CONCURRENTLY) on the live aw-bots Postgres.
2. Build images (CI), pin tags in `deployment/base/aw-bots/values.yaml` + exporter manifest, add `WEBHOOK_SECRET_ENC_KEYS`/`_ACTIVE_KEY` to `aw-bots-secrets`, `helm upgrade` (meeting-api → bot → exporter order, runbook "Upgrading").
3. MIGRATION-0008 **step 3** (drop the old index CONCURRENTLY).
4. Revoke tokens id 1 and 2; mint the three named keys into their Secrets (runbook step 12); roll the exporter.
5. Create the portal's webhook subscription (runbook step 14).
6. notetaker-postgres migration **0011** (`alembic upgrade head` from a checkout, as for 0008/0009).
7. Deploy calendar-dispatcher + portal with every user on `old`.
8. Pilot: set the owner's `bot_backend='aw-bots'`; live tests §11.3.
