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

A waiting meeting is bounded by ``deadline``: ``due_at`` plus ``MEETING_UNTRACKED_GRACE_SEC``. A
failed workload not proven gone by then, or a new bot not sent by then, ends the meeting
(``overdue``: ``workload_not_proven`` / ``retry_not_sent``); the retry driver and the reconcile
sweep both apply that one rule.

A waiting meeting leaves ``requested`` one of two ways. A new bot claims it once ``proven``: the
claim writes ``claimed(data)`` (the marker's failure moved into ``completion_history``, the marker
removed) and no status change. Or ``end(tx, meeting_id, …)`` ends it ``failed`` (``bot.failed``,
``aw_meetings_failed_total``) with the marker's reason and stage, or the reason given (a stop's
``stopped``): its planned end passed, it was stopped, or its last send failed before the claim.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Mapping, Optional

from .ports import IntakeTx, MeetingView
from .projection import iso_utc
from .rules import as_utc
from .settings import IntakeSettings
from .status import RETRY_EVENT, Outcome, WrittenEvent

__all__ = [
    "BOT_FAILED",
    "Failure",
    "MARKER",
    "claimed",
    "deadline",
    "end",
    "is_last",
    "marker",
    "overdue",
    "proven",
    "retired_sessions",
    "retry",
]

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


def proven(data: Any) -> bool:
    """A pending retry whose failed workload is proven gone: a new bot may claim the row."""
    mark = marker(data)
    return mark is not None and mark.get("proven_gone") is True


def claimed(data: Any) -> dict[str, Any]:
    """The row's ``data`` once a new bot claims it: the marker's reason, stage and message moved
    into ``data.completion_history`` (``bot_spawn.ports._archive_completion``, as a reopened row's
    are) with the failed session as ``after_session``, the marker removed. That session is retired:
    it never writes the row again (``retired_sessions``)."""
    from ..bot_spawn.ports import _archive_completion

    out = dict(data) if isinstance(data, Mapping) else {}
    mark = marker(out) or {}
    out.pop(MARKER, None)
    out["completion_reason"] = mark.get("reason")
    out["failure_stage"] = mark.get("stage")
    out["failure_reason"] = mark.get("message") or "the bot failed"
    out = _archive_completion(out)
    if mark.get("after_session"):
        history = out["completion_history"]
        history[-1] = {**history[-1], "after_session": mark["after_session"]}
    return out


def retired_sessions(data: Any) -> frozenset[str]:
    """The sessions a new bot has replaced on this row (``claimed``): they write nothing."""
    history = data.get("completion_history") if isinstance(data, Mapping) else None
    if not isinstance(history, list):
        return frozenset()
    return frozenset(
        str(h["after_session"])
        for h in history
        if isinstance(h, Mapping) and h.get("after_session")
    )


def deadline(mark: Mapping[str, Any], untracked_grace: float) -> Optional[datetime]:
    """When a waiting meeting stops waiting: its ``due_at`` plus ``untracked_grace``
    (``MEETING_UNTRACKED_GRACE_SEC``, the longest a failed workload may stay unaccounted for).
    """
    due = as_utc(mark.get("due_at"))
    return None if due is None else due + timedelta(seconds=untracked_grace)


def overdue(
    mark: Mapping[str, Any], untracked_grace: float, now: datetime
) -> Optional[tuple[str, str]]:
    """The change reason and message a waiting meeting past its ``deadline`` at ``now`` ends
    with, or ``None`` while it may still wait. The retry driver and the reconcile sweep both end
    a waiting meeting by this one rule."""
    limit = deadline(mark, untracked_grace)
    if limit is None or now < limit:
        return None
    if mark.get("proven_gone"):
        return "retry_not_sent", f"no new bot was sent by {iso_utc(limit)}"
    if not mark.get("workload"):
        return (
            "workload_not_proven",
            "the failed bot's start recorded no workload, so none could be proven gone by "
            f"{iso_utc(limit)}",
        )
    return (
        "workload_not_proven",
        f"the failed bot's workload {mark['workload']} was not proven gone by "
        f"{iso_utc(limit)}",
    )


def unfinished_spawn(
    meeting_id: int,
    data: Any,
    *,
    written: bool,
    newest_session: Optional[str],
    untracked_grace: float,
    now: datetime,
) -> Optional[tuple[str, dict[str, Any]]]:
    """A spawn that died after its claim, insert or reopen and before its session write: the row
    names the session it planned (``bot_spawn.ports.spawn_session``) and no session row has it
    (``written`` False). Past that time plus ``untracked_grace`` the meeting ends ``failed``;
    returns the change reason and the data patch, or ``None`` while the spawn may still write
    it. The planned workload (``workload_id_for``) may run, so the patch records it through the
    one ``unproven_teardown`` builder. A claimed retry (its newest session is the retired one)
    keeps the failed bot's reason and ends as a new bot not sent (``overdue``:
    ``retry_not_sent``); any other spawn is ``start_failed``."""
    from ..bot_spawn.ports import SPAWN_SESSION, unproven_teardown, workload_id_for

    plan = data.get(SPAWN_SESSION) if isinstance(data, Mapping) else None
    if written or not isinstance(plan, Mapping) or not plan.get("session"):
        return None
    sent = {"due_at": plan.get("at"), "proven_gone": True}
    over = overdue(sent, untracked_grace, now)
    if over is None:
        return None
    workload = workload_id_for(meeting_id, str(plan["session"]))
    patch: dict[str, Any] = dict(unproven_teardown(data, workload))
    if newest_session and newest_session in retired_sessions(data):
        code, message = over
        last = data["completion_history"][-1]
        for key in ("completion_reason", "failure_stage"):
            if last.get(key) is not None:
                patch[key] = last[key]
    else:
        code = "start_failed"
        message = f"the bot did not start by {iso_utc(deadline(sent, untracked_grace))}"
        patch.update(completion_reason="start_failed", failure_stage="requested")
    patch["failure_reason"] = f"{message}; its workload {workload} may still run"
    return code, patch


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


async def end(
    tx: IntakeTx,
    meeting_id: int,
    *,
    completion_reason: Optional[str] = None,
    change_reason: Optional[str] = None,
    outcome: Optional[Outcome] = None,
    message: Optional[str] = None,
) -> Optional[WrittenEvent]:
    """End a meeting waiting for its next bot ``failed``: ``completion_reason`` (else the
    marker's), the marker's stage as ``failure_stage`` and ``message`` (else the marker's) as
    ``failure_reason``, the marker cleared, with ``outcome`` when given; the change reason is
    ``change_reason``, else the completion reason. A failed workload not proven gone is left
    for the reconcile sweep to delete (``bot_spawn.ports.unproven_teardown``, the one record):
    the ending frees the link while it may still run. ``None``, having written nothing, when
    the meeting isn't waiting."""
    from ..bot_spawn.ports import unproven_teardown

    meeting = await tx.meeting(meeting_id)
    mark = marker(meeting.data)
    if mark is None or meeting.status != "requested":
        return None
    reason = completion_reason or mark.get("reason")
    patch: dict[str, Any] = {
        MARKER: None,
        "failure_reason": message or mark.get("message"),
    }
    if not mark.get("proven_gone") and mark.get("workload"):
        patch.update(unproven_teardown(meeting.data, str(mark["workload"])))
    if reason is not None:
        patch["completion_reason"] = reason
    if mark.get("stage") is not None:
        patch["failure_stage"] = mark["stage"]
    return await tx.status(
        meeting_id,
        "failed",
        expected_from={"requested"},
        data_patch=patch,
        outcome=outcome,
        change_reason=change_reason or reason,
    )
