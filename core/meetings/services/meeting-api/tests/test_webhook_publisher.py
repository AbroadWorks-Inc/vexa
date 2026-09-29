"""§1.8 — the outbox publisher, and ``webhook.test`` (§2.7).

The publisher (``intake/outbox.py``, single-flight, every ``WEBHOOK_PUBLISH_INTERVAL_S``) takes up
to 500 unpublished ``webhook_outbox`` rows (oldest first), reads each account's subscriptions (the
cached admin-api read) and, in ONE transaction per batch, re-reads ``webhook_subscriptions.active``
under ``FOR SHARE``, inserts one ``pending`` delivery per matching subscriber (``ON CONFLICT
(event_id, subscription_id) DO NOTHING``) and sets ``published_at``. A crash before the commit
means the next tick does it again: nothing lost, nothing duplicated.

``POST /internal/webhooks/test`` (admin-api's hand-off, internal secret) writes a ``webhook.test``
outbox row (sequence 0, ``evt_test_<uuid4 hex>``, already published) and exactly one delivery row
for that subscription, in one transaction.

Most of this needs Postgres (``MEETING_API_TEST_DATABASE_URL``; skipped cleanly when unset — see
``test_intake_pg_schema.py``'s docstring). The pure fan-out rule and the route's refusals run
offline.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import jsonschema
import pytest
from referencing import Registry, Resource

from intake_builders import http
from meeting_api import create_app
from meeting_api.intake.outbox import (
    FAILED_READ_BACKOFF_S,
    OutboxPublisher,
    OutboxRow,
    PostgresWebhookTests,
    SubscriptionNotFound,
    fan_out,
)
from meeting_api.sweeps.item_failures import InMemoryItemFailures
from meeting_api.webhooks.secret_box import SecretBox
from meeting_api.webhooks.sender import PostgresDeliveryStore, WebhookSender
from meeting_api.webhooks.subscriptions import Subscription, SubscriptionsUnavailable

UTC = timezone.utc
T0 = datetime(2026, 9, 29, 4, 0, 0, tzinfo=UTC)
SECRET = "whsec-current-0123456789"


def _contract(*parts: str) -> Path:
    rel = Path(*parts)
    for parent in Path(__file__).resolve().parents:
        if (parent / rel).exists():
            return parent / rel
    raise FileNotFoundError(str(rel))


RING = json.loads(
    _contract(
        "identity", "contracts", "webhook-subscriptions", "secret-box.vectors.json"
    ).read_text()
)["key_ring"]


def seal(plaintext: str) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = os.urandom(12)
    return nonce + AESGCM(base64.b64decode(RING["k1"])).encrypt(
        nonce, plaintext.encode(), b"aw-webhook-secret"
    )


def sub(
    sid: Optional[str] = None,
    *,
    events: tuple[str, ...] = (),
    url: str = "https://hooks.example.com/aw",
) -> Subscription:
    return Subscription(
        id=sid or str(uuid.uuid4()),
        url=url,
        events=events,
        secret_enc=seal(SECRET),
        enc_key_id="k1",
    )


class StaticSubscriptions:
    def __init__(self) -> None:
        self.by_user: dict[int, list[Subscription]] = {}
        self.unavailable: set[int] = set()
        self.reads: list[int] = []

    async def for_account(self, user_id: int) -> list[Subscription]:
        self.reads.append(user_id)
        if user_id in self.unavailable:
            raise SubscriptionsUnavailable("admin-api answered 503")
        return list(self.by_user.get(user_id, []))

    async def find(self, user_id: int, subscription_id: str) -> Optional[Subscription]:
        for s in await self.for_account(user_id):
            if s.id == subscription_id:
                return s
        return None


# ── the fan-out rule (pure) ──────────────────────────────────────────────────────────────────


def test_fan_out_matches_owner_and_events_and_never_a_test_event():
    everything, completed_only, other = sub(), sub(events=("meeting.completed",)), sub()
    subs = {1: [everything, completed_only], 2: [other]}
    rows = [
        OutboxRow("evt_a", "meeting.status_change", 10, 1),
        OutboxRow("evt_b", "meeting.completed", 10, 1),
        OutboxRow("evt_c", "meeting.completed", 20, 2),
        OutboxRow(
            "evt_d", "meeting.completed", 30, 3
        ),  # an account with no subscribers
        OutboxRow("evt_test_1", "webhook.test", None, None),
    ]
    got = {(f.event_id, f.subscription_id, f.user_id) for f in fan_out(rows, subs)}
    assert got == {
        ("evt_a", everything.id, 1),
        ("evt_b", everything.id, 1),
        ("evt_b", completed_only.id, 1),
        ("evt_c", other.id, 2),
    }


def test_a_test_event_is_never_fanned_out_even_with_an_owner():
    rows = [OutboxRow("evt_test_2", "webhook.test", 10, 1)]
    assert fan_out(rows, {1: [sub()]}) == []


# ── real Postgres ────────────────────────────────────────────────────────────────────────────


@pytest.fixture
async def pg():
    url = os.getenv("MEETING_API_TEST_DATABASE_URL")
    if not url:
        pytest.skip(
            "real-Postgres proofs for §1.8; set MEETING_API_TEST_DATABASE_URL to run"
        )
    pytest.importorskip("sqlalchemy", reason="see test_intake_pg_schema.py's docstring")
    pytest.importorskip("asyncpg", reason="see test_intake_pg_schema.py's docstring")
    from sqlalchemy.ext.asyncio import create_async_engine

    from admin_api.schema import models as admin_models
    from admin_api.schema import sync as admin_sync

    eng = create_async_engine(url)
    async with eng.begin() as conn:
        await conn.run_sync(admin_models.Base.metadata.drop_all)
    await admin_sync.ensure_schema(eng, admin_models.Base)
    yield eng
    async with eng.begin() as conn:
        await conn.run_sync(admin_models.Base.metadata.drop_all)
    await eng.dispose()


def _publisher(session_factory: Any, source: Any, **kw: Any) -> OutboxPublisher:
    """An ``OutboxPublisher`` with its own in-memory give-up record unless ``failures`` is given."""
    kw.setdefault("failures", InMemoryItemFailures(max_failures=5))
    return OutboxPublisher(session_factory, source, **kw)


def sessions(engine: Any) -> Any:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    return async_sessionmaker(engine, expire_on_commit=False)


async def execute(engine: Any, sql: str, **params: Any) -> Any:
    from sqlalchemy import text

    async with engine.begin() as conn:
        return await conn.execute(text(sql), params)


async def rows(engine: Any, sql: str, **params: Any) -> list[Any]:
    return list((await execute(engine, sql, **params)).all())


async def seed_meeting(engine: Any, user_id: int, native: str = "kxo-misr-avz") -> int:
    result = await execute(
        engine,
        "INSERT INTO meetings (user_id, platform, platform_specific_id, status, data) "
        "VALUES (:u, 'google_meet', :n, 'requested', '{}'::jsonb) RETURNING id",
        u=user_id,
        n=native,
    )
    return int(result.scalar_one())


async def seed_event(
    engine: Any, meeting_id: Optional[int], event_type: str, *, created: datetime = T0
) -> str:
    event_id = "evt_" + uuid.uuid4().hex
    await execute(
        engine,
        "INSERT INTO webhook_outbox (event_id, meeting_id, event_type, sequence, payload_text, "
        "created_at) VALUES (:e, :m, :t, 1, :p, :c)",
        e=event_id,
        m=meeting_id,
        t=event_type,
        p=json.dumps({"event_id": event_id, "event_type": event_type}),
        c=created,
    )
    return event_id


async def seed_subscription(
    engine: Any,
    source: StaticSubscriptions,
    user_id: int,
    *,
    events: tuple[str, ...] = (),
    active: bool = True,
    url: str = "https://hooks.example.com/aw",
) -> Subscription:
    s = sub(events=events, url=url)
    await execute(
        engine,
        "INSERT INTO webhook_subscriptions (id, user_id, url, secret_enc, enc_key_id, "
        "secret_last4, events, active) VALUES (CAST(:id AS uuid), :u, :url, :enc, 'k1', '6789', "
        ":events, :active)",
        id=s.id,
        u=user_id,
        url=url,
        enc=s.secret_enc,
        events=list(events),
        active=active,
    )
    source.by_user.setdefault(user_id, []).append(s)
    return s


async def deliveries(engine: Any) -> set[tuple[str, str, int, str]]:
    return {
        (r.event_id, str(r.subscription_id), r.user_id, r.state)
        for r in await rows(
            engine,
            "SELECT event_id, subscription_id, user_id, state FROM webhook_deliveries",
        )
    }


async def unpublished(engine: Any) -> set[str]:
    return {
        r.event_id
        for r in await rows(
            engine, "SELECT event_id FROM webhook_outbox WHERE published_at IS NULL"
        )
    }


class Mono:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class Clock:
    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


async def test_the_publisher_fans_out_to_matching_active_subscribers(pg):
    source = StaticSubscriptions()
    everything = await seed_subscription(pg, source, 1)
    completed_only = await seed_subscription(
        pg, source, 1, events=("meeting.completed",)
    )
    paused = await seed_subscription(pg, source, 1, active=False)
    other = await seed_subscription(pg, source, 2)
    m1, m2 = await seed_meeting(pg, 1), await seed_meeting(pg, 2, "abc-defg-hij")
    status_change = await seed_event(pg, m1, "meeting.status_change")
    completed = await seed_event(pg, m1, "meeting.completed")
    theirs = await seed_event(pg, m2, "meeting.completed")

    result = await _publisher(sessions(pg), source, clock=Clock()).run_once()

    assert await deliveries(pg) == {
        (status_change, everything.id, 1, "pending"),
        (completed, everything.id, 1, "pending"),
        (completed, completed_only.id, 1, "pending"),
        (theirs, other.id, 2, "pending"),
    }
    assert paused.id not in {d[1] for d in await deliveries(pg)}
    assert (result.published, result.deliveries, result.deferred) == (3, 4, 0)
    assert await unpublished(pg) == set()
    (due,) = {
        r.next_attempt_at
        for r in await rows(pg, "SELECT next_attempt_at FROM webhook_deliveries")
    }
    assert due == T0
    # published rows are never read again
    assert (
        await _publisher(sessions(pg), source, clock=Clock()).run_once()
    ).published == 0


class CrashingSessions:
    """A session factory whose commits fail ``fail`` times, and can be held at the commit."""

    def __init__(self, engine: Any, *, fail: int = 0) -> None:
        self._factory = sessions(engine)
        self.fail = fail
        self.reached = asyncio.Event()
        self.gate: Optional[asyncio.Event] = None

    def __call__(self) -> Any:
        session = self._factory()
        real_commit = session.commit

        async def commit() -> None:
            self.reached.set()
            if self.gate is not None:
                await self.gate.wait()
            if self.fail > 0:
                self.fail -= 1
                raise RuntimeError("crash before commit")
            await real_commit()

        session.commit = commit
        return session


async def test_a_crash_before_commit_then_a_redo_creates_no_duplicates(pg):
    source = StaticSubscriptions()
    a = await seed_subscription(pg, source, 1)
    b = await seed_subscription(pg, source, 1)
    meeting = await seed_meeting(pg, 1)
    events = [await seed_event(pg, meeting, "meeting.updated") for _ in range(3)]

    # the page's one transaction and each row's own fail: all rolled back, nothing published
    crashing = CrashingSessions(pg, fail=4)
    assert (await _publisher(crashing, source).run_once()).published == 0
    assert await deliveries(pg) == set()
    assert await unpublished(pg) == set(events)

    # a delivery that already exists (a concurrent run) is left alone, not duplicated
    await execute(
        pg,
        "INSERT INTO webhook_deliveries (event_id, subscription_id, user_id, state, attempts, "
        "next_attempt_at) VALUES (:e, CAST(:s AS uuid), 1, 'delivered', 1, :t)",
        e=events[0],
        s=a.id,
        t=T0,
    )
    result = await _publisher(crashing, source).run_once()
    assert result.published == 3
    got = await deliveries(pg)
    assert len(got) == 6
    assert (events[0], a.id, 1, "delivered") in got
    assert {(e, s.id) for e in events for s in (a, b)} == {(d[0], d[1]) for d in got}
    assert await unpublished(pg) == set()
    assert (await _publisher(crashing, source).run_once()).published == 0
    assert len(await deliveries(pg)) == 6


async def test_the_rows_are_read_in_pages_oldest_first_and_all_published(pg):
    """§6.9 F-I: pages of ``SWEEP_BATCH_SIZE`` (created_at, then event_id), every page in one
    tick."""
    from sqlalchemy import event, text

    source = StaticSubscriptions()
    await seed_subscription(pg, source, 1)
    meeting = await seed_meeting(pg, 1)
    async with pg.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO webhook_outbox (event_id, meeting_id, event_type, sequence, "
                "payload_text, created_at) VALUES (:e, :m, 'meeting.updated', :s, '{}', :c)"
            ),
            [
                {
                    "e": f"evt_{n:04d}",
                    "m": meeting,
                    "s": n,
                    "c": T0 + timedelta(seconds=n),
                }
                for n in range(501)
            ],
        )
    limits: list[Any] = []

    def grab(conn, cursor, statement, parameters, context, executemany):
        if "FROM webhook_outbox LEFT OUTER JOIN meetings" in statement:
            limits.append(statement)

    event.listen(pg.sync_engine, "before_cursor_execute", grab)
    try:
        result = await _publisher(sessions(pg), source, batch_size=200).run_once()
    finally:
        event.remove(pg.sync_engine, "before_cursor_execute", grab)
    assert result.published == 501
    assert await unpublished(pg) == set()
    assert len(await deliveries(pg)) == 501
    assert len(limits) == 3  # 200 + 200 + 101


async def test_a_poison_row_is_given_up_and_never_holds_back_the_rest(pg, monkeypatch):
    """A row whose publish keeps failing fails alone (its page is published row by row); after
    ``SWEEP_MAX_ITEM_FAILURES`` it is read past."""
    source = StaticSubscriptions()
    await seed_subscription(pg, source, 1)
    meeting = await seed_meeting(pg, 1)
    poison, good = [await seed_event(pg, meeting, "meeting.updated") for _ in range(2)]
    real = OutboxPublisher._publish
    tried: list[str] = []

    async def publish(self, rows, wanted):
        if any(r.event_id == poison for r in rows):
            tried.append(poison)
            raise RuntimeError("poison row")
        return await real(self, rows, wanted)

    monkeypatch.setattr(OutboxPublisher, "_publish", publish)
    failures = InMemoryItemFailures(max_failures=2)
    publisher = _publisher(sessions(pg), source, failures=failures)
    assert (await publisher.run_once()).published == 1
    assert await unpublished(pg) == {poison}
    await publisher.run_once()
    assert await failures.given_up("webhook-publisher", [poison]) == {poison}
    tried.clear()
    assert (await publisher.run_once()).published == 0
    assert tried == [] and await unpublished(pg) == {poison}
    assert good not in await unpublished(pg)


async def seed_many(
    engine: Any, meeting: int, count: int, *, prefix: str = "evt_"
) -> list[str]:
    from sqlalchemy import text

    ids = [f"{prefix}{n:05d}" for n in range(count)]
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO webhook_outbox (event_id, meeting_id, event_type, sequence, "
                "payload_text, created_at) VALUES (:e, :m, 'meeting.updated', :s, '{}', :c)"
            ),
            [
                {"e": e, "m": meeting, "s": n, "c": T0 + timedelta(milliseconds=n)}
                for n, e in enumerate(ids)
            ],
        )
    return ids


async def test_a_full_batch_to_nine_subscribers_publishes_in_one_tick(pg):
    """500 rows x 9 subscribers = 4,500 deliveries, 36,000 values: past asyncpg's 32,767-argument
    limit for one statement, so the insert must go in chunks inside the one transaction.
    """
    source = StaticSubscriptions()
    subs = [await seed_subscription(pg, source, 1) for _ in range(9)]
    events = await seed_many(pg, await seed_meeting(pg, 1), 500)

    result = await _publisher(sessions(pg), source, batch_size=500).run_once()

    assert (result.published, result.deliveries) == (500, 4500)
    assert await unpublished(pg) == set()
    got = await deliveries(pg)
    assert len(got) == 4500
    assert {(d[0], d[1]) for d in got} == {(e, s.id) for e in events for s in subs}

    # a redo of the same batch (a crash after the inserts, before the publish) adds nothing
    await execute(pg, "UPDATE webhook_outbox SET published_at = NULL")
    again = await _publisher(sessions(pg), source, batch_size=500).run_once()
    assert (again.published, again.deliveries) == (500, 0)
    assert len(await deliveries(pg)) == 4500


async def test_an_account_whose_subscriptions_cannot_be_read_waits(pg):
    source = StaticSubscriptions()
    await seed_subscription(pg, source, 1)
    theirs = await seed_subscription(pg, source, 2)
    m1, m2 = await seed_meeting(pg, 1), await seed_meeting(pg, 2, "abc-defg-hij")
    waiting = await seed_event(pg, m1, "meeting.updated")
    going = await seed_event(pg, m2, "meeting.updated")
    source.unavailable.add(1)

    result = await _publisher(sessions(pg), source).run_once()

    assert (result.published, result.deferred) == (1, 1)
    assert await unpublished(pg) == {waiting}
    assert await deliveries(pg) == {(going, theirs.id, 2, "pending")}
    source.unavailable.clear()
    assert (await _publisher(sessions(pg), source).run_once()).published == 1
    assert await unpublished(pg) == set()


async def test_one_accounts_failing_read_never_stalls_the_others(pg):
    """Account A's read keeps failing with more than a page of rows queued ahead of account B's:
    the same tick reads past A's rows and publishes B's."""
    source = StaticSubscriptions()
    await seed_subscription(pg, source, 1)
    theirs = await seed_subscription(pg, source, 2)
    stuck = await seed_many(pg, await seed_meeting(pg, 1), 201, prefix="evt_a")
    m2 = await seed_meeting(pg, 2, "abc-defg-hij")
    going = [
        await seed_event(pg, m2, "meeting.updated", created=T0 + timedelta(hours=1))
        for _ in range(3)
    ]
    source.unavailable.add(1)
    clock = Mono()
    publisher = _publisher(sessions(pg), source, monotonic=clock)

    result = await publisher.run_once()

    assert (result.published, result.deferred) == (3, 1)
    assert await unpublished(pg) == set(stuck)
    assert await deliveries(pg) == {(e, theirs.id, 2, "pending") for e in going}
    assert source.reads.count(1) == 1  # read once, then read past

    # the next ticks read past A without asking admin-api again, until the backoff ends
    clock.t += FAILED_READ_BACKOFF_S - 1
    assert (await publisher.run_once()).published == 0
    assert source.reads.count(1) == 1
    source.unavailable.clear()
    clock.t += 1
    assert (await publisher.run_once()).published == len(stuck)
    assert (
        source.reads.count(1) == 3
    )  # one read per page (200 + 1); admin-api's is cached 30 s


async def test_a_pause_committed_first_blocks_new_deliveries(pg):
    """admin-api pauses (active=false + cancel pending, one transaction) while the publisher runs:
    the publisher's FOR SHARE waits for the pause and then sees it."""
    from sqlalchemy import text

    source = StaticSubscriptions()
    s = await seed_subscription(pg, source, 1)  # the cached read still lists it
    meeting = await seed_meeting(pg, 1)
    await seed_event(pg, meeting, "meeting.updated")

    conn = await pg.connect()
    tx = await conn.begin()
    await conn.execute(
        text(
            "UPDATE webhook_subscriptions SET active = false WHERE id = CAST(:id AS uuid)"
        ),
        {"id": s.id},
    )
    publishing = asyncio.create_task(_publisher(sessions(pg), source).run_once())
    await asyncio.sleep(0.3)
    assert not publishing.done(), "the publisher must wait for the pause's row lock"
    await conn.execute(
        text(
            "UPDATE webhook_deliveries SET state = 'cancelled', lease_until = NULL "
            "WHERE subscription_id = CAST(:id AS uuid) AND state IN ('pending', 'sending')"
        ),
        {"id": s.id},
    )
    await tx.commit()
    await conn.close()

    result = await publishing
    assert result.published == 1
    assert await deliveries(pg) == set()


