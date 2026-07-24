"""Unit tests for aw_output_hook.py FastAPI sidecar."""

from __future__ import annotations

import json
import os
from io import BytesIO
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_SAMPLE_JOB = {
    "job_id": "job001",
    "meeting_id": "meet_evt_001",
    "platform": "meet",
    "scheduled_start_at": "2026-07-10T10:00:00+00:00",
    "expected_duration_min": 60,
    "hard_deadline_at": "2026-07-10T14:00:00+00:00",
    "join": {
        "url": "https://meet.google.com/abc-def-ghi",
        "organizer_email": "host@example.com",
        "requires_admit": False,
    },
    "display_name": "AW Notetaker",
    "consent": {
        "state": "implicit_internal",
        "recorded_at": "2026-07-10T09:55:00+00:00",
        "by_user_id": "system",
    },
    "retry": {"attempts": 0, "max_attempts": 2},
    "s3_key": "recordings/meet_evt_001_job001/",
    "live_streaming": {"enabled": False},
}
os.environ.setdefault("BOT_JOB_JSON", json.dumps(_SAMPLE_JOB))
os.environ.setdefault("CONNECTION_ID", "job001")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")

from fastapi.testclient import TestClient  # noqa: E402

import aw_output_hook as hook  # noqa: E402


@pytest.fixture(autouse=True)
def clear_chunk_store() -> None:
    hook._chunk_store.clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(hook.app)


def test_healthz_returns_ok(client: TestClient) -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_post_chunk_accumulates_bytes(client: TestClient) -> None:
    chunk_data = b"\x00\x01" * 200
    metadata = json.dumps({"chunk_seq": 0, "is_final": False})
    resp = client.post(
        "/chunks",
        files={"file": ("recording.0.webm", BytesIO(chunk_data), "video/webm")},
        data={"metadata": metadata},
    )
    assert resp.status_code == 200
    assert len(hook._chunk_store.get("job001", [])) == 1
    assert hook._chunk_store["job001"][0] == chunk_data


def test_post_multiple_chunks_all_stored(client: TestClient) -> None:
    for i in range(3):
        client.post(
            "/chunks",
            files={"file": (f"rec.{i}.webm", BytesIO(b"\x00" * 100), "video/webm")},
            data={"metadata": json.dumps({"chunk_seq": i, "is_final": i == 2})},
        )
    assert len(hook._chunk_store.get("job001", [])) == 3


def test_post_callback_triggers_pipeline(client: TestClient) -> None:
    payload = {
        "connection_id": "job001",
        "meeting_id": "42",
        "platform": "google_meet",
        "status": "completed",
        "completion_reason": "left_alone",
        "session_start": "2026-07-10T10:00:00Z",
        "session_end": "2026-07-10T11:00:00Z",
    }
    with patch(
        "aw_output_hook._run_pipeline_and_signal", new_callable=AsyncMock
    ) as mock_pipeline:
        resp = client.post("/callback", json=payload)
    assert resp.status_code == 200
    mock_pipeline.assert_awaited_once()


def test_post_callback_maps_reason_from_both_fields(client: TestClient) -> None:
    # (payload field, value, expected bot_left_reason). Vexa sends `reason` on
    # abrupt ends; `completion_reason` only in some flows — both must map.
    cases = [
        ("completion_reason", "left_alone", "last_participant"),
        ("completion_reason", "timeout", "hard_deadline"),
        ("completion_reason", "error", "error"),
        ("reason", "meeting_ended", "host_ended"),
        ("reason", "removed_by_admin", "host_ended"),
        ("reason", "normal_completion", "host_ended"),
        ("reason", "left_alone_timeout", "last_participant"),
        ("reason", "post_join_setup_error", "error"),
        ("reason", "totally_unknown", "host_ended"),
    ]
    for field, value, expected in cases:
        payload: dict[str, Any] = {
            "connection_id": "job001",
            "status": "completed",
            field: value,
        }
        with patch(
            "aw_output_hook._run_pipeline_and_signal", new_callable=AsyncMock
        ) as mock_pipeline:
            client.post("/callback", json=payload)
        assert (
            mock_pipeline.call_args.kwargs["bot_left_reason"] == expected
        ), f"{field}={value!r} → expected {expected!r}"


