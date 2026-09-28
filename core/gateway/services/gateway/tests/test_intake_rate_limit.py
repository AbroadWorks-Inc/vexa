"""§1.13 / §2.5: the per-account entry-write limit, and the /v2 shape of the edge's own refusals.

Entry writes (``PUT /v2/entries`` and ``POST /v2/entries/remove``) share one budget per account and
per 60 s window, counted in Redis so every gateway replica sees the same count. The write past the
budget is a 429 ``rate_limited`` with ``Retry-After`` set to what is left of the window; a store that
cannot count refuses the write with a 503 ``unavailable`` rather than letting it through uncounted.

On ``/v2/`` paths the edge's own refusals carry ``{"error": {"code", "message"}}``; every other route
keeps ``{"detail": ...}``, and a downstream answer on /v2 is forwarded untouched.
"""

from __future__ import annotations

import json
from typing import Optional

import pytest
from fastapi.testclient import TestClient

from conftest import VALID_KEY, FakeAuthorizer, FakeDownstream, FakeRedis
from gateway import create_app
from gateway.intake_limit import (
    InMemoryIntakeLimiter,
    IntakeUnavailable,
    RedisIntakeLimiter,
    from_env,
)
from gateway.ports import AuthUnavailable

AUTH = {"x-api-key": VALID_KEY}
UUID = "5f0c2b7e-8d1a-4c3e-9b6f-2a7d1e4c8b90"
ALL_SCOPES = ["bot", "tx", "erase", "export"]
ENTRY = b'{"external_id":"x"}'

# 15 s into a window: 45 s are left of it.
T0 = 60.0 * 29_000_000 + 15.0


class Clock:
    def __init__(self, t: float = T0):
        self.t = t

    def __call__(self) -> float:
        return self.t


class CountingDownstream(FakeDownstream):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls = 0

    async def request(self, method, url, *, headers=None, params=None, content=None):
        self.calls += 1
        return await super().request(
            method, url, headers=headers, params=params, content=content
        )


class _Pipeline:
    def __init__(self, store: "SharedRedis", transaction: bool):
        self._store = store
        self.transaction = transaction
        self.commands: list = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    def incr(self, key):
        self.commands.append(("INCR", key))
        return self

    def expire(self, key, seconds):
        self.commands.append(("EXPIRE", key, seconds))
        return self

    async def execute(self):
        self._store.transactions.append((self.transaction, list(self.commands)))
        if self._store.down:
            raise ConnectionError("redis is down")
        results = []
        for cmd in self.commands:
            if cmd[0] == "INCR":
                self._store.counts[cmd[1]] = self._store.counts.get(cmd[1], 0) + 1
                results.append(self._store.counts[cmd[1]])
            else:
                self._store.ttls[cmd[1]] = cmd[2]
                results.append(True)
        return results


class SharedRedis:
    """One Redis, as two gateway replicas see it: the pipeline surface of ``redis.asyncio``."""

    def __init__(self, down: bool = False):
        self.down = down
        self.counts: dict = {}
        self.ttls: dict = {}
        self.transactions: list = []

    def pipeline(self, transaction: bool = True):
        return _Pipeline(self, transaction)


class TwoKeyAuthorizer(FakeAuthorizer):
    """Two keys, two accounts."""

    USERS = {
        "key-a": {"user_id": 7, "scopes": ALL_SCOPES, "max_concurrent": 3},
        "key-b": {"user_id": 8, "scopes": ALL_SCOPES, "max_concurrent": 3},
    }

    async def resolve(self, api_key: str) -> Optional[dict]:
        user = self.USERS.get(api_key)
        return dict(user) if user else None


class DownAuthorizer(FakeAuthorizer):
    async def resolve(self, api_key: str) -> Optional[dict]:
        raise AuthUnavailable("admin-api validate unreachable")


