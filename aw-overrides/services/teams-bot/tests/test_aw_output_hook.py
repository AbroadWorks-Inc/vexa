"""Unit tests for the teams-bot aw_output_hook.py FastAPI sidecar.

Adapted from zoom-bot's suite, which is where this sidecar comes from. The
Teams-specific additions cover exactly the four things that differ:

  * WebM is the declared format and the fallback (zoom-bot's was raw PCM), so
    assembly routes through ffmpeg.
  * The chunk_seq dedup guard — the SINGLE reason teams-bot reuses zoom-bot's
    hook rather than meet-bot's. Teams flushes every retained chunk as one blob
    on leave with no chunk_seq; accepting it would deliver the meeting twice.
  * The empty is_final marker must be accepted without writing an empty S3
    object.
  * _warn_on_silence, ported from meet-bot: platforms/msteams/ ships with zero
    tests, so "the pipeline succeeded and the transcript is empty" is the
    failure that will actually be hit, and it is indistinguishable from success
    without that log line.

The raw-PCM tests are retained rather than deleted: the PCM branch still exists
in the module (the format is read from chunk metadata, not hardcoded per
platform), so it is still live code and still needs a guard.
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
    "meeting_id": "teams_evt_001",
    "platform": "teams",
    "scheduled_start_at": "2026-09-03T10:00:00+00:00",
    "expected_duration_min": 60,
    "hard_deadline_at": "2026-09-03T14:00:00+00:00",
    "join": {
        "url": "https://teams.live.com/meet/9312345678901",
        "organizer_email": "host@example.com",
        "requires_admit": True,
    },
    "display_name": "AW Notetaker",
    "consent": {
        "state": "implicit_internal",
        "recorded_at": "2026-09-03T09:55:00+00:00",
        "by_user_id": "system",
    },
    "retry": {"attempts": 0, "max_attempts": 2},
    "s3_key": "recordings/teams_evt_001_job001/",
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


def _chunk_meta(seq: int, is_final: bool = False, fmt: str | None = "webm") -> str:
    meta: dict[str, Any] = {"chunk_seq": seq, "is_final": is_final}
    if fmt is not None:
        meta["format"] = fmt
    return json.dumps(meta)


def test_healthz_returns_ok(client: TestClient) -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# --- job fixture: the platform this image exists for -------------------------


def test_job_fixture_is_a_teams_job() -> None:
    # Guards the fixture itself. If this file were copied on without editing the
    # job, every assertion below would still pass while describing zoom-bot.
    assert hook._JOB.platform == "teams"
    assert hook._JOB.s3_key.startswith("recordings/teams_")


# --- chunk ingestion + format tracking --------------------------------------


def test_post_webm_chunk_accumulates_bytes_and_records_format(
    client: TestClient,
) -> None:
    chunk_data = b"\x1a\x45\xdf\xa3" + b"\x00\x01" * 200  # EBML magic + payload
    resp = client.post(
        "/chunks",
        files={"file": ("recording.0.webm", BytesIO(chunk_data), "audio/webm")},
        data={"metadata": _chunk_meta(0)},
    )
    assert resp.status_code == 200
    assert hook._chunk_store["job001"] == [chunk_data]
    assert hook._chunk_format["job001"] == "webm"


def test_chunk_with_no_declared_format_defaults_to_webm(client: TestClient) -> None:
    # Teams' Node bridge derives the format from recorder.mimeType and declares
    # it explicitly, so this path is only reachable on malformed metadata. It
    # still matters: reading Opus-in-WebM as raw PCM assembles and transcribes
    # as NOISE rather than raising, so the default must be the container format.
    resp = client.post(
        "/chunks",
        files={"file": ("rec.0.webm", BytesIO(b"\x00" * 64), "audio/webm")},
        data={"metadata": _chunk_meta(0, fmt=None)},
    )
    assert resp.status_code == 200
    assert hook._chunk_format["job001"] == "webm"


def test_normalize_format_defaults_to_webm() -> None:
    assert hook._normalize_format(None) == "webm"
    assert hook._normalize_format("") == "webm"
    assert hook._normalize_format("  WEBM ") == "webm"
    # An explicitly declared format still wins over the default.
    assert hook._normalize_format("pcm") == "pcm"


def test_post_multiple_chunks_all_stored(client: TestClient) -> None:
    for i in range(3):
        client.post(
            "/chunks",
            files={"file": (f"rec.{i}.webm", BytesIO(b"\x00" * 100), "audio/webm")},
            data={"metadata": _chunk_meta(i, is_final=i == 2)},
        )
    assert len(hook._chunk_store.get("job001", [])) == 3


# --- THE DOUBLING GUARD ------------------------------------------------------


def test_chunkless_upload_is_ignored_and_leaves_chunk_store_untouched(
    client: TestClient,
) -> None:
    # msteams/leave.ts flushes every retained chunk as ONE blob, which index.ts
    # POSTs here with no chunk_seq. For Teams the incremental chunks are the ONLY
    # audio, so accepting this would deliver the meeting twice. A pre-existing
    # chunk is seeded so "untouched" is a real claim rather than "still empty".
    client.post(
        "/chunks",
        files={"file": ("rec.0.webm", BytesIO(b"real-chunk"), "audio/webm")},
        data={"metadata": _chunk_meta(0)},
    )
    before = list(hook._chunk_store["job001"])

    resp = client.post(
        "/chunks",
        files={
            "file": ("recording.webm", BytesIO(b"WHOLE-MEETING-AGAIN"), "audio/webm")
        },
        data={"metadata": json.dumps({"format": "webm"})},  # no chunk_seq
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored"}
    assert hook._chunk_store["job001"] == before
    assert b"WHOLE-MEETING-AGAIN" not in b"".join(hook._chunk_store["job001"])


def test_chunk_with_non_integer_seq_is_ignored(client: TestClient) -> None:
    resp = client.post(
        "/chunks",
        files={"file": ("rec.webm", BytesIO(b"\x00" * 8), "audio/webm")},
        data={"metadata": json.dumps({"chunk_seq": "not-a-number", "format": "webm"})},
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored"}
    assert hook._chunk_store.get("job001", []) == []


# --- callback → pipeline (identical contract to zoom-bot / meet-bot) --------


def test_post_callback_triggers_pipeline(client: TestClient) -> None:
    payload = {
        "connection_id": "job001",
        "platform": "teams",
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


def test_chunk_s3_key_webm_extension_zero_padded() -> None:
    key = hook._chunk_s3_key(5, "webm")
    assert key == f"{hook._JOB.s3_key}audio_chunks/chunk_000005.webm"


def test_chunk_s3_key_pcm_extension() -> None:
    assert hook._chunk_s3_key(0, "pcm").endswith("audio_chunks/chunk_000000.pcm")


def test_content_type_for_webm() -> None:
    assert hook._content_type_for("webm") == "audio/webm"


def test_content_type_for_pcm_is_l16() -> None:
    assert hook._content_type_for("pcm") == "audio/L16;rate=16000;channels=1"
    assert hook._content_type_for("s16le") == "audio/L16;rate=16000;channels=1"


# --- durability: incremental persistence to S3 ------------------------------


@pytest.fixture(autouse=True)
def hermetic_s3() -> Any:
    hook._s3_client = None
    fake_instance = MagicMock()
    with patch("aw_output_hook.S3Client", return_value=fake_instance):
        yield fake_instance
    hook._s3_client = None


def test_persist_chunk_to_s3_puts_object_with_webm_metadata(hermetic_s3: Any) -> None:
    hook._persist_chunk_to_s3(7, b"audio-bytes", "webm")
    hermetic_s3.put_object.assert_called_once()
    kwargs = hermetic_s3.put_object.call_args.kwargs
    assert kwargs["key"] == hook._chunk_s3_key(7, "webm")
    assert kwargs["body"] == b"audio-bytes"
    assert kwargs["content_type"] == "audio/webm"


def test_persist_chunk_to_s3_non_fatal_on_error(hermetic_s3: Any) -> None:
    hermetic_s3.put_object.side_effect = RuntimeError("s3 down")
    hook._persist_chunk_to_s3(1, b"x", "webm")  # must NOT raise


def test_post_chunk_persists_to_s3(client: TestClient, hermetic_s3: Any) -> None:
    resp = client.post(
        "/chunks",
        files={"file": ("rec.3.webm", BytesIO(b"\x00" * 128), "audio/webm")},
        data={"metadata": _chunk_meta(3)},
    )
    assert resp.status_code == 200
    hermetic_s3.put_object.assert_called_once()
    assert hermetic_s3.put_object.call_args.kwargs["key"].endswith("chunk_000003.webm")


def test_empty_final_chunk_accepted_but_not_persisted(
    client: TestClient, hermetic_s3: Any
) -> None:
    # msteams/recording.ts's in-page finalizer sends {base64: "", chunkSeq,
    # isFinal: true}. The Node bridge drops empty buffers before POSTing, so this
    # should not arrive in practice — the guard is pinned anyway, because an
    # empty S3 object is a worse outcome than a skipped one if that ever moves.
    resp = client.post(
        "/chunks",
        files={"file": ("rec.final.webm", BytesIO(b""), "audio/webm")},
        data={"metadata": _chunk_meta(9, is_final=True)},
    )
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    assert hook._chunk_store["job001"] == [b""]
    hermetic_s3.put_object.assert_not_called()


def test_post_chunk_still_ok_when_s3_fails(
    client: TestClient, hermetic_s3: Any
) -> None:
    hermetic_s3.put_object.side_effect = RuntimeError("s3 down")
    resp = client.post(
        "/chunks",
        files={"file": ("rec.0.webm", BytesIO(b"\x00" * 64), "audio/webm")},
        data={"metadata": _chunk_meta(0)},
    )
    assert resp.status_code == 200
    assert len(hook._chunk_store.get("job001", [])) == 1


# --- assembly: WebM transcodes via ffmpeg, PCM concatenates raw -------------


def test_assemble_audio_webm_routes_to_ffmpeg(tmp_path: Path) -> None:
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


def test_assemble_audio_pcm_concatenates_raw_without_ffmpeg() -> None:
    import asyncio

    chunks = [b"\x01\x02" * 10, b"\x03\x04" * 10]
    with patch("aw_output_hook._convert_container_to_pcm") as mock_ffmpeg:
        path = asyncio.run(hook._assemble_audio(chunks, "pcm"))
    mock_ffmpeg.assert_not_called()  # PCM needs no transcode
    assert path.read_bytes() == b"".join(chunks)
    path.unlink(missing_ok=True)


def test_convert_container_to_pcm_builds_ffmpeg_s16le_16k_mono(tmp_path: Path) -> None:
    import asyncio

    recorded: dict[str, Any] = {}

    class _Proc:
        returncode = 0

        async def wait(self) -> None:
            return None

    async def _fake_exec(*cmd: str, **_: Any) -> _Proc:
        recorded["cmd"] = list(cmd)
        Path(cmd[-1]).write_bytes(b"\x00" * 8)
        return _Proc()

    with patch("asyncio.create_subprocess_exec", new=_fake_exec):
        out = asyncio.run(hook._convert_container_to_pcm([b"webm"], "webm"))

    cmd = recorded["cmd"]
    assert cmd[0] == "ffmpeg"
    # The adapter reads the result as s16le/16 kHz/mono; a wrong flag here makes
    # every downstream timestamp wrong without failing anything.
    for flag in ("-f", "s16le", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1"):
        assert flag in cmd, f"ffmpeg command is missing {flag!r}: {cmd}"
    assert str(out).endswith(".raw")
    out.unlink(missing_ok=True)


def test_run_pipeline_assembles_webm_and_calls_run_from_redis(tmp_path: Path) -> None:
    import asyncio

    hook._chunk_store["job001"] = [b"\x00" * 512, b"\x01" * 512]
    hook._chunk_format["job001"] = "webm"
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

    assert mock_asm.call_args.args[1] == "webm"  # format threaded through
    mock_rfr.assert_awaited_once()
    call_kwargs = mock_rfr.call_args.kwargs
    assert call_kwargs["bot_left_reason"] == "last_participant"
    assert call_kwargs["session_uid"] == "job001"
    assert call_kwargs["audio_raw_path"] == fake_pcm


def test_run_pipeline_format_fallback_is_webm_not_pcm(tmp_path: Path) -> None:
    import asyncio

    # Chunks present but no recorded format (malformed metadata on every chunk).
    hook._chunk_store["job001"] = [b"\x00" * 32]
    fake_pcm = tmp_path / "audio.raw"
    fake_pcm.write_bytes(b"\x00" * 16)

    with (
        patch(
            "aw_output_hook._assemble_audio",
            new_callable=AsyncMock,
            return_value=fake_pcm,
        ) as mock_asm,
        patch("aw_output_hook.run_from_redis", new_callable=AsyncMock),
    ):
        asyncio.run(hook._run_pipeline(bot_left_reason="host_ended"))

    assert mock_asm.call_args.args[1] == "webm"


def test_run_pipeline_skips_when_no_chunks(tmp_path: Path) -> None:
    import asyncio

    with patch("aw_output_hook.run_from_redis", new_callable=AsyncMock) as mock_rfr:
        asyncio.run(hook._run_pipeline(bot_left_reason="host_ended"))
    mock_rfr.assert_not_awaited()


# --- silence diagnostic (ported from meet-bot) -------------------------------


def _signal(samples: int, amplitude: int = 8000) -> bytes:
    import array as _array

    buf = _array.array(
        "h", [amplitude if i % 2 else -amplitude for i in range(samples)]
    )
    return buf.tobytes()


def test_measure_level_reports_zero_on_digital_silence() -> None:
    peak, rms = hook._measure_level(b"\x00\x00" * 4096)
    assert peak == 0
    assert rms == 0.0


def test_measure_level_reports_peak_and_rms_on_signal() -> None:
    peak, rms = hook._measure_level(_signal(4096, amplitude=8000))
    assert peak == 8000
    assert rms == pytest.approx(8000.0, rel=0.01)


def test_measure_level_handles_empty_buffer() -> None:
    assert hook._measure_level(b"") == (0, 0.0)


def test_measure_level_sees_a_late_transient() -> None:
    # A positive control for the sampling: a buffer that is silent except at the
    # very end must still register, or "peak == 0" below would be meaningless.
    pcm = bytearray(b"\x00\x00" * 8192)
    pcm[-2:] = (9000).to_bytes(2, "little", signed=True)
    peak, _ = hook._measure_level(bytes(pcm))
    assert peak == 9000


def test_warn_on_silence_fires_on_zeros(tmp_path: Path, caplog: Any) -> None:
    import logging

    path = tmp_path / "silent.raw"
    path.write_bytes(b"\x00\x00" * 16000)
    with caplog.at_level(logging.ERROR, logger=hook.logger.name):
        hook._warn_on_silence(path)
    assert "DIGITAL SILENCE" in caplog.text
    # The diagnosis must be Teams' (in-page capture), not meet-bot's dead sink —
    # an operator sent to pactl would be looking in the wrong place entirely.
    assert "findMediaElements" in caplog.text
    assert "IN-PAGE" in caplog.text


def test_warn_on_silence_stays_quiet_on_signal(tmp_path: Path, caplog: Any) -> None:
    import logging

    path = tmp_path / "loud.raw"
    path.write_bytes(_signal(16000, amplitude=8000))
    with caplog.at_level(logging.ERROR, logger=hook.logger.name):
        hook._warn_on_silence(path)
    assert "DIGITAL SILENCE" not in caplog.text


def test_warn_on_silence_does_not_fire_on_a_quiet_meeting(
    tmp_path: Path, caplog: Any
) -> None:
    import logging

    # NOT a pin on the constant's value — a pin would go red on any deliberate
    # retune and prove nothing. This is the claim the threshold exists to make:
    # a real but quiet meeting must NOT be reported as a dead capture. Peak 2000
    # of 32767 (~ -24 dBFS) is a soft speaker on a laptop mic, and the whole
    # value of this diagnostic is that an operator can trust it — one false
    # "DIGITAL SILENCE" on a real recording and they stop reading the line.
    #
    # Found by the mutation harness: raising the threshold to 4096 survived every
    # other test in this file.
    path = tmp_path / "quiet.raw"
    path.write_bytes(_signal(16000, amplitude=2000))
    with caplog.at_level(logging.ERROR, logger=hook.logger.name):
        hook._warn_on_silence(path)
    assert "DIGITAL SILENCE" not in caplog.text


def test_warn_on_silence_threshold_boundary(tmp_path: Path, caplog: Any) -> None:
    import logging

    # At the threshold it still fires (the gate is `peak > threshold`); one LSB
    # above it does not. Pins the comparison operator, not just the constant.
    at = tmp_path / "at.raw"
    at.write_bytes(_signal(4096, amplitude=hook._SILENCE_PEAK_THRESHOLD))
    with caplog.at_level(logging.ERROR, logger=hook.logger.name):
        hook._warn_on_silence(at)
    assert "DIGITAL SILENCE" in caplog.text

    caplog.clear()
    above = tmp_path / "above.raw"
    above.write_bytes(_signal(4096, amplitude=hook._SILENCE_PEAK_THRESHOLD + 1))
    with caplog.at_level(logging.ERROR, logger=hook.logger.name):
        hook._warn_on_silence(above)
    assert "DIGITAL SILENCE" not in caplog.text


def test_warn_on_silence_is_silent_when_the_file_cannot_be_read(
    tmp_path: Path, caplog: Any
) -> None:
    import logging

    missing = tmp_path / "does-not-exist.raw"
    with caplog.at_level(logging.ERROR, logger=hook.logger.name):
        hook._warn_on_silence(missing)  # must NOT raise
    assert "DIGITAL SILENCE" not in caplog.text
    assert "could not read" in caplog.text


def test_run_pipeline_measures_the_assembled_audio(tmp_path: Path) -> None:
    import asyncio

    hook._chunk_store["job001"] = [b"webm-bytes"]
    hook._chunk_format["job001"] = "webm"
    fake_pcm = tmp_path / "audio.raw"
    fake_pcm.write_bytes(b"\x00" * 64)

    with (
        patch(
            "aw_output_hook._assemble_audio",
            new_callable=AsyncMock,
            return_value=fake_pcm,
        ),
        patch("aw_output_hook.run_from_redis", new_callable=AsyncMock),
        patch("aw_output_hook._warn_on_silence") as mock_warn,
    ):
        asyncio.run(hook._run_pipeline(bot_left_reason="host_ended"))

    mock_warn.assert_called_once_with(fake_pcm)


# --- sampled file read: the scan budget must be fully covered ---------------


def test_sample_pcm_file_returns_whole_file_when_under_budget(tmp_path: Path) -> None:
    path = tmp_path / "small.raw"
    payload = _signal(1024)
    path.write_bytes(payload)
    assert hook._sample_pcm_file(path) == payload


def test_sample_pcm_file_caps_at_the_scan_budget(tmp_path: Path) -> None:
    budget_samples = hook._LEVEL_SCAN_WINDOWS * hook._LEVEL_SCAN_WINDOW_SAMPLES
    path = tmp_path / "big.raw"
    path.write_bytes(b"\x00\x00" * (budget_samples * 4))
    sampled = hook._sample_pcm_file(path)
    assert len(sampled) == budget_samples * 2


def test_measure_level_covers_a_budget_sized_buffer_with_no_gaps() -> None:
    # The reason _sample_pcm_file hands back EXACTLY the scan budget: at that
    # size _measure_level computes stride == window, so it reads every byte it
    # was given. Proven by planting a marker in the last window — if any window
    # were skipped, the sampled read would be throwing measured audio away.
    import array as _array

    budget = hook._LEVEL_SCAN_WINDOWS * hook._LEVEL_SCAN_WINDOW_SAMPLES
    buf = _array.array("h", [0] * budget)
    buf[budget - 1] = 4321
    peak, _ = hook._measure_level(buf.tobytes())
    assert peak == 4321


def test_sample_pcm_file_sees_signal_late_in_a_large_file(tmp_path: Path) -> None:
    # End-to-end control for the sampled read: signal only in the final window of
    # an over-budget file must still reach _measure_level, so a real meeting is
    # never reported as silent because of where the sampling landed.
    budget_samples = hook._LEVEL_SCAN_WINDOWS * hook._LEVEL_SCAN_WINDOW_SAMPLES
    path = tmp_path / "late.raw"
    total = budget_samples * 4
    pcm = bytearray(b"\x00\x00" * total)
    pcm[-hook._LEVEL_SCAN_WINDOW_SAMPLES * 2 :] = _signal(
        hook._LEVEL_SCAN_WINDOW_SAMPLES, amplitude=7777
    )
    path.write_bytes(bytes(pcm))
    peak, _ = hook._measure_level(hook._sample_pcm_file(path))
    assert peak == 7777


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
