"""The webhook-secret box (§2.7): AES-256-GCM under a key ring, read from the shared vectors that
meeting-api's signing side reads too (``core/identity/contracts/webhook-subscriptions``).
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from admin_api.app.secret_box import (
    KeyRingError,
    SecretBox,
    SecretBoxError,
    secret_box_from_env,
)

VECTORS = json.loads(
    (
        Path(__file__).resolve().parents[3]
        / "contracts"
        / "webhook-subscriptions"
        / "secret-box.vectors.json"
    ).read_text()
)
RING_JSON = json.dumps(VECTORS["key_ring"])


def _box(active: str = "k1", **kwargs) -> SecretBox:
    return SecretBox.from_settings(RING_JSON, active, **kwargs)


@pytest.mark.parametrize("vector", VECTORS["vectors"], ids=lambda v: v["key_id"])
def test_encrypt_produces_the_shared_vector_bytes(vector):
    nonce = bytes.fromhex(vector["nonce_hex"])
    box = _box(vector["key_id"], nonce=lambda n: nonce)

    sealed = box.encrypt(vector["plaintext"])

    assert sealed.key_id == vector["key_id"]
    assert sealed.ciphertext.hex() == vector["stored_hex"]
    assert base64.b64encode(sealed.ciphertext).decode() == vector["stored_b64"]


@pytest.mark.parametrize("vector", VECTORS["vectors"], ids=lambda v: v["key_id"])
def test_decrypt_reads_the_shared_vector(vector):
    box = _box("k1")
    stored = bytes.fromhex(vector["stored_hex"])
    assert box.decrypt(stored, vector["key_id"]) == vector["plaintext"]


def test_the_layout_is_nonce_then_ciphertext_then_tag():
    box = _box()
    sealed = box.encrypt("x" * 30)
    assert len(sealed.ciphertext) == VECTORS["nonce_bytes"] + 30 + VECTORS["tag_bytes"]


def test_each_encryption_uses_a_fresh_nonce():
    box = _box()
    a, b = box.encrypt("same"), box.encrypt("same")
    assert a.ciphertext[:12] != b.ciphertext[:12]
    assert a.ciphertext != b.ciphertext


def test_a_row_under_the_old_key_is_readable_after_the_active_key_changes_and_is_rewrapped():
    old = _box("k1").encrypt("rotating-secret-0001")
    box = _box("k2")

    assert box.decrypt(old.ciphertext, old.key_id) == "rotating-secret-0001"
    rewrapped = box.rewrap(old.ciphertext, old.key_id)

    assert rewrapped is not None
    assert rewrapped.key_id == "k2"
    assert box.decrypt(rewrapped.ciphertext, "k2") == "rotating-secret-0001"
    # a ring without k1 still reads the re-wrapped row
    only_k2 = SecretBox.from_settings(
        json.dumps({"k2": VECTORS["key_ring"]["k2"]}), "k2"
    )
    assert only_k2.decrypt(rewrapped.ciphertext, "k2") == "rotating-secret-0001"


def test_a_row_already_under_the_active_key_is_not_rewrapped():
    sealed = _box("k2").encrypt("s" * 20)
    assert _box("k2").rewrap(sealed.ciphertext, "k2") is None


def test_an_unknown_key_id_errors():
    sealed = _box("k1").encrypt("s" * 20)
    with pytest.raises(SecretBoxError) as ei:
        _box("k1").decrypt(sealed.ciphertext, "k9")
    assert "k9" in str(ei.value)


def test_the_wrong_key_id_errors():
    sealed = _box("k1").encrypt("s" * 20)
    with pytest.raises(SecretBoxError):
        _box("k1").decrypt(sealed.ciphertext, "k2")


def test_a_tampered_ciphertext_errors():
    sealed = bytearray(_box("k1").encrypt("s" * 20).ciphertext)
    sealed[15] ^= 0x01
    with pytest.raises(SecretBoxError):
        _box("k1").decrypt(bytes(sealed), "k1")


def test_a_ciphertext_encrypted_without_the_aad_errors():
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = base64.b64decode(VECTORS["key_ring"]["k1"])
    nonce = bytes(12)
    blob = nonce + AESGCM(key).encrypt(nonce, b"s" * 20, None)
    with pytest.raises(SecretBoxError):
        _box("k1").decrypt(blob, "k1")


@pytest.mark.parametrize("case", VECTORS["invalid_rings"], ids=lambda c: c["case"])
def test_a_bad_key_ring_is_refused_without_echoing_key_material(case):
    with pytest.raises(KeyRingError) as ei:
        SecretBox.from_settings(case["keys"], case["active"])
    message = str(ei.value)
    for value in VECTORS["key_ring"].values():
        assert value not in message
    assert "AAECAwQF" not in message


def test_the_env_ring_is_absent_when_neither_setting_is_set():
    assert secret_box_from_env({}) is None


@pytest.mark.parametrize(
    "env",
    [
        {"WEBHOOK_SECRET_ENC_KEYS": RING_JSON},
        {"WEBHOOK_SECRET_ENC_ACTIVE_KEY": "k1"},
        {"WEBHOOK_SECRET_ENC_KEYS": RING_JSON, "WEBHOOK_SECRET_ENC_ACTIVE_KEY": "k9"},
        {"WEBHOOK_SECRET_ENC_KEYS": "{}", "WEBHOOK_SECRET_ENC_ACTIVE_KEY": "k1"},
    ],
)
def test_a_half_or_bad_env_ring_is_refused(env):
    with pytest.raises(KeyRingError):
        secret_box_from_env(env)


def test_the_env_ring_builds_the_box():
    box = secret_box_from_env(
        {"WEBHOOK_SECRET_ENC_KEYS": RING_JSON, "WEBHOOK_SECRET_ENC_ACTIVE_KEY": "k2"}
    )
    assert box is not None
    assert box.active_key_id == "k2"
    assert box.encrypt("s" * 20).key_id == "k2"


def test_the_box_never_shows_its_keys():
    box = _box()
    text = repr(box) + str(box)
    for value in VECTORS["key_ring"].values():
        assert value not in text
    assert "k1" in repr(box)
