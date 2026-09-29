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
from meeting_api.bot_spawn.auto_join import DueWindow
from meeting_api.bot_spawn.fakes import FakeRuntimeClient, InMemoryMeetingRepo
from meeting_api.bot_spawn.ports import (
    AuthSessionBusy,
    AuthSessionNotConfigured,
    ClaimNotDue,
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
        managed: bool = False,
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
            "has_entries": managed,
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
        managed: bool = False,
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
            if managed:
                await conn.execute(
                    text(
                        "INSERT INTO meeting_entries (user_id, source_user, external_id, "
                        "meeting_id, meeting_url, platform, native_meeting_id, start_at, "
                        "content_hash, state) VALUES (:u, 'a@x', :x, :m, :url, :p, :n, "
                        ":s, 'h', 'active')"
                    ),
                    {
                        "u": user_id,
                        "x": f"e{mid}",
                        "m": mid,
                        "p": PLAT,
                        "n": native,
                        "url": f"https://meet.google.com/{native}",
                        "s": start or _now(),
                    },
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
    due: Optional[DueWindow] = None,
) -> dict:
    return await backend.repo.create_meeting_guarded(
        user_id=USER,
        platform=PLAT,
        native_meeting_id=native,
        data=dict(SPAWN_DATA),
        max_concurrent=cap,
        claim_meeting_id=claim,
        claim_due=due,
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


@pytest.mark.parametrize(
    "live", ["needs_help", "needs_human_help", "stopping", "active"]
)
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


@pytest.mark.parametrize("managed", [True, False])
async def test_id_claim_rechecks_under_the_lock_that_the_row_is_still_due(
    backend, managed
):
    """M2 (§1.5): the scheduler read the row as due at ``now``; by the claim it starts tomorrow.
    Under the lock the claim applies the scheduler's due rule again and refuses (``ClaimNotDue``,
    nothing written); a row still due is claimed."""
    now = _now()
    due = DueWindow(now, lead_s=300, grace_s=600)
    tomorrow = await backend.seed(
        start=now + timedelta(days=1),
        end=now + timedelta(days=1, minutes=30),
        managed=managed,
    )
    with pytest.raises(ClaimNotDue):
        await _guarded(backend, claim=tomorrow, due=due)
    row = await backend.row(tomorrow)
    assert row["status"] == "scheduled" and "k" not in row["data"]

    today = await backend.seed(
        start=now + timedelta(minutes=4),
        end=now + timedelta(minutes=34),
        managed=managed,
    )
    assert (await _guarded(backend, claim=today, due=due))["status"] == "requested"


async def test_id_claim_of_an_entry_managed_row_past_its_end_is_not_due(backend):
    now = _now()
    ended = await backend.seed(
        start=now - timedelta(minutes=40),
        end=now - timedelta(minutes=10),
        managed=True,
    )
    with pytest.raises(ClaimNotDue):
        await _guarded(backend, claim=ended, due=DueWindow(now, 300, 600))
    assert (await backend.row(ended))["status"] == "scheduled"


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


@pytest.mark.parametrize("managed", [True, False])
async def test_post_bots_at_0955_claims_todays_1000_row(backend, managed):
    now = _now()  # "09:55"
    today = await backend.seed(
        start=now + timedelta(minutes=5),
        end=now + timedelta(minutes=35),
        managed=managed,
    )
    tomorrow = await backend.seed(
        start=now + timedelta(days=1, minutes=5),
        end=now + timedelta(days=1, minutes=35),
        managed=managed,
    )
    row = await _guarded(backend)
    assert row["id"] == today and row["status"] == "requested"
    assert (await backend.row(tomorrow))["status"] == "scheduled"


async def test_post_bots_at_1040_inserts_after_todays_row_ended(backend):
    now = _now()  # "10:40"
    ended = await backend.seed(
        start=now - timedelta(minutes=40),
        end=now - timedelta(minutes=10),
        managed=True,
    )
    tomorrow = await backend.seed(
        start=now + timedelta(hours=23, minutes=20),
        end=now + timedelta(hours=23, minutes=50),
        managed=True,
    )
    before = await backend.ids()
    row = await _guarded(backend)
    assert row["id"] not in before and row["status"] == "requested"
    assert (await backend.row(ended))["status"] == "scheduled"
    assert (await backend.row(tomorrow))["status"] == "scheduled"


@pytest.mark.parametrize("managed", [True, False])
async def test_post_bots_never_claims_a_future_occurrence(backend, managed):
    now = _now()
    tomorrow = await backend.seed(
        start=now + timedelta(days=1),
        end=now + timedelta(days=1, hours=1),
        managed=managed,
    )
    row = await _guarded(backend)
    assert row["id"] != tomorrow
    assert (await backend.row(tomorrow))["status"] == "scheduled"


async def test_post_bots_claims_the_earliest_adoptable_entry_managed_row(backend):
    now = _now()
    earliest = await backend.seed(
        start=now - timedelta(minutes=10),
        end=now + timedelta(minutes=20),
        managed=True,
    )
    later = await backend.seed(
        start=now + timedelta(minutes=30),
        end=now + timedelta(minutes=60),
        managed=True,
    )
    row = await _guarded(backend)
    assert row["id"] == earliest
    assert (await backend.row(later))["status"] == "scheduled"


async def test_post_bots_claims_the_newest_entry_less_plan(backend):
    """R18: among upstream-planned (entry-less) rows, upstream's own rule: the newest."""
    now = _now()
    stale = await backend.seed(start=now - timedelta(days=7))
    today = await backend.seed(start=now + timedelta(minutes=5))
    row = await _guarded(backend)
    assert row["id"] == today
    assert (await backend.row(stale))["status"] == "scheduled"


async def test_post_bots_prefers_the_entry_managed_row_by_r1(backend):
    now = _now()
    managed = await backend.seed(
        start=now + timedelta(minutes=5),
        end=now + timedelta(minutes=35),
        managed=True,
    )
    entry_less = await backend.seed(start=now)  # newer: upstream's rule would pick it
    row = await _guarded(backend)
    assert row["id"] == managed
    assert (await backend.row(entry_less))["status"] == "scheduled"


async def test_post_bots_tolerates_an_unparseable_scheduled_at(backend):
    """A planned row's ``scheduled_at`` that isn't ISO-8601 falls through to ``start_time`` /
    ``created_at``, as the auto-join sweep tolerates it; POST /bots still answers."""
    odd = await backend.seed(data={"scheduled_at": "tomorrow 10am"})
    row = await _guarded(backend)
    assert row["id"] == odd and row["status"] == "requested"


async def test_post_bots_claims_an_open_ended_planned_row(backend):
    """An upstream-planned row with no end (``POST /meetings`` then ``POST /bots``) stays claimable,
    from ``idle`` as from ``scheduled``."""
    idle = await backend.seed(status="idle")
    row = await _guarded(backend)
    assert row["id"] == idle and row["status"] == "requested"


@pytest.mark.parametrize(
    "live", ["needs_help", "needs_human_help", "stopping", "requested"]
)
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


class _NoStore:
    """The offline tests never reach a post-claim failure, so the store is never opened."""

    def room_lock(self, user_id, rooms):
        raise AssertionError("no post-claim failure expected")


def _port(repo, runtime=None, *, store=None, **kw) -> ExactRowSpawn:
    kw.setdefault("fetch_bot_context", _ctx)
    return ExactRowSpawn(
        repo,
        runtime or FakeRuntimeClient(),
        store=store or _NoStore(),
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


async def test_spawn_exact_of_a_row_no_longer_due_is_not_due(backend):
    now = _now()
    target = await backend.seed(start=now + timedelta(days=1))
    runtime = FakeRuntimeClient()
    outcome = await _port(backend.repo, runtime).spawn_exact(
        USER, target, due=DueWindow(now, 300, 600)
    )
    assert outcome == SpawnOutcome("not_due")
    assert (await backend.row(target))["status"] == "scheduled"
    assert runtime.specs == []


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


def _pg_intake(pg: PgBackend, runtime=None, *, send_max_attempts: int = 3, **port_kw):
    """``IntakeService`` over Postgres with the real ``ExactRowSpawn`` over the same database."""
    from intake_builders import make_settings
    from meeting_api.intake import PostgresIntakeStore
    from meeting_api.intake.fakes import FakePublisher, NoStop
    from meeting_api.intake.service import IntakeService

    store = PostgresIntakeStore(pg.session_factory)
    publisher = FakePublisher()
    port = _port(pg.repo, runtime, store=store, publisher=publisher, **port_kw)
    service = IntakeService(
        store,
        port,
        NoStop(),
        publisher,
        make_settings(send_max_attempts=send_max_attempts),
    )
    return service, publisher


async def test_pg_instant_join_through_intake_with_the_real_port(pg_engine):
    from intake_builders import ZOOM, instant_body

    pg = PgBackend(pg_engine)
    service, _ = _pg_intake(pg)
    sent_at = _now()
    reply = await service.put_entry(USER, instant_body("paste:1", ZOOM))
    assert reply["result"] == "created"
    meeting = reply["meeting"]
    assert meeting["status"] == "requested"
    joins = datetime.fromisoformat(meeting["bot_joins_at"].replace("Z", "+00:00"))
    assert abs((joins - sent_at).total_seconds()) < 30  # R7: the send time


async def test_pg_instant_join_at_the_bot_limit_is_tried_again(pg_engine):
    """§6.9 F-K: the first failed send is attempt 1; the meeting stays scheduled for the next."""
    from intake_builders import ZOOM, instant_body

    pg = PgBackend(pg_engine)
    for i in range(45):
        await pg.seed(status="active", native=f"n{i:02d}-aaaa-bbb")
    service, _ = _pg_intake(pg)
    reply = await service.put_entry(USER, instant_body("paste:2", ZOOM))
    assert (reply["result"], reply["meeting"]["status"]) == ("created", "scheduled")
    assert reply["meeting"]["outcome"] is None


async def test_pg_instant_join_at_the_bot_limit_ends_not_sent_on_its_last_send(
    pg_engine,
):
    from intake_builders import ZOOM, instant_body

    pg = PgBackend(pg_engine)
    for i in range(45):
        await pg.seed(status="active", native=f"n{i:02d}-aaaa-bbb")
    service, _ = _pg_intake(pg, send_max_attempts=1)
    reply = await service.put_entry(USER, instant_body("paste:2", ZOOM))
    meeting = reply["meeting"]
    assert reply["result"] == "created"
    assert meeting["status"] == "failed"
    assert meeting["outcome"]["kind"] == "not_sent"
    assert meeting["outcome"]["detail"] == "account_limit"
    assert meeting["outcome"]["message"] == "bot limit reached (45 of 45)"
    assert meeting["bot_joins_at"] is None


# ── post-claim failures end not_sent (Ruling R17), driven through the real request_bot ──────


async def _outcome_row(pg: PgBackend, mid: int) -> dict:
    from sqlalchemy import text

    async with pg.engine.connect() as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT m.status, a.outcome_kind, a.outcome_detail, a.outcome_message "
                    "FROM meetings m JOIN meeting_aw_state a ON a.meeting_id = m.id "
                    "WHERE m.id = :m"
                ),
                {"m": mid},
            )
        ).one()
    return dict(row._mapping)


