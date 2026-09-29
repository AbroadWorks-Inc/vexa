"""§1.4 — every status change goes through the status writer, and each one leaves exactly one
``webhook_outbox`` row with the meeting's sequence one higher.

Four groups:
  * the guard (offline) — nothing in ``src/meeting_api`` outside ``intake/status.py`` writes
    ``meetings.status``. The rule it enforces, over every production source file (AST, not text):
      - no assignment to an attribute named ``status`` (``row.status = …``, ``+=``, annotated);
      - no ``setattr`` whose attribute name is ``"status"`` or not a constant;
      - no ``Meeting(…)`` constructor call (a new row is written with a status, the column
        default included);
      - no ``update(Meeting)`` / ``insert(Meeting)`` (``Meeting.__table__`` counts as
        ``Meeting``, ``Meeting.__table__.update()`` too) and no ``.values(status=…)``;
      - no string literal, or f-string's constant parts, holding ``UPDATE`` / ``INSERT INTO`` the
        ``meetings`` table, bare, quoted or schema-qualified.
    ``ALLOWED`` names the only exemptions, each with its reason. In-memory fakes keep rows as dicts
    (``row["status"] = …``); a dict subscript is not a ``meetings`` write, so the rule doesn't match
    them, and the fakes are test doubles (``intake/fakes.py`` models the writer's sequence and
    outbox itself).
  * the ``completion_reason`` check (offline) — the writer refuses a reason outside the sealed
    ``lifecycle.v1`` set, before writing anything.
  * Postgres: each writer — the bot-spawn repo (create, guarded insert and claim, reopen, the
    session-keyed lifecycle write, ``fail_meeting``, the service-authority stop), the collector
    store (planned create, ``set_intent``, planned edit) and the lifecycle callback — records one
    outbox row per change, sequence + 1, typed where a typed event exists (§2.7), and nothing
    for a write that doesn't change the status; ``requested`` and ``stopping`` reach the outbox;
    each writer takes the link lock first; the lifecycle write changes the status only from its
    caller's predecessors and never off a finished status (a stale stop or replica writes nothing).
  * Postgres: the carries — the spawn recovery never writes over a live workload and a spawn failure
    has one terminal event; an R5 stop's terminal event (the bot's completion through the lifecycle
    callback) carries ``cancelled_by_calendar`` with ``completion_reason: "stopped"``; the legacy
    system URL still receives ``meeting.completed``, now with the meeting's ``uuid``.

The Postgres cases skip cleanly unless ``MEETING_API_TEST_DATABASE_URL`` is set; see
``test_intake_pg_schema.py``'s docstring for the ephemeral SQLAlchemy/asyncpg install.
"""

from __future__ import annotations

import ast
import asyncio
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

import meeting_api
from meeting_api.intake.projection import project_meeting
from meeting_api.intake.status import check_completion_reason
from internal_callers import BOT

USER = 7
PLAT, NID = "google_meet", "kxo-misr-avz"
LINK_KEY = f"aw-intake:{USER}:{PLAT}:{NID}"
SRC = Path(meeting_api.__file__).resolve().parent


# ── the guard ───────────────────────────────────────────────────────────────────────────────

ALL_RULES = frozenset({"attribute", "setattr", "create", "sql", "values", "raw_sql"})

#: The only files allowed to break a rule: path under ``src/meeting_api`` → (rules, reason).
ALLOWED: dict[str, tuple[frozenset[str], str]] = {
    "intake/status.py": (
        ALL_RULES,
        "the status writer itself (write_status, insert_meeting)",
    ),
    "lifecycle/machine.py": (
        frozenset({"attribute"}),
        "MeetingRecord.status is the in-process FSM record, never a meetings row; the lifecycle "
        "callback persists it through update_meeting_status, which calls write_status",
    ),
}

#: ``UPDATE`` / ``INSERT INTO`` the ``meetings`` table, bare, quoted or schema-qualified.
_RAW_SQL = re.compile(
    r'\b(?:update|insert\s+into)\s+(?:"?\w+"?\s*\.\s*)?"?meetings"?(?!\w)',
    re.IGNORECASE,
)


def _callee(node: ast.expr) -> Optional[str]:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _is_meeting(node: ast.expr) -> bool:
    """``Meeting``, ``models.Meeting``, or either one's ``__table__``."""
    if isinstance(node, ast.Attribute) and node.attr == "__table__":
        return _is_meeting(node.value)
    return _callee(node) == "Meeting"


def _sql_text(node: ast.AST) -> Optional[str]:
    """A string literal's text; an f-string's constant parts, each placeholder standing in as
    ``x`` (so ``f"UPDATE {schema}.meetings"`` reads ``UPDATE x.meetings``)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            str(v.value) if isinstance(v, ast.Constant) else "x" for v in node.values
        )
    return None


def _targets(node: ast.AST) -> list[ast.expr]:
    if isinstance(node, ast.Assign):
        found: list[ast.expr] = []
        for target in node.targets:
            found.extend(_flatten(target))
        return found
    if isinstance(node, (ast.AugAssign, ast.AnnAssign)):
        return _flatten(node.target)
    return []


def _flatten(target: ast.expr) -> list[ast.expr]:
    if isinstance(target, (ast.Tuple, ast.List)):
        return [t for element in target.elts for t in _flatten(element)]
    return [target]


def status_writes(source: str) -> list[tuple[int, str]]:
    """Every ``meetings.status`` write the guard's rule finds in ``source``: ``(line, rule)``."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        for target in _targets(node):
            if isinstance(target, ast.Attribute) and target.attr == "status":
                found.append((target.lineno, "attribute"))
        if isinstance(node, ast.Call):
            name = _callee(node.func)
            args = node.args
            # A computed attribute name may be "status": only a constant other name is safe.
            if (
                name == "setattr"
                and len(args) >= 2
                and not (
                    isinstance(args[1], ast.Constant) and args[1].value != "status"
                )
            ):
                found.append((node.lineno, "setattr"))
            if name == "Meeting":
                found.append((node.lineno, "create"))
            if name in ("update", "insert") and args and _is_meeting(args[0]):
                found.append((node.lineno, "sql"))
            if (
                name in ("update", "insert")
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == "__table__"
                and _is_meeting(node.func.value)
            ):
                found.append((node.lineno, "sql"))
            if name == "values" and any(kw.arg == "status" for kw in node.keywords):
                found.append((node.lineno, "values"))
        text = _sql_text(node)
        if text is not None and _RAW_SQL.search(text):
            found.append((getattr(node, "lineno", 0), "raw_sql"))
    return found


@pytest.mark.parametrize(
    "snippet,rule",
    [
        ("m.status = 'failed'", "attribute"),
        ("self.row.status += 'x'", "attribute"),
        ("m.status: str = 'active'", "attribute"),
        ("a, m.status = 1, 'failed'", "attribute"),
        ("setattr(m, 'status', 'failed')", "setattr"),
        ("db.add(Meeting(user_id=1, platform='p'))", "create"),
        ("db.add(models.Meeting(status='requested'))", "create"),
        ("await db.execute(update(Meeting).where(x).values(data={}))", "sql"),
        ("await db.execute(insert(Meeting))", "sql"),
        ("stmt = table.update().values(status='failed')", "values"),
        ("text('UPDATE meetings SET status = :s WHERE id = :m')", "raw_sql"),
        ("text('insert  into meetings (status) values (1)')", "raw_sql"),
        ("setattr(m, name, 'failed')", "setattr"),
        ("setattr(m, f'sta{x}', 'failed')", "setattr"),
        ("text('UPDATE public.meetings SET status = :s')", "raw_sql"),
        ("text('UPDATE \"meetings\" SET status = :s')", "raw_sql"),
        ('text(\'update "public"."meetings" set status = 1\')', "raw_sql"),
        ("text('INSERT INTO public.meetings (status) VALUES (1)')", "raw_sql"),
        ("text(f'UPDATE {schema}.meetings SET status = :s')", "raw_sql"),
        ("text(f'INSERT INTO meetings (status, x) VALUES ({a}, 1)')", "raw_sql"),
        ("await db.execute(update(Meeting.__table__).values(data={}))", "sql"),
        ("await db.execute(insert(models.Meeting.__table__))", "sql"),
        ("await db.execute(Meeting.__table__.update().where(x))", "sql"),
        ("await db.execute(Meeting.__table__.insert())", "sql"),
    ],
)
def test_the_guard_catches_every_kind_of_status_write(snippet, rule):
    assert rule in {r for _, r in status_writes(snippet)}


