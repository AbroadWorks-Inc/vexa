"""The per-account entry-write limit (§1.13).

``PUT /v2/entries`` and ``POST /v2/entries/remove`` share one budget per account: at most
``INTAKE_RATE_LIMIT_PER_MIN`` writes in each fixed 60 s window. The count lives in Redis, so every
gateway replica spends from the same budget. Windows are aligned to wall-clock minutes, which is
what lets replicas agree on which window a write falls in.

``IntakeLimiter`` is the port; ``RedisIntakeLimiter`` is production and ``InMemoryIntakeLimiter``
serves tests. A limiter that cannot count raises ``IntakeUnavailable``: the edge then refuses the
write (503) rather than letting it through uncounted.
"""

from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Protocol, Tuple

WINDOW_SECONDS = 60


class IntakeUnavailable(Exception):
    """The count could not be taken; the write must not go through."""


@dataclass(frozen=True)
class IntakeDecision:
    allowed: bool
    #: whole seconds left in the current window, at least 1
    retry_after: int


class IntakeLimiter(Protocol):
    async def hit(self, account: str) -> IntakeDecision:
        """Count one write for ``account`` and say whether it is within the budget."""
        ...


def _window(now: float) -> Tuple[int, int]:
    """The window ``now`` falls in, and the whole seconds left in it (never 0)."""
    window = int(now // WINDOW_SECONDS)
    left = (window + 1) * WINDOW_SECONDS - now
    return window, max(1, math.ceil(left))


def _check_limit(limit_per_min: int) -> int:
    if limit_per_min < 1:
        raise ValueError("INTAKE_RATE_LIMIT_PER_MIN must be at least 1")
    return limit_per_min


class RedisIntakeLimiter:
    """One counter per account and window, ``INCR`` + ``EXPIRE`` in one ``MULTI``/``EXEC``."""

    def __init__(
        self,
        redis: Any,
        *,
        limit_per_min: int,
        clock: Callable[[], float] = time.time,
    ):
        self._redis = redis
        self.limit_per_min = _check_limit(limit_per_min)
        self._clock = clock

    async def hit(self, account: str) -> IntakeDecision:
        window, left = _window(self._clock())
        key = f"aw:intake:{account}:{window}"
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.incr(key)
                pipe.expire(key, WINDOW_SECONDS)
                count, _ = await pipe.execute()
        except Exception as exc:
            raise IntakeUnavailable(
                f"intake counter unavailable: {type(exc).__name__}"
            ) from exc
        return IntakeDecision(
            allowed=int(count) <= self.limit_per_min, retry_after=left
        )


class InMemoryIntakeLimiter:
    """The same windows and budget, counted in this process only."""

    def __init__(self, *, limit_per_min: int, clock: Callable[[], float] = time.time):
        self.limit_per_min = _check_limit(limit_per_min)
        self._clock = clock
        self._counts: Dict[Tuple[str, int], int] = {}

    async def hit(self, account: str) -> IntakeDecision:
        window, left = _window(self._clock())
        count = self._counts.get((account, window), 0) + 1
        self._counts[(account, window)] = count
        return IntakeDecision(allowed=count <= self.limit_per_min, retry_after=left)

    def count(self, account: str) -> int:
        """Writes counted for ``account`` in the current window."""
        window, _ = _window(self._clock())
        return self._counts.get((account, window), 0)


def from_env(redis: Any) -> RedisIntakeLimiter:
    """The production limiter over the gateway's own Redis client."""
    return RedisIntakeLimiter(
        redis, limit_per_min=int(os.getenv("INTAKE_RATE_LIMIT_PER_MIN", "600"))
    )
