"""§1.8 — the subscription sender: delivery state in Postgres, leased claims, signing, retries.

The sender (``webhooks/sender.py``) runs one loop per replica. Each tick it claims due
``webhook_deliveries`` rows (``FOR UPDATE SKIP LOCKED``, state ``sending``, a 60 s lease), re-checks
the subscription is active (else ``cancelled``) and that its URL passes the SSRF guard, signs the
stored ``payload_text`` and posts it (10 s), then writes one attempt row and moves the row:
2xx → ``delivered``; 5xx, 429, timeout or connection error → retry at +60 s, +300 s, +1800 s,
+7200 s, then ``dead``; any other answer → ``failed``. Every move out of ``sending`` is guarded by
the claim (``state = 'sending'`` and the claim's own lease), so a pause or delete that cancels the
row mid-flight is never overwritten.

Every scenario runs twice: against the in-memory store below (offline) and against
``PostgresDeliveryStore`` on a real Postgres (``MEETING_API_TEST_DATABASE_URL``; skipped cleanly
when unset — see ``test_intake_pg_schema.py``'s docstring). The race between two senders is
Postgres-only.
"""

from __future__ import annotations

import asyncio
import base64
import contextvars
import hashlib
import hmac
import json
import logging
import os
import re
import threading
import uuid
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterator, Optional

import pytest

from meeting_api.webhooks.fakes import InMemoryDeliveryStore
from meeting_api.webhooks.secret_box import SecretBox
from meeting_api.webhooks.sender import (
    DEFAULT_RETRY_SCHEDULE_S,
    LEASE_MARGIN_S,
    LEASE_S,
    SEND_TIMEOUT_S,
    DeliveryResult,
    HttpxPoster,
    PostgresDeliveryStore,
    SenderSettings,
    SenderSettingsError,
    SigningSecrets,
    TransportError,
    WebhookSender,
)
from meeting_api.webhooks.ssrf import (
    DEFAULT_PRIVATE_HOST_ALLOWLIST,
    PinnedURL,
    parse_allowlist,
)
from meeting_api.webhooks.subscriptions import (
    AdminSubscriptions,
    Subscription,
    SubscriptionsUnavailable,
)

UTC = timezone.utc
T0 = datetime(2026, 9, 29, 4, 0, 0, tzinfo=UTC)
USER = 7


def _vectors() -> dict[str, Any]:
    rel = Path("identity") / "contracts" / "webhook-subscriptions"
    for parent in Path(__file__).resolve().parents:
        if (parent / rel).is_dir():
            return json.loads((parent / rel / "secret-box.vectors.json").read_text())
    raise FileNotFoundError(str(rel))


RING = _vectors()["key_ring"]
SECRET = "whsec-current-0123456789"
OLD_SECRET = "whsec-previous-9876543210"
NEW_SECRET = "whsec-rotated-5555555555"
ROTATION_WINDOW = timedelta(hours=24)  # admin-api's PREVIOUS_SECRET_TTL


def seal(plaintext: str, key_id: str = "k1") -> bytes:
    """admin-api's sealing, for fixtures: nonce || AES-GCM(ciphertext || tag)."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = os.urandom(12)
    key = base64.b64decode(RING[key_id])
    return nonce + AESGCM(key).encrypt(nonce, plaintext.encode(), b"aw-webhook-secret")


def box() -> SecretBox:
    return SecretBox.from_settings(json.dumps(RING), "k1")


def exporter_verify(
    body: bytes, headers: Mapping[str, str], secret: str, now: float
) -> bool:
    """``integrations/out/aw-notetaker/exporter/signature.py`` ``verify``, copied."""
    lowered = {k.lower(): v for k, v in headers.items()}
    signature = lowered.get("x-webhook-signature")
    timestamp = lowered.get("x-webhook-timestamp")
    if not secret or not signature or not timestamp:
        return False
    try:
        sent_at = int(timestamp)
    except ValueError:
        return False
    if abs(now - sent_at) > 300:
        return False
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256)
    return hmac.compare_digest(signature, f"sha256={mac.hexdigest()}")


def portal_verify(
    body: bytes, headers: Mapping[str, str], secret: str, now: float
) -> bool:
    """The portal's ``verifySignature`` (``portal/src/lib/aw-bots-webhook.ts``): the receiver's
    one secret must match ``X-Webhook-Signature`` or ``X-Webhook-Signature-Previous``.
    """
    lowered = {k.lower(): v for k, v in headers.items()}
    timestamp = lowered.get("x-webhook-timestamp")
    if timestamp is None or not re.fullmatch(r"[0-9]{1,12}", timestamp):
        return False
    if abs(now - int(timestamp)) > 300:
        return False
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256)
    expected = f"sha256={mac.hexdigest()}"
    return any(
        candidate is not None
        and re.fullmatch(r"sha256=[0-9a-f]{64}", candidate) is not None
        and hmac.compare_digest(candidate, expected)
        for candidate in (
            lowered.get("x-webhook-signature"),
            lowered.get("x-webhook-signature-previous"),
        )
    )


# ── fakes ────────────────────────────────────────────────────────────────────────────────────


class Clock:
    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


class StaticSubscriptions:
    """The subscription read, from a dict (``SubscriptionSource``)."""

    def __init__(self) -> None:
        self.by_user: dict[int, list[Subscription]] = {}
        self.unavailable = False
        self.on_find: Optional[Callable[[], None]] = None

    def put(self, user_id: int, sub: Subscription) -> None:
        subs = [s for s in self.by_user.get(user_id, []) if s.id != sub.id]
        self.by_user[user_id] = subs + [sub]

    async def for_account(self, user_id: int) -> list[Subscription]:
        if self.unavailable:
            raise SubscriptionsUnavailable("admin-api answered 503")
        return list(self.by_user.get(user_id, []))

    async def find(self, user_id: int, subscription_id: str) -> Optional[Subscription]:
        if self.on_find is not None:
            self.on_find()
        for sub in await self.for_account(user_id):
            if sub.id == subscription_id:
                return sub
        return None


@dataclass
class Posted:
    target: PinnedURL
    body: bytes
    headers: dict[str, str]


@dataclass
class Receiver:
    """The ``Poster``: records every post; ``answers`` scripts status codes or exceptions."""

    answers: list[Any] = field(default_factory=list)
    default: Any = 200
    posted: list[Posted] = field(default_factory=list)
    during: Optional[Callable[[], Awaitable[None]]] = None
    delay_s: float = 0.0

    async def post(
        self, target: PinnedURL, body: bytes, headers: Mapping[str, str]
    ) -> int:
        self.posted.append(Posted(target, body, dict(headers)))
        if self.during is not None:
            await self.during()
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        answer = self.answers.pop(0) if self.answers else self.default
        if isinstance(answer, BaseException):
            raise answer
        return int(answer)


# ── harnesses: one scenario, two stores ──────────────────────────────────────────────────────


class MemoryHarness:
    kind = "memory"

    def __init__(self) -> None:
        self.clock = Clock()
        self.store = InMemoryDeliveryStore(clock=self.clock)

    async def add_subscription(
        self,
        sub_id: str,
        user_id: int,
        active: bool = True,
        *,
        secrets: Optional[SigningSecrets] = None,
    ) -> None:
        self.store.active[sub_id] = active
        self.store.secrets[sub_id] = secrets or SigningSecrets(seal(SECRET), "k1")

    async def rotate(self, sub_id: str, new_secret: str) -> None:
        """admin-api's ``rotate-secret``: the current secret becomes the previous one for 24 h."""
        old = self.store.secrets[sub_id]
        self.store.secrets[sub_id] = SigningSecrets(
            seal(new_secret),
            "k1",
            previous_secret_enc=old.secret_enc,
            previous_enc_key_id=old.enc_key_id,
            previous_secret_expires_at=self.clock.now + ROTATION_WINDOW,
        )

    async def set_key_id(self, sub_id: str, key_id: str) -> None:
        self.store.secrets[sub_id] = replace(
            self.store.secrets[sub_id], enc_key_id=key_id
        )

    async def add_event(self, event_id: str, event_type: str, payload: str) -> None:
        self.store.outbox[event_id] = {
            "event_type": event_type,
            "payload_text": payload,
        }

    async def add_delivery(
        self, event_id: str, sub_id: str, user_id: int, due: datetime
    ) -> int:
        did = self.store._next_id
        self.store._next_id += 1
        self.store.deliveries[did] = {
            "id": did,
            "event_id": event_id,
            "subscription_id": sub_id,
            "user_id": user_id,
            "state": "pending",
            "attempts": 0,
            "next_attempt_at": due,
            "lease_until": None,
            "last_status_code": None,
            "last_error": None,
        }
        return did

    async def delivery(self, did: int) -> dict[str, Any]:
        return dict(self.store.deliveries[did])

    async def attempt_rows(self, did: int) -> list[dict[str, Any]]:
        return [a for a in self.store.attempts if a["delivery_id"] == did]

    async def set_active(self, sub_id: str, active: bool) -> None:
        self.store.active[sub_id] = active

    async def delete_subscription(self, sub_id: str) -> None:
        self.store.active.pop(sub_id, None)
        self.store.secrets.pop(sub_id, None)

    async def admin_cancel_pending(self, sub_id: str) -> None:
        """admin-api's pause/delete: ``state='cancelled'`` for pending and sending rows."""
        for d in self.store.deliveries.values():
            if d["subscription_id"] == sub_id and d["state"] in ("pending", "sending"):
                d.update(state="cancelled", lease_until=None)


