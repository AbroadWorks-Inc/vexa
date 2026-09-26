"""``write_status`` / ``write_event`` — the one status writer and the outbox (§1.4).

Every change to ``meetings.status`` goes through ``write_status``; every other meeting event goes
through ``write_event``. Both run inside the CALLER's transaction and never commit.

``write_status`` does five things, in this order:
  1. lock the ``meetings`` row (``FOR UPDATE``); if its status isn't in ``expected_from`` raise
     ``StatusConflict`` having written nothing;
  2. write the status and the optional ``data`` patch (a shallow merge into ``meetings.data``);
  3. lock ``meeting_aw_state`` (created if missing), set the outcome if given, ``event_seq += 1``;
  4. on a finished status (``completed`` / ``failed``), close the active entries, except an entry
     that re-runs (R7, R10, below): it stays active and its id comes back in ``rerun_entry_ids``;
  5. insert the event into ``webhook_outbox``: the §2.7 envelope around the one meeting projection
     (``project_meeting``), serialised once; the stored ``payload_text`` is the exact body sent.

Lock order (§1.4): the caller takes the link's advisory lock first, then ``write_status`` locks the
meeting row, then ``meeting_aw_state``. ``meeting_aw_state`` is always reached through its meeting
row's lock, which is why creating a missing row here cannot race another writer.

The finished meeting's window (R10) is what actually happened: ``[meeting start, finish)``. The
meeting start is ``data.scheduled_at``, else ``start_time``, else ``created_at`` (the
``meeting_event_time()`` order); the end is the finish instant, clamped to be no earlier than the
start. An active entry re-runs when all three hold: its ``[start_at, end_at)`` (unbounded without
an ``end_at``) doesn't overlap that window (half-open, as R1's), its ``start_at`` is after the
meeting start (an entry that isn't belongs to this meeting), and its ``start_at`` is after the
finish instant. Every other active entry is closed. The comparison uses the full-precision finish
instant; only the stored and displayed stamps (``closed_at``, ``outcome_at``, ``change.at``,
``created_at``) are whole seconds.

SQLAlchemy and the ORM models are imported inside the functions that touch the database, the way
``collector/adapters.py`` does, so the pure helpers here import without SQLAlchemy installed.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Collection, Mapping, Optional, Sequence

from .projection import iso_utc, project_meeting

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "API_VERSION",
    "FINISHED_STATUSES",
    "Outcome",
    "StatusConflict",
    "WrittenEvent",
    "derive_event_id_v2",
    "row_mapping",
    "write_event",
    "write_status",
]

API_VERSION = "2026-09-25"
FINISHED_STATUSES = frozenset({"completed", "failed"})
STATUS_CHANGE_EVENT = "meeting.status_change"

_UNBOUNDED = datetime.max.replace(tzinfo=timezone.utc)


class StatusConflict(Exception):
    """The meeting's status isn't one the caller expected (or the meeting doesn't exist:
    ``actual is None``). Raised before anything is written."""

    def __init__(
        self, meeting_id: int, actual: Optional[str], expected: Collection[str]
    ) -> None:
        super().__init__(
            f"meeting {meeting_id}: status {actual!r} not in {sorted(expected)!r}"
        )
        self.meeting_id = meeting_id
        self.actual = actual
        self.expected = frozenset(expected)


@dataclass(frozen=True)
class Outcome:
    kind: str
    detail: Optional[str]
    message: Optional[str]


@dataclass(frozen=True)
class WrittenEvent:
    event_id: str
    sequence: int
    rerun_entry_ids: tuple[int, ...]


def derive_event_id_v2(meeting_uuid: str, event_type: str, sequence: int) -> str:
    """``evt_`` + the full sha256 hex of ``uuid|event_type|sequence`` (§1.4): the same event keeps
    the same id across every redelivery, and every event of a meeting gets its own."""
    key = f"{meeting_uuid}|{event_type}|{sequence}"
    return "evt_" + hashlib.sha256(key.encode("utf-8")).hexdigest()


def row_mapping(row: Any) -> dict[str, Any]:
    """One ORM row as ``{column name: value}`` — the mapping shape ``project_meeting`` reads (so
    ``MeetingEntry.metadata_`` appears under ``"metadata"``, its column name).

    Loaded values only: it never emits SQL (an async session can't lazy-load from sync code), so a
    column a flush has expired — a server-side default or ``onupdate`` such as ``updated_at`` — is
    absent rather than fetched."""
    from sqlalchemy import inspect

    state = inspect(row)
    loaded = state.dict
    return {
        prop.columns[0].name: loaded[prop.key]
        for prop in state.mapper.column_attrs
        if prop.key in loaded
    }


def _overlaps(
    a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime
) -> bool:
    """Half-open interval overlap: ``[a_start, a_end)`` and ``[b_start, b_end)`` share an instant."""
    return a_start < b_end and a_end > b_start


def _as_utc(value: Any) -> Optional[datetime]:
    """A datetime or ISO-8601 string as an aware UTC datetime (naive means UTC), or ``None``."""
    if value is None or value == "":
        return None
    dt = (
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        if isinstance(value, str)
        else value
    )
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _finished_window(meeting: Any, *, finish: datetime) -> tuple[datetime, datetime]:
    """``[meeting start, finish)``, the end clamped to be no earlier than the start (R10)."""
    data = meeting.data if isinstance(meeting.data, dict) else {}
    start = (
        _as_utc(data.get("scheduled_at"))
        or _as_utc(meeting.start_time)
        or _as_utc(meeting.created_at)
        or finish
    )
    return start, max(finish, start)


def _is_rerun(
    entry: Any, window: tuple[datetime, datetime], *, finish: datetime
) -> bool:
    """R7/R10: the entry doesn't overlap the finished window, starts after the meeting start and
    starts after the finish."""
    start = _as_utc(entry.start_at)
    if start is None:
        return False
    end = _as_utc(entry.end_at) or _UNBOUNDED
    return not _overlaps(start, end, *window) and start > window[0] and start > finish


def _lead_s() -> int:
    """``AUTO_JOIN_LEAD_S``, read exactly as the auto-join sweep's entrypoint reads it."""
    # Imported at call time so bot_spawn can import this module without an import cycle.
    from ..bot_spawn.auto_join import DEFAULT_LEAD_S

    return int(float(os.getenv("AUTO_JOIN_LEAD_S", str(DEFAULT_LEAD_S))))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(instant: datetime) -> datetime:
    """The whole-second form stored and displayed (``closed_at``, ``outcome_at``, ``change.at``,
    ``created_at``)."""
    return instant.replace(microsecond=0)


async def _lock_meeting(db: AsyncSession, meeting_id: int) -> Any:
    from ..sessions.models import Meeting

    return await db.get(
        Meeting, meeting_id, with_for_update=True, populate_existing=True
    )


async def _lock_aw_state(db: AsyncSession, meeting_id: int) -> Any:
    from ..sessions.models import MeetingAwState

    aw = await db.get(
        MeetingAwState, meeting_id, with_for_update=True, populate_existing=True
    )
    if aw is None:
        aw = MeetingAwState(meeting_id=meeting_id, event_seq=0)
        db.add(aw)
        await db.flush()
    return aw


async def _entries(db: AsyncSession, meeting_id: int) -> list[Any]:
    from sqlalchemy import select

    from ..sessions.models import MeetingEntry

    result = await db.execute(
        select(MeetingEntry)
        .where(MeetingEntry.meeting_id == meeting_id)
        .order_by(MeetingEntry.id)
    )
    return list(result.scalars().all())


async def _insert_outbox(
    db: AsyncSession,
    meeting: Any,
    aw: Any,
    entries: Sequence[Any],
    event_type: str,
    change: Optional[Mapping[str, Any]],
    *,
    now: datetime,
) -> str:
    from ..sessions.models import WebhookOutbox

    sequence = int(aw.event_seq)
    event_id = derive_event_id_v2(str(meeting.uuid), event_type, sequence)
    data: dict[str, Any] = {
        "meeting": project_meeting(
            row_mapping(meeting),
            row_mapping(aw),
            [row_mapping(e) for e in entries],
            lead_s=_lead_s(),
        )
    }
    if change is not None:
        data["change"] = dict(change)
    envelope = {
        "event_id": event_id,
        "event_type": event_type,
        "api_version": API_VERSION,
        "created_at": iso_utc(_stamp(now)),
        "data": data,
    }
    db.add(
        WebhookOutbox(
            event_id=event_id,
            meeting_id=meeting.id,
            event_type=event_type,
            sequence=sequence,
            payload_text=json.dumps(envelope, separators=(",", ":"), sort_keys=True),
            created_at=_stamp(now),
        )
    )
    await db.flush()
    return event_id


async def write_status(
    db: AsyncSession,
    meeting_id: int,
    to_status: str,
    *,
    expected_from: Collection[str],
    data_patch: Optional[Mapping[str, Any]] = None,
    outcome: Optional[Outcome] = None,
    change_reason: Optional[str] = None,
    event_type: Optional[str] = None,
) -> WrittenEvent:
    """Move meeting ``meeting_id`` to ``to_status`` if it is currently in ``expected_from`` and
    record the event (§1.4 steps 1–5). Raises ``StatusConflict`` otherwise, having written
    nothing. The event type is ``event_type`` or ``meeting.status_change``; its ``change`` is
    ``{from, to, reason, at}``."""
    now = _now()
    meeting = await _lock_meeting(db, meeting_id)
    if meeting is None or meeting.status not in expected_from:
        raise StatusConflict(
            meeting_id, None if meeting is None else meeting.status, expected_from
        )

    from_status = meeting.status
    meeting.status = to_status
    if data_patch:
        current = meeting.data if isinstance(meeting.data, dict) else {}
        meeting.data = {**current, **data_patch}

    aw = await _lock_aw_state(db, meeting_id)
    if outcome is not None:
        aw.outcome_kind = outcome.kind
        aw.outcome_detail = outcome.detail
        aw.outcome_message = outcome.message
        aw.outcome_at = _stamp(now)
    aw.event_seq = int(aw.event_seq or 0) + 1

    entries = await _entries(db, meeting_id)
    rerun: list[int] = []
    if to_status in FINISHED_STATUSES:
        window = _finished_window(meeting, finish=now)
        for entry in entries:
            if entry.state != "active":
                continue
            if _is_rerun(entry, window, finish=now):
                rerun.append(int(entry.id))
            else:
                entry.state = "closed"
                entry.closed_at = _stamp(now)

    change = {
        "from": from_status,
        "to": to_status,
        "reason": change_reason,
        "at": iso_utc(_stamp(now)),
    }
    event_id = await _insert_outbox(
        db,
        meeting,
        aw,
        entries,
        event_type or STATUS_CHANGE_EVENT,
        change,
        now=now,
    )
    return WrittenEvent(event_id, int(aw.event_seq), tuple(rerun))


async def write_event(
    db: AsyncSession,
    meeting_id: int,
    event_type: str,
    change: Optional[Mapping[str, Any]] = None,
) -> WrittenEvent:
    """Record a meeting event that doesn't change the status (``meeting.updated``,
    ``meeting.waiting_for_room``, ``export.*``, …): the same locks, ``event_seq += 1`` and outbox
    row as ``write_status``. ``change`` is carried as ``data.change`` when given. Raises
    ``LookupError`` when the meeting doesn't exist."""
    now = _now()
    meeting = await _lock_meeting(db, meeting_id)
    if meeting is None:
        raise LookupError(f"meeting {meeting_id} not found")
    aw = await _lock_aw_state(db, meeting_id)
    aw.event_seq = int(aw.event_seq or 0) + 1
    entries = await _entries(db, meeting_id)
    event_id = await _insert_outbox(
        db, meeting, aw, entries, event_type, change, now=now
    )
    return WrittenEvent(event_id, int(aw.event_seq), ())
