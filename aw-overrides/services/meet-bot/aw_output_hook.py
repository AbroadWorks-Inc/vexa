"""FastAPI sidecar for the Vexa meet-bot K8s Job pod.

Receives audio chunks and session-end callbacks from Vexa's TypeScript bot
(on localhost) and drives the aw-integration output pipeline.

Two kinds of upload arrive on ``POST /chunks``:
  * **incremental chunk** — carries an integer ``chunk_seq`` (``format: webm``).
    A 30 s durability backup, persisted under ``audio_chunks/``.
  * **full-session recording** — NO ``chunk_seq`` (``format: wav``, plus
    ``sample_rate``/``channels``/``duration_seconds``). This is the PRIMARY
    transcription audio, written by the Node-side continuous PulseAudio capture.

Keeping the two apart is the fix for the doubled-transcript bug: every upload
used to be appended to ``_chunk_store`` before the metadata was even parsed, so
the whole-session recording was concatenated on top of the backup chunks and the
meeting was transcribed twice (the zoom-bot sidecar has guarded this since B1).

Env vars:
    BOT_JOB_JSON   — JSON-encoded BotJob (set by bot-orchestrator job_launcher)
    CONNECTION_ID  — Vexa session UID (= job.job_id = BOT_CONFIG.connectionId)
    REDIS_URL      — Redis URL
"""

from __future__ import annotations

import array
import asyncio
import io
import json
import logging
import os
import tempfile
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import uvicorn
from aw_integration.adapter import run_from_redis
from aw_integration.notetaker_client import NoteTakerClientWrapper
from aw_integration.s3_writer import S3Writer
from fastapi import FastAPI, File, Form, UploadFile
from notetaker_common.s3 import S3Client
from notetaker_common.schemas import BotJob
from prometheus_fastapi_instrumentator import Instrumentator

logger = logging.getLogger(__name__)

_JOB = BotJob.model_validate_json(os.environ["BOT_JOB_JSON"])
_SESSION_UID = os.environ["CONNECTION_ID"]
_REDIS_URL = os.environ.get(
    "REDIS_URL",
    "redis://notetaker-redis-master.notetaker.svc.cluster.local:6379/0",
)

_chunk_store: dict[str, list[bytes]] = {}

# The whole-session WAV from the Node recordingService (one upload, no
# chunk_seq) — the PRIMARY transcription audio. Kept strictly apart from
# _chunk_store, which holds only the 30 s webm backup chunks.
_full_session_store: dict[str, bytes] = {}
_full_session_meta: dict[str, dict[str, Any]] = {}

# Arrival time of the first upload of the session — the only wall clock the
# sidecar has for the capture itself. Used to bound the audio duration.
_first_upload_at: dict[str, datetime] = {}

# Shape the downstream pipeline (run_from_redis -> WhisperX) requires.
_TARGET_CHANNELS = 1
_TARGET_SAMPWIDTH = 2  # s16le
_TARGET_SAMPLE_RATE = 16000

# Payload-integrity tolerance: WAV data size vs. the reported duration_seconds.
# NOTE this pair cannot disagree by construction (services/recording.ts:98 and
# :128 both derive from totalSamples) — see _warn_on_payload_mismatch.
_PAYLOAD_MISMATCH_TOLERANCE = 0.20

# A real-time capture cannot produce more audio than time has elapsed, so audio
# longer than the wall clock means the meeting is in the buffer more than once.
# The grace term absorbs the two known skews: the first upload lands up to one
# 30 s chunk period AFTER capture starts (so the measured span understates the
# capture), and pod clocks are not exact.
_WALL_CLOCK_TOLERANCE = 0.10
_WALL_CLOCK_GRACE_SEC = 60.0

# Digital silence / near-silence on the int16 scale (full scale 32767). A real
# meeting peaks in the thousands; a dead or misrouted sink yields zeros or a few
# LSBs of noise. A peak at or below this means "no sound reached the capture".
_SILENCE_PEAK_THRESHOLD = 64  # ~ -54 dBFS

