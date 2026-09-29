"""§6.9 F-K2 — a bot that fails while its meeting is on gets a new bot on the SAME meeting.

A bot session ends (``lifecycle.v1`` is per session); the meeting row does not have to. When a
bot fails while its meeting is on, the row goes back to ``requested`` with ``bot.retry`` and the
marker ``data.bot_retry``, keeps holding its link (``requested`` is a live status), and the
auto-join sweep sends a new bot session to it once the failed workload is proven gone.

``due_at(meeting, failure, now=, settings=)`` is the decision, pure. A failure is retried when:

  * it is a bot failure: a ``failed`` session, whatever its reason except ``stopped`` (a user's
    stop), ``evicted`` (the host removed the bot) and ``startup_alone`` (nobody joined); or a
    ``completed`` session nobody reported (``lost``: the runtime or a reconcile sweep saw the
    workload gone) with any other reason, which is a lost bot, not a normal end;
  * entries manage the meeting (it has an active entry); meetings without entries are out of
    scope;
  * nobody ended it: no ``data.stop_requested``, no outcome (a calendar removal, R5/R8), and the
    row is live and not ``stopping``;
  * it is within the meeting's bounded sends: ``send_attempts + 1 < BOT_SEND_MAX_ATTEMPTS``
    (the §6.9 F-K counter), and the next bot, ``BOT_SEND_RETRY_BACKOFF_S`` from now, goes before
    the planned end (an open-ended meeting has only the attempts).

``in_scope`` / ``is_bot_failure`` are its two halves: a bot failure on an in-scope meeting that
the bounds refuse is the meeting's last failure (``is_last``), and ends it ``failed`` even when
the lost bot's session reads ``completed``.

``retry(tx, meeting_id, failure, now=, settings=, data_patch=)`` is the one writer, in the
caller's transaction under the meeting's link lock: one of the meeting's sends
(``record_send_failure``, the failure's typed code and message, held until ``due_at``), then the
status writer's ``requested`` with ``bot.retry``, the change reason the session's completion
reason (else the code), and ``data.bot_retry``::

    {reason, stage, message, after_session, workload, at, due_at, proven_gone}

``reason`` / ``stage`` are the failed session's completion reason and failure stage, kept on the
marker while the meeting is live (the row's own ``completion_reason`` stays empty until it ends);
``workload`` is the failed bot's workload and ``proven_gone`` whether it is already proven gone.
It returns ``None``, having written nothing, when the failure isn't retried.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Mapping, Optional

from .ports import IntakeTx, MeetingView
from .projection import iso_utc
from .settings import IntakeSettings
from .status import WrittenEvent

__all__ = [
    "BOT_FAILED",
    "Failure",
    "MARKER",
    "NOT_RETRIED",
    "RETRY_EVENT",
    "due_at",
    "in_scope",
    "is_bot_failure",
    "is_last",
    "marker",
    "pending_from",
    "retry",
]

RETRY_EVENT = "bot.retry"
MARKER = "bot_retry"
#: The typed code a failed bot session's send is recorded with (``last_error_code``).
BOT_FAILED = "bot_failed"
#: Completion reasons that end the meeting: a user's stop, the host removing the bot, nobody
#: joining.
NOT_RETRIED = frozenset({"stopped", "evicted", "startup_alone"})
#: The session's keys the marker carries instead of the row while the meeting is live.
_KEPT_ON_MARKER = ("completion_reason", "failure_stage", "failure_reason", MARKER)


def pending_from() -> frozenset[str]:
    """The statuses a bot failure sends back to ``requested``: every live status but
    ``stopping`` (a stop is under way)."""
    from ..bot_spawn.auto_join import LIVE_STATUSES

    return frozenset(LIVE_STATUSES) - {"stopping"}


@dataclass(frozen=True)
class Failure:
    """One failed bot session of a meeting: the terminal it reached (``failed``, or ``completed``
    for a lost bot), its completion reason and failure stage, the bot's reason text, whether no
    bot reported it (``lost``), the session and its workload, and whether that workload is
    already proven gone. ``code`` is the typed code its send is recorded with."""

    status: str
    reason: Optional[str]
    message: str
    stage: Optional[str] = None
    lost: bool = False
    session: Optional[str] = None
    workload: Optional[str] = None
    proven_gone: bool = False
    code: str = BOT_FAILED


def marker(data: Any) -> Optional[dict[str, Any]]:
    """The pending retry a row's ``data`` carries, else ``None``."""
    value = data.get(MARKER) if isinstance(data, Mapping) else None
    return dict(value) if isinstance(value, Mapping) else None


def is_bot_failure(failure: Failure) -> bool:
    """A failed bot (not a stop, a host removal, nobody joining, or a normal end)."""
    if failure.reason in NOT_RETRIED:
        return False
    if failure.status == "failed":
        return True
    return failure.status == "completed" and failure.lost


def in_scope(meeting: MeetingView) -> bool:
    """Entries manage the meeting and nobody has ended it."""
    if not meeting.active_entries() or meeting.data.get("stop_requested"):
        return False
    if meeting.aw is not None and meeting.aw.get("outcome_kind") is not None:
        return False
    return meeting.status in pending_from()


def is_last(meeting: MeetingView, failure: Failure) -> bool:
    """A bot failure on an in-scope meeting: when ``due_at`` refuses it, the meeting's last."""
    return is_bot_failure(failure) and in_scope(meeting)


def due_at(
    meeting: MeetingView,
    failure: Failure,
    *,
    now: datetime,
    settings: IntakeSettings,
) -> Optional[datetime]:
    """When the meeting's next bot goes, or ``None`` when the failure isn't retried."""
    if not is_last(meeting, failure):
        return None
    attempts = int((meeting.aw or {}).get("send_attempts") or 0) + 1
    if attempts >= settings.send_max_attempts:
        return None
    due = now + timedelta(seconds=settings.send_retry_backoff_s)
    end = meeting.end
    if end is not None and due >= end:
        return None
    return due


async def retry(
    tx: IntakeTx,
    meeting_id: int,
    failure: Failure,
    *,
    now: datetime,
    settings: IntakeSettings,
    data_patch: Optional[Mapping[str, Any]] = None,
) -> Optional[WrittenEvent]:
    """Send the meeting back to ``requested`` with ``bot.retry`` if ``failure`` is retried;
    ``None``, having written nothing, if it isn't. ``data_patch`` is the failed session's own
    data (its transition trail, logs, evidence); its ``completion_reason``, ``failure_stage`` and
    ``failure_reason`` go on the marker instead."""
    meeting = await tx.meeting(meeting_id)
    due = due_at(meeting, failure, now=now, settings=settings)
    if due is None:
        return None
    await tx.record_send_failure(
        meeting_id, failure.code, failure.message, retry_at=due
    )
    patch = {k: v for k, v in (data_patch or {}).items() if k not in _KEPT_ON_MARKER}
    patch[MARKER] = {
        "reason": failure.reason,
        "stage": failure.stage,
        "message": failure.message,
        "after_session": failure.session,
        "workload": failure.workload,
        "at": iso_utc(now),
        "due_at": iso_utc(due),
        "proven_gone": failure.proven_gone,
    }
    return await tx.status(
        meeting_id,
        "requested",
        expected_from=pending_from(),
        data_patch=patch,
        change_reason=failure.reason or failure.code,
        event_type=RETRY_EVENT,
    )
