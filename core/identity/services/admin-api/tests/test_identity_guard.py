"""Only the gateway can say who is calling (design §1.10).

A client route believes ``x-user-id`` only with a fresh ``x-gateway-signature`` over the request's
method and path. Unsigned, forged, stale, future, wrong-user and duplicated identities answer 401
before any route runs; so does every client request when ``GATEWAY_IDENTITY_SECRET`` isn't
configured. The operator's ``/admin/*`` surface, ``/internal/*`` and ``/health`` are exempt, and
every route of the app is checked against that list.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

import httpx
import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.routing import Route

from admin_api.app.identity_guard import MAX_SKEW_S, is_exempt, verify_signature
from admin_api.app.main import create_app
from gateway_identity import load_vectors, signature, signed_headers, via_gateway

VECTORS = load_vectors()
GUARD_BODY = {"detail": "the caller's identity must come from the gateway"}

#: Every route of the app that takes no caller from the gateway.
EXEMPT = {
    "/health",
    "/metrics",
    "/admin/instance",
    "/admin/tokens/{token_id}",
    "/admin/users",
    "/admin/users/email/{email}",
    "/admin/users/{user_id}",
    "/admin/users/{user_id}/settings/import",
    "/admin/users/{user_id}/tokens",
    "/internal/bootstrap-admin",
    "/internal/calendar-configs",
    "/internal/instance",
    "/internal/settings/{key}",
    "/internal/users/{user_id}/bot-context",
    "/internal/users/{user_id}/memberships",
    "/internal/users/{user_id}/memberships/{workspace_id}",
    "/internal/users/{user_id}/model-config",
    "/internal/users/{user_id}/settings",
    "/internal/users/{user_id}/webhook-subscriptions",
    "/internal/validate",
}


@pytest.mark.parametrize("case", VECTORS["verify_cases"], ids=lambda c: c["case"])
def test_the_verifier_agrees_with_the_shared_vectors(case):
    reason = verify_signature(
        VECTORS["secret"],
        case["user_id"],
        case["header"],
        case["method"],
        case["path"],
        case["now"],
    )
    assert (reason is None) is case["valid"], reason


def test_the_shared_skew_is_the_guards():
    assert VECTORS["max_skew_s"] == MAX_SKEW_S


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def test_a_signed_request_passes(client):
    r = client.get("/", headers=signed_headers(7, "GET", "/"))
    assert r.status_code == 200, r.text


def test_a_direct_call_with_a_bare_user_id_is_401(client):
    r = client.get("/", headers={"x-user-id": "1"})
    assert r.status_code == 401
    assert r.json() == GUARD_BODY


def test_a_key_alone_is_401(client):
    assert (
        client.get("/user/webhook", headers={"X-API-Key": "vxa_user_key"}).status_code
        == 401
    )


def test_a_forged_signature_is_401(client):
    forged = f"t={int(time.time())},v1={'0' * 64}"
    r = client.get("/", headers={"x-user-id": "7", "x-gateway-signature": forged})
    assert r.status_code == 401


@pytest.mark.parametrize(
    "offset", [-(MAX_SKEW_S + 1), MAX_SKEW_S + 1], ids=["stale", "future"]
)
def test_a_signature_outside_the_window_is_401(client, offset):
    sig = signature("7", "GET", "/", t=int(time.time()) + offset)
    r = client.get("/", headers={"x-user-id": "7", "x-gateway-signature": sig})
    assert r.status_code == 401


def test_a_signature_for_another_user_is_401(client):
    r = client.get(
        "/",
        headers={"x-user-id": "1", "x-gateway-signature": signature("7", "GET", "/")},
    )
    assert r.status_code == 401


def test_a_signature_for_another_route_is_401(client):
    sig = signature("7", "GET", "/v2/webhooks")
    r = client.get("/", headers={"x-user-id": "7", "x-gateway-signature": sig})
    assert r.status_code == 401


def test_a_duplicated_user_id_is_401(client):
    headers = [
        ("x-user-id", "7"),
        ("x-user-id", "1"),
        ("x-gateway-signature", signature("7", "GET", "/")),
    ]
    assert client.get("/", headers=httpx.Headers(headers)).status_code == 401


def test_without_the_secret_every_client_request_is_401(client, monkeypatch):
    monkeypatch.delenv("GATEWAY_IDENTITY_SECRET")
    assert client.get("/", headers=signed_headers(7, "GET", "/")).status_code == 401


def test_a_v2_route_answers_the_v2_error_shape(client):
    r = client.get("/v2/webhooks", headers={"x-user-id": "1"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"


def test_the_rejection_is_logged_without_the_signature(client, caplog):
    forged = f"t={int(time.time())},v1={'a' * 64}"
    with caplog.at_level(logging.WARNING, logger="admin_api.identity_guard"):
        client.get("/?x=1", headers={"x-user-id": "7", "x-gateway-signature": forged})
    assert "reason=mismatch method=GET path=/" in caplog.text
    assert "a" * 64 not in caplog.text


def test_the_stand_in_gateway_signs_what_it_forwards():
    r = TestClient(via_gateway(create_app())).get("/", headers={"x-user-id": "7"})
    assert r.status_code == 200, r.text


def test_the_operator_surface_needs_no_signature(client, monkeypatch):
    monkeypatch.setenv("ADMIN_API_TOKEN", "test-admin-token-unit")
    r = client.get("/admin/users/1", headers={"X-Admin-API-Key": "not-the-token"})
    assert r.status_code == 403
    assert r.json() != GUARD_BODY
    assert client.get("/health").status_code == 200


# ── every route of the app is either exempt or guarded ─────────────────────────────────────────


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "1", path)


def _routes(app: Any) -> list[tuple[str, str]]:
    """Every ``(method, path)`` the app serves, including routes of included routers (FastAPI keeps
    those nested, each with its include prefix)."""
    out = []

    def walk(routes: Any, prefix: str) -> None:
        for route in routes:
            nested = getattr(route, "original_router", None)
            if nested is not None:
                walk(nested.routes, prefix + route.include_context.prefix)
            elif isinstance(route, (APIRoute, Route)):
                for method in sorted(route.methods or ()):
                    if method != "HEAD":
                        out.append((method, prefix + route.path))

    walk(app.routes, "")
    return out


def test_the_exempt_routes_are_exactly_the_listed_ones():
    paths = {path for _, path in _routes(create_app())}
    assert {p for p in paths if is_exempt(p)} == EXEMPT
    assert {
        "/",
        "/user/calendar",
        "/user/webhook",
        "/v2/webhooks",
        "/openapi.json",
    } <= paths - EXEMPT


def test_every_other_route_refuses_an_unsigned_caller(client):
    for method, path in _routes(client.app):
        if path in EXEMPT:
            continue
        r = client.request(
            method, _concrete(path), headers={"x-user-id": "1", "X-API-Key": "k"}
        )
        assert r.status_code == 401, (method, path, r.status_code)
