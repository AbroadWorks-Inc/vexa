"""Only the gateway can say who is calling (design §1.10).

Client routes take the caller from ``x-user-id``. The gateway sets that header after it has
checked the caller's key, and signs it with the active key of the ``GATEWAY_IDENTITY_KEYS`` ring,
naming that key's id (§6.9 F-E)::

    x-gateway-signature: kid=<key id>,t=<unix seconds>,v2=<hex HMAC-SHA256(key, message)>
    message = "v2\n<kid>\n<t>\n<user_id>\n<scopes>\n<limits>\n<METHOD>\n<body_sha256>\n<query>\n<path>"

``IdentityGuard`` answers 401 before any client route runs unless the request carries exactly one
``x-user-id``, at most one ``x-user-scopes`` and one ``x-user-limits``, and exactly one signature
that

- parses as ``kid=<1-64 of A-Z a-z 0-9 . _ ->,t=<digits>,v2=<64 lower-case hex digits>``,
- names a ``kid`` in this service's ring,
- was made at most ``MAX_SKEW_S`` seconds before or after this service's clock, and
- matches, in constant time, the HMAC under that kid's key over the kid, that ``t``, that
  ``x-user-id``, the ``x-user-scopes`` and ``x-user-limits`` values (empty when absent), the
  request's method, the SHA-256 of its body, its raw query (``scope["query_string"]``) and its
  path (``scope["path"]``: percent-decoded).

The ring is ``GATEWAY_IDENTITY_KEYS``, in the webhook secret encryption ring's format: a JSON object
``{"<kid>": "<exactly 32 bytes, standard base64>"}``. The HMAC key is a kid's 32 raw bytes. Unset or
malformed, every client request is refused, and the rejection log says why (naming the setting,
never a key). The guard reads the whole body to hash it, only once the header has parsed, names a
known kid and is fresh, and hands the route the same bytes.

Routes that don't take a caller from the gateway are exempt, and they are listed here, not inferred:

- ``/internal/*``: service-to-service routes, each checking the internal secret;
- ``/bots/internal/callback/lifecycle`` and ``/runtime/callback``: the bot's and the runtime's
  callbacks, checked with the internal secret and the per-bot callback token (``callback_auth.py``);
- ``/health*`` and ``/metrics``: probes and scrapes.

The rule is pinned by ``core/gateway/contracts/gateway-identity/signature.vectors.json``, which the
gateway's tests and admin-api's tests read too.
"""

from __future__ import annotations

import functools
import hashlib
import hmac
import json
import os
import re
import time
from typing import Any, Awaitable, Callable, Mapping, Optional

from . import key_ring
from .key_ring import KeyRingError
from .obs import log_event

SIGNATURE_HEADER = "x-gateway-signature"
USER_HEADER = "x-user-id"
SCOPES_HEADER = "x-user-scopes"
LIMITS_HEADER = "x-user-limits"
MAX_SKEW_S = 60
KEYS_ENV = "GATEWAY_IDENTITY_KEYS"
KID = re.compile(r"[A-Za-z0-9._-]{1,64}")

EXEMPT_PATHS = frozenset(
    {"/bots/internal/callback/lifecycle", "/runtime/callback", "/metrics"}
)
EXEMPT_PREFIXES = ("/internal/", "/health")

_SIGNATURE = re.compile(r"kid=([A-Za-z0-9._-]{1,64}),t=([0-9]{1,12}),v2=([0-9a-f]{64})")


def is_exempt(path: str) -> bool:
    """True for a route that doesn't take its caller from the gateway."""
    return path in EXEMPT_PATHS or path.startswith(EXEMPT_PREFIXES)


def parse_ring(keys_json: str) -> dict[str, bytes]:
    """The ring in ``keys_json``: kid → 32 raw key bytes (``key_ring.parse``). The kid travels
    in a header, so it is narrower than the webhook ring's."""
    return key_ring.parse(
        keys_json,
        env_name=KEYS_ENV,
        kid_ok=lambda kid: KID.fullmatch(kid) is not None,
        kid_rule="1-64 characters of A-Z a-z 0-9 . _ -",
    )


@functools.lru_cache(maxsize=4)
def _ring(keys_json: str) -> Mapping[str, bytes]:
    return parse_ring(keys_json)