class PgHarness:
    kind = "pg"

    def __init__(self, engine: Any) -> None:
        from sqlalchemy.ext.asyncio import async_sessionmaker

        self.engine = engine
        self.session_factory = async_sessionmaker(engine, expire_on_commit=False)
        self.clock = Clock()
        # the test clock stands in for the database's now() (production passes none)
        self.store = PostgresDeliveryStore(self.session_factory, clock=self.clock)

    async def _exec(self, sql: str, **params: Any) -> Any:
        from sqlalchemy import text

        async with self.engine.begin() as conn:
            return await conn.execute(text(sql), params)

    async def add_subscription(
        self,
        sub_id: str,
        user_id: int,
        active: bool = True,
        *,
        secrets: Optional[SigningSecrets] = None,
    ) -> None:
        sealed = secrets or SigningSecrets(seal(SECRET), "k1")
        await self._exec(
            "INSERT INTO webhook_subscriptions (id, user_id, url, secret_enc, enc_key_id, "
            "secret_last4, previous_secret_enc, previous_enc_key_id, "
            "previous_secret_expires_at, events, active) VALUES (CAST(:id AS uuid), :uid, "
            "'https://x.test/', :enc, :key, '6789', :prev, :prev_key, :prev_until, '{}', :active)",
            id=sub_id,
            uid=user_id,
            enc=sealed.secret_enc,
            key=sealed.enc_key_id,
            prev=sealed.previous_secret_enc,
            prev_key=sealed.previous_enc_key_id,
            prev_until=sealed.previous_secret_expires_at,
            active=active,
        )

    async def rotate(self, sub_id: str, new_secret: str) -> None:
        """admin-api's ``rotate-secret`` (``webhook_subscriptions.py``), column for column."""
        await self._exec(
            "UPDATE webhook_subscriptions SET previous_secret_enc = secret_enc, "
            "previous_enc_key_id = enc_key_id, previous_secret_expires_at = :until, "
            "secret_enc = :enc, enc_key_id = 'k1', secret_last4 = :last4, updated_at = :now "
            "WHERE id = CAST(:id AS uuid)",
            until=self.clock.now + ROTATION_WINDOW,
            enc=seal(new_secret),
            last4=new_secret[-4:],
            now=self.clock.now,
            id=sub_id,
        )

    async def set_key_id(self, sub_id: str, key_id: str) -> None:
        await self._exec(
            "UPDATE webhook_subscriptions SET enc_key_id = :k WHERE id = CAST(:id AS uuid)",
            k=key_id,
            id=sub_id,
        )

    async def add_event(self, event_id: str, event_type: str, payload: str) -> None:
        await self._exec(
            "INSERT INTO webhook_outbox (event_id, meeting_id, event_type, sequence, "
            "payload_text, created_at, published_at) VALUES (:e, NULL, :t, 0, :p, :now, :now)",
            e=event_id,
            t=event_type,
            p=payload,
            now=T0,
        )

    async def add_delivery(
        self, event_id: str, sub_id: str, user_id: int, due: datetime
    ) -> int:
        result = await self._exec(
            "INSERT INTO webhook_deliveries (event_id, subscription_id, user_id, state, attempts, "
            "next_attempt_at) VALUES (:e, CAST(:s AS uuid), :u, 'pending', 0, :due) RETURNING id",
            e=event_id,
            s=sub_id,
            u=user_id,
            due=due,
        )
        return int(result.scalar_one())

    async def delivery(self, did: int) -> dict[str, Any]:
        result = await self._exec(
            "SELECT * FROM webhook_deliveries WHERE id = :id", id=did
        )
        row = dict(result.mappings().one())
        row["subscription_id"] = str(row["subscription_id"])
        return row

    async def attempt_rows(self, did: int) -> list[dict[str, Any]]:
        result = await self._exec(
            "SELECT * FROM webhook_delivery_attempts WHERE delivery_id = :id ORDER BY attempt, id",
            id=did,
        )
        return [dict(r) for r in result.mappings().all()]

    async def set_active(self, sub_id: str, active: bool) -> None:
        await self._exec(
            "UPDATE webhook_subscriptions SET active = :a WHERE id = CAST(:id AS uuid)",
            a=active,
            id=sub_id,
        )

    async def delete_subscription(self, sub_id: str) -> None:
        await self._exec(
            "DELETE FROM webhook_subscriptions WHERE id = CAST(:id AS uuid)", id=sub_id
        )

    async def admin_cancel_pending(self, sub_id: str) -> None:
        await self._exec(
            "UPDATE webhook_deliveries SET state = 'cancelled', lease_until = NULL "
            "WHERE subscription_id = CAST(:id AS uuid) AND state IN ('pending', 'sending')",
            id=sub_id,
        )


def _pg_url() -> Optional[str]:
    return os.getenv("MEETING_API_TEST_DATABASE_URL")


@pytest.fixture
async def pg_engine():
    if not _pg_url():
        pytest.skip(
            "real-Postgres proofs for §1.8; set MEETING_API_TEST_DATABASE_URL to run"
        )
    pytest.importorskip("sqlalchemy", reason="see test_intake_pg_schema.py's docstring")
    pytest.importorskip("asyncpg", reason="see test_intake_pg_schema.py's docstring")
    from sqlalchemy.ext.asyncio import create_async_engine

    from admin_api.schema import models as admin_models
    from admin_api.schema import sync as admin_sync

    eng = create_async_engine(_pg_url())
    async with eng.begin() as conn:
        await conn.run_sync(admin_models.Base.metadata.drop_all)
    await admin_sync.ensure_schema(eng, admin_models.Base)
    yield eng
    async with eng.begin() as conn:
        await conn.run_sync(admin_models.Base.metadata.drop_all)
    await eng.dispose()


@pytest.fixture(params=["memory", "pg"])
def h(request):
    if request.param == "memory":
        return MemoryHarness()
    return PgHarness(request.getfixturevalue("pg_engine"))


@dataclass
class World:
    h: Any
    clock: Clock
    subs: StaticSubscriptions
    receiver: Receiver
    sender: WebhookSender

    async def subscribe(
        self,
        *,
        url: str = "https://hooks.example.com/aw?token=q-9f8e7d",
        events: tuple[str, ...] = (),
        previous: Optional[tuple[str, datetime]] = None,
        user_id: int = USER,
    ) -> Subscription:
        sub = Subscription(
            id=str(uuid.uuid4()),
            url=url,
            events=events,
            secret_enc=seal(SECRET),
            enc_key_id="k1",
            previous_secret_enc=seal(previous[0], "k2") if previous else None,
            previous_enc_key_id="k2" if previous else None,
            previous_secret_expires_at=previous[1] if previous else None,
        )
        await self.h.add_subscription(
            sub.id,
            user_id,
            secrets=SigningSecrets(
                sub.secret_enc,
                sub.enc_key_id,
                previous_secret_enc=sub.previous_secret_enc,
                previous_enc_key_id=sub.previous_enc_key_id,
                previous_secret_expires_at=sub.previous_secret_expires_at,
            ),
        )
        self.subs.put(user_id, sub)
        return sub

    async def event(
        self,
        sub: Subscription,
        *,
        event_type: str = "meeting.completed",
        user_id: int = USER,
    ) -> tuple[int, bytes]:
        event_id = "evt_" + uuid.uuid4().hex
        payload = json.dumps(
            {
                "api_version": "2026-09-25",
                "created_at": "2026-09-29T04:00:00Z",
                "data": {
                    "meeting": {"id": str(uuid.uuid4()), "title": "Café ☕ standup"}
                },
                "event_id": event_id,
                "event_type": event_type,
            },
            separators=(",", ":"),
            sort_keys=True,
            ensure_ascii=False,
        )
        await self.h.add_event(event_id, event_type, payload)
        did = await self.h.add_delivery(event_id, sub.id, user_id, self.clock.now)
        return did, payload.encode("utf-8")


def resolve_public(host: str) -> list[str]:
    return ["93.184.216.34"]


