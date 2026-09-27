"""The gateway signs the identity it forwards (design §1.10).

Every forwarded request that carries ``x-user-id`` also carries ``x-gateway-signature``, made with
``GATEWAY_IDENTITY_SECRET`` over the forwarded method and path. A signature a client sends is
never forwarded. Without the secret the gateway refuses to start (preflight) and refuses to
forward. The signing rule is pinned by the shared vectors meeting-api and admin-api verify with.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gateway import config_preflight as cp
from gateway import create_app
from gateway.adapters import AdminApiAuthorizer
from gateway.identity_signature import SIGNATURE_HEADER, sign, signed_path

from conftest import VALID_KEY, VALID_USER, FakeAuthorizer, FakeDownstream, FakeRedis

AUTH = {"x-api-key": VALID_KEY}
SECRET = "test-gateway-identity-secret-unit"


def _vectors() -> dict:
    rel = Path("gateway") / "contracts" / "gateway-identity" / "signature.vectors.json"
    for parent in Path(__file__).resolve().parents:
        if (parent / rel).is_file():
            return json.loads((parent / rel).read_text())
    raise FileNotFoundError(str(rel))


VECTORS = _vectors()


def _verify(header: str, secret: str, user_id: str, method: str, path: str) -> int:
    """Check ``header`` the way the receiving services do; returns its ``t``."""
    t_part, v1_part = header.split(",")
    assert t_part.startswith("t=") and v1_part.startswith("v1=")
    t = int(t_part[2:])
    message = f"{t}.{user_id}.{method}.{path}".encode()
    expected = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    assert hmac.compare_digest(v1_part[3:], expected)
    return t


@pytest.fixture()
def signing(monkeypatch):
    monkeypatch.setenv("GATEWAY_IDENTITY_SECRET", SECRET)


def _client(downstream=None):
    downstream = downstream or FakeDownstream(status_code=200, body={"ok": True})
    return TestClient(create_app(FakeAuthorizer(), downstream, FakeRedis())), downstream


@pytest.mark.parametrize(
    "vector", VECTORS["vectors"], ids=lambda v: f"{v['method']} {v['path']}"
)
def test_signer_reproduces_the_shared_vectors(vector):
    assert (
        vector["message"]
        == f"{vector['t']}.{vector['user_id']}.{vector['method']}.{vector['path']}"
    )
    got = sign(
        VECTORS["secret"],
        vector["user_id"],
        vector["method"],
        vector["path"],
        vector["t"],
    )
    assert got == vector["header"]


def test_signed_path_is_the_decoded_path_without_the_query():
    assert signed_path("http://meeting-api:8080/meetings") == "/meetings"
    assert signed_path("http://meeting-api:8080/meetings?limit=5&x=%2F") == "/meetings"
    assert (
        signed_path("http://meeting-api:8080/meetings/teams/room%20one")
        == "/meetings/teams/room one"
    )
    assert (
        signed_path("http://admin-api:8001/v2/webhooks/7/test") == "/v2/webhooks/7/test"
    )


def test_a_forwarded_request_carries_a_fresh_signature_over_its_downstream_path(
    signing,
):
    client, downstream = _client()
    before = int(time.time())
    r = client.get("/meetings?limit=5", headers=AUTH)
    assert r.status_code == 200
    headers = downstream.last["headers"]
    assert headers["x-user-id"] == str(VALID_USER["user_id"])
    t = _verify(
        headers[SIGNATURE_HEADER], SECRET, headers["x-user-id"], "GET", "/meetings"
    )
    assert before <= t <= int(time.time())


def test_the_method_signed_is_the_forwarded_method(signing):
    client, downstream = _client()
    assert client.post("/bots", headers=AUTH, json={}).status_code == 200
    _verify(downstream.last["headers"][SIGNATURE_HEADER], SECRET, "7", "POST", "/bots")


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
    _verify(
        downstream.last["headers"][SIGNATURE_HEADER],
        SECRET,
        "7",
        "POST",
        f"/v2/meetings/{uuid}/export",
    )


def test_a_path_parameter_is_signed_decoded(signing):
    client, downstream = _client()
    r = client.get("/transcripts/teams/room%20one", headers=AUTH)
    assert r.status_code == 200
    path = signed_path(downstream.last["url"])
    assert path == "/transcripts/teams/room one"
    _verify(downstream.last["headers"][SIGNATURE_HEADER], SECRET, "7", "GET", path)


def test_a_client_supplied_signature_is_never_forwarded(signing):
    client, downstream = _client()
    forged = sign(SECRET, "1", "GET", "/meetings", int(time.time()))
    r = client.get(
        "/meetings", headers={**AUTH, SIGNATURE_HEADER: forged, "x-user-id": "1"}
    )
    assert r.status_code == 200
    headers = downstream.last["headers"]
    assert headers["x-user-id"] == "7"
    assert headers[SIGNATURE_HEADER] != forged
    _verify(headers[SIGNATURE_HEADER], SECRET, "7", "GET", "/meetings")


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

    async def post(self, url, *, json=None, headers=None, timeout=None):
        self.posts.append({"url": url, "json": json, "headers": dict(headers or {})})

        class _R:
            status_code = 200

            @staticmethod
            def json():
                if url.endswith("/internal/validate"):
                    return {"user_id": 7, "scopes": ["tx"], "max_concurrent": 3}
                return {"authorized": [], "errors": []}

        return _R()


async def test_the_ws_subscribe_hop_is_signed(signing):
    client = _RecordingClient()
    authorizer = AdminApiAuthorizer(
        client, "http://admin-api:8001", "http://meeting-api:8080"
    )
    await authorizer.authorize_subscribe(
        VALID_KEY, [{"platform": "zoom", "native_meeting_id": "1"}]
    )
    hop = client.posts[-1]
    assert hop["url"] == "http://meeting-api:8080/ws/authorize-subscribe"
    _verify(
        hop["headers"][SIGNATURE_HEADER], SECRET, "7", "POST", "/ws/authorize-subscribe"
    )


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