# Level measurement is sampled, not exhaustive: ~256 windows of 2048 samples.
_LEVEL_SCAN_WINDOWS = 256
_LEVEL_SCAN_WINDOW_SAMPLES = 2048

# Lazily-created S3 client for durable, incremental chunk persistence. Created on
# first use so the module imports without AWS creds (tests, tooling).
_s3_client: S3Client | None = None


def _full_session_s3_key() -> str:
    """S3 key for the whole-session recording.

    Deliberately OUTSIDE ``audio_chunks/`` so the backup-chunk prefix keeps its
    ``chunk_NNNNNN.webm`` sequence intact for recovery tooling (§13.12).
    """
    return f"{_JOB.s3_key}full_session.wav"


def _persist_full_session_to_s3(data: bytes) -> None:
    """Durably copy the whole-session recording to S3. Best-effort, like chunks."""
    global _s3_client
    try:
        if _s3_client is None:
            _s3_client = S3Client()
        _s3_client.put_object(
            key=_full_session_s3_key(), body=data, content_type="audio/wav"
        )
    except Exception as exc:  # noqa: BLE001 - durability is best-effort
        logger.error("failed to persist full-session recording to S3: %s", exc)


def _chunk_s3_key(seq: int) -> str:
    """S3 key for a durable raw audio chunk under the job's prefix.

    Zero-padded so lexical order == capture order (recovery/concat, §13.12).
    """
    return f"{_JOB.s3_key}audio_chunks/chunk_{seq:06d}.webm"


def _persist_chunk_to_s3(seq: int, data: bytes) -> None:
    """Durably copy one raw chunk to S3 the moment it arrives.

    This is the §3 durability fix: captured audio must leave the pod immediately
    so it survives sidecar/pod termination (previously chunks lived only in the
    in-memory ``_chunk_store`` and were lost when the pod died mid-session).

    Best-effort: an S3 error is logged but never breaks audio capture — the
    in-memory buffer + end-of-session pipeline remain the primary path.
    """
    global _s3_client
    try:
        if _s3_client is None:
            _s3_client = S3Client()
        _s3_client.put_object(
            key=_chunk_s3_key(seq), body=data, content_type="audio/webm"
        )
    except Exception as exc:  # noqa: BLE001 - durability is best-effort
        logger.error("failed to persist chunk seq=%s to S3: %s", seq, exc)


_PIPELINE_DONE_SENTINEL = Path("/tmp/pipeline_done")

# Only these callback statuses mean the session is actually over. Vexa also
# posts a "joining" callback at join time; running the end pipeline then
# writes the sentinel early and start.sh tears the sidecar down before the
# real end-of-meeting pipeline runs (§2).
_TERMINAL_STATUSES = {"completed", "failed"}

# Maps Vexa's completion_reason / leave `reason` -> our bot_left_reason (§4).
# Vexa sends the leave reason in `reason` (and sometimes `completion_reason`).
_REASON_MAP: dict[
    str, Literal["host_ended", "last_participant", "hard_deadline", "error"]
] = {
    # completion_reason values
    "left_alone": "last_participant",
    "timeout": "hard_deadline",
    "error": "error",
    # leave `reason` values (meetingFlow.ts)
    "normal_completion": "host_ended",
    "meeting_ended": "host_ended",
    "removed_by_admin": "host_ended",
    "left_alone_timeout": "last_participant",
    "startup_alone_timeout": "last_participant",
    "post_join_setup_error": "error",
    "join_meeting_error": "error",
}