@pytest.fixture
def world(h) -> Iterator[World]:
    clock = h.clock
    subs = StaticSubscriptions()
    receiver = Receiver()
    allowlist = parse_allowlist(DEFAULT_PRIVATE_HOST_ALLOWLIST)
    sender = WebhookSender(
        h.store,
        subs,
        box(),
        receiver,
        allowlist=allowlist,
        resolver=resolve_public,
        clock=clock,
    )
    yield World(h, clock, subs, receiver, sender)
    sender.close()


# ── delivered ────────────────────────────────────────────────────────────────────────────────


async def test_a_2xx_delivers_the_stored_bytes_signed(world):
    sub = await world.subscribe()
    did, body = await world.event(sub)

    assert await world.sender.run_once() == 1

    (post,) = world.receiver.posted
    assert post.body == body  # the stored payload_text, never re-serialised
    assert exporter_verify(body, post.headers, SECRET, now=T0.timestamp())
    assert post.headers["X-Webhook-Timestamp"] == str(int(T0.timestamp()))
    assert "X-Webhook-Signature-Previous" not in post.headers
    assert not {k.lower() for k in post.headers} & {"authorization"}
    assert str(post.target) == sub.url
    row = await world.h.delivery(did)
    assert (row["state"], row["attempts"], row["lease_until"]) == ("delivered", 1, None)
    assert row["last_status_code"] == 200
    (attempt,) = await world.h.attempt_rows(did)
    assert (attempt["attempt"], attempt["outcome"], attempt["status_code"]) == (
        1,
        "delivered",
        200,
    )
    assert attempt["duration_ms"] is not None
    # delivered is final: the next tick has nothing to claim
    world.clock.advance(3600)
    assert await world.sender.run_once() == 0
    assert len(world.receiver.posted) == 1


# ── retries ──────────────────────────────────────────────────────────────────────────────────


async def test_the_retry_schedule_then_dead_with_byte_identical_payloads(world):
    sub = await world.subscribe()
    did, body = await world.event(sub)
    world.receiver.answers = [500, 503, 429, TransportError("ConnectError"), 502]

    expected_waits = list(DEFAULT_RETRY_SCHEDULE_S)
    assert expected_waits == [60, 300, 1800, 7200]
    for n, wait in enumerate(expected_waits, start=1):
        assert await world.sender.run_once() == 1
        row = await world.h.delivery(did)
        assert row["state"] == "pending"
        assert row["attempts"] == n
        assert row["lease_until"] is None
        assert row["next_attempt_at"] == world.clock.now + timedelta(seconds=wait)
        # not due a second early
        world.clock.advance(wait - 1)
        assert await world.sender.run_once() == 0
        world.clock.advance(1)

    assert await world.sender.run_once() == 1
    row = await world.h.delivery(did)
    assert (row["state"], row["attempts"]) == ("dead", 5)
    world.clock.advance(86_400)
    assert await world.sender.run_once() == 0

    assert [p.body for p in world.receiver.posted] == [body] * 5
    attempts = await world.h.attempt_rows(did)
    assert [a["attempt"] for a in attempts] == [1, 2, 3, 4, 5]
    assert [a["outcome"] for a in attempts] == ["retry"] * 4 + ["dead"]
    assert [a["status_code"] for a in attempts] == [500, 503, 429, None, 502]
    assert attempts[3]["error"] == "connection error (ConnectError)"
    # each retry is signed afresh at its own time
    stamps = [int(p.headers["X-Webhook-Timestamp"]) for p in world.receiver.posted]
    assert stamps == sorted(set(stamps))


async def test_a_timeout_is_retried(h):
    clock = h.clock
    subs = StaticSubscriptions()
    receiver = Receiver(delay_s=0.2)
    sender = WebhookSender(
        h.store,
        subs,
        box(),
        receiver,
        allowlist=frozenset(),
        resolver=resolve_public,
        clock=clock,
        settings=SenderSettings(send_timeout_s=0.05),
    )
    w = World(h, clock, subs, receiver, sender)
    sub = await w.subscribe()
    did, _ = await w.event(sub)

    await sender.run_once()

    row = await h.delivery(did)
    assert (row["state"], row["attempts"]) == ("pending", 1)
    (attempt,) = await h.attempt_rows(did)
    assert (attempt["outcome"], attempt["status_code"], attempt["error"]) == (
        "retry",
        None,
        "timeout",
    )


# ── the retry schedule is a setting (WEBHOOK_RETRY_SCHEDULE_S) ────────────────────────────────


def test_the_default_retry_schedule_is_unchanged():
    assert DEFAULT_RETRY_SCHEDULE_S == (60, 300, 1800, 7200)
    assert SenderSettings.from_env({}).retry_schedule_s == (60, 300, 1800, 7200)
    assert SenderSettings.from_env(
        {"WEBHOOK_RETRY_SCHEDULE_S": ""}
    ).retry_schedule_s == (60, 300, 1800, 7200)
    assert SenderSettings().retry_schedule_s == DEFAULT_RETRY_SCHEDULE_S


@pytest.mark.parametrize(
    "raw, parsed",
    [
        ("5,10", (5, 10)),
        (" 5 , 10 ,15 ", (5, 10, 15)),
        ("1", (1,)),
        (",".join(["30"] * 20), (30,) * 20),
    ],
)
def test_a_retry_schedule_setting_is_read(raw, parsed):
    settings = SenderSettings.from_env({"WEBHOOK_RETRY_SCHEDULE_S": raw})
    assert settings.retry_schedule_s == parsed


@pytest.mark.parametrize(
    "raw",
    [
        "abc",
        "60,,300",
        "60,",
        "0",
        "60,0",
        "-5",
        "60,1.5",
        "60;300",
        ",".join(["30"] * 21),
    ],
)
def test_a_malformed_retry_schedule_is_refused(raw):
    with pytest.raises(SenderSettingsError) as err:
        SenderSettings.from_env({"WEBHOOK_RETRY_SCHEDULE_S": raw})
    assert "WEBHOOK_RETRY_SCHEDULE_S" in str(err.value)


async def test_a_custom_schedule_sets_the_retry_times_and_when_it_goes_dead(h):
    subs = StaticSubscriptions()
    receiver = Receiver(default=503)
    sender = WebhookSender(
        h.store,
        subs,
        box(),
        receiver,
        allowlist=frozenset(),
        resolver=resolve_public,
        clock=h.clock,
        settings=SenderSettings(retry_schedule_s=(5, 10)),
    )
    w = World(h, h.clock, subs, receiver, sender)
    did, _ = await w.event(await w.subscribe())

    for n, wait in enumerate((5, 10), start=1):
        assert await sender.run_once() == 1
        row = await h.delivery(did)
        assert (row["state"], row["attempts"]) == ("pending", n)
        assert row["next_attempt_at"] == w.clock.now + timedelta(seconds=wait)
        w.clock.advance(wait - 1)
        assert await sender.run_once() == 0
        w.clock.advance(1)

    assert await sender.run_once() == 1
    row = await h.delivery(did)
    assert (row["state"], row["attempts"]) == ("dead", 3)
    attempts = await h.attempt_rows(did)
    assert [a["outcome"] for a in attempts] == ["retry", "retry", "dead"]
    w.clock.advance(86_400)
    assert await sender.run_once() == 0
    assert len(receiver.posted) == 3


@pytest.mark.parametrize("code", [400, 401, 404, 410, 422, 301])
async def test_any_other_answer_fails_without_retry(world, code):
    sub = await world.subscribe()
    did, _ = await world.event(sub)
    world.receiver.answers = [code]

    await world.sender.run_once()

    row = await world.h.delivery(did)
    assert (row["state"], row["attempts"], row["last_status_code"]) == (
        "failed",
        1,
        code,
    )
    (attempt,) = await world.h.attempt_rows(did)
    assert (attempt["outcome"], attempt["status_code"]) == ("failed", code)
    world.clock.advance(86_400)
    assert await world.sender.run_once() == 0
    assert len(world.receiver.posted) == 1


# ── cancelled ────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("how", ["paused", "deleted"])
async def test_a_subscription_gone_mid_retry_is_cancelled_without_a_send(world, how):
    sub = await world.subscribe()
    did, _ = await world.event(sub)
    world.receiver.answers = [503]
    await world.sender.run_once()
    assert (await world.h.delivery(did))["state"] == "pending"

    # the table changes and the delivery row is untouched: the sender's re-check catches it
    if how == "paused":
        await world.h.set_active(sub.id, False)
    else:
        await world.h.delete_subscription(sub.id)
    world.clock.advance(60)
    assert await world.sender.run_once() == 1

    row = await world.h.delivery(did)
    assert (row["state"], row["attempts"], row["lease_until"]) == ("cancelled", 1, None)
    assert len(world.receiver.posted) == 1
    assert len(await world.h.attempt_rows(did)) == 1  # no attempt for a send never made


