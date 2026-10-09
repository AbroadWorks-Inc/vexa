"""exporter.rerun — which meetings can be exported again, and what gets queued."""

from __future__ import annotations

from typing import Any

import pytest

from exporter.rerun import NotRerunnable, request_reruns, rerun_envelope
from exporter.vexa_client import MeetingApiError
from tests.builders import MEETING_UUID, meeting_v2


class _Meetings:
    def __init__(self, meetings: dict[str, dict[str, Any]]) -> None:
        self._meetings = meetings

    def meeting(self, meeting_id: str) -> dict[str, Any]:
        if meeting_id not in self._meetings:
            raise MeetingApiError(404, f"/v2/meetings/{meeting_id}")
        return self._meetings[meeting_id]


class _Queue:
    def __init__(self) -> None:
        self.queued: list[tuple[dict[str, Any], bool]] = []

    def enqueue(self, envelope: dict[str, Any], *, rerun: bool = False) -> None:
        self.queued.append((envelope, rerun))


@pytest.mark.parametrize(
    ("status", "event_type"),
    [("completed", "meeting.completed"), ("failed", "bot.failed")],
)
def test_a_finished_meeting_is_queued_as_the_webhook_that_finished_it(
    status: str, event_type: str
) -> None:
    meeting = meeting_v2(status=status)
    assert rerun_envelope(meeting) == {
        "event_type": event_type,
        "data": {"meeting": meeting},
    }


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"status": "active"}, "is not finished"),
        ({"outcome": {"kind": "not_sent"}}, "no bot was sent"),
        ({"started_at": None}, "never in the meeting"),
        ({"upstream_id": None}, "lacks a field"),
    ],
)
def test_a_meeting_that_cannot_be_exported_is_refused(
    overrides: dict[str, Any], reason: str
) -> None:
    with pytest.raises(NotRerunnable, match=reason):
        rerun_envelope(meeting_v2(**{"status": "completed", **overrides}))


def test_each_id_is_queued_as_a_rerun_and_the_refused_ones_are_returned() -> None:
    queue = _Queue()
    live = "00000000-0000-4000-8000-000000000001"
    meetings = _Meetings(
        {
            MEETING_UUID: meeting_v2(status="completed"),
            live: meeting_v2(status="active"),
        }
    )

    refused = request_reruns([MEETING_UUID, live, "missing"], meetings, queue)  # type: ignore[arg-type]

    assert refused == [live, "missing"]
    assert [
        (envelope["data"]["meeting"]["id"], rerun) for envelope, rerun in queue.queued
    ] == [(MEETING_UUID, True)]
