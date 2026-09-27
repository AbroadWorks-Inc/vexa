"""§1.13 — meeting-api's Prometheus metrics and ``GET /metrics``.

``/metrics`` serves every metric §1.13 names for meeting-api, needs no gateway signature, and is in
no gateway route table. Each instrumented site moves its own metric: the ``/v2`` routes, the status
writer's ``not_sent``, the auto-join sweep's lag, the subscription sender, the export route and the
background loops' sweep stamps. The three database gauges are read at scrape time; a failed or slow
read serves them with no samples. No label carries a key, a secret, a URL, a query string or
transcript text. The last test reads the gauges from a real Postgres (skipped unless
``MEETING_API_TEST_DATABASE_URL`` is set).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import types
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import pytest
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families

from intake_builders import (
    A,
    ZOOM,
    http,
    instant_body,
    intake_app,
    make_harness,
    send_clock,
    sweep_intake,
)
from meeting_api import create_app, metrics
from meeting_api.intake.fakes import InMemoryIntakeReads
from meeting_api.intake.ports import SpawnOutcome

UTC = timezone.utc
ACCOUNT = "1"

MEETING_API_METRICS = {
    "aw_intake_requests_total": "counter",
    "aw_intake_request_seconds": "histogram",
    "aw_meetings_by_status": "gauge",
    "aw_meetings_not_sent_total": "counter",
    "aw_autojoin_lag_seconds": "histogram",
    "aw_webhook_deliveries_total": "counter",
    "aw_webhook_delivery_seconds": "histogram",
    "aw_webhook_pending": "gauge",
    "aw_webhook_outbox_unpublished": "gauge",
    "aw_export_total": "counter",
    "aw_sweep_last_run_timestamp_seconds": "gauge",
}

#: The label names each metric carries (§1.13, plus ``user_id`` where it is per request, meeting
#: or account).
LABELS = {
    "aw_intake_requests_total": {"route", "result", "user_id"},
    "aw_intake_request_seconds": set(),
    "aw_meetings_by_status": {"status", "user_id"},
    "aw_meetings_not_sent_total": {"detail", "user_id"},
    "aw_autojoin_lag_seconds": set(),
    "aw_webhook_deliveries_total": {"event_type", "outcome", "user_id"},
    "aw_webhook_delivery_seconds": set(),
    "aw_webhook_pending": {"user_id"},
    "aw_webhook_outbox_unpublished": set(),
    "aw_export_total": {"state", "user_id"},
    "aw_sweep_last_run_timestamp_seconds": {"sweep"},
}


def value(name: str, **labels: str) -> float:
    return metrics.registry().get_sample_value(name, labels) or 0.0


class StaticSource:
    def __init__(self, counts: metrics.DbCounts) -> None:
        self.counts = counts

    async def read(self) -> metrics.DbCounts:
        return self.counts


COUNTS = metrics.DbCounts(
    by_status=((7, "scheduled", 3), (7, "active", 1), (9, "requested", 2)),
    pending=((7, 4),),
    outbox_unpublished=2,
)


def scrape(source: Any = None) -> tuple[Any, str]:
    r = TestClient(create_app(metrics_source=source)).get("/metrics")
    assert r.status_code == 200, r.text
    return r, r.text


def families(text: str) -> dict[str, Any]:
    return {f.name: f for f in text_string_to_metric_families(text)}


def samples(text: str, name: str) -> list[Any]:
    return [
        s
        for f in text_string_to_metric_families(text)
        for s in f.samples
        if s.name == name
    ]


# ── the route ────────────────────────────────────────────────────────────────────────────────


def test_metrics_serves_every_meeting_api_metric_in_the_text_format():
    r, text = scrape(StaticSource(COUNTS))
    assert r.headers["content-type"].startswith("text/plain; version=0.0.4")
    found = families(text)
    for name, kind in MEETING_API_METRICS.items():
        family = name[: -len("_total")] if kind == "counter" else name
        assert family in found, name
        assert found[family].type == kind, name


def test_every_metric_carries_exactly_its_labels():
    metrics.intake_request("PUT /v2/entries", "created", 7, 0.01)
    metrics.meeting_not_sent(7, "room_busy")
    metrics.autojoin_lag(12.0)
    metrics.webhook_delivery("meeting.completed", "delivered", 7, 0.2)
    metrics.export_recorded("handed_off", 7)
    metrics.sweep_ran("auto-join")
    _, text = scrape(StaticSource(COUNTS))
    seen: dict[str, set[str]] = {}
    for family in text_string_to_metric_families(text):
        for s in family.samples:
            name = family.name + "_total" if family.type == "counter" else family.name
            seen.setdefault(name, set()).update(set(s.labels) - {"le"})
    for name, labels in LABELS.items():
        assert seen.get(name, set()) == labels, name


def test_the_database_gauges_show_what_the_source_read():
    _, text = scrape(StaticSource(COUNTS))
    by_status = {
        (s.labels["status"], s.labels["user_id"]): s.value
        for s in samples(text, "aw_meetings_by_status")
    }
    assert by_status == {
        ("scheduled", "7"): 3,
        ("active", "7"): 1,
        ("requested", "9"): 2,
    }
    assert [(s.labels, s.value) for s in samples(text, "aw_webhook_pending")] == [
        ({"user_id": "7"}, 4)
    ]
    assert [s.value for s in samples(text, "aw_webhook_outbox_unpublished")] == [2]


def test_without_a_source_the_database_gauges_have_no_samples():
    _, text = scrape(None)
    found = families(text)
    for name in (
        "aw_meetings_by_status",
        "aw_webhook_pending",
        "aw_webhook_outbox_unpublished",
    ):
        assert name in found
        assert found[name].samples == []


def test_a_failed_read_serves_no_samples_and_is_logged(capsys):
    class Broken:
        async def read(self) -> metrics.DbCounts:
            raise OSError("connection refused")

    _, text = scrape(Broken())
    for name in (
        "aw_meetings_by_status",
        "aw_webhook_pending",
        "aw_webhook_outbox_unpublished",
    ):
        assert samples(text, name) == []
    logged = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]
    assert any(
        e["event"] == "metrics_db_read_failed" and e["fields"] == {"error": "OSError"}
        for e in logged
    )


def test_a_slow_read_is_cut_off_at_the_timeout(monkeypatch):
    monkeypatch.setattr(metrics, "DB_READ_TIMEOUT_S", 0.05)

    class Slow:
        async def read(self) -> metrics.DbCounts:
            await asyncio.sleep(5)
            return COUNTS

    started = time.monotonic()
    _, text = scrape(Slow())
    assert time.monotonic() - started < 2
    assert samples(text, "aw_webhook_outbox_unpublished") == []


def test_the_non_terminal_statuses_are_every_status_but_the_finished_ones():
    from meeting_api.intake.router import MEETING_STATUSES
    from meeting_api.intake.rules import FINISHED_STATUSES

    assert set(metrics.non_terminal_statuses()) == MEETING_STATUSES - FINISHED_STATUSES


def test_the_identity_guard_does_not_block_metrics():
    # The suite runs with GATEWAY_IDENTITY_SECRET set; an unsigned scrape still gets through.
    r = TestClient(create_app()).get("/metrics")
    assert r.status_code == 200
    assert "aw_intake_requests_total" in r.text


def test_the_production_app_serves_metrics_without_a_signature(monkeypatch):
    import meeting_api.__main__ as main_mod

    monkeypatch.setattr(main_mod, "_require_config", lambda env=None: None)
    monkeypatch.setattr(main_mod, "_attach_background_loops", lambda *a, **k: None)
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+asyncpg://nobody:dummy@127.0.0.1:1/none"
    )
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin-token")
    monkeypatch.setattr(metrics, "DB_READ_TIMEOUT_S", 1.0)
    r = TestClient(main_mod.build_production_app()).get("/metrics")
    assert r.status_code == 200
    # nothing listens on port 1: the gauges come back empty, the scrape still answers
    assert samples(r.text, "aw_webhook_outbox_unpublished") == []


# ── never public ─────────────────────────────────────────────────────────────────────────────


def _core() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "gateway" / "services" / "gateway").is_dir():
            return parent
    raise FileNotFoundError("core/")


def test_no_gateway_route_table_holds_metrics():
    core = _core()
    manifests = sorted(core.glob("**/routes.v1.json"))
    assert core / "meetings" / "routes.v1.json" in manifests
    assert core / "identity" / "routes.v1.json" in manifests
    assert core / "gateway" / "services" / "gateway" / "routes.v1.json" in manifests
    for manifest in manifests:
        paths = [row["path"] for row in json.loads(manifest.read_text())["routes"]]
        assert not [p for p in paths if "metrics" in p], manifest
    gateway_app = (
        core / "gateway" / "services" / "gateway" / "src" / "gateway" / "app.py"
    )
    assert "/metrics" not in gateway_app.read_text()


# ── each site moves its metric ───────────────────────────────────────────────────────────────


async def test_every_v2_request_is_counted_by_route_and_result():
    h = make_harness()
    route = "PUT /v2/entries"
    created = value(
        "aw_intake_requests_total", route=route, result="created", user_id=ACCOUNT
    )
    unchanged = value(
        "aw_intake_requests_total", route=route, result="unchanged", user_id=ACCOUNT
    )
    invalid = value(
        "aw_intake_requests_total",
        route=route,
        result="invalid_request",
        user_id=ACCOUNT,
    )
    not_found = value(
        "aw_intake_requests_total",
        route="GET /v2/meetings/{meeting_id}",
        result="meeting_not_found",
        user_id=ACCOUNT,
    )
    count = value("aw_intake_request_seconds_count")
    body = {
        "external_id": "google:metrics-1",
        "user": A,
        "meeting_url": "https://meet.google.com/kxo-misr-avz",
        "start": "2026-09-29T09:00:00Z",
        "end": "2026-09-29T09:30:00Z",
        "attendees": [A],
    }
    async with http(intake_app(h.service, InMemoryIntakeReads(h.store), h.stop)) as c:
        headers = {"x-user-id": ACCOUNT}
        assert (
            await c.put("/v2/entries", json=body, headers=headers)
        ).status_code == 200
        assert (
            await c.put("/v2/entries", json=body, headers=headers)
        ).status_code == 200
        assert (await c.put("/v2/entries", json={}, headers=headers)).status_code == 400
        missing = await c.get(f"/v2/meetings/{uuid.uuid4()}", headers=headers)
        assert missing.status_code == 404
    assert value(
        "aw_intake_requests_total", route=route, result="created", user_id=ACCOUNT
    ) == (created + 1)
    assert value(
        "aw_intake_requests_total", route=route, result="unchanged", user_id=ACCOUNT
    ) == (unchanged + 1)
    assert value(
        "aw_intake_requests_total",
        route=route,
        result="invalid_request",
        user_id=ACCOUNT,
    ) == (invalid + 1)
    assert value(
        "aw_intake_requests_total",
        route="GET /v2/meetings/{meeting_id}",
        result="meeting_not_found",
        user_id=ACCOUNT,
    ) == (not_found + 1)
    assert value("aw_intake_request_seconds_count") == count + 4


async def test_a_failed_instant_join_counts_the_request_and_the_not_sent_is_the_writers():
    """The fakes' status writer is in memory; the not_sent counter is moved by the real
    ``write_status`` (next test), so this one only checks the route's result."""
    h = make_harness(spawn_failure=SpawnOutcome("failed", "room_busy", "Room busy."))
    route = "PUT /v2/entries"
    before = value(
        "aw_intake_requests_total", route=route, result="created", user_id=ACCOUNT
    )
    async with http(intake_app(h.service, InMemoryIntakeReads(h.store), h.stop)) as c:
        r = await c.put(
            "/v2/entries",
            json=instant_body("zoom:metrics-2", ZOOM),
            headers={"x-user-id": "1"},
        )
    assert r.status_code == 200, r.text
    assert r.json()["result"] == "created"
    assert value(
        "aw_intake_requests_total", route=route, result="created", user_id=ACCOUNT
    ) == (before + 1)