async def test_admin_apis_pause_cancels_a_retry_before_the_sender_sees_it(world):
    sub = await world.subscribe()
    did, _ = await world.event(sub)
    world.receiver.answers = [503]
    await world.sender.run_once()

    await world.h.set_active(sub.id, False)
    await world.h.admin_cancel_pending(sub.id)
    world.clock.advance(7200)

    assert await world.sender.run_once() == 0
    assert (await world.h.delivery(did))["state"] == "cancelled"
    assert len(world.receiver.posted) == 1


@pytest.mark.parametrize("answer", [200, 400, 500])
async def test_a_cancel_while_in_flight_is_never_overwritten(world, answer):
    sub = await world.subscribe()
    did, _ = await world.event(sub)

    async def pause_now() -> None:
        await world.h.set_active(sub.id, False)
        await world.h.admin_cancel_pending(sub.id)

    world.receiver.during = pause_now
    world.receiver.answers = [answer]

    await world.sender.run_once()

    row = await world.h.delivery(did)
    assert row["state"] == "cancelled"
    assert row["lease_until"] is None
    # the send happened, so it is logged
    (attempt,) = await world.h.attempt_rows(did)
    assert attempt["status_code"] == answer


# ── leases ───────────────────────────────────────────────────────────────────────────────────


async def test_an_expired_lease_is_reclaimed_and_the_old_claim_cannot_write(world):
    sub = await world.subscribe()
    did, _ = await world.event(sub)

    # a replica claims the row and dies before it answers
    (stale,) = await world.h.store.claim(lease_s=LEASE_S, limit=10)
    assert (await world.h.delivery(did))["state"] == "sending"

    world.clock.advance(30)
    assert await world.sender.run_once() == 0  # the lease still holds
    world.clock.advance(LEASE_S - 30 + 1)
    assert await world.sender.run_once() == 1  # expired: taken over
    assert (await world.h.delivery(did))["state"] == "delivered"

    # the dead replica's late answer is refused by the lease guard
    late = DeliveryResult(
        state="failed",
        outcome="failed",
        status_code=400,
        error=None,
        duration_ms=1,
        retry_in_s=None,
    )
    assert await world.h.store.record(stale, late) is False
    assert await world.h.store.cancel(stale) is False
    row = await world.h.delivery(did)
    assert (row["state"], row["attempts"]) == ("delivered", 1)
    assert len(world.receiver.posted) == 1


async def test_a_lease_short_claim_is_a_failed_attempt_that_ends_dead(h):
    """Before it posts, the sender checks the claim still has SEND_TIMEOUT_S + LEASE_MARGIN_S of
    its lease left (measured on the monotonic clock from before the claim). If not, it sends
    nothing and records a failed attempt (``lease short``) on the retry schedule, so the claim
    ends ``dead`` like any other failure. The other rows of the same tick are still delivered.
    """
    skew: contextvars.ContextVar[float] = contextvars.ContextVar("skew", default=0.0)
    subs = StaticSubscriptions()
    receiver = Receiver()
    sender = WebhookSender(
        h.store,
        subs,
        box(),
        receiver,
        allowlist=frozenset(),
        resolver=resolve_public,
        clock=h.clock,
        monotonic=lambda: 1000.0 + skew.get(),
        settings=SenderSettings(retry_schedule_s=(5, 10)),
    )
    w = World(h, h.clock, subs, receiver, sender)
    budget = LEASE_S - SEND_TIMEOUT_S - LEASE_MARGIN_S
    slow = await w.subscribe()
    skews = {slow.id: budget + 0.5}
    read = subs.find

    async def slow_read(user_id: int, subscription_id: str) -> Any:
        # only this delivery's task sees the time its read took
        skew.set(skews.get(subscription_id, 0.0))
        return await read(user_id, subscription_id)

    subs.find = slow_read  # type: ignore[method-assign]
    did, slow_body = await w.event(slow)

    for n, wait in enumerate((5, 10), start=1):
        others = [(await w.event(await w.subscribe()))[0] for _ in range(2)]
        assert await sender.run_once() == 3
        row = await h.delivery(did)
        assert (row["state"], row["attempts"], row["lease_until"]) == (
            "pending",
            n,
            None,
        )
        assert row["last_error"] == "lease short"
        assert row["next_attempt_at"] == w.clock.now + timedelta(seconds=wait)
        for other in others:
            assert (await h.delivery(other))["state"] == "delivered"
        w.clock.advance(wait)

    assert await sender.run_once() == 1
    row = await h.delivery(did)
    assert (row["state"], row["attempts"], row["lease_until"]) == ("dead", 3, None)
    attempts = await h.attempt_rows(did)
    assert [a["outcome"] for a in attempts] == ["retry", "retry", "dead"]
    assert {(a["status_code"], a["error"]) for a in attempts} == {(None, "lease short")}
    assert slow_body not in [p.body for p in receiver.posted]
    assert len(receiver.posted) == 4
    w.clock.advance(86_400)
    assert await sender.run_once() == 0

    # a read that leaves exactly the budget still sends
    edge = await w.subscribe()
    skews[edge.id] = budget - 0.5
    did2, _ = await w.event(edge)
    await sender.run_once()
    assert (await h.delivery(did2))["state"] == "delivered"
    sender.close()


async def test_claims_leases_and_retries_run_on_the_database_clock(pg_engine):
    """Production passes no clock: the claim predicate, lease_until, the retry time and the stamps
    are the database's now(), so a replica whose own clock is off can't shorten a lease or claim a
    row early."""
    from sqlalchemy import text

    h = PgHarness(pg_engine)
    store = PostgresDeliveryStore(h.session_factory)  # the database clock
    s = str(uuid.uuid4())
    await h.add_subscription(s, USER)
    await h.add_event("evt_due", "meeting.updated", "{}")
    await h.add_event("evt_later", "meeting.updated", "{}")
    async with pg_engine.begin() as conn:
        for event_id, offset in (("evt_due", "-1 second"), ("evt_later", "30 seconds")):
            await conn.execute(
                text(
                    "INSERT INTO webhook_deliveries (event_id, subscription_id, user_id, state, "
                    "attempts, next_attempt_at) VALUES (:e, CAST(:s AS uuid), :u, 'pending', 0, "
                    f"now() + interval '{offset}')"
                ),
                {"e": event_id, "s": s, "u": USER},
            )

    (claim,) = await store.claim(lease_s=LEASE_S, limit=10)
    assert claim.event_id == "evt_due"
    async with pg_engine.connect() as conn:
        db_now = (await conn.execute(text("SELECT now()"))).scalar_one()
    assert 59 <= (claim.lease_until - db_now).total_seconds() <= 61

    retry = DeliveryResult(
        state="pending",
        outcome="retry",
        status_code=503,
        error="HTTP 503",
        duration_ms=3,
        retry_in_s=DEFAULT_RETRY_SCHEDULE_S[0],
    )
    assert await store.record(claim, retry) is True
    row = await h.delivery(claim.id)
    async with pg_engine.connect() as conn:
        db_now = (await conn.execute(text("SELECT now()"))).scalar_one()
    assert 59 <= (row["next_attempt_at"] - db_now).total_seconds() <= 61

    # a sender whose own clock runs an hour ahead still claims nothing early
    subs = StaticSubscriptions()
    ahead = WebhookSender(
        store,
        subs,
        box(),
        Receiver(),
        allowlist=frozenset(),
        resolver=resolve_public,
        clock=lambda: datetime.now(UTC) + timedelta(hours=1),
    )
    assert await ahead.run_once() == 0


async def test_two_senders_never_send_the_same_delivery(pg_engine):
    h = PgHarness(pg_engine)
    clock = h.clock
    subs = StaticSubscriptions()
    receiver = Receiver(delay_s=0.01)
    w = World(h, clock, subs, receiver, None)  # type: ignore[arg-type]
    sub = await w.subscribe()
    dids = [(await w.event(sub))[0] for _ in range(40)]

    def sender() -> WebhookSender:
        return WebhookSender(
            PostgresDeliveryStore(h.session_factory, clock=h.clock),
            subs,
            box(),
            receiver,
            allowlist=frozenset(),
            resolver=resolve_public,
            clock=clock,
            settings=SenderSettings(claim_limit=7),
        )

    a, b = sender(), sender()
    rounds = []
    for _ in range(20):
        counts = await asyncio.gather(a.run_once(), b.run_once())
        rounds.append(counts)
        if sum(counts) == 0:
            break
    assert rounds[0] == [7, 7]  # both claimed at once, disjoint rows

    event_ids = [json.loads(p.body)["event_id"] for p in receiver.posted]
    assert len(event_ids) == 40
    assert len(set(event_ids)) == 40
    for did in dids:
        row = await h.delivery(did)
        assert (row["state"], row["attempts"]) == ("delivered", 1)
        assert len(await h.attempt_rows(did)) == 1


# ── rotation ─────────────────────────────────────────────────────────────────────────────────


