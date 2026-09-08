"""Unit tests for the zoom-bot aw_output_hook.py FastAPI sidecar.

Covers the format-aware behaviour that distinguishes it from meet-bot: raw-PCM
chunks (Zoom), the chunk_seq dedup guard (ignoring the legacy single-shot WAV
upload), and PCM assembly without ffmpeg.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import threading
import time
import tracemalloc
from io import BytesIO
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import UploadFile

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
    hook._pending_full_audio.clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(hook.app)


# ── the background-encode harness ───────────────────────────────────────────
#
# A bare `TestClient` creates an anyio portal PER REQUEST and cancels its task
# group on teardown, so a task started by the handler is cancelled the instant
# the response is returned -- measured: `done=True, cancelled=True`, the encode
# never ran. Production is not like that: `aw_output_hook.py` ends in
# `uvicorn.run(app, ...)`, one persistent loop for the life of the process, so
# `asyncio.create_task` outlives the request.
#
# A context-managed client keeps ONE portal for the block, which reproduces the
# production semantics. Tests that touch the background path MUST use it --
# otherwise they pass or fail on whether the fake encode happened to yield,
# which is not a property of the code under test.
@pytest.fixture
def live_client() -> Iterator[TestClient]:
    with TestClient(hook.app) as c:
        yield c


def _await_encode(uid: str = "job001", timeout: float = 5.0) -> None:
    """Block the test thread until the background encode finishes.

    The portal runs the loop on another thread, so sleeping here lets it
    progress. Fails loudly rather than silently proceeding on a task that never
    ran -- an absent task means the handler did not schedule one at all.
    """
    task = hook._pending_full_audio.get(uid)
    assert task is not None, "the handler scheduled no background encode"
    deadline = time.monotonic() + timeout
    while not task.done():
        assert time.monotonic() < deadline, "background encode never finished"
        time.sleep(0.01)


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


def test_chunkless_upload_becomes_the_playable_artifact_not_a_chunk(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole-session WAV is STORED now, but never as a chunk.

    `RecordingService.upload()` POSTs the whole WAV on graceful leave with no
    chunk_seq. It used to be discarded outright, because concatenating it on top
    of the per-chunk PCM would have doubled the audio.

    It is now the source of `full_session.m4a`, the one recording a human can
    click and play. Different store, different S3 key, and the chunk store stays
    untouched -- which is the invariant that actually matters.
    """
    calls: dict[str, object] = {}
    monkeypatch.setattr(
        hook, "_encode_playable_audio", AsyncMock(return_value=b"AAC-ENCODED")
    )
    monkeypatch.setattr(
        hook, "_persist_full_audio_to_s3", lambda data: calls.__setitem__("body", data)
    )

    client.post(
        "/chunks",
        files={"file": ("rec.0.pcm", BytesIO(b"\x00" * 8), "application/octet-stream")},
        data={"metadata": _pcm_meta(0)},
    )
    before = list(hook._chunk_store["job001"])

    resp = client.post(
        "/chunks",
        files={
            "file": ("recording.wav", BytesIO(b"RIFF...." + b"\x00" * 500), "audio/wav")
        },
        data={"metadata": json.dumps({"format": "wav"})},  # no chunk_seq
    )

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}, "the blob is stored, not discarded"
    # THE INVARIANT: transcription's source is unchanged.
    assert hook._chunk_store["job001"] == before, "chunk store must be untouched"
    assert calls["body"] == b"AAC-ENCODED"


