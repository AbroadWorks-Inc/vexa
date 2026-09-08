"""FastAPI sidecar for the Vexa teams-bot K8s Job pod.

Receives audio chunks and session-end callbacks from Vexa's TypeScript bot
(on localhost) and drives the aw-integration output pipeline. This file is
zoom-bot's sidecar, which was written **format-aware** precisely so a teams-bot
could reuse it: Meet/Teams stream WebM from an in-page ``MediaRecorder``,
whereas the Zoom Web bot streams raw PCM (parecord s16le/16 kHz/mono). The S3
object key, the content-type, and the end-of-session assembly all follow the
format the chunk metadata declares, so the only substantive divergence from
zoom-bot is which format the *fallback* assumes.

Why zoom-bot's sidecar and NOT meet-bot's — this is the load-bearing choice:
  * Teams still exposes ``__vexaSaveRecordingBlob`` (``msteams/recording.ts``)
    → ``writeBlob()``, and ``msteams/leave.ts`` flushes **every retained chunk**
    as one blob which ``index.ts`` then POSTs with **NO** ``chunk_seq``. For
    Teams the incremental chunks are the ONLY audio, so accepting that upload
    would deliver the meeting twice.
  * meet-bot's hook **accepts** a chunk-less upload as the *primary* audio,
    because for Meet it is a genuine Node-side ``parec`` WAV from a separate
    capture path. Meet deleted its own in-page blob path for exactly this
    reason ("every Meet transcript contained the meeting twice").
  * This hook **diverts** a chunk-less upload to ``_receive_full_session``,
    which stores it as the playable ``full_session.m4a`` and NEVER touches
    ``_chunk_store``. That still neutralises the doubling bug -- the blob never
    reaches transcription -- without modifying upstream Teams TypeScript. It
    used to be discarded outright; the separation, not the discard, is what
    makes it safe.

Teams-specific behaviours vs. zoom-bot:
  * WebM chunks are concatenated and transcoded via ffmpeg; the raw-PCM
    shortcut is retained but unreachable for Teams in normal operation.
  * ``_normalize_format`` and the assembly fallback default to ``webm``, not
    ``pcm`` (see ``_normalize_format`` for why that is defence, not a live bug).
  * ``_warn_on_silence`` is ported from meet-bot: this platform ships with zero
    upstream tests, and "the pipeline succeeded and the transcript is empty" is
    the failure mode that otherwise looks exactly like success.

Env vars:
    BOT_JOB_JSON   — JSON-encoded BotJob (set by bot-orchestrator job_launcher)
    CONNECTION_ID  — Vexa session UID (= job.job_id = BOT_CONFIG.connectionId)
    REDIS_URL      — Redis URL
"""

from __future__ import annotations

import array
import asyncio
import json
import logging
import os
import tempfile
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

# Format declared by the session's chunks (from chunk metadata). Teams = "webm".
# Recorded so the end-of-session assembly picks the right path. Defaults to
# "webm" for the teams-bot; a PCM-streaming platform overrides it per chunk.
_chunk_format: dict[str, str] = {}

# Lazily-created S3 client for durable, incremental chunk persistence. Created on
# first use so the module imports without AWS creds (tests, tooling).
_s3_client: S3Client | None = None

# Formats carried as raw PCM (no container, no transcode needed for assembly).
_RAW_PCM_FORMATS = {"pcm", "raw", "s16le", "l16"}

# Digital silence / near-silence on the int16 scale (full scale 32767). A real
# meeting peaks in the thousands; a capture attached to the wrong elements, or a
# Teams session that never joined audio, yields zeros or a few LSBs of noise. A
# peak at or below this means "no sound reached the capture".
_SILENCE_PEAK_THRESHOLD = 64  # ~ -54 dBFS

# Level measurement is sampled, not exhaustive: ~256 windows of 2048 samples.
_LEVEL_SCAN_WINDOWS = 256
_LEVEL_SCAN_WINDOW_SAMPLES = 2048


