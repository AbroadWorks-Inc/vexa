"""The export result (§1.9): ``POST /v2/meetings/{id}/export``'s body and its Postgres write.

The exporter reports, through the gateway with a key scoped ``export``, what became of a finished
meeting's export::

    {"state": "handed_off" | "failed", "s3_path": "<the export folder>", "error": "<why>"}

``error`` is optional. ``parse_export(body)`` validates it (``ExportIn``: ``s3_path`` 1-1024
characters, ``error`` at most 2000, no other key, no coercion) into an ``ExportReport``, or raises
``IntakeError("invalid_request")`` naming the first failed field and rule, never the value sent.

``lock_finished_meeting(db, user_id, meeting_id, not_finished)`` is the one lock of a finished
meeting, taken by the export write and by the erase (``reads.PostgresIntakeReads.erase``): the
meeting's link lock, then its row lock (the §1.4 order). A meeting of another account is
``meeting_not_found``; a scheduled or live one is ``meeting_not_finished`` with ``not_finished``
(``EXPORT_NOT_FINISHED`` or ``ERASE_NOT_FINISHED``).

``store_export(db, user_id, meeting_id, report)`` is ``IntakeReads.record_export`` over Postgres,
inside the caller's transaction (``PostgresIntakeReads`` opens one per call): it takes that lock,
then the meeting's ``meeting_aw_state`` lock. The result goes on ``export_state`` /
``export_s3_path`` / ``export_error`` / ``export_at`` and ``export.handed_off`` /
``export.failed`` is written through ``write_event`` in the same transaction; the event id comes
back. A report with the stored state and path writes nothing and returns ``None``, so the exporter
may repeat a report until it is accepted.

SQLAlchemy and the ORM models are imported inside ``store_export``, so the package imports without
SQLAlchemy installed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .adapters import take_link_lock
from .ports import ExportReport, Room
from .rules import FINISHED_STATUSES
from .status import lock_aw_state, lock_meeting, write_event
from .validation import IntakeError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "ERASE_NOT_FINISHED",
    "EXPORT_NOT_FINISHED",
    "NOT_FOUND",
    "ExportIn",
    "lock_finished_meeting",
    "parse_export",
    "store_export",
]

NOT_FOUND = "no such meeting"
EXPORT_NOT_FINISHED = (
    "the meeting hasn't finished; an export result is taken only for a finished meeting"
)
ERASE_NOT_FINISHED = "the meeting hasn't finished; remove its entries or stop it first"


class ExportIn(BaseModel):
    """``POST /v2/meetings/{id}/export``'s body."""

    model_config = ConfigDict(extra="forbid", strict=True)

    state: Literal["handed_off", "failed"]
    s3_path: str = Field(min_length=1, max_length=1024)
    error: Optional[str] = Field(default=None, max_length=2000)


def parse_export(body: Any) -> ExportReport:
    """The body as an ``ExportReport``, or ``invalid_request``."""
    try:
        parsed = ExportIn.model_validate(body)
    except ValidationError as exc:
        err = exc.errors()[0]
        loc = ".".join(str(p) for p in err.get("loc", ()))
        raise IntakeError(
            "invalid_request", f"{loc or '<body>'}: {err.get('msg', 'invalid')}"
        ) from exc
    return ExportReport(parsed.state, parsed.s3_path, parsed.error)


async def lock_finished_meeting(
    db: AsyncSession, user_id: int, meeting_id: int, not_finished: str
) -> Any:
    """Lock ``user_id``'s finished meeting ``meeting_id`` and return its row (see the module
    docstring)."""
    from sqlalchemy import select

    from ..sessions.models import Meeting

    found = (
        await db.execute(
            select(Meeting.platform, Meeting.platform_specific_id).where(
                Meeting.id == meeting_id, Meeting.user_id == user_id
            )
        )
    ).first()
    if found is None:
        raise IntakeError("meeting_not_found", NOT_FOUND)
    await take_link_lock(db, user_id, Room(found[0], found[1]))
    meeting = await lock_meeting(db, meeting_id)
    if meeting is None or meeting.user_id != user_id:
        raise IntakeError("meeting_not_found", NOT_FOUND)
    if meeting.status not in FINISHED_STATUSES:
        raise IntakeError("meeting_not_finished", not_finished)
    return meeting


async def store_export(
    db: AsyncSession, user_id: int, meeting_id: int, report: ExportReport
) -> Optional[str]:
    await lock_finished_meeting(db, user_id, meeting_id, EXPORT_NOT_FINISHED)
    aw = await lock_aw_state(db, meeting_id)
    if (aw.export_state, aw.export_s3_path) == (report.state, report.s3_path):
        return None
    aw.export_state = report.state
    aw.export_s3_path = report.s3_path
    aw.export_error = report.error
    aw.export_at = datetime.now(timezone.utc).replace(microsecond=0)
    await db.flush()
    written = await write_event(db, meeting_id, f"export.{report.state}")
    return written.event_id