def test_run_pipeline_calls_run_from_redis(tmp_path: Path) -> None:
    hook._chunk_store["job001"] = [b"\x00" * 512, b"\x01" * 512]
    fake_pcm = tmp_path / "audio.raw"
    fake_pcm.write_bytes(b"\x00\x01" * 1600)

    import asyncio

    with (
        patch("aw_output_hook._convert_webm_to_pcm", return_value=fake_pcm),
        patch("aw_output_hook.run_from_redis", new_callable=AsyncMock) as mock_rfr,
    ):
        asyncio.run(hook._run_pipeline(bot_left_reason="last_participant"))

    mock_rfr.assert_awaited_once()
    call_kwargs = mock_rfr.call_args.kwargs
    assert call_kwargs["bot_left_reason"] == "last_participant"
    assert call_kwargs["session_uid"] == "job001"
    assert call_kwargs["audio_raw_path"] == fake_pcm


# --- §3 durability: incremental chunk persistence to S3 ---------------------


@pytest.fixture(autouse=True)
def hermetic_s3() -> Any:
    """Patch S3Client so no test touches real AWS; expose the fake instance."""
    hook._s3_client = None
    fake_instance = MagicMock()
    with patch("aw_output_hook.S3Client", return_value=fake_instance):
        yield fake_instance
    hook._s3_client = None


def test_chunk_s3_key_zero_padded_under_job_prefix() -> None:
    assert hook._chunk_s3_key(5) == f"{hook._JOB.s3_key}audio_chunks/chunk_000005.webm"
    assert hook._chunk_s3_key(0).endswith("audio_chunks/chunk_000000.webm")


def test_persist_chunk_to_s3_puts_object(hermetic_s3: Any) -> None:
    hook._persist_chunk_to_s3(7, b"audio-bytes")
    hermetic_s3.put_object.assert_called_once()
    kwargs = hermetic_s3.put_object.call_args.kwargs
    assert kwargs["key"] == hook._chunk_s3_key(7)
    assert kwargs["body"] == b"audio-bytes"
    assert kwargs["content_type"] == "audio/webm"


def test_persist_chunk_to_s3_reuses_client(hermetic_s3: Any) -> None:
    hook._persist_chunk_to_s3(0, b"a")
    hook._persist_chunk_to_s3(1, b"b")
    assert hermetic_s3.put_object.call_count == 2  # same cached client


def test_persist_chunk_to_s3_non_fatal_on_error(hermetic_s3: Any) -> None:
    hermetic_s3.put_object.side_effect = RuntimeError("s3 down")
    hook._persist_chunk_to_s3(1, b"x")  # must NOT raise — capture continues


def test_post_chunk_persists_to_s3(client: TestClient, hermetic_s3: Any) -> None:
    resp = client.post(
        "/chunks",
        files={"file": ("rec.3.webm", BytesIO(b"\x00" * 128), "video/webm")},
        data={"metadata": json.dumps({"chunk_seq": 3, "is_final": False})},
    )
    assert resp.status_code == 200
    hermetic_s3.put_object.assert_called_once()
    assert hermetic_s3.put_object.call_args.kwargs["key"].endswith("chunk_000003.webm")


def test_post_chunk_still_ok_when_s3_fails(
    client: TestClient, hermetic_s3: Any
) -> None:
    hermetic_s3.put_object.side_effect = RuntimeError("s3 down")
    resp = client.post(
        "/chunks",
        files={"file": ("rec.0.webm", BytesIO(b"\x00" * 64), "video/webm")},
        data={"metadata": json.dumps({"chunk_seq": 0, "is_final": False})},
    )
    assert resp.status_code == 200  # capture unaffected by S3 failure
    assert len(hook._chunk_store.get("job001", [])) == 1  # still buffered


# --- §2 end-of-meeting: pipeline-done sentinel ------------------------------


def test_run_pipeline_and_signal_writes_sentinel(
    tmp_path: Path, monkeypatch: Any
) -> None:
    import asyncio

    sentinel = tmp_path / "pipeline_done"
    monkeypatch.setattr(hook, "_PIPELINE_DONE_SENTINEL", sentinel)
    with patch("aw_output_hook._run_pipeline", new_callable=AsyncMock):
        asyncio.run(hook._run_pipeline_and_signal(bot_left_reason="host_ended"))
    assert sentinel.exists()  # start.sh waits on this before teardown


def test_run_pipeline_and_signal_writes_sentinel_even_on_error(
    tmp_path: Path, monkeypatch: Any
) -> None:
    import asyncio

    sentinel = tmp_path / "pipeline_done"
    monkeypatch.setattr(hook, "_PIPELINE_DONE_SENTINEL", sentinel)
    with patch(
        "aw_output_hook._run_pipeline",
        new_callable=AsyncMock,
        side_effect=RuntimeError("boom"),
    ):
        asyncio.run(hook._run_pipeline_and_signal(bot_left_reason="host_ended"))
    assert sentinel.exists()  # sentinel written even when the pipeline fails