def _normalize_format(fmt: str | None) -> str:
    """Lower-case a declared chunk format, defaulting to Teams' WebM.

    Teams captures in-page via ``MediaRecorder`` (``msteams/recording.ts``),
    not via ``parec``, so WebM — not zoom-bot's raw PCM — is the right default.

    This default is DEFENCE, not a live bug fix. Teams' Node-side bridge derives
    the format from ``recorder.mimeType`` and declares it explicitly on every
    chunk, so in normal operation this fallback is never reached; it is reachable
    only on malformed or absent chunk metadata. It still matters, because the two
    wrong answers are not symmetric: reading Opus-in-WebM as raw PCM produces a
    file that assembles, uploads and transcribes as NOISE rather than raising —
    a silent corruption, whereas the reverse (a transcode attempt on raw PCM)
    fails loudly in ffmpeg.
    """
    return (fmt or "webm").strip().lower()


def _chunk_extension(fmt: str) -> str:
    """File extension for a chunk's S3 key, by declared format."""
    if fmt in _RAW_PCM_FORMATS:
        return "pcm"
    if fmt in {"webm", "wav", "ogg", "m4a"}:
        return fmt
    return "bin"


def _content_type_for(fmt: str) -> str:
    """S3 content-type for a chunk, by declared format."""
    if fmt in _RAW_PCM_FORMATS:
        # RFC 2586: linear 16-bit PCM. Descriptive so recovery tooling (§13.12)
        # can reassemble without guessing sample rate / channel count.
        return "audio/L16;rate=16000;channels=1"
    return {
        "webm": "audio/webm",
        "wav": "audio/wav",
        "ogg": "audio/ogg",
        "m4a": "audio/mp4",
    }.get(fmt, "application/octet-stream")


def _chunk_s3_key(seq: int, fmt: str) -> str:
    """S3 key for a durable raw audio chunk under the job's prefix.

    Zero-padded so lexical order == capture order (recovery/concat, §13.12).
    """
    ext = _chunk_extension(fmt)
    return f"{_JOB.s3_key}audio_chunks/chunk_{seq:06d}.{ext}"


def _persist_chunk_to_s3(seq: int, data: bytes, fmt: str) -> None:
    """Durably copy one raw chunk to S3 the moment it arrives.

    This is the B1 durability fix: captured audio must leave the pod immediately
    so it survives sidecar/pod termination (previously Zoom buffered the whole
    meeting into one local WAV that died with the pod on SIGKILL).

    Best-effort: an S3 error is logged but never breaks audio capture — the
    in-memory buffer + end-of-session pipeline remain the primary path.
    """
    global _s3_client
    try:
        if _s3_client is None:
            _s3_client = S3Client()
        _s3_client.put_object(
            key=_chunk_s3_key(seq, fmt), body=data, content_type=_content_type_for(fmt)
        )
    except Exception as exc:  # noqa: BLE001 - durability is best-effort
        logger.error("failed to persist chunk seq=%s to S3: %s", seq, exc)


# ── The durable, human-playable recording ────────────────────────────────────
#
# `full_session.m4a` is the ONE audio artifact meant for a person to click and
# listen to. Deliberately NOT `audio.wav`: that is the transient working copy the
# worker DELETES after transcription (see portal/src/lib/s3.ts), so it can never
# be linked.
#
# M4A/AAC, not WAV and not WebM:
#   * WAV is what meet-bot stores and it is uncompressed -- 284 MB for one 2.5h
#     meeting, ~55 MB for 30 minutes. That is ~11x the bytes for audio nobody
#     hears differently at speech bitrates.
#   * WebM/Opus is the smallest, but Safari (macOS and especially iOS) is an
#     unresolved compatibility risk, and a silent failure for a Safari user is
#     worse than a few MB.
#   * M4A/AAC plays natively in every browser INCLUDING Safari and iOS, at
#     roughly WebM's size. 48 kbps mono is ample for 16 kHz speech (~10 MB per
#     30 minutes).
#
# Lossy, and for Teams this is a second encode (Opus -> AAC). That costs nothing
# that matters: the transcript is produced from the ORIGINAL audio, never from
# this file. It exists only for playback.
_FULL_AUDIO_OBJECT = "full_session.m4a"
_FULL_AUDIO_CONTENT_TYPE = "audio/mp4"
_FULL_AUDIO_BITRATE = "48k"