def _gateway(
    limiter, *, authorizer=None, downstream=None, rate_limiter=None, scopes=ALL_SCOPES
):
    downstream = downstream or CountingDownstream(status_code=200, body={"ok": True})
    authorizer = authorizer or FakeAuthorizer(
        user={"user_id": 7, "scopes": list(scopes), "max_concurrent": 3}
    )
    app = create_app(
        authorizer,
        downstream,
        FakeRedis(),
        rate_limiter=rate_limiter,
        intake_limiter=limiter,
    )
    return TestClient(app), downstream


def _put(client, headers=AUTH):
    return client.put("/v2/entries", headers=headers, content=ENTRY)


def _remove(client, headers=AUTH):
    return client.post("/v2/entries/remove", headers=headers, content=ENTRY)


def _v2_error(r, code):
    body = r.json()
    assert set(body) == {"error"}, body
    assert body["error"]["code"] == code
    assert isinstance(body["error"]["message"], str) and body["error"]["message"]


# ── the limit ────────────────────────────────────────────────────────────────────────────────────


def test_the_601st_write_in_a_minute_is_429_with_retry_after():
    client, downstream = _gateway(
        InMemoryIntakeLimiter(limit_per_min=600, clock=Clock())
    )

    for _ in range(600):
        assert _put(client).status_code == 200
    assert downstream.calls == 600

    r = _put(client)
    assert r.status_code == 429
    _v2_error(r, "rate_limited")
    assert r.headers["retry-after"] == "45"
    assert downstream.calls == 600, "a refused write must not reach meeting-api"


def test_remove_counts_against_the_same_budget_as_put():
    client, downstream = _gateway(InMemoryIntakeLimiter(limit_per_min=3, clock=Clock()))

    assert _put(client).status_code == 200
    assert _remove(client).status_code == 200
    assert _put(client).status_code == 200

    r = _remove(client)
    assert r.status_code == 429
    _v2_error(r, "rate_limited")
    assert _put(client).status_code == 429
    assert downstream.calls == 3


def test_a_new_window_starts_a_new_count():
    clock = Clock()
    client, _ = _gateway(InMemoryIntakeLimiter(limit_per_min=2, clock=clock))
    assert _put(client).status_code == 200
    assert _put(client).status_code == 200
    assert _put(client).status_code == 429

    clock.t += 45.0  # the next window begins
    r = _put(client)
    assert r.status_code == 200


def test_two_gateway_instances_share_the_count():
    redis = SharedRedis()
    clock = Clock()
    one, down_one = _gateway(RedisIntakeLimiter(redis, limit_per_min=4, clock=clock))
    two, down_two = _gateway(RedisIntakeLimiter(redis, limit_per_min=4, clock=clock))

    assert _put(one).status_code == 200
    assert _put(two).status_code == 200
    assert _remove(one).status_code == 200
    assert _remove(two).status_code == 200

    for client in (one, two):
        r = _put(client)
        assert r.status_code == 429
        _v2_error(r, "rate_limited")
        assert r.headers["retry-after"] == "45"
    assert down_one.calls + down_two.calls == 4


@pytest.mark.parametrize(
    "make",
    [
        lambda: InMemoryIntakeLimiter(limit_per_min=2, clock=Clock()),
        lambda: RedisIntakeLimiter(SharedRedis(), limit_per_min=2, clock=Clock()),
    ],
    ids=["in-memory", "redis"],
)
def test_the_count_is_per_account(make):
    limiter = make()
    client, _ = _gateway(limiter, authorizer=TwoKeyAuthorizer())
    a, b = {"x-api-key": "key-a"}, {"x-api-key": "key-b"}

    assert _put(client, a).status_code == 200
    assert _put(client, a).status_code == 200
    assert _put(client, a).status_code == 429

    assert _put(client, b).status_code == 200
    assert _remove(client, b).status_code == 200
    assert _put(client, b).status_code == 429


OTHER_ROUTES = [
    ("GET", "/v2/entries"),
    ("GET", "/v2/meetings"),
    ("GET", f"/v2/meetings/{UUID}"),
    ("POST", f"/v2/meetings/{UUID}/stop"),
    ("DELETE", f"/v2/meetings/{UUID}"),
    ("POST", f"/v2/meetings/{UUID}/export"),
    ("GET", "/bots/status"),
    ("GET", "/meetings"),
]


