"""exporter.rerun — which meetings can be exported again, and what gets queued."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from exporter.notetaker import NotetakerError
from exporter.rerun import NotRerunnable, request_reruns, rerun_envelope
from exporter.vexa_client import MeetingApiError
from tests.builders import MEETING_UUID, meeting_v2


class _Meetings:
    def __init__(
        self, meetings: dict[str, dict[str, Any]], unreachable: set[str] | None = None
    ) -> None:
        self._meetings = meetings
        self._unreachable = unreachable or set()

    def meeting(self, meeting_id: str) -> dict[str, Any]:
        if meeting_id in self._unreachable:
            raise httpx.ConnectError("connection refused")
        if meeting_id not in self._meetings:
            raise MeetingApiError(404, f"/v2/meetings/{meeting_id}")
        return self._meetings[meeting_id]


class _Notetaker:
    def __init__(
        self, transcribing: set[str] | None = None, down: bool = False
    ) -> None:
        self._transcribing = transcribing or set()
        self._down = down

    def transcribing(self, meeting_id: str) -> bool:
        if self._down:
            raise NotetakerError("transport error: connection refused")
        return meeting_id in self._transcribing


class _Queue:
    def __init__(self, pending: dict[str, bool] | None = None) -> None:
        self.queued: list[tuple[dict[str, Any], bool]] = []
        self._pending = pending or {}  # meeting id -> rerun

    def request_rerun(self, envelope: dict[str, Any]) -> str:
        meeting_id = envelope["data"]["meeting"]["id"]
        if meeting_id in self._pending:
            return "rerun_queued" if self._pending[meeting_id] else "export_in_progress"
        self._pending[meeting_id] = True
        self.queued.append((envelope, True))
        return "queued"


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

    refused = request_reruns(
        [MEETING_UUID, live, "missing"], meetings, queue, _Notetaker()  # type: ignore[arg-type]
    )

    assert refused == [live, "missing"]
    assert [
        (envelope["data"]["meeting"]["id"], rerun) for envelope, rerun in queue.queued
    ] == [(MEETING_UUID, True)]


def test_a_meeting_being_transcribed_is_refused_and_nothing_is_queued(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A rerun would rewrite the folder under the running job."""
    queue = _Queue()
    meetings = _Meetings({MEETING_UUID: meeting_v2(status="completed")})

    refused = request_reruns(
        [MEETING_UUID], meetings, queue, _Notetaker({MEETING_UUID})  # type: ignore[arg-type]
    )

    assert refused == [MEETING_UUID]
    assert queue.queued == []
    assert "transcription in progress for this meeting" in capsys.readouterr().out


def test_a_worker_that_cannot_say_refuses_the_rerun() -> None:
    queue = _Queue()
    meetings = _Meetings({MEETING_UUID: meeting_v2(status="completed")})

    refused = request_reruns(
        [MEETING_UUID], meetings, queue, _Notetaker(down=True)  # type: ignore[arg-type]
    )

    assert refused == [MEETING_UUID]
    assert queue.queued == []


@pytest.mark.parametrize(
    ("pending_rerun", "reason"),
    [(False, "export in progress for this meeting"), (True, "rerun already queued")],
)
def test_a_meeting_still_in_the_queue_is_refused(
    pending_rerun: bool, reason: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """P9: a rerun would rewrite the folder its running export hands off, and a
    second rerun would be folded into the first without a word."""
    queue = _Queue({MEETING_UUID: pending_rerun})
    meetings = _Meetings({MEETING_UUID: meeting_v2(status="completed")})

    refused = request_reruns(
        [MEETING_UUID], meetings, queue, _Notetaker()  # type: ignore[arg-type]
    )

    assert refused == [MEETING_UUID]
    assert queue.queued == []
    assert f"refused {MEETING_UUID}: {reason}" in capsys.readouterr().out


def test_an_id_that_cannot_be_read_fails_alone_and_the_others_are_queued(
    capsys: pytest.CaptureFixture[str],
) -> None:
    first, unreachable, broken = (
        "00000000-0000-4000-8000-00000000000a",
        "00000000-0000-4000-8000-00000000000b",
        MEETING_UUID,
    )
    last = "00000000-0000-4000-8000-00000000000c"
    queue = _Queue()
    meetings = _Meetings(
        {
            first: meeting_v2(id=first, status="completed"),
            broken: meeting_v2(id=broken, status="completed"),
            last: meeting_v2(id=last, status="completed"),
        },
        unreachable={unreachable},
    )

    class _Status(_Notetaker):
        def transcribing(self, meeting_id: str) -> bool:
            if meeting_id == broken:
                raise NotetakerError("notetaker /status body is not a JSON object")
            return False

    refused = request_reruns(
        [first, unreachable, broken, last], meetings, queue, _Status()  # type: ignore[arg-type]
    )

    assert refused == [unreachable, broken]
    assert [envelope["data"]["meeting"]["id"] for envelope, _ in queue.queued] == [
        first,
        last,
    ]
    out = capsys.readouterr().out
    assert f"failed {unreachable}: ConnectError" in out
    assert f"refused {broken}: notetaker /status body" in out


def test_a_storage_fault_while_queuing_fails_that_id_alone(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Review 3: an S3 fault on one id must not abort the others."""
    from botocore.exceptions import ClientError, EndpointConnectionError

    from exporter.storage import PreconditionFailed

    ids = [f"00000000-0000-4000-8000-00000000001{n}" for n in range(4)]
    faults: dict[str, Exception] = {
        ids[0]: ClientError({"Error": {"Code": "AccessDenied"}}, "PutObject"),
        ids[1]: EndpointConnectionError(endpoint_url="https://s3"),
        ids[2]: PreconditionFailed("pending item kept changing"),
    }

    class _FaultyQueue(_Queue):
        def request_rerun(self, envelope: dict[str, Any]) -> str:
            meeting_id = envelope["data"]["meeting"]["id"]
            if meeting_id in faults:
                raise faults[meeting_id]
            return super().request_rerun(envelope)

    queue = _FaultyQueue()
    meetings = _Meetings({i: meeting_v2(id=i, status="completed") for i in ids})

    refused = request_reruns(ids, meetings, queue, _Notetaker())  # type: ignore[arg-type]

    assert refused == ids[:3]
    assert [envelope["data"]["meeting"]["id"] for envelope, _ in queue.queued] == [
        ids[3]
    ]
    out = capsys.readouterr().out
    assert all(f"failed {i}:" in out for i in ids[:3])
