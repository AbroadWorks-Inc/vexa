"""The Postgres ``IntakeStore`` (§1.3, §1.4): the entry service's storage over a SQLAlchemy-async
``session_factory``, the ``collector/adapters.py`` pattern.

``room_lock(user_id, rooms)`` opens one session and one transaction and takes the link lock of
every room, in the order given, which must be sorted and distinct (the caller sorts them, so two
requests needing the same links queue instead of deadlocking):

    pg_advisory_xact_lock(hashtextextended('aw-intake:'||user_id||':'||platform||':'||native, 0))

the single-bigint form (``sweeps/single_flight.py`` explains why the two-int form is a trap). The
lock is released when the transaction ends: it commits when the block exits normally and rolls back
when the block raises, taking every row and outbox event written in it along.

Inside the transaction the lock order is the link lock, then the ``meetings`` row, then its
``meeting_aw_state`` row: every write to a meeting or its aw-state row locks them through
``status.lock_meeting`` / ``status.lock_aw_state``, and status changes and events go through
``write_status`` / ``write_event`` in the same session. Entry rows are serialised by the link lock.

Reads return fresh rows (``populate_existing``), as column-name mappings (``status.row_mapping``),
so the views and the projection read exactly what is stored. ``room_meetings`` returns the link's
live meetings and the non-finished meetings entries manage; an entry-less upstream-planned row is
never offered to an entry (Ruling R15). ``count_active_entries`` counts on the partial index
``ix_meeting_entries_active_user``, index-only; the ``state = 'active'`` predicate is a literal so a
cached generic plan still matches the index.

``overdue_meetings`` is the not-sent sweep's read (§1.5): ``scheduled`` meetings that have an entry
row and whose ``scheduled_end_at`` has passed (an open-ended one: its start plus ``open_ended_s``).
A meeting's end is never before its start, so the read also bounds the meeting time by ``now`` and
walks the partial index ``ix_meeting_scheduled_due`` (``status = 'scheduled'`` is a literal for the
same generic-plan reason).

SQLAlchemy and the ORM models are imported inside the functions that use them, so the package
imports without SQLAlchemy installed.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import (
    TYPE_CHECKING,
    Any,
    AsyncIterator,
    Collection,
    Mapping,
    Optional,
    Sequence,
)

from .ports import EntryView, MeetingView, Room
from .projection import iso_utc
from .rules import FINISHED_STATUSES, Plan
from .status import (
    Outcome,
    WrittenEvent,
    lock_aw_state,
    lock_meeting,
    row_mapping,
    write_event,
    write_status,
)
from .validation import EntryIn

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = ["PostgresIntakeStore", "PostgresIntakeTx", "take_link_lock"]


async def take_link_lock(db: AsyncSession, user_id: int, room: Room) -> None:
    """Take the link's advisory lock (§1.4) for the rest of ``db``'s transaction."""
    from sqlalchemy import text

    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"aw-intake:{user_id}:{room.platform}:{room.native_meeting_id}"},
    )


def _plan_data(plan: Plan) -> dict[str, Any]:
    """The plan's keys in ``meetings.data``: the join time auto-join reads, the title and link."""
    return {
        "scheduled_at": iso_utc(plan.start),
        "title": plan.title,
        "constructed_meeting_url": plan.meeting_url,
    }


def _entry_view(row: Any) -> EntryView:
    return EntryView.from_row(row_mapping(row))


class PostgresIntakeStore:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    @asynccontextmanager
    async def room_lock(
        self, user_id: int, rooms: Sequence[Room]
    ) -> AsyncIterator[PostgresIntakeTx]:
        ordered = tuple(rooms)
        if list(ordered) != sorted(set(ordered)):
            raise ValueError("link locks must be distinct and taken in sorted order")
        async with self._session_factory() as db, db.begin():
            for room in ordered:
                await take_link_lock(db, user_id, room)
            yield PostgresIntakeTx(db)

    async def overdue_meetings(
        self, now: datetime, *, open_ended_s: int
    ) -> list[MeetingView]:
        async with self._reading() as tx:
            return await tx._overdue(now, open_ended_s=open_ended_s)

    @asynccontextmanager
    async def _reading(self) -> AsyncIterator[PostgresIntakeTx]:
        async with self._session_factory() as db:
            yield PostgresIntakeTx(db)


