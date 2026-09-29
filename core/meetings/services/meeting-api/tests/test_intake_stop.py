"""§1.7 — stopping the bot in the call: ``intake.IntakeStop``, the production ``StopPort``.

``POST /v2/meetings/{id}/stop`` and R5 (the last entry of a live meeting removed) both stop through
it, in §1.7's order: the link lock, the meeting row, the outcome on ``meeting_aw_state``, the status
through the one writer, the leave command on ``bot_commands:meeting:{id}``, and a workload delete
while the bot is still booting (``lifecycle.stop_router.stop_meeting_row``).

Two groups:
  * in memory — booting vs active, ``no_live_bot`` on scheduled and finished meetings, R5's outcome,
    a repeated stop, a meeting that finished before the stop, a command bus that is down, and the
    link lock taken on the meeting's link;
  * Postgres — the lock order (link → meeting row → ``meeting_aw_state``), and R5 end to end: the
    last entry removed, the stop, then the bot's completion through the lifecycle callback, with the
    outcome kept on ``meeting_aw_state`` and in the meeting as ``/v2`` serves it. The callback
    persists its terminal status without the status writer, so it writes no outbox event yet; the
    terminal event's ``meeting.outcome`` is asserted once the callback writes through
    ``write_status``.

The Postgres cases skip cleanly unless ``MEETING_API_TEST_DATABASE_URL`` is set.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Optional

import pytest

from intake_builders import (
    A,
    Harness,
    conforms,
    entry_body,
    http,
    intake_app,
    make_harness,
    make_settings,
    upcoming,
)
from meeting_api.bot_spawn.fakes import FakeRuntimeClient
from meeting_api.intake.fakes import FakePublisher, InMemoryIntakeReads
from meeting_api.intake.ports import Room
from meeting_api.intake.service import IntakeService
from meeting_api.intake.stop import IntakeStop
from meeting_api.intake.validation import IntakeError
from meeting_api.lifecycle.stop_router import InMemoryCommandPublisher
from internal_callers import BOT

ACCOUNT = {"x-user-id": "1"}
NO_LIVE_BOT = "no bot in this meeting; to cancel it, remove the entry"
PG_URL = os.getenv("MEETING_API_TEST_DATABASE_URL")


class _NoSpawn:
    async def spawn_exact(self, user_id: int, meeting_id: int) -> Any:
        raise AssertionError("no spawn expected: no join_now entry")


class _DownBus:
    async def publish(self, channel: str, message: str) -> Any:
        raise ConnectionError("redis unreachable")


class _CrashingBus:
    """The process dies between the removal's commit and the leave command."""

    async def publish(self, channel: str, message: str) -> Any:
        raise RuntimeError("the process died before the leave command")


REMOVE = {"external_id": "google:3n5kq8example", "user": A, "reason": "deleted"}


class _Stack:
    """The in-memory harness with the real ``IntakeStop`` in place of ``FakeStop``."""

    def __init__(self, h: Harness, commands: Any = None) -> None:
        self.h = h
        self.commands = commands if commands is not None else InMemoryCommandPublisher()
        self.runtime = FakeRuntimeClient()
        self.stop = IntakeStop(
            h.store, self.commands, self.runtime, publisher=h.publisher
        )
        self.service = IntakeService(
            h.store, h.spawn, self.stop, h.publisher, h.settings, clock=h.clock
        )

    async def live(
        self,
        status: str,
        *,
        external_id: str = "google:3n5kq8example",
        container: Optional[str] = "wl-1",
    ) -> int:
        """A meeting with one entry whose bot reached ``status``."""
        reply = await self.service.put_entry(1, entry_body(external_id))
        mid = self.h.meeting_id(reply["meeting"]["id"])
        self.h.set_status(reply["meeting"]["id"], status)
        row = self.h.store.meetings[mid]
        self.h.store.meetings[mid] = {**row, "bot_container_id": container}
        return mid

    def leaves(self) -> list[tuple[str, dict]]:
        return [(ch, json.loads(msg)) for ch, msg in self.commands.published]


def _stack(commands: Any = None) -> _Stack:
    return _Stack(make_harness(), commands)


# ── booting vs active ───────────────────────────────────────────────────────────────────────


