"""The webhook-secret box (§2.7): subscription signing secrets encrypted at rest under a key ring.

Algorithm: AES-256-GCM with a 12-byte random nonce and the associated data ``b"aw-webhook-secret"``.

Stored bytes (``webhook_subscriptions.secret_enc`` / ``previous_secret_enc``)::

    nonce (12 bytes) || ciphertext (len(plaintext) bytes) || tag (16 bytes)

The key id the bytes were sealed under is stored beside them (``enc_key_id`` /
``previous_enc_key_id``); decryption uses that id, never the active one. The same layout is pinned
as data in ``core/identity/contracts/webhook-subscriptions/secret-box.vectors.json``, which
meeting-api's signing side reads too (the two services share no code).

Key ring settings:

- ``WEBHOOK_SECRET_ENC_KEYS``: JSON object ``{"<id>": "<32 bytes, standard base64>"}``; an id is
  1-64 characters.
- ``WEBHOOK_SECRET_ENC_ACTIVE_KEY``: the id new secrets are sealed under; it must be in the ring.

Rotating the encryption key: add a key, switch the active id, rows re-encrypt on their next read
(``rewrap``), drop the old key once no row uses it.

A ring that is set but wrong (either setting missing, bad JSON, a key that isn't exactly 32 bytes,
the active id not in the ring) raises ``KeyRingError``, and admin-api refuses to start. With neither
setting present there is no box, and every operation that needs one refuses (503). No message ever
carries key material or a secret.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
from dataclasses import dataclass
from typing import Callable, Mapping, Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

__all__ = [
    "AAD",
    "KEYS_ENV",
    "ACTIVE_KEY_ENV",
    "KeyRingError",
    "SecretBoxError",
    "Sealed",
    "SecretBox",
    "secret_box_from_env",
]

AAD = b"aw-webhook-secret"
NONCE_BYTES = 12
KEY_BYTES = 32
MAX_KEY_ID = 64
KEYS_ENV = "WEBHOOK_SECRET_ENC_KEYS"
ACTIVE_KEY_ENV = "WEBHOOK_SECRET_ENC_ACTIVE_KEY"


class KeyRingError(ValueError):
    """The key ring settings are unusable. The message names the fault, never a key."""


class SecretBoxError(Exception):
    """A stored secret can't be opened: its key id isn't in the ring, or the bytes don't
    authenticate under it."""


@dataclass(frozen=True)
class Sealed:
    """Stored bytes plus the id of the key that sealed them."""

    ciphertext: bytes
    key_id: str


def _parse_ring(keys_json: str) -> dict[str, bytes]:
    try:
        raw = json.loads(keys_json)
    except ValueError as exc:
        raise KeyRingError(f"{KEYS_ENV} is not valid JSON") from exc
    if not isinstance(raw, dict) or not raw:
        raise KeyRingError(f"{KEYS_ENV} must be a non-empty JSON object of id -> key")
    ring: dict[str, bytes] = {}
    for key_id, encoded in raw.items():
        if not key_id or len(key_id) > MAX_KEY_ID:
            raise KeyRingError(
                f"{KEYS_ENV}: a key id must be 1-{MAX_KEY_ID} characters"
            )
        if not isinstance(encoded, str):
            raise KeyRingError(f"{KEYS_ENV}: key {key_id!r} is not a base64 string")
        try:
            key = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise KeyRingError(
                f"{KEYS_ENV}: key {key_id!r} is not valid base64"
            ) from exc
        if len(key) != KEY_BYTES:
            raise KeyRingError(
                f"{KEYS_ENV}: key {key_id!r} must be exactly {KEY_BYTES} bytes"
            )
        ring[key_id] = key
    return ring


class SecretBox:
    """Seals and opens webhook secrets under a key ring (see the module docstring)."""

    def __init__(
        self,
        ring: Mapping[str, bytes],
        active_key_id: str,
        *,
        nonce: Callable[[int], bytes] = os.urandom,
    ) -> None:
        if not ring:
            raise KeyRingError("the key ring is empty")
        for key_id, key in ring.items():
            if len(key) != KEY_BYTES:
                raise KeyRingError(f"key {key_id!r} must be exactly {KEY_BYTES} bytes")
        if not active_key_id or active_key_id not in ring:
            raise KeyRingError(f"{ACTIVE_KEY_ENV} does not name a key in the ring")
        self._ring = {key_id: AESGCM(key) for key_id, key in ring.items()}
        self._active = active_key_id
        self._nonce = nonce

    @classmethod
    def from_settings(
        cls,
        keys_json: str,
        active_key_id: str,
        *,
        nonce: Callable[[int], bytes] = os.urandom,
    ) -> "SecretBox":
        return cls(_parse_ring(keys_json), active_key_id, nonce=nonce)

    @property
    def active_key_id(self) -> str:
        return self._active

    def __repr__(self) -> str:
        return f"SecretBox(active={self._active!r}, key_ids={sorted(self._ring)!r})"

    def encrypt(self, plaintext: str) -> Sealed:
        """Seal ``plaintext`` under the active key."""
        nonce = self._nonce(NONCE_BYTES)
        if len(nonce) != NONCE_BYTES:
            raise SecretBoxError(f"a nonce must be {NONCE_BYTES} bytes")
        sealed = self._ring[self._active].encrypt(nonce, plaintext.encode("utf-8"), AAD)
        return Sealed(ciphertext=nonce + sealed, key_id=self._active)

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

    def rewrap(self, ciphertext: bytes, key_id: str) -> Optional[Sealed]:
        """The same secret sealed under the active key, or ``None`` when it already is."""
        if key_id == self._active:
            return None
        return self.encrypt(self.decrypt(ciphertext, key_id))


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
