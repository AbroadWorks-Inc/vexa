"""The gateway signs the identity it forwards (design §1.10, §6.9 F-E).

Every forwarded request that carries ``x-user-id`` also carries ``x-gateway-signature`` (version
``v2``), made with the active key of the ``GATEWAY_IDENTITY_KEYS`` ring and naming it (``kid``),
over the forwarded user, every other identity header it forwards (``IDENTITY_HEADERS``), method,
path, raw query string and the SHA-256 of the exact body bytes forwarded. A signature a client sends is never
forwarded. Without a usable ring the gateway refuses to start (preflight and the ring check) and
refuses to forward. The signing rule is pinned by the shared vectors meeting-api and admin-api
verify with.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from pathlib import Path
from typing import Any, Mapping, Optional

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway import config_preflight as cp
from gateway import create_app
from gateway.adapters import AdminApiAuthorizer
from gateway.identity_signature import (
    IDENTITY_HEADERS,
    SIGNATURE_HEADER,
    KeyRingError,
    SigningKey,
    parse_ring,
    sign,
    signed_target,
    signing_key,
)

from conftest import VALID_KEY, VALID_USER, FakeAuthorizer, FakeDownstream, FakeRedis

AUTH = {"x-api-key": VALID_KEY}
#: The suite's ring (``conftest.py``): one test-only key.
KID = "gw-unit"
KEY = b"test-gateway-identity-unit-key-1"
RING = json.dumps({KID: base64.b64encode(KEY).decode()})
#: The x-user-scopes and x-user-limits the gateway forwards for ``VALID_USER``.
SCOPES = ",".join(VALID_USER["scopes"])
LIMITS = str(VALID_USER["max_concurrent"])
#: Every identity header besides x-user-id the gateway forwards for ``VALID_USER``.
FORWARDED = {
    "x-user-email": VALID_USER["email"],
    "x-user-scopes": SCOPES,
    "x-user-limits": LIMITS,
}
#: A user with every piece of identity the gateway forwards.
FULL_USER = {
    **VALID_USER,
    "workspaces": ["ws-1", "ws-2"],
    "webhook_url": "https://hooks.example.com/aw",
    "webhook_secret": "whsec-test-only",
    "webhook_events": {"meeting.completed": True},
}


def _vectors() -> dict:
    rel = Path("gateway") / "contracts" / "gateway-identity" / "signature.vectors.json"
    for parent in Path(__file__).resolve().parents:
        if (parent / rel).is_file():
            return json.loads((parent / rel).read_text())
    raise FileNotFoundError(str(rel))


VECTORS = _vectors()


def _verify(
    header: str,
    key: bytes,
    user_id: str,
    method: str,
    path: str,
    query: str = "",
    body: bytes = b"",
    *,
    identity: Mapping[str, str] = FORWARDED,
    kid: str = KID,
) -> int:
    """Check ``header`` the way the receiving services do; returns its ``t``."""
    kid_part, t_part, mac_part = header.split(",")
    assert kid_part == f"kid={kid}"
    assert t_part.startswith("t=") and mac_part.startswith("v2=")
    t = int(t_part[2:])
    fields = [
        "v2",
        kid,
        str(t),
        user_id,
        *[identity.get(name, "") for name in VECTORS["identity_headers"]],
        method,
        hashlib.sha256(body).hexdigest(),
        query,
        path,
    ]
    message = "\n".join(fields).encode()
    expected = hmac.new(key, message, hashlib.sha256).hexdigest()
    assert hmac.compare_digest(mac_part[3:], expected)
    return t


def _as_sent(last: dict[str, Any]) -> httpx.Request:
    """The request httpx sends for the downstream call the fake recorded."""
    with httpx.Client() as client:
        return client.build_request(
            last["method"],
            last["url"],
            params=last["params"] or None,
            content=last["content"],
        )


def _verify_as_sent(last: dict[str, Any], user_id: str = "7") -> int:
    sent = _as_sent(last)
    return _verify(
        last["headers"][SIGNATURE_HEADER],
        KEY,
        user_id,
        sent.method,
        sent.url.path,
        sent.url.query.decode("ascii"),
        sent.content,
        identity=last["headers"],
    )


@pytest.fixture()
def signing(monkeypatch):
    monkeypatch.setenv("GATEWAY_IDENTITY_KEYS", RING)
    monkeypatch.setenv("GATEWAY_IDENTITY_ACTIVE_KEY", KID)


def _client(downstream: Optional[FakeDownstream] = None, **kwargs: Any):
    downstream = downstream or FakeDownstream(status_code=200, body={"ok": True})
    app = create_app(FakeAuthorizer(), downstream, FakeRedis(), **kwargs)
    return TestClient(app), downstream


def test_the_vectors_are_version_2():
    assert VECTORS["version"] == "v2"
    assert all(",v2=" in v["header"] for v in VECTORS["vectors"])


@pytest.mark.parametrize(
    "vector",
    VECTORS["vectors"],
    ids=lambda v: f"{v['method']} {v['path']}?{v['query']}",
)
def test_signer_reproduces_the_shared_vectors(vector):
    body = vector["body"].encode("utf-8")
    assert vector["body_sha256"] == hashlib.sha256(body).hexdigest()
    assert vector["message"] == "\n".join(
        [
            "v2",
            vector["kid"],
            str(vector["t"]),
            vector["user_id"],
            *[vector["headers"].get(h, "") for h in VECTORS["identity_headers"]],
            vector["method"],
            vector["body_sha256"],
            vector["query"],
            vector["path"],
        ]
    )
    key = SigningKey(vector["kid"], base64.b64decode(VECTORS["keys"][vector["kid"]]))
    got = sign(
        key,
        vector["user_id"],
        vector["headers"],
        vector["method"],
        vector["path"],
        vector["query"],
        body,
        vector["t"],
    )
    assert got == vector["header"]


def test_the_identity_headers_signed_are_the_shared_ones():
    assert list(IDENTITY_HEADERS) == VECTORS["identity_headers"]


def test_the_vectors_cover_query_body_and_rotation():
    vectors = VECTORS["vectors"]
    assert any(v["query"] for v in vectors) and any(not v["query"] for v in vectors)
    assert any(v["body"] for v in vectors) and any(not v["body"] for v in vectors)
    for name in VECTORS["identity_headers"]:
        assert any(name in v["headers"] for v in vectors)
        assert any(name not in v["headers"] for v in vectors)
    assert {v["kid"] for v in vectors} == set(VECTORS["keys"])
    assert VECTORS["active_key"] in VECTORS["keys"]
    cases = VECTORS["verify_cases"]
    older = [k for k in VECTORS["keys"] if k != VECTORS["active_key"]]
    assert any(c["valid"] and f"kid={older[0]}," in c["header"] for c in cases)
    assert any("not in the ring" in c["case"] and not c["valid"] for c in cases)
    assert any("dropped from the ring" in c["case"] and not c["valid"] for c in cases)


@pytest.mark.parametrize("case", VECTORS["ring_cases"], ids=lambda c: c["case"])
def test_the_ring_parser_agrees_with_the_shared_cases(case):
    if case["valid"]:
        ring = parse_ring(case["keys"])
        assert ring and all(len(key) == 32 for key in ring.values())
        return
    with pytest.raises(KeyRingError) as ei:
        parse_ring(case["keys"])
    assert "GATEWAY_IDENTITY_KEYS" in str(ei.value)
    assert _key_texts(case["keys"]).isdisjoint(str(ei.value).split())


def _key_texts(keys: str) -> set[str]:
    """The key strings in a ring's text, to check an error never shows one."""
    try:
        raw = json.loads(keys)
    except ValueError:
        return {keys}
    if not isinstance(raw, dict):
        return set()
    return {v for v in raw.values() if isinstance(v, str) and v}


