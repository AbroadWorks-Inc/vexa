"""The ``/v2`` reads and erasure over Postgres (§2.1, §1.13), and the paging cursors.

Cursors are opaque to clients: URL-safe base64 of a small JSON value.

  * ``GET /v2/entries`` pages by ``external_id`` (a JSON string). The unique index
    ``uq_meeting_entries_user_source_external`` (``user_id, source_user, external_id``) serves the
    order: equality on the first two columns, the range and the order on the third.
  * ``GET /v2/meetings`` pages by ``(meeting time, id)``, newest first (a JSON ``[time, id]``, the
    time as naive UTC ISO-8601 at full precision). The meeting time is ``meeting_event_time(data,
    start_time, created_at)``: ``data.scheduled_at``, else ``start_time``, else ``created_at``.
    Visibility is decided through the meeting's entries, so the entries drive this query.

``PostgresIntakeReads`` opens one session per call. ``erase`` is one transaction holding the
meeting's link lock and then its row lock (the §1.4 order), so it can't interleave with an entry
write on that link or with an event written for that meeting. ``record_export`` (§1.9) is
``export.store_export``.

SQLAlchemy and the ORM models are imported inside the functions that use them, so the package
imports without SQLAlchemy installed.
"""

from __future__ import annotations

import base64
import binascii
import json
import uuid as uuid_mod
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Mapping, Optional

from .adapters import load_views, take_link_lock
from .export import store_export
from .ports import (
    EntryView,
    ErasedRows,
    ExportReport,
    MeetingQuery,
    MeetingView,
    Room,
)
from .rules import FINISHED_STATUSES, meeting_start
from .status import lock_meeting, row_mapping
from .validation import IntakeError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = [
    "PostgresIntakeReads",
    "decode_entry_cursor",
    "decode_meeting_cursor",
    "encode_entry_cursor",
    "encode_meeting_cursor",
    "meeting_time",
]

_BAD_CURSOR = "cursor: not a cursor this endpoint issued"


def _encode(value: Any) -> str:
    raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode(cursor: str) -> Any:
    try:
        return json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
    except (ValueError, UnicodeError, binascii.Error) as exc:
        raise IntakeError("invalid_request", _BAD_CURSOR) from exc


def encode_entry_cursor(external_id: str) -> str:
    return _encode(external_id)


def decode_entry_cursor(cursor: str) -> str:
    value = _decode(cursor)
    if not isinstance(value, str):
        raise IntakeError("invalid_request", _BAD_CURSOR)
    return value


def encode_meeting_cursor(time: datetime, meeting_id: int) -> str:
    return _encode([time.isoformat(), meeting_id])


def decode_meeting_cursor(cursor: str) -> tuple[datetime, int]:
    value = _decode(cursor)
    if (
        not isinstance(value, list)
        or len(value) != 2
        or not isinstance(value[0], str)
        or not isinstance(value[1], int)
        or isinstance(value[1], bool)
    ):
        raise IntakeError("invalid_request", _BAD_CURSOR)
    try:
        time = datetime.fromisoformat(value[0])
    except ValueError as exc:
        raise IntakeError("invalid_request", _BAD_CURSOR) from exc
    if time.tzinfo is not None:
        raise IntakeError("invalid_request", _BAD_CURSOR)
    return time, value[1]


def meeting_time(row: Mapping[str, Any]) -> datetime:
    """``meeting_event_time`` of a ``meetings`` row mapping, as naive UTC (the SQL function's
    type)."""
    start = meeting_start(row.get("data"), row.get("start_time"), row.get("created_at"))
    if start is None:
        raise ValueError(f"meeting {row.get('id')} has no time")
    return start.astimezone(timezone.utc).replace(tzinfo=None)


def _uuid(value: str) -> Optional[uuid_mod.UUID]:
    try:
        return uuid_mod.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return None


_EVENT_TIME = (
    "meeting_event_time(meetings.data, meetings.start_time, meetings.created_at)"
)


