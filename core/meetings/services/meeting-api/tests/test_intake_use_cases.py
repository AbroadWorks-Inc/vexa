"""§2.6 — every use case of `PUT /v2/entries` and `POST /v2/entries/remove`, one test each.

Driven through `IntakeService` over the in-memory fakes (`meeting_api.intake.fakes`). Each test
checks the reply, the status, the outcome with its message, the entries, the ordered events, and
the spawns and stops.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from intake_builders import (
    A,
    B,
    GMEET,
    GMEET_OTHER,
    ZOOM,
    make_harness,
    ts,
)
from meeting_api.intake import IntakeError
from meeting_api.intake.ports import Room, SpawnOutcome
from meeting_api.intake.rules import Plan
from meeting_api.intake.status import Outcome
from meeting_api.intake.validation import parse_entry

GROOM = Room("google_meet", "kxo-misr-avz")
GROOM_OTHER = Room("google_meet", "abc-defg-hij")
REMOVED_CANCELLED = {
    "kind": "cancelled_by_calendar",
    "detail": "cancelled",
    "message": "last entry removed: cancelled",
}


def _outcome(reply: dict) -> dict | None:
    """The reply's outcome without its stamp (the stamp is checked where it matters)."""
    out = reply["meeting"]["outcome"]
    return None if out is None else {k: v for k, v in out.items() if k != "at"}


def _users(reply: dict) -> list[str]:
    return [e["user"] for e in reply["meeting"]["entries"]]


# ── 2.6.1 ────────────────────────────────────────────────────────────────────────────────────


async def test_2_6_1_one_off_created():
    h = make_harness()
    reply = await h.put(
        time_zone="Asia/Kolkata",
        title="Weekly sync",
        attendees=[A, B, "c@client.com"],
    )
    uuid = reply["meeting"]["id"]
    assert reply == {
        "result": "created",
        "previous_meeting_id": None,
        "entry": {"external_id": "google:3n5kq8example", "user": A, "state": "active"},
        "meeting": {
            "id": uuid,
            "status": "scheduled",
            "completion_reason": None,
            "failure_stage": None,
            "outcome": None,
            "platform": "google_meet",
            "room": "kxo-misr-avz",
            "meeting_url": GMEET,
            "title": "Weekly sync",
            "start": "2026-09-29T09:00:00Z",
            "end": "2026-09-29T09:30:00Z",
            "time_zone": "Asia/Kolkata",
            "bot_joins_at": "2026-09-29T08:55:00Z",
            "entries": [
                {
                    "external_id": "google:3n5kq8example",
                    "user": A,
                    "attendees": [A, B, "c@client.com"],
                    "series_id": None,
                    "metadata": None,
                }
            ],
            "export": None,
            "sequence": 1,
        },
    }
    assert h.meeting(uuid).data["auto_join"] is True
    assert h.events() == [(uuid, "meeting.scheduled")]
    assert h.published() == [h.store.events[0].event_id]
    assert h.spawn.calls == [] and h.stop.calls == []


# ── 2.6.2 ────────────────────────────────────────────────────────────────────────────────────


async def test_2_6_2_instant_join_new():
    h = make_harness()
    reply = await h.instant("manual:0f9d1c2a-6b7e-4f3a-9c1d-8e2b5a7f4c61")
    m = reply["meeting"]
    assert reply["result"] == "created"
    assert reply["entry"]["state"] == "active"
    assert (m["status"], m["platform"], m["room"]) == (
        "requested",
        "zoom",
        "12345678901",
    )
    assert (m["start"], m["end"], m["outcome"]) == ("2026-09-26T12:00:00Z", None, None)
    assert m["meeting_url"] == ZOOM
    assert _users(reply) == [A]
    assert h.events() == [
        (m["id"], "meeting.scheduled"),
        (m["id"], "meeting.status_change"),
    ]
    assert h.spawn.calls == [(1, h.meeting_id(m["id"]))]
    assert h.published() == [h.store.events[0].event_id]
    assert h.stop.calls == []


async def test_2_6_2_instant_join_unrecognized_link():
    h = make_harness()
    with pytest.raises(IntakeError) as err:
        await h.instant("manual:1", "https://example.com/not-a-meeting")
    assert (err.value.code, err.value.http_status) == ("unrecognized_link", 400)
    assert h.store.meetings == {} and h.store.entries == {} and h.events() == []
    assert h.spawn.calls == [] and h.published() == []


