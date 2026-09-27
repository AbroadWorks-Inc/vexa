"""Only the gateway can say who is calling (design §1.10).

Client routes take the caller from ``x-user-id``. The gateway sets that header after it has
checked the caller's key, and signs it with ``GATEWAY_IDENTITY_SECRET``::

    x-gateway-signature: t=<unix seconds>,v1=<hex HMAC-SHA256(secret, "<t>.<user_id>.<METHOD>.<path>")>

``IdentityGuard`` answers 401 before any client route runs unless the request carries exactly one
``x-user-id`` and exactly one signature that

- parses as ``t=<digits>,v1=<64 lower-case hex digits>``,
- was made at most ``MAX_SKEW_S`` seconds before or after this service's clock, and
- matches, in constant time, the HMAC over that ``t``, that ``x-user-id``, the request's method and
  its path (``scope["path"]``: percent-decoded, no query string).

Without ``GATEWAY_IDENTITY_SECRET`` every client request is refused.

Routes that don't take a caller from the gateway are exempt, and they are listed here, not inferred:

- ``/internal/*``: service-to-service routes, each checking the internal secret;
- ``/bots/internal/callback/lifecycle`` and ``/runtime/callback``: the bot's and the runtime's
  callbacks, checked with the internal secret and the per-bot callback token (``callback_auth.py``);
- ``/health*`` and ``/metrics``: probes and scrapes.

The rule is pinned by ``core/gateway/contracts/gateway-identity/signature.vectors.json``, which the
gateway's tests and admin-api's tests read too.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
from typing import Callable, Optional

from .obs import log_event

SIGNATURE_HEADER = "x-gateway-signature"
USER_HEADER = "x-user-id"
MAX_SKEW_S = 60

EXEMPT_PATHS = frozenset(
    {"/bots/internal/callback/lifecycle", "/runtime/callback", "/metrics"}
)
EXEMPT_PREFIXES = ("/internal/", "/health")

_SIGNATURE = re.compile(r"t=([0-9]{1,12}),v1=([0-9a-f]{64})")


def is_exempt(path: str) -> bool:
    """True for a route that doesn't take its caller from the gateway."""
    return path in EXEMPT_PATHS or path.startswith(EXEMPT_PREFIXES)


def identity_secret() -> str:
    """``GATEWAY_IDENTITY_SECRET``, or ``""`` when it is not configured."""
    return (os.getenv("GATEWAY_IDENTITY_SECRET") or "").strip()


def verify_signature(
    secret: str,
    user_id: Optional[str],
    header: Optional[str],
    method: str,
    path: str,
    now: float,
) -> Optional[str]:
    """``None`` when ``header`` vouches for ``user_id`` on this request, else why it doesn't."""
    if not secret:
        return "unconfigured"
    if not header or user_id is None:
        return "missing"
    match = _SIGNATURE.fullmatch(header)
    if match is None:
        return "malformed"
    t = int(match.group(1))
    if abs(now - t) > MAX_SKEW_S:
        return "expired"
    message = f"{t}.{user_id}.{method.upper()}.{path}".encode("utf-8")
    expected = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(match.group(2), expected):
        return "mismatch"
    return None


def _single(headers: list, name: bytes) -> Optional[str]:
    """The header's value when it appears exactly once, else ``None``."""
    values = [value for key, value in headers if key.lower() == name]
    if len(values) != 1:
        return None
    return values[0].decode("latin-1")


def _unauthorized(path: str) -> bytes:
    message = "the caller's identity must come from the gateway"
    if path.startswith("/v2/"):
        return json.dumps(
            {"error": {"code": "unauthorized", "message": message}}
        ).encode()
    return json.dumps({"detail": message}).encode()


class IdentityGuard:
    """ASGI middleware: a client request without a valid gateway signature gets 401."""

    def __init__(self, app, clock: Callable[[], float] = time.time) -> None:
        self.app = app
        self.clock = clock

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or is_exempt(scope["path"]):
            await self.app(scope, receive, send)
            return
        headers = scope.get("headers") or []
        path = scope["path"]
        reason = verify_signature(
            identity_secret(),
            _single(headers, USER_HEADER.encode()),
            _single(headers, SIGNATURE_HEADER.encode()),
            scope["method"],
            path,
            self.clock(),
        )
        if reason is None:
            await self.app(scope, receive, send)
            return
        log_event(
            "identity_rejected",
            audience="system",
            level="warning",
            span="identity",
            fields={"reason": reason, "method": scope["method"], "path": path},
        )
        body = _unauthorized(path)
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
