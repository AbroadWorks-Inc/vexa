"""The exporter against the sealed webhook.v1 goldens (design §2.7).

The goldens are meeting-api's real emitter output (`tests/test_webhook_goldens.py` there): what
aw-bots posts to a subscriber, and the headers it signs them with. The exporter must verify those
headers byte for byte and act on exactly the events it exports.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import boto3
import httpx
import pytest
from moto import mock_aws

warnings.filterwarnings(
    "ignore",
    message=r"Using `httpx` with `starlette\.testclient` is deprecated.*",
    category=UserWarning,
)

from fastapi.testclient import TestClient  # noqa: E402

from exporter.app import create_app  # noqa: E402
from exporter.audio import join_webm, webm_to_wav  # noqa: E402
from exporter.config import Settings  # noqa: E402
from exporter.export_result import ExportReporter  # noqa: E402
from exporter.job import Deps  # noqa: E402
from exporter.naming import folder_name  # noqa: E402
from exporter.notetaker import Notetaker  # noqa: E402
from exporter.queue import PendingQueue  # noqa: E402
from exporter.signature import verify  # noqa: E402
from exporter.storage import Storage  # noqa: E402
from exporter.vexa_client import MeetingApi  # noqa: E402

# The dummy secrets and instant meeting-api's golden test signs with.
SECRET = "whsec_demo_secret"
PREVIOUS_SECRET = "whsec_demo_previous_secret"
TIMESTAMP = 1790658761
VEXA_BUCKET = "aw-bots"


def _golden(name: str) -> Any:
    rel = Path("core") / "meetings" / "contracts" / "webhook.v1" / "golden"
    for parent in Path(__file__).resolve().parents:
        if (parent / rel).is_dir():
            return json.loads((parent / rel / f"{name}.json").read_text())
    raise FileNotFoundError(rel)


def _body(name: str) -> bytes:
    """The stored body aw-bots posts: the envelope serialised once, compactly
    with sorted keys."""
    return json.dumps(_golden(name), separators=(",", ":"), sort_keys=True).encode()


@pytest.mark.parametrize("secret", [SECRET, PREVIOUS_SECRET])
def test_the_rotated_golden_headers_verify_under_either_secret(secret: str) -> None:
    headers = _golden("SignatureHeaders.rotated")
    assert verify(_body("MeetingEvent.meeting-completed"), headers, secret, TIMESTAMP)


def test_the_golden_headers_verify_under_the_current_secret_only() -> None:
    headers = _golden("SignatureHeaders.subscription")
    body = _body("MeetingEvent.meeting-completed")
    assert verify(body, headers, SECRET, TIMESTAMP)
    assert not verify(body, headers, PREVIOUS_SECRET, TIMESTAMP)


@pytest.fixture
def client(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, PendingQueue]]:
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(name, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=VEXA_BUCKET)
        storage = Storage(s3)
        settings = Settings(
            gateway_url="http://gateway",
            exporter_api_key="test-exporter-key",
            webhook_secret=SECRET,
            vexa_bucket=VEXA_BUCKET,
            export_bucket="aw-chatworks-transcribe",
            export_prefix="recordings/",
            notetaker_url="http://notetaker",
        )
        http = httpx.Client()
        deps = Deps(
            settings=settings,
            storage=storage,
            meeting_api=MeetingApi(settings.gateway_url, "k", http),
            notetaker=Notetaker(settings.notetaker_url, http),
            export_result=ExportReporter(settings.gateway_url, "k", http),
            transcode=webm_to_wav,
            now=lambda: datetime.now(timezone.utc),
            join_webm=join_webm,
        )
        queue = PendingQueue(storage, VEXA_BUCKET)
        app = create_app(
            settings, queue, deps, clock=lambda: TIMESTAMP, start_worker=False
        )
        with TestClient(app) as test_client:
            yield test_client, queue


def _signed(body: bytes) -> dict[str, str]:
    mac = hmac.new(SECRET.encode(), f"{TIMESTAMP}.".encode() + body, hashlib.sha256)
    return {
        "X-Webhook-Timestamp": str(TIMESTAMP),
        "X-Webhook-Signature": f"sha256={mac.hexdigest()}",
    }


def _post(test_client: TestClient, name: str) -> httpx.Response:
    body = _body(name)
    return test_client.post("/hooks/vexa", content=body, headers=_signed(body))


def test_the_golden_meeting_completed_is_queued_under_its_uuid(
    client: tuple[TestClient, PendingQueue],
) -> None:
    """Posted exactly as aw-bots sends it: the stored body with the golden
    rotated headers."""
    test_client, queue = client
    meeting = _golden("MeetingEvent.meeting-completed")["data"]["meeting"]

    resp = test_client.post(
        "/hooks/vexa",
        content=_body("MeetingEvent.meeting-completed"),
        headers=_golden("SignatureHeaders.rotated"),
    )

    assert resp.status_code == 202
    assert queue.pending_ids() == [meeting["id"]]
    assert folder_name(meeting["platform"], meeting["room"], meeting["started_at"]) == (
        "google_meet_abc-defg-hij_20260929T042611000Z"
    )


def test_the_golden_bot_failed_that_never_started_is_ignored(
    client: tuple[TestClient, PendingQueue],
) -> None:
    """The golden `bot.failed` was refused in the lobby: no `started_at`,
    nothing recorded."""
    test_client, queue = client
    assert _golden("MeetingEvent.bot-failed")["data"]["meeting"]["started_at"] is None

    resp = _post(test_client, "MeetingEvent.bot-failed")

    assert resp.status_code == 200 and resp.json() == {"status": "ignored"}
    assert queue.pending_ids() == []


@pytest.mark.parametrize(
    "name",
    [
        "MeetingEvent.bot-retry",
        "MeetingEvent.meeting-updated",
        "TestEvent.webhook-test",
    ],
)
def test_the_other_goldens_are_ignored(
    client: tuple[TestClient, PendingQueue], name: str
) -> None:
    test_client, queue = client

    resp = _post(test_client, name)

    assert resp.status_code == 200 and resp.json() == {"status": "ignored"}
    assert queue.pending_ids() == []
