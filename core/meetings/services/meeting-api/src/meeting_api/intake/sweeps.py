"""The scheduler's intake side (§1.5, R2, R6): the link check the auto-join tick makes before it
sends a bot to an entry-managed meeting, and the not-sent sweep.

``check_room(store, user_id, meeting_id, room, *, publisher)`` reads the meeting and its link again
under the link lock and answers one of:

  * ``gone`` — the meeting is no longer ``scheduled``, or no longer on ``room``: nothing to decide
    this tick;
  * ``free`` — no other meeting on the link is live: send the bot;
  * ``merge`` (``live_id``) — the live meeting is a ``join_now`` meeting whose bot is staying
    (``service.is_merge_target``): R2's exception, ``IntakeService.merge_into_live``;
  * ``waiting`` — another bot holds the link, a leaving (``stopping``) one included. The meeting
    stays ``scheduled``; the first time,
    ``meeting_aw_state.waiting_for_room_sent_at`` is stamped and ``meeting.waiting_for_room`` goes
    out. Nothing else is written (no retry stamp), so the bot goes on the first tick after the link
    is free.

``not_sent_tick(store, *, publisher, now, failures)`` is R6's backstop. Every
entry-managed meeting past its end without a bot (``IntakeStore.overdue_meetings``) is read again
under its link lock and, still ``scheduled`` and overdue (``rules.is_overdue``), ends ``failed``
with outcome ``not_sent`` through the status writer (``meeting.not_sent``). An open-ended meeting
has no end to pass; its bounded sends end it (§6.9 F-K). ``not_sent_cause`` gives the
detail and message:

  * ``meeting_aw_state.last_error_code`` when a spawn failed, with ``last_error_message`` (else
    ``"the bot was not sent (<code>)"``);
  * else ``room_busy`` when ``meeting.waiting_for_room`` went out;
  * else ``ended_before_sent``.

The overdue read is paged (``SWEEP_BATCH_SIZE``, id order) and every page is worked in the tick.
Each meeting runs through ``sweeps.item_failures.run_item`` (§6.9 F-I): one meeting's failure is
logged with its id and stack, counted, and never stops the rest; after ``SWEEP_MAX_ITEM_FAILURES``
the sweep gives it up and skips it.

``OutboxOnly`` is the ``EventPublisher`` of the production ``IntakeService`` the scheduler merges
through (and the ``/v2`` routes use): events stay in ``webhook_outbox`` for the outbox
publisher (§1.8).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Mapping, Optional, Sequence

from ..obs import log_event
from ..sweeps.item_failures import ItemFailures, run_item, sweep_batch_size
from .ports import EventPublisher, IntakeStore, MeetingView, Room
from .rules import is_live, is_overdue
from .service import is_merge_target
from .status import Outcome

__all__ = [
    "NOT_SENT_MESSAGES",
    "OutboxOnly",
    "RoomCheck",
    "check_room",
    "not_sent_cause",
    "not_sent_tick",
]

#: The exact message of each not-sent cause that isn't a spawn failure (§1.13).
NOT_SENT_MESSAGES = {
    "ended_before_sent": "the meeting ended before a bot was sent",
    "room_busy": "another bot was still on this meeting link when the meeting ended",
}


@dataclass(frozen=True)
class RoomCheck:
    kind: Literal["gone", "free", "merge", "waiting"]
    live_id: Optional[int] = None


class OutboxOnly:
    """``EventPublisher`` that hands nothing on: every event is already in ``webhook_outbox``,
    where the outbox publisher (§1.8) picks it up."""

    async def publish(self, event_ids: Sequence[str]) -> None:
        return None


async def _publish(publisher: Optional[EventPublisher], event_ids: list[str]) -> None:
    if not event_ids or publisher is None:
        return
    try:
        await publisher.publish(event_ids)
    except Exception as exc:
        log_event(
            "scheduler_publish_failed",
            audience="operator",
            level="warning",
            span="meetings.auto_join",
            fields={"events": len(event_ids), "error": type(exc).__name__},
        )


async def check_room(
    store: IntakeStore,
    user_id: int,
    meeting_id: int,
    room: Room,
    *,
    publisher: Optional[EventPublisher] = None,
) -> RoomCheck:
    """R2 for one due entry-managed meeting, under its link lock (see the module docstring)."""
    events: list[str] = []
    async with store.room_lock(user_id, [room]) as tx:
        due = await tx.meeting(meeting_id)
        if due.status != "scheduled" or due.room != room:
            return RoomCheck("gone")
        live = [
            m
            for m in await tx.room_meetings(user_id, room)
            if m.id != meeting_id and is_live(m.status)
        ]
        if not live:
            return RoomCheck("free")
        target = next((m for m in live if is_merge_target(m)), None)
        if target is not None:
            return RoomCheck("merge", target.id)
        if not (due.aw or {}).get("waiting_for_room_sent_at"):
            await tx.mark_waiting_for_room(meeting_id)
            written = await tx.event(meeting_id, "meeting.waiting_for_room")
            events.append(written.event_id)
    await _publish(publisher, events)
    return RoomCheck("waiting")


def not_sent_cause(aw: Optional[Mapping[str, object]]) -> tuple[str, str]:
    """The ``not_sent`` detail and exact message for a meeting's ``meeting_aw_state`` row."""
    state = aw or {}
    code = state.get("last_error_code")
    if code:
        message = state.get("last_error_message")
        return str(code), str(message) if message else f"the bot was not sent ({code})"
    if state.get("waiting_for_room_sent_at"):
        return "room_busy", NOT_SENT_MESSAGES["room_busy"]
    return "ended_before_sent", NOT_SENT_MESSAGES["ended_before_sent"]


