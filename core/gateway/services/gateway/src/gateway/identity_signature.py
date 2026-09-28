"""The identity signature the gateway puts on every request it forwards (design §1.10, §6.9 F-E).

The gateway resolves the caller's key to a user and forwards ``x-user-id``. meeting-api and
admin-api believe that header only when it arrives with a fresh signature made with
``GATEWAY_IDENTITY_SECRET``, which only the gateway holds::

    x-gateway-signature: t=<unix seconds>,v2=<hex HMAC-SHA256(secret, message)>
    message = "v2\\n<t>\\n<user_id>\\n<METHOD>\\n<body_sha256>\\n<query>\\n<path>"

- ``user_id`` is the exact ``x-user-id`` value forwarded.
- ``METHOD`` is the forwarded method, upper-case.
- ``body_sha256`` is the lower-case hex SHA-256 of the exact body bytes forwarded (the empty
  string's digest for an empty body).
- ``query`` is the raw query string of the URL httpx sends, without the ``?``: the downstream URL
  with the forwarded params merged in, as ``httpx`` merges them. The receiving service reads the
  same string as the ASGI ``scope["query_string"]``.
- ``path`` is the path of that URL, percent-decoded (``httpx.URL.path``); the receiving service
  reads it as ``scope["path"]``. It is the last field because it is the only one that can contain
  a line feed.
- The verifiers accept a signature made at most 60 s before or after their own clock, under
  ``GATEWAY_IDENTITY_SECRET`` or their optional previous key. The gateway signs with the current
  key only.

The rule is pinned as data in ``core/gateway/contracts/gateway-identity/signature.vectors.json``,
which this package's tests and both verifiers' tests read; the services share no code.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from typing import Mapping, Optional

SIGNATURE_HEADER = "x-gateway-signature"
VERSION = "v2"


def identity_secret() -> str:
    """``GATEWAY_IDENTITY_SECRET``, or ``""`` when it is not configured."""
    return (os.getenv("GATEWAY_IDENTITY_SECRET") or "").strip()


def signed_target(
    url: str, params: Optional[Mapping[str, str]] = None
) -> tuple[str, str]:
    """The ``(path, query)`` the signature covers for a request forwarded to ``url`` with
    ``params``: those of the URL httpx sends."""
    import httpx

    target = httpx.URL(url)
    if params:
        target = target.copy_merge_params(params)
    return target.path, target.query.decode("ascii")


def sign(
    secret: str,
    user_id: str,
    method: str,
    path: str,
    query: str,
    body: bytes,
    t: int,
) -> str:
    """The ``x-gateway-signature`` value for one forwarded request made at unix time ``t``."""
    fields = [
        VERSION,
        str(t),
        user_id,
        method.upper(),
        hashlib.sha256(body).hexdigest(),
        query,
        path,
    ]
    message = "\n".join(fields).encode("utf-8")
    mac = hmac.new(secret.encode("utf-8"), message, hashlib.sha256)
    return f"t={t},{VERSION}={mac.hexdigest()}"


def sign_now(
    secret: str,
    user_id: str,
    method: str,
    url: str,
    params: Optional[Mapping[str, str]],
    body: bytes,
) -> str:
    """Sign a request forwarded now to ``url`` with ``params`` and ``body``."""
    path, query = signed_target(url, params)
    return sign(secret, user_id, method, path, query, body, int(time.time()))
