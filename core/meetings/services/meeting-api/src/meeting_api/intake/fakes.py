"""In-memory fakes for the entry service's ports (§1.3) — the ``collector/fakes.py`` pattern.

  * ``InMemoryIntakeStore`` — ``IntakeStore`` over dicts. ``room_lock`` checks the lock order
    (distinct rooms, sorted), records every acquisition in ``lock_log``, and gives a transaction
    that commits on a normal exit and rolls back when the block raises. Its status and event
    writes behave as ``write_status`` / ``write_event`` (§1.4): the status change is conditional
    (``StatusConflict``, nothing written), each event adds 1 to the meeting's sequence, a finished
    status closes the active entries except the ones that re-run (``rules.is_rerun``, returned in
    ``rerun_entry_ids``), and every event is recorded in order in ``events`` with the meeting as
    projected at that moment.
  * ``FakeSpawn`` — ``SpawnPort``: claims a ``scheduled`` row (status ``requested``), answers
    ``already_live`` for any other row, or returns the failure it was given.
  * ``FakeStop`` — ``StopPort``: moves a live meeting to ``stopping`` with the outcome given.
  * ``FakePublisher`` — ``EventPublisher``: records each published batch.

Rows are replaced, never mutated, so a transaction's rollback restores a shallow copy.
"""

from __future__ import annotations

import itertools
import uuid as uuid_mod
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from typing import (
    Any,
    AsyncIterator,
    Callable,
    Collection,
    Mapping,
    Optional,
    Sequence,
)

from .ports import EntryView, MeetingView, Room, SpawnOutcome
from .projection import iso_utc
from .rules import (
    FINISHED_STATUSES,
    Plan,
    finished_window,
    is_live,
    is_rerun,
    meeting_start,
)
from .status import (
    STATUS_CHANGE_EVENT,
    Outcome,
    StatusConflict,
    WrittenEvent,
    derive_event_id_v2,
)
from .validation import EntryIn

__all__ = [
    "FakePublisher",
    "FakeSpawn",
    "FakeStop",
    "InMemoryIntakeStore",
    "RecordedEvent",
]


@dataclass(frozen=True)
class RecordedEvent:
    event_id: str
    meeting_id: int
    meeting_uuid: str
    event_type: str
    sequence: int
    change: Optional[dict[str, Any]]
    meeting: dict[str, Any]


