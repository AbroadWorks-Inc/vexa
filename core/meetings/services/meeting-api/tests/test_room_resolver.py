"""§1.6 — the one link resolver: which meeting an upstream route that takes a link means.

A link (platform + room code) holds many meetings: past ones, the live one and every scheduled
occurrence, and the newest row is usually a future one. The resolver picks the meeting each route
kind means. Three groups:

  * the pure rule (``intake.resolver``) — READ, PLANNED_EDIT and STOP over the two §6.4 shapes:
    1 live + 2 future rows, and 0 live + 1 past + 2 future rows; ``ambiguous_room``;
  * the routes — every §8.2 lookup site through the SHIPPED apps over the in-memory fakes:
    reads (transcript, participants, ``/ws/authorize-subscribe``, annotate, docs, chat), planned
    edits (``PATCH``/``DELETE``, intent, workspace, share) and ``continue_meeting``; the stop
    route (``DELETE /bots``) is driven in ``test_stop_route.py``;
  * real Postgres — the adapter query (``intake.adapters.link_rows``) and the stores built on it,
    with the same assertions as the fakes. Skips cleanly unless ``MEETING_API_TEST_DATABASE_URL``
    is set.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import pytest
from fastapi.testclient import TestClient

from intake_builders import seed_link_row
from meeting_api.collector import create_app as create_collector_app
from meeting_api.collector.fakes import InMemoryTranscriptStore
from meeting_api.intake.resolver import (
    AmbiguousRoom,
    LinkKind,
    LinkRow,
    resolve,
    resolve_all,
)

USER = 7
H = {"x-user-id": str(USER)}
PLAT, NID = "google_meet", "kxo-misr-avz"

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)


def _real_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


# ── the pure rule ────────────────────────────────────────────────────────────────────────────


def _row(
    mid: int, status: str, start: datetime, created: Optional[datetime] = None
) -> LinkRow:
    """A row whose start is its ``data.scheduled_at`` when it is ``scheduled`` (a timed plan)."""
    return LinkRow(
        id=mid,
        status=status,
        start=start,
        created=created or start - timedelta(days=30),
        planned_at=start if status == "scheduled" else None,
    )


def _live_and_two_future() -> list[LinkRow]:
    return [
        _row(1, "active", NOW - timedelta(minutes=20)),
        _row(2, "scheduled", NOW + timedelta(days=1)),
        _row(3, "scheduled", NOW + timedelta(days=8)),
    ]


def _past_and_two_future() -> list[LinkRow]:
    return [
        _row(1, "completed", NOW - timedelta(days=6)),
        _row(2, "scheduled", NOW + timedelta(days=1)),
        _row(3, "scheduled", NOW + timedelta(days=8)),
    ]


@pytest.mark.parametrize("kind", list(LinkKind))
def test_every_kind_resolves_a_link_with_a_live_meeting_to_it(kind):
    """§1.6: whatever the kind, a link with a live meeting means that meeting — never one of the
    newer scheduled occurrences (the newest row is id 3 here)."""
    assert resolve(_live_and_two_future(), kind, now=NOW).id == 1


def test_read_without_a_live_meeting_is_the_most_recent_started_never_a_future_one():
    assert resolve(_past_and_two_future(), LinkKind.READ, now=NOW).id == 1


def test_read_takes_the_most_recent_start_among_started_rows():
    rows = [
        _row(5, "completed", NOW - timedelta(days=6)),
        _row(4, "failed", NOW - timedelta(hours=2)),
        _row(6, "scheduled", NOW + timedelta(hours=1)),
    ]
    assert resolve(rows, LinkKind.READ, now=NOW).id == 4


def test_read_of_a_link_with_only_future_meetings_is_nothing():
    rows = [_row(2, "scheduled", NOW + timedelta(days=1))]
    assert resolve(rows, LinkKind.READ, now=NOW) is None


def test_a_meeting_starting_exactly_now_has_started():
    rows = [_row(2, "scheduled", NOW)]
    assert resolve(rows, LinkKind.READ, now=NOW).id == 2


def test_planned_edit_with_several_scheduled_is_ambiguous_room():
    with pytest.raises(AmbiguousRoom) as caught:
        resolve(_past_and_two_future(), LinkKind.PLANNED_EDIT, now=NOW)
    assert caught.value.code == "ambiguous_room"
    assert caught.value.meeting_ids == (2, 3)


def test_planned_edit_takes_the_single_planned_meeting_even_in_the_future():
    rows = [
        _row(1, "completed", NOW - timedelta(days=6)),
        _row(2, "scheduled", NOW + timedelta(days=1)),
    ]
    assert resolve(rows, LinkKind.PLANNED_EDIT, now=NOW).id == 2


def test_planned_edit_counts_an_idle_plan_as_planned():
    rows = [
        _row(2, "idle", NOW - timedelta(days=40)),
        _row(3, "scheduled", NOW + timedelta(days=1)),
    ]
    with pytest.raises(AmbiguousRoom):
        resolve(rows, LinkKind.PLANNED_EDIT, now=NOW)


def test_an_untimed_plan_never_goes_stale():
    """§1.6: only a timed plan (``scheduled`` with a parseable
    ``data.scheduled_at``) goes stale. An idle row, or a scheduled one whose ``scheduled_at`` is
    absent or not a time, is an untimed plan and stays editable by link however old it is.
    """
    long_ago = NOW - timedelta(days=40)
    untimed = [
        LinkRow.of(1, "idle", {}, None, long_ago),
        LinkRow.of(2, "scheduled", {"scheduled_at": "whenever"}, None, long_ago),
        LinkRow.of(3, "scheduled", {}, _iso(long_ago), long_ago),
    ]
    for row in untimed:
        assert row.planned_at is None
        assert resolve([row], LinkKind.PLANNED_EDIT, now=NOW).id == row.id
    timed = LinkRow.of(4, "scheduled", {"scheduled_at": _iso(long_ago)}, None, long_ago)
    assert timed.planned_at == long_ago


def test_planned_edit_of_a_link_holding_only_history_is_its_most_recent_started_meeting():
    """No live meeting and no plan: the finished meeting stays addressable (share its transcript,
    delete its artifacts) exactly as READ resolves it."""
    rows = [
        _row(1, "completed", NOW - timedelta(days=6)),
        _row(2, "failed", NOW - timedelta(days=1)),
    ]
    assert resolve(rows, LinkKind.PLANNED_EDIT, now=NOW).id == 2


def test_read_counts_a_meeting_that_left_planning_as_started_whatever_its_scheduled_at():
    """A bot ran on the 16:00 slot at 15:30 and it finished: its ``scheduled_at`` is still ahead,
    but it has left planning, so it has started — and it is the most recent, not last week's.
    """
    rows = [
        _row(1, "completed", NOW - timedelta(days=7)),
        _row(2, "completed", NOW + timedelta(minutes=30)),
        _row(3, "scheduled", NOW + timedelta(days=7)),
    ]
    assert resolve(rows, LinkKind.READ, now=NOW).id == 2
    assert resolve(rows[:2], LinkKind.PLANNED_EDIT, now=NOW).id == 2


def test_planned_edit_ignores_a_stale_entry_less_plan():
    """§1.6: an entry-less ``scheduled`` plan past its ``scheduled_at`` +
    ``AUTO_JOIN_GRACE_S`` can never be sent; it is a leftover, so it neither counts as the plan nor
    makes the link ambiguous."""
    rows = [
        _row(1, "scheduled", NOW - timedelta(hours=2)),
        _row(2, "scheduled", NOW + timedelta(days=1)),
    ]
    assert resolve(rows, LinkKind.PLANNED_EDIT, now=NOW).id == 2


def test_an_entry_less_plan_inside_its_grace_still_counts():
    rows = [
        _row(1, "scheduled", NOW - timedelta(minutes=5)),
        _row(2, "scheduled", NOW + timedelta(days=1)),
    ]
    with pytest.raises(AmbiguousRoom):
        resolve(rows, LinkKind.PLANNED_EDIT, now=NOW)


def test_an_entry_managed_plan_never_goes_stale():
    """Entries keep a plan sendable until its end; the not-sent sweep ends it, not the resolver."""
    stale = LinkRow(
        id=1,
        status="scheduled",
        start=NOW - timedelta(hours=2),
        created=NOW - timedelta(days=3),
        managed=True,
        planned_at=NOW - timedelta(hours=2),
    )
    with pytest.raises(AmbiguousRoom):
        resolve(
            [stale, _row(2, "scheduled", NOW + timedelta(days=1))],
            LinkKind.PLANNED_EDIT,
            now=NOW,
        )


def test_stop_never_resolves_to_a_plan():
    """§1.6: the stop means the live meeting only; it never cancels future plans."""
    assert resolve(_past_and_two_future(), LinkKind.STOP, now=NOW) is None
    assert resolve_all(_past_and_two_future(), LinkKind.STOP, now=NOW) == []


def test_stop_addresses_every_live_row_newest_first():
    """One live meeting per link is the rule (the live unique index); a duplicate live row is a
    second bot in the same call and is stopped with it."""
    rows = [
        _row(
            1, "active", NOW - timedelta(minutes=20), created=NOW - timedelta(hours=1)
        ),
        _row(
            2,
            "awaiting_admission",
            NOW - timedelta(minutes=1),
            created=NOW - timedelta(minutes=2),
        ),
        _row(3, "scheduled", NOW + timedelta(days=1)),
    ]
    assert [r.id for r in resolve_all(rows, LinkKind.STOP, now=NOW)] == [2, 1]


def test_read_and_planned_edit_address_at_most_one_row():
    assert [
        r.id for r in resolve_all(_live_and_two_future(), LinkKind.READ, now=NOW)
    ] == [1]
    assert [
        r.id for r in resolve_all(_past_and_two_future(), LinkKind.READ, now=NOW)
    ] == [1]


def test_link_row_reads_the_meeting_start_as_intake_does():
    """``data.scheduled_at``, else ``start_time``, else ``created_at`` — and an unparseable
    ``scheduled_at`` counts as absent (``rules.meeting_start``)."""
    planned = LinkRow.of(
        1,
        "scheduled",
        {"scheduled_at": "2026-10-01T09:00:00Z"},
        None,
        "2026-09-01T00:00:00Z",
    )
    assert planned.start == datetime(2026, 10, 1, 9, tzinfo=timezone.utc)
    garbled = LinkRow.of(
        2,
        "completed",
        {"scheduled_at": "tomorrow-ish"},
        "2026-09-20T10:00:00Z",
        "2026-09-01T00:00:00Z",
    )
    assert garbled.start == datetime(2026, 9, 20, 10, tzinfo=timezone.utc)
    bare = LinkRow.of(3, "idle", {}, None, datetime(2026, 9, 1))
    assert bare.start == datetime(2026, 9, 1, tzinfo=timezone.utc)
    assert bare.created == datetime(2026, 9, 1, tzinfo=timezone.utc)


# ── the routes, over the in-memory fakes ─────────────────────────────────────────────────────


class _CaptureRedis:
    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []

    async def publish(self, channel, data):
        self.published.append((channel, data))


def _collector(shape: str):
    """The collector app over a store holding one of the two §6.4 shapes on (``PLAT``, ``NID``):
    ``live`` = 1 live + 2 future rows, ``past`` = 0 live + 1 past + 2 future rows. Returns the
    client, the store and ``{"now"|"past": id, "soon": id, "later": id}``. The future rows are
    seeded LAST, so the newest row is always a future one."""
    now = _real_now()
    store = InMemoryTranscriptStore()
    ids: dict[str, int] = {}
    if shape == "live":
        ids["now"] = store.seed_meeting(
            user_id=USER,
            platform=PLAT,
            native_meeting_id=NID,
            status="active",
            start_time=_iso(now - timedelta(minutes=20)),
            created_at=_iso(now - timedelta(minutes=21)),
            segments=[
                {
                    "segment_id": "s1",
                    "start": 0.0,
                    "end": 1.0,
                    "text": "hello",
                    "speaker": "Ada",
                }
            ],
        )
    else:
        ids["past"] = store.seed_meeting(
            user_id=USER,
            platform=PLAT,
            native_meeting_id=NID,
            status="completed",
            start_time=_iso(now - timedelta(days=6)),
            end_time=_iso(now - timedelta(days=6)),
            created_at=_iso(now - timedelta(days=7)),
            segments=[
                {
                    "segment_id": "s1",
                    "start": 0.0,
                    "end": 1.0,
                    "text": "hello",
                    "speaker": "Ada",
                }
            ],
        )
    for name, days in (("soon", 1), ("later", 8)):
        ids[name] = store.seed_meeting(
            user_id=USER,
            platform=PLAT,
            native_meeting_id=NID,
            status="scheduled",
            start_time=None,
            data={"scheduled_at": _iso(now + timedelta(days=days))},
            created_at=_iso(now - timedelta(minutes=5 - days)),
        )
    redis: Any = _CaptureRedis()
    return TestClient(create_collector_app(store, redis=redis)), store, ids


def _started(ids: dict[str, int]) -> int:
    return ids["now"] if "now" in ids else ids["past"]


@pytest.mark.parametrize("shape", ["live", "past"])
def test_transcript_read_is_the_live_or_most_recent_started_meeting(shape):
    client, _store, ids = _collector(shape)
    r = client.get(f"/transcripts/{PLAT}/{NID}", headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["id"] == _started(ids)
    assert [s["text"] for s in r.json()["segments"]] == ["hello"]


@pytest.mark.parametrize("shape", ["live", "past"])
def test_participants_read_is_the_live_or_most_recent_started_meeting(shape):
    client, _store, ids = _collector(shape)
    r = client.get(f"/meetings/{PLAT}/{NID}/participants", headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["meeting_id"] == _started(ids)
    assert [p["name"] for p in r.json()["participants"]] == ["Ada"]


@pytest.mark.parametrize("shape", ["live", "past"])
def test_authorize_subscribe_is_the_live_or_most_recent_started_meeting(shape):
    client, _store, ids = _collector(shape)
    r = client.post(
        "/ws/authorize-subscribe",
        headers=H,
        json={"meetings": [{"platform": PLAT, "native_meeting_id": NID}]},
    )
    assert r.status_code == 200, r.text
    assert [a["meeting_id"] for a in r.json()["authorized"]] == [str(_started(ids))]


def test_authorize_subscribe_through_a_share_never_picks_a_future_meeting():
    """The share branch (a transcript viewer) follows the same READ rule over the rows it may see."""
    now = _real_now()
    store = InMemoryTranscriptStore()
    past = store.seed_meeting(
        user_id=99,
        platform=PLAT,
        native_meeting_id=NID,
        status="completed",
        start_time=_iso(now - timedelta(days=2)),
        data={"transcript_viewers": [USER]},
    )
    store.seed_meeting(
        user_id=99,
        platform=PLAT,
        native_meeting_id=NID,
        status="scheduled",
        start_time=None,
        data={
            "transcript_viewers": [USER],
            "scheduled_at": _iso(now + timedelta(days=1)),
        },
    )
    client = TestClient(create_collector_app(store, redis=_CaptureRedis()))
    r = client.post(
        "/ws/authorize-subscribe",
        headers=H,
        json={"meetings": [{"platform": PLAT, "native_meeting_id": NID}]},
    )
    assert [a["meeting_id"] for a in r.json()["authorized"]] == [str(past)]


@pytest.mark.parametrize("shape", ["live", "past"])
def test_annotate_writes_the_live_or_most_recent_started_meeting(shape):
    client, store, ids = _collector(shape)
    r = client.post(
        f"/meetings/{PLAT}/{NID}/annotate", headers=H, json={"title": "Acme renewal"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["id"] == _started(ids)
    assert store._meetings[ids["soon"]]["data"].get("title") is None
    assert store._meetings[ids["later"]]["data"].get("title") is None


@pytest.mark.parametrize("shape", ["live", "past"])
def test_docs_attach_to_the_live_or_most_recent_started_meeting(shape):
    client, store, ids = _collector(shape)
    r = client.post(
        f"/meetings/{PLAT}/{NID}/docs",
        headers=H,
        json={"workspace": "w", "path": "notes.md"},
    )
    assert r.status_code == 200, r.text
    assert store._meetings[_started(ids)]["data"]["docs"] == [
        {"workspace": "w", "path": "notes.md"}
    ]
    assert "docs" not in store._meetings[ids["later"]]["data"]
    r = client.request(
        "DELETE", f"/meetings/{PLAT}/{NID}/docs?path=notes.md", headers=H
    )
    assert r.status_code == 200, r.text
    assert store._meetings[_started(ids)]["data"]["docs"] == []


@pytest.mark.parametrize("shape", ["live", "past"])
def test_chat_read_is_the_live_or_most_recent_started_meeting(shape):
    """The chat reply carries no id, so the resolution is observed at the store's port."""
    client, store, ids = _collector(shape)
    seen: list[tuple[str, Optional[int]]] = []
    real = store.resolve_room

    async def recording(user_id, platform, native_meeting_id, kind):
        row = await real(user_id, platform, native_meeting_id, kind)
        seen.append((kind.value, row.id if row else None))
        return row

    store.resolve_room = recording  # type: ignore[method-assign]
    r = client.get(f"/bots/{PLAT}/{NID}/chat", headers=H)
    assert r.status_code == 200, r.text
    assert r.json() == {"messages": []}
    assert seen == [("read", _started(ids))]