async def _events(pg: PgBackend, mid: int) -> list[dict]:
    from sqlalchemy import text

    async with pg.engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT payload_text FROM webhook_outbox WHERE meeting_id = :m "
                    "ORDER BY sequence"
                ),
                {"m": mid},
            )
        ).all()
    return [json.loads(r[0]) for r in rows]


# (runtime, expected code, expected message, the status upstream leaves the claimed row in)
POST_CLAIM = [
    pytest.param(
        {"fail": True},
        "spawn_error",
        "kernel could not start the workload",
        "failed",
        id="runtime-spawn-failed",
    ),
    pytest.param(
        {"quota_exceeded": True},
        "account_limit",
        "bot limit reached (owner quota exceeded)",
        "requested",
        id="runtime-quota",
    ),
]


@pytest.mark.parametrize("runtime_kw,code,message,upstream_status", POST_CLAIM)
async def test_pg_post_claim_failure_ends_the_row_not_sent(
    pg_engine, runtime_kw, code, message, upstream_status
):
    from meeting_api.intake import PostgresIntakeStore

    pg = PgBackend(pg_engine)
    target = await pg.seed(start=_now())
    port = _port(
        pg.repo,
        FakeRuntimeClient(**runtime_kw),
        store=PostgresIntakeStore(pg.session_factory),
    )
    outcome = await port.spawn_exact(USER, target)
    assert outcome == SpawnOutcome("failed", code, message)
    assert await _outcome_row(pg, target) == {
        "status": "failed",
        "outcome_kind": "not_sent",
        "outcome_detail": code,
        "outcome_message": message,
    }
    events = await _events(pg, target)
    assert [e["event_type"] for e in events] == [
        "meeting.status_change",
        "meeting.not_sent",
    ]
    last = events[-1]["data"]
    assert last["meeting"]["outcome"]["detail"] == code
    assert last["meeting"]["outcome"]["message"] == message
    # One terminal event either way: the port wrote it for a row still `requested`, the spawn flow
    # wrote it (through the writer, with the same outcome) for the row it failed itself.
    assert last["change"]["from"] == "requested" and last["change"]["to"] == "failed"
    assert last["change"]["reason"] == code


