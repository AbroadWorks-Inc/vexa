"""Decide how long the bot should wait before it opens the meeting page.

Context — why this exists
========================
The orchestrator spawns the bot pod ``BOT_LOOKAHEAD_MINUTES`` (10) before the
meeting starts, because startup is slow: pull the image, start Xvfb, start
PulseAudio, boot Chromium, warm the sidecar. That lead time is necessary.

What was NOT necessary was *knocking* that early. In the guest-join flow the bot
is never invited, so the meeting platform (Meet/Zoom) always drops it into a
waiting room and a human has to admit it. Asking to join 10 minutes early means
knocking on the door of an empty room: there is nobody present to admit the bot,
and the admission budget (``waitingRoomTimeout``, 15 min) is then spent almost
entirely BEFORE the meeting begins. Observed live on Meet 2026-08-19: bot knocked
23:50, owner joined 00:00, bot gave up at 00:05 with ``admission_timeout`` — only
5 of its 15 minutes of patience overlapped with a human being in the room. The
same pattern applies to Zoom's waiting room, so the wait lives here for both.

So: keep starting up early, but sit still until the meeting is nearly due, then
knock. Same budget, aimed at the window where someone can actually let the bot in.

Why this lives here, in the container boot path
-----------------------------------------------
``BOT_JOB_JSON`` (which carries ``scheduled_start_at``) is already in this
container's environment. ``BOT_CONFIG`` — the object the TypeScript bot core
reads — deliberately does NOT carry the start time, so implementing the wait
inside the bot would mean threading a new field through ``job_launcher.py``,
``types.ts``, the ``docker.ts`` zod schema and ``join.ts``, across two repos, for
no behavioural gain. ``start.sh`` already has everything needed.

Fail-open, always
-----------------
Every failure path returns ``0`` — i.e. "knock immediately", exactly the
behaviour before this module existed. A missing field, malformed JSON, a naive
timestamp or an unexpected exception must never be able to make the bot sleep
through a meeting. A sleep is the one change here that could silently cost a
recording, so every uncertain case declines to sleep.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

#: Knock this many seconds before the scheduled start. Not zero: the platform's
#: pre-join page needs a moment to load and the click itself takes time, and a bot
#: already in the waiting room when the owner arrives is admitted faster than one
#: that has not asked yet.
#:
#: Deliberately a constant rather than an env var: `job_launcher` forwards only a
#: fixed set of variables to bot pods, so an env knob here would need plumbing in
#: the orchestrator to be settable at all. Tunable in a follow-up if wanted.
DEFAULT_LEAD_SECONDS = 60

#: Hard ceiling on the sleep. Protects against a nonsense far-future
#: ``scheduled_start_at`` turning the bot into a pod that does nothing for hours.
#: Comfortably above the real lead time (10 min) so normal jobs are never clamped.
MAX_SLEEP_SECONDS = 900


def seconds_until_knock(
    raw_job: str | None,
    now: datetime,
    lead_seconds: int = DEFAULT_LEAD_SECONDS,
    max_sleep_seconds: int = MAX_SLEEP_SECONDS,
) -> tuple[int, str]:
    """Return ``(seconds_to_sleep, reason)`` — pure, total, never raises.

    ``reason`` is returned rather than logged inside so the decision is
    assertable in tests and visible in the pod log; a silent sleep would be
    undebuggable.

    :param raw_job: the ``BOT_JOB_JSON`` value, or None/empty if unset.
    :param now: current time; must be timezone-aware (caller passes UTC).
    :param lead_seconds: knock this long before the scheduled start.
    :param max_sleep_seconds: never sleep longer than this.
    """
    if now.tzinfo is None:
        # Enforced, not merely documented: a naive `now` would be compared against
        # an absolute instant, so the delta could be out by the pod's UTC offset in
        # either direction. Fail open like every other uncertain input.
        return 0, "now is timezone-naive; knocking now"
    if lead_seconds < 0:
        return 0, f"invalid lead_seconds={lead_seconds}; knocking now"
    if not raw_job:
        return 0, "BOT_JOB_JSON unset; knocking now"

    try:
        job = json.loads(raw_job)
    except (ValueError, TypeError):
        return 0, "BOT_JOB_JSON is not valid JSON; knocking now"

    if not isinstance(job, dict):
        return 0, "BOT_JOB_JSON is not an object; knocking now"

    raw_start = job.get("scheduled_start_at")
    if not isinstance(raw_start, str) or not raw_start.strip():
        return 0, "scheduled_start_at missing; knocking now"

    try:
        start = datetime.fromisoformat(raw_start.strip())
    except ValueError:
        return 0, f"scheduled_start_at unparseable ({raw_start!r}); knocking now"

    if start.tzinfo is None:
        # Assuming a zone here could be hours wrong in either direction, and
        # being wrong late is worse than not waiting at all.
        return 0, "scheduled_start_at has no timezone; knocking now"

    target = start.timestamp() - lead_seconds
    delta = target - now.timestamp()

    if delta <= 0:
        return 0, "meeting already due; knocking now"

    if delta > max_sleep_seconds:
        # Clamped rather than skipped on purpose: knocking IMMEDIATELY for a
        # far-future start reinstates the very bug this module fixes, whereas a
        # bounded wait is strictly closer to the intent. Loud, because reaching
        # this branch means the spawn lead time and this ceiling disagree.
        return (
            max_sleep_seconds,
            f"start is {int(delta)}s away, beyond the {max_sleep_seconds}s "
            f"ceiling; sleeping {max_sleep_seconds}s (check BOT_LOOKAHEAD_MINUTES)",
        )

    # `now` and the resulting delta are logged deliberately. This module trusts the
    # pod's wall clock absolutely, and a pod whose clock runs behind real time would
    # compute an inflated delta and sleep away part of its admission window -- a
    # milder recurrence of the bug this fixes. Bounded by MAX_SLEEP_SECONDS, so it
    # cannot be unbounded, but it would be undiagnosable without these numbers in
    # the log, so print them rather than defend against a skew we cannot measure.
    return (
        int(delta),
        f"knocking {lead_seconds}s before {start.isoformat()} "
        f"(now={now.isoformat()}, sleeping {int(delta)}s)",
    )


def main() -> int:
    """Print the seconds to sleep on stdout; the reason goes to stderr.

    stdout is consumed by ``start.sh``, so it carries the integer and nothing
    else. Any unexpected exception still prints ``0`` — the boot path must not
    die because a wait could not be computed.
    """
    # The whole body is inside the try, prints included: the module's invariant is
    # "every failure path yields 0", and a print that raised (a closed stdout, say)
    # would otherwise escape it.
    try:
        seconds, reason = seconds_until_knock(
            os.environ.get("BOT_JOB_JSON"), datetime.now(timezone.utc)
        )
        print(seconds)
        print(f"[knock_delay] {reason}", file=sys.stderr)
    except Exception as exc:  # pragma: no cover - defensive, see fail-open note
        try:
            print("0")
            print(
                f"[knock_delay] unexpected error: {exc!r}; knocking now",
                file=sys.stderr,
            )
        except Exception:
            # Even the fallback print failed. start.sh's `|| echo 0` and its
            # non-integer `case` guard both still yield "knock now".
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
