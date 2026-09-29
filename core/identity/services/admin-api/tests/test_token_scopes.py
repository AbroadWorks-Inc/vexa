"""The least-privilege scopes of §1.10 — ``webhooks``, ``erase``, ``export`` — end to end in
admin-api: minted, stored, and returned by ``/internal/validate``, the hop the gateway authorizes
every request with. The gateway half (the scope each route requires) is ``routes.v1.json``; the
last test reads it to check a minted key against the routes it should and shouldn't reach.

Minting and validation run against a real Postgres when ``MEETING_API_TEST_DATABASE_URL`` is set.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from admin_api.token_scope import (
    USER_TIER_SCOPES,
    VALID_SCOPES,
    generate_prefixed_token,
    parse_token_scope,
)
from gateway_identity import via_gateway

PG_URL = os.getenv("MEETING_API_TEST_DATABASE_URL")
ADMIN_TOKEN = "test-admin-token"
INTERNAL_SECRET = "test-internal-secret"
ROUTES = json.loads(
    (Path(__file__).resolve().parents[3] / "routes.v1.json").read_text()
)["routes"]
WEBHOOK_ROUTES = [r for r in ROUTES if r["path"].startswith("/v2/webhooks")]


def test_the_scope_vocabulary_holds_the_least_privilege_scopes():
    assert VALID_SCOPES == {"bot", "tx", "browser", "webhooks", "erase", "export"}


def test_the_user_tier_keeps_its_own_scopes():
    """§1.10 least privilege: the /user/* tier answers bot, tx and browser keys only."""
    assert USER_TIER_SCOPES == {"bot", "tx", "browser"}
    assert USER_TIER_SCOPES < VALID_SCOPES


@pytest.mark.parametrize("scope", ["webhooks", "erase", "export"])
def test_a_new_scope_mints_a_prefixed_token(scope):
    token = generate_prefixed_token(scope)
    assert token.startswith(f"vxa_{scope}_")
    assert parse_token_scope(token) == scope


def test_an_unknown_scope_still_does_not_mint():
    with pytest.raises(ValueError):
        generate_prefixed_token("webhook")


def test_every_webhook_route_requires_the_webhooks_scope():
    assert len(WEBHOOK_ROUTES) == 7
    assert all(r["scopes"] == ["webhooks"] for r in WEBHOOK_ROUTES)


needs_pg = pytest.mark.skipif(
    not PG_URL,
    reason="real-Postgres mint + validate; set MEETING_API_TEST_DATABASE_URL",
)


@pytest.fixture()
def client(monkeypatch):
    if not PG_URL:
        pytest.skip("MEETING_API_TEST_DATABASE_URL is not set")
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine, text

    from admin_api.app import db as app_db
    from admin_api.app.main import create_app
    from admin_api.schema.models import Base
    from admin_api.schema.sync import ensure_schema_sync

    engine = create_engine(PG_URL.replace("+asyncpg", "+psycopg"))
    ensure_schema_sync(engine, Base)
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE api_tokens, users RESTART IDENTITY CASCADE"))
        conn.execute(
            text(
                "INSERT INTO users (id, email, max_concurrent_bots, data, created_at) "
                "VALUES (1, 'ops@example.com', 3, '{}'::jsonb, now())"
            )
        )
    engine.dispose()
    monkeypatch.setenv("ADMIN_API_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("INTERNAL_API_SECRET", INTERNAL_SECRET)
    app_db.configure(PG_URL)
    with TestClient(via_gateway(create_app())) as c:
        yield c
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(app_db.get_engine().dispose())
    except Exception:
        pass
    finally:
        loop.close()
    # User 1 was inserted by id past a restarted sequence: remove it, so a later test that
    # inserts a user by the sequence (test_metrics) never collides with it.
    engine = create_engine(PG_URL.replace("+asyncpg", "+psycopg"))
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE api_tokens, users RESTART IDENTITY CASCADE"))
    engine.dispose()


def _mint(client, **kwargs):
    return client.post(
        "/admin/users/1/tokens", headers={"X-Admin-API-Key": ADMIN_TOKEN}, **kwargs
    )


def _validate(client, token):
    return client.post(
        "/internal/validate",
        json={"token": token},
        headers={"X-Internal-Secret": INTERNAL_SECRET},
    )


@needs_pg
@pytest.mark.parametrize(
    "scopes",
    [["webhooks"], ["erase"], ["export"], ["webhooks", "erase"], ["tx", "export"]],
)
def test_the_new_scopes_mint_and_validate(client, scopes):
    r = _mint(client, json={"scopes": scopes, "name": "operator"})
    assert r.status_code == 201, r.text
    assert r.json()["scopes"] == scopes
    token = r.json()["token"]
    assert token.startswith(f"vxa_{scopes[0]}_")

    v = _validate(client, token)
    assert v.status_code == 200
    assert v.json()["scopes"] == scopes


@needs_pg
def test_the_query_form_mints_them_too(client):
    r = _mint(client, params={"scopes": "webhooks,erase"})
    assert r.status_code == 201
    assert r.json()["scopes"] == ["webhooks", "erase"]


@needs_pg
def test_a_misspelled_scope_is_still_refused(client):
    r = _mint(client, json={"scopes": ["webhook"]})
    assert r.status_code == 422


@needs_pg
def test_a_webhooks_key_reaches_the_webhook_routes_and_a_bot_tx_key_does_not(client):
    """What /internal/validate hands the gateway, against the scope each route declares: the
    gateway admits a request when the two intersect."""
    webhooks_key = _mint(client, json={"scopes": ["webhooks"]}).json()["token"]
    bot_tx_key = _mint(client, json={"scopes": ["bot", "tx"]}).json()["token"]
    webhooks_scopes = set(_validate(client, webhooks_key).json()["scopes"])
    bot_tx_scopes = set(_validate(client, bot_tx_key).json()["scopes"])

    for route in WEBHOOK_ROUTES:
        required = set(route["scopes"])
        assert webhooks_scopes & required, route
        assert not bot_tx_scopes & required, route
    for route in ROUTES:
        if route not in WEBHOOK_ROUTES:
            assert not webhooks_scopes & set(route["scopes"]), route


@needs_pg
@pytest.mark.parametrize(
    "scopes,status", [(["webhooks"], 403), (["erase", "export"], 403), (["bot"], 200)]
)
def test_the_user_tier_refuses_a_key_with_only_least_privilege_scopes(
    client, scopes, status
):
    token = _mint(client, json={"scopes": scopes}).json()["token"]
    r = client.get("/user/webhook", headers={"X-API-Key": token, "x-user-id": "1"})
    assert r.status_code == status, r.text