class _Rows:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def scalars(self) -> "_Rows":
        return self

    def all(self) -> list:
        return list(self._rows)


class _Session:
    """Just enough of an ``AsyncSession`` for ``write_status`` over in-memory ORM rows."""

    def __init__(self, meeting: Any, aw: Any) -> None:
        self.rows = {
            ("meetings", meeting.id): meeting,
            ("meeting_aw_state", aw.meeting_id): aw,
        }

    async def get(self, cls: Any, ident: Any, **_: Any) -> Any:
        return self.rows.get((cls.__tablename__, ident))

    async def execute(self, stmt: Any) -> _Rows:
        return _Rows([])

    def add(self, obj: Any) -> None:
        pass

    async def flush(self) -> None:
        pass


def _orm_meeting(status: str = "scheduled") -> tuple[Any, Any]:
    from meeting_api.sessions import models

    meeting = models.Meeting(
        id=11,
        uuid=uuid.UUID("5f0c2b7e-8d1a-4c3e-9b6f-2a7d1e4c8b90"),
        user_id=7,
        platform="zoom",
        platform_specific_id="12345678901",
        status=status,
        data={"scheduled_at": "2026-09-29T09:00:00Z"},
        start_time=None,
        created_at=datetime(2026, 9, 1),
    )
    return meeting, models.MeetingAwState(meeting_id=11, event_seq=0)


