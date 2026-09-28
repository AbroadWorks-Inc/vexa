"""The R1 matching rules and the meeting windows (§1.1, R7, R10) — pure: no storage, no clock.

This module is the one home of the window and overlap logic. The entry service decides with it
which meeting an entry belongs to, and whether an update moves the entry away from a meeting that
has started.

  * ``overlaps`` — half-open intervals: ``a_start < b_end and a_end > b_start``; a missing end is
    unbounded. Back-to-back times (15:00 end, 15:00 start) don't overlap.
  * ``meeting_start`` — ``data.scheduled_at``, else ``start_time``, else ``created_at`` (the
    ``meeting_event_time()`` order). A ``scheduled_at`` that isn't an ISO-8601 time (upstream rows
    carry whatever their writer stored) counts as absent, as the auto-join sweep reads it;
    ``scheduled_time`` is that ``scheduled_at`` alone.
  * ``meeting_window`` — a meeting's stored time; an open-ended live meeting's window is
    ``[start, now]``, and an open-ended meeting that isn't live yet is unbounded.
  * ``match_entry`` — R1: the non-finished meeting whose window overlaps the entry. An open-ended
    live meeting matches only an entry already started or due (``start <= now + lead``).
  * ``join_now_target`` — R1 ``join_now``: the earliest non-finished meeting with ``end > now``
    and ``start <= now + adopt_ahead``. A live meeting counts as not ended however long it runs
    past its planned end: its bot is in the call now, and a pasted link never gets a second bot.
  * ``recompute`` — a meeting's plan from its active entries: earliest start, latest end (none if
    any entry is open-ended), and the first title, time zone and link in start order.
  * ``finished_window`` — R10: a started meeting's window is ``[meeting start, finish)``, the end
    clamped to be no earlier than the start.
  * ``is_future_move`` — R7: an update moves the entry to a new future time, away from a meeting
    that has started, when it doesn't overlap that meeting's window, starts after the meeting
    start and starts after ``now``. A finished meeting's window ends ``now`` (it finished at or
    before ``now``, so for an entry starting after ``now`` both give the same answer); a live
    one's at the later of ``now`` and its planned end; an open-ended live one's at ``now + lead``,
    the horizon R1 matches it by.
  * ``is_overdue`` — R6: a meeting still without a bot is past its end: its ``end``, or for an
    open-ended meeting ``start + open_ended_s`` (the not-sent sweep passes
    ``JOIN_NOW_ADOPT_AHEAD_S``, the window a pasted link adopts by).

Every tie between candidate meetings goes to the earliest start, then the lowest id.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional, Protocol, Sequence, TypeVar

__all__ = [
    "FINISHED_STATUSES",
    "Plan",
    "as_utc",
    "finished_window",
    "is_future_move",
    "is_live",
    "is_overdue",
    "join_now_target",
    "match_entry",
    "meeting_start",
    "meeting_window",
    "overlaps",
    "recompute",
    "scheduled_time",
]

FINISHED_STATUSES = frozenset({"completed", "failed"})


class Timed(Protocol):
    """An entry's time, from a request (``EntryIn``) or storage (``EntryView``)."""

    @property
    def start(self) -> datetime: ...

    @property
    def end(self) -> Optional[datetime]: ...


class Planned(Timed, Protocol):
    @property
    def title(self) -> Optional[str]: ...

    @property
    def time_zone(self) -> Optional[str]: ...

    @property
    def meeting_url(self) -> str: ...


class MeetingLike(Protocol):
    @property
    def id(self) -> int: ...

    @property
    def status(self) -> str: ...

    @property
    def start(self) -> Optional[datetime]: ...

    @property
    def end(self) -> Optional[datetime]: ...


_M = TypeVar("_M", bound=MeetingLike)


@dataclass(frozen=True)
class Plan:
    """A meeting's time, title, time zone and link, as its active entries give them."""

    start: datetime
    end: Optional[datetime]
    title: Optional[str]
    time_zone: Optional[str]
    meeting_url: str


def as_utc(value: Any) -> Optional[datetime]:
    """A datetime or ISO-8601 string as an aware UTC datetime (naive means UTC), or ``None``."""
    if value is None or value == "":
        return None
    dt = (
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        if isinstance(value, str)
        else value
    )
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def is_live(status: Optional[str]) -> bool:
    """A bot has been sent and hasn't finished (``bot_spawn.auto_join.LIVE_STATUSES``)."""
    # Imported at call time so bot_spawn can import the intake package without an import cycle.
    from ..bot_spawn.auto_join import LIVE_STATUSES

    return status in LIVE_STATUSES


