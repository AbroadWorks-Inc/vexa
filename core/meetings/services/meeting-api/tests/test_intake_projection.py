"""§2.4/§1.1 — the one meeting projection.

``project_meeting`` is the single function every reply, read and webhook renders a meeting
through (task A3). Pure: it takes a ``meetings`` row mapping, a ``meeting_aw_state`` row mapping
(or ``None``), and the meeting's ``meeting_entries`` rows, and returns exactly the §2.4 ``meeting``
object — no DB, no clock, no network.

Field-source decisions this test pins (see the task report for the full reasoning):
  * ``title`` / ``meeting_url`` come from ``data.title`` / ``data.constructed_meeting_url`` — the
    keys ``collector/adapters.py`` and ``app.py``'s ``_meeting_projection_from_row`` already read
    (there is no plain ``data.meeting_url``).
  * ``completion_reason`` / ``failure_stage`` come from ``data.completion_reason`` /
    ``data.failure_stage`` (``app.py``'s own hoist does the same).
  * ``bot_joins_at`` once sent uses ``data.auto_join_last_attempt`` (``bot_spawn/auto_join.py``'s
    own dispatch stamp, written BEFORE every spawn attempt) — the only recorded "the bot was sent
    at this instant" fact in the codebase. A meeting sent by a path that never stamps it renders
    ``None`` rather than an invented value.
"""

from __future__ import annotations

from typing import Any

from meeting_api.intake.projection import project_meeting

UUID = "5f0c2b7e-8d1a-4c3e-9b6f-2a7d1e4c8b90"

EXPECTED_KEYS = [
    "id",
    "status",
    "completion_reason",
    "failure_stage",
    "outcome",
    "platform",
    "room",
    "meeting_url",
    "title",
    "start",
    "end",
    "time_zone",
    "bot_joins_at",
    "entries",
    "export",
    "sequence",
]


def _meeting(**over: Any) -> dict:
    base = {
        "id": 42,
        "uuid": UUID,
        "user_id": 7,
        "status": "scheduled",
        "platform": "google_meet",
        "platform_specific_id": "kxo-misr-avz",
        "data": {
            "title": "Weekly sync",
            "constructed_meeting_url": "https://meet.google.com/kxo-misr-avz",
            "scheduled_at": "2026-09-29T09:00:00Z",
        },
        "start_time": None,
        "end_time": None,
        "created_at": "2026-09-20T00:00:00Z",
    }
    base.update(over)
    return base


def _aw(**over: Any) -> dict:
    base: dict[str, Any] = {
        "scheduled_end_at": "2026-09-29T09:30:00Z",
        "time_zone": "Asia/Kolkata",
        "event_seq": 1,
        "outcome_kind": None,
        "outcome_detail": None,
        "outcome_message": None,
        "outcome_at": None,
        "export_state": None,
        "export_s3_path": None,
        "export_error": None,
        "export_at": None,
    }
    base.update(over)
    return base


def _entry(**over: Any) -> dict:
    base = {
        "external_id": "google:3n5kq8example",
        "source_user": "a@abroadworks.com",
        "attendees": ["a@abroadworks.com", "b@abroadworks.com", "c@client.com"],
        "series_id": None,
        "metadata": None,
        "state": "active",
    }
    base.update(over)
    return base


def _project(meeting=None, aw=None, entries=(), lead_s=120):
    return project_meeting(
        meeting if meeting is not None else _meeting(),
        aw,
        entries,
        lead_s=lead_s,
    )


# ── every key, exact set and order ────────────────────────────────────────────────────────────


def test_every_key_present_exact_set_and_order():
    result = _project(aw=_aw(), entries=[_entry()])
    assert list(result.keys()) == EXPECTED_KEYS


def test_matches_the_section_2_4_example_values():
    result = _project(aw=_aw(), entries=[_entry()])
    assert result["id"] == UUID
    assert result["status"] == "scheduled"
    assert result["completion_reason"] is None
    assert result["failure_stage"] is None
    assert result["outcome"] is None
    assert result["platform"] == "google_meet"
    assert result["room"] == "kxo-misr-avz"
    assert result["meeting_url"] == "https://meet.google.com/kxo-misr-avz"
    assert result["title"] == "Weekly sync"
    assert result["start"] == "2026-09-29T09:00:00Z"
    assert result["end"] == "2026-09-29T09:30:00Z"
    assert result["time_zone"] == "Asia/Kolkata"
    assert result["bot_joins_at"] == "2026-09-29T08:58:00Z"
    assert result["entries"] == [
        {
            "external_id": "google:3n5kq8example",
            "user": "a@abroadworks.com",
            "attendees": ["a@abroadworks.com", "b@abroadworks.com", "c@client.com"],
            "series_id": None,
            "metadata": None,
        }
    ]
    assert result["export"] is None
    assert result["sequence"] == 1


# ── aw=None ────────────────────────────────────────────────────────────────────────────────────


def test_aw_none_outcome_null_and_sequence_zero():
    result = _project(aw=None, entries=[_entry()])
    assert result["outcome"] is None
    assert result["sequence"] == 0
    assert result["export"] is None
    assert result["end"] is None
    assert result["time_zone"] is None


# ── each outcome kind (§1.1) ───────────────────────────────────────────────────────────────────


