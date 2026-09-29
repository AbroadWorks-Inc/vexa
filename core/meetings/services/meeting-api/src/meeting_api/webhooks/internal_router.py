"""``POST /internal/webhooks/test`` — admin-api's hand-off of ``POST /v2/webhooks/{id}/test``
(§1.8, §2.7).

admin-api calls it with ``X-Internal-Secret`` and ``{"user_id", "subscription_id"}``; the route
hands the write to a ``WebhookTests`` port (``intake.outbox.PostgresWebhookTests`` in production),
which queues the ``webhook.test`` event and its one delivery, and answers 202 ``{"event_id"}``. The
gateway routes nothing here.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..callback_auth import INTERNAL_SECRET_HEADER, internal_secret, secret_matches
from ..intake import error_body
from ..intake.outbox import SubscriptionNotFound, WebhookTests

__all__ = ["build_webhook_test_router"]


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(error_body(code, message), status_code=status)


def build_webhook_test_router(tests: Optional[WebhookTests]) -> APIRouter:
    """``POST /internal/webhooks/test`` ``{user_id, subscription_id}`` → 202 ``{event_id}``.

    ``X-Internal-Secret`` must equal ``INTERNAL_API_SECRET`` (unset → 503, wrong → 403). No
    ``tests`` port (no Postgres) → 503. A body that isn't ``{user_id: int, subscription_id: str}``
    → 400; a subscription that isn't the account's → 404."""
    router = APIRouter()

    @router.post("/internal/webhooks/test", include_in_schema=False)
    async def queue_webhook_test(request: Request) -> JSONResponse:
        secret = internal_secret()
        if not secret:
            return _error(503, "unavailable", "INTERNAL_API_SECRET is not configured")
        if not secret_matches(request.headers.get(INTERNAL_SECRET_HEADER), secret):
            return _error(403, "forbidden", "invalid internal secret")
        if tests is None:
            return _error(503, "unavailable", "webhook tests need Postgres")
        try:
            body = await request.json()
        except ValueError:
            body = None
        user_id = body.get("user_id") if isinstance(body, dict) else None
        subscription_id = (
            body.get("subscription_id") if isinstance(body, dict) else None
        )
        if (
            not isinstance(user_id, int)
            or isinstance(user_id, bool)
            or not isinstance(subscription_id, str)
            or not subscription_id
        ):
            return _error(
                400,
                "invalid_request",
                "the body must be {user_id: integer, subscription_id: string}",
            )
        try:
            event_id = await tests.queue_test(user_id, subscription_id)
        except SubscriptionNotFound:
            return _error(
                404, "webhook_not_found", "no such subscription for this account"
            )
        return JSONResponse({"event_id": event_id}, status_code=202)

    return router
