"""Unit tests for the zoom-bot aw_output_hook.py FastAPI sidecar.

Covers the format-aware behaviour that distinguishes it from meet-bot: raw-PCM
chunks (Zoom), the chunk_seq dedup guard (ignoring the legacy single-shot WAV
upload), and PCM assembly without ffmpeg.
"""

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
    "meeting_id": "zoom_evt_001",
    "platform": "zoom",
    "scheduled_start_at": "2026-08-20T10:00:00+00:00",
    "expected_duration_min": 60,
    "hard_deadline_at": "2026-08-20T14:00:00+00:00",
    "join": {
        "url": "https://app.zoom.us/wc/1234567890/join",
        "organizer_email": "host@example.com",
        "requires_admit": True,
    },
    "display_name": "AW Notetaker",
    "consent": {
        "state": "implicit_internal",
        "recorded_at": "2026-08-20T09:55:00+00:00",
        "by_user_id": "system",
    },
    "retry": {"attempts": 0, "max_attempts": 2},
    "s3_key": "recordings/zoom_evt_001_job001/",
    "live_streaming": {"enabled": False},
}
os.environ.setdefault("BOT_JOB_JSON", json.dumps(_SAMPLE_JOB))
os.environ.setdefault("CONNECTION_ID", "job001")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")

from fastapi.testclient import TestClient  # noqa: E402

import aw_output_hook as hook  # noqa: E402


@pytest.fixture(autouse=True)
def clear_state() -> None:
    hook._chunk_store.clear()
    hook._chunk_format.clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(hook.app)


def _pcm_meta(seq: int, is_final: bool = False, fmt: str = "pcm") -> str:
    return json.dumps({"chunk_seq": seq, "is_final": is_final, "format": fmt})


def test_healthz_returns_ok(client: TestClient) -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# --- chunk ingestion + format tracking --------------------------------------


def test_post_pcm_chunk_accumulates_bytes(client: TestClient) -> None:
    chunk_data = b"\x00\x01" * 200
    resp = client.post(
        "/chunks",
        files={
            "file": ("recording.0.pcm", BytesIO(chunk_data), "application/octet-stream")
        },
        data={"metadata": _pcm_meta(0)},
    )
    assert resp.status_code == 200
    assert hook._chunk_store["job001"] == [chunk_data]
    assert hook._chunk_format["job001"] == "pcm"


def test_post_multiple_chunks_all_stored(client: TestClient) -> None:
    for i in range(3):
        client.post(
            "/chunks",
            files={
                "file": (
                    f"rec.{i}.pcm",
                    BytesIO(b"\x00" * 100),
                    "application/octet-stream",
                )
            },
            data={"metadata": _pcm_meta(i, is_final=i == 2)},
        )
    assert len(hook._chunk_store.get("job001", [])) == 3


