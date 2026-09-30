"""The webhook subscription routes (§2.7) are forwarded to admin-api verbatim: method, path, query
and body, with the identity headers the edge sets. Only a ``webhooks`` key reaches them (§1.10).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from conftest import VALID_KEY, FakeAuthorizer, FakeDownstream, FakeRedis
from gateway import ROUTE_SCOPES, create_app

AUTH = {"x-api-key": VALID_KEY}
SID = "0d7c9f2e-5b1a-4e3c-8f6d-2a9b1c4e7f30"
ADMIN = "http://admin-api"

ROUTES = [
    ("POST", "/v2/webhooks", "/v2/webhooks", None),
    ("GET", "/v2/webhooks", "/v2/webhooks", None),
    ("PATCH", f"/v2/webhooks/{SID}", f"/v2/webhooks/{SID}", None),
    ("DELETE", f"/v2/webhooks/{SID}", f"/v2/webhooks/{SID}", None),
    (
        "POST",
        f"/v2/webhooks/{SID}/rotate-secret",
        f"/v2/webhooks/{SID}/rotate-secret",
        None,
    ),
    ("POST", f"/v2/webhooks/{SID}/test", f"/v2/webhooks/{SID}/test", None),
    (
        "GET",
        f"/v2/webhooks/{SID}/deliveries?limit=20&before=41",
        f"/v2/webhooks/{SID}/deliveries",
        {"limit": "20", "before": "41"},
    ),
]


def _gateway(scopes):
    downstream = FakeDownstream(status_code=200, body={"ok": True})
    app = create_app(
        FakeAuthorizer(user={"user_id": 7, "scopes": scopes, "max_concurrent": 3}),
        downstream,
        FakeRedis(),
    )
    return TestClient(app), downstream


@pytest.mark.parametrize("method,url,downstream_path,params", ROUTES)
def test_each_webhook_route_is_forwarded_to_admin_api_verbatim(
    method, url, downstream_path, params
):
    client, downstream = _gateway(["webhooks"])
    body = (
        b'{"url":"https://hooks.example.com/aw"}'
        if method in ("POST", "PATCH")
        else b""
    )

    r = client.request(method, url, headers=AUTH, content=body)

    assert r.status_code == 200
    assert downstream.last["method"] == method
    assert downstream.last["url"] == f"{ADMIN}{downstream_path}"
    assert downstream.last["params"] == params
    assert downstream.last["content"] == body
    assert downstream.last["headers"]["x-user-id"] == "7"


@pytest.mark.parametrize("method,url,_path,_params", ROUTES)
@pytest.mark.parametrize(
    "scopes", [["bot", "tx"], ["bot", "tx", "browser"], ["erase"], ["export"]]
)
def test_a_key_without_the_webhooks_scope_is_refused(
    method, url, _path, _params, scopes
):
    client, downstream = _gateway(scopes)
    r = client.request(method, url, headers=AUTH, content=b"{}")
    assert r.status_code == 403
    assert downstream.last is None


def test_the_subscription_id_is_forwarded_as_one_opaque_segment():
    client, downstream = _gateway(["webhooks"])
    r = client.get("/v2/webhooks/a%3Fuser%3Dother/deliveries", headers=AUTH)
    assert r.status_code == 200
    assert downstream.last["url"] == f"{ADMIN}/v2/webhooks/a%3Fuser%3Dother/deliveries"
    assert downstream.last["params"] is None
    client.delete("/v2/webhooks/%2E%2E", headers=AUTH)
    assert downstream.last["url"] == f"{ADMIN}/v2/webhooks/%2E%2E"


def test_the_webhook_scopes_are_as_section_2_7():
    for method, template in [
        ("POST", "/v2/webhooks"),
        ("GET", "/v2/webhooks"),
        ("PATCH", "/v2/webhooks/{subscription_id}"),
        ("DELETE", "/v2/webhooks/{subscription_id}"),
        ("POST", "/v2/webhooks/{subscription_id}/rotate-secret"),
        ("POST", "/v2/webhooks/{subscription_id}/test"),
        ("GET", "/v2/webhooks/{subscription_id}/deliveries"),
    ]:
        assert ROUTE_SCOPES[(method, template)] == {"webhooks"}, (method, template)