NOT_SENT = "not-sent"


async def not_sent_tick(
    store: IntakeStore,
    *,
    publisher: Optional[EventPublisher] = None,
    now: datetime,
    failures: ItemFailures,
    batch_size: Optional[int] = None,
) -> int:
    """End every overdue entry-managed meeting ``not_sent`` (R6), a page at a time; returns how
    many ended."""
    limit = batch_size or sweep_batch_size()
    ended: list[int] = []
    after: Optional[int] = None
    while True:
        page = await store.overdue_meetings(now, after=after, limit=limit)
        if not page:
            break
        after = page[-1].id
        skip = await failures.given_up(NOT_SENT, [str(v.id) for v in page])
        for view in page:
            if str(view.id) in skip:
                continue

            async def end(view: MeetingView = view) -> None:
                if await _end_not_sent(store, publisher, view, now=now):
                    ended.append(view.id)

            await run_item(failures, NOT_SENT, str(view.id), end, user_id=view.user_id)
        if len(page) < limit:
            break
    return len(ended)


async def _end_not_sent(
    store: IntakeStore,
    publisher: Optional[EventPublisher],
    view: MeetingView,
    *,
    now: datetime,
) -> bool:
    user_id, room = view.user_id, view.room
    async with store.room_lock(user_id, [room]) as tx:
        current = await tx.meeting(view.id)
        if (
            current.status != "scheduled"
            or current.room != room
            or not current.entries
            or not is_overdue(current.end, now=now)
        ):
            return False
        detail, message = not_sent_cause(current.aw)
        written = await tx.status(
            view.id,
            "failed",
            expected_from={"scheduled"},
            outcome=Outcome("not_sent", detail, message),
            change_reason=detail,
            event_type="meeting.not_sent",
        )
    log_event(
        "meeting_not_sent",
        audience="user",
        level="warning",
        span="meetings.auto_join",
        user_id=user_id,
        meeting_id=str(view.id),
        fields={"detail": detail, "message": message},
    )
    await _publish(publisher, [written.event_id])
    return True