def test_chunkless_upload_is_ignored(client: TestClient) -> None:
    # The legacy single-shot RecordingService.upload() POSTs the whole WAV with
    # no chunk_seq. Accepting it would double the audio, so it must be ignored.
    resp = client.post(
        "/chunks",
        files={
            "file": ("recording.wav", BytesIO(b"RIFF...." + b"\x00" * 500), "audio/wav")
        },
        data={"metadata": json.dumps({"format": "wav"})},  # no chunk_seq
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored"}
    assert hook._chunk_store.get("job001", []) == []


def test_chunk_with_non_integer_seq_is_ignored(client: TestClient) -> None:
    resp = client.post(
        "/chunks",
        files={"file": ("rec.pcm", BytesIO(b"\x00" * 8), "application/octet-stream")},
        data={"metadata": json.dumps({"chunk_seq": "not-a-number", "format": "pcm"})},
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored"}
    assert hook._chunk_store.get("job001", []) == []


# --- callback → pipeline (identical contract to meet-bot) -------------------


def test_post_callback_triggers_pipeline(client: TestClient) -> None:
    payload = {
        "connection_id": "job001",
        "platform": "zoom",
        "status": "completed",
        "completion_reason": "left_alone",
    }
    with patch(
        "aw_output_hook._run_pipeline_and_signal", new_callable=AsyncMock
    ) as mock_pipeline:
        resp = client.post("/callback", json=payload)
    assert resp.status_code == 200
    mock_pipeline.assert_awaited_once()


def test_post_callback_joining_status_does_not_trigger_pipeline(
    client: TestClient, tmp_path: Path, monkeypatch: Any
) -> None:
    sentinel = tmp_path / "pipeline_done"
    monkeypatch.setattr(hook, "_PIPELINE_DONE_SENTINEL", sentinel)
    payload = {"connection_id": "job001", "status": "joining"}
    with patch(
        "aw_output_hook._run_pipeline_and_signal", new_callable=AsyncMock
    ) as mock_pipeline:
        resp = client.post("/callback", json=payload)
    assert resp.status_code == 200
    mock_pipeline.assert_not_called()
    assert not sentinel.exists()


def test_post_callback_maps_reason_from_both_fields(client: TestClient) -> None:
    cases = [
        ("completion_reason", "left_alone", "last_participant"),
        ("completion_reason", "timeout", "hard_deadline"),
        ("reason", "meeting_ended", "host_ended"),
        ("reason", "removed_by_admin", "host_ended"),
        ("reason", "left_alone_timeout", "last_participant"),
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


# --- format-aware S3 keys / content-type ------------------------------------


def test_chunk_s3_key_pcm_extension_zero_padded() -> None:
    key = hook._chunk_s3_key(5, "pcm")
    assert key == f"{hook._JOB.s3_key}audio_chunks/chunk_000005.pcm"


def test_chunk_s3_key_webm_extension() -> None:
    assert hook._chunk_s3_key(0, "webm").endswith("audio_chunks/chunk_000000.webm")


def test_content_type_for_pcm_is_l16() -> None:
    assert hook._content_type_for("pcm") == "audio/L16;rate=16000;channels=1"
    assert hook._content_type_for("s16le") == "audio/L16;rate=16000;channels=1"


def test_content_type_for_webm() -> None:
    assert hook._content_type_for("webm") == "audio/webm"


# --- durability: incremental persistence to S3 ------------------------------


@pytest.fixture(autouse=True)
def hermetic_s3() -> Any:
    hook._s3_client = None
    fake_instance = MagicMock()
    with patch("aw_output_hook.S3Client", return_value=fake_instance):
        yield fake_instance
    hook._s3_client = None


def test_persist_chunk_to_s3_puts_object_with_pcm_metadata(hermetic_s3: Any) -> None:
    hook._persist_chunk_to_s3(7, b"audio-bytes", "pcm")
    hermetic_s3.put_object.assert_called_once()
    kwargs = hermetic_s3.put_object.call_args.kwargs
    assert kwargs["key"] == hook._chunk_s3_key(7, "pcm")
    assert kwargs["body"] == b"audio-bytes"
    assert kwargs["content_type"] == "audio/L16;rate=16000;channels=1"


def test_persist_chunk_to_s3_non_fatal_on_error(hermetic_s3: Any) -> None:
    hermetic_s3.put_object.side_effect = RuntimeError("s3 down")
    hook._persist_chunk_to_s3(1, b"x", "pcm")  # must NOT raise


def test_post_chunk_persists_to_s3(client: TestClient, hermetic_s3: Any) -> None:
    resp = client.post(
        "/chunks",
        files={
            "file": ("rec.3.pcm", BytesIO(b"\x00" * 128), "application/octet-stream")
        },
        data={"metadata": _pcm_meta(3)},
    )
    assert resp.status_code == 200
    hermetic_s3.put_object.assert_called_once()
    assert hermetic_s3.put_object.call_args.kwargs["key"].endswith("chunk_000003.pcm")


def test_empty_final_chunk_stored_but_not_persisted(
    client: TestClient, hermetic_s3: Any
) -> None:
    # The final flush may carry zero audio bytes (is_final marker). It should be
    # accepted (kept in-order) but not written to S3 as an empty object.
    resp = client.post(
        "/chunks",
        files={"file": ("rec.final.pcm", BytesIO(b""), "application/octet-stream")},
        data={"metadata": _pcm_meta(9, is_final=True)},
    )
    assert resp.status_code == 200
    assert hook._chunk_store["job001"] == [b""]
    hermetic_s3.put_object.assert_not_called()


def test_post_chunk_still_ok_when_s3_fails(
    client: TestClient, hermetic_s3: Any
) -> None:
    hermetic_s3.put_object.side_effect = RuntimeError("s3 down")
    resp = client.post(
        "/chunks",
        files={
            "file": ("rec.0.pcm", BytesIO(b"\x00" * 64), "application/octet-stream")
        },
        data={"metadata": _pcm_meta(0)},
    )
    assert resp.status_code == 200
    assert len(hook._chunk_store.get("job001", [])) == 1


# --- assembly: PCM concatenates raw, WebM transcodes via ffmpeg -------------


def test_assemble_audio_pcm_concatenates_raw_without_ffmpeg() -> None:
    import asyncio

    chunks = [b"\x01\x02" * 10, b"\x03\x04" * 10]
    with patch("aw_output_hook._convert_container_to_pcm") as mock_ffmpeg:
        path = asyncio.run(hook._assemble_audio(chunks, "pcm"))
    mock_ffmpeg.assert_not_called()  # PCM needs no transcode
    assert path.read_bytes() == b"".join(chunks)
    path.unlink(missing_ok=True)


def test_assemble_audio_webm_uses_ffmpeg(tmp_path: Path) -> None:
    import asyncio

    fake_out = tmp_path / "out.raw"
    fake_out.write_bytes(b"\x00" * 16)
    with patch(
        "aw_output_hook._convert_container_to_pcm",
        new_callable=AsyncMock,
        return_value=fake_out,
    ) as mock_ffmpeg:
        path = asyncio.run(hook._assemble_audio([b"webm-bytes"], "webm"))
    mock_ffmpeg.assert_awaited_once()
    assert path == fake_out


def test_run_pipeline_assembles_pcm_and_calls_run_from_redis(tmp_path: Path) -> None:
    import asyncio

    hook._chunk_store["job001"] = [b"\x00" * 512, b"\x01" * 512]
    hook._chunk_format["job001"] = "pcm"
    fake_pcm = tmp_path / "audio.raw"
    fake_pcm.write_bytes(b"\x00\x01" * 1600)

    with (
        patch(
            "aw_output_hook._assemble_audio",
            new_callable=AsyncMock,
            return_value=fake_pcm,
        ) as mock_asm,
        patch("aw_output_hook.run_from_redis", new_callable=AsyncMock) as mock_rfr,
    ):
        asyncio.run(hook._run_pipeline(bot_left_reason="last_participant"))

    assert mock_asm.call_args.args[1] == "pcm"  # format threaded through
    mock_rfr.assert_awaited_once()
    call_kwargs = mock_rfr.call_args.kwargs
    assert call_kwargs["bot_left_reason"] == "last_participant"
    assert call_kwargs["session_uid"] == "job001"
    assert call_kwargs["audio_raw_path"] == fake_pcm


def test_run_pipeline_skips_when_no_chunks(tmp_path: Path) -> None:
    import asyncio

    with patch("aw_output_hook.run_from_redis", new_callable=AsyncMock) as mock_rfr:
        asyncio.run(hook._run_pipeline(bot_left_reason="host_ended"))
    mock_rfr.assert_not_awaited()


# --- end-of-meeting sentinel -------------------------------------------------


def test_run_pipeline_and_signal_writes_sentinel(
    tmp_path: Path, monkeypatch: Any
) -> None:
    import asyncio

    sentinel = tmp_path / "pipeline_done"
    monkeypatch.setattr(hook, "_PIPELINE_DONE_SENTINEL", sentinel)
    with patch("aw_output_hook._run_pipeline", new_callable=AsyncMock):
        asyncio.run(hook._run_pipeline_and_signal(bot_left_reason="host_ended"))
    assert sentinel.exists()


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
    assert sentinel.exists()
