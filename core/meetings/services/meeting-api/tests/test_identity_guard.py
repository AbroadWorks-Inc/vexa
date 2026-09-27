"""Only the gateway can say who is calling (design §1.10).

A client route believes ``x-user-id`` only with a fresh ``x-gateway-signature`` over the request's
method and path. Unsigned, forged, stale, future, wrong-user, wrong-route and duplicated identities
answer 401 before any route runs; so does every client request when ``GATEWAY_IDENTITY_SECRET``
isn't configured. The exempt routes are listed, and every route of the production app is checked
against that list.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

import httpx
import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.routing import Route

import meeting_api.__main__ as main_mod
from gateway_identity import (
    load_vectors,
    signature,
    signed_headers,
    via_gateway,
)
from meeting_api import create_app
from meeting_api.identity_guard import MAX_SKEW_S, is_exempt, verify_signature

VECTORS = load_vectors()

#: Every route of the production app that takes no caller from the gateway.
EXEMPT = {
    "/health",
    "/bots/internal/callback/lifecycle",
    "/runtime/callback",
    "/internal/recordings/upload",
    "/internal/webhooks/test",
    "/metrics",
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
    r = client.get("/meetings", headers=signed_headers(7, "GET", "/meetings"))
    assert r.status_code == 200, r.text


def test_a_direct_call_with_a_bare_user_id_is_401(client):
    r = client.get("/meetings", headers={"x-user-id": "1"})
    assert r.status_code == 401
    assert r.json() == {"detail": "the caller's identity must come from the gateway"}


def test_no_identity_at_all_is_401(client):
    assert client.get("/meetings").status_code == 401


def test_a_forged_signature_is_401(client):
    forged = f"t={int(time.time())},v1={'0' * 64}"
    r = client.get(
        "/meetings", headers={"x-user-id": "7", "x-gateway-signature": forged}
    )
    assert r.status_code == 401


def test_a_signature_made_with_another_secret_is_401(client):
    sig = signature("7", "GET", "/meetings", secret="not-the-gateways-secret")
    r = client.get("/meetings", headers={"x-user-id": "7", "x-gateway-signature": sig})
    assert r.status_code == 401


@pytest.mark.parametrize(
    "offset", [-(MAX_SKEW_S + 1), MAX_SKEW_S + 1], ids=["stale", "future"]
)
def test_a_signature_outside_the_window_is_401(client, offset):
    sig = signature("7", "GET", "/meetings", t=int(time.time()) + offset)
    r = client.get("/meetings", headers={"x-user-id": "7", "x-gateway-signature": sig})
    assert r.status_code == 401


def test_a_signature_for_another_user_is_401(client):
    r = client.get(
        "/meetings",
        headers={
            "x-user-id": "1",
            "x-gateway-signature": signature("7", "GET", "/meetings"),
        },
    )
    assert r.status_code == 401


@pytest.mark.parametrize(
    "method,path",
    [("POST", "/meetings"), ("GET", "/meetings/1")],
    ids=["method", "path"],
)
def test_a_signature_for_another_route_is_401(client, method, path):
    r = client.get(
        "/meetings",
        headers={"x-user-id": "7", "x-gateway-signature": signature("7", method, path)},
    )
    assert r.status_code == 401


def test_the_query_string_is_not_signed(client):
    r = client.get("/meetings?limit=5", headers=signed_headers(7, "GET", "/meetings"))
    assert r.status_code == 200, r.text


def test_a_duplicated_user_id_is_401(client):
    headers = [
        ("x-user-id", "7"),
        ("x-user-id", "1"),
        ("x-gateway-signature", signature("7", "GET", "/meetings")),
    ]
    r = client.get("/meetings", headers=httpx.Headers(headers))
    assert r.status_code == 401


def test_without_the_secret_every_client_request_is_401(client, monkeypatch):
    monkeypatch.delenv("GATEWAY_IDENTITY_SECRET")
    r = client.get("/meetings", headers=signed_headers(7, "GET", "/meetings"))
    assert r.status_code == 401


def test_a_v2_route_answers_the_v2_error_shape(client):
    r = client.get("/v2/meetings", headers={"x-user-id": "1"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"


def test_the_rejection_is_logged_without_the_signature(client, capsys):
    forged = f"t={int(time.time())},v1={'a' * 64}"
    client.get(
        "/meetings?x=1", headers={"x-user-id": "7", "x-gateway-signature": forged}
    )
    lines = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("{")
    ]
    rejected = [line for line in lines if line.get("event") == "identity_rejected"]
    assert rejected and rejected[-1]["fields"] == {
        "reason": "mismatch",
        "method": "GET",
        "path": "/meetings",
    }
    assert "a" * 64 not in json.dumps(lines)


def test_the_stand_in_gateway_signs_what_it_forwards():
    r = TestClient(via_gateway(create_app())).get(
        "/meetings", headers={"x-user-id": "7"}
    )
    assert r.status_code == 200, r.text


def test_an_exempt_route_needs_no_signature(client):
    assert client.get("/health").status_code == 200
    # The route's own check answers, not the guard: the internal secret is not configured here.
    r = client.post("/internal/webhooks/test", json={})
    assert r.status_code != 401


# ── every route of the production app is either exempt or guarded ─────────────────────────────


@pytest.fixture
def production(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(main_mod, "_require_config", lambda env=None: None)
    monkeypatch.setattr(main_mod, "_attach_background_loops", lambda *a, **k: None)
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+asyncpg://nobody:dummy@127.0.0.1:1/none"
    )
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin-token")
    monkeypatch.setenv("ADMIN_API_URL", "http://admin-api.test")
    monkeypatch.setenv("INTERNAL_API_SECRET", "test-internal-secret")
    return main_mod.build_production_app()


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


def test_the_exempt_routes_are_exactly_the_listed_ones(production):
    paths = {path for _, path in _routes(production)}
    assert {p for p in paths if is_exempt(p)} == EXEMPT
    assert {
        "/bots",
        "/meetings",
        "/recordings",
        "/ws/authorize-subscribe",
        "/v2/entries",
        "/v2/meetings/{meeting_id}",
        "/openapi.json",
    } <= paths - EXEMPT


def test_every_other_route_refuses_an_unsigned_caller(production):
    client = TestClient(production)
    for method, path in _routes(production):
        if path in EXEMPT:
            continue
        r = client.request(method, _concrete(path), headers={"x-user-id": "1"})
        assert r.status_code == 401, (method, path, r.status_code)
