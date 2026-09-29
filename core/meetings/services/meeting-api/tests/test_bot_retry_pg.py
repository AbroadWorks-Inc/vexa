"""§6.9 F-K2 on real Postgres — a bot that fails while its meeting is on gets a new bot on the SAME
meeting.

The meetings here are made the way production makes them: an entry through ``IntakeService`` and
a bot sent by the real ``ExactRowSpawn`` / ``request_bot`` over the SQLAlchemy repo (the runtime
is ``FakeRuntimeClient``). Groups:

  * the failure writers — the session-keyed lifecycle write (``update_meeting_status``),
    ``fail_meeting`` and the spawn port's post-claim ending each send a retried failure back to
    ``requested`` with ``bot.retry``; the last one ends ``failed`` (``bot.failed``, counted once);
    what isn't retried ends as before; a pending row refuses status writes;
  * the lifecycle callback — a retried session's terminal goes out as ``bot.retry`` and reaches
    none of the meeting-level edges (the system sink the exporter reads above all);
  * the newest-session guard — a session that isn't the meeting's newest writes nothing;
  * the retry driver (the auto-join tick) end to end — each failure kind → ``bot.retry`` → a new
    session once the old workload is proven gone → ``active``; the last failure; a live pod
    deleted and confirmed first; a 404 waits out ``MEETING_UNTRACKED_GRACE_SEC``; the planned end
    or a stop ends a waiting meeting; the link stays held; the exporter's ``meeting.completed``
    fires once; ``sequence`` only grows across sessions;
  * the reconcile listing leaves a waiting meeting out; each retry counts once in
    ``aw_bot_retries_total{reason,user_id}``.

Skips cleanly unless ``MEETING_API_TEST_DATABASE_URL`` is set; see ``test_intake_pg_schema.py``'s
docstring for the ephemeral SQLAlchemy/asyncpg install.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import pytest

from intake_builders import GMEET, ZOOM, entry_body, instant_body, make_settings
from meeting_api.bot_spawn.fakes import FakeRuntimeClient

pytestmark = pytest.mark.skipif(
    not os.getenv("MEETING_API_TEST_DATABASE_URL"),
    reason="real-Postgres proofs for §6.9 F-K2; set MEETING_API_TEST_DATABASE_URL to run",
)

USER = 7
UTC = timezone.utc


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


async def _ctx(_user_id: int) -> dict:
    return {"max_concurrent": 45}


class _NoStop:
    async def stop_live(self, user_id, meeting_id, *, outcome):
        raise AssertionError("no stop expected")

    async def leave(self, user_id, stop):
        raise AssertionError("no leave expected")


class Pg:
    """The SQLAlchemy repo, the Postgres intake store, and an ``IntakeService`` over them with
    the real spawn port."""

    def __init__(self, engine: Any) -> None:
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from meeting_api.bot_spawn.adapters import SqlAlchemyMeetingRepo
        from meeting_api.intake import PostgresIntakeStore

        self.engine = engine
        self.session_factory = async_sessionmaker(engine, expire_on_commit=False)
        self.repo = SqlAlchemyMeetingRepo(self.session_factory)
        self.store = PostgresIntakeStore(self.session_factory)

    def port(self, runtime: Optional[FakeRuntimeClient] = None, repo: Any = None):
        from meeting_api.intake.fakes import FakePublisher
        from meeting_api.intake.spawn import ExactRowSpawn

        return ExactRowSpawn(
            repo or self.repo,
            runtime or FakeRuntimeClient(),
            store=self.store,
            fetch_bot_context=_ctx,
            publisher=FakePublisher(),
            token_secret="s",
            redis_url="redis://r",
        )

    def service(self, runtime: Optional[FakeRuntimeClient] = None):
        from meeting_api.intake.fakes import FakePublisher
        from meeting_api.intake.service import IntakeService

        return IntakeService(
            self.store, self.port(runtime), _NoStop(), FakePublisher(), make_settings()
        )

    async def scalar(self, sql: str, **params: Any) -> Any:
        from sqlalchemy import text

        async with self.engine.connect() as conn:
            return (await conn.execute(text(sql), params)).scalar()

    async def execute(self, sql: str, **params: Any) -> None:
        from sqlalchemy import text

        async with self.engine.begin() as conn:
            await conn.execute(text(sql), params)

    async def id_of(self, uuid: str) -> int:
        return int(await self.scalar("SELECT id FROM meetings WHERE uuid = :u", u=uuid))

    async def row(self, mid: int) -> dict:
        row = await self.repo.get_meeting(mid)
        assert row is not None
        return row

    async def aw(self, mid: int) -> dict:
        from sqlalchemy import text

        async with self.engine.connect() as conn:
            row = (
                await conn.execute(
                    text("SELECT * FROM meeting_aw_state WHERE meeting_id = :m"),
                    {"m": mid},
                )
            ).one()
        return dict(row._mapping)

    async def events(self, mid: int) -> list[dict]:
        from sqlalchemy import text

        async with self.engine.connect() as conn:
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

    async def types(self, mid: int) -> list[str]:
        return [e["event_type"] for e in await self.events(mid)]

    async def sessions(self, mid: int) -> list[str]:
        return list(await self.repo.list_sessions(meeting_id=mid))

    async def calendar_meeting(
        self,
        external_id: str = "google:cal-1",
        *,
        end_in: timedelta = timedelta(minutes=30),
    ) -> int:
        """A calendar meeting that started five minutes ago, still ``scheduled``."""
        now = _now()
        reply = await self.service().put_entry(
            USER,
            entry_body(
                external_id,
                meeting_url=GMEET,
                start=_iso(now - timedelta(minutes=5)),
                end=_iso(now + end_in),
            ),
        )
        assert reply["meeting"]["status"] == "scheduled"
        return await self.id_of(reply["meeting"]["id"])

    async def tick(
        self,
        runtime: FakeRuntimeClient,
        *,
        at: datetime,
        untracked_grace: float = 600.0,
        finish_meeting: Any = None,
        fetch_bot_context: Any = _ctx,
    ) -> dict:
        """One auto-join tick at ``at``: the due rows and the meetings waiting for a new bot."""
        from meeting_api.bot_spawn.auto_join import auto_join_tick

        return await auto_join_tick(
            self.repo,
            runtime,
            store=self.store,
            intake=self.service(runtime),
            fetch_bot_context=fetch_bot_context,
            transcribe_gate=lambda: None,
            now=at,
            token_secret="s",
            redis_url="redis://r",
            untracked_grace=untracked_grace,
            finish_meeting=finish_meeting,
        )

    async def sent(self, *, status: str = "active", **kw: Any) -> tuple[int, str]:
        """A calendar meeting whose bot was sent and reached ``status``: ``(id, session)``."""
        mid = await self.calendar_meeting(**kw)
        outcome = await self.port().spawn_exact(USER, mid)
        assert outcome.result == "sent", outcome
        session = (await self.sessions(mid))[-1]
        path = {
            "requested": [],
            "joining": ["joining"],
            "awaiting_admission": ["joining", "awaiting_admission"],
            "active": ["joining", "active"],
            "needs_help": ["joining", "needs_help"],
        }
        for step in path[status]:
            assert await self.repo.update_meeting_status(
                session_uid=session, status=step
            )
        return mid, session


@pytest.fixture
async def pg():
    pytest.importorskip("sqlalchemy", reason="see test_intake_pg_schema.py's docstring")
    pytest.importorskip("asyncpg", reason="see test_intake_pg_schema.py's docstring")
    from sqlalchemy.ext.asyncio import create_async_engine

    from admin_api.schema import models as admin_models
    from admin_api.schema import sync as admin_sync

    engine = create_async_engine(os.environ["MEETING_API_TEST_DATABASE_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(admin_models.Base.metadata.drop_all)
    await admin_sync.ensure_schema(engine, admin_models.Base)
    yield Pg(engine)
    async with engine.begin() as conn:
        await conn.run_sync(admin_models.Base.metadata.drop_all)
    await engine.dispose()


def _failed_count(reason: str) -> float:
    from meeting_api.metrics import registry

    value = registry().get_sample_value(
        "aw_meetings_failed_total", {"reason": reason, "user_id": str(USER)}
    )
    return value or 0.0


# ── the lifecycle write ─────────────────────────────────────────────────────────────────────

# (the stage the bot reached, the session's terminal, its completion reason, what drove it)
RETRIED = [
    pytest.param(
        "joining", "failed", "join_failure", "bot_callback", id="crash-joining"
    ),
    pytest.param(
        "awaiting_admission",
        "failed",
        "awaiting_admission_rejected",
        "bot_callback",
        id="lobby-rejected",
    ),
    pytest.param(
        "awaiting_admission",
        "failed",
        "awaiting_admission_timeout",
        "bot_callback",
        id="lobby-timeout",
    ),
    pytest.param(
        "active", "failed", "left_alone", "runtime_destroy", id="crash-in-call"
    ),
    pytest.param(
        "active", "completed", "left_alone", "runtime_destroy", id="lost-workload"
    ),
    pytest.param(
        "requested",
        "failed",
        "join_failure",
        "runtime_destroy",
        id="crash-before-reporting",
    ),
]


@pytest.mark.parametrize("stage,terminal,reason,source", RETRIED)
async def test_pg_a_bot_failure_sends_the_meeting_back_to_requested(
    pg, stage, terminal, reason, source
):
    mid, session = await pg.sent(status=stage)
    workload = (await pg.row(mid))["bot_container_id"]
    row = await pg.repo.update_meeting_status(
        session_uid=session,
        status=terminal,
        completion_reason=reason,
        data={"reason": "the bot fell over", "status_transition": [{"to": terminal}]},
        transition_source=source,
    )
    assert row is not None and row["status"] == "requested"
    assert row["end_time"] is None
    data = row["data"]
    assert "completion_reason" not in data
    marker = data["bot_retry"]
    assert (marker["reason"], marker["message"], marker["after_session"]) == (
        reason,
        "the bot fell over",
        session,
    )
    assert marker["workload"] == workload
    assert marker["proven_gone"] is (source == "runtime_destroy")
    aw = await pg.aw(mid)
    assert (aw["send_attempts"], aw["last_error_code"]) == (1, "bot_failed")
    assert aw["outcome_kind"] is None
    events = await pg.events(mid)
    assert events[-1]["event_type"] == "bot.retry"
    assert events[-1]["data"]["change"]["to"] == "requested"
    assert events[-1]["data"]["change"]["reason"] == reason
    assert events[-1]["data"]["meeting"]["status"] == "requested"
    assert (
        await pg.scalar(
            "SELECT count(*) FROM meeting_entries WHERE meeting_id = :m AND state = 'active'",
            m=mid,
        )
        == 1
    )


NOT_RETRIED = [
    pytest.param("active", "completed", "left_alone", "bot_callback", id="normal-end"),
    pytest.param("active", "completed", "stopped", "bot_callback", id="stop"),
    pytest.param("active", "failed", "evicted", "bot_callback", id="host-removed"),
    pytest.param(
        "active", "completed", "startup_alone", "bot_callback", id="nobody-joined"
    ),
    pytest.param(
        "active", "completed", "evicted", "runtime_destroy", id="lost-but-evicted"
    ),
]


@pytest.mark.parametrize("stage,terminal,reason,source", NOT_RETRIED)
async def test_pg_what_is_not_retried_ends_as_the_bot_reported(
    pg, stage, terminal, reason, source
):
    mid, session = await pg.sent(status=stage)
    row = await pg.repo.update_meeting_status(
        session_uid=session,
        status=terminal,
        completion_reason=reason,
        transition_source=source,
    )
    assert row["status"] == terminal and "bot_retry" not in row["data"]
    assert row["data"]["completion_reason"] == reason
    assert "bot.retry" not in await pg.types(mid)
    assert (await pg.aw(mid))["send_attempts"] == 0


async def test_pg_the_last_failure_ends_failed_and_counts_once(pg):
    mid, session = await pg.sent(status="joining")
    await pg.execute(
        "UPDATE meeting_aw_state SET send_attempts = 2 WHERE meeting_id = :m", m=mid
    )
    before = _failed_count("join_failure")
    for _ in range(2):  # the bot's retried callback
        await pg.repo.update_meeting_status(
            session_uid=session,
            status="failed",
            completion_reason="join_failure",
            transition_source="bot_callback",
        )
    row = await pg.row(mid)
    assert (row["status"], row["data"]["completion_reason"]) == (
        "failed",
        "join_failure",
    )
    assert (await pg.types(mid))[-1] == "bot.failed"
    assert _failed_count("join_failure") == before + 1


async def test_pg_a_lost_bot_on_its_last_attempt_ends_failed(pg):
    mid, session = await pg.sent(status="active")
    await pg.execute(
        "UPDATE meeting_aw_state SET send_attempts = 2 WHERE meeting_id = :m", m=mid
    )
    before = _failed_count("left_alone")
    row = await pg.repo.update_meeting_status(
        session_uid=session,
        status="completed",
        completion_reason="left_alone",
        transition_source="runtime_destroy",
    )
    assert (row["status"], row["data"]["completion_reason"]) == ("failed", "left_alone")
    assert (await pg.types(mid))[-1] == "bot.failed"
    assert _failed_count("left_alone") == before + 1


async def test_pg_a_failure_past_the_planned_end_is_the_last(pg):
    mid, session = await pg.sent(status="active", end_in=timedelta(seconds=30))
    row = await pg.repo.update_meeting_status(
        session_uid=session,
        status="failed",
        completion_reason="left_alone",
        transition_source="runtime_destroy",
    )
    assert row["status"] == "failed"


async def test_pg_a_pending_row_refuses_status_writes_and_takes_data(pg):
    mid, session = await pg.sent(status="active")
    await pg.repo.update_meeting_status(
        session_uid=session,
        status="failed",
        completion_reason="left_alone",
        transition_source="runtime_destroy",
    )
    events = len(await pg.events(mid))
    assert (
        await pg.repo.update_meeting_status(session_uid=session, status="active")
        is None
    )
    assert (
        await pg.repo.update_meeting_status(
            session_uid=session, status="failed", completion_reason="join_failure"
        )
        is None
    )
    assert (await pg.row(mid))["status"] == "requested"
    stopped = await pg.repo.update_meeting_status(
        session_uid=session, status="requested", data={"stop_requested": True}
    )
    assert stopped["data"]["stop_requested"] is True
    assert stopped["data"]["bot_retry"]["reason"] == "left_alone"
    assert len(await pg.events(mid)) == events


# ── fail_meeting and the spawn port ─────────────────────────────────────────────────────────


async def test_pg_fail_meeting_after_the_claim_retries(pg):
    mid, _ = await pg.sent(status="requested")
    row = await pg.repo.fail_meeting(
        meeting_id=mid,
        reason="workload dead on arrival",
        workload_id="mtg-x",
        outcome=None,
    )
    assert row["status"] == "requested"
    marker = row["data"]["bot_retry"]
    assert (marker["reason"], marker["stage"], marker["message"]) == (
        "start_failed",
        "requested",
        "workload dead on arrival",
    )
    assert (marker["workload"], marker["proven_gone"]) == ("mtg-x", True)
    assert (
        "completion_reason" not in row["data"] and "failure_reason" not in row["data"]
    )
    assert (await pg.types(mid))[-1] == "bot.retry"


async def test_pg_the_last_fail_meeting_after_a_bot_session_is_bot_failed(pg):
    from meeting_api.intake.status import Outcome

    mid, _ = await pg.sent(status="requested")
    await pg.execute(
        "UPDATE meeting_aw_state SET send_attempts = 2 WHERE meeting_id = :m", m=mid
    )
    before = _failed_count("start_failed")
    row = await pg.repo.fail_meeting(
        meeting_id=mid,
        reason="kernel said no",
        outcome=Outcome("not_sent", "spawn_error", "kernel said no"),
    )
    assert row["status"] == "failed"
    assert (await pg.aw(mid))["outcome_kind"] is None
    assert (await pg.types(mid))[-1] == "bot.failed"
    assert _failed_count("start_failed") == before + 1


async def test_pg_a_post_claim_spawn_failure_is_retried(pg):
    mid = await pg.calendar_meeting()
    outcome = await pg.port(FakeRuntimeClient(fail=True)).spawn_exact(USER, mid)
    assert (outcome.result, outcome.code) == ("failed", "spawn_error")
    row = await pg.row(mid)
    assert row["status"] == "requested"
    marker = row["data"]["bot_retry"]
    assert (marker["reason"], marker["proven_gone"]) == ("start_failed", True)
    assert marker["workload"].startswith(f"mtg-{mid}-")
    assert await pg.types(mid) == [
        "meeting.scheduled",
        "meeting.status_change",
        "bot.retry",
    ]
    assert (await pg.aw(mid))["send_attempts"] == 1


async def test_pg_a_runtime_quota_refusal_is_retried_with_the_workload_proven_gone(pg):
    """The runtime's 429 is an explicit refusal: no workload was started."""
    mid = await pg.calendar_meeting()
    runtime = FakeRuntimeClient(quota_exceeded=True)
    outcome = await pg.port(runtime).spawn_exact(USER, mid)
    assert (outcome.result, outcome.code) == ("failed", "account_limit")
    row = await pg.row(mid)
    marker = row["data"]["bot_retry"]
    assert (row["status"], marker["proven_gone"]) == ("requested", True)
    assert marker["workload"] == runtime.specs[0]["workloadId"]
    assert marker["message"] == "owner quota exceeded"
    aw = await pg.aw(mid)
    assert (aw["send_attempts"], aw["last_error_code"]) == (1, "account_limit")
    assert (await pg.types(mid)).count("bot.retry") == 1