class PostgresIntakeTx:
    """One transaction under the link locks it was opened with (``IntakeTx``)."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    # ── reads ───────────────────────────────────────────────────────────────────────────────

    async def _scalars(self, stmt: Any) -> list[Any]:
        result = await self._db.execute(stmt.execution_options(populate_existing=True))
        return list(result.scalars().all())

    async def _views(self, meetings: Sequence[Any]) -> list[MeetingView]:
        from sqlalchemy import select

        from ..sessions.models import MeetingAwState, MeetingEntry

        ids = [m.id for m in meetings]
        if not ids:
            return []
        aws = {
            aw.meeting_id: aw
            for aw in await self._scalars(
                select(MeetingAwState).where(MeetingAwState.meeting_id.in_(ids))
            )
        }
        entries: dict[int, list[EntryView]] = {}
        for row in await self._scalars(
            select(MeetingEntry)
            .where(MeetingEntry.meeting_id.in_(ids))
            .order_by(MeetingEntry.id)
        ):
            entries.setdefault(row.meeting_id, []).append(_entry_view(row))
        return [
            MeetingView(
                row=row_mapping(m),
                aw=row_mapping(aws[m.id]) if m.id in aws else None,
                entries=tuple(entries.get(m.id, ())),
            )
            for m in meetings
        ]

    async def find_entry(
        self, user_id: int, source_user: str, external_id: str
    ) -> Optional[EntryView]:
        row = await self._entry_row(user_id, source_user, external_id)
        return None if row is None else _entry_view(row)

    async def _entry_row(
        self, user_id: int, source_user: str, external_id: str
    ) -> Optional[Any]:
        from sqlalchemy import select

        from ..sessions.models import MeetingEntry

        found = await self._scalars(
            select(MeetingEntry).where(
                MeetingEntry.user_id == user_id,
                MeetingEntry.source_user == source_user,
                MeetingEntry.external_id == external_id,
            )
        )
        return found[0] if found else None

    async def entry(self, entry_id: int) -> Optional[EntryView]:
        from sqlalchemy import select

        from ..sessions.models import MeetingEntry

        found = await self._scalars(
            select(MeetingEntry).where(MeetingEntry.id == entry_id)
        )
        return _entry_view(found[0]) if found else None

    async def room_meetings(self, user_id: int, room: Room) -> list[MeetingView]:
        from sqlalchemy import and_, exists, or_, select

        from ..bot_spawn.auto_join import LIVE_STATUSES
        from ..sessions.models import Meeting, MeetingEntry

        managed = exists().where(MeetingEntry.meeting_id == Meeting.id)
        meetings = await self._scalars(
            select(Meeting)
            .where(
                Meeting.user_id == user_id,
                Meeting.platform == room.platform,
                Meeting.platform_specific_id == room.native_meeting_id,
                or_(
                    Meeting.status.in_(LIVE_STATUSES),
                    and_(Meeting.status.notin_(FINISHED_STATUSES), managed),
                ),
            )
            .order_by(Meeting.id)
        )
        return await self._views(meetings)

    async def _overdue(self, now: datetime, *, open_ended_s: int) -> list[MeetingView]:
        """``PostgresIntakeStore.overdue_meetings``'s read."""
        from sqlalchemy import and_, exists, func, or_, select, text

        from ..sessions.models import Meeting, MeetingAwState, MeetingEntry

        aware = now.astimezone(timezone.utc)
        naive = aware.replace(tzinfo=None)
        event_time = func.meeting_event_time(
            Meeting.data, Meeting.start_time, Meeting.created_at
        )
        end = MeetingAwState.scheduled_end_at
        stmt = (
            select(Meeting)
            .outerjoin(MeetingAwState, MeetingAwState.meeting_id == Meeting.id)
            .where(
                text("meetings.status = 'scheduled'"),
                event_time <= naive,
                exists().where(MeetingEntry.meeting_id == Meeting.id),
                or_(
                    end <= aware,
                    and_(
                        end.is_(None),
                        event_time <= naive - timedelta(seconds=open_ended_s),
                    ),
                ),
            )
            .order_by(Meeting.id)
        )
        return await self._views(await self._scalars(stmt))

    async def meeting(self, meeting_id: int) -> MeetingView:
        from sqlalchemy import select

        from ..sessions.models import Meeting

        found = await self._scalars(select(Meeting).where(Meeting.id == meeting_id))
        if not found:
            raise LookupError(f"meeting {meeting_id} not found")
        return (await self._views(found))[0]

    async def count_active_entries(self, user_id: int) -> int:
        from sqlalchemy import text

        result = await self._db.execute(
            text(
                "SELECT count(*) FROM meeting_entries "
                "WHERE user_id = :user_id AND state = 'active'"
            ),
            {"user_id": user_id},
        )
        return int(result.scalar_one())

    async def active_entries(self, meeting_id: int) -> list[EntryView]:
        return [_entry_view(row) for row in await self._active_rows(meeting_id)]

    async def _active_rows(self, meeting_id: int) -> list[Any]:
        from sqlalchemy import select

        from ..sessions.models import MeetingEntry

        return await self._scalars(
            select(MeetingEntry)
            .where(
                MeetingEntry.meeting_id == meeting_id, MeetingEntry.state == "active"
            )
            .order_by(MeetingEntry.id)
        )

    # ── writes ──────────────────────────────────────────────────────────────────────────────

    async def _locked_meeting(self, meeting_id: int) -> Any:
        meeting = await lock_meeting(self._db, meeting_id)
        if meeting is None:
            raise LookupError(f"meeting {meeting_id} not found")
        return meeting

    async def create_meeting(
        self, user_id: int, room: Room, plan: Plan, *, join_now: bool
    ) -> MeetingView:
        from ..sessions.models import Meeting, MeetingAwState

        meeting = Meeting(
            user_id=user_id,
            platform=room.platform,
            platform_specific_id=room.native_meeting_id,
            status="scheduled",
            data={**_plan_data(plan), "auto_join": True},
        )
        self._db.add(meeting)
        await self._db.flush()
        self._db.add(
            MeetingAwState(
                meeting_id=meeting.id,
                event_seq=0,
                scheduled_end_at=plan.end,
                time_zone=plan.time_zone,
            )
        )
        await self._db.flush()
        return await self.meeting(meeting.id)

    async def save_entry(
        self, user_id: int, entry: EntryIn, room: Room, meeting_id: int
    ) -> EntryView:
        from ..sessions.models import MeetingEntry

        row = await self._entry_row(user_id, entry.user, entry.external_id)
        if row is None:
            row = MeetingEntry(
                user_id=user_id, source_user=entry.user, external_id=entry.external_id
            )
            self._db.add(row)
        row.meeting_id = meeting_id
        row.meeting_url = entry.meeting_url
        row.platform = room.platform
        row.native_meeting_id = room.native_meeting_id
        row.title = entry.title
        row.start_at = entry.start
        row.end_at = entry.end
        row.time_zone = entry.time_zone
        row.series_id = entry.series_id
        row.attendees = list(entry.attendees)
        row.join_now = entry.join_now
        row.metadata_ = entry.metadata
        row.content_hash = entry.content_hash
        row.state = "active"
        row.removed_reason = None
        row.removed_at = None
        row.closed_at = None
        await self._db.flush()
        return _entry_view(row)

    async def mark_entry_removed(self, entry_id: int, reason: Optional[str]) -> None:
        from sqlalchemy import func

        from ..sessions.models import MeetingEntry

        row = await self._db.get(MeetingEntry, entry_id, populate_existing=True)
        if row is None:
            raise LookupError(f"entry {entry_id} not found")
        row.state = "removed"
        row.removed_reason = reason
        row.removed_at = func.now()
        await self._db.flush()

    async def apply_plan(self, meeting_id: int, room: Room, plan: Plan) -> None:
        meeting = await self._locked_meeting(meeting_id)
        data = meeting.data if isinstance(meeting.data, dict) else {}
        meeting.platform = room.platform
        meeting.platform_specific_id = room.native_meeting_id
        meeting.data = {**data, **_plan_data(plan)}
        aw = await lock_aw_state(self._db, meeting_id)
        aw.scheduled_end_at = plan.end
        aw.time_zone = plan.time_zone
        await self._db.flush()

    async def set_title(self, meeting_id: int, title: Optional[str]) -> None:
        meeting = await self._locked_meeting(meeting_id)
        data = meeting.data if isinstance(meeting.data, dict) else {}
        meeting.data = {**data, "title": title}
        await self._db.flush()

    async def record_spawn_error(
        self, meeting_id: int, code: Optional[str], message: Optional[str]
    ) -> None:
        await self._locked_meeting(meeting_id)
        aw = await lock_aw_state(self._db, meeting_id)
        aw.last_error_code = code
        aw.last_error_message = message
        await self._db.flush()

    async def record_outcome(self, meeting_id: int, outcome: Outcome) -> None:
        await self._locked_meeting(meeting_id)
        aw = await lock_aw_state(self._db, meeting_id)
        aw.outcome_kind = outcome.kind
        aw.outcome_detail = outcome.detail
        aw.outcome_message = outcome.message
        aw.outcome_at = datetime.now(timezone.utc).replace(microsecond=0)
        await self._db.flush()

    async def mark_waiting_for_room(self, meeting_id: int) -> None:
        await self._locked_meeting(meeting_id)
        aw = await lock_aw_state(self._db, meeting_id)
        aw.waiting_for_room_sent_at = datetime.now(timezone.utc).replace(microsecond=0)
        await self._db.flush()

    async def move_active_entries(
        self, from_meeting_id: int, to_meeting_id: int
    ) -> None:
        for row in await self._active_rows(from_meeting_id):
            row.meeting_id = to_meeting_id
        await self._db.flush()

    async def status(
        self,
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
        return await write_status(
            self._db,
            meeting_id,
            to_status,
            expected_from=expected_from,
            data_patch=data_patch,
            outcome=outcome,
            change_reason=change_reason,
            event_type=event_type,
            event_data=event_data,
        )

    async def event(
        self,
        meeting_id: int,
        event_type: str,
        change: Optional[Mapping[str, Any]] = None,
    ) -> WrittenEvent:
        return await write_event(self._db, meeting_id, event_type, change)
