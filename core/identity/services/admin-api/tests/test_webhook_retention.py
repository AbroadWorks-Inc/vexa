"""Delivery retention (§1.13): a daily single-flight sweep in admin-api.

It deletes deliveries in a final state older than ``WEBHOOK_DELIVERY_RETENTION_DAYS`` (their
attempts cascade), then published outbox rows with no deliveries left, each in batches of
``WEBHOOK_DELIVERY_RETENTION_BATCH_SIZE`` rows, at most ``WEBHOOK_DELIVERY_RETENTION_MAX_BATCHES``
batches of each a run. Unpublished outbox rows are never pruned. The deletes run against a real Postgres when ``MEETING_API_TEST_DATABASE_URL`` is
set; the loop and its wiring run offline.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from admin_api.app import metrics, retention
from admin_api.app.retention import (
    FINAL_STATES,
    RetentionResult,
    attach_retention,
    retention_batches_from_env,
    retention_days_from_env,
    retention_loop,
)

PG_URL = os.getenv("MEETING_API_TEST_DATABASE_URL")
NOW = datetime(2026, 9, 27, 3, 0, tzinfo=timezone.utc)
OLD = NOW - timedelta(days=31)
RECENT = NOW - timedelta(days=29)


# ── offline: settings, the loop, the wiring ─────────────────────────────────────────────────


def test_the_final_states_are_the_ones_a_delivery_never_leaves():
    assert set(FINAL_STATES) == {"delivered", "failed", "dead", "cancelled"}


def test_retention_days_default_to_30_and_read_the_setting():
    assert retention_days_from_env({}) == 30
    assert retention_days_from_env({"WEBHOOK_DELIVERY_RETENTION_DAYS": "7"}) == 7


@pytest.mark.parametrize("value", ["0", "-1", "soon"])
def test_a_retention_that_is_not_a_positive_number_of_days_is_refused(value):
    with pytest.raises(ValueError):
        retention_days_from_env({"WEBHOOK_DELIVERY_RETENTION_DAYS": value})


def test_the_batches_default_to_1000_rows_and_100_batches_and_read_the_settings():
    assert retention_batches_from_env({}) == (1000, 100)
    assert retention_batches_from_env(
        {
            "WEBHOOK_DELIVERY_RETENTION_BATCH_SIZE": "50",
            "WEBHOOK_DELIVERY_RETENTION_MAX_BATCHES": "3",
        }
    ) == (50, 3)


@pytest.mark.parametrize(
    "name",
    ["WEBHOOK_DELIVERY_RETENTION_BATCH_SIZE", "WEBHOOK_DELIVERY_RETENTION_MAX_BATCHES"],
)
@pytest.mark.parametrize("value", ["0", "-1", "many"])
def test_a_batch_setting_that_is_not_a_positive_number_is_refused(name, value):
    from fastapi import FastAPI

    with pytest.raises(ValueError) as ei:
        retention_batches_from_env({name: value})
    assert name in str(ei.value)
    with pytest.raises(ValueError):
        attach_retention(FastAPI(), lambda: None, environ={name: value})


def _runs(result: str) -> float:
    return (
        metrics.REGISTRY.get_sample_value(
            "aw_sweep_runs_total", {"sweep": "webhook-retention", "result": result}
        )
        or 0.0
    )


@pytest.mark.parametrize(
    "outcome,result",
    [
        (RetentionResult(deliveries=3, outbox=1), "complete"),
        (RetentionResult(deliveries=20, outbox=0, capped=True), "capped"),
        (RuntimeError("database went away"), "failed"),
    ],
    ids=["complete", "capped", "failed"],
)
def test_each_run_is_counted_by_its_result(monkeypatch, outcome, result):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    seen: dict = {}

    async def fake_single_flight(engine, **kwargs):
        seen.update(kwargs)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def fake_loop(run, *, interval_s, sleep=asyncio.sleep):
        seen["run"] = run
        await asyncio.sleep(3600)

    monkeypatch.setattr(retention, "run_single_flight", fake_single_flight)
    monkeypatch.setattr(retention, "retention_loop", fake_loop)
    app = FastAPI()
    attach_retention(
        app,
        lambda: None,
        environ={
            "WEBHOOK_DELIVERY_RETENTION_BATCH_SIZE": "10",
            "WEBHOOK_DELIVERY_RETENTION_MAX_BATCHES": "2",
        },
    )
    before = _runs(result)
    with TestClient(app):
        pass
    if isinstance(outcome, Exception):
        with pytest.raises(RuntimeError):
            asyncio.run(seen["run"]())
    else:
        asyncio.run(seen["run"]())
    assert _runs(result) == before + 1
    assert (seen["batch_size"], seen["max_batches"]) == (10, 2)


def test_the_loop_runs_once_per_interval_and_survives_a_failed_run(caplog):
    runs: list[int] = []
    sleeps: list[float] = []

    async def run():
        runs.append(1)
        if len(runs) == 1:
            raise RuntimeError("database went away")

    async def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 3:
            raise asyncio.CancelledError

    with caplog.at_level(logging.ERROR, logger="admin_api.retention"):
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(retention_loop(run, interval_s=86400, sleep=sleep))

    assert len(runs) == 3
    assert sleeps == [86400, 86400, 86400]
    assert any("retention" in r.getMessage() for r in caplog.records)


def test_the_app_starts_the_sweep_on_startup_and_stops_it_on_shutdown(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    calls: list[tuple] = []

    async def fake_loop(run, *, interval_s, sleep=asyncio.sleep):
        calls.append(("loop", interval_s))
        await asyncio.sleep(3600)

    monkeypatch.setattr(retention, "retention_loop", fake_loop)
    app = FastAPI()
    attach_retention(
        app, lambda: None, environ={"WEBHOOK_DELIVERY_RETENTION_DAYS": "5"}
    )

    with TestClient(app):
        task = app.state.webhook_retention_task
        assert not task.done()
    assert task.cancelled() or task.done()
    assert calls == [("loop", retention.SWEEP_INTERVAL_S)]


def test_the_production_app_starts_the_sweep_after_the_schema_converges(monkeypatch):
    """No database is reached: build_production_app only configures the engine and registers
    startup hooks, which run in registration order."""
    monkeypatch.setenv("INTERNAL_API_SECRET", "a-real-secret")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@db.invalid:5432/vexa")
    from admin_api.__main__ import build_production_app

    hooks = [h.__name__ for h in build_production_app().router.on_startup]

    assert "_start_retention" in hooks
    assert hooks.index("_converge_schema") < hooks.index("_start_retention")


def test_a_bad_retention_setting_refuses_to_attach():
    from fastapi import FastAPI

    with pytest.raises(ValueError):
        attach_retention(
            FastAPI(), lambda: None, environ={"WEBHOOK_DELIVERY_RETENTION_DAYS": "0"}
        )


# ── real Postgres: what is deleted, and in which order ──────────────────────────────────────

needs_pg = pytest.mark.skipif(
    not PG_URL,
    reason="real-Postgres retention; set MEETING_API_TEST_DATABASE_URL to run",
)


@pytest.fixture(scope="module")
def sync_engine():
    if not PG_URL:
        pytest.skip("MEETING_API_TEST_DATABASE_URL is not set")
    from sqlalchemy import create_engine

    from admin_api.schema.models import Base
    from admin_api.schema.sync import ensure_schema_sync

    engine = create_engine(PG_URL.replace("+asyncpg", "+psycopg"))
    ensure_schema_sync(engine, Base)
    yield engine
    engine.dispose()


@pytest.fixture()
def db(sync_engine):
    from sqlalchemy import text

    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE webhook_delivery_attempts, webhook_deliveries, webhook_outbox "
                "RESTART IDENTITY CASCADE"
            )
        )
    return sync_engine


def _sql(engine, statement, **params):
    from sqlalchemy import text

    with engine.begin() as conn:
        result = conn.execute(text(statement), params)
        return result.fetchall() if result.returns_rows else None


def _outbox(engine, *, published=True, at=OLD):
    event_id = f"evt_{uuid.uuid4().hex}"
    _sql(
        engine,
        "INSERT INTO webhook_outbox (event_id, event_type, sequence, payload_text, created_at, "
        "published_at) VALUES (:e, 'meeting.updated', 1, '{}', :at, :pub)",
        e=event_id,
        at=at,
        pub=at if published else None,
    )
    return event_id


def _delivery(engine, event_id, state, *, at=OLD, attempts=1):
    ((delivery_id,),) = _sql(
        engine,
        "INSERT INTO webhook_deliveries (event_id, subscription_id, user_id, state, attempts, "
        "next_attempt_at, created_at, updated_at) VALUES (:e, :s, 1, :st, :a, :at, :at, :at) "
        "RETURNING id",
        e=event_id,
        s=uuid.uuid4(),
        st=state,
        a=attempts,
        at=at,
    )
    for n in range(1, attempts + 1):
        _sql(
            engine,
            "INSERT INTO webhook_delivery_attempts (delivery_id, attempt, outcome, created_at) "
            "VALUES (:d, :n, 'delivered', :at)",
            d=delivery_id,
            n=n,
            at=at,
        )
    return delivery_id


def _ids(engine, table, column="id"):
    return {r[0] for r in _sql(engine, f"SELECT {column} FROM {table}")}


async def _sweep(**kwargs):
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(PG_URL)
    try:
        return await retention.sweep_once(
            engine,
            now=NOW,
            retention_days=30,
            **{"batch_size": 1000, "max_batches": 100, **kwargs},
        )
    finally:
        await engine.dispose()


@needs_pg
def test_old_final_deliveries_go_with_their_attempts_and_everything_else_stays(db):
    event = _outbox(db)
    gone = {state: _delivery(db, event, state, attempts=2) for state in FINAL_STATES}
    kept_live = {
        state: _delivery(db, event, state, attempts=1)
        for state in ("pending", "sending")
    }
    kept_recent = _delivery(db, event, "delivered", at=RECENT)

    result = asyncio.run(_sweep())

    assert result == RetentionResult(deliveries=4, outbox=0)
    assert _ids(db, "webhook_deliveries") == set(kept_live.values()) | {kept_recent}
    remaining_attempts = _ids(db, "webhook_delivery_attempts", "delivery_id")
    assert remaining_attempts.isdisjoint(gone.values())
    assert remaining_attempts == set(kept_live.values()) | {kept_recent}
    assert _ids(db, "webhook_outbox", "event_id") == {event}


@needs_pg
def test_retention_runs_in_order_deliveries_then_outbox(db):
    """An outbox row whose last delivery ages out goes in the same sweep, because the deliveries
    are deleted first."""
    lone = _outbox(db)
    _delivery(db, lone, "delivered")
    shared = _outbox(db)
    _delivery(db, shared, "dead")
    _delivery(db, shared, "pending")

    result = asyncio.run(_sweep())

    assert result == RetentionResult(deliveries=2, outbox=1)
    assert _ids(db, "webhook_outbox", "event_id") == {shared}


@needs_pg
def test_published_outbox_rows_with_no_deliveries_go_and_unpublished_rows_never_do(db):
    published_empty = _outbox(db)
    published_recent_empty = _outbox(db, at=RECENT)
    unpublished_old = _outbox(db, published=False)
    unpublished_with_old_final = _outbox(db, published=False)
    _delivery(db, unpublished_with_old_final, "cancelled")

    result = asyncio.run(_sweep())

    assert result == RetentionResult(deliveries=1, outbox=2)
    remaining = _ids(db, "webhook_outbox", "event_id")
    assert published_empty not in remaining and published_recent_empty not in remaining
    assert remaining == {unpublished_old, unpublished_with_old_final}


@needs_pg
def test_the_retention_window_is_the_setting(db):
    event = _outbox(db)
    ten_days = _delivery(db, event, "delivered", at=NOW - timedelta(days=10))

    async def sweep(days):
        from sqlalchemy.ext.asyncio import create_async_engine

        engine = create_async_engine(PG_URL)
        try:
            return await retention.sweep_once(
                engine, now=NOW, retention_days=days, batch_size=1000, max_batches=100
            )
        finally:
            await engine.dispose()

    assert asyncio.run(sweep(30)).deliveries == 0
    assert asyncio.run(sweep(7)).deliveries == 1
    assert ten_days not in _ids(db, "webhook_deliveries")


@needs_pg
def test_only_one_sweep_runs_at_a_time(db, caplog):
    """Single flight: while another session holds the retention lock, a run does nothing."""
    from sqlalchemy import text

    event = _outbox(db)
    _delivery(db, event, "delivered")

    async def scenario():
        from sqlalchemy.ext.asyncio import create_async_engine

        engine = create_async_engine(PG_URL)
        try:
            async with engine.connect() as holder:
                got = await holder.scalar(
                    text("SELECT pg_try_advisory_lock(cast(:k as bigint))"),
                    {"k": retention.RETENTION_LOCK_KEY},
                )
                assert got is True
                skipped = await retention.run_single_flight(
                    engine, now=NOW, retention_days=30, batch_size=1000, max_batches=100
                )
                await holder.execute(
                    text("SELECT pg_advisory_unlock(cast(:k as bigint))"),
                    {"k": retention.RETENTION_LOCK_KEY},
                )
            ran = await retention.run_single_flight(
                engine, now=NOW, retention_days=30, batch_size=1000, max_batches=100
            )
            return skipped, ran
        finally:
            await engine.dispose()

    with caplog.at_level(logging.INFO, logger="admin_api.retention"):
        skipped, ran = asyncio.run(scenario())

    assert skipped is None
    assert ran == RetentionResult(deliveries=1, outbox=1)
    assert _ids(db, "webhook_outbox", "event_id") == set()
    (line,) = [r.getMessage() for r in caplog.records if "deleted" in r.getMessage()]
    assert line == (
        "webhook retention: deleted 1 deliveries in a final state older than 30 days, "
        "then 1 published outbox rows with no deliveries left"
    )


@needs_pg
def test_many_deliveries_go_in_batches_and_the_cap_holds(db):
    """25 old final deliveries, batches of 10, at most 2 batches a run: the first run deletes 20
    and says it stopped at its cap; the next deletes the last 5."""
    event = _outbox(db)
    old = [_delivery(db, event, "delivered") for _ in range(25)]
    live = _delivery(db, event, "pending")

    first = asyncio.run(_sweep(batch_size=10, max_batches=2))
    assert first == RetentionResult(deliveries=20, outbox=0, capped=True)
    left = _ids(db, "webhook_deliveries")
    assert len(left) == 6 and live in left
    assert left - {live} == set(sorted(old)[20:])

    second = asyncio.run(_sweep(batch_size=10, max_batches=2))
    assert second == RetentionResult(deliveries=5, outbox=0)
    assert _ids(db, "webhook_deliveries") == {live}


@needs_pg
def test_a_backlog_that_ends_on_a_batch_edge_is_not_capped(db):
    event = _outbox(db)
    for _ in range(20):
        _delivery(db, event, "dead")
    _delivery(db, event, "pending")

    result = asyncio.run(_sweep(batch_size=10, max_batches=3))

    assert result == RetentionResult(deliveries=20, outbox=0)


@needs_pg
def test_the_outbox_prune_is_batched_and_capped_too(db):
    empty = {_outbox(db) for _ in range(15)}

    first = asyncio.run(_sweep(batch_size=4, max_batches=2))
    assert first == RetentionResult(deliveries=0, outbox=8, capped=True)
    assert len(_ids(db, "webhook_outbox", "event_id") & empty) == 7

    rest = asyncio.run(_sweep(batch_size=4, max_batches=2))
    assert rest == RetentionResult(deliveries=0, outbox=7)
    assert _ids(db, "webhook_outbox", "event_id") == set()


@needs_pg
def test_a_capped_run_is_logged(db, caplog):
    event = _outbox(db)
    for _ in range(3):
        _delivery(db, event, "delivered")

    async def scenario():
        from sqlalchemy.ext.asyncio import create_async_engine

        engine = create_async_engine(PG_URL)
        try:
            return await retention.run_single_flight(
                engine, now=NOW, retention_days=30, batch_size=1, max_batches=2
            )
        finally:
            await engine.dispose()

    with caplog.at_level(logging.INFO, logger="admin_api.retention"):
        result = asyncio.run(scenario())

    assert result == RetentionResult(deliveries=2, outbox=0, capped=True)
    (line,) = [r for r in caplog.records if "cap" in r.getMessage()]
    assert line.levelno == logging.WARNING
    assert line.getMessage() == (
        "webhook retention stopped at its cap of 2 batches of 1 rows; "
        "the rest is deleted by the next run"
    )