async def test_pg_the_last_post_claim_failure_after_a_bot_session_is_bot_failed(pg):
    mid = await pg.calendar_meeting()
    await pg.repo.create_session(meeting_id=mid, session_uid="sess-earlier")
    await pg.execute(
        "UPDATE meeting_aw_state SET send_attempts = 2 WHERE meeting_id = :m", m=mid
    )
    await pg.port(FakeRuntimeClient(quota_exceeded=True)).spawn_exact(USER, mid)
    assert (await pg.row(mid))["status"] == "failed"
    assert (await pg.aw(mid))["outcome_kind"] is None
    assert (await pg.types(mid))[-1] == "bot.failed"


async def test_pg_a_create_workload_timeout_is_retried_with_the_workload_unproven(pg):
    class Timeout(FakeRuntimeClient):
        async def create_workload(self, spec):
            self.specs.append(spec)
            raise TimeoutError("the runtime took too long")

    mid = await pg.calendar_meeting()
    runtime = Timeout()
    outcome = await pg.port(runtime).spawn_exact(USER, mid)
    assert (outcome.result, outcome.code) == ("failed", "internal_error")
    row = await pg.row(mid)
    marker = row["data"]["bot_retry"]
    assert row["status"] == "requested"
    assert (marker["workload"], marker["proven_gone"]) == (
        runtime.specs[0]["workloadId"],
        False,
    )
    assert (await pg.types(mid)).count("bot.retry") == 1


