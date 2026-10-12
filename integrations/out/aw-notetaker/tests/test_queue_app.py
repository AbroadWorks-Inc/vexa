"""Tests for exporter.queue (durable pending queue + sweep worker) and
exporter.app (the signed aw-bots subscription intake) — spec §4.1, design
§1.9, §2.7."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import json
import logging
import threading
import time
import warnings
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import boto3
import httpx
import pytest
from botocore.exceptions import ClientError
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

from exporter.app import create_app  # noqa: E402
from exporter.audio import join_webm  # noqa: E402
from exporter.config import Settings  # noqa: E402
from exporter.export_result import ExportReportError, ExportReporter  # noqa: E402
from exporter.job import Deps, ExportResult  # noqa: E402
from exporter.naming import folder_name  # noqa: E402
from exporter.notetaker import Notetaker  # noqa: E402
from exporter.queue import (  # noqa: E402
    PendingQueue,
    WorkerState,
    run_worker,
    sweep_once,
)
from exporter.storage import Storage  # noqa: E402
from exporter.vexa_client import MeetingApi  # noqa: E402
from tests.builders import (  # noqa: E402
    EVENT_ID,
    MEETING_UUID,
    meeting_event,
    meeting_v2,
)

WEBHOOK_SECRET = "test-secret"
VEXA_BUCKET = "aw-bots"
EXPORT_BUCKET = "aw-chatworks-transcribe"
MEETING = meeting_v2()
FOLDER = folder_name(
    str(MEETING["platform"]),
    str(MEETING["room"]),
    str(MEETING["started_at"]),
)
BASE = f"recordings/{FOLDER}/"
# The queue and the event markers are keyed by the meeting's UUID and the event's id.
KEY = MEETING_UUID
PENDING_KEY = f"aw-exporter/pending/{KEY}.json"
FAILED_KEY = f"aw-exporter/failed/{KEY}.json"
EVENT_KEY = f"aw-exporter/events/{EVENT_ID}.json"


def _envelope(
    event_type: str = "meeting.completed", **meeting_overrides: Any
) -> dict[str, Any]:
    return meeting_event(event_type, **meeting_overrides)


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
        gateway_url="http://gateway",
        exporter_api_key="test-exporter-key",
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
        meeting_api=MeetingApi(settings.gateway_url, settings.exporter_api_key, http),
        notetaker=Notetaker(settings.notetaker_url, http),
        export_result=ExportReporter(
            settings.gateway_url, settings.exporter_api_key, http
        ),
        transcode=_fake_transcode,
        now=lambda: datetime(2026, 6, 18, 11, 0, 0, tzinfo=timezone.utc),
        join_webm=join_webm,
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
    body = b'{"event_type":"meeting.started","data":{}}'
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
    stored = storage.get_json(VEXA_BUCKET, PENDING_KEY)
    assert stored is not None
    assert stored["envelope"] == envelope
    assert stored["attempts"] == 0


def _post(storage: Storage, envelope: Any) -> tuple[Any, PendingQueue]:
    settings = _settings()
    queue = PendingQueue(storage, settings.vexa_bucket)
    app = create_app(
        settings,
        queue,
        _deps(storage, settings),
        clock=_fixed_clock,
        start_worker=False,
    )
    body = json.dumps(envelope).encode()
    with TestClient(app) as client:
        resp = client.post("/hooks/vexa", content=body, headers=_sign(body))
    return resp, queue


def test_bot_failed_is_queued_like_a_completed_meeting(storage: Storage) -> None:
    envelope = _envelope("bot.failed", status="failed")

    resp, queue = _post(storage, envelope)

    assert resp.status_code == 202
    assert resp.json() == {"status": "queued"}
    stored = storage.get_json(VEXA_BUCKET, PENDING_KEY)
    assert stored is not None and stored["envelope"] == envelope


def test_bot_failed_without_a_start_time_is_skipped_not_rejected(
    storage: Storage, caplog: pytest.LogCaptureFixture
) -> None:
    """The bot never got into the meeting (webhook.v1 golden: started_at
    null), so nothing was recorded; it is answered 2xx, never a failed
    delivery."""
    with caplog.at_level(logging.INFO, logger="exporter"):
        resp, queue = _post(
            storage, _envelope("bot.failed", status="failed", started_at=None)
        )

    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored"}
    assert queue.pending_ids() == []
    assert any(
        r.getMessage()
        == f"bot_failed_skipped meeting_id={MEETING_UUID} reason=no_start_time; "
        "nothing recorded"
        for r in caplog.records
    )


@pytest.mark.parametrize("event_type", ["meeting.completed", "bot.failed"])
def test_a_not_sent_meeting_is_never_queued(storage: Storage, event_type: str) -> None:
    outcome = {"kind": "not_sent", "detail": "account_limit", "message": "limit"}

    resp, queue = _post(
        storage, _envelope(event_type, status="failed", outcome=outcome)
    )

    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored"}
    assert queue.pending_ids() == []


def test_meeting_not_sent_event_is_ignored(storage: Storage) -> None:
    resp, queue = _post(storage, _envelope("meeting.not_sent", status="failed"))

    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored"}
    assert queue.pending_ids() == []


def test_bot_failed_with_an_incomplete_meeting_returns_400(storage: Storage) -> None:
    envelope = _envelope("bot.failed", status="failed")
    del envelope["data"]["meeting"]["platform"]

    resp, queue = _post(storage, envelope)

    assert resp.status_code == 400
    assert queue.pending_ids() == []


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


@pytest.mark.parametrize("started_at", ["missing", None, ""])
def test_a_completed_meeting_without_a_start_time_is_skipped_not_rejected(
    storage: Storage, caplog: pytest.LogCaptureFixture, started_at: Any
) -> None:
    """The bot never got into the meeting (for example stopped in the
    lobby), so there is nothing to export. It is answered 200: under a
    subscription a 400 is a permanent `failed` delivery, and this is no
    delivery fault. It is logged at warning, once per meeting, so it can be
    counted."""
    envelope = (
        _envelope_missing("started_at")
        if started_at == "missing"
        else _envelope(started_at=started_at)
    )
    with caplog.at_level(logging.INFO, logger="exporter"):
        resp, queue = _post(storage, envelope)

    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored"}
    assert queue.pending_ids() == []
    assert storage.list_keys(VEXA_BUCKET, "aw-exporter/") == []
    skipped = [
        r
        for r in caplog.records
        if r.getMessage()
        == f"completed_skipped meeting_id={MEETING_UUID} reason=no_start_time; "
        "the bot was never in the meeting, nothing exported"
    ]
    assert len(skipped) == 1 and skipped[0].levelno == logging.WARNING


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
    assert queue.pending_ids() == [KEY]
    item = queue.load(KEY)
    assert item is not None
    assert item["envelope"] == envelope
    assert item["attempts"] == 0
    assert item["last_error"] is None


def test_enqueue_writes_untagged_pending_object(storage: Storage) -> None:
    """Vexa-bucket writes (spec §3/§7) are NOT tagged — that bucket has its own
    prefix lifecycle."""
    queue = PendingQueue(storage, VEXA_BUCKET)
    queue.enqueue(_envelope())
    tags = storage._client.get_object_tagging(Bucket=VEXA_BUCKET, Key=PENDING_KEY)[
        "TagSet"
    ]
    assert tags == []


def test_done_deletes_pending(storage: Storage) -> None:
    queue = PendingQueue(storage, VEXA_BUCKET)
    queue.enqueue(_envelope())
    queue.done(KEY)
    assert queue.pending_ids() == []
    assert queue.load(KEY) is None


def test_record_failure_increments_attempts_and_sets_backoff(storage: Storage) -> None:
    queue = PendingQueue(storage, VEXA_BUCKET)
    queue.enqueue(_envelope())
    attempts = queue.record_failure(
        KEY, "boom", backoff_seconds=30.0, now=lambda: 1000.0
    )
    assert attempts == 1
    item = queue.load(KEY)
    assert item is not None
    assert item["last_error"] == "boom"
    assert item["next_attempt_at"] == 1000.0 + 30 * 2**1


def test_record_failure_backs_off_from_the_base_it_is_given(storage: Storage) -> None:
    queue = PendingQueue(storage, VEXA_BUCKET)
    queue.enqueue(_envelope())
    queue.record_failure(KEY, "boom", backoff_seconds=5.0, now=lambda: 1000.0)
    queue.record_failure(KEY, "boom", backoff_seconds=5.0, now=lambda: 1000.0)
    item = queue.load(KEY)
    assert item is not None
    assert item["next_attempt_at"] == 1000.0 + 5 * 2**2


def test_the_sweep_backs_off_by_the_setting(storage: Storage) -> None:
    settings = _settings(retry_backoff_seconds=7.0)
    queue = PendingQueue(storage, settings.vexa_bucket)
    queue.enqueue(_envelope())

    def failing_job(
        envelope: dict[str, Any], deps: Deps, rerun: bool = False, progress: Any = None
    ) -> ExportResult:
        raise RuntimeError("boom")

    asyncio.run(
        sweep_once(queue, _deps(storage, settings), job=failing_job, now=lambda: 1000.0)
    )
    item = queue.load(KEY)
    assert item is not None
    assert item["next_attempt_at"] == 1000.0 + 7 * 2**1


def test_fail_moves_pending_to_failed(storage: Storage) -> None:
    queue = PendingQueue(storage, VEXA_BUCKET)
    queue.enqueue(_envelope())
    queue.record_failure(KEY, "boom", backoff_seconds=30.0, now=lambda: 1000.0)
    queue.fail(KEY)
    assert queue.pending_ids() == []
    assert queue.load(KEY) is None
    failed = storage.get_json(VEXA_BUCKET, FAILED_KEY)
    assert failed is not None
    assert failed["last_error"] == "boom"


def test_restart_semantics_fresh_queue_sees_still_pending_id(storage: Storage) -> None:
    queue = PendingQueue(storage, VEXA_BUCKET)
    queue.enqueue(_envelope())
    fresh_queue = PendingQueue(storage, VEXA_BUCKET)
    assert fresh_queue.pending_ids() == [KEY]


def test_reenqueue_of_pending_id_refreshes_envelope_but_keeps_attempts(
    storage: Storage,
) -> None:
    queue = PendingQueue(storage, VEXA_BUCKET)
    queue.enqueue(_envelope())
    queue.record_failure(KEY, "boom", backoff_seconds=30.0, now=lambda: 1000.0)

    updated_envelope = _envelope(ended_at="2026-06-18T10:50:00Z")
    queue.enqueue(updated_envelope)

    assert queue.pending_ids() == [KEY]
    item = queue.load(KEY)
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

    def fake_job(
        envelope: dict[str, Any], deps: Deps, rerun: bool = False, progress: Any = None
    ) -> ExportResult:
        return ExportResult("handed_off", "folder")

    asyncio.run(sweep_once(queue, deps, job=fake_job))

    assert queue.pending_ids() == []


def test_a_rerun_reaches_the_job_and_a_redelivered_webhook_keeps_it(
    storage: Storage,
) -> None:
    settings = _settings()
    queue = PendingQueue(storage, settings.vexa_bucket)
    assert queue.request_rerun(_envelope()) == "queued"
    queue.enqueue(_envelope())
    seen: list[bool] = []

    def fake_job(
        envelope: dict[str, Any], deps: Deps, rerun: bool = False, progress: Any = None
    ) -> ExportResult:
        seen.append(rerun)
        return ExportResult("handed_off", "folder")

    asyncio.run(sweep_once(queue, _deps(storage, settings), job=fake_job))

    assert seen == [True]
    assert queue.pending_ids() == []


def test_sweep_twice_reaches_max_attempts_and_moves_to_failed(storage: Storage) -> None:
    settings = _settings(max_attempts=2)
    queue = PendingQueue(storage, settings.vexa_bucket)
    queue.enqueue(_envelope())
    deps = _deps(storage, settings)

    def failing_job(
        envelope: dict[str, Any], deps: Deps, rerun: bool = False, progress: Any = None
    ) -> ExportResult:
        raise RuntimeError("boom")

    asyncio.run(sweep_once(queue, deps, job=failing_job, now=lambda: 1000.0))
    # First failure: attempts=1 < max_attempts=2, still pending, backed off.
    assert queue.pending_ids() == [KEY]
    item = queue.load(KEY)
    assert item is not None
    assert item["attempts"] == 1

    # A sweep before the backoff window elapses must not reprocess.
    asyncio.run(sweep_once(queue, deps, job=failing_job, now=lambda: 1000.0))
    item = queue.load(KEY)
    assert item is not None
    assert item["attempts"] == 1

    # Second failure past the backoff window: reaches max_attempts.
    asyncio.run(sweep_once(queue, deps, job=failing_job, now=lambda: 5000.0))

    assert queue.pending_ids() == []
    assert storage.get_json(VEXA_BUCKET, PENDING_KEY) is None
    failed = storage.get_json(VEXA_BUCKET, FAILED_KEY)
    assert failed is not None
    assert failed["attempts"] == 2

    marker = storage.get_json(EXPORT_BUCKET, BASE + "_export.json")
    assert marker is not None
    assert marker["state"] == "failed"
    assert marker["error"] == "boom"
    assert marker["attempts"] == 2
    assert marker["meeting_id"] == MEETING_UUID
    assert marker["vexa_meeting_id"] == 11367

    # retention-class tagging (spec §3/§7): the quarantine marker is exporter-bucket
    # metadata; the failed/ item stays in the Vexa bucket, untagged.
    marker_tags = storage._client.get_object_tagging(
        Bucket=EXPORT_BUCKET, Key=BASE + "_export.json"
    )["TagSet"]
    assert {"Key": "retention-class", "Value": "metadata"} in marker_tags
    failed_tags = storage._client.get_object_tagging(
        Bucket=VEXA_BUCKET, Key=FAILED_KEY
    )["TagSet"]
    assert failed_tags == []


# The old system hook's meeting block: an integer id and no upstream_id.
LEGACY_MEETING = {
    "id": 11367,
    "uuid": MEETING_UUID,
    "user_id": 7,
    "platform": "google_meet",
    "native_meeting_id": "abc-defg-hij",
    "start_time": "2026-06-18T10:00:00.000Z",
}


def test_a_queued_item_that_is_not_a_v2_meeting_goes_straight_to_failed(
    storage: Storage, caplog: pytest.LogCaptureFixture
) -> None:
    """Design §1.9: an item whose meeting is not the §2.4 meeting (a UUID
    `id` and an integer `upstream_id`), such as one the old exporter queued
    from the system hook, fails loudly. Retrying can't change it, so it is
    quarantined on its first attempt, and nothing is written to the export
    bucket (no `_export.json`, no report)."""
    settings = _settings(max_attempts=5)
    queue = PendingQueue(storage, settings.vexa_bucket)
    queue.enqueue(
        {"event_type": "meeting.completed", "data": {"meeting": LEGACY_MEETING}}
    )
    deps = _deps(storage, settings)

    with caplog.at_level(logging.ERROR, logger="exporter"):
        asyncio.run(sweep_once(queue, deps))  # default job=export_meeting

    assert queue.pending_ids() == []
    failed = storage.get_json(VEXA_BUCKET, "aw-exporter/failed/11367.json")
    assert failed is not None
    assert failed["attempts"] == 1
    assert "upstream_id" in failed["last_error"]
    assert storage.list_keys(EXPORT_BUCKET, "") == []
    assert any(
        "not a v2 meeting" in r.getMessage() and "meeting_id=11367" in r.getMessage()
        for r in caplog.records
    )


def test_a_meeting_without_upstream_id_goes_straight_to_failed(
    storage: Storage,
) -> None:
    settings = _settings(max_attempts=5)
    queue = PendingQueue(storage, settings.vexa_bucket)
    queue.enqueue(_envelope_missing("upstream_id"))

    asyncio.run(sweep_once(queue, _deps(storage, settings)))

    failed = storage.get_json(VEXA_BUCKET, FAILED_KEY)
    assert failed is not None and failed["attempts"] == 1
    assert storage.list_keys(EXPORT_BUCKET, "") == []


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
    started_at must not crash sweep_once: export_meeting's own folder_name
    call raises, the quarantine's own folder_name call also raises, so the
    marker is skipped but the item still lands under failed/."""
    settings = _settings(max_attempts=1)
    queue = PendingQueue(storage, settings.vexa_bucket)
    queue.enqueue(_envelope_missing("started_at"))
    deps = _deps(storage, settings)

    asyncio.run(sweep_once(queue, deps))  # default job=export_meeting

    assert queue.pending_ids() == []
    assert storage.get_json(VEXA_BUCKET, PENDING_KEY) is None
    failed = storage.get_json(VEXA_BUCKET, FAILED_KEY)
    assert failed is not None
    assert failed["attempts"] == 1
    assert storage.list_keys(EXPORT_BUCKET, "") == []


