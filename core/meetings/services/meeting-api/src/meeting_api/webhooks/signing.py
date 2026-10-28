"""The headers of a subscription delivery (§2.7) — the scheme the exporter verifies
(``integrations/out/aw-notetaker/exporter/signature.py``) and sealed ``webhook.v1``
``SignatureHeaders`` describes:

- ``X-Webhook-Timestamp: <unix seconds>``;
- ``X-Webhook-Signature: sha256=<hex HMAC-SHA256(secret, "<timestamp>." + raw body)>``, always
  exactly one value;
- ``X-Webhook-Signature-Previous``: the same under the previous secret, only when the caller
  passes one (the 24 h after a rotation);
- ``Content-Type: application/json``.

There is no ``Authorization`` header: the secret never crosses the wire, only its HMAC does.
"""

from __future__ import annotations

from typing import Optional

from .delivery import sign_payload

__all__ = ["signed_headers"]


def signed_headers(
    body: bytes,
    *,
    secret: str,
    timestamp: int,
    previous_secret: Optional[str] = None,
) -> dict[str, str]:
    """The headers that sign ``body`` (the exact bytes posted) at ``timestamp``."""
    stamp = str(int(timestamp))
    headers = {
        "Content-Type": "application/json",
        "X-Webhook-Timestamp": stamp,
        "X-Webhook-Signature": sign_payload(body, secret, stamp),
    }
    if previous_secret is not None:
        headers["X-Webhook-Signature-Previous"] = sign_payload(
            body, previous_secret, stamp
        )
    return headers
