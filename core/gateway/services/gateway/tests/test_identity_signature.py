"""The gateway signs the identity it forwards (design §1.10, §6.9 F-E).

Every forwarded request that carries ``x-user-id`` also carries ``x-gateway-signature`` (version
``v2``), made with ``GATEWAY_IDENTITY_SECRET`` over the forwarded user, ``x-user-scopes`` and
``x-user-limits`` values, method, path, raw query string and the SHA-256 of the exact body bytes
forwarded. A signature a client sends is never forwarded.
Without the secret the gateway refuses to start (preflight) and refuses to forward. The signing
rule is pinned by the shared vectors meeting-api and admin-api verify with.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from pathlib import Path
from typing import Any, Optional

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway import config_preflight as cp
from gateway import create_app
from gateway.adapters import AdminApiAuthorizer
from gateway.identity_signature import SIGNATURE_HEADER, sign, signed_target

from conftest import VALID_KEY, VALID_USER, FakeAuthorizer, FakeDownstream, FakeRedis

AUTH = {"x-api-key": VALID_KEY}
SECRET = "test-gateway-identity-secret-unit"
#: The x-user-scopes and x-user-limits the gateway forwards for ``VALID_USER``.
SCOPES = ",".join(VALID_USER["scopes"])
LIMITS = str(VALID_USER["max_concurrent"])


def _vectors() -> dict:
    rel = Path("gateway") / "contracts" / "gateway-identity" / "signature.vectors.json"
    for parent in Path(__file__).resolve().parents:
        if (parent / rel).is_file():
            return json.loads((parent / rel).read_text())
    raise FileNotFoundError(str(rel))


VECTORS = _vectors()


def _verify(
    header: str,
    secret: str,
    user_id: str,
    method: str,
    path: str,
    query: str = "",
    body: bytes = b"",
    *,
    scopes: str = SCOPES,
    limits: str = LIMITS,
) -> int:
    """Check ``header`` the way the receiving services do; returns its ``t``."""
    t_part, mac_part = header.split(",")
    assert t_part.startswith("t=") and mac_part.startswith("v2=")
    t = int(t_part[2:])
    fields = [
        "v2",
        str(t),
        user_id,
        scopes,
        limits,
        method,
        hashlib.sha256(body).hexdigest(),
        query,
        path,
    ]
    message = "\n".join(fields).encode()
    expected = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
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
        SECRET,
        user_id,
        sent.method,
        sent.url.path,
        sent.url.query.decode("ascii"),
        sent.content,
        scopes=last["headers"].get("x-user-scopes", ""),
        limits=last["headers"].get("x-user-limits", ""),
    )


@pytest.fixture()
def signing(monkeypatch):
    monkeypatch.setenv("GATEWAY_IDENTITY_SECRET", SECRET)


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
            str(vector["t"]),
            vector["user_id"],
            vector["scopes"],
            vector["limits"],
            vector["method"],
            vector["body_sha256"],
            vector["query"],
            vector["path"],
        ]
    )
    got = sign(
        VECTORS["secret"],
        vector["user_id"],
        vector["scopes"],
        vector["limits"],
        vector["method"],
        vector["path"],
        vector["query"],
        body,
        vector["t"],
    )
    assert got == vector["header"]


def test_the_vectors_cover_query_body_and_rotation():
    vectors = VECTORS["vectors"]
    assert any(v["query"] for v in vectors) and any(not v["query"] for v in vectors)
    assert any(v["body"] for v in vectors) and any(not v["body"] for v in vectors)
    for field in ("scopes", "limits"):
        assert any(v[field] for v in vectors) and any(not v[field] for v in vectors)
    cases = VECTORS["verify_cases"]
    assert any(c["previous_secret"] and c["valid"] for c in cases)
    assert any(
        c["previous_secret"] is None and "previous" in c["case"] and not c["valid"]
        for c in cases
    )


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
    _verify(
        downstream.last["headers"][SIGNATURE_HEADER], SECRET, "7", "GET", "/meetings"
    )


def test_the_query_signed_is_the_query_forwarded(signing):
    client, downstream = _client()
    r = client.get("/meetings?limit=5&x=%2F", headers=AUTH)
    assert r.status_code == 200
    _verify_as_sent(downstream.last)
    _verify(
        downstream.last["headers"][SIGNATURE_HEADER],
        SECRET,
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
        SECRET,
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
    _verify(headers[SIGNATURE_HEADER], SECRET, "7", "GET", "/meetings")
    with pytest.raises(AssertionError):
        _verify(
            headers[SIGNATURE_HEADER],
            SECRET,
            "7",
            "GET",
            "/meetings",
            scopes="bot,tx,export",
        )
    with pytest.raises(AssertionError):
        _verify(headers[SIGNATURE_HEADER], SECRET, "7", "GET", "/meetings", limits="45")


def test_a_forward_to_admin_api_is_signed_over_the_admin_api_path(signing):
    client, downstream = _client()
    assert client.get("/user/webhook", headers=AUTH).status_code == 200
    assert downstream.last["url"].endswith("/user/webhook")
    _verify(
        downstream.last["headers"][SIGNATURE_HEADER],
        SECRET,
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
    forged = sign(SECRET, "1", "", "", "GET", "/meetings", "", b"", int(time.time()))
    r = client.get(
        "/meetings", headers={**AUTH, SIGNATURE_HEADER: forged, "x-user-id": "1"}
    )
    assert r.status_code == 200
    headers = downstream.last["headers"]
    assert headers["x-user-id"] == "7"
    assert headers[SIGNATURE_HEADER] != forged
    _verify_as_sent(downstream.last)


def test_the_gateway_signs_with_the_current_key_only(signing, monkeypatch):
    monkeypatch.setenv("GATEWAY_IDENTITY_SECRET_PREVIOUS", "test-previous-key")
    client, downstream = _client()
    assert client.get("/meetings", headers=AUTH).status_code == 200
    _verify_as_sent(downstream.last)


def test_without_the_secret_the_gateway_refuses_to_forward(monkeypatch):
    monkeypatch.delenv("GATEWAY_IDENTITY_SECRET", raising=False)
    client, downstream = _client()
    r = client.get("/meetings", headers=AUTH)
    assert r.status_code == 503
    assert "GATEWAY_IDENTITY_SECRET" in r.json()["detail"]
    assert downstream.last is None


def test_preflight_refuses_boot_without_the_identity_secret():
    with pytest.raises(cp.ConfigError) as ei:
        cp.preflight({"INTERNAL_API_SECRET": "a-real-secret"})
    assert "GATEWAY_IDENTITY_SECRET" in str(ei.value)


@pytest.mark.parametrize("placeholder", ["<REPLACE_ME>", "changeme"])
def test_preflight_refuses_a_placeholder_identity_secret(placeholder):
    with pytest.raises(cp.ConfigError) as ei:
        cp.preflight(
            {
                "INTERNAL_API_SECRET": "a-real-secret",
                "GATEWAY_IDENTITY_SECRET": placeholder,
            }
        )
    assert "GATEWAY_IDENTITY_SECRET" in str(ei.value)
    assert placeholder not in str(ei.value)


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
        SECRET,
        "7",
        "POST",
        "/ws/authorize-subscribe",
        body=hop["content"],
        scopes="tx",
        limits="3",
    )
    assert hop["headers"]["x-user-scopes"] == "tx"
    assert hop["headers"]["x-user-limits"] == "3"


async def test_the_ws_subscribe_hop_is_not_made_without_the_secret(monkeypatch):
    monkeypatch.delenv("GATEWAY_IDENTITY_SECRET", raising=False)
    client = _RecordingClient()
    authorizer = AdminApiAuthorizer(
        client, "http://admin-api:8001", "http://meeting-api:8080"
    )
    result = await authorizer.authorize_subscribe(
        VALID_KEY, [{"platform": "zoom", "native_meeting_id": "1"}]
    )
    assert result["authorized"] == []
    assert "GATEWAY_IDENTITY_SECRET" in result["errors"][0]
    assert all(not p["url"].endswith("/ws/authorize-subscribe") for p in client.posts)
