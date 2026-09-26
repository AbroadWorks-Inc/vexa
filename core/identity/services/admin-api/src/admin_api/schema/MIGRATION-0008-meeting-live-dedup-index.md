# MIGRATION-0008 — meeting `uuid`, intake + webhook tables, the live-only dedup index, the due index (§1.2)

**Status:** `meetings.uuid` + the two new `meetings` indexes, and the six intake/webhook tables,
added to the SSOT model (`schema/models.py`) + the meeting-api mirror
(`meeting-api/.../sessions/models.py`). On an **existing** prod/staging DB the `meetings` changes
are an **out-of-band ops step that MUST precede the deploy** — see "Production rollout" below.
Fresh/empty DBs (tests, new envs) converge cleanly via `ensure_schema` and need no manual step; the
six new tables likewise build cleanly on any DB via `ensure_schema`'s additive `create_all`, they
are only listed here so the runbook can be run as one pass.

## Why

**The six new tables** (`meeting_entries`, `meeting_aw_state`, `webhook_subscriptions`,
`webhook_outbox`, `webhook_deliveries`, `webhook_delivery_attempts`) are ours — no upstream parent
had them. They need no dedup runbook: they are new tables with no existing rows, so
`ensure_schema`'s ordinary `create_all` builds them (and every index on them) cleanly, with no
lock-contention hazard. They are included in this migration's step 1 only so an operator can run
the whole pass in one script; nothing about them is order-sensitive except that `webhook_outbox`
must exist before `webhook_deliveries`, and `webhook_deliveries` before
`webhook_delivery_attempts` (their FKs).

**`meetings.uuid`** is the stable external identity every intake/webhook payload keys off (never
the internal `id`). Added nullable, backfilled in batches, then locked down via a validated CHECK
constraint — never a blocking `SET NOT NULL` — so the live `meetings` table is never held under an
exclusive lock for the backfill's duration.

**The index swap** (`uq_meeting_active_user_platform_native` → `uq_meeting_live_user_platform_native`).
The old unique index allowed only one **non-finished** row per `(user, platform,
platform_specific_id)`. Intake now writes many `meeting_entries` rows per link for a recurring
series, each producing (or reusing) a `scheduled` `meetings` row — so with the old predicate, a
second occurrence of the same recurring meeting could never get its own `scheduled` row. Only a
**live** (bot-lifecycle) row may be unique per link now; `'scheduled'` is deliberately NOT in the
new predicate.

`ensure_schema` matches indexes **by name** and never alters one in place, and a failed unique
index stops admin-api (`sync.py:93-142` raises `SchemaInvariantError`) — so the swap cannot ride an
in-band `ensure_schema` pass against a populated table. It is this manual runbook, run **before**
the deploy (Part 5).

**The due index** (`ix_meeting_scheduled_due`) is the scheduler's due query: only meetings still
`scheduled`, ordered by the same `meeting_event_time()` wrapper the event-order indexes use
(MIGRATION-0005, already live) — restricted so the scheduler never walks history. It reuses that
already-live function; this migration does not create it.

## Pre-check — no link has two live rows (read-only)

`platform_specific_id IS NOT NULL` because a partial unique index treats NULLs as DISTINCT — rows
with a NULL native id never collide and are irrelevant here.

```sql
SELECT user_id, platform, platform_specific_id, count(*) AS live_dups,
       array_agg(id ORDER BY created_at DESC, id DESC) AS meeting_ids
FROM meetings
WHERE status IN ('requested', 'joining', 'awaiting_admission', 'needs_help', 'active', 'stopping')
  AND platform_specific_id IS NOT NULL
GROUP BY user_id, platform, platform_specific_id
HAVING count(*) > 1;
```

