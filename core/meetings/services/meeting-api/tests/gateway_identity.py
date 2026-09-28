"""The gateway's signature, for meeting-api's route tests (design §1.10, §6.9 F-E).

meeting-api believes ``x-user-id`` only with the gateway's ``x-gateway-signature``. A route test
that sends ``x-user-id`` stands for a request the gateway forwarded, so it reaches the app through
``via_gateway(app)``, which signs such a request the way the gateway does: over the ``x-user-id``,
``x-user-scopes`` and ``x-user-limits`` it carries (an absent header is an empty field), its
method, its path (``scope["path"]``), its raw query (``scope["query_string"]``) and the SHA-256 of
its body, with the suite's ring key ``KEY`` named ``KID``. A request that already carries a signature, or
carries no ``x-user-id``, passes through untouched, which is how the guard's own tests send
unsigned and forged requests.

The signing rule is the shared one (``core/gateway/contracts/gateway-identity``); the suite's
``GATEWAY_IDENTITY_KEYS`` is ``RING`` (``conftest.py``).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from pathlib import Path
from typing import Any, Optional

KID = "gw-meeting-api-test"
KEY = b"test-gateway-identity-meeting-ap"
RING = json.dumps({KID: base64.b64encode(KEY).decode()})
SIGNATURE_HEADER = "x-gateway-signature"


def load_vectors() -> dict[str, Any]:
    rel = Path("gateway") / "contracts" / "gateway-identity" / "signature.vectors.json"
    for parent in Path(__file__).resolve().parents:
        if (parent / rel).is_file():
            return json.loads((parent / rel).read_text())
    raise FileNotFoundError(str(rel))


def signature(
    user_id: str,
    method: str,
    path: str,
    *,
    scopes: str = "",
    limits: str = "",
    query: str = "",
    body: bytes = b"",
    t: Optional[int] = None,
    kid: str = KID,
    key: bytes = KEY,
) -> str:
    """The ``x-gateway-signature`` value the gateway would send."""
    t = int(time.time()) if t is None else t
    fields = [
        "v2",
        kid,
        str(t),
        user_id,
        scopes,
        limits,
        method.upper(),
        hashlib.sha256(body).hexdigest(),
        query,
        path,
    ]
    message = "\n".join(fields).encode()
    return f"kid={kid},t={t},v2={hmac.new(key, message, hashlib.sha256).hexdigest()}"


def signed_headers(
    user_id: Any,
    method: str,
    path: str,
    *,
    scopes: Optional[str] = None,
    limits: Optional[str] = None,
    query: str = "",
    body: bytes = b"",
    **extra: str,
) -> dict[str, str]:
    """``x-user-id`` with its signature, plus any extra headers. ``scopes`` and ``limits``, when
    given, are sent as ``x-user-scopes`` and ``x-user-limits`` and signed."""
    uid = str(user_id)
    sig = signature(
        uid,
        method,
        path,
        scopes=scopes or "",
        limits=limits or "",
        query=query,
        body=body,
    )
    headers = {"x-user-id": uid, SIGNATURE_HEADER: sig}
    if scopes is not None:
        headers["x-user-scopes"] = scopes
    if limits is not None:
        headers["x-user-limits"] = limits
    return {**headers, **extra}


class _ViaGateway:
    def __init__(self, app: Any) -> None:
        self.app = app

    def __getattr__(self, name: str) -> Any:
        return getattr(self.app, name)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = list(scope.get("headers") or [])
            names = [key for key, _ in headers]
            if (
                names.count(b"x-user-id") == 1
                and SIGNATURE_HEADER.encode() not in names
            ):
                chunks = []
                while True:
                    message = await receive()
                    chunks.append(message.get("body", b""))
                    if not message.get("more_body", False):
                        break
                body = b"".join(chunks)
                uid = _first(headers, b"x-user-id")
                sig = signature(
                    uid,
                    scope["method"],
                    scope["path"],
                    scopes=_first(headers, b"x-user-scopes"),
                    limits=_first(headers, b"x-user-limits"),
                    query=scope.get("query_string", b"").decode("latin-1"),
                    body=body,
                )
                scope = {
                    **scope,
                    "headers": [*headers, (SIGNATURE_HEADER.encode(), sig.encode())],
                }
                receive = _replay(body, receive)
        await self.app(scope, receive, send)


def _first(headers: list, name: bytes) -> str:
    """The header's first value, or ``""`` when it is absent."""
    return next((value.decode("latin-1") for key, value in headers if key == name), "")


def _replay(body: bytes, receive: Any) -> Any:
    sent = False

    async def replayed() -> dict[str, Any]:
        nonlocal sent
        if sent:
            return await receive()
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return replayed


def via_gateway(app: Any) -> Any:
    """``app`` behind a stand-in gateway that signs each request carrying ``x-user-id``."""
    return _ViaGateway(app)
