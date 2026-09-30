"""Verify an aw-bots subscription delivery's signature (webhook.v1
`SignatureHeaders`; meeting_api/webhooks/signing.py).

`X-Webhook-Signature` carries `sha256=<hex HMAC-SHA256(secret, "<ts>." +
body)>` under the subscription's current secret; for 24 h after a
`rotate-secret`, `X-Webhook-Signature-Previous` carries the same under the
previous one. A match on either header with the exporter's one secret is a
valid delivery, so the exporter keeps receiving while its secret is rotated.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping

_SIGNATURE_HEADERS = ("X-Webhook-Signature", "X-Webhook-Signature-Previous")


def _get(headers: Mapping[str, str], name: str) -> str | None:
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None


def verify(
    body: bytes,
    headers: Mapping[str, str],
    secret: str,
    now: float,
    max_age_s: int = 300,
) -> bool:
    if not secret:
        return False
    timestamp = _get(headers, "X-Webhook-Timestamp")
    signatures = [s for s in (_get(headers, h) for h in _SIGNATURE_HEADERS) if s]
    if not signatures or not timestamp:
        return False
    try:
        sent_at = int(timestamp)
    except ValueError:
        return False
    if abs(now - sent_at) > max_age_s:
        return False
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256)
    expected = f"sha256={mac.hexdigest()}"
    return any(hmac.compare_digest(s, expected) for s in signatures)