def _full_audio_s3_key() -> str:
    return f"{_JOB.s3_key}{_FULL_AUDIO_OBJECT}"


# Streamed to disk in chunks, NEVER read() into memory. The blob is the whole
# meeting: WebM/Opus for Teams (~5 MB per 30 min) but an uncompressed WAV for
# Zoom, where meet-bot measured the equivalent at ~460 MB for a 4h session --
# and 4h is the FLOOR of the orchestrator's activeDeadlineSeconds. Against a 6Gi
# pod shared with Chromium, Node and a 1.5Gi memory-backed /dev/shm, a
# materialised copy of that is not affordable. ffmpeg reads from a file anyway,
# so nothing is gained by holding it in RAM first.
_UPLOAD_STREAM_CHUNK = 1 << 20  # 1 MiB

# ── the playback artifact runs OFF the request path ─────────────────────────
#
# The bot's client (`services/vexa-bot/core/src/services/recording.ts:122`)
# gives this endpoint 30s and, on timeout, DESTROYS the request and re-POSTs the
# ENTIRE blob -- 4 attempts, 1s/2s/4s backoff, then `index.ts` logs the throw and
# moves on. AAC encodes at ~130x realtime, so roughly 65 MINUTES of audio
# exhausts that budget (measured: 4h/461MB -> ~117s). Encoding inside the handler
# therefore meant the handler went quiet, the bot retried, and up to FOUR
# concurrent ffmpeg encodes stacked on one pod.
#
# Nothing about that was self-limiting -- the overlapping uploads run cleanly --
# and while `_chunk_store` is never touched (so the transcript can never be
# doubled), the CONTENTION did reach transcription: on Teams
# `_convert_container_to_pcm` is also ffmpeg under one 3000m CPU limit, and
# `job_launcher.py` sets no ephemeral-storage request or limit at all.
#
# So the reply is sent as soon as the blob is on disk -- seconds, it is
# disk-bound -- and the encode runs in a background task. The reply now means
# ACCEPTED, not STORED.
_pending_full_audio: dict[str, "asyncio.Task[None]"] = {}

# Claimed SYNCHRONOUSLY, before the first await, and released by the task.
# Testing `_pending_full_audio` and then writing it after
# `await _stream_upload_to_temp` is a TOCTOU: three concurrent POSTs all saw an
# empty dict, all ran an encode, and only the LAST was tracked -- so the drain
# waited for one and abandoned two. A predicate evaluated before a lock is not a
# predicate. There is no await between the test and the claim below, which is
# what makes it atomic on a single-threaded event loop.
_full_audio_inflight: set[str] = set()

# start.sh tears the sidecar down once the pipeline sentinel appears, which
# SIGKILLs a running ffmpeg. So the sentinel waits for the encode. Generous for a
# 4h session (~117s measured) without letting a hung ffmpeg pin the pod forever.
_FULL_AUDIO_DRAIN_TIMEOUT_S = 300.0


async def _stream_upload_to_temp(file: UploadFile, suffix: str) -> tuple[Path, int]:
    """Copy an UploadFile to a temp path in bounded chunks. Returns (path, bytes).

    ⚠ `delete=False` means this function OWNS the file until it successfully
    returns it. If the write loop raises -- realistically ENOSPC on `f.write()`,
    which is likeliest under exactly the disk pressure this streaming exists to
    relieve -- the caller's `src, size = await ...` assignment never completes, so
    the caller's own `finally` never starts and the partial file would be orphaned
    for the life of the pod. At Zoom's ~460 MB that could then starve
    `_assemble_audio`/`_convert_container_to_pcm`, which need scratch disk moments
    later to do the REAL transcription work -- turning "the bonus playback file
    failed" into "transcription's own temp write failed too".

    So clean up here and re-raise: ownership transfers only on a clean return.
    """
    total = 0
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        dest = Path(f.name)
        try:
            while True:
                block = await file.read(_UPLOAD_STREAM_CHUNK)
                if not block:
                    break
                f.write(block)
                total += len(block)
        except BaseException:
            dest.unlink(missing_ok=True)
            raise
    return dest, total


