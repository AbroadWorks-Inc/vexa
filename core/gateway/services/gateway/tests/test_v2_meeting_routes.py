"""The meeting intake routes (§2.1) are forwarded to meeting-api verbatim: method, path, query and
body, with the identity headers the edge sets. The meeting id is re-encoded as one opaque segment,
like every other path parameter the edge forwards."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from conftest import VALID_KEY, FakeAuthorizer, FakeDownstream, FakeRedis
from gateway import ROUTE_SCOPES, create_app, routes_manifest

AUTH = {"x-api-key": VALID_KEY}
UUID = "5f0c2b7e-8d1a-4c3e-9b6f-2a7d1e4c8b90"
ALL_SCOPES = ["bot", "tx", "erase"]


def _gateway():
    downstream = FakeDownstream(status_code=200, body={"ok": True})
    app = create_app(
        FakeAuthorizer(user={"user_id": 7, "scopes": ALL_SCOPES, "max_concurrent": 3}),
        downstream,
        FakeRedis(),
    )
    return TestClient(app), downstream


@pytest.mark.parametrize(
    "method,url,downstream_path,params",
    [
        ("PUT", "/v2/entries", "/v2/entries", None),
        ("POST", "/v2/entries/remove", "/v2/entries/remove", None),
        (
            "GET",
            "/v2/entries?user=a%40abroadworks.com&limit=50",
            "/v2/entries",
            {"user": "a@abroadworks.com", "limit": "50"},
        ),
        (
            "GET",
            "/v2/meetings?user=a%40abroadworks.com&status=completed",
            "/v2/meetings",
            {"user": "a@abroadworks.com", "status": "completed"},
        ),
        ("GET", f"/v2/meetings/{UUID}", f"/v2/meetings/{UUID}", None),
        ("POST", f"/v2/meetings/{UUID}/stop", f"/v2/meetings/{UUID}/stop", None),
        ("DELETE", f"/v2/meetings/{UUID}", f"/v2/meetings/{UUID}", None),
    ],
)
def test_each_v2_route_is_forwarded_verbatim(method, url, downstream_path, params):
    client, downstream = _gateway()
    body = b'{"external_id":"x"}' if method in ("PUT", "POST") else b""

    r = client.request(method, url, headers=AUTH, content=body)

    assert r.status_code == 200
    assert downstream.last["method"] == method
    assert downstream.last["url"].endswith(downstream_path)
    assert downstream.last["params"] == params
    assert downstream.last["content"] == body
    assert downstream.last["headers"]["x-user-id"] == "7"


def test_the_meeting_id_is_forwarded_as_one_opaque_segment():
    client, downstream = _gateway()
    r = client.get("/v2/meetings/a%3Fuser%3Dother", headers=AUTH)
    assert r.status_code == 200
    assert downstream.last["url"].endswith("/v2/meetings/a%3Fuser%3Dother")
    assert downstream.last["params"] is None


def test_the_v2_scopes_are_as_section_2_1():
    assert ROUTE_SCOPES[("PUT", "/v2/entries")] == {"bot"}
    assert ROUTE_SCOPES[("POST", "/v2/entries/remove")] == {"bot"}
    assert ROUTE_SCOPES[("GET", "/v2/entries")] == {"bot"}
    assert ROUTE_SCOPES[("GET", "/v2/meetings")] == {"tx"}
    assert ROUTE_SCOPES[("GET", "/v2/meetings/{meeting_id}")] == {"tx"}
    assert ROUTE_SCOPES[("POST", "/v2/meetings/{meeting_id}/stop")] == {"bot"}
    assert ROUTE_SCOPES[("DELETE", "/v2/meetings/{meeting_id}")] == {"erase"}


def test_the_scope_vocabulary_names_the_least_privilege_scopes():
    """§1.10: ``webhooks``, ``erase`` and ``export`` are real scopes a manifest may name."""
    assert routes_manifest.SCOPES == {
        "bot",
        "tx",
        "browser",
        "erase",
        "webhooks",
        "export",
    }