def test_transcript_of_a_meeting_that_ran_ahead_of_its_slot_is_the_recent_one():
    """A bot ran and finished before its ``scheduled_at`` (30 min ahead): GET /transcripts serves
    that meeting, not last week's."""
    now = _real_now()
    store = InMemoryTranscriptStore()
    store.seed_meeting(
        user_id=USER,
        platform=PLAT,
        native_meeting_id=NID,
        status="completed",
        start_time=_iso(now - timedelta(days=7)),
        created_at=_iso(now - timedelta(days=8)),
        data={"scheduled_at": _iso(now - timedelta(days=7))},
    )
    recent = store.seed_meeting(
        user_id=USER,
        platform=PLAT,
        native_meeting_id=NID,
        status="completed",
        start_time=_iso(now - timedelta(minutes=20)),
        created_at=_iso(now - timedelta(days=1)),
        data={"scheduled_at": _iso(now + timedelta(minutes=30))},
    )
    redis: Any = _CaptureRedis()
    client = TestClient(create_collector_app(store, redis=redis))
    r = client.get(f"/transcripts/{PLAT}/{NID}", headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["id"] == recent


def test_planned_edit_skips_a_stale_entry_less_plan():
    """§1.6 through the route: a missed entry-less occurrence (2 h ago) next to the next
    plan does not make the link ambiguous; PATCH edits the next plan."""
    now = _real_now()
    store = InMemoryTranscriptStore()
    stale = store.seed_meeting(
        user_id=USER,
        platform=PLAT,
        native_meeting_id=NID,
        status="scheduled",
        start_time=None,
        data={"scheduled_at": _iso(now - timedelta(hours=2))},
    )
    plan = store.seed_meeting(
        user_id=USER,
        platform=PLAT,
        native_meeting_id=NID,
        status="scheduled",
        start_time=None,
        data={"scheduled_at": _iso(now + timedelta(days=1))},
    )
    redis: Any = _CaptureRedis()
    client = TestClient(create_collector_app(store, redis=redis))
    r = client.patch(f"/meetings/{PLAT}/{NID}", headers=H, json={"title": "Next"})
    assert r.status_code == 200, r.text
    assert r.json()["id"] == plan
    assert store._meetings[stale]["data"].get("title") is None


def test_planned_edit_reaches_a_long_past_idle_plan_next_to_a_newer_finished_meeting():
    """An untimed idle plan created long ago is still the link's plan: a native PATCH edits it,
    not the finished meeting held on the same link since (§1.6)."""
    now = _real_now()
    store = InMemoryTranscriptStore()
    idle = store.seed_meeting(
        user_id=USER,
        platform=PLAT,
        native_meeting_id=NID,
        status="idle",
        start_time=None,
        created_at=_iso(now - timedelta(days=40)),
    )
    store.seed_meeting(
        user_id=USER,
        platform=PLAT,
        native_meeting_id=NID,
        status="completed",
        start_time=_iso(now - timedelta(days=2)),
        created_at=_iso(now - timedelta(days=2)),
    )
    redis: Any = _CaptureRedis()
    client = TestClient(create_collector_app(store, redis=redis))
    r = client.patch(f"/meetings/{PLAT}/{NID}", headers=H, json={"title": "Planning"})
    assert r.status_code == 200, r.text
    assert r.json()["id"] == idle


def test_reads_of_a_link_holding_only_future_meetings_404():
    """Never a future one: a room whose every meeting is ahead has nothing to read yet."""
    now = _real_now()
    store = InMemoryTranscriptStore()
    store.seed_meeting(
        user_id=USER,
        platform=PLAT,
        native_meeting_id=NID,
        status="scheduled",
        start_time=None,
        data={"scheduled_at": _iso(now + timedelta(days=1))},
    )
    client = TestClient(create_collector_app(store, redis=_CaptureRedis()))
    assert client.get(f"/transcripts/{PLAT}/{NID}", headers=H).status_code == 404
    assert (
        client.get(f"/meetings/{PLAT}/{NID}/participants", headers=H).status_code == 404
    )


# PLANNED_EDIT with a live meeting: the live one. With 0 live + 1 past + 2 future: ambiguous_room.


def test_share_with_a_live_meeting_mints_on_it():
    client, store, ids = _collector("live")
    r = client.post(f"/meetings/{PLAT}/{NID}/share", headers=H, json={})
    assert r.status_code == 200, r.text
    assert r.json()["token"].startswith(f"{ids['now']}.")
    assert "share_grants" not in store._meetings[ids["later"]]["data"]


def test_workspace_bind_with_a_live_meeting_binds_it():
    client, store, ids = _collector("live")
    r = client.post(
        f"/meetings/{PLAT}/{NID}/workspace", headers=H, json={"workspace_id": "ws-1"}
    )
    assert r.status_code == 200, r.text
    assert store._meetings[ids["now"]]["data"]["workspace_id"] == "ws-1"
    assert "workspace_id" not in store._meetings[ids["later"]]["data"]


def test_intent_with_a_live_meeting_addresses_it():
    client, store, ids = _collector("live")
    r = client.put(f"/meetings/{PLAT}/{NID}/intent", headers=H, json={"intent": "idle"})
    assert r.status_code == 200, r.text
    assert r.json()["meeting_id"] == ids["now"]
    assert store._meetings[ids["later"]]["status"] == "scheduled"


def test_patch_and_delete_with_a_live_meeting_address_it():
    """The live meeting is the one addressed, and the bot lifecycle owns it: 409, the plans
    untouched — upstream's own refusal, now reached on the right row."""
    client, store, ids = _collector("live")
    r = client.patch(f"/meetings/{PLAT}/{NID}", headers=H, json={"title": "x"})
    assert r.status_code == 409, r.text
    assert "no longer planned" in r.json()["detail"]
    r = client.delete(f"/meetings/{PLAT}/{NID}", headers=H)
    assert r.status_code == 409, r.text
    assert {store._meetings[ids[k]]["status"] for k in ("soon", "later")} == {
        "scheduled"
    }
    assert store._meetings[ids["soon"]]["data"].get("title") is None


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("PATCH", f"/meetings/{PLAT}/{NID}", {"title": "x"}),
        ("DELETE", f"/meetings/{PLAT}/{NID}", None),
        ("PUT", f"/meetings/{PLAT}/{NID}/intent", {"intent": "idle"}),
        ("POST", f"/meetings/{PLAT}/{NID}/workspace", {"workspace_id": "ws-1"}),
        ("POST", f"/meetings/{PLAT}/{NID}/share", {}),
    ],
)
def test_planned_edits_on_several_scheduled_meetings_answer_409_ambiguous_room(
    method, path, body
):
    """§1.6: 0 live + 1 past + 2 future — two scheduled meetings, so the link names no single plan.
    Upstream's error shape: the code is the ``detail`` value. Nothing is written."""
    client, store, ids = _collector("past")
    before = json.dumps(store._meetings, sort_keys=True, default=str)
    r = client.request(method, path, headers=H, json=body)
    assert r.status_code == 409, r.text
    assert r.json() == {"detail": "ambiguous_room"}
    assert json.dumps(store._meetings, sort_keys=True, default=str) == before