@pytest.mark.parametrize(
    "snippet",
    [
        "row['status'] = 'failed'",
        "if m.status == 'failed': pass",
        "DeliveryResult(status='failed')",
        "select(Meeting.status).where(Meeting.status == 'active')",
        "text(\"SELECT status FROM meetings WHERE status = 'scheduled'\")",
        "text('UPDATE meeting_aw_state SET event_seq = 1')",
        "text('INSERT INTO meetings_archive (x) VALUES (1)')",
        "setattr(m, 'title', 'x')",
        "await db.execute(update(Transcription.__table__))",
    ],
)
def test_the_guard_ignores_reads_and_dicts(snippet):
    assert status_writes(snippet) == []


def test_nothing_outside_the_status_writer_writes_meetings_status():
    """§1.4: ``write_status`` / ``insert_meeting`` are the only writers of ``meetings.status``."""
    offenders = []
    files = sorted(SRC.rglob("*.py"))
    assert len(files) > 50  # the scan covers the package, not an empty glob
    for path in files:
        rel = path.relative_to(SRC).as_posix()
        allowed = ALLOWED.get(rel, (frozenset(), ""))[0]
        for line, rule in status_writes(path.read_text()):
            if rule not in allowed:
                offenders.append(f"{rel}:{line} ({rule})")
    assert offenders == []


def test_every_exemption_is_still_needed():
    """An exemption that no longer matches anything is removed, not kept "just in case"."""
    for rel, (rules, reason) in ALLOWED.items():
        assert reason
        found = {r for _, r in status_writes((SRC / rel).read_text())}
        assert found & rules, rel


# ── the completion_reason check ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("reason", ["stopped", "left_alone", "max_bot_time_exceeded"])
def test_a_sealed_reason_passes(reason):
    check_completion_reason("completed", {"completion_reason": reason})


def test_no_reason_passes():
    check_completion_reason("failed", None)
    check_completion_reason("failed", {"failure_stage": "requested"})
    check_completion_reason("failed", {"completion_reason": None})


def test_upstreams_start_failed_passes_on_a_failed_row_only():
    check_completion_reason("failed", {"completion_reason": "start_failed"})
    with pytest.raises(ValueError):
        check_completion_reason("completed", {"completion_reason": "start_failed"})


def test_any_other_reason_is_refused():
    with pytest.raises(ValueError, match="sealed lifecycle.v1"):
        check_completion_reason(
            "completed", {"completion_reason": "cancelled_by_calendar"}
        )


def test_the_sealed_set_is_the_schemas():
    from meeting_api.lifecycle.machine import CompletionReason
    from meeting_api.lifecycle.receiver import SEALED_COMPLETION_REASONS

    assert SEALED_COMPLETION_REASONS == {r.value for r in CompletionReason}


@pytest.mark.parametrize(
    "to,outcome_kind,expected",
    [
        ("active", None, "meeting.started"),
        ("completed", None, "meeting.completed"),
        ("completed", "cancelled_by_calendar", "meeting.completed"),
        ("failed", None, "bot.failed"),
        ("failed", "cancelled_by_calendar", "bot.failed"),
        ("failed", "not_sent", "meeting.not_sent"),
        ("requested", None, "meeting.status_change"),
        ("joining", None, "meeting.status_change"),
        ("awaiting_admission", None, "meeting.status_change"),
        ("needs_help", None, "meeting.status_change"),
        ("stopping", "cancelled_by_calendar", "meeting.status_change"),
        ("scheduled", None, "meeting.status_change"),
        ("idle", None, "meeting.status_change"),
    ],
)
def test_a_status_changes_event_is_typed_where_a_typed_event_exists(
    to, outcome_kind, expected
):
    """§2.7: one event per change, typed where §2.7 has a typed event."""
    from meeting_api.intake.status import typed_event

    assert typed_event(to, outcome_kind) == expected


def test_every_typed_event_is_a_sealed_webhook_v1_event_type():
    from meeting_api.lifecycle.webhook import _SCHEMA

    sealed = set(_SCHEMA["$defs"]["EventType"]["enum"])
    from meeting_api.intake.status import typed_event

    for to in ("active", "completed", "failed", "joining"):
        for kind in (None, "not_sent"):
            assert typed_event(to, kind) in sealed


async def test_an_intake_created_meetings_first_event_records_its_status():
    """The fake store models the writer: ``meeting.scheduled`` carries ``change`` too."""
    from intake_builders import make_harness

    h = make_harness()
    await h.put(start="2026-09-29T10:00:00Z", end="2026-09-29T10:30:00Z")
    first = h.store.events[0]
    assert first.event_type == "meeting.scheduled"
    assert first.change is not None
    assert (first.change["from"], first.change["to"], first.change["reason"]) == (
        None,
        "scheduled",
        None,
    )
    assert first.change["at"].endswith("Z")


def test_the_projection_renders_an_unparseable_scheduled_at_as_no_time():
    """An upstream row's ``data.scheduled_at`` can be free text; every writer builds an event, so
    the projection never raises on it."""
    for status in ("scheduled", "requested"):
        projected = project_meeting(
            {
                "uuid": "5f0c2b7e-8d1a-4c3e-9b6f-2a7d1e4c8b90",
                "status": status,
                "data": {"scheduled_at": "tomorrow 10am"},
                "start_time": None,
            },
            None,
            [],
            lead_s=300,
        )
        assert projected["start"] is None
        assert projected["bot_joins_at"] is None


# ── Postgres ────────────────────────────────────────────────────────────────────────────────


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class _Pg:
    def __init__(self, engine: Any) -> None:
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from meeting_api.bot_spawn.adapters import SqlAlchemyMeetingRepo
        from meeting_api.collector.adapters import SqlAlchemyTranscriptStore

        self.engine = engine
        self.session_factory = async_sessionmaker(engine, expire_on_commit=False)
        self.repo = SqlAlchemyMeetingRepo(self.session_factory)
        self.store = SqlAlchemyTranscriptStore(self.session_factory)

    async def exec(self, sql: str, **params: Any) -> Any:
        from sqlalchemy import text

        async with self.engine.begin() as conn:
            return await conn.execute(text(sql), params)

    async def scalar(self, sql: str, **params: Any) -> Any:
        from sqlalchemy import text

        async with self.engine.connect() as conn:
            return (await conn.execute(text(sql), params)).scalar()

    async def seed(
        self,
        status: str,
        *,
        data: Optional[dict] = None,
        session_uid: Optional[str] = None,
        native: Optional[str] = NID,
    ) -> int:
        """A row written behind the writer's back (as a test fixture), without events."""
        mid = int(
            (
                await self.exec(
                    "INSERT INTO meetings (user_id, platform, platform_specific_id, status, data) "
                    "VALUES (:u, :p, :n, :s, CAST(:d AS jsonb)) RETURNING id",
                    u=USER,
                    p=PLAT,
                    n=native,
                    s=status,
                    d=json.dumps(data or {}),
                )
            ).scalar_one()
        )
        if session_uid is not None:
            await self.session(mid, session_uid)
        return mid

    async def session(self, mid: int, session_uid: str) -> None:
        await self.exec(
            "INSERT INTO meeting_sessions (meeting_id, session_uid) VALUES (:m, :u)",
            m=mid,
            u=session_uid,
        )

    async def status(self, mid: int) -> str:
        return str(
            await self.scalar("SELECT status FROM meetings WHERE id = :m", m=mid)
        )

    async def events(self, mid: int) -> list[dict]:
        from sqlalchemy import text

        async with self.engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT event_type, sequence, payload_text FROM webhook_outbox "
                        "WHERE meeting_id = :m ORDER BY sequence"
                    ),
                    {"m": mid},
                )
            ).all()
        return [
            {"event_type": t, "sequence": int(s), "payload": json.loads(p)}
            for t, s, p in rows
        ]

    async def seq(self, mid: int) -> int:
        value = await self.scalar(
            "SELECT event_seq FROM meeting_aw_state WHERE meeting_id = :m", m=mid
        )
        return int(value or 0)

    async def only_meeting(self) -> int:
        return int(await self.scalar("SELECT max(id) FROM meetings"))