async def test_active_bot_is_asked_to_leave_and_goes_stopping():
    s = _stack()
    mid = await s.live("active")
    mark = s.h.mark()

    await s.stop.stop_live(1, mid, outcome=None)

    row = s.h.store.meetings[mid]
    assert row["status"] == "stopping"
    assert row["data"]["stop_requested"] is True
    assert s.leaves() == [
        (f"bot_commands:meeting:{mid}", {"action": "leave", "meeting_id": mid})
    ]
    assert s.runtime.deleted == []  # a listening bot finishes its recording and leaves
    [event] = s.h.store.events[mark:]
    assert event.event_type == "meeting.status_change"
    assert event.change is not None
    assert (event.change["from"], event.change["to"]) == ("active", "stopping")
    assert event.change["reason"] == "stopped"
    assert event.meeting["outcome"] is None  # a user's stop sets no outcome
    assert s.h.published()[-1] == event.event_id


@pytest.mark.parametrize("status", ["needs_help", "needs_human_help"])
async def test_a_bot_that_reached_the_meeting_goes_stopping(status):
    s = _stack()
    mid = await s.live(status)
    await s.stop.stop_live(1, mid, outcome=None)
    assert s.h.store.meetings[mid]["status"] == "stopping"
    assert s.runtime.deleted == []


@pytest.mark.parametrize("status", ["requested", "joining", "awaiting_admission"])
async def test_booting_bot_is_torn_down_and_keeps_its_stage(status):
    s = _stack()
    mid = await s.live(status, container="wl-boot")
    mark = s.h.mark()

    await s.stop.stop_live(1, mid, outcome=None)

    row = s.h.store.meetings[mid]
    # The stage the bot reached stays: `stopping` means a bot in the meeting is leaving, and the
    # terminal chain reads it that way (`lifecycle.machine`, `lifecycle.reconcile`).
    assert row["status"] == status
    assert row["data"]["stop_requested"] is True
    assert s.runtime.deleted == ["wl-boot"]
    assert [ch for ch, _ in s.leaves()] == [f"bot_commands:meeting:{mid}"]
    assert s.h.store.events[mark:] == []  # no status changed


async def test_booting_bot_without_a_workload_yet_gets_the_flag_only():
    s = _stack()
    mid = await s.live("requested", container=None)
    await s.stop.stop_live(1, mid, outcome=None)
    assert s.h.store.meetings[mid]["data"]["stop_requested"] is True
    assert s.runtime.deleted == []


# ── no_live_bot ─────────────────────────────────────────────────────────────────────────────


async def _stop_route(s: _Stack, uuid: str):
    client = http(intake_app(s.service, InMemoryIntakeReads(s.h.store), s.stop))
    async with client:
        return await client.post(f"/v2/meetings/{uuid}/stop", headers=ACCOUNT)


async def test_stop_route_on_a_scheduled_meeting_is_no_live_bot():
    s = _stack()
    reply = await s.service.put_entry(1, entry_body())
    r = await _stop_route(s, reply["meeting"]["id"])
    assert r.status_code == 409, r.text
    conforms(r.json(), "Error")
    assert r.json()["error"] == {"code": "no_live_bot", "message": NO_LIVE_BOT}
    assert s.leaves() == []
    assert s.h.store.meetings[s.h.meeting_id(reply["meeting"]["id"])]["status"] == (
        "scheduled"
    )


@pytest.mark.parametrize("finished", ["completed", "failed"])
async def test_stop_route_on_a_finished_meeting_is_no_live_bot(finished):
    s = _stack()
    mid = await s.live("active")
    uuid = s.h.store.meetings[mid]["uuid"]
    s.h.set_status(uuid, finished)
    r = await _stop_route(s, uuid)
    assert r.status_code == 409, r.text
    assert r.json()["error"] == {"code": "no_live_bot", "message": NO_LIVE_BOT}
    assert s.leaves() == []
    assert s.runtime.deleted == []


async def test_stop_route_on_a_live_meeting_answers_the_meeting_stopping():
    s = _stack()
    mid = await s.live("active")
    r = await _stop_route(s, s.h.store.meetings[mid]["uuid"])
    assert r.status_code == 200, r.text
    conforms(r.json(), "Meeting")
    assert r.json()["status"] == "stopping"
    assert r.json()["outcome"] is None
    assert [ch for ch, _ in s.leaves()] == [f"bot_commands:meeting:{mid}"]