def test_planned_edit_with_a_single_plan_addresses_it():
    now = _real_now()
    store = InMemoryTranscriptStore()
    store.seed_meeting(
        user_id=USER,
        platform=PLAT,
        native_meeting_id=NID,
        status="completed",
        start_time=_iso(now - timedelta(days=6)),
    )
    plan = store.seed_meeting(
        user_id=USER,
        platform=PLAT,
        native_meeting_id=NID,
        status="scheduled",
        start_time=None,
        data={"scheduled_at": _iso(now + timedelta(days=1))},
    )
    client = TestClient(create_collector_app(store, redis=_CaptureRedis()))
    r = client.patch(f"/meetings/{PLAT}/{NID}", headers=H, json={"title": "Standup"})
    assert r.status_code == 200, r.text
    assert r.json()["id"] == plan


def test_planned_edit_on_history_only_still_reaches_the_finished_meeting():
    """No live meeting, no plan: share mints on the most recent finished meeting, as before."""
    now = _real_now()
    store = InMemoryTranscriptStore()
    done = store.seed_meeting(
        user_id=USER,
        platform=PLAT,
        native_meeting_id=NID,
        status="completed",
        start_time=_iso(now - timedelta(days=2)),
    )
    client = TestClient(create_collector_app(store, redis=_CaptureRedis()))
    r = client.post(f"/meetings/{PLAT}/{NID}/share", headers=H, json={})
    assert r.status_code == 200, r.text
    assert r.json()["token"].startswith(f"{done}.")


