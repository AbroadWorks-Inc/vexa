"""intake — the meeting projection and request validation (§2, §2.4, §1.1).

Front door (P6): import from here, never a deep module path.

Public surface:
  * ``project_meeting(meeting, aw, entries, *, lead_s)`` — ``projection.py``. Pure: renders one
    aw-bots ``meeting`` object (§2.4) from a ``meetings`` row mapping, a ``meeting_aw_state`` row
    mapping (or ``None``), and the meeting's active ``meeting_entries`` row mappings. Every reply,
    read and webhook that ships a meeting goes through this one function.
  * ``parse_entry(body, *, now, max_days_ahead)`` / ``parse_remove(body)`` — ``validation.py``.
    The ONLY way a `PUT /v2/entries` / `POST /v2/entries/remove` body becomes a typed, normalised
    ``EntryIn`` / ``RemoveIn``, or raises ``IntakeError`` (§2.5).
"""

from __future__ import annotations

from .projection import project_meeting
from .validation import EntryIn, IntakeError, RemoveIn, parse_entry, parse_remove

__all__ = [
    "project_meeting",
    "EntryIn",
    "RemoveIn",
    "IntakeError",
    "parse_entry",
    "parse_remove",
]