async def test_a_pause_that_waits_for_the_publisher_cancels_what_it_inserted(pg):
    """The publisher holds FOR SHARE on the subscription until it commits, so admin-api's pause
    waits, and its cancel then sees (and cancels) the delivery the publisher just inserted.
    """
    from sqlalchemy import text

    source = StaticSubscriptions()
    s = await seed_subscription(pg, source, 1)
    meeting = await seed_meeting(pg, 1)
    event = await seed_event(pg, meeting, "meeting.updated")

    held = CrashingSessions(pg)
    held.gate = asyncio.Event()
    publishing = asyncio.create_task(_publisher(held, source).run_once())
    await asyncio.wait_for(held.reached.wait(), 5)  # inserted, not yet committed

    async def pause() -> None:
        async with pg.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE webhook_subscriptions SET active = false WHERE id = CAST(:id AS uuid)"
                ),
                {"id": s.id},
            )
            await conn.execute(
                text(
                    "UPDATE webhook_deliveries SET state = 'cancelled', lease_until = NULL "
                    "WHERE subscription_id = CAST(:id AS uuid) AND state IN ('pending', 'sending')"
                ),
                {"id": s.id},
            )

    pausing = asyncio.create_task(pause())
    await asyncio.sleep(0.3)
    assert not pausing.done(), "the pause must wait for the publisher's FOR SHARE"
    held.gate.set()
    await publishing
    await pausing
    assert await deliveries(pg) == {(event, s.id, 1, "cancelled")}


