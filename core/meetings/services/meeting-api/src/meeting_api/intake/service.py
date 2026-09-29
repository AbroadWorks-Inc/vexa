"""The entry service (§1.3): the behaviour of ``PUT /v2/entries`` and ``POST /v2/entries/remove``.

Every write runs under the link locks of the entry's link and of its meeting's link (they differ
after a link change while live), plus the new link on an update, all sorted, through
``IntakeStore.room_lock``. The entry is read under the lock; if a link it needs isn't covered, the
transaction ends without writing and the service starts again once with those links added. A
remove doesn't know the links before reading, so it first reads without a link lock.
Events are published after the transaction commits (a failed publish is logged: the outbox holds
them). A transaction that loses a race on a database constraint (``ConstraintRace``: another write
stored the same key first) is rolled back and run again, up to ``INTAKE_CONFLICT_RETRIES`` more
times after a short random pause; if it still loses, the request fails with ``internal_error``
(500), logged with its stack (§6.9 F-D). A stop (R5) is recorded inside the transaction (``stop.record_stop``); the leave command
and spawns run after the commit, each through its own port.

``PUT`` (§1.3 steps 1–9):
  1. validate (``parse_entry``) and parse the link: unknown → ``unrecognized_link``, a host in
     ``ENTRY_BLOCKED_HOSTS`` → ``platform_not_enabled``;
  2. an active entry with the same ``content_hash`` → ``unchanged``, nothing written;
  3. a new entry, or a ``removed`` one coming back, takes the R1 path: it joins the meeting R1
     matches (``joined_existing``) or gets a new ``scheduled`` meeting (``created``). A ``closed``
     entry, even with the same ``content_hash``, does the same only if the update points to a new
     future time (R7); otherwise → ``not_changed_finished``;
  4. an entry of a live meeting that the update moves to a new future time (R7,
     ``rules.is_future_move``: from the later of now and the meeting's planned end) leaves it at
     once and takes the R1 path, with ``previous_meeting_id``. Its row stays on the live meeting,
     ``closed``, as history (the owner still sees the meeting, and its events list them), and the
     entry continues as a new row; the live meeting keeps its bot (a move is not a removal, R5).
     Any other update of a live meeting's entry is stored and the meeting is left as it is
     (``not_changed_live``);
  5. an entry of a scheduled meeting stays on it while it still overlaps the meeting's other
     entries; otherwise it joins the meeting R1 matches on its link, or the meeting follows it when
     it was the meeting's only entry, or it gets a new meeting (``updated``, with
     ``previous_meeting_id`` when it left a meeting). A meeting left with no entries is removed
     (R8, detail ``entry_moved``); one left with others is re-planned;
  6. a ``join_now`` entry whose meeting is still ``scheduled`` after the commit is spawned on
     that exact row: ``sent`` keeps the result, ``already_live`` → ``joined_existing``. A failure
     before the claim is one of the meeting's bounded sends (``send_failed``, §6.9 F-K): the
     meeting stays ``scheduled`` with the typed code and the scheduler tries again, and the last
     attempt ends it ``not_sent``. When other active entries share the meeting (it was adopted),
     only the pasted entry is removed instead (reason ``not_sent``), the failure is recorded on
     the meeting, which stays ``scheduled`` with its time back to the remaining entries
     (``joined_existing``, Ruling R12). A failure after the claim has already ended the meeting
     ``not_sent`` (Ruling R17): the reply keeps the result with that meeting.
Only a write that adds an active entry checks the quota (429 ``quota_exceeded``).

``remove``: the entry becomes ``removed``. Others remain → the meeting is re-planned
(``entry_removed``). It was the last one → a scheduled meeting ends ``failed`` with
``completion_reason: "stopped"`` and outcome ``cancelled_by_calendar`` (``removed``, R8); a live
one is stopped with that outcome (``bot_stopping``, R5), and one waiting for its next bot (§6.9
F-K2) ends the same way at once (``removed``). The status change is conditional, so a
meeting that went live meanwhile takes the live branch. When the meeting has already finished,
only the entry goes (``entry_removed``) and the finished meeting is left as history.

``merge_into_live`` is R2's exception and ``send_failed`` a failed bot send (§6.9 F-K); the auto-join
tick calls both, never a route.

Events: ``meeting.scheduled`` for a meeting an entry created, ``meeting.updated`` when a
meeting's time, link, title or entries change, ``meeting.removed`` (R8, R2), and
``meeting.not_sent`` for a failed instant join.
"""