async def _encode_playable_audio(src: Path) -> bytes:
    """Transcode one whole-session blob to mono 48 kbps AAC in an MP4 container.

    Input is whatever the bot captured: a WebM/Opus blob for Teams. ffmpeg is
    already in the image for the WebM -> PCM transcription path, so this adds no
    dependency.
    """
    dst = src.with_suffix(".m4a")
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-y",
            "-i",
            str(src),
            "-vn",
            "-ac",
            "1",
            "-c:a",
            "aac",
            "-b:a",
            _FULL_AUDIO_BITRATE,
            str(dst),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                f"ffmpeg exited rc={proc.returncode}: {err.decode()[-400:]}"
            )
        # Off-thread like `_persist_full_audio_to_s3`: this pod also serves
        # /chunks and /healthz, and a synchronous read of the encoded output
        # would stall the event loop for as long as it takes.
        return await asyncio.to_thread(dst.read_bytes)
    finally:
        # `src` belongs to the caller (`_receive_full_session`), which unlinks it
        # in its own finally — cleaning it up here would make the ownership
        # ambiguous and hide a leak if the caller's path ever changed.
        dst.unlink(missing_ok=True)


async def _receive_full_session(
    meta: dict[str, Any], file: UploadFile
) -> dict[str, str]:
    """Store the whole-session blob as the playable artifact -- and NOWHERE else.

    ⚠ THE ONE RULE: this must never touch `_chunk_store`. The chunks are the
    source of truth for TRANSCRIPTION, and this blob is the SAME audio again. The
    original bug that made zoom-bot's hook the template for this file was exactly
    that -- the full-session recording landed in the chunk store and doubled every
    transcript. Hence a separate store, a separate S3 key, and a test that asserts
    the chunk store is untouched.

    Best-effort throughout: a failure here must never break transcription, which
    depends only on the chunks.
    """
    fmt = _normalize_format(meta.get("format"))

    if _SESSION_UID in _full_audio_inflight:
        # A retry, or a second flush. One encode per session is enough: the blob
        # is the same audio and the S3 key is the same, so a second encode would
        # only duplicate the CPU. Answering "ok" is what stops the retry storm.
        logger.info("a full-session encode is already in flight; not starting another")
        return {"status": "ok"}
    _full_audio_inflight.add(_SESSION_UID)  # <- no await above this line

    try:
        src, size = await _stream_upload_to_temp(file, f".{_chunk_extension(fmt)}")
    except Exception as exc:
        # "Best-effort throughout" has to hold here too. The helper has already
        # removed its own partial file; all that is left is to decline rather
        # than 500, so the bot logs a failed upload instead of a server error.
        logger.error("could not stream the full-session upload to disk: %s", exc)
        _full_audio_inflight.discard(_SESSION_UID)
        return {"status": "ignored"}

    if size == 0:
        # The final marker carries no audio. Do not shell out to ffmpeg.
        src.unlink(missing_ok=True)
        _full_audio_inflight.discard(_SESSION_UID)
        logger.info("full-session upload carried no audio; nothing to store")
        return {"status": "ignored"}

    # Ownership of `src` transfers to the task, which unlinks it on every path.
    # The dict holds the only strong reference -- a bare create_task can be
    # garbage-collected mid-run.
    _pending_full_audio[_SESSION_UID] = asyncio.create_task(
        _encode_and_store_full_audio(src, size, fmt)
    )
    logger.info(
        "accepted the full-session blob (%d bytes, format=%s); encoding in the "
        "background so the bot's 30s upload timeout cannot fire",
        size,
        fmt,
    )
    return {"status": "ok"}


