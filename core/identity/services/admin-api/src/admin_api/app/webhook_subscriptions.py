"""Webhook subscriptions (§2.7): the ``/v2/webhooks`` routes and meeting-api's internal read.

    POST   /v2/webhooks                      add a subscriber {url, secret?, events, description?}
    GET    /v2/webhooks                      list; ``secret_last4`` only, never a secret
    PATCH  /v2/webhooks/{id}                 change {url?, events?, active?, description?}
    DELETE /v2/webhooks/{id}                 remove
    POST   /v2/webhooks/{id}/rotate-secret   new secret {secret?}; the old one stays valid 24 h
    POST   /v2/webhooks/{id}/test            send ``webhook.test`` to this subscriber now
    GET    /v2/webhooks/{id}/deliveries      the delivery log, newest first (?limit=&before=)

    GET    /internal/users/{id}/webhook-subscriptions   (internal secret) active subscriptions,
                                                        ciphertext only

The gateway checks the ``webhooks`` scope and sets ``x-user-id`` (the account). Every failure is
the §2.5 body ``{"error": {"code", "message"}}``: a request that fails validation is 400
``invalid_request`` (so is an id in the path that isn't a UUID), a UUID that isn't one of the
account's subscriptions is 404 ``webhook_not_found``, an account with no ``users`` row adding one
is 404 ``account_not_found``, the 21st subscription is 429 ``quota_exceeded`` (no
``Retry-After``), and a database that can't be reached, a missing secret key ring or a failed test
hand-off is 503 ``unavailable``. A constraint violation is a bug and stays a 500.

- A secret the caller omits is generated and returned once, in that response only. A secret the
  caller supplies is never echoed. Secrets are stored sealed by ``SecretBox``; responses and logs
  carry ``secret_last4`` at most.
- ``events: []`` means every event; each listed event must be a ``webhook.v1`` ``EventType``.
- The URL passes ``url_guard`` on every save, in a worker thread under ``URL_CHECK_TIMEOUT_S``: a
  slow resolver never stalls the event loop that also answers ``/internal/validate``, and a check
  that times out refuses the URL.
- Pausing (``active: false``) and deleting cancel the subscription's pending deliveries in the
  same transaction: ``UPDATE webhook_deliveries SET state='cancelled' WHERE subscription_id=:id
  AND state IN ('pending','sending')``.
- Rotation moves the current secret to ``previous_secret_*`` with
  ``previous_secret_expires_at = now + 24 h``; meeting-api signs with both until then.
- The internal read returns ``secret_enc`` / ``previous_secret_enc`` as base64 of the stored bytes
  with their key ids, ``previous_*`` only while unexpired; a row still sealed under a key other than
  the active one is re-sealed under the active key in the same request (compare
  ``secret_box.py``). A row under a key id the ring doesn't hold can't be re-sealed: it is returned
  as stored, without a row lock, and the read logs such rows once.
- ``webhook.test`` is handed to meeting-api's ``POST /internal/webhooks/test`` with
  ``{"user_id", "subscription_id"}`` and the internal secret; meeting-api writes and sends it, and
  replies with the ``event_id`` it queued. The route answers ``{"subscription_id", "event_id"}``.
"""

from __future__ import annotations

import base64
import logging
import os
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional, Protocol

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..schema.models import (
    User,
    WebhookDelivery,
    WebhookDeliveryAttempt,
    WebhookOutbox,
    WebhookSubscription,
)
from .db import get_db
from .secret_box import SecretBox, SecretBoxError, secret_box_from_env
from .url_guard import (
    DEFAULT_PRIVATE_HOST_ALLOWLIST,
    Resolver,
    UrlRefused,
    check_subscription_url_off_loop,
    parse_allowlist,
)

__all__ = [
    "EVENT_TYPES",
    "PREVIOUS_SECRET_TTL",
    "WebhookSettings",
    "WebhookTestSender",
    "WebhookTestUnavailable",
    "HttpWebhookTestSender",
    "WebhookDeps",
    "build_webhook_router",
]

log = logging.getLogger("admin_api.webhooks")

