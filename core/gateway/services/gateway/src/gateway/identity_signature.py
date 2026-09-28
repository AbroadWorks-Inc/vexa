"""The identity signature the gateway puts on every request it forwards (design §1.10, §6.9 F-E).

The gateway resolves the caller's key to a user and forwards ``x-user-id``. meeting-api and
admin-api believe that header only when it arrives with a fresh signature made with a key of the
``GATEWAY_IDENTITY_KEYS`` ring, which only the gateway and they hold::

    x-gateway-signature: kid=<key id>,t=<unix seconds>,v2=<hex HMAC-SHA256(key, message)>
    message = "v2\\n<kid>\\n<t>\\n<user_id>\\n<scopes>\\n<limits>\\n<METHOD>\\n<body_sha256>\\n<query>\\n<path>"

- ``kid`` names the ring key the signature is made with: ``GATEWAY_IDENTITY_ACTIVE_KEY``. The HMAC
  key is that key's 32 raw bytes.
- ``user_id`` is the exact ``x-user-id`` value forwarded.
- ``scopes`` and ``limits`` are the exact ``x-user-scopes`` and ``x-user-limits`` values forwarded,
  empty when the header isn't forwarded.
- ``METHOD`` is the forwarded method, upper-case.
- ``body_sha256`` is the lower-case hex SHA-256 of the exact body bytes forwarded (the empty
  string's digest for an empty body).
- ``query`` is the raw query string of the URL httpx sends, without the ``?``: the downstream URL
  with the forwarded params merged in, as ``httpx`` merges them. The receiving service reads the
  same string as the ASGI ``scope["query_string"]``.
- ``path`` is the path of that URL, percent-decoded (``httpx.URL.path``); the receiving service
  reads it as ``scope["path"]``. It is the last field because it is the only one that can contain
  a line feed.
- The verifiers look ``kid`` up in their own ring and accept a signature made at most 60 s before
  or after their own clock.

The ring has the webhook secret encryption ring's format: ``GATEWAY_IDENTITY_KEYS`` is a JSON object
``{"<kid>": "<exactly 32 bytes, standard base64>"}``. A kid is 1-64 characters of ``A-Z a-z 0-9 .
_ -``, because it travels in the header. A missing or wrong ring raises ``KeyRingError``, whose
message names the fault and never a key.

The rule is pinned as data in ``core/gateway/contracts/gateway-identity/signature.vectors.json``,
which this package's tests and both verifiers' tests read; the services share no code.
"""

from __future__ import annotations

import base64
import binascii
import functools
import hashlib
import hmac
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Mapping, Optional

SIGNATURE_HEADER = "x-gateway-signature"
VERSION = "v2"
KEYS_ENV = "GATEWAY_IDENTITY_KEYS"
ACTIVE_KEY_ENV = "GATEWAY_IDENTITY_ACTIVE_KEY"
KEY_BYTES = 32
KID = re.compile(r"[A-Za-z0-9._-]{1,64}")


class KeyRingError(ValueError):
    """The identity key ring is unusable. The message names the fault, never a key."""


@dataclass(frozen=True)
class SigningKey:
    """The ring key the gateway signs with: its id and its raw bytes (never shown)."""

    kid: str
    key: bytes = field(repr=False)


def parse_ring(keys_json: str) -> dict[str, bytes]:
    """The ring in ``keys_json``: kid → 32 raw key bytes."""
    try:
        raw = json.loads(keys_json)
    except ValueError as exc:
        raise KeyRingError(f"{KEYS_ENV} is not valid JSON") from exc
    if not isinstance(raw, dict) or not raw:
        raise KeyRingError(f"{KEYS_ENV} must be a non-empty JSON object of id -> key")
    ring: dict[str, bytes] = {}
    for kid, encoded in raw.items():
        if not KID.fullmatch(kid):
            raise KeyRingError(
                f"{KEYS_ENV}: a key id must be 1-64 characters of A-Z a-z 0-9 . _ -"
            )
        if not isinstance(encoded, str):
            raise KeyRingError(f"{KEYS_ENV}: key {kid!r} is not a base64 string")
        try:
            key = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise KeyRingError(f"{KEYS_ENV}: key {kid!r} is not valid base64") from exc
        if len(key) != KEY_BYTES:
            raise KeyRingError(
                f"{KEYS_ENV}: key {kid!r} must be exactly {KEY_BYTES} bytes"
            )
        ring[kid] = key
    return ring


@functools.lru_cache(maxsize=4)
def _signing_key(keys_json: str, active: str) -> SigningKey:
    ring = parse_ring(keys_json)
    if active not in ring:
        raise KeyRingError(f"{ACTIVE_KEY_ENV} does not name a key in {KEYS_ENV}")
    return SigningKey(active, ring[active])


def signing_key(environ: Optional[Mapping[str, str]] = None) -> SigningKey:
    """The key the gateway signs with, from ``GATEWAY_IDENTITY_KEYS`` +
    ``GATEWAY_IDENTITY_ACTIVE_KEY``; ``KeyRingError`` when either is unset or wrong."""
    env = os.environ if environ is None else environ
    keys_json = (env.get(KEYS_ENV) or "").strip()
    active = (env.get(ACTIVE_KEY_ENV) or "").strip()
    if not keys_json:
        raise KeyRingError(f"{KEYS_ENV} is not configured")
    if not active:
        raise KeyRingError(f"{ACTIVE_KEY_ENV} is not configured")
    return _signing_key(keys_json, active)


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
    key: SigningKey,
    user_id: str,
    scopes: str,
    limits: str,
    method: str,
    path: str,
    query: str,
    body: bytes,
    t: int,
) -> str:
    """The ``x-gateway-signature`` value for one forwarded request made at unix time ``t``."""
    fields = [
        VERSION,
        key.kid,
        str(t),
        user_id,
        scopes,
        limits,
        method.upper(),
        hashlib.sha256(body).hexdigest(),
        query,
        path,
    ]
    message = "\n".join(fields).encode("utf-8")
    mac = hmac.new(key.key, message, hashlib.sha256)
    return f"kid={key.kid},t={t},{VERSION}={mac.hexdigest()}"


def sign_now(
    key: SigningKey,
    headers: Mapping[str, str],
    method: str,
    url: str,
    params: Optional[Mapping[str, str]],
    body: bytes,
) -> str:
    """Sign a request forwarded now to ``url`` with ``headers``, ``params`` and ``body``.

    ``headers`` are the lower-case headers forwarded; the signature covers their ``x-user-id``,
    ``x-user-scopes`` and ``x-user-limits``."""
    path, query = signed_target(url, params)
    return sign(
        key,
        headers["x-user-id"],
        headers.get("x-user-scopes", ""),
        headers.get("x-user-limits", ""),
        method,
        path,
        query,
        body,
        int(time.time()),
    )
