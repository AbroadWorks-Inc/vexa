"""Container image + compose integration test (task 10).

Builds the aw-exporter Docker image, wires it up against a local MinIO
(standing in for S3) plus in-process stub meeting-api and notetaker
servers, and drives one `meeting.completed` webhook through the full
container end to end: the copy of `master.webm`, the `audio.wav` transcode,
the speaker-attribution artifacts, and the `POST /process` hand-off.

Opt-in only (`@pytest.mark.integration`, excluded from the default run by
`pyproject.toml`'s `addopts`); run with `pytest -m integration -q
tests/integration`. Requires a local Docker daemon and network access to
pull `quay.io/minio/minio:latest` (Docker Hub refuses anonymous pulls in
this environment, so MinIO's own registry is used, matching upstream's
compose harness — see `image-licenses.json`'s `minio/minio` entry for the
licensing rationale).
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import socket
import subprocess
import threading
import time
import uuid
import wave
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import boto3
import httpx
import pytest
import uvicorn
from botocore.exceptions import ClientError

from aw_exporter.naming import folder_name
from tests.integration.stub_meeting_api import create_app as create_meeting_api_app
from tests.integration.stub_notetaker import create_app as create_notetaker_app

if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client

pytestmark = pytest.mark.integration

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
IMAGE_TAG = "aw-exporter:it"

VEXA_BUCKET = "aw-bots"
EXPORT_BUCKET = "aw-chatworks-transcribe"
WEBHOOK_SECRET = "it-webhook-secret"  # test-only literal, not a real credential
MINIO_ACCESS_KEY = "minio-it"  # test-only literal
MINIO_SECRET_KEY = "minio-it-secret"  # test-only literal

USER_ID = 1
VEXA_MEETING_ID = 99
RECORDING_ID = 2
SESSION_UID = "sess-1"
NATIVE_MEETING_ID = "it-synthetic-meet-abcd"
STORAGE_PATH = f"recordings/{USER_ID}/{RECORDING_ID}/{SESSION_UID}/audio/master.webm"
TAPE_KEY = f"signal/{USER_ID}/{VEXA_MEETING_ID}/{SESSION_UID}/captured-signal.jsonl"
START_TIME = "2026-09-23T10:00:00.000Z"
END_TIME = "2026-09-23T10:00:03.000Z"
RECORDING_CREATED_AT = "2026-09-23T10:00:00.000Z"
AUDIO_DURATION_S = 3.0

FOLDER = folder_name("google_meet", NATIVE_MEETING_ID, START_TIME)
BASE = f"recordings/{FOLDER}/"

EXPECTED_KEYS = {
    BASE + "master.webm",
    BASE + "audio.wav",
    BASE + "speaker_timeline.json",
    BASE + "participants.json",
    BASE + "meeting.json",
    BASE + "recordings.json",
    BASE + "_export.json",
}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("0.0.0.0", 0))
        return int(sock.getsockname()[1])


def _run_docker(*args: str, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout
    )


def _wait_for_http(url: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(url, timeout=2)
            if resp.status_code < 500:
                return
        except httpx.TransportError as exc:
            last_exc = exc
        time.sleep(0.3)
    raise TimeoutError(f"timed out waiting for {url}: {last_exc}")


class _UvicornThread:
    """Runs a FastAPI app on a background thread, for the stub servers."""

    def __init__(self, app: Any, port: int) -> None:
        config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning")
        self.server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self) -> None:
        self._thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("stub server did not start within 10s")
            time.sleep(0.05)

    def stop(self) -> None:
        self.server.should_exit = True
        self._thread.join(timeout=10)


def _make_sine_webm(dst: Path, duration_s: float) -> None:
    result = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={duration_s}",
            "-c:a",
            "libopus",
            str(dst),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg seed generation failed: {result.stderr}")


def _sign(body: bytes, secret: str) -> dict[str, str]:
    ts = str(int(time.time()))
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256)
    return {
        "X-Webhook-Signature": f"sha256={mac.hexdigest()}",
        "X-Webhook-Timestamp": ts,
    }


def _envelope() -> dict[str, Any]:
    return {
        "event_id": "evt-it-1",
        "event_type": "meeting.completed",
        "data": {
            "meeting": {
                "id": VEXA_MEETING_ID,
                "user_id": USER_ID,
                "platform": "google_meet",
                "native_meeting_id": NATIVE_MEETING_ID,
                "start_time": START_TIME,
                "end_time": END_TIME,
                "constructed_meeting_url": "https://meet.google.com/it-synthetic-meet",
                "status": "completed",
                "data": {"name": "aw-exporter compose integration test"},
            }
        },
    }


def _list_keys(s3: S3Client, bucket: str, prefix: str) -> set[str]:
    paginator = s3.get_paginator("list_objects_v2")
    keys: set[str] = set()
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            keys.add(obj["Key"])
    return keys


def _wait_for_export_marker(s3: S3Client, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    key = BASE + "_export.json"
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        try:
            obj = s3.get_object(Bucket=EXPORT_BUCKET, Key=key)
            last = json.loads(obj["Body"].read())
            if last.get("state") == "handed_off":
                return last
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") not in {"404", "NoSuchKey"}:
                raise
        time.sleep(0.5)
    raise TimeoutError(f"_export.json not handed_off within {timeout}s (last={last})")


@pytest.fixture(scope="module")
def built_image() -> str:
    result = _run_docker("build", "-t", IMAGE_TAG, str(PACKAGE_ROOT), timeout=300)
    if result.returncode != 0:
        pytest.fail(f"docker build failed:\n{result.stdout}\n{result.stderr}")
    return IMAGE_TAG


@pytest.fixture
def suffix() -> str:
    return uuid.uuid4().hex[:8]


@pytest.fixture
def minio(suffix: str) -> Iterator[dict[str, Any]]:
    name = f"aw-exporter-it-minio-{suffix}"
    port = _free_port()
    try:
        run = _run_docker(
            "run",
            "-d",
            "--name",
            name,
            "-e",
            f"MINIO_ROOT_USER={MINIO_ACCESS_KEY}",
            "-e",
            f"MINIO_ROOT_PASSWORD={MINIO_SECRET_KEY}",
            "-p",
            f"{port}:9000",
            "quay.io/minio/minio:latest",
            "server",
            "/data",
        )
        if run.returncode != 0:
            pytest.fail(f"docker run minio failed:\n{run.stdout}\n{run.stderr}")
        endpoint = f"http://127.0.0.1:{port}"
        _wait_for_http(f"{endpoint}/minio/health/live", timeout=30)
        yield {"name": name, "port": port, "endpoint": endpoint}
    finally:
        _run_docker("rm", "-f", name)


@pytest.fixture
def s3(minio: dict[str, Any]) -> S3Client:
    client = boto3.client(
        "s3",
        endpoint_url=minio["endpoint"],
        aws_access_key_id=MINIO_ACCESS_KEY,
        aws_secret_access_key=MINIO_SECRET_KEY,
        region_name="us-east-1",
    )
    client.create_bucket(Bucket=VEXA_BUCKET)
    client.create_bucket(Bucket=EXPORT_BUCKET)
    return client


@pytest.fixture
def seeded_s3(s3: S3Client, tmp_path: Path) -> S3Client:
    master_path = tmp_path / "master.webm"
    _make_sine_webm(master_path, AUDIO_DURATION_S)
    s3.put_object(
        Bucket=VEXA_BUCKET,
        Key=STORAGE_PATH,
        Body=master_path.read_bytes(),
        ContentType="video/webm",
    )
    tape_header = json.dumps(
        {
            "type": "captured_signal_header",
            "v": 1,
            "platform": "google_meet",
            "lane": "gmeet",
            "sample_rate": 16000,
            "started_at": RECORDING_CREATED_AT,
        }
    )
    s3.put_object(
        Bucket=VEXA_BUCKET,
        Key=TAPE_KEY,
        Body=(tape_header + "\n").encode(),
        ContentType="application/x-ndjson",
    )
    return s3


@pytest.fixture
def meeting_api_server() -> Iterator[dict[str, Any]]:
    port = _free_port()
    recording = {
        "id": RECORDING_ID,
        "meeting_id": VEXA_MEETING_ID,
        "created_at": RECORDING_CREATED_AT,
        "media_files": [{"type": "audio", "format": "webm"}],
    }
    app = create_meeting_api_app(recording, STORAGE_PATH)
    server = _UvicornThread(app, port)
    server.start()
    try:
        yield {"port": port}
    finally:
        server.stop()


@pytest.fixture
def notetaker_server() -> Iterator[dict[str, Any]]:
    port = _free_port()
    app = create_notetaker_app()
    server = _UvicornThread(app, port)
    server.start()
    try:
        yield {"port": port, "calls": app.state.calls}
    finally:
        server.stop()


@pytest.fixture
def exporter(
    built_image: str,
    suffix: str,
    minio: dict[str, Any],
    meeting_api_server: dict[str, Any],
    notetaker_server: dict[str, Any],
) -> Iterator[dict[str, Any]]:
    name = f"aw-exporter-it-exporter-{suffix}"
    port = _free_port()
    env = {
        "MEETING_API_URL": f"http://host.docker.internal:{meeting_api_server['port']}",
        "VEXA_WEBHOOK_SECRET": WEBHOOK_SECRET,
        "VEXA_BUCKET": VEXA_BUCKET,
        "EXPORT_BUCKET": EXPORT_BUCKET,
        "EXPORT_PREFIX": "recordings/",
        "NOTETAKER_URL": f"http://host.docker.internal:{notetaker_server['port']}",
        "EXPORT_SWEEP_SECONDS": "1",
        "AWS_ENDPOINT_URL_S3": f"http://host.docker.internal:{minio['port']}",
        "AWS_ACCESS_KEY_ID": MINIO_ACCESS_KEY,
        "AWS_SECRET_ACCESS_KEY": MINIO_SECRET_KEY,
        "AWS_REGION": "us-east-1",
    }
    args = [
        "run",
        "-d",
        "--name",
        name,
        "--add-host=host.docker.internal:host-gateway",
        "-p",
        f"{port}:8080",
    ]
    for key, value in env.items():
        args += ["-e", f"{key}={value}"]
    args.append(built_image)
    try:
        run = _run_docker(*args, timeout=60)
        if run.returncode != 0:
            pytest.fail(f"docker run exporter failed:\n{run.stdout}\n{run.stderr}")
        base_url = f"http://127.0.0.1:{port}"
        try:
            _wait_for_http(f"{base_url}/healthz", timeout=30)
        except TimeoutError:
            logs = _run_docker("logs", name).stdout
            pytest.fail(f"exporter container never became healthy:\n{logs}")
        yield {"name": name, "base_url": base_url}
    finally:
        _run_docker("rm", "-f", name)


def test_compose_flow_hands_off_meeting(
    exporter: dict[str, Any],
    seeded_s3: S3Client,
    notetaker_server: dict[str, Any],
) -> None:
    body = json.dumps(_envelope()).encode()
    headers = {**_sign(body, WEBHOOK_SECRET), "Content-Type": "application/json"}

    resp = httpx.post(
        f"{exporter['base_url']}/hooks/vexa", content=body, headers=headers, timeout=10
    )
    assert resp.status_code == 202, resp.text

    marker = _wait_for_export_marker(seeded_s3, timeout=60)
    assert marker["state"] == "handed_off"

    keys = _list_keys(seeded_s3, EXPORT_BUCKET, BASE)
    assert keys == EXPECTED_KEYS

    assert notetaker_server["calls"] == [
        {
            "meeting_id": f"vexa-{VEXA_MEETING_ID}",
            "s3_path": BASE,
            "platform": "google_meet",
            "idempotency_key": f"vexa-{VEXA_MEETING_ID}",
        }
    ]

    audio_bytes = seeded_s3.get_object(Bucket=EXPORT_BUCKET, Key=BASE + "audio.wav")[
        "Body"
    ].read()
    with wave.open(io.BytesIO(audio_bytes), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getframerate() == 16000
        duration = wav.getnframes() / wav.getframerate()
    assert abs(duration - AUDIO_DURATION_S) <= 0.2
