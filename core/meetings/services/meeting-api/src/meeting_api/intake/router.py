"""The ``/v2`` meeting intake routes (§2.1), as one router: ``build_intake_router(...)``.

    PUT    /v2/entries                 create or update one entry (``IntakeService.put_entry``)
    POST   /v2/entries/remove          remove one entry (``IntakeService.remove_entry``)
    GET    /v2/entries?user=           the sender's active entries for one user, with content_hash
    GET    /v2/meetings?user=          the meetings a user may see, newest meeting time first
    GET    /v2/meetings/{id}[?user=]   one meeting by UUID
    POST   /v2/meetings/{id}/stop      the bot in the call leaves now (``StopPort``)
    DELETE /v2/meetings/{id}           erase a finished meeting's aw-bots data (§1.13)
    POST   /v2/meetings/{id}/export    the export result (§1.9)

The gateway checks the scope and sets ``x-user-id`` (the account). Every success body is an
``intake.v1`` shape (``Reply``, ``EntryPage``, ``MeetingPage``, ``Meeting``, ``Erased``); every
failure is the §2.5 body ``{"error": {"code", "message"}}``. The error handling is scoped to these
routes by their route class, so a request that fails validation here is a 400 ``invalid_request``
while the upstream routes keep FastAPI's 422. A database that can't be reached, or a write that
lost a race on a unique key (the same entry arriving at once on two links), is a 503
``unavailable``: the client retries.

Visibility for ``user=``: a meeting is visible to a user when one of its entries, in any state,
has that user as its ``user`` or among its ``attendees``. ``GET /v2/meetings/{id}`` without
``user=`` reads any meeting of the account; with it, a meeting the user can't see is
``meeting_not_found``, exactly like another account's UUID.

Erasure runs upstream's completed-artifact deletion first (``collector.app.delete_completed_artifacts``
with the injected deleter: recording objects, then transcript rows). A storage failure there
aborts before any row is removed (503 ``unavailable``). Then one transaction removes the meeting's
delivery rows, outbox rows and entries (``IntakeReads.erase``). The meeting row and
``meeting_aw_state`` stay, and no event is written.

The export result (§1.9) has its body and write in ``export.py`` (``parse_export``,
``IntakeReads.record_export``): it is taken only for a finished meeting of the account
(``meeting_not_found`` otherwise, ``meeting_not_finished`` for a scheduled or live one), stored on
``meeting_aw_state`` and emitted as ``export.handed_off`` / ``export.failed``; the same result again
changes nothing. The reply is the meeting (``intake.v1`` ``Meeting``), whose ``export`` shows it.

Metrics (§1.13): every request moves ``aw_intake_requests_total{route,result,user_id}`` and
``aw_intake_request_seconds``. ``route`` is the method and the path template; ``result`` is an entry
write's ``result``, a failure's §2.5 code (``internal_error`` for an unexpected exception), else
``ok``. Each new export result moves ``aw_export_total{state,user_id}``.
"""

from __future__ import annotations

import re
import time
import traceback
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute

from ..bot_spawn.auto_join import LIVE_STATUSES
from ..collector.app import delete_completed_artifacts
from ..collector.ports import TranscriptStore
from ..metrics import export_recorded, intake_request
from ..obs import log_event
from .export import EXPORT_NOT_FINISHED, parse_export
from .ports import IntakeReads, MeetingQuery, MeetingView, StopPort
from .reads import (
    decode_entry_cursor,
    decode_meeting_cursor,
    encode_entry_cursor,
    encode_meeting_cursor,
    meeting_time,
)
from .rules import FINISHED_STATUSES, is_live
from .service import IntakeService
from .validation import IntakeError

__all__ = ["build_intake_router", "MAX_LIMIT", "DEFAULT_LIMIT", "MEETING_STATUSES"]

#: §2.1: ``limit`` ≤ 200 on the paged reads.
MAX_LIMIT = 200
DEFAULT_LIMIT = 100