@pytest.mark.parametrize("method,url", OTHER_ROUTES)
def test_other_routes_are_not_counted_and_not_limited(method, url):
    limiter = InMemoryIntakeLimiter(limit_per_min=1, clock=Clock())
    client, _ = _gateway(limiter, scopes=ALL_SCOPES + ["browser"])

    for _ in range(3):
        assert client.request(method, url, headers=AUTH).status_code == 200
    assert (
        _put(client).status_code == 200
    ), "other routes spent none of the write budget"
    assert _put(client).status_code == 429
    assert client.request(method, url, headers=AUTH).status_code == 200


@pytest.mark.parametrize("method,url", OTHER_ROUTES)
def test_other_routes_are_unaffected_by_the_store_being_down(method, url):
    client, _ = _gateway(RedisIntakeLimiter(SharedRedis(down=True), limit_per_min=600))
    assert client.request(method, url, headers=AUTH).status_code == 200


def test_storage_down_is_503_unavailable_on_both_writes():
    client, downstream = _gateway(
        RedisIntakeLimiter(SharedRedis(down=True), limit_per_min=600)
    )

    for r in (_put(client), _remove(client)):
        assert r.status_code == 503
        _v2_error(r, "unavailable")
    assert (
        downstream.calls == 0
    ), "a write that could not be counted must not go through"


def test_a_refused_scope_spends_none_of_the_budget():
    limiter = InMemoryIntakeLimiter(limit_per_min=1, clock=Clock())
    client, _ = _gateway(limiter, scopes=["tx"])
    for _ in range(3):
        assert _put(client).status_code == 403
    assert limiter.count("7") == 0


# ── the limiter ──────────────────────────────────────────────────────────────────────────────────