async def _not_sent() -> None:
    """``write_status`` ends a scheduled meeting not_sent; its message carries a URL."""
    pytest.importorskip("sqlalchemy", reason="the ORM models need SQLAlchemy")
    from meeting_api.intake.status import Outcome, write_status

    meeting, aw = _orm_meeting()
    await write_status(
        _Session(meeting, aw),
        11,
        "failed",
        expected_from={"scheduled"},
        outcome=Outcome(
            "not_sent", "room_busy", "Room busy, https://zoom.us/j/1?pwd=x"
        ),
        event_type="meeting.not_sent",
    )


async def test_write_status_counts_a_not_sent_outcome_by_reason():

    before = value("aw_meetings_not_sent_total", detail="room_busy", user_id="7")
    await _not_sent()
    assert (
        value("aw_meetings_not_sent_total", detail="room_busy", user_id="7")
        == before + 1
    )


async def test_write_status_does_not_count_other_changes_or_a_conflict():
    pytest.importorskip("sqlalchemy", reason="the ORM models need SQLAlchemy")
    from meeting_api.intake.status import Outcome, StatusConflict, write_status

    def total() -> float:
        return sum(
            s.value
            for f in metrics.registry().collect()
            for s in f.samples
            if s.name == "aw_meetings_not_sent_total"
        )

    before = total()
    meeting, aw = _orm_meeting()
    await write_status(
        _Session(meeting, aw), 11, "requested", expected_from={"scheduled"}
    )
    with pytest.raises(StatusConflict):
        await write_status(
            _Session(meeting, aw),
            11,
            "failed",
            expected_from={"scheduled"},
            outcome=Outcome("not_sent", "room_busy", "Room busy."),
        )
    assert total() == before