@pytest.mark.parametrize(
    "env,named",
    [
        ({}, "GATEWAY_IDENTITY_KEYS"),
        ({"GATEWAY_IDENTITY_KEYS": RING}, "GATEWAY_IDENTITY_ACTIVE_KEY"),
        ({"GATEWAY_IDENTITY_ACTIVE_KEY": KID}, "GATEWAY_IDENTITY_KEYS"),
        (
            {"GATEWAY_IDENTITY_KEYS": RING, "GATEWAY_IDENTITY_ACTIVE_KEY": "gw-other"},
            "GATEWAY_IDENTITY_ACTIVE_KEY",
        ),
        (
            {"GATEWAY_IDENTITY_KEYS": "not json", "GATEWAY_IDENTITY_ACTIVE_KEY": KID},
            "GATEWAY_IDENTITY_KEYS",
        ),
    ],
    ids=["unset", "no-active", "no-ring", "active-not-in-ring", "malformed"],
)
def test_an_unusable_ring_gives_no_signing_key(env, named):
    with pytest.raises(KeyRingError) as ei:
        signing_key(env)
    assert named in str(ei.value)
    assert base64.b64encode(KEY).decode() not in str(ei.value)


def test_the_signing_key_is_the_active_one_and_never_shown():
    key = signing_key(
        {"GATEWAY_IDENTITY_KEYS": RING, "GATEWAY_IDENTITY_ACTIVE_KEY": KID}
    )
    assert (key.kid, key.key) == (KID, KEY)
    assert KEY.decode() not in repr(key)
    assert base64.b64encode(KEY).decode() not in repr(key)