# STOP (DELETE /bots/{p}/{n}, the live meeting only) is driven through the route in
# test_stop_route.py; continue_meeting (find_latest) is the READ rule.


def _seed_bot_row(
    repo,
    mid: int,
    status: str,
    *,
    start: Optional[datetime] = None,
    scheduled_at: Optional[datetime] = None,
) -> int:
    now = _real_now()
    repo._meetings[mid] = {
        "id": mid,
        "user_id": USER,
        "platform": PLAT,
        "native_meeting_id": NID,
        "platform_specific_id": NID,
        "status": status,
        "bot_container_id": None,
        "start_time": _iso(start) if start else None,
        "end_time": None,
        "data": {"scheduled_at": _iso(scheduled_at)} if scheduled_at else {},
        "created_at": _iso(now - timedelta(days=10) + timedelta(minutes=mid)),
        "updated_at": _iso(now),
    }
    repo._next_id = max(repo._next_id, mid + 1)
    return mid


@pytest.mark.parametrize("shape", ["live", "past"])
def test_find_latest_is_the_live_or_most_recent_started_meeting(shape):
    from meeting_api.bot_spawn.fakes import InMemoryMeetingRepo

    repo = InMemoryMeetingRepo()
    now = _real_now()
    first = _seed_bot_row(
        repo,
        1,
        "active" if shape == "live" else "completed",
        start=now - timedelta(minutes=20),
    )
    _seed_bot_row(repo, 2, "scheduled", scheduled_at=now + timedelta(days=1))
    _seed_bot_row(repo, 3, "scheduled", scheduled_at=now + timedelta(days=8))
    assert asyncio.run(repo.find_latest(USER, PLAT, NID))["id"] == first


