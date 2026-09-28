"""meeting-api's Prometheus metrics (§1.13), served at ``GET /metrics`` in the text exposition format.

``/metrics`` is exempt from the identity guard (``identity_guard.EXEMPT_PATHS``) and is in no gateway
route table, so it is never public: the cluster's Prometheus scrapes the pod directly.

Counters and histograms move where the thing happens:

====================================================  ==============================================
metric                                                moved by
====================================================  ==============================================
``aw_intake_requests_total{route,result,user_id}``    every ``/v2`` request (``intake.router``)
``aw_intake_request_seconds``                         every ``/v2`` request
``aw_meetings_not_sent_total{detail,user_id}``        ``intake.status.write_status`` writing a
                                                      ``not_sent`` outcome
``aw_meetings_failed_total{reason,user_id}``          ``intake.status.write_status`` ending a
                                                      sent bot's meeting ``failed`` (not
                                                      ``not_sent``), once its transaction
                                                      commits
``aw_autojoin_lag_seconds``                           the auto-join sweep, per bot sent: the tick's
                                                      time minus (``scheduled_at`` − lead)
``aw_webhook_deliveries_total{event_type,outcome,     the subscription sender, per claimed delivery
user_id}``
``aw_webhook_delivery_seconds``                       the sender, per post made
``aw_export_total{state,user_id}``                    the export route, per new result
``aw_sweep_last_run_timestamp_seconds{sweep}``        each background loop whose tick ran to its
                                                      end on this replica
``aw_sweep_items_total{sweep,result}``                ``sweeps.item_failures.run_item``: an intake
                                                      sweep's item that failed (``failed``) or was
                                                      given up (``given_up``)
====================================================  ==============================================

Three gauges are read from the database when Prometheus scrapes (``MetricsSource``), within
``DB_READ_TIMEOUT_S``; the queries are async, so a scrape never holds the event loop:

- ``aw_meetings_by_status{status,user_id}``: meetings in a non-terminal status;
- ``aw_webhook_pending{user_id}``: deliveries due (``pending`` or ``sending``, ``next_attempt_at``
  reached);
- ``aw_webhook_outbox_unpublished``: outbox rows not yet published (the outbox has no account).

When that read fails or times out, the three are served with no samples and the failure is logged:
a missing series, never a stale or zero one.

``user_id`` is the account (one client is one account), so it stays a handful of values. It labels
the per-request and per-meeting counters and the per-account gauges; the histograms and the sweep
stamps describe the service, not an account, and carry no account. No label carries a key, a
secret, a URL, a query string or transcript text: the values are route templates, typed codes,
event types, outcomes, statuses, sweep names and account ids.

The metrics live in their own ``CollectorRegistry``, built on first use, so this module imports
without ``prometheus_client``: the gateway conformance harness builds ``create_app`` in its own
environment and never scrapes it.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Iterator, Optional, Protocol

from .obs import log_event

__all__ = [
    "DB_READ_TIMEOUT_S",
    "DbCounts",
    "MetricsSource",
    "PostgresMetricsSource",
    "autojoin_lag",
    "export_recorded",
    "intake_request",
    "meeting_failed",
    "meeting_not_sent",
    "non_terminal_statuses",
    "registry",
    "render",
    "sweep_item",
    "sweep_ran",
    "webhook_delivery",
]

DB_READ_TIMEOUT_S = 2.0

#: Seconds; the alert is on a p95 above 60 s.
_LAG_BUCKETS = (5, 10, 15, 30, 45, 60, 90, 120, 180, 300, 600, 1800, 3600)


@dataclass(frozen=True)
class DbCounts:
    """What the scrape-time gauges show: ``(user_id, status, n)``, ``(user_id, n)`` and a count."""

    by_status: tuple[tuple[int, str, int], ...]
    pending: tuple[tuple[int, int], ...]
    outbox_unpublished: int


class MetricsSource(Protocol):
    async def read(self) -> DbCounts: ...


class _Metrics:
    def __init__(self) -> None:
        from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

        self.registry = CollectorRegistry(auto_describe=True)
        r = self.registry
        self.intake_requests = Counter(
            "aw_intake_requests_total",
            "Intake requests by route and result.",
            ["route", "result", "user_id"],
            registry=r,
        )
        self.intake_seconds = Histogram(
            "aw_intake_request_seconds", "Intake request latency.", registry=r
        )
        self.not_sent = Counter(
            "aw_meetings_not_sent_total",
            "Meetings ended not_sent, by reason.",
            ["detail", "user_id"],
            registry=r,
        )
        self.failed = Counter(
            "aw_meetings_failed_total",
            "Meetings whose bot was sent that ended failed (not not_sent), by reason.",
            ["reason", "user_id"],
            registry=r,
        )
        self.autojoin_lag = Histogram(
            "aw_autojoin_lag_seconds",
            "Bot sent minus (scheduled_at - lead).",
            buckets=_LAG_BUCKETS,
            registry=r,
        )
        self.deliveries = Counter(
            "aw_webhook_deliveries_total",
            "Webhook delivery results.",
            ["event_type", "outcome", "user_id"],
            registry=r,
        )
        self.delivery_seconds = Histogram(
            "aw_webhook_delivery_seconds", "Webhook delivery latency.", registry=r
        )
        self.exports = Counter(
            "aw_export_total",
            "Export results, counted at the export route.",
            ["state", "user_id"],
            registry=r,
        )
        self.sweep_items = Counter(
            "aw_sweep_items_total",
            "Intake sweep items that failed, or were given up, by sweep.",
            ["sweep", "result"],
            registry=r,
        )
        self.sweep_last_run = Gauge(
            "aw_sweep_last_run_timestamp_seconds",
            "When each sweep last ran.",
            ["sweep"],
            registry=r,
        )


@lru_cache(maxsize=None)
def _metrics() -> _Metrics:
    return _Metrics()


def registry() -> Any:
    """This service's ``CollectorRegistry`` (the counters, histograms and sweep stamps)."""
    return _metrics().registry


