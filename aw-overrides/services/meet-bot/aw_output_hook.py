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
from fastapi import FastAPI, File, Form, UploadFile

from aw_integration.adapter import run_from_redis
from aw_integration.notetaker_client import NoteTakerClientWrapper
from aw_integration.s3_writer import S3Writer
from notetaker_common.schemas import BotJob

logger = logging.getLogger(__name__)

_JOB = BotJob.model_validate_json(os.environ["BOT_JOB_JSON"])
_SESSION_UID = os.environ["CONNECTION_ID"]
_REDIS_URL = os.environ.get(
    "REDIS_URL",
    "redis://notetaker-redis-master.notetaker.svc.cluster.local:6379/0",
)

_chunk_store: dict[str, list[bytes]] = {}

_REASON_MAP: dict[
    str, Literal["host_ended", "last_participant", "hard_deadline", "error"]
] = {
    "left_alone": "last_participant",
    "timeout": "hard_deadline",
    "error": "error",
}

app = FastAPI(title="aw-output-hook")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/chunks")
async def receive_chunk(
    file: UploadFile = File(...),
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
    return {"status": "ok"}


@app.post("/callback")
async def receive_callback(payload: dict[str, Any]) -> dict[str, str]:
    completion_reason = str(payload.get("completion_reason", ""))
    bot_left_reason = _REASON_MAP.get(completion_reason, "host_ended")
    logger.info(
        "callback status=%s completion_reason=%s",
        payload.get("status"),
        completion_reason,
    )
    asyncio.create_task(_run_pipeline(bot_left_reason=bot_left_reason))
    return {"status": "queued"}


async def _run_pipeline(
    *,
    bot_left_reason: Literal[
        "host_ended", "last_participant", "hard_deadline", "error"
    ],
) -> None:
    chunks = _chunk_store.get(_SESSION_UID, [])
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
