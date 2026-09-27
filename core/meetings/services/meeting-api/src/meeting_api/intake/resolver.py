"""The one link resolver (§1.6): which meeting an upstream route that takes a link means.

Upstream routes address a meeting by its link, platform plus room code, and one link holds many
meetings: the past ones, the live one and every scheduled occurrence. ``resolve`` picks the one a
route kind means from the link's rows as ``LinkRow``; ``adapters.link_rows`` reads them from
Postgres and ``fakes.link_rows_in`` is its in-memory twin, so both stores choose with this code.

  * ``READ`` (``GET /transcripts/{p}/{n}``, participants, ``POST /ws/authorize-subscribe``, chat,
    ``annotate``, docs, ``continue_meeting``): the live meeting, else the most recent that has
    started; never a future one. A meeting has started when it has left planning (any status but
    ``scheduled``/``idle``, whatever its ``scheduled_at``: a bot can run and finish ahead of its
    slot) or when its start is at or before ``now``.
  * ``PLANNED_EDIT`` (``PATCH``/``DELETE /meetings/{p}/{n}``, ``PUT …/intent``, ``POST
    …/workspace``, ``POST …/share``): the live meeting, else the single planned one (``scheduled``
    or ``idle``) that is not stale; two or more raise ``AmbiguousRoom``. A plan is stale only when
    it is entry-less, ``scheduled`` and timed (a parseable ``data.scheduled_at``, ``planned_at``)
    and that time plus ``AUTO_JOIN_GRACE_S`` has passed: the sweep will never send it (Ruling R22).
    An ``idle`` row or any untimed plan never goes stale and stays editable by link; an
    entry-managed plan never does either (the not-sent sweep ends it). A link with neither resolves
    as ``READ`` does, so a finished meeting stays addressable.
  * ``STOP`` (``DELETE /bots/{p}/{n}``): the live meeting only; a stop never cancels a plan.

"Live" is ``rules.is_live`` (``LIVE_STATUSES``) and a meeting's start is ``rules.meeting_start``.
Live rows go newest first (``created_at``, then id): the live unique index allows one per link, and
``resolve_all(rows, STOP)`` hands back every live row so a duplicate bot is stopped with it.
Started rows go by start, then id.

``ManagedByEntries`` is the refusal of an upstream edit to a meeting with at least one
``meeting_entries`` row: such a meeting is edited only through ``/v2/entries``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Optional, Sequence

from .rules import as_utc, is_live, meeting_start, scheduled_time

__all__ = [
    "PLANNED_STATUSES",
    "AmbiguousRoom",
    "LinkKind",
    "LinkRow",
    "ManagedByEntries",
    "resolve",
    "resolve_all",
]

PLANNED_STATUSES = frozenset({"scheduled", "idle"})

_OLDEST = datetime.min.replace(tzinfo=timezone.utc)


class LinkKind(str, Enum):
    READ = "read"
    PLANNED_EDIT = "planned_edit"
    STOP = "stop"


@dataclass(frozen=True)
class LinkRow:
    """One ``meetings`` row on a link: its status, its start (``rules.meeting_start``), its
    ``created_at``, whether entries manage it (it has a ``meeting_entries`` row) and
    ``planned_at``, its ``data.scheduled_at`` alone (``rules.scheduled_time``; ``None`` for an
    untimed row)."""

    id: int
    status: str
    start: Optional[datetime]
    created: Optional[datetime]
    managed: bool = False
    planned_at: Optional[datetime] = None

    @classmethod
    def of(
        cls,
        meeting_id: int,
        status: str,
        data: Any,
        start_time: Any,
        created_at: Any,
        managed: bool = False,
    ) -> LinkRow:
        return cls(
            int(meeting_id),
            str(status),
            meeting_start(data, start_time, created_at),
            as_utc(created_at),
            bool(managed),
            scheduled_time(data),
        )


class AmbiguousRoom(Exception):
    """A planned edit on a link holding several planned meetings and no live one (409)."""

    code = "ambiguous_room"

    def __init__(self, meeting_ids: Sequence[int]) -> None:
        self.meeting_ids = tuple(sorted(meeting_ids))
        super().__init__(
            f"the link holds several planned meetings: {list(self.meeting_ids)}"
        )


class ManagedByEntries(Exception):
    """An upstream edit of a meeting that entries manage (409)."""

    code = "managed_by_entries"

    def __init__(self, meeting_id: int) -> None:
        self.meeting_id = meeting_id
        super().__init__(
            f"meeting {meeting_id} is managed by entries; edit it through /v2/entries"
        )


def _live(rows: Sequence[LinkRow]) -> list[LinkRow]:
    return sorted(
        (r for r in rows if is_live(r.status)),
        key=lambda r: (r.created or _OLDEST, r.id),
        reverse=True,
    )


def _most_recent_started(rows: Sequence[LinkRow], now: datetime) -> list[LinkRow]:
    started = [
        r
        for r in rows
        if r.status not in PLANNED_STATUSES or (r.start is not None and r.start <= now)
    ]
    if not started:
        return []
    return [max(started, key=lambda r: (r.start or _OLDEST, r.id))]


def _stale(row: LinkRow, now: datetime, grace_s: float) -> bool:
    """A timed, entry-less ``scheduled`` plan past its ``scheduled_at`` plus the grace."""
    return (
        row.status == "scheduled"
        and not row.managed
        and row.planned_at is not None
        and row.planned_at + timedelta(seconds=grace_s) < now
    )


def resolve_all(
    rows: Sequence[LinkRow],
    kind: LinkKind,
    *,
    now: datetime,
    grace_s: Optional[float] = None,
) -> list[LinkRow]:
    """Every row ``kind`` addresses, the one it resolves to first: at most one for ``READ`` and
    ``PLANNED_EDIT``, every live row for ``STOP``. Raises ``AmbiguousRoom`` (``PLANNED_EDIT``).
    ``grace_s`` defaults to ``AUTO_JOIN_GRACE_S`` (``settings.auto_join_grace_s``).
    """
    live = _live(rows)
    if kind is LinkKind.STOP:
        return live
    if live:
        return live[:1]
    if kind is LinkKind.PLANNED_EDIT:
        if grace_s is None:
            from .settings import auto_join_grace_s

            grace_s = auto_join_grace_s()
        planned = [
            r
            for r in rows
            if r.status in PLANNED_STATUSES and not _stale(r, now, grace_s)
        ]
        if len(planned) > 1:
            raise AmbiguousRoom([r.id for r in planned])
        if planned:
            return planned
    return _most_recent_started(rows, now)


def resolve(
    rows: Sequence[LinkRow],
    kind: LinkKind,
    *,
    now: datetime,
    grace_s: Optional[float] = None,
) -> Optional[LinkRow]:
    """The meeting ``kind`` resolves the link's ``rows`` to, or ``None``."""
    found = resolve_all(rows, kind, now=now, grace_s=grace_s)
    return found[0] if found else None
