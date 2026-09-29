"""Bounded work for the intake sweeps (§6.9 F-I): pages, and a bounded retry per item.

Every intake sweep reads its work in pages of at most ``SWEEP_BATCH_SIZE`` (200) items, in a stable
order (``run_pages``, the one paged loop), and runs each item through ``run_item``. An item that raises is recorded against its sweep
(``ItemFailures.failed``), logged with its id, error and stack, and counted in
``aw_sweep_items_total{sweep,result}`` (``failed``); the ``SWEEP_MAX_ITEM_FAILURES``-th (5) failure
gives it up (``given_up``, logged at error level), and the sweep skips it from then on
(``ItemFailures.given_up``). An action bounded by the age of its item instead raises
``ItemExpired`` past that age, which gives the item up at once (``ItemFailures.give_up``). An
action that couldn't get an answer for a reason that isn't the item's failure (a dependency that
didn't answer) raises ``ItemDeferred``: logged at warning level and counted under its own
``result``, it records nothing, and the item runs again on the next pass. One item's failure never
stops the rest of its page. A sweep whose given-up item must still end (a meeting that would
otherwise stay open for good) passes ``on_given_up``, run once, right after the give-up; its own
failure is logged. ``run_item`` never raises: a failure that can't even be recorded (the
database is down) is logged and the item is tried again on the next tick, since nothing was
counted.

``PostgresItemFailures`` keeps the counts in ``sweep_item_failures`` (one row per sweep and item),
so every replica and every tick shares them; ``InMemoryItemFailures`` is its fake. The row stores
the error's type, never its text: a database error's text can carry the values it was given.
``prune_item_failures`` deletes the records untouched for ``SWEEP_ITEM_FAILURES_RETENTION_S``
(7 days), a page of ``SWEEP_BATCH_SIZE`` at a time; the reconcile sweep runs it every pass. A
given-up item is never brought back while it is pending: every read that finds it given up
(``ItemFailures.given_up``, which its sweep makes whenever it lists the item) touches its record,
so only the given-up items no sweep has listed for the retention go (their meeting is gone or
finished, or their work was cleared), with the records of items that stopped failing.

SQLAlchemy is imported inside the Postgres methods, so this module imports without it.
"""

from __future__ import annotations

import time
import traceback
from functools import partial
from typing import (
    Any,
    Awaitable,
    Callable,
    Collection,
    Optional,
    Protocol,
    Sequence,
    TypeVar,
)

from ..metrics import sweep_item
from ..obs import log_event
from ..settings import seconds, whole

__all__ = [
    "InMemoryItemFailures",
    "ItemDeferred",
    "ItemExpired",
    "ItemFailures",
    "PostgresItemFailures",
    "prune_item_failures",
    "run_item",
    "run_pages",
    "sweep_batch_size",
    "sweep_item_failures_retention_s",
    "sweep_max_item_failures",
]


def sweep_batch_size() -> int:
    """``SWEEP_BATCH_SIZE``: the most items a sweep reads at once."""
    return whole("SWEEP_BATCH_SIZE", "200")


def sweep_max_item_failures() -> int:
    """``SWEEP_MAX_ITEM_FAILURES``: the failures after which a sweep gives an item up."""
    return whole("SWEEP_MAX_ITEM_FAILURES", "5")


def sweep_item_failures_retention_s() -> float:
    """``SWEEP_ITEM_FAILURES_RETENTION_S``: how long a failure record untouched is kept."""
    return seconds("SWEEP_ITEM_FAILURES_RETENTION_S", "604800")


class ItemDeferred(Exception):
    """An item's action got no answer, for a reason that isn't the item's failure: ``run_item``
    logs it at warning level and counts it as ``result``; nothing is recorded."""

    def __init__(self, result: str, message: str) -> None:
        super().__init__(message)
        self.result = result


class ItemExpired(Exception):
    """An item past its sweep's age bound: ``run_item`` gives it up at once."""