async def test_the_auto_join_sweep_observes_the_lag_of_each_bot_it_sends():
    from meeting_api.bot_spawn.auto_join import auto_join_tick
    from meeting_api.bot_spawn.fakes import FakeRuntimeClient, InMemoryMeetingRepo

    now = datetime(2026, 7, 10, 15, 0, 0, tzinfo=UTC)
    lead_s = 120
    repo, runtime = InMemoryMeetingRepo(), FakeRuntimeClient()
    at = now + timedelta(seconds=30)
    repo._meetings[1] = {
        "id": 1,
        "user_id": 7,
        "platform": "google_meet",
        "native_meeting_id": "abc-defg-hij",
        "platform_specific_id": "abc-defg-hij",
        "status": "scheduled",
        "bot_container_id": None,
        "start_time": None,
        "end_time": None,
        "data": {"title": "t", "auto_join": True, "scheduled_at": at.isoformat()},
        "created_at": "2026-07-08T09:00:00Z",
        "updated_at": "2026-07-08T09:00:00Z",
    }
    count = value("aw_autojoin_lag_seconds_count")
    total = value("aw_autojoin_lag_seconds_sum")
    with send_clock(now):
        counters = await auto_join_tick(
            repo,
            runtime,
            **sweep_intake(
                transcribe_gate=lambda: None,
                now=now,
                lead_s=lead_s,
                token_secret="s",
                redis_url="redis://r",
                allow_uncapped=True,
            ),
        )
    assert counters["spawned"] == 1
    assert value("aw_autojoin_lag_seconds_count") == count + 1
    # sent at `now`; due from scheduled_at − lead = now − 90 s
    assert value("aw_autojoin_lag_seconds_sum") == pytest.approx(total + 90.0)


