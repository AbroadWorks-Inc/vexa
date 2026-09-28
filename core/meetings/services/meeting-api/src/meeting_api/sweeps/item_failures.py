"""Bounded work for the intake sweeps (§6.9 F-I): pages, and a bounded retry per item.

Every intake sweep reads its work in pages of at most ``SWEEP_BATCH_SIZE`` (200) items, in a stable
order, and runs each item through ``run_item``. An item that raises is recorded against its sweep
(``ItemFailures.failed``), logged with its id, error and stack, and counted in
``aw_sweep_items_total{sweep,result}`` (``failed``); the ``SWEEP_MAX_ITEM_FAILURES``-th (5) failure
gives it up (``given_up``, logged at error level), and the sweep skips it from then on
(``ItemFailures.given_up``). One item's failure never stops the rest of its page. ``run_item``
never raises: a failure that can't even be recorded (the database is down) is logged and the item
is tried again on the next tick, since nothing was counted.

``PostgresItemFailures`` keeps the counts in ``sweep_item_failures`` (one row per sweep and item),
so every replica and every tick shares them; ``InMemoryItemFailures`` is its fake. The row stores
the error's type, never its text: a database error's text can carry the values it was given.

SQLAlchemy is imported inside the Postgres methods, so this module imports without it.
"""

from __future__ import annotations

import os
import traceback
from typing import Any, Awaitable, Callable, Collection, Protocol

from ..metrics import sweep_item
from ..obs import log_event

__all__ = [
    "InMemoryItemFailures",
    "ItemFailures",
    "PostgresItemFailures",
    "run_item",
    "sweep_batch_size",
    "sweep_max_item_failures",
]


def sweep_batch_size() -> int:
    """``SWEEP_BATCH_SIZE``: the most items a sweep reads at once."""
    return int(os.getenv("SWEEP_BATCH_SIZE", "200"))


def sweep_max_item_failures() -> int:
    """``SWEEP_MAX_ITEM_FAILURES``: the failures after which a sweep gives an item up."""
    return int(os.getenv("SWEEP_MAX_ITEM_FAILURES", "5"))


class ItemFailures(Protocol):
    async def failed(self, sweep: str, item_id: str, error: BaseException) -> bool:
        """Record one failure of the item; ``True`` when this one gives it up."""
        ...

    async def given_up(self, sweep: str, item_ids: Collection[str]) -> set[str]:
        """Which of ``item_ids`` the sweep has given up."""
        ...


async def run_item(
    failures: ItemFailures,
    sweep: str,
    item_id: str,
    action: Callable[[], Awaitable[Any]],
    *,
    user_id: Any = None,
) -> bool:
    """Run one item of ``sweep``; ``True`` when it ran to its end (see the module docstring)."""
    try:
        await action()
        return True
    except Exception as exc:
        fields = {
            "sweep": sweep,
            "item_id": item_id,
            "error": type(exc).__name__,
            "traceback": "".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)
            ),
        }
        try:
            gave_up = await failures.failed(sweep, item_id, exc)
        except Exception as record:
            fields["record_error"] = type(record).__name__
            log_event(
                "sweep_item_failure_unrecorded",
                audience="operator",
                level="error",
                span="sweeps",
                user_id=user_id,
                fields=fields,
            )
            return False
        result = "given_up" if gave_up else "failed"
        log_event(
            f"sweep_item_{result}",
            audience="operator",
            level="error" if gave_up else "warning",
            span="sweeps",
            user_id=user_id,
            fields=fields,
        )
        sweep_item(sweep, result)
        return False


class InMemoryItemFailures:
    """``ItemFailures`` over a dict: ``counts[(sweep, item_id)]`` failures, ``gave_up`` the keys
    given up."""

    def __init__(self, *, max_failures: int) -> None:
        self._max = max_failures
        self.counts: dict[tuple[str, str], int] = {}
        self.gave_up: set[tuple[str, str]] = set()

    async def failed(self, sweep: str, item_id: str, error: BaseException) -> bool:
        key = (sweep, item_id)
        self.counts[key] = self.counts.get(key, 0) + 1
        if key in self.gave_up or self.counts[key] < self._max:
            return False
        self.gave_up.add(key)
        return True

    async def given_up(self, sweep: str, item_ids: Collection[str]) -> set[str]:
        return {i for i in item_ids if (sweep, i) in self.gave_up}


class PostgresItemFailures:
    """``ItemFailures`` over ``sweep_item_failures``: one upsert per failure, which stamps
    ``gave_up_at`` on the ``max_failures``-th and reports that one alone."""

    def __init__(self, session_factory: Any, *, max_failures: int) -> None:
        self._session_factory = session_factory
        self._max = max_failures

    async def failed(self, sweep: str, item_id: str, error: BaseException) -> bool:
        from sqlalchemy import text

        stmt = text(
            "WITH before AS (SELECT gave_up_at FROM sweep_item_failures "
            "WHERE sweep = :sweep AND item_id = :item) "
            "INSERT INTO sweep_item_failures AS f (sweep, item_id, failures, last_error, "
            "gave_up_at, updated_at) VALUES (:sweep, :item, 1, :error, "
            "CASE WHEN 1 >= :max THEN now() END, now()) "
            "ON CONFLICT (sweep, item_id) DO UPDATE SET failures = f.failures + 1, "
            "last_error = :error, updated_at = now(), gave_up_at = COALESCE(f.gave_up_at, "
            "CASE WHEN f.failures + 1 >= :max THEN now() END) "
            "RETURNING f.gave_up_at IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM before WHERE gave_up_at IS NOT NULL)"
        )
        async with self._session_factory() as db, db.begin():
            result = await db.execute(
                stmt,
                {
                    "sweep": sweep,
                    "item": item_id,
                    "error": type(error).__name__,
                    "max": self._max,
                },
            )
            return bool(result.scalar_one())

    async def given_up(self, sweep: str, item_ids: Collection[str]) -> set[str]:
        from sqlalchemy import bindparam, text

        if not item_ids:
            return set()
        stmt = text(
            "SELECT item_id FROM sweep_item_failures WHERE sweep = :sweep "
            "AND item_id IN :items AND gave_up_at IS NOT NULL"
        ).bindparams(bindparam("items", expanding=True))
        async with self._session_factory() as db:
            rows = await db.execute(stmt, {"sweep": sweep, "items": list(item_ids)})
            return {str(r[0]) for r in rows}