#: ``webhook.v1`` ``EventType`` — the sealed enum in
#: ``core/meetings/contracts/webhook.v1/webhook.schema.json``. admin-api's image is built from this
#: service's folder alone, so the schema isn't readable at runtime; ``test_webhook_subscriptions``
#: fails the moment this set and the sealed enum differ.
EVENT_TYPES = frozenset(
    {
        "meeting.started",
        "meeting.status_change",
        "meeting.completed",
        "meeting.scheduled",
        "meeting.updated",
        "meeting.removed",
        "meeting.waiting_for_room",
        "meeting.not_sent",
        "bot.failed",
        "bot.retry",
        "recording.ready",
        "transcription.ready",
        "export.handed_off",
        "export.failed",
        "webhook.test",
    }
)

PREVIOUS_SECRET_TTL = timedelta(hours=24)
#: The whole URL check on save, DNS included; a check that takes longer refuses the URL.
URL_CHECK_TIMEOUT_S = 5.0
MAX_URL = 2048
MIN_SECRET, MAX_SECRET = 16, 512
MAX_DESCRIPTION = 500
DEFAULT_DELIVERY_LIMIT, MAX_DELIVERY_LIMIT = 50, 200
_PENDING_STATES = ("pending", "sending")
_NOT_FOUND = "no such webhook subscription"
_PROGRAMMING_ERRORS = (TypeError, AttributeError, KeyError, AssertionError, NameError)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class WebhookSettings:
    """``WEBHOOK_MAX_SUBSCRIPTIONS`` and ``WEBHOOK_PRIVATE_HOST_ALLOWLIST`` (config.v1.json)."""

    max_subscriptions: int = 20
    private_host_allowlist: frozenset[str] = field(
        default_factory=lambda: parse_allowlist(DEFAULT_PRIVATE_HOST_ALLOWLIST)
    )

    @classmethod
    def from_env(cls) -> "WebhookSettings":
        """``ValueError`` unless ``WEBHOOK_MAX_SUBSCRIPTIONS`` is a whole number of at least 1."""
        raw = os.getenv("WEBHOOK_MAX_SUBSCRIPTIONS", "20")
        try:
            max_subscriptions = int(raw)
        except ValueError as exc:
            raise ValueError(
                "WEBHOOK_MAX_SUBSCRIPTIONS must be a whole number"
            ) from exc
        if max_subscriptions < 1:
            raise ValueError("WEBHOOK_MAX_SUBSCRIPTIONS must be at least 1")
        return cls(
            max_subscriptions=max_subscriptions,
            private_host_allowlist=parse_allowlist(
                os.getenv(
                    "WEBHOOK_PRIVATE_HOST_ALLOWLIST", DEFAULT_PRIVATE_HOST_ALLOWLIST
                )
            ),
        )


class WebhookTestUnavailable(Exception):
    """meeting-api didn't take the test send."""


class WebhookTestSender(Protocol):
    """Hands a ``webhook.test`` send to meeting-api; returns the event id it queued."""

    async def send_test(self, user_id: int, subscription_id: str) -> str: ...


class HttpWebhookTestSender:
    """``WebhookTestSender`` over meeting-api's ``POST /internal/webhooks/test``."""

    def __init__(
        self,
        base_url: str,
        internal_secret: str,
        *,
        timeout_s: float = 10.0,
        transport: Any = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._internal_secret = internal_secret
        self._timeout_s = timeout_s
        self._transport = transport

    @classmethod
    def from_env(cls) -> "HttpWebhookTestSender":
        return cls(
            os.getenv("MEETING_API_URL", "http://meeting-api:8080"),
            os.environ.get("INTERNAL_API_SECRET", ""),
        )

    async def send_test(self, user_id: int, subscription_id: str) -> str:
        import httpx

        if not self._internal_secret:
            raise WebhookTestUnavailable("INTERNAL_API_SECRET is not configured")
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_s, transport=self._transport
            ) as client:
                resp = await client.post(
                    f"{self._base_url}/internal/webhooks/test",
                    json={"user_id": user_id, "subscription_id": subscription_id},
                    headers={"X-Internal-Secret": self._internal_secret},
                )
        except httpx.HTTPError as exc:
            raise WebhookTestUnavailable(type(exc).__name__) from exc
        if not 200 <= resp.status_code < 300:
            raise WebhookTestUnavailable(f"meeting-api answered {resp.status_code}")
        try:
            body = resp.json()
        except ValueError:
            body = None
        event_id = body.get("event_id") if isinstance(body, dict) else None
        if not isinstance(event_id, str) or not event_id:
            raise WebhookTestUnavailable("meeting-api's reply carries no event_id")
        return event_id