class InMemoryIntakeStore:
    def __init__(self, *, clock: Callable[[], datetime], lead_s: int) -> None:
        self._clock = clock
        self._lead_s = lead_s
        self.meetings: dict[int, dict[str, Any]] = {}
        self.aw: dict[int, dict[str, Any]] = {}
        self.entries: dict[int, EntryView] = {}
        self._entry_keys: dict[tuple[int, str, str], int] = {}
        self.events: list[RecordedEvent] = []
        self.lock_log: list[tuple[int, tuple[Room, ...]]] = []
        self.on_lock: Optional[
            Callable[["InMemoryIntakeStore", tuple[Room, ...]], None]
        ] = None
        self._meeting_ids = itertools.count(1)
        self._entry_ids = itertools.count(1)

    # ── transactions ────────────────────────────────────────────────────────────────────────

    @asynccontextmanager
    async def room_lock(
        self, user_id: int, rooms: Sequence[Room]
    ) -> AsyncIterator["_FakeTx"]:
        ordered = tuple(rooms)
        if list(ordered) != sorted(set(ordered)):
            raise ValueError("link locks must be distinct and taken in sorted order")
        self.lock_log.append((user_id, ordered))
        if self.on_lock is not None:
            self.on_lock(self, ordered)
        meetings, aw, entries = dict(self.meetings), dict(self.aw), dict(self.entries)
        keys, events = dict(self._entry_keys), len(self.events)
        try:
            yield _FakeTx(self)
        except BaseException:
            self.meetings, self.aw, self.entries = meetings, aw, entries
            self._entry_keys = keys
            del self.events[events:]
            raise

    # ── reads ───────────────────────────────────────────────────────────────────────────────

    def view(self, meeting_id: int) -> MeetingView:
        row = self.meetings.get(meeting_id)
        if row is None:
            raise LookupError(f"meeting {meeting_id} not found")
        entries = tuple(
            e for _, e in sorted(self.entries.items()) if e.meeting_id == meeting_id
        )
        return MeetingView(row=row, aw=self.aw.get(meeting_id), entries=entries)

    def find_entry(
        self, user_id: int, source_user: str, external_id: str
    ) -> Optional[EntryView]:
        entry_id = self._entry_keys.get((user_id, source_user, external_id))
        return None if entry_id is None else self.entries[entry_id]

    def entries_of(self, meeting_id: int, state: str = "active") -> list[EntryView]:
        return [
            e
            for _, e in sorted(self.entries.items())
            if e.meeting_id == meeting_id and e.state == state
        ]

    # ── writes ──────────────────────────────────────────────────────────────────────────────

    def seed_meeting(
        self,
        user_id: int,
        room: Room,
        *,
        status: str,
        plan: Optional[Plan] = None,
        entries: Sequence[tuple[EntryIn, str]] = (),
    ) -> int:
        """Plant a meeting (``plan`` gives its time; none for an entry-less upstream row) and its
        entries, each with its state. Records no event."""
        meeting_id = next(self._meeting_ids)
        self.meetings[meeting_id] = {
            "id": meeting_id,
            "uuid": str(uuid_mod.uuid4()),
            "user_id": user_id,
            "platform": room.platform,
            "platform_specific_id": room.native_meeting_id,
            "status": status,
            "data": {},
            "start_time": None,
            "end_time": None,
            "created_at": self._clock().replace(microsecond=0),
        }
        self.aw[meeting_id] = {
            "meeting_id": meeting_id,
            "scheduled_end_at": None,
            "time_zone": None,
            "event_seq": 0,
            "outcome_kind": None,
            "outcome_detail": None,
            "outcome_message": None,
            "outcome_at": None,
        }
        if plan is not None:
            self.apply_plan(meeting_id, room, plan)
        for entry, state in entries:
            saved = self.save_entry(user_id, entry, room, meeting_id)
            self.entries[saved.id] = replace(saved, state=state)
        return meeting_id

    def apply_plan(self, meeting_id: int, room: Room, plan: Plan) -> None:
        row = self.meetings[meeting_id]
        self.meetings[meeting_id] = {
            **row,
            "platform": room.platform,
            "platform_specific_id": room.native_meeting_id,
            "data": {
                **row["data"],
                "scheduled_at": iso_utc(plan.start),
                "title": plan.title,
                "constructed_meeting_url": plan.meeting_url,
            },
        }
        self.aw[meeting_id] = {
            **self.aw[meeting_id],
            "scheduled_end_at": plan.end,
            "time_zone": plan.time_zone,
        }

    def save_entry(
        self, user_id: int, entry: EntryIn, room: Room, meeting_id: int
    ) -> EntryView:
        existing = self.find_entry(user_id, entry.user, entry.external_id)
        saved = EntryView(
            id=existing.id if existing is not None else next(self._entry_ids),
            user_id=user_id,
            source_user=entry.user,
            external_id=entry.external_id,
            meeting_id=meeting_id,
            meeting_url=entry.meeting_url,
            platform=room.platform,
            native_meeting_id=room.native_meeting_id,
            title=entry.title,
            start=entry.start,
            end=entry.end,
            time_zone=entry.time_zone,
            series_id=entry.series_id,
            attendees=entry.attendees,
            join_now=entry.join_now,
            metadata=entry.metadata,
            content_hash=entry.content_hash,
            state="active",
        )
        self.entries[saved.id] = saved
        self._entry_keys[(user_id, entry.user, entry.external_id)] = saved.id
        return saved

    def write_status(
        self,
        meeting_id: int,
        to_status: str,
        *,
        expected_from: Collection[str],
        data_patch: Optional[Mapping[str, Any]] = None,
        outcome: Optional[Outcome] = None,
        change_reason: Optional[str] = None,
        event_type: Optional[str] = None,
    ) -> WrittenEvent:
        now = self._clock()
        row = self.meetings.get(meeting_id)
        if row is None or row["status"] not in expected_from:
            raise StatusConflict(
                meeting_id, None if row is None else row["status"], expected_from
            )
        from_status = row["status"]
        row = {
            **row,
            "status": to_status,
            "data": {**row["data"], **(data_patch or {})},
        }
        self.meetings[meeting_id] = row
        if outcome is not None:
            self.aw[meeting_id] = {
                **self.aw[meeting_id],
                "outcome_kind": outcome.kind,
                "outcome_detail": outcome.detail,
                "outcome_message": outcome.message,
                "outcome_at": now.replace(microsecond=0),
            }
        rerun: list[int] = []
        if to_status in FINISHED_STATUSES:
            start = meeting_start(row["data"], row["start_time"], row["created_at"])
            window = finished_window(start, finish=now)
            for entry in self.entries_of(meeting_id):
                if is_rerun(entry.start, entry.end, window, finish=now):
                    rerun.append(entry.id)
                else:
                    self.entries[entry.id] = replace(entry, state="closed")
        change = {
            "from": from_status,
            "to": to_status,
            "reason": change_reason,
            "at": iso_utc(now.replace(microsecond=0)),
        }
        written = self.write_event(
            meeting_id, event_type or STATUS_CHANGE_EVENT, change
        )
        return WrittenEvent(written.event_id, written.sequence, tuple(rerun))

    def write_event(
        self,
        meeting_id: int,
        event_type: str,
        change: Optional[Mapping[str, Any]] = None,
    ) -> WrittenEvent:
        if meeting_id not in self.meetings:
            raise LookupError(f"meeting {meeting_id} not found")
        sequence = int(self.aw[meeting_id]["event_seq"]) + 1
        self.aw[meeting_id] = {**self.aw[meeting_id], "event_seq": sequence}
        view = self.view(meeting_id)
        event_id = derive_event_id_v2(view.uuid, event_type, sequence)
        self.events.append(
            RecordedEvent(
                event_id=event_id,
                meeting_id=meeting_id,
                meeting_uuid=view.uuid,
                event_type=event_type,
                sequence=sequence,
                change=dict(change) if change is not None else None,
                meeting=view.project(lead_s=self._lead_s),
            )
        )
        return WrittenEvent(event_id, sequence, ())


