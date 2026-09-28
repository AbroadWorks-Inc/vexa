"""exporter.export_result — the export result reported through the gateway
(design §1.9), and its retries through the durable queue."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import boto3
import httpx
import pytest
from moto import mock_aws

from exporter.audio import join_webm
from exporter.config import Settings
from exporter.export_result import (
    ERROR_MAX_CHARS,
    ExportReporter,
    ExportReportError,
)
from exporter.job import Deps, ExportResult
from exporter.queue import PendingQueue, sweep_once
from exporter.storage import Storage
from tests.builders import write_silent_wav

UUID = "5f0c2b7e-8d1a-4c3e-9b6f-2a7d1e4c8b90"
KEY = "test-exporter-key"
VEXA_BUCKET = "aw-bots"
EXPORT_BUCKET = "aw-chatworks-transcribe"
FOLDER = "google_meet_abc-defg-hij_20260618T100000000Z"
BASE = f"recordings/{FOLDER}/"
S3_PATH = f"s3://{EXPORT_BUCKET}/{BASE}"


def _reporter(handler: Any) -> ExportReporter:
    return ExportReporter(
        "http://gateway/", KEY, httpx.Client(transport=httpx.MockTransport(handler))
    )


# ---------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------


def test_report_posts_the_result_with_the_exporter_key() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"id": UUID})

    _reporter(handler).report(UUID, "handed_off", S3_PATH)

    assert len(seen) == 1
    req = seen[0]
    assert req.method == "POST"
    assert str(req.url) == f"http://gateway/v2/meetings/{UUID}/export"
    assert req.headers["X-API-Key"] == KEY
    assert "X-User-Id" not in req.headers
    assert json.loads(req.content) == {"state": "handed_off", "s3_path": S3_PATH}


def test_a_failed_result_carries_its_error() -> None:
    bodies: list[Any] = []

    def handler(req: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(req.content))
        return httpx.Response(200, json={})

    _reporter(handler).report(UUID, "failed", S3_PATH, "notetaker 422")

    assert bodies == [{"state": "failed", "s3_path": S3_PATH, "error": "notetaker 422"}]


def test_a_long_error_is_cut_to_what_meeting_api_stores() -> None:
    bodies: list[Any] = []

    def handler(req: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(req.content))
        return httpx.Response(200, json={})

    _reporter(handler).report(UUID, "failed", S3_PATH, "x" * 5000)

    assert bodies[0]["error"] == "x" * ERROR_MAX_CHARS


def test_the_meeting_id_is_sent_as_one_path_segment() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={})

    _reporter(handler).report("a/../b?x", "handed_off", S3_PATH)

    assert seen[0].url.raw_path == b"/v2/meetings/a%2F..%2Fb%3Fx/export"


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 429, 500, 503])
def test_any_non_2xx_answer_is_not_accepted(status: int) -> None:
    reporter = _reporter(lambda req: httpx.Response(status, json={}))

    with pytest.raises(ExportReportError) as exc_info:
        reporter.report(UUID, "handed_off", S3_PATH)

    assert str(status) in str(exc_info.value)
    assert KEY not in str(exc_info.value)


def test_a_transport_error_is_not_accepted() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=req)

    with pytest.raises(ExportReportError):
        _reporter(handler).report(UUID, "handed_off", S3_PATH)


def test_report_makes_exactly_one_request() -> None:
    """The retry is the queue's (a failed job step runs again after its
    backoff), never a loop inside the client."""
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503)

    with pytest.raises(ExportReportError):
        _reporter(handler).report(UUID, "handed_off", S3_PATH)
    assert calls["n"] == 1


# ---------------------------------------------------------------------------
# Retried through the queue until accepted
# ---------------------------------------------------------------------------


@pytest.fixture
def storage(monkeypatch: pytest.MonkeyPatch) -> Iterator[Storage]:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=VEXA_BUCKET)
        client.create_bucket(Bucket=EXPORT_BUCKET)
        yield Storage(client)


def _envelope() -> dict[str, Any]:
    return {
        "event_id": "evt_test",
        "event_type": "meeting.completed",
        "data": {
            "meeting": {
                "id": 11367,
                "uuid": UUID,
                "user_id": 7,
                "platform": "google_meet",
                "native_meeting_id": "abc-defg-hij",
                "start_time": "2026-06-18T10:00:00.000Z",
                "end_time": None,
            }
        },
    }


class _MeetingApi:
    def __init__(self, storage_path: str) -> None:
        self._storage_path = storage_path

    def list_recordings(self, meeting_id: int) -> list[dict[str, Any]]:
        return [
            {
                "id": 3,
                "created_at": "2026-06-18T10:00:15.000Z",
                "media_files": [{"id": 1, "type": "audio", "format": "webm"}],
            }
        ]

    def master(self, recording_id: int) -> dict[str, Any]:
        return {"storage_path": self._storage_path}

    def transcript(self, meeting_id: int) -> dict[str, Any] | None:
        return None


class _Notetaker:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def process(self, meeting_id: str, s3_path: str, platform: str) -> None:
        self.calls.append(meeting_id)


class _Gateway:
    """The export route, answering 503 until ``failures`` requests are spent."""

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.bodies: list[Any] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(req.content))
        if self.failures > 0:
            self.failures -= 1
            return httpx.Response(503, json={"error": {"code": "unavailable"}})
        return httpx.Response(200, json={"id": UUID})


def _deps(storage: Storage, gateway: _Gateway, notetaker: _Notetaker) -> Deps:
    storage_path = "recordings/1/3/uid-1/audio/master.webm"
    storage.put_bytes(VEXA_BUCKET, storage_path, b"webm-bytes", "video/webm")

    def transcode(src: Path, dst: Path) -> None:
        write_silent_wav(dst, 5.0)

    settings = Settings(
        gateway_url="http://gateway",
        exporter_api_key=KEY,
        webhook_secret="s",
        vexa_bucket=VEXA_BUCKET,
        export_bucket=EXPORT_BUCKET,
        export_prefix="recordings/",
        notetaker_url="http://notetaker",
        max_attempts=5,
    )
    return Deps(
        settings=settings,
        storage=storage,
        meeting_api=_MeetingApi(storage_path),  # type: ignore[arg-type]
        notetaker=notetaker,  # type: ignore[arg-type]
        export_result=_reporter(gateway),
        transcode=transcode,
        now=lambda: datetime(2026, 6, 18, 11, 0, 0, tzinfo=timezone.utc),
        join_webm=join_webm,
    )


def _at(t: float) -> Callable[[], float]:
    return lambda: t


def _marker(storage: Storage) -> dict[str, Any]:
    marker = storage.get_json(EXPORT_BUCKET, BASE + "_export.json")
    assert isinstance(marker, dict)
    return marker


def test_the_result_is_retried_by_the_queue_until_accepted(storage: Storage) -> None:
    gateway = _Gateway(failures=2)
    notetaker = _Notetaker()
    deps = _deps(storage, gateway, notetaker)
    queue = PendingQueue(storage, VEXA_BUCKET)
    queue.enqueue(_envelope())

    asyncio.run(sweep_once(queue, deps, now=lambda: 1000.0))
    item = queue.load("11367")
    assert item is not None and item["attempts"] == 1
    assert "503" in item["last_error"]

    # Not visible again before its backoff: nothing is re-sent.
    asyncio.run(sweep_once(queue, deps, now=lambda: 1001.0))
    assert len(gateway.bodies) == 1

    asyncio.run(sweep_once(queue, deps, now=lambda: 5000.0))
    item = queue.load("11367")
    assert item is not None and item["attempts"] == 2

    asyncio.run(sweep_once(queue, deps, now=lambda: 50000.0))
    assert queue.pending_ids() == []
    assert gateway.bodies == [{"state": "handed_off", "s3_path": S3_PATH}] * 3
    # The folder was built and handed off once; the retries re-sent the report only.
    assert notetaker.calls == [UUID]
    marker = _marker(storage)
    assert marker["state"] == "handed_off"


def test_a_report_never_accepted_ends_in_failed_and_keeps_the_hand_off(
    storage: Storage, caplog: pytest.LogCaptureFixture
) -> None:
    """The retries are bounded by EXPORT_MAX_ATTEMPTS. The folder was handed
    off, so its `_export.json` stays `handed_off` and no `failed` result is
    sent in its place; the item lands in failed/ for an operator."""
    gateway = _Gateway(failures=100)
    notetaker = _Notetaker()
    deps = _deps(storage, gateway, notetaker)
    queue = PendingQueue(storage, VEXA_BUCKET)
    queue.enqueue(_envelope())

    caplog.set_level(logging.INFO, logger="exporter")
    for now in (1000.0, 1e5, 1e6, 1e7, 1e8):
        asyncio.run(sweep_once(queue, deps, now=_at(now)))

    assert queue.pending_ids() == []
    assert storage.get_json(VEXA_BUCKET, "aw-exporter/failed/11367.json") is not None
    assert len(gateway.bodies) == 5
    assert all(body["state"] == "handed_off" for body in gateway.bodies)
    assert notetaker.calls == [UUID]
    marker = _marker(storage)
    assert marker["state"] == "handed_off"
    assert any(
        "export result not accepted" in r.getMessage() and r.levelno == logging.ERROR
        for r in caplog.records
    )


def test_a_quarantined_export_reports_failed_with_its_error(storage: Storage) -> None:
    gateway = _Gateway(failures=0)
    deps = _deps(storage, gateway, _Notetaker())
    queue = PendingQueue(storage, VEXA_BUCKET)
    queue.enqueue(_envelope())

    def failing_job(envelope: dict[str, Any], deps: Deps) -> ExportResult:
        raise RuntimeError("notetaker 422")

    for now in (1000.0, 1e5, 1e6, 1e7, 1e8):
        asyncio.run(sweep_once(queue, deps, job=failing_job, now=_at(now)))

    assert queue.pending_ids() == []
    marker = _marker(storage)
    assert marker["state"] == "failed"
    assert marker["meeting_id"] == UUID
    assert marker["vexa_meeting_id"] == 11367
    assert gateway.bodies == [
        {"state": "failed", "s3_path": S3_PATH, "error": "notetaker 422"}
    ]


def test_a_failed_report_that_is_not_accepted_still_quarantines(
    storage: Storage, caplog: pytest.LogCaptureFixture
) -> None:
    gateway = _Gateway(failures=1)
    deps = _deps(storage, gateway, _Notetaker())
    queue = PendingQueue(storage, VEXA_BUCKET)
    queue.enqueue(_envelope())

    def failing_job(envelope: dict[str, Any], deps: Deps) -> ExportResult:
        raise RuntimeError("boom")

    caplog.set_level(logging.INFO, logger="exporter")
    for now in (1000.0, 1e5, 1e6, 1e7, 1e8):
        asyncio.run(sweep_once(queue, deps, job=failing_job, now=_at(now)))

    assert queue.pending_ids() == []
    failed = storage.get_json(VEXA_BUCKET, "aw-exporter/failed/11367.json")
    assert failed is not None
    assert len(gateway.bodies) == 1
    assert any(
        "export result not accepted" in r.getMessage() and r.levelno == logging.ERROR
        for r in caplog.records
    )


class _FlakyMarkerRead:
    """`Storage` whose reads of `_export.json` fail while `failing` is set: a
    transient S3 error on the folder's marker."""

    def __init__(self, storage: Storage) -> None:
        self._storage = storage
        self.failing = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._storage, name)

    def get_json(self, bucket: str, key: str) -> Any:
        if self.failing and key.endswith("_export.json"):
            raise OSError("transient S3 error")
        return self._storage.get_json(bucket, key)


