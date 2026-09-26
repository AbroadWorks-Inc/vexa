"""§1.2 — real-Postgres proofs for the intake/webhook schema and MIGRATION-0008.

Why these live here and not in admin-api: the task brief assigns this file to meeting-api's test
lane, and meeting-api is where `MEETING_API_TEST_DATABASE_URL` real-Postgres conformance already
lives (`tests/test_single_flight.py`) — this file follows that exact skip-cleanly-when-unset
pattern. It drives `admin_api.schema.sync.ensure_schema` / `admin_api.schema.models` directly (the
DB source-of-truth) over the PRE-ALLOWED `meeting_api → admin_api` edge
(`scripts/check-isolation-py.mjs` `ALLOWED_EDGES`), reached via the `pythonpath` entry this task
added to `pyproject.toml` (the same wiring `gateway/services/conformance` already uses to reach
`meeting_api`'s `src`).

**Local/dev environment note** (read before "why did this collect-error"): both `sqlalchemy` and
`asyncpg` are genuinely absent from this package's own `pyproject.toml`/`uv.lock` — by design, the
same way `meeting_api.sessions.models` is imported lazily only by the production `bot_spawn` /
`recordings` / `collector.adapters` paths, never by the (fakes-only) offline test suite. The
Dockerfile installs the real driver stack at image-build time (`sqlalchemy[asyncio]==2.0.36`,
`asyncpg==0.30.0` — see `Dockerfile`'s "Production-only runtime deps" step), not via `uv sync`. To
actually run this file (as opposed to letting it skip cleanly), install those exact pins into this
package's local venv first — this does NOT touch `pyproject.toml`/`uv.lock`, so a subsequent clean
`uv sync` reverts to the lean offline set:

    uv pip install "sqlalchemy[asyncio]==2.0.36" "asyncpg==0.30.0"

Then, with the throwaway Postgres running (constraints.md):

    MEETING_API_TEST_DATABASE_URL=postgresql+asyncpg://postgres:test@localhost:55432/postgres \\
      PYTHONDONTWRITEBYTECODE=1 uv run pytest -q tests/test_intake_pg_schema.py
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("MEETING_API_TEST_DATABASE_URL"),
    reason="real-Postgres proofs for §1.2 intake schema; set MEETING_API_TEST_DATABASE_URL to run",
)

sqlalchemy = pytest.importorskip("sqlalchemy", reason="see this module's docstring")
pytest.importorskip("asyncpg", reason="see this module's docstring")

from sqlalchemy import inspect, text  # noqa: E402
from sqlalchemy.dialects import postgresql  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402
from sqlalchemy.schema import CreateTable  # noqa: E402

from admin_api.schema import models as admin_models  # noqa: E402
from admin_api.schema import sync as admin_sync  # noqa: E402

DB_URL = os.environ.get("MEETING_API_TEST_DATABASE_URL", "")
MIGRATION_DOC = (
    Path(admin_models.__file__).parent / "MIGRATION-0008-meeting-live-dedup-index.md"
)

NEW_TABLE_CLASSES = [
    admin_models.MeetingEntry,
    admin_models.MeetingAwState,
    admin_models.WebhookSubscription,
    admin_models.WebhookOutbox,
    admin_models.WebhookDelivery,
    admin_models.WebhookDeliveryAttempt,
]

LIVE_STATUSES = (
    "requested",
    "joining",
    "awaiting_admission",
    "needs_help",
    "active",
    "stopping",
)

# The pre-migration `meetings` shape (a minimal analog of the columns MIGRATION-0008 actually
# touches — this is what a live prod `meetings` table looks like BEFORE this migration runs).
OLD_MEETINGS_TABLE_SQL = """
CREATE TABLE meetings (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL,
    platform VARCHAR(100) NOT NULL,
    platform_specific_id VARCHAR(255),
    status VARCHAR(50) NOT NULL,
    data JSONB NOT NULL DEFAULT '{}'::jsonb,
    start_time TIMESTAMP,
    created_at TIMESTAMP DEFAULT now(),
    updated_at TIMESTAMP DEFAULT now()
)
"""
OLD_DEDUP_INDEX_SQL = """
CREATE UNIQUE INDEX uq_meeting_active_user_platform_native
ON meetings (user_id, platform, platform_specific_id)
WHERE status NOT IN ('completed', 'failed')
"""

# MIGRATION-0008 steps, verbatim (also asserted, below, to be substrings of the runbook doc itself
# — the drift guard: if the doc and this file diverge, that assertion fails).
STEP_1_2_ADD_UUID = "ALTER TABLE meetings ADD COLUMN uuid uuid;"
STEP_1_3_BACKFILL = """DO $$
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
END $$;"""
STEP_1_4_SET_DEFAULT = (
    "ALTER TABLE meetings ALTER COLUMN uuid SET DEFAULT gen_random_uuid();"
)
STEP_1_5_UUID_INDEX = "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS ix_meetings_uuid ON meetings (uuid);"
STEP_1_6_NOT_NULL = [
    "ALTER TABLE meetings ADD CONSTRAINT ck_meetings_uuid_not_null CHECK (uuid IS NOT NULL) NOT VALID;",
    "ALTER TABLE meetings VALIDATE CONSTRAINT ck_meetings_uuid_not_null;",
    "ALTER TABLE meetings ALTER COLUMN uuid SET NOT NULL;",
    "ALTER TABLE meetings DROP CONSTRAINT ck_meetings_uuid_not_null;",
]
STEP_1_7_LIVE_DEDUP_INDEX = """CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_meeting_live_user_platform_native
ON meetings (user_id, platform, platform_specific_id)
WHERE status IN ('requested', 'joining', 'awaiting_admission', 'needs_help', 'active', 'stopping');"""
STEP_1_8_DUE_INDEX = """CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_meeting_scheduled_due
ON meetings (meeting_event_time(data, start_time, created_at))
WHERE status = 'scheduled';"""
STEP_3_DROP_OLD_INDEX = (
    "DROP INDEX CONCURRENTLY IF EXISTS uq_meeting_active_user_platform_native;"
)

PRECHECK_SQL = """SELECT user_id, platform, platform_specific_id, count(*) AS live_dups,
       array_agg(id ORDER BY created_at DESC, id DESC) AS meeting_ids
