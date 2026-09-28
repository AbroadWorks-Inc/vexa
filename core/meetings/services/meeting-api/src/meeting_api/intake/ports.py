"""The entry service's ports (§1.3): storage, spawn, stop and event publishing.

The service talks to storage only through ``IntakeStore`` / ``IntakeTx``; ``fakes.py`` holds the
in-memory implementation and the Postgres adapter implements the same protocol. Spawning and
stopping go through ``SpawnPort`` / ``StopPort`` (the existing spawn and stop paths), and events
are handed to ``EventPublisher`` after the transaction that wrote them commits.

The ``/v2`` reads, erasure and the export result (§2.1, §1.13, §1.9) go through ``IntakeReads``: the
entries a sender holds for one user, the meetings a user may see, one meeting by UUID, the removal
of a finished meeting's entries, outbox and delivery rows, and the exporter's result on a finished
meeting. ``reads.py`` holds the Postgres implementation.

``IntakeStore.room_lock(user_id, rooms)`` opens one transaction holding the link lock of every
room given, taken in the order given (the caller passes them sorted); an empty ``rooms`` is a plain
transaction with no link lock. The transaction commits when the block exits normally and rolls
back when it raises; one that loses a race on a database constraint raises ``ConstraintRace``. ``IntakeStore.overdue_meetings(now)`` is the not-sent
sweep's read across every account (§1.5).

An entry is keyed by (``user_id``, ``source_user``, ``external_id``) and has at most one row that
isn't ``closed``. A ``closed`` row is history: it stays on the meeting it belonged to, a finished
one or a live one the entry moved away from (R7), and saving the entry again adds a new row.

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
    "ConstraintRace",
    "EntryView",
    "ErasedRows",
    "EventPublisher",
    "ExportReport",
    "IntakeReads",
    "IntakeStore",
    "IntakeTx",
    "MeetingQuery",
    "MeetingView",
    "RecordedStop",
    "Room",
    "SpawnOutcome",
    "SpawnPort",
    "StopPort",
]


class ConstraintRace(Exception):
    """The transaction lost a race on a database constraint (another write stored the same key
    first) and was rolled back, having written nothing: running it again reads that write.
    """


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
    closed_at: Optional[datetime] = None

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
            "closed_at": self.closed_at,
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
            closed_at=row.get("closed_at"),
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
    ) -> Optional[EntryView]:
        """The entry's row that isn't ``closed``, else its newest ``closed`` row."""
        ...

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
        """Store the entry keyed by (``user_id``, ``entry.user``, ``entry.external_id``): its
        content, link and meeting, state ``active``, on its row that isn't ``closed``, else on a
        new row (a closed row stays as history)."""
        ...

    async def mark_entry_removed(
        self, entry_id: int, reason: Optional[str]
    ) -> None: ...

    async def close_entry(self, entry_id: int) -> None:
        """The entry's row becomes ``closed`` (stamped ``closed_at``) on its meeting, as history:
        the entry moved away from that live meeting to a new time (R7)."""
        ...

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
        ``last_error_message``) without changing the meeting's status or counting a send: the
        failed send was a pasted entry's, not the meeting's own (Ruling R12)."""
        ...

    async def record_send_failure(
        self, meeting_id: int, code: str, message: str, *, retry_at: datetime
    ) -> int:
        """One failed send of the meeting's bot (§6.9 F-K), without changing its status: under the
        meeting row lock, then ``meeting_aw_state``, add 1 to ``send_attempts``, store the code and
        message (``last_error_code``, ``last_error_message``), and hold the next send until
        ``retry_at`` (``data.auto_join_next_retry``, with ``data.auto_join_error``). Returns the
        attempts made so far."""
        ...

    async def record_outcome(self, meeting_id: int, outcome: Outcome) -> None:
        """Set the outcome on ``meeting_aw_state`` (meeting row lock, then ``meeting_aw_state``)
        without changing the status: for a meeting another writer already finished (§1.5).
        """
        ...

    async def mark_stop_requested(
        self, meeting_id: int, outcome: Optional[Outcome]
    ) -> None:
        """Set ``data.stop_requested`` and, when given, the outcome (meeting row lock, then
        ``meeting_aw_state``) without changing the status: the stop of a bot still booting, which
        keeps the stage it reached (§1.7)."""
        ...

    async def mark_waiting_for_room(self, meeting_id: int) -> None:
        """Stamp ``meeting_aw_state.waiting_for_room_sent_at`` with the current time (meeting row
        lock, then ``meeting_aw_state``): the meeting's ``meeting.waiting_for_room`` went out
        (R2)."""
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

    async def overdue_meetings(self, now: datetime) -> list[MeetingView]:
        """Every account's ``scheduled`` meetings that entries manage and that are past their end
        at ``now`` (``rules.is_overdue``), by id: the not-sent sweep's candidates (§1.5). Read
        without a link lock; the sweep reads each one again under its link lock."""
        ...