app = FastAPI(title="aw-output-hook")
Instrumentator().instrument(app).expose(app)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/chunks")
async def receive_chunk(
    file: UploadFile = File(...),  # noqa: B008 - FastAPI requires File() in the default
    metadata: str = Form(...),
) -> dict[str, str]:
    # Parse the metadata BEFORE storing anything: which store the payload
    # belongs in depends entirely on whether it carries a chunk_seq. The old
    # order (append first, parse after) is what put the full-session recording
    # into _chunk_store and doubled every transcript.
    meta = json.loads(metadata)
    raw_seq = meta.get("chunk_seq")
    try:
        seq: int | None = int(raw_seq)
    except (TypeError, ValueError):
        seq = None

    data = await file.read()
    _first_upload_at.setdefault(_SESSION_UID, datetime.now(tz=timezone.utc))

    if seq is None:
        return await _receive_full_session(meta, data)

    _chunk_store.setdefault(_SESSION_UID, []).append(data)
    logger.info(
        "chunk seq=%s is_final=%s bytes=%d",
        seq,
        meta.get("is_final"),
        len(data),
    )
    # Durability (§3): persist this chunk to S3 before acking, off the event loop
    # (boto3 is blocking). The empty final-marker upload carries no audio, so
    # skip it rather than writing a zero-byte object.
    if data:
        await asyncio.to_thread(_persist_chunk_to_s3, seq, data)
    return {"status": "ok"}


async def _receive_full_session(meta: dict[str, Any], data: bytes) -> dict[str, str]:
    """Accept the whole-session recording as the primary transcription audio.

    Never enters ``_chunk_store`` and never takes a chunk number — it is a
    different recording of the same meeting, not the next chunk of it.
    """
    logger.info(
        "full-session recording received bytes=%d format=%s "
        "sample_rate=%s channels=%s duration_s=%s",
        len(data),
        meta.get("format"),
        meta.get("sample_rate"),
        meta.get("channels"),
        meta.get("duration_seconds"),
    )
    if not data:
        logger.warning("full-session upload was empty; ignoring")
        return {"status": "ignored"}
    # The contract says a chunk-less upload is the wav full-session recording.
    # Anything else still belongs here rather than in _chunk_store — an upload
    # with no chunk_seq is never a chunk, and appending it to the chunk store IS
    # the doubling bug. MS Teams still carries the legacy blob path and will land
    # here posting the old shape when it ships, so say so loudly instead of
    # quietly reintroducing the bug for it.
    fmt = str(meta.get("format") or "").strip().lower()
    if fmt != "wav":
        logger.error(
            "chunk-less upload declares format=%r, not 'wav' — the /chunks "
            "contract says a full-session recording is wav. Storing it as the "
            "full-session recording anyway (it is NOT a chunk); the S3 backup "
            "keeps the full_session.wav name despite the declared format, and "
            "the primary path will reject it and fall back to the chunks",
            meta.get("format"),
        )
    _full_session_store[_SESSION_UID] = data
    _full_session_meta[_SESSION_UID] = meta
    await asyncio.to_thread(_persist_full_session_to_s3, data)
    return {"status": "ok"}


@app.post("/callback")
async def receive_callback(payload: dict[str, Any]) -> dict[str, str]:
    # Vexa sends the leave reason in `reason`; `completion_reason` is set only in
    # some flows. Prefer completion_reason, fall back to reason (§2 fix — we were
    # reading only completion_reason, which was empty on abrupt ends).
    reason = str(payload.get("completion_reason") or payload.get("reason") or "")
    bot_left_reason = _REASON_MAP.get(reason, "host_ended")
    logger.info(
        "callback status=%s reason=%s -> bot_left_reason=%s",
        payload.get("status"),
        reason,
        bot_left_reason,
    )
    # Surface the bot's own diagnostics on failure (Vexa attaches error_details /
    # error_message on join/leave errors). Without this we debug blind.
    if payload.get("status") == "failed" or payload.get("error_details"):
        logger.error("bot reported failure; callback payload=%s", payload)
    status = str(payload.get("status") or "")
    if status not in _TERMINAL_STATUSES:
        logger.info("callback status=%s (non-terminal); skipping end pipeline", status)
        return {"status": "ok"}
    asyncio.create_task(_run_pipeline_and_signal(bot_left_reason=bot_left_reason))
    # Vexa's unified-callback only accepts: processed | ok | container_updated | ignored.
    return {"status": "ok"}


