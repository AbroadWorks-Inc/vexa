"""F154 — the auto-join sweep's `_tick` closure passed `publish_status=publish_status` into
`auto_join_tick(...)` while the `publish_status` function it referenced had been deleted by
commit 1699aa3ac ("bot_name: no fourth store") — the commit correctly folded the two
`fetch_bot_context` builders into one `_bot_context_fetcher()` helper but dropped
`publish_status` entirely even though `_tick()` still names it. On the running dogfood stack
every `_auto_join_loop` sweep raised `NameError: name 'publish_status' is not defined` inside
`_tick()`, so calendar auto-join has been silently dead since the commit landed.

This test drives the REAL `_attach_background_loops` wiring in `meeting_api.__main__` — not a
reimplementation of it — so it fails with the NameError on the buggy tip and passes once
`publish_status` is restored. It stubs `auto_join_tick` to capture the kwargs `_tick()` builds
(proving `publish_status` is a real callable, not merely present) and then invokes the captured
`publish_status` to prove it publishes a `meeting.status` frame to redis exactly as main's
pre-regression closure did.

Every OTHER background loop `_attach_background_loops` starts (segment-consumer, db-writer,
webhook-drain, stop-reconcile, service-authority, calendar-sync, signal-tape-janitor) is left
running for real against bare fakes — cheapest way to reach the auto-join tick is through the
actual `lifespan` context manager, and a patched `asyncio.sleep` that raises after being awaited
once ends every loop (including auto-join) after exactly one tick, whether that tick fails inside
its own try/except or returns cleanly.
"""
from __future__ import annotations

import asyncio
import json
import sys
import types

import meeting_api.__main__ as main_mod
import meeting_api.bot_spawn.auto_join as auto_join_mod


class _StopLoop(Exception):
    """Sentinel raised by the patched asyncio.sleep so every background loop's `while True` ends
    after exactly one tick, instead of running forever."""


class _FakeMeetingRepo:
    # Only needs to exist + carry the attribute `_auto_join_loop` gates on; auto_join_tick itself
    # is stubbed below, so no real repo behaviour is exercised.
    def list_due_meetings(self, now, lead_s):  # pragma: no cover - never called (tick is stubbed)
        return []