Must return **zero rows** — the old index already enforces uniqueness across every non-finished
status (a superset of the new predicate's live statuses), so this is expected to already hold.
If it does not, resolve the duplicates (as MIGRATION-0002's step 2) before continuing.

## Production rollout (run in this ORDER, before deploying the SSOT change)

Run against prod via `kubectl -n aw-bots exec -it <postgres pod> -- psql -U postgres -d vexa`, as
**standalone statements** (no `BEGIN` — `CONCURRENTLY` refuses transaction blocks).

### Step 1.1 — the six new tables

SQL generated verbatim from the models (`CreateTable(...).compile(dialect=postgresql.dialect())`),
pasted unedited — see `tests/test_intake_pg_schema.py::test_migration_new_table_sql_matches_models`
for the drift guard that keeps this block honest against the models.

```sql
CREATE TABLE meeting_entries (
	id BIGSERIAL NOT NULL, 
	user_id INTEGER NOT NULL, 
	source_user TEXT NOT NULL, 
	external_id VARCHAR(255) NOT NULL, 
	meeting_id INTEGER NOT NULL, 
	meeting_url TEXT NOT NULL, 
	platform VARCHAR(100) NOT NULL, 
	native_meeting_id VARCHAR(255) NOT NULL, 
	title VARCHAR(512), 
	start_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	end_at TIMESTAMP WITH TIME ZONE, 
	time_zone TEXT, 
	series_id VARCHAR(255), 
	attendees TEXT[], 
	join_now BOOLEAN DEFAULT false NOT NULL, 
	metadata JSONB, 
	content_hash VARCHAR(64) NOT NULL, 
	state VARCHAR(16) NOT NULL, 
	removed_reason TEXT, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now(), 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now(), 
	removed_at TIMESTAMP WITH TIME ZONE, 
	closed_at TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_meeting_entries_user_source_external UNIQUE (user_id, source_user, external_id), 
	FOREIGN KEY(meeting_id) REFERENCES meetings (id) ON DELETE RESTRICT
);

CREATE TABLE meeting_aw_state (
	meeting_id INTEGER NOT NULL, 
	scheduled_end_at TIMESTAMP WITH TIME ZONE, 
	time_zone TEXT, 
	event_seq BIGINT DEFAULT '0' NOT NULL, 
	outcome_kind TEXT, 
	outcome_detail TEXT, 
	outcome_message TEXT, 
	outcome_at TIMESTAMP WITH TIME ZONE, 
	last_error_code TEXT, 
	last_error_message TEXT, 
	waiting_for_room_sent_at TIMESTAMP WITH TIME ZONE, 
	export_state TEXT, 
	export_s3_path TEXT, 
	export_error TEXT, 
	export_at TIMESTAMP WITH TIME ZONE, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now(), 
	PRIMARY KEY (meeting_id), 
	FOREIGN KEY(meeting_id) REFERENCES meetings (id) ON DELETE CASCADE
);

CREATE TABLE webhook_subscriptions (
	id UUID DEFAULT gen_random_uuid() NOT NULL, 
	user_id INTEGER NOT NULL, 
	url TEXT NOT NULL, 
	secret_enc BYTEA NOT NULL, 
	enc_key_id VARCHAR(64) NOT NULL, 
	secret_last4 VARCHAR(4) NOT NULL, 
	previous_secret_enc BYTEA, 
	previous_enc_key_id VARCHAR(64), 
	previous_secret_expires_at TIMESTAMP WITH TIME ZONE, 
	events TEXT[] DEFAULT '{}'::text[] NOT NULL, 
	active BOOLEAN DEFAULT true NOT NULL, 
	description TEXT, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now(), 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now(), 
	PRIMARY KEY (id)
);

CREATE TABLE webhook_outbox (
	event_id VARCHAR(80) NOT NULL, 
	meeting_id INTEGER, 
	event_type VARCHAR(64) NOT NULL, 
	sequence BIGINT NOT NULL, 
	payload_text TEXT NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now(), 
	published_at TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (event_id), 
	FOREIGN KEY(meeting_id) REFERENCES meetings (id) ON DELETE CASCADE
);

CREATE TABLE webhook_deliveries (
	id BIGSERIAL NOT NULL, 
	event_id VARCHAR(80) NOT NULL, 
	subscription_id UUID NOT NULL, 
	user_id INTEGER NOT NULL, 
	state VARCHAR(16) NOT NULL, 
	attempts INTEGER DEFAULT '0' NOT NULL, 
	next_attempt_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	lease_until TIMESTAMP WITH TIME ZONE, 
	last_status_code INTEGER, 
	last_error TEXT, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now(), 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now(), 
	PRIMARY KEY (id), 
	CONSTRAINT uq_webhook_deliveries_event_subscription UNIQUE (event_id, subscription_id), 
	FOREIGN KEY(event_id) REFERENCES webhook_outbox (event_id) ON DELETE CASCADE
);

CREATE TABLE webhook_delivery_attempts (
	id BIGSERIAL NOT NULL, 
	delivery_id BIGINT NOT NULL, 
	attempt INTEGER NOT NULL, 
	outcome VARCHAR(16) NOT NULL, 
	status_code INTEGER, 
	error TEXT, 
	duration_ms INTEGER, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now(), 
	PRIMARY KEY (id), 
	FOREIGN KEY(delivery_id) REFERENCES webhook_deliveries (id) ON DELETE CASCADE
);
```

(Every other index on these six tables — `ix_meeting_entries_meeting_id`,
`ix_meeting_entries_user_platform_native_state`, `ix_meeting_entries_attendees_gin`,
`ix_meeting_entries_active_user`, `ix_webhook_subscriptions_user_id`,
`ix_webhook_outbox_unpublished`, `ix_webhook_deliveries_due`,
`ix_webhook_deliveries_subscription_created_at`, `ix_webhook_delivery_attempts_delivery_id`,
`ix_webhook_delivery_attempts_created_at` — builds cleanly via `ensure_schema` at deploy time;
these are brand-new, empty tables, so an in-band `CREATE INDEX` takes no meaningful lock.)

### Step 1.2 — `meetings.uuid`, nullable

```sql
ALTER TABLE meetings ADD COLUMN uuid uuid;
```

### Step 1.3 — backfill in batches of 1000

```sql
DO $$
DECLARE
  rows_updated integer;
BEGIN
  LOOP
    UPDATE meetings SET uuid = gen_random_uuid()
    WHERE id IN (
      SELECT id FROM meetings WHERE uuid IS NULL LIMIT 1000
    );
    GET DIAGNOSTICS rows_updated = ROW_COUNT;
    EXIT WHEN rows_updated = 0;
    COMMIT;
  END LOOP;
END $$;
```

(Run via `psql`'s own top-level `\i` or as a single non-interactive statement — the `COMMIT` inside
the `DO` block requires a plain session, not an explicit surrounding transaction.)

### Step 1.4 — default new rows going forward

```sql
ALTER TABLE meetings ALTER COLUMN uuid SET DEFAULT gen_random_uuid();
```

### Step 1.5 — the unique index, without locking writes

The exact name SQLAlchemy generates for `Column(unique=True, index=True)` on `meetings.uuid`:

```sql
CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS ix_meetings_uuid ON meetings (uuid);
```

### Step 1.6 — `NOT NULL`, without a blocking full-table scan

```sql
ALTER TABLE meetings ADD CONSTRAINT ck_meetings_uuid_not_null CHECK (uuid IS NOT NULL) NOT VALID;
ALTER TABLE meetings VALIDATE CONSTRAINT ck_meetings_uuid_not_null;
ALTER TABLE meetings ALTER COLUMN uuid SET NOT NULL;
ALTER TABLE meetings DROP CONSTRAINT ck_meetings_uuid_not_null;
```

`VALIDATE CONSTRAINT` takes only a `SHARE UPDATE EXCLUSIVE` lock (reads/writes proceed); with a
valid `CHECK (col IS NOT NULL)` already proven, `SET NOT NULL` (PG 12+) is a metadata-only change
and does not re-scan the table. The helper CHECK is then redundant with the column's own `NOT NULL`
and is dropped.

### Step 1.7 — the live-link dedup index, without locking writes

```sql
CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_meeting_live_user_platform_native
ON meetings (user_id, platform, platform_specific_id)
WHERE status IN ('requested', 'joining', 'awaiting_admission', 'needs_help', 'active', 'stopping');
```

### Step 1.8 — the due index, without locking writes

Reuses `meeting_event_time()` (already live — MIGRATION-0005); this migration does not create it.

```sql
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_meeting_scheduled_due
ON meetings (meeting_event_time(data, start_time, created_at))
WHERE status = 'scheduled';
```

A failed `CONCURRENTLY` build leaves an **INVALID** index — check and drop-retry:

```sql
SELECT indexrelid::regclass FROM pg_index WHERE NOT indisvalid;
-- if present: DROP INDEX CONCURRENTLY <name>;  then re-run that step.
```

## Step 2 — the deploy

With `meetings.uuid`, the two new `meetings` indexes, and the six new tables already present and
committed, admin-api's boot `ensure_schema` finds all of it in the existing-table/-column/-index
sets (matched by name) and no-ops on every one of them.

**Deploy-order hazard.** The OLD index (`uq_meeting_active_user_platform_native`, predicate `status
NOT IN ('completed', 'failed')`) is still present through step 1 and 2 — it is a SUPERSET
constraint that also blocks a second `'scheduled'` row per link. A recurring series therefore
cannot get its second `scheduled` occurrence until the OLD index is gone (step 3). Do not treat
step 3 as optional cleanup: intake's multi-occurrence write path is only truly unblocked once it
runs.

## Step 3 — drop the old index, without locking writes

Run only **after** step 2 (the new code + new index have been live and healthy for a while):

```sql
DROP INDEX CONCURRENTLY IF EXISTS uq_meeting_active_user_platform_native;
```

## Verify

```sql
\d meetings
SELECT indexrelid::regclass, indisvalid FROM pg_index
WHERE indexrelid::regclass::text IN (
  'ix_meetings_uuid', 'uq_meeting_live_user_platform_native', 'ix_meeting_scheduled_due'
);
-- expect indisvalid = true for all three, and uuid NOT NULL in \d's output.
```

## Rollback

**Before step 3:** free — the old index is still in place and enforcing the old (superset)
invariant, so reverting the code deploy alone is sufficient. The new column/indexes/tables can be
left in place (harmless, unused by the old code) or dropped:

```sql
DROP INDEX CONCURRENTLY IF EXISTS ix_meeting_scheduled_due;
DROP INDEX CONCURRENTLY IF EXISTS uq_meeting_live_user_platform_native;
DROP INDEX CONCURRENTLY IF EXISTS ix_meetings_uuid;
ALTER TABLE meetings DROP COLUMN IF EXISTS uuid;
```

**After step 3:** the old index is gone; recreate it (out-of-band, `CONCURRENTLY`, same predicate as
MIGRATION-0002) before or as part of reverting the code deploy, so the ROB1/ROB2 backstop is never
absent while the old code (which relies on it) is live.