@pytest.mark.parametrize(
    "url,params,path,query",
    [
        ("http://meeting-api:8080/meetings", None, "/meetings", ""),
        (
            "http://meeting-api:8080/meetings",
            {"limit": "5", "x": "/"},
            "/meetings",
            "limit=5&x=%2F",
        ),
        (
            "http://meeting-api:8080/meetings/teams/room%20one",
            {"q": "a b"},
            "/meetings/teams/room one",
            "q=a+b",
        ),
        ("http://admin-api:8001/v2/webhooks/7/test", {}, "/v2/webhooks/7/test", ""),
    ],
)
def test_the_signed_target_is_what_httpx_sends(url, params, path, query):
    assert signed_target(url, params) == (path, query)
    with httpx.Client() as client:
        sent = client.build_request("GET", url, params=params or None)
    assert (sent.url.path, sent.url.query.decode("ascii")) == (path, query)


def test_a_forwarded_request_is_signed_over_its_path_and_empty_body(signing):
    client, downstream = _client()
    before = int(time.time())
    r = client.get("/meetings", headers=AUTH)
    assert r.status_code == 200
    assert downstream.last["headers"]["x-user-id"] == str(VALID_USER["user_id"])
    t = _verify_as_sent(downstream.last)
    assert before <= t <= int(time.time())
    _verify(downstream.last["headers"][SIGNATURE_HEADER], KEY, "7", "GET", "/meetings")


def test_the_query_signed_is_the_query_forwarded(signing):
    client, downstream = _client()
    r = client.get("/meetings?limit=5&x=%2F", headers=AUTH)
    assert r.status_code == 200
    _verify_as_sent(downstream.last)
    _verify(
        downstream.last["headers"][SIGNATURE_HEADER],
        KEY,
        "7",
        "GET",
        "/meetings",
        "limit=5&x=%2F",
    )


def test_the_body_signed_is_the_body_forwarded(signing):
    client, downstream = _client()
    raw = b'{"platform":"google_meet","native_meeting_id":"abc-defg-hij"}'
    r = client.post(
        "/bots", headers={**AUTH, "content-type": "application/json"}, content=raw
    )
    assert r.status_code == 200
    assert downstream.last["content"] == raw
    _verify_as_sent(downstream.last)
    _verify(
        downstream.last["headers"][SIGNATURE_HEADER],
        KEY,
        "7",
        "POST",
        "/bots",
        body=raw,
    )


def test_the_scopes_and_limits_signed_are_those_forwarded(signing):
    client, downstream = _client()
    r = client.get(
        "/meetings",
        headers={**AUTH, "x-user-scopes": "bot,tx,export", "x-user-limits": "45"},
    )
    assert r.status_code == 200
    headers = downstream.last["headers"]
    assert headers["x-user-scopes"] == SCOPES
    assert headers["x-user-limits"] == LIMITS
    _verify(headers[SIGNATURE_HEADER], KEY, "7", "GET", "/meetings")
    for name, value in (("x-user-scopes", "bot,tx,export"), ("x-user-limits", "45")):
        with pytest.raises(AssertionError):
            _verify(
                headers[SIGNATURE_HEADER],
                KEY,
                "7",
                "GET",
                "/meetings",
                identity={**FORWARDED, name: value},
            )


def _forward_as(user: dict) -> dict[str, Any]:
    downstream = FakeDownstream(status_code=200, body={"ok": True})
    client = TestClient(create_app(FakeAuthorizer(user=user), downstream, FakeRedis()))
    raw = b'{"platform":"google_meet","native_meeting_id":"abc-defg-hij"}'
    r = client.post(
        "/bots", headers={**AUTH, "content-type": "application/json"}, content=raw
    )
    assert r.status_code == 200
    return downstream.last


def test_every_identity_header_the_gateway_forwards_is_signed(signing):
    last = _forward_as(FULL_USER)
    forwarded = {h for h in last["headers"] if h.startswith("x-user-")}
    assert forwarded == {"x-user-id", *IDENTITY_HEADERS}
    _verify_as_sent(last)