@dataclass
class WebhookDeps:
    """What the webhook routes run on: the secret box (``None`` = no key ring configured), the
    test hand-off port, the settings, the URL guard's resolver and the clock."""

    secret_box: Optional[SecretBox]
    test_sender: WebhookTestSender
    settings: WebhookSettings = field(default_factory=WebhookSettings)
    resolver: Optional[Resolver] = None
    clock: Callable[[], datetime] = _utcnow
    url_check_timeout_s: float = URL_CHECK_TIMEOUT_S

    @classmethod
    def from_env(cls) -> "WebhookDeps":
        """Production wiring. Raises ``KeyRingError`` on a key ring that is set but wrong."""
        return cls(
            secret_box=secret_box_from_env(),
            test_sender=HttpWebhookTestSender.from_env(),
            settings=WebhookSettings.from_env(),
        )


class WebhookError(Exception):
    """A §2.5 refusal."""

    STATUS = {
        "invalid_request": 400,
        "unauthorized": 401,
        "webhook_not_found": 404,
        "account_not_found": 404,
        "quota_exceeded": 429,
        "unavailable": 503,
    }

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = self.STATUS[code]


def _error(exc: WebhookError) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": exc.code, "message": exc.message}},
        status_code=exc.http_status,
    )


def _validation_message(exc: RequestValidationError) -> str:
    """The first failed field and rule, never the value sent."""
    for err in exc.errors():
        loc = ".".join(
            str(p) for p in err.get("loc", ()) if p not in ("body", "query", "path")
        )
        return f"{loc or '<request>'}: {err.get('msg', 'invalid')}"
    return "invalid request"


def _retryable(exc: BaseException) -> bool:
    """The database couldn't be reached. A constraint violation is not retryable: it is a bug,
    and stays a 500."""
    if isinstance(exc, (OSError, TimeoutError)):
        return True
    from sqlalchemy import exc as sa_exc

    if isinstance(exc, sa_exc.DBAPIError) and exc.connection_invalidated:
        return True
    return isinstance(
        exc,
        (
            sa_exc.OperationalError,
            sa_exc.InterfaceError,
            sa_exc.DisconnectionError,
            sa_exc.TimeoutError,
        ),
    )


class _WebhookRoute(APIRoute):
    """Answers every failure of a webhook route with the §2.5 body."""

    def get_route_handler(self) -> Callable[[Request], Awaitable[Response]]:
        handler = super().get_route_handler()

        async def route(request: Request) -> Response:
            try:
                return await handler(request)
            except RequestValidationError as exc:
                return _error(WebhookError("invalid_request", _validation_message(exc)))
            except WebhookError as exc:
                return _error(exc)
            except _PROGRAMMING_ERRORS:
                raise
            except Exception as exc:
                if not _retryable(exc):
                    raise
                log.warning(
                    "webhook route storage unavailable: %s on %s",
                    type(exc).__name__,
                    request.url.path,
                )
                return _error(
                    WebhookError("unavailable", "storage is unavailable; retry")
                )

        return route


def _check_events(events: list[str]) -> list[str]:
    unknown = sorted({e for e in events if e not in EVENT_TYPES})
    if unknown:
        raise ValueError(f"not webhook.v1 event types: {', '.join(unknown)}")
    return sorted(set(events))


class SubscriptionCreate(BaseModel):
    url: str = Field(min_length=1, max_length=MAX_URL)
    secret: Optional[str] = Field(
        default=None, min_length=MIN_SECRET, max_length=MAX_SECRET
    )
    events: list[str]
    description: Optional[str] = Field(default=None, max_length=MAX_DESCRIPTION)

    model_config = {"extra": "forbid"}

    @field_validator("events")
    @classmethod
    def _events(cls, value: list[str]) -> list[str]:
        return _check_events(value)


