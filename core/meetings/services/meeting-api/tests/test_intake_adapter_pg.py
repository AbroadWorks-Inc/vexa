"""§1.3 / §1.4 — the Postgres ``IntakeStore`` (``meeting_api.intake.adapters``) against real Postgres.

Three groups:
  * the store itself — the link lock's key and lifetime, the lock order, R15's ``room_meetings``,
    ``record_spawn_error`` and the index-only quota count (``EXPLAIN``);
  * races — the real ``IntakeService`` over the Postgres store with Spawn/Stop ports that claim and
    stop through the one status writer, driven by genuinely concurrent sessions
    (``asyncio.gather`` over separate connections). Where a race needs a fixed interleaving, a
    third connection holds the link lock with the design's own SQL expression until every racer is
    parked on it (``pg_stat_activity``), so the racers really do meet at the lock;
  * parity — a set of A6 use cases run against the in-memory fake and the Postgres store, with
    equal replies, events, published batches and final rows.

Skips cleanly unless ``MEETING_API_TEST_DATABASE_URL`` is set; see ``test_intake_pg_schema.py``'s
docstring for the ephemeral SQLAlchemy/asyncpg install.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("MEETING_API_TEST_DATABASE_URL"),
    reason="real-Postgres proofs for §1.3/§1.4; set MEETING_API_TEST_DATABASE_URL to run",
)

pytest.importorskip("sqlalchemy", reason="see test_intake_pg_schema.py's docstring")
pytest.importorskip("asyncpg", reason="see test_intake_pg_schema.py's docstring")

from sqlalchemy import event, text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker  # noqa: E402

from intake_builders import (  # noqa: E402
    A,
    B,
    GMEET,
    GMEET_OTHER,
    FakeClock,
    Requests,
    make_harness,
    make_settings,
    ts,
)
from meeting_api.intake import PostgresIntakeStore  # noqa: E402
from meeting_api.intake import adapters as adapters_mod  # noqa: E402
from meeting_api.intake import status as status_mod  # noqa: E402
from meeting_api.intake.fakes import FakePublisher  # noqa: E402
from meeting_api.intake.ports import Room, SpawnOutcome  # noqa: E402
from meeting_api.intake.projection import iso_utc  # noqa: E402
from meeting_api.intake.rules import Plan, is_live  # noqa: E402
from meeting_api.intake.service import IntakeService  # noqa: E402
from meeting_api.intake.status import (  # noqa: E402
    Outcome,
    StatusConflict,
    WrittenEvent,
)

GROOM = Room("google_meet", "kxo-misr-avz")
GROOM_OTHER = Room("google_meet", "abc-defg-hij")
ACCOUNT_LIMIT = SpawnOutcome("failed", "account_limit", "bot limit reached (45 of 45)")

#: The design's lock expression (§1.4), verbatim apart from the bind parameters.
DESIGN_LINK_KEY = (
    "hashtextextended('aw-intake:' || CAST(:uid AS integer) || ':' || "
    "CAST(:platform AS text) || ':' || CAST(:native AS text), 0)"
)


# ── the Postgres harness ─────────────────────────────────────────────────────────────────────


class PgSpawn:
    """``SpawnPort`` over Postgres, the ``FakeSpawn`` behaviour: claims a ``scheduled`` row
    (``requested``) under its link lock through the one status writer, answers ``already_live``
    for any other row, or returns the failure it was given."""

    def __init__(
        self,
        store: PostgresIntakeStore,
        *,
        failure: Optional[SpawnOutcome] = None,
        before: Optional[Callable[[int], Awaitable[None]]] = None,
    ) -> None:
        self._store = store
        self.failure = failure
        self.before = before
        self.calls: list[tuple[int, int]] = []

    async def spawn_exact(self, user_id: int, meeting_id: int) -> SpawnOutcome:
        self.calls.append((user_id, meeting_id))
        if self.before is not None:
            await self.before(meeting_id)
        if self.failure is not None:
            return self.failure
        claimed = await claim(self._store, user_id, meeting_id)
        return SpawnOutcome("sent" if claimed else "already_live")


class PgStop:
    """``StopPort`` over Postgres, the ``FakeStop`` behaviour: ``stop_live`` moves a live meeting
    to ``stopping`` with the outcome given; ``leave`` records the stop it is handed."""

    def __init__(self, store: PostgresIntakeStore) -> None:
        self._store = store
        self.calls: list[tuple[int, int, Optional[Outcome]]] = []

    async def stop_live(
        self, user_id: int, meeting_id: int, *, outcome: Optional[Outcome]
    ) -> None:
        self.calls.append((user_id, meeting_id, outcome))
        room = (await read_meeting(self._store, user_id, meeting_id)).room
        async with self._store.room_lock(user_id, [room]) as tx:
            status = (await tx.meeting(meeting_id)).status
            if is_live(status) and status != "stopping":
                await tx.status(
                    meeting_id, "stopping", expected_from={status}, outcome=outcome
                )

    async def leave(self, user_id: int, stop: Any) -> None:
        self.calls.append((user_id, stop.meeting_id, stop.outcome))


async def read_meeting(store: PostgresIntakeStore, user_id: int, meeting_id: int):
    async with store.room_lock(user_id, ()) as tx:
        return await tx.meeting(meeting_id)


async def claim(
    store: PostgresIntakeStore,
    user_id: int,
    meeting_id: int,
    *,
    hold: Optional[Callable[[], Awaitable[None]]] = None,
) -> bool:
    """The scheduler's (and spawn's) claim: ``scheduled`` → ``requested`` under the link lock,
    conditional. ``hold`` runs after the claim is written and before the commit."""
    room = (await read_meeting(store, user_id, meeting_id)).room
    async with store.room_lock(user_id, [room]) as tx:
        try:
            await tx.status(meeting_id, "requested", expected_from={"scheduled"})
        except StatusConflict:
            return False
        if hold is not None:
            await hold()
    return True


@dataclass
class PgHarness(Requests):
    clock: FakeClock
    engine: Any
    store: PostgresIntakeStore
    spawn: PgSpawn
    stop: PgStop
    publisher: FakePublisher
    service: IntakeService
    user_rooms: dict[int, tuple[int, Room]] = field(default_factory=dict)

    async def meeting_id(self, uuid: str) -> int:
        async with self.engine.connect() as conn:
            return (
                await conn.execute(
                    text("SELECT id FROM meetings WHERE uuid = CAST(:u AS uuid)"),
                    {"u": uuid},
                )
            ).scalar_one()

    async def set_status(
        self, uuid: str, status: str, *, outcome: Optional[Outcome] = None
    ) -> WrittenEvent:
        """A status change from outside intake (the bot lifecycle), through the one writer under
        the meeting's link lock."""
        mid = await self.meeting_id(uuid)
        room = (await read_meeting(self.store, 1, mid)).room
        async with self.store.room_lock(1, [room]) as tx:
            current = await tx.meeting(mid)
            return await tx.status(
                mid, status, expected_from={current.status}, outcome=outcome
            )

    async def seed_upstream(self, room: Room, plan: Plan, *, user_id: int = 1) -> int:
        """An entry-less upstream-planned row (``POST /meetings`` / calendar sync): no entries and
        no ``meeting_aw_state`` row."""
        async with self.engine.begin() as conn:
            return (
                await conn.execute(
                    text(
                        "INSERT INTO meetings (user_id, platform, platform_specific_id, status, "
                        "data) VALUES (:uid, :p, :n, 'scheduled', CAST(:d AS jsonb)) RETURNING id"
                    ),
                    {
                        "uid": user_id,
                        "p": room.platform,
                        "n": room.native_meeting_id,
                        "d": json.dumps(
                            {
                                "scheduled_at": iso_utc(plan.start),
                                "title": plan.title,
                                "constructed_meeting_url": plan.meeting_url,
                            }
                        ),
                    },
                )
            ).scalar_one()


