"""Unit tests for aw_output_hook.py FastAPI sidecar."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import wave
from datetime import datetime, timedelta, timezone
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
    hook._full_session_store.clear()
    hook._full_session_meta.clear()
    hook._first_upload_at.clear()


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


def test_post_callback_joining_status_does_not_trigger_pipeline(
    client: TestClient, tmp_path: Path, monkeypatch: Any
) -> None:
    # The "joining" callback fires at bot-join time, long before the meeting
    # ends. It must NOT schedule the end-of-session pipeline (which would
    # write the sentinel and cause start.sh to tear the sidecar down early —
    # the live-confirmed bug: S3 only got audio_chunks/, no audio.wav etc.).
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


def test_post_callback_completed_status_triggers_pipeline(
    client: TestClient, tmp_path: Path, monkeypatch: Any
) -> None:
    # A terminal ("completed") callback must still schedule the end pipeline.
    sentinel = tmp_path / "pipeline_done"
    monkeypatch.setattr(hook, "_PIPELINE_DONE_SENTINEL", sentinel)
    payload = {
        "connection_id": "job001",
        "status": "completed",
        "reason": "meeting_ended",
    }
    with patch(
        "aw_output_hook._run_pipeline_and_signal", new_callable=AsyncMock
    ) as mock_pipeline:
        resp = client.post("/callback", json=payload)
    assert resp.status_code == 200
    mock_pipeline.assert_awaited_once()
    assert mock_pipeline.call_args.kwargs["bot_left_reason"] == "host_ended"


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


# --- full-session recording vs. backup chunks (doubled-audio bug) -----------
#
# Every Meet transcript contained the meeting TWICE: the /chunks handler
# appended EVERY upload to _chunk_store before parsing the metadata, so the
# browser's whole-session blob (no chunk_seq) was concatenated on top of the
# 30 s backup chunks. None of the tests above could catch it — they all post
# WITH a chunk_seq. These do not.


def _wav_bytes(
    pcm: bytes, *, sample_rate: int = 16000, channels: int = 1, sampwidth: int = 2
) -> bytes:
    buf = BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sampwidth)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buf.getvalue()


def _post_full_session(
    client: TestClient,
    pcm: bytes,
    *,
    duration_seconds: float | None = None,
    sample_rate: int = 16000,
    channels: int = 1,
    sampwidth: int = 2,
) -> Any:
    """POST the whole-session recording: format=wav, NO chunk_seq."""
    payload = _wav_bytes(
        pcm, sample_rate=sample_rate, channels=channels, sampwidth=sampwidth
    )
    meta: dict[str, Any] = {
        "format": "wav",
        "sample_rate": sample_rate,
        "channels": channels,
        "file_size_bytes": len(payload),
    }
    if duration_seconds is None:
        duration_seconds = len(pcm) / (sample_rate * channels * sampwidth)
    meta["duration_seconds"] = duration_seconds
    return client.post(
        "/chunks",
        files={"file": ("full_session.wav", BytesIO(payload), "audio/wav")},
        data={"metadata": json.dumps(meta)},
    )


def test_full_session_upload_never_enters_chunk_store(
    client: TestClient, hermetic_s3: Any
) -> None:
    resp = _post_full_session(client, b"\x11\x22" * 16000)
    assert resp.status_code == 200
    # THE bug: this upload used to become _chunk_store[-1] and chunk_NNNNNN.webm.
    assert hook._chunk_store.get("job001", []) == []
    assert hook._full_session_store["job001"]
    keys = [c.kwargs["key"] for c in hermetic_s3.put_object.call_args_list]
    assert not any("audio_chunks/" in k for k in keys), keys


def test_full_session_upload_persisted_outside_audio_chunks(
    client: TestClient, hermetic_s3: Any
) -> None:
    _post_full_session(client, b"\x00\x01" * 8000)
    hermetic_s3.put_object.assert_called_once()
    kwargs = hermetic_s3.put_object.call_args.kwargs
    assert kwargs["key"] == f"{hook._JOB.s3_key}full_session.wav"
    assert kwargs["content_type"] == "audio/wav"


def test_empty_full_session_upload_ignored(
    client: TestClient, hermetic_s3: Any
) -> None:
    resp = client.post(  # zero-byte body, as the finaliser sometimes sends
        "/chunks",
        files={"file": ("full_session.wav", BytesIO(b""), "audio/wav")},
        data={"metadata": json.dumps({"format": "wav", "duration_seconds": 0})},
    )
    assert resp.json() == {"status": "ignored"}
    assert "job001" not in hook._full_session_store
    hermetic_s3.put_object.assert_not_called()


def test_header_only_full_session_falls_back_to_chunks(
    client: TestClient, hermetic_s3: Any, tmp_path: Path
) -> None:
    # A WAV carrying only a RIFF header has no audio; handing an empty PCM file
    # downstream would blow up in encode_wav(). Fall back to the backup chunks.
    client.post(
        "/chunks",
        files={"file": ("rec.0.webm", BytesIO(b"\x00" * 100), "video/webm")},
        data={"metadata": json.dumps({"chunk_seq": 0, "is_final": True})},
    )
    _post_full_session(client, b"")
    fake_pcm = tmp_path / "audio.raw"
    fake_pcm.write_bytes(b"\x07" * 64)

    with (
        patch(
            "aw_output_hook._convert_webm_to_pcm",
            new_callable=AsyncMock,
            return_value=fake_pcm,
        ) as mock_ff,
        patch("aw_output_hook.run_from_redis", new_callable=AsyncMock) as mock_rfr,
    ):
        asyncio.run(hook._run_pipeline(bot_left_reason="host_ended"))

    mock_ff.assert_awaited_once()
    assert mock_rfr.call_args.kwargs["audio_raw_path"] == fake_pcm


def test_full_session_is_the_transcription_source(
    client: TestClient, hermetic_s3: Any
) -> None:
    pcm = bytes(range(256)) * 125  # 32000 bytes == 1.0 s
    _post_full_session(client, pcm)

    with (
        patch("aw_output_hook._convert_webm_to_pcm", new_callable=AsyncMock) as mock_ff,
        patch("aw_output_hook.run_from_redis", new_callable=AsyncMock) as mock_rfr,
    ):
        asyncio.run(hook._run_pipeline(bot_left_reason="host_ended"))

    mock_ff.assert_not_awaited()  # no ffmpeg on the primary path
    audio_path = mock_rfr.call_args.kwargs["audio_raw_path"]
    assert audio_path.read_bytes() == pcm  # RIFF header stripped, PCM intact


def test_transcribed_bytes_are_full_session_not_the_sum(
    client: TestClient, hermetic_s3: Any
) -> None:
    # The regression pin: N backup chunks PLUS a full-session upload must hand
    # downstream exactly the full-session audio — not chunks + full session.
    for i in range(3):
        client.post(
            "/chunks",
            files={"file": (f"rec.{i}.webm", BytesIO(b"\xaa" * 5000), "video/webm")},
            data={"metadata": json.dumps({"chunk_seq": i, "is_final": i == 2})},
        )
    pcm = b"\x01\x02" * 16000  # 32000 bytes == 1.0 s
    _post_full_session(client, pcm)

    with (
        patch("aw_output_hook._convert_webm_to_pcm", new_callable=AsyncMock),
        patch("aw_output_hook.run_from_redis", new_callable=AsyncMock) as mock_rfr,
    ):
        asyncio.run(hook._run_pipeline(bot_left_reason="host_ended"))

    sent = mock_rfr.call_args.kwargs["audio_raw_path"].read_bytes()
    assert len(sent) == len(pcm)
    assert len(sent) != len(pcm) + 15000  # the doubled-audio shape
    assert len(hook._chunk_store["job001"]) == 3  # backups still buffered


def test_falls_back_to_chunks_and_warns_when_full_session_absent(
    client: TestClient, hermetic_s3: Any, tmp_path: Path, caplog: Any
) -> None:
    for i in range(2):
        client.post(
            "/chunks",
            files={"file": (f"rec.{i}.webm", BytesIO(b"\x00" * 100), "video/webm")},
            data={"metadata": json.dumps({"chunk_seq": i, "is_final": i == 1})},
        )
    fake_pcm = tmp_path / "audio.raw"
    fake_pcm.write_bytes(b"\x07" * 64)

    with (
        caplog.at_level(logging.WARNING, logger="aw_output_hook"),
        patch(
            "aw_output_hook._convert_webm_to_pcm",
            new_callable=AsyncMock,
            return_value=fake_pcm,
        ) as mock_ff,
        patch("aw_output_hook.run_from_redis", new_callable=AsyncMock) as mock_rfr,
    ):
        asyncio.run(hook._run_pipeline(bot_left_reason="host_ended"))

    mock_ff.assert_awaited_once()
    assert mock_rfr.call_args.kwargs["audio_raw_path"] == fake_pcm
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any(
        "never arrived" in m and "backup" in m for m in warnings
    ), warnings  # operators must be told the transcript came from backups


def test_unusable_full_session_falls_back_to_chunks(
    client: TestClient, hermetic_s3: Any, tmp_path: Path, caplog: Any
) -> None:
    client.post(
        "/chunks",
        files={"file": ("rec.0.webm", BytesIO(b"\x00" * 100), "video/webm")},
        data={"metadata": json.dumps({"chunk_seq": 0, "is_final": True})},
    )
    client.post(  # not a RIFF container at all
        "/chunks",
        files={"file": ("full_session.wav", BytesIO(b"not-a-wav"), "audio/wav")},
        data={"metadata": json.dumps({"format": "wav", "duration_seconds": 1.0})},
    )
    fake_pcm = tmp_path / "audio.raw"
    fake_pcm.write_bytes(b"\x07" * 64)

    with (
        caplog.at_level(logging.ERROR, logger="aw_output_hook"),
        patch(
            "aw_output_hook._convert_webm_to_pcm",
            new_callable=AsyncMock,
            return_value=fake_pcm,
        ) as mock_ff,
        patch("aw_output_hook.run_from_redis", new_callable=AsyncMock) as mock_rfr,
    ):
        asyncio.run(hook._run_pipeline(bot_left_reason="host_ended"))

    mock_ff.assert_awaited_once()
    assert mock_rfr.call_args.kwargs["audio_raw_path"] == fake_pcm
    assert any("unusable" in r.getMessage() for r in caplog.records)


def test_no_audio_at_all_skips_pipeline(client: TestClient) -> None:
    with patch("aw_output_hook.run_from_redis", new_callable=AsyncMock) as mock_rfr:
        asyncio.run(hook._run_pipeline(bot_left_reason="host_ended"))
    mock_rfr.assert_not_awaited()


@pytest.mark.parametrize(
    ("sample_rate", "channels"),
    [(48000, 1), (16000, 2)],
)
def test_wrong_wav_format_logs_loudly(
    client: TestClient,
    hermetic_s3: Any,
    caplog: Any,
    sample_rate: int,
    channels: int,
) -> None:
    pcm = b"\x00\x01\x02\x03" * 8000
    _post_full_session(client, pcm, sample_rate=sample_rate, channels=channels)

    with (
        caplog.at_level(logging.ERROR, logger="aw_output_hook"),
        patch("aw_output_hook.run_from_redis", new_callable=AsyncMock),
    ):
        asyncio.run(hook._run_pipeline(bot_left_reason="host_ended"))

    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("NOT s16le/16kHz/mono" in m for m in errors), errors


def test_truncated_payload_warns(
    client: TestClient, hermetic_s3: Any, caplog: Any
) -> None:
    # Only 0.5 s of the declared 1.0 s arrived -> a truncated transfer. This is
    # ALL the payload check can catch: recording.ts derives the WAV data size and
    # duration_seconds from the same totalSamples counter, so a doubled counter
    # doubles both in lockstep and drifts by zero. See test_wall_clock_* below
    # for the check that actually catches doubling.
    _post_full_session(client, b"\x01\x02" * 8000, duration_seconds=1.0)

    with (
        caplog.at_level(logging.WARNING, logger="aw_output_hook"),
        patch("aw_output_hook.run_from_redis", new_callable=AsyncMock),
    ):
        asyncio.run(hook._run_pipeline(bot_left_reason="host_ended"))

    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("payload mismatch" in m for m in warnings), warnings


def test_matching_payload_does_not_warn(
    client: TestClient, hermetic_s3: Any, caplog: Any
) -> None:
    pcm = b"\x01\x02" * 16000  # exactly 1.0 s
    _post_full_session(client, pcm, duration_seconds=1.0)

    with (
        caplog.at_level(logging.WARNING, logger="aw_output_hook"),
        patch("aw_output_hook.run_from_redis", new_callable=AsyncMock),
    ):
        asyncio.run(hook._run_pipeline(bot_left_reason="host_ended"))

    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert not any("payload mismatch" in m for m in warnings), warnings


def test_payload_check_cannot_see_doubling(client: TestClient) -> None:
    # Pin the reviewer's algebra so nobody restores the vacuous check believing
    # it guards doubling: recording.ts:98 writes dataSize = totalSamples * 2 and
    # :128 reports duration = totalSamples / sampleRate. Feed a DOUBLED counter
    # through both formulas and the drift is exactly zero.
    sample_rate = 16000
    for total_samples in (16000, 32000):  # honest, then doubled
        data_size = total_samples * 2
        reported = total_samples / sample_rate
        decoded = data_size / (sample_rate * 2)
        assert decoded == reported


# --- C1: a correct-looking WAV with no sound in it -------------------------
#
# 8000 / -8000 alternating, little-endian int16: unmistakably not silence.
_TONE = b"\x40\x1f\xc0\xe0" * 8000  # 32000 bytes == 1.0 s


def test_digital_silence_logs_error_naming_pulse_sink(
    client: TestClient, hermetic_s3: Any, caplog: Any, monkeypatch: Any
) -> None:
    # 1.0 s of pure zeros: 200 ok, right duration, right format, empty
    # transcript. A renamed sink or a pactl failure swallowed by start.sh's
    # `|| true` lands here indistinguishable from success.
    monkeypatch.setenv("PULSE_SINK", "meet_sink")
    _post_full_session(client, b"\x00" * 32000)

    with (
        caplog.at_level(logging.ERROR, logger="aw_output_hook"),
        patch("aw_output_hook.run_from_redis", new_callable=AsyncMock) as mock_rfr,
    ):
        asyncio.run(hook._run_pipeline(bot_left_reason="host_ended"))

    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("DIGITAL SILENCE" in m for m in errors), errors
    assert any("meet_sink" in m for m in errors), errors  # says where to look
    mock_rfr.assert_awaited_once()  # loud, but never fatal


def test_near_silence_logs_error(
    client: TestClient, hermetic_s3: Any, caplog: Any
) -> None:
    # A few LSBs of noise, not literal zeros — the misrouted-sink shape.
    _post_full_session(client, b"\x03\x00\xfd\xff" * 8000)

    with (
        caplog.at_level(logging.ERROR, logger="aw_output_hook"),
        patch("aw_output_hook.run_from_redis", new_callable=AsyncMock),
    ):
        asyncio.run(hook._run_pipeline(bot_left_reason="host_ended"))

    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("DIGITAL SILENCE" in m for m in errors), errors


def test_audible_audio_logs_no_silence_error(
    client: TestClient, hermetic_s3: Any, caplog: Any
) -> None:
    _post_full_session(client, _TONE)

    with (
        caplog.at_level(logging.ERROR, logger="aw_output_hook"),
        patch("aw_output_hook.run_from_redis", new_callable=AsyncMock),
    ):
        asyncio.run(hook._run_pipeline(bot_left_reason="host_ended"))

    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert not any("DIGITAL SILENCE" in m for m in errors), errors


def test_measure_level_scans_the_whole_buffer() -> None:
    # 10 s of silence then 10 s of tone. A scan that only reads the start of the
    # buffer would report DIGITAL SILENCE for a session that has audio in it.
    peak, _ = hook._measure_level(b"\x00" * 320000 + _TONE * 10)
    assert peak == 8000


def test_measure_level_reads_peak_and_rms() -> None:
    assert hook._measure_level(b"") == (0, 0.0)
    assert hook._measure_level(b"\x00" * 32000) == (0, 0.0)
    peak, rms = hook._measure_level(_TONE)
    assert peak == 8000
    assert rms == pytest.approx(8000, rel=0.01)


# --- C2: the doubling guard that can actually fire -------------------------


def test_wall_clock_overrun_warns(
    client: TestClient, hermetic_s3: Any, caplog: Any
) -> None:
    # 120 s of audio delivered within the same second the upload arrived: more
    # audio than time has passed, which a real-time capture cannot produce.
    _post_full_session(client, _TONE * 120)

    with (
        caplog.at_level(logging.WARNING, logger="aw_output_hook"),
        patch("aw_output_hook.run_from_redis", new_callable=AsyncMock),
    ):
        asyncio.run(hook._run_pipeline(bot_left_reason="host_ended"))

    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("outruns the wall clock" in m for m in warnings), warnings


def test_wall_clock_within_budget_does_not_warn(
    client: TestClient, hermetic_s3: Any, caplog: Any
) -> None:
    _post_full_session(client, _TONE * 120)
    # As if the first upload arrived an hour ago: 120 s of audio in 1 h is fine.
    hook._first_upload_at["job001"] = datetime.now(tz=timezone.utc) - timedelta(hours=1)

    with (
        caplog.at_level(logging.WARNING, logger="aw_output_hook"),
        patch("aw_output_hook.run_from_redis", new_callable=AsyncMock),
    ):
        asyncio.run(hook._run_pipeline(bot_left_reason="host_ended"))

    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert not any("outruns the wall clock" in m for m in warnings), warnings


def test_wall_clock_grace_absorbs_a_short_meeting(caplog: Any) -> None:
    # The first upload lands up to one 30 s chunk period AFTER capture starts,
    # so a short session legitimately shows more audio than measured span.
    now = datetime.now(tz=timezone.utc)
    hook._first_upload_at["job001"] = now - timedelta(seconds=90)
    with caplog.at_level(logging.WARNING, logger="aw_output_hook"):
        hook._warn_on_wall_clock_overrun(
            pcm_bytes=int(120 * 32000),  # 120 s of audio in a 90 s span
            bytes_per_second=32000,
            session_end=now,
        )
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_wall_clock_check_is_independent_of_reported_duration(caplog: Any) -> None:
    # The point of the rewrite: this guard reads the clock, never the bot's own
    # sample counter, so a counter that doubles in lockstep cannot hide from it.
    now = datetime.now(tz=timezone.utc)
    hook._first_upload_at["job001"] = now - timedelta(seconds=1800)
    with caplog.at_level(logging.WARNING, logger="aw_output_hook"):
        hook._warn_on_wall_clock_overrun(
            pcm_bytes=int(3600 * 32000),  # a 30 min meeting, recorded twice
            bytes_per_second=32000,
            session_end=now,
        )
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("2.00x" in m for m in warnings), warnings


# --- W3: the full-session buffer must not outlive the temp file ------------


def test_full_session_buffer_released_after_pipeline(
    client: TestClient, hermetic_s3: Any
) -> None:
    _post_full_session(client, _TONE)
    assert hook._full_session_store["job001"]

    with patch("aw_output_hook.run_from_redis", new_callable=AsyncMock):
        asyncio.run(hook._run_pipeline(bot_left_reason="host_ended"))

    # ~460 MB on a 4h session, against a 6Gi pod shared with Chromium + Node.
    assert "job001" not in hook._full_session_store
    assert "job001" not in hook._full_session_meta


# --- chunk-less uploads of ANY format stay out of the chunk store ----------


def test_chunkless_non_wav_upload_never_enters_chunk_store(
    client: TestClient, hermetic_s3: Any, caplog: Any
) -> None:
    # MS Teams still carries the legacy blob path and will post exactly this
    # shape. Branching on the format instead of on chunk_seq would drop it
    # straight back into _chunk_store — the original bug, verbatim.
    with caplog.at_level(logging.ERROR, logger="aw_output_hook"):
        resp = client.post(
            "/chunks",
            files={
                "file": ("blob.webm", BytesIO(b"\x1aE\xdf\xa3" * 100), "video/webm")
            },
            data={"metadata": json.dumps({"format": "webm"})},  # no chunk_seq
        )

    assert resp.status_code == 200
    assert hook._chunk_store.get("job001", []) == []
    assert hook._full_session_store["job001"]
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("not 'wav'" in m for m in errors), errors


def test_empty_final_chunk_marker_not_written_to_s3(
    client: TestClient, hermetic_s3: Any
) -> None:
    resp = client.post(
        "/chunks",
        files={"file": ("rec.9.webm", BytesIO(b""), "video/webm")},
        data={"metadata": json.dumps({"chunk_seq": 9, "is_final": True})},
    )
    assert resp.status_code == 200
    hermetic_s3.put_object.assert_not_called()  # no zero-byte S3 object