async def _run_pipeline_and_signal(
    *,
    bot_left_reason: Literal[
        "host_ended", "last_participant", "hard_deadline", "error"
    ],
) -> None:
    """Run the end-of-session pipeline, then drop a sentinel file so start.sh can
    tear down the container only AFTER delivery completes (§2 end-of-meeting
    handling). Always signals — even on failure — so start.sh never hangs.
    """
    try:
        await _run_pipeline(bot_left_reason=bot_left_reason)
    except Exception:
        logger.exception("pipeline failed for session %s", _SESSION_UID)
    finally:
        try:
            _PIPELINE_DONE_SENTINEL.touch()
        except OSError as exc:
            logger.error("failed to write pipeline-done sentinel: %s", exc)


async def _run_pipeline(
    *,
    bot_left_reason: Literal[
        "host_ended", "last_participant", "hard_deadline", "error"
    ],
) -> None:
    session_end = datetime.now(tz=timezone.utc)
    audio_path = await _resolve_transcription_audio(session_end)
    if audio_path is None:
        return

    await run_from_redis(
        session_uid=_SESSION_UID,
        job=_JOB,
        audio_raw_path=audio_path,
        session_end_wall_clock=session_end,
        redis_url=_REDIS_URL,
        bot_left_reason=bot_left_reason,
        s3_writer=S3Writer(),
        notetaker_client=NoteTakerClientWrapper(),
    )
    logger.info("pipeline complete session_uid=%s", _SESSION_UID)


async def _resolve_transcription_audio(session_end: datetime) -> Path | None:
    """Pick the audio to transcribe: full-session recording first, chunks second.

    Returns the raw s16le/16 kHz/mono PCM path ``run_from_redis`` expects, or
    ``None`` when the session captured no audio at all.
    """
    full_session = _full_session_store.get(_SESSION_UID)
    if full_session:
        try:
            return await asyncio.to_thread(
                _pcm_from_full_session_wav,
                full_session,
                _full_session_meta.get(_SESSION_UID) or {},
                session_end,
            )
        except Exception:
            logger.exception(
                "full-session recording for session %s is unusable; "
                "falling back to the backup chunks",
                _SESSION_UID,
            )

    chunks = _chunk_store.get(_SESSION_UID, [])
    if not chunks:
        logger.warning(
            "no audio for session %s (bot never joined/recorded); "
            "skipping conversion + upload",
            _SESSION_UID,
        )
        return None
    if not full_session:
        logger.warning(
            "PRIMARY full-session recording never arrived for session %s; "
            "rebuilding the transcript from %d backup webm chunk(s)",
            _SESSION_UID,
            len(chunks),
        )
    return await _convert_webm_to_pcm(chunks)