@pytest.fixture
def pg(link_pg_engine: Any) -> _Pg:
    return _Pg(link_pg_engine)


@dataclass
class _Before:
    events: int
    seq: int


async def _before(pg: _Pg, mid: int) -> _Before:
    return _Before(len(await pg.events(mid)), await pg.seq(mid))


#: §2.7: the event type of a status change without an explicit one (``failed`` with a
#: ``not_sent`` outcome is ``meeting.not_sent``, passed explicitly by those tests).
TYPED = {
    "active": "meeting.started",
    "completed": "meeting.completed",
    "failed": "bot.failed",
}


async def _one_change(
    pg: _Pg,
    mid: int,
    before: _Before,
    to: str,
    *,
    frm: Optional[str],
    event_type: str = "",
) -> dict:
    """Exactly one new outbox row, sequence + 1, recording ``frm`` → ``to``; returns its payload."""
    events = await pg.events(mid)
    assert len(events) == before.events + 1, [e["event_type"] for e in events]
    last = events[-1]
    assert last["sequence"] == before.seq + 1 == await pg.seq(mid)
    assert last["event_type"] == (event_type or TYPED.get(to, "meeting.status_change"))
    payload = last["payload"]
    assert payload["data"]["change"]["from"] == frm
    assert payload["data"]["change"]["to"] == to
    assert payload["data"]["meeting"]["status"] == to
    assert payload["data"]["meeting"]["sequence"] == last["sequence"]
    assert await pg.status(mid) == to
    return payload


async def _no_change(pg: _Pg, mid: int, before: _Before) -> None:
    assert await _before(pg, mid) == before


# ── Postgres: the bot-spawn repo ────────────────────────────────────────────────────────────


async def test_pg_create_meeting_records_requested(pg):
    row = await pg.repo.create_meeting(
        user_id=USER, platform=PLAT, native_meeting_id=NID, data={"k": "v"}
    )
    payload = await _one_change(pg, row["id"], _Before(0, 0), "requested", frm=None)
    assert payload["data"]["meeting"]["id"] == str(
        await pg.scalar("SELECT uuid FROM meetings WHERE id = :m", m=row["id"])
    )


async def test_pg_create_meeting_without_a_link_takes_no_link_lock(pg):
    async with pg.engine.connect() as holder:
        tx = await holder.begin()
        await _hold_link(holder, f"aw-intake:{USER}:{PLAT}:None")
        row = await asyncio.wait_for(
            pg.repo.create_meeting(
                user_id=USER, platform=PLAT, native_meeting_id=None, data={}
            ),
            timeout=5,
        )
        await tx.rollback()
    await _one_change(pg, row["id"], _Before(0, 0), "requested", frm=None)


async def test_pg_an_intake_created_meetings_first_event_records_its_status(pg):
    from intake_builders import entry_body, make_settings

    from meeting_api.intake import IntakeService, PostgresIntakeStore
    from meeting_api.intake.fakes import NoStop
    from meeting_api.intake.sweeps import OutboxOnly

    store = PostgresIntakeStore(pg.session_factory)
    service = IntakeService(
        store, _NoSpawnPort(), NoStop(), OutboxOnly(), make_settings()
    )
    await service.put_entry(
        USER, entry_body(start="2026-10-20T09:00:00Z", end="2026-10-20T09:30:00Z")
    )
    mid = await pg.only_meeting()
    [first] = await pg.events(mid)
    assert (first["event_type"], first["sequence"]) == ("meeting.scheduled", 1)
    change = first["payload"]["data"]["change"]
    assert (change["from"], change["to"], change["reason"]) == (None, "scheduled", None)


async def test_pg_post_bots_insert_records_requested(pg):
    """``requested`` reaches subscribers: the upstream ``POST /bots`` insert has its event."""
    row = await pg.repo.create_meeting_guarded(
        user_id=USER, platform=PLAT, native_meeting_id=NID, data={}, max_concurrent=5
    )
    await _one_change(pg, row["id"], _Before(0, 0), "requested", frm=None)


async def test_pg_post_bots_claim_of_a_plan_records_requested(pg):
    mid = await pg.seed("idle", data={"title": "plan", "stop_requested": True})
    before = await _before(pg, mid)
    row = await pg.repo.create_meeting_guarded(
        user_id=USER, platform=PLAT, native_meeting_id=NID, data={"k": "v"}
    )
    assert row["id"] == mid
    payload = await _one_change(pg, mid, before, "requested", frm="idle")
    assert payload["data"]["meeting"]["title"] == "plan"
    assert "stop_requested" not in row["data"]
    assert row["data"]["k"] == "v"


async def test_pg_reopen_records_requested(pg):
    mid = await pg.seed("completed", data={"completion_reason": "stopped"})
    before = await _before(pg, mid)
    row = await pg.repo.reopen_meeting(
        meeting_id=mid, data_patch={"k": "v", "gone": None}
    )
    payload = await _one_change(pg, mid, before, "requested", frm="completed")
    assert (
        payload["data"]["meeting"]["completion_reason"] is None
    )  # archived with the run
    assert row["data"]["k"] == "v"


async def test_pg_every_lifecycle_step_records_one_event(pg):
    """The session-keyed write the lifecycle callback, the reconcile sweeps and the upstream stop
    use: each change one event (``stopping`` included), a repeat of the status none."""
    mid = await pg.seed("requested", session_uid="sess-1")
    prev = "requested"
    for status in ("joining", "awaiting_admission", "active", "stopping", "completed"):
        before = await _before(pg, mid)
        row = await pg.repo.update_meeting_status(
            session_uid="sess-1",
            status=status,
            completion_reason="stopped" if status == "completed" else None,
            data={"step": status},
            change_reason=f"to {status}",
        )
        payload = await _one_change(pg, mid, before, status, frm=prev)
        assert payload["data"]["change"]["reason"] == f"to {status}"
        assert row is not None and row["sequence"] == before.seq + 1
        prev = status
    before = await _before(pg, mid)
    row = await pg.repo.update_meeting_status(
        session_uid="sess-1", status="completed", data={"provenance": "frozen"}
    )
    await _no_change(pg, mid, before)
    assert row is not None and row["data"]["provenance"] == "frozen"
    assert row["data"]["completion_reason"] == "stopped"