# ── R5: the outcome travels with the stop ───────────────────────────────────────────────────


async def test_r5_last_entry_removed_stops_the_bot_with_the_outcome():
    s = _stack()
    mid = await s.live("active")
    mark = s.h.mark()

    reply = await s.service.remove_entry(
        1, {"external_id": "google:3n5kq8example", "user": A, "reason": "deleted"}
    )

    assert reply["result"] == "bot_stopping"
    assert reply["meeting"]["status"] == "stopping"
    expected = {
        "kind": "cancelled_by_calendar",
        "detail": "deleted",
        "message": "last entry removed: deleted",
    }
    assert {k: reply["meeting"]["outcome"][k] for k in expected} == expected
    aw = s.h.store.aw[mid]
    assert (aw["outcome_kind"], aw["outcome_detail"], aw["outcome_message"]) == (
        "cancelled_by_calendar",
        "deleted",
        "last entry removed: deleted",
    )
    events = s.h.store.events[mark:]
    assert [e.event_type for e in events] == [
        "meeting.updated",
        "meeting.status_change",
    ]
    stopping = events[-1]
    assert stopping.change is not None and stopping.change["to"] == "stopping"
    # Every event written through the status writer from here on carries the outcome.
    assert stopping.meeting["outcome"]["kind"] == "cancelled_by_calendar"
    assert [ch for ch, _ in s.leaves()] == [f"bot_commands:meeting:{mid}"]


async def test_r5_on_a_booting_bot_records_the_outcome_and_tears_it_down():
    s = _stack()
    mid = await s.live("joining", container="wl-join")
    reply = await s.service.remove_entry(
        1, {"external_id": "google:3n5kq8example", "user": A}
    )
    assert reply["result"] == "bot_stopping"
    assert reply["meeting"]["status"] == "joining"
    assert reply["meeting"]["outcome"]["kind"] == "cancelled_by_calendar"
    assert s.h.store.aw[mid]["outcome_detail"] is None
    assert s.h.store.aw[mid]["outcome_message"] == "last entry removed"
    assert s.runtime.deleted == ["wl-join"]


# ── once, and only while live ───────────────────────────────────────────────────────────────


async def test_a_second_stop_sends_nothing_more():
    s = _stack()
    mid = await s.live("active")
    await s.stop.stop_live(1, mid, outcome=None)
    mark, sent = s.h.mark(), len(s.commands.published)
    await s.stop.stop_live(1, mid, outcome=None)
    assert s.h.store.events[mark:] == []
    assert len(s.commands.published) == sent


async def test_a_second_stop_of_a_booting_bot_sends_nothing_more():
    s = _stack()
    mid = await s.live("requested")
    await s.stop.stop_live(1, mid, outcome=None)
    await s.stop.stop_live(1, mid, outcome=None)
    assert len(s.commands.published) == 1
    assert s.runtime.deleted == ["wl-1"]


async def test_a_meeting_that_finished_before_the_stop_is_left_alone():
    s = _stack()
    mid = await s.live("active")
    s.h.set_status(s.h.store.meetings[mid]["uuid"], "completed")
    mark = s.h.mark()
    await s.stop.stop_live(1, mid, outcome=None)
    assert s.h.store.meetings[mid]["status"] == "completed"
    assert s.h.store.events[mark:] == []
    assert s.leaves() == []


async def test_a_down_command_bus_is_unavailable_and_the_stop_stays_recorded():
    s = _stack(commands=_DownBus())
    mid = await s.live("active")
    with pytest.raises(IntakeError) as raised:
        await s.stop.stop_live(1, mid, outcome=None)
    assert raised.value.code == "unavailable"
    assert raised.value.http_status == 503
    row = s.h.store.meetings[mid]
    assert row["status"] == "stopping"  # the stale-stopping sweep converges it
    assert row["data"]["stop_requested"] is True


