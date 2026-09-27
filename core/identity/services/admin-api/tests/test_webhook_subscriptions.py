"""Webhook subscriptions (§2.7): the /v2/webhooks routes, the internal read, the URL guard.

The URL guard and the event vocabulary run offline. The routes run against a real Postgres when
``MEETING_API_TEST_DATABASE_URL`` is set (the throwaway ``aw-intake-pg`` container), and skip
cleanly otherwise — deleting, pausing and quota are transactional and a fake would prove nothing.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from admin_api.app.secret_box import KeyRingError, SecretBox
from admin_api.app.url_guard import (
    UrlRefused,
    check_subscription_url,
    check_subscription_url_off_loop,
    parse_allowlist,
)
from admin_api.app.webhook_subscriptions import (
    EVENT_TYPES,
    HttpWebhookTestSender,
    WebhookDeps,
    WebhookSettings,
    WebhookTestUnavailable,
)
from gateway_identity import via_gateway

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts" / "webhook-subscriptions"
SECRET_VECTORS = json.loads((CONTRACTS / "secret-box.vectors.json").read_text())
URL_VECTORS = json.loads((CONTRACTS / "url-guard.vectors.json").read_text())
RING = SECRET_VECTORS["key_ring"]
RING_JSON = json.dumps(RING)
PORTAL = "portal.notetaker.svc.cluster.local"
INTERNAL_SECRET = "test-internal-secret"
PG_URL = os.getenv("MEETING_API_TEST_DATABASE_URL")


def _webhook_schema() -> dict:
    rel = Path("meetings") / "contracts" / "webhook.v1" / "webhook.schema.json"
    for parent in Path(__file__).resolve().parents:
        if (parent / rel).is_file():
            return json.loads((parent / rel).read_text())
    raise FileNotFoundError(rel)


# ── offline: vocabulary, URL guard, key ring at boot, the test hand-off client ───────────────


def test_event_types_are_the_sealed_webhook_v1_enum():
    assert EVENT_TYPES == set(_webhook_schema()["$defs"]["EventType"]["enum"])


@pytest.mark.parametrize(
    "case",
    URL_VECTORS["vectors"],
    ids=lambda c: f"{c['expect']}:{c['url']}:{','.join(c['allowlist'])}",
)
def test_url_guard_follows_the_shared_vectors(case):
    def resolver(host):
        return list(case["resolves_to"] or [])

    if case["expect"] == "allow":
        check_subscription_url(
            case["url"], allowlist=case["allowlist"], resolver=resolver
        )
    else:
        with pytest.raises(UrlRefused):
            check_subscription_url(
                case["url"], allowlist=case["allowlist"], resolver=resolver
            )


def test_an_allow_listed_host_is_never_resolved():
    def resolver(host):
        raise AssertionError("an allow-listed host must not be resolved")

    check_subscription_url(
        f"http://{PORTAL}/hooks", allowlist=parse_allowlist(PORTAL), resolver=resolver
    )


def test_a_refusal_never_echoes_the_url():
    with pytest.raises(UrlRefused) as ei:
        check_subscription_url(
            "http://10.0.0.1/x?token=abcd1234", allowlist=(), resolver=lambda h: []
        )
    assert "abcd1234" not in str(ei.value) and "10.0.0.1" not in str(ei.value)


def test_the_allow_list_setting_is_comma_separated_and_case_folded():
    assert parse_allowlist(" Portal.Notetaker.svc.cluster.local , other.svc ,") == {
        PORTAL,
        "other.svc",
    }
    assert WebhookSettings().private_host_allowlist == {PORTAL}


def test_a_bad_key_ring_refuses_to_build_the_app(monkeypatch):
    from admin_api.app.main import create_app

    monkeypatch.setenv("WEBHOOK_SECRET_ENC_KEYS", '{"k1": "AAECAwQFBgcICQoLDA0ODw=="}')
    monkeypatch.setenv("WEBHOOK_SECRET_ENC_ACTIVE_KEY", "k1")
    with pytest.raises(KeyRingError):
        create_app()


def test_the_settings_are_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("WEBHOOK_MAX_SUBSCRIPTIONS", "3")
    monkeypatch.setenv("WEBHOOK_PRIVATE_HOST_ALLOWLIST", "a.svc,b.svc")
    settings = WebhookSettings.from_env()
    assert settings.max_subscriptions == 3
    assert settings.private_host_allowlist == {"a.svc", "b.svc"}


def test_the_test_hand_off_posts_to_meeting_api_with_the_internal_secret():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["secret"] = request.headers.get("x-internal-secret")
        seen["body"] = json.loads(request.content)
        return httpx.Response(202, json={"event_id": "evt_test_1", "extra": "dropped"})

    sender = HttpWebhookTestSender(
        "http://meeting-api:8080/",
        INTERNAL_SECRET,
        transport=httpx.MockTransport(handler),
    )
    event_id = asyncio.run(sender.send_test(7, "sub-1"))

    assert event_id == "evt_test_1"
    assert seen == {
        "url": "http://meeting-api:8080/internal/webhooks/test",
        "secret": INTERNAL_SECRET,
        "body": {"user_id": 7, "subscription_id": "sub-1"},
    }


@pytest.mark.parametrize("status", [401, 404, 500])
def test_the_test_hand_off_fails_on_a_refusal(status):
    sender = HttpWebhookTestSender(
        "http://meeting-api:8080",
        INTERNAL_SECRET,
        transport=httpx.MockTransport(lambda r: httpx.Response(status)),
    )
    with pytest.raises(WebhookTestUnavailable):
        asyncio.run(sender.send_test(7, "sub-1"))


@pytest.mark.parametrize("reply", [{}, {"event_id": ""}, {"event_id": 7}, ["evt_x"]])
def test_the_test_hand_off_fails_on_a_reply_without_an_event_id(reply):
    sender = HttpWebhookTestSender(
        "http://meeting-api:8080",
        INTERNAL_SECRET,
        transport=httpx.MockTransport(lambda r: httpx.Response(202, json=reply)),
    )
    with pytest.raises(WebhookTestUnavailable):
        asyncio.run(sender.send_test(7, "sub-1"))


def test_the_url_check_runs_off_the_event_loop():
    """A resolver that blocks holds up only its own check: another coroutine on the same loop
    runs meanwhile."""
    entered, release = threading.Event(), threading.Event()
    order: list[str] = []

    def resolver(host):
        entered.set()
        release.wait(5)
        order.append("resolved")
        return ["93.184.216.34"]

    async def scenario():
        check = asyncio.create_task(
            check_subscription_url_off_loop(
                "https://slow.example.com/aw",
                allowlist=(),
                resolver=resolver,
                timeout_s=5,
            )
        )
        while not entered.is_set():
            await asyncio.sleep(0.01)
        order.append("other coroutine ran")
        release.set()
        await check

    asyncio.run(scenario())
    assert order == ["other coroutine ran", "resolved"]


def test_a_url_check_past_its_timeout_is_refused():
    release = threading.Event()

    def resolver(host):
        release.wait(5)
        return ["93.184.216.34"]

    try:
        with pytest.raises(UrlRefused) as ei:
            asyncio.run(
                check_subscription_url_off_loop(
                    "https://slow.example.com/aw",
                    allowlist=(),
                    resolver=resolver,
                    timeout_s=0.1,
                )
            )
        assert "slow.example.com" not in str(ei.value)
    finally:
        release.set()


@pytest.mark.parametrize("value", ["0", "-3", "many"])
def test_a_subscription_quota_below_one_is_refused_at_load(monkeypatch, value):
    from admin_api.app.main import create_app

    monkeypatch.setenv("WEBHOOK_MAX_SUBSCRIPTIONS", value)
    with pytest.raises(ValueError):
        WebhookSettings.from_env()
    with pytest.raises(ValueError):
        create_app()


def test_a_constraint_violation_is_not_retryable():
    from sqlalchemy.exc import IntegrityError, OperationalError

    from admin_api.app.webhook_subscriptions import _retryable

    assert _retryable(IntegrityError("INSERT", {}, Exception("dup"))) is False
    assert _retryable(OperationalError("SELECT", {}, Exception("down"))) is True


def test_the_test_hand_off_fails_without_the_internal_secret():
    sender = HttpWebhookTestSender("http://meeting-api:8080", "")
    with pytest.raises(WebhookTestUnavailable):
        asyncio.run(sender.send_test(7, "sub-1"))


# ── real Postgres: the routes ────────────────────────────────────────────────────────────────

needs_pg = pytest.mark.skipif(
    not PG_URL,
    reason="real-Postgres webhook routes; set MEETING_API_TEST_DATABASE_URL to run",
)


class FakeTestSender:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str]] = []
        self.fail = False

    async def send_test(self, user_id: int, subscription_id: str) -> str:
        if self.fail:
            raise WebhookTestUnavailable("meeting-api answered 503")
        self.calls.append((user_id, subscription_id))
        return f"evt_test_{len(self.calls)}"


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now


def _resolver(host: str) -> list[str]:
    return {
        "hooks.example.com": ["93.184.216.34"],
        "other.example.com": ["93.184.216.35"],
        "internal.example.com": ["10.1.2.3"],
        PORTAL: ["10.100.0.7"],
    }.get(host, [])


def _box(active: str = "k1") -> SecretBox:
    return SecretBox.from_settings(RING_JSON, active)


@pytest.fixture(scope="module")
def sync_engine():
    if not PG_URL:
        pytest.skip("MEETING_API_TEST_DATABASE_URL is not set")
    from sqlalchemy import create_engine

    from admin_api.schema.models import Base
    from admin_api.schema.sync import ensure_schema_sync

    engine = create_engine(PG_URL.replace("+asyncpg", "+psycopg"))
    ensure_schema_sync(engine, Base)
    yield engine
    engine.dispose()


def _sql(engine, statement: str, **params):
    from sqlalchemy import text

    with engine.begin() as conn:
        result = conn.execute(text(statement), params)
        return result.fetchall() if result.returns_rows else None


@pytest.fixture()
def env(sync_engine, monkeypatch):
    from fastapi.testclient import TestClient

    from admin_api.app import db as app_db
    from admin_api.app.main import create_app

    _sql(
        sync_engine,
        "TRUNCATE webhook_delivery_attempts, webhook_deliveries, webhook_outbox, "
        "webhook_subscriptions, api_tokens, users RESTART IDENTITY CASCADE",
    )
    for uid in (1, 2):
        _sql(
            sync_engine,
            "INSERT INTO users (id, email, max_concurrent_bots, data, created_at) "
            "VALUES (:id, :email, 3, '{}'::jsonb, now())",
            id=uid,
            email=f"user{uid}@example.com",
        )
    monkeypatch.setenv("INTERNAL_API_SECRET", INTERNAL_SECRET)
    sender, clock = FakeTestSender(), Clock()
    deps = WebhookDeps(
        secret_box=_box("k1"),
        test_sender=sender,
        settings=WebhookSettings(
            max_subscriptions=20, private_host_allowlist=parse_allowlist(PORTAL)
        ),
        resolver=_resolver,
        clock=clock,
    )
    app_db.configure(PG_URL)
    with TestClient(via_gateway(create_app(webhooks=deps))) as client:
        yield {
            "client": client,
            "deps": deps,
            "sender": sender,
            "clock": clock,
            "engine": sync_engine,
        }
    _dispose(app_db)


def _dispose(app_db) -> None:
    """Best-effort: the pool's connections belong to the TestClient's loop, now closed."""
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(app_db.get_engine().dispose())
    except Exception:
        pass
    finally:
        loop.close()


def _as(user: int) -> dict:
    return {"x-user-id": str(user)}


def _create(env, user=1, **body):
    payload = {"url": "https://hooks.example.com/aw", "events": [], **body}
    return env["client"].post("/v2/webhooks", json=payload, headers=_as(user))


def _row(env, sid):
    rows = _sql(
        env["engine"], "SELECT * FROM webhook_subscriptions WHERE id = :id", id=sid
    )
    return rows[0]._mapping if rows else None


def _internal(env, user=1, secret=INTERNAL_SECRET):
    headers = {"X-Internal-Secret": secret} if secret is not None else {}
    return env["client"].get(
        f"/internal/users/{user}/webhook-subscriptions", headers=headers
    )


def _seed_delivery(
    env, sid, state, *, user=1, event_type="meeting.updated", attempts=0
):
    event_id = f"evt_{uuid.uuid4().hex}"
    _sql(
        env["engine"],
        "INSERT INTO webhook_outbox (event_id, event_type, sequence, payload_text, "
        "created_at, published_at) VALUES (:e, :t, 1, '{}', now(), now())",
        e=event_id,
        t=event_type,
    )
    (row,) = _sql(
        env["engine"],
        "INSERT INTO webhook_deliveries (event_id, subscription_id, user_id, state, attempts, "
        "next_attempt_at, created_at) VALUES (:e, :s, :u, :st, :a, now(), now()) RETURNING id",
        e=event_id,
        s=sid,
        u=user,
        st=state,
        a=attempts,
    )
    for n in range(1, attempts + 1):
        _sql(
            env["engine"],
            "INSERT INTO webhook_delivery_attempts (delivery_id, attempt, outcome, status_code, "
            "duration_ms, created_at) VALUES (:d, :n, 'retry', 503, 12, now())",
            d=row[0],
            n=n,
        )
    return row[0]


def _states(env, sid):
    return {
        r[0]: r[1]
        for r in _sql(
            env["engine"],
            "SELECT id, state FROM webhook_deliveries WHERE subscription_id = :s",
            s=sid,
        )
    }


@needs_pg
def test_a_generated_secret_is_returned_once_and_stored_sealed(env):
    r = _create(env, description="portal")
    assert r.status_code == 201, r.text
    body = r.json()
    secret = body["secret"]
    assert len(secret) >= 32
    assert body["secret_last4"] == secret[-4:]
    assert (
        body["events"] == []
        and body["active"] is True
        and body["description"] == "portal"
    )

    row = _row(env, body["id"])
    assert row["enc_key_id"] == "k1"
    assert secret.encode() not in bytes(row["secret_enc"])
    assert _box().decrypt(bytes(row["secret_enc"]), "k1") == secret

    listed = env["client"].get("/v2/webhooks", headers=_as(1)).json()["subscriptions"]
    assert [s["id"] for s in listed] == [body["id"]]
    assert "secret" not in listed[0]
    assert secret not in json.dumps(listed)


@needs_pg
def test_a_supplied_secret_is_never_echoed(env):
    supplied = "receiver-owned-secret-000042"
    r = _create(env, secret=supplied, events=["meeting.completed", "meeting.completed"])
    assert r.status_code == 201
    assert "secret" not in r.json()
    assert supplied not in r.text
    assert r.json()["secret_last4"] == "0042"
    assert r.json()["events"] == ["meeting.completed"]
    assert (
        _box().decrypt(bytes(_row(env, r.json()["id"])["secret_enc"]), "k1") == supplied
    )


@needs_pg
@pytest.mark.parametrize(
    "body,field",
    [
        ({"events": ["meeting.exploded"]}, "events"),
        ({"events": "meeting.completed"}, "events"),
        ({"url": ""}, "url"),
        ({"secret": "short"}, "secret"),
        ({"surprise": 1}, "surprise"),
        ({"url": "https://hooks.example.com/" + "a" * 2100}, "url"),
    ],
)
def test_an_invalid_body_is_invalid_request(env, body, field):
    r = _create(env, **body)
    assert r.status_code == 400
    error = r.json()["error"]
    assert error["code"] == "invalid_request"
    assert error["message"].startswith(field)
    if "secret" in body:
        assert body["secret"] not in r.text


@needs_pg
def test_events_are_required(env):
    r = env["client"].post(
        "/v2/webhooks", json={"url": "https://hooks.example.com/aw"}, headers=_as(1)
    )
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_request"


@needs_pg
@pytest.mark.parametrize(
    "url",
    [
        "http://10.0.0.1/hook",
        "http://localhost:8080/hook",
        "http://169.254.169.254/latest",
        "https://internal.example.com/hook",
        "https://nowhere.example.com/hook",
    ],
)
def test_private_urls_are_refused(env, url):
    r = _create(env, url=url)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_request"
    assert r.json()["error"]["message"].startswith("url:")
    assert _sql(env["engine"], "SELECT count(*) FROM webhook_subscriptions")[0][0] == 0


@needs_pg
def test_an_allow_listed_private_host_is_accepted_and_only_when_listed(env):
    assert _create(env, url=f"http://{PORTAL}/api/hooks").status_code == 201
    env["deps"].settings = WebhookSettings(
        max_subscriptions=20, private_host_allowlist=frozenset()
    )
    assert _create(env, url=f"http://{PORTAL}/api/hooks").status_code == 400


@needs_pg
def test_the_21st_subscription_is_quota_exceeded(env):
    for n in range(20):
        assert _create(env, url=f"https://hooks.example.com/{n}").status_code == 201
    r = _create(env, url="https://hooks.example.com/21")
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "quota_exceeded"
    assert "retry-after" not in {k.lower() for k in r.headers}
    count = _sql(
        env["engine"], "SELECT count(*) FROM webhook_subscriptions WHERE user_id = 1"
    )
    assert count[0][0] == 20
    # the quota is per account
    assert _create(env, user=2).status_code == 201


@needs_pg
def test_concurrent_adds_never_pass_the_quota(env):
    env["deps"].settings = WebhookSettings(
        max_subscriptions=3, private_host_allowlist=frozenset()
    )
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=6) as pool:
        codes = list(
            pool.map(
                lambda n: _create(
                    env, url=f"https://hooks.example.com/{n}"
                ).status_code,
                range(6),
            )
        )
    assert sorted(codes) == [201, 201, 201, 429, 429, 429]


@needs_pg
def test_pausing_cancels_pending_deliveries_and_nothing_else(env):
    sid = _create(env).json()["id"]
    other = _create(env, url="https://other.example.com/aw").json()["id"]
    pending = _seed_delivery(env, sid, "pending")
    sending = _seed_delivery(env, sid, "sending")
    delivered = _seed_delivery(env, sid, "delivered")
    failed = _seed_delivery(env, sid, "failed")
    other_pending = _seed_delivery(env, other, "pending")

    r = env["client"].patch(
        f"/v2/webhooks/{sid}", json={"active": False}, headers=_as(1)
    )

    assert r.status_code == 200 and r.json()["active"] is False
    assert _states(env, sid) == {
        pending: "cancelled",
        sending: "cancelled",
        delivered: "delivered",
        failed: "failed",
    }
    assert _states(env, other) == {other_pending: "pending"}


@needs_pg
def test_a_patch_that_does_not_pause_cancels_nothing(env):
    sid = _create(env).json()["id"]
    pending = _seed_delivery(env, sid, "pending")
    r = env["client"].patch(
        f"/v2/webhooks/{sid}",
        json={"events": ["meeting.completed"], "description": None},
        headers=_as(1),
    )
    assert r.status_code == 200
    assert (
        r.json()["events"] == ["meeting.completed"] and r.json()["description"] is None
    )
    assert _states(env, sid) == {pending: "pending"}


@needs_pg
def test_a_failed_pause_changes_nothing(env, monkeypatch):
    """Pause and cancellation commit together: when the cancellation fails, the subscription
    stays active."""
    from sqlalchemy.ext.asyncio import AsyncSession

    sid = _create(env).json()["id"]
    pending = _seed_delivery(env, sid, "pending")
    real_execute = AsyncSession.execute

    async def failing_execute(self, statement, *args, **kwargs):
        if (
            "webhook_deliveries" in str(statement)
            and "UPDATE" in str(statement).upper()
        ):
            from sqlalchemy.exc import OperationalError

            raise OperationalError("UPDATE webhook_deliveries", {}, Exception("boom"))
        return await real_execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "execute", failing_execute)
    r = env["client"].patch(
        f"/v2/webhooks/{sid}", json={"active": False}, headers=_as(1)
    )
    monkeypatch.setattr(AsyncSession, "execute", real_execute)

    assert r.status_code == 503 and r.json()["error"]["code"] == "unavailable"
    assert _row(env, sid)["active"] is True
    assert _states(env, sid) == {pending: "pending"}


@needs_pg
def test_deleting_cancels_pending_deliveries_and_keeps_the_log(env):
    sid = _create(env).json()["id"]
    pending = _seed_delivery(env, sid, "pending")
    delivered = _seed_delivery(env, sid, "delivered")

    r = env["client"].delete(f"/v2/webhooks/{sid}", headers=_as(1))

    assert r.status_code == 204
    assert _row(env, sid) is None
    assert _states(env, sid) == {pending: "cancelled", delivered: "delivered"}
    again = env["client"].delete(f"/v2/webhooks/{sid}", headers=_as(1))
    assert again.status_code == 404
    assert again.json()["error"]["code"] == "webhook_not_found"


@needs_pg
def test_another_accounts_subscription_is_not_found(env):
    sid = _create(env).json()["id"]
    pending = _seed_delivery(env, sid, "pending")
    client = env["client"]
    for r in (
        client.patch(f"/v2/webhooks/{sid}", json={"active": False}, headers=_as(2)),
        client.delete(f"/v2/webhooks/{sid}", headers=_as(2)),
        client.post(f"/v2/webhooks/{sid}/rotate-secret", json={}, headers=_as(2)),
        client.post(f"/v2/webhooks/{sid}/test", headers=_as(2)),
        client.get(f"/v2/webhooks/{sid}/deliveries", headers=_as(2)),
        client.get("/v2/webhooks/not-a-uuid/deliveries", headers=_as(1)),
    ):
        assert r.status_code == 404, r.text
        assert r.json()["error"]["code"] == "webhook_not_found"
    assert _row(env, sid)["active"] is True
    assert _states(env, sid) == {pending: "pending"}
    assert client.get("/v2/webhooks", headers=_as(2)).json() == {"subscriptions": []}


@needs_pg
def test_a_patch_to_a_private_url_is_refused_and_changes_nothing(env):
    sid = _create(env).json()["id"]
    r = env["client"].patch(
        f"/v2/webhooks/{sid}", json={"url": "http://10.0.0.1/x"}, headers=_as(1)
    )
    assert r.status_code == 400
    assert _row(env, sid)["url"] == "https://hooks.example.com/aw"
    for body in ({}, {"url": None}, {"active": None}):
        r = env["client"].patch(f"/v2/webhooks/{sid}", json=body, headers=_as(1))
        assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_request"


@needs_pg
def test_a_missing_identity_is_unauthorized(env):
    r = env["client"].get("/v2/webhooks")
    assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized"


@needs_pg
def test_rotation_keeps_the_old_secret_for_24_hours(env):
    first = _create(env).json()
    sid, old_secret = first["id"], first["secret"]
    old_row = _row(env, sid)

    r = env["client"].post(f"/v2/webhooks/{sid}/rotate-secret", json={}, headers=_as(1))

    assert r.status_code == 200
    new_secret = r.json()["secret"]
    assert new_secret != old_secret and r.json()["secret_last4"] == new_secret[-4:]
    row = _row(env, sid)
    assert bytes(row["previous_secret_enc"]) == bytes(old_row["secret_enc"])
    assert row["previous_enc_key_id"] == "k1"
    assert row["previous_secret_expires_at"] == env["clock"].now + timedelta(hours=24)
    assert _box().decrypt(bytes(row["secret_enc"]), row["enc_key_id"]) == new_secret

    (sub,) = _internal(env).json()["subscriptions"]
    box = _box()
    assert (
        box.decrypt(base64.b64decode(sub["secret_enc"]), sub["enc_key_id"])
        == new_secret
    )
    assert (
        box.decrypt(
            base64.b64decode(sub["previous_secret_enc"]),
            sub["previous_enc_key_id"],
        )
        == old_secret
    )
    assert sub["previous_secret_expires_at"] == "2026-09-28T12:00:00Z"

    env["clock"].now += timedelta(hours=23, minutes=59)
    assert _internal(env).json()["subscriptions"][0]["previous_secret_enc"] is not None
    env["clock"].now += timedelta(minutes=2)
    (sub,) = _internal(env).json()["subscriptions"]
    assert sub["previous_secret_enc"] is None
    assert sub["previous_enc_key_id"] is None
    assert sub["previous_secret_expires_at"] is None


@needs_pg
def test_rotation_to_a_supplied_secret_does_not_echo_it(env):
    sid = _create(env).json()["id"]
    supplied = "receiver-rotated-secret-9999"
    r = env["client"].post(
        f"/v2/webhooks/{sid}/rotate-secret", json={"secret": supplied}, headers=_as(1)
    )
    assert r.status_code == 200 and "secret" not in r.json() and supplied not in r.text
    assert r.json()["secret_last4"] == "9999"


@needs_pg
def test_the_internal_read_returns_active_subscriptions_ciphertext_only(env):
    active = _create(env).json()
    paused = _create(env, url="https://other.example.com/aw").json()
    env["client"].patch(
        f"/v2/webhooks/{paused['id']}", json={"active": False}, headers=_as(1)
    )
    _create(env, user=2)

    r = _internal(env)

    assert r.status_code == 200
    body = r.json()
    assert body["user_id"] == 1
    (sub,) = body["subscriptions"]
    assert sub["id"] == active["id"]
    assert set(sub) == {
        "id",
        "url",
        "events",
        "secret_enc",
        "enc_key_id",
        "previous_secret_enc",
        "previous_enc_key_id",
        "previous_secret_expires_at",
    }
    assert active["secret"] not in r.text
    assert sub["previous_secret_enc"] is None


@needs_pg
def test_the_internal_read_needs_the_internal_secret(env, monkeypatch):
    assert _internal(env, secret=None).status_code == 403
    assert _internal(env, secret="wrong").status_code == 403
    monkeypatch.delenv("INTERNAL_API_SECRET")
    monkeypatch.setenv("DEV_MODE", "true")
    assert _internal(env, secret=None).status_code == 503


@needs_pg
def test_a_row_under_the_old_key_is_read_and_rewrapped_after_the_active_key_changes(
    env,
):
    first = _create(env).json()
    sid, secret = first["id"], first["secret"]
    old = (
        env["client"]
        .post(f"/v2/webhooks/{sid}/rotate-secret", json={}, headers=_as(1))
        .json()
    )
    newer = old["secret"]
    assert _row(env, sid)["enc_key_id"] == "k1"

    env["deps"].secret_box = _box("k2")
    (sub,) = _internal(env).json()["subscriptions"]

    only_k2 = SecretBox.from_settings(json.dumps({"k2": RING["k2"]}), "k2")
    assert sub["enc_key_id"] == "k2" and sub["previous_enc_key_id"] == "k2"
    assert only_k2.decrypt(base64.b64decode(sub["secret_enc"]), "k2") == newer
    assert only_k2.decrypt(base64.b64decode(sub["previous_secret_enc"]), "k2") == secret
    row = _row(env, sid)
    assert row["enc_key_id"] == "k2" and row["previous_enc_key_id"] == "k2"
    assert base64.b64encode(bytes(row["secret_enc"])).decode() == sub["secret_enc"]
    # a second read has nothing left to re-seal
    again = _internal(env).json()["subscriptions"][0]
    assert again["secret_enc"] == sub["secret_enc"]


@needs_pg
def test_rows_under_a_key_missing_from_the_ring_are_left_alone_logged_once_unlocked(
    env, caplog
):
    """A key id the ring doesn't hold can't be re-sealed, so such a row isn't stale: it is
    returned as stored, the read logs it once, and it takes no row lock — a writer holding one
    doesn't block the read."""
    first = _create(env).json()["id"]
    second = _create(env, url="https://other.example.com/aw").json()["id"]
    before = bytes(_row(env, first)["secret_enc"])
    _sql(env["engine"], "UPDATE webhook_subscriptions SET enc_key_id = 'k0'")

    from sqlalchemy import text

    holder = env["engine"].connect()
    tx = holder.begin()
    holder.execute(text("SELECT id FROM webhook_subscriptions FOR UPDATE"))
    result: dict = {}

    def read():
        result["response"] = _internal(env)

    try:
        with caplog.at_level(logging.WARNING, logger="admin_api.webhooks"):
            reader = threading.Thread(target=read)
            reader.start()
            reader.join(10)
            finished = not reader.is_alive()
    finally:
        tx.rollback()
        holder.close()
    reader.join(10)

    assert finished, "the read waited on a row lock"
    r = result["response"]
    assert r.status_code == 200
    assert {s["enc_key_id"] for s in r.json()["subscriptions"]} == {"k0"}
    assert bytes(_row(env, first)["secret_enc"]) == before
    missing = [rec for rec in caplog.records if "k0" in rec.getMessage()]
    assert len(missing) == 1
    assert first in missing[0].getMessage() and second in missing[0].getMessage()