async def test_pg_the_lifecycle_write_refuses_an_unsealed_reason_and_writes_nothing(pg):
    mid = await pg.seed("active", session_uid="sess-1")
    before = await _before(pg, mid)
    with pytest.raises(ValueError):
        await pg.repo.update_meeting_status(
            session_uid="sess-1", status="completed", completion_reason="not_a_reason"
        )
    await _no_change(pg, mid, before)
    assert await pg.status(mid) == "active"


async def test_pg_fail_meeting_records_failed(pg):
    from meeting_api.intake.status import Outcome

    plain = await pg.seed("requested")
    before = await _before(pg, plain)
    await pg.repo.fail_meeting(meeting_id=plain, reason="no workload")
    payload = await _one_change(pg, plain, before, "failed", frm="requested")
    assert payload["data"]["meeting"]["completion_reason"] == "start_failed"

    typed = await pg.seed("requested", native="abc-defg-hij")
    before = await _before(pg, typed)
    await pg.repo.fail_meeting(
        meeting_id=typed,
        reason="kernel said no",
        outcome=Outcome("not_sent", "spawn_error", "kernel said no"),
    )
    payload = await _one_change(
        pg, typed, before, "failed", frm="requested", event_type="meeting.not_sent"
    )
    assert payload["data"]["meeting"]["outcome"]["detail"] == "spawn_error"
    assert payload["data"]["change"]["reason"] == "spawn_error"


async def test_pg_fail_meeting_keeps_an_outcome_already_recorded(pg):
    from meeting_api.intake.status import Outcome

    mid = await pg.seed("requested", data={"stop_requested": True})
    await pg.exec(
        "INSERT INTO meeting_aw_state (meeting_id, outcome_kind, outcome_detail) "
        "VALUES (:m, 'cancelled_by_calendar', 'deleted')",
        m=mid,
    )
    before = await _before(pg, mid)
    await pg.repo.fail_meeting(
        meeting_id=mid,
        reason="fenced",
        completion_reason="stopped",
        outcome=Outcome("not_sent", "meeting_stopped", "stopped"),
    )
    payload = await _one_change(pg, mid, before, "failed", frm="requested")
    assert payload["data"]["meeting"]["outcome"]["kind"] == "cancelled_by_calendar"
    assert payload["data"]["meeting"]["completion_reason"] == "stopped"


@pytest.mark.parametrize("finished", ["completed", "failed"])
async def test_pg_fail_meeting_leaves_a_finished_row_alone(pg, finished):
    mid = await pg.seed(finished, data={"completion_reason": "left_alone"})
    before = await _before(pg, mid)
    row = await pg.repo.fail_meeting(meeting_id=mid, reason="late")
    await _no_change(pg, mid, before)
    assert row is not None and row["status"] == finished
    assert row["data"]["completion_reason"] == "left_alone"


async def test_pg_the_service_authority_stop_records_stopping(pg):
    """``stopping`` reaches subscribers from the service-authority stop too."""
    from meeting_api.service_authority import ServiceAuthorityDecision

    mid = await pg.seed(
        "active",
        data={"service_authority": {"service_identity": "svc-1", "mode": "enforce"}},
    )
    decision = ServiceAuthorityDecision(
        authority_version="service-authority.v1",
        decision_id="decision-stop",
        request_id="svc-1:continue",
        service_identity="svc-1",
        allow=False,
        reason="opaque_limit",
        decided_at=datetime.now(timezone.utc),
        stop_scope="billable_service",
    )
    request = SimpleNamespace(
        service_identity="svc-1", boundary_at=datetime.now(timezone.utc)
    )
    before = await _before(pg, mid)
    assert await pg.repo.record_service_authority_decision(
        meeting_id=mid, request=request, decision=decision
    )
    await _one_change(pg, mid, before, "stopping", frm="active")


# ── Postgres: the collector store ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "scheduled_at,status", [(None, "idle"), ("2026-10-20T09:00:00Z", "scheduled")]
)
async def test_pg_a_planned_create_records_its_status(pg, scheduled_at, status):
    row = await pg.store.create_planned_meeting(
        USER, platform=PLAT, native_meeting_id=NID, scheduled_at=scheduled_at
    )
    await _one_change(pg, row["id"], _Before(0, 0), status, frm=None)


async def test_pg_set_intent_records_a_change_and_only_a_change(pg):
    mid = await pg.seed("idle")
    before = await _before(pg, mid)
    await pg.store.set_intent(
        USER, PLAT, NID, "scheduled", scheduled_at="2026-10-20T09:00:00Z"
    )
    await _one_change(pg, mid, before, "scheduled", frm="idle")
    before = await _before(pg, mid)
    result = await pg.store.set_intent(
        USER, PLAT, NID, "scheduled", scheduled_at="2026-10-21T09:00:00Z"
    )
    assert result is not None and result["changed"] is True
    await _no_change(pg, mid, before)


async def test_pg_a_planned_edit_records_a_status_change_only(pg):
    mid = await pg.seed("idle")
    before = await _before(pg, mid)
    await pg.store.update_planned_meeting(USER, mid, {"title": "renamed"})
    await _no_change(pg, mid, before)
    await pg.store.update_planned_meeting(
        USER, mid, {"scheduled_at": "2026-10-20T09:00:00Z"}
    )
    payload = await _one_change(pg, mid, before, "scheduled", frm="idle")
    assert payload["data"]["meeting"]["title"] == "renamed"


async def test_pg_a_planned_edit_that_moves_the_link_locks_both_links(pg):
    mid = await pg.seed("scheduled", data={"scheduled_at": "2026-10-20T09:00:00Z"})
    other = f"aw-intake:{USER}:{PLAT}:abc-defg-hij"
    async with pg.engine.connect() as holder:
        tx = await holder.begin()
        await _hold_link(holder, other)
        edit = asyncio.create_task(
            pg.store.update_planned_meeting(
                USER, mid, {"native_meeting_id": "abc-defg-hij"}
            )
        )
        await _waiting_on_advisory(pg)
        assert not edit.done()
        await tx.rollback()
    row = await edit
    assert row is not None and row["native_meeting_id"] == "abc-defg-hij"


# ── Postgres: the lifecycle callback ────────────────────────────────────────────────────────


async def _callback(
    repo: Any, *events: dict, sink: Any = None, store: Any = None
) -> None:
    import httpx

    from meeting_api import create_app

    app = create_app(meeting_repo=repo, system_webhook_sink=sink, meeting_store=store)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        for event in events:
            r = await client.post(
                "/bots/internal/callback/lifecycle", headers=BOT, json=event
            )
            assert r.status_code == 200, r.text


def _event(status: str, **extra: Any) -> dict:
    return {
        "connection_id": "sess-cb",
        "status": status,
        "timestamp": "2026-10-20T09:00:00.000Z",
        **extra,
    }


async def test_pg_the_lifecycle_callback_records_one_event_per_step(pg):
    mid = await pg.seed("requested", session_uid="sess-cb")
    await _callback(
        pg.repo,
        _event("joining"),
        _event("active"),
        _event("completed", completion_reason="stopped", exit_code=0),
        _event(
            "completed", completion_reason="stopped", exit_code=0
        ),  # the bot's retry
    )
    events = await pg.events(mid)
    assert [
        (e["sequence"], e["event_type"], e["payload"]["data"]["change"]["to"])
        for e in events
    ] == [
        (1, "meeting.status_change", "joining"),
        (2, "meeting.started", "active"),
        (3, "meeting.completed", "completed"),
    ]
    assert await pg.seq(mid) == 3