async def test_2_6_2_instant_join_spawn_fails():
    failure = SpawnOutcome("failed", "account_limit", "bot limit reached (45 of 45)")
    h = make_harness(spawn_failure=failure)
    reply = await h.instant("manual:1")
    m = reply["meeting"]
    assert reply["result"] == "created"
    assert (m["status"], m["completion_reason"]) == ("failed", None)
    assert m["outcome"] == {
        "kind": "not_sent",
        "detail": "account_limit",
        "message": "bot limit reached (45 of 45)",
        "at": "2026-09-26T12:00:00Z",
    }
    assert reply["entry"]["state"] == "closed"
    assert _users(reply) == [A]
    assert h.events() == [(m["id"], "meeting.scheduled"), (m["id"], "meeting.not_sent")]
    assert h.published() == [e.event_id for e in h.store.events]
    assert h.spawn.calls == [(1, h.meeting_id(m["id"]))] and h.stop.calls == []


async def test_2_6_2_instant_join_scheduler_won():
    h = make_harness()
    h.spawn.before = lambda mid: h.store.write_status(
        mid, "requested", expected_from={"scheduled"}
    )
    reply = await h.instant("manual:1")
    m = reply["meeting"]
    assert reply["result"] == "joined_existing"
    assert (m["status"], m["outcome"]) == ("requested", None)
    assert h.events() == [
        (m["id"], "meeting.scheduled"),
        (m["id"], "meeting.status_change"),
    ]
    assert len(h.spawn.calls) == 1 and h.stop.calls == []


# ── 2.6.3 ────────────────────────────────────────────────────────────────────────────────────


async def test_2_6_3_adopts_0945_for_1000():
    h = make_harness("2026-09-29T09:00:00Z")
    planned = await h.put(start="2026-09-29T10:00:00Z", end="2026-09-29T10:30:00Z")
    uuid = planned["meeting"]["id"]
    h.clock.set("2026-09-29T09:45:00Z")
    mark = h.mark()
    reply = await h.instant("manual:1", GMEET)
    m = reply["meeting"]
    assert (reply["result"], m["id"], reply["previous_meeting_id"]) == (
        "joined_existing",
        uuid,
        None,
    )
    assert (m["status"], m["start"], m["end"]) == (
        "requested",
        "2026-09-29T09:45:00Z",
        None,
    )
    assert [e["external_id"] for e in m["entries"]] == [
        "google:3n5kq8example",
        "manual:1",
    ]
    assert h.events(mark) == [
        (uuid, "meeting.updated"),
        (uuid, "meeting.status_change"),
    ]
    assert h.spawn.calls == [(1, h.meeting_id(uuid))] and h.stop.calls == []
    assert len(h.store.meetings) == 1


async def test_2_6_3_live_no_second_bot():
    h = make_harness("2026-09-29T09:00:00Z")
    uuid = (await h.put(start="2026-09-29T10:00:00Z", end="2026-09-29T10:30:00Z"))[
        "meeting"
    ]["id"]
    h.clock.set("2026-09-29T10:05:00Z")
    h.set_status(uuid, "active")
    h.clock.set("2026-09-29T10:10:00Z")
    mark = h.mark()
    reply = await h.instant("manual:1", GMEET)
    m = reply["meeting"]
    assert (reply["result"], m["id"], m["status"]) == (
        "joined_existing",
        uuid,
        "active",
    )
    assert (m["start"], m["end"]) == ("2026-09-29T10:00:00Z", "2026-09-29T10:30:00Z")
    assert len(m["entries"]) == 2
    assert h.events(mark) == [(uuid, "meeting.updated")]
    assert h.spawn.calls == [] and h.stop.calls == []
    assert len(h.store.meetings) == 1