@pytest.fixture
async def pg(intake_pg_engine, monkeypatch):
    from admin_api.schema import models as admin_models
    from admin_api.schema import sync as admin_sync

    await admin_sync.ensure_schema(intake_pg_engine, admin_models.Base)
    monkeypatch.setenv(
        "AUTO_JOIN_LEAD_S", "300"
    )  # the harness's lead, for the outbox payloads
    return intake_pg_engine


@pytest.fixture
def make_pg(pg, monkeypatch) -> Callable[..., PgHarness]:
    def make(
        now: str = "2026-09-26T12:00:00Z",
        *,
        max_active_entries: int = 100_000,
        spawn_failure: Optional[SpawnOutcome] = None,
    ) -> PgHarness:
        clock = FakeClock(ts(now))
        # the status writer stamps with the harness's clock, as the fake does
        monkeypatch.setattr(status_mod, "_now", clock)
        store = PostgresIntakeStore(async_sessionmaker(pg, expire_on_commit=False))
        spawn = PgSpawn(store, failure=spawn_failure)
        stop = PgStop(store)
        publisher = FakePublisher()
        service = IntakeService(
            store,
            spawn,
            stop,
            publisher,
            make_settings(max_active_entries=max_active_entries),
            clock=clock,
        )
        return PgHarness(clock, pg, store, spawn, stop, publisher, service)

    return make


# ── SQL helpers ──────────────────────────────────────────────────────────────────────────────


async def scalar(engine, sql: str, **params: Any) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(text(sql), params)).scalar_one()


async def rows(engine, sql: str, **params: Any) -> list[Any]:
    async with engine.connect() as conn:
        return list((await conn.execute(text(sql), params)).all())


