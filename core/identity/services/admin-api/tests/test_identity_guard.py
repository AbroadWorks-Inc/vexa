"""Only the gateway can say who is calling (design §1.10).

A client route believes ``x-user-id`` only with a fresh ``x-gateway-signature`` (version ``v2``)
over the request's method, path, raw query and body (§6.9 F-E). Unsigned, forged, stale, future,
wrong-user, wrong-query, wrong-body, v1 and duplicated identities answer 401 before any route runs;
so does every client request when ``GATEWAY_IDENTITY_SECRET`` isn't configured. A signature under
``GATEWAY_IDENTITY_SECRET_PREVIOUS`` is accepted only while that key is set. The operator's ``/admin/*`` surface, ``/internal/*`` and ``/health`` are exempt, and
every route of the app is checked against that list.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

import httpx
import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.routing import Route

from admin_api.app.identity_guard import (
    MAX_SKEW_S,
    IdentityGuard,
    is_exempt,
    verify_signature,
)
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
    secrets = [VECTORS["secret"]]
    if case["previous_secret"] is not None:
        secrets.append(case["previous_secret"])
    reason = verify_signature(
        secrets,
        case["user_id"],
        case["header"],
        case["method"],
        case["path"],
        case["query"],
        case["body"].encode("utf-8"),
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
    forged = f"t={int(time.time())},v2={'0' * 64}"
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


def test_a_signed_query_passes(client):
    r = client.get("/?limit=5", headers=signed_headers(7, "GET", "/", query="limit=5"))
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("sent", ["/?limit=6", "/", "/?limit=5&x=1"])
def test_a_replay_with_another_query_is_401(client, sent):
    headers = signed_headers(7, "GET", "/", query="limit=5")
    assert client.get(sent, headers=headers).status_code == 401


BODY = b'{"url":"https://portal.example/hook","events":["meeting.completed"]}'


def test_a_signed_body_passes(client):
    headers = signed_headers(7, "GET", "/", body=BODY)
    assert client.request("GET", "/", headers=headers, content=BODY).status_code == 200


@pytest.mark.parametrize(
    "sent",
    [BODY.replace(b"hook", b"hool"), b"", BODY + b" "],
    ids=["one-byte", "dropped", "appended"],
)
def test_a_replay_with_another_body_is_401(client, sent):
    headers = signed_headers(7, "GET", "/", body=BODY)
    assert client.request("GET", "/", headers=headers, content=sent).status_code == 401


def test_a_v1_signature_is_401(client):
    v1 = "t={},v1={}".format(int(time.time()), "0" * 64)
    r = client.get("/", headers={"x-user-id": "7", "x-gateway-signature": v1})
    assert r.status_code == 401


PREVIOUS = "test-gateway-identity-previous-admin-api"


def test_the_previous_key_is_accepted_while_it_is_configured(client, monkeypatch):
    monkeypatch.setenv("GATEWAY_IDENTITY_SECRET_PREVIOUS", PREVIOUS)
    sig = signature("7", "GET", "/", secret=PREVIOUS)
    r = client.get("/", headers={"x-user-id": "7", "x-gateway-signature": sig})
    assert r.status_code == 200, r.text
    assert client.get("/", headers=signed_headers(7, "GET", "/")).status_code == 200


def test_the_previous_key_is_refused_once_it_is_dropped(client, monkeypatch):
    monkeypatch.delenv("GATEWAY_IDENTITY_SECRET_PREVIOUS", raising=False)
    sig = signature("7", "GET", "/", secret=PREVIOUS)
    r = client.get("/", headers={"x-user-id": "7", "x-gateway-signature": sig})
    assert r.status_code == 401


def test_the_previous_key_alone_accepts_nothing(client, monkeypatch):
    monkeypatch.delenv("GATEWAY_IDENTITY_SECRET")
    monkeypatch.setenv("GATEWAY_IDENTITY_SECRET_PREVIOUS", PREVIOUS)
    sig = signature("7", "GET", "/", secret=PREVIOUS)
    r = client.get("/", headers={"x-user-id": "7", "x-gateway-signature": sig})
    assert r.status_code == 401


async def _echo(scope, receive, send):
    chunks = []
    while True:
        message = await receive()
        chunks.append(message.get("body", b""))
        if not message.get("more_body", False):
            break
    body = b"".join(chunks)
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": body})


def _drive(app, headers, chunks, query=b""):
    """Call ``app`` over raw ASGI with the body in ``chunks``; returns (status, body, reads)."""
    pending = [
        {"type": "http.request", "body": c, "more_body": i < len(chunks) - 1}
        for i, c in enumerate(chunks)
    ]
    reads = []
    sent: list[dict] = []

    async def receive():
        reads.append(1)
        if pending:
            return pending.pop(0)
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/echo",
        "query_string": query,
        "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
    }
    asyncio.run(app(scope, receive, send))
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    body = b"".join(
        m.get("body", b"") for m in sent if m["type"] == "http.response.body"
    )
    return status, body, len(reads)


def test_a_body_in_chunks_is_hashed_whole_and_passed_on_intact():
    body = b'{"a":' + b"1" * 70000 + b"}"
    chunks = [body[:10], body[10:40000], body[40000:]]
    headers = signed_headers(7, "POST", "/echo", query="x=1", body=body)
    status, echoed, _ = _drive(IdentityGuard(_echo), headers, chunks, query=b"x=1")
    assert status == 200
    assert echoed == body


def test_a_chunked_body_with_one_byte_changed_is_401():
    body = b"0123456789" * 10
    headers = signed_headers(7, "POST", "/echo", body=body)
    tampered = [body[:50], b"X" + body[51:]]
    status, _, _ = _drive(IdentityGuard(_echo), headers, tampered)
    assert status == 401


def test_an_unsigned_request_is_refused_without_reading_its_body():
    status, _, reads = _drive(IdentityGuard(_echo), {"x-user-id": "7"}, [b"x" * 1000])
    assert status == 401
    assert reads == 0


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
    forged = f"t={int(time.time())},v2={'a' * 64}"
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