async def test_2_6_3_paste_1020_not_tomorrow():
    h = make_harness("2026-09-28T12:00:00Z")
    today = (
        await h.put(
            "google:standup_0929",
            start="2026-09-29T10:00:00Z",
            end="2026-09-29T10:15:00Z",
        )
    )["meeting"]["id"]
    tomorrow = (
        await h.put(
            "google:standup_0930",
            start="2026-09-30T10:00:00Z",
            end="2026-09-30T10:15:00Z",
        )
    )["meeting"]["id"]
    h.clock.set("2026-09-29T10:16:00Z")
    h.set_status(today, "completed")
    h.clock.set("2026-09-29T10:20:00Z")
    mark = h.mark()
    reply = await h.instant("manual:1", GMEET)
    m = reply["meeting"]
    assert reply["result"] == "created"
    assert m["id"] not in (today, tomorrow)
    assert (m["status"], m["start"], m["end"]) == (
        "requested",
        "2026-09-29T10:20:00Z",
        None,
    )
    assert h.events(mark) == [
        (m["id"], "meeting.scheduled"),
        (m["id"], "meeting.status_change"),
    ]
    assert h.meeting(tomorrow).status == "scheduled"
    assert [e.external_id for e in h.meeting(tomorrow).active_entries()] == [
        "google:standup_0930"
    ]
    assert h.spawn.calls == [(1, h.meeting_id(m["id"]))]


# ── 2.6.4 / 2.6.5 ────────────────────────────────────────────────────────────────────────────


async def test_2_6_4_series_one_meeting_per_occurrence():
    h = make_harness()
    uuids = []
    for day in range(29, 39):
        date = f"2026-{9 if day <= 30 else 10:02d}-{day if day <= 30 else day - 30:02d}"
        reply = await h.put(
            f"google:9dstandupexample_{date.replace('-', '')}T043000Z",
            meeting_url=GMEET_OTHER,
            start=f"{date}T04:30:00Z",
            end=f"{date}T04:45:00Z",
            time_zone="Asia/Kolkata",
            title="Daily standup",
            series_id="google:9dstandupexample",
        )
        assert reply["result"] == "created"
        assert reply["meeting"]["status"] == "scheduled"
        assert [e["series_id"] for e in reply["meeting"]["entries"]] == [
            "google:9dstandupexample"
        ]
        uuids.append(reply["meeting"]["id"])
    assert len(set(uuids)) == 10
    assert h.events() == [(u, "meeting.scheduled") for u in uuids]
    assert h.spawn.calls == [] and h.stop.calls == []


async def test_2_6_5_occurrence_moved():
    h = make_harness()
    uuid = (await h.put())["meeting"]["id"]
    reply = await h.put(start="2026-09-30T11:00:00Z", end="2026-09-30T11:30:00Z")
    m = reply["meeting"]
    assert (reply["result"], m["id"], reply["previous_meeting_id"]) == (
        "updated",
        uuid,
        None,
    )
    assert (m["status"], m["outcome"]) == ("scheduled", None)
    assert (m["start"], m["end"], m["bot_joins_at"]) == (
        "2026-09-30T11:00:00Z",
        "2026-09-30T11:30:00Z",
        "2026-09-30T10:55:00Z",
    )
    assert _users(reply) == [A]
    assert h.events() == [(uuid, "meeting.scheduled"), (uuid, "meeting.updated")]
    assert h.spawn.calls == [] and h.stop.calls == []


# ── 2.6.6 ────────────────────────────────────────────────────────────────────────────────────


async def test_2_6_6_cancelled():
    h = make_harness()
    uuid = (await h.put("google:9dstandupexample_20260930T043000Z"))["meeting"]["id"]
    reply = await h.remove(
        "google:9dstandupexample_20260930T043000Z", reason="cancelled"
    )
    m = reply["meeting"]
    assert (reply["result"], m["id"]) == ("removed", uuid)
    assert reply["entry"] == {
        "external_id": "google:9dstandupexample_20260930T043000Z",
        "user": A,
        "state": "removed",
    }
    assert (m["status"], m["completion_reason"]) == ("failed", "stopped")
    assert _outcome(reply) == REMOVED_CANCELLED
    assert m["outcome"]["at"] == "2026-09-26T12:00:00Z"
    assert m["entries"] == []
    assert h.events() == [(uuid, "meeting.scheduled"), (uuid, "meeting.removed")]
    assert h.published() == [e.event_id for e in h.store.events]
    assert h.spawn.calls == [] and h.stop.calls == []


