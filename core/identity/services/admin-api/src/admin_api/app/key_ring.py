"""The key ring format admin-api's two rings share (design §1.10, §6.9 F-E).

A ring is a JSON object ``{"<key id>": "<exactly 32 bytes, standard base64>"}``. Two settings use
it: ``GATEWAY_IDENTITY_KEYS`` (``identity_guard``) and ``WEBHOOK_SECRET_ENC_KEYS``
(``secret_box``). They differ only in which key ids they accept, so each caller passes its own
key-id pattern and the words that describe it.

Every fault raises ``KeyRingError`` naming the setting and the key id, never key material.
"""

from __future__ import annotations

import base64
import binascii
import json
import re

__all__ = ["KEY_BYTES", "KeyRingError", "parse"]

KEY_BYTES = 32


class KeyRingError(ValueError):
    """A key ring is unusable. The message names the fault, never a key."""


def parse(
    keys_json: str, env_name: str, kid: re.Pattern[str], kid_rule: str
) -> dict[str, bytes]:
    """The ring in ``keys_json``: key id -> 32 raw key bytes.

    ``env_name`` names the setting in every error; a key id must fully match ``kid``, and
    ``kid_rule`` says in words what that pattern allows.
    """
    try:
        raw = json.loads(keys_json)
    except ValueError as exc:
        raise KeyRingError(f"{env_name} is not valid JSON") from exc
    if not isinstance(raw, dict) or not raw:
        raise KeyRingError(f"{env_name} must be a non-empty JSON object of id -> key")
    ring: dict[str, bytes] = {}
    for key_id, encoded in raw.items():
        if not kid.fullmatch(key_id):
            raise KeyRingError(f"{env_name}: a key id must be {kid_rule}")
        if not isinstance(encoded, str):
            raise KeyRingError(f"{env_name}: key {key_id!r} is not a base64 string")
        try:
            key = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise KeyRingError(
                f"{env_name}: key {key_id!r} is not valid base64"
            ) from exc
        if len(key) != KEY_BYTES:
            raise KeyRingError(
                f"{env_name}: key {key_id!r} must be exactly {KEY_BYTES} bytes"
            )
        ring[key_id] = key
    return ring