@needs_pg
def test_a_blocked_resolver_does_not_freeze_other_requests(env):
    """admin-api is the gateway's validation oracle: a slow DNS answer on one save must not stall
    any other request on the loop."""
    entered, release = threading.Event(), threading.Event()

    def resolver(host):
        if host == "slow.example.com":
            entered.set()
            release.wait(10)
            return ["93.184.216.34"]
        return _resolver(host)

    env["deps"].resolver = resolver
    result: dict = {}

    def create():
        result["response"] = _create(env, url="https://slow.example.com/aw")

    writer = threading.Thread(target=create)
    writer.start()
    try:
        assert entered.wait(5)
        started = time.monotonic()
        assert env["client"].get("/health").status_code == 200
        assert env["client"].get("/v2/webhooks", headers=_as(2)).status_code == 200
        assert time.monotonic() - started < 2
        assert writer.is_alive(), "the slow save finished before the others ran"
    finally:
        release.set()
        writer.join(10)
    assert result["response"].status_code == 201


@needs_pg
def test_a_resolver_past_the_timeout_refuses_the_url(env):
    release = threading.Event()

    def resolver(host):
        release.wait(10)
        return ["93.184.216.34"]

    env["deps"].resolver = resolver
    env["deps"].url_check_timeout_s = 0.2
    try:
        r = _create(env, url="https://slow.example.com/aw")
    finally:
        release.set()
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_request"
    assert r.json()["error"]["message"].startswith("url:")