async def test_the_previous_header_appears_only_during_a_rotation(world):
    rotating = await world.subscribe(previous=(OLD_SECRET, T0 + timedelta(hours=24)))
    plain = await world.subscribe()
    await world.event(rotating)
    await world.event(plain)

    await world.sender.run_once()
    by_url_sub = {p.headers["X-Webhook-Signature"]: p for p in world.receiver.posted}
    assert len(by_url_sub) == 2
    with_previous = [
        p for p in world.receiver.posted if "X-Webhook-Signature-Previous" in p.headers
    ]
    assert len(with_previous) == 1
    post = with_previous[0]
    old_view = {
        "X-Webhook-Timestamp": post.headers["X-Webhook-Timestamp"],
        "X-Webhook-Signature": post.headers["X-Webhook-Signature-Previous"],
    }
    assert exporter_verify(post.body, post.headers, SECRET, now=T0.timestamp())
    assert exporter_verify(post.body, old_view, OLD_SECRET, now=T0.timestamp())

    # after the window the old secret is no longer used
    world.clock.advance(24 * 3600)
    world.receiver.posted.clear()
    await world.event(rotating)
    await world.sender.run_once()
    (after,) = world.receiver.posted
    assert "X-Webhook-Signature-Previous" not in after.headers


async def test_a_rotated_secret_signs_the_next_delivery_at_once(world):
    """§6.9 F-C: the secrets are read from ``webhook_subscriptions`` with the claim, never from
    the subscription read's cache, so the delivery right after ``rotate-secret`` is signed with the
    new secret and carries the old one as ``X-Webhook-Signature-Previous``."""
    sub = await world.subscribe()
    await world.event(sub)
    await world.sender.run_once()
    (before,) = world.receiver.posted
    assert "X-Webhook-Signature-Previous" not in before.headers
    world.receiver.posted.clear()

    await world.h.rotate(
        sub.id, NEW_SECRET
    )  # the cached read still holds the old secret only
    world.clock.advance(1)
    _, body = await world.event(sub)
    assert await world.sender.run_once() == 1

    (post,) = world.receiver.posted
    now = world.clock.now.timestamp()
    assert post.body == body
    assert exporter_verify(body, post.headers, NEW_SECRET, now=now)
    assert not exporter_verify(body, post.headers, SECRET, now=now)
    previous_view = {
        "X-Webhook-Timestamp": post.headers["X-Webhook-Timestamp"],
        "X-Webhook-Signature": post.headers["X-Webhook-Signature-Previous"],
    }
    assert exporter_verify(body, previous_view, SECRET, now=now)
    # a receiver on either secret accepts it (the portal's check)
    assert portal_verify(body, post.headers, NEW_SECRET, now=now)
    assert portal_verify(body, post.headers, SECRET, now=now)
    assert not portal_verify(body, post.headers, OLD_SECRET, now=now)

    # once the window has passed, only the new secret signs
    world.clock.advance(ROTATION_WINDOW.total_seconds())
    world.receiver.posted.clear()
    await world.event(sub)
    await world.sender.run_once()
    (after,) = world.receiver.posted
    assert "X-Webhook-Signature-Previous" not in after.headers
    assert exporter_verify(
        after.body, after.headers, NEW_SECRET, now=world.clock.now.timestamp()
    )


async def test_a_rotated_then_paused_subscription_is_cancelled_not_signed(world):
    sub = await world.subscribe()
    did, _ = await world.event(sub)
    await world.h.rotate(sub.id, NEW_SECRET)
    await world.h.set_active(sub.id, False)

    assert await world.sender.run_once() == 1

    assert world.receiver.posted == []
    row = await world.h.delivery(did)
    assert (row["state"], row["attempts"]) == ("cancelled", 0)
    assert await world.h.attempt_rows(did) == []


# ── the URL guard at send time ───────────────────────────────────────────────────────────────


async def test_the_allowlist_lets_the_portal_through_and_blocks_10_0_0_1(world):
    portal = await world.subscribe(
        url="http://portal.notetaker.svc.cluster.local/api/hooks"
    )
    private = await world.subscribe(url="http://10.0.0.1/x")
    portal_did, _ = await world.event(portal)
    private_did, _ = await world.event(private)

    await world.sender.run_once()

    (post,) = world.receiver.posted
    assert post.target.host == "portal.notetaker.svc.cluster.local"
    assert post.target.pinned_ips == []
    assert (await world.h.delivery(portal_did))["state"] == "delivered"
    row = await world.h.delivery(private_did)
    assert (row["state"], row["attempts"]) == ("failed", 1)
    (attempt,) = await world.h.attempt_rows(private_did)
    assert attempt["outcome"] == "failed"
    assert attempt["status_code"] is None
    assert attempt["error"].startswith("url refused")
    assert "10.0.0.1" not in attempt["error"]


@pytest.mark.parametrize("port", ["99999", "0", "abc"])
async def test_a_url_with_an_invalid_port_is_refused_not_a_fault(world, port, capsys):
    sub = await world.subscribe(url=f"https://hooks.example.com:{port}/aw")
    did, _ = await world.event(sub)

    await world.sender.run_once()

    row = await world.h.delivery(did)
    assert (row["state"], row["attempts"]) == ("failed", 1)
    (attempt,) = await world.h.attempt_rows(did)
    assert (attempt["outcome"], attempt["status_code"]) == ("failed", None)
    assert attempt["error"].startswith("url refused")
    assert port not in attempt["error"]
    assert world.receiver.posted == []
    assert '"webhook_delivery_crashed"' not in capsys.readouterr().out


async def test_a_host_that_does_not_resolve_is_retried(h):
    clock = h.clock
    subs = StaticSubscriptions()
    receiver = Receiver()
    sender = WebhookSender(
        h.store,
        subs,
        box(),
        receiver,
        allowlist=frozenset(),
        resolver=lambda host: [],
        clock=clock,
    )
    w = World(h, clock, subs, receiver, sender)
    did, _ = await w.event(await w.subscribe())

    await sender.run_once()

    row = await h.delivery(did)
    assert (row["state"], row["last_error"]) == (
        "pending",
        "host could not be resolved",
    )
    assert receiver.posted == []


# ── the sender's own DNS threads (WEBHOOK_DNS_THREADS, WEBHOOK_DNS_TIMEOUT_S) ─────────────────


async def _until(predicate: Callable[[], bool], timeout_s: float = 2.0) -> None:
    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), timeout_s)


async def test_a_slow_resolver_never_holds_the_default_executor(h):
    """The URL check resolves on the sender's own bounded pool, not the default executor that
    recording storage offloads to (``recordings/adapters.py``)."""
    loop = asyncio.get_running_loop()
    default = ThreadPoolExecutor(max_workers=1, thread_name_prefix="default")
    loop.set_default_executor(default)
    release = threading.Event()
    resolving: list[str] = []

    def slow_resolver(host: str) -> list[str]:
        resolving.append(threading.current_thread().name)
        release.wait(5)
        return resolve_public(host)

    subs = StaticSubscriptions()
    receiver = Receiver()
    sender = WebhookSender(
        h.store,
        subs,
        box(),
        receiver,
        allowlist=frozenset(),
        resolver=slow_resolver,
        clock=h.clock,
        settings=SenderSettings(dns_threads=2, dns_timeout_s=5.0),
    )
    w = World(h, h.clock, subs, receiver, sender)
    dids = [(await w.event(await w.subscribe()))[0] for _ in range(3)]

    tick = asyncio.create_task(sender.run_once())
    try:
        await _until(lambda: len(resolving) == 2)
        # every sender DNS thread is busy, and the default executor still answers at once
        free = await asyncio.wait_for(loop.run_in_executor(None, lambda: "free"), 1.0)
        assert free == "free"
        await asyncio.sleep(0.05)
        assert (
            len(resolving) == 2
        )  # bounded: the third lookup waits for a sender thread
    finally:
        release.set()
    assert await tick == 3
    assert len(resolving) == 3
    assert all(name.startswith("webhook-dns") for name in resolving), resolving
    for did in dids:
        assert (await h.delivery(did))["state"] == "delivered"
    sender.close()


async def test_a_dns_timeout_is_a_failed_attempt_on_the_schedule(h):
    release = threading.Event()

    def hung_resolver(host: str) -> list[str]:
        release.wait(5)
        return resolve_public(host)

    subs = StaticSubscriptions()
    receiver = Receiver()
    sender = WebhookSender(
        h.store,
        subs,
        box(),
        receiver,
        allowlist=frozenset(),
        resolver=hung_resolver,
        clock=h.clock,
        settings=SenderSettings(retry_schedule_s=(5,), dns_timeout_s=0.05),
    )
    w = World(h, h.clock, subs, receiver, sender)
    did, _ = await w.event(await w.subscribe())
    try:
        assert await sender.run_once() == 1
        row = await h.delivery(did)
        assert (row["state"], row["attempts"], row["last_error"]) == (
            "pending",
            1,
            "dns timeout",
        )
        assert row["next_attempt_at"] == w.clock.now + timedelta(seconds=5)
        w.clock.advance(5)
        assert await sender.run_once() == 1
    finally:
        release.set()
    row = await h.delivery(did)
    assert (row["state"], row["attempts"]) == ("dead", 2)
    attempts = await h.attempt_rows(did)
    assert [(a["outcome"], a["error"]) for a in attempts] == [
        ("retry", "dns timeout"),
        ("dead", "dns timeout"),
    ]
    assert receiver.posted == []
    sender.close()


