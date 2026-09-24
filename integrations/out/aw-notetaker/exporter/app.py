"""HTTP intake — `POST /hooks/vexa` (spec §4.1).

Verifies Vexa's webhook signature on the raw body before touching JSON,
validates the meeting fields the export job needs before anything is
durably enqueued, hands `meeting.completed` events to the worker in
`queue.py`, and no-ops (200) on any other event type. Enqueue failure is a
503 so Vexa's own delivery retries redeliver it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from exporter import signature
from exporter.config import Settings
from exporter.job import Deps
from exporter.queue import PendingQueue, run_worker

logger = logging.getLogger("exporter")

# The fields export_meeting/naming.folder_name need out of data.meeting;
# an envelope missing any of these is rejected at intake rather than being
# durably enqueued and failing (or being quarantined) later.
_REQUIRED_MEETING_FIELDS = (
    "id",
    "user_id",
    "platform",
    "native_meeting_id",
    "start_time",
)


def _meeting_is_valid(meeting: Mapping[str, Any]) -> bool:
    for field in _REQUIRED_MEETING_FIELDS:
        value = meeting.get(field)
        if value is None:
            return False
        if isinstance(value, str) and not value.strip():
            return False
    return True


def create_app(
    settings: Settings,
    queue: PendingQueue,
    deps: Deps,
    clock: Callable[[], float] = time.time,
    start_worker: bool = True,
) -> FastAPI:
    stop = asyncio.Event()
    worker_task: asyncio.Task[None] | None = None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        nonlocal worker_task
        if start_worker:
            worker_task = asyncio.create_task(run_worker(queue, deps, stop))
        try:
            yield
        finally:
            stop.set()
            if worker_task is not None:
                await worker_task

    app = FastAPI(lifespan=lifespan)

    @app.post("/hooks/vexa")
    async def hooks_vexa(request: Request) -> JSONResponse:
        body = await request.body()
        headers = dict(request.headers)
        if not signature.verify(body, headers, settings.webhook_secret, clock()):
            logger.warning("webhook rejected: invalid signature")
            raise HTTPException(status_code=401, detail="invalid signature")
        try:
            envelope = json.loads(body)
        except ValueError as exc:  # JSONDecodeError and UnicodeDecodeError
            logger.warning("webhook rejected: invalid json")
            raise HTTPException(status_code=400, detail="invalid json") from exc
        if not isinstance(envelope, dict):
            logger.warning("webhook rejected: envelope is not an object")
            raise HTTPException(status_code=400, detail="invalid envelope")
        if envelope.get("event_type") != "meeting.completed":
            return JSONResponse({"status": "ignored"})
        data = envelope.get("data")
        meeting = data.get("meeting") if isinstance(data, dict) else None
        if not isinstance(meeting, dict) or not _meeting_is_valid(meeting):
            logger.warning("webhook rejected: invalid or incomplete data.meeting")
            raise HTTPException(status_code=400, detail="invalid data.meeting")
        try:
            await asyncio.to_thread(queue.enqueue, envelope)
        except Exception as exc:
            logger.error(
                "enqueue failed meeting_id=%s error_class=%s",
                meeting.get("id"),
                type(exc).__name__,
            )
            raise HTTPException(status_code=503, detail="enqueue failed") from exc
        return JSONResponse({"status": "queued"}, status_code=202)

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    return app