async def test_pg_post_claim_token_failure_ends_not_sent(pg_engine, monkeypatch):
    from meeting_api.intake import PostgresIntakeStore

    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    pg = PgBackend(pg_engine)
    target = await pg.seed(start=_now())
    port = ExactRowSpawn(
        pg.repo,
        FakeRuntimeClient(),
        store=PostgresIntakeStore(pg.session_factory),
        fetch_bot_context=_ctx,
        token_secret=None,
        redis_url="redis://r",
    )
    outcome = await port.spawn_exact(USER, target)
    assert outcome == SpawnOutcome(
        "failed", "internal_error", "internal error (ValueError)"
    )
    assert await _outcome_row(pg, target) == {
        "status": "failed",
        "outcome_kind": "not_sent",
        "outcome_detail": "internal_error",
        "outcome_message": "internal error (ValueError)",
    }
    assert [e["event_type"] for e in await _events(pg, target)] == [
        "meeting.status_change",
        "meeting.not_sent",
    ]


async def test_pg_post_claim_failure_of_a_new_instant_join_replies_created(
    pg_engine, monkeypatch
):
    """With one send allowed (§6.9 F-K2 retries a failure while there are sends left)."""
    from intake_builders import ZOOM, instant_body

    monkeypatch.setenv("BOT_SEND_MAX_ATTEMPTS", "1")
    pg = PgBackend(pg_engine)
    service, publisher = _pg_intake(pg, FakeRuntimeClient(fail=True))
    reply = await service.put_entry(USER, instant_body("paste:3", ZOOM))
    assert reply["result"] == "created"
    meeting = reply["meeting"]
    assert meeting["status"] == "failed"
    assert meeting["outcome"]["kind"] == "not_sent"
    assert meeting["outcome"]["detail"] == "spawn_error"
    assert meeting["outcome"]["message"] == "kernel could not start the workload"
    # The spawn flow wrote the one terminal event (in the outbox, where the outbox publisher picks
    # it up); the port found the meeting ended and wrote no second one.
    assert (
        await pg.scalar(
            "SELECT count(*) FROM webhook_outbox WHERE event_type = 'meeting.not_sent'"
        )
        == 1
    )