def test_outcome_cancelled_by_calendar():
    aw = _aw(
        outcome_kind="cancelled_by_calendar",
        outcome_detail="entry_moved",
        outcome_message=None,
        outcome_at="2026-09-29T04:20:00Z",
    )
    result = _project(aw=aw)
    assert result["outcome"] == {
        "kind": "cancelled_by_calendar",
        "detail": "entry_moved",
        "message": None,
        "at": "2026-09-29T04:20:00Z",
    }


def test_outcome_not_sent():
    aw = _aw(
        outcome_kind="not_sent",
        outcome_detail="account_limit",
        outcome_message="bot limit reached (45 of 45)",
        outcome_at="2026-09-29T04:20:00Z",
    )
    result = _project(aw=aw)
    assert result["outcome"] == {
        "kind": "not_sent",
        "detail": "account_limit",
        "message": "bot limit reached (45 of 45)",
        "at": "2026-09-29T04:20:00Z",
    }


def test_outcome_merged_into_live():
    aw = _aw(
        outcome_kind="merged_into_live",
        outcome_detail=UUID,
        outcome_message=None,
        outcome_at="2026-09-29T09:01:00Z",
    )
    result = _project(aw=aw)
    assert result["outcome"] == {
        "kind": "merged_into_live",
        "detail": UUID,
        "message": None,
        "at": "2026-09-29T09:01:00Z",
    }


# ── bot_joins_at: all three cases ─────────────────────────────────────────────────────────────


def test_bot_joins_at_scheduled_is_scheduled_at_minus_lead():
    meeting = _meeting(status="scheduled")
    result = _project(meeting=meeting, aw=_aw(), lead_s=120)
    assert result["bot_joins_at"] == "2026-09-29T08:58:00Z"


def test_bot_joins_at_once_sent_is_the_recorded_dispatch_stamp():
    meeting = _meeting(status="active")
    meeting["data"]["auto_join_last_attempt"] = "2026-09-29T08:58:30+00:00"
    result = _project(meeting=meeting, aw=_aw(), lead_s=120)
    assert result["bot_joins_at"] == "2026-09-29T08:58:30Z"


def test_bot_joins_at_null_for_unsent_instant_join():
    meeting = _meeting(status="scheduled", data={})
    result = _project(meeting=meeting, aw=None)
    assert result["bot_joins_at"] is None


def test_bot_joins_at_null_once_sent_with_no_recorded_dispatch_stamp():
    # Sent (no longer 'scheduled'), but through a path that never stamped
    # auto_join_last_attempt (e.g. a direct spawn, not the auto-join sweep) — no invented value.
    meeting = _meeting(status="active", data={})
    result = _project(meeting=meeting, aw=_aw())
    assert result["bot_joins_at"] is None


# ── forbidden keys ─────────────────────────────────────────────────────────────────────────────


def test_forbidden_keys_absent():
    meeting = _meeting()
    meeting["data"]["webhook_secret"] = "should-never-appear"
    result = _project(meeting=meeting, aw=_aw(), entries=[_entry()])
    flat = _flatten(result)
    assert "user_id" not in flat
    assert 42 not in _all_values(result)  # the integer row id must never surface
    for key in flat:
        lowered = key.lower()
        assert "secret" not in lowered
        assert "token" not in lowered
    assert "should-never-appear" not in _all_values(result)


def _flatten(obj, prefix="") -> set:
    keys = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            keys.add(k)
            keys |= _flatten(v)
    elif isinstance(obj, list):
        for item in obj:
            keys |= _flatten(item)
    return keys


def _all_values(obj) -> list:
    values = []
    if isinstance(obj, dict):
        for v in obj.values():
            values.append(v)
            values.extend(_all_values(v))
    elif isinstance(obj, list):
        for item in obj:
            values.append(item)
            values.extend(_all_values(item))
    return values


# ── entries: active only ──────────────────────────────────────────────────────────────────────


def test_entries_only_active_are_rendered():
    active = _entry(external_id="google:active-1")
    removed = _entry(external_id="google:removed-1", state="removed")
    closed = _entry(external_id="google:closed-1", state="closed")
    result = _project(aw=_aw(), entries=[active, removed, closed])
    assert [e["external_id"] for e in result["entries"]] == ["google:active-1"]


def test_entries_empty_when_none_active():
    removed = _entry(state="removed")
    result = _project(aw=_aw(), entries=[removed])
    assert result["entries"] == []


# ── export ─────────────────────────────────────────────────────────────────────────────────────


def test_export_present_when_export_state_set():
    aw = _aw(
        export_state="done",
        export_s3_path="s3://bucket/key",
        export_error=None,
        export_at="2026-09-29T10:00:00Z",
    )
    result = _project(aw=aw)
    assert result["export"] == {
        "state": "done",
        "s3_path": "s3://bucket/key",
        "error": None,
        "at": "2026-09-29T10:00:00Z",
    }


def test_export_null_when_export_state_absent():
    result = _project(aw=_aw())
    assert result["export"] is None


# ── start falls back to start_time when data has no scheduled_at ────────────────────────────────


def test_start_falls_back_to_start_time():
    meeting = _meeting(data={"title": "Instant"}, start_time="2026-09-29T09:05:00Z")
    result = _project(meeting=meeting, aw=None)
    assert result["start"] == "2026-09-29T09:05:00Z"