async def test_the_stop_takes_the_meetings_link_lock():
    s = _stack()
    mid = await s.live("active")
    locks = len(s.h.store.lock_log)
    await s.stop.stop_live(1, mid, outcome=None)
    room = Room("google_meet", "kxo-misr-avz")
    # A read without a link lock (to learn the link), then the write under it.
    assert s.h.store.lock_log[locks:] == [(1, ()), (1, (room,)), (1, ())]


# ── R5: the stop is recorded in the removal's transaction ───────────────────────────────────


async def test_r5_records_the_stop_in_the_removals_one_locked_transaction():
    s = _stack()
    await s.live("active")
    locks = len(s.h.store.lock_log)
    await s.service.remove_entry(1, REMOVE)
    room = Room("google_meet", "kxo-misr-avz")
    # One transaction under the link lock holds both the removal and the stop (§1.7 steps 1–4).
    assert [rooms for _, rooms in s.h.store.lock_log[locks:] if rooms] == [(room,)]


@pytest.mark.parametrize("bus", [_DownBus, _CrashingBus])
async def test_r5_a_failed_leave_never_loses_the_stop(bus):
    """The leave command fails after the removal committed: the stop is already recorded with the
    removal, so the reply stands and the stale-stopping reconcile sweep ends the bot."""
    s = _stack(commands=bus())
    mid = await s.live("active")
    reply = await s.service.remove_entry(1, REMOVE)
    assert reply["result"] == "bot_stopping"
    assert reply["meeting"]["status"] == "stopping"
    row = s.h.store.meetings[mid]
    assert row["status"] == "stopping"
    assert row["data"]["stop_requested"] is True
    assert s.h.store.aw[mid]["outcome_kind"] == "cancelled_by_calendar"
    assert [e.state for e in s.h.store.entries.values()] == ["removed"]


async def test_r5_a_stop_that_fails_to_record_keeps_the_entry():
    """Recording the stop fails inside the removal's transaction: the removal rolls back with it,
    so the entry is still there and a retry removes it and stops the bot."""
    s = _stack()
    mid = await s.live("active")
    real = s.h.store.write_status

    def failing(meeting_id: int, to_status: str, **kw: Any) -> Any:
        if to_status == "stopping":
            raise RuntimeError("storage failed mid-transaction")
        return real(meeting_id, to_status, **kw)

    s.h.store.write_status = failing  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await s.service.remove_entry(1, REMOVE)
    assert [e.state for e in s.h.store.entries.values()] == ["active"]
    assert s.h.store.meetings[mid]["status"] == "active"

    s.h.store.write_status = real  # type: ignore[method-assign]
    reply = await s.service.remove_entry(1, REMOVE)
    assert reply["result"] == "bot_stopping"
    assert [ch for ch, _ in s.leaves()] == [f"bot_commands:meeting:{mid}"]


# ── Postgres ────────────────────────────────────────────────────────────────────────────────


async def _pg_engine():
    if not PG_URL:
        pytest.skip(
            "real-Postgres proofs for §1.7; set MEETING_API_TEST_DATABASE_URL to run"
        )
    pytest.importorskip("sqlalchemy", reason="see test_intake_pg_schema.py's docstring")
    pytest.importorskip("asyncpg", reason="see test_intake_pg_schema.py's docstring")
    from sqlalchemy.ext.asyncio import create_async_engine

    from admin_api.schema import models as admin_models
    from admin_api.schema import sync as admin_sync

    engine = create_async_engine(PG_URL)
    async with engine.begin() as conn:
        await conn.run_sync(admin_models.Base.metadata.drop_all)
    await admin_sync.ensure_schema(engine, admin_models.Base)
    return engine


@pytest.fixture
async def pg_engine():
    engine = await _pg_engine()
    yield engine
    from admin_api.schema import models as admin_models

    async with engine.begin() as conn:
        await conn.run_sync(admin_models.Base.metadata.drop_all)
    await engine.dispose()


