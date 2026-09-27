"""Webhook delivery retention (§1.13): a daily, single-flight sweep.

Each run, in one transaction and in this order:

1. delete deliveries in a final state (``delivered``, ``failed``, ``dead``, ``cancelled``) created
   more than ``WEBHOOK_DELIVERY_RETENTION_DAYS`` days ago; their attempts go with them
   (``ON DELETE CASCADE``);
2. delete published outbox rows with no deliveries left.

Unpublished outbox rows are never pruned: one that stays unpublished is an alert, not garbage.

Single flight: a run takes a session-level Postgres advisory lock (``pg_try_advisory_lock`` on
``RETENTION_LOCK_KEY``) and skips when another replica holds it — the same mechanism as
meeting-api's ``sweeps/single_flight.py``, reimplemented here because the services share no code.
The key's high 32 bits are this sweep's own namespace, so it can't meet meeting-api's sweep keys
or the per-user locks on the shared database.

``attach_retention`` starts the loop on application startup (a run at start, then every 24 hours)
and cancels it on shutdown. A run that swept (not one skipped for the lock, not a failed one) stamps
``aw_sweep_last_run_timestamp_seconds{sweep="webhook-retention"}`` (§1.13).
"""

from __future__ import annotations

import asyncio
import binascii
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Mapping, Optional

from sqlalchemy import text

from .metrics import sweep_ran

__all__ = [
    "FINAL_STATES",
    "SWEEP_INTERVAL_S",
    "RETENTION_LOCK_KEY",
    "RetentionResult",
    "retention_days_from_env",
    "sweep_once",
    "run_single_flight",
    "retention_loop",
    "attach_retention",
]

log = logging.getLogger("admin_api.retention")

FINAL_STATES = ("delivered", "failed", "dead", "cancelled")
SWEEP_INTERVAL_S = 24 * 60 * 60
DEFAULT_RETENTION_DAYS = 30
#: "WHRT" in the high 32 bits, crc32 of the sweep name in the low 32: a positive signed int8.
RETENTION_LOCK_KEY = (0x57485254 << 32) | binascii.crc32(b"webhook-retention")

_DELETE_DELIVERIES = text(
    "DELETE FROM webhook_deliveries "
    "WHERE state IN ('delivered', 'failed', 'dead', 'cancelled') AND created_at < :cutoff"
)
_DELETE_OUTBOX = text(
    "DELETE FROM webhook_outbox o WHERE o.published_at IS NOT NULL "
    "AND NOT EXISTS (SELECT 1 FROM webhook_deliveries d WHERE d.event_id = o.event_id)"
)


@dataclass(frozen=True)
class RetentionResult:
    deliveries: int
    outbox: int


def retention_days_from_env(environ: Optional[Mapping[str, str]] = None) -> int:
    """``WEBHOOK_DELIVERY_RETENTION_DAYS`` (default 30); ``ValueError`` unless a positive integer."""
    env = os.environ if environ is None else environ
    raw = env.get("WEBHOOK_DELIVERY_RETENTION_DAYS") or str(DEFAULT_RETENTION_DAYS)
    try:
        days = int(raw)
    except ValueError as exc:
        raise ValueError(
            "WEBHOOK_DELIVERY_RETENTION_DAYS must be a whole number"
        ) from exc
    if days < 1:
        raise ValueError("WEBHOOK_DELIVERY_RETENTION_DAYS must be at least 1")
    return days


async def sweep_once(
    engine: Any, *, now: datetime, retention_days: int
) -> RetentionResult:
    """One retention pass (see the module docstring), in one transaction."""
    cutoff = now - timedelta(days=retention_days)
    async with engine.begin() as conn:
        deliveries = await conn.execute(_DELETE_DELIVERIES, {"cutoff": cutoff})
        outbox = await conn.execute(_DELETE_OUTBOX)
    return RetentionResult(
        deliveries=int(deliveries.rowcount or 0), outbox=int(outbox.rowcount or 0)
    )


async def run_single_flight(
    engine: Any, *, now: datetime, retention_days: int
) -> Optional[RetentionResult]:
    """``sweep_once`` under the retention lock; ``None`` when another run holds it."""
    async with engine.connect() as lock:
        got = await lock.scalar(
            text("SELECT pg_try_advisory_lock(cast(:key as bigint))"),
            {"key": RETENTION_LOCK_KEY},
        )
        if not got:
            log.info("webhook retention skipped: another run holds the lock")
            return None
        try:
            result = await sweep_once(engine, now=now, retention_days=retention_days)
        finally:
            await lock.execute(
                text("SELECT pg_advisory_unlock(cast(:key as bigint))"),
                {"key": RETENTION_LOCK_KEY},
            )
    log.info(
        "webhook retention: deleted %d deliveries in a final state older than %d days, "
        "then %d published outbox rows with no deliveries left",
        result.deliveries,
        retention_days,
        result.outbox,
    )
    return result


async def retention_loop(
    run: Callable[[], Awaitable[Any]],
    *,
    interval_s: float,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> None:
    """Call ``run`` now and then every ``interval_s``; a failed run is logged and retried next
    interval."""
    while True:
        try:
            await run()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("webhook retention run failed; retrying next interval")
        await sleep(interval_s)


def attach_retention(
    app: Any,
    get_engine: Callable[[], Any],
    *,
    environ: Optional[Mapping[str, str]] = None,
) -> None:
    """Run the sweep for the app's lifetime. ``ValueError`` on a bad retention setting."""
    days = retention_days_from_env(environ)

    async def _run() -> None:
        result = await run_single_flight(
            get_engine(), now=datetime.now(timezone.utc), retention_days=days
        )
        if result is not None:
            sweep_ran("webhook-retention")

    @app.on_event("startup")
    async def _start_retention() -> None:
        app.state.webhook_retention_task = asyncio.create_task(
            retention_loop(_run, interval_s=SWEEP_INTERVAL_S), name="webhook-retention"
        )

    @app.on_event("shutdown")
    async def _stop_retention() -> None:
        task = getattr(app.state, "webhook_retention_task", None)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