def identity_ring() -> Mapping[str, bytes]:
    """The ring from ``GATEWAY_IDENTITY_KEYS``: empty when unset, ``KeyRingError`` when wrong."""
    keys_json = (os.getenv(KEYS_ENV) or "").strip()
    return _ring(keys_json) if keys_json else {}


def precheck(
    ring: Mapping[str, bytes],
    user_id: Optional[str],
    header: Optional[str],
    now: float,
) -> Optional[str]:
    """Why ``header`` can't vouch for anything, found without the body; ``None`` if it may."""
    if not ring:
        return "unconfigured"
    if not header or user_id is None:
        return "missing"
    match = _SIGNATURE.fullmatch(header)
    if match is None:
        return "malformed"
    if match.group(1) not in ring:
        return "unknown_key"
    if abs(now - int(match.group(2))) > MAX_SKEW_S:
        return "expired"
    return None


def verify_signature(
    ring: Mapping[str, bytes],
    user_id: Optional[str],
    scopes: str,
    limits: str,
    header: Optional[str],
    method: str,
    path: str,
    query: str,
    body: bytes,
    now: float,
) -> Optional[str]:
    """``None`` when ``header`` vouches for ``user_id`` on this request, else why it doesn't."""
    reason = precheck(ring, user_id, header, now)
    if reason is not None:
        return reason
    match = _SIGNATURE.fullmatch(header or "")
    if match is None or user_id is None:
        return "malformed"
    kid = match.group(1)
    fields = [
        "v2",
        kid,
        match.group(2),
        user_id,
        scopes,
        limits,
        method.upper(),
        hashlib.sha256(body).hexdigest(),
        query,
        path,
    ]
    message = "\n".join(fields).encode("utf-8")
    expected = hmac.new(ring[kid], message, hashlib.sha256).hexdigest()
    return None if hmac.compare_digest(match.group(3), expected) else "mismatch"


Receive = Callable[[], Awaitable[dict[str, Any]]]


async def _read_body(receive: Receive) -> Optional[bytes]:
    """The whole request body, or ``None`` when the client went away before sending it."""
    chunks = []
    while True:
        message = await receive()
        if message["type"] != "http.request":
            return None
        chunks.append(message.get("body", b""))
        if not message.get("more_body", False):
            return b"".join(chunks)


def _replay(body: bytes, receive: Receive) -> Receive:
    """A ``receive`` that hands the route ``body`` once, then the client's own messages."""
    sent = False

    async def replayed() -> dict[str, Any]:
        nonlocal sent
        if sent:
            return await receive()
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return replayed


def _single(headers: list, name: bytes) -> Optional[str]:
    """The header's value when it appears exactly once, else ``None``."""
    values = [value for key, value in headers if key.lower() == name]
    if len(values) != 1:
        return None
    return values[0].decode("latin-1")


def _at_most_one(headers: list, name: bytes) -> Optional[str]:
    """The header's value, ``""`` when it is absent, or ``None`` when it appears more than once."""
    values = [value for key, value in headers if key.lower() == name]
    if len(values) > 1:
        return None
    return values[0].decode("latin-1") if values else ""


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
        fault = None
        try:
            ring = identity_ring()
        except KeyRingError as e:
            ring, fault = {}, str(e)
        user_id = _single(headers, USER_HEADER.encode())
        scopes = _at_most_one(headers, SCOPES_HEADER.encode())
        limits = _at_most_one(headers, LIMITS_HEADER.encode())
        header = _single(headers, SIGNATURE_HEADER.encode())
        now = self.clock()
        reason = "ring_invalid" if fault else precheck(ring, user_id, header, now)
        if reason is None and (scopes is None or limits is None):
            reason = "duplicated"
        if reason is None and scopes is not None and limits is not None:
            body = await _read_body(receive)
            if body is None:
                return
            reason = verify_signature(
                ring,
                user_id,
                scopes,
                limits,
                header,
                scope["method"],
                path,
                scope.get("query_string", b"").decode("latin-1"),
                body,
                now,
            )
            if reason is None:
                await self.app(scope, _replay(body, receive), send)
                return
        log_event(
            "identity_rejected",
            audience="system",
            level="warning",
            span="identity",
            fields={
                "reason": reason,
                "method": scope["method"],
                "path": path,
                **({"fault": fault} if fault else {}),
            },
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
