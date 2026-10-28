"""The old per-user delivery ledger.

The lifecycle callback does not write it. Subscriber delivery is ``webhook_outbox``. These
tests hold the read route's owner scope and the ledger's own host-only rule.
"""
from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from meeting_api import create_app
from meeting_api.bot_spawn.fakes import InMemoryMeetingRepo
from meeting_api.webhooks import InMemoryDeliveryLedger, WebhookSink, build_delivery_record
from gateway_identity import via_gateway
from internal_callers import BOT

# A resolver stub so the SSRF guard never touches DNS — hook.example resolves to a public IP.
_PUBLIC = lambda host: ["93.184.216.34"]  # noqa: E731


def _seed(repo, *, session_uid, data, user_id=1):
    m = asyncio.run(repo.create_meeting(user_id=user_id, platform="google_meet",
                                        native_meeting_id="m1", data=data))
    asyncio.run(repo.create_session(meeting_id=m["id"], session_uid=session_uid))
    return m


def _app(repo, receiver, ledger):
    sink = WebhookSink(transport=receiver, resolver=_PUBLIC)
    return create_app(meeting_repo=repo, webhook_sink=sink, delivery_ledger=ledger)


# ── the fix: a real delivery appears in the user-visible history ────────────────────────────────

def test_the_callback_does_not_fill_the_old_delivery_history(goldens, receiver):
    """A lifecycle advance does not POST meeting.data.webhook_url and does not write the
    old per-user ledger. Subscriber delivery is the outbox."""
    repo, ledger = InMemoryMeetingRepo(), InMemoryDeliveryLedger()
    _seed(repo, session_uid="sess-uid", data={
        "webhook_url": "https://hook.example/x", "webhook_secret": "s3cr3t",
        "webhook_events": {"meeting.status_change": True},
    })
    client = TestClient(via_gateway(_app(repo, receiver, ledger)))

    r = client.post("/bots/internal/callback/lifecycle", headers=BOT, json=goldens["joining"])
    assert r.status_code == 200, r.text
    assert receiver.received == []
    h = client.get("/webhooks/deliveries", headers={"X-User-Id": "1"})
    assert h.status_code == 200, h.text
    assert h.json()["deliveries"] == []


def test_delivery_history_is_owner_scoped(receiver):
    """The history is scoped to X-User-Id — another user never sees this user's deliveries."""
    repo, ledger = InMemoryMeetingRepo(), InMemoryDeliveryLedger()
    record = build_delivery_record(
        event_type="meeting.status_change",
        event_id="evt_scope",
        target_host="hook.example",
        outcome="delivered",
        status_code=200,
        meeting_id=7,
    )
    asyncio.run(ledger.record(1, record))
    client = TestClient(via_gateway(_app(repo, receiver, ledger)))

    assert client.get("/webhooks/deliveries", headers={"X-User-Id": "1"}).json()["deliveries"]
    assert client.get("/webhooks/deliveries", headers={"X-User-Id": "2"}).json()["deliveries"] == []
    # No X-User-Id at all → refused before the route (never leak another user's history to an
    # unidentified caller).
    r = client.get("/webhooks/deliveries")
    assert r.status_code == 401 and "deliveries" not in r.json()


# ── the ledger port itself (P14 guard is belt-and-braces, not only the caller's discipline) ──────

def test_ledger_sanitizes_url_and_secret_even_if_a_caller_passes_them():
    """Even a caller that shoves a url/secret into the record dict gets them stripped (P14)."""
    ledger = InMemoryDeliveryLedger()
    record = build_delivery_record(
        event_type="meeting.completed", event_id="evt_abc", target_host="hook.example",
        outcome="delivered", status_code=200, meeting_id=7,
    )
    record["webhook_url"] = "https://hook.example/secret-path?token=abc"  # a caller mistake
    record["webhook_secret"] = "s3cr3t"
    asyncio.run(ledger.record(1, record))
    rows = asyncio.run(ledger.list(1))
    assert len(rows) == 1
    assert "webhook_url" not in rows[0] and "webhook_secret" not in rows[0]
    assert rows[0]["target_host"] == "hook.example"


def test_ledger_is_newest_first_and_capped():
    ledger = InMemoryDeliveryLedger(max_per_user=3)
    for i in range(5):
        asyncio.run(ledger.record(1, build_delivery_record(
            event_type="meeting.status_change", event_id=f"evt_{i}",
            target_host="hook.example", outcome="delivered", status_code=200,
        )))
    rows = asyncio.run(ledger.list(1))
    assert len(rows) == 3  # capped
    assert [r["event_id"] for r in rows] == ["evt_4", "evt_3", "evt_2"]  # newest first