async def test_status_writer_events_reach_the_subscriber_byte_for_byte(pg, monkeypatch):
    """write_status → webhook_outbox → publisher → sender → a signed POST of payload_text."""
    from meeting_api.intake import write_status

    monkeypatch.setenv("AUTO_JOIN_LEAD_S", "300")
    source = StaticSubscriptions()
    s = await seed_subscription(pg, source, 1, url="https://hooks.example.com/aw")
    meeting = await seed_meeting(pg, 1)
    async with sessions(pg)() as db:
        written = await write_status(
            db, meeting, "joining", expected_from={"requested"}
        )
        await db.commit()

    await _publisher(sessions(pg), source).run_once()
    posted: list[bytes] = []

    class Receiver:
        async def post(self, target: Any, body: bytes, headers: Any) -> int:
            posted.append(body)
            return 204

    sender = WebhookSender(
        PostgresDeliveryStore(sessions(pg)),
        source,
        SecretBox.from_settings(json.dumps(RING), "k1"),
        Receiver(),
        allowlist=frozenset(),
        resolver=lambda host: ["93.184.216.34"],
    )
    assert await sender.run_once() == 1
    (stored,) = await rows(
        pg,
        "SELECT payload_text FROM webhook_outbox WHERE event_id = :e",
        e=written.event_id,
    )
    assert posted == [stored.payload_text.encode("utf-8")]
    assert await deliveries(pg) == {(written.event_id, s.id, 1, "delivered")}