def test_continue_meeting_reuses_the_past_meeting_not_a_future_plan(monkeypatch):
    from meeting_api.bot_spawn import request_bot
    from meeting_api.bot_spawn.fakes import FakeRuntimeClient, InMemoryMeetingRepo

    monkeypatch.setenv("ADMIN_TOKEN", "test-admin-token")
    repo, runtime = InMemoryMeetingRepo(), FakeRuntimeClient()
    now = _real_now()
    past = _seed_bot_row(repo, 1, "completed", start=now - timedelta(days=6))
    _seed_bot_row(repo, 2, "scheduled", scheduled_at=now + timedelta(days=1))
    _seed_bot_row(repo, 3, "scheduled", scheduled_at=now + timedelta(days=8))

    row = asyncio.run(
        request_bot(
            repo,
            runtime,
            user_id=USER,
            platform=PLAT,
            native_meeting_id=NID,
            token_secret="s",
            redis_url="redis://r",
            continue_meeting=True,
        )
    )
    assert row["id"] == past
    assert repo.reopened == [past]


# ── real Postgres ────────────────────────────────────────────────────────────────────────────


async def _pg_shape(engine: Any, shape: str) -> dict[str, int]:
    now = _real_now()
    ids: dict[str, int] = {}
    if shape == "live":
        ids["now"] = await seed_link_row(
            engine,
            "active",
            start_time=now - timedelta(minutes=20),
            created_at=now - timedelta(minutes=21),
        )
    else:
        ids["past"] = await seed_link_row(
            engine,
            "completed",
            start_time=now - timedelta(days=6),
            created_at=now - timedelta(days=7),
        )
    ids["soon"] = await seed_link_row(
        engine, "scheduled", scheduled_at=_iso(now + timedelta(days=1)), managed=True
    )
    ids["later"] = await seed_link_row(
        engine, "scheduled", scheduled_at=_iso(now + timedelta(days=8))
    )
    return ids