@needs_pg
def test_an_account_without_a_users_row_is_refused(env):
    r = _create(env, user=3)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "account_not_found"
    assert _sql(env["engine"], "SELECT count(*) FROM webhook_subscriptions")[0][0] == 0


@needs_pg
def test_a_constraint_violation_on_save_is_not_reported_as_retryable(env, monkeypatch):
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.ext.asyncio import AsyncSession

    async def failing_commit(self):
        raise IntegrityError("INSERT INTO webhook_subscriptions", {}, Exception("dup"))

    monkeypatch.setattr(AsyncSession, "commit", failing_commit)
    with pytest.raises(IntegrityError):
        _create(env)


@needs_pg
def test_without_a_key_ring_saving_a_secret_is_unavailable(env):
    env["deps"].secret_box = None
    r = _create(env)
    assert r.status_code == 503 and r.json()["error"]["code"] == "unavailable"
    assert _internal(env).status_code == 503
    assert env["client"].get("/v2/webhooks", headers=_as(1)).status_code == 200


@needs_pg
def test_the_test_send_goes_to_that_subscriber_through_meeting_api(env):
    sid = _create(env).json()["id"]

    r = env["client"].post(f"/v2/webhooks/{sid}/test", headers=_as(1))

    assert r.status_code == 202
    assert r.json() == {"subscription_id": sid, "event_id": "evt_test_1"}
    assert env["sender"].calls == [(1, sid)]