# ── webhook.test ─────────────────────────────────────────────────────────────────────────────

INTERNAL = {"X-Internal-Secret": "test-internal-secret"}


def _envelope_conforms(envelope: dict[str, Any]) -> None:
    schema = json.loads(
        _contract(
            "meetings", "contracts", "webhook.v1", "webhook.schema.json"
        ).read_text()
    )
    registry = Registry().with_resource(schema["$id"], Resource.from_contents(schema))
    jsonschema.Draft202012Validator(
        {"$ref": f"{schema['$id']}#/$defs/Envelope"}, registry=registry
    ).validate(envelope)


async def test_webhook_test_goes_to_one_subscriber_and_is_never_replayed(
    pg, monkeypatch
):
    monkeypatch.setenv("INTERNAL_API_SECRET", "test-internal-secret")
    source = StaticSubscriptions()
    target = await seed_subscription(pg, source, 1)
    bystander = await seed_subscription(pg, source, 1)
    app = create_app(webhook_tests=PostgresWebhookTests(sessions(pg)))

    async with http(app) as client:
        r = await client.post(
            "/internal/webhooks/test",
            json={"user_id": 1, "subscription_id": target.id},
            headers=INTERNAL,
        )
    assert r.status_code == 202, r.text
    event_id = r.json()["event_id"]
    assert re.fullmatch(r"evt_test_[0-9a-f]{32}", event_id)

    (row,) = await rows(pg, "SELECT * FROM webhook_outbox")
    assert (row.event_id, row.event_type, row.sequence, row.meeting_id) == (
        event_id,
        "webhook.test",
        0,
        None,
    )
    assert row.published_at is not None  # never waits for the publisher, never "stuck"
    envelope = json.loads(row.payload_text)
    _envelope_conforms(envelope)
    assert envelope["event_id"] == event_id
    assert envelope["data"] == {"subscription_id": target.id}
    assert await deliveries(pg) == {(event_id, target.id, 1, "pending")}

    assert (await _publisher(sessions(pg), source).run_once()).published == 0
    assert await deliveries(pg) == {(event_id, target.id, 1, "pending")}

    posted: list[tuple[str, bytes]] = []

    class Receiver:
        async def post(self, t: Any, body: bytes, headers: Any) -> int:
            posted.append((str(t), body))
            return 200

    sender = WebhookSender(
        PostgresDeliveryStore(sessions(pg)),
        source,
        SecretBox.from_settings(json.dumps(RING), "k1"),
        Receiver(),
        allowlist=frozenset(),
        resolver=lambda host: ["93.184.216.34"],
    )
    assert await sender.run_once() == 1
    assert await sender.run_once() == 0
    assert posted == [(target.url, row.payload_text.encode())]
    assert await deliveries(pg) == {(event_id, target.id, 1, "delivered")}
    assert bystander.id not in {d[1] for d in await deliveries(pg)}