class _FakeSession:
    """Just enough of an AsyncSession for the sweeps' advisory lock: every lock is granted."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, *a, **kw):
        return types.SimpleNamespace(scalar=lambda: True)


def _fake_session_factory():
    # §1.5: the auto-join and not-sent sweeps need Postgres (the intake store) and run only with a
    # session factory; this one serves the single-flight lock and nothing else.
    return _FakeSession()


class _FakeRuntime:
    pass


class _FakeRedis:
    def __init__(self):
        self.published: list[tuple[str, str]] = []

    async def publish(self, channel, message):
        self.published.append((channel, message))


def _intake(session_factory, repo, runtime, redis_client):
    """The entry service as ``build_production_app`` builds it (``_build_intake``), over the fakes."""
    from meeting_api.service_authority import AllowAllServiceAuthority

    return main_mod._build_intake(
        session_factory, repo, runtime,
        service_authority=AllowAllServiceAuthority(), commands=redis_client,
    )


def _fake_app():
    app = types.SimpleNamespace()
    app.state = types.SimpleNamespace()
    app.router = types.SimpleNamespace()
    return app


async def test_auto_join_tick_publish_status_is_wired_and_publishes(monkeypatch):
    captured_kwargs: dict = {}
    logged_exceptions: list[BaseException] = []

    async def _stub_auto_join_tick(*args, **kwargs):
        # Evaluating this call's keyword arguments (in the REAL `_tick()` closure) is exactly
        # where the regression raised NameError on `publish_status=publish_status` — reaching
        # this stub body at all is already proof the closure built cleanly.
        captured_kwargs.update(kwargs)

    monkeypatch.setattr(auto_join_mod, "auto_join_tick", _stub_auto_join_tick)

    # `_auto_join_loop`'s while-loop swallows any tick exception via a bare
    # `except Exception: log.exception(...)`. Capture what `log.exception` was called with
    # directly (rather than fighting pytest's log-capture plugin across the asyncio.create_task
    # boundary) so a red run names the real defect instead of just its symptom.
    def _record_exception(msg, *args, **kwargs):
        exc = sys.exc_info()[1]
        if exc is not None:
            logged_exceptions.append(exc)

    monkeypatch.setattr(main_mod.log, "exception", _record_exception)

    real_sleep = asyncio.sleep  # captured before the patch below — used to yield the event loop

    async def _sleep_once_then_stop(delay, *a, **kw):
        raise _StopLoop()

    monkeypatch.setattr(asyncio, "sleep", _sleep_once_then_stop)

    app = _fake_app()
    redis_client = _FakeRedis()
    repo, runtime = _FakeMeetingRepo(), _FakeRuntime()

    main_mod._attach_background_loops(
        app,
        transcript_store=types.SimpleNamespace(),
        segment_bus=types.SimpleNamespace(),
        redis_client=redis_client,
        meeting_repo=repo,
        runtime=runtime,
        service_authority=None,
        system_webhook_sink=None,
        session_factory=_fake_session_factory,
        storage=None,
        intake=_intake(_fake_session_factory, repo, runtime, redis_client),
    )

    async with app.router.lifespan_context(app):
        # `asyncio.create_task` only SCHEDULES the loops — they don't run a single line until we
        # yield control. `real_sleep` (captured before the patch, called directly so it bypasses
        # the patched `asyncio.sleep` module attribute) gives every loop's first tick + its
        # patched-sleep-raises-_StopLoop teardown time to actually run before we exit the
        # context. `lifespan`'s own `finally` then cancels + gathers with return_exceptions=True,
        # so those per-loop failures never surface here.
        await real_sleep(0.05)

    if "publish_status" not in captured_kwargs:
        details = "\n".join(repr(e) for e in logged_exceptions) or (
            "(nothing logged either — _tick() may not have run at all)"
        )
        raise AssertionError(
            "auto_join_tick was never called with publish_status — _tick() raised before "
            f"reaching it:\n{details}"
        )
    publish_status = captured_kwargs["publish_status"]
    assert callable(publish_status)

    # Prove it behaves exactly like main's pre-regression closure: publishes a meeting.status
    # frame on the user's channel, best-effort (never raises even if redis_client.publish does).
    await publish_status(
        user_id=42, meeting_id="m-1", native_id="n-1", status="joining", when="2026-09-03T08:52:00Z",
    )

    assert len(redis_client.published) == 1
    channel, message = redis_client.published[0]
    assert channel == "u:42:meetings"
    frame = json.loads(message)
    assert frame == {
        "type": "meeting.status",
        "meeting_id": "m-1",
        "native": "n-1",
        "status": "joining",
        "when": "2026-09-03T08:52:00Z",
    }


# ── §1.5: the intake store and entry service reach the tick; the not-sent sweep is wired ────────


async def _run_loops(monkeypatch, *, session_factory, env=None):
    """Start the REAL lifespan once, every loop ending after one tick. Returns the guarded
    single-flight keys, the sleep delays and the app."""
    from meeting_api.sweeps import single_flight

    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)
    guarded: list[int] = []

    async def _record(lock, key, body):
        guarded.append(key)
        await body()
        return True

    monkeypatch.setattr(single_flight, "run_single_flight", _record)
    delays: list[float] = []
    real_sleep = asyncio.sleep

    async def _sleep_once_then_stop(delay, *a, **kw):
        delays.append(delay)
        raise _StopLoop()

    monkeypatch.setattr(asyncio, "sleep", _sleep_once_then_stop)
    monkeypatch.setattr(main_mod.log, "exception", lambda *a, **kw: None)
    app = _fake_app()
    redis_client, repo, runtime = _FakeRedis(), _FakeMeetingRepo(), _FakeRuntime()
    main_mod._attach_background_loops(
        app,
        transcript_store=types.SimpleNamespace(),
        segment_bus=types.SimpleNamespace(),
        redis_client=redis_client,
        meeting_repo=repo,
        runtime=runtime,
        service_authority=None,
        system_webhook_sink=None,
        session_factory=session_factory,
        storage=None,
        intake=(
            _intake(session_factory, repo, runtime, redis_client)
            if session_factory is not None
            else None
        ),
    )
    async with app.router.lifespan_context(app):
        await real_sleep(0.05)
    return guarded, delays


async def test_auto_join_tick_gets_the_intake_store_service_and_publisher(monkeypatch):
    from meeting_api.intake import IntakeService, OutboxOnly, PostgresIntakeStore
    from meeting_api.sweeps.single_flight import sweep_lock_key

    captured: dict = {}

    async def _stub(*args, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(auto_join_mod, "auto_join_tick", _stub)
    guarded, _ = await _run_loops(monkeypatch, session_factory=_fake_session_factory)

    assert isinstance(captured["store"], PostgresIntakeStore)
    assert isinstance(captured["intake"], IntakeService)
    assert isinstance(captured["publisher"], OutboxOnly)
    assert sweep_lock_key("auto-join") in guarded


async def test_not_sent_sweep_runs_single_flight_on_its_own_interval(monkeypatch):
    from datetime import datetime, timedelta, timezone

    from meeting_api.intake import IntakeService, OutboxOnly, PostgresIntakeStore
    from meeting_api.intake import sweeps as sweeps_mod
    from meeting_api.sweeps.single_flight import sweep_lock_key

    calls: list[tuple] = []

    async def _stub_auto_join(*args, **kwargs):
        calls.append(("auto-join", kwargs["store"]))

    async def _stub_not_sent(store, service, **kwargs):
        calls.append(("not-sent", store, service, kwargs))
        return 0

    monkeypatch.setattr(auto_join_mod, "auto_join_tick", _stub_auto_join)
    monkeypatch.setattr(sweeps_mod, "not_sent_tick", _stub_not_sent)
    guarded, delays = await _run_loops(
        monkeypatch,
        session_factory=_fake_session_factory,
        env={"NOT_SENT_SWEEP_INTERVAL_S": "7", "JOIN_NOW_ADOPT_AHEAD_S": "1800"},
    )

    (not_sent,) = [c for c in calls if c[0] == "not-sent"]
    (auto_join,) = [c for c in calls if c[0] == "auto-join"]
    _, store, service, kwargs = not_sent
    assert isinstance(store, PostgresIntakeStore) and store is auto_join[1]
    assert isinstance(service, IntakeService)
    assert isinstance(kwargs["publisher"], OutboxOnly)
    assert kwargs["open_ended_s"] == 1800
    assert abs(kwargs["now"] - datetime.now(timezone.utc)) < timedelta(seconds=30)
    assert sweep_lock_key("not-sent") in guarded
    assert 7.0 in delays


async def test_without_postgres_neither_sweep_runs(monkeypatch):
    from meeting_api.intake import sweeps as sweeps_mod

    calls: list[str] = []

    async def _stub_auto_join(*args, **kwargs):
        calls.append("auto-join")

    async def _stub_not_sent(*args, **kwargs):
        calls.append("not-sent")
        return 0

    monkeypatch.setattr(auto_join_mod, "auto_join_tick", _stub_auto_join)
    monkeypatch.setattr(sweeps_mod, "not_sent_tick", _stub_not_sent)
    await _run_loops(monkeypatch, session_factory=None)
    assert calls == []