class LinkHolder:
    """A third connection holding link locks with the design's expression (§1.4)."""

    def __init__(self, engine) -> None:
        self._engine = engine
        self._conn: Any = None
        self._tx: Any = None

    async def hold(self, user_id: int, *rooms: Room) -> None:
        self._conn = await self._engine.connect()
        self._tx = await self._conn.begin()
        for room in rooms:
            await self._conn.execute(
                text(f"SELECT pg_advisory_xact_lock({DESIGN_LINK_KEY})"),
                {
                    "uid": user_id,
                    "platform": room.platform,
                    "native": room.native_meeting_id,
                },
            )

    async def release(self) -> None:
        if self._tx is not None:
            await self._tx.rollback()
            await self._conn.close()
            self._tx = self._conn = None


@pytest.fixture
async def holder(pg):
    """The link holder, released at teardown even when the test fails, so no lock outlives it."""
    held = LinkHolder(pg)
    yield held
    await held.release()


async def advisory_waiters(engine) -> int:
    return await scalar(
        engine,
        "SELECT count(*) FROM pg_stat_activity "
        "WHERE wait_event_type = 'Lock' AND wait_event = 'advisory'",
    )


async def wait_for_waiters(engine, n: int, *, within_s: float = 5.0) -> None:
    deadline = time.monotonic() + within_s
    while await advisory_waiters(engine) < n:
        if time.monotonic() > deadline:
            raise AssertionError(f"fewer than {n} sessions parked on a link lock")
        await asyncio.sleep(0.02)


async def link_rows(engine, room: Room) -> list[tuple[int, str]]:
    return [
        (r.id, r.status)
        for r in await rows(
            engine,
            "SELECT id, status FROM meetings WHERE platform = :p AND platform_specific_id = :n "
            "ORDER BY id",
            p=room.platform,
            n=room.native_meeting_id,
        )
    ]


async def counts(engine) -> dict[str, int]:
    out = {}
    for table in ("meetings", "meeting_entries", "meeting_aw_state", "webhook_outbox"):
        out[table] = await scalar(engine, f"SELECT count(*) FROM {table}")
    return out


# ── the store: the link lock ─────────────────────────────────────────────────────────────────


async def test_the_link_lock_is_the_design_key_held_for_the_transaction(make_pg):
    h = make_pg()
    probe_sql = text(f"SELECT pg_try_advisory_xact_lock({DESIGN_LINK_KEY})")
    params = {"uid": 1, "platform": GROOM.platform, "native": GROOM.native_meeting_id}

    async def probe() -> bool:
        async with h.engine.connect() as conn, conn.begin():
            return (await conn.execute(probe_sql, params)).scalar_one()

    async with h.store.room_lock(1, [GROOM]):
        assert await probe() is False  # the design's key is the one held
        other = {**params, "uid": 2}
        async with h.engine.connect() as conn, conn.begin():
            assert (await conn.execute(probe_sql, other)).scalar_one() is True
    assert await probe() is True  # released at commit

    class Boom(Exception):
        pass

    with pytest.raises(Boom):
        async with h.store.room_lock(1, [GROOM]):
            raise Boom
    assert await probe() is True  # released at rollback


async def test_the_store_refuses_locks_out_of_order(make_pg):
    h = make_pg()
    with pytest.raises(ValueError):
        async with h.store.room_lock(1, [GROOM, GROOM_OTHER]):
            pass
    with pytest.raises(ValueError):
        async with h.store.room_lock(1, [GROOM, GROOM]):
            pass


async def test_opposite_lock_sets_taken_sorted_never_deadlock(make_pg, holder):
    """Two transactions each wanting both links, parked behind a holder: sorted order means one
    waits for the other instead of each holding one link and waiting for the other's."""
    h = make_pg()
    await holder.hold(1, GROOM_OTHER)
    both = sorted([GROOM, GROOM_OTHER])

    async def take() -> None:
        async with h.store.room_lock(1, both):
            await asyncio.sleep(0.05)

    tasks = asyncio.gather(take(), take())
    await wait_for_waiters(h.engine, 2)
    started = time.monotonic()
    await holder.release()
    await asyncio.wait_for(tasks, 5)
    assert time.monotonic() - started < 5


# ── the store: statements, R15, spawn errors, the quota count ────────────────────────────────


async def test_room_meetings_returns_live_or_entry_managed_rows_only(make_pg):
    """Ruling R15: the link's live rows, plus its non-finished rows that entries manage. An
    entry-less upstream-planned row, a finished row and another link's row are never returned.
    """
    h = make_pg()
    first = (await h.put("g:1"))["meeting"]["id"]
    second = (
        await h.put("g:2", start="2026-09-29T11:00:00Z", end="2026-09-29T11:30:00Z")
    )["meeting"]["id"]
    finished = (
        await h.put("g:3", start="2026-09-29T13:00:00Z", end="2026-09-29T13:30:00Z")
    )["meeting"]["id"]
    await h.set_status(finished, "failed")
    await h.put("g:4", meeting_url=GMEET_OTHER)
    upstream = await h.seed_upstream(
        GROOM, Plan(ts("2026-09-29T15:00:00Z"), None, "Upstream", None, GMEET)
    )
    live_upstream = await h.seed_upstream(
        GROOM, Plan(ts("2026-09-29T16:00:00Z"), None, "Sent", None, GMEET)
    )
    async with h.engine.begin() as conn:
        await conn.execute(
            text("UPDATE meetings SET status = 'active' WHERE id = :id"),
            {"id": live_upstream},
        )
    async with h.store.room_lock(1, [GROOM]) as tx:
        got = await tx.room_meetings(1, GROOM)
        assert await tx.room_meetings(2, GROOM) == []
    ids = [m.id for m in got]
    assert upstream not in ids
    assert ids == [
        await h.meeting_id(first),
        await h.meeting_id(second),
        live_upstream,
    ]
    assert [len(m.entries) for m in got] == [1, 1, 0]
    assert got[2].aw is None


