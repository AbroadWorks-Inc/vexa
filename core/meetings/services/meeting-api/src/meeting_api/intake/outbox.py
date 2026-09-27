"""The outbox publisher (§1.8) and the ``webhook.test`` writer (§2.7).

Every meeting event is already in ``webhook_outbox`` when its transaction commits (the status
writer, §1.4); ``payload_text`` is the exact body a subscriber receives. The publisher turns those
rows into ``webhook_deliveries`` rows, which the senders (``webhooks/sender.py``) deliver.

``OutboxPublisher.run_once`` — single-flight, every ``WEBHOOK_PUBLISH_INTERVAL_S``:

1. read up to ``BATCH_SIZE`` (500) unpublished rows, oldest first (the
   ``ix_webhook_outbox_unpublished`` order), each with its meeting's account;
2. read each account's subscriptions from admin-api (``webhooks/subscriptions.py``, cached 30 s),
   up to ``READ_CONCURRENCY`` at once. An account whose read fails keeps its rows unpublished, is
   read past (its rows left out of the query) for ``FAILED_READ_BACKOFF_S``, and the same tick reads
   the next rows, so one account's failing read never stalls the others;
3. in ONE transaction for the rest of the batch: re-read ``webhook_subscriptions.active`` for the
   matching subscriptions under ``FOR SHARE``, insert one ``pending`` delivery due now per matching
   active subscriber (``ON CONFLICT (event_id, subscription_id) DO NOTHING``, ``INSERT_CHUNK``
   rows per statement), and set ``published_at``.

"Matching" is ``fan_out``: the subscription belongs to the meeting's account and wants the event
(``events == []`` or the event type is listed). ``webhook.test`` rows are never fanned out: they
carry their one delivery already.

Which source decides what: the admin-api read decides which subscriptions an account has and which
events each wants; the ``webhook_subscriptions`` table decides, inside the publishing transaction,
whether each is still active. ``FOR SHARE`` on those rows closes the race with a pause or delete in
admin-api (which flips ``active`` and cancels pending deliveries in one transaction): a pause that
commits first is seen, and a pause that comes second waits for this commit and then cancels what it
inserted. A crash before the commit publishes nothing, and the next tick redoes the batch; the
conflict clause means a redo never duplicates a delivery.

``PostgresWebhookTests.queue_test`` — behind ``POST /internal/webhooks/test``
(``webhooks/internal_router.py``, admin-api's hand-off of ``POST /v2/webhooks/{id}/test``): in one
transaction, an outbox row (``sequence`` 0, id
``evt_test_<uuid4 hex>``, ``meeting_id`` NULL, ``published_at`` now, so the publisher never takes
it and it never counts as unpublished) and exactly one delivery for that subscription. The route
answers ``{"event_id"}``.

Redis is not used anywhere here.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Collection, Mapping, Optional, Protocol, Sequence

from ..obs import log_event
from ..webhooks.subscriptions import (
    Subscription,
    SubscriptionSource,
    SubscriptionsUnavailable,
)
from .projection import iso_utc
from .status import API_VERSION

__all__ = [
    "BATCH_SIZE",
    "INSERT_CHUNK",
    "TEST_EVENT",
    "Fanout",
    "OutboxPublisher",
    "OutboxRow",
    "PostgresWebhookTests",
    "PublishResult",
    "SubscriptionNotFound",
    "WebhookTests",
    "fan_out",
]

BATCH_SIZE = 500
#: Deliveries per INSERT statement (8 values each): asyncpg refuses a statement with more than
#: 32,767 arguments, and a full batch fans out to up to 500 x 20 subscribers.
INSERT_CHUNK = 1000
#: How long an account whose subscription read failed is read past before it is tried again (the
#: read cache's own lifetime), and how many accounts are read at once.
FAILED_READ_BACKOFF_S = 30.0
READ_CONCURRENCY = 8
TEST_EVENT = "webhook.test"
_SPAN = "webhooks.publish"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class OutboxRow:
    event_id: str
    event_type: str
    meeting_id: Optional[int]
    user_id: Optional[int]


@dataclass(frozen=True)
class Fanout:
    event_id: str
    subscription_id: str
    user_id: int


@dataclass(frozen=True)
class PublishResult:
    """Rows published and deliveries inserted this tick; ``deferred`` = accounts whose rows wait
    because their subscription read failed."""

    published: int
    deliveries: int
    deferred: int


def fan_out(
    rows: Sequence[OutboxRow], subscriptions: Mapping[int, Sequence[Subscription]]
) -> list[Fanout]:
    """One delivery per (row, subscription of the row's account that wants its event type)."""
    out = []
    for row in rows:
        if row.event_type == TEST_EVENT or row.user_id is None:
            continue
        for sub in subscriptions.get(row.user_id, ()):
            if sub.wants(row.event_type):
                out.append(Fanout(row.event_id, sub.id, row.user_id))
    return out


def _uuid(value: str) -> Optional[uuid.UUID]:
    try:
        return uuid.UUID(value)
    except (ValueError, TypeError, AttributeError):
        return None


class OutboxPublisher:
    """The single-flight outbox publisher (see the module docstring)."""

    def __init__(
        self,
        session_factory: Any,
        subscriptions: SubscriptionSource,
        *,
        batch_size: int = BATCH_SIZE,
        clock: Callable[[], datetime] = _utcnow,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._session_factory = session_factory
        self._subscriptions = subscriptions
        self._batch_size = batch_size
        self._clock = clock
        self._monotonic = monotonic
        self._reads = asyncio.Semaphore(READ_CONCURRENCY)
        #: account → monotonic instant its failed subscription read may be retried
        self._waiting: dict[int, float] = {}

    async def _unpublished(self, exclude: Collection[int]) -> list[OutboxRow]:
        from sqlalchemy import or_, select

        from ..sessions.models import Meeting, WebhookOutbox

        query = (
            select(
                WebhookOutbox.event_id,
                WebhookOutbox.event_type,
                WebhookOutbox.meeting_id,
                Meeting.user_id,
            )
            .outerjoin(Meeting, Meeting.id == WebhookOutbox.meeting_id)
            .where(WebhookOutbox.published_at.is_(None))
        )
        if exclude:
            query = query.where(
                or_(Meeting.user_id.is_(None), Meeting.user_id.notin_(sorted(exclude)))
            )
        async with self._session_factory() as db:
            result = await db.execute(
                query.order_by(WebhookOutbox.created_at).limit(self._batch_size)
            )
            return [OutboxRow(*row) for row in result.all()]

    async def _read(self, user_id: int) -> Optional[list[Subscription]]:
        async with self._reads:
            try:
                return await self._subscriptions.for_account(user_id)
            except SubscriptionsUnavailable as exc:
                log_event(
                    "webhook_publish_deferred",
                    audience="operator",
                    level="warning",
                    span=_SPAN,
                    user_id=user_id,
                    fields={"error": str(exc), "retry_in_s": FAILED_READ_BACKOFF_S},
                )
                return None

    async def run_once(self) -> PublishResult:
        now = self._monotonic()
        waiting = {uid for uid, until in self._waiting.items() if until > now}
        self._waiting = {uid: self._waiting[uid] for uid in waiting}
        published = inserted = 0
        while True:
            rows = await self._unpublished(waiting)
            if not rows:
                break
            accounts = sorted(
                {
                    row.user_id
                    for row in rows
                    if row.user_id is not None and row.event_type != TEST_EVENT
                }
            )
            answers = await asyncio.gather(*(self._read(uid) for uid in accounts))
            subs = {uid: got for uid, got in zip(accounts, answers) if got is not None}
            failed = {uid for uid, got in zip(accounts, answers) if got is None}
            for uid in failed:
                self._waiting[uid] = now + FAILED_READ_BACKOFF_S
            waiting |= failed
            ready = [row for row in rows if row.user_id not in failed]
            if ready:
                inserted += await self._publish(ready, fan_out(ready, subs))
                published += len(ready)
            if not failed:
                break
        return PublishResult(published, inserted, len(waiting))

    async def _publish(
        self, rows: Sequence[OutboxRow], wanted: Sequence[Fanout]
    ) -> int:
        from sqlalchemy import select, update
        from sqlalchemy.dialects.postgresql import insert

        from ..sessions.models import (
            WebhookDelivery,
            WebhookOutbox,
            WebhookSubscription,
        )

        now = self._clock()
        ids = {w.subscription_id: _uuid(w.subscription_id) for w in wanted}
        async with self._session_factory() as db:
            inserted = 0
            keyed = [key for key in ids.values() if key is not None]
            if keyed:
                active = {
                    (str(sid), int(uid))
                    for sid, uid in (
                        await db.execute(
                            select(WebhookSubscription.id, WebhookSubscription.user_id)
                            .where(
                                WebhookSubscription.id.in_(keyed),
                                WebhookSubscription.active.is_(True),
                            )
                            .order_by(WebhookSubscription.id)
                            .with_for_update(read=True)
                        )
                    ).all()
                }
                values = [
                    {
                        "event_id": w.event_id,
                        "subscription_id": ids[w.subscription_id],
                        "user_id": w.user_id,
                        "state": "pending",
                        "attempts": 0,
                        "next_attempt_at": now,
                        "created_at": now,
                        "updated_at": now,
                    }
                    for w in wanted
                    if (str(ids[w.subscription_id]), w.user_id) in active
                ]
                for start in range(0, len(values), INSERT_CHUNK):
                    result = await db.execute(
                        insert(WebhookDelivery.__table__)
                        .values(values[start : start + INSERT_CHUNK])
                        .on_conflict_do_nothing(
                            index_elements=["event_id", "subscription_id"]
                        )
                    )
                    inserted += int(getattr(result, "rowcount", 0) or 0)
            await db.execute(
                update(WebhookOutbox)
                .where(
                    WebhookOutbox.event_id.in_([row.event_id for row in rows]),
                    WebhookOutbox.published_at.is_(None),
                )
                .values(published_at=now)
                .execution_options(synchronize_session=False)
            )
            await db.commit()
        return inserted


class SubscriptionNotFound(Exception):
    """The subscription doesn't exist or belongs to another account."""


class WebhookTests(Protocol):
    """Queues a ``webhook.test`` send to one subscription; returns its event id."""

    async def queue_test(self, user_id: int, subscription_id: str) -> str: ...


class PostgresWebhookTests:
    """``WebhookTests`` over Postgres (see the module docstring)."""

    def __init__(
        self, session_factory: Any, *, clock: Callable[[], datetime] = _utcnow
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock

    async def queue_test(self, user_id: int, subscription_id: str) -> str:
        from sqlalchemy import select

        from ..sessions.models import (
            WebhookDelivery,
            WebhookOutbox,
            WebhookSubscription,
        )

        sid = _uuid(subscription_id)
        if sid is None:
            raise SubscriptionNotFound(subscription_id)
        now = self._clock()
        stamp = now.replace(microsecond=0)
        event_id = "evt_test_" + uuid.uuid4().hex
        envelope = {
            "event_id": event_id,
            "event_type": TEST_EVENT,
            "api_version": API_VERSION,
            "created_at": iso_utc(stamp),
            "data": {"subscription_id": str(sid)},
        }
        async with self._session_factory() as db:
            owned = (
                await db.execute(
                    select(WebhookSubscription.id).where(
                        WebhookSubscription.id == sid,
                        WebhookSubscription.user_id == user_id,
                    )
                )
            ).scalar_one_or_none()
            if owned is None:
                raise SubscriptionNotFound(subscription_id)
            db.add(
                WebhookOutbox(
                    event_id=event_id,
                    meeting_id=None,
                    event_type=TEST_EVENT,
                    sequence=0,
                    payload_text=json.dumps(
                        envelope, separators=(",", ":"), sort_keys=True
                    ),
                    created_at=stamp,
                    published_at=now,
                )
            )
            await db.flush()
            db.add(
                WebhookDelivery(
                    event_id=event_id,
                    subscription_id=sid,
                    user_id=user_id,
                    state="pending",
                    attempts=0,
                    next_attempt_at=now,
                    created_at=now,
                    updated_at=now,
                )
            )
            await db.commit()
        log_event(
            "webhook_test_queued",
            audience="operator",
            span=_SPAN,
            user_id=user_id,
            fields={"event_id": event_id, "subscription_id": str(sid)},
        )
        return event_id
