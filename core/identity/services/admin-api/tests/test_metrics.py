"""§1.13 — admin-api's Prometheus metrics and ``GET /metrics``.

``/metrics`` serves ``aw_api_token_expires_seconds{name,user_id}`` (the time left on each named key,
read from ``api_tokens`` at scrape time) and ``aw_sweep_last_run_timestamp_seconds{sweep}`` (the
retention sweep). It needs no gateway signature and is in no gateway route table. A key with no
expiry has no sample; an expired one shows a negative time; two keys of one account with one name
show the sooner. A failed or slow read serves the token gauge with no samples. No label carries a
key, a secret, a URL or a query string. The last test reads real Postgres (skipped unless
``MEETING_API_TEST_DATABASE_URL`` is set).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families

from admin_api.app import metrics, retention
from admin_api.app.main import create_app

NOW = datetime(2026, 9, 27, 12, 0, 0)
PG_URL = os.getenv("MEETING_API_TEST_DATABASE_URL")


class StaticTokens:
    def __init__(self, rows: list[tuple[int, str, datetime]]) -> None:
        self.rows = rows

    async def read(self) -> list[tuple[int, str, datetime]]:
        return list(self.rows)


def scrape(source: Any = None) -> str:
    app = create_app(token_expiry=source)
    r = TestClient(app).get("/metrics")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/plain; version=0.0.4")
    return r.text


def samples(text: str, name: str) -> list[Any]:
    return [
        s
        for f in text_string_to_metric_families(text)
        for s in f.samples
        if s.name == name
    ]


@pytest.fixture(autouse=True)
def _clock(monkeypatch):
    monkeypatch.setattr(metrics, "_utcnow", lambda: NOW)


# ── the route ────────────────────────────────────────────────────────────────────────────────


def test_metrics_serves_both_admin_api_metrics():
    metrics.sweep_ran("webhook-retention")
    text = scrape(StaticTokens([(1, "exporter", NOW + timedelta(days=90))]))
    found = {f.name: f.type for f in text_string_to_metric_families(text)}
    assert found["aw_api_token_expires_seconds"] == "gauge"
    assert found["aw_sweep_last_run_timestamp_seconds"] == "gauge"


def test_the_token_gauge_is_the_time_left_on_each_named_key():
    text = scrape(
        StaticTokens(
            [
                (1, "exporter", NOW + timedelta(days=90)),
                (1, "portal", NOW + timedelta(days=10)),
                (1, "calendar-dispatcher", NOW - timedelta(hours=1)),
            ]
        )
    )
    got = {
        (s.labels["name"], s.labels["user_id"]): s.value
        for s in samples(text, "aw_api_token_expires_seconds")
    }
    assert got == {
        ("exporter", "1"): 90 * 86400,
        ("portal", "1"): 10 * 86400,
        ("calendar-dispatcher", "1"): -3600,
    }


def test_every_metric_carries_exactly_its_labels():
    metrics.sweep_ran("webhook-retention")
    text = scrape(StaticTokens([(1, "exporter", NOW + timedelta(days=1))]))
    labels = {
        f.name: {k for s in f.samples for k in s.labels}
        for f in text_string_to_metric_families(text)
        if f.name.startswith("aw_")
    }
    assert labels == {
        "aw_api_token_expires_seconds": {"name", "user_id"},
        "aw_sweep_last_run_timestamp_seconds": {"sweep"},
    }


def test_without_a_source_the_token_gauge_has_no_samples():
    text = scrape(None)
    assert "# TYPE aw_api_token_expires_seconds gauge" in text
    assert samples(text, "aw_api_token_expires_seconds") == []


def test_a_failed_read_serves_no_samples_and_is_logged(caplog):
    class Broken:
        async def read(self) -> list:
            raise OSError("connection refused")

    with caplog.at_level(logging.WARNING, logger="admin_api.metrics"):
        text = scrape(Broken())
    assert samples(text, "aw_api_token_expires_seconds") == []
    assert any("OSError" in r.getMessage() for r in caplog.records)


def test_a_slow_read_is_cut_off_at_the_timeout(monkeypatch):
    monkeypatch.setattr(metrics, "DB_READ_TIMEOUT_S", 0.05)

    class Slow:
        async def read(self) -> list:
            await asyncio.sleep(5)
            return []

    started = time.monotonic()
    text = scrape(Slow())
    assert time.monotonic() - started < 2
    assert samples(text, "aw_api_token_expires_seconds") == []


def test_the_identity_guard_does_not_block_metrics():
    # The suite runs with GATEWAY_IDENTITY_KEYS set; an unsigned scrape still gets through.
    r = TestClient(create_app()).get("/metrics")
    assert r.status_code == 200
    assert "aw_api_token_expires_seconds" in r.text


def test_the_production_app_reads_the_tokens_table(monkeypatch):
    """No database is reached: the app is only built, and its source is the Postgres one."""
    monkeypatch.setenv("INTERNAL_API_SECRET", "a-real-secret")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@db.invalid:5432/vexa")
    captured: dict[str, Any] = {}
    real = create_app

    def _create_app(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return real(**kwargs)

    import admin_api.app.main as main_mod

    monkeypatch.setattr(main_mod, "create_app", _create_app)
    from admin_api.__main__ import build_production_app

    build_production_app()
    assert isinstance(captured["token_expiry"], metrics.PostgresTokenExpiry)


# ── never public ─────────────────────────────────────────────────────────────────────────────


def test_the_identity_route_table_does_not_hold_metrics():
    for parent in Path(__file__).resolve().parents:
        manifest = parent / "identity" / "routes.v1.json"
        if manifest.is_file():
            break
    paths = [row["path"] for row in json.loads(manifest.read_text())["routes"]]
    assert paths
    assert not [p for p in paths if "metrics" in p]


# ── the retention sweep stamps its last run ──────────────────────────────────────────────────


def _run_once(monkeypatch, outcome: Any) -> None:
    async def _single_flight(engine: Any, **kw: Any) -> Any:
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def _loop(run: Any, *, interval_s: float, sleep: Any = None) -> None:
        try:
            await run()
        except RuntimeError:
            pass

    monkeypatch.setattr(retention, "run_single_flight", _single_flight)
    monkeypatch.setattr(retention, "retention_loop", _loop)
    from fastapi import FastAPI

    app = FastAPI()
    retention.attach_retention(app, lambda: None, environ={})
    with TestClient(app):
        pass


def _stamp() -> float:
    return (
        metrics.REGISTRY.get_sample_value(
            "aw_sweep_last_run_timestamp_seconds", {"sweep": "webhook-retention"}
        )
        or 0.0
    )


def test_a_retention_run_stamps_its_last_run(monkeypatch):
    started = time.time()
    _run_once(monkeypatch, retention.RetentionResult(deliveries=3, outbox=1))
    assert _stamp() >= started


@pytest.mark.parametrize("outcome", [None, RuntimeError("database went away")])
def test_a_skipped_or_failed_run_does_not_stamp(monkeypatch, outcome):
    before = _stamp()
    _run_once(monkeypatch, outcome)
    assert _stamp() == before


# ── no secret-bearing label ──────────────────────────────────────────────────────────────────


def test_no_label_carries_a_key_secret_url_or_query():
    metrics.sweep_ran("webhook-retention")
    text = scrape(StaticTokens([(1, "exporter", NOW + timedelta(days=1))]))
    for family in text_string_to_metric_families(text):
        for s in family.samples:
            for v in s.labels.values():
                assert "://" not in v and "?" not in v and "=" not in v, v
                assert not v.startswith("vxa_"), v


# ── the Postgres source ──────────────────────────────────────────────────────────────────────


@pytest.mark.skipif(
    not PG_URL,
    reason="real-Postgres token expiry; set MEETING_API_TEST_DATABASE_URL to run",
)
def test_the_postgres_source_reads_each_named_keys_soonest_expiry(monkeypatch):
    from sqlalchemy import create_engine, text
    from sqlalchemy.ext.asyncio import create_async_engine

    from admin_api.schema.models import Base
    from admin_api.schema.sync import ensure_schema_sync

    sync = create_engine(PG_URL.replace("+asyncpg", "+psycopg"))
    ensure_schema_sync(sync, Base)
    email = f"metrics-{uuid.uuid4().hex}@abroadworks.com"
    secret_value = f"vxa_bot_{uuid.uuid4().hex}"
    with sync.begin() as conn:
        user_id = conn.execute(
            text("INSERT INTO users (email, name) VALUES (:e, 'metrics') RETURNING id"),
            {"e": email},
        ).scalar_one()
        for token, name, expires_at in (
            (secret_value, "exporter", NOW + timedelta(days=90)),
            (f"vxa_tx_{uuid.uuid4().hex}", "exporter", NOW + timedelta(days=5)),
            (f"vxa_tx_{uuid.uuid4().hex}", "portal", None),
            (f"vxa_tx_{uuid.uuid4().hex}", None, NOW + timedelta(days=1)),
        ):
            conn.execute(
                text(
                    "INSERT INTO api_tokens (token, user_id, scopes, name, expires_at) "
                    "VALUES (:t, :u, '{tx}', :n, :x)"
                ),
                {"t": token, "u": user_id, "n": name, "x": expires_at},
            )
    engine = create_async_engine(PG_URL)
    try:
        text_out = scrape(metrics.PostgresTokenExpiry(lambda: engine))
    finally:
        asyncio.run(engine.dispose())
        with sync.begin() as conn:
            conn.execute(
                text("DELETE FROM api_tokens WHERE user_id = :u"), {"u": user_id}
            )
            conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": user_id})
        sync.dispose()
    mine = {
        s.labels["name"]: s.value
        for s in samples(text_out, "aw_api_token_expires_seconds")
        if s.labels["user_id"] == str(user_id)
    }
    assert mine == {"exporter": 5 * 86400}
    assert secret_value not in text_out
