"""May a calendar OCCURRENCE be dispatched a bot again, given how its last run ended?

One decision, one table. Calendar sync recreates a terminal row's occurrence so the auto-join sweep
can retry it (#1200); this module decides whether that occurrence is still *owed* a bot at all.

The bug this exists to close (stage rev 192, witnessed live): meeting 26312 ended
``completed``/``stopped`` at 22:28:31 — the bot had joined, done its job, and been deliberately
stopped. Sync recreated the occurrence as 26313 at 22:28:46, the sweep's 300s backoff since 22:21:01
had legitimately elapsed, and at 22:28:47 an uninvited bot walked back into the founder's meeting.
The retry gate was a RATE limit (how recently did we try?) where it needed an ELIGIBILITY rule
(should we ever try again?). Backoff cannot fix "never".

The rule, in priority order:

1. **USER_STOPPED** — positive evidence the user stopped it (``data.stop_requested``, or
   ``completion_reason='stopped'``). Final for the occurrence at ANY lifecycle stage: a stop pressed
   while the bot sat in the waiting room is the user saying no just as loudly as one pressed
   mid-meeting. Founder ruling 2026-08-17: *"explicit stop must be stop"*.
2. **SERVED** — the bot reached the room and the visit is over. ``status='completed'`` (the FSM
   admits COMPLETED only from ACTIVE — see ``machine.LEGAL_TRANSITIONS`` — so every completed row is
   a row whose bot got in), or ``status='failed'`` with ``failure_stage='active'`` (admitted, then
   the run broke). A second bot here is a second visit, not a retry.
3. **RETRY** — failure class, and the bot never reached the room. Only these stay eligible, and only
   behind the sweep's backoff (#1200's storm ceiling: one dispatch per backoff per occurrence).

What is not retried is one list for the whole service: ``intake.retry.NOT_RETRIED``, the reasons a
new bot would meet again (the host or the platform refused it, ``awaiting_admission_rejected``; the
profile is signed out, ``auth_session_missing``; the request is malformed, ``validation_error``; or
an active-phase ending, ``evicted``, ``startup_alone``, ``max_bot_time_exceeded``). Every other
reason this codebase writes (``KNOWN_REASONS``) is retryable, including ``start_failed`` — written
straight to ``data.completion_reason`` by ``fail_meeting`` and NOT a member of the sealed
``CompletionReason`` enum; the workload never started, the most retryable failure there is. A value
outside ``KNOWN_REASONS`` is SERVED: nothing positively classes it as retryable.

An absent reason is retryable too — no producer emits one today (the bot stamps a reason on every
terminal edge, and ``reconcile`` derives one), so this is legacy and synthetic rows. Retryable ONLY
because rule 2 already caught the shape that matters: a reason-less row whose ``failure_stage`` is
``active`` is SERVED. Without positive evidence the bot was admitted, the pre-#1200 behaviour
stands and the backoff bounds it. Rule 2 also claims every ``left_alone`` this codebase writes: the
bot reports it as ``completed``, and the reconcile sweep writes it only for a row that reached the
meeting (``active``/``stopping``; ``needs_help`` is pre-active), so at the ``active`` stage.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from .machine import CompletionReason

# The user-stop reason, the one value that is final regardless of status or stage.
USER_STOP_REASON = CompletionReason.STOPPED.value

# Out-of-enum reasons this codebase writes directly to ``data.completion_reason``. Enumerated
# because the sealed enum is not the whole truth: ``fail_meeting`` writes ``start_failed`` without
# ever going through ``CompletionReason``.
START_FAILED = "start_failed"

# Every reason value this codebase can put on a terminal row. The matrix test asserts this is
# exhaustive over the sealed enum, so a new CompletionReason cannot be added without deciding — and
# testing — which side of this gate it falls on.
KNOWN_REASONS: frozenset[str] = frozenset(
    {reason.value for reason in CompletionReason} | {START_FAILED}
)

# The stage that means the bot was ADMITTED. A failed run attributed to it reached the meeting.
ACTIVE_STAGE = "active"

TERMINAL_STATUSES = ("completed", "failed")


class Disposition(str, Enum):
    """What a terminal row leaves owed to its occurrence."""

    USER_STOPPED = "user_stopped"   # the user said no — never dispatch again
    SERVED = "served"               # the bot came and the visit is over — never dispatch again
    RETRY = "retry"                 # unserved failure — eligible again once the backoff elapses

    @property
    def dispatchable(self) -> bool:
        """May the occurrence be given another bot (subject to the sweep's backoff)?"""
        return self is Disposition.RETRY


def disposition(row: Any) -> Optional[Disposition]:
    """Classify a TERMINAL meeting row. ``None`` when the row is not terminal (nothing decided).

    Reads only ``status`` and ``data`` — the same shape calendar sync, the auto-join sweep and the
    repo adapters all hand around, so one rule serves every caller.
    """
    if not isinstance(row, dict):
        return None
    status = row.get("status")
    if status not in TERMINAL_STATUSES:
        return None
    data = row.get("data") if isinstance(row.get("data"), dict) else {}
    reason = data.get("completion_reason")
    reason = reason if isinstance(reason, str) and reason else None

    # 1. The user stopped it. Strongest signal, checked first, honoured at every stage — including a
    #    stop while the bot was still joining or waiting to be admitted, which lands as
    #    `failed`/`stopped` (stop.classify_user_stop) and would otherwise read as a failure to retry.
    if data.get("stop_requested") or reason == USER_STOP_REASON:
        return Disposition.USER_STOPPED

    # 2. The bot reached the room. `completed` is only reachable from ACTIVE; a `failed` run
    #    attributed to the ACTIVE stage was admitted and then broke.
    if status == "completed" or data.get("failure_stage") == ACTIVE_STAGE:
        return Disposition.SERVED

    # 3. Pre-active failure: retry only what is positively retryable (absent reasons included, see
    #    the module docstring — rule 2 already claimed the admitted shape).
    from ..intake.retry import NOT_RETRIED

    if reason is None or (reason in KNOWN_REASONS and reason not in NOT_RETRIED):
        return Disposition.RETRY
    return Disposition.SERVED


def may_dispatch_again(row: Any) -> bool:
    """Is this terminal row's occurrence still eligible for another auto-join dispatch?"""
    verdict = disposition(row)
    return verdict is None or verdict.dispatchable