async def _encode_and_store_full_audio(src: Path, size: int, fmt: str) -> None:
    """Encode and upload the playback artifact. Owns and always removes `src`.

    Runs detached from the request, so nothing here can propagate to the bot.
    Every failure is logged and swallowed: transcription depends only on the
    chunks, and this artifact is a bonus on top of it.
    """
    try:
        try:
            encoded = await _encode_playable_audio(src)
        except Exception as exc:  # noqa: BLE001 - playback is not the critical path
            logger.error(
                "could not encode the playable recording (%d bytes, format=%s): %s; "
                "transcription is unaffected",
                size,
                fmt,
                exc,
            )
            return

        try:
            await asyncio.to_thread(_persist_full_audio_to_s3, encoded)
        except Exception as exc:  # noqa: BLE001
            logger.error("failed to persist %s: %s", _FULL_AUDIO_OBJECT, exc)
            return

        logger.info(
            "stored %s (%d bytes in, %d bytes out, %.0f%% smaller)",
            _FULL_AUDIO_OBJECT,
            size,
            len(encoded),
            (1 - len(encoded) / size) * 100 if size else 0,
        )
    finally:
        # ALWAYS, on every path including both excepts and cancellation. A leaked
        # temp file here is a whole meeting's audio left on the pod disk.
        src.unlink(missing_ok=True)
        _full_audio_inflight.discard(_SESSION_UID)


async def _drain_full_audio() -> None:
    """Let an in-flight playback encode finish before the teardown sentinel.

    The encode starts when the blob arrives, which is BEFORE the callback, so it
    has been running concurrently with the pipeline -- this waits only for
    whatever is left rather than for the whole encode.

    On timeout the task is cancelled rather than shielded: the sentinel is about
    to be written and start.sh would SIGKILL it moments later anyway, and
    cancelling runs its `finally`, so the temp file is removed instead of
    orphaned.
    """
    task = _pending_full_audio.get(_SESSION_UID)
    if task is None or task.done():
        return
    logger.info("waiting for the playback encode before tearing the sidecar down")
    try:
        await asyncio.wait_for(task, _FULL_AUDIO_DRAIN_TIMEOUT_S)
    except asyncio.TimeoutError:
        logger.error(
            "playback encode still running after %.0fs; abandoning it so teardown "
            "can proceed (transcription is already delivered)",
            _FULL_AUDIO_DRAIN_TIMEOUT_S,
        )
    except Exception:  # noqa: BLE001
        logger.exception("playback encode failed")