async def test_record_spawn_error_creates_the_aw_row_under_the_meeting_lock(make_pg):
    h = make_pg()
    mid = await h.seed_upstream(
        GROOM, Plan(ts("2026-09-29T15:00:00Z"), None, None, None, GMEET)
    )
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(" ".join(statement.split()))

    event.listen(h.engine.sync_engine, "before_cursor_execute", record)
    try:
        async with h.store.room_lock(1, [GROOM]) as tx:
            await tx.record_spawn_error(mid, "account_limit", "bot limit reached")
    finally:
        event.remove(h.engine.sync_engine, "before_cursor_execute", record)

    order = [
        i
        for i, s in enumerate(statements)
        if "pg_advisory_xact_lock" in s
        or s.endswith("FOR UPDATE")
        or s.startswith("INSERT INTO meeting_aw_state")
    ]
    kinds = [
        (
            "link"
            if "pg_advisory_xact_lock" in statements[i]
            else (
                "meeting"
                if "FROM meetings " in statements[i]
                else "aw-insert" if statements[i].startswith("INSERT") else "aw"
            )
        )
        for i in order
    ]
    assert kinds == ["link", "meeting", "aw", "aw-insert"], statements
    got = await rows(
        h.engine,
        "SELECT last_error_code, last_error_message, event_seq FROM meeting_aw_state "
        "WHERE meeting_id = :id",
        id=mid,
    )
    assert [tuple(r) for r in got] == [("account_limit", "bot limit reached", 0)]
    assert await scalar(
        h.engine, "SELECT status FROM meetings WHERE id = :id", id=mid
    ) == ("scheduled")


async def test_count_active_entries_is_an_index_only_count_on_the_partial_index(
    make_pg,
):
    h = make_pg()
    mid = await h.seed_upstream(
        GROOM, Plan(ts("2026-09-29T15:00:00Z"), None, None, None, GMEET)
    )
    async with h.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO meeting_entries (user_id, source_user, external_id, meeting_id, "
                "meeting_url, platform, native_meeting_id, start_at, content_hash, state) "
                "SELECT (g % 50) + 1, 'a@abroadworks.com', 'e' || g, :mid, :url, 'google_meet', "
                "'kxo-misr-avz', now(), 'h', "
                "CASE WHEN g % 10 = 0 THEN 'active' ELSE 'closed' END "
                "FROM generate_series(1, 20000) AS g"
            ),
            {"mid": mid, "url": GMEET},
        )
    async with h.engine.connect() as conn:
        auto = await conn.execution_options(isolation_level="AUTOCOMMIT")
        await auto.exec_driver_sql("VACUUM ANALYZE meeting_entries")

    captured: list[tuple[str, Any]] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        if "count(" in statement:
            captured.append((statement, parameters))

    event.listen(h.engine.sync_engine, "before_cursor_execute", record)
    try:
        async with h.store.room_lock(1, ()) as tx:
            got = await tx.count_active_entries(1)
    finally:
        event.remove(h.engine.sync_engine, "before_cursor_execute", record)

    truth = await scalar(
        h.engine,
        "SELECT count(*) FROM meeting_entries WHERE user_id = 1 AND state = 'active'",
    )
    assert got == truth == 400
    ((statement, parameters),) = captured

    def nodes(plan: dict) -> list[dict]:
        return [plan] + [n for child in plan.get("Plans", []) for n in nodes(child)]

    assert parameters == (1,)
    async with h.engine.connect() as conn:
        await conn.exec_driver_sql(f"PREPARE active_count AS {statement}")
        for mode in ("force_custom_plan", "force_generic_plan"):
            # the generic plan is the one a cached prepared statement settles on
            await conn.exec_driver_sql(f"SET plan_cache_mode = {mode}")
            plan = (
                await conn.exec_driver_sql(
                    "EXPLAIN (FORMAT JSON) EXECUTE active_count(1)"
                )
            ).scalar_one()
            plan = json.loads(plan) if isinstance(plan, str) else plan
            scans = [n for n in nodes(plan[0]["Plan"]) if "Scan" in n["Node Type"]]
            assert [(n["Node Type"], n.get("Index Name")) for n in scans] == [
                ("Index Only Scan", "ix_meeting_entries_active_user")
            ], (mode, plan)