FROM meetings
WHERE status IN ('requested', 'joining', 'awaiting_admission', 'needs_help', 'active', 'stopping')
  AND platform_specific_id IS NOT NULL
GROUP BY user_id, platform, platform_specific_id
HAVING count(*) > 1;"""


@pytest.fixture()
async def engine():
    eng = create_async_engine(DB_URL)
    async with eng.begin() as conn:
        await conn.run_sync(admin_models.Base.metadata.drop_all)
    yield eng
    async with eng.begin() as conn:
        await conn.run_sync(admin_models.Base.metadata.drop_all)
    await eng.dispose()


async def _table_names(conn) -> set[str]:
    return set(
        await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_table_names())
    )


async def _run_autocommit(engine, *statements: str) -> None:
    """`CREATE/DROP INDEX CONCURRENTLY` refuse to run inside a transaction block."""
    conn = await engine.connect()
    conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
    try:
        for stmt in statements:
            await conn.execute(text(stmt))
    finally:
        await conn.close()


# ── the migration doc drift guard ────────────────────────────────────────────────────────────


def test_migration_doc_new_table_sql_matches_the_models():
    """Step 1.1's CREATE TABLE SQL in the .md must be the models' OWN compiled DDL, verbatim."""
    doc_text = MIGRATION_DOC.read_text()
    for cls in NEW_TABLE_CLASSES:
        sql = (
            str(
                CreateTable(cls.__table__).compile(dialect=postgresql.dialect())
            ).strip()
            + ";"
        )
        assert (
            sql in doc_text
        ), f"{cls.__table__.name}: doc's SQL has drifted from the model"


@pytest.mark.parametrize(
    "step",
    [
        STEP_1_2_ADD_UUID,
        STEP_1_3_BACKFILL,
        STEP_1_4_SET_DEFAULT,
        STEP_1_5_UUID_INDEX,
        *STEP_1_6_NOT_NULL,
        STEP_1_7_LIVE_DEDUP_INDEX,
        STEP_1_8_DUE_INDEX,
        STEP_3_DROP_OLD_INDEX,
        PRECHECK_SQL,
    ],
)
def test_migration_doc_contains_every_step_verbatim(step):
    assert step in MIGRATION_DOC.read_text()


# ── ensure_schema on an empty DB ─────────────────────────────────────────────────────────────


async def test_ensure_schema_converges_cleanly_on_an_empty_db(engine):
    await admin_sync.ensure_schema(engine, admin_models.Base)
    async with engine.begin() as conn:
        names = await _table_names(conn)
    for name in (
        "meetings",
        "meeting_entries",
        "meeting_aw_state",
        "webhook_subscriptions",
        "webhook_outbox",
        "webhook_deliveries",
        "webhook_delivery_attempts",
    ):
        assert name in names

    async with engine.begin() as conn:
        row = (
            await conn.execute(
                text(
                    "INSERT INTO meetings (user_id, platform, status) "
                    "VALUES (1, 'google_meet', 'requested') RETURNING uuid"
                )
            )
        ).one()
    assert row.uuid is not None


# ── live-only dedup: two scheduled + one active is fine; a second active raises ─────────────


async def test_two_scheduled_rows_are_fine_but_a_second_live_row_raises(engine):
    await admin_sync.ensure_schema(engine, admin_models.Base)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO meetings (user_id, platform, platform_specific_id, status) VALUES "
                "(1, 'google_meet', 'abc-123', 'scheduled')"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO meetings (user_id, platform, platform_specific_id, status) VALUES "
                "(1, 'google_meet', 'abc-123', 'scheduled')"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO meetings (user_id, platform, platform_specific_id, status) VALUES "
                "(1, 'google_meet', 'abc-123', 'active')"
            )
        )

    with pytest.raises(IntegrityError):
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO meetings (user_id, platform, platform_specific_id, status) VALUES "
                    "(1, 'google_meet', 'abc-123', 'joining')"
                )
            )


# ── RESTRICT on an entry-managed meeting; CASCADE on an entry-less one ──────────────────────


async def test_restrict_blocks_delete_when_a_meeting_entry_exists(engine):
    await admin_sync.ensure_schema(engine, admin_models.Base)
    async with engine.begin() as conn:
        meeting_id = (
            await conn.execute(
                text(
                    "INSERT INTO meetings (user_id, platform, status) VALUES (1, 'google_meet', 'scheduled') "
                    "RETURNING id"
                )
            )
        ).scalar_one()
        await conn.execute(
            text(
                "INSERT INTO meeting_entries (user_id, source_user, external_id, meeting_id, "
                "meeting_url, platform, native_meeting_id, start_at, content_hash, state) VALUES "
                "(1, 'me@example.com', 'evt-1', :meeting_id, 'https://meet.example/x', 'google_meet', "
                "'abc-123', now(), 'hash', 'active')"
            ),
            {"meeting_id": meeting_id},
        )

    with pytest.raises(IntegrityError):
        async with engine.begin() as conn:
            await conn.execute(
                text("DELETE FROM meetings WHERE id = :id"), {"id": meeting_id}
            )


async def test_cascade_removes_aw_state_when_the_meeting_is_deleted(engine):
    await admin_sync.ensure_schema(engine, admin_models.Base)
    async with engine.begin() as conn:
        meeting_id = (
            await conn.execute(
                text(
                    "INSERT INTO meetings (user_id, platform, status) VALUES (1, 'google_meet', 'scheduled') "
                    "RETURNING id"
                )
            )
        ).scalar_one()
        await conn.execute(
            text("INSERT INTO meeting_aw_state (meeting_id) VALUES (:meeting_id)"),
            {"meeting_id": meeting_id},
        )

    async with engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM meetings WHERE id = :id"), {"id": meeting_id}
        )

    async with engine.begin() as conn:
        remaining = (
            await conn.execute(
                text("SELECT count(*) FROM meeting_aw_state WHERE meeting_id = :id"),
                {"id": meeting_id},
            )
        ).scalar_one()
    assert remaining == 0


# ── EXPLAIN uses the due index ───────────────────────────────────────────────────────────────


async def test_explain_due_query_uses_the_scheduled_due_index(engine):
    await admin_sync.ensure_schema(engine, admin_models.Base)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO meetings (user_id, platform, status, start_time) VALUES "
                "(1, 'google_meet', 'scheduled', now())"
            )
        )
        # A tiny table always costs less as a seq scan; force the planner to prefer an index plan
        # so this proves the index is USABLE for the query shape, not that Postgres picks it here.
        await conn.execute(text("SET LOCAL enable_seqscan = off"))
        plan = "\n".join(
            row[0]
            for row in (
                await conn.execute(
                    text(
                        "EXPLAIN SELECT id FROM meetings WHERE status = 'scheduled' "
                        "ORDER BY meeting_event_time(data, start_time, created_at) LIMIT 10"
                    )
                )
            ).all()
        )
    assert "ix_meeting_scheduled_due" in plan, plan


# ── MIGRATION-0008 applied to a dirty (old-shape, populated) DB ─────────────────────────────


async def test_migration_0008_applied_to_a_dirty_db(engine):
    async with engine.begin() as conn:
        await conn.execute(text(OLD_MEETINGS_TABLE_SQL))
        await conn.execute(text(OLD_DEDUP_INDEX_SQL))
        # meeting_event_time() is already live in prod (MIGRATION-0005) — simulate that here with
        # the SAME function admin-api's own convergence creates (no local re-authoring of the SQL).
        await conn.execute(text(admin_sync._MEETING_EVENT_TIME_FN))

    async with engine.begin() as conn:
        for i in range(3):
            await conn.execute(
                text(
                    "INSERT INTO meetings (user_id, platform, platform_specific_id, status) "
                    "VALUES (:u, 'google_meet', :native, 'scheduled')"
                ),
                {"u": i, "native": f"native-{i}"},
            )

    # Pre-check: no link has two live rows (expected zero on this fixture).
    async with engine.begin() as conn:
        dup_rows = (await conn.execute(text(PRECHECK_SQL))).all()
    assert dup_rows == []

    # Step 1.1 — the six new tables.
    async with engine.begin() as conn:
        for cls in NEW_TABLE_CLASSES:
            sql = str(CreateTable(cls.__table__).compile(dialect=postgresql.dialect()))
            await conn.execute(text(sql))

    # Step 1.2 — add the column (nullable).
    async with engine.begin() as conn:
        await conn.execute(text(STEP_1_2_ADD_UUID))

    # Step 1.3 — the backfill DO block issues its own internal COMMIT per batch, which Postgres
    # only allows outside an explicit transaction block (autocommit, like the doc itself notes).
    await _run_autocommit(engine, STEP_1_3_BACKFILL)

    # Step 1.4 — default new rows going forward.
    async with engine.begin() as conn:
        await conn.execute(text(STEP_1_4_SET_DEFAULT))

    # Step 1.5, 1.7, 1.8 — CONCURRENTLY builds (autocommit, one statement per connection use).
    await _run_autocommit(engine, STEP_1_5_UUID_INDEX)
    await _run_autocommit(engine, STEP_1_7_LIVE_DEDUP_INDEX)
    await _run_autocommit(engine, STEP_1_8_DUE_INDEX)

    # Step 1.6 — NOT NULL via a validated CHECK constraint.
    async with engine.begin() as conn:
        for stmt in STEP_1_6_NOT_NULL:
            await conn.execute(text(stmt))

    # Step 3 — drop the old index (post-deploy, but nothing here depends on a real deploy step).
    await _run_autocommit(engine, STEP_3_DROP_OLD_INDEX)

    async with engine.begin() as conn:
        uuids = [
            r[0] for r in (await conn.execute(text("SELECT uuid FROM meetings"))).all()
        ]
        assert len(uuids) == 3
        assert all(u is not None for u in uuids)
        assert len(set(uuids)) == 3  # distinct

        not_null = (
            await conn.execute(
                text(
                    "SELECT is_nullable FROM information_schema.columns "
                    "WHERE table_name = 'meetings' AND column_name = 'uuid'"
                )
            )
        ).scalar_one()
        assert not_null == "NO"

        valid = dict(
            (
                await conn.execute(
                    text(
                        "SELECT indexrelid::regclass::text, indisvalid FROM pg_index "
                        "WHERE indexrelid::regclass::text = ANY(:names)"
                    ),
                    {
                        "names": [
                            "ix_meetings_uuid",
                            "uq_meeting_live_user_platform_native",
                            "ix_meeting_scheduled_due",
                        ]
                    },
                )
            ).all()
        )
        assert valid == {
            "ix_meetings_uuid": True,
            "uq_meeting_live_user_platform_native": True,
            "ix_meeting_scheduled_due": True,
        }

        old_index_gone = (
            await conn.execute(
                text("SELECT to_regclass('uq_meeting_active_user_platform_native')")
            )
        ).scalar_one()
        assert old_index_gone is None