async def test_2_6_6_cancelled_other_entry_remains():
    h = make_harness()
    uuid = (await h.put())["meeting"]["id"]
    await h.put(user=B)
    reply = await h.remove(reason="cancelled")
    m = reply["meeting"]
    assert (reply["result"], m["id"], m["status"], m["outcome"]) == (
        "entry_removed",
        uuid,
        "scheduled",
        None,
    )
    assert reply["entry"]["state"] == "removed"
    assert _users(reply) == [B]
    assert h.events() == [
        (uuid, "meeting.scheduled"),
        (uuid, "meeting.updated"),
        (uuid, "meeting.updated"),
    ]
    assert h.spawn.calls == [] and h.stop.calls == []


async def test_2_6_6_cancelled_live_last_entry():
    h = make_harness()
    uuid = (await h.put())["meeting"]["id"]
    h.clock.set("2026-09-29T09:01:00Z")
    h.set_status(uuid, "active")
    mark = h.mark()
    reply = await h.remove(reason="cancelled")
    m = reply["meeting"]
    assert (reply["result"], m["id"], m["status"]) == ("bot_stopping", uuid, "stopping")
    assert _outcome(reply) == REMOVED_CANCELLED
    assert m["entries"] == []
    assert h.events(mark) == [
        (uuid, "meeting.updated"),
        (uuid, "meeting.status_change"),
    ]
    assert h.stop.calls == [
        (
            1,
            h.meeting_id(uuid),
            Outcome(
                "cancelled_by_calendar", "cancelled", "last entry removed: cancelled"
            ),
        )
    ]
    assert h.spawn.calls == []


# ── 2.6.7 / 2.6.8 ────────────────────────────────────────────────────────────────────────────


def _occurrence(n: int, *, hour: int = 4) -> dict:
    day = 29 + n
    return {
        "start": (
            f"2026-10-{day - 30:02d}T{hour:02d}:30:00Z"
            if day > 30
            else f"2026-09-{day:02d}T{hour:02d}:30:00Z"
        ),
        "end": (
            f"2026-10-{day - 30:02d}T{hour:02d}:45:00Z"
            if day > 30
            else f"2026-09-{day:02d}T{hour:02d}:45:00Z"
        ),
        "series_id": "google:series",
    }


async def test_2_6_7_new_ids():
    h = make_harness()
    old = [
        (await h.put(f"google:old_{n}", **_occurrence(n)))["meeting"]["id"]
        for n in range(3)
    ]
    mark = h.mark()
    removed = [await h.remove(f"google:old_{n}", reason="cancelled") for n in range(3)]
    created = [
        await h.put(f"google:new_{n}", **_occurrence(n, hour=6)) for n in range(3)
    ]
    assert [r["result"] for r in removed] == ["removed"] * 3
    assert [r["meeting"]["id"] for r in removed] == old
    assert all(_outcome(r) == REMOVED_CANCELLED for r in removed)
    assert [r["result"] for r in created] == ["created"] * 3
    new = [r["meeting"]["id"] for r in created]
    assert not set(new) & set(old)
    assert [r["meeting"]["start"] for r in created] == [
        "2026-09-29T06:30:00Z",
        "2026-09-30T06:30:00Z",
        "2026-10-01T06:30:00Z",
    ]
    assert h.events(mark) == [(u, "meeting.removed") for u in old] + [
        (u, "meeting.scheduled") for u in new
    ]
    assert h.spawn.calls == [] and h.stop.calls == []


async def test_2_6_7_same_ids():
    h = make_harness()
    old = [
        (await h.put(f"google:occ_{n}", **_occurrence(n)))["meeting"]["id"]
        for n in range(3)
    ]
    mark = h.mark()
    replies = [
        await h.put(f"google:occ_{n}", **_occurrence(n, hour=6)) for n in range(3)
    ]
    assert [r["result"] for r in replies] == ["updated"] * 3
    assert [r["meeting"]["id"] for r in replies] == old
    assert [r["meeting"]["bot_joins_at"] for r in replies] == [
        "2026-09-29T06:25:00Z",
        "2026-09-30T06:25:00Z",
        "2026-10-01T06:25:00Z",
    ]
    assert all(r["meeting"]["status"] == "scheduled" for r in replies)
    assert h.events(mark) == [(u, "meeting.updated") for u in old]
    assert h.spawn.calls == [] and h.stop.calls == []