# ── races through the service ────────────────────────────────────────────────────────────────


async def test_two_concurrent_puts_for_one_link_and_time_make_one_meeting(
    make_pg, holder
):
    h = make_pg()
    await holder.hold(1, GROOM)
    racers = asyncio.gather(h.put("g:a", user=A), h.put("g:b", user=B))
    await wait_for_waiters(h.engine, 2)
    started = time.monotonic()
    await holder.release()
    first, second = await asyncio.wait_for(racers, 5)
    elapsed = time.monotonic() - started

    assert sorted([first["result"], second["result"]]) == ["created", "joined_existing"]
    assert first["meeting"]["id"] == second["meeting"]["id"]
    assert await link_rows(h.engine, GROOM) == [
        (await h.meeting_id(first["meeting"]["id"]), "scheduled")
    ]
    assert (
        await scalar(
            h.engine, "SELECT count(*) FROM meeting_entries WHERE state = 'active'"
        )
        == 2
    )
    types = [
        r.event_type
        for r in await rows(
            h.engine, "SELECT event_type FROM webhook_outbox ORDER BY sequence"
        )
    ]
    assert types == ["meeting.scheduled", "meeting.updated"]
    print(f"[A7 timing] two PUTs one link: {elapsed:.3f}s")


async def test_a_duplicate_put_racing_itself_makes_one_meeting_and_replies_unchanged(
    make_pg, holder
):
    h = make_pg()
    await holder.hold(1, GROOM)
    racers = asyncio.gather(h.put(title="Weekly sync"), h.put(title="Weekly sync"))
    await wait_for_waiters(h.engine, 2)
    started = time.monotonic()
    await holder.release()
    first, second = await asyncio.wait_for(racers, 5)
    elapsed = time.monotonic() - started

    replies = sorted([first, second], key=lambda r: r["result"])
    assert [r["result"] for r in replies] == ["created", "unchanged"]
    assert replies[0]["meeting"] == replies[1]["meeting"]
    assert len(await link_rows(h.engine, GROOM)) == 1
    assert await counts(h.engine) == {
        "meetings": 1,
        "meeting_entries": 1,
        "meeting_aw_state": 1,
        "webhook_outbox": 1,
    }
    assert [len(b) for b in h.publisher.batches] == [1]
    print(f"[A7 timing] duplicate PUT racing itself: {elapsed:.3f}s")


async def test_opposite_link_moves_finish_within_five_seconds(make_pg, holder):
    h = make_pg()
    one = (await h.put("g:1", meeting_url=GMEET))["meeting"]["id"]
    two = (
        await h.put(
            "g:2",
            meeting_url=GMEET_OTHER,
            start="2026-09-29T11:00:00Z",
            end="2026-09-29T11:30:00Z",
        )
    )["meeting"]["id"]
    await holder.hold(1, GROOM, GROOM_OTHER)
    racers = asyncio.gather(
        h.put("g:1", meeting_url=GMEET_OTHER),
        h.put(
            "g:2",
            meeting_url=GMEET,
            start="2026-09-29T11:00:00Z",
            end="2026-09-29T11:30:00Z",
        ),
    )
    await wait_for_waiters(h.engine, 2)
    started = time.monotonic()
    await holder.release()
    moved_one, moved_two = await asyncio.wait_for(racers, 5)
    elapsed = time.monotonic() - started
    assert elapsed < 5

    assert (moved_one["result"], moved_one["meeting"]["id"]) == ("updated", one)
    assert (moved_two["result"], moved_two["meeting"]["id"]) == ("updated", two)
    assert moved_one["meeting"]["room"] == GROOM_OTHER.native_meeting_id
    assert moved_two["meeting"]["room"] == GROOM.native_meeting_id
    assert await link_rows(h.engine, GROOM) == [(await h.meeting_id(two), "scheduled")]
    assert await link_rows(h.engine, GROOM_OTHER) == [
        (await h.meeting_id(one), "scheduled")
    ]
    print(f"[A7 timing] opposite link moves: {elapsed:.3f}s")


async def test_two_scheduled_and_one_live_row_share_a_link(make_pg):
    h = make_pg()
    nine = (await h.put("g:9"))["meeting"]["id"]
    eleven = (
        await h.put("g:11", start="2026-09-29T11:00:00Z", end="2026-09-29T11:30:00Z")
    )["meeting"]["id"]
    await h.set_status(nine, "requested")
    await h.set_status(nine, "active")
    one_pm = await h.put(
        "g:13", start="2026-09-29T13:00:00Z", end="2026-09-29T13:30:00Z"
    )
    assert one_pm["result"] == "created"
    assert await link_rows(h.engine, GROOM) == [
        (await h.meeting_id(nine), "active"),
        (await h.meeting_id(eleven), "scheduled"),
        (await h.meeting_id(one_pm["meeting"]["id"]), "scheduled"),
    ]
    from sqlalchemy.exc import IntegrityError

    # only the live row is unique per link: a second bot on the link is refused by the index
    with pytest.raises(IntegrityError, match="uq_meeting_live_user_platform_native"):
        await h.set_status(eleven, "requested")
    assert (await link_rows(h.engine, GROOM))[1] == (
        await h.meeting_id(eleven),
        "scheduled",
    )