def overlaps(
    a_start: datetime,
    a_end: Optional[datetime],
    b_start: datetime,
    b_end: Optional[datetime],
) -> bool:
    """``[a_start, a_end)`` and ``[b_start, b_end)`` share an instant; ``None`` is unbounded."""
    return (b_end is None or a_start < b_end) and (a_end is None or a_end > b_start)


def meeting_start(
    data: Optional[Mapping[str, Any]], start_time: Any, created_at: Any
) -> Optional[datetime]:
    """``data.scheduled_at``, else ``start_time``, else ``created_at``, as aware UTC. An
    unparseable ``scheduled_at`` falls through to the next."""
    return scheduled_time(data) or as_utc(start_time) or as_utc(created_at)


def scheduled_time(data: Optional[Mapping[str, Any]]) -> Optional[datetime]:
    """``data.scheduled_at`` as aware UTC, or ``None`` when it is absent or not a time."""
    return _parsed(data.get("scheduled_at") if isinstance(data, Mapping) else None)


def _parsed(value: Any) -> Optional[datetime]:
    """``as_utc`` for a stored value that may not be a time at all: ``None`` when it isn't."""
    if not isinstance(value, (str, datetime)):
        return None
    try:
        return as_utc(value)
    except ValueError:
        return None


def meeting_window(
    m: MeetingLike, *, now: datetime
) -> tuple[datetime, Optional[datetime]]:
    """The time R1 compares an entry against (see the module docstring)."""
    start = m.start or now
    if m.end is None and is_live(m.status):
        return start, now
    return start, m.end


def _earliest(candidates: Iterable[_M], *, now: datetime) -> Optional[_M]:
    return min(candidates, key=lambda m: (m.start or now, m.id), default=None)


def match_entry(
    entry: Timed, candidates: Sequence[_M], *, now: datetime, lead_s: float
) -> Optional[_M]:
    """R1: the non-finished candidate the entry overlaps, or ``None`` for a new meeting."""

    def hit(m: _M) -> bool:
        start, end = meeting_window(m, now=now)
        if m.end is None and is_live(m.status):
            due = entry.start <= now + timedelta(seconds=lead_s)
            return due and (entry.end is None or entry.end > start)
        return overlaps(entry.start, entry.end, start, end)

    return _earliest(
        (m for m in candidates if m.status not in FINISHED_STATUSES and hit(m)), now=now
    )


def join_now_target(
    candidates: Sequence[_M], *, now: datetime, adopt_ahead_s: float
) -> Optional[_M]:
    """R1 ``join_now``: the meeting a pasted link adopts, or ``None`` for a new open-ended one."""
    horizon = now + timedelta(seconds=adopt_ahead_s)

    def adoptable(m: _M) -> bool:
        if m.status in FINISHED_STATUSES or (m.start or now) > horizon:
            return False
        return is_live(m.status) or m.end is None or m.end > now

    return _earliest((m for m in candidates if adoptable(m)), now=now)


def recompute(entries: Sequence[Planned]) -> Plan:
    """The plan of a meeting holding ``entries`` (at least one)."""
    if not entries:
        raise ValueError("a meeting's plan needs at least one entry")
    ordered = sorted(entries, key=lambda e: e.start)
    ends = [e.end for e in ordered]
    return Plan(
        start=ordered[0].start,
        end=None if any(end is None for end in ends) else max(e for e in ends if e),
        title=next((e.title for e in ordered if e.title is not None), None),
        time_zone=next((e.time_zone for e in ordered if e.time_zone is not None), None),
        meeting_url=ordered[0].meeting_url,
    )


def finished_window(
    start: Optional[datetime], *, finish: datetime
) -> tuple[datetime, datetime]:
    """R10: ``[start, finish)``, the end clamped to be no earlier than the start."""
    begin = start or finish
    return begin, max(finish, begin)


def is_future_move(
    entry: Timed, meeting: MeetingLike, *, now: datetime, lead_s: float
) -> bool:
    """R7: an update of an entry of a started (live or finished) meeting that points the entry to
    a new future time (see the module docstring)."""
    if meeting.status in FINISHED_STATUSES:
        finish = now
    elif meeting.end is not None:
        finish = max(now, meeting.end)
    else:
        finish = now + timedelta(seconds=lead_s)
    window = finished_window(meeting.start, finish=finish)
    return (
        not overlaps(entry.start, entry.end, *window)
        and entry.start > window[0]
        and entry.start > now
    )


def is_overdue(
    start: Optional[datetime],
    end: Optional[datetime],
    *,
    now: datetime,
    open_ended_s: float,
) -> bool:
    """R6: ``now`` is at or past the meeting's end; an open-ended meeting's end is ``start +
    open_ended_s``. A meeting with neither a start nor an end is never overdue."""
    if end is not None:
        return now >= end
    return start is not None and now >= start + timedelta(seconds=open_ended_s)