@pytest.mark.parametrize("name", IDENTITY_HEADERS)
def test_a_changed_identity_header_breaks_the_signature(signing, name):
    last = _forward_as(FULL_USER)
    _verify_as_sent(last)
    for changed in (
        {**last["headers"], name: "changed"},
        {k: v for k, v in last["headers"].items() if k != name},
    ):
        with pytest.raises(AssertionError):
            _verify_as_sent({**last, "headers": changed})


def test_an_identity_header_not_forwarded_is_signed_empty(signing):
    last = _forward_as(VALID_USER)
    assert "x-user-workspaces" not in last["headers"]
    _verify_as_sent(last)
    with pytest.raises(AssertionError):
        _verify_as_sent(
            {**last, "headers": {**last["headers"], "x-user-workspaces": "ws-1"}}
        )


def test_a_forward_to_admin_api_is_signed_over_the_admin_api_path(signing):
    client, downstream = _client()
    assert client.get("/user/webhook", headers=AUTH).status_code == 200
    assert downstream.last["url"].endswith("/user/webhook")
    _verify(
        downstream.last["headers"][SIGNATURE_HEADER],
        KEY,
        "7",
        "GET",
        "/user/webhook",
    )


def test_the_export_result_is_signed_over_its_meeting_api_path(signing):
    downstream = FakeDownstream()
    exporter = FakeAuthorizer(user={**VALID_USER, "scopes": ["tx", "export"]})
    client = TestClient(create_app(exporter, downstream, FakeRedis()))
    uuid = "5f0c2b7e-8d1a-4c3e-9b6f-2a7d1e4c8b90"
    r = client.post(f"/v2/meetings/{uuid}/export", headers=AUTH, json={})
    assert r.status_code == 200
    _verify_as_sent(downstream.last)


def test_a_path_parameter_is_signed_decoded(signing):
    client, downstream = _client()
    r = client.get("/transcripts/teams/room%20one", headers=AUTH)
    assert r.status_code == 200
    assert _as_sent(downstream.last).url.path == "/transcripts/teams/room one"
    _verify_as_sent(downstream.last)


def test_the_streamed_leg_is_signed_over_its_query(signing):
    client, downstream = _client(mcp_url="http://mcp:8010")
    with client.stream(
        "GET",
        "/mcp?sessionId=abc",
        headers={**AUTH, "accept": "text/event-stream"},
    ) as r:
        list(r.iter_bytes())
    assert downstream.last["params"] == {"sessionId": "abc"}
    _verify_as_sent(downstream.last)


def test_a_client_supplied_signature_is_never_forwarded(signing):
    client, downstream = _client()
    forged = sign(
        SigningKey(KID, KEY), "1", {}, "GET", "/meetings", "", b"", int(time.time())
    )
    r = client.get(
        "/meetings", headers={**AUTH, SIGNATURE_HEADER: forged, "x-user-id": "1"}
    )
    assert r.status_code == 200
    headers = downstream.last["headers"]
    assert headers["x-user-id"] == "7"
    assert headers[SIGNATURE_HEADER] != forged
    _verify_as_sent(downstream.last)


def test_the_gateway_signs_with_the_active_key_of_the_ring(signing, monkeypatch):
    new_key = b"test-gateway-identity-unit-key-2"
    ring = {
        KID: base64.b64encode(KEY).decode(),
        "gw-unit-2": base64.b64encode(new_key).decode(),
    }
    monkeypatch.setenv("GATEWAY_IDENTITY_KEYS", json.dumps(ring))
    monkeypatch.setenv("GATEWAY_IDENTITY_ACTIVE_KEY", "gw-unit-2")
    client, downstream = _client()
    assert client.get("/meetings", headers=AUTH).status_code == 200
    _verify(
        downstream.last["headers"][SIGNATURE_HEADER],
        new_key,
        "7",
        "GET",
        "/meetings",
        kid="gw-unit-2",
    )


@pytest.mark.parametrize(
    "keys,active",
    [(None, KID), (RING, None), ("not json", KID), (RING, "gw-other")],
    ids=["no-ring", "no-active", "malformed", "active-not-in-ring"],
)
def test_without_a_usable_ring_the_gateway_refuses_to_forward(
    monkeypatch, keys, active
):
    for name, value in (
        ("GATEWAY_IDENTITY_KEYS", keys),
        ("GATEWAY_IDENTITY_ACTIVE_KEY", active),
    ):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    client, downstream = _client()
    r = client.get("/meetings", headers=AUTH)
    assert r.status_code == 503
    assert "GATEWAY_IDENTITY_" in r.json()["detail"]
    assert base64.b64encode(KEY).decode() not in r.text
    assert downstream.last is None


