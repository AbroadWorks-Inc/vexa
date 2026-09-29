"""HTTP intake — `POST /hooks/vexa`, the exporter's aw-bots `/v2/webhooks`
subscription (spec §4.1, design §1.9, §2.7).

Verifies the delivery's signature on the raw body before touching JSON
(`X-Webhook-Signature`, or `X-Webhook-Signature-Previous` during a secret
rotation, under `EXPORTER_WEBHOOK_SECRET`), validates the webhook.v1
`MeetingEvent` fields the export job needs before anything is durably
enqueued, hands finished meetings to the worker in `queue.py`, and answers
2xx to every other event (`webhook.test` included) without acting on it.
aw-bots retries a 5xx, so an enqueue failure is a 503; any other refusal is
final.

A finished meeting is `meeting.completed`, or `bot.failed`: a meeting whose
bot recorded part of the call and then failed is still exported (§6.9 F-K2);
the job skips one that has no recording. A `bot.failed` without a
`started_at` never had its bot in the meeting, so it is skipped here, and a
`not_sent` meeting (no bot was ever sent) is never exported.

Delivery is at-least-once: an `event_id` already queued is answered 2xx as a
duplicate. The event is recorded after its meeting is queued, so a failure
between the two is a 503 and aw-bots' retry queues it again.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
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

# The data.meeting fields export_meeting/naming.folder_name need (intake.v1
# Meeting); an envelope missing any of these is rejected at intake rather than
# being durably enqueued and failing (or being quarantined) later.
_REQUIRED_MEETING_FIELDS = ("id", "platform", "room", "started_at")

# webhook.v1 MeetingEvent.event_id; it names the event's marker object.
_EVENT_ID = re.compile(r"^evt_[0-9a-f]{64}$")

_EXPORTED_EVENTS = frozenset({"meeting.completed", "bot.failed"})


def _not_sent(meeting: Mapping[str, Any]) -> bool:
    outcome = meeting.get("outcome")
    return isinstance(outcome, dict) and outcome.get("kind") == "not_sent"


def _meeting_is_valid(meeting: Mapping[str, Any]) -> bool:
    for field in _REQUIRED_MEETING_FIELDS:
        value = meeting.get(field)
        if not isinstance(value, str) or not value.strip():
            return False
    upstream_id = meeting.get("upstream_id")
    return (
        isinstance(upstream_id, int)
        and not isinstance(upstream_id, bool)
        and upstream_id > 0
    )


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
        event_type = envelope.get("event_type")
        if event_type not in _EXPORTED_EVENTS:
            return JSONResponse({"status": "ignored"})
        data = envelope.get("data")
        meeting = data.get("meeting") if isinstance(data, dict) else None
        if isinstance(meeting, dict) and _not_sent(meeting):
            logger.info(
                "not_sent_ignored meeting_id=%s event_type=%s; no bot was sent",
                meeting.get("id"),
                event_type,
            )
            return JSONResponse({"status": "ignored"})
        if (
            event_type == "bot.failed"
            and isinstance(meeting, dict)
            and not meeting.get("started_at")
        ):
            logger.info(
                "bot_failed_skipped meeting_id=%s reason=no_start_time; "
                "nothing recorded",
                meeting.get("id"),
            )
            return JSONResponse({"status": "ignored"})
        if not isinstance(meeting, dict) or not _meeting_is_valid(meeting):
            logger.warning("webhook rejected: invalid or incomplete data.meeting")
            raise HTTPException(status_code=400, detail="invalid data.meeting")
        event_id = envelope.get("event_id")
        if not isinstance(event_id, str) or not _EVENT_ID.match(event_id):
            logger.warning("webhook rejected: invalid event_id")
            raise HTTPException(status_code=400, detail="invalid event_id")
        try:
            if await asyncio.to_thread(queue.seen, event_id):
                logger.info(
                    "duplicate_event event_id=%s meeting_id=%s; ignored",
                    event_id,
                    meeting["id"],
                )
                return JSONResponse({"status": "duplicate"})
            await asyncio.to_thread(queue.enqueue, envelope)
            await asyncio.to_thread(queue.record_event, envelope)
        except Exception as exc:
            logger.error(
                "enqueue failed meeting_id=%s event_id=%s error_class=%s",
                meeting["id"],
                event_id,
                type(exc).__name__,
            )
            raise HTTPException(status_code=503, detail="enqueue failed") from exc
        return JSONResponse({"status": "queued"}, status_code=202)

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    return app