#: The statuses a meeting can have, so the ones ``status=`` may name: upstream's planned ones,
#: every live one, and the finished ones.
MEETING_STATUSES = frozenset(
    {"idle", "scheduled", "completed", "failed", *LIVE_STATUSES}
)
#: Errors that mean a bug, not an outage: they stay 500s wherever a failure is otherwise mapped.
_PROGRAMMING_ERRORS = (TypeError, AttributeError, KeyError, AssertionError, NameError)

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_NO_LIVE_BOT = "no bot in this meeting; to cancel it, remove the entry"
_NOT_FINISHED = "the meeting hasn't finished; remove its entries or stop it first"
_NOT_FOUND = "no such meeting"

ArtifactDeleter = Callable[[dict], Awaitable[list[str]]]


def _error(exc: IntakeError) -> JSONResponse:
    headers = (
        {"Retry-After": str(exc.retry_after_s)}
        if exc.retry_after_s is not None
        else None
    )
    return JSONResponse(
        {"error": {"code": exc.code, "message": exc.message}},
        status_code=exc.http_status,
        headers=headers,
    )


def _validation_message(exc: RequestValidationError) -> str:
    """The first failed field and rule, never the value sent."""
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ()) if p != "query")
        return f"{loc or '<request>'}: {err.get('msg', 'invalid')}"
    return "invalid request"


def _retryable(exc: BaseException) -> bool:
    """The database couldn't be reached, or a write lost a race on a unique key."""
    if isinstance(exc, (OSError, TimeoutError)):
        return True
    try:
        from sqlalchemy import exc as sa_exc
    except ImportError:
        return False
    if isinstance(exc, sa_exc.DBAPIError) and exc.connection_invalidated:
        return True
    return isinstance(
        exc,
        (
            sa_exc.OperationalError,
            sa_exc.InterfaceError,
            sa_exc.DisconnectionError,
            sa_exc.TimeoutError,
            sa_exc.IntegrityError,
        ),
    )


class _IntakeRoute(APIRoute):
    """Answers every failure of a ``/v2`` route with the §2.5 body."""

    def get_route_handler(self) -> Callable[[Request], Awaitable[Response]]:
        handler = super().get_route_handler()
        label = f"{','.join(sorted(self.methods))} {self.path}"

        async def route(request: Request) -> Response:
            started = time.monotonic()
            result = "internal_error"
            try:
                response = await handler(request)
                # an entry write leaves its reply's result on request.state
                result = getattr(request.state, "intake_result", "ok")
                return response
            except RequestValidationError as exc:
                result = "invalid_request"
                return _error(IntakeError(result, _validation_message(exc)))
            except IntakeError as exc:
                result = exc.code
                return _error(exc)
            except Exception as exc:
                if not _retryable(exc):
                    raise
                log_event(
                    "intake_unavailable",
                    audience="operator",
                    level="warning",
                    span="meetings.intake",
                    fields={"path": request.url.path, "error": type(exc).__name__},
                )
                result = "unavailable"
                return _error(IntakeError(result, "storage is unavailable; retry"))
            finally:
                intake_request(
                    label,
                    result,
                    request.headers.get("x-user-id"),
                    time.monotonic() - started,
                )

        return route


def _account(x_user_id: Optional[str]) -> int:
    if not x_user_id:
        raise IntakeError("unauthorized", "missing user identity")
    try:
        return int(x_user_id)
    except ValueError as exc:
        raise IntakeError("unauthorized", "invalid user identity") from exc


def _user(value: str) -> str:
    if not _EMAIL.match(value):
        raise IntakeError("invalid_request", "user: must be an email address")
    return value.lower()


def _time(value: Optional[str], field: str) -> Optional[datetime]:
    """An ISO-8601 bound with an offset, as naive UTC (the meeting time's type)."""
    if value is None:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise IntakeError(
            "invalid_request", f"{field}: not a valid ISO-8601 timestamp"
        ) from exc
    if dt.tzinfo is None:
        raise IntakeError(
            "invalid_request",
            f"{field}: naive timestamps are not allowed (an explicit UTC offset is required)",
        )
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


async def _body(request: Request) -> Any:
    try:
        return await request.json()
    except ValueError as exc:
        raise IntakeError("invalid_request", "<body>: not valid JSON") from exc