async def test_pg_post_claim_failure_of_an_adopted_meeting_replies_joined_existing(
    pg_engine, monkeypatch
):
    """R12 keeps an adopted meeting scheduled only for a failure BEFORE the claim; after the
    claim the meeting itself ended not_sent, and the reply names it. With one send allowed
    (§6.9 F-K2 retries a failure while there are sends left)."""
    from intake_builders import GMEET, entry_body, instant_body

    monkeypatch.setenv("BOT_SEND_MAX_ATTEMPTS", "1")
    pg = PgBackend(pg_engine)
    service, _ = _pg_intake(pg, FakeRuntimeClient(fail=True))
    now = _now()
    planned = await service.put_entry(
        USER,
        entry_body(
            "google:cal-1",
            meeting_url=GMEET,
            start=_iso(now + timedelta(minutes=20)),
            end=_iso(now + timedelta(minutes=50)),
        ),
    )
    reply = await service.put_entry(USER, instant_body("paste:4", GMEET))
    assert reply["result"] == "joined_existing"
    meeting = reply["meeting"]
    assert meeting["id"] == planned["meeting"]["id"]
    assert meeting["status"] == "failed"
    assert meeting["outcome"]["detail"] == "spawn_error"


async def test_pg_pre_claim_failure_of_an_adopted_meeting_keeps_it_scheduled(pg_engine):
    from intake_builders import GMEET, entry_body, instant_body

    pg = PgBackend(pg_engine)
    for i in range(45):
        await pg.seed(status="active", native=f"n{i:02d}-aaaa-bbb")
    service, _ = _pg_intake(pg)
    now = _now()
    planned = await service.put_entry(
        USER,
        entry_body(
            "google:cal-2",
            meeting_url=GMEET,
            start=_iso(now + timedelta(minutes=20)),
            end=_iso(now + timedelta(minutes=50)),
        ),
    )
    reply = await service.put_entry(USER, instant_body("paste:5", GMEET))
    meeting = reply["meeting"]
    assert (reply["result"], meeting["id"], reply["entry"]["state"]) == (
        "joined_existing",
        planned["meeting"]["id"],
        "removed",
    )
    assert (meeting["status"], meeting["outcome"]) == ("scheduled", None)