class _FakeTx:
    def __init__(self, store: InMemoryIntakeStore) -> None:
        self._s = store

    async def find_entry(
        self, user_id: int, source_user: str, external_id: str
    ) -> Optional[EntryView]:
        return self._s.find_entry(user_id, source_user, external_id)

    async def entry(self, entry_id: int) -> Optional[EntryView]:
        return self._s.entries.get(entry_id)

    async def room_meetings(self, user_id: int, room: Room) -> list[MeetingView]:
        return [
            self._s.view(mid)
            for mid, row in sorted(self._s.meetings.items())
            if row["user_id"] == user_id
            and row["platform"] == room.platform
            and row["platform_specific_id"] == room.native_meeting_id
            and row["status"] not in FINISHED_STATUSES
        ]

    async def meeting(self, meeting_id: int) -> MeetingView:
        return self._s.view(meeting_id)

    async def count_active_entries(self, user_id: int) -> int:
        return sum(
            1
            for e in self._s.entries.values()
            if e.user_id == user_id and e.state == "active"
        )

    async def create_meeting(
        self, user_id: int, room: Room, plan: Plan, *, join_now: bool
    ) -> MeetingView:
        meeting_id = self._s.seed_meeting(user_id, room, status="scheduled", plan=plan)
        row = self._s.meetings[meeting_id]
        self._s.meetings[meeting_id] = {
            **row,
            "data": {**row["data"], "auto_join": True},
        }
        return self._s.view(meeting_id)

    async def save_entry(
        self, user_id: int, entry: EntryIn, room: Room, meeting_id: int
    ) -> EntryView:
        return self._s.save_entry(user_id, entry, room, meeting_id)

    async def mark_entry_removed(self, entry_id: int, reason: Optional[str]) -> None:
        entry = self._s.entries[entry_id]
        self._s.entries[entry_id] = replace(
            entry, state="removed", removed_reason=reason
        )

    async def active_entries(self, meeting_id: int) -> list[EntryView]:
        return self._s.entries_of(meeting_id)

    async def apply_plan(self, meeting_id: int, room: Room, plan: Plan) -> None:
        self._s.apply_plan(meeting_id, room, plan)

    async def set_title(self, meeting_id: int, title: Optional[str]) -> None:
        row = self._s.meetings[meeting_id]
        self._s.meetings[meeting_id] = {**row, "data": {**row["data"], "title": title}}

    async def move_active_entries(
        self, from_meeting_id: int, to_meeting_id: int
    ) -> None:
        for entry in self._s.entries_of(from_meeting_id):
            self._s.entries[entry.id] = replace(entry, meeting_id=to_meeting_id)

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
    ) -> WrittenEvent:
        return self._s.write_status(
            meeting_id,
            to_status,
            expected_from=expected_from,
            data_patch=data_patch,
            outcome=outcome,
            change_reason=change_reason,
            event_type=event_type,
        )

    async def event(
        self,
        meeting_id: int,
        event_type: str,
        change: Optional[Mapping[str, Any]] = None,
    ) -> WrittenEvent:
        return self._s.write_event(meeting_id, event_type, change)


class FakeSpawn:
    def __init__(
        self,
        store: InMemoryIntakeStore,
        *,
        failure: Optional[SpawnOutcome] = None,
        before: Optional[Callable[[int], None]] = None,
    ) -> None:
        self._store = store
        self.failure = failure
        self.before = before
        self.calls: list[tuple[int, int]] = []

    async def spawn_exact(self, user_id: int, meeting_id: int) -> SpawnOutcome:
        self.calls.append((user_id, meeting_id))
        if self.before is not None:
            self.before(meeting_id)
        if self.failure is not None:
            return self.failure
        if self._store.meetings[meeting_id]["status"] != "scheduled":
            return SpawnOutcome("already_live")
        self._store.write_status(meeting_id, "requested", expected_from={"scheduled"})
        return SpawnOutcome("sent")


class FakeStop:
    def __init__(self, store: InMemoryIntakeStore) -> None:
        self._store = store
        self.calls: list[tuple[int, int, Optional[Outcome]]] = []

    async def stop_live(
        self, user_id: int, meeting_id: int, *, outcome: Optional[Outcome]
    ) -> None:
        self.calls.append((user_id, meeting_id, outcome))
        status = self._store.meetings[meeting_id]["status"]
        if is_live(status) and status != "stopping":
            self._store.write_status(
                meeting_id, "stopping", expected_from={status}, outcome=outcome
            )


class FakePublisher:
    def __init__(self) -> None:
        self.batches: list[tuple[str, ...]] = []

    async def publish(self, event_ids: Sequence[str]) -> None:
        self.batches.append(tuple(event_ids))