def _persist_full_audio_to_s3(data: bytes) -> None:
    global _s3_client
    if _s3_client is None:
        _s3_client = S3Client()
    _s3_client.put_object(
        key=_full_audio_s3_key(), body=data, content_type=_FULL_AUDIO_CONTENT_TYPE
    )


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
    # Admission failures. The bot reached the lobby and was never let in, so no
    # recording exists. Mapped to "error" rather than "host_ended" because the
    # host did NOT end anything -- nobody admitted the bot. `bot_left_reason` is
    # a Literal in notetaker_common.schemas (host_ended | last_participant |
    # hard_deadline | error) and a dedicated "never_admitted" value would be a
    # cross-repo contract change (notetaker-common + the talke worker + portal),
    # so "error" is the honest choice inside the existing contract: the session
    # failed to record. See the WARNING below for the unmapped case.
    "awaiting_admission_timeout": "error",
    "admission_timeout": "error",
    "waiting_room_timeout": "error",
    "removed_from_waiting_room": "error",
    "admission_rejected_by_admin": "error",
    "awaiting_admission_rejected": "error",
    "join_error_page_alive": "error",
    "unknown_blocking_state": "error",
    # Eviction is a human removing the bot -> matches the "removed_by_admin"
    # precedent rather than reading as a fault.
    "evicted": "host_ended",
    # Left unmapped on purpose (the WARNING surfaces them): "stopped" (a
    # deliberate external stop, no Literal fits), and "ambiguous" /
    # "waiting_room_timeout_approaching" (non-terminal, never reach the pipeline).
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
    meta = json.loads(metadata)

    # THE DOUBLING GUARD. The incremental durability stream tags every chunk with
    # a numeric chunk_seq. Teams additionally retains every chunk in-page and, on
    # leave, flushes them as ONE blob through the legacy single-shot
    # RecordingService.upload(), which POSTs it here WITHOUT a chunk_seq. For
    # Teams the incremental chunks are the ONLY audio, so accepting that upload
    # would concatenate the whole meeting on top of itself. So a chunk-less
    # upload is routed AWAY from the chunk store, to `_receive_full_session` --
    # this is the single reason teams-bot reuses zoom-bot's sidecar rather than
    # meet-bot's, which treats a chunk-less upload as PRIMARY audio.
    if "chunk_seq" not in meta:
        # GENUINELY chunk-less: the legacy single-shot RecordingService.upload()
        # omits the key entirely (verified in recording.ts -- its metadata has no
        # chunk_seq field at all, while uploadChunk always sends a real number).
        # ONLY absence counts: a PRESENT `null` -- what JSON.stringify emits for
        # NaN -- is a malformed chunk and falls through to the discard below.
        # This is the whole-session blob, and it is NOT ignored any more: the
        # chunks stay the source of truth for TRANSCRIPTION, and this becomes the
        # playable `full_session.m4a`. `_receive_full_session` never touches
        # `_chunk_store` -- that separation is what neutralises the doubling bug.
        return await _receive_full_session(meta, file)

    raw_seq = meta["chunk_seq"]
    try:
        seq = int(raw_seq)
    except (TypeError, ValueError):
        # PRESENT but unparseable -- a malformed CHUNK, not the blob. These were
        # one branch because both meant "discard"; once the blob stopped being
        # discarded they diverged, and sending a malformed chunk down the blob
        # path would encode a 30-second fragment and PUT it OVER
        # full_session.m4a. `chunk_seq: null` is what JSON.stringify emits for
        # NaN, so this is reachable rather than theoretical.
        logger.warning(
            "dropping a chunk with an unparseable chunk_seq %r; it is a malformed "
            "chunk, not the whole-session blob",
            raw_seq,
        )
        return {"status": "ignored"}

    data = await file.read()
    fmt = _normalize_format(meta.get("format"))
    _chunk_format[_SESSION_UID] = fmt
    _chunk_store.setdefault(_SESSION_UID, []).append(data)
    logger.info(
        "chunk seq=%s is_final=%s bytes=%d format=%s",
        seq,
        meta.get("is_final"),
        len(data),
        fmt,
    )
    # Durability (§3): persist this chunk to S3 before acking, off the event loop
    # (boto3 is blocking). Empty final-marker chunks carry no audio, so skip them.
    #
    # Teams' in-page finalizer DOES emit such a marker — `{base64: "", chunkSeq,
    # isFinal: true}` — but the Node bridge drops empty buffers (`if
    # (!buf.length) return false`) BEFORE the POST, so in practice the marker
    # should not reach this endpoint at all. The `if data:` guard is kept and
    # pinned by a test regardless: it costs nothing, and if the bridge's guard
    # ever moves, an empty S3 object is a worse outcome than a skipped one.
    if data:
        await asyncio.to_thread(_persist_chunk_to_s3, seq, data, fmt)
    return {"status": "ok"}


