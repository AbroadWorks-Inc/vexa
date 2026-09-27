"""intake — entry handling, the meeting projection, request validation and the status writer
(§1.1, §1.3, §1.4, §2, §2.4).

Front door (P6): import from here, never a deep module path.

Public surface:
  * ``project_meeting(meeting, aw, entries, *, lead_s)`` — ``projection.py``. Pure: renders one
    aw-bots ``meeting`` object (§2.4) from a ``meetings`` row mapping, a ``meeting_aw_state`` row
    mapping (or ``None``), and the meeting's ``meeting_entries`` row mappings; it lists the closed
    entries of a finished meeting and the active entries of any other (R9). Every reply, read and
    webhook that ships a meeting goes through this one function.
  * ``parse_entry(body, *, now, max_days_ahead)`` / ``parse_remove(body)`` — ``validation.py``.
    The ONLY way a `PUT /v2/entries` / `POST /v2/entries/remove` body becomes a typed, normalised
    ``EntryIn`` / ``RemoveIn``, or raises ``IntakeError`` (§2.5).
  * ``write_status(db, meeting_id, to_status, *, expected_from, ...)`` / ``write_event(db,
    meeting_id, event_type, change)`` — ``status.py``. The ONE writer of ``meetings.status`` and
    the outbox (§1.4): row lock → status → ``meeting_aw_state`` lock + ``event_seq`` → entry
    closing on a finished status → one ``webhook_outbox`` row, all in the caller's transaction.
    ``StatusConflict``, ``Outcome``, ``WrittenEvent``, ``derive_event_id_v2`` and ``row_mapping``
    (an ORM row as the column-name mapping ``project_meeting`` reads) come with it.
  * ``IntakeService(store, spawn, stop, publisher, settings)`` — ``service.py``. The behaviour of
    ``PUT /v2/entries`` (``put_entry``) and ``POST /v2/entries/remove`` (``remove_entry``) under
    the link locks, plus R2's ``merge_into_live`` and R7's ``rerun_entries`` (§1.3). It reaches
    storage, spawning, stopping and publishing only through the ports in ``ports.py``
    (``IntakeStore``/``IntakeTx``, ``SpawnPort``, ``StopPort``, ``EventPublisher``); the
    in-memory fakes are in ``fakes.py``. ``IntakeSettings.from_env()`` — ``settings.py``.
  * ``PostgresIntakeStore(session_factory)`` — ``adapters.py``. The ``IntakeStore`` over Postgres:
    one transaction per ``room_lock`` holding the links' advisory locks (§1.4), status changes and
    events through ``write_status`` / ``write_event`` in that transaction.
"""

from __future__ import annotations

from .adapters import PostgresIntakeStore
from .ports import (
    EntryView,
    EventPublisher,
    IntakeStore,
    IntakeTx,
    MeetingView,
    Room,
    SpawnOutcome,
    SpawnPort,
    StopPort,
)
from .projection import project_meeting
from .service import IntakeService
from .settings import IntakeSettings
from .status import (
    Outcome,
    StatusConflict,
    WrittenEvent,
    derive_event_id_v2,
    row_mapping,
    write_event,
    write_status,
)
from .validation import EntryIn, IntakeError, RemoveIn, parse_entry, parse_remove

__all__ = [
    "IntakeService",
    "IntakeSettings",
    "IntakeStore",
    "IntakeTx",
    "PostgresIntakeStore",
    "EntryView",
    "MeetingView",
    "Room",
    "SpawnOutcome",
    "SpawnPort",
    "StopPort",
    "EventPublisher",
    "project_meeting",
    "EntryIn",
    "RemoveIn",
    "IntakeError",
    "parse_entry",
    "parse_remove",
    "write_status",
    "write_event",
    "StatusConflict",
    "Outcome",
    "WrittenEvent",
    "derive_event_id_v2",
    "row_mapping",
]