def _account(user_id: Any) -> str:
    """An account id as a label value: its digits, or ``""`` for anything else."""
    text = str(user_id) if user_id is not None else ""
    return text if text.isdigit() else ""


def intake_request(route: str, result: str, user_id: Any, seconds: float) -> None:
    m = _metrics()
    m.intake_requests.labels(
        route=route, result=result, user_id=_account(user_id)
    ).inc()
    m.intake_seconds.observe(seconds)


def meeting_not_sent(user_id: Any, detail: Optional[str]) -> None:
    _metrics().not_sent.labels(detail=detail or "", user_id=_account(user_id)).inc()


def meeting_failed(user_id: Any, reason: Optional[str]) -> None:
    _metrics().failed.labels(reason=reason or "", user_id=_account(user_id)).inc()


def autojoin_lag(seconds: float) -> None:
    _metrics().autojoin_lag.observe(seconds)


def webhook_delivery(
    event_type: str, outcome: str, user_id: Any, seconds: Optional[float]
) -> None:
    m = _metrics()
    m.deliveries.labels(
        event_type=event_type, outcome=outcome, user_id=_account(user_id)
    ).inc()
    if seconds is not None:
        m.delivery_seconds.observe(seconds)


def export_recorded(state: str, user_id: Any) -> None:
    _metrics().exports.labels(state=state, user_id=_account(user_id)).inc()


def sweep_item(sweep: str, result: str) -> None:
    _metrics().sweep_items.labels(sweep=sweep, result=result).inc()


def sweep_ran(sweep: str) -> None:
    _metrics().sweep_last_run.labels(sweep=sweep).set(time.time())


def non_terminal_statuses() -> tuple[str, ...]:
    """Every meeting status but ``completed`` and ``failed``: the planned ones and the live ones."""
    from .bot_spawn.auto_join import LIVE_STATUSES

    return ("idle", "scheduled", *LIVE_STATUSES)


async def _read(source: Optional[MetricsSource]) -> Optional[DbCounts]:
    if source is None:
        return None
    try:
        return await asyncio.wait_for(source.read(), timeout=DB_READ_TIMEOUT_S)
    except Exception as exc:
        log_event(
            "metrics_db_read_failed",
            audience="operator",
            level="warning",
            span="metrics",
            fields={"error": type(exc).__name__},
        )
        return None


class _Scrape:
    """One scrape: the registry's metrics, then the database gauges."""

    def __init__(self, counts: Optional[DbCounts]) -> None:
        self._counts = counts

    def collect(self) -> Iterator[Any]:
        from prometheus_client.core import GaugeMetricFamily

        yield from registry().collect()
        by_status = GaugeMetricFamily(
            "aw_meetings_by_status",
            "Meetings in a non-terminal status.",
            labels=["status", "user_id"],
        )
        pending = GaugeMetricFamily(
            "aw_webhook_pending", "Webhook deliveries due.", labels=["user_id"]
        )
        unpublished = GaugeMetricFamily(
            "aw_webhook_outbox_unpublished", "Outbox rows not yet published."
        )
        counts = self._counts
        if counts is not None:
            for user_id, status, n in counts.by_status:
                by_status.add_metric([status, _account(user_id)], n)
            for user_id, n in counts.pending:
                pending.add_metric([_account(user_id)], n)
            unpublished.add_metric([], counts.outbox_unpublished)
        yield by_status
        yield pending
        yield unpublished


async def render(source: Optional[MetricsSource]) -> tuple[bytes, str]:
    """The exposition body and its content type."""
    from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

    counts = await _read(source)
    return generate_latest(_Scrape(counts)), CONTENT_TYPE_LATEST  # type: ignore[arg-type]


class PostgresMetricsSource:
    """``MetricsSource`` over ``meetings``, ``webhook_deliveries`` and ``webhook_outbox``, in one
    short read-only session. Time is the database's ``now()``."""

    def __init__(self, session_factory: Any) -> None:
        self._session_factory = session_factory

    async def read(self) -> DbCounts:
        from sqlalchemy import bindparam, text

        by_status_sql = text(
            "SELECT user_id, status, count(*) FROM meetings "
            "WHERE status IN :statuses GROUP BY user_id, status"
        ).bindparams(bindparam("statuses", expanding=True))
        pending_sql = text(
            "SELECT user_id, count(*) FROM webhook_deliveries "
            "WHERE state IN ('pending', 'sending') AND next_attempt_at <= now() "
            "GROUP BY user_id"
        )
        unpublished_sql = text(
            "SELECT count(*) FROM webhook_outbox WHERE published_at IS NULL"
        )
        async with self._session_factory() as db:
            by_status = (
                await db.execute(
                    by_status_sql, {"statuses": list(non_terminal_statuses())}
                )
            ).all()
            pending = (await db.execute(pending_sql)).all()
            unpublished = (await db.execute(unpublished_sql)).scalar_one()
        return DbCounts(
            by_status=tuple((int(u), str(s), int(n)) for u, s, n in by_status),
            pending=tuple((int(u), int(n)) for u, n in pending),
            outbox_unpublished=int(unpublished),
        )