# ── the instant join reply ──────────────────────────────────────────────────────────────────


async def test_pg_a_retried_post_claim_failure_of_a_new_instant_join_replies_created(
    pg,
):
    reply = await pg.service(FakeRuntimeClient(fail=True)).put_entry(
        USER, instant_body("paste:1", ZOOM)
    )
    assert reply["result"] == "created"
    assert reply["meeting"]["status"] == "requested"
    assert reply["meeting"]["outcome"] is None


# ── the lifecycle callback ──────────────────────────────────────────────────────────────────


class _SystemSink:
    def __init__(self) -> None:
        self.events: list[str] = []

    async def deliver(self, envelope, *, label=""):
        self.events.append(envelope["event_type"])


async def test_pg_a_lost_workload_through_the_lifecycle_is_bot_retry_only(pg):
    from meeting_api import create_app
    from meeting_api.lifecycle.machine import TransitionSource

    mid, session = await pg.sent(status="active")
    sink = _SystemSink()
    app = create_app(meeting_repo=pg.repo, system_webhook_sink=sink)
    status, _ = await app.state.apply_lifecycle_event(
        {
            "connection_id": session,
            "status": "completed",
            "completion_reason": "left_alone",
        },
        transition_source=TransitionSource.RUNTIME_DESTROY,
        force_terminal_on_destroy=True,
    )
    assert status == 200
    assert (await pg.row(mid))["status"] == "requested"
    assert app.state.typed_webhooks[-1]["event_type"] == "bot.retry"
    assert sink.events == []
    assert (await pg.types(mid))[-1] == "bot.retry"


# ── the newest session speaks for the meeting ───────────────────────────────────────────────


async def test_pg_a_session_that_is_not_the_meetings_newest_writes_nothing(pg):
    mid, old = await pg.sent(status="joining")
    await pg.repo.create_session(meeting_id=mid, session_uid="sess-new")
    events = len(await pg.events(mid))
    assert await pg.repo.update_meeting_status(session_uid=old, status="active") is None
    assert (
        await pg.repo.update_meeting_status(
            session_uid=old, status="joining", data={"x": 1}
        )
        is None
    )
    assert (
        await pg.repo.update_meeting_status(
            session_uid=old, status="failed", completion_reason="join_failure"
        )
        is None
    )
    row = await pg.row(mid)
    assert (row["status"], "x" in row["data"]) == ("joining", False)
    assert len(await pg.events(mid)) == events
    new = await pg.repo.update_meeting_status(session_uid="sess-new", status="active")
    assert new["status"] == "active"


# ── the retry driver, end to end ────────────────────────────────────────────────────────────


def _gone(*workloads: str) -> FakeRuntimeClient:
    """A runtime that tracks ``workloads`` as destroyed (and every new one it starts)."""
    return FakeRuntimeClient(workloads={w: {"state": "destroyed"} for w in workloads})


async def _fail(pg: Pg, session: str, terminal: str, reason: str, source: str) -> None:
    await pg.repo.update_meeting_status(
        session_uid=session,
        status=terminal,
        completion_reason=reason,
        transition_source=source,
    )


