"""§2.1 — meeting-api's production app mounts the ``/v2`` intake routes over the production ports.

``__main__.build_production_app`` is built for real, with the boot's config check skipped and a
database and redis that are never reached: no request here needs either. The routes exist and
answer (a body that fails validation is 400, a missing identity 401), and the entry service behind
them is the production one: ``IntakeStop`` over the bot command bus, ``ExactRowSpawn`` under the
same service authority ``POST /bots`` uses with the auto-join sweep's spawn context, and the
outbox-only publisher. The scheduler runs on that same entry service. The bare app factory mounts
no ``/v2`` route (it has no Postgres intake store).
"""

from __future__ import annotations

from typing import Any

import pytest

import meeting_api.__main__ as main_mod
from intake_builders import conforms, http
from meeting_api import create_app
from meeting_api.intake import ExactRowSpawn, IntakeStop
from meeting_api.intake.sweeps import OutboxOnly

V2_ROUTES = {
    ("PUT", "/v2/entries"),
    ("POST", "/v2/entries/remove"),
    ("GET", "/v2/entries"),
    ("GET", "/v2/meetings"),
    ("GET", "/v2/meetings/{meeting_id}"),
    ("POST", "/v2/meetings/{meeting_id}/stop"),
    ("DELETE", "/v2/meetings/{meeting_id}"),
}
ACCOUNT = {"x-user-id": "1"}


def _routes(app: Any) -> set[tuple[str, str]]:
    """Every ``(method, path)`` the app serves, read from its OpenAPI document (included routers
    are nested objects in ``app.routes``)."""
    return {
        (method.upper(), path)
        for path, operations in app.openapi()["paths"].items()
        for method in operations
    }


@pytest.fixture
def production(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, dict[str, Any]]:
    """The production app, and the keyword arguments its background loops were attached with."""
    monkeypatch.setattr(main_mod, "_require_config", lambda env=None: None)
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+asyncpg://nobody:dummy@127.0.0.1:1/none"
    )
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin-token")
    monkeypatch.setenv("ADMIN_API_URL", "http://admin-api.test")
    monkeypatch.setenv("INTERNAL_API_SECRET", "test-internal-secret")
    attached: dict[str, Any] = {}
    real_attach = main_mod._attach_background_loops

    def attach(app: Any, *args: Any, **kwargs: Any) -> None:
        attached.update(kwargs)
        real_attach(app, *args, **kwargs)

    monkeypatch.setattr(main_mod, "_attach_background_loops", attach)
    return main_mod.build_production_app(), attached


def test_the_production_app_mounts_every_v2_route(production):
    app, _ = production
    assert V2_ROUTES <= _routes(app)


def test_the_bare_app_factory_mounts_no_v2_route():
    assert not {path for _, path in _routes(create_app())} & {p for _, p in V2_ROUTES}


@pytest.mark.parametrize(
    "method,path,body,headers,status,code",
    [
        ("PUT", "/v2/entries", {}, ACCOUNT, 400, "invalid_request"),
        ("POST", "/v2/entries/remove", {}, ACCOUNT, 400, "invalid_request"),
        ("GET", "/v2/entries?user=not-an-email", None, ACCOUNT, 400, "invalid_request"),
        ("PUT", "/v2/entries", {}, {}, 401, "unauthorized"),
        ("POST", "/v2/meetings/any/stop", None, {}, 401, "unauthorized"),
        ("DELETE", "/v2/meetings/any", None, {}, 401, "unauthorized"),
    ],
)
async def test_the_mounted_routes_answer(
    production, method, path, body, headers, status, code
):
    app, _ = production
    async with http(app) as client:
        r = await client.request(method, path, json=body, headers=headers)
    assert r.status_code == status, r.text
    conforms(r.json(), "Error")
    assert r.json()["error"]["code"] == code


def test_the_routes_run_on_the_production_ports(production):
    app, attached = production
    service = app.state.intake_service
    stop, spawn = service._stop, service._spawn
    assert isinstance(stop, IntakeStop)
    assert stop._commands is app.state.command_publisher  # the bot command bus (redis)
    assert isinstance(spawn, ExactRowSpawn)
    # The real authority, never ``None`` (which would fall through to allow-all).
    assert spawn._authority is app.state.service_authority
    assert spawn._fetch_bot_context is not None  # the sweep's identity edge
    assert stop._runtime is spawn._runtime
    assert isinstance(service._publisher, OutboxOnly)
    assert spawn._publisher is service._publisher
    # One entry service: the scheduler merges and re-runs through the one the routes use.
    assert attached["intake"].service is service
    assert attached["intake"].stop is stop


def test_a_half_wired_mount_is_refused():
    with pytest.raises(ValueError, match="intake"):
        create_app(intake_service=object())  # type: ignore[arg-type]


def test_the_loops_take_the_entry_service_from_the_caller():
    import inspect

    param = inspect.signature(main_mod._attach_background_loops).parameters["intake"]
    assert param.default is inspect.Parameter.empty
    assert param.kind is inspect.Parameter.KEYWORD_ONLY


def test_no_entry_service_is_built_without_a_service_authority():
    with pytest.raises(ValueError, match="service authority"):
        main_mod._build_intake(
            object(), object(), object(), service_authority=None, commands=object()
        )
