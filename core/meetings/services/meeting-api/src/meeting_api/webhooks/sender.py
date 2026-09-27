"""The subscription sender (§1.8): delivery state in Postgres, leased claims, signed posts.

One sender loop runs per meeting-api replica, every ``WEBHOOK_SEND_INTERVAL_S``. Each tick
(``WebhookSender.run_once``):

1. **Claim** up to ``claim_limit`` due ``webhook_deliveries`` rows (``state IN ('pending',
   'sending') AND next_attempt_at <= now AND (lease_until IS NULL OR lease_until < now)``) with
   ``FOR UPDATE SKIP LOCKED``, set ``state = 'sending'`` and ``lease_until = now + LEASE_S``, and
   commit. Two replicas never claim the same row; a crashed replica's row is claimed again once its
   lease has passed. Every instant here is the database's ``now()``, never a replica's clock.
2. **Re-check** each claim, concurrently: the subscription is still active in
   ``webhook_subscriptions`` (else the row is ``cancelled``, with no attempt), and its URL passes
   the SSRF guard (``ssrf.validate_webhook_url`` with ``WEBHOOK_PRIVATE_HOST_ALLOWLIST``, resolved
   in a worker thread under ``URL_CHECK_TIMEOUT_S``).
   Before posting, the claim must still have ``SEND_TIMEOUT_S + LEASE_MARGIN_S`` of its lease left,
   measured on the monotonic clock from just before the claim. If it hasn't (a slow subscription
   read or DNS), nothing is sent or written and the row is left for the next claim.
3. **Sign** the stored ``payload_text`` (``signing.signed_headers``; the secret opened by
   ``secret_box.py``, the previous one too while it is still valid) and post those exact bytes,
   under ``SEND_TIMEOUT_S`` in total, to the address the guard validated.
4. **Record** one attempt row and move the row, in one transaction:

   ============================================  =====================  ==================
   what happened                                 state                  attempt ``outcome``
   ============================================  =====================  ==================
   2xx                                           ``delivered``          ``delivered``
   5xx, 429, timeout, connection error, or       ``pending`` at +60 s,  ``retry``
   nothing to sign or resolve with (the          +300 s, +1800 s,
   subscription read, the secret, DNS)           +7200 s; then ``dead`` ``dead``
   any other answer, or a URL the guard refuses  ``failed``             ``failed``
   ============================================  =====================  ==================

Every move out of ``sending`` is guarded ``WHERE id = :id AND state = 'sending' AND lease_until =
:the claim's lease``: a delivery that admin-api cancelled while it was in flight (a pause or a
delete) stays ``cancelled``, and a claim whose lease another replica has since taken writes
nothing. The attempt row is written either way, since the send happened.

Errors stored and logged name a type or a cause, never a URL, a host, a secret or a key. Redis is
not used anywhere here.

Metrics (§1.13): each claim moves ``aw_webhook_deliveries_total{event_type,outcome,user_id}`` once,
with the attempt's ``outcome`` (``delivered``, ``retry``, ``failed``, ``dead``) or what happened
instead (``cancelled``, ``superseded``, ``lease_short``, ``crashed``); each post made moves
``aw_webhook_delivery_seconds``.

``DeliveryStore`` is the storage port (``PostgresDeliveryStore`` in production) and ``Poster`` the
transport port (``HttpxPoster``: httpx over ``ssrf.build_pinned_transport``, dialling the address
the guard validated with the Host header and TLS SNI of the real host).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Collection, List, Mapping, Optional, Protocol

from ..metrics import webhook_delivery
from ..obs import log_event
from .secret_box import SecretBox, SecretBoxError
from .signing import signed_headers
from .ssrf import (
    PinnedURL,
    SSRFError,
    UnresolvableHost,
    build_pinned_transport,
    validate_webhook_url,
)
from .subscriptions import SubscriptionSource, SubscriptionsUnavailable

__all__ = [
    "CLAIM_LIMIT",
    "LEASE_S",
    "RETRY_SCHEDULE_S",
    "SEND_TIMEOUT_S",
    "URL_CHECK_TIMEOUT_S",
    "Claim",
    "DeliveryResult",
    "DeliveryStore",
    "HttpxPoster",
    "Poster",
    "PostgresDeliveryStore",
    "TransportError",
    "WebhookSender",
]

LEASE_S = 60
LEASE_MARGIN_S = 5.0
SEND_TIMEOUT_S = 10.0
URL_CHECK_TIMEOUT_S = 5.0
RETRY_SCHEDULE_S = (60, 300, 1800, 7200)
CLAIM_LIMIT = 50

_SPAN = "webhooks.delivery"


@dataclass(frozen=True)
class Claim:
    """A delivery this sender holds until ``lease_until``; ``attempt`` is the one it will make."""

    id: int
    event_id: str
    event_type: str
    subscription_id: str
    user_id: int
    attempt: int
    lease_until: datetime
    payload_text: str


@dataclass(frozen=True)
class DeliveryResult:
    """What one attempt did: the row's next ``state`` and the attempt row's fields."""

    state: str
    outcome: str
    status_code: Optional[int]
    error: Optional[str]
    duration_ms: Optional[int]
    retry_in_s: Optional[int]


class DeliveryStore(Protocol):
    """Delivery rows. The store owns the clock: the claim predicate, ``lease_until``, the retry
    time and every stamp are its ``now`` (Postgres: the database's ``now()``)."""

    async def claim(self, *, lease_s: int, limit: int) -> list[Claim]: ...

    async def is_active(self, subscription_id: str) -> bool: ...

    async def cancel(self, claim: Claim) -> bool: ...

    async def record(self, claim: Claim, result: DeliveryResult) -> bool: ...


class TransportError(Exception):
    """The post didn't get an answer (connection refused, reset, TLS, ...). The message is the
    fault's type name only."""


class Poster(Protocol):
    async def post(
        self, target: PinnedURL, body: bytes, headers: Mapping[str, str]
    ) -> int: ...


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _retry(claim: Claim, **fields: Any) -> DeliveryResult:
    if claim.attempt <= len(RETRY_SCHEDULE_S):
        return DeliveryResult(
            state="pending",
            outcome="retry",
            retry_in_s=RETRY_SCHEDULE_S[claim.attempt - 1],
            **fields,
        )
    return DeliveryResult(state="dead", outcome="dead", retry_in_s=None, **fields)


def _answered(claim: Claim, code: int, duration_ms: int) -> DeliveryResult:
    fields: dict[str, Any] = {
        "status_code": code,
        "duration_ms": duration_ms,
        "error": None,
    }
    if 200 <= code < 300:
        return DeliveryResult(
            state="delivered", outcome="delivered", retry_in_s=None, **fields
        )
    if code >= 500 or code == 429:
        return _retry(claim, **{**fields, "error": f"HTTP {code}"})
    return DeliveryResult(
        state="failed",
        outcome="failed",
        retry_in_s=None,
        **{**fields, "error": f"HTTP {code}"},
    )


def _unsent(claim: Claim, error: str) -> DeliveryResult:
    return _retry(claim, status_code=None, error=error, duration_ms=None)


class WebhookSender:
    """One replica's sender (see the module docstring)."""

    def __init__(
        self,
        store: DeliveryStore,
        subscriptions: SubscriptionSource,
        box: SecretBox,
        poster: Poster,
        *,
        allowlist: Collection[str],
        resolver: Optional[Callable[[str], List[str]]] = None,
        clock: Callable[[], datetime] = _utcnow,
        monotonic: Callable[[], float] = time.monotonic,
        claim_limit: int = CLAIM_LIMIT,
        send_timeout_s: float = SEND_TIMEOUT_S,
        url_check_timeout_s: float = URL_CHECK_TIMEOUT_S,
    ) -> None:
        self._store = store
        self._subscriptions = subscriptions
        self._box = box
        self._poster = poster
        self._allowlist = frozenset(allowlist)
        self._resolver = resolver
        self._clock = clock
        self._monotonic = monotonic
        self._claim_limit = claim_limit
        self._send_timeout_s = send_timeout_s
        self._url_check_timeout_s = url_check_timeout_s

    async def run_once(self) -> int:
        """One tick: claim what is due and deliver it. Returns how many rows were claimed."""
        # Read before the claim, so the lease measured from here is never longer than the one the
        # store granted.
        claimed_at = self._monotonic()
        claims = await self._store.claim(lease_s=LEASE_S, limit=self._claim_limit)
        if claims:
            await asyncio.gather(*(self._deliver(c, claimed_at) for c in claims))
        return len(claims)

    async def _deliver(self, claim: Claim, claimed_at: float) -> None:
        try:
            await self._deliver_claim(claim, claimed_at)
        except asyncio.CancelledError:
            raise
        # the lease expires and the row is claimed again
        except Exception as exc:  # noqa: BLE001
            webhook_delivery(claim.event_type, "crashed", claim.user_id, None)
            log_event(
                "webhook_delivery_crashed",
                audience="operator",
                level="error",
                span=_SPAN,
                user_id=claim.user_id,
                fields={**self._ids(claim), "error": type(exc).__name__},
            )

    async def _deliver_claim(self, claim: Claim, claimed_at: float) -> None:
        now = self._clock()
        if not await self._store.is_active(claim.subscription_id):
            cancelled = await self._store.cancel(claim)
            self._log(claim, "cancelled" if cancelled else "superseded", None)
            return
        try:
            sub = await self._subscriptions.find(claim.user_id, claim.subscription_id)
        except SubscriptionsUnavailable:
            sub = None
            error = "subscriptions unavailable"
        else:
            error = "subscription not in the account's read"
        if sub is None:
            await self._finish(claim, _unsent(claim, error))
            return

        try:
            target = await asyncio.wait_for(
                asyncio.to_thread(
                    validate_webhook_url,
                    sub.url,
                    self._resolver,
                    allowlist=self._allowlist,
                ),
                timeout=self._url_check_timeout_s,
            )
        except UnresolvableHost:
            await self._finish(claim, _unsent(claim, "host could not be resolved"))
            return
        except asyncio.TimeoutError:
            await self._finish(claim, _unsent(claim, "host resolution timed out"))
            return
        except SSRFError as exc:
            refused = DeliveryResult(
                state="failed",
                outcome="failed",
                status_code=None,
                error=f"url refused: {exc}",
                duration_ms=None,
                retry_in_s=None,
            )
            await self._finish(claim, refused)
            return

        try:
            secret = self._box.decrypt(sub.secret_enc, sub.enc_key_id)
        except SecretBoxError:
            await self._finish(claim, _unsent(claim, "secret could not be opened"))
            return
        previous = None
        previous_enc, previous_key = sub.previous_secret_enc, sub.previous_enc_key_id
        if sub.previous_live(now) and previous_enc and previous_key:
            try:
                previous = self._box.decrypt(previous_enc, previous_key)
            except SecretBoxError:
                log_event(
                    "webhook_previous_secret_unreadable",
                    audience="operator",
                    level="warning",
                    span=_SPAN,
                    user_id=claim.user_id,
                    fields=self._ids(claim),
                )

        if (
            self._monotonic() - claimed_at + self._send_timeout_s + LEASE_MARGIN_S
            > LEASE_S
        ):
            # Too little lease left to post and record inside it: send nothing, write nothing,
            # and let the lease run out so the row is claimed again.
            self._log(claim, "lease_short", None)
            return
        body = claim.payload_text.encode("utf-8")
        headers = signed_headers(
            body,
            secret=secret,
            timestamp=int(now.timestamp()),
            previous_secret=previous,
        )
        started = time.monotonic()
        try:
            code = await asyncio.wait_for(
                self._poster.post(target, body, headers), timeout=self._send_timeout_s
            )
        except asyncio.TimeoutError:
            result = _unsent(claim, "timeout")
        except TransportError as exc:
            result = _unsent(claim, f"connection error ({exc})")
        except SSRFError:
            result = _unsent(claim, "address refused at connect")
        else:
            elapsed = int((time.monotonic() - started) * 1000)
            result = _answered(claim, code, elapsed)
        if result.duration_ms is None:
            result = DeliveryResult(
                state=result.state,
                outcome=result.outcome,
                status_code=result.status_code,
                error=result.error,
                duration_ms=int((time.monotonic() - started) * 1000),
                retry_in_s=result.retry_in_s,
            )
        await self._finish(claim, result)

    async def _finish(self, claim: Claim, result: DeliveryResult) -> None:
        moved = await self._store.record(claim, result)
        self._log(claim, result.state if moved else "superseded", result)

    @staticmethod
    def _ids(claim: Claim) -> dict[str, Any]:
        return {
            "delivery_id": claim.id,
            "event_id": claim.event_id,
            "event_type": claim.event_type,
            "subscription_id": claim.subscription_id,
            "attempt": claim.attempt,
        }

    def _log(self, claim: Claim, state: str, result: Optional[DeliveryResult]) -> None:
        webhook_delivery(
            claim.event_type,
            state if result is None or state == "superseded" else result.outcome,
            claim.user_id,
            (
                None
                if result is None or result.duration_ms is None
                else result.duration_ms / 1000
            ),
        )
        fields = {**self._ids(claim), "state": state}
        if result is not None:
            fields.update(
                outcome=result.outcome,
                status_code=result.status_code,
                error=result.error,
                duration_ms=result.duration_ms,
            )
        failed = state in ("failed", "dead")
        log_event(
            "webhook_delivery",
            audience="operator",
            level="warning" if failed else "info",
            span=_SPAN,
            user_id=claim.user_id,
            fields=fields,
        )


class HttpxPoster:
    """``Poster`` over httpx: dials the address the guard validated (``target.pinned_ips``) with
    the real host's Host header and TLS SNI; an allow-listed host is dialled as is. Redirects are
    not followed."""

    def __init__(
        self,
        *,
        allowlist: Collection[str],
        timeout_s: float = SEND_TIMEOUT_S,
        inner: Any = None,
    ) -> None:
        self._allowlist = frozenset(allowlist)
        self._timeout_s = timeout_s
        self._inner = inner

    async def post(
        self, target: PinnedURL, body: bytes, headers: Mapping[str, str]
    ) -> int:
        import httpx

        pinned = list(target.pinned_ips)
        transport = build_pinned_transport(
            self._inner, resolver=lambda _host: pinned, allowlist=self._allowlist
        )
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_s, transport=transport, follow_redirects=False
            ) as client:
                resp = await client.post(
                    target.url, content=body, headers=dict(headers)
                )
        except httpx.TimeoutException as exc:
            raise asyncio.TimeoutError() from exc
        except (httpx.HTTPError, OSError) as exc:
            raise TransportError(type(exc).__name__) from exc
        return resp.status_code


