"""intake — the one meeting projection (§2.4, §1.1).

Front door (P6): import from here, never a deep module path.

Public surface:
  * ``project_meeting(meeting, aw, entries, *, lead_s)`` — ``projection.py``. Pure: renders one
    aw-bots ``meeting`` object (§2.4) from a ``meetings`` row mapping, a ``meeting_aw_state`` row
    mapping (or ``None``), and the meeting's active ``meeting_entries`` row mappings. Every reply,
    read and webhook that ships a meeting goes through this one function.
"""

from __future__ import annotations

from .projection import project_meeting

__all__ = ["project_meeting"]
