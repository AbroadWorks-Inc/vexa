"""The Olanote opt-out flag: `meeting_entries.metadata` decides a meeting's `data.auto_join`.

A sender puts `{"olanote": {"auto_join": false}}` on an entry and the auto-join sweep leaves that
meeting alone. The flag is part of the meeting's PLAN, so the create path and the update path
carry it from one place; re-arming a dormant meeting is the update path, and these tests pin that
it works in both directions.

Two rules the rest of the design rests on, each pinned here:

  * **Absent means on.** No metadata, no `olanote` key, a malformed value — every one of them
    leaves the bot on. Only an explicit `false` turns it off, because opting out is the deliberate
    act. A reader that defaulted to `false` would silently stop recording every meeting ever made.
  * **Any `false` wins.** Several entries can point at one meeting. Our own dispatcher puts the
    organizer's single answer on all of them, so they always agree — but the fork must not depend
    on our discipline, and the cautious branch is the one that matches what `recompute` already
    does with `end`.

`metadata` arrives as whatever the JSONB column decodes to, so it is not guaranteed to be a dict:
a sender can store a string, a number or a list there. Reading it must never raise — a malformed
blob from one sender cannot be allowed to break intake for the meeting.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import pytest

from intake_builders import make_harness
from meeting_api.intake.rules import Plan, recompute

UTC = timezone.utc
GMEET = "https://meet.google.com/kxo-misr-avz"

OFF: dict[str, Any] = {"olanote": {"auto_join": False}}
ON: dict[str, Any] = {"olanote": {"auto_join": True}}


def at(hh: int, mm: int = 0) -> datetime:
    return datetime(2026, 9, 29, hh, mm, tzinfo=UTC)


@dataclass(frozen=True)
class E:
    """An entry as `recompute` reads it — the attributes of the `Planned` protocol, no more."""

    start: datetime
    end: Optional[datetime] = None
    title: Optional[str] = None
    time_zone: Optional[str] = None
    meeting_url: str = GMEET
    metadata: Optional[Any] = None


# ── reading the flag off one entry ───────────────────────────────────────────────────────────


def test_an_entry_with_no_metadata_leaves_the_bot_on():
    """SQL NULL in the column. Also the shape every entry our dispatcher has ever sent."""
    assert recompute([E(at(9), at(10), metadata=None)]).auto_join is True


def test_json_null_metadata_leaves_the_bot_on():
    """All 2,016 production rows hold `'null'::jsonb`, which decodes to Python `None` — the same
    value SQL NULL decodes to. The two are indistinguishable here BY DESIGN, and both mean on.

    They are only distinguishable in SQL, which is why nothing may ask `metadata IS NOT NULL` to
    decide whether a flag is present: that predicate is true for every one of those 2,016 rows
    while every value means nothing.
    """
    assert recompute([E(at(9), at(10), metadata=None)]).auto_join is True


def test_metadata_without_an_olanote_key_leaves_the_bot_on():
    assert recompute([E(at(9), at(10), metadata={"ics": {"uid": "x"}})]).auto_join is True


def test_an_olanote_object_without_auto_join_leaves_the_bot_on():
    assert recompute([E(at(9), at(10), metadata={"olanote": {}})]).auto_join is True


@pytest.mark.parametrize(
    "value",
    ["false", "False", "no", 0, 1, 0.0, None, [], {}, [False], {"auto_join": False}],
    ids=["str-false", "str-False", "str-no", "int-0", "int-1", "float", "none",
         "list", "dict", "list-of-false", "nested"],
)
def test_a_non_boolean_auto_join_leaves_the_bot_on_and_never_raises(value: Any):
    """Only a real `False` turns the bot off. A truthy/falsy near-miss is not a decision — the
    string "false" is TRUTHY in Python, so a reader that trusted `bool()` would leave the bot on
    for "false" and off for 0, both of them wrong.
    """
    got = recompute([E(at(9), at(10), metadata={"olanote": {"auto_join": value}})])
    assert got.auto_join is True


@pytest.mark.parametrize(
    "blob", ["hello", 5, 0, [1, 2], True, False, ""],
    ids=["str", "int", "zero", "list", "true", "false", "empty-str"],
)
def test_metadata_that_is_not_an_object_leaves_the_bot_on_and_never_raises(blob: Any):
    """JSONB holds any JSON value, so `metadata` is not guaranteed to be an object."""
    assert recompute([E(at(9), at(10), metadata=blob)]).auto_join is True


def test_an_explicit_false_turns_the_bot_off():
    assert recompute([E(at(9), at(10), metadata=OFF)]).auto_join is False


def test_an_explicit_true_leaves_the_bot_on():
    assert recompute([E(at(9), at(10), metadata=ON)]).auto_join is True


def test_a_foreign_key_beside_ours_is_ignored_and_the_flag_still_read():
    """Our key is namespaced; another sender's keys are none of our business, and ours is still
    found beside them."""
    blob = {"ics": {"uid": "x"}, "olanote": {"auto_join": False}, "zzz": [1, 2, 3]}
    assert recompute([E(at(9), at(10), metadata=blob)]).auto_join is False


# ── several entries on one meeting: any false wins ───────────────────────────────────────────


def test_two_entries_one_saying_off_turns_the_meeting_off():
    got = recompute([E(at(9), at(10), metadata=ON), E(at(9, 30), at(10), metadata=OFF)])
    assert got.auto_join is False


def test_the_false_entry_wins_whatever_order_it_arrives_in():
    """Not a tie-break on start order: `false` wins outright, so neither ordering can differ."""
    early_off = recompute([E(at(9), at(10), metadata=OFF), E(at(9, 30), at(10), metadata=ON)])
    late_off = recompute([E(at(9), at(10), metadata=ON), E(at(9, 30), at(10), metadata=OFF)])
    assert (early_off.auto_join, late_off.auto_join) == (False, False)


def test_two_entries_both_saying_on_leave_the_meeting_on():
    got = recompute([E(at(9), at(10), metadata=ON), E(at(9, 30), at(10), metadata=ON)])
    assert got.auto_join is True


def test_one_silent_entry_does_not_override_another_that_says_off():
    """Silence is not a vote for `true`. An entry with no flag says nothing; the one that does
    say something decides."""
    got = recompute([E(at(9), at(10), metadata=None), E(at(9, 30), at(10), metadata=OFF)])
    assert got.auto_join is False


def test_a_plan_built_by_hand_defaults_to_on():
    """`Plan` is constructed positionally in several places; the new field must not change what
    those mean, and its default must be the same `true` the rest of the design assumes."""
    assert Plan(at(9), at(10), "t", None, GMEET).auto_join is True


# ── the meeting's data.auto_join, through the service ────────────────────────────────────────


async def test_a_meeting_is_created_with_auto_join_on_when_the_entry_is_silent():
    """The behaviour before any of this existed, pinned so the change cannot move it."""
    h = make_harness()
    reply = await h.put()
    assert h.meeting(reply["meeting"]["id"]).data["auto_join"] is True


async def test_a_meeting_is_created_dormant_when_the_entry_says_off():
    h = make_harness()
    reply = await h.put(metadata=OFF)
    assert h.meeting(reply["meeting"]["id"]).data["auto_join"] is False


async def test_a_live_meeting_goes_dormant_when_a_later_entry_says_off():
    """The update path. Our dispatcher re-sends the entry because the flag changed its content
    hash, and this is what that re-send must do."""
    h = make_harness()
    uuid = (await h.put())["meeting"]["id"]
    assert h.meeting(uuid).data["auto_join"] is True

    await h.put(metadata=OFF)
    assert h.meeting(uuid).data["auto_join"] is False


async def test_a_dormant_meeting_is_re_armed_when_the_entry_says_on_again():
    """Re-enabling is the same update path, and it is the one that would be missed if the flag
    were written beside the plan instead of inside it."""
    h = make_harness()
    uuid = (await h.put(metadata=OFF))["meeting"]["id"]
    assert h.meeting(uuid).data["auto_join"] is False

    await h.put(metadata=ON)
    assert h.meeting(uuid).data["auto_join"] is True


async def test_dropping_the_flag_entirely_re_arms_the_meeting():
    """An entry that stops carrying the key is silent again, and silence means on."""
    h = make_harness()
    uuid = (await h.put(metadata=OFF))["meeting"]["id"]
    await h.put()
    assert h.meeting(uuid).data["auto_join"] is True


async def test_a_second_entry_saying_off_turns_the_whole_meeting_off():
    """Two people on one invite, both connected: either one's `false` keeps the bot away."""
    h = make_harness()
    uuid = (await h.put("google:one"))["meeting"]["id"]
    await h.put("google:two", user="b@abroadworks.com", metadata=OFF)
    assert h.meeting(uuid).data["auto_join"] is False


