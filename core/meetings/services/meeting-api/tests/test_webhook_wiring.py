"""Per-user webhook wiring — config rides on meeting.data; the lifecycle callback delivers.

The principled 0.12 path (vs main's monolith users-table read): identity owns the config; the gateway
forwards it; bot_spawn persists it into meeting.data; the lifecycle callback delivers the sealed
``meeting.status_change`` envelope via the injected WebhookSink — meeting-api never reads the users table.
"""
from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from meeting_api import create_app
from meeting_api.bot_spawn.fakes import FakeRuntimeClient, InMemoryMeetingRepo
from meeting_api.bot_spawn.service import request_bot
from meeting_api.webhooks import DeliveryResult
from internal_callers import BOT


class _CaptureSink:
    """A WebhookSink stand-in that records each deliver() call."""

    def __init__(self):
        self.calls = []

    async def deliver(self, url, envelope, webhook_secret=None, *, scope="per-client",
                      events_config=None, label="", metadata=None):
        self.calls.append({
            "url": url, "event_type": envelope.get("event_type"),
            "secret": webhook_secret, "events_config": events_config,
            "envelope": envelope,
        })
        return DeliveryResult(status="delivered", status_code=200)


# ── config storage (bot_spawn → meeting.data) ────────────────────────────────────────────────────

def test_request_bot_stores_webhook_in_meeting_data():
    repo, rt = InMemoryMeetingRepo(), FakeRuntimeClient()
    asyncio.run(request_bot(
        repo, rt, user_id=1, platform="google_meet", native_meeting_id="m1",
        webhook_url="https://hook.example/x", webhook_secret="s3cr3t",
        webhook_events={"meeting.status_change": True},
        redis_url="redis://r", token_secret="secret",
    ))
    m = asyncio.run(repo.find_active(1, "google_meet", "m1"))
    assert m["data"]["webhook_url"] == "https://hook.example/x"
    assert m["data"]["webhook_secret"] == "s3cr3t"
    assert m["data"]["webhook_events"] == {"meeting.status_change": True}


def test_request_bot_omits_webhook_when_unset():
    repo, rt = InMemoryMeetingRepo(), FakeRuntimeClient()
    asyncio.run(request_bot(
        repo, rt, user_id=1, platform="google_meet", native_meeting_id="m2",
        redis_url="redis://r", token_secret="secret",
    ))
    m = asyncio.run(repo.find_active(1, "google_meet", "m2"))
    assert "webhook_url" not in m["data"]


# ── delivery (lifecycle callback → WebhookSink) ──────────────────────────────────────────────────

def _seed(repo, *, session_uid, data):
    m = asyncio.run(repo.create_meeting(user_id=1, platform="google_meet", native_meeting_id="m1", data=data))
    asyncio.run(repo.create_session(meeting_id=m["id"], session_uid=session_uid))
    return m


def test_status_change_is_not_posted_to_the_per_user_url(goldens):
    """A meeting.data webhook_url is not a second sender. Subscribers are delivered
    from the outbox row the status write commits."""
    repo, sink = InMemoryMeetingRepo(), _CaptureSink()
    _seed(repo, session_uid="sess-uid", data={
        "webhook_url": "https://hook.example/x", "webhook_secret": "s3cr3t",
        "webhook_events": {"meeting.status_change": True},
    })
    client = TestClient(create_app(meeting_repo=repo, webhook_sink=sink))
    r = client.post("/bots/internal/callback/lifecycle", headers=BOT, json=goldens["joining"])
    assert r.status_code == 200, r.text
    assert sink.calls == []


def test_no_webhook_when_url_unconfigured(goldens):
    repo, sink = InMemoryMeetingRepo(), _CaptureSink()
    _seed(repo, session_uid="sess-uid", data={})  # no webhook_url on the meeting
    client = TestClient(create_app(meeting_repo=repo, webhook_sink=sink))
    r = client.post("/bots/internal/callback/lifecycle", headers=BOT, json=goldens["joining"])
    assert r.status_code == 200, r.text
    assert not sink.calls


