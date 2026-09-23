"""Verify Vexa's webhook signature (meeting_api/webhooks/delivery.py sign_payload)."""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping


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
    signature = _get(headers, "X-Webhook-Signature")
    timestamp = _get(headers, "X-Webhook-Timestamp")
    if not signature or not timestamp:
        return False
    try:
        sent_at = int(timestamp)
    except ValueError:
        return False
    if abs(now - sent_at) > max_age_s:
        return False
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256)
    return hmac.compare_digest(signature, f"sha256={mac.hexdigest()}")