# ── spawn context, bot name, exclusivity, the live-index backstop ────────────────────────────


async def test_spawn_exact_refuses_a_context_without_a_bot_limit():
    repo = FakeBackend()
    target = await repo.seed(start=_now())

    async def no_limit(_user_id: int) -> dict:
        return {"bot_name": "Scribe"}

    outcome = await _port(repo.repo, fetch_bot_context=no_limit).spawn_exact(
        USER, target
    )
    assert outcome == SpawnOutcome(
        "failed",
        "internal_error",
        "the bot limit could not be read: identity returned no max_concurrent",
    )
    assert (await repo.row(target))["status"] == "scheduled"


async def test_spawn_exact_names_the_bot_as_the_sweep_does():
    repo = FakeBackend()
    target = await repo.seed(
        start=_now(),
        data={"calendar_sources": [{"auto_join": True, "bot_name": "Cal Notes"}]},
    )
    runtime = FakeRuntimeClient()

    async def ctx(_user_id: int) -> dict:
        return {"max_concurrent": 45, "bot_name": "Scribe"}

    port = _port(repo.repo, runtime, fetch_bot_context=ctx)
    assert await port.spawn_exact(USER, target) == SpawnOutcome("sent")
    assert json.loads(runtime.specs[0]["env"]["BOT_CONFIG"])["botName"] == "Cal Notes"


async def test_request_bot_refuses_claim_with_continue_meeting():
    repo = FakeBackend()
    target = await repo.seed(start=_now())
    with pytest.raises(ValueError, match="continue_meeting"):
        await request_bot(
            repo.repo,
            FakeRuntimeClient(),
            user_id=USER,
            platform=PLAT,
            native_meeting_id=NID,
            token_secret="s",
            redis_url="redis://r",
            continue_meeting=True,
            claim_meeting_id=target,
        )
    assert (await repo.row(target))["status"] == "scheduled"


async def test_pg_live_index_violation_in_the_exact_claim_is_already_live(pg_engine):
    """A row made live by a writer outside these locks trips the live-link index at the claim's
    write: that one IntegrityError is ``DuplicateMeeting``."""
    from meeting_api.intake.status import lock_meeting

    pg = PgBackend(pg_engine)
    target = await pg.seed(start=_now())
    await pg.seed(status="active")  # the row the dedup would have seen
    async with pg.session_factory() as db:
        row = await lock_meeting(db, target)
        with pytest.raises(DuplicateMeeting):
            await pg.repo._claim_exact(db, row, {})
    assert (await pg.row(target))["status"] == "scheduled"