@pytest.mark.parametrize(
    "missing", ["GATEWAY_IDENTITY_KEYS", "GATEWAY_IDENTITY_ACTIVE_KEY"]
)
def test_preflight_refuses_boot_without_the_ring(missing):
    env = {
        "INTERNAL_API_SECRET": "a-real-secret",
        "GATEWAY_IDENTITY_KEYS": RING,
        "GATEWAY_IDENTITY_ACTIVE_KEY": KID,
    }
    del env[missing]
    with pytest.raises(cp.ConfigError) as ei:
        cp.preflight(env)
    assert missing in str(ei.value)


@pytest.mark.parametrize("placeholder", ["<REPLACE_ME>", "changeme"])
def test_preflight_refuses_a_placeholder_ring(placeholder):
    with pytest.raises(cp.ConfigError) as ei:
        cp.preflight(
            {
                "INTERNAL_API_SECRET": "a-real-secret",
                "GATEWAY_IDENTITY_KEYS": placeholder,
                "GATEWAY_IDENTITY_ACTIVE_KEY": KID,
            }
        )
    assert "GATEWAY_IDENTITY_KEYS" in str(ei.value)
    assert placeholder not in str(ei.value)


@pytest.mark.parametrize(
    "keys,active",
    [("not json", KID), (RING, "gw-other")],
    ids=["malformed", "active-not-in-ring"],
)
def test_the_production_app_refuses_to_start_with_an_unusable_ring(
    monkeypatch, keys, active
):
    from gateway import adapters

    monkeypatch.setenv("INTERNAL_API_SECRET", "a-real-secret")
    monkeypatch.setenv("GATEWAY_IDENTITY_KEYS", keys)
    monkeypatch.setenv("GATEWAY_IDENTITY_ACTIVE_KEY", active)
    with pytest.raises(KeyRingError) as ei:
        adapters.build_production_app()
    assert "GATEWAY_IDENTITY_" in str(ei.value)


class _RecordingClient:
    def __init__(self):
        self.posts: list[dict] = []

    async def post(self, url, *, json=None, content=None, headers=None, timeout=None):
        self.posts.append(
            {
                "url": url,
                "json": json,
                "content": content,
                "headers": dict(headers or {}),
            }
        )

        class _R:
            status_code = 200

            @staticmethod
            def json():
                if url.endswith("/internal/validate"):
                    return {"user_id": 7, "scopes": ["tx"], "max_concurrent": 3}
                return {"authorized": [], "errors": []}

        return _R()


async def test_the_ws_subscribe_hop_is_signed_over_the_body_it_posts(signing):
    client = _RecordingClient()
    authorizer = AdminApiAuthorizer(
        client, "http://admin-api:8001", "http://meeting-api:8080"
    )
    meetings = [{"platform": "zoom", "native_meeting_id": "1"}]
    await authorizer.authorize_subscribe(VALID_KEY, meetings)
    hop = client.posts[-1]
    assert hop["url"] == "http://meeting-api:8080/ws/authorize-subscribe"
    assert hop["json"] is None
    assert json.loads(hop["content"]) == {"meetings": meetings}
    assert hop["headers"]["content-type"] == "application/json"
    _verify(
        hop["headers"][SIGNATURE_HEADER],
        KEY,
        "7",
        "POST",
        "/ws/authorize-subscribe",
        body=hop["content"],
        identity={"x-user-scopes": "tx", "x-user-limits": "3"},
    )
    assert hop["headers"]["x-user-scopes"] == "tx"
    assert hop["headers"]["x-user-limits"] == "3"


async def test_the_ws_subscribe_hop_is_not_made_without_the_ring(monkeypatch):
    monkeypatch.delenv("GATEWAY_IDENTITY_KEYS", raising=False)
    client = _RecordingClient()
    authorizer = AdminApiAuthorizer(
        client, "http://admin-api:8001", "http://meeting-api:8080"
    )
    result = await authorizer.authorize_subscribe(
        VALID_KEY, [{"platform": "zoom", "native_meeting_id": "1"}]
    )
    assert result["authorized"] == []
    assert "GATEWAY_IDENTITY_KEYS" in result["errors"][0]
    assert all(not p["url"].endswith("/ws/authorize-subscribe") for p in client.posts)