class _PgStack:
    def __init__(self, engine: Any, commands: Any = None) -> None:
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from meeting_api.bot_spawn.adapters import SqlAlchemyMeetingRepo
        from meeting_api.intake import PostgresIntakeReads, PostgresIntakeStore

        self.engine = engine
        self.session_factory = async_sessionmaker(engine, expire_on_commit=False)
        self.repo = SqlAlchemyMeetingRepo(self.session_factory)
        self.store = PostgresIntakeStore(self.session_factory)
        self.reads = PostgresIntakeReads(self.session_factory)
        self.commands = commands if commands is not None else InMemoryCommandPublisher()
        self.runtime = FakeRuntimeClient()
        self.publisher = FakePublisher()
        self.stop = IntakeStop(
            self.store, self.commands, self.runtime, publisher=self.publisher
        )
        #: The meeting ``live`` plans, a day ahead of the real clock the service runs on.
        self.window = upcoming()
        self.service = IntakeService(
            self.store,
            _NoSpawn(),
            self.stop,
            self.publisher,
            make_settings(),
        )

    async def exec(self, sql: str, **params: Any) -> Any:
        from sqlalchemy import text

        async with self.engine.begin() as conn:
            return await conn.execute(text(sql), params)

    async def scalar(self, sql: str, **params: Any) -> Any:
        from sqlalchemy import text

        async with self.engine.connect() as conn:
            return (await conn.execute(text(sql), params)).scalar()

    async def live(self, status: str, *, session_uid: str = "sess-r5") -> int:
        """A meeting entries manage whose bot reached ``status``, with its session."""
        reply = await self.service.put_entry(7, entry_body(**self.window))
        mid = int(
            await self.scalar(
                "SELECT id FROM meetings WHERE uuid = CAST(:u AS uuid)",
                u=reply["meeting"]["id"],
            )
        )
        await self.exec(
            "UPDATE meetings SET status = :s WHERE id = :m", s=status, m=mid
        )
        await self.exec(
            "INSERT INTO meeting_sessions (meeting_id, session_uid) VALUES (:m, :u)",
            m=mid,
            u=session_uid,
        )
        return mid


async def _waiting_on(
    stack: _PgStack, wait_event_type: str, wait_event: str, n: int = 1
) -> None:
    for _ in range(300):
        waiting = await stack.scalar(
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE wait_event_type = :t AND wait_event = :e",
            t=wait_event_type,
            e=wait_event,
        )
        if int(waiting) >= n:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"the stop never queued on {wait_event_type}/{wait_event}")


async def test_pg_the_stop_takes_the_link_lock_before_anything(pg_engine):
    from sqlalchemy import text

    s = _PgStack(pg_engine)
    mid = await s.live("active")
    async with pg_engine.connect() as holder:
        tx = await holder.begin()
        await holder.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
            {"k": "aw-intake:7:google_meet:kxo-misr-avz"},
        )
        stop = asyncio.create_task(s.stop.stop_live(7, mid, outcome=None))
        await _waiting_on(s, "Lock", "advisory")
        assert not stop.done()
        assert await s.scalar("SELECT status FROM meetings WHERE id = :m", m=mid) == (
            "active"
        )
        assert s.commands.published == []
        await tx.rollback()
    await stop
    assert await s.scalar("SELECT status FROM meetings WHERE id = :m", m=mid) == (
        "stopping"
    )


async def test_pg_the_stop_locks_the_meeting_row_after_the_link(pg_engine):
    from sqlalchemy import text

    s = _PgStack(pg_engine)
    mid = await s.live("active")
    async with pg_engine.connect() as holder:
        tx = await holder.begin()
        await holder.execute(
            text("SELECT id FROM meetings WHERE id = :m FOR UPDATE"), {"m": mid}
        )
        stop = asyncio.create_task(s.stop.stop_live(7, mid, outcome=None))
        await _waiting_on(s, "Lock", "transactionid")
        async with pg_engine.connect() as probe:
            took = (
                await probe.execute(
                    text("SELECT pg_try_advisory_lock(hashtextextended(:k, 0))"),
                    {"k": "aw-intake:7:google_meet:kxo-misr-avz"},
                )
            ).scalar_one()
        assert took is False  # the waiting stop already holds the link lock
        assert not stop.done()
        await tx.rollback()
    await stop