class PostgresIntakeReads:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def entries(
        self, user_id: int, source_user: str, *, after: Optional[str], limit: int
    ) -> list[EntryView]:
        from sqlalchemy import select

        from ..sessions.models import MeetingEntry

        stmt = select(MeetingEntry).where(
            MeetingEntry.user_id == user_id,
            MeetingEntry.source_user == source_user,
            MeetingEntry.state == "active",
        )
        if after is not None:
            stmt = stmt.where(MeetingEntry.external_id > after)
        stmt = stmt.order_by(MeetingEntry.external_id).limit(limit)
        async with self._session_factory() as db:
            rows = (await db.execute(stmt)).scalars().all()
            return [EntryView.from_row(row_mapping(row)) for row in rows]

    async def meetings(self, user_id: int, query: MeetingQuery) -> list[MeetingView]:
        from sqlalchemy import and_, exists, or_, select, text

        from ..sessions.models import Meeting, MeetingEntry

        visible = exists().where(
            MeetingEntry.meeting_id == Meeting.id,
            MeetingEntry.user_id == user_id,
            or_(
                MeetingEntry.source_user == query.user,
                MeetingEntry.attendees.contains([query.user]),
            ),
        )
        stmt = select(Meeting).where(Meeting.user_id == user_id, visible)
        if query.start_from is not None:
            stmt = stmt.where(
                text(f"{_EVENT_TIME} >= :start_from").bindparams(
                    start_from=query.start_from
                )
            )
        if query.start_to is not None:
            stmt = stmt.where(
                text(f"{_EVENT_TIME} < :start_to").bindparams(start_to=query.start_to)
            )
        if query.status is not None:
            stmt = stmt.where(Meeting.status == query.status)
        if query.external_id is not None:
            stmt = stmt.where(
                exists().where(
                    and_(
                        MeetingEntry.meeting_id == Meeting.id,
                        MeetingEntry.user_id == user_id,
                        MeetingEntry.external_id == query.external_id,
                    )
                )
            )
        if query.after is not None:
            after_time, after_id = query.after
            stmt = stmt.where(
                text(
                    f"({_EVENT_TIME}, meetings.id) < (:after_time, :after_id)"
                ).bindparams(after_time=after_time, after_id=after_id)
            )
        stmt = stmt.order_by(text(f"{_EVENT_TIME} DESC"), Meeting.id.desc()).limit(
            query.limit
        )
        async with self._session_factory() as db:
            rows = (await db.execute(stmt)).scalars().all()
            return await load_views(db, rows, populate_existing=False)

    async def meeting_by_uuid(self, user_id: int, uuid: str) -> Optional[MeetingView]:
        from sqlalchemy import select

        from ..sessions.models import Meeting

        parsed = _uuid(uuid)
        if parsed is None:
            return None
        async with self._session_factory() as db:
            rows = (
                (
                    await db.execute(
                        select(Meeting).where(
                            Meeting.uuid == parsed, Meeting.user_id == user_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            views = await load_views(db, rows, populate_existing=False)
        return views[0] if views else None

    async def visible_to(self, user_id: int, meeting_id: int, user: str) -> bool:
        from sqlalchemy import or_, select

        from ..sessions.models import MeetingEntry

        stmt = (
            select(MeetingEntry.id)
            .where(
                MeetingEntry.meeting_id == meeting_id,
                MeetingEntry.user_id == user_id,
                or_(
                    MeetingEntry.source_user == user,
                    MeetingEntry.attendees.contains([user]),
                ),
            )
            .limit(1)
        )
        async with self._session_factory() as db:
            return (await db.execute(stmt)).first() is not None

    async def erase(self, user_id: int, meeting_id: int) -> ErasedRows:
        from sqlalchemy import delete, select

        from ..sessions.models import (
            Meeting,
            MeetingEntry,
            WebhookDelivery,
            WebhookOutbox,
        )

        async with self._session_factory() as db, db.begin():
            found = (
                await db.execute(
                    select(Meeting.platform, Meeting.platform_specific_id).where(
                        Meeting.id == meeting_id, Meeting.user_id == user_id
                    )
                )
            ).first()
            if found is None:
                raise IntakeError("meeting_not_found", "no such meeting")
            await take_link_lock(db, user_id, Room(found[0], found[1]))
            meeting = await lock_meeting(db, meeting_id)
            if meeting is None or meeting.user_id != user_id:
                raise IntakeError("meeting_not_found", "no such meeting")
            if meeting.status not in FINISHED_STATUSES:
                raise IntakeError(
                    "meeting_not_finished",
                    "the meeting hasn't finished; remove its entries or stop it first",
                )
            events = select(WebhookOutbox.event_id).where(
                WebhookOutbox.meeting_id == meeting_id
            )
            deliveries = await db.execute(
                delete(WebhookDelivery)
                .where(WebhookDelivery.event_id.in_(events))
                .execution_options(synchronize_session=False)
            )
            outbox = await db.execute(
                delete(WebhookOutbox)
                .where(WebhookOutbox.meeting_id == meeting_id)
                .execution_options(synchronize_session=False)
            )
            entries = await db.execute(
                delete(MeetingEntry)
                .where(
                    MeetingEntry.meeting_id == meeting_id,
                    MeetingEntry.user_id == user_id,
                )
                .execution_options(synchronize_session=False)
            )
            return ErasedRows(
                entries=int(entries.rowcount),  # type: ignore[attr-defined]
                outbox=int(outbox.rowcount),  # type: ignore[attr-defined]
                deliveries=int(deliveries.rowcount),  # type: ignore[attr-defined]
            )

    async def record_export(
        self, user_id: int, meeting_id: int, report: ExportReport
    ) -> Optional[str]:
        async with self._session_factory() as db, db.begin():
            return await store_export(db, user_id, meeting_id, report)