from __future__ import annotations

import asyncio
import random
import traceback
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Mapping, Optional, Sequence
from urllib.parse import urlparse

from ..collector.meeting_link import parse_meeting_url
from ..obs import log_event
from . import retry
from .ports import (
    ConstraintRace,
    EntryView,
    EventPublisher,
    IntakeStore,
    IntakeTx,
    MeetingView,
    Room,
    SpawnOutcome,
    SpawnPort,
    RecordedStop,
    StopPort,
)
from .rules import (
    FINISHED_STATUSES,
    is_future_move,
    is_live,
    join_now_target,
    match_entry,
    overlaps,
    recompute,
)
from .settings import IntakeSettings
from .status import Outcome, StatusConflict, creation_change
from .stop import record_stop
from .validation import EntryIn, IntakeError, RemoveIn, parse_entry, parse_remove

__all__ = ["IntakeService", "is_merge_target"]

#: The sealed ``lifecycle.v1`` reason a removed meeting ends with (R5, R8).
STOPPED = "stopped"
CANCELLED_BY_CALENDAR = "cancelled_by_calendar"
ENTRY_MOVED = "entry_moved"
ENTRY_MOVED_MESSAGE = "last entry moved to another meeting"


#: The remove reply for each way ``_end_planned`` / the live branch ends (§2.4): the meeting was
#: removed; its bot is leaving; or it had already finished, so only the entry went.
_REMOVE_RESULT = {
    "removed": "removed",
    "stopping": "bot_stopping",
    "finished": "entry_removed",
}


#: The pause before running a write that lost a constraint race again, in seconds: a random point
#: in this range, times the try's number, so two racers don't meet again (§6.9 F-D).
_CONFLICT_DELAY_S = (0.01, 0.05)

#: Errors that mean a bug, not an outage: a failed leave command is logged, these are raised.
_PROGRAMMING_ERRORS = (TypeError, AttributeError, KeyError, AssertionError, NameError)


def is_merge_target(meeting: MeetingView) -> bool:
    """R2's exception: a live meeting whose bot is staying (not ``stopping``), open-ended, and
    holding an active ``join_now`` entry. The scheduler's link check and ``merge_into_live``'s
    re-check under the link lock both decide with this."""
    return (
        is_live(meeting.status)
        and meeting.status != "stopping"
        and meeting.end is None
        and any(e.join_now for e in meeting.active_entries())
    )


def _removed_message(reason: Optional[str]) -> str:
    return f"last entry removed: {reason}" if reason else "last entry removed"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class _Work:
    """One locked transaction's writes: the events to publish after it commits and the stops it
    recorded, whose bots are sent the leave command after that."""

    tx: IntakeTx
    events: list[str] = field(default_factory=list)
    stops: list[RecordedStop] = field(default_factory=list)

    async def event(
        self,
        meeting_id: int,
        event_type: str,
        change: Optional[Mapping[str, Any]] = None,
    ) -> None:
        written = await self.tx.event(meeting_id, event_type, change)
        self.events.append(written.event_id)

    async def status(self, meeting_id: int, to_status: str, **kwargs: Any) -> None:
        written = await self.tx.status(meeting_id, to_status, **kwargs)
        self.events.append(written.event_id)

    async def stop(self, meeting_id: int, outcome: Outcome) -> None:
        """R5: the meeting lost its last entry while live. Its stop is recorded here, in this
        transaction under the link lock (§1.7 steps 1–4, ``record_stop``), so the removal and the
        stop commit together; the bot is sent the leave command after the commit."""
        await self.event(meeting_id, "meeting.updated")
        recorded = await record_stop(self.tx, meeting_id, outcome)
        if recorded is not None:
            self.events.extend(recorded.events)
            self.stops.append(recorded)


@dataclass(frozen=True)
class _Done:
    result: str
    meeting: MeetingView
    previous: Optional[str] = None
    spawn: bool = False