async def test_pg_link_rows_reads_each_row_as_the_fake_does(link_pg_engine):
    """The adapter query: every row on the user's link — none of another user's or another
    link's — with the intake meeting start and whether entries manage it."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from meeting_api.intake.adapters import link_rows
    from meeting_api.intake.ports import Room

    now = _real_now()
    ids = await _pg_shape(link_pg_engine, "past")
    garbled = await seed_link_row(
        link_pg_engine,
        "failed",
        scheduled_at="soon-ish",
        start_time=now - timedelta(hours=3),
    )
    await seed_link_row(link_pg_engine, "active", user_id=USER + 1)
    await seed_link_row(link_pg_engine, "active", native="abc-defg-hij")

    session_factory = async_sessionmaker(link_pg_engine, expire_on_commit=False)
    async with session_factory() as db:
        rows = {r.id: r for r in await link_rows(db, USER, Room(PLAT, NID))}
    assert set(rows) == {ids["past"], ids["soon"], ids["later"], garbled}
    assert rows[ids["soon"]].managed is True
    assert rows[ids["later"]].managed is False
    assert rows[ids["soon"]].status == "scheduled"
    assert abs(rows[ids["soon"]].start - (now + timedelta(days=1))) < timedelta(
        seconds=1
    )
    assert abs(rows[ids["past"]].start - (now - timedelta(days=6))) < timedelta(
        seconds=1
    )
    assert abs(rows[garbled].start - (now - timedelta(hours=3))) < timedelta(seconds=1)
    assert all(r.created is not None for r in rows.values())


@pytest.mark.parametrize("shape", ["live", "past"])
async def test_pg_store_resolves_each_kind_as_the_fake_does(link_pg_engine, shape):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from meeting_api.collector.adapters import SqlAlchemyTranscriptStore

    ids = await _pg_shape(link_pg_engine, shape)
    store = SqlAlchemyTranscriptStore(
        async_sessionmaker(link_pg_engine, expire_on_commit=False)
    )
    started = ids.get("now", ids.get("past"))

    assert (await store.resolve_room(USER, PLAT, NID, LinkKind.READ)).id == started
    assert (await store.get_transcript(USER, PLAT, NID))["id"] == started
    assert (await store.get_meeting_participants(USER, PLAT, NID))[
        "meeting_id"
    ] == started
    assert await store.authorize_subscribe(USER, PLAT, NID) == started
    assert await store.connect_doc(
        USER, PLAT, NID, {"workspace": "w", "path": "a.md"}
    ) == [{"workspace": "w", "path": "a.md"}]
    if shape == "live":
        assert (
            await store.resolve_room(USER, PLAT, NID, LinkKind.PLANNED_EDIT)
        ).id == started
        minted = await store.mint_transcript_share(USER, PLAT, NID)
        assert minted["token"].startswith(f"{started}.")
        assert await store.bind_workspace(USER, PLAT, NID, "ws-1") == "ws-1"
    else:
        for call in (
            store.resolve_room(USER, PLAT, NID, LinkKind.PLANNED_EDIT),
            store.mint_transcript_share(USER, PLAT, NID),
            store.bind_workspace(USER, PLAT, NID, "ws-1"),
            store.set_intent(USER, PLAT, NID, "idle"),
        ):
            with pytest.raises(AmbiguousRoom):
                await call
    stop = await store.resolve_room(USER, PLAT, NID, LinkKind.STOP)
    assert (stop.id if stop else None) == ids.get("now")


@pytest.mark.parametrize("shape", ["live", "past"])
async def test_pg_find_latest_is_the_read_rule(link_pg_engine, shape):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from meeting_api.bot_spawn.adapters import SqlAlchemyMeetingRepo

    ids = await _pg_shape(link_pg_engine, shape)
    repo = SqlAlchemyMeetingRepo(
        async_sessionmaker(link_pg_engine, expire_on_commit=False)
    )
    assert (await repo.find_latest(USER, PLAT, NID))["id"] == ids.get(
        "now", ids.get("past")
    )