class ItemFailures(Protocol):
    async def failed(self, sweep: str, item_id: str, error: BaseException) -> bool:
        """Record one failure of the item; ``True`` when this one gives it up."""
        ...

    async def give_up(self, sweep: str, item_id: str, error: BaseException) -> bool:
        """Record one failure of the item and give it up; ``True`` unless it was already."""
        ...

    async def given_up(self, sweep: str, item_ids: Collection[str]) -> set[str]:
        """Which of ``item_ids`` the sweep has given up; each one's record is touched, since
        its sweep still lists it (so ``prune`` keeps it)."""
        ...

    async def prune(self, *, older_than_s: float, limit: int) -> int:
        """Delete the records untouched for ``older_than_s``, ``limit`` at a time; how many."""
        ...


async def prune_item_failures(failures: ItemFailures, *, log: Any) -> int:
    """Delete the failure records untouched for ``SWEEP_ITEM_FAILURES_RETENTION_S``, a page of
    ``SWEEP_BATCH_SIZE`` at a time. Never raises (a failure is logged); returns how many.
    """
    try:
        return await failures.prune(
            older_than_s=sweep_item_failures_retention_s(), limit=sweep_batch_size()
        )
    except Exception:
        log.exception("sweep_item_failures prune failed")
        return 0


async def run_item(
    failures: ItemFailures,
    sweep: str,
    item_id: str,
    action: Callable[[], Awaitable[Any]],
    *,
    user_id: Any = None,
    on_given_up: Optional[Callable[[BaseException], Awaitable[Any]]] = None,
) -> bool:
    """Run one item of ``sweep``; ``True`` when it ran to its end (see the module docstring)."""
    try:
        await action()
        return True
    except ItemDeferred as exc:
        log_event(
            f"sweep_item_{exc.result}",
            audience="operator",
            level="warning",
            span="sweeps",
            user_id=user_id,
            fields={"sweep": sweep, "item_id": item_id, "reason": str(exc)},
        )
        sweep_item(sweep, exc.result)
        return False
    except Exception as exc:
        fields = {
            "sweep": sweep,
            "item_id": item_id,
            "error": type(exc).__name__,
            "traceback": "".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)
            ),
        }
        record = failures.give_up if isinstance(exc, ItemExpired) else failures.failed
        try:
            gave_up = await record(sweep, item_id, exc)
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
        if gave_up and on_given_up is not None:
            try:
                await on_given_up(exc)
            except Exception as after:
                log_event(
                    "sweep_item_give_up_action_failed",
                    audience="operator",
                    level="error",
                    span="sweeps",
                    user_id=user_id,
                    fields={
                        "sweep": sweep,
                        "item_id": item_id,
                        "error": type(after).__name__,
                        "traceback": "".join(
                            traceback.format_exception(
                                type(after), after, after.__traceback__
                            )
                        ),
                    },
                )
        return False


T = TypeVar("T")


def _every(page: Sequence[T]) -> Sequence[T]:
    return page


async def run_pages(
    failures: ItemFailures,
    sweep: str,
    read_page: Callable[[Any], Awaitable[Sequence[T]]],
    *,
    limit: int,
    after_of: Callable[[T], Any],
    item_id_of: Callable[[T], str],
    user_id_of: Callable[[T], Any],
    action: Callable[[T], Awaitable[Any]],
    select: Callable[[Sequence[T]], Sequence[T]] = _every,
    on_given_up: Optional[Callable[[T, BaseException], Awaitable[Any]]] = None,
) -> None:
    """Run ``sweep`` over all its work, a page at a time: ``read_page(after)`` reads at most
    ``limit`` items in a stable order after the cursor ``after`` (``None`` first, then
    ``after_of`` of the page's last item), until a short page. Each item of ``select(page)`` the
    sweep hasn't given up runs ``action(item)`` through ``run_item`` as ``item_id_of(item)``, and
    ``on_given_up(item, error)`` when that run gives it up. A failing read raises."""
    after: Any = None
    while True:
        page = await read_page(after)
        work = select(page)
        skip = await failures.given_up(sweep, [item_id_of(item) for item in work])
        for item in work:
            item_id = item_id_of(item)
            if item_id not in skip:
                await run_item(
                    failures,
                    sweep,
                    item_id,
                    partial(action, item),
                    user_id=user_id_of(item),
                    on_given_up=(
                        None if on_given_up is None else partial(on_given_up, item)
                    ),
                )
        if len(page) < limit:
            return
        after = after_of(page[-1])


