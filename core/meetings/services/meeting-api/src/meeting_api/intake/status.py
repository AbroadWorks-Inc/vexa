"""``write_status`` / ``write_event`` — the one status writer and the outbox (§1.4).

Every change to ``meetings.status`` goes through ``write_status``, and every new ``meetings`` row
through ``insert_meeting`` (a new row's status is its first status change); every other meeting
event goes through ``write_event``. All three run inside the CALLER's transaction and never commit.
Nothing else in meeting-api writes ``meetings.status`` (``tests/test_status_writers_all.py``).

``completion_reason`` stays in the sealed ``lifecycle.v1`` set (§1.1): a ``data`` patch naming any
other value is refused before anything is written. The one exception is upstream's ``start_failed``
on a ``failed`` row (``lifecycle.occurrence.START_FAILED``), the reason ``fail_meeting`` gives a
workload that never started and the auto-join retry rule reads.

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
finish instant. Every other active entry is closed. The window and the rule are ``rules.py``'s
``meeting_start``, ``finished_window`` and ``is_rerun``, the one definition the entry service
uses too. The comparison uses the full-precision finish
instant; only the stored and displayed stamps (``closed_at``, ``outcome_at``, ``change.at``,
``created_at``) are whole seconds.

SQLAlchemy and the ORM models are imported inside the functions that touch the database, the way
``collector/adapters.py`` does, so the pure helpers here import without SQLAlchemy installed.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Collection, Mapping, Optional, Sequence

from .projection import iso_utc, project_meeting
from .rules import (
    FINISHED_STATUSES,
    as_utc,
    finished_window,
    is_rerun,
    meeting_start,
)
from .settings import auto_join_lead_s

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "API_VERSION",
    "FINISHED_STATUSES",
    "STATUS_CHANGE_EVENT",
    "Outcome",
    "StatusConflict",
    "WrittenEvent",
    "check_completion_reason",
    "check_event_data",
    "derive_event_id_v2",
    "insert_meeting",
    "lock_aw_state",
    "lock_meeting",
    "project_stored",
    "row_mapping",
    "write_event",
    "write_status",
]

API_VERSION = "2026-09-25"
STATUS_CHANGE_EVENT = "meeting.status_change"


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


def check_event_data(event_data: Optional[Mapping[str, Any]]) -> None:
    """Extra envelope ``data`` keys never replace the meeting projection or the change."""
    reserved = set(event_data or ()) & {"meeting", "change"}
    if reserved:
        raise ValueError(f"event_data may not set {sorted(reserved)}")


def check_completion_reason(
    to_status: str, data_patch: Optional[Mapping[str, Any]]
) -> None:
    """A ``data.completion_reason`` the patch sets is in the sealed ``lifecycle.v1`` set (read from
    the schema the lifecycle receiver validates against), or upstream's ``start_failed`` on a
    ``failed`` row. Raises ``ValueError`` otherwise."""
    reason = (data_patch or {}).get("completion_reason")
    if reason is None:
        return
    from ..lifecycle.occurrence import START_FAILED
    from ..lifecycle.receiver import SEALED_COMPLETION_REASONS

    if reason in SEALED_COMPLETION_REASONS:
        return
    if reason == START_FAILED and to_status == "failed":
        return
    raise ValueError(
        f"completion_reason {reason!r} is not in the sealed lifecycle.v1 set"
    )


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


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(instant: datetime) -> datetime:
    """The whole-second form stored and displayed (``closed_at``, ``outcome_at``, ``change.at``,
    ``created_at``)."""
    return instant.replace(microsecond=0)


async def lock_meeting(db: AsyncSession, meeting_id: int) -> Any:
    """The ``meetings`` row, locked ``FOR UPDATE`` and freshly read (``None`` when missing)."""
    from ..sessions.models import Meeting

    return await db.get(
        Meeting, meeting_id, with_for_update=True, populate_existing=True
    )


async def lock_aw_state(db: AsyncSession, meeting_id: int) -> Any:
    """The ``meeting_aw_state`` row, locked ``FOR UPDATE`` and freshly read, created when missing.
    Call it only while holding the meeting row's lock (the §1.4 lock order)."""
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
    event_data: Optional[Mapping[str, Any]] = None,
) -> str:
    from ..sessions.models import WebhookOutbox

    sequence = int(aw.event_seq)
    event_id = derive_event_id_v2(str(meeting.uuid), event_type, sequence)
    data: dict[str, Any] = {
        "meeting": project_meeting(
            row_mapping(meeting),
            row_mapping(aw),
            [row_mapping(e) for e in entries],
            lead_s=auto_join_lead_s(),
        )
    }
    if change is not None:
        data["change"] = dict(change)
    data.update(event_data or {})
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
    event_data: Optional[Mapping[str, Any]] = None,
) -> WrittenEvent:
    """Move meeting ``meeting_id`` to ``to_status`` if it is currently in ``expected_from`` and
    record the event (§1.4 steps 1–5). Raises ``StatusConflict`` otherwise, having written
    nothing. The event type is ``event_type`` or ``meeting.status_change``; its ``change`` is
    ``{from, to, reason, at}``. ``event_data`` adds keys to the envelope's ``data`` next to
    ``meeting`` and ``change`` (``merged_into``, §2.7); it may not name either of those. A
    ``data_patch`` whose ``completion_reason`` is outside the sealed set raises ``ValueError``
    before anything is written (``check_completion_reason``).

    The caller holds the meeting's link lock (§1.4 lock order). Changes the caller made to the row
    itself (columns, or ``data`` keys a merge can't remove) are flushed before the call: the row is
    re-read here.
    """
    check_event_data(event_data)
    check_completion_reason(to_status, data_patch)
    now = _now()
    meeting = await lock_meeting(db, meeting_id)
    if meeting is None or meeting.status not in expected_from:
        raise StatusConflict(
            meeting_id, None if meeting is None else meeting.status, expected_from
        )

    from_status = meeting.status
    meeting.status = to_status
    if data_patch:
        current = meeting.data if isinstance(meeting.data, dict) else {}
        meeting.data = {**current, **data_patch}

    aw = await lock_aw_state(db, meeting_id)
    if outcome is not None:
        aw.outcome_kind = outcome.kind
        aw.outcome_detail = outcome.detail
        aw.outcome_message = outcome.message
        aw.outcome_at = _stamp(now)
    aw.event_seq = int(aw.event_seq or 0) + 1

    entries = await _entries(db, meeting_id)
    rerun: list[int] = []
    if to_status in FINISHED_STATUSES:
        start = meeting_start(meeting.data, meeting.start_time, meeting.created_at)
        window = finished_window(start, finish=now)
        for entry in entries:
            if entry.state != "active":
                continue
            if is_rerun(
                as_utc(entry.start_at), as_utc(entry.end_at), window, finish=now
            ):
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
        event_data=event_data,
    )
    return WrittenEvent(event_id, int(aw.event_seq), tuple(rerun))