def _later(seconds: float = 61) -> datetime:
    return _now() + timedelta(seconds=seconds)


@pytest.mark.parametrize("stage,terminal,reason,source", RETRIED)
async def test_pg_each_failure_kind_gets_a_new_session_that_goes_active(
    pg, stage, terminal, reason, source
):
    mid, old = await pg.sent(status=stage)
    old_workload = (await pg.row(mid))["bot_container_id"]
    await _fail(pg, old, terminal, reason, source)
    runtime = _gone(old_workload)
    assert (await pg.tick(runtime, at=_now()))["spawned"] == 0  # before due_at
    assert len(runtime.specs) == 0
    counters = await pg.tick(runtime, at=_later())
    assert counters["spawned"] == 1 and len(runtime.specs) == 1
    row = await pg.row(mid)
    assert row["status"] == "requested" and "bot_retry" not in row["data"]
    assert row["bot_container_id"] == runtime.specs[0]["workloadId"] != old_workload
    history = row["data"]["completion_history"][-1]
    assert history["completion_reason"] == reason
    sessions = await pg.sessions(mid)
    assert sessions[0] == old and len(sessions) == 2
    for step in ("joining", "active"):
        assert await pg.repo.update_meeting_status(
            session_uid=sessions[-1], status=step
        )
    assert (await pg.row(mid))["status"] == "active"
    assert (await pg.types(mid))[-3:] == [
        "bot.retry",
        "meeting.status_change",
        "meeting.started",
    ]


async def test_pg_a_post_claim_spawn_failure_gets_a_new_session(pg):
    mid = await pg.calendar_meeting()
    await pg.port(FakeRuntimeClient(fail=True)).spawn_exact(USER, mid)
    runtime = _gone()
    assert (await pg.tick(runtime, at=_later()))["spawned"] == 1
    assert (await pg.row(mid))["status"] == "requested"
    assert len(await pg.sessions(mid)) == 1  # the failed attempt never had one


async def test_pg_the_last_failure_ends_the_meeting_failed_counted_once(pg):
    mid, session = await pg.sent(status="joining")
    before = _failed_count("join_failure")
    for attempt in range(3):
        await _fail(pg, session, "failed", "join_failure", "bot_callback")
        if attempt < 2:
            workload = (await pg.row(mid))["bot_container_id"]
            await pg.tick(_gone(workload), at=_later())
            session = (await pg.sessions(mid))[-1]
            await pg.repo.update_meeting_status(session_uid=session, status="joining")
    row = await pg.row(mid)
    assert (row["status"], row["data"]["completion_reason"]) == (
        "failed",
        "join_failure",
    )
    types = await pg.types(mid)
    assert (types.count("bot.retry"), types[-1]) == (2, "bot.failed")
    assert _failed_count("join_failure") == before + 1
    assert len(await pg.sessions(mid)) == 3


async def test_pg_a_live_pod_is_deleted_and_confirmed_before_the_new_bot(pg):
    mid, session = await pg.sent(status="joining")
    old = (await pg.row(mid))["bot_container_id"]
    await _fail(pg, session, "failed", "join_failure", "bot_callback")
    runtime = FakeRuntimeClient(workloads={old: {"state": "running"}})
    assert (await pg.tick(runtime, at=_later()))["spawned"] == 1
    assert runtime.deleted == [old]
    assert len(runtime.specs) == 1


async def test_pg_a_pod_whose_delete_is_not_confirmed_gets_no_new_bot(pg):
    class Refuses(FakeRuntimeClient):
        async def delete_workload(self, workload_id):
            raise RuntimeError("the kernel is down")

    mid, session = await pg.sent(status="joining")
    old = (await pg.row(mid))["bot_container_id"]
    await _fail(pg, session, "failed", "join_failure", "bot_callback")
    runtime = Refuses(workloads={old: {"state": "running"}})
    assert (await pg.tick(runtime, at=_later()))["spawned"] == 0
    assert runtime.specs == [] and (await pg.row(mid))["data"]["bot_retry"]


async def test_pg_a_404_is_gone_only_after_the_untracked_grace(pg):
    mid, session = await pg.sent(status="joining")
    await _fail(pg, session, "failed", "join_failure", "bot_callback")
    runtime = FakeRuntimeClient(workloads={})  # the kernel knows no workload: 404
    assert (await pg.tick(runtime, at=_later(), untracked_grace=600))["spawned"] == 0
    assert runtime.specs == []
    assert (await pg.tick(runtime, at=_later(601), untracked_grace=600))["spawned"] == 1


async def test_pg_the_planned_end_passing_while_pending_ends_failed(pg):
    mid, session = await pg.sent(status="active", end_in=timedelta(minutes=3))
    await _fail(pg, session, "completed", "left_alone", "runtime_destroy")
    assert (await pg.row(mid))["status"] == "requested"
    before = _failed_count("left_alone")
    runtime = _gone()
    await pg.tick(runtime, at=_later(181))
    row = await pg.row(mid)
    assert (row["status"], row["data"]["completion_reason"]) == ("failed", "left_alone")
    assert not row["data"].get("bot_retry")
    assert runtime.specs == []
    assert (await pg.types(mid))[-1] == "bot.failed"
    assert _failed_count("left_alone") == before + 1


async def test_pg_a_stop_on_a_waiting_meeting_ends_it_stopped(pg):
    mid, session = await pg.sent(status="joining")
    await _fail(pg, session, "failed", "join_failure", "bot_callback")
    await pg.repo.update_meeting_status(
        session_uid=session, status="requested", data={"stop_requested": True}
    )
    runtime = _gone()
    await pg.tick(runtime, at=_later())
    row = await pg.row(mid)
    assert (row["status"], row["data"]["completion_reason"]) == ("failed", "stopped")
    assert runtime.specs == []


async def test_pg_the_link_stays_held_while_the_meeting_waits(pg):
    from intake_builders import GMEET

    mid, session = await pg.sent(status="active")
    await _fail(pg, session, "completed", "left_alone", "runtime_destroy")
    uuid = await pg.scalar("SELECT uuid::text FROM meetings WHERE id = :m", m=mid)
    joined = await pg.service().put_entry(USER, instant_body("paste:9", GMEET))
    assert (joined["result"], joined["meeting"]["id"]) == ("joined_existing", uuid)
    assert (
        await pg.scalar(
            "SELECT count(*) FROM meetings WHERE platform_specific_id = 'kxo-misr-avz'"
        )
        == 1
    )


async def test_pg_the_next_meeting_on_the_link_waits_for_the_new_bot(pg):
    mid, session = await pg.sent(status="active", end_in=timedelta(minutes=3))
    workload = (await pg.row(mid))["bot_container_id"]
    await _fail(pg, session, "completed", "left_alone", "runtime_destroy")
    now = _now()
    after = await pg.service().put_entry(
        USER,
        entry_body(
            "google:cal-2",
            meeting_url=GMEET,
            start=_iso(now + timedelta(minutes=4)),
            end=_iso(now + timedelta(minutes=30)),
        ),
    )
    nxt = await pg.id_of(after["meeting"]["id"])
    assert nxt != mid
    runtime = _gone(workload)
    counters = await pg.tick(runtime, at=_later(130))
    assert (counters["skipped_live"], counters["spawned"]) == (1, 1)
    assert (await pg.row(nxt))["status"] == "scheduled"
    assert "meeting.waiting_for_room" in await pg.types(nxt)
    assert len(runtime.specs) == 1 and (await pg.row(mid))["bot_container_id"] == (
        runtime.specs[0]["workloadId"]
    )


