"""The stop of the bot in the call (§1.7): ``record_stop`` and the production ``StopPort``.

§1.7's order:

  1. the link lock;
  2. the meeting row, locked by the status writer;
  3. the outcome on ``meeting_aw_state``, when given;
  4. the status: a bot that reached the meeting (``active``, ``needs_help``, …) goes ``stopping``
     through ``write_status`` with ``data.stop_requested``; a bot still booting (``requested``,
     ``joining``, ``awaiting_admission``) keeps the stage it reached and gets only
     ``data.stop_requested`` (and the outcome), because ``stopping`` means "a bot in the meeting is
     leaving" to every terminal reader (``lifecycle.machine``, ``lifecycle.reconcile``);
  5. after the commit, the leave command on ``bot_commands:meeting:{id}``;
  6. and, while the bot is still booting, a workload delete.

``record_stop(tx, meeting_id, outcome)`` is steps 2–4 inside a transaction that already holds the
meeting's link lock, the one implementation of them. R5 calls it in the removal's own
transaction (``IntakeService``), so a removed last entry and its stop commit together or not at
all. ``IntakeStop.stop_live`` (``POST /v2/meetings/{id}/stop``, no outcome: the meeting ends with
upstream's ``stopped``) opens that transaction itself: it reads the meeting's link, takes the link
lock and calls ``record_stop``. ``IntakeStop.leave`` is steps 5–6 after the commit:
``lifecycle.stop_router.stop_meeting_row``, the one stop of a recorded row that upstream
``DELETE /bots`` uses too. The bot then leaves and its lifecycle callback ends the meeting
``completed``/``failed`` with upstream's ``stopped`` reason.

A meeting waiting for its next bot (§6.9 F-K2, ``data.bot_retry``) has no bot to leave: its stop
ends it at once, ``failed`` with ``stopped`` and the outcome (``retry.end``), and ``leave`` sends
nothing for it.

A meeting with no live bot, or one already stop-requested, is left as it is: nothing is written
and nothing is sent (the route answers ``no_live_bot`` for the first before it gets here). A
command bus that can't be reached is ``unavailable`` (503); the stop is already recorded, and the
stale-stopping reconcile sweep ends the bot. Events are handed to ``publisher`` after the commit,
when one is given; the outbox holds them either way.
"""

from __future__ import annotations

from typing import Any, Optional, Sequence

from ..lifecycle.stop_router import (
    BOOTING_STATUSES,
    CommandPublisher,
    stop_meeting_row,
)
from ..obs import log_event
from . import retry
from .ports import (
    EventPublisher,
    IntakeStore,
    IntakeTx,
    MeetingView,
    RecordedStop,
    Room,
)
from .rules import is_live
from .status import Outcome
from .validation import IntakeError

__all__ = ["IntakeStop", "record_stop"]

#: The sealed ``lifecycle.v1`` reason a stop ends with, carried as the ``stopping`` change reason.
STOPPED = "stopped"


def _stop_requested(meeting: MeetingView) -> bool:
    return meeting.status == "stopping" or bool(meeting.data.get("stop_requested"))


async def record_stop(
    tx: IntakeTx, meeting_id: int, outcome: Optional[Outcome]
) -> Optional[RecordedStop]:
    """§1.7 steps 2–4 in ``tx``, which holds the meeting's link lock. ``None`` when there is no
    live bot to stop, or its stop is already recorded."""
    meeting = await tx.meeting(meeting_id)
    if not is_live(meeting.status) or _stop_requested(meeting):
        return None
    if retry.marker(meeting.data) is not None:
        ended = await retry.end(
            tx, meeting_id, completion_reason=STOPPED, outcome=outcome
        )
        events = () if ended is None else (ended.event_id,)
        return RecordedStop(meeting_id, dict(meeting.row), outcome, events)
    if meeting.status in BOOTING_STATUSES:
        await tx.mark_stop_requested(meeting_id, outcome)
        return RecordedStop(meeting_id, dict(meeting.row), outcome, ())
    written = await tx.status(
        meeting_id,
        "stopping",
        expected_from={meeting.status},
        data_patch={"stop_requested": True},
        outcome=outcome,
        change_reason=STOPPED,
    )
    return RecordedStop(meeting_id, dict(meeting.row), outcome, (written.event_id,))


class _StoreRows:
    """The row as the intake store has it now: the re-read ``stop_meeting_row`` decides the
    booting teardown on (``get_meeting``)."""

    def __init__(self, store: IntakeStore, user_id: int) -> None:
        self._store = store
        self._user_id = user_id

    async def get_meeting(self, meeting_id: int) -> Optional[dict[str, Any]]:
        async with self._store.room_lock(self._user_id, ()) as tx:
            return dict((await tx.meeting(meeting_id)).row)


class IntakeStop:
    """``StopPort`` over the intake store, the bot command bus and the runtime (§1.7)."""

    def __init__(
        self,
        store: IntakeStore,
        commands: CommandPublisher,
        runtime: Any = None,
        *,
        publisher: Optional[EventPublisher] = None,
    ) -> None:
        self._store = store
        self._commands = commands
        self._runtime = runtime
        self._publisher = publisher

    async def stop_live(
        self, user_id: int, meeting_id: int, *, outcome: Optional[Outcome]
    ) -> None:
        recorded = await self._record(user_id, meeting_id, outcome)
        if recorded is None:
            return
        await self._publish(recorded.events)
        await self.leave(user_id, recorded)

    async def leave(self, user_id: int, stop: RecordedStop) -> None:
        if retry.marker(stop.row.get("data")) is not None:
            return  # a meeting waiting for its next bot had no bot to tell, and has ended
        try:
            await stop_meeting_row(
                _StoreRows(self._store, user_id),
                self._commands,
                self._runtime,
                dict(stop.row),
            )
        except Exception as exc:
            log_event(
                "intake_stop_command_failed",
                audience="operator",
                level="warning",
                span="meetings.intake.stop",
                user_id=user_id,
                meeting_id=str(stop.meeting_id),
                fields={"error": type(exc).__name__},
            )
            raise IntakeError(
                "unavailable",
                "the stop is recorded, but the leave command could not be sent (the command "
                "bus is unavailable); it will reconcile when the bus returns",
            ) from exc

    async def _record(
        self, user_id: int, meeting_id: int, outcome: Optional[Outcome]
    ) -> Optional[RecordedStop]:
        """Steps 1–4 in one transaction under the meeting's link lock (restarted once when the
        link changed between the read and the lock)."""
        room = await self._room(user_id, meeting_id)
        for _ in range(2):
            async with self._store.room_lock(user_id, [room]) as tx:
                current = (await tx.meeting(meeting_id)).room
                if current != room:
                    room = current
                    continue
                return await record_stop(tx, meeting_id, outcome)
        raise IntakeError(
            "unavailable", "the meeting's link changed during the stop; retry"
        )

    async def _room(self, user_id: int, meeting_id: int) -> Room:
        async with self._store.room_lock(user_id, ()) as tx:
            return (await tx.meeting(meeting_id)).room

    async def _publish(self, event_ids: Sequence[str]) -> None:
        if not event_ids or self._publisher is None:
            return
        try:
            await self._publisher.publish(event_ids)
        except Exception as exc:
            log_event(
                "intake_stop_publish_failed",
                audience="operator",
                level="warning",
                span="meetings.intake.stop",
                fields={"events": len(event_ids), "error": type(exc).__name__},
            )