async def test_a_forced_exception_rolls_back_the_state_and_the_outbox(
    make_pg, monkeypatch
):
    h = make_pg()
    await h.put()
    before = await counts(h.engine)
    batches = list(h.publisher.batches)
    real = adapters_mod.write_event
    written: list[str] = []

    async def write_then_fail(*args: Any, **kwargs: Any):
        result = await real(*args, **kwargs)
        written.append(result.event_id)
        raise RuntimeError("storage failed after the outbox row")

    monkeypatch.setattr(adapters_mod, "write_event", write_then_fail)
    with pytest.raises(RuntimeError):
        await h.put(
            "g:second", start="2026-09-30T09:00:00Z", end="2026-09-30T09:30:00Z"
        )
    assert len(written) == 1  # the outbox row was written inside the transaction
    assert await counts(h.engine) == before
    assert (
        await scalar(
            h.engine,
            "SELECT count(*) FROM webhook_outbox WHERE event_id = :e",
            e=written[0],
        )
        == 0
    )
    assert h.publisher.batches == batches


async def test_instant_join_racing_the_scheduler_is_one_bot_and_joined_existing(
    make_pg,
):
    """The scheduler claims the new row under its link lock and holds its transaction open; the
    spawn path's claim parks on the same link lock, then finds the row claimed."""
    h = make_pg()
    claimed = asyncio.Event()
    release = asyncio.Event()
    created: asyncio.Future[int] = asyncio.get_running_loop().create_future()

    async def scheduler() -> bool:
        mid = await created
        return await claim(h.store, 1, mid, hold=hold)

    async def hold() -> None:
        claimed.set()
        await release.wait()

    async def spawn_before(meeting_id: int) -> None:
        created.set_result(meeting_id)
        await claimed.wait()

    h.spawn.before = spawn_before

    async def conductor() -> float:
        await wait_for_waiters(h.engine, 1)  # the spawn's claim is parked on the link
        started = time.monotonic()
        release.set()
        return started

    reply, scheduler_won, started = await asyncio.wait_for(
        asyncio.gather(h.instant("manual:1", GMEET), scheduler(), conductor()), 5
    )
    elapsed = time.monotonic() - started
    assert scheduler_won is True
    assert (reply["result"], reply["meeting"]["status"]) == (
        "joined_existing",
        "requested",
    )
    changes = [
        json.loads(r.payload_text)["data"]["change"]
        for r in await rows(
            h.engine,
            "SELECT payload_text FROM webhook_outbox "
            "WHERE event_type = 'meeting.status_change' ORDER BY sequence",
        )
    ]
    assert [(c["from"], c["to"]) for c in changes] == [("scheduled", "requested")]
    assert len(h.spawn.calls) == 1
    print(f"[A7 timing] instant join vs scheduler: {elapsed:.3f}s")


async def test_instant_join_and_the_scheduler_free_for_all_send_one_bot(make_pg):
    """No fixed interleaving: whoever claims first sends the bot, the other finds it claimed.
    Which side wins each round is up to the scheduler of the two sessions; only the outcome is
    asserted."""
    h = make_pg()
    outcomes = []
    for n in range(10):
        created: asyncio.Future[int] = asyncio.get_running_loop().create_future()

        async def spawn_before(
            meeting_id: int, fut=created, lag=0.03 * (n % 2)
        ) -> None:
            fut.set_result(meeting_id)
            await asyncio.sleep(
                lag
            )  # every other round the scheduler gets a head start

        async def scheduler(fut=created) -> bool:
            return await claim(h.store, 1, await fut)

        h.spawn.before = spawn_before
        reply, scheduler_won = await asyncio.wait_for(
            asyncio.gather(
                h.instant(
                    f"manual:{n}", f"https://meet.google.com/aaa-bbbb-cc{chr(97 + n)}"
                ),
                scheduler(),
            ),
            5,
        )
        assert reply["meeting"]["status"] == "requested"
        assert reply["result"] == ("joined_existing" if scheduler_won else "created")
        outcomes.append(scheduler_won)
    requested = await scalar(
        h.engine,
        "SELECT count(*) FROM webhook_outbox WHERE event_type = 'meeting.status_change'",
    )
    assert requested == 10  # one claim per meeting, never two
    print(f"[A7] free-for-all scheduler wins: {sum(outcomes)}/10")


# ── parity with the in-memory fake ───────────────────────────────────────────────────────────


async def _set(h: Any, uuid: str, status: str, **kwargs: Any) -> None:
    result = h.set_status(uuid, status, **kwargs)
    if inspect.isawaitable(result):
        await result