def test_sweep_once_isolates_per_id_failures(storage: Storage) -> None:
    settings = _settings(max_attempts=5)
    queue = PendingQueue(storage, settings.vexa_bucket)
    queue.enqueue(_envelope(id="1", room="good-meeting"))
    queue.enqueue(_envelope(id="2", room="bad-meeting"))
    deps = _deps(storage, settings)

    def selective_job(
        envelope: dict[str, Any], deps: Deps, rerun: bool = False, progress: Any = None
    ) -> ExportResult:
        if envelope["data"]["meeting"]["id"] == "2":
            raise RuntimeError("boom")
        return ExportResult("handed_off", "folder")

    asyncio.run(sweep_once(queue, deps, job=selective_job))

    assert queue.load("1") is None
    bad_item = queue.load("2")
    assert bad_item is not None
    assert bad_item["attempts"] == 1


def test_run_worker_survives_listing_the_queue_raising(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(sweep_seconds=0.01)
    queue = PendingQueue(storage, settings.vexa_bucket)
    deps = _deps(storage, settings)
    calls = {"n": 0}
    real = queue.pending_ids

    def flaky_pending_ids() -> list[str]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return real()

    monkeypatch.setattr(queue, "pending_ids", flaky_pending_ids)

    async def scenario() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(run_worker(queue, deps, stop))
        await asyncio.sleep(0.2)
        stop.set()
        await asyncio.wait_for(task, timeout=2)

    asyncio.run(scenario())

    assert calls["n"] >= 2


# ---------------------------------------------------------------------------
# Crashes (F1): a run that stops without finishing leaves its lease behind
# ---------------------------------------------------------------------------


def _left_by_a_dead_run(queue: PendingQueue, expires_at: float = 900.0) -> None:
    item = queue.load(KEY)
    assert item is not None
    item["lease"] = {"owner": "dead-pod", "expires_at": expires_at}
    queue._storage.put_json(VEXA_BUCKET, PENDING_KEY, item)


def _ok_job(calls: list[str]) -> Any:
    def job(
        envelope: dict[str, Any], deps: Deps, rerun: bool = False, progress: Any = None
    ) -> ExportResult:
        calls.append("ran")
        return ExportResult(state="handed_off", folder="x")

    return job


def test_an_expired_lease_counts_a_crash_and_runs_the_meeting_again(
    storage: Storage,
) -> None:
    settings = _settings()
    queue = PendingQueue(storage, settings.vexa_bucket)
    queue.enqueue(_envelope())
    _left_by_a_dead_run(queue)
    calls: list[str] = []
    failing: list[str] = []

    def crash_then_fail(
        envelope: dict[str, Any], deps: Deps, rerun: bool = False, progress: Any = None
    ) -> ExportResult:
        failing.append("ran")
        raise RuntimeError("boom")

    asyncio.run(
        sweep_once(
            queue, _deps(storage, settings), job=crash_then_fail, now=lambda: 1000.0
        )
    )

    item = queue.load(KEY)
    assert item is not None
    assert item["crashes"] == 1, "the dead run is counted"
    assert failing == ["ran"], "and the meeting runs again"
    assert "lease" not in item, "a failed run clears its lease"
    assert item["attempts"] == 1
    assert calls == []


def test_the_second_crash_quarantines_the_meeting_and_it_never_runs_again(
    storage: Storage,
) -> None:
    settings = _settings(max_crashes=2)
    queue = PendingQueue(storage, settings.vexa_bucket)
    queue.enqueue(_envelope())
    deps = _deps(storage, settings)
    calls: list[str] = []

    _left_by_a_dead_run(queue)
    item = queue.load(KEY)
    assert item is not None
    item["crashes"] = 1
    storage.put_json(VEXA_BUCKET, PENDING_KEY, item)

    asyncio.run(sweep_once(queue, deps, job=_ok_job(calls), now=lambda: 1000.0))

    assert calls == [], "a meeting out of crashes is not run"
    assert storage.get_json(VEXA_BUCKET, PENDING_KEY) is None
    failed = storage.get_json(VEXA_BUCKET, FAILED_KEY)
    assert failed is not None and failed["crashes"] == 2 and "lease" not in failed
    marker = storage.get_json(EXPORT_BUCKET, BASE + "_export.json")
    assert marker is not None and marker["state"] == "failed"
    assert "stopped 2 times" in marker["error"]

    asyncio.run(sweep_once(queue, deps, job=_ok_job(calls), now=lambda: 9000.0))
    assert calls == []


def test_a_lease_that_has_not_expired_is_left_running(storage: Storage) -> None:
    settings = _settings()
    queue = PendingQueue(storage, settings.vexa_bucket)
    queue.enqueue(_envelope())
    _left_by_a_dead_run(queue, expires_at=2000.0)
    calls: list[str] = []

    asyncio.run(
        sweep_once(
            queue, _deps(storage, settings), job=_ok_job(calls), now=lambda: 1000.0
        )
    )

    assert calls == []
    item = queue.load(KEY)
    assert item is not None and "crashes" not in item


def test_a_finished_meeting_leaves_no_lease_behind(storage: Storage) -> None:
    settings = _settings()
    queue = PendingQueue(storage, settings.vexa_bucket)
    queue.enqueue(_envelope())
    seen: list[Any] = []

    def job(
        envelope: dict[str, Any], deps: Deps, rerun: bool = False, progress: Any = None
    ) -> ExportResult:
        seen.append((queue.load(KEY) or {}).get("lease"))
        return ExportResult(state="handed_off", folder="x")

    asyncio.run(
        sweep_once(queue, _deps(storage, settings), job=job, now=lambda: 1000.0)
    )

    assert (
        seen[0]["expires_at"] == 1000.0 + settings.lease_seconds
    ), "leased before it runs"
    assert queue.pending_ids() == []


def test_a_stopped_worker_releases_its_lease_so_a_deploy_is_not_a_crash(
    storage: Storage,
) -> None:
    import threading

    settings = _settings(sweep_seconds=0.01, stop_wait_seconds=0.05)
    queue = PendingQueue(storage, settings.vexa_bucket)
    queue.enqueue(_envelope())
    started, finish = threading.Event(), threading.Event()

    def slow_job(
        envelope: dict[str, Any], deps: Deps, rerun: bool = False, progress: Any = None
    ) -> ExportResult:
        started.set()
        finish.wait(5)
        raise RuntimeError("killed with the pod")

    async def scenario() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(
            run_worker(queue, _deps(storage, settings), stop, job=slow_job)
        )
        while not started.is_set():
            await asyncio.sleep(0.01)
        stop.set()
        await asyncio.wait_for(task, timeout=2)

    asyncio.run(scenario())
    item = queue.load(KEY)
    finish.set()
    assert item is not None
    assert "lease" not in item and "crashes" not in item


def test_a_rerun_asked_for_while_the_meeting_exports_is_refused_and_changes_nothing(
    storage: Storage,
) -> None:
    """P9: the rerun would rewrite the folder the running export hands off;
    it is refused, never folded in and dropped."""
    settings = _settings()
    queue = PendingQueue(storage, settings.vexa_bucket)
    queue.enqueue(_envelope())
    answers: list[str] = []

    def job(
        envelope: dict[str, Any], deps: Deps, rerun: bool = False, progress: Any = None
    ) -> ExportResult:
        answers.append(queue.request_rerun(_envelope()))
        return ExportResult("handed_off", "folder")

    asyncio.run(
        sweep_once(queue, _deps(storage, settings), job=job, now=lambda: 1000.0)
    )

    assert answers == ["export_in_progress"]
    assert queue.pending_ids() == []


def test_a_rerun_is_queued_only_when_nothing_is_pending_for_the_meeting(
    storage: Storage,
) -> None:
    queue = PendingQueue(storage, VEXA_BUCKET)
    queue.enqueue(_envelope())
    assert queue.request_rerun(_envelope()) == "export_in_progress"
    item = queue.load(KEY)
    assert item is not None and item["rerun"] is False

    queue.done(KEY)
    assert queue.request_rerun(_envelope()) == "queued"
    assert queue.request_rerun(_envelope()) == "rerun_queued"
    item = queue.load(KEY)
    assert item is not None and item["rerun"] is True and item["attempts"] == 0


def test_a_rerun_out_of_attempts_is_failed_not_the_earlier_hand_off(
    storage: Storage,
) -> None:
    settings = _settings(max_attempts=1)
    queue = PendingQueue(storage, settings.vexa_bucket)
    storage.put_json(EXPORT_BUCKET, BASE + "_export.json", {"state": "handed_off"})
    queue.request_rerun(_envelope())

    def failing(
        envelope: dict[str, Any], deps: Deps, rerun: bool = False, progress: Any = None
    ) -> ExportResult:
        raise RuntimeError("notetaker 409")

    asyncio.run(
        sweep_once(queue, _deps(storage, settings), job=failing, now=lambda: 1000.0)
    )

    marker = storage.get_json(EXPORT_BUCKET, BASE + "_export.json")
    assert marker["state"] == "failed" and marker["error"] == "notetaker 409"
    assert storage.get_json(VEXA_BUCKET, FAILED_KEY) is not None


def test_one_long_export_does_not_hold_back_the_others(storage: Storage) -> None:
    import threading

    settings = _settings(sweep_seconds=0.01, stop_wait_seconds=0.05)
    queue = PendingQueue(storage, settings.vexa_bucket)
    long_id, short_id = "11111111-1111-4111-8111-111111111111", KEY
    queue.enqueue(_envelope(id=long_id))
    finish = threading.Event()
    done: list[str] = []

    def job(
        envelope: dict[str, Any], deps: Deps, rerun: bool = False, progress: Any = None
    ) -> ExportResult:
        meeting_id = envelope["data"]["meeting"]["id"]
        if meeting_id == long_id:
            finish.wait(5)
        done.append(meeting_id)
        return ExportResult(state="handed_off", folder="x")

    async def scenario() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(
            run_worker(queue, _deps(storage, settings), stop, job=job)
        )
        await asyncio.sleep(0.1)
        queue.enqueue(_envelope())  # arrives while the long one runs
        for _ in range(200):
            if short_id in done:
                break
            await asyncio.sleep(0.01)
        finish.set()
        for _ in range(200):
            if long_id in done:
                break
            await asyncio.sleep(0.01)
        stop.set()
        await asyncio.wait_for(task, timeout=2)

    asyncio.run(scenario())
    assert done == [short_id, long_id]


OTHER = "22222222-2222-4222-8222-222222222222"


def _timed_job(active: list[str], seen: list[tuple[str, ...]], lock: Any) -> Any:
    def job(
        envelope: dict[str, Any], deps: Deps, rerun: bool = False, progress: Any = None
    ) -> ExportResult:
        meeting_id = envelope["data"]["meeting"]["id"]
        with lock:
            active.append(meeting_id)
            seen.append(tuple(sorted(active)))
        time.sleep(0.1)
        with lock:
            active.remove(meeting_id)
        return ExportResult(state="handed_off", folder="x")

    return job


def test_a_meeting_with_a_crash_runs_alone(storage: Storage) -> None:
    """F1: one OOM kill charges a crash to every export of the pod. The
    meeting then runs with no other export beside it, so a second crash is
    proven its own."""
    settings = _settings(concurrency=4)
    queue = PendingQueue(storage, settings.vexa_bucket)
    queue.enqueue(_envelope())
    _left_by_a_dead_run(queue)
    queue.enqueue(_envelope(id=OTHER))
    active: list[str] = []
    seen: list[tuple[str, ...]] = []

    asyncio.run(
        sweep_once(
            queue,
            _deps(storage, settings),
            job=_timed_job(active, seen, threading.Lock()),
            now=lambda: 1000.0,
        )
    )

    assert sorted(seen) == sorted([(KEY,), (OTHER,)]), "each ran with no other"
    assert queue.pending_ids() == []


def test_no_export_starts_while_a_meeting_with_a_crash_runs(storage: Storage) -> None:
    settings = _settings(sweep_seconds=0.01, concurrency=4)
    queue = PendingQueue(storage, settings.vexa_bucket)
    queue.enqueue(_envelope())
    _left_by_a_dead_run(queue)
    started, finish = threading.Event(), threading.Event()
    order: list[str] = []

    def job(
        envelope: dict[str, Any], deps: Deps, rerun: bool = False, progress: Any = None
    ) -> ExportResult:
        meeting_id = envelope["data"]["meeting"]["id"]
        order.append(f"start {meeting_id}")
        if meeting_id == KEY:
            started.set()
            finish.wait(5)
        order.append(f"end {meeting_id}")
        return ExportResult(state="handed_off", folder="x")

    async def scenario() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(
            run_worker(queue, _deps(storage, settings), stop, job=job)
        )
        while not started.is_set():
            await asyncio.sleep(0.01)
        queue.enqueue(_envelope(id=OTHER))  # arrives while the suspect runs
        await asyncio.sleep(0.2)
        finish.set()
        for _ in range(300):
            if f"end {OTHER}" in order:
                break
            await asyncio.sleep(0.01)
        stop.set()
        await asyncio.wait_for(task, timeout=2)

    asyncio.run(scenario())
    assert order == [f"start {KEY}", f"end {KEY}", f"start {OTHER}", f"end {OTHER}"]


def test_the_crash_budget_is_checked_without_a_lease(storage: Storage) -> None:
    """A quarantine that failed leaves the crashes spent and no lease: the
    meeting must not run again."""
    settings = _settings(max_crashes=2)
    queue = PendingQueue(storage, settings.vexa_bucket)
    queue.enqueue(_envelope())
    item = queue.load(KEY)
    assert item is not None
    item["crashes"] = 2
    storage.put_json(VEXA_BUCKET, PENDING_KEY, item)
    calls: list[str] = []

    asyncio.run(
        sweep_once(
            queue, _deps(storage, settings), job=_ok_job(calls), now=lambda: 1000.0
        )
    )

    assert calls == []
    assert storage.get_json(VEXA_BUCKET, PENDING_KEY) is None
    assert storage.get_json(VEXA_BUCKET, FAILED_KEY) is not None


def test_bookkeeping_that_fails_after_the_job_leaves_no_lease(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The job finished; recording that failed. Its lease is released, so the
    next worker runs the meeting again without counting a crash."""
    settings = _settings()
    queue = PendingQueue(storage, settings.vexa_bucket)
    queue.enqueue(_envelope())
    real_done = queue.done
    failures = [RuntimeError("S3 unavailable")]

    def flaky_done(meeting_id: str) -> None:
        if failures:
            raise failures.pop()
        real_done(meeting_id)

    monkeypatch.setattr(queue, "done", flaky_done)
    calls: list[str] = []
    deps = _deps(storage, settings)

    asyncio.run(sweep_once(queue, deps, job=_ok_job(calls), now=lambda: 1000.0))
    item = queue.load(KEY)
    assert item is not None and "lease" not in item

    asyncio.run(sweep_once(queue, deps, job=_ok_job(calls), now=lambda: 9000.0))
    assert calls == ["ran", "ran"]
    assert queue.pending_ids() == []
    assert storage.get_json(VEXA_BUCKET, FAILED_KEY) is None


def test_a_lease_this_worker_left_behind_is_not_a_crash(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recording the outcome and releasing the lease both failed: the worker
    that holds the lease knows it is not running the meeting."""
    settings = _settings(sweep_seconds=0.01, retry_backoff_seconds=0.001)
    queue = PendingQueue(storage, settings.vexa_bucket)
    queue.enqueue(_envelope())
    real_done, real_release = queue.done, queue.release
    crashes: list[str] = []
    failing = {"done": 1, "release": 1}

    def flaky_done(meeting_id: str) -> None:
        if failing["done"]:
            failing["done"] -= 1
            raise RuntimeError("S3 unavailable")
        real_done(meeting_id)

    def flaky_release(meeting_id: str, owner: str) -> None:
        if failing["release"]:
            failing["release"] -= 1
            raise RuntimeError("S3 unavailable")
        real_release(meeting_id, owner)

    monkeypatch.setattr(queue, "done", flaky_done)
    monkeypatch.setattr(queue, "release", flaky_release)
    monkeypatch.setattr(queue, "record_crash", lambda m: crashes.append(m) or 1)
    calls: list[str] = []

    async def scenario() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(
            run_worker(queue, _deps(storage, settings), stop, job=_ok_job(calls))
        )
        for _ in range(300):
            if not queue.pending_ids():
                break
            await asyncio.sleep(0.01)
        stop.set()
        await asyncio.wait_for(task, timeout=2)

    asyncio.run(scenario())
    assert calls == ["ran", "ran"]
    assert crashes == []
    assert queue.pending_ids() == []


def test_renew_extends_only_a_lease_the_owner_still_holds(storage: Storage) -> None:
    queue = PendingQueue(storage, VEXA_BUCKET)
    queue.enqueue(_envelope())
    assert queue.lease(KEY, "me", now=1000.0, lease_seconds=120.0)
    assert queue.renew(KEY, "me", now=1010.0, lease_seconds=120.0)
    item = queue.load(KEY)
    assert item is not None and item["lease"]["expires_at"] == 1130.0

    queue.release(KEY, "me")
    assert not queue.renew(KEY, "me", now=1020.0, lease_seconds=120.0)
    item = queue.load(KEY)
    assert item is not None and "lease" not in item, "a released lease stays released"

    assert queue.lease(KEY, "other", now=1030.0, lease_seconds=120.0)
    assert not queue.renew(KEY, "me", now=1040.0, lease_seconds=120.0)
    item = queue.load(KEY)
    assert item is not None and item["lease"]["owner"] == "other"


def test_a_stopped_worker_waits_for_a_renewal_in_flight_before_it_releases(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(sweep_seconds=0.01, lease_seconds=0.03, stop_wait_seconds=0.05)
    queue = PendingQueue(storage, settings.vexa_bucket)
    queue.enqueue(_envelope())
    in_renewal, renew_go, finish = (threading.Event() for _ in range(3))
    order: list[str] = []
    real_renew, real_release = queue.renew, queue.release

    def slow_renew(meeting_id: str, owner: str, **kw: Any) -> bool:
        in_renewal.set()
        renew_go.wait(5)
        held = real_renew(meeting_id, owner, **kw)
        order.append("renewed")
        return held

    def release(meeting_id: str, owner: str) -> None:
        order.append("released")
        real_release(meeting_id, owner)

    monkeypatch.setattr(queue, "renew", slow_renew)
    monkeypatch.setattr(queue, "release", release)

    def slow_job(
        envelope: dict[str, Any], deps: Deps, rerun: bool = False, progress: Any = None
    ) -> ExportResult:
        finish.wait(5)
        raise RuntimeError("killed with the pod")

    async def scenario() -> dict[str, Any] | None:
        stop = asyncio.Event()
        task = asyncio.create_task(
            run_worker(queue, _deps(storage, settings), stop, job=slow_job)
        )
        while not in_renewal.is_set():
            await asyncio.sleep(0.01)
        stop.set()
        await asyncio.sleep(0.1)
        assert order == [], "nothing is released while a renewal is in flight"
        renew_go.set()
        await asyncio.wait_for(task, timeout=2)
        item = queue.load(KEY)
        finish.set()
        return item

    item = asyncio.run(scenario())
    assert order == ["renewed", "released"]
    assert item is not None and "lease" not in item and "crashes" not in item


def _racing_get(
    storage: Storage, monkeypatch: pytest.MonkeyPatch, race: Callable[[Any], Any]
) -> None:
    """The first read of the pending item lets another writer in right after
    it: `race(response)` runs then, and returns the response to hand back."""
    client = storage._client
    real_get = client.get_object
    raced: list[bool] = []

    def get_object(**kwargs: Any) -> Any:
        if kwargs.get("Key") == PENDING_KEY and not raced:
            raced.append(True)
            return race(lambda: real_get(**kwargs))
        return real_get(**kwargs)

    monkeypatch.setattr(client, "get_object", get_object)


def test_a_change_that_loses_to_a_concurrent_write_is_made_again_on_it(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure is recorded while a redelivered webhook refreshes the item:
    neither change is lost (the write is conditional on the version read)."""
    queue = PendingQueue(storage, VEXA_BUCKET)
    queue.enqueue(_envelope())
    refreshed = _envelope(ended_at="2026-06-18T10:50:00Z")

    def race(read: Callable[[], Any]) -> Any:
        response = read()
        body = json.loads(response["Body"].read())
        response["Body"] = io.BytesIO(json.dumps(body).encode())
        storage._client.put_object(
            Bucket=VEXA_BUCKET,
            Key=PENDING_KEY,
            Body=json.dumps({**body, "envelope": refreshed}).encode(),
        )
        return response

    _racing_get(storage, monkeypatch, race)

    assert queue.record_failure(KEY, "boom", backoff_seconds=30.0, now=lambda: 1.0) == 1
    item = queue.load(KEY)
    assert item is not None
    assert item["attempts"] == 1 and item["envelope"] == refreshed


def test_a_webhook_and_a_rerun_creating_the_item_at_once_keep_the_rerun(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The webhook read no item; a rerun was created before it wrote. Its
    create (`IfNoneMatch`) fails and it refreshes the rerun instead."""
    queue = PendingQueue(storage, VEXA_BUCKET)
    rerun_item = {
        "envelope": _envelope(),
        "attempts": 0,
        "next_attempt_at": 0.0,
        "last_error": None,
        "rerun": True,
    }

    def race(read: Callable[[], Any]) -> Any:
        storage._client.put_object(
            Bucket=VEXA_BUCKET, Key=PENDING_KEY, Body=json.dumps(rerun_item).encode()
        )
        raise ClientError({"Error": {"Code": "NoSuchKey", "Message": "-"}}, "GetObject")

    _racing_get(storage, monkeypatch, race)

    queue.enqueue(_envelope())

    item = queue.load(KEY)
    assert item is not None and item["rerun"] is True


def test_the_worker_never_blocks_the_event_loop_on_s3(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every S3 call of a run and its quarantine is made in a thread, so the
    webhook intake and /healthz keep answering."""
    settings = _settings(max_attempts=1)
    queue = PendingQueue(storage, settings.vexa_bucket)
    queue.enqueue(_envelope())
    client = storage._client
    real_get, real_put = client.get_object, client.put_object

    def slow_get(**kwargs: Any) -> Any:
        time.sleep(0.15)
        return real_get(**kwargs)

    def slow_put(**kwargs: Any) -> Any:
        time.sleep(0.15)
        return real_put(**kwargs)

    monkeypatch.setattr(client, "get_object", slow_get)
    monkeypatch.setattr(client, "put_object", slow_put)

    def failing(
        envelope: dict[str, Any], deps: Deps, rerun: bool = False, progress: Any = None
    ) -> ExportResult:
        raise RuntimeError("boom")

    async def scenario() -> float:
        gaps: list[float] = []
        finished = asyncio.Event()

        async def ticker() -> None:
            last = time.monotonic()
            while not finished.is_set():
                await asyncio.sleep(0.01)
                now = time.monotonic()
                gaps.append(now - last)
                last = now

        ticking = asyncio.create_task(ticker())
        await sweep_once(queue, _deps(storage, settings), job=failing)
        finished.set()
        await ticking
        return max(gaps)

    assert asyncio.run(scenario()) < 0.1
    marker = storage.get_json(EXPORT_BUCKET, BASE + "_export.json")
    assert marker is not None and marker["state"] == "failed"
    assert storage.get_json(VEXA_BUCKET, FAILED_KEY) is not None


def test_a_rerun_keeps_its_hand_off_on_the_item_and_a_retry_gets_it(
    storage: Storage,
) -> None:
    """Review 1a: the hand-off marker a rerun records after its /process is
    on the pending item, and the retry's job is given it."""
    settings = _settings(retry_backoff_seconds=0.0)
    queue = PendingQueue(storage, settings.vexa_bucket)
    queue.request_rerun(_envelope())
    marker = {"state": "handed_off", "meeting_id": MEETING_UUID}
    given: list[Any] = []

    def job(
        envelope: dict[str, Any], deps: Deps, rerun: bool = False, progress: Any = None
    ) -> ExportResult:
        given.append(progress.handed_off)
        if progress.handed_off is None:
            progress.record(marker)
            raise ExportReportError("gateway 503")
        return ExportResult("handed_off", "x")

    deps = _deps(storage, settings)
    asyncio.run(sweep_once(queue, deps, job=job, now=lambda: 1000.0))
    item = queue.load(KEY)
    assert item is not None and item["rerun_handed_off"] == marker
    asyncio.run(sweep_once(queue, deps, job=job, now=lambda: 2000.0))

    assert given == [None, marker]
    assert queue.pending_ids() == []


def test_a_rerun_out_of_attempts_after_its_hand_off_keeps_the_hand_off(
    storage: Storage,
) -> None:
    """Its transcript is being redone: the quarantine must not turn the
    hand-off into failed."""
    settings = _settings(max_attempts=1)
    queue = PendingQueue(storage, settings.vexa_bucket)
    storage.put_json(EXPORT_BUCKET, BASE + "_export.json", {"state": "handed_off"})
    queue.request_rerun(_envelope())

    def job(
        envelope: dict[str, Any], deps: Deps, rerun: bool = False, progress: Any = None
    ) -> ExportResult:
        progress.record({"state": "handed_off"})
        raise RuntimeError("S3 put of _export.json failed")

    asyncio.run(
        sweep_once(queue, _deps(storage, settings), job=job, now=lambda: 1000.0)
    )

    marker = storage.get_json(EXPORT_BUCKET, BASE + "_export.json")
    assert marker == {"state": "handed_off"}


def test_healthz_fails_while_an_export_runs_past_the_job_timeout(
    storage: Storage,
) -> None:
    """Review 2: a hung export gets the pod restarted by the liveness probe."""
    settings = _settings(job_timeout_seconds=60.0)
    queue = PendingQueue(storage, settings.vexa_bucket)
    worker = WorkerState(settings.concurrency)
    app = create_app(
        settings, queue, _deps(storage, settings), start_worker=False, worker=worker
    )
    with TestClient(app) as client:
        worker.started[KEY] = time.monotonic() - 30
        assert client.get("/healthz").status_code == 200
        worker.started[KEY] = time.monotonic() - 61
        resp = client.get("/healthz")
    assert resp.status_code == 503 and resp.json() == {"status": "export_overdue"}


def test_a_running_job_is_on_the_watchdog_clock_until_it_ends(
    storage: Storage,
) -> None:
    settings = _settings(sweep_seconds=0.01)
    queue = PendingQueue(storage, settings.vexa_bucket)
    queue.enqueue(_envelope())
    worker = WorkerState(settings.concurrency)
    started, finish = threading.Event(), threading.Event()

    def job(
        envelope: dict[str, Any], deps: Deps, rerun: bool = False, progress: Any = None
    ) -> ExportResult:
        started.set()
        finish.wait(5)
        return ExportResult("handed_off", "x")

    async def scenario() -> tuple[list[str], list[str]]:
        stop = asyncio.Event()
        task = asyncio.create_task(
            run_worker(queue, _deps(storage, settings), stop, job=job, worker=worker)
        )
        while not started.is_set():
            await asyncio.sleep(0.01)
        during = list(worker.started)
        finish.set()
        for _ in range(300):
            if not queue.pending_ids():
                break
            await asyncio.sleep(0.01)
        stop.set()
        await asyncio.wait_for(task, timeout=2)
        return during, list(worker.started)

    during, after = asyncio.run(scenario())
    assert during == [KEY] and after == []


def test_a_stopped_worker_lets_a_running_export_finish_and_record_it(
    storage: Storage,
) -> None:
    """Review 7: a job finishing inside the grace period records done, so the
    next pod does not export it again."""
    settings = _settings(sweep_seconds=0.01, stop_wait_seconds=5.0)
    queue = PendingQueue(storage, settings.vexa_bucket)
    queue.enqueue(_envelope())
    started = threading.Event()

    def job(
        envelope: dict[str, Any], deps: Deps, rerun: bool = False, progress: Any = None
    ) -> ExportResult:
        started.set()
        time.sleep(0.3)
        return ExportResult("handed_off", "x")

    async def scenario() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(
            run_worker(queue, _deps(storage, settings), stop, job=job)
        )
        while not started.is_set():
            await asyncio.sleep(0.01)
        stop.set()
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(scenario())
    assert queue.pending_ids() == []


def test_a_stopped_worker_leaves_a_hung_export_leased_to_count_a_crash(
    storage: Storage,
) -> None:
    """A job past EXPORT_JOB_TIMEOUT_SECONDS hung: its lease is not released,
    so the next worker charges the crash and runs it alone."""
    settings = _settings(
        sweep_seconds=0.01, stop_wait_seconds=0.05, job_timeout_seconds=0.05
    )
    queue = PendingQueue(storage, settings.vexa_bucket)
    queue.enqueue(_envelope())
    started, finish = threading.Event(), threading.Event()

    def hung(
        envelope: dict[str, Any], deps: Deps, rerun: bool = False, progress: Any = None
    ) -> ExportResult:
        started.set()
        finish.wait(5)
        return ExportResult("handed_off", "x")

    async def scenario() -> dict[str, Any] | None:
        stop = asyncio.Event()
        task = asyncio.create_task(
            run_worker(queue, _deps(storage, settings), stop, job=hung)
        )
        while not started.is_set():
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.1)
        stop.set()
        await asyncio.wait_for(task, timeout=2)
        item = queue.load(KEY)
        finish.set()
        return item

    item = asyncio.run(scenario())
    assert item is not None and "lease" in item


def test_bookkeeping_that_keeps_failing_stops_rerunning_the_job(
    storage: Storage, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Review 5: done() always fails (e.g. DeleteObject denied). The job runs
    again only after a growing backoff, and after max_attempts failures not
    again until the exporter restarts."""
    settings = _settings(
        sweep_seconds=0.01, retry_backoff_seconds=0.001, max_attempts=3
    )
    queue = PendingQueue(storage, settings.vexa_bucket)
    queue.enqueue(_envelope())

    def denied(meeting_id: str) -> None:
        raise RuntimeError("AccessDenied")

    monkeypatch.setattr(queue, "done", denied)
    calls: list[str] = []

    async def scenario() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(
            run_worker(queue, _deps(storage, settings), stop, job=_ok_job(calls))
        )
        await asyncio.sleep(0.6)
        stop.set()
        await asyncio.wait_for(task, timeout=2)

    with caplog.at_level(logging.ERROR, logger="exporter"):
        asyncio.run(scenario())
    assert calls == ["ran", "ran", "ran"]
    assert any(
        "bookkeeping failed 3 times in a row" in r.getMessage() for r in caplog.records
    )


def test_a_job_gets_the_item_as_leased_not_as_read_before_its_slot(
    storage: Storage,
) -> None:
    """Review 6: an envelope refreshed while the meeting waited for a slot is
    the one exported."""
    settings = _settings(concurrency=1)
    queue = PendingQueue(storage, settings.vexa_bucket)
    queue.enqueue(_envelope(id=OTHER))
    queue.enqueue(_envelope())
    order: list[tuple[str, str]] = []

    def job(
        envelope: dict[str, Any], deps: Deps, rerun: bool = False, progress: Any = None
    ) -> ExportResult:
        meeting = envelope["data"]["meeting"]
        order.append((meeting["id"], meeting["ended_at"]))
        if len(order) == 1:  # the other one waits for the slot meanwhile
            other = OTHER if meeting["id"] == KEY else KEY
            queue.enqueue(_envelope(id=other, ended_at="2026-06-18T10:59:00Z"))
        return ExportResult("handed_off", "x")

    asyncio.run(
        sweep_once(queue, _deps(storage, settings), job=job, now=lambda: 1000.0)
    )
    assert [ended for _, ended in order] == [
        "2026-06-18T10:42:00Z",
        "2026-06-18T10:59:00Z",
    ]


def _resent_after_a_lost_answer(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first conditional write of the pending item lands, its answer is
    lost and botocore sends it again, which S3 refuses (412)."""
    client = storage._client
    real_put = client.put_object
    resent: list[bool] = []

    def put_object(**kwargs: Any) -> Any:
        conditional = "IfMatch" in kwargs or "IfNoneMatch" in kwargs
        if kwargs.get("Key") == PENDING_KEY and conditional and not resent:
            resent.append(True)
            real_put(**kwargs)
        return real_put(**kwargs)

    monkeypatch.setattr(client, "put_object", put_object)


def test_a_write_resent_after_its_answer_was_lost_is_not_applied_twice(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review 4."""
    queue = PendingQueue(storage, VEXA_BUCKET)
    queue.enqueue(_envelope())
    _resent_after_a_lost_answer(storage, monkeypatch)

    assert queue.record_failure(KEY, "boom", backoff_seconds=30.0, now=lambda: 1.0) == 1
    item = queue.load(KEY)
    assert item is not None and item["attempts"] == 1


def test_a_rerun_request_resent_after_its_answer_was_lost_is_queued(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue = PendingQueue(storage, VEXA_BUCKET)
    _resent_after_a_lost_answer(storage, monkeypatch)

    assert queue.request_rerun(_envelope()) == "queued"


def test_caplog_shows_job_failure_and_quarantine(
    storage: Storage, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="exporter")
    settings = _settings(max_attempts=1)
    queue = PendingQueue(storage, settings.vexa_bucket)
    queue.enqueue(_envelope())
    deps = _deps(storage, settings)

    def failing_job(
        envelope: dict[str, Any], deps: Deps, rerun: bool = False, progress: Any = None
    ) -> ExportResult:
        raise RuntimeError("boom")

    asyncio.run(sweep_once(queue, deps, job=failing_job))

    messages = " | ".join(record.getMessage() for record in caplog.records)
    assert "export job failed" in messages
    assert f"meeting_id={MEETING_UUID}" in messages
    assert "quarantine" in messages


def test_import_main_module_is_safe() -> None:
    import exporter.__main__  # noqa: F401


# ---------------------------------------------------------------------------
# The subscription delivery: signature, events, dedupe (design §2.7)
# ---------------------------------------------------------------------------


def _client(storage: Storage, settings: Settings | None = None) -> tuple[Any, Any]:
    settings = settings or _settings()
    queue = PendingQueue(storage, settings.vexa_bucket)
    app = create_app(
        settings,
        queue,
        _deps(storage, settings),
        clock=_fixed_clock,
        start_worker=False,
    )
    return TestClient(app), queue


def _post_signed(
    client: Any, envelope: Any, headers: dict[str, str] | None = None
) -> Any:
    body = json.dumps(envelope).encode()
    return client.post("/hooks/vexa", content=body, headers=headers or _sign(body))


def test_a_rotated_delivery_verifies_on_the_previous_header(storage: Storage) -> None:
    """For 24 h after `rotate-secret` aw-bots signs with the new secret and
    sends the old one's signature in `X-Webhook-Signature-Previous`; an
    exporter still on the old secret keeps receiving."""
    body = json.dumps(_envelope()).encode()
    headers = _sign(body, secret="the-new-secret")
    headers["X-Webhook-Signature-Previous"] = _sign(body)["X-Webhook-Signature"]
    client, queue = _client(storage)
    with client:
        resp = client.post("/hooks/vexa", content=body, headers=headers)
    assert resp.status_code == 202
    assert queue.pending_ids() == [KEY]


def test_a_signature_under_another_secret_is_401(storage: Storage) -> None:
    body = json.dumps(_envelope()).encode()
    client, queue = _client(storage)
    with client:
        resp = client.post(
            "/hooks/vexa", content=body, headers=_sign(body, secret="other")
        )
    assert resp.status_code == 401
    assert queue.pending_ids() == []


@pytest.mark.parametrize("ts", ["699", "1401"])
def test_a_timestamp_more_than_300_s_off_is_401(storage: Storage, ts: str) -> None:
    body = json.dumps(_envelope()).encode()
    client, queue = _client(storage)
    with client:
        resp = client.post("/hooks/vexa", content=body, headers=_sign(body, ts=ts))
    assert resp.status_code == 401
    assert queue.pending_ids() == []


# Every webhook.v1 EventType the exporter does not act on.
IGNORED_EVENTS = [
    "meeting.scheduled",
    "meeting.updated",
    "meeting.removed",
    "meeting.waiting_for_room",
    "meeting.not_sent",
    "meeting.status_change",
    "meeting.started",
    "bot.retry",
    "recording.ready",
    "transcription.ready",
    "export.handed_off",
    "export.failed",
]


@pytest.mark.parametrize("event_type", IGNORED_EVENTS)
def test_every_other_event_is_answered_2xx_and_ignored(
    storage: Storage, event_type: str
) -> None:
    client, queue = _client(storage)
    with client:
        resp = _post_signed(client, _envelope(event_type))
    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored"}
    assert queue.pending_ids() == []
    assert storage.list_keys(VEXA_BUCKET, "aw-exporter/") == []


def test_a_verified_webhook_test_is_answered_2xx_and_does_nothing(
    storage: Storage,
) -> None:
    envelope = {
        "event_id": "evt_test_" + "0c4d8e2f" * 4,
        "event_type": "webhook.test",
        "api_version": "2026-09-25",
        "created_at": "2026-06-18T10:42:00Z",
        "data": {"subscription_id": "2d9f6c1e-4b7a-4e3d-8c5f-1a2b3c4d5e6f"},
    }
    client, queue = _client(storage)
    with client:
        resp = _post_signed(client, envelope)
    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored"}
    assert storage.list_keys(VEXA_BUCKET, "aw-exporter/") == []


@pytest.mark.parametrize("event_type", ["meeting.completed", "bot.failed"])
def test_each_exported_event_is_queued_under_the_meeting_uuid(
    storage: Storage, event_type: str
) -> None:
    envelope = _envelope(
        event_type, status="failed" if "failed" in event_type else "completed"
    )
    client, queue = _client(storage)
    with client:
        resp = _post_signed(client, envelope)
    assert resp.status_code == 202
    assert queue.pending_ids() == [KEY]
    stored = storage.get_json(VEXA_BUCKET, PENDING_KEY)
    assert stored is not None and stored["envelope"] == envelope


@pytest.mark.parametrize(
    "event_id", [None, "", "evt_test", "evt_" + "G" * 64, "../evt_" + "a" * 64]
)
def test_an_exported_event_without_a_valid_event_id_is_400(
    storage: Storage, event_id: Any
) -> None:
    envelope = _envelope()
    if event_id is None:
        del envelope["event_id"]
    else:
        envelope["event_id"] = event_id
    client, queue = _client(storage)
    with client:
        resp = _post_signed(client, envelope)
    assert resp.status_code == 400
    assert queue.pending_ids() == []


@pytest.mark.parametrize("upstream_id", [None, "11367", 0, True])
def test_an_exported_event_without_an_integer_upstream_id_is_400(
    storage: Storage, upstream_id: Any
) -> None:
    client, queue = _client(storage)
    with client:
        resp = _post_signed(client, _envelope(upstream_id=upstream_id))
    assert resp.status_code == 400
    assert queue.pending_ids() == []


def test_a_redelivered_event_is_a_duplicate_and_queued_once(
    storage: Storage, caplog: pytest.LogCaptureFixture
) -> None:
    """At-least-once delivery: the same `event_id` again is answered 2xx and
    changes nothing, while the meeting is pending and after it is done."""
    settings = _settings()
    client, queue = _client(storage, settings)
    with client, caplog.at_level(logging.INFO, logger="exporter"):
        first = _post_signed(client, _envelope())
        queue.record_failure(KEY, "boom", backoff_seconds=30.0, now=lambda: 1000.0)
        again = _post_signed(client, _envelope())
        pending = queue.load(KEY)
        queue.done(KEY)
        after_done = _post_signed(client, _envelope())

    assert first.status_code == 202
    assert again.status_code == 200 and again.json() == {"status": "duplicate"}
    assert after_done.status_code == 200
    assert after_done.json() == {"status": "duplicate"}
    assert pending is not None and pending["attempts"] == 1
    assert queue.pending_ids() == []
    assert storage.get_json(VEXA_BUCKET, EVENT_KEY) == {
        "meeting_id": MEETING_UUID,
        "event_type": "meeting.completed",
        "sequence": 9,
    }
    assert any(
        r.getMessage()
        == f"duplicate_event event_id={EVENT_ID} meeting_id={MEETING_UUID}; ignored"
        for r in caplog.records
    )


def test_an_event_that_could_not_be_recorded_is_503_and_queued_on_redelivery(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The event is recorded after it is queued, so a failed record never
    loses it: the 503 makes aw-bots retry, and the retry is queued again."""
    client, queue = _client(storage)
    real = queue.record_event
    calls = {"n": 0}

    def flaky(envelope: dict[str, Any]) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("s3 put failed")
        real(envelope)

    monkeypatch.setattr(queue, "record_event", flaky)
    with client:
        first = _post_signed(client, _envelope())
        retry = _post_signed(client, _envelope())
    assert first.status_code == 503
    assert retry.status_code == 202
    assert queue.pending_ids() == [KEY]
    assert queue.seen(EVENT_ID)


def test_a_second_event_for_the_same_meeting_refreshes_the_pending_item(
    storage: Storage,
) -> None:
    """One export per meeting: the queue is keyed by the meeting, so a
    `bot.failed` and a `meeting.completed` of one meeting are one item."""
    client, queue = _client(storage)
    failed = _envelope("bot.failed", status="failed")
    completed = meeting_event(event_id="evt_" + "b" * 64)
    with client:
        assert _post_signed(client, failed).status_code == 202
        assert _post_signed(client, completed).status_code == 202
    assert queue.pending_ids() == [KEY]
    item = queue.load(KEY)
    assert item is not None and item["envelope"] == completed


def test_the_export_runs_once_for_a_redelivered_event(storage: Storage) -> None:
    settings = _settings()
    client, queue = _client(storage, settings)
    deps = _deps(storage, settings)
    runs: list[str] = []

    def job(
        envelope: dict[str, Any], deps: Deps, rerun: bool = False, progress: Any = None
    ) -> ExportResult:
        runs.append(envelope["event_id"])
        return ExportResult("handed_off", FOLDER)

    with client:
        _post_signed(client, _envelope())
        asyncio.run(sweep_once(queue, deps, job=job))
        _post_signed(client, _envelope())
        asyncio.run(sweep_once(queue, deps, job=job))
    assert runs == [EVENT_ID]
    assert queue.pending_ids() == []