def _entry_state(entry: Any) -> dict[str, Any]:
    """One ``EntryState`` (intake.v1): the entry's §2.2 fields, ``content_hash`` and ``state``."""
    from .projection import iso_utc

    return {
        "external_id": entry.external_id,
        "user": entry.source_user,
        "meeting_url": entry.meeting_url,
        "start": iso_utc(entry.start),
        "end": iso_utc(entry.end),
        "time_zone": entry.time_zone,
        "title": entry.title,
        "attendees": list(entry.attendees),
        "series_id": entry.series_id,
        "join_now": entry.join_now,
        "metadata": entry.metadata,
        "content_hash": entry.content_hash,
        "state": entry.state,
    }


def build_intake_router(
    service: IntakeService,
    reads: IntakeReads,
    stop: StopPort,
    *,
    artifact_store: TranscriptStore,
    artifact_deleter: Optional[ArtifactDeleter],
    lead_s: int,
) -> APIRouter:
    """The ``/v2`` meeting routes over the entry service, the reads, the stop port, and upstream's
    transcript store and recording-object deleter (for erasure)."""
    router = APIRouter(route_class=_IntakeRoute)

    async def _meeting(
        user_id: int, meeting_id: str, user: Optional[str]
    ) -> MeetingView:
        meeting = await reads.meeting_by_uuid(user_id, meeting_id)
        if meeting is None:
            raise IntakeError("meeting_not_found", _NOT_FOUND)
        if user is not None and not await reads.visible_to(
            user_id, meeting.id, _user(user)
        ):
            raise IntakeError("meeting_not_found", _NOT_FOUND)
        return meeting

    async def _fresh(user_id: int, meeting: MeetingView) -> MeetingView:
        return await reads.meeting_by_uuid(user_id, meeting.uuid) or meeting

    @router.put("/v2/entries")
    async def put_entry(
        request: Request, x_user_id: Optional[str] = Header(default=None)
    ) -> JSONResponse:
        user_id = _account(x_user_id)
        reply = await service.put_entry(user_id, await _body(request))
        request.state.intake_result = reply["result"]
        return JSONResponse(reply)

    @router.post("/v2/entries/remove")
    async def remove_entry(
        request: Request, x_user_id: Optional[str] = Header(default=None)
    ) -> JSONResponse:
        user_id = _account(x_user_id)
        reply = await service.remove_entry(user_id, await _body(request))
        request.state.intake_result = reply["result"]
        return JSONResponse(reply)

    @router.get("/v2/entries")
    async def list_entries(
        user: str = Query(),
        cursor: Optional[str] = Query(default=None),
        limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
        x_user_id: Optional[str] = Header(default=None),
    ) -> JSONResponse:
        user_id = _account(x_user_id)
        after = decode_entry_cursor(cursor) if cursor is not None else None
        found = await reads.entries(user_id, _user(user), after=after, limit=limit + 1)
        page = found[:limit]
        more = len(found) > limit
        return JSONResponse(
            {
                "entries": [_entry_state(e) for e in page],
                "next_cursor": (
                    encode_entry_cursor(page[-1].external_id) if more else None
                ),
            }
        )

    @router.get("/v2/meetings")
    async def list_meetings(
        user: str = Query(),
        start_from: Optional[str] = Query(default=None, alias="from"),
        start_to: Optional[str] = Query(default=None, alias="to"),
        status: Optional[str] = Query(default=None),
        external_id: Optional[str] = Query(default=None),
        cursor: Optional[str] = Query(default=None),
        limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
        x_user_id: Optional[str] = Header(default=None),
    ) -> JSONResponse:
        user_id = _account(x_user_id)
        if status is not None and status not in MEETING_STATUSES:
            raise IntakeError(
                "invalid_request",
                f"status: must be one of {sorted(MEETING_STATUSES)}",
            )
        query = MeetingQuery(
            user=_user(user),
            start_from=_time(start_from, "from"),
            start_to=_time(start_to, "to"),
            status=status,
            external_id=external_id,
            after=decode_meeting_cursor(cursor) if cursor is not None else None,
            limit=limit + 1,
        )
        found = await reads.meetings(user_id, query)
        page = found[:limit]
        last = page[-1] if len(found) > limit else None
        return JSONResponse(
            {
                "meetings": [m.project(lead_s=lead_s) for m in page],
                "next_cursor": (
                    encode_meeting_cursor(meeting_time(last.row), last.id)
                    if last is not None
                    else None
                ),
            }
        )

    @router.get("/v2/meetings/{meeting_id}")
    async def get_meeting(
        meeting_id: str,
        user: Optional[str] = Query(default=None),
        x_user_id: Optional[str] = Header(default=None),
    ) -> JSONResponse:
        user_id = _account(x_user_id)
        meeting = await _meeting(user_id, meeting_id, user)
        return JSONResponse(meeting.project(lead_s=lead_s))

    @router.post("/v2/meetings/{meeting_id}/stop")
    async def stop_meeting(
        meeting_id: str, x_user_id: Optional[str] = Header(default=None)
    ) -> JSONResponse:
        user_id = _account(x_user_id)
        meeting = await _meeting(user_id, meeting_id, None)
        if not is_live(meeting.status):
            raise IntakeError("no_live_bot", _NO_LIVE_BOT)
        # A user's stop records no outcome: the meeting ends with upstream's `stopped` (§1.7).
        await stop.stop_live(user_id, meeting.id, outcome=None)
        return JSONResponse((await _fresh(user_id, meeting)).project(lead_s=lead_s))

    @router.delete("/v2/meetings/{meeting_id}")
    async def erase_meeting(
        meeting_id: str, x_user_id: Optional[str] = Header(default=None)
    ) -> JSONResponse:
        user_id = _account(x_user_id)
        meeting = await _meeting(user_id, meeting_id, None)
        if meeting.status not in FINISHED_STATUSES:
            raise IntakeError("meeting_not_finished", _NOT_FINISHED)
        try:
            artifacts = await delete_completed_artifacts(
                artifact_store, artifact_deleter, user_id, meeting.id
            )
        except HTTPException as exc:
            code = {404: "meeting_not_found", 409: "meeting_not_finished"}.get(
                exc.status_code, "unavailable"
            )
            raise IntakeError(code, str(exc.detail)) from exc
        except _PROGRAMMING_ERRORS:
            raise
        except Exception as exc:
            # Storage first: whatever failed, no row has been removed yet, and the same call can
            # be retried with the original object keys.
            log_event(
                "intake_erase_storage_failed",
                audience="operator",
                level="warning",
                span="meetings.intake.erase",
                user_id=user_id,
                meeting_id=str(meeting.id),
                fields={
                    "error": type(exc).__name__,
                    "traceback": traceback.format_exc(),
                },
            )
            raise IntakeError(
                "unavailable",
                "artifact deletion failed before any row was removed; retry",
            ) from exc
        erased = await reads.erase(user_id, meeting.id)
        log_event(
            "intake_meeting_erased",
            audience="user",
            span="meetings.intake.erase",
            user_id=user_id,
            meeting_id=str(meeting.id),
            fields={
                "objects": artifacts["objects_deleted"],
                "entries": erased.entries,
                "outbox": erased.outbox,
                "deliveries": erased.deliveries,
            },
        )
        return JSONResponse(
            {
                "meeting": (await _fresh(user_id, meeting)).project(lead_s=lead_s),
                "deleted": {
                    "objects": artifacts["objects_deleted"],
                    "entries": erased.entries,
                    "outbox": erased.outbox,
                    "deliveries": erased.deliveries,
                },
            }
        )

    @router.post("/v2/meetings/{meeting_id}/export")
    async def record_export(
        meeting_id: str,
        request: Request,
        x_user_id: Optional[str] = Header(default=None),
    ) -> JSONResponse:
        user_id = _account(x_user_id)
        report = parse_export(await _body(request))
        meeting = await _meeting(user_id, meeting_id, None)
        if meeting.status not in FINISHED_STATUSES:
            raise IntakeError("meeting_not_finished", EXPORT_NOT_FINISHED)
        event_id = await reads.record_export(user_id, meeting.id, report)
        if event_id:
            export_recorded(report.state, user_id)
        log_event(
            "intake_export_recorded" if event_id else "intake_export_unchanged",
            audience="user",
            span="meetings.intake.export",
            user_id=user_id,
            meeting_id=str(meeting.id),
            fields={"state": report.state},
        )
        return JSONResponse((await _fresh(user_id, meeting)).project(lead_s=lead_s))

    return router