async def test_pg_the_exporters_meeting_completed_fires_once_and_sequence_grows(pg):
    from meeting_api import create_app
    from meeting_api.lifecycle.machine import TransitionSource

    mid, old = await pg.sent(status="active")
    sink = _SystemSink()
    app = create_app(meeting_repo=pg.repo, system_webhook_sink=sink)
    workload = (await pg.row(mid))["bot_container_id"]
    await app.state.apply_lifecycle_event(
        {
            "connection_id": old,
            "status": "completed",
            "completion_reason": "left_alone",
        },
        transition_source=TransitionSource.RUNTIME_DESTROY,
        force_terminal_on_destroy=True,
    )
    await pg.tick(_gone(workload), at=_later())
    new = (await pg.sessions(mid))[-1]
    for body in (
        {"status": "joining"},
        {"status": "active"},
        {"status": "completed", "completion_reason": "left_alone"},
    ):
        status, _ = await app.state.apply_lifecycle_event(
            {"connection_id": new, **body}
        )
        assert status == 200
    # the failed bot's late callback changes nothing
    status, _ = await app.state.apply_lifecycle_event(
        {"connection_id": old, "status": "failed", "completion_reason": "join_failure"},
        transition_source=TransitionSource.RUNTIME_DESTROY,
        force_terminal_on_destroy=True,
    )
    assert (await pg.row(mid))["status"] == "completed"
    assert sink.events == ["meeting.completed"]
    sequences = [e["data"]["meeting"]["sequence"] for e in await pg.events(mid)]
    assert sequences == sorted(sequences) and len(set(sequences)) == len(sequences)
    types = await pg.types(mid)
    assert types.count("meeting.started") == 2 and types[-1] == "meeting.completed"


# ── the reconcile listing and the retry counter ─────────────────────────────────────────────


async def test_pg_the_reconcile_listing_leaves_a_waiting_meeting_out(pg):
    mid, session = await pg.sent(status="active")
    await pg.execute(
        "UPDATE meetings SET updated_at = now() - interval '1 hour' WHERE id = :m",
        m=mid,
    )
    assert [
        r[0] for r in await pg.repo.list_stale_nonterminal(stop_grace=0, active_grace=0)
    ] == [mid]
    await _fail(pg, session, "completed", "left_alone", "runtime_destroy")
    await pg.execute(
        "UPDATE meetings SET updated_at = now() - interval '1 hour' WHERE id = :m",
        m=mid,
    )
    assert await pg.repo.list_stale_nonterminal(stop_grace=0, active_grace=0) == []


def _retries(reason: str) -> float:
    from meeting_api.metrics import registry

    value = registry().get_sample_value(
        "aw_bot_retries_total", {"reason": reason, "user_id": str(USER)}
    )
    return value or 0.0


async def test_pg_each_retry_counts_once_by_its_reason(pg):
    mid, session = await pg.sent(status="awaiting_admission")
    before = _retries("awaiting_admission_timeout")
    for _ in range(2):  # the bot's retried callback is refused the second time
        await _fail(pg, session, "failed", "awaiting_admission_timeout", "bot_callback")
    assert _retries("awaiting_admission_timeout") == before + 1


# ── never two bots: a workload that may exist is never recorded gone ────────────────────────


class _TeardownFails(FakeRuntimeClient):
    async def delete_workload(self, workload_id):
        raise RuntimeError("the kernel is down")


def _session_write_fails(pg: Pg):
    from meeting_api.bot_spawn.adapters import SqlAlchemyMeetingRepo

    class Repo(SqlAlchemyMeetingRepo):
        async def create_session(self, **kw):
            raise RuntimeError("the database went away")

    return Repo(pg.session_factory)


async def test_pg_a_post_spawn_failure_with_a_failed_teardown_waits_for_proof(pg):
    mid = await pg.calendar_meeting()
    runtime = _TeardownFails()
    outcome = await pg.port(runtime, repo=_session_write_fails(pg)).spawn_exact(
        USER, mid
    )
    assert (outcome.result, outcome.code) == ("failed", "spawn_error")
    workload = runtime.specs[0]["workloadId"]
    marker = (await pg.row(mid))["data"]["bot_retry"]
    assert (marker["workload"], marker["proven_gone"]) == (workload, False)
    still_up = _TeardownFails(workloads={workload: {"state": "running"}})
    assert (await pg.tick(still_up, at=_later()))["spawned"] == 0
    assert still_up.specs == []
    confirmed = FakeRuntimeClient(workloads={workload: {"state": "running"}})
    assert (await pg.tick(confirmed, at=_later()))["spawned"] == 1
    assert confirmed.deleted == [workload]


async def test_pg_a_post_spawn_failure_with_a_confirmed_teardown_is_proven(pg):
    mid = await pg.calendar_meeting()
    runtime = FakeRuntimeClient()
    await pg.port(runtime, repo=_session_write_fails(pg)).spawn_exact(USER, mid)
    marker = (await pg.row(mid))["data"]["bot_retry"]
    assert (marker["workload"], marker["proven_gone"]) == (
        runtime.specs[0]["workloadId"],
        True,
    )
    assert runtime.deleted == [runtime.specs[0]["workloadId"]]


def _unanswered():
    import httpx

    from meeting_api.bot_spawn.ports import SpawnFailed

    return [
        pytest.param(httpx.ReadError("connection reset"), id="read-error"),
        pytest.param(httpx.RemoteProtocolError("peer closed"), id="protocol-error"),
        pytest.param(httpx.ConnectError("refused"), id="connect-error"),
        pytest.param(TimeoutError("slow"), id="timeout"),
        pytest.param(
            SpawnFailed("runtime kernel returned 503: busy", refused=False), id="5xx"
        ),
    ]


@pytest.mark.parametrize("error", _unanswered())
async def test_pg_a_create_the_runtime_did_not_refuse_is_never_recorded_gone(pg, error):
    class Unanswered(FakeRuntimeClient):
        async def create_workload(self, spec):
            self.specs.append(spec)
            raise error

    mid = await pg.calendar_meeting()
    runtime = Unanswered()
    outcome = await pg.port(runtime).spawn_exact(USER, mid)
    assert outcome.result == "failed"
    workload = runtime.specs[0]["workloadId"]
    marker = (await pg.row(mid))["data"]["bot_retry"]
    assert (marker["workload"], marker["proven_gone"]) == (workload, False)
    unknown = FakeRuntimeClient(workloads={})  # 404: not proof before the grace
    assert (await pg.tick(unknown, at=_later()))["spawned"] == 0
    assert unknown.specs == []


async def test_pg_a_failure_before_the_workload_create_is_proven_and_retried(
    pg, monkeypatch
):
    """The token could not be minted: the create never ran, so no pod can exist."""
    from meeting_api.intake.fakes import FakePublisher
    from meeting_api.intake.spawn import ExactRowSpawn

    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    mid = await pg.calendar_meeting()
    port = ExactRowSpawn(
        pg.repo,
        FakeRuntimeClient(),
        store=pg.store,
        fetch_bot_context=_ctx,
        publisher=FakePublisher(),
        token_secret=None,
        redis_url="redis://r",
    )
    runtime = FakeRuntimeClient()
    port._runtime = runtime
    assert (await port.spawn_exact(USER, mid)).code == "internal_error"
    assert runtime.specs == []  # the create never ran
    marker = (await pg.row(mid))["data"]["bot_retry"]
    assert (marker["workload"], marker["proven_gone"]) == (None, True)
    assert (await pg.tick(_gone(), at=_later()))["spawned"] == 1