class InMemoryItemFailures:
    """``ItemFailures`` over a dict: ``counts[(sweep, item_id)]`` failures, ``gave_up`` the keys
    given up."""

    def __init__(self, *, max_failures: int) -> None:
        self._max = max_failures
        self.counts: dict[tuple[str, str], int] = {}
        self.gave_up: set[tuple[str, str]] = set()
        self.updated: dict[tuple[str, str], float] = {}

    async def failed(self, sweep: str, item_id: str, error: BaseException) -> bool:
        key = (sweep, item_id)
        self.counts[key] = self.counts.get(key, 0) + 1
        self.updated[key] = time.time()
        if key in self.gave_up or self.counts[key] < self._max:
            return False
        self.gave_up.add(key)
        return True

    async def give_up(self, sweep: str, item_id: str, error: BaseException) -> bool:
        key = (sweep, item_id)
        self.counts[key] = self.counts.get(key, 0) + 1
        self.updated[key] = time.time()
        if key in self.gave_up:
            return False
        self.gave_up.add(key)
        return True

    async def given_up(self, sweep: str, item_ids: Collection[str]) -> set[str]:
        found = {i for i in item_ids if (sweep, i) in self.gave_up}
        for item_id in found:
            self.updated[(sweep, item_id)] = time.time()
        return found

    async def prune(self, *, older_than_s: float, limit: int) -> int:
        cutoff = time.time() - older_than_s
        old = sorted(k for k, at in self.updated.items() if at < cutoff)
        for key in old:
            self.counts.pop(key, None)
            self.gave_up.discard(key)
            self.updated.pop(key, None)
        return len(old)


class PostgresItemFailures:
    """``ItemFailures`` over ``sweep_item_failures``: one upsert per failure, which stamps
    ``gave_up_at`` on the ``max_failures``-th (on the first, for ``give_up``) and reports that one
    alone."""

    def __init__(self, session_factory: Any, *, max_failures: int) -> None:
        self._session_factory = session_factory
        self._max = max_failures

    async def failed(self, sweep: str, item_id: str, error: BaseException) -> bool:
        return await self._record(sweep, item_id, error, self._max)

    async def give_up(self, sweep: str, item_id: str, error: BaseException) -> bool:
        return await self._record(sweep, item_id, error, 1)

    async def _record(
        self, sweep: str, item_id: str, error: BaseException, give_up_at: int
    ) -> bool:
        """One failure; the item is given up once its count reaches ``give_up_at``."""
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
                    "max": give_up_at,
                },
            )
            return bool(result.scalar_one())

    async def given_up(self, sweep: str, item_ids: Collection[str]) -> set[str]:
        from sqlalchemy import bindparam, text

        if not item_ids:
            return set()
        stmt = text(
            "UPDATE sweep_item_failures SET updated_at = now() WHERE sweep = :sweep "
            "AND item_id IN :items AND gave_up_at IS NOT NULL RETURNING item_id"
        ).bindparams(bindparam("items", expanding=True))
        async with self._session_factory() as db, db.begin():
            rows = await db.execute(stmt, {"sweep": sweep, "items": list(item_ids)})
            return {str(r[0]) for r in rows}

    async def prune(self, *, older_than_s: float, limit: int) -> int:
        """One ``DELETE`` of at most ``limit`` records untouched since the cutoff per page, each
        page in its own transaction, until a short page. The cutoff is fixed first, so a record
        written meanwhile is never taken and the pages end."""
        from sqlalchemy import text

        stmt = text(
            "DELETE FROM sweep_item_failures WHERE (sweep, item_id) IN ("
            "SELECT sweep, item_id FROM sweep_item_failures WHERE updated_at < :cutoff "
            "ORDER BY sweep, item_id LIMIT :limit)"
        )
        async with self._session_factory() as db:
            cutoff = (
                await db.execute(
                    text("SELECT now() - make_interval(secs => :age)"),
                    {"age": older_than_s},
                )
            ).scalar_one()
        pruned = 0
        while True:
            async with self._session_factory() as db, db.begin():
                deleted = (
                    await db.execute(stmt, {"cutoff": cutoff, "limit": limit})
                ).rowcount
            pruned += deleted
            if deleted < limit:
                return pruned