# ── typed events (webhook.v1 EventType parity) ───────────────────────────────────────────────────
# Each lifecycle transition still emits meeting.status_change; the mapped transitions ALSO emit the
# typed event the contract declares: active → meeting.started, completed → meeting.completed (the
# post-meeting `{meeting}` envelope), failed → bot.failed.

_ALL_EVENTS = {
    "meeting.status_change": True, "meeting.started": True,
    "meeting.completed": True, "bot.failed": True,
}


def _wired_client():
    repo, sink = InMemoryMeetingRepo(), _CaptureSink()
    _seed(repo, session_uid="sess-uid", data={
        "webhook_url": "https://hook.example/x", "webhook_secret": "s3cr3t",
        "webhook_events": dict(_ALL_EVENTS),
    })
    return TestClient(create_app(meeting_repo=repo, webhook_sink=sink)), sink


def _post(client, event):
    r = client.post("/bots/internal/callback/lifecycle", headers=BOT, json=event)
    assert r.status_code == 200, r.text


def test_meeting_started_is_not_posted_to_the_per_user_url(goldens):
    client, sink = _wired_client()
    _post(client, goldens["joining"])
    _post(client, goldens["active"])
    assert sink.calls == []


def test_meeting_completed_is_not_posted_to_the_per_user_url(goldens):
    client, sink = _wired_client()
    _post(client, goldens["joining"])
    _post(client, goldens["active"])
    _post(client, goldens["completed-stopped"])
    assert sink.calls == []


def test_meeting_completed_exposes_frozen_privacy_safe_service_provenance():
    repo, sink = InMemoryMeetingRepo(), _CaptureSink()
    seeded = _seed(repo, session_uid="sess-uid", data={
        "webhook_url": "https://hook.example/x",
        "webhook_events": dict(_ALL_EVENTS),
        "transcribe_enabled": True,
        "transcription_provider": "customer",
    })

    async def finalizer(meeting_id):
        assert meeting_id == seeded["id"]
        return 3

    client = TestClient(
        create_app(
            meeting_repo=repo,
            webhook_sink=sink,
            transcript_finalizer=finalizer,
        )
    )
    for event in (
        {"connection_id": "sess-uid", "status": "joining",
         "timestamp": "2026-07-28T10:00:00.000Z"},
        {"connection_id": "sess-uid", "status": "active",
         "timestamp": "2026-07-28T10:05:00.000Z"},
        {"connection_id": "sess-uid", "status": "completed",
         "completion_reason": "stopped", "timestamp": "2026-07-28T10:30:00.000Z"},
    ):
        _post(client, event)

    assert sink.calls == []
    completed = repo._meetings[seeded["id"]]["data"]
    assert completed["service_provenance"] == {
        "bot_admitted_at": "2026-07-28T10:05:00.000Z",
        "bot_departed_at": "2026-07-28T10:30:00.000Z",
        "bot_outcome": "served",
        "transcription_provider": "customer",
        "transcription_outcome": "served",
        "lifecycle_contract_version": "2026-07-28",
    }
    assert "transcription_service_url" not in repr(completed["service_provenance"])
    assert repo._meetings[seeded["id"]]["data"]["segments_captured"] == 3


def test_finalization_failure_never_claims_vexa_transcription_was_served():
    repo, sink = InMemoryMeetingRepo(), _CaptureSink()
    seeded = _seed(repo, session_uid="sess-uid", data={
        "webhook_url": "https://hook.example/x",
        "webhook_events": dict(_ALL_EVENTS),
        "transcribe_enabled": True,
        "transcription_provider": "vexa",
    })

    async def failed_finalizer(_meeting_id):
        raise RuntimeError("durable transcript unavailable")

    client = TestClient(
        create_app(
            meeting_repo=repo,
            webhook_sink=sink,
            transcript_finalizer=failed_finalizer,
        )
    )
    for event in (
        {"connection_id": "sess-uid", "status": "joining",
         "timestamp": "2026-07-28T10:00:00.000Z"},
        {"connection_id": "sess-uid", "status": "active",
         "timestamp": "2026-07-28T10:05:00.000Z"},
        {"connection_id": "sess-uid", "status": "completed",
         "completion_reason": "stopped", "timestamp": "2026-07-28T10:30:00.000Z"},
    ):
        _post(client, event)

    assert sink.calls == []
    provenance = repo._meetings[seeded["id"]]["data"]["service_provenance"]
    assert provenance["transcription_provider"] == "vexa"
    assert provenance["transcription_outcome"] == "failed"