async def test_pg_a_stale_stop_never_reopens_a_completed_meeting(pg):
    """The upstream stop acts on a row it read before any lock; the meeting completed meanwhile.
    The stop's ``stopping`` is refused: no change, no event."""
    from meeting_api.lifecycle.stop_router import _mark_stop_requested

    mid = await pg.seed("active", session_uid="sess-stale")
    stale = await pg.repo.get_meeting(mid)
    await pg.repo.update_meeting_status(
        session_uid="sess-stale", status="completed", completion_reason="left_alone"
    )
    before = await _before(pg, mid)
    await _mark_stop_requested(pg.repo, stale)
    await _no_change(pg, mid, before)
    assert await pg.status(mid) == "completed"


async def test_pg_a_stale_reconcile_failed_after_completed_writes_nothing(pg):
    """A replica whose in-process record still says ``active`` posts the reconcile sweep's
    ``failed`` after the meeting completed: no second terminal event, entries not re-closed.
    """
    from meeting_api.lifecycle.machine import MeetingStore

    mid = await pg.seed("active", session_uid="sess-cb")
    await pg.exec(
        "INSERT INTO meeting_entries (user_id, source_user, external_id, meeting_id, "
        "meeting_url, platform, native_meeting_id, start_at, state, attendees, join_now, "
        "content_hash) VALUES (:u, 'a@abroadworks.com', 'google:x', :m, :url, :p, :n, "
        "now() - interval '1 hour', 'active', '{}', false, 'h')",
        u=USER,
        m=mid,
        url=f"https://meet.google.com/{NID}",
        p=PLAT,
        n=NID,
    )
    await _callback(pg.repo, _event("completed", completion_reason="left_alone"))
    closed_at = await pg.scalar(
        "SELECT closed_at FROM meeting_entries WHERE meeting_id = :m", m=mid
    )
    assert closed_at is not None
    before = await _before(pg, mid)

    store = MeetingStore()
    store.rehydrate("sess-cb", "active", {})
    await _callback(
        pg.repo,
        _event("failed", completion_reason="join_failure", reason="workload gone"),
        store=store,
    )
    await _no_change(pg, mid, before)
    assert await pg.status(mid) == "completed"
    assert (
        await pg.scalar(
            "SELECT closed_at FROM meeting_entries WHERE meeting_id = :m", m=mid
        )
        == closed_at
    )


async def test_pg_a_stale_replicas_edge_changes_only_from_its_own_from(pg):
    """The callback passes the edge's ``from`` as the predecessors: a replica that last saw
    ``joining`` can't move a meeting another replica already moved to ``stopping``."""
    from meeting_api.lifecycle.machine import MeetingStore

    mid = await pg.seed("stopping", session_uid="sess-cb")
    store = MeetingStore()
    store.rehydrate("sess-cb", "joining", {})
    before = await _before(pg, mid)
    await _callback(pg.repo, _event("active"), store=store)
    await _no_change(pg, mid, before)
    assert await pg.status(mid) == "stopping"


async def test_pg_the_lifecycle_write_takes_the_callers_predecessors(pg):
    mid = await pg.seed("joining", session_uid="sess-p")
    before = await _before(pg, mid)
    row = await pg.repo.update_meeting_status(
        session_uid="sess-p", status="active", expected_from={"awaiting_admission"}
    )
    assert row is None
    await _no_change(pg, mid, before)
    await pg.repo.update_meeting_status(
        session_uid="sess-p", status="active", expected_from={"joining"}
    )
    await _one_change(pg, mid, before, "active", frm="joining")


async def test_pg_a_finished_meeting_never_changes_status_again(pg):
    """Even a caller naming the finished status as a predecessor can't reopen it."""
    mid = await pg.seed("completed", session_uid="sess-f")
    before = await _before(pg, mid)
    row = await pg.repo.update_meeting_status(
        session_uid="sess-f", status="active", expected_from={"completed"}
    )
    assert row is None
    await _no_change(pg, mid, before)
    assert await pg.status(mid) == "completed"


# ── Postgres: every writer takes the link lock first ────────────────────────────────────────


async def _hold_link(conn: Any, key: str = LINK_KEY) -> None:
    from sqlalchemy import text

    await conn.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": key}
    )


async def _waiting_on_advisory(pg: _Pg) -> None:
    for _ in range(300):
        waiting = await pg.scalar(
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE wait_event_type = 'Lock' AND wait_event = 'advisory'"
        )
        if int(waiting) >= 1:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the writer never queued on the link lock")


async def _prep_requested(pg: _Pg) -> tuple[int, Any]:
    mid = await pg.seed("requested", session_uid="sess-lock")
    return mid, lambda: pg.repo.update_meeting_status(
        session_uid="sess-lock", status="joining"
    )


async def _prep_fail(pg: _Pg) -> tuple[int, Any]:
    mid = await pg.seed("requested")
    return mid, lambda: pg.repo.fail_meeting(meeting_id=mid, reason="x")


async def _prep_reopen(pg: _Pg) -> tuple[int, Any]:
    mid = await pg.seed("failed")
    return mid, lambda: pg.repo.reopen_meeting(meeting_id=mid)


async def _prep_intent(pg: _Pg) -> tuple[int, Any]:
    mid = await pg.seed("idle")
    return mid, lambda: pg.store.set_intent(
        USER, PLAT, NID, "scheduled", scheduled_at="2026-10-20T09:00:00Z"
    )


async def _prep_planned_edit(pg: _Pg) -> tuple[int, Any]:
    mid = await pg.seed("idle")
    return mid, lambda: pg.store.update_planned_meeting(
        USER, mid, {"scheduled_at": "2026-10-20T09:00:00Z"}
    )


async def _prep_planned_create(pg: _Pg) -> tuple[int, Any]:
    return 0, lambda: pg.store.create_planned_meeting(
        USER, platform=PLAT, native_meeting_id=NID
    )


async def _prep_create(pg: _Pg) -> tuple[int, Any]:
    return 0, lambda: pg.repo.create_meeting(
        user_id=USER, platform=PLAT, native_meeting_id=NID, data={}
    )


@pytest.mark.parametrize(
    "prep",
    [
        _prep_requested,
        _prep_fail,
        _prep_reopen,
        _prep_intent,
        _prep_planned_edit,
        _prep_planned_create,
        _prep_create,
    ],
    ids=lambda p: p.__name__.removeprefix("_prep_"),
)
async def test_pg_every_writer_takes_the_link_lock_before_writing(pg, prep):
    mid, call = await prep(pg)
    before = await pg.scalar("SELECT count(*) FROM webhook_outbox")
    async with pg.engine.connect() as holder:
        tx = await holder.begin()
        await _hold_link(holder)
        write = asyncio.create_task(call())
        await _waiting_on_advisory(pg)
        assert not write.done()
        assert await pg.scalar("SELECT count(*) FROM webhook_outbox") == before
        await tx.rollback()
    await write
    assert await pg.scalar("SELECT count(*) FROM webhook_outbox") == before + 1


async def test_pg_the_planned_create_takes_the_link_lock_before_the_user_lock(pg):
    from sqlalchemy import text

    async with pg.engine.connect() as holder:
        tx = await holder.begin()
        await holder.execute(text("SELECT pg_advisory_xact_lock(:u)"), {"u": USER})
        create = asyncio.create_task(
            pg.store.create_planned_meeting(USER, platform=PLAT, native_meeting_id=NID)
        )
        await _waiting_on_advisory(pg)
        async with pg.engine.connect() as probe:
            took = (
                await probe.execute(
                    text("SELECT pg_try_advisory_lock(hashtextextended(:k, 0))"),
                    {"k": LINK_KEY},
                )
            ).scalar_one()
        assert took is False  # the waiting create already holds the link lock
        await tx.rollback()
    await create


# ── Postgres: the spawn recovery (A9 carries) ───────────────────────────────────────────────