async def test_pg_the_stop_locks_aw_state_after_the_meeting_row(pg_engine):
    from sqlalchemy import text
    from meeting_api.intake.status import Outcome

    s = _PgStack(pg_engine)
    mid = await s.live("active")
    await s.exec(
        "INSERT INTO meeting_aw_state (meeting_id) VALUES (:m) ON CONFLICT DO NOTHING",
        m=mid,
    )
    async with pg_engine.connect() as holder:
        tx = await holder.begin()
        await holder.execute(
            text(
                "SELECT meeting_id FROM meeting_aw_state WHERE meeting_id = :m FOR UPDATE"
            ),
            {"m": mid},
        )
        stop = asyncio.create_task(
            s.stop.stop_live(
                7,
                mid,
                outcome=Outcome("cancelled_by_calendar", None, "last entry removed"),
            )
        )
        await _waiting_on(s, "Lock", "transactionid")
        async with pg_engine.connect() as probe:
            await probe.execute(text("SET lock_timeout = '50ms'"))
            with pytest.raises(Exception) as locked:
                await probe.execute(
                    text("SELECT id FROM meetings WHERE id = :m FOR UPDATE NOWAIT"),
                    {"m": mid},
                )
        assert "lock" in str(locked.value).lower()  # the stop holds the meeting row
        assert not stop.done()
        await tx.rollback()
    await stop
    assert (
        await s.scalar(
            "SELECT outcome_kind FROM meeting_aw_state WHERE meeting_id = :m", m=mid
        )
        == "cancelled_by_calendar"
    )


async def test_pg_booting_bot_is_torn_down_through_the_real_store(pg_engine):
    s = _PgStack(pg_engine)
    mid = await s.live("awaiting_admission")
    await s.exec("UPDATE meetings SET bot_container_id = 'wl-pg' WHERE id = :m", m=mid)
    await s.stop.stop_live(7, mid, outcome=None)
    row = await s.repo.get_meeting(mid)
    assert row is not None
    assert row["status"] == "awaiting_admission"
    assert row["data"]["stop_requested"] is True
    assert s.runtime.deleted == ["wl-pg"]
    assert (
        await s.scalar(
            "SELECT count(*) FROM webhook_outbox WHERE meeting_id = :m", m=mid
        )
        == 1  # meeting.scheduled only: no status changed
    )


async def test_pg_r5_outcome_survives_the_bots_completion(pg_engine):
    """R5 end to end: remove the last entry of a live meeting → ``stopping`` with outcome
    ``cancelled_by_calendar`` (in the outbox event) → the bot's ``completed``/``stopped`` through the
    lifecycle callback → the meeting is ``completed`` and still carries the outcome."""
    from sqlalchemy import text

    from meeting_api import create_app

    s = _PgStack(pg_engine)
    mid = await s.live("active", session_uid="sess-r5")
    reply = await s.service.remove_entry(
        7, {"external_id": "google:3n5kq8example", "user": A, "reason": "deleted"}
    )
    assert reply["result"] == "bot_stopping"
    assert reply["meeting"]["status"] == "stopping"
    assert reply["meeting"]["outcome"]["kind"] == "cancelled_by_calendar"
    assert [ch for ch, _ in s.commands.published] == [f"bot_commands:meeting:{mid}"]

    async with pg_engine.connect() as conn:
        payloads = [
            json.loads(p)
            for (p,) in (
                await conn.execute(
                    text(
                        "SELECT payload_text FROM webhook_outbox WHERE meeting_id = :m "
                        "ORDER BY sequence"
                    ),
                    {"m": mid},
                )
            ).all()
        ]
    stopping = payloads[-1]
    assert stopping["data"]["change"]["to"] == "stopping"
    assert stopping["data"]["meeting"]["outcome"]["kind"] == "cancelled_by_calendar"
    assert stopping["data"]["meeting"]["outcome"]["detail"] == "deleted"

    app = create_app(meeting_repo=s.repo)
    event = {
        "connection_id": "sess-r5",
        "status": "completed",
        "timestamp": s.window["start"],  # the bot's report, inside the planned meeting
        "exit_code": 0,
        "completion_reason": "stopped",
        "bot_logs": ["[ACT] leave received"],
    }
    async with http(app) as client:
        r = await client.post(
            "/bots/internal/callback/lifecycle", headers=BOT, json=event
        )
    assert r.status_code == 200, r.text

    row = await s.repo.get_meeting(mid)
    assert row is not None
    assert row["status"] == "completed"
    assert row["data"]["completion_reason"] == "stopped"
    assert (
        await s.scalar(
            "SELECT outcome_kind FROM meeting_aw_state WHERE meeting_id = :m", m=mid
        )
        == "cancelled_by_calendar"
    )
    served = await s.reads.meeting_by_uuid(7, reply["meeting"]["id"])
    assert served is not None
    projected = served.project(lead_s=300)
    assert projected["status"] == "completed"
    assert projected["outcome"]["kind"] == "cancelled_by_calendar"
    assert projected["outcome"]["detail"] == "deleted"