async def test_a_sweep_that_sends_no_bot_observes_no_lag():
    from meeting_api.bot_spawn.auto_join import auto_join_tick
    from meeting_api.bot_spawn.fakes import FakeRuntimeClient, InMemoryMeetingRepo

    now = datetime(2026, 7, 10, 15, 0, 0, tzinfo=UTC)
    repo, runtime = InMemoryMeetingRepo(), FakeRuntimeClient()
    count = value("aw_autojoin_lag_seconds_count")
    with send_clock(now):
        await auto_join_tick(
            repo,
            runtime,
            **sweep_intake(transcribe_gate=lambda: None, now=now, allow_uncapped=True),
        )
    assert value("aw_autojoin_lag_seconds_count") == count


# ── the sender ───────────────────────────────────────────────────────────────────────────────

WEBHOOK_URL = "https://hooks.example.com/aw?token=q-9f8e7d"
WEBHOOK_SECRET = "whsec-current-0123456789"


def _ring() -> dict[str, Any]:
    core = _core()
    path = (
        core
        / "identity"
        / "contracts"
        / "webhook-subscriptions"
        / "secret-box.vectors.json"
    )
    return json.loads(path.read_text())["key_ring"]


class _Subs:
    def __init__(self, sub: Any) -> None:
        self.sub = sub

    async def for_account(self, user_id: int) -> list[Any]:
        return [self.sub]

    async def find(self, user_id: int, subscription_id: str) -> Optional[Any]:
        return self.sub if subscription_id == self.sub.id else None


class _Receiver:
    def __init__(self, code: int) -> None:
        self.code = code
        self.posted = 0

    async def post(self, target: Any, body: bytes, headers: Any) -> int:
        self.posted += 1
        return self.code