async def test_close_shuts_the_dns_pool_down(world):
    workers: list[threading.Thread] = []

    def resolver(host: str) -> list[str]:
        workers.append(threading.current_thread())
        return resolve_public(host)

    sender = WebhookSender(
        world.h.store,
        world.subs,
        box(),
        world.receiver,
        allowlist=frozenset(),
        resolver=resolver,
        clock=world.clock,
    )
    await world.event(await world.subscribe())
    assert await sender.run_once() == 1
    (worker,) = workers
    assert worker.is_alive()
    sender.close()
    worker.join(2)
    assert not worker.is_alive()


def test_the_default_send_bounds():
    settings = SenderSettings.from_env({})
    assert (settings.send_timeout_s, settings.lease_s, settings.claim_limit) == (
        10.0,
        60,
        50,
    )
    assert SenderSettings.from_env(
        {
            "WEBHOOK_SEND_TIMEOUT_S": "4.5",
            "WEBHOOK_LEASE_S": "30",
            "WEBHOOK_CLAIM_LIMIT": "7",
        }
    ) == SenderSettings(send_timeout_s=4.5, lease_s=30, claim_limit=7)


@pytest.mark.parametrize(
    "key, raw",
    [
        ("WEBHOOK_SEND_TIMEOUT_S", "0"),
        ("WEBHOOK_SEND_TIMEOUT_S", "ten"),
        ("WEBHOOK_SEND_TIMEOUT_S", "nan"),
        ("WEBHOOK_LEASE_S", "0"),
        ("WEBHOOK_LEASE_S", "1.5"),
        ("WEBHOOK_CLAIM_LIMIT", "0"),
        ("WEBHOOK_CLAIM_LIMIT", "many"),
    ],
)
def test_a_malformed_send_bound_is_refused(key, raw):
    with pytest.raises(SenderSettingsError) as err:
        SenderSettings.from_env({key: raw})
    assert key in str(err.value)


def test_a_lease_too_short_to_post_inside_is_refused():
    with pytest.raises(SenderSettingsError) as err:
        SenderSettings.from_env(
            {"WEBHOOK_SEND_TIMEOUT_S": "10", "WEBHOOK_LEASE_S": "15"}
        )
    assert "WEBHOOK_LEASE_S" in str(err.value)


async def test_the_sender_claims_with_its_lease_and_limit():
    class Claims:
        def __init__(self) -> None:
            self.asked: list[tuple[int, int]] = []

        async def claim(self, *, lease_s: int, limit: int) -> list[Any]:
            self.asked.append((lease_s, limit))
            return []

    store = Claims()
    sender = WebhookSender(
        store,  # type: ignore[arg-type]
        StaticSubscriptions(),
        box(),
        Receiver(),
        allowlist=frozenset(),
        resolver=resolve_public,
        settings=SenderSettings(lease_s=30, claim_limit=7),
    )
    assert await sender.run_once() == 0
    assert store.asked == [(30, 7)]
    sender.close()


def test_the_default_dns_settings():
    settings = SenderSettings.from_env({})
    assert (settings.dns_threads, settings.dns_timeout_s) == (4, 5.0)
    assert SenderSettings.from_env(
        {"WEBHOOK_DNS_THREADS": "8", "WEBHOOK_DNS_TIMEOUT_S": "2.5"}
    ) == SenderSettings(dns_threads=8, dns_timeout_s=2.5)


@pytest.mark.parametrize(
    "key, raw",
    [
        ("WEBHOOK_DNS_THREADS", "0"),
        ("WEBHOOK_DNS_THREADS", "-1"),
        ("WEBHOOK_DNS_THREADS", "2.5"),
        ("WEBHOOK_DNS_THREADS", "four"),
        ("WEBHOOK_DNS_TIMEOUT_S", "0"),
        ("WEBHOOK_DNS_TIMEOUT_S", "-3"),
        ("WEBHOOK_DNS_TIMEOUT_S", "soon"),
        ("WEBHOOK_DNS_TIMEOUT_S", "nan"),
        ("WEBHOOK_DNS_TIMEOUT_S", "inf"),
    ],
)
def test_a_malformed_dns_setting_is_refused(key, raw):
    with pytest.raises(SenderSettingsError) as err:
        SenderSettings.from_env({key: raw})
    assert key in str(err.value)


# ── nothing to sign with ─────────────────────────────────────────────────────────────────────


async def test_an_unreadable_subscription_list_is_retried_without_a_send(world):
    sub = await world.subscribe()
    did, _ = await world.event(sub)
    world.subs.unavailable = True

    await world.sender.run_once()

    row = await world.h.delivery(did)
    assert (row["state"], row["last_error"]) == ("pending", "subscriptions unavailable")
    assert world.receiver.posted == []
    (attempt,) = await world.h.attempt_rows(did)
    assert attempt["outcome"] == "retry"


async def test_a_secret_the_ring_cannot_open_is_retried_without_a_send(world):
    sub = await world.subscribe()
    await world.h.set_key_id(sub.id, "k-gone")
    did, _ = await world.event(sub)

    await world.sender.run_once()

    row = await world.h.delivery(did)
    assert (row["state"], row["last_error"]) == (
        "pending",
        "secret could not be opened",
    )
    assert world.receiver.posted == []


# ── a sender fault is an attempt ─────────────────────────────────────────────────────────────

CRASH_HOST = "crash.example.com"


class SenderFault(Exception):
    """A fault the sender does not map; its text carries what must never be stored or logged."""


def _fault() -> SenderFault:
    return SenderFault(f"boom {SECRET} https://{CRASH_HOST}/aw?token=q-9f8e7d")


def resolve_or_fault(host: str) -> list[str]:
    if host == CRASH_HOST:
        raise _fault()
    return resolve_public(host)


def _faulting_world(h: Any, *, claim_limit: int = 50) -> World:
    subs = StaticSubscriptions()
    receiver = Receiver()
    sender = WebhookSender(
        h.store,
        subs,
        box(),
        receiver,
        allowlist=frozenset(),
        resolver=resolve_or_fault,
        clock=h.clock,
        settings=SenderSettings(claim_limit=claim_limit),
    )
    return World(h, h.clock, subs, receiver, sender)


@pytest.mark.parametrize("where", ["transport", "validation"])
async def test_a_sender_fault_is_an_attempt_on_the_retry_schedule_then_dead(
    h, where, capsys
):
    w = _faulting_world(h)
    if where == "transport":
        sub = await w.subscribe()
        w.receiver.default = _fault()
    else:
        sub = await w.subscribe(url=f"https://{CRASH_HOST}/aw?token=q-9f8e7d")
    did, body = await w.event(sub)

    for n, wait in enumerate(DEFAULT_RETRY_SCHEDULE_S, start=1):
        assert await w.sender.run_once() == 1
        row = await h.delivery(did)
        assert (row["state"], row["attempts"], row["lease_until"]) == (
            "pending",
            n,
            None,
        )
        assert row["next_attempt_at"] == w.clock.now + timedelta(seconds=wait)
        assert row["last_error"] == "sender error"
        w.clock.advance(wait - 1)
        assert await w.sender.run_once() == 0
        w.clock.advance(1)

    assert await w.sender.run_once() == 1
    row = await h.delivery(did)
    assert (row["state"], row["attempts"], row["lease_until"]) == ("dead", 5, None)
    w.clock.advance(86_400)
    assert await w.sender.run_once() == 0

    attempts = await h.attempt_rows(did)
    assert [a["attempt"] for a in attempts] == [1, 2, 3, 4, 5]
    assert [a["outcome"] for a in attempts] == ["retry"] * 4 + ["dead"]
    assert {(a["status_code"], a["error"]) for a in attempts} == {
        (None, "sender error")
    }
    logged = capsys.readouterr().out
    assert logged.count('"webhook_delivery_crashed"') == 5
    assert "SenderFault" in logged  # the operator log names the fault's type, only
    stored = json.dumps([row, attempts], default=str)
    for value in (SECRET, "q-9f8e7d", CRASH_HOST, body.decode()):
        assert value not in logged, value
        assert value not in stored, value


