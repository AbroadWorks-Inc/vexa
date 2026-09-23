"""Tests for exporter.queue (durable pending queue + sweep worker) and
exporter.app (signed webhook intake) — spec §4.1."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import boto3
import httpx
import pytest
from moto import mock_aws

# starlette's TestClient prefers the (not-yet-installed) httpx2 package and
# warns on the still-supported httpx fallback; this pins that fallback's
# deprecation notice down without silencing real warnings from our own code.
warnings.filterwarnings(
    "ignore",
    message=r"Using `httpx` with `starlette\.testclient` is deprecated.*",
    category=UserWarning,
)

from fastapi.testclient import TestClient  # noqa: E402

import exporter.queue as queue_module  # noqa: E402
from exporter.app import create_app  # noqa: E402
from exporter.config import Settings  # noqa: E402
from exporter.job import Deps, ExportResult  # noqa: E402
from exporter.naming import folder_name  # noqa: E402
from exporter.notetaker import Notetaker  # noqa: E402
from exporter.queue import PendingQueue, run_worker, sweep_once  # noqa: E402
from exporter.storage import Storage  # noqa: E402
from exporter.vexa_client import MeetingApi  # noqa: E402

WEBHOOK_SECRET = "test-secret"
VEXA_BUCKET = "aw-bots"
EXPORT_BUCKET = "aw-chatworks-transcribe"
MEETING = {
    "id": 11367,
    "user_id": 7,
    "platform": "google_meet",
    "native_meeting_id": "abc-defg-hij",
    "start_time": "2026-06-18T10:00:00.000Z",
    "end_time": "2026-06-18T10:42:00.000Z",
}
FOLDER = folder_name(
    str(MEETING["platform"]),
    str(MEETING["native_meeting_id"]),
    str(MEETING["start_time"]),
)
BASE = f"recordings/{FOLDER}/"


def _envelope(
    event_type: str = "meeting.completed", **meeting_overrides: Any
) -> dict[str, Any]:
    meeting = dict(MEETING)
    meeting.update(meeting_overrides)
    return {
        "event_id": "evt_test",
        "event_type": event_type,
        "data": {"meeting": meeting},
    }


def _envelope_missing(*fields: str) -> dict[str, Any]:
    envelope = _envelope()
    meeting = envelope["data"]["meeting"]
    for field in fields:
        meeting.pop(field, None)
    return envelope


FIXED_TS = "1000"


def _fixed_clock() -> float:
    return 1100.0


def _sign(
    body: bytes, ts: str = FIXED_TS, secret: str = WEBHOOK_SECRET
) -> dict[str, str]:
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256)
    return {
        "X-Webhook-Signature": f"sha256={mac.hexdigest()}",
        "X-Webhook-Timestamp": ts,
    }


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


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = dict(
        meeting_api_url="http://meeting-api",
        webhook_secret=WEBHOOK_SECRET,
        vexa_bucket=VEXA_BUCKET,
        export_bucket=EXPORT_BUCKET,
        export_prefix="recordings/",
        notetaker_url="http://notetaker",
        max_attempts=5,
        concurrency=4,
        sweep_seconds=60.0,
    )
    base.update(overrides)
    return Settings(**base)


def _fake_transcode(src: Path, dst: Path) -> None:
    dst.write_bytes(b"RIFF")


def _deps(storage: Storage, settings: Settings) -> Deps:
    http = httpx.Client()
    return Deps(
        settings=settings,
        storage=storage,
        meeting_api=MeetingApi(settings.meeting_api_url, http),
        notetaker=Notetaker(settings.notetaker_url, http),
        transcode=_fake_transcode,
        now=lambda: datetime(2026, 6, 18, 11, 0, 0, tzinfo=timezone.utc),
    )


# ---------------------------------------------------------------------------
# HTTP intake
# ---------------------------------------------------------------------------


def test_unsigned_request_rejected(storage: Storage) -> None:
    settings = _settings()
    queue = PendingQueue(storage, settings.vexa_bucket)
    app = create_app(settings, queue, _deps(storage, settings), start_worker=False)
    with TestClient(app) as client:
        resp = client.post("/hooks/vexa", json=_envelope())
    assert resp.status_code == 401


def test_wrong_event_type_ignored_and_not_queued(storage: Storage) -> None:
    settings = _settings()
    queue = PendingQueue(storage, settings.vexa_bucket)
    app = create_app(
        settings,
        queue,
        _deps(storage, settings),
        clock=_fixed_clock,
        start_worker=False,
    )
    body = b'{"event_type":"bot.failed","data":{}}'
    headers = _sign(body)
    with TestClient(app) as client:
        resp = client.post("/hooks/vexa", content=body, headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored"}
    assert queue.pending_ids() == []


def test_meeting_completed_enqueues_and_returns_202(storage: Storage) -> None:
    settings = _settings()
    queue = PendingQueue(storage, settings.vexa_bucket)
    app = create_app(
        settings,
        queue,
        _deps(storage, settings),
        clock=_fixed_clock,
        start_worker=False,
    )
    envelope = _envelope()
    body = json.dumps(envelope).encode()
    headers = _sign(body)
    with TestClient(app) as client:
        resp = client.post("/hooks/vexa", content=body, headers=headers)
    assert resp.status_code == 202
    assert resp.json() == {"status": "queued"}
    stored = storage.get_json(VEXA_BUCKET, "aw-exporter/pending/11367.json")
    assert stored is not None
    assert stored["envelope"] == envelope
    assert stored["attempts"] == 0


def test_enqueue_failure_returns_503(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings()
    queue = PendingQueue(storage, settings.vexa_bucket)

    def boom(envelope: dict[str, Any]) -> None:
        raise RuntimeError("s3 put failed")

    monkeypatch.setattr(queue, "enqueue", boom)
    app = create_app(
        settings,
        queue,
        _deps(storage, settings),
        clock=_fixed_clock,
        start_worker=False,
    )
    body = json.dumps(_envelope()).encode()
    headers = _sign(body)
    with TestClient(app) as client:
        resp = client.post("/hooks/vexa", content=body, headers=headers)
    assert resp.status_code == 503


def test_invalid_json_after_valid_signature_returns_400(storage: Storage) -> None:
    settings = _settings()
    queue = PendingQueue(storage, settings.vexa_bucket)
    app = create_app(
        settings,
        queue,
        _deps(storage, settings),
        clock=_fixed_clock,
        start_worker=False,
    )
    body = b"not json"
    headers = _sign(body)
    with TestClient(app) as client:
        resp = client.post("/hooks/vexa", content=body, headers=headers)
    assert resp.status_code == 400


def test_missing_meeting_id_returns_400(storage: Storage) -> None:
    settings = _settings()
    queue = PendingQueue(storage, settings.vexa_bucket)
    app = create_app(
        settings,
        queue,
        _deps(storage, settings),
        clock=_fixed_clock,
        start_worker=False,
    )
    body = b'{"event_type":"meeting.completed","data":{"meeting":{}}}'
    headers = _sign(body)
    with TestClient(app) as client:
        resp = client.post("/hooks/vexa", content=body, headers=headers)
    assert resp.status_code == 400


def test_missing_start_time_returns_400_and_not_queued(storage: Storage) -> None:
    settings = _settings()
    queue = PendingQueue(storage, settings.vexa_bucket)
    app = create_app(
        settings,
        queue,
        _deps(storage, settings),
        clock=_fixed_clock,
        start_worker=False,
    )
    envelope = _envelope_missing("start_time")
    body = json.dumps(envelope).encode()
    headers = _sign(body)
    with TestClient(app) as client:
        resp = client.post("/hooks/vexa", content=body, headers=headers)
    assert resp.status_code == 400
    assert queue.pending_ids() == []


@pytest.mark.parametrize(
    "body",
    [
        b"[1, 2]",
        b'"a string"',
        b"null",
        b'{"event_type":"meeting.completed","data":[1]}',
        b'{"event_type":"meeting.completed","data":"x"}',
        b'{"event_type":"meeting.completed","data":{"meeting":[1]}}',
        b'{"event_type":"meeting.completed","data":{"meeting":"x"}}',
        b"\xff\xfe not utf-8",
    ],
)
def test_signed_non_object_shapes_return_400_not_500(
    storage: Storage, body: bytes
) -> None:
    settings = _settings()
    queue = PendingQueue(storage, settings.vexa_bucket)
    app = create_app(
        settings,
        queue,
        _deps(storage, settings),
        clock=_fixed_clock,
        start_worker=False,
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.post("/hooks/vexa", content=body, headers=_sign(body))
    assert resp.status_code == 400
    assert queue.pending_ids() == []


def test_healthz_returns_ok(storage: Storage) -> None:
    settings = _settings()
    queue = PendingQueue(storage, settings.vexa_bucket)
    app = create_app(settings, queue, _deps(storage, settings), start_worker=False)
    with TestClient(app) as client:
        resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_lifespan_starts_and_stops_worker_when_start_worker_true(
    storage: Storage,
) -> None:
    settings = _settings()
    queue = PendingQueue(storage, settings.vexa_bucket)
    app = create_app(settings, queue, _deps(storage, settings), start_worker=True)
    with TestClient(app) as client:
        resp = client.get("/healthz")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# PendingQueue
# ---------------------------------------------------------------------------


def test_enqueue_load_pending_ids_roundtrip(storage: Storage) -> None:
    queue = PendingQueue(storage, VEXA_BUCKET)
    envelope = _envelope()
    queue.enqueue(envelope)
    assert queue.pending_ids() == ["11367"]
    item = queue.load("11367")
    assert item is not None
    assert item["envelope"] == envelope
    assert item["attempts"] == 0
    assert item["last_error"] is None


def test_enqueue_writes_untagged_pending_object(storage: Storage) -> None:
    """Vexa-bucket writes (spec §3/§7) are NOT tagged — that bucket has its own
    prefix lifecycle."""
    queue = PendingQueue(storage, VEXA_BUCKET)
    queue.enqueue(_envelope())
    tags = storage._client.get_object_tagging(
        Bucket=VEXA_BUCKET, Key="aw-exporter/pending/11367.json"
    )["TagSet"]
    assert tags == []


def test_done_deletes_pending(storage: Storage) -> None:
    queue = PendingQueue(storage, VEXA_BUCKET)
    queue.enqueue(_envelope())
    queue.done("11367")
    assert queue.pending_ids() == []
    assert queue.load("11367") is None


def test_record_failure_increments_attempts_and_sets_backoff(storage: Storage) -> None:
    queue = PendingQueue(storage, VEXA_BUCKET)
    queue.enqueue(_envelope())
    attempts = queue.record_failure("11367", "boom", now=lambda: 1000.0)
    assert attempts == 1
    item = queue.load("11367")
    assert item is not None
    assert item["last_error"] == "boom"
    assert item["next_attempt_at"] == 1000.0 + 30 * 2**1


def test_fail_moves_pending_to_failed(storage: Storage) -> None:
    queue = PendingQueue(storage, VEXA_BUCKET)
    queue.enqueue(_envelope())
    queue.record_failure("11367", "boom", now=lambda: 1000.0)
    queue.fail("11367")
    assert queue.pending_ids() == []
    assert queue.load("11367") is None
    failed = storage.get_json(VEXA_BUCKET, "aw-exporter/failed/11367.json")
    assert failed is not None
    assert failed["last_error"] == "boom"


def test_restart_semantics_fresh_queue_sees_still_pending_id(storage: Storage) -> None:
    queue = PendingQueue(storage, VEXA_BUCKET)
    queue.enqueue(_envelope())
    fresh_queue = PendingQueue(storage, VEXA_BUCKET)
    assert fresh_queue.pending_ids() == ["11367"]


def test_reenqueue_of_pending_id_refreshes_envelope_but_keeps_attempts(
    storage: Storage,
) -> None:
    queue = PendingQueue(storage, VEXA_BUCKET)
    queue.enqueue(_envelope())
    queue.record_failure("11367", "boom", now=lambda: 1000.0)

    updated_envelope = _envelope(end_time="2026-06-18T10:50:00.000Z")
    queue.enqueue(updated_envelope)

    assert queue.pending_ids() == ["11367"]
    item = queue.load("11367")
    assert item is not None
    assert item["envelope"] == updated_envelope
    assert item["attempts"] == 1
    assert item["last_error"] == "boom"
    assert item["next_attempt_at"] == 1000.0 + 30 * 2**1


# ---------------------------------------------------------------------------
# sweep_once / run_worker
# ---------------------------------------------------------------------------


def test_sweep_once_success_removes_pending(storage: Storage) -> None:
    settings = _settings()
    queue = PendingQueue(storage, settings.vexa_bucket)
    queue.enqueue(_envelope())
    deps = _deps(storage, settings)

    def fake_job(envelope: dict[str, Any], deps: Deps) -> ExportResult:
        return ExportResult("handed_off", "folder")

    asyncio.run(sweep_once(queue, deps, job=fake_job))

    assert queue.pending_ids() == []


def test_sweep_twice_reaches_max_attempts_and_moves_to_failed(storage: Storage) -> None:
    settings = _settings(max_attempts=2)
    queue = PendingQueue(storage, settings.vexa_bucket)
    queue.enqueue(_envelope())
    deps = _deps(storage, settings)

    def failing_job(envelope: dict[str, Any], deps: Deps) -> ExportResult:
        raise RuntimeError("boom")

    asyncio.run(sweep_once(queue, deps, job=failing_job, now=lambda: 1000.0))
    # First failure: attempts=1 < max_attempts=2, still pending, backed off.
    assert queue.pending_ids() == ["11367"]
    item = queue.load("11367")
    assert item is not None
    assert item["attempts"] == 1

    # A sweep before the backoff window elapses must not reprocess.
    asyncio.run(sweep_once(queue, deps, job=failing_job, now=lambda: 1000.0))
    item = queue.load("11367")
    assert item is not None
    assert item["attempts"] == 1

    # Second failure past the backoff window: reaches max_attempts.
    asyncio.run(sweep_once(queue, deps, job=failing_job, now=lambda: 5000.0))

    assert queue.pending_ids() == []
    assert storage.get_json(VEXA_BUCKET, "aw-exporter/pending/11367.json") is None
    failed = storage.get_json(VEXA_BUCKET, "aw-exporter/failed/11367.json")
    assert failed is not None
    assert failed["attempts"] == 2

    marker = storage.get_json(EXPORT_BUCKET, BASE + "_export.json")
    assert marker is not None
    assert marker["state"] == "failed"
    assert marker["error"] == "boom"
    assert marker["attempts"] == 2
    assert marker["vexa_meeting_id"] == 11367

    # retention-class tagging (spec §3/§7): the quarantine marker is exporter-bucket
    # metadata; the failed/ item stays in the Vexa bucket, untagged.
    marker_tags = storage._client.get_object_tagging(
        Bucket=EXPORT_BUCKET, Key=BASE + "_export.json"
    )["TagSet"]
    assert {"Key": "retention-class", "Value": "metadata"} in marker_tags
    failed_tags = storage._client.get_object_tagging(
        Bucket=VEXA_BUCKET, Key="aw-exporter/failed/11367.json"
    )["TagSet"]
    assert failed_tags == []


def test_run_worker_exits_when_stop_is_set(storage: Storage) -> None:
    settings = _settings()
    queue = PendingQueue(storage, settings.vexa_bucket)
    deps = _deps(storage, settings)

    async def scenario() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(run_worker(queue, deps, stop))
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, timeout=2)

    asyncio.run(scenario())


def test_sweep_once_quarantines_malformed_envelope_without_raising(
    storage: Storage,
) -> None:
    """A pending item seeded directly (bypassing intake validation) missing
    start_time must not crash sweep_once: export_meeting's own folder_name
    call raises, the quarantine's own folder_name call also raises, so the
    marker is skipped but the item still lands under failed/."""
    settings = _settings(max_attempts=1)
    queue = PendingQueue(storage, settings.vexa_bucket)
    queue.enqueue(_envelope_missing("start_time"))
    deps = _deps(storage, settings)

    asyncio.run(sweep_once(queue, deps))  # default job=export_meeting

    assert queue.pending_ids() == []
    assert storage.get_json(VEXA_BUCKET, "aw-exporter/pending/11367.json") is None
    failed = storage.get_json(VEXA_BUCKET, "aw-exporter/failed/11367.json")
    assert failed is not None
    assert failed["attempts"] == 1
    assert storage.list_keys(EXPORT_BUCKET, "") == []


def test_sweep_once_isolates_per_id_failures(storage: Storage) -> None:
    settings = _settings(max_attempts=5)
    queue = PendingQueue(storage, settings.vexa_bucket)
    queue.enqueue(_envelope(id=1, native_meeting_id="good-meeting"))
    queue.enqueue(_envelope(id=2, native_meeting_id="bad-meeting"))
    deps = _deps(storage, settings)

    def selective_job(envelope: dict[str, Any], deps: Deps) -> ExportResult:
        if envelope["data"]["meeting"]["id"] == 2:
            raise RuntimeError("boom")
        return ExportResult("handed_off", "folder")

    asyncio.run(sweep_once(queue, deps, job=selective_job))

    assert queue.load("1") is None
    bad_item = queue.load("2")
    assert bad_item is not None
    assert bad_item["attempts"] == 1


def test_run_worker_survives_sweep_once_raising(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(sweep_seconds=0.01)
    queue = PendingQueue(storage, settings.vexa_bucket)
    deps = _deps(storage, settings)
    calls = {"n": 0}

    async def flaky_sweep_once(q: PendingQueue, d: Deps) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")

    monkeypatch.setattr(queue_module, "sweep_once", flaky_sweep_once)

    async def scenario() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(run_worker(queue, deps, stop))
        await asyncio.sleep(0.2)
        stop.set()
        await asyncio.wait_for(task, timeout=2)

    asyncio.run(scenario())

    assert calls["n"] >= 2


def test_caplog_shows_job_failure_and_quarantine(
    storage: Storage, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="exporter")
    settings = _settings(max_attempts=1)
    queue = PendingQueue(storage, settings.vexa_bucket)
    queue.enqueue(_envelope())
    deps = _deps(storage, settings)

    def failing_job(envelope: dict[str, Any], deps: Deps) -> ExportResult:
        raise RuntimeError("boom")

    asyncio.run(sweep_once(queue, deps, job=failing_job))

    messages = " | ".join(record.getMessage() for record in caplog.records)
    assert "export job failed" in messages
    assert "meeting_id=11367" in messages
    assert "quarantine" in messages


def test_import_main_module_is_safe() -> None:
    import exporter.__main__  # noqa: F401
