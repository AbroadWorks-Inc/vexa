"""``project_meeting`` — the one meeting projection (§2.4, §1.1).

Pure: no DB, no clock, no network. Every value comes from the ``meetings`` row mapping, the
``meeting_aw_state`` row mapping (or ``None`` when the meeting has no aw-state row), and the
meeting's ``meeting_entries`` row mappings, all handed in by the caller. This is the single
renderer every reply (§2.4), read and webhook uses, so a route or a delivery worker never grows
its own second copy of "what a meeting looks like".

Field-source decisions (grounded in the existing code, not invented):
  * ``completion_reason`` / ``failure_stage`` — ``data.completion_reason`` / ``data.failure_stage``,
    the keys the lifecycle machine already writes there (mirrors ``app.py``'s own
    ``_meeting_projection_from_row`` hoist).
  * ``title`` / ``meeting_url`` — ``data.title`` / ``data.constructed_meeting_url``, the keys
    ``collector/adapters.py`` and ``bot_spawn/service.py`` already write; there is no plain
    ``data.meeting_url``.
  * ``bot_joins_at`` once sent — ``data.auto_join_last_attempt``, the "the bot was sent at this
    instant" stamp: ``bot_spawn/auto_join.py`` writes it before every dispatch attempt and the
    exact-row claim (``intake/spawn.py``) writes it when it claims the row. A meeting sent through
    a path that never stamps it (upstream ``POST /bots``) renders ``None`` here rather than an
    invented value.
  * ``upstream_id`` — the integer row id, the one the upstream reads take
    (``GET /recordings?meeting_id=``, ``GET /transcripts/by-id/{id}``) and the bot's signal files are
    keyed by; the exporter reads a meeting's recordings with it (§1.9).
  * ``started_at`` / ``ended_at`` — the ``start_time`` / ``end_time`` columns: when the bot first
    went ``active`` and when the meeting finished, stamped by the lifecycle write. ``start`` stays
    the planned time.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional, Sequence

from .rules import scheduled_time

__all__ = ["iso_utc", "project_meeting"]


def iso_utc(value: Any) -> Optional[str]:
    """A datetime or an ISO-8601 string → a UTC ISO-8601 string with a trailing ``Z``.

    Mirrors ``bot_spawn/adapters.py``'s ``_iso_utc`` (naive means UTC), extended to also accept an
    already-string timestamp so this function stays agnostic to whether the caller's mapping holds
    raw ``datetime`` objects or the ISO strings a repo's row-to-dict already produced.
    """
    if value is None:
        return None
    dt = (
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        if isinstance(value, str)
        else value
    )
    aware = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return aware.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _bot_joins_at(
    meeting: Mapping[str, Any], data: Mapping[str, Any], *, lead_s: int
) -> Optional[str]:
    """§1.1 R6 / §2.4 — when the bot is due at the room.

    * Still ``scheduled`` with a known time (``data.scheduled_at``): ``scheduled_at - lead_s`` —
      the bot hasn't been sent, this is the forward projection.
    * Waiting for its next bot after one failed (§6.9 F-K2: ``requested`` with
      ``data.bot_retry``): when that bot is due, the retry's ``due_at``.
    * Sent (any other status): the auto-join sweep's own dispatch stamp
      (``data.auto_join_last_attempt``) if one was recorded, else ``None``.
    * Neither (an unsent instant join with no schedule, or a ``scheduled_at`` that isn't a time):
      ``None``.
    """
    scheduled_at = scheduled_time(data)
    if meeting.get("status") == "scheduled" and scheduled_at is not None:
        return iso_utc(scheduled_at - timedelta(seconds=lead_s))
    retry = data.get("bot_retry")
    if meeting.get("status") == "requested" and isinstance(retry, Mapping):
        return iso_utc(retry.get("due_at"))
    return iso_utc(data.get("auto_join_last_attempt"))


def _outcome(aw: Optional[Mapping[str, Any]]) -> Optional[dict]:
    if aw is None or aw.get("outcome_kind") is None:
        return None
    return {
        "kind": aw.get("outcome_kind"),
        "detail": aw.get("outcome_detail"),
        "message": aw.get("outcome_message"),
        "at": iso_utc(aw.get("outcome_at")),
    }


def _export(aw: Optional[Mapping[str, Any]]) -> Optional[dict]:
    if aw is None or aw.get("export_state") is None:
        return None
    return {
        "state": aw.get("export_state"),
        "s3_path": aw.get("export_s3_path"),
        "error": aw.get("export_error"),
        "at": iso_utc(aw.get("export_at")),
    }


def _data_of(meeting: Mapping[str, Any]) -> dict[str, Any]:
    """The meeting row's ``data`` blob, or ``{}`` when absent/malformed."""
    value = meeting.get("data")
    return value if isinstance(value, dict) else {}


def _entry(entry: Mapping[str, Any]) -> dict:
    return {
        "external_id": entry.get("external_id"),
        "user": entry.get("source_user"),
        "attendees": entry.get("attendees"),
        "series_id": entry.get("series_id"),
        "metadata": entry.get("metadata"),
    }


def _listed(entries: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """R9, R7: the meeting's active and closed entries, never removed ones, each entry once. A
    closed entry is history: who a finished meeting was for, or an entry that moved from a live
    meeting to a new time. An entry holding two rows here (it left and came back) is listed at
    its first place with its active row, else its newest closed one."""
    chosen: dict[tuple[Any, Any], Mapping[str, Any]] = {}
    for entry in entries:
        if entry.get("state") not in ("active", "closed"):
            continue
        key = (entry.get("source_user"), entry.get("external_id"))
        held = chosen.get(key)
        if held is None or held.get("state") != "active":
            chosen[key] = entry
    return list(chosen.values())


def project_meeting(
    meeting: Mapping[str, Any],
    aw: Optional[Mapping[str, Any]],
    entries: Sequence[Mapping[str, Any]],
    *,
    lead_s: int,
) -> dict[str, Any]:
    """The §2.4 ``meeting`` object, exactly — no more, no fewer keys, this order.

    Never contains ``user_id`` or any secret/token: ``id`` is always ``str(meeting["uuid"])``, the
    integer row id is only ``upstream_id``, and everything else is drawn from the named
    ``data``/``aw`` keys below, never a verbatim copy of ``data`` or ``aw``.
    """
    data = _data_of(meeting)
    return {
        "id": str(meeting["uuid"]),
        "upstream_id": meeting.get("id"),
        "status": meeting.get("status"),
        "completion_reason": data.get("completion_reason"),
        "failure_stage": data.get("failure_stage"),
        "outcome": _outcome(aw),
        "platform": meeting.get("platform"),
        "room": meeting.get("platform_specific_id"),
        "meeting_url": data.get("constructed_meeting_url"),
        "title": data.get("title"),
        "start": iso_utc(scheduled_time(data)) or iso_utc(meeting.get("start_time")),
        "end": iso_utc(aw.get("scheduled_end_at")) if aw is not None else None,
        "time_zone": aw.get("time_zone") if aw is not None else None,
        "bot_joins_at": _bot_joins_at(meeting, data, lead_s=lead_s),
        "started_at": iso_utc(meeting.get("start_time")),
        "ended_at": iso_utc(meeting.get("end_time")),
        "entries": [_entry(e) for e in _listed(entries)],
        "export": _export(aw),
        "sequence": aw.get("event_seq", 0) if aw is not None else 0,
    }