@needs_pg
def test_a_failed_test_hand_off_is_unavailable(env):
    sid = _create(env).json()["id"]
    env["sender"].fail = True
    r = env["client"].post(f"/v2/webhooks/{sid}/test", headers=_as(1))
    assert r.status_code == 503 and r.json()["error"]["code"] == "unavailable"


@needs_pg
def test_the_delivery_log_pages_newest_first_with_every_attempt(env):
    sid = _create(env).json()["id"]
    d1 = _seed_delivery(
        env, sid, "delivered", attempts=1, event_type="meeting.scheduled"
    )
    d2 = _seed_delivery(env, sid, "pending", attempts=2)
    d3 = _seed_delivery(env, sid, "dead", attempts=5, event_type="meeting.completed")
    client = env["client"]

    page = client.get(f"/v2/webhooks/{sid}/deliveries?limit=2", headers=_as(1)).json()

    assert [d["id"] for d in page["deliveries"]] == [d3, d2]
    assert page["next_before"] == d2
    assert page["deliveries"][0]["event_type"] == "meeting.completed"
    assert page["deliveries"][0]["state"] == "dead"
    assert [a["attempt"] for a in page["deliveries"][0]["attempt_log"]] == [
        1,
        2,
        3,
        4,
        5,
    ]
    assert page["deliveries"][0]["attempt_log"][0]["status_code"] == 503

    rest = client.get(
        f"/v2/webhooks/{sid}/deliveries?limit=2&before={page['next_before']}",
        headers=_as(1),
    ).json()
    assert [d["id"] for d in rest["deliveries"]] == [d1]
    assert rest["next_before"] is None

    for bad in ("limit=0", "limit=201", "before=0", "limit=x"):
        r = client.get(f"/v2/webhooks/{sid}/deliveries?{bad}", headers=_as(1))
        assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_request"