async def test_webhook_test_refuses_another_accounts_subscription(pg):
    source = StaticSubscriptions()
    theirs = await seed_subscription(pg, source, 2)
    tests = PostgresWebhookTests(sessions(pg))
    with pytest.raises(SubscriptionNotFound):
        await tests.queue_test(1, theirs.id)
    with pytest.raises(SubscriptionNotFound):
        await tests.queue_test(1, str(uuid.uuid4()))
    with pytest.raises(SubscriptionNotFound):
        await tests.queue_test(1, "not-a-uuid")
    assert await rows(pg, "SELECT event_id FROM webhook_outbox") == []


class FakeTests:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str]] = []

    async def queue_test(self, user_id: int, subscription_id: str) -> str:
        self.calls.append((user_id, subscription_id))
        if subscription_id == "missing":
            raise SubscriptionNotFound(subscription_id)
        return "evt_test_" + "0" * 32


@pytest.mark.parametrize(
    "secret,headers,body,status",
    [
        (None, INTERNAL, {"user_id": 1, "subscription_id": "s"}, 503),
        ("test-internal-secret", {}, {"user_id": 1, "subscription_id": "s"}, 403),
        (
            "test-internal-secret",
            {"X-Internal-Secret": "wrong"},
            {"user_id": 1, "subscription_id": "s"},
            403,
        ),
        (
            "test-internal-secret",
            INTERNAL,
            {"user_id": "1", "subscription_id": "s"},
            400,
        ),
        (
            "test-internal-secret",
            INTERNAL,
            {"user_id": True, "subscription_id": "s"},
            400,
        ),
        ("test-internal-secret", INTERNAL, {"user_id": 1}, 400),
        ("test-internal-secret", INTERNAL, [1], 400),
        (
            "test-internal-secret",
            INTERNAL,
            {"user_id": 1, "subscription_id": "missing"},
            404,
        ),
        ("test-internal-secret", INTERNAL, {"user_id": 1, "subscription_id": "s"}, 202),
    ],
)
async def test_the_test_route_checks_the_secret_and_the_body(
    monkeypatch, secret, headers, body, status
):
    if secret is None:
        monkeypatch.delenv("INTERNAL_API_SECRET", raising=False)
    else:
        monkeypatch.setenv("INTERNAL_API_SECRET", secret)
    fake = FakeTests()
    async with http(create_app(webhook_tests=fake)) as client:
        r = await client.post("/internal/webhooks/test", json=body, headers=headers)
    assert r.status_code == status, r.text
    if status == 202:
        assert r.json() == {"event_id": "evt_test_" + "0" * 32}
        assert fake.calls == [(1, "s")]
    elif status in (503, 403, 400):
        assert fake.calls == []
        assert "error" in r.json()


