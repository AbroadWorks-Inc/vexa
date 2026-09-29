"""The entry service's settings (§1.11), each declared in meeting-api's ``config.v1.json``.

``auto_join_lead_s`` is the one reader of the auto-join sweep's ``AUTO_JOIN_LEAD_S`` (whole seconds,
with the sweep's default): the entrypoint hands it to the sweep, and intake and the meeting reads
use it for ``bot_joins_at``.
``join_now_adopt_ahead_s`` is the one reader of ``JOIN_NOW_ADOPT_AHEAD_S``, which a ``join_now``
entry and upstream ``POST /bots`` both adopt by (§1.1 R1, §1.5). ``auto_join_grace_s`` is the one
reader of the sweep's ``AUTO_JOIN_GRACE_S``, with the sweep's default: the entrypoint hands it to
the sweep, and the link resolver ignores a timed, entry-less ``scheduled`` plan past its
``scheduled_at`` plus this grace, which the sweep will never send (§1.6).
``BOT_SEND_MAX_ATTEMPTS`` / ``BOT_SEND_RETRY_BACKOFF_S`` bound the sends of an entry-managed
meeting's bot (§6.9 F-K): that many failed sends in total, this many seconds apart, then the
meeting ends ``not_sent``. ``INTAKE_CONFLICT_RETRIES`` is how many more times an
entry write that lost a constraint race is run again (§6.9 F-D), after a random pause between
``INTAKE_CONFLICT_DELAY_MIN_S`` and ``INTAKE_CONFLICT_DELAY_MAX_S`` times the try's number.
``INTAKE_STOP_LINK_RETRIES`` is how many more times a stop whose meeting moved to another link
between the read and the lock is run again (§1.7). Each of these is read through
``meeting_api.settings``, so a value the service can't run on raises ``SettingsError``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from ..settings import SettingsError, seconds, whole

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
    send_max_attempts: int
    send_retry_backoff_s: int
    conflict_retries: int
    stop_link_retries: int = 1
    conflict_delay_min_s: float = 0.01
    conflict_delay_max_s: float = 0.05

    @classmethod
    def from_env(cls) -> IntakeSettings:
        """The settings from the environment; a value the entry service can't run on raises
        ``SettingsError`` (checked when meeting-api boots)."""
        blocked = os.getenv("ENTRY_BLOCKED_HOSTS", "meet.abroadworks.com")
        delay_min_s = seconds("INTAKE_CONFLICT_DELAY_MIN_S", "0.01", zero=True)
        delay_max_s = seconds("INTAKE_CONFLICT_DELAY_MAX_S", "0.05")
        if delay_min_s > delay_max_s:
            raise SettingsError(
                "INTAKE_CONFLICT_DELAY_MIN_S must not be more than INTAKE_CONFLICT_DELAY_MAX_S"
            )
        return cls(
            max_days_ahead=int(os.getenv("ENTRY_MAX_DAYS_AHEAD", "30")),
            join_now_adopt_ahead_s=join_now_adopt_ahead_s(),
            lead_s=auto_join_lead_s(),
            blocked_hosts=frozenset(
                h.strip().lower() for h in blocked.split(",") if h.strip()
            ),
            max_active_entries=int(os.getenv("INTAKE_MAX_ACTIVE_ENTRIES", "100000")),
            send_max_attempts=whole("BOT_SEND_MAX_ATTEMPTS", "3"),
            send_retry_backoff_s=whole("BOT_SEND_RETRY_BACKOFF_S", "60"),
            conflict_retries=whole("INTAKE_CONFLICT_RETRIES", "3", zero=True),
            stop_link_retries=whole("INTAKE_STOP_LINK_RETRIES", "1", zero=True),
            conflict_delay_min_s=delay_min_s,
            conflict_delay_max_s=delay_max_s,
        )
