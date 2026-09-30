"""Opens a webhook subscription's signing secret at signing time (§2.7).

admin-api seals each subscription secret; meeting-api only opens it, right before it signs a
delivery. The algorithm is AES-256-GCM with a 12-byte nonce and the associated data
``b"aw-webhook-secret"``, and the stored bytes (``webhook_subscriptions.secret_enc`` /
``previous_secret_enc``) are::

    nonce (12 bytes) || ciphertext (len(plaintext) bytes) || tag (16 bytes)

The id of the key that sealed them is stored beside them (``enc_key_id`` / ``previous_enc_key_id``)
and decryption uses that id. The two services share no code; both test suites read the same layout
from ``core/identity/contracts/webhook-subscriptions/secret-box.vectors.json``.

Key ring settings (the same two admin-api reads):

- ``WEBHOOK_SECRET_ENC_KEYS``: JSON object ``{"<id>": "<32 bytes, standard base64>"}``; an id is
  1-64 characters.
- ``WEBHOOK_SECRET_ENC_ACTIVE_KEY``: the id admin-api seals new secrets under; it must be in the
  ring.

A ring that is set but wrong raises ``KeyRingError`` and meeting-api refuses to start. With neither
setting there is no box, and nothing is signed or sent. No message ever carries key material or a
secret.
"""

from __future__ import annotations

import os
from typing import Mapping, Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .. import key_ring
from ..key_ring import KEY_BYTES, KeyRingError

__all__ = [
    "AAD",
    "ACTIVE_KEY_ENV",
    "KEYS_ENV",
    "NONCE_BYTES",
    "KeyRingError",
    "SecretBox",
    "SecretBoxError",
    "secret_box_from_env",
]

AAD = b"aw-webhook-secret"
NONCE_BYTES = 12
MAX_KEY_ID = 64
KEYS_ENV = "WEBHOOK_SECRET_ENC_KEYS"
ACTIVE_KEY_ENV = "WEBHOOK_SECRET_ENC_ACTIVE_KEY"


class SecretBoxError(Exception):
    """A stored secret can't be opened: its key id isn't in the ring, or the bytes don't
    authenticate under it."""


def _parse_ring(keys_json: str) -> dict[str, bytes]:
    return key_ring.parse(
        keys_json,
        env_name=KEYS_ENV,
        kid_ok=lambda key_id: 0 < len(key_id) <= MAX_KEY_ID,
        kid_rule=f"1-{MAX_KEY_ID} characters",
    )


class SecretBox:
    """Opens webhook secrets under a key ring (see the module docstring)."""

    def __init__(self, ring: Mapping[str, bytes], active_key_id: str) -> None:
        if not ring:
            raise KeyRingError("the key ring is empty")
        for key_id, key in ring.items():
            if len(key) != KEY_BYTES:
                raise KeyRingError(f"key {key_id!r} must be exactly {KEY_BYTES} bytes")
        if not active_key_id or active_key_id not in ring:
            raise KeyRingError(f"{ACTIVE_KEY_ENV} does not name a key in the ring")
        self._ring = {key_id: AESGCM(key) for key_id, key in ring.items()}
        self._active = active_key_id

    @classmethod
    def from_settings(cls, keys_json: str, active_key_id: str) -> "SecretBox":
        return cls(_parse_ring(keys_json), active_key_id)

    def __repr__(self) -> str:
        return f"SecretBox(active={self._active!r}, key_ids={sorted(self._ring)!r})"

    def decrypt(self, ciphertext: bytes, key_id: str) -> str:
        """Open bytes sealed under ``key_id``."""
        aead = self._ring.get(key_id)
        if aead is None:
            raise SecretBoxError(f"key id {key_id!r} is not in the key ring")
        if len(ciphertext) <= NONCE_BYTES:
            raise SecretBoxError("stored secret is too short")
        try:
            plain = aead.decrypt(
                ciphertext[:NONCE_BYTES], ciphertext[NONCE_BYTES:], AAD
            )
        except InvalidTag as exc:
            raise SecretBoxError(
                f"stored secret does not authenticate under key id {key_id!r}"
            ) from exc
        return plain.decode("utf-8")


def secret_box_from_env(
    environ: Optional[Mapping[str, str]] = None,
) -> Optional[SecretBox]:
    """The box from ``WEBHOOK_SECRET_ENC_KEYS`` + ``WEBHOOK_SECRET_ENC_ACTIVE_KEY``.

    ``None`` when neither is set; ``KeyRingError`` when only one is set or the ring is wrong.
    """
    env = os.environ if environ is None else environ
    keys_json = (env.get(KEYS_ENV) or "").strip()
    active = (env.get(ACTIVE_KEY_ENV) or "").strip()
    if not keys_json and not active:
        return None
    if not keys_json:
        raise KeyRingError(f"{ACTIVE_KEY_ENV} is set but {KEYS_ENV} is not")
    if not active:
        raise KeyRingError(f"{KEYS_ENV} is set but {ACTIVE_KEY_ENV} is not")
    return SecretBox.from_settings(keys_json, active)
