"""The entry service's ports (§1.3): storage, spawn, stop and event publishing.

The service talks to storage only through ``IntakeStore`` / ``IntakeTx``; ``fakes.py`` holds the
in-memory implementation and the Postgres adapter implements the same protocol. Spawning and
stopping go through ``SpawnPort`` / ``StopPort`` (the existing spawn and stop paths), and events
are handed to ``EventPublisher`` after the transaction that wrote them commits.

``IntakeStore.room_lock(user_id, rooms)`` opens one transaction holding the link lock of every
room given, taken in the order given (the caller passes them sorted); an empty ``rooms`` is a plain
transaction with no link lock. The transaction commits when the block exits normally and rolls
back when it raises.

The views are what the service reads. ``MeetingView`` carries the ``meetings`` row and the
``meeting_aw_state`` row as column-name mappings plus the meeting's entries, so the reply is the
one projection (``project_meeting``) over exactly what was read.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import (
    Any,
    AsyncContextManager,
    Collection,
    Literal,
    Mapping,
    Optional,
    Protocol,
    Sequence,
)

from .projection import project_meeting
from .rules import Plan, as_utc, meeting_start
from .status import Outcome, WrittenEvent
from .validation import EntryIn

__all__ = [
    "EntryView",
    "EventPublisher",
    "IntakeStore",
    "IntakeTx",
    "MeetingView",
    "Room",
    "SpawnOutcome",
    "SpawnPort",
    "StopPort",
]


@dataclass(frozen=True, order=True)
class Room:
    """A meeting link: the platform plus its room code, as ``collector/meeting_link.py`` parses it.
    Ordered, so a caller locking two rooms sorts them into the one lock order."""

    platform: str
    native_meeting_id: str


@dataclass(frozen=True)
class EntryView:
    """One ``meeting_entries`` row. ``start``/``end`` are the ``start_at``/``end_at`` columns."""

    id: int
    user_id: int
    source_user: str
    external_id: str
    meeting_id: int
    meeting_url: str
    platform: str
    native_meeting_id: str
    title: Optional[str]
    start: datetime
    end: Optional[datetime]
    time_zone: Optional[str]
    series_id: Optional[str]
    attendees: tuple[str, ...]
    join_now: bool
    metadata: Optional[dict[str, Any]]
    content_hash: str
    state: str
    removed_reason: Optional[str] = None

    @property
    def room(self) -> Room:
        return Room(self.platform, self.native_meeting_id)

    def row(self) -> dict[str, Any]:
        """The row under its column names, the mapping ``project_meeting`` reads."""
        return {
            "id": self.id,
            "user_id": self.user_id,
            "source_user": self.source_user,
            "external_id": self.external_id,
            "meeting_id": self.meeting_id,
            "meeting_url": self.meeting_url,
            "platform": self.platform,
            "native_meeting_id": self.native_meeting_id,
            "title": self.title,
            "start_at": self.start,
            "end_at": self.end,
            "time_zone": self.time_zone,
            "series_id": self.series_id,
            "attendees": list(self.attendees),
            "join_now": self.join_now,
            "metadata": self.metadata,
            "content_hash": self.content_hash,
            "state": self.state,
            "removed_reason": self.removed_reason,
        }

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> EntryView:
        """The entry from its row under its column names (``row()``'s shape, as
        ``status.row_mapping`` reads a stored row)."""
        return cls(
            id=int(row["id"]),
            user_id=int(row["user_id"]),
            source_user=row["source_user"],
            external_id=row["external_id"],
            meeting_id=int(row["meeting_id"]),
            meeting_url=row["meeting_url"],
            platform=row["platform"],
            native_meeting_id=row["native_meeting_id"],
            title=row["title"],
            start=row["start_at"],
            end=row["end_at"],
            time_zone=row["time_zone"],
            series_id=row["series_id"],
            attendees=tuple(row["attendees"] or ()),
            join_now=bool(row["join_now"]),
            metadata=row["metadata"],
            content_hash=row["content_hash"],
            state=row["state"],
            removed_reason=row["removed_reason"],
        )

    def as_entry_in(self) -> EntryIn:
        """The stored entry as the request that would store it (for a re-run, R7)."""
        return EntryIn(
            external_id=self.external_id,
            user=self.source_user,
            meeting_url=self.meeting_url,
            start=self.start,
            end=self.end,
            time_zone=self.time_zone,
            title=self.title,
            attendees=self.attendees,
            series_id=self.series_id,
            join_now=self.join_now,
            metadata=self.metadata,
            content_hash=self.content_hash,
        )


@dataclass(frozen=True)
class MeetingView:
    """One meeting as read: its ``meetings`` row, its ``meeting_aw_state`` row (``None`` when it
    has none) and every entry pointing at it."""

    row: Mapping[str, Any]
    aw: Optional[Mapping[str, Any]]
    entries: tuple[EntryView, ...] = field(default=())

    @property
    def id(self) -> int:
        return int(self.row["id"])

    @property
    def uuid(self) -> str:
        return str(self.row["uuid"])

    @property
    def user_id(self) -> int:
        return int(self.row["user_id"])

    @property
    def status(self) -> str:
        return str(self.row["status"])

    @property
    def data(self) -> Mapping[str, Any]:
        value = self.row.get("data")
        return value if isinstance(value, Mapping) else {}

    @property
    def room(self) -> Room:
        return Room(str(self.row["platform"]), str(self.row["platform_specific_id"]))

    @property
    def start(self) -> Optional[datetime]:
        return meeting_start(
            self.data, self.row.get("start_time"), self.row.get("created_at")
        )

    @property
    def end(self) -> Optional[datetime]:
        return as_utc(self.aw.get("scheduled_end_at")) if self.aw else None

    @property
    def title(self) -> Optional[str]:
        return self.data.get("title")

    @property
    def time_zone(self) -> Optional[str]:
        return self.aw.get("time_zone") if self.aw else None

    def project(self, *, lead_s: int) -> dict[str, Any]:
        """The §2.4 ``meeting`` object."""
        return project_meeting(
            self.row, self.aw, [e.row() for e in self.entries], lead_s=lead_s
        )

    def active_entries(self) -> tuple[EntryView, ...]:
        return tuple(e for e in self.entries if e.state == "active")


class IntakeTx(Protocol):
    """One transaction under the link locks it was opened with."""

    async def find_entry(
        self, user_id: int, source_user: str, external_id: str
    ) -> Optional[EntryView]: ...

    async def entry(self, entry_id: int) -> Optional[EntryView]: ...

    async def room_meetings(self, user_id: int, room: Room) -> list[MeetingView]:
        """The account's meetings on this link that entries can join, by id: every live one, and
        every other non-finished one that entries manage (it has at least one entry row). An
        entry-less upstream-planned row is never returned (Ruling R15)."""
        ...

    async def meeting(self, meeting_id: int) -> MeetingView:
        """Raises ``LookupError`` when the meeting doesn't exist."""
        ...

    async def count_active_entries(self, user_id: int) -> int: ...

    async def create_meeting(
        self, user_id: int, room: Room, plan: Plan, *, join_now: bool
    ) -> MeetingView:
        """A new ``scheduled`` meeting with ``data.auto_join = true`` and the plan applied."""
        ...

    async def save_entry(
        self, user_id: int, entry: EntryIn, room: Room, meeting_id: int
    ) -> EntryView:
        """Insert or update the entry keyed by (``user_id``, ``entry.user``,
        ``entry.external_id``): its content, link and meeting, state ``active``."""
        ...

    async def mark_entry_removed(
        self, entry_id: int, reason: Optional[str]
    ) -> None: ...

    async def active_entries(self, meeting_id: int) -> list[EntryView]: ...

    async def apply_plan(self, meeting_id: int, room: Room, plan: Plan) -> None:
        """Write the plan: ``data.scheduled_at`` (start), ``data.title``,
        ``data.constructed_meeting_url``, ``scheduled_end_at``, ``time_zone`` and the link.
        """
        ...

    async def set_title(self, meeting_id: int, title: Optional[str]) -> None: ...

    async def record_spawn_error(
        self, meeting_id: int, code: Optional[str], message: Optional[str]
    ) -> None:
        """Store a spawn failure on ``meeting_aw_state`` (``last_error_code``,
        ``last_error_message``) without changing the meeting's status."""
        ...

    async def move_active_entries(
        self, from_meeting_id: int, to_meeting_id: int
    ) -> None: ...

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
        """``write_status`` in this transaction (§1.4)."""
        ...

    async def event(
        self,
        meeting_id: int,
        event_type: str,
        change: Optional[Mapping[str, Any]] = None,
    ) -> WrittenEvent:
        """``write_event`` in this transaction (§1.4)."""
        ...


class IntakeStore(Protocol):
    def room_lock(
        self, user_id: int, rooms: Sequence[Room]
    ) -> AsyncContextManager[IntakeTx]: ...


@dataclass(frozen=True)
class SpawnOutcome:
    """The spawn path's answer for one exact row (§1.5): ``code`` and ``message`` are set on
    ``failed`` (a §1.13 typed code and its exact message)."""

    result: Literal["sent", "already_live", "failed"]
    code: Optional[str] = None
    message: Optional[str] = None


class SpawnPort(Protocol):
    async def spawn_exact(self, user_id: int, meeting_id: int) -> SpawnOutcome: ...


class StopPort(Protocol):
    async def stop_live(
        self, user_id: int, meeting_id: int, *, outcome: Optional[Outcome]
    ) -> None:
        """Stop the bot in the call (§1.7), recording ``outcome`` on the meeting."""
        ...


class EventPublisher(Protocol):
    async def publish(self, event_ids: Sequence[str]) -> None: ...