async def test_the_reviews_reproduction_no_longer_holds_the_row_in_sending(h):
    """D2 I2: a faulting delivery, ticked 5 times 61 s apart, used to stay ``sending`` with 0
    attempts, its ``next_attempt_at`` unchanged and no attempt row."""
    w = _faulting_world(h)
    did, _ = await w.event(await w.subscribe(url=f"https://{CRASH_HOST}/aw"))
    due = (await h.delivery(did))["next_attempt_at"]

    claimed = []
    for _ in range(5):
        claimed.append(await w.sender.run_once())
        w.clock.advance(61)

    row = await h.delivery(did)
    assert claimed == [1, 1, 0, 0, 0]
    assert (row["state"], row["attempts"]) == ("pending", 2)
    assert row["next_attempt_at"] == due + timedelta(
        seconds=61 + DEFAULT_RETRY_SCHEDULE_S[1]
    )
    assert len(await h.attempt_rows(did)) == 2


async def test_faulting_deliveries_never_hold_back_the_others(h):
    """Claims are taken oldest-due first; a faulting row moves to its next retry time, so rows due
    before it are claimed ahead of it on the next tick."""
    w = _faulting_world(h, claim_limit=3)
    bad = await w.subscribe(url=f"https://{CRASH_HOST}/aw")
    good = await w.subscribe()
    w.clock.advance(-10)
    poisoned = [(await w.event(bad))[0] for _ in range(3)]
    w.clock.advance(10)

    assert await w.sender.run_once() == 3  # the three poisoned rows, the oldest due
    w.clock.advance(30)
    healthy = [(await w.event(good))[0] for _ in range(2)]
    # every poisoned lease has run out, and their first retry is due
    w.clock.advance(31)

    assert await w.sender.run_once() == 3
    for did in healthy:
        assert (await h.delivery(did))["state"] == "delivered"
    assert len(w.receiver.posted) == 2

    for _ in range(40):
        w.clock.advance(3600)
        await w.sender.run_once()
    for did in poisoned:
        row = await h.delivery(did)
        assert (row["state"], row["attempts"]) == ("dead", 5)
    for did in healthy:
        assert len(await h.attempt_rows(did)) == 1


async def test_claims_are_taken_oldest_due_first_across_pending_and_expired_leases(h):
    w = _faulting_world(h, claim_limit=2)
    sub = await w.subscribe()
    w.clock.advance(-30)
    leased, _ = await w.event(sub)  # due first, then held by a replica that died
    (stale,) = await h.store.claim(lease_s=LEASE_S, limit=1)
    assert stale.id == leased
    w.clock.advance(10)
    older, _ = await w.event(sub)
    w.clock.advance(10)
    newer, _ = await w.event(sub)
    w.clock.advance(10)
    await h.add_event("evt_future", "meeting.updated", "{}")
    future = await h.add_delivery(
        "evt_future", sub.id, USER, w.clock.now + timedelta(seconds=5)
    )

    # the dead replica's lease still holds: the two pending rows, oldest first
    assert [c.id for c in await h.store.claim(lease_s=LEASE_S, limit=2)] == [
        older,
        newer,
    ]
    w.clock.advance(LEASE_S)
    # its lease has run out and it is due first; the two just claimed are still leased
    assert [c.id for c in await h.store.claim(lease_s=LEASE_S, limit=2)] == [
        leased,
        future,
    ]


class _FaultyRecord:
    """The store, but ``record`` raises for one delivery."""

    def __init__(self, inner: Any, broken: int) -> None:
        self._inner = inner
        self._broken = broken

    async def claim(self, *, lease_s: int, limit: int) -> list[Any]:
        return await self._inner.claim(lease_s=lease_s, limit=limit)

    async def is_active(self, subscription_id: str) -> bool:
        return await self._inner.is_active(subscription_id)

    async def cancel(self, claim: Any) -> bool:
        return await self._inner.cancel(claim)

    async def record(self, claim: Any, result: DeliveryResult) -> bool:
        if claim.id == self._broken:
            raise _fault()
        return await self._inner.record(claim, result)


async def test_a_store_that_cannot_record_one_row_still_delivers_the_rest(h, capsys):
    w = _faulting_world(h)
    sub = await w.subscribe()
    broken, _ = await w.event(sub)
    others = [(await w.event(sub))[0] for _ in range(3)]
    sender = WebhookSender(
        _FaultyRecord(h.store, broken),
        w.subs,
        box(),
        w.receiver,
        allowlist=frozenset(),
        resolver=resolve_public,
        clock=h.clock,
    )

    assert await sender.run_once() == 4

    for did in others:
        assert (await h.delivery(did))["state"] == "delivered"
    row = await h.delivery(broken)
    assert (row["state"], row["attempts"]) == ("sending", 0)  # its lease runs out
    logged = capsys.readouterr().out
    assert '"webhook_delivery_crashed"' in logged
    assert SECRET not in logged


# ── logs ─────────────────────────────────────────────────────────────────────────────────────


async def test_no_secret_key_or_url_reaches_the_logs(world, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    rotating = await world.subscribe(previous=(OLD_SECRET, T0 + timedelta(hours=1)))
    failing = await world.subscribe(url="http://10.0.0.1/x?token=q-9f8e7d")
    retrying = await world.subscribe()
    await world.event(rotating)
    await world.event(failing)
    await world.event(retrying)
    await world.h.set_key_id(retrying.id, "k-gone")

    await world.sender.run_once()

    out = capsys.readouterr()
    logged = out.out + out.err + caplog.text
    assert logged  # the sender does log each outcome
    forbidden = [
        SECRET,
        OLD_SECRET,
        RING["k1"],
        RING["k2"],
        base64.b64encode(rotating.secret_enc).decode(),
        "q-9f8e7d",
        "hooks.example.com",
        "10.0.0.1",
    ]
    for value in forbidden:
        assert value not in logged, value


# ── no Redis ─────────────────────────────────────────────────────────────────────────────────


async def test_the_sender_never_touches_redis(world, monkeypatch):
    import redis.asyncio.client as redis_client

    async def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("webhook delivery must not use Redis")

    monkeypatch.setattr(redis_client.Redis, "execute_command", refuse)
    world.receiver.answers = [500]
    did, _ = await world.event(await world.subscribe())
    await world.sender.run_once()
    world.clock.advance(60)
    await world.sender.run_once()
    assert (await world.h.delivery(did))["state"] == "delivered"

    src = Path(__file__).resolve().parents[1] / "src" / "meeting_api"
    for module in ("webhooks/sender.py", "webhooks/subscriptions.py"):
        code = (src / module).read_text()
        assert not re.search(r"^\s*(import|from)\s+\S*redis", code, re.M), module
        assert "redis_client" not in code, module


# ── the subscription read (admin-api's internal door, cached 30 s) ───────────────────────────


def _admin_body(sub_id: str, *, previous: bool = False) -> dict[str, Any]:
    return {
        "user_id": USER,
        "subscriptions": [
            {
                "id": sub_id,
                "url": "https://hooks.example.com/aw",
                "events": ["meeting.completed"],
                "secret_enc": base64.b64encode(seal(SECRET)).decode(),
                "enc_key_id": "k1",
                "previous_secret_enc": (
                    base64.b64encode(seal(OLD_SECRET, "k2")).decode()
                    if previous
                    else None
                ),
                "previous_enc_key_id": "k2" if previous else None,
                "previous_secret_expires_at": (
                    "2026-09-30T04:00:00Z" if previous else None
                ),
            }
        ],
    }


class Monotonic:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _admin(handler: Callable[[Any], Any], clock: Monotonic) -> AdminSubscriptions:
    import httpx

    return AdminSubscriptions(
        "http://admin-api.test/",
        "test-internal-secret",
        transport=httpx.MockTransport(handler),
        monotonic=clock,
    )


async def test_the_read_is_cached_for_30_seconds():
    import httpx

    sid = str(uuid.uuid4())
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=_admin_body(sid, previous=True))

    clock = Monotonic()
    admin = _admin(handler, clock)
    (sub,) = await admin.for_account(USER)
    assert calls[0].url == httpx.URL(
        f"http://admin-api.test/internal/users/{USER}/webhook-subscriptions"
    )
    assert calls[0].headers["X-Internal-Secret"] == "test-internal-secret"
    assert sub.id == sid and sub.events == ("meeting.completed",)
    assert box().decrypt(sub.secret_enc, sub.enc_key_id) == SECRET
    assert sub.previous_secret_expires_at == datetime(2026, 9, 30, 4, tzinfo=UTC)
    assert sub.previous_live(datetime(2026, 9, 30, 3, 59, tzinfo=UTC))
    assert not sub.previous_live(datetime(2026, 9, 30, 4, tzinfo=UTC))

    clock.t += 29.9
    await admin.for_account(USER)
    assert await admin.find(USER, sid) is not None
    assert len(calls) == 1
    clock.t += 0.2
    await admin.for_account(USER)
    assert len(calls) == 2