class PostgresDeliveryStore:
    """``DeliveryStore`` over ``webhook_deliveries`` / ``webhook_delivery_attempts`` /
    ``webhook_outbox`` / ``webhook_subscriptions`` (one short transaction per call).

    Time is the database's ``now()``: the claim predicate, ``lease_until = now() + lease``, the
    retry time and every stamp, so a replica whose clock is off can neither shorten a lease nor
    claim a row early. ``clock`` replaces ``now()`` for tests only."""

    _NOW = "COALESCE(CAST(:now AS timestamptz), now())"

    _CLAIM = f"""
        WITH due AS (
            SELECT id FROM webhook_deliveries
            WHERE state IN ('pending', 'sending')
              AND next_attempt_at <= {_NOW}
              AND (lease_until IS NULL OR lease_until < {_NOW})
            ORDER BY next_attempt_at, id
            LIMIT :limit
            FOR UPDATE SKIP LOCKED
        ), claimed AS (
            UPDATE webhook_deliveries AS d
            SET state = 'sending',
                lease_until = {_NOW} + make_interval(secs => CAST(:lease_s AS double precision)),
                updated_at = {_NOW}
            FROM due
            WHERE d.id = due.id
            RETURNING d.id, d.event_id, d.subscription_id, d.user_id, d.attempts, d.lease_until
        )
        SELECT c.id, c.event_id, c.subscription_id, c.user_id, c.attempts, c.lease_until,
               o.event_type, o.payload_text
        FROM claimed AS c
        JOIN webhook_outbox AS o ON o.event_id = c.event_id
        ORDER BY c.id
    """

    _OWNED = "id = :id AND state = 'sending' AND lease_until = :lease"

    def __init__(
        self,
        session_factory: Any,
        *,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock

    def _now(self) -> Optional[datetime]:
        return None if self._clock is None else self._clock()

    async def claim(self, *, lease_s: int, limit: int) -> list[Claim]:
        from sqlalchemy import text

        async with self._session_factory() as db:
            rows = (
                await db.execute(
                    text(self._CLAIM),
                    {"now": self._now(), "lease_s": lease_s, "limit": limit},
                )
            ).all()
            await db.commit()
        return [
            Claim(
                id=int(row.id),
                event_id=row.event_id,
                event_type=row.event_type,
                subscription_id=str(row.subscription_id),
                user_id=int(row.user_id),
                attempt=int(row.attempts) + 1,
                lease_until=row.lease_until,
                payload_text=row.payload_text,
            )
            for row in rows
        ]

    async def is_active(self, subscription_id: str) -> bool:
        from sqlalchemy import text

        async with self._session_factory() as db:
            active = (
                await db.execute(
                    text(
                        "SELECT active FROM webhook_subscriptions "
                        "WHERE id = CAST(:id AS uuid)"
                    ),
                    {"id": subscription_id},
                )
            ).scalar_one_or_none()
        return bool(active)

    async def cancel(self, claim: Claim) -> bool:
        from sqlalchemy import text

        async with self._session_factory() as db:
            result = await db.execute(
                text(
                    "UPDATE webhook_deliveries SET state = 'cancelled', lease_until = NULL, "
                    f"updated_at = {self._NOW} WHERE {self._OWNED}"
                ),
                {"now": self._now(), "id": claim.id, "lease": claim.lease_until},
            )
            await db.commit()
        return int(getattr(result, "rowcount", 0) or 0) == 1

    async def record(self, claim: Claim, result: DeliveryResult) -> bool:
        from sqlalchemy import text

        now = self._now()
        async with self._session_factory() as db:
            await db.execute(
                text(
                    "INSERT INTO webhook_delivery_attempts "
                    "(delivery_id, attempt, outcome, status_code, error, duration_ms, created_at) "
                    "SELECT CAST(:id AS bigint), CAST(:attempt AS integer), CAST(:outcome AS text), "
                    "CAST(:status_code AS integer), CAST(:error AS text), "
                    f"CAST(:duration_ms AS integer), {self._NOW} "
                    "WHERE EXISTS (SELECT 1 FROM webhook_deliveries WHERE id = CAST(:id AS bigint))"
                ),
                {
                    "id": claim.id,
                    "attempt": claim.attempt,
                    "outcome": result.outcome,
                    "status_code": result.status_code,
                    "error": result.error,
                    "duration_ms": result.duration_ms,
                    "now": now,
                },
            )
            moved = await db.execute(
                text(
                    "UPDATE webhook_deliveries SET state = :state, attempts = :attempt, "
                    "next_attempt_at = CASE WHEN CAST(:retry_in AS double precision) IS NULL "
                    f"THEN next_attempt_at ELSE {self._NOW} + "
                    "make_interval(secs => CAST(:retry_in AS double precision)) END, "
                    "lease_until = NULL, last_status_code = :status_code, last_error = :error, "
                    f"updated_at = {self._NOW} WHERE {self._OWNED}"
                ),
                {
                    "state": result.state,
                    "attempt": claim.attempt,
                    "retry_in": result.retry_in_s,
                    "status_code": result.status_code,
                    "error": result.error,
                    "now": now,
                    "id": claim.id,
                    "lease": claim.lease_until,
                },
            )
            await db.commit()
        return int(getattr(moved, "rowcount", 0) or 0) == 1
