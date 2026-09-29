"""meeting-api's own bookkeeping in ``meetings.data`` never reaches a webhook receiver (§1.8, §2.7).

The bot retry (§6.9 F-K2) keeps three keys in ``meetings.data``: the retry marker
(``intake.retry.MARKER``), the session a spawn is about to write (``bot_spawn.ports.SPAWN_SESSION``)
and the workloads whose delete is unconfirmed (``bot_spawn.ports.UNPROVEN_TEARDOWN``). They name
workloads and sessions, which are meeting-api's business alone.

- The legacy payload (system hook and per-user ``webhook_url``) ships ``data`` through
  ``webhooks.delivery.clean_meeting_data``, so the keys are stripped there.
- The §2.4 meeting (every ``/v2`` reply, read and subscription delivery) is built from named keys
  only, so it never carries them either.
"""

from __future__ import annotations

import json
from typing import Any

from meeting_api.app import legacy_meeting_projection
from meeting_api.bot_spawn.ports import SPAWN_SESSION, UNPROVEN_TEARDOWN
from meeting_api.intake.projection import project_meeting
from meeting_api.intake.retry import MARKER as BOT_RETRY
from meeting_api.webhooks.delivery import clean_meeting_data

INTERNAL_KEYS = (BOT_RETRY, SPAWN_SESSION, UNPROVEN_TEARDOWN)
WORKLOAD = "sentinel-workload-7f3a"
SESSION = "sentinel-session-91c2"


def _data() -> dict[str, Any]:
    return {
        "title": "Weekly sync",
        "constructed_meeting_url": "https://meet.google.com/kxo-misr-avz",
        BOT_RETRY: {
            "reason": "crashed",
            "after_session": SESSION,
            "workload": WORKLOAD,
            "due_at": "2026-09-29T09:13:00Z",
            "proven_gone": False,
        },
        SPAWN_SESSION: {"session": SESSION, "at": "2026-09-29T09:12:00Z"},
        UNPROVEN_TEARDOWN: [{"workload": WORKLOAD, "since": "2026-09-29T09:12:30Z"}],
    }


def _row(**over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": 42,
        "uuid": "5f0c2b7e-8d1a-4c3e-9b6f-2a7d1e4c8b90",
        "user_id": 7,
        "platform": "google_meet",
        "native_meeting_id": "kxo-misr-avz",
        "platform_specific_id": "kxo-misr-avz",
        "status": "completed",
        "start_time": "2026-09-29T09:01:00Z",
        "end_time": "2026-09-29T09:30:00Z",
        "created_at": "2026-09-29T08:55:00Z",
        "updated_at": "2026-09-29T09:30:00Z",
        "data": _data(),
    }
    row.update(over)
    return row


def _assert_absent(payload: Any) -> None:
    text = json.dumps(payload)
    for key in INTERNAL_KEYS:
        assert f'"{key}"' not in text, key
    assert WORKLOAD not in text
    assert SESSION not in text


def test_clean_meeting_data_strips_the_retry_keys():
    cleaned = clean_meeting_data(_data())
    assert set(cleaned) == {"title", "constructed_meeting_url"}


def test_the_legacy_payload_never_carries_them():
    projected = legacy_meeting_projection(_row())
    assert projected["data"] == {
        "title": "Weekly sync",
        "constructed_meeting_url": "https://meet.google.com/kxo-misr-avz",
    }
    _assert_absent(projected)


def test_the_v2_meeting_never_carries_them():
    for status in ("requested", "active", "completed", "failed"):
        meeting = project_meeting(_row(status=status), None, [], lead_s=120)
        _assert_absent(meeting)