async def test_pg_a_session_without_a_recorded_workload_names_its_own(pg):
    mid, session = await pg.sent(status="joining")
    await pg.execute("UPDATE meetings SET bot_container_id = NULL WHERE id = :m", m=mid)
    await _fail(pg, session, "failed", "join_failure", "bot_callback")
    marker = (await pg.row(mid))["data"]["bot_retry"]
    assert marker["workload"] == f"mtg-{mid}-{session[:8]}"


async def test_pg_the_retired_session_cannot_write_between_the_claim_and_the_new_session(
    pg,
):
    mid, old = await pg.sent(status="active")
    workload = (await pg.row(mid))["bot_container_id"]
    await _fail(pg, old, "completed", "left_alone", "runtime_destroy")
    assert await pg.repo.prove_retry_gone(meeting_id=mid, workload=workload)
    claimed = await pg.repo.create_meeting_guarded(
        user_id=USER,
        platform="google_meet",
        native_meeting_id="kxo-misr-avz",
        data={},
        claim_meeting_id=mid,
    )
    assert claimed["data"]["completion_history"][-1]["after_session"] == old
    events = len(await pg.events(mid))
    # the failed bot's late terminal lands before the new session row exists
    assert (
        await pg.repo.update_meeting_status(
            session_uid=old,
            status="failed",
            completion_reason="join_failure",
            transition_source="runtime_destroy",
        )
        is None
    )
    row = await pg.row(mid)
    assert (row["status"], row["data"].get("bot_retry")) == ("requested", None)
    assert len(await pg.events(mid)) == events


async def test_pg_a_failed_attempt_with_a_session_retires_it_at_the_next_claim(pg):
    mid = await pg.calendar_meeting()

    from meeting_api.bot_spawn.adapters import SqlAlchemyMeetingRepo

    class ContainerWriteFails(SqlAlchemyMeetingRepo):
        async def set_bot_container(self, **kw):
            raise RuntimeError("the database went away")

    runtime = FakeRuntimeClient()
    await pg.port(runtime, repo=ContainerWriteFails(pg.session_factory)).spawn_exact(
        USER, mid
    )
    (session,) = await pg.sessions(mid)
    assert (await pg.row(mid))["data"]["bot_retry"]["after_session"] == session
    await pg.tick(_gone(), at=_later())
    history = (await pg.row(mid))["data"]["completion_history"]
    assert history[-1]["after_session"] == session
    assert (
        await pg.repo.update_meeting_status(session_uid=session, status="joining")
        is None
    )


# ── a waiting meeting is bounded ────────────────────────────────────────────────────────────


class _Unknown(FakeRuntimeClient):
    async def get_workload(self, workload_id):
        raise RuntimeError("the kernel does not answer")


async def test_pg_an_instant_join_whose_old_pod_never_proves_gone_ends_at_the_deadline(
    pg,
):
    reply = await pg.service().put_entry(USER, instant_body("paste:7", ZOOM))
    mid = await pg.id_of(reply["meeting"]["id"])
    (session,) = await pg.sessions(mid)
    await _fail(pg, session, "failed", "join_failure", "bot_callback")
    runtime = _Unknown()
    assert (await pg.tick(runtime, at=_later(), untracked_grace=600))["spawned"] == 0
    assert (await pg.row(mid))["status"] == "requested"
    await pg.tick(runtime, at=_later(61 + 601), untracked_grace=600)
    row = await pg.row(mid)
    assert (row["status"], row["data"]["completion_reason"]) == (
        "failed",
        "join_failure",
    )
    assert not row["data"].get("bot_retry") and runtime.specs == []
    events = await pg.events(mid)
    assert events[-1]["event_type"] == "bot.failed"
    assert events[-1]["data"]["change"]["reason"] == "workload_not_proven"


async def test_pg_the_reconcile_sweep_ends_a_waiting_meeting_the_driver_gave_up(pg):
    from meeting_api.lifecycle.reconcile import reconcile_stale_nonterminal_sweep

    class Log:
        def __getattr__(self, name):
            return lambda *a, **k: None

    mid, session = await pg.sent(status="joining")
    await _fail(pg, session, "failed", "join_failure", "bot_callback")

    async def post(body):
        raise AssertionError("a waiting meeting is not ended through the lifecycle")

    await reconcile_stale_nonterminal_sweep(
        pg.repo,
        _Unknown(),
        post,
        stop_grace=45,
        active_grace=300,
        log=Log(),
        untracked_grace=600,
    )
    assert (await pg.row(mid))["status"] == "requested"  # before its deadline
    await pg.execute(
        "UPDATE meetings SET data = jsonb_set(data, '{bot_retry,due_at}', "
        "'\"2026-01-01T00:00:00Z\"') WHERE id = :m",
        m=mid,
    )
    await reconcile_stale_nonterminal_sweep(
        pg.repo,
        _Unknown(),
        post,
        stop_grace=45,
        active_grace=300,
        log=Log(),
        untracked_grace=600,
    )
    row = await pg.row(mid)
    assert (row["status"], row["data"]["completion_reason"]) == (
        "failed",
        "join_failure",
    )
    assert (await pg.types(mid))[-1] == "bot.failed"


# ── a meeting the retry ends gets the same meeting-level finish ─────────────────────────────


class _Streams:
    def __init__(self) -> None:
        self.reaped: list[str] = []

    async def publish(self, channel: str, data: str):
        return 1

    async def xadd(self, stream: str, payload: dict):
        self.reaped.append(stream)
        return "1-0"


async def _finishing_app(pg: Pg):
    from meeting_api import create_app

    sink, streams, finalized = _SystemSink(), _Streams(), []

    async def finalize(meeting_id: int) -> int:
        finalized.append(meeting_id)
        return 3

    app = create_app(
        meeting_repo=pg.repo,
        system_webhook_sink=sink,
        transcript_finalizer=finalize,
        redis=streams,
    )
    return app, sink, streams, finalized


async def test_pg_a_waiting_meeting_ended_at_its_planned_end_is_finished_like_any_end(
    pg,
):
    from meeting_api.lifecycle.machine import TransitionSource

    mid, session = await pg.sent(status="active", end_in=timedelta(minutes=3))
    app, sink, streams, finalized = await _finishing_app(pg)
    await app.state.apply_lifecycle_event(
        {
            "connection_id": session,
            "status": "completed",
            "completion_reason": "left_alone",
        },
        transition_source=TransitionSource.RUNTIME_DESTROY,
        force_terminal_on_destroy=True,
    )
    assert (sink.events, finalized, streams.reaped) == ([], [], [])
    await pg.tick(_gone(), at=_later(181), finish_meeting=app.state.finish_meeting)
    row = await pg.row(mid)
    assert row["status"] == "failed"
    assert sink.events == ["bot.failed"]
    assert finalized == [mid] and streams.reaped == [f"tc:meeting:{mid}"]
    assert app.state.typed_webhooks[-1]["data"]["meeting"]["status"] == "failed"


