"""The entry service's settings (§1.11), each declared in meeting-api's ``config.v1.json``.

``AUTO_JOIN_LEAD_S`` is the auto-join sweep's own setting: ``auto_join_lead_s`` reads it exactly as
the sweep's entrypoint does, with the sweep's default, and is the one reader intake uses.
``join_now_adopt_ahead_s`` is the one reader of ``JOIN_NOW_ADOPT_AHEAD_S``, which a ``join_now``
entry and upstream ``POST /bots`` both adopt by (§1.1 R1, §1.5). ``auto_join_grace_s`` reads the
sweep's ``AUTO_JOIN_GRACE_S`` with the sweep's default: the link resolver ignores an entry-less
plan past its start plus this grace, which the sweep will never send (§1.6, Ruling R22).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

__all__ = [
    "IntakeSettings",
    "auto_join_grace_s",
    "auto_join_lead_s",
    "join_now_adopt_ahead_s",
]


def auto_join_lead_s() -> int:
    """``AUTO_JOIN_LEAD_S``: the bot is sent this many seconds before the meeting's start."""
    # Imported at call time so bot_spawn can import the intake package without an import cycle.
    from ..bot_spawn.auto_join import DEFAULT_LEAD_S

    return int(float(os.getenv("AUTO_JOIN_LEAD_S", str(DEFAULT_LEAD_S))))


def auto_join_grace_s() -> float:
    """``AUTO_JOIN_GRACE_S``: an entry-less scheduled meeting is never sent later than this many
    seconds after its start."""
    from ..bot_spawn.auto_join import DEFAULT_GRACE_S

    return float(os.getenv("AUTO_JOIN_GRACE_S", str(DEFAULT_GRACE_S)))


def join_now_adopt_ahead_s() -> int:
    """``JOIN_NOW_ADOPT_AHEAD_S``: a pasted link adopts a meeting starting within this many
    seconds."""
    return int(os.getenv("JOIN_NOW_ADOPT_AHEAD_S", "3600"))


@dataclass(frozen=True)
class IntakeSettings:
    max_days_ahead: int
    join_now_adopt_ahead_s: int
    lead_s: int
    blocked_hosts: frozenset[str]
    max_active_entries: int

    @classmethod
    def from_env(cls) -> IntakeSettings:
        blocked = os.getenv("ENTRY_BLOCKED_HOSTS", "meet.abroadworks.com")
        return cls(
            max_days_ahead=int(os.getenv("ENTRY_MAX_DAYS_AHEAD", "30")),
            join_now_adopt_ahead_s=join_now_adopt_ahead_s(),
            lead_s=auto_join_lead_s(),
            blocked_hosts=frozenset(
                h.strip().lower() for h in blocked.split(",") if h.strip()
            ),
            max_active_entries=int(os.getenv("INTAKE_MAX_ACTIVE_ENTRIES", "100000")),
        )