def test_the_route_lives_beside_the_app_not_in_the_outbox_writer():
    import meeting_api.intake.outbox as outbox
    from meeting_api.webhooks.internal_router import build_webhook_test_router

    assert callable(build_webhook_test_router)
    assert not hasattr(outbox, "build_webhook_test_router")


async def test_without_a_database_the_test_route_is_unavailable(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_SECRET", "test-internal-secret")
    async with http(create_app()) as client:
        r = await client.post(
            "/internal/webhooks/test",
            json={"user_id": 1, "subscription_id": "s"},
            headers=INTERNAL,
        )
    assert r.status_code == 503


# ── no Redis ─────────────────────────────────────────────────────────────────────────────────


async def test_the_publisher_never_touches_redis(pg, monkeypatch):
    import redis.asyncio.client as redis_client

    async def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("webhook publishing must not use Redis")

    monkeypatch.setattr(redis_client.Redis, "execute_command", refuse)
    source = StaticSubscriptions()
    await seed_subscription(pg, source, 1)
    await seed_event(pg, await seed_meeting(pg, 1), "meeting.updated")
    assert (await _publisher(sessions(pg), source).run_once()).deliveries == 1
    await PostgresWebhookTests(sessions(pg)).queue_test(1, source.by_user[1][0].id)


def test_the_publisher_module_imports_no_redis():
    code = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "meeting_api"
        / "intake"
        / "outbox.py"
    ).read_text()
    assert not re.search(r"^\s*(import|from)\s+\S*redis", code, re.M)
    assert "redis_client" not in code