async def _seed_upstream(h: Any, room: Room, plan: Plan) -> None:
    if isinstance(h, PgHarness):
        await h.seed_upstream(room, plan)
    else:
        h.store.seed_meeting(1, room, status="scheduled", plan=plan)


async def s_2_6_1(h: Any) -> list[dict]:
    return [
        await h.put(
            time_zone="Asia/Kolkata", title="Weekly sync", attendees=[A, B, "c@x.com"]
        )
    ]


async def s_2_6_2_instant(h: Any) -> list[dict]:
    return [await h.instant("manual:1")]


async def s_removed_comes_back(h: Any) -> list[dict]:
    first = await h.put()
    joined = await h.put(user=B)
    removed = await h.remove(user=B, reason="declined")
    return [first, joined, removed, await h.put(user=B)]


async def s_2_6_2_spawn_fails(h: Any) -> list[dict]:
    return [await h.instant("manual:1")]


async def s_2_6_6(h: Any) -> list[dict]:
    first = await h.put("google:9dstandupexample_20260930T043000Z")
    return [
        first,
        await h.remove("google:9dstandupexample_20260930T043000Z", reason="cancelled"),
    ]


async def s_2_6_6_live(h: Any) -> list[dict]:
    first = await h.put()
    h.clock.set("2026-09-29T09:01:00Z")
    await _set(h, first["meeting"]["id"], "active")
    return [first, await h.remove(reason="cancelled")]


async def s_2_6_9_link_changed(h: Any) -> list[dict]:
    return [await h.put(), await h.put(meeting_url=GMEET_OTHER)]


async def s_2_6_12(h: Any) -> list[dict]:
    return [await h.put(title="Weekly sync"), await h.put(user=B, title="Weekly sync")]


async def s_2_6_16_same_time(h: Any) -> list[dict]:
    first = await h.put(start="2026-09-29T09:00:00Z", end="2026-09-29T10:00:00Z")
    h.clock.set("2026-09-29T09:00:00Z")
    await _set(h, first["meeting"]["id"], "active")
    h.clock.set("2026-09-29T09:20:00Z")
    await _set(h, first["meeting"]["id"], "completed")
    h.clock.set("2026-09-29T09:30:00Z")
    again = await h.put(
        start="2026-09-29T09:00:00Z", end="2026-09-29T10:00:00Z", title="Renamed"
    )
    return [first, again]


async def s_2_6_16_future_time(h: Any) -> list[dict]:
    first = await h.put(start="2026-09-29T09:00:00Z", end="2026-09-29T10:00:00Z")
    h.clock.set("2026-09-29T10:00:00Z")
    await _set(
        h,
        first["meeting"]["id"],
        "failed",
        outcome=Outcome("not_sent", "ended_before_sent", "the meeting ended"),
    )
    h.clock.set("2026-09-29T11:00:00Z")
    later = await h.put(start="2026-09-29T15:00:00Z", end="2026-09-29T16:00:00Z")
    return [first, later]


async def s_r12(h: Any) -> list[dict]:
    planned = await h.put(start="2026-09-29T10:00:00Z", end="2026-09-29T10:30:00Z")
    h.clock.set("2026-09-29T09:45:00Z")
    return [planned, await h.instant("manual:1", GMEET)]


async def s_r15_upstream_row(h: Any) -> list[dict]:
    await _seed_upstream(
        h,
        GROOM,
        Plan(ts("2026-09-29T10:00:00Z"), None, "Planned upstream", None, GMEET),
    )
    return [await h.instant("manual:1", GMEET)]


async def s_merge_into_live(h: Any) -> list[dict]:
    live = await h.instant("manual:1", GMEET)
    due = await h.put(start="2026-09-26T12:30:00Z", end="2026-09-26T13:00:00Z")
    live_id, due_id = live["meeting"]["id"], due["meeting"]["id"]
    if isinstance(h, PgHarness):
        live_mid, due_mid = await h.meeting_id(live_id), await h.meeting_id(due_id)
    else:
        live_mid, due_mid = h.meeting_id(live_id), h.meeting_id(due_id)
    merged = await h.service.merge_into_live(1, due_mid, live_mid)
    return [live, due, {"merged": merged}]


PARITY = [
    ("2.6.1", s_2_6_1, "2026-09-26T12:00:00Z", None),
    ("2.6.2", s_2_6_2_instant, "2026-09-26T12:00:00Z", None),
    ("removed comes back", s_removed_comes_back, "2026-09-26T12:00:00Z", None),
    ("2.6.2 spawn fails", s_2_6_2_spawn_fails, "2026-09-26T12:00:00Z", ACCOUNT_LIMIT),
    ("2.6.6", s_2_6_6, "2026-09-26T12:00:00Z", None),
    ("2.6.6 live", s_2_6_6_live, "2026-09-26T12:00:00Z", None),
    ("2.6.9", s_2_6_9_link_changed, "2026-09-26T12:00:00Z", None),
    ("2.6.12", s_2_6_12, "2026-09-26T12:00:00Z", None),
    ("2.6.16 same", s_2_6_16_same_time, "2026-09-26T12:00:00Z", None),
    ("2.6.16 future", s_2_6_16_future_time, "2026-09-26T12:00:00Z", None),
    ("R12", s_r12, "2026-09-29T09:00:00Z", ACCOUNT_LIMIT),
    ("R15", s_r15_upstream_row, "2026-09-29T09:45:00Z", ACCOUNT_LIMIT),
    ("R2 merge", s_merge_into_live, "2026-09-26T12:00:00Z", None),
]

