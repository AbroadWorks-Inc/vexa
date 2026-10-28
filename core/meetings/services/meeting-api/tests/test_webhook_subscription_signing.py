"""§2.7 — how meeting-api signs a subscription delivery, opens its secret and guards its URL.

Three pieces, each held to the data admin-api is held to (the two services share no code):

  * ``webhooks/secret_box.py`` — opens a subscription's stored secret: AES-256-GCM, 12-byte nonce,
    AAD ``aw-webhook-secret``, stored as ``nonce || ciphertext || tag``, under the key ring. Checked
    against ``core/identity/contracts/webhook-subscriptions/secret-box.vectors.json``.
  * ``webhooks/signing.py`` — ``X-Webhook-Timestamp`` + exactly one ``X-Webhook-Signature``, plus
    ``X-Webhook-Signature-Previous`` when a previous secret is given, and no ``Authorization``.
    Verified with a test-local copy of the exporter's receiver check
    (``integrations/out/aw-notetaker/exporter/signature.py``) and the sealed ``webhook.v1``
    ``SignatureHeaders`` shape.
  * ``webhooks/ssrf.py`` — the send-time URL guard with ``WEBHOOK_PRIVATE_HOST_ALLOWLIST`` and
    IPv4-mapped IPv6 addresses judged as the IPv4 address they map. Checked against
    ``url-guard.vectors.json``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Optional

import jsonschema
import pytest
from referencing import Registry, Resource

from meeting_api.webhooks import secret_box as sb
from meeting_api.webhooks.signing import signed_headers
from meeting_api.webhooks.ssrf import (
    DEFAULT_PRIVATE_HOST_ALLOWLIST,
    PinnedURL,
    SSRFError,
    UnresolvableHost,
    build_pinned_transport,
    parse_allowlist,
    revalidate_at_connect,
    validate_webhook_url,
)


def _contract_dir(*parts: str) -> Path:
    rel = Path(*parts)
    for parent in Path(__file__).resolve().parents:
        if (parent / rel).is_dir():
            return parent / rel
    raise FileNotFoundError(str(rel))


VECTORS = _contract_dir("identity", "contracts", "webhook-subscriptions")
BOX = json.loads((VECTORS / "secret-box.vectors.json").read_text())
URLS = json.loads((VECTORS / "url-guard.vectors.json").read_text())


# ── the exporter's receiver check, copied (never imported) ───────────────────────────────────


def _get(headers: Mapping[str, str], name: str) -> Optional[str]:
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None


def exporter_verify(
    body: bytes,
    headers: Mapping[str, str],
    secret: str,
    now: float,
    max_age_s: int = 300,
) -> bool:
    """``integrations/out/aw-notetaker/exporter/signature.py`` ``verify``, verbatim."""
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


def _signature_headers_schema_conforms(headers: dict[str, str]) -> None:
    path = _contract_dir("meetings", "contracts", "webhook.v1") / "webhook.schema.json"
    schema = json.loads(path.read_text())
    registry = Registry().with_resource(schema["$id"], Resource.from_contents(schema))
    jsonschema.Draft202012Validator(
        {"$ref": f"{schema['$id']}#/$defs/SignatureHeaders"}, registry=registry
    ).validate(headers)


# ── secret box ───────────────────────────────────────────────────────────────────────────────


def _ring_json() -> str:
    return json.dumps(BOX["key_ring"])


def test_the_vectors_pin_the_layout_this_box_reads():
    assert BOX["algorithm"] == "AES-256-GCM"
    assert BOX["aad"].encode() == sb.AAD
    assert BOX["nonce_bytes"] == sb.NONCE_BYTES
    assert BOX["layout"] == "nonce || ciphertext || tag"


@pytest.mark.parametrize("vector", BOX["vectors"], ids=lambda v: v["key_id"])
def test_every_stored_vector_opens_to_its_plaintext(vector):
    box = sb.SecretBox.from_settings(_ring_json(), "k1")
    stored = bytes.fromhex(vector["stored_hex"])
    assert base64.b64decode(vector["stored_b64"]) == stored
    assert box.decrypt(stored, vector["key_id"]) == vector["plaintext"]


def test_decryption_uses_the_stored_key_id_not_the_active_one():
    box = sb.SecretBox.from_settings(_ring_json(), "k2")
    vector = BOX["vectors"][0]
    assert vector["key_id"] == "k1"
    assert box.decrypt(bytes.fromhex(vector["stored_hex"]), "k1") == vector["plaintext"]


@pytest.mark.parametrize("ring", BOX["invalid_rings"], ids=lambda r: r["case"])
def test_every_invalid_ring_is_refused(ring):
    with pytest.raises(sb.KeyRingError):
        sb.SecretBox.from_settings(ring["keys"], ring["active"])
    with pytest.raises(sb.KeyRingError):
        sb.secret_box_from_env(
            {sb.KEYS_ENV: ring["keys"], sb.ACTIVE_KEY_ENV: ring["active"]}
        )


def test_a_ring_error_never_carries_key_material():
    bad = json.dumps({"k1": BOX["key_ring"]["k1"][:-4]})  # 29 bytes
    with pytest.raises(sb.KeyRingError) as exc:
        sb.SecretBox.from_settings(bad, "k1")
    assert BOX["key_ring"]["k1"][:20] not in str(exc.value)


def test_a_tampered_or_foreign_ciphertext_does_not_open():
    box = sb.SecretBox.from_settings(_ring_json(), "k1")
    vector = BOX["vectors"][0]
    stored = bytearray(bytes.fromhex(vector["stored_hex"]))
    stored[-1] ^= 0x01
    with pytest.raises(sb.SecretBoxError):
        box.decrypt(bytes(stored), "k1")
    with pytest.raises(sb.SecretBoxError):
        box.decrypt(bytes.fromhex(vector["stored_hex"]), "k2")  # the wrong key
    with pytest.raises(sb.SecretBoxError):
        box.decrypt(bytes.fromhex(vector["stored_hex"]), "k9")  # not in the ring
    with pytest.raises(sb.SecretBoxError):
        box.decrypt(b"short", "k1")


def test_the_env_ring_is_none_only_when_both_settings_are_unset():
    assert sb.secret_box_from_env({}) is None
    assert sb.secret_box_from_env({sb.KEYS_ENV: " ", sb.ACTIVE_KEY_ENV: ""}) is None
    with pytest.raises(sb.KeyRingError):
        sb.secret_box_from_env({sb.KEYS_ENV: _ring_json()})
    with pytest.raises(sb.KeyRingError):
        sb.secret_box_from_env({sb.ACTIVE_KEY_ENV: "k1"})
    box = sb.secret_box_from_env({sb.KEYS_ENV: _ring_json(), sb.ACTIVE_KEY_ENV: "k1"})
    assert box is not None
    assert BOX["key_ring"]["k1"] not in repr(box)


# ── signing ──────────────────────────────────────────────────────────────────────────────────

BODY = b'{"data":{},"event_id":"evt_1","event_type":"webhook.test"}'
NOW = 1_790_000_000


def test_the_signature_verifies_with_the_exporters_check():
    headers = signed_headers(BODY, secret="current-secret", timestamp=NOW)
    assert exporter_verify(BODY, headers, "current-secret", now=NOW)
    assert exporter_verify(BODY, headers, "current-secret", now=NOW + 300)
    assert not exporter_verify(BODY, headers, "current-secret", now=NOW + 301)
    assert not exporter_verify(BODY + b" ", headers, "current-secret", now=NOW)
    assert not exporter_verify(BODY, headers, "another-secret", now=NOW)


def test_the_headers_are_the_sealed_shape_with_one_signature_and_no_authorization():
    headers = signed_headers(BODY, secret="current-secret", timestamp=NOW)
    assert headers["X-Webhook-Timestamp"] == str(NOW)
    assert headers["Content-Type"] == "application/json"
    assert "X-Webhook-Signature-Previous" not in headers
    assert not {k.lower() for k in headers} & {"authorization"}
    assert isinstance(headers["X-Webhook-Signature"], str)
    assert headers["X-Webhook-Signature"].count("sha256=") == 1
    _signature_headers_schema_conforms(headers)


def test_the_previous_header_is_the_same_scheme_under_the_previous_secret():
    headers = signed_headers(
        BODY, secret="new-secret", timestamp=NOW, previous_secret="old-secret"
    )
    _signature_headers_schema_conforms(headers)
    assert exporter_verify(BODY, headers, "new-secret", now=NOW)
    # a receiver still on the old secret matches the previous header
    as_old = {
        "X-Webhook-Timestamp": headers["X-Webhook-Timestamp"],
        "X-Webhook-Signature": headers["X-Webhook-Signature-Previous"],
    }
    assert exporter_verify(BODY, as_old, "old-secret", now=NOW)
    assert headers["X-Webhook-Signature"] != headers["X-Webhook-Signature-Previous"]


# ── the URL guard ────────────────────────────────────────────────────────────────────────────


def _vector_resolver(vector: dict[str, Any]):
    def resolve(host: str) -> list[str]:
        if vector["resolves_to"] is None:
            raise AssertionError(
                f"{vector['url']} must be judged without resolving {host}"
            )
        return list(vector["resolves_to"])

    return resolve


@pytest.mark.parametrize(
    "vector",
    URLS["vectors"],
    ids=lambda v: f"{v['expect']}:{v['url']}:{','.join(v['allowlist'])}",
)
def test_every_shared_url_vector(vector):
    allowlist = parse_allowlist(",".join(vector["allowlist"]))
    if vector["expect"] == "allow":
        pinned = validate_webhook_url(
            vector["url"], resolver=_vector_resolver(vector), allowlist=allowlist
        )
        assert isinstance(pinned, PinnedURL)
        assert str(pinned) == vector["url"]
    else:
        with pytest.raises(SSRFError):
            validate_webhook_url(
                vector["url"], resolver=_vector_resolver(vector), allowlist=allowlist
            )


@pytest.mark.parametrize(
    "url", ["https://hooks.example.com:tok3n/x", "http://[tok3n::1/x"]
)
def test_a_url_that_does_not_parse_is_refused_without_echoing_it(url):
    with pytest.raises(SSRFError) as ei:
        validate_webhook_url(url, resolver=lambda h: ["93.184.216.34"])
    assert not isinstance(ei.value, UnresolvableHost)
    assert "tok3n" not in str(ei.value)


def test_the_default_allowlist_lets_the_portal_through_and_blocks_10_0_0_1():
    allowlist = parse_allowlist(DEFAULT_PRIVATE_HOST_ALLOWLIST)
    assert allowlist == frozenset({"portal.notetaker.svc.cluster.local"})

    def must_not_resolve(host: str) -> list[str]:
        raise AssertionError(f"an allow-listed host is never resolved ({host})")

    pinned = validate_webhook_url(
        "http://portal.notetaker.svc.cluster.local/api/webhooks/aw-bots",
        resolver=must_not_resolve,
        allowlist=allowlist,
    )
    assert pinned.pinned_ips == []
    with pytest.raises(SSRFError):
        validate_webhook_url("http://10.0.0.1/x", allowlist=allowlist)


def test_callers_without_an_allowlist_keep_refusing_private_hosts():
    with pytest.raises(SSRFError):
        validate_webhook_url(
            "http://portal.notetaker.svc.cluster.local/x",
            resolver=lambda host: ["10.100.0.7"],
        )


def test_a_host_that_resolves_to_nothing_is_its_own_refusal():
    with pytest.raises(UnresolvableHost):
        validate_webhook_url("https://nowhere.example.com/x", resolver=lambda host: [])
    with pytest.raises(SSRFError) as blocked:
        validate_webhook_url(
            "https://internal.example.com/x", resolver=lambda h: ["10.1.2.3"]
        )
    assert not isinstance(blocked.value, UnresolvableHost)


def test_the_connect_time_check_applies_the_same_rules():
    allowlist = parse_allowlist(DEFAULT_PRIVATE_HOST_ALLOWLIST)
    assert (
        revalidate_at_connect("portal.notetaker.svc.cluster.local", allowlist=allowlist)
        == []
    )
    with pytest.raises(SSRFError):
        revalidate_at_connect("::ffff:10.0.0.1")
    with pytest.raises(SSRFError):
        revalidate_at_connect(
            "mapped.example.com", resolver=lambda h: ["::ffff:192.168.1.5"]
        )
    assert revalidate_at_connect(
        "hooks.example.com", resolver=lambda h: ["93.184.216.34"]
    ) == ["93.184.216.34"]


async def test_the_pinned_transport_dials_an_allow_listed_host_as_is():
    import httpx

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    def must_not_resolve(host: str) -> list[str]:
        raise AssertionError("an allow-listed host is never resolved")

    transport = build_pinned_transport(
        httpx.MockTransport(handler),
        resolver=must_not_resolve,
        allowlist=parse_allowlist(DEFAULT_PRIVATE_HOST_ALLOWLIST),
    )
    async with httpx.AsyncClient(transport=transport) as client:
        r = await client.post(
            "http://portal.notetaker.svc.cluster.local/hook", content=b"{}"
        )
    assert r.status_code == 204
    assert seen[0].url.host == "portal.notetaker.svc.cluster.local"

    blocked = build_pinned_transport(httpx.MockTransport(handler))
    async with httpx.AsyncClient(transport=blocked) as client:
        with pytest.raises(SSRFError):
            await client.post("http://[::ffff:10.0.0.1]/hook", content=b"{}")