async def test_2_6_7_moved_out_and_back():
    h = make_harness()
    first = (await h.put())["meeting"]["id"]
    out = await h.remove(reason="moved_out_of_window")
    assert (out["result"], out["meeting"]["status"]) == ("removed", "failed")
    assert _outcome(out) == {
        "kind": "cancelled_by_calendar",
        "detail": "moved_out_of_window",
        "message": "last entry removed: moved_out_of_window",
    }
    mark = h.mark()
    back = await h.put()
    m = back["meeting"]
    assert (back["result"], back["previous_meeting_id"]) == ("created", first)
    assert m["id"] != first
    assert (m["status"], m["outcome"], back["entry"]["state"]) == (
        "scheduled",
        None,
        "active",
    )
    assert h.meeting(first).status == "failed"
    assert h.events(mark) == [(m["id"], "meeting.scheduled")]
    assert h.spawn.calls == [] and h.stop.calls == []


async def test_2_6_8_series_cancelled():
    h = make_harness()
    uuids = [
        (await h.put(f"google:occ_{n}", **_occurrence(n)))["meeting"]["id"]
        for n in range(3)
    ]
    mark = h.mark()
    replies = [await h.remove(f"google:occ_{n}", reason="cancelled") for n in range(3)]
    assert [r["result"] for r in replies] == ["removed"] * 3
    assert [
        (r["meeting"]["status"], r["meeting"]["completion_reason"]) for r in replies
    ] == [("failed", "stopped")] * 3
    assert all(_outcome(r) == REMOVED_CANCELLED for r in replies)
    assert h.events(mark) == [(u, "meeting.removed") for u in uuids]
    assert h.spawn.calls == [] and h.stop.calls == []


# ── 2.6.9 / 2.6.10 ───────────────────────────────────────────────────────────────────────────


async def test_2_6_9_link_changed_moves():
    h = make_harness()
    uuid = (await h.put())["meeting"]["id"]
    locks_before = len(h.store.lock_log)
    reply = await h.put(meeting_url=GMEET_OTHER)
    m = reply["meeting"]
    assert (reply["result"], m["id"], reply["previous_meeting_id"]) == (
        "updated",
        uuid,
        None,
    )
    assert (m["room"], m["meeting_url"], m["status"]) == (
        "abc-defg-hij",
        GMEET_OTHER,
        "scheduled",
    )
    assert _users(reply) == [A]
    # first under the new link's lock; the entry was on another link → restart under both, sorted
    assert h.store.lock_log[locks_before:] == [
        (1, (GROOM_OTHER,)),
        (1, (GROOM_OTHER, GROOM)),
    ]
    assert h.events() == [(uuid, "meeting.scheduled"), (uuid, "meeting.updated")]
    assert h.spawn.calls == [] and h.stop.calls == []


async def test_2_6_9_link_changed_joins_other():
    h = make_harness()
    old = (await h.put("google:a1"))["meeting"]["id"]
    other = (await h.put("google:b1", user=B, meeting_url=GMEET_OTHER))["meeting"]["id"]
    reply = await h.put("google:a1", meeting_url=GMEET_OTHER)
    m = reply["meeting"]
    assert (reply["result"], m["id"], reply["previous_meeting_id"]) == (
        "updated",
        other,
        old,
    )
    assert sorted(_users(reply)) == [A, B]
    left = h.meeting(old)
    assert (left.status, left.data["completion_reason"]) == ("failed", "stopped")
    assert (
        left.aw["outcome_kind"],
        left.aw["outcome_detail"],
        left.aw["outcome_message"],
    ) == (
        "cancelled_by_calendar",
        "entry_moved",
        "last entry moved to another meeting",
    )
    assert h.events() == [
        (old, "meeting.scheduled"),
        (other, "meeting.scheduled"),
        (other, "meeting.updated"),
        (old, "meeting.removed"),
    ]
    assert h.spawn.calls == [] and h.stop.calls == []