async def test_a_subscription_missing_from_the_cache_is_read_again_once():
    import httpx

    first, second = str(uuid.uuid4()), str(uuid.uuid4())
    bodies = [
        _admin_body(first),
        {
            "user_id": USER,
            "subscriptions": _admin_body(first)["subscriptions"]
            + [dict(_admin_body(second)["subscriptions"][0])],
        },
    ]
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=bodies[min(len(calls), 2) - 1])

    admin = _admin(handler, Monotonic())
    await admin.for_account(USER)
    found = await admin.find(USER, second)  # created after the cached read
    assert found is not None and found.id == second
    assert len(calls) == 2
    assert await admin.find(USER, str(uuid.uuid4())) is None
    assert len(calls) == 3


@pytest.mark.parametrize("answer", ["status", "error", "shape"])
async def test_a_failed_read_is_unavailable_never_empty(answer):
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        if answer == "error":
            raise httpx.ConnectError("refused")
        if answer == "status":
            return httpx.Response(503)
        return httpx.Response(200, json={"subscriptions": [{"id": 1}]})

    with pytest.raises(SubscriptionsUnavailable):
        await _admin(handler, Monotonic()).for_account(USER)


async def test_an_unconfigured_read_is_unavailable():
    for url, secret in (("", "s"), ("http://admin-api.test", "")):
        with pytest.raises(SubscriptionsUnavailable):
            await AdminSubscriptions(url, secret).for_account(USER)


# ── the production poster ────────────────────────────────────────────────────────────────────


async def test_the_poster_dials_the_validated_address_and_keeps_the_host():
    import httpx

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(202)

    poster = HttpxPoster(allowlist=frozenset(), inner=httpx.MockTransport(handler))
    target = PinnedURL(
        "https://hooks.example.com/aw?x=1",
        host="hooks.example.com",
        port=None,
        scheme="https",
        pinned_ips=["93.184.216.34"],
    )
    assert await poster.post(target, b"{}", {"X-Webhook-Timestamp": "1"}) == 202
    (request,) = seen
    assert request.url.host == "93.184.216.34"
    assert request.headers["host"] == "hooks.example.com"
    assert request.extensions["sni_hostname"] == "hooks.example.com"
    assert request.content == b"{}"


async def test_the_poster_turns_transport_faults_into_transport_errors():
    import httpx

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused to hooks.example.com")

    poster = HttpxPoster(allowlist=frozenset(), inner=httpx.MockTransport(refuse))
    target = PinnedURL(
        "https://hooks.example.com/aw",
        host="hooks.example.com",
        port=None,
        scheme="https",
        pinned_ips=["93.184.216.34"],
    )
    with pytest.raises(TransportError) as exc:
        await poster.post(target, b"{}", {})
    assert str(exc.value) == "ConnectError"  # the type only: never a host or URL


# ── wiring (``python -m meeting_api``) ───────────────────────────────────────────────────────


def _production_env(monkeypatch: pytest.MonkeyPatch) -> None:
    import meeting_api.__main__ as main_mod

    monkeypatch.setattr(main_mod, "_require_config", lambda env=None: None)
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+asyncpg://nobody:dummy@127.0.0.1:1/none"
    )
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin-token")
    monkeypatch.setenv("ADMIN_API_URL", "http://admin-api.test")
    monkeypatch.setenv("INTERNAL_API_SECRET", "test-internal-secret")


def test_a_key_ring_set_but_wrong_refuses_to_boot(monkeypatch):
    import meeting_api.__main__ as main_mod
    from meeting_api.webhooks.secret_box import KeyRingError

    _production_env(monkeypatch)
    monkeypatch.setenv("WEBHOOK_SECRET_ENC_KEYS", json.dumps(RING))
    monkeypatch.setenv("WEBHOOK_SECRET_ENC_ACTIVE_KEY", "k9")
    with pytest.raises(KeyRingError):
        main_mod.build_production_app()


@pytest.mark.parametrize(
    "key, raw",
    [
        ("WEBHOOK_RETRY_SCHEDULE_S", "60,soon"),
        ("WEBHOOK_DNS_THREADS", "0"),
        ("WEBHOOK_DNS_TIMEOUT_S", "never"),
    ],
)
def test_a_malformed_sender_setting_refuses_to_boot(monkeypatch, key, raw):
    import meeting_api.__main__ as main_mod

    _production_env(monkeypatch)
    monkeypatch.setenv(key, raw)
    with pytest.raises(SenderSettingsError, match=key):
        main_mod.build_production_app()


def test_a_valid_key_ring_reaches_the_background_loops(monkeypatch):
    import meeting_api.__main__ as main_mod

    _production_env(monkeypatch)
    monkeypatch.setenv("WEBHOOK_SECRET_ENC_KEYS", json.dumps(RING))
    monkeypatch.setenv("WEBHOOK_SECRET_ENC_ACTIVE_KEY", "k1")
    attached: dict[str, Any] = {}
    real_attach = main_mod._attach_background_loops

    def attach(app: Any, *args: Any, **kwargs: Any) -> None:
        attached.update(kwargs)
        real_attach(app, *args, **kwargs)

    monkeypatch.setattr(main_mod, "_attach_background_loops", attach)
    main_mod.build_production_app()
    assert isinstance(attached["webhook_secret_box"], SecretBox)


class _StopLoop(Exception):
    pass


async def _run_lifespan_once(
    monkeypatch, *, secret_box: Optional[SecretBox]
) -> tuple[list[int], list[str]]:
    """Drive the real lifespan with every loop ended after one tick; return the single-flight keys
    taken and the sender ticks run."""
    import types

    import meeting_api.__main__ as main_mod
    import meeting_api.sweeps.single_flight as single_flight
    import meeting_api.webhooks.sender as sender_mod

    guarded: list[int] = []
    ticks: list[str] = []

    async def record_guard(lock: Any, key: int, body: Any) -> bool:
        guarded.append(key)
        return False

    async def record_tick(self: Any) -> int:
        ticks.append("send")
        return 0

    async def stop(delay: float, *a: Any, **kw: Any) -> None:
        raise _StopLoop()

    monkeypatch.setattr(single_flight, "run_single_flight", record_guard)
    monkeypatch.setattr(sender_mod.WebhookSender, "run_once", record_tick)
    monkeypatch.setenv("ADMIN_API_URL", "http://admin-api.test")
    monkeypatch.setenv("INTERNAL_API_SECRET", "test-internal-secret")
    app = types.SimpleNamespace(
        state=types.SimpleNamespace(), router=types.SimpleNamespace()
    )
    main_mod._attach_background_loops(
        app,
        transcript_store=types.SimpleNamespace(),
        segment_bus=types.SimpleNamespace(),
        redis_client=types.SimpleNamespace(),
        session_factory=lambda: None,
        intake=None,
        webhook_secret_box=secret_box,
    )
    monkeypatch.setattr(asyncio, "sleep", stop)
    async with app.router.lifespan_context(app):
        for _ in range(
            20
        ):  # let every loop reach its first tick (asyncio.sleep is patched)
            await _yield()
    return guarded, ticks


async def _yield() -> None:
    fut = asyncio.get_running_loop().create_future()
    asyncio.get_running_loop().call_soon(fut.set_result, None)
    await fut


async def test_one_sender_runs_per_replica_unguarded(monkeypatch):
    from meeting_api.sweeps.single_flight import sweep_lock_key

    guarded, ticks = await _run_lifespan_once(monkeypatch, secret_box=box())
    assert ticks == ["send"]
    assert sweep_lock_key("webhook-sender") not in guarded


async def test_no_sender_without_a_key_ring(monkeypatch):
    guarded, ticks = await _run_lifespan_once(monkeypatch, secret_box=None)
    assert ticks == []


async def test_the_sender_settings_reach_the_sender(monkeypatch):
    import meeting_api.webhooks.sender as sender_mod

    seen: dict[str, Any] = {}

    class Capturing(sender_mod.WebhookSender):
        def __init__(self, *a: Any, **kw: Any) -> None:
            seen.update(kw)
            super().__init__(*a, **kw)

        def close(self) -> None:
            seen["closed"] = True
            super().close()

    monkeypatch.setattr(sender_mod, "WebhookSender", Capturing)
    monkeypatch.setenv("WEBHOOK_RETRY_SCHEDULE_S", "5,10")
    monkeypatch.setenv("WEBHOOK_DNS_THREADS", "2")
    monkeypatch.setenv("WEBHOOK_DNS_TIMEOUT_S", "1.5")
    _, ticks = await _run_lifespan_once(monkeypatch, secret_box=box())
    assert ticks == ["send"]
    assert seen["settings"] == SenderSettings(
        retry_schedule_s=(5, 10), dns_threads=2, dns_timeout_s=1.5
    )
    assert seen["closed"] is True  # the app's shutdown shuts the sender's DNS pool down