AW_KEYS = (
    "scheduled_end_at",
    "time_zone",
    "event_seq",
    "outcome_kind",
    "outcome_detail",
    "outcome_message",
    "outcome_at",
    "last_error_code",
    "last_error_message",
)


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "isoformat"):
        return iso_utc(value)
    return value


def _relabel(value: Any, labels: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {k: _relabel(v, labels) for k, v in value.items()}
    if isinstance(value, list):
        return [_relabel(v, labels) for v in value]
    if isinstance(value, str):
        for uuid, label in labels.items():
            value = value.replace(uuid, label)
    return value


def _meeting_state(view: Any) -> dict:
    # an upstream row without a ``meeting_aw_state`` row reads as the fake's all-default one
    aw = view.aw if view.aw is not None else {"event_seq": 0}
    return {
        "status": view.status,
        "platform": view.row["platform"],
        "room": view.row["platform_specific_id"],
        "data": dict(view.data),
        "aw": {k: aw.get(k) for k in AW_KEYS},
        "entries": [
            {
                k: v
                for k, v in e.row().items()
                if k not in ("id", "meeting_id", "user_id")
            }
            for e in view.entries
        ],
    }


def _fake_snapshot(h: Any, replies: list[dict]) -> dict:
    views = [h.store.view(mid) for mid in sorted(h.store.meetings)]
    labels = {v.uuid: f"<m{n}>" for n, v in enumerate(views)}
    by_id = {v.id: labels[v.uuid] for v in views}
    event_key = {
        e.event_id: (labels[e.meeting_uuid], e.event_type, e.sequence)
        for e in h.store.events
    }
    events = sorted(
        (
            labels[e.meeting_uuid],
            e.sequence,
            e.event_type,
            e.change,
            e.meeting,
            e.event_data or {},
        )
        for e in h.store.events
    )
    return _relabel(
        _jsonable(
            {
                "replies": replies,
                "meetings": [_meeting_state(v) for v in views],
                "events": [list(e) for e in events],
                "published": [[event_key[i] for i in b] for b in h.publisher.batches],
                "spawns": [by_id[mid] for _, mid in h.spawn.calls],
                "stops": [(by_id[mid], out) for _, mid, out in h.stop.calls],
            }
        ),
        labels,
    )


async def _pg_snapshot(h: PgHarness, replies: list[dict]) -> dict:
    ids = [r.id for r in await rows(h.engine, "SELECT id FROM meetings ORDER BY id")]
    views = [await read_meeting(h.store, 1, mid) for mid in ids]
    labels = {v.uuid: f"<m{n}>" for n, v in enumerate(views)}
    by_id = {v.id: labels[v.uuid] for v in views}
    outbox = await rows(
        h.engine,
        "SELECT event_id, meeting_id, event_type, sequence, payload_text FROM webhook_outbox",
    )
    event_key = {
        r.event_id: (by_id[r.meeting_id], r.event_type, r.sequence) for r in outbox
    }
    events = []
    for r in outbox:
        data = json.loads(r.payload_text)["data"]
        meeting, change = data.pop("meeting"), data.pop("change", None)
        events.append(
            (by_id[r.meeting_id], r.sequence, r.event_type, change, meeting, data)
        )
    return _relabel(
        _jsonable(
            {
                "replies": replies,
                "meetings": [_meeting_state(v) for v in views],
                "events": [list(e) for e in sorted(events)],
                "published": [[event_key[i] for i in b] for b in h.publisher.batches],
                "spawns": [by_id[mid] for _, mid in h.spawn.calls],
                "stops": [(by_id[mid], out) for _, mid, out in h.stop.calls],
            }
        ),
        labels,
    )


@pytest.mark.parametrize(
    "scenario, now, failure", [p[1:] for p in PARITY], ids=[p[0] for p in PARITY]
)
async def test_the_postgres_store_matches_the_fake(make_pg, scenario, now, failure):
    fake = make_harness(now, spawn_failure=failure)
    fake_replies = await scenario(fake)
    pg_h = make_pg(now, spawn_failure=failure)
    pg_replies = await scenario(pg_h)

    expected = _fake_snapshot(fake, fake_replies)
    actual = await _pg_snapshot(pg_h, pg_replies)
    for key in expected:
        assert actual[key] == expected[key], key
