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
import hashlib
import hmac
import json
import logging
import os
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import pytest

from meeting_api.webhooks.secret_box import SecretBox
from meeting_api.webhooks.sender import (
    LEASE_S,
    RETRY_SCHEDULE_S,
    Claim,
    DeliveryResult,
    HttpxPoster,
    PostgresDeliveryStore,
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

    def put(self, user_id: int, sub: Subscription) -> None:
        subs = [s for s in self.by_user.get(user_id, []) if s.id != sub.id]
        self.by_user[user_id] = subs + [sub]

    async def for_account(self, user_id: int) -> list[Subscription]:
        if self.unavailable:
            raise SubscriptionsUnavailable("admin-api answered 503")
        return list(self.by_user.get(user_id, []))

    async def find(self, user_id: int, subscription_id: str) -> Optional[Subscription]:
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


class InMemoryDeliveryStore:
    """``DeliveryStore`` over dicts, with the same guards as the Postgres adapter."""

    def __init__(self) -> None:
        self.deliveries: dict[int, dict[str, Any]] = {}
        self.attempts: list[dict[str, Any]] = []
        self.outbox: dict[str, dict[str, Any]] = {}
        self.active: dict[str, bool] = {}
        self._next_id = 1

    async def claim(
        self, *, now: datetime, lease_until: datetime, limit: int
    ) -> list[Claim]:
        due = sorted(
            (
                d
                for d in self.deliveries.values()
                if d["state"] in ("pending", "sending")
                and d["next_attempt_at"] <= now
                and (d["lease_until"] is None or d["lease_until"] < now)
            ),
            key=lambda d: (d["next_attempt_at"], d["id"]),
        )[:limit]
        claims = []
        for d in due:
            d.update(state="sending", lease_until=lease_until, updated_at=now)
            event = self.outbox[d["event_id"]]
            claims.append(
                Claim(
                    id=d["id"],
                    event_id=d["event_id"],
                    event_type=event["event_type"],
                    subscription_id=d["subscription_id"],
                    user_id=d["user_id"],
                    attempt=d["attempts"] + 1,
                    lease_until=lease_until,
                    payload_text=event["payload_text"],
                )
            )
        return claims

    async def is_active(self, subscription_id: str) -> bool:
        return self.active.get(subscription_id, False)

    def _owned(self, claim: Claim) -> Optional[dict[str, Any]]:
        d = self.deliveries.get(claim.id)
        if d and d["state"] == "sending" and d["lease_until"] == claim.lease_until:
            return d
        return None

    async def cancel(self, claim: Claim, *, now: datetime) -> bool:
        d = self._owned(claim)
        if d is None:
            return False
        d.update(state="cancelled", lease_until=None, updated_at=now)
        return True

    async def record(
        self, claim: Claim, result: DeliveryResult, *, now: datetime
    ) -> bool:
        if claim.id in self.deliveries:
            self.attempts.append(
                {
                    "delivery_id": claim.id,
                    "attempt": claim.attempt,
                    "outcome": result.outcome,
                    "status_code": result.status_code,
                    "error": result.error,
                    "duration_ms": result.duration_ms,
                }
            )
        d = self._owned(claim)
        if d is None:
            return False
        d.update(
            state=result.state,
            attempts=claim.attempt,
            lease_until=None,
            last_status_code=result.status_code,
            last_error=result.error,
            updated_at=now,
        )
        if result.next_attempt_at is not None:
            d["next_attempt_at"] = result.next_attempt_at
        return True


# ── harnesses: one scenario, two stores ──────────────────────────────────────────────────────


class MemoryHarness:
    kind = "memory"

    def __init__(self) -> None:
        self.store = InMemoryDeliveryStore()

    async def add_subscription(
        self, sub_id: str, user_id: int, active: bool = True
    ) -> None:
        self.store.active[sub_id] = active

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
        self.store = PostgresDeliveryStore(self.session_factory)

    async def _exec(self, sql: str, **params: Any) -> Any:
        from sqlalchemy import text

        async with self.engine.begin() as conn:
            return await conn.execute(text(sql), params)

    async def add_subscription(
        self, sub_id: str, user_id: int, active: bool = True
    ) -> None:
        await self._exec(
            "INSERT INTO webhook_subscriptions (id, user_id, url, secret_enc, enc_key_id, "
            "secret_last4, events, active) VALUES (CAST(:id AS uuid), :uid, 'https://x.test/', "
            ":enc, 'k1', '6789', '{}', :active)",
            id=sub_id,
            uid=user_id,
            enc=seal(SECRET),
            active=active,
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
        await self.h.add_subscription(sub.id, user_id)
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
def world(h) -> World:
    clock = Clock()
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
    return World(h, clock, subs, receiver, sender)


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

    expected_waits = list(RETRY_SCHEDULE_S)
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
    clock = Clock()
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
        send_timeout_s=0.05,
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
    (stale,) = await world.h.store.claim(
        now=world.clock.now,
        lease_until=world.clock.now + timedelta(seconds=LEASE_S),
        limit=10,
    )
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
        next_attempt_at=None,
    )
    assert await world.h.store.record(stale, late, now=world.clock.now) is False
    assert await world.h.store.cancel(stale, now=world.clock.now) is False
    row = await world.h.delivery(did)
    assert (row["state"], row["attempts"]) == ("delivered", 1)
    assert len(world.receiver.posted) == 1


async def test_two_senders_never_send_the_same_delivery(pg_engine):
    h = PgHarness(pg_engine)
    clock = Clock()
    subs = StaticSubscriptions()
    receiver = Receiver(delay_s=0.01)
    w = World(h, clock, subs, receiver, None)  # type: ignore[arg-type]
    sub = await w.subscribe()
    dids = [(await w.event(sub))[0] for _ in range(40)]

    def sender() -> WebhookSender:
        return WebhookSender(
            PostgresDeliveryStore(h.session_factory),
            subs,
            box(),
            receiver,
            allowlist=frozenset(),
            resolver=resolve_public,
            clock=clock,
            claim_limit=7,
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


async def test_a_host_that_does_not_resolve_is_retried(h):
    clock = Clock()
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
    world.subs.put(USER, replace(sub, enc_key_id="k-gone"))
    did, _ = await world.event(sub)

    await world.sender.run_once()

    row = await world.h.delivery(did)
    assert (row["state"], row["last_error"]) == (
        "pending",
        "secret could not be opened",
    )
    assert world.receiver.posted == []


# ── logs ─────────────────────────────────────────────────────────────────────────────────────


async def test_no_secret_key_or_url_reaches_the_logs(world, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    rotating = await world.subscribe(previous=(OLD_SECRET, T0 + timedelta(hours=1)))
    failing = await world.subscribe(url="http://10.0.0.1/x?token=q-9f8e7d")
    retrying = await world.subscribe()
    await world.event(rotating)
    await world.event(failing)
    await world.event(retrying)
    world.subs.put(USER, replace(retrying, enc_key_id="k-gone"))

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