async def test_2_6_10_title_changed():
    h = make_harness()
    uuid = (await h.put(title="Weekly sync"))["meeting"]["id"]
    reply = await h.put(title="Weekly sync (agenda)")
    assert (reply["result"], reply["meeting"]["id"]) == ("updated", uuid)
    assert reply["meeting"]["title"] == "Weekly sync (agenda)"
    assert h.events() == [(uuid, "meeting.scheduled"), (uuid, "meeting.updated")]

    h.clock.set("2026-09-29T09:01:00Z")
    h.set_status(uuid, "active")
    mark = h.mark()
    live = await h.put(title="Renamed while live")
    assert (live["result"], live["meeting"]["id"], live["meeting"]["status"]) == (
        "not_changed_live",
        uuid,
        "active",
    )
    assert live["meeting"]["title"] == "Weekly sync (agenda)"
    stored = h.store.entries_of(h.meeting_id(uuid))
    assert [e.title for e in stored] == ["Renamed while live"]
    assert h.events(mark) == []
    assert h.spawn.calls == [] and h.stop.calls == []


# ── 2.6.11 / 2.6.12 ──────────────────────────────────────────────────────────────────────────


async def test_2_6_11_declined_one_of_two():
    h = make_harness()
    uuid = (await h.put())["meeting"]["id"]
    await h.put(user=B)
    mark = h.mark()
    reply = await h.remove(user=B, reason="declined")
    m = reply["meeting"]
    assert (reply["result"], m["id"], m["status"], m["outcome"]) == (
        "entry_removed",
        uuid,
        "scheduled",
        None,
    )
    assert (reply["entry"]["user"], reply["entry"]["state"]) == (B, "removed")
    assert _users(reply) == [A]
    assert h.events(mark) == [(uuid, "meeting.updated")]
    assert h.spawn.calls == [] and h.stop.calls == []


async def test_2_6_11_not_eligible_last_entry():
    h = make_harness()
    uuid = (await h.put())["meeting"]["id"]
    mark = h.mark()
    reply = await h.remove(reason="not_eligible")
    m = reply["meeting"]
    assert (reply["result"], m["status"], m["completion_reason"]) == (
        "removed",
        "failed",
        "stopped",
    )
    assert _outcome(reply) == {
        "kind": "cancelled_by_calendar",
        "detail": "not_eligible",
        "message": "last entry removed: not_eligible",
    }
    assert h.events(mark) == [(uuid, "meeting.removed")]
    assert h.spawn.calls == [] and h.stop.calls == []


async def test_2_6_12_same_meeting_two_users():
    h = make_harness()
    first = await h.put(title="Weekly sync")
    reply = await h.put(user=B, title="Weekly sync")
    m = reply["meeting"]
    assert (reply["result"], m["id"]) == ("joined_existing", first["meeting"]["id"])
    assert reply["entry"] == {
        "external_id": "google:3n5kq8example",
        "user": B,
        "state": "active",
    }
    assert _users(reply) == [A, B]
    assert (m["start"], m["end"]) == ("2026-09-29T09:00:00Z", "2026-09-29T09:30:00Z")
    assert len(h.store.meetings) == 1
    assert h.events() == [(m["id"], "meeting.scheduled"), (m["id"], "meeting.updated")]
    assert h.spawn.calls == [] and h.stop.calls == []


# ── 2.6.13 / 2.6.14 / 2.6.15 ─────────────────────────────────────────────────────────────────


async def test_2_6_13_back_to_back():
    h = make_harness()
    first = await h.put(
        "google:first", start="2026-09-29T15:00:00Z", end="2026-09-29T16:00:00Z"
    )
    second = await h.put(
        "google:second", start="2026-09-29T16:00:00Z", end="2026-09-29T17:00:00Z"
    )
    assert (first["result"], second["result"]) == ("created", "created")
    assert first["meeting"]["id"] != second["meeting"]["id"]
    assert h.events() == [
        (first["meeting"]["id"], "meeting.scheduled"),
        (second["meeting"]["id"], "meeting.scheduled"),
    ]
    assert h.spawn.calls == [] and h.stop.calls == []


async def test_2_6_14_two_accounts():
    h = make_harness()
    one = await h.put(user_id=1)
    two = await h.put(user_id=2)
    assert (one["result"], two["result"]) == ("created", "created")
    assert one["meeting"]["id"] != two["meeting"]["id"]
    assert {row["user_id"] for row in h.store.meetings.values()} == {1, 2}
    assert _users(one) == [A] and _users(two) == [A]
    assert h.events() == [
        (one["meeting"]["id"], "meeting.scheduled"),
        (two["meeting"]["id"], "meeting.scheduled"),
    ]
    assert h.spawn.calls == [] and h.stop.calls == []


