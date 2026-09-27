"""The gateway's signature, for admin-api's route tests (design §1.10).

admin-api believes ``x-user-id`` only with the gateway's ``x-gateway-signature``. A route test
that sends ``x-user-id`` stands for a request the gateway forwarded, so it reaches the app through
``via_gateway(app)``, which signs such a request the way the gateway does: over its method, its
path (``scope["path"]``) and the ``x-user-id`` it carries, with ``SECRET``. A request that already
carries a signature, or carries no ``x-user-id``, passes through untouched, which is how the guard's
own tests send unsigned and forged requests.

The signing rule is the shared one (``core/gateway/contracts/gateway-identity``); the suite's
``GATEWAY_IDENTITY_SECRET`` is ``SECRET`` (``conftest.py``).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from pathlib import Path
from typing import Any, Optional

SECRET = "test-gateway-identity-secret-admin-api"
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
    t: Optional[int] = None,
    secret: str = SECRET,
) -> str:
    """The ``x-gateway-signature`` value the gateway would send."""
    t = int(time.time()) if t is None else t
    message = f"{t}.{user_id}.{method.upper()}.{path}".encode()
    return f"t={t},v1={hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()}"


def signed_headers(
    user_id: Any, method: str, path: str, **extra: str
) -> dict[str, str]:
    """``x-user-id`` with its signature, plus any extra headers."""
    uid = str(user_id)
    return {"x-user-id": uid, SIGNATURE_HEADER: signature(uid, method, path), **extra}


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
                uid = next(
                    value for key, value in headers if key == b"x-user-id"
                ).decode("latin-1")
                sig = signature(uid, scope["method"], scope["path"])
                scope = {
                    **scope,
                    "headers": [*headers, (SIGNATURE_HEADER.encode(), sig.encode())],
                }
        await self.app(scope, receive, send)


def via_gateway(app: Any) -> Any:
    """``app`` behind a stand-in gateway that signs each request carrying ``x-user-id``."""
    return _ViaGateway(app)