@app.post("/callback")
async def receive_callback(payload: dict[str, Any]) -> dict[str, str]:
    # Vexa sends the leave reason in `reason`; `completion_reason` is set only in
    # some flows. Prefer completion_reason, fall back to reason (§2 fix — we were
    # reading only completion_reason, which was empty on abrupt ends).
    reason = str(payload.get("completion_reason") or payload.get("reason") or "")
    # DEFAULT IS "error", NOT "host_ended".
    #
    # This used to default to "host_ended", which meant every unmapped reason --
    # and every callback with no reason at all -- claimed the host ended the
    # meeting. Live on 2026-09-04 a bot that was never admitted reported
    # `completed / awaiting_admission_timeout` and the row read "host_ended", so
    # the portal would have told the user the host hung up when the truth was
    # "nobody let the bot in". A confident wrong reason is worse than an honest
    # unknown: it sends whoever is debugging to the wrong place entirely.
    #
    # "error" is the least-wrong value in the existing Literal for "we do not
    # know why this ended", and it is loud rather than plausible.
    # Surface the bot's own diagnostics on failure (Vexa attaches error_details /
    # error_message on join/leave errors). Without this we debug blind.
    if payload.get("status") == "failed" or payload.get("error_details"):
        logger.error("bot reported failure; callback payload=%s", payload)

    status = str(payload.get("status") or "")
    if status not in _TERMINAL_STATUSES:
        logger.info(
            "callback status=%s reason=%r (non-terminal); skipping end pipeline",
            status,
            reason,
        )
        return {"status": "ok"}

    # Only NOW map the reason. Every callback used to be mapped here, including
    # the non-terminal ones -- `joining`, `awaiting_admission`, `active` -- which
    # legitimately carry no reason at all. So a healthy meeting logged three
    # UNMAPPED warnings telling the reader to "add it to _REASON_MAP", when there
    # was nothing to add and the value was discarded two lines later by the
    # terminal check anyway. Proven live on 2026-09-08. A warning that cries wolf
    # on every run is worse than no warning: it trains people to skip the log.
    bot_left_reason = _REASON_MAP.get(reason, "error")
    if reason not in _REASON_MAP:
        logger.warning(
            "UNMAPPED leave reason %r (status=%s); defaulting bot_left_reason=error. "
            "Add it to _REASON_MAP so the row says something true.",
            reason,
            status,
        )
    logger.info(
        "callback status=%s reason=%s -> bot_left_reason=%s",
        status,
        reason,
        bot_left_reason,
    )
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
        # Before the sentinel, ALWAYS -- even if the pipeline failed. The
        # playback artifact is independent of transcription, and start.sh
        # SIGKILLs the sidecar once this sentinel appears.
        await _drain_full_audio()
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
    chunks = _chunk_store.get(_SESSION_UID, [])
    if not chunks:
        logger.warning(
            "no audio chunks for session %s (bot never joined/recorded); "
            "skipping conversion + upload",
            _SESSION_UID,
        )
        return
    fmt = _chunk_format.get(_SESSION_UID, "webm")
    audio_path = await _assemble_audio(chunks, fmt)
    _warn_on_silence(audio_path)

    await run_from_redis(
        session_uid=_SESSION_UID,
        job=_JOB,
        audio_raw_path=audio_path,
        session_end_wall_clock=datetime.now(tz=timezone.utc),
        redis_url=_REDIS_URL,
        bot_left_reason=bot_left_reason,
        s3_writer=S3Writer(),
        notetaker_client=NoteTakerClientWrapper(),
    )
    logger.info("pipeline complete session_uid=%s", _SESSION_UID)


async def _assemble_audio(chunks: list[bytes], fmt: str) -> Path:
    """Produce the s16le/16 kHz/mono raw PCM path the adapter expects.

    Container formats (Teams/Meet WebM) — the Teams path — are concatenated and
    transcoded via ffmpeg. Raw-PCM formats (Zoom) are already in the target
    shape, so concatenating the chunks IS the raw audio and no transcode is
    needed; that branch is retained rather than deleted because the format is
    read from chunk metadata, not hardcoded per platform.
    """
    if fmt in _RAW_PCM_FORMATS:
        with tempfile.NamedTemporaryFile(suffix=".raw", delete=False) as f:
            for chunk in chunks:
                f.write(chunk)
            return Path(f.name)
    return await _convert_container_to_pcm(chunks, fmt)