async def test_2_6_15_cancel_while_live():
    h = make_harness()
    uuid = (await h.put())["meeting"]["id"]
    h.clock.set("2026-09-29T09:02:00Z")
    h.set_status(uuid, "active")
    mark = h.mark()
    reply = await h.remove(reason="cancelled")
    assert (reply["result"], reply["meeting"]["status"]) == ("bot_stopping", "stopping")
    assert _outcome(reply) == REMOVED_CANCELLED
    assert [c[1] for c in h.stop.calls] == [h.meeting_id(uuid)]
    # the bot leaves; the lifecycle finishes the meeting with upstream's sealed reason
    h.clock.set("2026-09-29T09:03:00Z")
    h.store.write_status(
        h.meeting_id(uuid),
        "completed",
        expected_from={"stopping"},
        data_patch={"completion_reason": "stopped"},
    )
    final = h.store.events[-1].meeting
    assert (final["status"], final["completion_reason"]) == ("completed", "stopped")
    assert final["outcome"]["kind"] == "cancelled_by_calendar"
    assert h.events(mark) == [
        (uuid, "meeting.updated"),
        (uuid, "meeting.status_change"),
        (uuid, "meeting.completed"),
    ]
    assert h.spawn.calls == []


# ── 2.6.16 / R7 ──────────────────────────────────────────────────────────────────────────────


async def _not_sent_at_ten(h) -> str:
    uuid = (await h.put(start="2026-09-29T09:00:00Z", end="2026-09-29T10:00:00Z"))[
        "meeting"
    ]["id"]
    h.clock.set("2026-09-29T10:00:00Z")
    h.set_status(
        uuid,
        "failed",
        outcome=Outcome(
            "not_sent", "ended_before_sent", "the meeting ended before a bot was sent"
        ),
    )
    h.clock.set("2026-09-29T11:00:00Z")
    return uuid


async def test_2_6_16_same_time_after_finish():
    h = make_harness()
    uuid = (await h.put(start="2026-09-29T09:00:00Z", end="2026-09-29T10:00:00Z"))[
        "meeting"
    ]["id"]
    h.clock.set("2026-09-29T09:00:00Z")
    h.set_status(uuid, "active")
    h.clock.set("2026-09-29T09:20:00Z")
    h.set_status(uuid, "completed")  # everyone left early
    h.clock.set("2026-09-29T09:30:00Z")
    mark = h.mark()
    reply = await h.put(
        start="2026-09-29T09:00:00Z", end="2026-09-29T10:00:00Z", title="Renamed"
    )
    m = reply["meeting"]
    assert (reply["result"], m["id"], m["status"]) == (
        "not_changed_finished",
        uuid,
        "completed",
    )
    assert reply["entry"]["state"] == "closed"
    assert m["outcome"] is None
    assert m["title"] is None and _users(reply) == [A]
    assert h.events(mark) == []
    assert h.spawn.calls == [] and h.stop.calls == []


async def test_2_6_16_future_time_after_finish():
    h = make_harness()
    uuid = await _not_sent_at_ten(h)
    mark = h.mark()
    reply = await h.put(start="2026-09-29T15:00:00Z", end="2026-09-29T16:00:00Z")
    m = reply["meeting"]
    assert (reply["result"], reply["previous_meeting_id"]) == ("created", uuid)
    assert m["id"] != uuid
    assert (m["status"], m["start"], m["outcome"]) == (
        "scheduled",
        "2026-09-29T15:00:00Z",
        None,
    )
    assert reply["entry"]["state"] == "active"
    assert h.meeting(uuid).status == "failed"
    assert h.events(mark) == [(m["id"], "meeting.scheduled")]
    assert h.spawn.calls == [] and h.stop.calls == []