def _sender(code: int, *, active: bool = True) -> tuple[Any, _Receiver]:
    import base64

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    from meeting_api.webhooks.fakes import InMemoryDeliveryStore
    from meeting_api.webhooks.secret_box import SecretBox
    from meeting_api.webhooks.sender import WebhookSender
    from meeting_api.webhooks.subscriptions import Subscription

    ring = _ring()
    nonce = os.urandom(12)
    sealed = nonce + AESGCM(base64.b64decode(ring["k1"])).encrypt(
        nonce, WEBHOOK_SECRET.encode(), b"aw-webhook-secret"
    )
    sub = Subscription(
        id=str(uuid.uuid4()),
        url=WEBHOOK_URL,
        events=(),
        secret_enc=sealed,
        enc_key_id="k1",
    )
    now = datetime(2026, 9, 29, 4, 0, 0, tzinfo=UTC)
    store = InMemoryDeliveryStore(clock=lambda: now)
    store.active[sub.id] = active
    store.outbox["evt_1"] = {
        "event_type": "meeting.completed",
        "payload_text": json.dumps({"data": {"transcript": "hello there"}}),
    }
    store.deliveries[1] = {
        "id": 1,
        "event_id": "evt_1",
        "subscription_id": sub.id,
        "user_id": 7,
        "state": "pending",
        "attempts": 0,
        "next_attempt_at": now,
        "lease_until": None,
        "last_status_code": None,
        "last_error": None,
    }
    receiver = _Receiver(code)
    sender = WebhookSender(
        store,
        _Subs(sub),
        SecretBox.from_settings(json.dumps(ring), "k1"),
        receiver,
        allowlist=frozenset(),
        resolver=lambda host: ["93.184.216.34"],
        clock=lambda: now,
    )
    return sender, receiver


@pytest.mark.parametrize(
    "code, outcome", [(200, "delivered"), (503, "retry"), (404, "failed")]
)
async def test_the_sender_counts_each_delivery_outcome_and_its_latency(code, outcome):
    labels = {"event_type": "meeting.completed", "outcome": outcome, "user_id": "7"}
    before = value("aw_webhook_deliveries_total", **labels)
    count = value("aw_webhook_delivery_seconds_count")
    sender, receiver = _sender(code)
    assert await sender.run_once() == 1
    assert receiver.posted == 1
    assert value("aw_webhook_deliveries_total", **labels) == before + 1
    assert value("aw_webhook_delivery_seconds_count") == count + 1


async def test_a_cancelled_delivery_is_counted_without_a_latency():
    labels = {"event_type": "meeting.completed", "outcome": "cancelled", "user_id": "7"}
    before = value("aw_webhook_deliveries_total", **labels)
    count = value("aw_webhook_delivery_seconds_count")
    sender, receiver = _sender(200, active=False)
    await sender.run_once()
    assert receiver.posted == 0
    assert value("aw_webhook_deliveries_total", **labels) == before + 1
    assert value("aw_webhook_delivery_seconds_count") == count


# ── the export route ─────────────────────────────────────────────────────────────────────────


async def _export_results() -> None:
    """A finished meeting takes a handed_off result, the same again, then a failed one."""
    h = make_harness()
    reply = await h.put(attendees=[A])
    meeting_uuid = reply["meeting"]["id"]
    for step in ("requested", "active", "completed"):
        h.set_status(meeting_uuid, step)
    path = "s3://aw-chatworks-transcribe/recordings/google_meet_kxo-misr-avz_x/"
    async with http(intake_app(h.service, InMemoryIntakeReads(h.store), h.stop)) as c:
        url = f"/v2/meetings/{meeting_uuid}/export"
        headers = {"x-user-id": ACCOUNT}
        body = {"state": "handed_off", "s3_path": path}
        assert (await c.post(url, json=body, headers=headers)).status_code == 200
        # the same result again changes nothing, so it isn't counted again
        assert (await c.post(url, json=body, headers=headers)).status_code == 200
        failure = {"state": "failed", "s3_path": path, "error": "notetaker 422"}
        assert (await c.post(url, json=failure, headers=headers)).status_code == 200


async def test_the_export_route_counts_each_new_result_by_state():
    handed = value("aw_export_total", state="handed_off", user_id=ACCOUNT)
    failed = value("aw_export_total", state="failed", user_id=ACCOUNT)
    await _export_results()
    assert value("aw_export_total", state="handed_off", user_id=ACCOUNT) == handed + 1
    assert value("aw_export_total", state="failed", user_id=ACCOUNT) == failed + 1