def _pcm_from_full_session_wav(
    data: bytes, meta: dict[str, Any], session_end: datetime
) -> Path:
    """Extract raw PCM from the full-session WAV — no ffmpeg on the primary path.

    The recordingService writes s16le/16 kHz/mono already, so the only work is
    dropping the RIFF header. The header length is *parsed* (stdlib ``wave``),
    never assumed to be 44 bytes: parecord/ffmpeg happily emit LIST/INFO chunks
    that would shift the audio if the offset were hard-coded.
    """
    with wave.open(io.BytesIO(data), "rb") as wf:
        channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        framerate = wf.getframerate()
        pcm = wf.readframes(wf.getnframes())

    if not pcm:
        raise ValueError("full-session WAV decoded to zero audio frames")

    if (channels, sampwidth, framerate) != (
        _TARGET_CHANNELS,
        _TARGET_SAMPWIDTH,
        _TARGET_SAMPLE_RATE,
    ):
        logger.error(
            "full-session WAV is NOT s16le/16kHz/mono: channels=%d sampwidth=%d "
            "framerate=%d (expected %d/%d/%d). The pipeline reads it as "
            "s16le/16kHz/mono regardless, so every timestamp will be wrong — "
            "check the recordingService capture flags",
            channels,
            sampwidth,
            framerate,
            _TARGET_CHANNELS,
            _TARGET_SAMPWIDTH,
            _TARGET_SAMPLE_RATE,
        )

    _warn_on_silence(pcm)

    bytes_per_second = framerate * channels * sampwidth
    _warn_on_payload_mismatch(
        pcm_bytes=len(pcm),
        bytes_per_second=bytes_per_second,
        reported_seconds=meta.get("duration_seconds"),
    )
    _warn_on_wall_clock_overrun(
        pcm_bytes=len(pcm),
        bytes_per_second=bytes_per_second,
        session_end=session_end,
    )

    with tempfile.NamedTemporaryFile(suffix=".raw", delete=False) as f:
        f.write(pcm)
        path = Path(f.name)

    # The meeting is now on disk as PCM and in S3 as the original WAV, so the
    # in-memory copy is pure overhead for the rest of the pipeline. Holding it
    # costs ~460 MB on a 4h session — and 4h is the FLOOR of the orchestrator's
    # _active_deadline — against a 6Gi pod shared with Chromium, Node and a
    # 1.5Gi memory-backed /dev/shm.
    _full_session_store.pop(_SESSION_UID, None)
    _full_session_meta.pop(_SESSION_UID, None)
    return path


