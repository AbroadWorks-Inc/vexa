"""§6.9 F-I — a sweep's failing item is retried a bounded number of times, then given up for good.

Offline: ``run_item`` over ``InMemoryItemFailures`` (the count, the give-up, the log and the
counter, and that it never raises). Postgres: ``PostgresItemFailures`` keeps the count in
``sweep_item_failures``, so a second replica continues it; skips cleanly unless
``MEETING_API_TEST_DATABASE_URL`` is set (see ``test_intake_pg_schema.py``'s docstring).
"""

from __future__ import annotations

import json
import os

import pytest

from meeting_api.sweeps.item_failures import (
    InMemoryItemFailures,
    ItemDeferred,
    ItemExpired,
    run_item,
    sweep_batch_size,
    sweep_max_item_failures,
)


def _count(sweep: str, result: str) -> float:
    from meeting_api.metrics import registry

    value = registry().get_sample_value(
        "aw_sweep_items_total", {"sweep": sweep, "result": result}
    )
    return value or 0.0


def test_the_settings_and_their_defaults(monkeypatch):
    monkeypatch.delenv("SWEEP_BATCH_SIZE", raising=False)
    monkeypatch.delenv("SWEEP_MAX_ITEM_FAILURES", raising=False)
    assert (sweep_batch_size(), sweep_max_item_failures()) == (200, 5)
    monkeypatch.setenv("SWEEP_BATCH_SIZE", "50")
    monkeypatch.setenv("SWEEP_MAX_ITEM_FAILURES", "2")
    assert (sweep_batch_size(), sweep_max_item_failures()) == (50, 2)


async def test_a_failing_item_is_logged_counted_and_given_up_after_the_limit(capsys):
    failures = InMemoryItemFailures(max_failures=3)
    calls: list[str] = []

    async def boom() -> None:
        calls.append("run")
        raise RuntimeError("poison row")

    failed, given_up = _count("probe", "failed"), _count("probe", "given_up")
    for _ in range(3):
        assert await run_item(failures, "probe", "42", boom) is False
    assert await failures.given_up("probe", ["42", "43"]) == {"42"}
    assert _count("probe", "failed") == failed + 2
    assert _count("probe", "given_up") == given_up + 1
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    events = [(e["event"], e["fields"]["item_id"]) for e in lines]
    assert events == [
        ("sweep_item_failed", "42"),
        ("sweep_item_failed", "42"),
        ("sweep_item_given_up", "42"),
    ]
    assert lines[-1]["fields"]["error"] == "RuntimeError"
    assert "poison row" in lines[-1]["fields"]["traceback"]
    assert len(calls) == 3


async def test_a_good_item_runs_and_records_nothing():
    failures = InMemoryItemFailures(max_failures=1)
    done: list[str] = []

    async def ok() -> None:
        done.append("ok")

    assert await run_item(failures, "probe", "7", ok) is True
    assert done == ["ok"] and await failures.given_up("probe", ["7"]) == set()


async def test_the_count_is_per_sweep():
    failures = InMemoryItemFailures(max_failures=1)

    async def boom() -> None:
        raise RuntimeError("x")

    await run_item(failures, "auto-join", "9", boom)
    assert await failures.given_up("auto-join", ["9"]) == {"9"}
    assert await failures.given_up("not-sent", ["9"]) == set()


async def test_a_failure_that_cannot_be_recorded_is_logged_and_not_raised(capsys):
    class Down(InMemoryItemFailures):
        async def failed(self, sweep, item_id, error):
            raise ConnectionRefusedError("database is down")

    async def boom() -> None:
        raise RuntimeError("x")

    assert await run_item(Down(max_failures=1), "probe", "1", boom) is False
    events = [
        json.loads(line)["event"] for line in capsys.readouterr().out.splitlines()
    ]
    assert events == ["sweep_item_failure_unrecorded"]


async def test_a_deferred_item_is_logged_and_counted_and_records_nothing(capsys):
    failures = InMemoryItemFailures(max_failures=1)

    async def no_answer() -> None:
        raise ItemDeferred("runtime_unreachable", "no runtime answer about w")

    before = _count("probe", "runtime_unreachable")
    for _ in range(3):
        assert await run_item(failures, "probe", "8", no_answer) is False
    assert failures.counts == {} and await failures.given_up("probe", ["8"]) == set()
    assert _count("probe", "runtime_unreachable") == before + 3
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [(e["event"], e["level"]) for e in lines] == [
        ("sweep_item_runtime_unreachable", "warning")
    ] * 3
    assert lines[0]["fields"] == {
        "sweep": "probe",
        "item_id": "8",
        "reason": "no runtime answer about w",
    }


async def test_an_expired_item_is_given_up_at_once(capsys):
    failures = InMemoryItemFailures(max_failures=5)

    async def too_old() -> None:
        raise ItemExpired("pending past its max age")

    given_up = _count("probe", "given_up")
    assert await run_item(failures, "probe", "3", too_old) is False
    assert await failures.given_up("probe", ["3"]) == {"3"}
    assert _count("probe", "given_up") == given_up + 1
    (line,) = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert (line["event"], line["level"]) == ("sweep_item_given_up", "error")


# ── Postgres ─────────────────────────────────────────────────────────────────────────────────

pg_only = pytest.mark.skipif(
    not os.getenv("MEETING_API_TEST_DATABASE_URL"),
    reason="real-Postgres proof for §6.9 F-I; set MEETING_API_TEST_DATABASE_URL to run",
)


@pg_only
async def test_pg_the_count_survives_a_second_replica_and_gives_up_once(
    link_pg_engine,
):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from meeting_api.sweeps.item_failures import PostgresItemFailures

    factory = async_sessionmaker(link_pg_engine, expire_on_commit=False)
    one = PostgresItemFailures(factory, max_failures=3)
    two = PostgresItemFailures(factory, max_failures=3)
    error = RuntimeError("poison")
    assert await one.failed("not-sent", "5", error) is False
    assert await two.failed("not-sent", "5", error) is False
    assert await one.given_up("not-sent", ["5"]) == set()
    assert await two.failed("not-sent", "5", error) is True
    assert await one.failed("not-sent", "5", error) is False  # already given up: once
    assert await one.given_up("not-sent", ["5", "6"]) == {"5"}
    assert await one.given_up("auto-join", ["5"]) == set()
    async with link_pg_engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT failures, last_error, gave_up_at IS NOT NULL FROM "
                    "sweep_item_failures WHERE sweep = 'not-sent' AND item_id = '5'"
                )
            )
        ).one()
    assert tuple(row) == (4, "RuntimeError", True)


@pg_only
async def test_pg_give_up_gives_the_item_up_on_its_first_call_once(link_pg_engine):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from meeting_api.sweeps.item_failures import PostgresItemFailures

    factory = async_sessionmaker(link_pg_engine, expire_on_commit=False)
    failures = PostgresItemFailures(factory, max_failures=5)
    error = ItemExpired("too old")
    assert await failures.failed("unproven-teardown", "5:w", RuntimeError("x")) is False
    assert await failures.give_up("unproven-teardown", "5:w", error) is True
    assert await failures.give_up("unproven-teardown", "5:w", error) is False
    assert await failures.given_up("unproven-teardown", ["5:w"]) == {"5:w"}
