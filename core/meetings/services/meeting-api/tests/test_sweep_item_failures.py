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
    prune_item_failures,
    run_item,
    sweep_batch_size,
    sweep_item_failures_retention_s,
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


async def test_the_give_up_action_runs_once_right_after_the_give_up(capsys):
    failures = InMemoryItemFailures(max_failures=2)
    ended: list[str] = []

    async def boom() -> None:
        raise RuntimeError("poison row")

    async def end(error: BaseException) -> None:
        ended.append(type(error).__name__)

    for _ in range(2):
        await run_item(failures, "probe", "4", boom, on_given_up=end)
    assert ended == ["RuntimeError"]

    async def broken_end(error: BaseException) -> None:
        raise ConnectionRefusedError("database is down")

    capsys.readouterr()
    assert await run_item(failures, "probe", "5", boom, on_given_up=broken_end) is False
    assert await run_item(failures, "probe", "5", boom, on_given_up=broken_end) is False
    events = [json.loads(x)["event"] for x in capsys.readouterr().out.splitlines()]
    assert events[-2:] == ["sweep_item_given_up", "sweep_item_give_up_action_failed"]


def test_the_retention_defaults_to_seven_days(monkeypatch):
    monkeypatch.delenv("SWEEP_ITEM_FAILURES_RETENTION_S", raising=False)
    assert sweep_item_failures_retention_s() == 604800.0
    monkeypatch.setenv("SWEEP_ITEM_FAILURES_RETENTION_S", "3600")
    assert sweep_item_failures_retention_s() == 3600.0


async def test_records_older_than_the_retention_are_pruned(monkeypatch):
    """M8: a failure record untouched for ``SWEEP_ITEM_FAILURES_RETENTION_S`` goes, a page of
    ``SWEEP_BATCH_SIZE`` at a time; a newer one stays."""
    import time

    monkeypatch.setenv("SWEEP_ITEM_FAILURES_RETENTION_S", "3600")
    monkeypatch.setenv("SWEEP_BATCH_SIZE", "2")
    failures = InMemoryItemFailures(max_failures=1)
    for item in ("1", "2", "3", "fresh"):
        await failures.failed("probe", item, RuntimeError("x"))
    for item in ("1", "2", "3"):
        failures.updated[("probe", item)] = time.time() - 3601
    assert await prune_item_failures(failures, log=_Log()) == 3
    assert failures.counts == {("probe", "fresh"): 1}
    assert failures.gave_up == {("probe", "fresh")}


async def test_a_prune_that_fails_is_logged_and_not_raised():
    class Down(InMemoryItemFailures):
        async def prune(self, *, older_than_s, limit):
            raise ConnectionRefusedError("database is down")

    log = _Log()
    assert await prune_item_failures(Down(max_failures=1), log=log) == 0
    assert log.errors == ["sweep_item_failures prune failed"]


class _Log:
    def __init__(self) -> None:
        self.errors: list[str] = []

    def exception(self, message: str, *args) -> None:
        self.errors.append(message % args if args else message)


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


@pg_only
async def test_pg_records_older_than_the_retention_are_pruned_a_page_at_a_time(
    link_pg_engine,
):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from meeting_api.sweeps.item_failures import PostgresItemFailures

    factory = async_sessionmaker(link_pg_engine, expire_on_commit=False)
    failures = PostgresItemFailures(factory, max_failures=5)
    for item in ("1", "2", "3", "fresh"):
        await failures.failed("probe", item, RuntimeError("x"))
    async with link_pg_engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE sweep_item_failures SET updated_at = now() - interval '8 days' "
                "WHERE item_id <> 'fresh'"
            )
        )
    assert await failures.prune(older_than_s=7 * 86400, limit=2) == 3
    async with link_pg_engine.connect() as conn:
        left = (
            await conn.execute(text("SELECT item_id FROM sweep_item_failures"))
        ).scalars()
        assert list(left) == ["fresh"]
