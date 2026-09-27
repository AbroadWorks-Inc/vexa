"""In-memory fakes for the entry service's ports (§1.3) — the ``collector/fakes.py`` pattern.

  * ``InMemoryIntakeStore`` — ``IntakeStore`` over dicts. ``room_lock`` checks the lock order
    (distinct rooms, sorted), records every acquisition in ``lock_log``, and gives a transaction
    that commits on a normal exit and rolls back when the block raises. ``room_meetings`` returns
    the link's live meetings and the non-finished ones that have an entry (Ruling R15). Its status
    and event writes behave as ``write_status`` / ``write_event`` (§1.4): the status change is
    conditional (``StatusConflict``, nothing written), each event adds 1 to the meeting's
    sequence, a finished status closes the active entries except the ones that re-run
    (``rules.is_rerun``, returned in ``rerun_entry_ids``), and every event is recorded in order in
    ``events`` with the meeting as projected at that moment. ``overdue_meetings`` applies
    ``rules.is_overdue`` to the ``scheduled`` meetings that have an entry, as the Postgres read does.
  * ``FakeSpawn`` — ``SpawnPort``: claims a ``scheduled`` row (status ``requested``), answers
    ``already_live`` for any other row, or returns the failure it was given: before the claim by
    default, after it with ``after_claim=True`` (the row then ends ``failed``, outcome
    ``not_sent``, as the real port leaves it).
  * ``FakeStop`` — ``StopPort``: ``stop_live`` moves a live meeting to ``stopping`` with the
    outcome given; ``leave`` records the stop it is handed (the entry service records R5's stop
    itself, in the removal's transaction).
  * ``FakePublisher`` — ``EventPublisher``: records each published batch.
  * ``InMemoryIntakeReads`` — ``IntakeReads`` over an ``InMemoryIntakeStore``: the same visibility,
    order and cursors as ``reads.PostgresIntakeReads``. The store has no delivery rows, so
    ``deliveries`` maps an event id to the number of delivery rows a test says it has; ``erase``
    removes those with the meeting's events.
  * ``link_rows_in(rows, user_id, room)`` — the twin of ``adapters.link_rows`` (§1.6): the link
    resolver's rows out of plain meeting dicts (``id``, ``user_id``, ``platform``,
    ``native_meeting_id``, ``status``, ``data``, ``start_time``, ``created_at`` and
    ``has_entries``), the shape the collector and bot-spawn fakes hold.

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
    Iterable,
    Mapping,
    Optional,
    Sequence,
)

from .ports import (
    EntryView,
    ErasedRows,
    MeetingQuery,
    MeetingView,
    RecordedStop,
    Room,
    SpawnOutcome,
)
from .projection import iso_utc
from .resolver import LinkRow
from .rules import (
    FINISHED_STATUSES,
    Plan,
    finished_window,
    is_live,
    is_overdue,
    is_rerun,
    meeting_start,
)
from .status import (
    Outcome,
    StatusConflict,
    WrittenEvent,
    check_event_data,
    derive_event_id_v2,
    typed_event,
)
from .validation import EntryIn, IntakeError

__all__ = [
    "FakePublisher",
    "FakeSpawn",
    "FakeStop",
    "InMemoryIntakeReads",
    "InMemoryIntakeStore",
    "RecordedEvent",
    "link_rows_in",
]


def link_rows_in(
    rows: Iterable[Mapping[str, Any]], user_id: int, room: Room
) -> list[LinkRow]:
    """The account's rows on the link as ``LinkRow`` (``adapters.link_rows``'s twin)."""
    return [
        LinkRow.of(
            row["id"],
            row["status"],
            row.get("data"),
            row.get("start_time"),
            row.get("created_at"),
            bool(row.get("has_entries")),
        )
        for row in rows
        if row.get("user_id") == user_id
        and row.get("platform") == room.platform
        and row.get("native_meeting_id") == room.native_meeting_id
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
    event_data: Optional[dict[str, Any]] = None


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

    async def overdue_meetings(
        self, now: datetime, *, open_ended_s: int
    ) -> list[MeetingView]:
        managed = {e.meeting_id for e in self.entries.values()}
        views = [
            self.view(mid)
            for mid, row in sorted(self.meetings.items())
            if row["status"] == "scheduled" and mid in managed
        ]
        return [
            v
            for v in views
            if is_overdue(v.start, v.end, now=now, open_ended_s=open_ended_s)
        ]

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
            "last_error_code": None,
            "last_error_message": None,
            "waiting_for_room_sent_at": None,
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
        event_data: Optional[Mapping[str, Any]] = None,
    ) -> WrittenEvent:
        check_event_data(event_data)
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
        typed = typed_event(to_status, self.aw[meeting_id].get("outcome_kind"))
        written = self.write_event(
            meeting_id, event_type or typed, change, event_data=event_data
        )
        return WrittenEvent(written.event_id, written.sequence, tuple(rerun))

    def write_event(
        self,
        meeting_id: int,
        event_type: str,
        change: Optional[Mapping[str, Any]] = None,
        *,
        event_data: Optional[Mapping[str, Any]] = None,
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
                event_data=dict(event_data) if event_data is not None else None,
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
        managed = {e.meeting_id for e in self._s.entries.values()}
        return [
            self._s.view(mid)
            for mid, row in sorted(self._s.meetings.items())
            if row["user_id"] == user_id
            and row["platform"] == room.platform
            and row["platform_specific_id"] == room.native_meeting_id
            and (
                is_live(row["status"])
                or (row["status"] not in FINISHED_STATUSES and mid in managed)
            )
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

    async def record_spawn_error(
        self, meeting_id: int, code: Optional[str], message: Optional[str]
    ) -> None:
        self._s.aw[meeting_id] = {
            **self._s.aw[meeting_id],
            "last_error_code": code,
            "last_error_message": message,
        }

    async def record_outcome(self, meeting_id: int, outcome: Outcome) -> None:
        self._s.aw[meeting_id] = {
            **self._s.aw[meeting_id],
            "outcome_kind": outcome.kind,
            "outcome_detail": outcome.detail,
            "outcome_message": outcome.message,
            "outcome_at": self._s._clock().replace(microsecond=0),
        }

    async def mark_stop_requested(
        self, meeting_id: int, outcome: Optional[Outcome]
    ) -> None:
        row = self._s.meetings[meeting_id]
        self._s.meetings[meeting_id] = {
            **row,
            "data": {**row["data"], "stop_requested": True},
        }
        if outcome is not None:
            await self.record_outcome(meeting_id, outcome)

    async def mark_waiting_for_room(self, meeting_id: int) -> None:
        self._s.aw[meeting_id] = {
            **self._s.aw[meeting_id],
            "waiting_for_room_sent_at": self._s._clock().replace(microsecond=0),
        }

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
        event_data: Optional[Mapping[str, Any]] = None,
    ) -> WrittenEvent:
        return self._s.write_status(
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
        return self._s.write_event(meeting_id, event_type, change)


class FakeSpawn:
    def __init__(
        self,
        store: InMemoryIntakeStore,
        *,
        failure: Optional[SpawnOutcome] = None,
        before: Optional[Callable[[int], None]] = None,
        after_claim: bool = False,
    ) -> None:
        self._store = store
        self.failure = failure
        self.before = before
        self.after_claim = after_claim
        self.calls: list[tuple[int, int]] = []

    async def spawn_exact(self, user_id: int, meeting_id: int) -> SpawnOutcome:
        self.calls.append((user_id, meeting_id))
        if self.before is not None:
            self.before(meeting_id)
        if self.failure is not None and self.after_claim:
            return self._fail_after_claim(meeting_id, self.failure)
        if self.failure is not None:
            return self.failure
        if self._store.meetings[meeting_id]["status"] != "scheduled":
            return SpawnOutcome("already_live")
        self._store.write_status(meeting_id, "requested", expected_from={"scheduled"})
        return SpawnOutcome("sent")

    def _fail_after_claim(self, meeting_id: int, failure: SpawnOutcome) -> SpawnOutcome:
        """The real port's post-claim failure: the row was claimed, then ends ``failed`` with
        outcome ``not_sent`` (``meeting.not_sent``)."""
        if self._store.meetings[meeting_id]["status"] != "scheduled":
            return SpawnOutcome("already_live")
        self._store.write_status(meeting_id, "requested", expected_from={"scheduled"})
        self._store.write_status(
            meeting_id,
            "failed",
            expected_from={"requested"},
            outcome=Outcome("not_sent", failure.code, failure.message),
            change_reason=failure.code,
            event_type="meeting.not_sent",
        )
        return failure


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

    async def leave(self, user_id: int, stop: RecordedStop) -> None:
        self.calls.append((user_id, stop.meeting_id, stop.outcome))


class FakePublisher:
    def __init__(self) -> None:
        self.batches: list[tuple[str, ...]] = []

    async def publish(self, event_ids: Sequence[str]) -> None:
        self.batches.append(tuple(event_ids))


class InMemoryIntakeReads:
    def __init__(self, store: InMemoryIntakeStore) -> None:
        self._s = store
        self.deliveries: dict[str, int] = {}

    async def entries(
        self, user_id: int, source_user: str, *, after: Optional[str], limit: int
    ) -> list[EntryView]:
        found = sorted(
            (
                e
                for e in self._s.entries.values()
                if e.user_id == user_id
                and e.source_user == source_user
                and e.state == "active"
                and (after is None or e.external_id > after)
            ),
            key=lambda e: e.external_id,
        )
        return found[:limit]

    def _visible(self, user_id: int, meeting_id: int, user: str) -> bool:
        return any(
            e.meeting_id == meeting_id
            and e.user_id == user_id
            and (e.source_user == user or user in e.attendees)
            for e in self._s.entries.values()
        )

    async def meetings(self, user_id: int, query: MeetingQuery) -> list[MeetingView]:
        from .reads import meeting_time

        rows = []
        for mid, row in self._s.meetings.items():
            if row["user_id"] != user_id or not self._visible(user_id, mid, query.user):
                continue
            key = (meeting_time(row), mid)
            if query.start_from is not None and key[0] < query.start_from:
                continue
            if query.start_to is not None and key[0] >= query.start_to:
                continue
            if query.status is not None and row["status"] != query.status:
                continue
            if query.external_id is not None and not any(
                e.meeting_id == mid
                and e.user_id == user_id
                and e.external_id == query.external_id
                for e in self._s.entries.values()
            ):
                continue
            if query.after is not None and not key < query.after:
                continue
            rows.append((key, mid))
        rows.sort(reverse=True)
        return [self._s.view(mid) for _, mid in rows[: query.limit]]

    async def meeting_by_uuid(self, user_id: int, uuid: str) -> Optional[MeetingView]:
        for mid, row in self._s.meetings.items():
            if row["uuid"] == uuid and row["user_id"] == user_id:
                return self._s.view(mid)
        return None

    async def visible_to(self, user_id: int, meeting_id: int, user: str) -> bool:
        return self._visible(user_id, meeting_id, user)

    async def erase(self, user_id: int, meeting_id: int) -> ErasedRows:
        row = self._s.meetings.get(meeting_id)
        if row is None or row["user_id"] != user_id:
            raise IntakeError("meeting_not_found", "no such meeting")
        if row["status"] not in FINISHED_STATUSES:
            raise IntakeError(
                "meeting_not_finished",
                "the meeting hasn't finished; remove its entries or stop it first",
            )
        events = [e for e in self._s.events if e.meeting_id == meeting_id]
        deliveries = sum(self.deliveries.pop(e.event_id, 0) for e in events)
        self._s.events[:] = [e for e in self._s.events if e.meeting_id != meeting_id]
        gone = [
            e
            for e in self._s.entries.values()
            if e.meeting_id == meeting_id and e.user_id == user_id
        ]
        for entry in gone:
            del self._s.entries[entry.id]
            del self._s._entry_keys[
                (entry.user_id, entry.source_user, entry.external_id)
            ]
        return ErasedRows(entries=len(gone), outbox=len(events), deliveries=deliveries)
