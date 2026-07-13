"""Unit tests for aw_output_hook.py FastAPI sidecar."""

from __future__ import annotations

import json
import os
from io import BytesIO
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

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
    with patch("aw_output_hook._run_pipeline", new_callable=AsyncMock) as mock_pipeline:
        resp = client.post("/callback", json=payload)
    assert resp.status_code == 200
    mock_pipeline.assert_awaited_once()


def test_post_callback_maps_completion_reason(client: TestClient) -> None:
    cases = [
        ("left_alone", "last_participant"),
        ("timeout", "hard_deadline"),
        ("error", "error"),
        ("host_ended", "host_ended"),
        ("anything_else", "host_ended"),
    ]
    for completion_reason, expected_reason in cases:
        payload: dict[str, Any] = {
            "connection_id": "job001",
            "meeting_id": "42",
            "platform": "google_meet",
            "status": "completed",
            "completion_reason": completion_reason,
            "session_start": "2026-07-10T10:00:00Z",
            "session_end": "2026-07-10T11:00:00Z",
        }
        with patch(
            "aw_output_hook._run_pipeline", new_callable=AsyncMock
        ) as mock_pipeline:
            client.post("/callback", json=payload)
        call_kwargs = mock_pipeline.call_args.kwargs
        assert (
            call_kwargs["bot_left_reason"] == expected_reason
        ), f"completion_reason={completion_reason!r} → expected {expected_reason!r}"


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