async def test_removing_the_on_entry_leaves_the_meeting_off():
    """Replan after a removal reads the entries that are left, not the ones that were."""
    h = make_harness()
    uuid = (await h.put("google:one"))["meeting"]["id"]
    await h.put("google:two", user="b@abroadworks.com", metadata=OFF)
    await h.remove("google:one")
    assert h.meeting(uuid).data["auto_join"] is False


async def test_removing_the_off_entry_re_arms_the_meeting():
    h = make_harness()
    uuid = (await h.put("google:one"))["meeting"]["id"]
    await h.put("google:two", user="b@abroadworks.com", metadata=OFF)
    assert h.meeting(uuid).data["auto_join"] is False

    await h.remove("google:two", user="b@abroadworks.com")
    assert h.meeting(uuid).data["auto_join"] is True


# ── end to end: the real producer's data into the real sweep ─────────────────────────────────
#
# The two halves of this feature are written by `intake` and read by `bot_spawn`, and they agree
# on one word: `data["auto_join"]`. Nothing in either module would notice if that word changed on
# one side only — the tests above would stay green while no bot was ever held back, which is
# exactly how `action_items` came back empty for ten days.
#
# So these two feed the REAL `_plan_data` into the REAL `auto_join_tick`: no hand-written row, no
# fixture whose shape we chose ourselves.