def _spawn_port(repo: Any, runtime: Any, store: Any) -> Any:
    from meeting_api.intake.spawn import ExactRowSpawn

    async def ctx(user_id: int) -> dict:
        return {"max_concurrent": 5, "bot_name": "AW Notetaker"}

    return ExactRowSpawn(
        repo,
        runtime,
        store=store,
        fetch_bot_context=ctx,
        token_secret="test-token-secret",
        redis_url="redis://r",
    )


async def _seed_due(pg: _Pg) -> int:
    return await pg.seed(
        "scheduled",
        data={
            "auto_join": True,
            "scheduled_at": _iso(datetime.now(timezone.utc)),
            "constructed_meeting_url": f"https://meet.google.com/{NID}",
        },
    )


async def test_pg_a_runtime_spawn_failure_has_one_terminal_event(pg):
    """The spawn flow writes ``failed`` through the writer with the ``not_sent`` outcome; the port's
    recovery finds the meeting ended and writes nothing more."""
    from meeting_api.bot_spawn.fakes import FakeRuntimeClient
    from meeting_api.intake import PostgresIntakeStore

    mid = await _seed_due(pg)
    port = _spawn_port(
        pg.repo, FakeRuntimeClient(fail=True), PostgresIntakeStore(pg.session_factory)
    )
    outcome = await port.spawn_exact(USER, mid)
    assert (outcome.result, outcome.code) == ("failed", "spawn_error")
    events = await pg.events(mid)
    assert [
        (e["event_type"], e["payload"]["data"]["change"]["to"]) for e in events
    ] == [
        ("meeting.status_change", "requested"),
        ("meeting.not_sent", "failed"),
    ]
    assert (
        events[-1]["payload"]["data"]["meeting"]["outcome"]["detail"] == "spawn_error"
    )


async def test_pg_the_recovery_never_writes_over_a_live_workload(pg, capsys):
    """A failure after the workload id was recorded (here: the interlock read) leaves the row
    ``requested`` with its bot: the recovery skips the terminal write and logs it."""
    from meeting_api.bot_spawn.adapters import SqlAlchemyMeetingRepo
    from meeting_api.bot_spawn.fakes import FakeRuntimeClient
    from meeting_api.intake import PostgresIntakeStore

    class ReadFailsAfterSpawn(SqlAlchemyMeetingRepo):
        async def get_lifecycle_state_by_session(self, *, session_uid):
            raise RuntimeError("database went away")

    mid = await _seed_due(pg)
    runtime = FakeRuntimeClient()
    port = _spawn_port(
        ReadFailsAfterSpawn(pg.session_factory),
        runtime,
        PostgresIntakeStore(pg.session_factory),
    )
    outcome = await port.spawn_exact(USER, mid)
    assert outcome.result == "failed"
    assert await pg.status(mid) == "requested"
    assert await pg.scalar("SELECT bot_container_id FROM meetings WHERE id = :m", m=mid)
    assert [e["event_type"] for e in await pg.events(mid)] == ["meeting.status_change"]
    assert (
        await pg.scalar(
            "SELECT outcome_kind FROM meeting_aw_state WHERE meeting_id = :m", m=mid
        )
        is None
    )
    assert "spawn_not_sent_skipped_live_workload" in capsys.readouterr().out


async def test_pg_a_stop_fenced_spawn_keeps_an_r5_outcome(pg):
    """R5 removed the last entry while the bot was being spawned (stop flag and outcome recorded):
    the fence's ``failed`` keeps ``cancelled_by_calendar`` and is the only terminal event.
    """
    from meeting_api.bot_spawn.adapters import SqlAlchemyMeetingRepo
    from meeting_api.bot_spawn.fakes import FakeRuntimeClient
    from meeting_api.intake import PostgresIntakeStore

    engine = pg.engine

    class R5AfterClaim(SqlAlchemyMeetingRepo):
        async def create_meeting_guarded(self, **kwargs):
            row = await super().create_meeting_guarded(**kwargs)
            await self.merge_meeting_data(row["id"], {"stop_requested": True})
            from sqlalchemy import text

            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "UPDATE meeting_aw_state SET outcome_kind = 'cancelled_by_calendar', "
                        "outcome_detail = 'deleted' WHERE meeting_id = :m"
                    ),
                    {"m": row["id"]},
                )
            return row

    mid = await _seed_due(pg)
    runtime = FakeRuntimeClient()
    port = _spawn_port(
        R5AfterClaim(pg.session_factory),
        runtime,
        PostgresIntakeStore(pg.session_factory),
    )
    outcome = await port.spawn_exact(USER, mid)
    assert (outcome.result, outcome.code) == ("failed", "meeting_stopped")
    assert runtime.specs == []
    events = await pg.events(mid)
    assert [
        (e["event_type"], e["payload"]["data"]["change"]["to"]) for e in events
    ] == [
        ("meeting.status_change", "requested"),
        ("bot.failed", "failed"),
    ]
    terminal = events[-1]["payload"]["data"]["meeting"]
    assert terminal["outcome"]["kind"] == "cancelled_by_calendar"
    assert terminal["completion_reason"] == "stopped"


# ── Postgres: R5's terminal event (A12 carry) ───────────────────────────────────────────────


async def test_pg_an_r5_stopped_meetings_terminal_event_carries_the_outcome(pg):
    """R5 end to end: the last entry of a live meeting is removed (``stopping`` + outcome), the bot
    completes through the lifecycle callback, and the TERMINAL outbox event carries
    ``cancelled_by_calendar`` with ``completion_reason: "stopped"``."""
    from intake_builders import A, entry_body, make_settings

    from meeting_api.bot_spawn.fakes import FakeRuntimeClient
    from meeting_api.intake import IntakeService, IntakeStop, PostgresIntakeStore
    from meeting_api.intake.sweeps import OutboxOnly
    from meeting_api.lifecycle.stop_router import InMemoryCommandPublisher

    store = PostgresIntakeStore(pg.session_factory)
    stop = IntakeStop(
        store, InMemoryCommandPublisher(), FakeRuntimeClient(), publisher=OutboxOnly()
    )
    service = IntakeService(store, _NoSpawnPort(), stop, OutboxOnly(), make_settings())
    reply = await service.put_entry(
        USER, entry_body(start="2026-10-20T09:00:00Z", end="2026-10-20T09:30:00Z")
    )
    mid = int(
        await pg.scalar(
            "SELECT id FROM meetings WHERE uuid = CAST(:u AS uuid)",
            u=reply["meeting"]["id"],
        )
    )
    # The bot is sent (the exact-row claim) and reaches the meeting (the lifecycle callback).
    await pg.repo.create_meeting_guarded(
        user_id=USER,
        platform=PLAT,
        native_meeting_id=NID,
        data={},
        claim_meeting_id=mid,
    )
    await pg.session(mid, "sess-cb")
    await _callback(pg.repo, _event("joining"), _event("active"))

    removed = await service.remove_entry(
        USER, {"external_id": "google:3n5kq8example", "user": A, "reason": "deleted"}
    )
    assert removed["result"] == "bot_stopping"

    await _callback(
        pg.repo, _event("completed", completion_reason="stopped", exit_code=0)
    )

    events = await pg.events(mid)
    assert [e["sequence"] for e in events] == list(range(1, len(events) + 1))
    assert [
        (e["event_type"], e["payload"]["data"].get("change", {}).get("to"))
        for e in events
    ] == [
        ("meeting.scheduled", "scheduled"),
        ("meeting.status_change", "requested"),
        ("meeting.status_change", "joining"),
        ("meeting.started", "active"),
        ("meeting.updated", None),  # the entry removed
        ("meeting.status_change", "stopping"),
        ("meeting.completed", "completed"),
    ]
    terminal = events[-1]["payload"]["data"]
    assert terminal["change"]["from"] == "stopping"
    assert terminal["meeting"]["status"] == "completed"
    assert terminal["meeting"]["completion_reason"] == "stopped"
    assert terminal["meeting"]["outcome"]["kind"] == "cancelled_by_calendar"
    assert terminal["meeting"]["outcome"]["detail"] == "deleted"