# ── the sweep stamps ─────────────────────────────────────────────────────────────────────────


class _StopLoop(Exception):
    pass


class _FakeSession:
    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def execute(self, *a: Any, **kw: Any) -> Any:
        return types.SimpleNamespace(scalar=lambda: True)


class _FakeRepo:
    def list_due_meetings(self, now: Any, lead_s: Any) -> list:  # pragma: no cover
        return []


class _FakeRedis:
    async def publish(self, channel: str, message: str) -> None:
        pass


async def _one_tick_of_every_loop(monkeypatch: Any, **overrides: Any) -> None:
    import meeting_api.__main__ as main_mod
    from meeting_api.service_authority import AllowAllServiceAuthority

    real_sleep = asyncio.sleep

    async def _stop(*a: Any, **kw: Any) -> None:
        raise _StopLoop()

    monkeypatch.setattr(asyncio, "sleep", _stop)
    monkeypatch.setattr(main_mod.log, "exception", lambda *a, **k: None)
    app = types.SimpleNamespace(
        state=types.SimpleNamespace(), router=types.SimpleNamespace()
    )
    repo, runtime, redis_client = _FakeRepo(), types.SimpleNamespace(), _FakeRedis()
    intake = main_mod._build_intake(
        _FakeSession,
        repo,
        runtime,
        service_authority=AllowAllServiceAuthority(),
        commands=redis_client,
    )
    main_mod._attach_background_loops(
        app,
        transcript_store=types.SimpleNamespace(),
        segment_bus=types.SimpleNamespace(),
        redis_client=redis_client,
        meeting_repo=repo,
        runtime=runtime,
        session_factory=_FakeSession,
        intake=intake,
        **overrides,
    )
    async with app.router.lifespan_context(app):
        await real_sleep(0.05)


async def test_a_sweep_that_runs_to_its_end_stamps_its_last_run(monkeypatch):
    import meeting_api.bot_spawn.auto_join as auto_join_mod

    async def _tick(*a: Any, **kw: Any) -> dict:
        return {}

    monkeypatch.setattr(auto_join_mod, "auto_join_tick", _tick)
    started = time.time()
    await _one_tick_of_every_loop(monkeypatch)
    assert value("aw_sweep_last_run_timestamp_seconds", sweep="auto-join") >= started


async def test_a_sweep_that_fails_does_not_stamp(monkeypatch):
    import meeting_api.bot_spawn.auto_join as auto_join_mod

    async def _tick(*a: Any, **kw: Any) -> dict:
        raise RuntimeError("identity went away")

    monkeypatch.setattr(auto_join_mod, "auto_join_tick", _tick)
    metrics.registry()  # built before the read below
    before = value("aw_sweep_last_run_timestamp_seconds", sweep="auto-join")
    await _one_tick_of_every_loop(monkeypatch)
    assert value("aw_sweep_last_run_timestamp_seconds", sweep="auto-join") == before


async def test_the_sender_loop_stamps_its_last_run(monkeypatch):
    import meeting_api.webhooks.sender as sender_mod

    class _Sender:
        def __init__(self, *a: Any, **kw: Any) -> None:
            pass

        async def run_once(self) -> int:
            return 0

    monkeypatch.setattr(sender_mod, "WebhookSender", _Sender)
    monkeypatch.setenv("ADMIN_API_URL", "http://admin-api.test")
    started = time.time()
    await _one_tick_of_every_loop(monkeypatch, webhook_secret_box=object())
    assert (
        value("aw_sweep_last_run_timestamp_seconds", sweep="webhook-sender") >= started
    )


# ── no secret-bearing label ──────────────────────────────────────────────────────────────────

_SAFE_VALUE = re.compile(r"^[A-Za-z0-9_.:\- /{}]*$")