def _handed_off_then_marker_read_fails(
    storage: Storage, gateway: _Gateway, *, quarantine_read_fails: bool
) -> tuple[PendingQueue, Deps]:
    """Attempts 1-4 hand off the folder but the report isn't accepted;
    attempt 5 fails reading `_export.json`, which sends the item to
    quarantine."""
    flaky = _FlakyMarkerRead(storage)
    deps = _deps(storage, gateway, _Notetaker())
    deps.storage = flaky  # type: ignore[assignment]
    queue = PendingQueue(storage, VEXA_BUCKET)
    queue.enqueue(_envelope())
    for now in (1000.0, 1e5, 1e6, 1e7):
        asyncio.run(sweep_once(queue, deps, now=_at(now)))
    assert _marker(storage)["state"] == "handed_off"
    assert len(gateway.bodies) == 4

    real_get_json = flaky.get_json
    reads = {"n": 0}

    def get_json(bucket: str, key: str) -> Any:
        if key.endswith("_export.json"):
            reads["n"] += 1
            if reads["n"] == 1 or quarantine_read_fails:
                raise OSError("transient S3 error")
        return real_get_json(bucket, key)

    flaky.get_json = get_json  # type: ignore[method-assign]
    asyncio.run(sweep_once(queue, deps, now=_at(1e8)))
    return queue, deps


