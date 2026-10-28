"""The key ring format meeting-api reads (§6.9 F-E).

A ring is one setting holding a JSON object ``{"<kid>": "<exactly 32 bytes, standard base64>"}``:
the gateway identity keys (``identity_guard``) and the webhook secret encryption keys
(``webhooks.secret_box``). ``parse`` is the one reader; each ring passes its own setting name and
its own key id rule. Every error names the setting and the fault, never a key.
"""

from __future__ import annotations

import base64
import binascii
import json
from typing import Callable

__all__ = ["KEY_BYTES", "KeyRingError", "parse"]

KEY_BYTES = 32


class KeyRingError(ValueError):
    """A key ring is unusable. The message names the fault, never a key."""


def parse(
    keys_json: str, *, env_name: str, kid_ok: Callable[[str], bool], kid_rule: str
) -> dict[str, bytes]:
    """The ring in ``keys_json`` (the value of ``env_name``): kid → 32 raw key bytes. A kid must
    pass ``kid_ok``; ``kid_rule`` says what that asks, for the error."""
    try:
        raw = json.loads(keys_json)
    except ValueError as exc:
        raise KeyRingError(f"{env_name} is not valid JSON") from exc
    if not isinstance(raw, dict) or not raw:
        raise KeyRingError(f"{env_name} must be a non-empty JSON object of id -> key")
    ring: dict[str, bytes] = {}
    for kid, encoded in raw.items():
        if not kid_ok(kid):
            raise KeyRingError(f"{env_name}: a key id must be {kid_rule}")
        if not isinstance(encoded, str):
            raise KeyRingError(f"{env_name}: key {kid!r} is not a base64 string")
        try:
            key = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise KeyRingError(f"{env_name}: key {kid!r} is not valid base64") from exc
        if len(key) != KEY_BYTES:
            raise KeyRingError(
                f"{env_name}: key {kid!r} must be exactly {KEY_BYTES} bytes"
            )
        ring[kid] = key
    return ring