def _planned_data(metadata: Any, *, at: datetime) -> dict[str, Any]:
    """`meetings.data` exactly as the intake adapter writes it for one entry."""
    from meeting_api.intake.adapters import _plan_data

    return _plan_data(recompute([E(at, None, title="t", metadata=metadata)]))


async def _tick_on(data: dict[str, Any], *, now: datetime):
    from intake_builders import send_clock, sweep_intake
    from meeting_api.bot_spawn.auto_join import auto_join_tick
    from meeting_api.bot_spawn.fakes import FakeRuntimeClient, InMemoryMeetingRepo

    repo, runtime = InMemoryMeetingRepo(), FakeRuntimeClient()
    repo._meetings[1] = {
        "id": 1, "user_id": 7, "platform": "google_meet",
        "native_meeting_id": "abc-defg-hij", "platform_specific_id": "abc-defg-hij",
        "status": "scheduled", "bot_container_id": None, "start_time": None, "end_time": None,
        "data": data, "created_at": "2026-09-29T08:00:00Z", "updated_at": "2026-09-29T08:00:00Z",
    }
    with send_clock(now):
        counters = await auto_join_tick(
            repo, runtime,
            **sweep_intake(transcribe_gate=lambda: None, now=now, token_secret="s",
                           redis_url="redis://r", allow_uncapped=True),
        )
    return counters, runtime


async def test_no_bot_is_sent_for_a_meeting_whose_entry_said_off():
    """The whole feature, in one line of consequence: nobody joins the call."""
    data = _planned_data(OFF, at=at(9))
    counters, runtime = await _tick_on(data, now=at(8, 59))
    assert (counters["due"], counters["spawned"], runtime.specs) == (0, 0, [])


async def test_a_bot_is_sent_for_the_same_meeting_without_the_flag():
    """The positive control. Without it the test above would pass for any reason at all —
    a mistyped key, a row the sweep never looked at, a window it was outside of."""
    data = _planned_data(None, at=at(9))
    counters, runtime = await _tick_on(data, now=at(8, 59))
    assert counters["spawned"] == 1 and len(runtime.specs) == 1


# ── the adapter, and the fake that mirrors it ────────────────────────────────────────────────
#
# The service-level tests above run over `InMemoryIntakeStore`, which writes the plan's keys with
# its OWN copy of the adapter's logic. That copy is why those tests barely move when production's
# `_plan_data` is broken — so the adapter gets its own guards here, and the fake is held against
# it rather than trusted.


@pytest.mark.parametrize("metadata, expected", [(None, True), (ON, True), (OFF, False)],
                         ids=["silent", "on", "off"])
def test_the_adapter_writes_the_flag_into_meetings_data(metadata: Any, expected: bool):
    from meeting_api.intake.adapters import _plan_data

    data = _plan_data(recompute([E(at(9), at(10), title="t", metadata=metadata)]))
    assert data["auto_join"] is expected


@pytest.mark.parametrize("metadata", [None, ON, OFF], ids=["silent", "on", "off"])
def test_the_in_memory_fake_writes_the_same_plan_keys_as_the_adapter(metadata: Any):
    """Anti-drift. Two implementations of one rule is one implementation and one liability: the
    fake is what most of this suite measures, so a fake that agreed with the tests but not with
    the adapter would be a green suite over a broken service.

    The real-Postgres parity group proves the whole store this way; this is the same check for
    the one function, available without a database.
    """
    from meeting_api.intake.adapters import _plan_data
    from meeting_api.intake.fakes import InMemoryIntakeStore
    from meeting_api.intake.ports import Room

    plan = recompute([E(at(9), at(10), title="t", time_zone="Asia/Kolkata", metadata=metadata)])
    room = Room("google_meet", "kxo-misr-avz")

    store = InMemoryIntakeStore(clock=lambda: at(8), lead_s=300)
    mid = store.seed_meeting(1, room, status="scheduled", plan=plan)

    produced = _plan_data(plan)
    assert {k: store.meetings[mid]["data"][k] for k in produced} == produced