def test_a_handed_off_folder_is_never_quarantined_as_failed(
    storage: Storage,
) -> None:
    gateway = _Gateway(failures=4)

    queue, _ = _handed_off_then_marker_read_fails(
        storage, gateway, quarantine_read_fails=False
    )

    assert queue.pending_ids() == []
    marker = _marker(storage)
    assert marker["state"] == "handed_off"
    assert gateway.bodies[-1] == {"state": "handed_off", "s3_path": S3_PATH}
    assert all(body["state"] == "handed_off" for body in gateway.bodies)


def test_an_unreadable_marker_at_quarantine_is_left_alone(
    storage: Storage, caplog: pytest.LogCaptureFixture
) -> None:
    gateway = _Gateway(failures=4)

    caplog.set_level(logging.INFO, logger="exporter")
    queue, _ = _handed_off_then_marker_read_fails(
        storage, gateway, quarantine_read_fails=True
    )

    assert queue.pending_ids() == []
    assert storage.get_json(VEXA_BUCKET, "aw-exporter/failed/11367.json") is not None
    assert _marker(storage)["state"] == "handed_off"
    assert len(gateway.bodies) == 4
    assert any(
        "could not read _export.json" in r.getMessage() and r.levelno == logging.ERROR
        for r in caplog.records
    )


def test_an_accepted_hand_off_at_quarantine_completes_the_item(
    storage: Storage,
) -> None:
    """The folder is handed off and meeting-api accepted the report: the item
    is finished, so it leaves the queue without landing in failed/."""
    gateway = _Gateway(failures=4)

    queue, _ = _handed_off_then_marker_read_fails(
        storage, gateway, quarantine_read_fails=False
    )

    assert queue.pending_ids() == []
    assert storage.get_json(VEXA_BUCKET, "aw-exporter/failed/11367.json") is None
    assert gateway.bodies[-1] == {"state": "handed_off", "s3_path": S3_PATH}


def test_an_unaccepted_hand_off_at_quarantine_goes_to_failed(
    storage: Storage,
) -> None:
    gateway = _Gateway(failures=5)

    queue, _ = _handed_off_then_marker_read_fails(
        storage, gateway, quarantine_read_fails=False
    )

    assert queue.pending_ids() == []
    assert storage.get_json(VEXA_BUCKET, "aw-exporter/failed/11367.json") is not None
    assert _marker(storage)["state"] == "handed_off"
    assert len(gateway.bodies) == 5
    assert gateway.bodies[-1] == {"state": "handed_off", "s3_path": S3_PATH}
