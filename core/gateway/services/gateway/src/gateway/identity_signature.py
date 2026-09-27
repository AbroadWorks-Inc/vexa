"""The identity signature the gateway puts on every request it forwards (design §1.10).

The gateway resolves the caller's key to a user and forwards ``x-user-id``. meeting-api and
admin-api believe that header only when it arrives with a fresh signature made with
``GATEWAY_IDENTITY_SECRET``, which only the gateway holds::

    x-gateway-signature: t=<unix seconds>,v1=<hex HMAC-SHA256(secret, "<t>.<user_id>.<METHOD>.<path>")>

- ``user_id`` is the exact ``x-user-id`` value forwarded.
- ``METHOD`` is the forwarded method, upper-case.
- ``path`` is the path of the URL the request is forwarded to, percent-decoded and without the
  query string: ``httpx.URL(url).path``. The receiving service reads the same string as the ASGI
  ``scope["path"]``.
- The verifiers accept a signature made at most 60 s before or after their own clock.

The rule is pinned as data in ``core/gateway/contracts/gateway-identity/signature.vectors.json``,
which this package's tests and both verifiers' tests read; the services share no code.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time

SIGNATURE_HEADER = "x-gateway-signature"


def identity_secret() -> str:
    """``GATEWAY_IDENTITY_SECRET``, or ``""`` when it is not configured."""
    return (os.getenv("GATEWAY_IDENTITY_SECRET") or "").strip()


def signed_path(url: str) -> str:
    """The path the signature covers for a request forwarded to ``url``."""
    import httpx

    return httpx.URL(url).path


def sign(secret: str, user_id: str, method: str, path: str, t: int) -> str:
    """The ``x-gateway-signature`` value for one forwarded request made at unix time ``t``."""
    message = f"{t}.{user_id}.{method.upper()}.{path}"
    mac = hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256)
    return f"t={t},v1={mac.hexdigest()}"


def sign_now(secret: str, user_id: str, method: str, url: str) -> str:
    """Sign a request forwarded now to ``url``."""
    return sign(secret, user_id, method, signed_path(url), int(time.time()))