# ── Postgres: a move while live, then every finishing writer (R7) ───────────────────────────


def _intake(pg: _Pg, spawn: Any = None) -> tuple[Any, Any]:
    from intake_builders import make_settings

    from meeting_api.intake import IntakeService, PostgresIntakeStore
    from meeting_api.intake.fakes import NoStop
    from meeting_api.intake.sweeps import OutboxOnly

    store = PostgresIntakeStore(pg.session_factory)
    return store, IntakeService(
        store, spawn or _NoSpawnPort(), NoStop(), OutboxOnly(), make_settings()
    )


def _tomorrow_body() -> dict:
    from datetime import timedelta

    from intake_builders import entry_body

    tomorrow = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=1)
    return entry_body(start=_iso(tomorrow), end=_iso(tomorrow + timedelta(hours=1)))


async def _uuid(pg: _Pg, mid: int) -> str:
    return str(await pg.scalar("SELECT uuid FROM meetings WHERE id = :m", m=mid))


async def _moved_while_live(
    pg: _Pg, service: Any, *, reach_the_call: bool
) -> tuple[int, dict, dict]:
    """A call that started 5 minutes ago gets its bot (claimed; with ``reach_the_call`` it goes
    ``joining`` → ``active`` through the lifecycle callback); then its entry moves to tomorrow,
    which leaves the live meeting at once (R7). Returns the live meeting's id, the moved body and
    the reply."""
    from datetime import timedelta

    from intake_builders import entry_body

    now = datetime.now(timezone.utc).replace(microsecond=0)
    reply = await service.put_entry(
        USER,
        entry_body(
            start=_iso(now - timedelta(minutes=5)),
            end=_iso(now + timedelta(minutes=55)),
        ),
    )
    mid = int(
        await pg.scalar(
            "SELECT id FROM meetings WHERE uuid = CAST(:u AS uuid)",
            u=reply["meeting"]["id"],
        )
    )
    await pg.repo.create_meeting_guarded(
        user_id=USER,
        platform=PLAT,
        native_meeting_id=NID,
        data={},
        claim_meeting_id=mid,
    )
    await pg.session(mid, "sess-cb")
    if reach_the_call:
        await _callback(pg.repo, _event("joining"), _event("active"))
    moved = _tomorrow_body()
    left = await service.put_entry(USER, moved)
    assert (left["result"], left["previous_meeting_id"]) == (
        "created",
        reply["meeting"]["id"],
    )
    return mid, moved, left


async def _finish_by_callback(pg: _Pg, mid: int) -> None:
    await _callback(
        pg.repo, _event("completed", completion_reason="stopped", exit_code=0)
    )


async def _finish_by_runtime_destroy(pg: _Pg, mid: int) -> None:
    from meeting_api import create_app
    from meeting_api.lifecycle.machine import TransitionSource

    app = create_app(meeting_repo=pg.repo)
    await app.state.apply_lifecycle_event(
        {
            "connection_id": "sess-cb",
            "status": "completed",
            "completion_reason": "stopped",
        },
        transition_source=TransitionSource.RUNTIME_DESTROY,
        force_terminal_on_destroy=True,
    )


async def _finish_by_fail_meeting(pg: _Pg, mid: int) -> None:
    from meeting_api.intake.status import Outcome

    await pg.repo.fail_meeting(
        meeting_id=mid,
        reason="bot workload failed to start",
        outcome=Outcome("not_sent", "spawn_error", "bot workload failed to start"),
    )


async def _history_holds(pg: _Pg, live: int, new_uuid: str) -> None:
    """The live meeting keeps the entry as closed history, its terminal event names the owner,
    the owner still sees it; the new meeting keeps the entry active."""
    from meeting_api.intake.ports import MeetingQuery
    from meeting_api.intake.reads import PostgresIntakeReads

    from intake_builders import A

    rows = (
        await pg.exec(
            "SELECT meeting_id, state FROM meeting_entries ORDER BY id",
        )
    ).all()
    new = int(
        await pg.scalar(
            "SELECT id FROM meetings WHERE uuid = CAST(:u AS uuid)", u=new_uuid
        )
    )
    assert [tuple(r) for r in rows] == [(live, "closed"), (new, "active")]
    terminal = (await pg.events(live))[-1]["payload"]["data"]["meeting"]
    assert [e["user"] for e in terminal["entries"]] == [A]
    reads = PostgresIntakeReads(pg.session_factory)
    seen = {m.uuid for m in await reads.meetings(USER, MeetingQuery(user=A))}
    assert seen == {await _uuid(pg, live), new_uuid}
    assert await reads.visible_to(USER, live, A)
    assert await pg.status(new) == "scheduled"
    assert [e["event_type"] for e in await pg.events(new)] == ["meeting.scheduled"]


@pytest.mark.parametrize(
    "finish, reach_the_call, finished, terminal",
    [
        (_finish_by_callback, True, "completed", "meeting.completed"),
        (_finish_by_runtime_destroy, True, "completed", "meeting.completed"),
        (_finish_by_fail_meeting, False, "failed", "meeting.not_sent"),
    ],
    ids=["lifecycle_callback", "runtime_destroy", "fail_meeting"],
)
async def test_pg_a_move_while_live_survives_every_finishing_writer(
    pg, finish, reach_the_call, finished, terminal
):
    """R7 end to end: the entry moved while the bot was in the call and got its new meeting at
    once; the live meeting then finishes through a real writer. Its terminal event lists the
    entry, the owner still sees it, the new meeting is untouched, and a later PUT with the same
    content is ``unchanged`` there."""
    _, service = _intake(pg)
    mid, moved, left = await _moved_while_live(
        pg, service, reach_the_call=reach_the_call
    )
    await finish(pg, mid)
    assert await pg.status(mid) == finished
    assert (await pg.events(mid))[-1]["event_type"] == terminal
    await _history_holds(pg, mid, left["meeting"]["id"])

    again = await service.put_entry(USER, moved)
    assert (again["result"], again["meeting"]["id"]) == (
        "unchanged",
        left["meeting"]["id"],
    )


class _MoveThenFail:
    """A runtime whose workload start fails after the claim, having first let ``during`` run (a
    calendar update arriving while the claimed bot is starting)."""

    def __init__(self, during: Any) -> None:
        self.during = during
        self.specs: list[dict] = []

    async def create_workload(self, spec: dict) -> dict:
        from meeting_api.bot_spawn.ports import SpawnFailed

        self.specs.append(spec)
        await self.during()
        raise SpawnFailed("kernel could not start the workload")

    async def delete_workload(self, workload_id: str) -> None:
        return None

    async def get_workload(self, workload_id: str) -> Any:
        return None