def test_bot_failed_is_not_posted_to_the_per_user_url(goldens):
    client, sink = _wired_client()
    _post(client, goldens["joining"])
    _post(client, goldens["failed-join"])
    assert sink.calls == []


def test_an_intermediate_transition_posts_no_per_user_webhook(goldens):
    client, sink = _wired_client()
    _post(client, goldens["joining"])
    assert sink.calls == []


def test_typed_event_suppressed_by_real_event_filter(goldens):
    """With the REAL WebhookSink filter semantics, an unsubscribed typed event is suppressed —
    the emitter passes events_config through, and delivery.is_event_enabled opts in per type."""
    from meeting_api.webhooks import is_event_enabled

    cfg = {"meeting.status_change": True}  # user did NOT opt into meeting.started
    assert is_event_enabled(cfg, "meeting.status_change") is True
    assert is_event_enabled(cfg, "meeting.started") is False
    assert is_event_enabled(None, "meeting.completed") is True  # default-enabled set


def test_typed_builder_validates_against_sealed_schema(goldens):
    """build_typed_envelope conforms every envelope to webhook.v1#/$defs/Envelope at the seam;
    intermediate transitions return None."""
    from meeting_api.lifecycle import LifecycleSink, MeetingStore, TransitionSource
    from meeting_api.lifecycle.webhook import build_typed_envelope, typed_event_type

    sink = LifecycleSink(store=MeetingStore())
    ch = sink.apply_change(goldens["joining"], transition_source=TransitionSource.BOT_CALLBACK)
    assert typed_event_type(ch) is None and build_typed_envelope(ch) is None
    ch = sink.apply_change(goldens["active"], transition_source=TransitionSource.BOT_CALLBACK)
    env = build_typed_envelope(ch)  # raises if it does not conform to the sealed Envelope shape
    assert env["event_type"] == "meeting.started"
    ch = sink.apply_change(goldens["completed-stopped"], transition_source=TransitionSource.BOT_CALLBACK)
    env = build_typed_envelope(ch)
    assert env["event_type"] == "meeting.completed"
    assert set(env["data"].keys()) == {"meeting"}


# ── delivery OUTCOME is reported (#815) ──────────────────────────────────────────────────────────
# `WebhookSink.deliver` never raises: it RETURNS delivered|suppressed|blocked|failed|queued. That
# outcome used to be discarded, so a webhook the subscriber never received (unsubscribed event type,
# SSRF-blocked target, 4xx endpoint) looked exactly like one that arrived — "my webhooks stopped"
# was undiagnosable in production. Every outcome now emits one `webhook_delivery` logevent.

class _OutcomeSink:
    """A WebhookSink stand-in that returns a chosen DeliveryResult."""

    def __init__(self, result):
        self._result = result

    async def deliver(self, url, envelope, webhook_secret=None, *, scope="per-client",
                      events_config=None, label="", metadata=None):
        return self._result


def _run_advance(repo, sink, goldens):
    client = TestClient(create_app(meeting_repo=repo, webhook_sink=sink))
    return client.post("/bots/internal/callback/lifecycle", headers=BOT, json=goldens["joining"])


def test_the_callback_logs_no_per_user_delivery(goldens, capsys):
    repo = InMemoryMeetingRepo()
    _seed(repo, session_uid="sess-uid", data={
        "webhook_url": "https://hook.example/x?token=SECRET-IN-URL",
        "webhook_events": {"meeting.status_change": True},
    })
    r = _run_advance(repo, _OutcomeSink(DeliveryResult(status="delivered", status_code=200)), goldens)
    assert r.status_code == 200, r.text
    captured = capsys.readouterr().out
    assert "webhook_delivery" not in captured
    assert "SECRET-IN-URL" not in captured