async def test_no_label_carries_a_key_secret_url_query_or_transcript():
    # every site, fed inputs that carry a URL with a query, a key, a secret and transcript text
    metrics.intake_request("PUT /v2/entries", "created", "vxa_bot_secretkey", 0.01)
    metrics.intake_request("PUT /v2/entries", "created", "1", 0.01)
    await _export_results()
    sender, _ = _sender(200)
    await sender.run_once()
    await _not_sent()
    _, text = scrape(StaticSource(COUNTS))
    values = [
        v
        for family in text_string_to_metric_families(text)
        for s in family.samples
        for k, v in s.labels.items()
        if k != "le"
    ]
    assert values
    for v in values:
        assert _SAFE_VALUE.match(v), v
        assert "://" not in v and "?" not in v and "=" not in v, v
        for forbidden in (
            "secret",
            "token",
            "pwd",
            "whsec",
            "vxa_",
            "hello there",
            "s3:",
        ):
            assert forbidden not in v.lower(), v
    assert WEBHOOK_URL not in text and WEBHOOK_SECRET not in text


# ── the Postgres source ──────────────────────────────────────────────────────────────────────

pg = pytest.mark.skipif(
    not os.getenv("MEETING_API_TEST_DATABASE_URL"),
    reason="real-Postgres proofs for §1.13; set MEETING_API_TEST_DATABASE_URL to run",
)


@pg
async def test_the_postgres_source_counts_meetings_deliveries_and_the_outbox(
    intake_pg_engine,
):
    pytest.importorskip("asyncpg", reason="see test_intake_pg_schema.py's docstring")
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from admin_api.schema import models as admin_models
    from admin_api.schema import sync as admin_sync

    engine = intake_pg_engine
    await admin_sync.ensure_schema(engine, admin_models.Base)
    now = datetime.now(UTC)
    async with engine.begin() as conn:
        for user_id, status, native in (
            (7, "scheduled", "aaa-aaaa-aaa"),
            (7, "scheduled", "bbb-bbbb-bbb"),
            (7, "active", "ccc-cccc-ccc"),
            (9, "requested", "ddd-dddd-ddd"),
            (7, "completed", "eee-eeee-eee"),
            (7, "failed", "fff-ffff-fff"),
        ):
            await conn.execute(
                text(
                    "INSERT INTO meetings (user_id, platform, platform_specific_id, status, data) "
                    "VALUES (:u, 'google_meet', :n, :s, '{}'::jsonb)"
                ),
                {"u": user_id, "n": native, "s": status},
            )
        for event_id, published in (("evt_a", now), ("evt_b", None), ("evt_c", None)):
            await conn.execute(
                text(
                    "INSERT INTO webhook_outbox (event_id, meeting_id, event_type, sequence, "
                    "payload_text, published_at) VALUES (:e, NULL, 'meeting.completed', 0, '{}', "
                    ":p)"
                ),
                {"e": event_id, "p": published},
            )
        for state, due, user_id in (
            ("pending", now - timedelta(seconds=5), 7),
            ("sending", now - timedelta(seconds=5), 7),
            ("pending", now + timedelta(hours=1), 7),
            ("delivered", now - timedelta(seconds=5), 7),
            ("pending", now - timedelta(seconds=5), 9),
        ):
            await conn.execute(
                text(
                    "INSERT INTO webhook_deliveries (event_id, subscription_id, user_id, state, "
                    "attempts, next_attempt_at) VALUES ('evt_a', CAST(:s AS uuid), :u, :st, 0, "
                    ":due)"
                ),
                {"s": str(uuid.uuid4()), "u": user_id, "st": state, "due": due},
            )
    source = metrics.PostgresMetricsSource(
        async_sessionmaker(engine, expire_on_commit=False)
    )
    counts = await source.read()
    assert sorted(counts.by_status) == [
        (7, "active", 1),
        (7, "scheduled", 2),
        (9, "requested", 1),
    ]
    assert sorted(counts.pending) == [(7, 2), (9, 1)]
    assert counts.outbox_unpublished == 2