# ── wiring (``python -m meeting_api``) ───────────────────────────────────────────────────────


async def test_the_production_app_mounts_the_test_route_over_postgres(monkeypatch):
    import meeting_api.__main__ as main_mod

    monkeypatch.setattr(main_mod, "_require_config", lambda env=None: None)
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+asyncpg://nobody:dummy@127.0.0.1:1/none"
    )
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin-token")
    monkeypatch.setenv("INTERNAL_API_SECRET", "test-internal-secret")
    app = main_mod.build_production_app()
    assert isinstance(app.state.webhook_tests, PostgresWebhookTests)
    async with http(
        app
    ) as client:  # mounted: a bad body is refused before any database call
        r = await client.post("/internal/webhooks/test", json={}, headers=INTERNAL)
    assert r.status_code == 400, r.text


class _StopLoop(Exception):
    pass


async def _yield() -> None:
    fut = asyncio.get_running_loop().create_future()
    asyncio.get_running_loop().call_soon(fut.set_result, None)
    await fut


async def test_the_publisher_is_single_flight(monkeypatch):
    import types

    import meeting_api.__main__ as main_mod
    import meeting_api.intake.outbox as outbox_mod
    import meeting_api.sweeps.single_flight as single_flight
    from meeting_api.sweeps.single_flight import sweep_lock_key

    guarded: list[int] = []
    ran: list[str] = []

    async def record_guard(lock: Any, key: int, body: Any) -> bool:
        guarded.append(key)
        if key == sweep_lock_key("webhook-publisher"):
            await body()
        return True

    async def record_tick(self: Any) -> Any:
        ran.append("publish")

    async def stop(delay: float, *a: Any, **kw: Any) -> None:
        raise _StopLoop()

    monkeypatch.setattr(single_flight, "run_single_flight", record_guard)
    monkeypatch.setattr(outbox_mod.OutboxPublisher, "run_once", record_tick)
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
    )
    monkeypatch.setattr(asyncio, "sleep", stop)
    async with app.router.lifespan_context(app):
        for _ in range(20):
            await _yield()
    assert ran == ["publish"]
    assert guarded.count(sweep_lock_key("webhook-publisher")) == 1