@needs_pg
def test_no_secret_reaches_any_response_or_log(env, caplog):
    client = env["client"]
    supplied = "receiver-owned-secret-for-log-test"
    rotated = "receiver-rotated-secret-for-log-test"
    responses = []
    with caplog.at_level(logging.DEBUG):
        created = _create(env)
        generated = created.json()["secret"]
        sid = created.json()["id"]
        responses.append(
            _create(env, url="https://other.example.com/aw", secret=supplied)
        )
        responses.append(client.get("/v2/webhooks", headers=_as(1)))
        responses.append(
            client.patch(
                f"/v2/webhooks/{sid}", json={"description": "x"}, headers=_as(1)
            )
        )
        rotate = client.post(
            f"/v2/webhooks/{sid}/rotate-secret", json={}, headers=_as(1)
        )
        second = rotate.json()["secret"]
        responses.append(
            client.post(
                f"/v2/webhooks/{sid}/rotate-secret",
                json={"secret": rotated},
                headers=_as(1),
            )
        )
        env["deps"].secret_box = _box("k2")
        internal = _internal(env)
        responses.append(client.post(f"/v2/webhooks/{sid}/test", headers=_as(1)))
        responses.append(client.get(f"/v2/webhooks/{sid}/deliveries", headers=_as(1)))
        responses.append(client.delete(f"/v2/webhooks/{sid}", headers=_as(1)))

    ciphertexts = [s["secret_enc"] for s in internal.json()["subscriptions"]] + [
        s["previous_secret_enc"]
        for s in internal.json()["subscriptions"]
        if s["previous_secret_enc"]
    ]
    plaintexts = [generated, second, supplied, rotated]
    keys = list(RING.values()) + [base64.b64decode(v).hex() for v in RING.values()]

    for r in responses:
        for value in plaintexts:
            assert value not in r.text, f"a secret leaked into {r.request.url}"
    assert created.text.count(generated) == 1 and rotate.text.count(second) == 1
    for value in plaintexts:
        assert value not in internal.text

    logged = "\n".join(
        rec.getMessage() + " " + json.dumps(getattr(rec, "fields", None), default=str)
        for rec in caplog.records
    )
    assert "webhook subscription created" in logged
    for value in plaintexts + ciphertexts + keys:
        assert value not in logged