async def test_pg_r5_a_failed_leave_leaves_the_stop_to_the_reconcile_sweep(pg_engine):
    """The leave command can't be sent after the removal committed. The stop committed with the
    removal, so the meeting is ``stopping`` with its outcome and the stale-stopping reconcile sweep
    (``list_stale_stopping`` → complete + workload delete) ends the bot; a retried remove answers
    ``already_removed`` and sends nothing."""
    s = _PgStack(pg_engine, commands=_DownBus())
    mid = await s.live("active")
    await s.exec("UPDATE meetings SET bot_container_id = 'wl-r5' WHERE id = :m", m=mid)
    reply = await s.service.remove_entry(7, REMOVE)
    assert reply["result"] == "bot_stopping"
    row = await s.repo.get_meeting(mid)
    assert row is not None
    assert row["status"] == "stopping"
    assert row["data"]["stop_requested"] is True
    assert (
        await s.scalar(
            "SELECT outcome_kind FROM meeting_aw_state WHERE meeting_id = :m", m=mid
        )
        == "cancelled_by_calendar"
    )
    stale = await s.repo.list_stale_stopping(older_than_seconds=0)
    assert (mid, "sess-r5", "wl-r5") in stale
    again = await s.service.remove_entry(7, REMOVE)
    assert again["result"] == "already_removed"


async def test_pg_a_put_after_the_removal_already_sees_the_stop(pg_engine):
    """A PUT on the link queued right behind the removal runs after it, and finds the meeting
    ``stopping``: no transaction can land between the removal and its stop."""
    from sqlalchemy import text

    s = _PgStack(pg_engine)
    mid = await s.live("active")
    async with pg_engine.connect() as holder:
        tx = await holder.begin()
        await holder.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
            {"k": "aw-intake:7:google_meet:kxo-misr-avz"},
        )
        remove = asyncio.create_task(s.service.remove_entry(7, REMOVE))
        await _waiting_on(s, "Lock", "advisory", n=1)
        put = asyncio.create_task(
            s.service.put_entry(
                7,
                entry_body("google:late-invite", **s.window),
            )
        )
        await _waiting_on(s, "Lock", "advisory", n=2)
        await tx.rollback()
    removed, put_reply = await asyncio.gather(remove, put)
    assert removed["result"] == "bot_stopping"
    assert put_reply["meeting"]["status"] == "stopping"
    row = await s.repo.get_meeting(mid)
    assert row is not None and row["status"] == "stopping"


# ── a link that keeps changing during the stop (INTAKE_STOP_LINK_RETRIES) ───────────────────


@pytest.mark.parametrize("retries, tries", [(None, 2), ("0", 1), ("3", 4)])
async def test_a_stop_whose_link_keeps_changing_is_tried_a_bounded_number_of_times(
    monkeypatch, retries, tries
):
    if retries is None:
        monkeypatch.delenv("INTAKE_STOP_LINK_RETRIES", raising=False)
    else:
        monkeypatch.setenv("INTAKE_STOP_LINK_RETRIES", retries)
    s = _stack()
    mid = await s.live("active")
    locked: list[tuple] = []

    def move(store, rooms):
        if rooms:  # every locked read finds the meeting on another link
            locked.append(rooms)
            row = store.meetings[mid]
            store.meetings[mid] = {
                **row,
                "platform_specific_id": f"moved-{len(locked)}",
            }

    s.h.store.on_lock = move
    with pytest.raises(IntakeError) as err:
        await s.stop.stop_live(1, mid, outcome=None)
    assert err.value.code == "unavailable"
    assert len(locked) == tries
    assert s.leaves() == []