async def test_pg_any_other_integrity_error_in_the_exact_claim_propagates(pg_engine):
    from sqlalchemy.exc import IntegrityError

    from meeting_api.intake.status import derive_event_id_v2

    pg = PgBackend(pg_engine)
    target = await pg.seed(start=_now())
    uuid = await pg.scalar("SELECT uuid::text FROM meetings WHERE id = :m", m=target)
    taken = derive_event_id_v2(uuid, "meeting.status_change", 1)
    async with pg.engine.begin() as conn:
        from sqlalchemy import text

        await conn.execute(
            text(
                "INSERT INTO webhook_outbox (event_id, meeting_id, event_type, sequence, "
                "payload_text) VALUES (:e, :m, 'meeting.status_change', 1, '{}')"
            ),
            {"e": taken, "m": target},
        )
    with pytest.raises(IntegrityError):
        await _guarded(pg, claim=target)
    assert (await pg.row(target))["status"] == "scheduled"


async def test_pg_post_claim_stop_fence_ends_not_sent(pg_engine):
    """A user's stop landing between the claim and the workload: the spawn fence ends the row
    ``failed`` through the status writer with not_sent/meeting_stopped, in one ``meeting.not_sent``
    event; the port adds nothing."""
    from meeting_api.bot_spawn.adapters import SqlAlchemyMeetingRepo
    from meeting_api.intake import PostgresIntakeStore

    class StopAfterClaim(SqlAlchemyMeetingRepo):
        async def create_meeting_guarded(self, **kwargs):
            row = await super().create_meeting_guarded(**kwargs)
            await self.merge_meeting_data(row["id"], {"stop_requested": True})
            return row

    pg = PgBackend(pg_engine)
    target = await pg.seed(start=_now())
    runtime = FakeRuntimeClient()
    port = _port(
        StopAfterClaim(pg.session_factory),
        runtime,
        store=PostgresIntakeStore(pg.session_factory),
    )
    outcome = await port.spawn_exact(USER, target)
    assert outcome.result == "failed" and outcome.code == "meeting_stopped"
    assert runtime.specs == []  # no workload was created
    assert await _outcome_row(pg, target) == {
        "status": "failed",
        "outcome_kind": "not_sent",
        "outcome_detail": "meeting_stopped",
        "outcome_message": outcome.message,
    }
    assert [e["event_type"] for e in await _events(pg, target)] == [
        "meeting.status_change",
        "meeting.not_sent",
    ]


# ── the intake service on a post-claim failure (the fake port's after_claim mode) ────────────


async def test_post_claim_failure_of_a_new_instant_join_replies_created():
    """With one send allowed (§6.9 F-K2 retries a failure while there are sends left)."""
    from intake_builders import make_harness

    failure = SpawnOutcome(
        "failed", "spawn_error", "kernel could not start the workload"
    )
    h = make_harness(spawn_failure=failure, send_max_attempts=1)
    h.spawn.after_claim = True
    reply = await h.instant("manual:1")
    m = reply["meeting"]
    assert reply["result"] == "created"
    assert m["status"] == "failed"
    assert (m["outcome"]["kind"], m["outcome"]["detail"]) == ("not_sent", "spawn_error")
    assert [t for _, t in h.events()] == [
        "meeting.scheduled",
        "meeting.status_change",
        "meeting.not_sent",
    ]


async def test_post_claim_failure_of_an_adopted_meeting_replies_joined_existing():
    """With one send allowed (§6.9 F-K2 retries a failure while there are sends left)."""
    from intake_builders import GMEET, make_harness

    failure = SpawnOutcome(
        "failed", "spawn_error", "kernel could not start the workload"
    )
    h = make_harness("2026-09-29T09:00:00Z", spawn_failure=failure, send_max_attempts=1)
    h.spawn.after_claim = True
    uuid = (await h.put(start="2026-09-29T10:00:00Z", end="2026-09-29T10:30:00Z"))[
        "meeting"
    ]["id"]
    h.clock.set("2026-09-29T09:45:00Z")
    reply = await h.instant("manual:1", GMEET)
    m = reply["meeting"]
    assert (reply["result"], m["id"], m["status"]) == (
        "joined_existing",
        uuid,
        "failed",
    )
    assert m["outcome"]["detail"] == "spawn_error"
    # R12 is for failures before the claim: nothing was removed, the meeting itself ended
    assert h.store.find_entry(1, "a@abroadworks.com", "manual:1").state == "closed"