async def test_pg_a_stopped_waiting_meeting_is_finished_like_any_end(pg):
    from meeting_api.intake.stop import IntakeStop
    from meeting_api.lifecycle.stop_router import InMemoryCommandPublisher

    mid, session = await pg.sent(status="joining")
    await _fail(pg, session, "failed", "join_failure", "bot_callback")
    app, sink, streams, finalized = await _finishing_app(pg)
    commands = InMemoryCommandPublisher()
    stop = IntakeStop(pg.store, commands, FakeRuntimeClient())
    stop.finish_meeting = app.state.finish_meeting
    await stop.stop_live(USER, mid, outcome=None)
    assert (await pg.row(mid))["status"] == "failed"
    assert commands.published == []
    assert sink.events == ["bot.failed"] and finalized == [mid]
    assert streams.reaped == [f"tc:meeting:{mid}"]


async def test_pg_a_waiting_meeting_the_sweep_ends_is_finished_like_any_end(pg):
    from meeting_api.lifecycle.reconcile import reconcile_stale_nonterminal_sweep

    class Log:
        def __getattr__(self, name):
            return lambda *a, **k: None

    mid, session = await pg.sent(status="joining")
    await _fail(pg, session, "failed", "join_failure", "bot_callback")
    await pg.execute(
        "UPDATE meetings SET data = jsonb_set(data, '{bot_retry,due_at}', "
        "'\"2026-01-01T00:00:00Z\"') WHERE id = :m",
        m=mid,
    )
    app, sink, streams, finalized = await _finishing_app(pg)

    async def post(body):
        raise AssertionError("not through the lifecycle")

    await reconcile_stale_nonterminal_sweep(
        pg.repo,
        _Unknown(),
        post,
        stop_grace=45,
        active_grace=300,
        log=Log(),
        untracked_grace=600,
        finish_meeting=app.state.finish_meeting,
    )
    assert (await pg.row(mid))["status"] == "failed"
    assert sink.events == ["bot.failed"] and finalized == [mid]


# ── the driver's new bot failing before its claim ───────────────────────────────────────────


async def test_pg_a_new_bot_failing_before_its_claim_is_one_more_bounded_send(pg):
    async def unavailable(_user_id: int) -> None:
        return None  # identity down: the spawn is refused before it claims the row

    mid, session = await pg.sent(status="active")
    workload = (await pg.row(mid))["bot_container_id"]
    await _fail(pg, session, "completed", "left_alone", "runtime_destroy")
    runtime = _gone(workload)
    counters = await pg.tick(runtime, at=_later(), fetch_bot_context=unavailable)
    assert (counters["spawned"], counters["errors"]) == (0, 1) and runtime.specs == []
    row, aw = await pg.row(mid), await pg.aw(mid)
    assert row["status"] == "requested"
    assert (aw["send_attempts"], aw["last_error_code"]) == (2, "internal_error")
    marker = row["data"]["bot_retry"]
    assert (marker["reason"], marker["proven_gone"]) == ("left_alone", True)
    assert (await pg.types(mid)).count("bot.retry") == 2
    # the third send fails before its claim as well: the meeting's last
    before = _failed_count("left_alone")
    await pg.tick(runtime, at=_later(125), fetch_bot_context=unavailable)
    row = await pg.row(mid)
    assert (row["status"], row["data"]["completion_reason"]) == ("failed", "left_alone")
    events = await pg.events(mid)
    assert events[-1]["event_type"] == "bot.failed"
    assert events[-1]["data"]["change"]["reason"] == "internal_error"
    assert _failed_count("left_alone") == before + 1
    assert runtime.specs == []


# ── an entry-less meeting keeps its not_sent ending ─────────────────────────────────────────


async def _entry_less_with_a_session(pg: Pg) -> int:
    now = _now()
    await pg.execute(
        "INSERT INTO meetings (user_id, platform, platform_specific_id, status, data) "
        "VALUES (:u, 'google_meet', 'abc-defg-hij', 'scheduled', CAST(:d AS jsonb))",
        u=USER,
        d=json.dumps({"scheduled_at": _iso(now), "auto_join": True}),
    )
    mid = int(await pg.scalar("SELECT max(id) FROM meetings"))
    await pg.repo.create_session(meeting_id=mid, session_uid="sess-earlier")
    return mid


async def test_pg_an_entry_less_meetings_last_failure_keeps_not_sent(pg, monkeypatch):
    from meeting_api.intake.fakes import FakePublisher
    from meeting_api.intake.spawn import ExactRowSpawn

    refused = await _entry_less_with_a_session(pg)
    await pg.port(FakeRuntimeClient(quota_exceeded=True)).spawn_exact(USER, refused)
    assert (await pg.aw(refused))["outcome_kind"] == "not_sent"
    assert (await pg.types(refused))[-1] == "meeting.not_sent"

    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    await pg.execute("UPDATE meetings SET status = 'failed' WHERE id = :m", m=refused)
    unsent = await _entry_less_with_a_session(pg)
    port = ExactRowSpawn(
        pg.repo,
        FakeRuntimeClient(),
        store=pg.store,
        fetch_bot_context=_ctx,
        publisher=FakePublisher(),
        token_secret=None,
        redis_url="redis://r",
    )
    await port.spawn_exact(USER, unsent)
    assert (await pg.aw(unsent))["outcome_kind"] == "not_sent"
    assert (await pg.types(unsent))[-1] == "meeting.not_sent"


# ── an unproven workload of a meeting that ends is deleted ──────────────────────────────────


class _NoAnswer(FakeRuntimeClient):
    """A runtime whose create raises ``error`` (the workload may have been started)."""

    def __init__(self, error: BaseException, **kw: Any) -> None:
        super().__init__(**kw)
        self.error = error

    async def create_workload(self, spec):
        self.specs.append(spec)
        raise self.error


async def test_pg_an_unproven_create_on_the_last_attempt_deletes_the_workload(pg):
    import httpx

    mid = await pg.calendar_meeting()
    await pg.execute(
        "UPDATE meeting_aw_state SET send_attempts = 2 WHERE meeting_id = :m", m=mid
    )
    runtime = _NoAnswer(httpx.ReadError("connection reset"))
    await pg.port(runtime).spawn_exact(USER, mid)
    assert (await pg.row(mid))["status"] == "failed"
    assert runtime.deleted == [runtime.specs[0]["workloadId"]]


async def test_pg_an_unproven_create_on_an_entry_less_meeting_deletes_the_workload(pg):
    from meeting_api.bot_spawn.ports import SpawnFailed

    await pg.execute(
        "INSERT INTO meetings (user_id, platform, platform_specific_id, status, data) "
        "VALUES (:u, 'google_meet', 'abc-defg-hij', 'scheduled', CAST(:d AS jsonb))",
        u=USER,
        d=json.dumps({"scheduled_at": _iso(_now()), "auto_join": True}),
    )
    mid = int(await pg.scalar("SELECT max(id) FROM meetings"))
    runtime = _NoAnswer(SpawnFailed("runtime kernel returned 503: busy", refused=False))
    await pg.port(runtime).spawn_exact(USER, mid)
    assert (await pg.row(mid))["status"] == "failed"
    assert runtime.deleted == [runtime.specs[0]["workloadId"]]


async def test_pg_an_unproven_create_that_is_retried_leaves_the_workload_to_the_proof(
    pg,
):
    import httpx

    mid = await pg.calendar_meeting()
    runtime = _NoAnswer(httpx.ReadError("connection reset"))
    await pg.port(runtime).spawn_exact(USER, mid)
    assert (await pg.row(mid))["data"]["bot_retry"]["proven_gone"] is False
    assert runtime.deleted == []


