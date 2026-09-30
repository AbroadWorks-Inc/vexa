"""§1.13 against a REAL Redis: the write limit as production runs it.

Opt-in. Skipped unless ``GATEWAY_TEST_REDIS_URL`` names a throwaway Redis, for example:

    docker run -d --rm --name aw-intake-redis -p 56379:6379 redis:7
    GATEWAY_TEST_REDIS_URL=redis://localhost:56379/0 uv run pytest tests/test_intake_rate_limit_real_redis.py

The last test stops that Redis (``SHUTDOWN NOSAVE``) to prove a store lost mid-run refuses writes
with a 503. It runs only when ``GATEWAY_TEST_REDIS_SHUTDOWN=1`` is set as well, so pointing the URL
at a Redis someone else uses can never stop it.
"""

from __future__ import annotations

import math
import os
import random
import time

import httpx
import pytest

from conftest import VALID_KEY, FakeAuthorizer, FakeDownstream, FakeRedis
from gateway import create_app
from gateway.intake_limit import WINDOW_SECONDS, RedisIntakeLimiter

REDIS_URL = os.getenv("GATEWAY_TEST_REDIS_URL", "")
pytestmark = pytest.mark.skipif(
    not REDIS_URL, reason="GATEWAY_TEST_REDIS_URL is not set (opt-in real-Redis leg)"
)

AUTH = {"x-api-key": VALID_KEY}
ENTRY = b'{"external_id":"x"}'


def _redis():
    import redis.asyncio as aioredis

    # The options build_production_app gives the gateway's own client.
    return aioredis.from_url(
        REDIS_URL,
        encoding="utf-8",
        decode_responses=True,
        socket_timeout=10,
        socket_connect_timeout=5,
        socket_keepalive=True,
        health_check_interval=30,
        retry_on_timeout=True,
    )


def _account() -> int:
    """A fresh account per test, so reruns inside one window never share a count."""
    return random.randint(10**9, 10**10)


def _app(redis, account: int, *, limit: int, now: float):
    limiter = RedisIntakeLimiter(redis, limit_per_min=limit, clock=lambda: now)
    app = create_app(
        FakeAuthorizer(
            user={"user_id": account, "scopes": ["bot"], "max_concurrent": 3}
        ),
        FakeDownstream(status_code=200, body={"ok": True}),
        FakeRedis(),
        intake_limiter=limiter,
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway"
    )


async def _put(client):
    return await client.put("/v2/entries", headers=AUTH, content=ENTRY)


async def test_the_transaction_counts_and_sets_a_ttl_of_about_60():
    redis = _redis()
    account, now = _account(), time.time()
    limiter = RedisIntakeLimiter(redis, limit_per_min=600, clock=lambda: now)

    first = await limiter.hit(str(account))
    second = await limiter.hit(str(account))

    key = f"aw:intake:{account}:{int(now // WINDOW_SECONDS)}"
    assert first.allowed and second.allowed
    assert await redis.get(key) == "2"
    ttl = await redis.ttl(key)
    assert 55 <= ttl <= 60, ttl
    await redis.aclose()


async def test_the_601st_write_is_429_with_retry_after():
    redis = _redis()
    account, now = _account(), time.time()
    async with _app(redis, account, limit=600, now=now) as client:
        for _ in range(600):
            assert (await _put(client)).status_code == 200
        r = await _put(client)
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "rate_limited"
    left = (int(now // WINDOW_SECONDS) + 1) * WINDOW_SECONDS - now
    assert r.headers["retry-after"] == str(max(1, math.ceil(left)))
    await redis.aclose()


async def test_two_gateway_instances_share_the_count():
    one_redis, two_redis = _redis(), _redis()  # each replica has its own client
    account, now = _account(), time.time()
    async with _app(one_redis, account, limit=4, now=now) as one, _app(
        two_redis, account, limit=4, now=now
    ) as two:
        for client in (one, two, one, two):
            assert (await _put(client)).status_code == 200
        for client in (one, two):
            r = await _put(client)
            assert r.status_code == 429
            assert r.json()["error"]["code"] == "rate_limited"
    await one_redis.aclose()
    await two_redis.aclose()


@pytest.mark.skipif(
    os.getenv("GATEWAY_TEST_REDIS_SHUTDOWN") != "1",
    reason="stops the Redis; set GATEWAY_TEST_REDIS_SHUTDOWN=1 for a throwaway one",
)
async def test_redis_stopped_mid_run_is_503_unavailable():
    redis = _redis()
    account, now = _account(), time.time()
    async with _app(redis, account, limit=600, now=now) as client:
        assert (await _put(client)).status_code == 200

        admin = _redis()
        try:
            await admin.shutdown(nosave=True)
        except Exception:
            pass  # the server closing the connection is the expected answer
        with pytest.raises(Exception):
            await _redis().ping()  # the store really is gone

        r = await _put(client)
        assert r.status_code == 503
        assert r.json()["error"]["code"] == "unavailable"
        other = await client.get("/v2/entries", headers=AUTH)
        assert other.status_code == 200, "routes that do not count are unaffected"