def test_chunkless_upload_failure_never_touches_the_chunk_store(
    live_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed encode degrades to "ignored" and leaves transcription alone.

    Without this, the success case above would be the only coverage -- and the
    PREVIOUS version of this test passed for the WRONG REASON once the behaviour
    changed: ffmpeg failed on its fake bytes and returned "ignored", which is
    indistinguishable from the old deliberate discard.
    """
    monkeypatch.setattr(
        hook, "_encode_playable_audio", AsyncMock(side_effect=RuntimeError("ffmpeg"))
    )
    live_client.post(
        "/chunks",
        files={"file": ("rec.0.pcm", BytesIO(b"\x00" * 8), "application/octet-stream")},
        data={"metadata": _pcm_meta(0)},
    )
    before = list(hook._chunk_store["job001"])

    resp = live_client.post(
        "/chunks",
        files={"file": ("recording.wav", BytesIO(b"RIFF"), "audio/wav")},
        data={"metadata": json.dumps({"format": "wav"})},
    )

    _await_encode()
    # "ok" = accepted; the encode failed afterwards and was logged, not
    # reported. What must hold is that transcription is untouched.
    assert resp.json() == {"status": "ok"}
    assert hook._chunk_store["job001"] == before


def test_empty_chunkless_upload_is_ignored_without_encoding(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    enc = AsyncMock(return_value=b"x")
    monkeypatch.setattr(hook, "_encode_playable_audio", enc)
    resp = client.post(
        "/chunks",
        files={"file": ("recording.wav", BytesIO(b""), "audio/wav")},
        data={"metadata": json.dumps({"format": "wav"})},
    )
    assert resp.json() == {"status": "ignored"}
    enc.assert_not_called()


def test_full_audio_s3_key_is_the_portal_linkable_object() -> None:
    """`full_session.m4a`, NOT `audio.wav` -- the latter is deleted by the worker
    after transcription, so linking it would 404. Cross-repo contract with the
    portal's FULL_AUDIO_CANDIDATES."""
    assert hook._FULL_AUDIO_OBJECT == "full_session.m4a"
    assert hook._FULL_AUDIO_CONTENT_TYPE == "audio/mp4"
    assert hook._full_audio_s3_key().endswith("/full_session.m4a")
    assert "audio_chunks/" not in hook._full_audio_s3_key()


def test_a_MALFORMED_chunk_seq_is_dropped_not_treated_as_the_blob(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absent and unparseable used to be one branch. They are not the same thing.

    Both used to mean "discard", so one `except (TypeError, ValueError)` covered
    them. Once the blob stopped being discarded they diverged: the legacy
    single-shot upload OMITS `chunk_seq` entirely (verified in recording.ts),
    whereas a PRESENT but unparseable value is a malformed CHUNK. Sending the
    latter down the blob path would encode a ~30-second fragment and PUT it OVER
    `full_session.m4a`, destroying the real recording with a piece of itself.

    `chunk_seq: null` is what JSON.stringify emits for NaN, so this is reachable.
    """
    blob = MagicMock()
    monkeypatch.setattr(hook, "_receive_full_session", blob)

    bad_values: list[Any] = ["not-a-number", None, [], {}]
    for bad in bad_values:
        resp = client.post(
            "/chunks",
            files={
                "file": ("rec.pcm", BytesIO(b"\x00" * 8), "application/octet-stream")
            },
            data={"metadata": json.dumps({"chunk_seq": bad, "format": "pcm"})},
        )
        assert resp.json() == {"status": "ignored"}, f"chunk_seq={bad!r}"

    blob.assert_not_called()
    assert hook._chunk_store.get("job001", []) == []


def test_an_ABSENT_chunk_seq_is_the_whole_session_blob(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the same decision: no key at all IS the blob.

    This is the shape `RecordingService.upload()` actually sends -- its metadata
    has no `chunk_seq` field -- so if this regresses, the feature silently does
    nothing at all and every Zoom meeting has no playable audio.
    """
    called: dict[str, object] = {}

    async def _capture(meta: dict[str, Any], file: Any) -> dict[str, str]:
        called["meta"] = meta
        return {"status": "ok"}

    monkeypatch.setattr(hook, "_receive_full_session", _capture)

    resp = client.post(
        "/chunks",
        files={"file": ("rec.pcm", BytesIO(b"\x00" * 8), "application/octet-stream")},
        data={"metadata": json.dumps({"format": "pcm"})},  # no chunk_seq key
    )
    assert resp.json() == {"status": "ok"}
    assert called, "an absent chunk_seq must route to the full-session path"
    assert (
        hook._chunk_store.get("job001", []) == []
    ), "and must never reach the transcription chunk store"


@pytest.mark.asyncio
async def test_CONCURRENT_blob_posts_start_exactly_one_encode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The in-flight guard must be a CLAIM, not a look.

    Reading `_pending_full_audio` and writing it after the `await` was a TOCTOU:
    three concurrent POSTs all saw an empty dict, all ran an encode, and only
    the LAST was tracked -- so the drain waited for one and abandoned two. Each
    is ~117s of CPU on a 4h blob, competing with the transcription transcode.

    ⚠ This test does NOT go through TestClient, and that is the point. A
    TestClient runs each request to completion through one anyio portal, so
    threads produce no request concurrency at all -- an earlier version of this
    test used `ThreadPoolExecutor` and BOTH TOCTOU mutants survived it. The
    concurrency it claimed to exercise was never generated.

    `_YieldingUpload` guarantees the interleave that makes the race reachable,
    and it is faithful rather than artificial: Starlette spools any body over
    `MultiPartParser.max_file_size` (1 MiB) to disk, after which every
    `UploadFile.read` is `await run_in_threadpool(...)` -- roughly 440 real
    await points inside the write loop for a 460 MB blob.
    """
    starts = {"n": 0}

    async def _slow(src: Path) -> bytes:
        starts["n"] += 1
        await asyncio.sleep(0.05)
        return b"AAC"

    monkeypatch.setattr(hook, "_encode_playable_audio", _slow)
    monkeypatch.setattr(hook, "_persist_full_audio_to_s3", lambda data: None)

    class _YieldingUpload(UploadFile):
        async def read(self, size: int = -1) -> bytes:  # type: ignore[override]
            await asyncio.sleep(0)  # the spooled-to-disk await point
            return await super().read(size)

    uploads = [
        _YieldingUpload(filename="full.bin", file=BytesIO(b"BLOB" * 64))
        for _ in range(3)
    ]
    results = await asyncio.gather(
        *(hook._receive_full_session({}, u) for u in uploads)
    )

    assert results == [{"status": "ok"}] * 3, "a duplicate must not be an error"
    task = hook._pending_full_audio.get(hook._SESSION_UID)
    assert task is not None
    await task

    assert starts["n"] == 1, (
        f"{starts['n']} encodes ran for one session -- the guard was evaluated "
        "before the claim was taken"
    )
    assert (
        len(hook._pending_full_audio) == 1
    ), "every started encode must be tracked, or the drain cannot cover it"


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
        # UNMAPPED must NOT claim "host_ended". That default was the bug: nine
        # reasons the shared Vexa code emits were unmapped, so `evicted` (bot
        # thrown out) and `admission_rejected_by_admin` (bot denied) both read as
        # "the host ended the meeting" -- a plausible, benign explanation for a
        # missing recording, which is why it went unnoticed.
        ("reason", "totally_unknown", "error"),
        # Admission / blocking failures: the bot never got in, or was ejected.
        ("reason", "admission_timeout", "error"),
        ("reason", "awaiting_admission_timeout", "error"),
        ("reason", "admission_rejected_by_admin", "error"),
        ("reason", "join_error_page_alive", "error"),
        ("reason", "unknown_blocking_state", "error"),
        # Eviction follows the existing removed_by_admin precedent.
        ("reason", "evicted", "host_ended"),
        # REGRESSION GUARD: the reasons that genuinely DO mean the host ended it
        # must still map to host_ended -- the fix must not flatten everything
        # into "error".
        ("reason", "normal_completion", "host_ended"),
        ("completion_reason", "left_alone", "last_participant"),
        ("completion_reason", "timeout", "hard_deadline"),
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


def test_mapped_reasons_do_not_warn_but_unmapped_ones_do(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """The WARNING is the only OBSERVABLE difference between a reason that is
    explicitly mapped to "error" and one that merely defaults to it.

    Without this test, deleting an explicit mapping is an EQUIVALENT MUTANT:
    the outcome assertions still pass because the default produces the same
    value. Verified by mutation - dropping `"admission_timeout": "error"`
    survived the whole suite until this existed.
    """
    with patch("aw_output_hook._run_pipeline_and_signal", new_callable=AsyncMock):
        # explicitly mapped -> silent
        caplog.clear()
        with caplog.at_level("WARNING"):
            client.post(
                "/callback",
                json={"status": "completed", "reason": "admission_timeout"},
            )
        assert not [
            r for r in caplog.records if "UNMAPPED" in r.message
        ], "an explicitly mapped reason must not warn"

        # genuinely unknown -> warns, so it gets noticed and mapped
        caplog.clear()
        with caplog.at_level("WARNING"):
            client.post(
                "/callback",
                json={"status": "completed", "reason": "brand_new_reason_xyz"},
            )
        assert [
            r for r in caplog.records if "UNMAPPED" in r.message
        ], "an unmapped reason must warn"


# --- streaming the whole-session blob, and never leaking it ------------------
#
# The blob is streamed to disk in 1 MiB blocks rather than read() into memory.
# For Teams that is a ~5 MB WebM; for Zoom it is an UNCOMPRESSED WAV, which
# meet-bot measured at ~460 MB for a 4h session against a 6Gi pod shared with
# Chromium, Node and a 1.5Gi /dev/shm. 4h is the FLOOR of the orchestrator's
# activeDeadlineSeconds, so that is a reachable size, not a hypothetical.


@pytest.mark.asyncio
async def test_stream_upload_to_temp_writes_every_byte_and_reports_the_size() -> None:
    payload = b"A" * (hook._UPLOAD_STREAM_CHUNK + 1234)  # spans >1 block
    upload = UploadFile(filename="rec.bin", file=BytesIO(payload))

    path, size = await hook._stream_upload_to_temp(upload, ".bin")
    try:
        assert size == len(payload), "size must be the streamed total"
        assert path.read_bytes() == payload, "no bytes lost across block boundaries"
        assert path.suffix == ".bin"
    finally:
        path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_stream_upload_to_temp_handles_an_empty_upload() -> None:
    upload = UploadFile(filename="rec.bin", file=BytesIO(b""))
    path, size = await hook._stream_upload_to_temp(upload, ".bin")
    try:
        assert size == 0
        assert path.read_bytes() == b""
    finally:
        path.unlink(missing_ok=True)


def test_temp_file_is_removed_after_a_successful_store(
    live_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A leaked temp file here is a WHOLE MEETING's audio left on the pod disk."""
    seen: dict[str, Path] = {}

    async def _capture(src: Path) -> bytes:
        seen["src"] = src
        assert src.exists(), "the encode must receive a real file"
        return b"AAC"

    monkeypatch.setattr(hook, "_encode_playable_audio", _capture)
    monkeypatch.setattr(hook, "_persist_full_audio_to_s3", lambda data: None)

    resp = live_client.post(
        "/chunks",
        files={"file": ("recording.bin", BytesIO(b"BLOB"), "application/octet-stream")},
        data={"metadata": json.dumps({})},  # no chunk_seq
    )
    _await_encode()
    assert resp.json() == {"status": "ok"}
    assert not seen["src"].exists(), "temp file leaked after success"


def test_temp_file_is_removed_even_when_the_encode_FAILS(
    live_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `finally` must cover the exception path too, not just success."""
    seen: dict[str, Path] = {}

    async def _boom(src: Path) -> bytes:
        seen["src"] = src
        raise RuntimeError("ffmpeg exploded")

    monkeypatch.setattr(hook, "_encode_playable_audio", _boom)

    resp = live_client.post(
        "/chunks",
        files={"file": ("recording.bin", BytesIO(b"BLOB"), "application/octet-stream")},
        data={"metadata": json.dumps({})},
    )
    _await_encode()
    # "ok" means ACCEPTED, not stored. The reply is sent before the encode runs
    # -- deliberately, so the bot's 30s upload timeout cannot fire and trigger
    # the 4x re-POST storm -- so a later encode failure cannot be reported in it.
    # It is logged instead, and the ONLY externally visible obligation is that
    # nothing is left behind.
    assert resp.json() == {"status": "ok"}
    assert not seen["src"].exists(), "temp file leaked after a failed encode"


@pytest.mark.asyncio
async def test_stream_upload_keeps_PEAK_MEMORY_bounded_by_the_block_size() -> None:
    """Measure the property, not a proxy for it.

    An earlier version of this test asserted only the READ SIZES -- that every
    `read()` was capped at 1 MiB. A mutant that reads in 1 MiB blocks and then
    concatenates them into one `bytes` before a single write preserves those read
    sizes perfectly, so it survived the entire 191-test suite. That mutant IS the
    regression this code exists to prevent: unbounded peak memory, ~460 MB for a
    4h Zoom session in a 6Gi pod shared with Chromium, Node and a 1.5Gi /dev/shm.

    `tracemalloc` measures the thing itself. The payload is allocated BEFORE
    tracing starts, so what is measured is only what the copy allocates:
    ~2 MiB streaming vs ~16 MiB buffering on an 8 MiB blob -- an 8x separation
    that grows with the blob, so the bound cannot be met by accident.
    """
    payload = b"X" * (hook._UPLOAD_STREAM_CHUNK * 8)  # 8 MiB
    calls: list[int | None] = []

    class _Spy(UploadFile):
        async def read(self, size: int = -1) -> bytes:  # type: ignore[override]
            calls.append(size)
            return await super().read(size)

    upload = _Spy(filename="rec.bin", file=BytesIO(payload))

    tracemalloc.start()
    try:
        path, size = await hook._stream_upload_to_temp(upload, ".bin")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    try:
        assert size == len(payload)
        assert path.read_bytes() == payload, "no bytes lost across block boundaries"

        bound = hook._UPLOAD_STREAM_CHUNK * 4
        assert peak < bound, (
            f"peak allocation {peak / 1048576:.2f} MiB exceeds the "
            f"{bound / 1048576:.0f} MiB bound on an 8 MiB blob -- the blob is being "
            "buffered in memory, which at Zoom's ~460 MB is the OOM this exists "
            "to prevent"
        )
        # Secondary, and weaker: the read loop's shape. Kept because an uncapped
        # read(-1) is a distinct bug from buffering, and this is what catches it.
        assert len(calls) >= 8, f"read in too few calls to be streaming: {calls}"
        assert set(calls) == {
            hook._UPLOAD_STREAM_CHUNK
        }, f"every read must be capped at the block size: {calls}"
    finally:
        path.unlink(missing_ok=True)


def test_the_full_session_blob_NEVER_enters_the_chunk_store(
    live_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The doubling guard, pinned.

    `_receive_full_session`'s docstring states "THE ONE RULE: this must never
    touch `_chunk_store`", and until now that was a comment, not a guarantee.
    If the blob were appended to the chunk store, the assembled transcription
    audio would contain the meeting TWICE -- which is exactly the live defect
    meet-bot's own comment predicted, and the reason the old code discarded
    this upload entirely rather than risk it.

    A comment that describes a safety property is not a safety property.
    """
    monkeypatch.setattr(hook, "_persist_chunk_to_s3", lambda seq, data, fmt: None)
    monkeypatch.setattr(hook, "_persist_full_audio_to_s3", lambda data: None)

    async def _enc(src: Path) -> bytes:
        return b"AAC"

    monkeypatch.setattr(hook, "_encode_playable_audio", _enc)

    # a real chunk first, so the store is non-empty and a mutation is visible
    live_client.post(
        "/chunks",
        files={"file": ("c0.webm", BytesIO(b"CHUNKZERO"), "video/webm")},
        data={"metadata": json.dumps({"chunk_seq": 0})},
    )
    before = {k: list(v) for k, v in hook._chunk_store.items()}
    assert before, "positive control: the chunked path must have stored something"

    resp = live_client.post(
        "/chunks",
        files={
            "file": (
                "full.bin",
                BytesIO(b"WHOLE-SESSION-BLOB"),
                "application/octet-stream",
            )
        },
        data={"metadata": json.dumps({})},  # no chunk_seq -> the blob path
    )
    _await_encode()
    assert resp.json() == {"status": "ok"}, "the blob path must still succeed"

    after = {k: list(v) for k, v in hook._chunk_store.items()}
    assert after == before, (
        "the whole-session blob leaked into the transcription chunk store -- "
        "the assembled audio would contain the meeting twice"
    )
    assert not any(
        b"WHOLE-SESSION-BLOB" in c for chunks in after.values() for c in chunks
    ), "the blob's bytes are in the chunk store"


@pytest.mark.asyncio
async def test_stream_upload_removes_its_PARTIAL_file_when_the_write_loop_RAISES() -> (
    None
):
    """`delete=False` makes the helper the OWNER until it returns cleanly.

    The caller's cleanup cannot cover this: `src, size = await ...` never
    completes, so the caller's `finally` never starts. Realistic trigger is
    ENOSPC on `f.write()` -- likeliest under exactly the disk pressure the
    streaming rewrite exists to relieve. At Zoom's ~460 MB an orphan here could
    starve `_assemble_audio`, i.e. break the transcription that actually matters.
    """
    tmpdir = Path(tempfile.gettempdir())
    before = set(tmpdir.glob("*.leaktest"))
    partial: dict[str, int] = {"bytes": -1}

    class _DiesMidWrite(UploadFile):
        """Yields one full block, then fails -- so a PARTIAL file exists."""

        def __init__(self) -> None:
            super().__init__(
                filename="rec.bin",
                file=BytesIO(b"X" * (hook._UPLOAD_STREAM_CHUNK * 2)),
            )
            self.calls = 0

        async def read(self, size: int = -1) -> bytes:  # type: ignore[override]
            self.calls += 1
            if self.calls > 1:
                # Observe the partial file BEFORE the raise triggers cleanup.
                written = [q for q in tmpdir.glob("*.leaktest") if q not in before]
                partial["bytes"] = written[0].stat().st_size if written else 0
                raise OSError(28, "No space left on device")
            return await super().read(size)

    upload = _DiesMidWrite()
    with pytest.raises(OSError):
        await hook._stream_upload_to_temp(upload, ".leaktest")

    # A GENUINE control. `upload.calls == 2` alone proves only that read() was
    # called twice -- deleting `f.write(block)` keeps it true with no partial
    # file ever existing, and the test stays green. So capture the path and
    # record its size at the moment of the raise, before the cleanup removes it.
    assert upload.calls == 2, "read() must have been called twice"
    assert partial["bytes"] == hook._UPLOAD_STREAM_CHUNK, (
        "a PARTIAL file must actually have existed for this test to mean "
        f"anything; observed {partial['bytes']} bytes on disk at the raise"
    )
    leaked = set(tmpdir.glob("*.leaktest")) - before
    assert not leaked, f"partial temp file orphaned for the life of the pod: {leaked}"


def test_a_disk_failure_while_streaming_DECLINES_rather_than_500(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "Best-effort throughout" must hold for the streaming step too.

    A 500 here would surface as a server error on the bot's upload retry; the
    contract is that a failure to store the bonus playback file is invisible to
    transcription, which depends only on the chunks.
    """
    encoded = MagicMock()
    monkeypatch.setattr(hook, "_encode_playable_audio", encoded)

    async def _enospc(file: Any, suffix: str) -> tuple[Path, int]:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(hook, "_stream_upload_to_temp", _enospc)
    before = {k: list(v) for k, v in hook._chunk_store.items()}

    resp = client.post(
        "/chunks",
        files={"file": ("full.bin", BytesIO(b"BLOB"), "application/octet-stream")},
        data={"metadata": json.dumps({})},
    )
    assert resp.status_code == 200, "must not 500"
    assert resp.json() == {"status": "ignored"}
    # must not shell out to ffmpeg with no input file
    encoded.assert_not_called()
    assert {
        k: list(v) for k, v in hook._chunk_store.items()
    } == before, "transcription must be untouched by a playback-file failure"


# ── F1: the encode must not sit inside the request the bot is awaiting ───────
#
# `recording.ts:122` gives this endpoint 30s and on timeout DESTROYS the request
# and re-POSTs the whole blob, 4 attempts. AAC runs at ~130x realtime, so ~65
# MINUTES of audio blows that budget and up to FOUR concurrent encodes stack on
# one pod. These tests pin the shape that prevents it.


def test_the_reply_is_sent_BEFORE_the_encode_finishes(
    live_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE F1 fix. If this regresses, long meetings lose their audio entirely.

    The assertion is that the task is still UNFINISHED at the moment the
    response arrives. A handler that awaits the encode inline cannot satisfy
    that: the response could only appear after the task completed.
    """

    # Gated on an event the TEST releases, not on elapsed time. A sleep here
    # would make this flake red if the test thread stalled between the POST and
    # the `.done()` read -- false alarms in CI teach people to ignore the suite.
    gate = threading.Event()

    async def _slow(src: Path) -> bytes:
        # BOUNDED. An unbounded wait deadlocks under the very mutant this
        # test exists to catch: an inline `await` never returns the response,
        # so the test never reaches `gate.set()`. A hang in CI is a worse
        # failure than a red assertion -- bound it and let the assert speak.
        await asyncio.to_thread(gate.wait, 15.0)
        return b"AAC"

    monkeypatch.setattr(hook, "_encode_playable_audio", _slow)
    monkeypatch.setattr(hook, "_persist_full_audio_to_s3", lambda data: None)

    resp = live_client.post(
        "/chunks",
        files={"file": ("full.bin", BytesIO(b"BLOB"), "application/octet-stream")},
        data={"metadata": json.dumps({})},
    )
    task = hook._pending_full_audio.get("job001")

    assert resp.json() == {"status": "ok"}
    assert task is not None, "no background encode was scheduled"
    assert not task.done(), (
        "the handler waited for the encode -- on a >65 min meeting that means a "
        "30s client timeout, 4 re-POSTs and 4 concurrent ffmpeg processes"
    )
    gate.set()
    _await_encode()


def test_a_RETRY_while_an_encode_is_in_flight_does_not_start_a_second_one(
    live_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Belt and braces on the retry storm: one encode per session, not four."""
    starts = {"n": 0}
    gate = threading.Event()  # holds the first encode open across the 2nd POST

    async def _slow(src: Path) -> bytes:
        starts["n"] += 1
        # BOUNDED. An unbounded wait deadlocks under the very mutant this
        # test exists to catch: an inline `await` never returns the response,
        # so the test never reaches `gate.set()`. A hang in CI is a worse
        # failure than a red assertion -- bound it and let the assert speak.
        await asyncio.to_thread(gate.wait, 15.0)
        return b"AAC"

    monkeypatch.setattr(hook, "_encode_playable_audio", _slow)
    monkeypatch.setattr(hook, "_persist_full_audio_to_s3", lambda data: None)

    post = lambda: live_client.post(  # noqa: E731
        "/chunks",
        files={"file": ("full.bin", BytesIO(b"BLOB"), "application/octet-stream")},
        data={"metadata": json.dumps({})},
    )
    first, second = post(), post()

    assert first.json() == {"status": "ok"}
    assert second.json() == {"status": "ok"}, "a duplicate must not be an error"
    gate.set()
    _await_encode()
    assert starts["n"] == 1, (
        f"{starts['n']} encodes ran for one session; each is ~117s of CPU on a 4h "
        "blob and they compete with the transcription transcode"
    )


@pytest.mark.asyncio
async def test_the_teardown_sentinel_WAITS_for_the_playback_encode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """start.sh SIGKILLs the sidecar once the sentinel appears.

    So an encode still running when the sentinel is written is an encode that
    gets killed mid-write and an artifact that silently never exists. The
    encode starts when the blob arrives, i.e. BEFORE the callback, so it runs
    concurrently with the pipeline -- this only waits for what is left.
    """
    sentinel = tmp_path / "pipeline_done"
    monkeypatch.setattr(hook, "_PIPELINE_DONE_SENTINEL", sentinel)
    monkeypatch.setattr(hook, "_run_pipeline", AsyncMock())

    order: list[str] = []

    async def _slow() -> None:
        await asyncio.sleep(0.15)
        # Observed from INSIDE the task, which is the only place that can tell
        # the two orderings apart. Recording it after `_run_pipeline_and_signal`
        # returns cannot: touch-then-drain and drain-then-touch BOTH end with the
        # sentinel present, so the assertion passed either way -- and touch-first
        # is the fatal variant, because start.sh kills on the sentinel's
        # APPEARANCE, not on the pipeline returning.
        order.append("sentinel-already-written" if sentinel.exists() else "encode")

    hook._pending_full_audio[hook._SESSION_UID] = asyncio.create_task(_slow())

    await hook._run_pipeline_and_signal(bot_left_reason="last_participant")
    order.append("sentinel" if sentinel.exists() else "NO-SENTINEL")

    assert order == ["encode", "sentinel"], (
        "the sentinel must be written only AFTER the encode finished; got "
        f"{order} -- a leading 'sentinel-already-written' means start.sh would "
        "have SIGKILLed the encode mid-write"
    )


@pytest.mark.asyncio
async def test_a_HUNG_encode_cannot_pin_the_pod_forever(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The wait is bounded, and the abandoned task still cleans up after itself.

    Transcription has already been delivered by this point, so teardown must
    proceed. Cancelling (rather than shielding) is what runs the task's
    `finally` and removes its temp file instead of orphaning it.
    """
    sentinel = tmp_path / "pipeline_done"
    monkeypatch.setattr(hook, "_PIPELINE_DONE_SENTINEL", sentinel)
    monkeypatch.setattr(hook, "_run_pipeline", AsyncMock())
    monkeypatch.setattr(hook, "_FULL_AUDIO_DRAIN_TIMEOUT_S", 0.05)

    leftover = tmp_path / "blob.webm"
    leftover.write_bytes(b"X")
    cleaned = {"v": False}

    async def _hangs() -> None:
        try:
            await asyncio.sleep(30)
        finally:
            leftover.unlink(missing_ok=True)
            cleaned["v"] = True

    task = asyncio.create_task(_hangs())
    hook._pending_full_audio[hook._SESSION_UID] = task

    await hook._run_pipeline_and_signal(bot_left_reason="last_participant")

    assert sentinel.exists(), "teardown must proceed rather than hang on a stuck encode"
    assert task.cancelled(), "the abandoned encode must be cancelled, not left running"
    assert (
        cleaned["v"] and not leftover.exists()
    ), "cancellation must run the task's finally so its temp file is removed"


def test_a_NON_TERMINAL_callback_does_not_warn_about_an_unmapped_reason(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """A healthy meeting must produce ZERO unmapped-reason warnings.

    `joining` / `awaiting_admission` / `active` are ordinary progress callbacks
    and legitimately carry no leave reason. Mapping them logged three UNMAPPED
    warnings per meeting -- observed live on 2026-09-08 -- each telling the
    reader to "add it to _REASON_MAP so the row says something true", when there
    was nothing to add and the value was discarded by the terminal check two
    lines later. A warning that fires on every healthy run trains people to
    ignore the log, which is worse than not warning at all.
    """
    with caplog.at_level(logging.WARNING):
        for st in ("joining", "awaiting_admission", "active"):
            resp = client.post(
                "/callback",
                json={"connection_id": "job001", "status": st},
            )
            assert resp.status_code == 200

    unmapped = [r for r in caplog.records if "UNMAPPED leave reason" in r.getMessage()]
    assert not unmapped, (
        "non-terminal callbacks must not warn; got "
        f"{[r.getMessage() for r in unmapped]}"
    )


def test_a_TERMINAL_callback_with_an_unknown_reason_STILL_warns(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The positive control: the diagnostic must not have been silenced.

    Moving the mapping below the terminal check could just as easily have
    disabled the warning entirely. This proves it still fires where it means
    something -- a genuine leave whose reason we do not recognise, which is how
    `bot_left_reason` silently became wrong before.
    """
    monkeypatch.setattr(hook, "_run_pipeline_and_signal", AsyncMock(return_value=None))
    with caplog.at_level(logging.WARNING):
        resp = client.post(
            "/callback",
            json={
                "connection_id": "job001",
                "status": "completed",
                "completion_reason": "a_reason_we_have_never_seen",
            },
        )
        assert resp.status_code == 200

    msgs = [r.getMessage() for r in caplog.records if "UNMAPPED" in r.getMessage()]
    assert msgs, "a terminal callback with an unknown reason must still warn"
    assert "a_reason_we_have_never_seen" in msgs[0]
