"""Tests for exporter.queue (durable pending queue + sweep worker) and
exporter.app (the signed aw-bots subscription intake) — spec §4.1, design
§1.9, §2.7."""

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
from exporter.audio import join_webm  # noqa: E402
from exporter.config import Settings  # noqa: E402
from exporter.export_result import ExportReporter  # noqa: E402
from exporter.job import Deps, ExportResult  # noqa: E402
from exporter.naming import folder_name  # noqa: E402
from exporter.notetaker import Notetaker  # noqa: E402
from exporter.queue import PendingQueue, run_worker, sweep_once  # noqa: E402
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
    envelope = _envelope_missing("started_at")
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
    attempts = queue.record_failure(KEY, "boom", now=lambda: 1000.0)
    assert attempts == 1
    item = queue.load(KEY)
    assert item is not None
    assert item["last_error"] == "boom"
    assert item["next_attempt_at"] == 1000.0 + 30 * 2**1


def test_fail_moves_pending_to_failed(storage: Storage) -> None:
    queue = PendingQueue(storage, VEXA_BUCKET)
    queue.enqueue(_envelope())
    queue.record_failure(KEY, "boom", now=lambda: 1000.0)
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
    queue.record_failure(KEY, "boom", now=lambda: 1000.0)

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

    def selective_job(envelope: dict[str, Any], deps: Deps) -> ExportResult:
        if envelope["data"]["meeting"]["id"] == "2":
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
        queue.record_failure(KEY, "boom", now=lambda: 1000.0)
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

    def job(envelope: dict[str, Any], deps: Deps) -> ExportResult:
        runs.append(envelope["event_id"])
        return ExportResult("handed_off", FOLDER)

    with client:
        _post_signed(client, _envelope())
        asyncio.run(sweep_once(queue, deps, job=job))
        _post_signed(client, _envelope())
        asyncio.run(sweep_once(queue, deps, job=job))
    assert runs == [EVENT_ID]
    assert queue.pending_ids() == []
