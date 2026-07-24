"""FastAPI sidecar for the Vexa meet-bot K8s Job pod.

Receives audio chunks and session-end callbacks from Vexa's TypeScript bot
(on localhost) and drives the aw-integration output pipeline.

Env vars:
    BOT_JOB_JSON   — JSON-encoded BotJob (set by bot-orchestrator job_launcher)
    CONNECTION_ID  — Vexa session UID (= job.job_id = BOT_CONFIG.connectionId)
    REDIS_URL      — Redis URL
"""

from __future__ import annotations

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

# Lazily-created S3 client for durable, incremental chunk persistence. Created on
# first use so the module imports without AWS creds (tests, tooling).
_s3_client: S3Client | None = None


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
    data = await file.read()
    _chunk_store.setdefault(_SESSION_UID, []).append(data)
    meta = json.loads(metadata)
    logger.info(
        "chunk seq=%s is_final=%s bytes=%d",
        meta.get("chunk_seq"),
        meta.get("is_final"),
        len(data),
    )
    # Durability (§3): persist this chunk to S3 before acking, off the event loop
    # (boto3 is blocking). Ordering falls back to arrival index if seq is missing.
    try:
        seq = int(meta.get("chunk_seq"))
    except (TypeError, ValueError):
        seq = len(_chunk_store[_SESSION_UID]) - 1
    await asyncio.to_thread(_persist_chunk_to_s3, seq, data)
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
    chunks = _chunk_store.get(_SESSION_UID, [])
    if not chunks:
        logger.warning(
            "no audio chunks for session %s (bot never joined/recorded); "
            "skipping conversion + upload",
            _SESSION_UID,
        )
        return
    audio_path = await _convert_webm_to_pcm(chunks)

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