async def test_r7_moved_while_live_reruns_at_finish():
    h = make_harness()
    uuid = (await h.put(start="2026-09-29T09:00:00Z", end="2026-09-29T10:00:00Z"))[
        "meeting"
    ]["id"]
    h.clock.set("2026-09-29T09:00:00Z")
    h.set_status(uuid, "active")
    h.clock.set("2026-09-29T09:30:00Z")
    held = await h.put(start="2026-09-29T15:00:00Z", end="2026-09-29T16:00:00Z")
    assert (held["result"], held["meeting"]["start"]) == (
        "not_changed_live",
        "2026-09-29T09:00:00Z",
    )
    h.clock.set("2026-09-29T09:55:00Z")
    mark = h.mark()
    written = h.set_status(uuid, "completed")
    entry_id = h.store.find_entry(1, A, "google:3n5kq8example").id
    assert written.rerun_entry_ids == (entry_id,)

    await h.service.rerun_entries(1, written.rerun_entry_ids)

    moved = h.store.find_entry(1, A, "google:3n5kq8example")
    new = h.store.view(moved.meeting_id)
    assert new.uuid != uuid and moved.state == "active"
    projected = new.project(lead_s=300)
    assert (projected["status"], projected["start"], projected["end"]) == (
        "scheduled",
        "2026-09-29T15:00:00Z",
        "2026-09-29T16:00:00Z",
    )
    assert projected["entries"][0]["user"] == A
    assert h.events(mark) == [
        (uuid, "meeting.completed"),
        (new.uuid, "meeting.scheduled"),
    ]
    assert h.published()[-1] == h.store.events[-1].event_id
    assert h.spawn.calls == [] and h.stop.calls == []


# ── 2.6.17 / 2.6.18 ──────────────────────────────────────────────────────────────────────────


async def test_2_6_17_too_far_unknown_blocked():
    h = make_harness()
    cases = [
        (
            dict(start="2026-10-27T09:00:00Z", end="2026-10-27T10:00:00Z"),
            "too_far_ahead",
        ),
        (dict(meeting_url="https://example.com/call/123"), "unrecognized_link"),
        (
            dict(meeting_url="https://meet.abroadworks.com/Deal4711"),
            "platform_not_enabled",
        ),
    ]
    for fields, code in cases:
        with pytest.raises(IntakeError) as err:
            await h.put(**fields)
        assert (err.value.code, err.value.http_status) == (code, 400)
    assert h.store.meetings == {} and h.store.entries == {} and h.events() == []
    assert h.published() == [] and h.spawn.calls == []


async def test_2_6_18_owner_recurring_moved():
    h = make_harness("2026-09-24T10:00:00Z")
    created = await h.put(
        "google:6ktestrecurring_20260928T113000Z",
        start="2026-09-28T11:30:00Z",
        end="2026-09-28T12:00:00Z",
        time_zone="Asia/Kolkata",
        title="Test recurring meeting",
        series_id="google:6ktestrecurring",
    )
    assert created["result"] == "created"
    moved = await h.put(
        "google:6ktestrecurring_20260928T113000Z",
        start="2026-09-25T11:30:00Z",
        end="2026-09-25T12:00:00Z",
        time_zone="Asia/Kolkata",
        title="Test recurring meeting",
        series_id="google:6ktestrecurring",
    )
    m = moved["meeting"]
    assert (moved["result"], m["id"]) == ("updated", created["meeting"]["id"])
    assert (m["status"], m["room"], m["bot_joins_at"]) == (
        "scheduled",
        "kxo-misr-avz",
        "2026-09-25T11:25:00Z",
    )
    assert h.events() == [(m["id"], "meeting.scheduled"), (m["id"], "meeting.updated")]
    assert h.spawn.calls == [] and h.stop.calls == []


# ── quota ────────────────────────────────────────────────────────────────────────────────────


async def test_finished_meetings_free_the_quota():
    h = make_harness()
    template = parse_entry(
        {
            "external_id": "google:history",
            "user": A,
            "meeting_url": GMEET_OTHER,
            "start": "2026-09-20T09:00:00Z",
            "end": "2026-09-20T09:30:00Z",
        },
        now=ts("2026-09-19T00:00:00Z"),
        max_days_ahead=30,
    )
    plan = Plan(template.start, template.end, None, None, template.meeting_url)
    for n in range(100_000):
        h.store.seed_meeting(
            1,
            GROOM_OTHER,
            status="completed",
            plan=plan,
            entries=[(replace(template, external_id=f"google:history_{n}"), "closed")],
        )
    reply = await h.put()
    assert (reply["result"], reply["meeting"]["status"]) == ("created", "scheduled")
    assert sum(1 for e in h.store.entries.values() if e.state == "active") == 1
    assert h.events() == [(reply["meeting"]["id"], "meeting.scheduled")]
