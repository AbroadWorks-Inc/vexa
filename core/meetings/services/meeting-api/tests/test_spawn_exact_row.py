"""§1.5 — the spawn claims the exact row, dedups against the full live set, and every failure maps
to its typed code and exact message.

Three groups:
  * the claim — ``create_meeting_guarded`` with and without ``claim_meeting_id``, run against the
    in-memory fake AND the SQLAlchemy adapter on real Postgres with the same assertions (the parity
    the fake owes the adapter). With an id, exactly that row is claimed; without one (upstream
    ``POST /bots``), the R1 ``join_now`` rule picks the row, else a new row is inserted — never a
    future occurrence. A live row on the link (``needs_help`` and ``stopping`` included) blocks;
  * the production ``SpawnPort`` (``intake/spawn.py``) — ``spawn_exact`` answers ``sent`` /
    ``already_live`` / ``failed`` with the §1.5 code and exact message, stamps
    ``data.auto_join_last_attempt`` in the claim (Ruling R7), and never lets an exception out;
  * Postgres only — the lock order (link lock, then the per-user lock, then the row), no deadlock
    between the exact-row claim and upstream ``POST /bots`` on one link, and an instant join driven
    through the real ``IntakeService`` with the real port.

The Postgres cases skip cleanly unless ``MEETING_API_TEST_DATABASE_URL`` is set; see
``test_intake_pg_schema.py``'s docstring for the ephemeral SQLAlchemy/asyncpg install.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import pytest

from meeting_api.bot_spawn import request_bot
from meeting_api.bot_spawn.fakes import FakeRuntimeClient, InMemoryMeetingRepo
from meeting_api.bot_spawn.ports import (
    AuthSessionBusy,
    AuthSessionNotConfigured,
    ClaimTargetMoved,
    DuplicateMeeting,
    MaxBotsExceeded,
    MeetingStopped,
    QuotaExceeded,
    SpawnFailed,
    TranscriptionNotConfigured,
)
from meeting_api.intake import spawn as spawn_mod
from meeting_api.intake.ports import SpawnOutcome
from meeting_api.intake.spawn import ExactRowSpawn, spawn_failure
from meeting_api.service_authority import (
    ServiceAuthorityDenied,
    ServiceAuthorityUnavailable,
)

USER = 7
PLAT, NID = "google_meet", "kxo-misr-avz"
OTHER_NID = "abc-defg-hij"
SPAWN_DATA = {"constructed_meeting_url": f"https://meet.google.com/{NID}", "k": "v"}
PG_URL = os.getenv("MEETING_API_TEST_DATABASE_URL")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


# ── the two backends: the in-memory fake and the adapter on real Postgres ───────────────────


class FakeBackend:
    name = "fake"

    def __init__(self) -> None:
        self.repo = InMemoryMeetingRepo()
        self._next = 100

    async def seed(
        self,
        *,
        status: str = "scheduled",
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        native: str = NID,
        user_id: int = USER,
        data: Optional[dict] = None,
    ) -> int:
        self._next += 1
        mid = self._next
        payload = {"title": f"row {mid}", "auto_join": True, **(data or {})}
        if start is not None:
            payload["scheduled_at"] = _iso(start)
        self.repo._meetings[mid] = {
            "id": mid,
            "user_id": user_id,
            "platform": PLAT,
            "native_meeting_id": native,
            "platform_specific_id": native,
            "status": status,
            "bot_container_id": None,
            "start_time": None,
            "end_time": None,
            "data": payload,
            "scheduled_end_at": end,
            "created_at": "2026-09-01T09:00:00Z",
            "updated_at": "2026-09-01T09:00:00Z",
        }
        return mid

    async def row(self, mid: int) -> dict:
        return dict(self.repo._meetings[mid])

    async def ids(self) -> set[int]:
        return set(self.repo._meetings)


class PgBackend:
    name = "pg"

    def __init__(self, engine: Any) -> None:
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from meeting_api.bot_spawn.adapters import SqlAlchemyMeetingRepo

        self.engine = engine
        self.session_factory = async_sessionmaker(engine, expire_on_commit=False)
        self.repo = SqlAlchemyMeetingRepo(self.session_factory)

    async def seed(
        self,
        *,
        status: str = "scheduled",
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        native: str = NID,
        user_id: int = USER,
        data: Optional[dict] = None,
    ) -> int:
        from sqlalchemy import text

        payload = {"auto_join": True, **(data or {})}
        if start is not None:
            payload["scheduled_at"] = _iso(start)
        async with self.engine.begin() as conn:
            mid = (
                await conn.execute(
                    text(
                        "INSERT INTO meetings (user_id, platform, platform_specific_id, status, data) "
                        "VALUES (:u, :p, :n, :s, CAST(:d AS jsonb)) RETURNING id"
                    ),
                    {
                        "u": user_id,
                        "p": PLAT,
                        "n": native,
                        "s": status,
                        "d": json.dumps(payload),
                    },
                )
            ).scalar_one()
            await conn.execute(
                text(
                    "UPDATE meetings SET data = data || CAST(:d AS jsonb) WHERE id = :m"
                ),
                {"m": mid, "d": json.dumps({"title": f"row {mid}"})},
            )
            if end is not None:
                await conn.execute(
                    text(
                        "INSERT INTO meeting_aw_state (meeting_id, scheduled_end_at) "
                        "VALUES (:m, :e)"
                    ),
                    {"m": mid, "e": end},
                )
        return int(mid)

    async def row(self, mid: int) -> dict:
        row = await self.repo.get_meeting(mid)
        assert row is not None
        return row

    async def ids(self) -> set[int]:
        from sqlalchemy import text

        async with self.engine.connect() as conn:
            return {
                r[0]
                for r in (await conn.execute(text("SELECT id FROM meetings"))).all()
            }

    async def scalar(self, sql: str, **params: Any) -> Any:
        from sqlalchemy import text

        async with self.engine.connect() as conn:
            return (await conn.execute(text(sql), params)).scalar_one()


async def _pg_engine():
    """A fresh engine on ``MEETING_API_TEST_DATABASE_URL`` with the admin-api schema built, every
    table dropped first (``conftest.intake_pg_engine``'s lifecycle, inline so the parametrised
    ``backend`` fixture can build it only for its Postgres leg)."""
    if not PG_URL:
        pytest.skip(
            "real-Postgres parity for §1.5; set MEETING_API_TEST_DATABASE_URL to run"
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


async def _drop(engine) -> None:
    from admin_api.schema import models as admin_models

    async with engine.begin() as conn:
        await conn.run_sync(admin_models.Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture
async def pg_engine():
    engine = await _pg_engine()
    yield engine
    await _drop(engine)


@pytest.fixture(params=["fake", "pg"])
async def backend(request):
    if request.param == "fake":
        yield FakeBackend()
        return
    engine = await _pg_engine()
    yield PgBackend(engine)
    await _drop(engine)


async def _guarded(
    backend,
    *,
    claim: Optional[int] = None,
    cap: Optional[int] = None,
    native: str = NID,
) -> dict:
    return await backend.repo.create_meeting_guarded(
        user_id=USER,
        platform=PLAT,
        native_meeting_id=native,
        data=dict(SPAWN_DATA),
        max_concurrent=cap,
        claim_meeting_id=claim,
    )


# ── the claim with a row id ──────────────────────────────────────────────────────────────────


async def test_id_claim_takes_exactly_that_row(backend):
    now = _now()
    today = await backend.seed(
        start=now + timedelta(minutes=5), end=now + timedelta(minutes=35)
    )
    # The newest planned row on the link: upstream's claim picked it; the exact claim never does.
    newer = await backend.seed(start=now + timedelta(days=1, minutes=5))
    row = await _guarded(backend, claim=today)
    assert row["id"] == today and row["status"] == "requested"
    assert (await backend.row(newer))["status"] == "scheduled"
    data = (await backend.row(today))["data"]
    assert (
        data["title"] == f"row {today}" and data["k"] == "v"
    )  # spawn keys merge over the plan
    stamp = datetime.fromisoformat(
        data["auto_join_last_attempt"].replace("Z", "+00:00")
    )
    assert abs((stamp - now).total_seconds()) < 30  # R7: the send time


@pytest.mark.parametrize("live", ["needs_help", "stopping", "active"])
async def test_id_claim_is_blocked_by_any_live_row_on_the_link(backend, live):
    await backend.seed(status=live)
    target = await backend.seed(start=_now())
    with pytest.raises(DuplicateMeeting):
        await _guarded(backend, claim=target)
    assert (await backend.row(target))["status"] == "scheduled"


async def test_id_claim_of_a_live_target_is_already_live(backend):
    target = await backend.seed(status="active")
    with pytest.raises(DuplicateMeeting):
        await _guarded(backend, claim=target)


@pytest.mark.parametrize("status", ["failed", "completed", "idle"])
async def test_id_claim_of_a_row_no_longer_scheduled_is_meeting_stopped(
    backend, status
):
    target = await backend.seed(status=status)
    with pytest.raises(MeetingStopped, match=f"meeting {target} is {status}"):
        await _guarded(backend, claim=target)
    assert (await backend.row(target))["status"] == status


async def test_id_claim_of_a_row_on_another_link_is_refused(backend):
    target = await backend.seed(native=OTHER_NID, start=_now())
    with pytest.raises(ClaimTargetMoved):
        await _guarded(backend, claim=target)
    assert (await backend.row(target))["status"] == "scheduled"


async def test_id_claim_of_another_users_row_is_refused(backend):
    target = await backend.seed(user_id=USER + 1, start=_now())
    with pytest.raises(LookupError):
        await _guarded(backend, claim=target)
    assert (await backend.row(target))["status"] == "scheduled"


async def test_id_claim_at_the_cap_carries_the_numbers(backend):
    await backend.seed(status="active", native="aaa-bbbb-ccc")
    await backend.seed(status="joining", native="ddd-eeee-fff")
    target = await backend.seed(start=_now())
    with pytest.raises(MaxBotsExceeded) as caught:
        await _guarded(backend, claim=target, cap=2)
    assert (caught.value.active, caught.value.cap) == (2, 2)
    assert (await backend.row(target))["status"] == "scheduled"


# ── upstream POST /bots: the R1 join_now rule, else insert ───────────────────────────────────


async def test_post_bots_at_0955_claims_todays_1000_row(backend):
    now = _now()  # "09:55"
    today = await backend.seed(
        start=now + timedelta(minutes=5), end=now + timedelta(minutes=35)
    )
    tomorrow = await backend.seed(
        start=now + timedelta(days=1, minutes=5),
        end=now + timedelta(days=1, minutes=35),
    )
    row = await _guarded(backend)
    assert row["id"] == today and row["status"] == "requested"
    assert (await backend.row(tomorrow))["status"] == "scheduled"


async def test_post_bots_at_1040_inserts_after_todays_row_ended(backend):
    now = _now()  # "10:40"
    ended = await backend.seed(
        start=now - timedelta(minutes=40), end=now - timedelta(minutes=10)
    )
    tomorrow = await backend.seed(
        start=now + timedelta(hours=23, minutes=20),
        end=now + timedelta(hours=23, minutes=50),
    )
    before = await backend.ids()
    row = await _guarded(backend)
    assert row["id"] not in before and row["status"] == "requested"
    assert (await backend.row(ended))["status"] == "scheduled"
    assert (await backend.row(tomorrow))["status"] == "scheduled"


async def test_post_bots_never_claims_a_future_occurrence(backend):
    now = _now()
    tomorrow = await backend.seed(
        start=now + timedelta(days=1), end=now + timedelta(days=1, hours=1)
    )
    row = await _guarded(backend)
    assert row["id"] != tomorrow
    assert (await backend.row(tomorrow))["status"] == "scheduled"


async def test_post_bots_claims_the_earliest_adoptable_row(backend):
    now = _now()
    earliest = await backend.seed(
        start=now - timedelta(minutes=10), end=now + timedelta(minutes=20)
    )
    later = await backend.seed(
        start=now + timedelta(minutes=30), end=now + timedelta(minutes=60)
    )
    row = await _guarded(backend)
    assert row["id"] == earliest
    assert (await backend.row(later))["status"] == "scheduled"


async def test_post_bots_claims_an_open_ended_planned_row(backend):
    """An upstream-planned row with no end (``POST /meetings`` then ``POST /bots``) stays claimable,
    from ``idle`` as from ``scheduled``."""
    idle = await backend.seed(status="idle")
    row = await _guarded(backend)
    assert row["id"] == idle and row["status"] == "requested"


@pytest.mark.parametrize("live", ["needs_help", "stopping", "requested"])
async def test_post_bots_is_blocked_by_any_live_row_on_the_link(backend, live):
    await backend.seed(status=live)
    with pytest.raises(DuplicateMeeting):
        await _guarded(backend)


# ── request_bot with claim_meeting_id ────────────────────────────────────────────────────────


async def test_request_bot_spawns_on_the_exact_row(backend):
    now = _now()
    target = await backend.seed(start=now + timedelta(minutes=5))
    await backend.seed(
        start=now + timedelta(minutes=5)
    )  # a sibling the claim must not touch
    runtime = FakeRuntimeClient()
    meeting = await request_bot(
        backend.repo,
        runtime,
        user_id=USER,
        platform=PLAT,
        native_meeting_id=NID,
        token_secret="s",
        redis_url="redis://r",
        claim_meeting_id=target,
    )
    assert meeting["id"] == target
    row = await backend.row(target)
    assert row["status"] == "requested" and row["bot_container_id"]
    assert len(runtime.specs) == 1


# ── the §1.5 failure table ───────────────────────────────────────────────────────────────────

FAILURES = [
    (
        MaxBotsExceeded(USER, 45, active=45),
        "account_limit",
        "bot limit reached (45 of 45)",
    ),
    (MaxBotsExceeded(USER, 0), "account_limit", "bot limit reached (limit 0)"),
    (
        QuotaExceeded("runtime kernel: owner quota exceeded"),
        "account_limit",
        "bot limit reached (runtime kernel: owner quota exceeded)",
    ),
    (
        DuplicateMeeting("An active meeting already exists for google_meet/x"),
        "already_live",
        "An active meeting already exists for google_meet/x",
    ),
    (
        MeetingStopped(
            "meeting 5 is failed; only a scheduled meeting can be sent a bot"
        ),
        "meeting_stopped",
        "meeting 5 is failed; only a scheduled meeting can be sent a bot",
    ),
    (
        SpawnFailed("runtime kernel returned 500: image missing"),
        "spawn_error",
        "runtime kernel returned 500: image missing",
    ),
    (SpawnFailed(), "spawn_error", "bot workload failed to start"),
    (
        ServiceAuthorityDenied("balance_exhausted", "dec-1"),
        "authority_denied",
        "service not allowed (balance_exhausted; decision dec-1)",
    ),
    (
        ServiceAuthorityUnavailable("authority timed out"),
        "authority_unavailable",
        "authority timed out",
    ),
    (
        ServiceAuthorityUnavailable(),
        "authority_unavailable",
        "service authority unavailable",
    ),
    (
        AuthSessionNotConfigured(
            "BOT_AUTHENTICATED is set but the userdata store is incomplete"
        ),
        "auth_session",
        "BOT_AUTHENTICATED is set but the userdata store is incomplete",
    ),
    (
        AuthSessionBusy(12, "s3://b/u"),
        "auth_session",
        "authenticated session 's3://b/u' is in use by active meeting 12 — one stored session runs "
        "one bot at a time",
    ),
    (
        TranscriptionNotConfigured("no transcription backend configured"),
        "transcription_config",
        "no transcription backend configured",
    ),
]


@pytest.mark.parametrize("exc,code,message", FAILURES)
def test_every_spawn_failure_maps_to_its_code_and_exact_message(exc, code, message):
    assert spawn_failure(exc) == (code, message)


def test_anything_else_is_internal_error_logged_with_its_stack(capsys):
    try:
        raise RuntimeError("boom")
    except RuntimeError as exc:
        assert spawn_failure(exc, user_id=USER, meeting_id=5) == (
            "internal_error",
            "internal error (RuntimeError)",
        )
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line["event"] == "spawn_internal_error" and line["level"] == "error"
    assert "RuntimeError: boom" in line["fields"]["traceback"]
    assert 'raise RuntimeError("boom")' in line["fields"]["traceback"]


# ── the production SpawnPort ─────────────────────────────────────────────────────────────────


async def _ctx(_user_id: int) -> dict:
    return {"max_concurrent": 45}


def _port(repo, runtime=None, **kw) -> ExactRowSpawn:
    kw.setdefault("fetch_bot_context", _ctx)
    return ExactRowSpawn(
        repo,
        runtime or FakeRuntimeClient(),
        token_secret="s",
        redis_url="redis://r",
        **kw,
    )


async def test_spawn_exact_sends_the_bot_to_that_row(backend):
    target = await backend.seed(start=_now() + timedelta(minutes=5))
    runtime = FakeRuntimeClient()
    assert await _port(backend.repo, runtime).spawn_exact(USER, target) == SpawnOutcome(
        "sent"
    )
    row = await backend.row(target)
    assert row["status"] == "requested" and "auto_join_last_attempt" in row["data"]
    assert len(runtime.specs) == 1


async def test_spawn_exact_on_a_live_row_is_already_live(backend):
    target = await backend.seed(status="needs_help")
    assert await _port(backend.repo).spawn_exact(USER, target) == SpawnOutcome(
        "already_live"
    )


async def test_spawn_exact_at_the_bot_limit_names_the_numbers(backend):
    for i in range(45):
        await backend.seed(status="active", native=f"n{i:02d}-aaaa-bbb")
    target = await backend.seed(start=_now())
    outcome = await _port(backend.repo).spawn_exact(USER, target)
    assert outcome == SpawnOutcome(
        "failed", "account_limit", "bot limit reached (45 of 45)"
    )
    assert (await backend.row(target))["status"] == "scheduled"


@pytest.mark.parametrize("exc,code,message", FAILURES)
async def test_spawn_exact_returns_every_failure_typed(monkeypatch, exc, code, message):
    repo = FakeBackend()
    target = await repo.seed(start=_now())

    async def raising(*_a, **_k):
        raise exc

    monkeypatch.setattr(spawn_mod, "request_bot", raising)
    outcome = await _port(repo.repo).spawn_exact(USER, target)
    expected = "already_live" if code == "already_live" else "failed"
    if expected == "already_live":
        assert outcome == SpawnOutcome("already_live")
    else:
        assert outcome == SpawnOutcome("failed", code, message)


async def test_spawn_exact_never_raises(monkeypatch):
    repo = FakeBackend()
    target = await repo.seed(start=_now())

    async def raising(*_a, **_k):
        raise KeyError("x")

    monkeypatch.setattr(spawn_mod, "request_bot", raising)
    outcome = await _port(repo.repo).spawn_exact(USER, target)
    assert outcome == SpawnOutcome(
        "failed", "internal_error", "internal error (KeyError)"
    )


async def test_spawn_exact_for_an_unknown_row_is_internal_error():
    outcome = await _port(InMemoryMeetingRepo()).spawn_exact(USER, 999)
    assert outcome == SpawnOutcome(
        "failed", "internal_error", "internal error (LookupError)"
    )


async def test_spawn_exact_refuses_when_the_bot_limit_cannot_be_read():
    repo = FakeBackend()
    target = await repo.seed(start=_now())

    async def unavailable(_user_id: int) -> None:
        return None

    outcome = await _port(repo.repo, fetch_bot_context=unavailable).spawn_exact(
        USER, target
    )
    assert outcome == SpawnOutcome(
        "failed",
        "internal_error",
        "the bot limit could not be read: identity is unavailable",
    )
    outcome = await _port(repo.repo, fetch_bot_context=None).spawn_exact(USER, target)
    assert outcome == SpawnOutcome(
        "failed",
        "internal_error",
        "the bot limit could not be read: no identity edge is configured",
    )
    assert (await repo.row(target))["status"] == "scheduled"


async def test_spawn_exact_rereads_a_row_whose_link_moved_once(monkeypatch):
    repo = FakeBackend()
    target = await repo.seed(start=_now())
    calls: list[str] = []
    real = spawn_mod.request_bot

    async def moved_then_real(*args, **kwargs):
        calls.append(kwargs["native_meeting_id"])
        if len(calls) == 1:
            raise ClaimTargetMoved(target)
        return await real(*args, **kwargs)

    monkeypatch.setattr(spawn_mod, "request_bot", moved_then_real)
    assert await _port(repo.repo).spawn_exact(USER, target) == SpawnOutcome("sent")
    assert len(calls) == 2


# ── Postgres only: lock order, no deadlock, the real port behind IntakeService ──────────────


async def _advisory_waiters(pg: PgBackend) -> int:
    return int(
        await pg.scalar(
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE wait_event_type = 'Lock' AND wait_event = 'advisory'"
        )
    )


async def _wait_for_waiters(pg: PgBackend, n: int) -> None:
    for _ in range(200):
        if await _advisory_waiters(pg) >= n:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the claim never queued on the advisory lock")


async def test_pg_id_claim_takes_the_link_lock_then_the_user_lock(pg_engine):
    from sqlalchemy import text

    pg = PgBackend(pg_engine)
    target = await pg.seed(start=_now())
    link_key = f"aw-intake:{USER}:{PLAT}:{NID}"
    async with pg_engine.connect() as holder:
        tx = await holder.begin()
        # Hold the per-user lock: the claim takes the link lock, then queues on this one.
        await holder.execute(text("SELECT pg_advisory_xact_lock(:u)"), {"u": USER})
        claim = asyncio.create_task(_guarded(pg, claim=target))
        await _wait_for_waiters(pg, 1)
        async with pg_engine.connect() as probe:
            took = (
                await probe.execute(
                    text("SELECT pg_try_advisory_lock(hashtextextended(:k, 0))"),
                    {"k": link_key},
                )
            ).scalar_one()
        assert took is False  # the waiting claim already holds the link lock
        assert not claim.done()
        await tx.rollback()
    row = await claim
    assert row["id"] == target and row["status"] == "requested"


async def test_pg_claim_waits_for_intakes_link_lock(pg_engine):
    from sqlalchemy import text

    pg = PgBackend(pg_engine)
    target = await pg.seed(start=_now())
    async with pg_engine.connect() as holder:
        tx = await holder.begin()
        await holder.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
            {"k": f"aw-intake:{USER}:{PLAT}:{NID}"},
        )
        await holder.execute(
            text("UPDATE meetings SET status = 'failed' WHERE id = :m"), {"m": target}
        )
        claim = asyncio.create_task(_guarded(pg, claim=target))
        await _wait_for_waiters(pg, 1)
        assert not claim.done()
        await tx.commit()  # intake ended the meeting while the claim queued
    with pytest.raises(MeetingStopped):
        await claim


async def test_pg_the_claim_writes_one_status_event_through_the_writer(pg_engine):
    pg = PgBackend(pg_engine)
    target = await pg.seed(start=_now())
    await _guarded(pg, claim=target)
    assert (
        await pg.scalar(
            "SELECT count(*) FROM webhook_outbox WHERE meeting_id = :m", m=target
        )
        == 1
    )
    payload = json.loads(
        await pg.scalar(
            "SELECT payload_text FROM webhook_outbox WHERE meeting_id = :m", m=target
        )
    )
    assert payload["event_type"] == "meeting.status_change"
    assert payload["data"]["change"]["from"] == "scheduled"
    assert payload["data"]["change"]["to"] == "requested"
    assert (
        await pg.scalar(
            "SELECT event_seq FROM meeting_aw_state WHERE meeting_id = :m", m=target
        )
        == 1
    )


async def test_pg_exact_claim_and_post_bots_race_without_deadlock(pg_engine):
    """Ten rounds of the exact-row spawn against upstream ``POST /bots`` on the same link: one
    bot every round, never a deadlock (Postgres would raise ``DeadlockDetected``)."""
    pg = PgBackend(pg_engine)
    for round_ in range(10):
        native = f"r{round_:02d}-race-lnk"
        target = await pg.seed(start=_now(), native=native)

        async def exact():
            try:
                return await _guarded(pg, claim=target, native=native)
            except DuplicateMeeting:
                return None

        async def upstream():
            try:
                return await _guarded(pg, native=native)
            except DuplicateMeeting:
                return None

        results = await asyncio.gather(exact(), upstream(), return_exceptions=True)
        assert not [r for r in results if isinstance(r, BaseException)], results
        live = await pg.scalar(
            "SELECT count(*) FROM meetings WHERE platform_specific_id = :n "
            "AND status IN ('requested','joining','awaiting_admission','needs_help','active',"
            "'stopping')",
            n=native,
        )
        assert live == 1


async def test_pg_instant_join_through_intake_with_the_real_port(pg_engine):
    from intake_builders import ZOOM, instant_body, make_settings
    from meeting_api.intake import PostgresIntakeStore
    from meeting_api.intake.fakes import FakePublisher
    from meeting_api.intake.service import IntakeService

    pg = PgBackend(pg_engine)

    class NoStop:
        async def stop_live(self, user_id, meeting_id, *, outcome):
            raise AssertionError("no stop expected")

    port = _port(pg.repo)
    service = IntakeService(
        PostgresIntakeStore(pg.session_factory),
        port,
        NoStop(),
        FakePublisher(),
        make_settings(),
    )
    sent_at = _now()
    reply = await service.put_entry(USER, instant_body("paste:1", ZOOM))
    assert reply["result"] == "created"
    meeting = reply["meeting"]
    assert meeting["status"] == "requested"
    joins = datetime.fromisoformat(meeting["bot_joins_at"].replace("Z", "+00:00"))
    assert abs((joins - sent_at).total_seconds()) < 30  # R7: the send time


async def test_pg_instant_join_at_the_bot_limit_ends_not_sent(pg_engine):
    from intake_builders import ZOOM, instant_body, make_settings
    from meeting_api.intake import PostgresIntakeStore
    from meeting_api.intake.fakes import FakePublisher
    from meeting_api.intake.service import IntakeService

    pg = PgBackend(pg_engine)
    for i in range(45):
        await pg.seed(status="active", native=f"n{i:02d}-aaaa-bbb")

    class NoStop:
        async def stop_live(self, user_id, meeting_id, *, outcome):
            raise AssertionError("no stop expected")

    service = IntakeService(
        PostgresIntakeStore(pg.session_factory),
        _port(pg.repo),
        NoStop(),
        FakePublisher(),
        make_settings(),
    )
    reply = await service.put_entry(USER, instant_body("paste:2", ZOOM))
    meeting = reply["meeting"]
    assert meeting["status"] == "failed"
    assert meeting["outcome"]["kind"] == "not_sent"
    assert meeting["outcome"]["detail"] == "account_limit"
    assert meeting["outcome"]["message"] == "bot limit reached (45 of 45)"
    assert meeting["bot_joins_at"] is None
