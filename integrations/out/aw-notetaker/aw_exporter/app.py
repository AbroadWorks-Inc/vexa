"""HTTP intake — `POST /hooks/vexa` (spec §4.1).

Verifies Vexa's webhook signature on the raw body before touching JSON,
durably enqueues `meeting.completed` events for the worker in `queue.py`,
and no-ops (200) on any other event type. Enqueue failure is a 503 so
Vexa's own delivery retries redeliver it.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from aw_exporter import signature
from aw_exporter.config import Settings
from aw_exporter.job import Deps
from aw_exporter.queue import PendingQueue, run_worker


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
            raise HTTPException(status_code=401, detail="invalid signature")
        try:
            envelope = json.loads(body)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="invalid json") from exc
        if envelope.get("event_type") != "meeting.completed":
            return JSONResponse({"status": "ignored"})
        meeting = (envelope.get("data") or {}).get("meeting") or {}
        if meeting.get("id") is None:
            raise HTTPException(status_code=400, detail="missing data.meeting.id")
        try:
            queue.enqueue(envelope)
        except Exception as exc:
            raise HTTPException(status_code=503, detail="enqueue failed") from exc
        return JSONResponse({"status": "queued"}, status_code=202)

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    return app
