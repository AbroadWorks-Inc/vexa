"""The ``webhook.v1`` envelope goldens are the real builders' output (§1.8, §2.7).

``Envelope.meeting-completed.json`` and ``Envelope.bot-failed.json`` are what the lifecycle callback
sends the legacy system and per-user URLs: ``lifecycle.webhook.build_typed_envelope`` around
``app.legacy_meeting_projection`` of the row the repo's status write returns (the row plus the
meeting's ``uuid``, ``entries``, ``outcome`` and ``sequence`` from the one meeting projection). Each
test builds its envelope from fixed inputs and compares it with the golden file, so a builder change
shows up here. The goldens are never edited by hand: to regenerate them after an intended change,
run this module with ``WEBHOOK_V1_GOLDENS_WRITE=1``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from meeting_api.app import legacy_meeting_projection
from meeting_api.bot_spawn.adapters import _with_projection
from meeting_api.intake.projection import project_meeting
from meeting_api.lifecycle.machine import LifecycleSink, MeetingStore, TransitionSource
from meeting_api.lifecycle.webhook import build_typed_envelope

UUID = "5f0c2b7e-8d1a-4c3e-9b6f-2a7d1e4c8b90"


def _golden_path(name: str) -> Path:
    rel = Path("meetings") / "contracts" / "webhook.v1" / "golden" / f"{name}.json"
    for parent in Path(__file__).resolve().parents:
        if (parent / rel).is_file():
            return parent / rel
    raise FileNotFoundError(rel)


def _change(connection_id: str, *events: dict[str, Any]) -> Any:
    sink = LifecycleSink(store=MeetingStore())
    change = None
    for event in events:
        change = sink.apply_change(
            {"connection_id": connection_id, **event},
            transition_source=TransitionSource.BOT_CALLBACK,
        )
    return change


def _row(meeting: dict[str, Any], aw: dict[str, Any], entries: list[dict]) -> dict:
    """The row as ``SqlAlchemyMeetingRepo.update_meeting_status`` returns it."""
    stored = {
        **meeting,
        "uuid": UUID,
        "platform_specific_id": meeting["native_meeting_id"],
    }
    return _with_projection(meeting, project_meeting(stored, aw, entries, lead_s=300))


def _completed() -> dict:
    change = _change(
        "sess-golden-completed",
        {"status": "joining", "timestamp": "2026-06-18T09:59:40.000Z"},
        {"status": "active", "timestamp": "2026-06-18T10:00:00.000Z"},
        {
            "status": "completed",
            "completion_reason": "stopped",
            "timestamp": "2026-06-18T10:42:00.000Z",
        },
    )
    row = _row(
        {
            "id": 11367,
            "user_id": 7,
            "platform": "google_meet",
            "native_meeting_id": "abc-defg-hij",
            "constructed_meeting_url": "https://meet.google.com/abc-defg-hij",
            "status": "completed",
            "start_time": "2026-06-18T10:00:00.000Z",
            "end_time": "2026-06-18T10:42:00.000Z",
            "data": {
                "name": "Weekly sync",
                "completion_reason": "stopped",
                "service_provenance": {
                    "bot_admitted_at": "2026-06-18T10:00:00.000Z",
                    "bot_departed_at": "2026-06-18T10:42:00.000Z",
                    "bot_outcome": "served",
                    "transcription_provider": "customer",
                    "transcription_outcome": "served",
                    "lifecycle_contract_version": "2026-07-28",
                },
            },
            "created_at": "2026-06-18T09:59:30.000Z",
            "updated_at": "2026-06-18T10:42:00.000Z",
        },
        {"event_seq": 9, "outcome_kind": None},
        [
            {
                "external_id": "google:3n5kq8example",
                "source_user": "a@abroadworks.com",
                "attendees": ["a@abroadworks.com", "b@example.com"],
                "series_id": "google:series-weekly",
                "metadata": {"crm_id": "42"},
                "state": "closed",
            }
        ],
    )
    envelope = build_typed_envelope(
        change,
        meeting=legacy_meeting_projection(row),
        created_at="2026-06-18T10:42:00.000Z",
    )
    assert envelope is not None
    return envelope


def _bot_failed() -> dict:
    change = _change(
        "sess-golden-failed",
        {"status": "joining", "timestamp": "2026-06-18T10:02:00.000Z"},
        {"status": "awaiting_admission", "timestamp": "2026-06-18T10:02:30.000Z"},
        {
            "status": "failed",
            "failure_stage": "awaiting_admission",
            "completion_reason": "awaiting_admission_rejected",
            "reason": "host denied admission",
            "timestamp": "2026-06-18T10:03:12.000Z",
        },
    )
    row = _row(
        {
            "id": 11368,
            "user_id": 7,
            "platform": "zoom",
            "native_meeting_id": "98765432101",
            "constructed_meeting_url": "https://zoom.us/j/98765432101",
            "status": "failed",
            "start_time": None,
            "end_time": "2026-06-18T10:03:12.000Z",
            "data": {
                "completion_reason": "awaiting_admission_rejected",
                "failure_stage": "awaiting_admission",
            },
            "created_at": "2026-06-18T10:02:00.000Z",
            "updated_at": "2026-06-18T10:03:12.000Z",
        },
        {"event_seq": 4, "outcome_kind": None},
        [],
    )
    envelope = build_typed_envelope(
        change,
        meeting=legacy_meeting_projection(row),
        created_at="2026-06-18T10:03:12.000Z",
    )
    assert envelope is not None
    return envelope


@pytest.mark.parametrize(
    "name,build",
    [("Envelope.meeting-completed", _completed), ("Envelope.bot-failed", _bot_failed)],
)
def test_the_golden_is_the_builders_output(name, build):
    built = build()
    path = _golden_path(name)
    if os.getenv("WEBHOOK_V1_GOLDENS_WRITE") == "1":
        path.write_text(json.dumps(built, indent=2) + "\n")
    assert json.loads(path.read_text()) == built


def test_the_legacy_meeting_block_carries_the_meetings_uuid_entries_outcome_and_sequence():
    meeting = _completed()["data"]["meeting"]
    assert meeting["uuid"] == UUID
    assert meeting["sequence"] == 9
    assert meeting["outcome"] is None
    assert [e["external_id"] for e in meeting["entries"]] == ["google:3n5kq8example"]