@dataclass(frozen=True)
class SpawnOutcome:
    """The spawn path's answer for one exact row (§1.5): ``code`` and ``message`` are set on
    ``failed`` (a §1.13 typed code and its exact message). ``not_due`` answers only the scheduler,
    whose claim re-checks its due rule under the lock; nothing was claimed."""

    result: Literal["sent", "already_live", "not_due", "failed"]
    code: Optional[str] = None
    message: Optional[str] = None


class SpawnPort(Protocol):
    async def spawn_exact(self, user_id: int, meeting_id: int) -> SpawnOutcome: ...


@dataclass(frozen=True)
class RecordedStop:
    """A stop recorded under the meeting's link lock (§1.7 steps 1–4, ``stop.record_stop``): the
    meeting row as read before the write, the outcome recorded, and the events written.
    """

    meeting_id: int
    row: Mapping[str, Any]
    outcome: Optional[Outcome]
    events: tuple[str, ...]


class StopPort(Protocol):
    async def stop_live(
        self, user_id: int, meeting_id: int, *, outcome: Optional[Outcome]
    ) -> None:
        """Stop the bot in the call (§1.7), recording ``outcome`` on the meeting."""
        ...

    async def leave(self, user_id: int, stop: RecordedStop) -> None:
        """§1.7 steps 5–6 for a stop already recorded and committed: the leave command, and the
        workload delete while the bot is still booting."""
        ...


class EventPublisher(Protocol):
    async def publish(self, event_ids: Sequence[str]) -> None: ...


@dataclass(frozen=True)
class MeetingQuery:
    """``GET /v2/meetings`` (§2.1): the meetings ``user`` may see, newest meeting time first.

    ``start_from`` / ``start_to`` bound the meeting time (``meeting_event_time``: ``data.scheduled_at``,
    else ``start_time``, else ``created_at``) as naive UTC, from inclusive, to exclusive. ``after`` is
    the decoded cursor, ``(meeting time as naive UTC, meetings.id)``: only rows strictly after it in
    the order are returned. ``limit`` rows at most."""

    user: str
    start_from: Optional[datetime] = None
    start_to: Optional[datetime] = None
    status: Optional[str] = None
    external_id: Optional[str] = None
    after: Optional[tuple[datetime, int]] = None
    limit: int = 100


@dataclass(frozen=True)
class ErasedRows:
    """What ``IntakeReads.erase`` removed (§1.13)."""

    entries: int
    outbox: int
    deliveries: int


@dataclass(frozen=True)
class ExportReport:
    """The exporter's result for one meeting (§1.9): ``state`` is ``handed_off`` or ``failed``,
    ``s3_path`` the export folder, ``error`` why it failed."""

    state: Literal["handed_off", "failed"]
    s3_path: str
    error: Optional[str] = None


class IntakeReads(Protocol):
    async def entries(
        self, user_id: int, source_user: str, *, after: Optional[str], limit: int
    ) -> list[EntryView]:
        """The account's ``active`` entries for ``source_user``, by ``external_id``, only those
        after ``after``; ``limit`` rows at most."""
        ...

    async def meetings(self, user_id: int, query: MeetingQuery) -> list[MeetingView]:
        """The account's meetings ``query.user`` may see: a meeting with an entry of any state
        whose ``source_user`` is the user or whose ``attendees`` hold them (§2.1)."""
        ...

    async def meeting_by_uuid(self, user_id: int, uuid: str) -> Optional[MeetingView]:
        """The account's meeting with this UUID; ``None`` for another account's, an unknown one or
        a string that isn't a UUID."""
        ...

    async def visible_to(self, user_id: int, meeting_id: int, user: str) -> bool:
        """Whether ``user`` owns or is invited to the meeting through one of its entries."""
        ...

    async def erase(self, user_id: int, meeting_id: int) -> ErasedRows:
        """In one transaction, under the meeting's link lock and row lock: delete the delivery rows
        of the meeting's outbox events (their attempts cascade), the outbox rows, then the
        meeting's entries. The meeting row and ``meeting_aw_state`` stay. Raises
        ``IntakeError("meeting_not_finished")`` if the meeting isn't finished."""
        ...

    async def record_export(
        self, user_id: int, meeting_id: int, report: ExportReport
    ) -> Optional[str]:
        """In one transaction, under the meeting's link lock, its row lock and then its
        ``meeting_aw_state`` lock: store ``report`` on ``export_state`` / ``export_s3_path`` /
        ``export_error`` / ``export_at`` and write ``export.<state>`` through ``write_event``;
        returns that event's id. A report with the stored state and path writes nothing and
        returns ``None``. Raises ``IntakeError("meeting_not_found")`` for another account's
        meeting and ``IntakeError("meeting_not_finished")`` if the meeting isn't finished.
        """
        ...