async def insert_meeting(
    db: AsyncSession,
    *,
    user_id: int,
    platform: str,
    native_meeting_id: Optional[str],
    status: str,
    data: Mapping[str, Any],
    first_event: Optional[str],
    scheduled_end_at: Optional[datetime] = None,
    time_zone: Optional[str] = None,
) -> Any:
    """Create a ``meetings`` row in ``status``, with its ``meeting_aw_state`` row, and return it
    (loaded). A new row's status is its first status change (§1.4), so ``first_event`` names the
    event recorded with it: sequence 1, ``change`` ``{from: null, to: status}``. ``None`` only when
    the caller records the meeting's first event itself in this transaction, once the row carries
    what that event must show (intake: the entry it was created for, then ``meeting.scheduled``).

    The caller holds the link's lock when the row has a link. A unique-index violation raises
    ``IntegrityError`` at the flush here."""
    from ..sessions.models import Meeting, MeetingAwState

    check_completion_reason(status, data)
    now = _now()
    meeting = Meeting(
        user_id=user_id,
        platform=platform,
        platform_specific_id=native_meeting_id,
        status=status,
        data=dict(data),
    )
    db.add(meeting)
    await db.flush()
    await db.refresh(meeting)
    aw = MeetingAwState(
        meeting_id=meeting.id,
        event_seq=0,
        scheduled_end_at=scheduled_end_at,
        time_zone=time_zone,
    )
    db.add(aw)
    await db.flush()
    if first_event is not None:
        aw.event_seq = 1
        change = {
            "from": None,
            "to": status,
            "reason": None,
            "at": iso_utc(_stamp(now)),
        }
        await _insert_outbox(db, meeting, aw, [], first_event, change, now=now)
    return meeting


async def project_stored(db: AsyncSession, meeting_id: int) -> Optional[dict[str, Any]]:
    """The one meeting projection (``project_meeting``) of meeting ``meeting_id`` as ``db``'s
    transaction sees it now, or ``None`` when it doesn't exist. Reads only; takes no lock.
    """
    from ..sessions.models import Meeting, MeetingAwState

    meeting = await db.get(Meeting, meeting_id, populate_existing=True)
    if meeting is None:
        return None
    aw = await db.get(MeetingAwState, meeting_id, populate_existing=True)
    entries = await _entries(db, meeting_id)
    return project_meeting(
        row_mapping(meeting),
        None if aw is None else row_mapping(aw),
        [row_mapping(e) for e in entries],
        lead_s=auto_join_lead_s(),
    )


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
    meeting = await lock_meeting(db, meeting_id)
    if meeting is None:
        raise LookupError(f"meeting {meeting_id} not found")
    aw = await lock_aw_state(db, meeting_id)
    aw.event_seq = int(aw.event_seq or 0) + 1
    entries = await _entries(db, meeting_id)
    event_id = await _insert_outbox(
        db, meeting, aw, entries, event_type, change, now=now
    )
    return WrittenEvent(event_id, int(aw.event_seq), ())