async def test_the_redis_limiter_counts_and_expires_in_one_transaction():
    redis = SharedRedis()
    limiter = RedisIntakeLimiter(redis, limit_per_min=600, clock=Clock())

    decision = await limiter.hit("7")

    assert decision.allowed and decision.retry_after == 45
    window = int(T0 // 60)
    key = f"aw:intake:7:{window}"
    assert redis.transactions == [(True, [("INCR", key), ("EXPIRE", key, 60)])]
    assert redis.counts == {key: 1}


async def test_the_redis_limiter_raises_unavailable_when_the_store_fails():
    limiter = RedisIntakeLimiter(
        SharedRedis(down=True), limit_per_min=600, clock=Clock()
    )
    with pytest.raises(IntakeUnavailable):
        await limiter.hit("7")


async def test_retry_after_is_the_seconds_left_in_the_window_and_never_zero():
    clock = Clock(60.0 * 29_000_000 + 59.6)
    limiter = InMemoryIntakeLimiter(limit_per_min=1, clock=clock)
    await limiter.hit("7")
    decision = await limiter.hit("7")
    assert not decision.allowed and decision.retry_after == 1


@pytest.mark.parametrize("bad", [0, -1])
def test_a_limit_below_one_is_refused(bad):
    with pytest.raises(ValueError):
        InMemoryIntakeLimiter(limit_per_min=bad)
    with pytest.raises(ValueError):
        RedisIntakeLimiter(SharedRedis(), limit_per_min=bad)


def test_the_limit_is_read_from_intake_rate_limit_per_min(monkeypatch):
    monkeypatch.setenv("INTAKE_RATE_LIMIT_PER_MIN", "25")
    assert from_env(SharedRedis()).limit_per_min == 25


def test_the_default_limit_is_the_declared_one(monkeypatch):
    from gateway import config_preflight as cp

    monkeypatch.delenv("INTAKE_RATE_LIMIT_PER_MIN", raising=False)
    declared = {k["key"]: k for k in cp.load_declaration()["keys"]}
    assert declared["INTAKE_RATE_LIMIT_PER_MIN"]["class"] == "defaulted"
    assert declared["INTAKE_RATE_LIMIT_PER_MIN"]["default"] == "600"
    assert from_env(SharedRedis()).limit_per_min == 600


# ── the /v2 error shape ──────────────────────────────────────────────────────────────────────────


def test_v2_auth_refusals_have_the_error_shape():
    client, _ = _gateway(None)

    r = client.put("/v2/entries", content=ENTRY)
    assert r.status_code == 401
    _v2_error(r, "unauthorized")

    r = client.get("/v2/meetings", headers={"x-api-key": "not-a-key"})
    assert r.status_code == 401
    _v2_error(r, "unauthorized")


def test_v2_auth_unavailable_has_the_error_shape():
    client, _ = _gateway(None, authorizer=DownAuthorizer())
    r = client.get("/v2/meetings", headers=AUTH)
    assert r.status_code == 503
    _v2_error(r, "unavailable")
    assert r.headers["retry-after"] == "1"


def test_v2_scope_refusal_has_the_error_shape():
    client, _ = _gateway(None, scopes=["tx"])
    r = _put(client)
    assert r.status_code == 403
    _v2_error(r, "forbidden")


def test_v2_per_user_rate_refusal_has_the_error_shape():
    from gateway.ratelimit import PerUserRateLimiter

    limiter = PerUserRateLimiter(capacity=1, refill_per_sec=0, clock=lambda: 0.0)
    client, _ = _gateway(None, rate_limiter=limiter)
    assert client.get("/v2/meetings", headers=AUTH).status_code == 200
    r = client.get("/v2/meetings", headers=AUTH)
    assert r.status_code == 429
    _v2_error(r, "rate_limited")
    assert r.headers["retry-after"] == "1"


def test_upstream_routes_keep_the_detail_shape():
    from gateway.ratelimit import PerUserRateLimiter

    client, _ = _gateway(None, scopes=["tx"])
    assert client.get("/bots/status").json() == {"detail": "Missing API key"}
    assert client.get("/bots/status", headers={"x-api-key": "nope"}).json() == {
        "detail": "Invalid API key"
    }
    r = client.get("/bots/status", headers=AUTH)
    assert r.status_code == 403
    assert r.json() == {"detail": "Insufficient scope for this endpoint"}

    down, _ = _gateway(None, authorizer=DownAuthorizer())
    r = down.get("/bots/status", headers=AUTH)
    assert r.status_code == 503
    assert r.json() == {"detail": "Authentication temporarily unavailable, retry"}

    limiter = PerUserRateLimiter(capacity=1, refill_per_sec=0, clock=lambda: 0.0)
    rated, _ = _gateway(None, rate_limiter=limiter)
    assert rated.get("/bots/status", headers=AUTH).status_code == 200
    r = rated.get("/bots/status", headers=AUTH)
    assert r.status_code == 429
    assert r.json() == {"detail": "Rate limit exceeded"}


@pytest.mark.parametrize(
    "status,body",
    [
        (404, {"error": {"code": "entry_not_found", "message": "no such entry"}}),
        (401, {"detail": "a downstream answer in any shape"}),
        (503, {"detail": "database down"}),
    ],
)
def test_a_downstream_answer_on_v2_is_forwarded_untouched(status, body):
    downstream = CountingDownstream(status_code=status, body=body)
    client, _ = _gateway(
        InMemoryIntakeLimiter(limit_per_min=600, clock=Clock()), downstream=downstream
    )
    r = _remove(client)
    assert r.status_code == status
    assert json.loads(r.content) == body


# ── every gateway-originated /v2 error ───────────────────────────────────────────────────────────


class RaisingDownstream(CountingDownstream):
    def __init__(self, exc: Exception):
        super().__init__()
        self._exc = exc

    async def request(self, method, url, *, headers=None, params=None, content=None):
        raise self._exc


def test_identity_ring_unset_is_503_unavailable_on_v2_and_detail_elsewhere(
    monkeypatch,
):
    monkeypatch.delenv("GATEWAY_IDENTITY_KEYS", raising=False)
    client, downstream = _gateway(None)

    r = client.get("/v2/meetings", headers=AUTH)
    assert r.status_code == 503
    assert r.json() == {
        "error": {
            "code": "unavailable",
            "message": "GATEWAY_IDENTITY_KEYS is not configured",
        }
    }

    r = client.get("/meetings", headers=AUTH)
    assert r.status_code == 503
    assert r.json() == {"detail": "GATEWAY_IDENTITY_KEYS is not configured"}
    assert downstream.calls == 0


@pytest.mark.parametrize(
    "method,url",
    [
        ("GET", "/v2/meetings/a%00b"),
        ("DELETE", "/v2/meetings/a%00b"),
        ("POST", "/v2/meetings/a%00b/stop"),
        ("POST", "/v2/meetings/a%00b/export"),
        ("PATCH", "/v2/webhooks/a%00b"),
        ("POST", "/v2/webhooks/a%00b/test"),
    ],
)
def test_a_bad_path_parameter_on_v2_is_400_invalid_request(method, url):
    client, downstream = _gateway(None)
    r = client.request(method, url, headers=AUTH, content=b"{}")
    assert r.status_code == 400
    assert r.json() == {
        "error": {"code": "invalid_request", "message": "invalid path parameter"}
    }
    assert downstream.calls == 0


def test_a_bad_path_parameter_elsewhere_keeps_detail():
    client, _ = _gateway(None)
    r = client.get("/user/calendars/a%00b/sync", headers=AUTH)
    assert r.status_code == 400
    assert r.json() == {"detail": "invalid path parameter"}


def test_an_unparseable_hop_url_on_v2_is_400_invalid_request():
    import httpx

    client, _ = _gateway(None, downstream=RaisingDownstream(httpx.InvalidURL("bad")))
    r = client.get("/v2/meetings", headers=AUTH)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_request"

    r = client.get("/meetings", headers=AUTH)
    assert r.json() == {"detail": "invalid path parameter"}


@pytest.mark.parametrize(
    "exc,status,message",
    [
        ("ReadTimeout", 504, "upstream timeout"),
        ("ConnectError", 502, "upstream unreachable: ConnectError"),
    ],
)
def test_an_upstream_fault_on_v2_is_unavailable_and_detail_elsewhere(
    exc, status, message
):
    import httpx

    downstream = RaisingDownstream(getattr(httpx, exc)("boom"))
    client, _ = _gateway(
        InMemoryIntakeLimiter(limit_per_min=600, clock=Clock()), downstream=downstream
    )

    for r in (_put(client), client.get("/v2/meetings", headers=AUTH)):
        assert r.status_code == status
        assert r.json() == {"error": {"code": "unavailable", "message": message}}

    r = client.get("/meetings", headers=AUTH)
    assert r.status_code == status
    assert r.json() == {"detail": message}


def test_every_refusal_status_the_gateway_uses_has_a_v2_code():
    """``_V2_ERROR_CODES`` is a closed mapping: every ``_refusal`` call site in app.py passes a
    literal status, and each one has a §2.5 code, so no refusal can fail on an unknown status.
    """
    import ast
    import pathlib

    from gateway import app as gateway_app

    tree = ast.parse(pathlib.Path(gateway_app.__file__).read_text())
    statuses = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_refusal"
        ):
            status = node.args[1]
            assert isinstance(status, ast.Constant) and isinstance(
                status.value, int
            ), f"line {node.lineno}: _refusal must be called with a literal status"
            statuses.append(status.value)
    assert set(statuses) >= {400, 401, 403, 429, 502, 503, 504}
    missing = sorted(set(statuses) - set(gateway_app._V2_ERROR_CODES))
    assert not missing, f"statuses with no §2.5 code: {missing}"
    assert set(gateway_app._V2_ERROR_CODES.values()) <= {
        "invalid_request",
        "unauthorized",
        "forbidden",
        "rate_limited",
        "unavailable",
    }