async def test_pg_a_cancelled_create_leaves_the_meeting_bounded(pg):
    import asyncio

    mid = await pg.calendar_meeting()
    runtime = _NoAnswer(asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await pg.port(runtime).spawn_exact(USER, mid)
    marker = (await pg.row(mid))["data"]["bot_retry"]
    assert (marker["workload"], marker["proven_gone"]) == (
        runtime.specs[0]["workloadId"],
        False,
    )  # unproven: the F-K2 proof and deadline bound it


async def test_pg_a_cancelled_create_on_the_last_attempt_ends_and_deletes(pg):
    import asyncio

    mid = await pg.calendar_meeting()
    await pg.execute(
        "UPDATE meeting_aw_state SET send_attempts = 2 WHERE meeting_id = :m", m=mid
    )
    runtime = _NoAnswer(asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await pg.port(runtime).spawn_exact(USER, mid)
    assert (await pg.row(mid))["status"] == "failed"
    assert runtime.deleted == [runtime.specs[0]["workloadId"]]


async def test_pg_the_last_new_bot_failing_after_its_claim_is_finished_like_any_end(pg):
    from meeting_api.lifecycle.machine import TransitionSource

    mid, session = await pg.sent(status="active")
    app, sink, streams, finalized = await _finishing_app(pg)
    await app.state.apply_lifecycle_event(
        {
            "connection_id": session,
            "status": "completed",
            "completion_reason": "left_alone",
        },
        transition_source=TransitionSource.RUNTIME_DESTROY,
        force_terminal_on_destroy=True,
    )
    await pg.execute(
        "UPDATE meeting_aw_state SET send_attempts = 2 WHERE meeting_id = :m", m=mid
    )
    runtime = FakeRuntimeClient(
        fail=True
    )  # the new bot's create is refused after its claim
    await pg.tick(runtime, at=_later(), finish_meeting=app.state.finish_meeting)
    assert (await pg.row(mid))["status"] == "failed"
    assert sink.events == ["bot.failed"]
    assert finalized == [mid] and streams.reaped == [f"tc:meeting:{mid}"]


# ── an unproven workload's failed delete is retried, bounded (F-I) ──────────────────────────


class _SilentLog:
    def __getattr__(self, name):
        return lambda *a, **k: None


class _DeleteFails(FakeRuntimeClient):
    async def delete_workload(self, workload_id):
        self.deleted.append(f"failed:{workload_id}")
        raise RuntimeError("the kernel is down")


async def _ended_with_a_pending_teardown(pg: Pg) -> tuple[int, str]:
    """A last attempt whose create went unanswered and whose delete failed."""
    import httpx

    mid = await pg.calendar_meeting()
    await pg.execute(
        "UPDATE meeting_aw_state SET send_attempts = 2 WHERE meeting_id = :m", m=mid
    )

    class NoAnswerDeleteFails(_DeleteFails):
        async def create_workload(self, spec):
            self.specs.append(spec)
            raise httpx.ReadError("connection reset")

    runtime = NoAnswerDeleteFails()
    await pg.port(runtime).spawn_exact(USER, mid)
    workload = runtime.specs[0]["workloadId"]
    row = await pg.row(mid)
    assert row["status"] == "failed"
    assert row["data"]["unproven_teardown"]["workload"] == workload
    return mid, workload


async def _sweep(
    pg: Pg, runtime: FakeRuntimeClient, failures: Any, grace: float = 600
) -> None:
    from meeting_api.lifecycle.reconcile import reconcile_stale_nonterminal_sweep

    async def post(body):
        raise AssertionError("no lifecycle terminal expected")

    await reconcile_stale_nonterminal_sweep(
        pg.repo,
        runtime,
        post,
        stop_grace=45,
        active_grace=300,
        log=_SilentLog(),
        untracked_grace=grace,
        item_failures=failures,
    )


def _sweep_items(result: str) -> float:
    from meeting_api.metrics import registry

    value = registry().get_sample_value(
        "aw_sweep_items_total", {"sweep": "unproven-teardown", "result": result}
    )
    return value or 0.0


async def test_pg_a_failed_delete_is_retried_on_the_next_pass_then_cleared(pg):
    from meeting_api.sweeps.item_failures import PostgresItemFailures

    mid, workload = await _ended_with_a_pending_teardown(pg)
    failures = PostgresItemFailures(pg.session_factory, max_failures=5)
    down = _DeleteFails(workloads={workload: {"state": "running"}})
    before = _sweep_items("failed")
    await _sweep(pg, down, failures)
    assert down.deleted == [f"failed:{workload}"]
    assert "unproven_teardown" in (await pg.row(mid))["data"]
    assert _sweep_items("failed") == before + 1
    up = FakeRuntimeClient(workloads={workload: {"state": "running"}})
    await _sweep(pg, up, failures)
    assert up.deleted == [workload]
    assert "unproven_teardown" not in (await pg.row(mid))["data"]


async def test_pg_a_workload_the_runtime_reports_gone_is_cleared(pg):
    from meeting_api.sweeps.item_failures import PostgresItemFailures

    mid, workload = await _ended_with_a_pending_teardown(pg)
    gone = FakeRuntimeClient(workloads={workload: {"state": "destroyed"}})
    await _sweep(pg, gone, PostgresItemFailures(pg.session_factory, max_failures=5))
    assert "unproven_teardown" not in (await pg.row(mid))["data"]
    assert gone.deleted == []


async def test_pg_a_failed_delete_is_given_up_after_the_max_failures_and_counted(pg):
    from meeting_api.sweeps.item_failures import PostgresItemFailures

    mid, workload = await _ended_with_a_pending_teardown(pg)
    failures = PostgresItemFailures(pg.session_factory, max_failures=2)
    down = _DeleteFails(workloads={workload: {"state": "running"}})
    before = _sweep_items("given_up")
    for _ in range(3):
        await _sweep(pg, down, failures)
    assert down.deleted == [f"failed:{workload}"] * 2  # the third pass skips it
    assert _sweep_items("given_up") == before + 1
    item = f"{mid}:{workload}"
    assert await pg.scalar(
        "SELECT gave_up_at IS NOT NULL FROM sweep_item_failures "
        "WHERE sweep = 'unproven-teardown' AND item_id = :i",
        i=item,
    )


async def test_pg_a_404_inside_the_grace_is_not_cleared_and_after_it_is(pg):
    from meeting_api.sweeps.item_failures import PostgresItemFailures

    mid, workload = await _ended_with_a_pending_teardown(pg)
    failures = PostgresItemFailures(pg.session_factory, max_failures=2)
    unknown = FakeRuntimeClient(workloads={})  # 404: the kernel doesn't know it
    for _ in range(3):
        await _sweep(pg, unknown, failures)
    assert "unproven_teardown" in (await pg.row(mid))["data"]
    assert (
        await pg.scalar(
            "SELECT count(*) FROM sweep_item_failures WHERE sweep = 'unproven-teardown'"
        )
        == 0
    )  # waiting out the grace is not a failure
    await pg.execute(
        "UPDATE meetings SET data = jsonb_set(data, '{unproven_teardown,since}', "
        "'\"2026-01-01T00:00:00Z\"') WHERE id = :m",
        m=mid,
    )
    await _sweep(pg, unknown, failures)
    assert "unproven_teardown" not in (await pg.row(mid))["data"]
