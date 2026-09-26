"""§1.4 — the single status writer and the outbox (`meeting_api.intake.status`).

Three layers:
  * pure — the event id and the half-open overlap rule; no SQLAlchemy needed.
  * recording fake session — drives `write_status` / `write_event` against real ORM instances and a
    fake `AsyncSession` that records every lock, add and flush, so the step order (row lock → status
    → aw-state lock → entries → outbox) and the "conflict writes nothing" rule are checked call by
    call. Needs SQLAlchemy for the model classes (the `orm` fixture skips cleanly without it).
  * real Postgres — the same rules against a real transaction, plus a blocking proof of the lock
    order. Skips cleanly unless `MEETING_API_TEST_DATABASE_URL` is set; see
    `tests/test_intake_pg_schema.py`'s docstring for the ephemeral SQLAlchemy/asyncpg install.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid as uuid_mod
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from referencing import Registry, Resource

from meeting_api.intake import (
    Outcome,
    StatusConflict,
    derive_event_id_v2,
    project_meeting,
    row_mapping,
    write_event,
    write_status,
)
from meeting_api.intake.status import _overlaps
from meeting_api.lifecycle.webhook import derive_event_id

UTC = timezone.utc


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _canonical(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), sort_keys=True)


def _webhook_schema_conforms(envelope: dict) -> None:
    for parent in Path(__file__).resolve().parents:
        path = parent / "meetings" / "contracts" / "webhook.v1" / "webhook.schema.json"
        if path.is_file():
            break
    schema = json.loads(path.read_text())
    registry = Registry().with_resource(schema["$id"], Resource.from_contents(schema))
    jsonschema.Draft202012Validator(
        {"$ref": f"{schema['$id']}#/$defs/Envelope"}, registry=registry
    ).validate(envelope)


# ── pure ─────────────────────────────────────────────────────────────────────────────────────


def test_event_id_v2_is_evt_plus_the_full_sha256():
    u = "5f0c2b7e-8d1a-4c3e-9b6f-2a7d1e4c8b90"
    expected = hashlib.sha256(f"{u}|meeting.status_change|7".encode()).hexdigest()
    got = derive_event_id_v2(u, "meeting.status_change", 7)
    assert got == "evt_" + expected
    assert len(got) == 4 + 64


def test_event_id_v2_changes_with_each_part():
    u = "5f0c2b7e-8d1a-4c3e-9b6f-2a7d1e4c8b90"
    base = derive_event_id_v2(u, "meeting.updated", 1)
    assert base != derive_event_id_v2(u, "meeting.updated", 2)
    assert base != derive_event_id_v2(u, "meeting.removed", 1)
    assert base != derive_event_id_v2(str(uuid_mod.uuid4()), "meeting.updated", 1)


def test_legacy_event_id_is_unchanged():
    """The system-URL id stays the 32-hex (connection_id, event_type, new_status) key."""
    key = "conn-1|meeting.status_change|active"
    assert derive_event_id("conn-1", "meeting.status_change", "active") == (
        "evt_" + hashlib.sha256(key.encode()).hexdigest()[:32]
    )


def test_overlap_is_half_open():
    t = datetime(2026, 9, 29, 15, 0, tzinfo=UTC)
    h = timedelta(hours=1)
    assert _overlaps(t, t + h, t + h / 2, t + 2 * h)
    assert not _overlaps(t, t + h, t + h, t + 2 * h)  # back-to-back
    assert not _overlaps(t + h, t + 2 * h, t, t + h)
    assert _overlaps(t, t + 3 * h, t + h, t + 2 * h)  # contained


# ── recording fake session ───────────────────────────────────────────────────────────────────


@pytest.fixture
def orm():
    pytest.importorskip("sqlalchemy", reason="the ORM models need SQLAlchemy")
    from meeting_api.sessions import models

    return models


class _Result:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def scalars(self) -> "_Result":
        return self

    def all(self) -> list:
        return list(self._rows)

    def __iter__(self):
        return iter(self._rows)


class RecordingSession:
    """A fake `AsyncSession` over in-memory ORM instances. `calls` records, in order, every row
    lock (`get(..., with_for_update=True)`), entries read, add and flush."""

    def __init__(self, orm, meeting=None, aw=None, entries=()) -> None:
        self._orm = orm
        self.rows: dict[tuple[str, Any], Any] = {}
        if meeting is not None:
            self.rows[("meetings", meeting.id)] = meeting
        if aw is not None:
            self.rows[("meeting_aw_state", aw.meeting_id)] = aw
        self.entries = list(entries)
        self.added: list = []
        self.calls: list[tuple] = []

    async def get(self, cls, ident, *, with_for_update=None, populate_existing=False):
        table = cls.__tablename__
        self.calls.append(("lock" if with_for_update else "get", table, ident))
        return self.rows.get((table, ident))

    async def execute(self, stmt):
        entity = stmt.column_descriptions[0]["entity"]
        assert entity is self._orm.MeetingEntry, f"unexpected statement: {stmt}"
        meeting_id = next(iter(stmt.compile().params.values()))
        self.calls.append(("entries", meeting_id))
        return _Result(
            sorted(
                (e for e in self.entries if e.meeting_id == meeting_id),
                key=lambda e: e.id,
            )
        )

    def add(self, obj) -> None:
        table = obj.__tablename__
        self.calls.append(("add", table))
        self.added.append(obj)
        if table == "meeting_aw_state":
            self.rows[(table, obj.meeting_id)] = obj

    async def flush(self) -> None:
        self.calls.append(("flush",))

    def outbox(self) -> list:
        return [o for o in self.added if o.__tablename__ == "webhook_outbox"]


def _meeting(orm, *, status="active", scheduled_at=None, data=None, meeting_id=11):
    d = dict(data or {})
    if scheduled_at is not None:
        d["scheduled_at"] = _iso(scheduled_at)
    return orm.Meeting(
        id=meeting_id,
        uuid=uuid_mod.UUID("5f0c2b7e-8d1a-4c3e-9b6f-2a7d1e4c8b90"),
        user_id=7,
        platform="google_meet",
        platform_specific_id="kxo-misr-avz",
        status=status,
        data=d,
        start_time=None,
        created_at=datetime(2026, 9, 1, 0, 0),
    )


def _aw(orm, *, meeting_id=11, event_seq=0, scheduled_end_at=None):
    return orm.MeetingAwState(
        meeting_id=meeting_id, event_seq=event_seq, scheduled_end_at=scheduled_end_at
    )


def _entry(orm, entry_id, *, start, end, state="active", meeting_id=11):
    return orm.MeetingEntry(
        id=entry_id,
        user_id=7,
        source_user="a@abroadworks.com",
        external_id=f"google:{entry_id}",
        meeting_id=meeting_id,
        meeting_url="https://meet.google.com/kxo-misr-avz",
        platform="google_meet",
        native_meeting_id="kxo-misr-avz",
        start_at=start,
        end_at=end,
        content_hash="h" * 64,
        state=state,
        metadata_={"k": entry_id},
    )


async def test_fake_conflict_writes_nothing(orm):
    meeting = _meeting(orm, status="active")
    aw = _aw(orm, event_seq=3)
    db = RecordingSession(orm, meeting, aw)

    with pytest.raises(StatusConflict) as exc:
        await write_status(db, 11, "requested", expected_from={"scheduled"})

    assert exc.value.meeting_id == 11
    assert exc.value.actual == "active"
    assert db.calls == [("lock", "meetings", 11)]
    assert db.added == []
    assert meeting.status == "active"
    assert aw.event_seq == 3


async def test_fake_missing_meeting_is_a_conflict(orm):
    db = RecordingSession(orm)
    with pytest.raises(StatusConflict) as exc:
        await write_status(db, 11, "requested", expected_from={"scheduled"})
    assert exc.value.actual is None
    assert db.added == []


async def test_fake_lock_order_is_meeting_row_then_aw_state(orm):
    db = RecordingSession(orm, _meeting(orm, status="scheduled"), _aw(orm))
    await write_status(db, 11, "requested", expected_from={"scheduled"})
    locks = [c for c in db.calls if c[0] == "lock"]
    assert locks == [("lock", "meetings", 11), ("lock", "meeting_aw_state", 11)]
    assert db.calls[0] == ("lock", "meetings", 11)
    # the outbox row is written after both locks and the entries read
    assert db.calls.index(("add", "webhook_outbox")) > db.calls.index(("entries", 11))


async def test_fake_sequence_goes_up_by_exactly_one(orm):
    meeting = _meeting(orm, status="scheduled")
    aw = _aw(orm, event_seq=4)
    db = RecordingSession(orm, meeting, aw)

    first = await write_status(db, 11, "requested", expected_from={"scheduled"})
    second = await write_status(db, 11, "joining", expected_from={"requested"})

    assert (first.sequence, second.sequence) == (5, 6)
    assert aw.event_seq == 6
    assert [o.sequence for o in db.outbox()] == [5, 6]


async def test_fake_missing_aw_state_is_created_then_locked(orm):
    db = RecordingSession(orm, _meeting(orm, status="scheduled"))
    written = await write_status(db, 11, "requested", expected_from={"scheduled"})
    assert written.sequence == 1
    assert db.calls[:3] == [
        ("lock", "meetings", 11),
        ("lock", "meeting_aw_state", 11),
        ("add", "meeting_aw_state"),
    ]
    assert db.rows[("meeting_aw_state", 11)].event_seq == 1


async def test_fake_status_data_patch_and_outcome_are_written(orm):
    meeting = _meeting(orm, status="scheduled", data={"title": "Weekly sync"})
    aw = _aw(orm)
    db = RecordingSession(orm, meeting, aw)

    await write_status(
        db,
        11,
        "failed",
        expected_from={"scheduled"},
        data_patch={"completion_reason": "stopped"},
        outcome=Outcome(kind="not_sent", detail="room_busy", message="Room busy."),
        change_reason="room_busy",
    )

    assert meeting.status == "failed"
    assert meeting.data == {"title": "Weekly sync", "completion_reason": "stopped"}
    assert (aw.outcome_kind, aw.outcome_detail, aw.outcome_message) == (
        "not_sent",
        "room_busy",
        "Room busy.",
    )
    assert aw.outcome_at is not None


async def test_fake_terminal_write_closes_active_entries(orm):
    now = _now()
    meeting = _meeting(orm, status="active", scheduled_at=now - timedelta(hours=1))
    aw = _aw(orm, scheduled_end_at=now - timedelta(minutes=5))
    past = _entry(
        orm, 1, start=now - timedelta(hours=1), end=now - timedelta(minutes=5)
    )
    removed = _entry(orm, 2, start=now - timedelta(hours=1), end=now, state="removed")
    db = RecordingSession(orm, meeting, aw, [past, removed])

    written = await write_status(db, 11, "completed", expected_from={"active"})

    assert written.rerun_entry_ids == ()
    assert past.state == "closed" and past.closed_at is not None
    assert removed.state == "removed" and removed.closed_at is None
    envelope = json.loads(db.outbox()[0].payload_text)
    assert envelope["data"]["meeting"]["entries"] == []


async def test_fake_future_non_overlapping_entry_is_returned_for_rerun(orm):
    now = _now()
    meeting = _meeting(orm, status="active", scheduled_at=now - timedelta(hours=1))
    aw = _aw(orm, scheduled_end_at=now + timedelta(minutes=30))
    current = _entry(
        orm, 1, start=now - timedelta(hours=1), end=now + timedelta(minutes=30)
    )
    # future but inside the finished meeting's window → closed
    future_overlap = _entry(
        orm, 2, start=now + timedelta(minutes=10), end=now + timedelta(minutes=40)
    )
    # future and starting exactly at the window's end (half-open) → re-run
    future_after = _entry(
        orm, 3, start=now + timedelta(minutes=30), end=now + timedelta(hours=1)
    )
    db = RecordingSession(orm, meeting, aw, [current, future_overlap, future_after])

    written = await write_status(db, 11, "completed", expected_from={"active"})

    assert written.rerun_entry_ids == (3,)
    assert (current.state, future_overlap.state, future_after.state) == (
        "closed",
        "closed",
        "active",
    )
    assert future_after.closed_at is None


async def test_fake_open_ended_meeting_window_ends_at_the_finish(orm):
    """No `scheduled_end_at` (a `join_now` meeting): the window is [scheduled_at, finish), so any
    entry that starts after the finish comes back, and one with no end of its own too.
    """
    now = _now()
    meeting = _meeting(orm, status="active", scheduled_at=now - timedelta(hours=1))
    aw = _aw(orm, scheduled_end_at=None)
    started = _entry(orm, 1, start=now - timedelta(hours=1), end=None)
    later = _entry(
        orm, 2, start=now + timedelta(minutes=1), end=now + timedelta(hours=1)
    )
    later_open = _entry(orm, 3, start=now + timedelta(hours=2), end=None)
    db = RecordingSession(orm, meeting, aw, [started, later, later_open])

    written = await write_status(db, 11, "failed", expected_from={"active"})

    assert written.rerun_entry_ids == (2, 3)
    assert started.state == "closed"


async def test_fake_non_terminal_write_leaves_entries_alone(orm):
    now = _now()
    meeting = _meeting(orm, status="scheduled", scheduled_at=now - timedelta(hours=1))
    past = _entry(
        orm, 1, start=now - timedelta(hours=1), end=now - timedelta(minutes=5)
    )
    db = RecordingSession(orm, meeting, _aw(orm), [past])

    written = await write_status(db, 11, "requested", expected_from={"scheduled"})

    assert written.rerun_entry_ids == ()
    assert past.state == "active"


async def test_fake_payload_is_the_canonical_envelope(orm, monkeypatch):
    monkeypatch.setenv("AUTO_JOIN_LEAD_S", "300")
    now = _now()
    meeting = _meeting(
        orm,
        status="awaiting_admission",
        scheduled_at=now + timedelta(hours=1),
        data={"title": "Weekly sync"},
    )
    aw = _aw(orm, event_seq=6)
    entry = _entry(orm, 1, start=now + timedelta(hours=1), end=now + timedelta(hours=2))
    db = RecordingSession(orm, meeting, aw, [entry])

    written = await write_status(db, 11, "active", expected_from={"awaiting_admission"})

    (row,) = db.outbox()
    text = row.payload_text
    envelope = json.loads(text)
    assert _canonical(envelope) == text  # the stored text round-trips byte for byte
    assert row.event_id == written.event_id == envelope["event_id"]
    assert row.event_type == envelope["event_type"] == "meeting.status_change"
    assert row.sequence == written.sequence == 7
    assert row.meeting_id == 11
    assert written.event_id == derive_event_id_v2(
        str(meeting.uuid), "meeting.status_change", 7
    )
    assert envelope["api_version"] == "2026-09-25"
    change = envelope["data"]["change"]
    assert change["from"] == "awaiting_admission"
    assert change["to"] == "active"
    assert change["reason"] is None
    assert change["at"] == envelope["created_at"]
    assert envelope["data"]["meeting"] == project_meeting(
        row_mapping(meeting), row_mapping(aw), [row_mapping(entry)], lead_s=300
    )
    assert envelope["data"]["meeting"]["sequence"] == 7
    assert envelope["data"]["meeting"]["entries"][0]["metadata"] == {"k": 1}
    _webhook_schema_conforms(envelope)


async def test_fake_event_type_override(orm):
    db = RecordingSession(orm, _meeting(orm, status="active"), _aw(orm))
    written = await write_status(
        db,
        11,
        "completed",
        expected_from={"active"},
        change_reason="stopped",
        event_type="meeting.completed",
    )
    (row,) = db.outbox()
    envelope = json.loads(row.payload_text)
    assert row.event_type == envelope["event_type"] == "meeting.completed"
    assert envelope["data"]["change"]["reason"] == "stopped"
    assert written.event_id == derive_event_id_v2(
        "5f0c2b7e-8d1a-4c3e-9b6f-2a7d1e4c8b90", "meeting.completed", 1
    )


async def test_fake_write_event_without_change(orm):
    meeting = _meeting(orm, status="scheduled")
    aw = _aw(orm, event_seq=2)
    db = RecordingSession(orm, meeting, aw)

    written = await write_event(db, 11, "meeting.waiting_for_room")

    assert meeting.status == "scheduled"
    assert written.sequence == 3 and written.rerun_entry_ids == ()
    (row,) = db.outbox()
    envelope = json.loads(row.payload_text)
    assert "change" not in envelope["data"]
    assert envelope["event_type"] == "meeting.waiting_for_room"
    assert [c for c in db.calls if c[0] == "lock"] == [
        ("lock", "meetings", 11),
        ("lock", "meeting_aw_state", 11),
    ]
    _webhook_schema_conforms(envelope)


async def test_fake_write_event_with_change(orm):
    db = RecordingSession(orm, _meeting(orm, status="scheduled"), _aw(orm))
    await write_event(db, 11, "meeting.updated", {"fields": ["title"]})
    envelope = json.loads(db.outbox()[0].payload_text)
    assert envelope["data"]["change"] == {"fields": ["title"]}


async def test_fake_write_event_missing_meeting(orm):
    db = RecordingSession(orm)
    with pytest.raises(LookupError):
        await write_event(db, 11, "meeting.updated")
    assert db.added == []


# ── real Postgres ────────────────────────────────────────────────────────────────────────────

pg = pytest.mark.skipif(
    not os.getenv("MEETING_API_TEST_DATABASE_URL"),
    reason="real-Postgres proofs for §1.4; set MEETING_API_TEST_DATABASE_URL to run",
)


@pytest.fixture
async def pg_schema(intake_pg_engine):
    pytest.importorskip("asyncpg", reason="see test_intake_pg_schema.py's docstring")
    from admin_api.schema import models as admin_models
    from admin_api.schema import sync as admin_sync

    await admin_sync.ensure_schema(intake_pg_engine, admin_models.Base)
    return intake_pg_engine


def _sessions(engine):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    return async_sessionmaker(engine, expire_on_commit=False)


async def _seed_meeting(
    engine, *, status: str, scheduled_at: datetime, scheduled_end_at=None
) -> int:
    from sqlalchemy import text

    async with engine.begin() as conn:
        meeting_id = (
            await conn.execute(
                text(
                    "INSERT INTO meetings (user_id, platform, platform_specific_id, status, data) "
                    "VALUES (7, 'google_meet', 'kxo-misr-avz', :status, CAST(:data AS jsonb)) "
                    "RETURNING id"
                ),
                {
                    "status": status,
                    "data": json.dumps(
                        {"scheduled_at": _iso(scheduled_at), "title": "Weekly sync"}
                    ),
                },
            )
        ).scalar_one()
        if scheduled_end_at is not None:
            await conn.execute(
                text(
                    "INSERT INTO meeting_aw_state (meeting_id, scheduled_end_at) "
                    "VALUES (:id, :end)"
                ),
                {"id": meeting_id, "end": scheduled_end_at},
            )
    return meeting_id


async def _seed_entry(engine, meeting_id: int, external_id: str, start, end) -> int:
    from sqlalchemy import text

    async with engine.begin() as conn:
        return (
            await conn.execute(
                text(
                    "INSERT INTO meeting_entries (user_id, source_user, external_id, meeting_id, "
                    "meeting_url, platform, native_meeting_id, start_at, end_at, content_hash, "
                    "state) VALUES (7, 'a@abroadworks.com', :ext, :mid, "
                    "'https://meet.google.com/kxo-misr-avz', 'google_meet', 'kxo-misr-avz', "
                    ":start, :end, 'hash', 'active') RETURNING id"
                ),
                {"ext": external_id, "mid": meeting_id, "start": start, "end": end},
            )
        ).scalar_one()


async def _snapshot(engine, meeting_id: int) -> dict:
    from sqlalchemy import text

    async with engine.begin() as conn:
        status = (
            await conn.execute(
                text("SELECT status FROM meetings WHERE id = :id"), {"id": meeting_id}
            )
        ).scalar_one()
        seq = (
            await conn.execute(
                text("SELECT event_seq FROM meeting_aw_state WHERE meeting_id = :id"),
                {"id": meeting_id},
            )
        ).scalar_one_or_none()
        entries = dict(
            (
                await conn.execute(
                    text(
                        "SELECT id, state FROM meeting_entries WHERE meeting_id = :id"
                    ),
                    {"id": meeting_id},
                )
            ).all()
        )
        outbox = (
            await conn.execute(
                text(
                    "SELECT event_id, event_type, sequence, payload_text FROM webhook_outbox "
                    "WHERE meeting_id = :id ORDER BY sequence"
                ),
                {"id": meeting_id},
            )
        ).all()
    return {"status": status, "seq": seq, "entries": entries, "outbox": outbox}


@pg
async def test_pg_conflict_writes_nothing(pg_schema):
    meeting_id = await _seed_meeting(pg_schema, status="active", scheduled_at=_now())
    before = await _snapshot(pg_schema, meeting_id)

    with pytest.raises(StatusConflict):
        async with _sessions(pg_schema)() as db, db.begin():
            await write_status(db, meeting_id, "requested", expected_from={"scheduled"})

    after = await _snapshot(pg_schema, meeting_id)
    assert after == before
    assert after["seq"] is None  # not even the aw-state row was created
    assert after["outbox"] == []


@pg
async def test_pg_sequence_goes_up_by_exactly_one(pg_schema):
    meeting_id = await _seed_meeting(pg_schema, status="scheduled", scheduled_at=_now())
    sf = _sessions(pg_schema)
    async with sf() as db, db.begin():
        first = await write_status(
            db, meeting_id, "requested", expected_from={"scheduled"}
        )
    async with sf() as db, db.begin():
        second = await write_event(db, meeting_id, "meeting.updated")

    snap = await _snapshot(pg_schema, meeting_id)
    assert (first.sequence, second.sequence) == (1, 2)
    assert snap["seq"] == 2
    assert snap["status"] == "requested"
    assert [(r.event_id, r.event_type, r.sequence) for r in snap["outbox"]] == [
        (first.event_id, "meeting.status_change", 1),
        (second.event_id, "meeting.updated", 2),
    ]


@pg
async def test_pg_terminal_write_closes_entries_in_the_same_transaction(pg_schema):
    now = _now()
    meeting_id = await _seed_meeting(
        pg_schema,
        status="active",
        scheduled_at=now - timedelta(hours=1),
        scheduled_end_at=now - timedelta(minutes=5),
    )
    e1 = await _seed_entry(
        pg_schema,
        meeting_id,
        "g:1",
        now - timedelta(hours=1),
        now - timedelta(minutes=5),
    )
    e2 = await _seed_entry(
        pg_schema,
        meeting_id,
        "g:2",
        now - timedelta(hours=1),
        now - timedelta(minutes=5),
    )
    before = await _snapshot(pg_schema, meeting_id)

    class _Abort(Exception):
        pass

    with pytest.raises(_Abort):
        async with _sessions(pg_schema)() as db, db.begin():
            await write_status(db, meeting_id, "completed", expected_from={"active"})
            raise _Abort

    assert await _snapshot(pg_schema, meeting_id) == before  # rollback leaves nothing

    async with _sessions(pg_schema)() as db, db.begin():
        written = await write_status(
            db, meeting_id, "completed", expected_from={"active"}
        )

    snap = await _snapshot(pg_schema, meeting_id)
    assert written.rerun_entry_ids == ()
    assert snap["status"] == "completed"
    assert snap["entries"] == {e1: "closed", e2: "closed"}
    assert [r.sequence for r in snap["outbox"]] == [1]


@pg
async def test_pg_future_non_overlapping_entry_is_rerun_not_closed(pg_schema):
    from sqlalchemy import text

    now = _now()
    meeting_id = await _seed_meeting(
        pg_schema,
        status="active",
        scheduled_at=now - timedelta(hours=1),
        scheduled_end_at=now + timedelta(minutes=30),
    )
    current = await _seed_entry(
        pg_schema,
        meeting_id,
        "g:1",
        now - timedelta(hours=1),
        now + timedelta(minutes=30),
    )
    moved = await _seed_entry(
        pg_schema, meeting_id, "g:2", now + timedelta(hours=3), now + timedelta(hours=4)
    )

    async with _sessions(pg_schema)() as db, db.begin():
        written = await write_status(
            db, meeting_id, "completed", expected_from={"active"}
        )

    assert written.rerun_entry_ids == (moved,)
    snap = await _snapshot(pg_schema, meeting_id)
    assert snap["entries"] == {current: "closed", moved: "active"}
    async with pg_schema.begin() as conn:
        closed_at = dict(
            (
                await conn.execute(
                    text(
                        "SELECT id, closed_at FROM meeting_entries WHERE meeting_id = :id"
                    ),
                    {"id": meeting_id},
                )
            ).all()
        )
    assert closed_at[current] is not None and closed_at[moved] is None


@pg
async def test_pg_payload_round_trips_byte_for_byte(pg_schema):
    from sqlalchemy import text

    meeting_id = await _seed_meeting(
        pg_schema, status="scheduled", scheduled_at=_now() + timedelta(hours=1)
    )
    await _seed_entry(
        pg_schema,
        meeting_id,
        "g:1",
        _now() + timedelta(hours=1),
        _now() + timedelta(hours=2),
    )
    async with _sessions(pg_schema)() as db, db.begin():
        written = await write_status(
            db,
            meeting_id,
            "failed",
            expected_from={"scheduled"},
            outcome=Outcome(kind="not_sent", detail="room_busy", message="Ünïcode ✓"),
        )

    (row,) = (await _snapshot(pg_schema, meeting_id))["outbox"]
    envelope = json.loads(row.payload_text)
    assert _canonical(envelope) == row.payload_text
    assert envelope["event_id"] == row.event_id == written.event_id
    async with pg_schema.begin() as conn:
        meeting_uuid = (
            await conn.execute(
                text("SELECT uuid FROM meetings WHERE id = :id"), {"id": meeting_id}
            )
        ).scalar_one()
    assert envelope["data"]["meeting"]["id"] == str(meeting_uuid)
    assert written.event_id == derive_event_id_v2(
        str(meeting_uuid), "meeting.status_change", 1
    )
    assert envelope["data"]["meeting"]["outcome"]["message"] == "Ünïcode ✓"
    _webhook_schema_conforms(envelope)


@pg
async def test_pg_statement_order_locks_meeting_row_then_aw_state(pg_schema):
    from sqlalchemy import event

    meeting_id = await _seed_meeting(
        pg_schema, status="scheduled", scheduled_at=_now(), scheduled_end_at=_now()
    )
    statements: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        statements.append(" ".join(statement.split()))

    event.listen(pg_schema.sync_engine, "before_cursor_execute", _record)
    try:
        async with _sessions(pg_schema)() as db, db.begin():
            await write_status(db, meeting_id, "requested", expected_from={"scheduled"})
    finally:
        event.remove(pg_schema.sync_engine, "before_cursor_execute", _record)

    locks = [s for s in statements if s.endswith("FOR UPDATE")]
    assert len(locks) == 2, statements
    assert "FROM meetings " in locks[0]
    assert "FROM meeting_aw_state " in locks[1]
    assert statements.index(locks[0]) < statements.index(locks[1])


@pg
async def test_pg_aw_state_is_not_locked_while_the_meeting_row_is_contended(pg_schema):
    """Another transaction holds the meeting row: `write_status` blocks on it WITHOUT having
    touched `meeting_aw_state` (a third transaction can still take that row NOWAIT), and it
    completes once the row is released."""
    from sqlalchemy import text

    meeting_id = await _seed_meeting(
        pg_schema, status="scheduled", scheduled_at=_now(), scheduled_end_at=_now()
    )
    sf = _sessions(pg_schema)

    holder = await pg_schema.connect()
    tx = await holder.begin()
    await holder.execute(
        text("SELECT id FROM meetings WHERE id = :id FOR UPDATE"), {"id": meeting_id}
    )

    async def _writer():
        async with sf() as db, db.begin():
            return await write_status(
                db, meeting_id, "requested", expected_from={"scheduled"}
            )

    task = asyncio.create_task(_writer())
    try:
        await asyncio.sleep(0.5)
        assert not task.done(), "write_status must wait for the meeting row lock"
        async with pg_schema.connect() as probe:
            async with probe.begin():
                waiting = (
                    await probe.execute(
                        text(
                            "SELECT count(*) FROM pg_stat_activity "
                            "WHERE wait_event_type = 'Lock' AND query LIKE '%FROM meetings%'"
                        )
                    )
                ).scalar_one()
                assert waiting == 1, "the writer is parked on the meeting row lock"
                await probe.execute(
                    text(
                        "SELECT meeting_id FROM meeting_aw_state WHERE meeting_id = :id "
                        "FOR UPDATE NOWAIT"
                    ),
                    {"id": meeting_id},
                )
    finally:
        await tx.rollback()
        await holder.close()

    written = await asyncio.wait_for(task, 10)
    assert written.sequence == 1