class SubscriptionPatch(BaseModel):
    url: Optional[str] = Field(default=None, min_length=1, max_length=MAX_URL)
    events: Optional[list[str]] = None
    active: Optional[bool] = None
    description: Optional[str] = Field(default=None, max_length=MAX_DESCRIPTION)

    model_config = {"extra": "forbid"}

    @field_validator("events")
    @classmethod
    def _events(cls, value: Optional[list[str]]) -> Optional[list[str]]:
        return None if value is None else _check_events(value)


class SecretRotate(BaseModel):
    secret: Optional[str] = Field(
        default=None, min_length=MIN_SECRET, max_length=MAX_SECRET
    )

    model_config = {"extra": "forbid"}


def _account(x_user_id: Optional[str]) -> int:
    if not x_user_id:
        raise WebhookError("unauthorized", "missing user identity")
    try:
        return int(x_user_id)
    except ValueError as exc:
        raise WebhookError("unauthorized", "invalid user identity") from exc


def _iso(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _view(sub: WebhookSubscription) -> dict[str, Any]:
    return {
        "id": str(sub.id),
        "url": sub.url,
        "events": list(sub.events or []),
        "active": bool(sub.active),
        "description": sub.description,
        "secret_last4": sub.secret_last4,
        "previous_secret_expires_at": _iso(sub.previous_secret_expires_at),
        "created_at": _iso(sub.created_at),
        "updated_at": _iso(sub.updated_at),
    }


def _b64(value: Optional[bytes]) -> Optional[str]:
    return None if value is None else base64.b64encode(bytes(value)).decode("ascii")


def build_webhook_router(
    deps: WebhookDeps, *, check_internal: Callable[[Request], None]
) -> APIRouter:
    """The webhook routes over ``deps``; ``check_internal`` guards the internal read."""
    router = APIRouter(route_class=_WebhookRoute)

    def _box() -> SecretBox:
        if deps.secret_box is None:
            raise WebhookError(
                "unavailable", "webhook secret encryption is not configured"
            )
        return deps.secret_box

    async def _check_url(url: str) -> None:
        try:
            await check_subscription_url_off_loop(
                url,
                allowlist=deps.settings.private_host_allowlist,
                resolver=deps.resolver,
                timeout_s=deps.url_check_timeout_s,
            )
        except UrlRefused as exc:
            raise WebhookError("invalid_request", f"url: {exc}") from exc

    async def _owned(
        db: AsyncSession,
        user_id: int,
        subscription_id: str,
        *,
        for_update: bool = False,
    ) -> WebhookSubscription:
        try:
            sid = uuid.UUID(subscription_id)
        except ValueError as exc:
            raise WebhookError(
                "invalid_request", "subscription_id: not a UUID"
            ) from exc
        stmt = select(WebhookSubscription).where(
            WebhookSubscription.id == sid, WebhookSubscription.user_id == user_id
        )
        if for_update:
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        sub = (await db.execute(stmt)).scalar_one_or_none()
        if sub is None:
            raise WebhookError("webhook_not_found", _NOT_FOUND)
        return sub

    async def _cancel_pending(db: AsyncSession, subscription_id: uuid.UUID) -> int:
        result = await db.execute(
            update(WebhookDelivery)
            .where(
                WebhookDelivery.subscription_id == subscription_id,
                WebhookDelivery.state.in_(_PENDING_STATES),
            )
            .values(state="cancelled", lease_until=None)
            .execution_options(synchronize_session=False)
        )
        return int(getattr(result, "rowcount", 0) or 0)

    @router.post("/v2/webhooks")
    async def create_subscription(
        body: SubscriptionCreate,
        x_user_id: Optional[str] = Header(default=None),
        db: AsyncSession = Depends(get_db),
    ) -> JSONResponse:
        user_id = _account(x_user_id)
        box = _box()
        await _check_url(body.url)
        generated = body.secret is None
        secret = secrets.token_urlsafe(32) if body.secret is None else body.secret
        # One writer per account at a time, so two adds can't both take the last slot.
        account = await db.execute(
            select(User.id).where(User.id == user_id).with_for_update()
        )
        if account.first() is None:
            await db.rollback()
            raise WebhookError("account_not_found", "no such account")
        count = len(
            (
                await db.execute(
                    select(WebhookSubscription.id).where(
                        WebhookSubscription.user_id == user_id
                    )
                )
            ).all()
        )
        if count >= deps.settings.max_subscriptions:
            await db.rollback()
            raise WebhookError(
                "quota_exceeded",
                f"at most {deps.settings.max_subscriptions} webhook subscriptions per account",
            )
        sealed = box.encrypt(secret)
        now = deps.clock()
        sub = WebhookSubscription(
            id=uuid.uuid4(),
            user_id=user_id,
            url=body.url,
            secret_enc=sealed.ciphertext,
            enc_key_id=sealed.key_id,
            secret_last4=secret[-4:],
            events=body.events,
            active=True,
            description=body.description,
            created_at=now,
            updated_at=now,
        )
        db.add(sub)
        await db.commit()
        log.info("webhook subscription created: id=%s user=%s", sub.id, user_id)
        view = _view(sub)
        if generated:
            view["secret"] = secret
        return JSONResponse(view, status_code=201)

    @router.get("/v2/webhooks")
    async def list_subscriptions(
        x_user_id: Optional[str] = Header(default=None),
        db: AsyncSession = Depends(get_db),
    ) -> JSONResponse:
        user_id = _account(x_user_id)
        rows = (
            (
                await db.execute(
                    select(WebhookSubscription)
                    .where(WebhookSubscription.user_id == user_id)
                    .order_by(WebhookSubscription.created_at, WebhookSubscription.id)
                )
            )
            .scalars()
            .all()
        )
        return JSONResponse({"subscriptions": [_view(sub) for sub in rows]})

    @router.patch("/v2/webhooks/{subscription_id}")
    async def patch_subscription(
        subscription_id: str,
        body: SubscriptionPatch,
        x_user_id: Optional[str] = Header(default=None),
        db: AsyncSession = Depends(get_db),
    ) -> JSONResponse:
        user_id = _account(x_user_id)
        fields = body.model_fields_set
        if not fields:
            raise WebhookError("invalid_request", "<body>: no field to change")
        for name in ("url", "events", "active"):
            if name in fields and getattr(body, name) is None:
                raise WebhookError("invalid_request", f"{name}: may not be null")
        if body.url is not None:
            await _check_url(body.url)
        sub = await _owned(db, user_id, subscription_id, for_update=True)
        if body.url is not None:
            sub.url = body.url
        if body.events is not None:
            sub.events = body.events
        if "description" in fields:
            sub.description = body.description
        cancelled = 0
        if body.active is not None:
            sub.active = body.active
            if body.active is False:
                cancelled = await _cancel_pending(db, sub.id)
        sub.updated_at = deps.clock()
        await db.commit()
        await db.refresh(sub)
        log.info(
            "webhook subscription changed: id=%s user=%s fields=%s cancelled=%d",
            sub.id,
            user_id,
            sorted(fields),
            cancelled,
        )
        return JSONResponse(_view(sub))

    @router.delete("/v2/webhooks/{subscription_id}")
    async def delete_subscription(
        subscription_id: str,
        x_user_id: Optional[str] = Header(default=None),
        db: AsyncSession = Depends(get_db),
    ) -> Response:
        user_id = _account(x_user_id)
        sub = await _owned(db, user_id, subscription_id, for_update=True)
        sid = sub.id
        cancelled = await _cancel_pending(db, sid)
        await db.delete(sub)
        await db.commit()
        log.info(
            "webhook subscription deleted: id=%s user=%s cancelled=%d",
            sid,
            user_id,
            cancelled,
        )
        return Response(status_code=204)

    @router.post("/v2/webhooks/{subscription_id}/rotate-secret")
    async def rotate_secret(
        subscription_id: str,
        body: SecretRotate,
        x_user_id: Optional[str] = Header(default=None),
        db: AsyncSession = Depends(get_db),
    ) -> JSONResponse:
        user_id = _account(x_user_id)
        box = _box()
        sub = await _owned(db, user_id, subscription_id, for_update=True)
        generated = body.secret is None
        secret = secrets.token_urlsafe(32) if body.secret is None else body.secret
        sealed = box.encrypt(secret)
        now = deps.clock()
        sub.previous_secret_enc = sub.secret_enc
        sub.previous_enc_key_id = sub.enc_key_id
        sub.previous_secret_expires_at = now + PREVIOUS_SECRET_TTL
        sub.secret_enc = sealed.ciphertext
        sub.enc_key_id = sealed.key_id
        sub.secret_last4 = secret[-4:]
        sub.updated_at = now
        await db.commit()
        await db.refresh(sub)
        log.info("webhook secret rotated: id=%s user=%s", sub.id, user_id)
        view = _view(sub)
        if generated:
            view["secret"] = secret
        return JSONResponse(view)

    @router.post("/v2/webhooks/{subscription_id}/test")
    async def test_subscription(
        subscription_id: str,
        x_user_id: Optional[str] = Header(default=None),
        db: AsyncSession = Depends(get_db),
    ) -> JSONResponse:
        user_id = _account(x_user_id)
        sub = await _owned(db, user_id, subscription_id)
        sid = str(sub.id)
        await db.rollback()
        try:
            event_id = await deps.test_sender.send_test(user_id, sid)
        except WebhookTestUnavailable as exc:
            log.warning(
                "webhook test hand-off failed: id=%s user=%s (%s)", sid, user_id, exc
            )
            raise WebhookError(
                "unavailable", "the test send could not be queued; retry"
            ) from exc
        log.info("webhook test queued: id=%s user=%s", sid, user_id)
        return JSONResponse(
            {"subscription_id": sid, "event_id": event_id}, status_code=202
        )

    @router.get("/v2/webhooks/{subscription_id}/deliveries")
    async def list_deliveries(
        subscription_id: str,
        limit: int = Query(default=DEFAULT_DELIVERY_LIMIT, ge=1, le=MAX_DELIVERY_LIMIT),
        before: Optional[int] = Query(default=None, ge=1),
        x_user_id: Optional[str] = Header(default=None),
        db: AsyncSession = Depends(get_db),
    ) -> JSONResponse:
        user_id = _account(x_user_id)
        sub = await _owned(db, user_id, subscription_id)
        stmt = (
            select(WebhookDelivery, WebhookOutbox.event_type)
            .join(WebhookOutbox, WebhookOutbox.event_id == WebhookDelivery.event_id)
            .where(
                WebhookDelivery.subscription_id == sub.id,
                WebhookDelivery.user_id == user_id,
            )
            .order_by(WebhookDelivery.id.desc())
            .limit(limit + 1)
        )
        if before is not None:
            stmt = stmt.where(WebhookDelivery.id < before)
        rows = (await db.execute(stmt)).all()
        page, more = rows[:limit], len(rows) > limit
        attempts: dict[int, list[dict[str, Any]]] = {}
        if page:
            ids = [delivery.id for delivery, _ in page]
            for attempt in (
                (
                    await db.execute(
                        select(WebhookDeliveryAttempt)
                        .where(WebhookDeliveryAttempt.delivery_id.in_(ids))
                        .order_by(
                            WebhookDeliveryAttempt.delivery_id,
                            WebhookDeliveryAttempt.attempt,
                        )
                    )
                )
                .scalars()
                .all()
            ):
                attempts.setdefault(attempt.delivery_id, []).append(
                    {
                        "attempt": attempt.attempt,
                        "outcome": attempt.outcome,
                        "status_code": attempt.status_code,
                        "error": attempt.error,
                        "duration_ms": attempt.duration_ms,
                        "at": _iso(attempt.created_at),
                    }
                )
        deliveries = [
            {
                "id": delivery.id,
                "event_id": delivery.event_id,
                "event_type": event_type,
                "state": delivery.state,
                "attempts": delivery.attempts,
                "next_attempt_at": _iso(delivery.next_attempt_at),
                "last_status_code": delivery.last_status_code,
                "last_error": delivery.last_error,
                "created_at": _iso(delivery.created_at),
                "updated_at": _iso(delivery.updated_at),
                "attempt_log": attempts.get(delivery.id, []),
            }
            for delivery, event_type in page
        ]
        return JSONResponse(
            {
                "deliveries": deliveries,
                "next_before": page[-1][0].id if more else None,
            }
        )

    @router.get(
        "/internal/users/{user_id}/webhook-subscriptions", include_in_schema=False
    )
    async def internal_subscriptions(
        user_id: int, request: Request, db: AsyncSession = Depends(get_db)
    ) -> JSONResponse:
        check_internal(request)
        box = _box()
        now = deps.clock()

        def _previous_live(sub: WebhookSubscription) -> bool:
            expires = sub.previous_secret_expires_at
            if sub.previous_secret_enc is None or expires is None:
                return False
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            return expires > now

        def _rewrappable(key_id: Optional[str]) -> bool:
            return (
                key_id is not None
                and key_id != box.active_key_id
                and box.has_key(key_id)
            )

        def _stale(sub: WebhookSubscription) -> bool:
            return _rewrappable(sub.enc_key_id) or (
                _previous_live(sub) and _rewrappable(sub.previous_enc_key_id)
            )

        def _unknown_keys(sub: WebhookSubscription) -> list[str]:
            ids = [sub.enc_key_id]
            if _previous_live(sub):
                ids.append(str(sub.previous_enc_key_id))
            return [key_id for key_id in ids if not box.has_key(key_id)]

        query = (
            select(WebhookSubscription)
            .where(
                WebhookSubscription.user_id == user_id,
                WebhookSubscription.active.is_(True),
            )
            .order_by(WebhookSubscription.created_at, WebhookSubscription.id)
        )
        rows = list((await db.execute(query)).scalars().all())
        if any(_stale(sub) for sub in rows):
            # Re-read under row locks so a concurrent rotation is never overwritten.
            rows = list(
                (
                    await db.execute(
                        query.with_for_update().execution_options(
                            populate_existing=True
                        )
                    )
                )
                .scalars()
                .all()
            )
            rewrapped = 0
            for sub in rows:
                try:
                    if _rewrappable(sub.enc_key_id):
                        current = box.encrypt(
                            box.decrypt(bytes(sub.secret_enc), sub.enc_key_id)
                        )
                        sub.secret_enc, sub.enc_key_id = (
                            current.ciphertext,
                            current.key_id,
                        )
                        rewrapped += 1
                    if _previous_live(sub) and _rewrappable(sub.previous_enc_key_id):
                        previous = box.encrypt(
                            box.decrypt(
                                bytes(sub.previous_secret_enc),
                                str(sub.previous_enc_key_id),
                            )
                        )
                        sub.previous_secret_enc = previous.ciphertext
                        sub.previous_enc_key_id = previous.key_id
                        rewrapped += 1
                except SecretBoxError as exc:
                    log.error(
                        "webhook secret could not be re-sealed: id=%s user=%s (%s)",
                        sub.id,
                        user_id,
                        exc,
                    )
            await db.commit()
            if rewrapped:
                log.info(
                    "webhook secrets re-sealed under the active key: user=%s count=%d",
                    user_id,
                    rewrapped,
                )
        unknown = [
            f"{sub.id}:{key_id}" for sub in rows for key_id in _unknown_keys(sub)
        ]
        if unknown:
            log.warning(
                "webhook secrets under a key id the ring doesn't hold, returned as stored: "
                "user=%s rows=%s",
                user_id,
                ",".join(unknown),
            )
        payload = []
        for sub in rows:
            live = _previous_live(sub)
            payload.append(
                {
                    "id": str(sub.id),
                    "url": sub.url,
                    "events": list(sub.events or []),
                    "secret_enc": _b64(sub.secret_enc),
                    "enc_key_id": sub.enc_key_id,
                    "previous_secret_enc": (
                        _b64(sub.previous_secret_enc) if live else None
                    ),
                    "previous_enc_key_id": sub.previous_enc_key_id if live else None,
                    "previous_secret_expires_at": (
                        _iso(sub.previous_secret_expires_at) if live else None
                    ),
                }
            )
        return JSONResponse({"user_id": user_id, "subscriptions": payload})

    return router
