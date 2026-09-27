"""admin-api's Prometheus metrics (§1.13), served at ``GET /metrics`` in the text exposition format.

``/metrics`` is exempt from the identity guard (``identity_guard.EXEMPT_PATHS``) and is in no gateway
route table, so it is never public: the cluster's Prometheus scrapes the pod directly.

- ``aw_api_token_expires_seconds{name,user_id}``: seconds left on each named key, read from
  ``api_tokens`` when Prometheus scrapes (``TokenExpiry``), within ``DB_READ_TIMEOUT_S``. A key with
  no expiry, or with no name, has no sample; an expired key shows a negative time; when one account
  holds two keys of one name, the sooner expiry is shown. If the read fails or times out, the gauge
  is served with no samples and the failure is logged.
- ``aw_sweep_last_run_timestamp_seconds{sweep}``: when this replica last ran a sweep to its end;
  ``webhook-retention`` is stamped by ``retention.attach_retention``.

``user_id`` is the account that owns the key. No label carries a key, a secret, a URL or a query
string: the values are key names, account ids and sweep names.

The metrics live in their own ``CollectorRegistry`` (``REGISTRY``), not the process default.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable, Iterator, Optional, Protocol, Sequence

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Gauge,
    generate_latest,
)
from prometheus_client.core import GaugeMetricFamily

__all__ = [
    "DB_READ_TIMEOUT_S",
    "REGISTRY",
    "PostgresTokenExpiry",
    "TokenExpiry",
    "render",
    "sweep_ran",
]

log = logging.getLogger("admin_api.metrics")

DB_READ_TIMEOUT_S = 2.0

REGISTRY = CollectorRegistry(auto_describe=True)
SWEEP_LAST_RUN = Gauge(
    "aw_sweep_last_run_timestamp_seconds",
    "When each sweep last ran.",
    ["sweep"],
    registry=REGISTRY,
)

#: ``(user_id, name, expires_at)``: each named key's soonest expiry, naive UTC like ``api_tokens``.
TokenRow = tuple[int, str, datetime]


class TokenExpiry(Protocol):
    async def read(self) -> Sequence[TokenRow]: ...


def sweep_ran(sweep: str) -> None:
    SWEEP_LAST_RUN.labels(sweep=sweep).set(time.time())


def _utcnow() -> datetime:
    """Naive UTC, the clock ``api_tokens.expires_at`` is written and checked against."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _read(source: Optional[TokenExpiry]) -> Optional[Sequence[TokenRow]]:
    if source is None:
        return None
    try:
        return await asyncio.wait_for(source.read(), timeout=DB_READ_TIMEOUT_S)
    except Exception as exc:
        log.warning("metrics: the api_tokens read failed (%s)", type(exc).__name__)
        return None


class _Scrape:
    """One scrape: the registry's metrics, then the token gauge."""

    def __init__(self, rows: Optional[Sequence[TokenRow]], now: datetime) -> None:
        self._rows = rows
        self._now = now

    def collect(self) -> Iterator[Any]:
        yield from REGISTRY.collect()
        family = GaugeMetricFamily(
            "aw_api_token_expires_seconds",
            "Time left on each named key.",
            labels=["name", "user_id"],
        )
        for user_id, name, expires_at in self._rows or ():
            family.add_metric(
                [name, str(user_id)], (expires_at - self._now).total_seconds()
            )
        yield family


async def render(source: Optional[TokenExpiry]) -> tuple[bytes, str]:
    """The exposition body and its content type."""
    rows = await _read(source)
    return generate_latest(_Scrape(rows, _utcnow())), CONTENT_TYPE_LATEST


class PostgresTokenExpiry:
    """``TokenExpiry`` over ``api_tokens``: one grouped read on the engine ``get_engine`` returns."""

    def __init__(self, get_engine: Callable[[], Any]) -> None:
        self._get_engine = get_engine

    async def read(self) -> Sequence[TokenRow]:
        from sqlalchemy import func, select

        from ..schema.models import APIToken

        query = (
            select(APIToken.user_id, APIToken.name, func.min(APIToken.expires_at))
            .where(APIToken.name.is_not(None), APIToken.expires_at.is_not(None))
            .group_by(APIToken.user_id, APIToken.name)
        )
        async with self._get_engine().connect() as conn:
            rows = (await conn.execute(query)).all()
        return [(int(u), str(n), x) for u, n, x in rows]