class IntakeService:
    def __init__(
        self,
        store: IntakeStore,
        spawn: SpawnPort,
        stop: StopPort,
        publisher: EventPublisher,
        settings: IntakeSettings,
        *,
        clock: Callable[[], datetime] = _utcnow,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._store = store
        self._spawn = spawn
        self._stop = stop
        self._publisher = publisher
        self._settings = settings
        self._clock = clock
        self._sleep = sleep

    # ── routes ──────────────────────────────────────────────────────────────────────────────

    async def put_entry(self, user_id: int, body: Any) -> dict[str, Any]:
        now = self._clock()
        entry = parse_entry(body, now=now, max_days_ahead=self._settings.max_days_ahead)
        room = self._room(entry.meeting_url)

        async def read(tx: IntakeTx) -> Optional[EntryView]:
            return await tx.find_entry(user_id, entry.user, entry.external_id)

        async def body_(w: _Work, existing: Optional[EntryView]) -> _Done:
            return await self._put(w, user_id, entry, room, existing, now)

        done = await self._locked(user_id, frozenset({room}), read, body_, restarts=1)
        assert done is not None
        if done.spawn:
            done = await self._spawn_now(user_id, done, entry)
        return self._reply(done, entry.user, entry.external_id)

    async def remove_entry(self, user_id: int, body: Any) -> dict[str, Any]:
        req = parse_remove(body)

        async def read(tx: IntakeTx) -> Optional[EntryView]:
            return await tx.find_entry(user_id, req.user, req.external_id)

        async def body_(w: _Work, existing: Optional[EntryView]) -> _Done:
            return await self._remove(w, req, existing)

        done = await self._locked(user_id, frozenset(), read, body_, restarts=2)
        assert done is not None
        return self._reply(done, req.user, req.external_id)

    # ── scheduler / status-writer callers ───────────────────────────────────────────────────

    async def merge_into_live(
        self, user_id: int, due_meeting_id: int, live_meeting_id: int
    ) -> bool:
        """R2's exception: the due meeting's entries move onto the open-ended live meeting on its
        link, which takes the due meeting's title if it has none; the due meeting ends ``failed``
        with outcome ``merged_into_live`` (``meeting.removed``). Returns whether it merged: the
        rows are re-checked under the link lock, and nothing is written unless the due meeting
        is still ``scheduled`` and the other is on the same link and ``is_merge_target``.
        """
        due = await self._read(user_id, due_meeting_id)
        room = due.room
        async with self._store.room_lock(user_id, [room]) as tx:
            w = _Work(tx)
            due = await tx.meeting(due_meeting_id)
            live = await tx.meeting(live_meeting_id)
            if (
                due.status != "scheduled"
                or due.user_id != user_id
                or due.room != room
                or not is_merge_target(live)
                or live.room != room
                or live.user_id != user_id
            ):
                return False
            await tx.move_active_entries(due.id, live.id)
            if live.title is None and due.title is not None:
                await tx.set_title(live.id, due.title)
            await w.status(
                due.id,
                "failed",
                expected_from={"scheduled"},
                outcome=Outcome(
                    "merged_into_live",
                    live.uuid,
                    f"merged into live meeting {live.uuid}",
                ),
                event_type="meeting.removed",
                event_data={"merged_into": live.uuid},
            )
            await w.event(live.id, "meeting.updated")
        await self._publish(w.events)
        return True

    # ── the locked unit of work ─────────────────────────────────────────────────────────────

    async def _locked(
        self,
        user_id: int,
        base: frozenset[Room],
        read: Callable[[IntakeTx], Awaitable[Optional[EntryView]]],
        body: Callable[[_Work, Optional[EntryView]], Awaitable[Optional[_Done]]],
        *,
        restarts: int,
    ) -> Optional[_Done]:
        retries = self._settings.conflict_retries
        for attempt in range(retries + 1):
            try:
                work, done = await self._transact(
                    user_id, base, read, body, restarts=restarts
                )
                break
            except ConstraintRace as exc:
                if attempt == retries:
                    log_event(
                        "intake_conflict_unresolved",
                        audience="operator",
                        level="error",
                        span="meetings.intake",
                        user_id=user_id,
                        fields={
                            "constraint": str(exc),
                            "tries": attempt + 1,
                            "traceback": "".join(
                                traceback.format_exception(
                                    type(exc), exc, exc.__traceback__
                                )
                            ),
                        },
                    )
                    raise IntakeError(
                        "internal_error",
                        "the write kept conflicting with another; it was not stored",
                    ) from exc
                await self._sleep(random.uniform(*_CONFLICT_DELAY_S) * (attempt + 1))
        await self._publish(work.events)
        for stop in work.stops:
            await self._leave(user_id, stop)
        if work.stops and done is not None:
            done = replace(done, meeting=await self._read(user_id, done.meeting.id))
        return done

    async def _transact(
        self,
        user_id: int,
        base: frozenset[Room],
        read: Callable[[IntakeTx], Awaitable[Optional[EntryView]]],
        body: Callable[[_Work, Optional[EntryView]], Awaitable[Optional[_Done]]],
        *,
        restarts: int,
    ) -> tuple[_Work, Optional[_Done]]:
        """One run of the write: its transaction under the links it needs (restarted with the
        entry's links added when they weren't covered), committed."""
        rooms = base
        for _ in range(restarts + 1):
            async with self._store.room_lock(user_id, sorted(rooms)) as tx:
                existing = await read(tx)
                if existing is not None:
                    meeting = await tx.meeting(existing.meeting_id)
                    needed = {existing.room, meeting.room}
                    if not needed <= rooms:
                        rooms = base | needed
                        continue
                work = _Work(tx)
                done = await body(work, existing)
            return work, done
        raise IntakeError(
            "unavailable",
            "the entry's meeting link changed during the request; retry",
        )

    async def _leave(self, user_id: int, stop: RecordedStop) -> None:
        """§1.7 steps 5–6 for a stop this request recorded and committed. A failure is logged and
        doesn't fail the request: the stop is recorded, and the stale-stopping reconcile sweep
        ends the bot (a retried remove answers ``already_removed`` and sends nothing).
        """
        try:
            await self._stop.leave(user_id, stop)
        except _PROGRAMMING_ERRORS:
            raise
        except Exception as exc:
            log_event(
                "intake_stop_leave_failed",
                audience="operator",
                level="warning",
                span="meetings.intake",
                user_id=user_id,
                meeting_id=str(stop.meeting_id),
                fields={"error": type(exc).__name__},
            )

    async def _publish(self, event_ids: Sequence[str]) -> None:
        """Hand committed events to the publisher. A failure is logged and doesn't fail the
        request: the events are in the outbox, and the publisher picks up unpublished rows.
        """
        if not event_ids:
            return
        try:
            await self._publisher.publish(event_ids)
        except Exception as exc:
            log_event(
                "intake_publish_failed",
                audience="operator",
                level="warning",
                span="meetings.intake",
                fields={"events": len(event_ids), "error": type(exc).__name__},
            )

    async def _read(self, user_id: int, meeting_id: int) -> MeetingView:
        async with self._store.room_lock(user_id, ()) as tx:
            return await tx.meeting(meeting_id)

    # ── PUT ─────────────────────────────────────────────────────────────────────────────────

    async def _put(
        self,
        w: _Work,
        user_id: int,
        entry: EntryIn,
        room: Room,
        existing: Optional[EntryView],
        now: datetime,
    ) -> _Done:
        tx = w.tx
        if existing is None:
            await self._check_quota(tx, user_id)
            return await self._attach(w, user_id, entry, room, now, previous=None)
        old = await tx.meeting(existing.meeting_id)
        if existing.state == "active" and existing.content_hash == entry.content_hash:
            return _Done("unchanged", old)
        if existing.state == "removed":
            await self._check_quota(tx, user_id)
            return await self._attach(w, user_id, entry, room, now, previous=old)
        lead_s = self._settings.lead_s
        if existing.state == "closed" or old.status in FINISHED_STATUSES:
            if not is_future_move(entry, old, now=now, lead_s=lead_s):
                return _Done("not_changed_finished", old)
            await self._check_quota(tx, user_id)
            return await self._attach(w, user_id, entry, room, now, previous=old)
        if old.status != "scheduled":
            if is_future_move(entry, old, now=now, lead_s=lead_s):
                await tx.close_entry(existing.id)
                return await self._attach(w, user_id, entry, room, now, previous=old)
            await tx.save_entry(user_id, entry, room, old.id)
            return _Done("not_changed_live", await tx.meeting(old.id))
        return await self._move(w, user_id, entry, room, existing, old, now)

    async def _attach(
        self,
        w: _Work,
        user_id: int,
        entry: EntryIn,
        room: Room,
        now: datetime,
        *,
        previous: Optional[MeetingView],
    ) -> _Done:
        """The R1 path: join the meeting R1 matches on the link, or create one."""
        target = self._target(entry, await w.tx.room_meetings(user_id, room), now)
        if target is None:
            meeting_id = await self._create(w, user_id, entry, room)
            result = "created"
        else:
            await self._join(w, user_id, entry, room, target)
            meeting_id, result = target.id, "joined_existing"
        final = await w.tx.meeting(meeting_id)
        moved_from = previous.uuid if previous and previous.id != final.id else None
        spawn = entry.join_now and final.status == "scheduled"
        return _Done(result, final, moved_from, spawn)

    async def _move(
        self,
        w: _Work,
        user_id: int,
        entry: EntryIn,
        room: Room,
        existing: EntryView,
        old: MeetingView,
        now: datetime,
    ) -> _Done:
        """An active entry of a scheduled meeting changed (§1.3 step 5)."""
        tx = w.tx
        others = [e for e in await tx.active_entries(old.id) if e.id != existing.id]
        if others and room == old.room:
            kept = recompute(others)
            if overlaps(entry.start, entry.end, kept.start, kept.end):
                return await self._update_in_place(w, user_id, entry, room, old)
        candidates = [
            m for m in await tx.room_meetings(user_id, room) if m.id != old.id
        ]
        target = self._target(entry, candidates, now)
        if target is None and not others:
            return await self._update_in_place(w, user_id, entry, room, old)
        if target is None:
            meeting_id = await self._create(w, user_id, entry, room)
        else:
            await self._join(w, user_id, entry, room, target)
            meeting_id = target.id
        if others:
            await self._replan(tx, old.id, old.room)
            await w.event(old.id, "meeting.updated")
        else:
            await self._end_planned(
                w, old, Outcome(CANCELLED_BY_CALENDAR, ENTRY_MOVED, ENTRY_MOVED_MESSAGE)
            )
        final = await tx.meeting(meeting_id)
        return _Done(
            "updated", final, old.uuid, entry.join_now and final.status == "scheduled"
        )

    async def _update_in_place(
        self, w: _Work, user_id: int, entry: EntryIn, room: Room, meeting: MeetingView
    ) -> _Done:
        """The entry stays on its meeting, which is re-planned (onto the entry's link when it was
        the only entry and the link changed)."""
        await w.tx.save_entry(user_id, entry, room, meeting.id)
        await self._replan(w.tx, meeting.id, room)
        await w.event(meeting.id, "meeting.updated")
        final = await w.tx.meeting(meeting.id)
        return _Done(
            "updated", final, None, entry.join_now and final.status == "scheduled"
        )

    async def _create(self, w: _Work, user_id: int, entry: EntryIn, room: Room) -> int:
        meeting = await w.tx.create_meeting(
            user_id, room, recompute([entry]), join_now=entry.join_now
        )
        await w.tx.save_entry(user_id, entry, room, meeting.id)
        await w.event(
            meeting.id, "meeting.scheduled", creation_change("scheduled", self._clock())
        )
        return meeting.id

    async def _join(
        self, w: _Work, user_id: int, entry: EntryIn, room: Room, target: MeetingView
    ) -> None:
        """Attach the entry; a live meeting's time is never recomputed (R1)."""
        await w.tx.save_entry(user_id, entry, room, target.id)
        if target.status == "scheduled":
            await self._replan(w.tx, target.id, target.room)
        await w.event(target.id, "meeting.updated")

    def _target(
        self, entry: EntryIn, candidates: Sequence[MeetingView], now: datetime
    ) -> Optional[MeetingView]:
        if entry.join_now:
            return join_now_target(
                candidates, now=now, adopt_ahead_s=self._settings.join_now_adopt_ahead_s
            )
        return match_entry(entry, candidates, now=now, lead_s=self._settings.lead_s)

    async def _spawn_now(self, user_id: int, done: _Done, entry: EntryIn) -> _Done:
        """§1.3 ``join_now``: spawn the exact row after the commit."""
        outcome = await self._spawn.spawn_exact(user_id, done.meeting.id)
        result = done.result
        if outcome.result == "already_live":
            result = "joined_existing"
        elif outcome.result == "failed":
            result = await self._spawn_failed(user_id, done, entry, outcome)
        return replace(
            done, result=result, meeting=await self._read(user_id, done.meeting.id)
        )

    async def _spawn_failed(
        self, user_id: int, done: _Done, entry: EntryIn, outcome: SpawnOutcome
    ) -> str:
        """A real spawn failure for a ``join_now`` entry.

        A failure BEFORE the claim leaves the meeting ``scheduled``. When other active entries
        share it (an adopted meeting, Ruling R12) only the pasted entry goes (``removed``, reason
        ``not_sent``), the failure is recorded on the meeting, its time returns to the remaining
        entries, and it stays ``scheduled`` for them (``joined_existing``). Otherwise the failure
        is one of the meeting's bounded sends (``_send_failed``): the scheduler tries again after
        the backoff, and the last attempt ends the meeting ``not_sent``; the reply keeps
        ``done.result``.

        A failure AFTER the claim finds the meeting finished, or waiting for another bot: the spawn
        port already ended it ``not_sent`` (Ruling R17) or sent it back to ``requested`` (§6.9
        F-K2), and the reply is ``done.result`` with that meeting (``created`` for a new one,
        ``joined_existing`` for an adopted one). A meeting that is live got its bot from the
        scheduler meanwhile (``joined_existing``)."""
        meeting = done.meeting
        code = outcome.code or "internal_error"
        message = outcome.message or "the bot was not sent"
        async with self._store.room_lock(user_id, [meeting.room]) as tx:
            w = _Work(tx)
            mine = await tx.find_entry(user_id, entry.user, entry.external_id)
            others = [
                e
                for e in await tx.active_entries(meeting.id)
                if mine is None or e.id != mine.id
            ]
            current = await tx.meeting(meeting.id)
            if current.status in FINISHED_STATUSES or retry.marker(current.data) is not None:
                # The spawn claimed the row and failed after the claim: the port ended the meeting
                # not_sent itself, or sent it back for another bot (§6.9 F-K2). The reply is the
                # meeting as it stands.
                return done.result
            if current.status != "scheduled":
                return "joined_existing"
            if others and mine is not None:
                await tx.mark_entry_removed(mine.id, "not_sent")
                await tx.record_spawn_error(meeting.id, code, message)
                await self._replan(tx, meeting.id, current.room)
                await w.event(meeting.id, "meeting.updated")
                result = "joined_existing"
            else:
                await self._send_failed(w, meeting.id, code, message, now=self._clock())
                result = done.result
        await self._publish(w.events)
        return result

    async def send_failed(
        self, user_id: int, meeting_id: int, code: str, message: str, *, now: datetime
    ) -> bool:
        """§6.9 F-K: one failed send of an entry-managed meeting's bot, under its link lock. The
        attempt is counted on the meeting (``send_attempts``, so every replica and tick shares the
        count) with the typed code and exact message, and the next send waits
        ``BOT_SEND_RETRY_BACKOFF_S``; the ``BOT_SEND_MAX_ATTEMPTS``-th failure ends the meeting
        ``failed``, outcome ``not_sent``, with that code and message (``meeting.not_sent``).
        Returns whether it ended. A meeting no longer ``scheduled`` is left alone."""
        room = (await self._read(user_id, meeting_id)).room
        async with self._store.room_lock(user_id, [room]) as tx:
            w = _Work(tx)
            current = await tx.meeting(meeting_id)
            if current.status != "scheduled" or current.room != room:
                return False
            ended = await self._send_failed(w, meeting_id, code, message, now=now)
        await self._publish(w.events)
        return ended

    async def _send_failed(
        self, w: _Work, meeting_id: int, code: str, message: str, *, now: datetime
    ) -> bool:
        retry_at = now + timedelta(seconds=self._settings.send_retry_backoff_s)
        attempts = await w.tx.record_send_failure(
            meeting_id, code, message, retry_at=retry_at
        )
        if attempts < self._settings.send_max_attempts:
            return False
        await w.status(
            meeting_id,
            "failed",
            expected_from={"scheduled"},
            outcome=Outcome("not_sent", code, message),
            change_reason=code,
            event_type="meeting.not_sent",
        )
        return True

    async def _check_quota(self, tx: IntakeTx, user_id: int) -> None:
        limit = self._settings.max_active_entries
        count = await tx.count_active_entries(user_id)
        if count >= limit:
            raise IntakeError(
                "quota_exceeded", f"active entry quota reached ({count} of {limit})"
            )

    def _room(self, meeting_url: str) -> Room:
        parsed = parse_meeting_url(meeting_url)
        if parsed is None:
            raise IntakeError(
                "unrecognized_link",
                "meeting_url is not a meeting link aw-bots recognises",
            )
        host = (urlparse(meeting_url.strip()).hostname or "").lower().rstrip(".")
        if host in self._settings.blocked_hosts:
            raise IntakeError(
                "platform_not_enabled", f"meeting links on {host} are not enabled"
            )
        return Room(*parsed)

    # ── remove ──────────────────────────────────────────────────────────────────────────────

    async def _remove(
        self, w: _Work, req: RemoveIn, existing: Optional[EntryView]
    ) -> _Done:
        if existing is None:
            raise IntakeError(
                "entry_not_found", "no entry with this external_id for this user"
            )
        tx = w.tx
        meeting = await tx.meeting(existing.meeting_id)
        if existing.state != "active":
            return _Done("already_removed", meeting)
        await tx.mark_entry_removed(existing.id, req.reason)
        if meeting.status in FINISHED_STATUSES:
            return _Done("entry_removed", await tx.meeting(meeting.id))
        if await tx.active_entries(meeting.id):
            if meeting.status == "scheduled":
                await self._replan(tx, meeting.id, meeting.room)
            await w.event(meeting.id, "meeting.updated")
            return _Done("entry_removed", await tx.meeting(meeting.id))
        outcome = Outcome(
            CANCELLED_BY_CALENDAR, req.reason, _removed_message(req.reason)
        )
        if meeting.status == "scheduled":
            ended = await self._end_planned(w, meeting, outcome)
        else:
            await w.stop(meeting.id, outcome)
            # A meeting waiting for its next bot (§6.9 F-K2) ends at once: it is removed.
            finished = (await tx.meeting(meeting.id)).status in FINISHED_STATUSES
            ended = "removed" if finished else "stopping"
        return _Done(_REMOVE_RESULT[ended], await tx.meeting(meeting.id))

    # ── shared ──────────────────────────────────────────────────────────────────────────────

    async def _replan(self, tx: IntakeTx, meeting_id: int, room: Room) -> None:
        await tx.apply_plan(
            meeting_id, room, recompute(await tx.active_entries(meeting_id))
        )

    async def _end_planned(
        self, w: _Work, meeting: MeetingView, outcome: Outcome
    ) -> str:
        """R8: a scheduled meeting with no entries left ends ``failed``, ``stopped``, with the
        outcome (``meeting.removed``). Conditional: ``"removed"``; ``"stopping"`` when it went
        live meanwhile (its stop is recorded in this transaction, R5); ``"finished"`` when it ended
        meanwhile."""
        try:
            await w.status(
                meeting.id,
                "failed",
                expected_from={"scheduled"},
                data_patch={"completion_reason": STOPPED},
                outcome=outcome,
                change_reason=STOPPED,
                event_type="meeting.removed",
            )
            return "removed"
        except StatusConflict:
            current = await w.tx.meeting(meeting.id)
            if is_live(current.status):
                await w.stop(meeting.id, outcome)
                return "stopping"
            return "finished"

    def _reply(self, done: _Done, user: str, external_id: str) -> dict[str, Any]:
        rows = [
            e
            for e in done.meeting.entries
            if (e.source_user, e.external_id) == (user, external_id)
        ]
        current = [e for e in rows if e.state != "closed"]
        found = current or rows
        if not found:
            raise LookupError(
                f"entry {external_id!r} of {user!r} is not on meeting {done.meeting.uuid}"
            )
        entry = found[-1]
        return {
            "result": done.result,
            "previous_meeting_id": done.previous,
            "entry": {
                "external_id": external_id,
                "user": user,
                "state": entry.state,
            },
            "meeting": done.meeting.project(lead_s=self._settings.lead_s),
        }