def _measure_level(pcm: bytes) -> tuple[int, float]:
    """Estimate peak |sample| and RMS of s16le PCM.

    Sampled, not exhaustive. ``audioop`` is the only stdlib C path for this and
    it is REMOVED in Python 3.13, so an exact scan would mean a pure-Python loop
    over ~230M samples for a 4h session. This reads ~256 evenly spaced 2048-
    sample windows instead (~0.5M samples), which is ample to tell a real
    meeting from a dead sink — the only thing the caller claims. It is not a
    precise level meter: a brief sound in an otherwise dead capture can slip
    between the windows.
    """
    total_samples = len(pcm) // 2
    if total_samples == 0:
        return 0, 0.0
    window = min(_LEVEL_SCAN_WINDOW_SAMPLES, total_samples)
    count = max(1, min(_LEVEL_SCAN_WINDOWS, total_samples // window))
    stride = (total_samples - window) // (count - 1) if count > 1 else 0

    view = memoryview(pcm)
    peak = 0
    sum_squares = 0
    counted = 0
    for i in range(count):
        start = i * stride
        samples = array.array("h")
        samples.frombytes(view[start * 2 : (start + window) * 2])
        peak = max(peak, max(samples), -min(samples))
        sum_squares += sum(s * s for s in samples)
        counted += len(samples)
    return peak, (sum_squares / counted) ** 0.5 if counted else 0.0


def _warn_on_silence(pcm: bytes) -> None:
    """Shout when the capture ran but no sound reached it.

    Duration and format are both *correct* when the PulseAudio sink is dead or
    renamed — the WAV is simply full of zeros, and every downstream stage
    reports success while the transcript comes back empty. ``start.sh`` swallows
    its ``pactl`` calls with ``|| true``, so this is a live failure mode, not a
    hypothetical one. Never fatal: a genuinely quiet meeting looks identical and
    the operator, not this function, should make that call.

    The gate is PEAK only, deliberately. RMS is measured and reported because it
    tells the operator whether the capture is dead or merely quiet, but it is not
    part of the test: at any RMS threshold low enough to avoid firing on a quiet
    meeting, a buffer whose peak clears 64 always clears the RMS bar too, so an
    RMS gate here would be a safety net that cannot fire — the exact thing that
    made the old duration check worthless. What this check therefore does NOT
    catch: a capture that is dead apart from one loud transient.
    """
    peak, rms = _measure_level(pcm)
    if peak > _SILENCE_PEAK_THRESHOLD:
        return
    logger.error(
        "full-session audio for session %s is at or near DIGITAL SILENCE "
        "(peak=%d rms=%.1f of 32767 full scale): the capture ran and the "
        "duration is right, but there is no sound in it. Check that "
        "PULSE_SINK=%s still names the sink Chromium plays into — start.sh "
        "swallows its pactl calls with `|| true`, so a renamed or missing sink "
        "produces exactly this. Delivering anyway",
        _SESSION_UID,
        peak,
        rms,
        os.environ.get("PULSE_SINK", "<unset>"),
    )


def _warn_on_payload_mismatch(
    *, pcm_bytes: int, bytes_per_second: int, reported_seconds: Any
) -> None:
    """Check the decoded payload against the duration the upload declared.

    Scope, precisely: this catches a TRUNCATED or corrupt transfer, where fewer
    frames arrive than the header and metadata describe. It CANNOT catch
    doubled audio — ``services/recording.ts`` derives the WAV data size
    (``totalSamples * 2``, :98) and ``duration_seconds``
    (``totalSamples / sampleRate``, :128-130) from the same counter, so if that
    counter ever counted the meeting twice both numbers would double in
    lockstep and the drift would still be zero. The real doubling guard is
    _warn_on_wall_clock_overrun; do not mistake this one for a safety net it
    cannot be.
    """
    if bytes_per_second <= 0:
        return
    actual = pcm_bytes / bytes_per_second
    try:
        expected = float(reported_seconds)
    except (TypeError, ValueError):
        logger.info(
            "full-session upload carried no duration_seconds; "
            "no payload cross-check (audio=%.1fs)",
            actual,
        )
        return
    if expected <= 0:
        return
    drift = abs(actual - expected) / expected
    if drift > _PAYLOAD_MISMATCH_TOLERANCE:
        logger.warning(
            "full-session payload mismatch for session %s: decoded %.1fs of "
            "audio but the upload declared %.1fs (%.0f%% off) — the transfer "
            "was probably truncated",
            _SESSION_UID,
            actual,
            expected,
            drift * 100,
        )


def _warn_on_wall_clock_overrun(
    *, pcm_bytes: int, bytes_per_second: int, session_end: datetime
) -> None:
    """Warn when there is more audio than there was time to record it in.

    This is the doubling guard. A real-time capture cannot produce more audio
    than has elapsed on the clock, so audio materially longer than the span
    from the first upload to the end of the session means the meeting is in the
    buffer more than once. Independent of the bot's own sample counter, which
    is what made the previous check vacuous.

    Warn only — an unexpected value here must never block delivery.
    """
    if bytes_per_second <= 0:
        return
    started_at = _first_upload_at.get(_SESSION_UID)
    if started_at is None:
        return
    elapsed = (session_end - started_at).total_seconds()
    if elapsed <= 0:
        return
    actual = pcm_bytes / bytes_per_second
    if actual <= elapsed + _WALL_CLOCK_GRACE_SEC:
        return
    if actual <= elapsed * (1 + _WALL_CLOCK_TOLERANCE):
        return
    logger.warning(
        "audio outruns the wall clock for session %s: %.1fs of audio but only "
        "%.1fs elapsed since the first upload (%.2fx). A real-time capture "
        "cannot record more audio than time — the meeting is probably in the "
        "buffer more than once",
        _SESSION_UID,
        actual,
        elapsed,
        actual / elapsed,
    )


async def _convert_webm_to_pcm(chunks: list[bytes]) -> Path:
    with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as f:
        for chunk in chunks:
            f.write(chunk)
        webm_path = Path(f.name)

    pcm_path = webm_path.with_suffix(".raw")
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(webm_path),
        "-f",
        "s16le",
        "-acodec",
        "pcm_s16le",
        "-ar",
        "16000",
        "-ac",
        "1",
        str(pcm_path),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    webm_path.unlink(missing_ok=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg exited rc={proc.returncode}")
    return pcm_path


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="info")