async def test_pg_a_move_while_a_join_now_bot_starts_survives_its_failure(pg, monkeypatch):
    """The join_now writer path: a pasted link adopts the calendar meeting under way and claims
    it; while its bot starts, the calendar entry moves to tomorrow (it leaves at once, R7); then
    the workload fails, which ends the meeting ``not_sent`` (one send allowed: §6.9 F-K2 retries a
    failure while there are sends left). The new meeting is untouched and the failed one keeps
    the calendar entry as history."""
    from datetime import timedelta

    monkeypatch.setenv("BOT_SEND_MAX_ATTEMPTS", "1")

    from intake_builders import GMEET, entry_body, instant_body

    from meeting_api.intake import ExactRowSpawn, PostgresIntakeStore
    from meeting_api.intake.sweeps import OutboxOnly

    moved = _tomorrow_body()
    replies: dict[str, dict] = {}

    async def the_move() -> None:
        replies["moved"] = await service.put_entry(USER, moved)

    async def ctx(_user_id: int) -> dict:
        return {"max_concurrent": 45}

    store = PostgresIntakeStore(pg.session_factory)
    spawn = ExactRowSpawn(
        pg.repo,
        _MoveThenFail(the_move),
        store=store,
        fetch_bot_context=ctx,
        publisher=OutboxOnly(),
        token_secret="s",
        redis_url="redis://r",
    )
    _, service = _intake(pg, spawn)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    calendar = await service.put_entry(
        USER,
        entry_body(
            start=_iso(now - timedelta(minutes=5)),
            end=_iso(now + timedelta(minutes=55)),
        ),
    )
    pasted = await service.put_entry(USER, instant_body("paste:1", GMEET))
    live_uuid = calendar["meeting"]["id"]
    assert pasted["meeting"]["id"] == live_uuid
    assert (replies["moved"]["result"], replies["moved"]["previous_meeting_id"]) == (
        "created",
        live_uuid,
    )
    live = int(
        await pg.scalar(
            "SELECT id FROM meetings WHERE uuid = CAST(:u AS uuid)", u=live_uuid
        )
    )
    assert await pg.status(live) == "failed"
    assert (await pg.events(live))[-1]["event_type"] == "meeting.not_sent"
    listed = (await pg.events(live))[-1]["payload"]["data"]["meeting"]["entries"]
    assert sorted(e["external_id"] for e in listed) == [
        "google:3n5kq8example",
        "paste:1",
    ]
    new = int(
        await pg.scalar(
            "SELECT id FROM meetings WHERE uuid = CAST(:u AS uuid)",
            u=replies["moved"]["meeting"]["id"],
        )
    )
    assert await pg.status(new) == "scheduled"
    state = await pg.scalar(
        "SELECT state FROM meeting_entries WHERE meeting_id = :m", m=new
    )
    assert state == "active"


class _NoSpawnPort:
    async def spawn_exact(self, user_id: int, meeting_id: int) -> Any:
        raise AssertionError("no spawn in this test")


# ── Postgres: the legacy system URL ─────────────────────────────────────────────────────────


class _SystemCapture:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def deliver(self, envelope: dict, *, label: str = "") -> None:
        self.calls.append(envelope)


async def test_pg_the_system_url_still_receives_meeting_completed(pg):
    """Upstream's system hook keeps working with upstream's meeting block; the §2.4 meeting's
    own keys go to ``/v2/webhooks`` subscribers only (§6.9 F-X)."""
    mid = await pg.seed("requested", session_uid="sess-cb")
    sink = _SystemCapture()
    await _callback(
        pg.repo,
        _event("joining"),
        _event("active"),
        _event("completed", completion_reason="stopped", exit_code=0),
        sink=sink,
    )
    assert [e["event_type"] for e in sink.calls] == ["meeting.completed"]
    meeting = sink.calls[0]["data"]["meeting"]
    assert meeting["id"] == mid
    assert meeting["status"] == "completed"
    assert meeting["completion_reason"] == "stopped"
    assert meeting["start_time"] is not None and meeting["end_time"] is not None
    assert not {"uuid", "entries", "outcome", "sequence"} & set(meeting)
    assert await pg.seq(mid) == 3  # the subscription events are still written


# ── Postgres: aw_meetings_failed_total, once per failed bot (§6.9 F-B) ──────────────────────


def _failed_count(reason: str) -> float:
    from meeting_api.metrics import registry

    value = registry().get_sample_value(
        "aw_meetings_failed_total", {"reason": reason, "user_id": str(USER)}
    )
    return value or 0.0


def _failed_total() -> float:
    from meeting_api.metrics import registry

    return sum(
        s.value
        for f in registry().collect()
        for s in f.samples
        if s.name == "aw_meetings_failed_total"
    )


async def test_pg_a_bot_failure_through_the_callback_counts_once(pg):
    mid = await pg.seed("requested", session_uid="sess-cb")
    before = _failed_count("join_failure")
    failed = _event("failed", completion_reason="join_failure", exit_code=1)
    await _callback(pg.repo, _event("joining"), failed, failed)  # the bot's retry
    assert await pg.status(mid) == "failed"
    assert _failed_count("join_failure") == before + 1


async def test_pg_a_bot_failure_through_the_runtime_destroy_counts_once(pg):
    from meeting_api import create_app
    from meeting_api.lifecycle.machine import TransitionSource

    mid = await pg.seed("active", session_uid="sess-cb")
    before = _failed_count("evicted")
    app = create_app(meeting_repo=pg.repo)
    for _ in range(2):
        await app.state.apply_lifecycle_event(
            {
                "connection_id": "sess-cb",
                "status": "failed",
                "completion_reason": "evicted",
            },
            transition_source=TransitionSource.RUNTIME_DESTROY,
            force_terminal_on_destroy=True,
        )
    assert await pg.status(mid) == "failed"
    assert _failed_count("evicted") == before + 1


async def test_pg_fail_meeting_counts_once_by_its_completion_reason(pg):
    mid = await pg.seed("requested")
    before = _failed_count("start_failed")
    for _ in range(2):
        await pg.repo.fail_meeting(meeting_id=mid, reason="workload dead on arrival")
    assert await pg.status(mid) == "failed"
    assert _failed_count("start_failed") == before + 1


async def test_pg_a_not_sent_failure_keeps_its_own_counter(pg):
    from meeting_api.intake.status import Outcome
    from meeting_api.metrics import registry

    mid = await pg.seed("requested")
    failed_before = _failed_total()
    not_sent_before = (
        registry().get_sample_value(
            "aw_meetings_not_sent_total",
            {"detail": "spawn_error", "user_id": str(USER)},
        )
        or 0.0
    )
    await pg.repo.fail_meeting(
        meeting_id=mid,
        reason="bot workload failed to start",
        outcome=Outcome("not_sent", "spawn_error", "bot workload failed to start"),
    )
    assert _failed_total() == failed_before
    assert (
        registry().get_sample_value(
            "aw_meetings_not_sent_total",
            {"detail": "spawn_error", "user_id": str(USER)},
        )
        == not_sent_before + 1
    )


async def test_pg_a_planned_meeting_that_fails_is_not_a_failed_bot(pg):
    """R8: a scheduled meeting that ends ``failed`` never had a bot."""
    from meeting_api.intake.status import write_status

    mid = await pg.seed("scheduled")
    before = _failed_total()
    async with pg.session_factory() as db, db.begin():
        await write_status(
            db,
            mid,
            "failed",
            expected_from={"scheduled"},
            data_patch={"completion_reason": "stopped"},
        )
    assert _failed_total() == before


async def test_pg_a_failed_write_that_rolls_back_and_is_retried_counts_once(pg):
    """Counted after the commit: the rolled-back first try counts nothing."""
    from meeting_api.intake.status import write_status

    class _Abort(Exception):
        pass

    mid = await pg.seed("active")
    before = _failed_count("evicted")
    for attempt in range(2):
        try:
            async with pg.session_factory() as db, db.begin():
                await write_status(
                    db,
                    mid,
                    "failed",
                    expected_from={"active"},
                    data_patch={"completion_reason": "evicted"},
                )
                if attempt == 0:
                    raise _Abort
        except _Abort:
            assert _failed_count("evicted") == before
    assert await pg.status(mid) == "failed"
    assert _failed_count("evicted") == before + 1