async def _convert_container_to_pcm(chunks: list[bytes], fmt: str) -> Path:
    suffix = f".{_chunk_extension(fmt)}"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        for chunk in chunks:
            f.write(chunk)
        container_path = Path(f.name)

    pcm_path = container_path.with_suffix(".raw")
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(container_path),
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
    container_path.unlink(missing_ok=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg exited rc={proc.returncode}")
    return pcm_path


def _measure_level(pcm: bytes) -> tuple[int, float]:
    """Estimate peak |sample| and RMS of s16le PCM.

    Sampled, not exhaustive. ``audioop`` is the only stdlib C path for this and
    it is REMOVED in Python 3.13, so an exact scan would mean a pure-Python loop
    over ~230M samples for a 4h session. This reads ~256 evenly spaced 2048-
    sample windows instead (~0.5M samples), which is ample to tell a real
    meeting from a dead capture — the only thing the caller claims. It is not a
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


def _sample_pcm_file(path: Path) -> bytes:
    """Read evenly spaced windows of an s16le PCM file, up to the scan budget.

    meet-bot measures its level on a buffer it already holds in memory. Here the
    assembled audio only exists on disk, and reading it whole would mean a
    ~460 MB transient for a 4h session — on a 6 Gi pod that is also running
    Chromium, Node and a 1.5 Gi memory-backed /dev/shm, while the WebM chunks are
    still referenced by the caller. So the sampling happens at read time.

    The budget handed back is exactly ``_LEVEL_SCAN_WINDOWS *
    _LEVEL_SCAN_WINDOW_SAMPLES`` samples, which is the point: ``_measure_level``
    then computes ``stride == window`` and scans that buffer with no gaps and no
    overlap, so nothing read here is thrown away. Coverage of the underlying file
    is therefore the same ~256-window sample meet-bot takes, at ~1 MB of memory.

    Returns ``b""`` on any read failure — the caller must never be able to fail a
    delivery over a diagnostic.
    """
    budget_samples = _LEVEL_SCAN_WINDOWS * _LEVEL_SCAN_WINDOW_SAMPLES
    try:
        size = path.stat().st_size
        total_samples = size // 2
        if total_samples <= budget_samples:
            return path.read_bytes()
        window = _LEVEL_SCAN_WINDOW_SAMPLES
        count = _LEVEL_SCAN_WINDOWS
        stride = (total_samples - window) // (count - 1)
        out = bytearray()
        with path.open("rb") as fh:
            for i in range(count):
                fh.seek(i * stride * 2)
                out += fh.read(window * 2)
        return bytes(out)
    except OSError as exc:
        logger.error("could not read %s to measure audio level: %s", path, exc)
        return b""


def _warn_on_silence(audio_path: Path) -> None:
    """Shout when the capture ran but no sound reached it.

    Duration and format are both *correct* when the capture attached to the
    wrong thing — the audio is simply full of zeros, and every downstream stage
    reports success while the transcript comes back empty. This matters more for
    Teams than for any other platform: ``platforms/msteams/`` ships with zero
    tests, so "the pipeline succeeded and the transcript is empty" is the failure
    that will actually be hit, and it is indistinguishable from success without
    this line.

    The diagnosis is deliberately NOT meet-bot's. For Meet, digital silence means
    a dead or renamed PulseAudio sink, because Meet captures node-side from
    ``${PULSE_SINK}.monitor``. Teams captures IN-PAGE via ``MediaRecorder`` over
    the elements ``audioService.findMediaElements()`` returns, so the sink is not
    even in the path and an operator sent to check ``pactl`` would be looking in
    the wrong place.

    Never fatal: a genuinely quiet meeting looks identical and the operator, not
    this function, should make that call.

    The gate is PEAK only, deliberately. RMS is measured and reported because it
    tells the operator whether the capture is dead or merely quiet, but it is not
    part of the test: at any RMS threshold low enough to avoid firing on a quiet
    meeting, a buffer whose peak clears 64 always clears the RMS bar too, so an
    RMS gate here would be a safety net that cannot fire. What this check
    therefore does NOT catch: a capture that is dead apart from one loud
    transient.
    """
    pcm = _sample_pcm_file(audio_path)
    if not pcm:
        return
    peak, rms = _measure_level(pcm)
    if peak > _SILENCE_PEAK_THRESHOLD:
        return
    logger.error(
        "assembled audio for session %s is at or near DIGITAL SILENCE "
        "(peak=%d rms=%.1f of 32767 full scale): the capture ran and the "
        "duration is right, but there is no sound in it. Teams records IN-PAGE, "
        "so do NOT start with pactl — check (1) that "
        "audioService.findMediaElements() attached to elements that actually "
        "carry audio (Teams injects <audio> elements on join; if the recorder "
        "bound before they existed it records a stream with no sources), and "
        "(2) that Teams joined audio at all — start.sh reports the bot mic as "
        "`bot mic VERIFIED muted` when it is in force and `MIC FIX NOT IN "
        "FORCE` when Chromium got no capture device, and with no capture device "
        "Teams may never establish an audio session in either direction. "
        "Delivering anyway",
        _SESSION_UID,
        peak,
        rms,
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="info")
