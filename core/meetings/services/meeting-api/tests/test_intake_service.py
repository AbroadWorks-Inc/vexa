"""§1.3 — the entry service's own rules, beyond the §2.6 use cases: `unchanged` writes nothing,
removed and closed entries come back, the quota counts active entries only, the lock order and the
restart when the link changed, events published only after a commit, `completion_reason` inside
the sealed `lifecycle.v1` set, R2's merge, R7's re-run, and the settings."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from intake_builders import A, B, GMEET, GMEET_OTHER, ZOOM, make_harness, ts
from meeting_api.intake import IntakeError
from meeting_api.intake.ports import Room, SpawnOutcome
from meeting_api.intake.rules import Plan
from meeting_api.intake.settings import IntakeSettings

GROOM = Room("google_meet", "kxo-misr-avz")
GROOM_OTHER = Room("google_meet", "abc-defg-hij")
ZROOM = Room("zoom", "12345678901")


def _sealed_completion_reasons() -> set[str]:
    rel = Path("meetings") / "contracts" / "lifecycle.v1" / "lifecycle.schema.json"
    for parent in Path(__file__).resolve().parents:
        if (parent / rel).is_file():
            schema = json.loads((parent / rel).read_text())
            return set(schema["$defs"]["CompletionReason"]["enum"])
    raise FileNotFoundError(rel)


# ── unchanged / coming back ──────────────────────────────────────────────────────────────────


async def test_unchanged_emits_nothing():
    h = make_harness()
    first = await h.put(title="Weekly sync")
    mark, batches, locks = h.mark(), len(h.publisher.batches), len(h.store.lock_log)
    again = await h.put(title="Weekly sync")
    assert (again["result"], again["meeting"]) == ("unchanged", first["meeting"])
    assert again["entry"]["state"] == "active"
    assert h.events(mark) == [] and len(h.publisher.batches) == batches
    assert h.store.lock_log[locks:] == [(1, (GROOM,))]


async def test_a_closed_entry_sent_again_unchanged_is_not_changed_finished():
    """§1.3 step 4: a closed entry that comes back, even with the same ``content_hash``, takes
    R7's future-time check; this one isn't a future time, so it stays closed."""
    h = make_harness()
    uuid = (await h.put(start="2026-09-29T09:00:00Z", end="2026-09-29T10:00:00Z"))[
        "meeting"
    ]["id"]
    h.clock.set("2026-09-29T09:00:00Z")
    h.set_status(uuid, "active")
    h.clock.set("2026-09-29T09:10:00Z")
    h.set_status(uuid, "completed")
    mark = h.mark()
    reply = await h.put(start="2026-09-29T09:00:00Z", end="2026-09-29T10:00:00Z")
    assert (reply["result"], reply["entry"]["state"]) == (
        "not_changed_finished",
        "closed",
    )
    assert reply["meeting"]["id"] == uuid
    assert h.events(mark) == []


async def test_removed_entry_comes_back_with_the_same_content():
    h = make_harness()
    uuid = (await h.put())["meeting"]["id"]
    await h.put(user=B)
    await h.remove(user=B, reason="declined")
    mark = h.mark()
    back = await h.put(user=B)
    assert (back["result"], back["meeting"]["id"], back["previous_meeting_id"]) == (
        "joined_existing",
        uuid,
        None,
    )
    assert back["entry"]["state"] == "active"
    assert [e["user"] for e in back["meeting"]["entries"]] == [A, B]
    assert h.events(mark) == [(uuid, "meeting.updated")]


async def test_closed_entry_comes_back_only_for_a_future_time():
    h = make_harness()
    uuid = (await h.put(start="2026-09-29T09:00:00Z", end="2026-09-29T10:00:00Z"))[
        "meeting"
    ]["id"]
    h.clock.set("2026-09-29T09:00:00Z")
    h.set_status(uuid, "active")
    h.clock.set("2026-09-29T09:10:00Z")
    h.set_status(uuid, "completed")
    same = await h.put(
        start="2026-09-29T09:00:00Z", end="2026-09-29T10:00:00Z", title="x"
    )
    assert (same["result"], same["entry"]["state"]) == (
        "not_changed_finished",
        "closed",
    )
    later = await h.put(start="2026-09-29T11:00:00Z", end="2026-09-29T12:00:00Z")
    assert (later["result"], later["previous_meeting_id"]) == ("created", uuid)
    assert later["entry"]["state"] == "active"
    # the finished meeting keeps the entry as history (a closed row stays where it closed)
    assert [e["user"] for e in h.meeting(uuid).project(lead_s=300)["entries"]] == [A]
    assert [e.state for e in h.meeting(uuid).entries] == ["closed"]


async def test_remove_of_an_unknown_entry_is_entry_not_found():
    h = make_harness()
    with pytest.raises(IntakeError) as err:
        await h.remove("google:never-sent")
    assert (err.value.code, err.value.http_status) == ("entry_not_found", 404)
    assert h.store.lock_log == [(1, ())]


async def test_remove_twice_is_already_removed():
    h = make_harness()
    await h.put()
    first = await h.remove(reason="cancelled")
    mark = h.mark()
    again = await h.remove(reason="cancelled")
    assert (again["result"], again["entry"]["state"]) == ("already_removed", "removed")
    assert again["meeting"] == first["meeting"]
    assert h.events(mark) == []


# ── quota ────────────────────────────────────────────────────────────────────────────────────


async def test_quota_counts_active_entries_only():
    h = make_harness(max_active_entries=2)
    await h.put("google:1", start="2026-09-29T09:00:00Z", end="2026-09-29T09:30:00Z")
    two = await h.put(
        "google:2", start="2026-09-29T11:00:00Z", end="2026-09-29T11:30:00Z"
    )
    mark, meetings = h.mark(), dict(h.store.meetings)
    with pytest.raises(IntakeError) as err:
        await h.put(
            "google:3", start="2026-09-29T13:00:00Z", end="2026-09-29T13:30:00Z"
        )
    assert (err.value.code, err.value.http_status, err.value.retry_after_s) == (
        "quota_exceeded",
        429,
        None,
    )
    assert err.value.message == "active entry quota reached (2 of 2)"
    assert h.events(mark) == [] and h.store.meetings == meetings
    # an update adds no entry, so it is allowed at the quota
    moved = await h.put(
        "google:2", start="2026-09-29T12:00:00Z", end="2026-09-29T12:30:00Z"
    )
    assert moved["result"] == "updated"
    # a removed entry frees its place, and takes one again when it comes back
    await h.remove("google:1", reason="cancelled")
    third = await h.put(
        "google:3", start="2026-09-29T13:00:00Z", end="2026-09-29T13:30:00Z"
    )
    assert third["result"] == "created"
    with pytest.raises(IntakeError):
        await h.put(
            "google:1", start="2026-09-29T09:00:00Z", end="2026-09-29T09:30:00Z"
        )
    # a finished meeting's closed entry doesn't count
    h.clock.set("2026-09-29T12:00:00Z")
    h.set_status(two["meeting"]["id"], "active")
    h.clock.set("2026-09-29T12:40:00Z")
    h.set_status(two["meeting"]["id"], "completed")
    assert (
        await h.put(
            "google:4", start="2026-09-29T14:00:00Z", end="2026-09-29T14:30:00Z"
        )
    )["result"] == "created"


# ── lock order and restarts ──────────────────────────────────────────────────────────────────


async def test_lock_order_is_sorted_when_the_link_changes():
    h = make_harness()
    await h.put(meeting_url=GMEET_OTHER)
    locks = len(h.store.lock_log)
    await h.put(meeting_url=GMEET)
    assert h.store.lock_log[locks:] == [(1, (GROOM,)), (1, (GROOM_OTHER, GROOM))]


async def test_the_fake_refuses_locks_out_of_order():
    h = make_harness()
    with pytest.raises(ValueError):
        async with h.store.room_lock(1, [GROOM, GROOM_OTHER]):
            pass


async def test_restart_when_the_link_changed_under_the_lock():
    h = make_harness()
    await h.put()  # on GROOM
    entry_id = h.store.find_entry(1, A, "google:3n5kq8example").id

    def moved_by_another_writer(store, rooms):
        # after this request chose its lock (GROOM) and before it read the entry, another
        # request moved the entry to GROOM_OTHER
        e = store.entries[entry_id]
        store.entries[entry_id] = replace(
            e, native_meeting_id=GROOM_OTHER.native_meeting_id
        )
        store.on_lock = None

    h.store.on_lock = moved_by_another_writer
    locks = len(h.store.lock_log)
    reply = await h.put(title="Weekly sync")
    assert h.store.lock_log[locks:] == [(1, (GROOM,)), (1, (GROOM_OTHER, GROOM))]
    assert reply["result"] == "updated"
    assert h.store.find_entry(1, A, "google:3n5kq8example").room == GROOM


async def test_a_link_that_keeps_changing_is_unavailable_and_writes_nothing():
    h = make_harness()
    await h.put()
    entry_id = h.store.find_entry(1, A, "google:3n5kq8example").id
    links = iter(["zzz-zzzz-zzb", "zzz-zzzz-zzc"])

    def keep_moving(store, rooms):
        e = store.entries[entry_id]
        store.entries[entry_id] = replace(e, native_meeting_id=next(links))

    h.store.on_lock = keep_moving
    mark, locks = h.mark(), len(h.store.lock_log)
    with pytest.raises(IntakeError) as err:
        await h.put(meeting_url=GMEET_OTHER)
    assert (err.value.code, err.value.http_status) == ("unavailable", 503)
    assert h.store.lock_log[locks:] == [
        (1, (GROOM_OTHER,)),
        # the entry's new link and its meeting's link
        (1, (GROOM_OTHER, GROOM, Room("google_meet", "zzz-zzzz-zzb"))),
    ]
    assert h.events(mark) == []


async def test_remove_reads_without_a_lock_then_locks_the_entry_link():
    h = make_harness()
    await h.put(meeting_url=ZOOM)
    locks = len(h.store.lock_log)
    await h.remove(reason="cancelled")
    assert h.store.lock_log[locks:] == [(1, ()), (1, (ZROOM,))]


# ── publish after commit ─────────────────────────────────────────────────────────────────────


async def test_a_failed_transaction_writes_and_publishes_nothing(monkeypatch):
    h = make_harness()
    await h.put()
    before = (dict(h.store.meetings), dict(h.store.entries), h.mark(), h.published())

    def boom(*args, **kwargs):
        raise RuntimeError("storage failed mid-transaction")

    monkeypatch.setattr(h.store, "write_event", boom)
    with pytest.raises(RuntimeError):
        await h.put(
            "google:second", start="2026-09-30T09:00:00Z", end="2026-09-30T09:30:00Z"
        )
    assert (
        dict(h.store.meetings),
        dict(h.store.entries),
        h.mark(),
        h.published(),
    ) == before


async def test_events_are_published_once_per_commit_in_write_order():
    h = make_harness()
    await h.put("google:a1")
    await h.put("google:b1", user=B, meeting_url=GMEET_OTHER)
    await h.put("google:a1", meeting_url=GMEET_OTHER)
    ids = [e.event_id for e in h.store.events]
    assert h.publisher.batches == [(ids[0],), (ids[1],), (ids[2], ids[3])]


# ── sealed completion_reason ─────────────────────────────────────────────────────────────────


async def test_completion_reason_stays_in_the_sealed_set():
    sealed = _sealed_completion_reasons()
    h = make_harness(
        spawn_failure=SpawnOutcome("failed", "spawn_error", "runtime refused")
    )
    await h.put("google:removed")
    await h.remove("google:removed", reason="deleted")
    await h.put("google:moved")
    await h.put("google:other", user=B, meeting_url=GMEET_OTHER)
    await h.put("google:moved", meeting_url=GMEET_OTHER)
    await h.instant("manual:1")
    written = [
        row["data"]["completion_reason"]
        for row in h.store.meetings.values()
        if "completion_reason" in row["data"]
    ]
    assert written and set(written) <= sealed
    assert set(written) == {"stopped"}
    assert all(
        e.meeting["completion_reason"] in sealed | {None} for e in h.store.events
    )


# ── R2 merge ─────────────────────────────────────────────────────────────────────────────────


async def test_merge_into_live_moves_entries_onto_the_open_ended_meeting():
    h = make_harness("2026-09-29T09:00:00Z")
    live = (
        await h.put(
            start="2026-09-29T10:00:00Z",
            end="2026-09-29T10:30:00Z",
            title="Weekly sync",
        )
    )["meeting"]["id"]
    h.clock.set("2026-09-29T09:20:00Z")
    # a pasted link adopts the 10:00 meeting and sends its bot: live and open-ended
    assert (await h.instant("manual:1", GMEET))["meeting"]["id"] == live
    due = (
        await h.put(
            "google:later",
            start="2026-09-29T11:00:00Z",
            end="2026-09-29T11:30:00Z",
            title="Later",
        )
    )["meeting"]["id"]
    assert due != live
    mark = h.mark()
    assert (
        await h.service.merge_into_live(1, h.meeting_id(due), h.meeting_id(live))
        is True
    )
    gone, kept = h.meeting(due), h.meeting(live)
    assert gone.status == "failed" and "completion_reason" not in gone.data
    assert (
        gone.aw["outcome_kind"],
        gone.aw["outcome_detail"],
        gone.aw["outcome_message"],
    ) == (
        "merged_into_live",
        live,
        f"merged into live meeting {live}",
    )
    assert gone.active_entries() == ()
    assert [e.external_id for e in kept.active_entries()] == [
        "google:3n5kq8example",
        "manual:1",
        "google:later",
    ]
    assert (kept.status, kept.title) == ("requested", "Weekly sync")
    assert h.events(mark) == [(due, "meeting.removed"), (live, "meeting.updated")]
    removed = h.store.events[-2]
    assert removed.event_data == {"merged_into": live}
    assert h.store.events[-1].event_data is None
    assert h.published()[-2:] == [e.event_id for e in h.store.events[-2:]]


async def test_merge_into_live_takes_the_title_when_the_live_meeting_has_none():
    h = make_harness("2026-09-29T09:00:00Z")
    live = (await h.instant("manual:1", GMEET))["meeting"]["id"]
    due = (
        await h.put(
            start="2026-09-29T11:00:00Z",
            end="2026-09-29T11:30:00Z",
            title="Client call",
        )
    )["meeting"]["id"]
    assert await h.service.merge_into_live(1, h.meeting_id(due), h.meeting_id(live))
    assert h.meeting(live).title == "Client call"


async def test_merge_into_live_refuses_a_bounded_or_finished_pair():
    h = make_harness("2026-09-29T09:00:00Z")
    bounded = (
        await h.put(
            "google:a", start="2026-09-29T09:00:00Z", end="2026-09-29T10:00:00Z"
        )
    )["meeting"]["id"]
    h.set_status(bounded, "active")
    due = (
        await h.put(
            "google:b", start="2026-09-29T10:00:00Z", end="2026-09-29T10:30:00Z"
        )
    )["meeting"]["id"]
    mark = h.mark()
    assert (
        await h.service.merge_into_live(1, h.meeting_id(due), h.meeting_id(bounded))
        is False
    )
    assert h.meeting(due).status == "scheduled" and h.events(mark) == []


# ── join_now details ─────────────────────────────────────────────────────────────────────────


async def test_join_now_failure_after_the_scheduler_won_is_joined_existing():
    h = make_harness(
        spawn_failure=SpawnOutcome("failed", "spawn_error", "runtime refused")
    )
    h.spawn.before = lambda mid: h.store.write_status(
        mid, "requested", expected_from={"scheduled"}
    )
    reply = await h.instant("manual:1")
    assert (
        reply["result"],
        reply["meeting"]["status"],
        reply["meeting"]["outcome"],
    ) == (
        "joined_existing",
        "requested",
        None,
    )


# ── settings ─────────────────────────────────────────────────────────────────────────────────


def test_settings_defaults(monkeypatch):
    for key in (
        "ENTRY_MAX_DAYS_AHEAD",
        "JOIN_NOW_ADOPT_AHEAD_S",
        "AUTO_JOIN_LEAD_S",
        "ENTRY_BLOCKED_HOSTS",
        "INTAKE_MAX_ACTIVE_ENTRIES",
        "BOT_SEND_MAX_ATTEMPTS",
        "BOT_SEND_RETRY_BACKOFF_S",
        "INTAKE_CONFLICT_RETRIES",
    ):
        monkeypatch.delenv(key, raising=False)
    from meeting_api.bot_spawn.auto_join import DEFAULT_LEAD_S

    assert IntakeSettings.from_env() == IntakeSettings(
        max_days_ahead=30,
        join_now_adopt_ahead_s=3600,
        lead_s=DEFAULT_LEAD_S,
        blocked_hosts=frozenset({"meet.abroadworks.com"}),
        max_active_entries=100_000,
        send_max_attempts=3,
        send_retry_backoff_s=60,
        conflict_retries=3,
    )


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("ENTRY_MAX_DAYS_AHEAD", "14")
    monkeypatch.setenv("JOIN_NOW_ADOPT_AHEAD_S", "900")
    monkeypatch.setenv("AUTO_JOIN_LEAD_S", "300")
    monkeypatch.setenv(
        "ENTRY_BLOCKED_HOSTS", " Meet.AbroadWorks.com , calls.example.org ,"
    )
    monkeypatch.setenv("INTAKE_MAX_ACTIVE_ENTRIES", "5")
    monkeypatch.setenv("BOT_SEND_MAX_ATTEMPTS", "5")
    monkeypatch.setenv("BOT_SEND_RETRY_BACKOFF_S", "90")
    monkeypatch.setenv("INTAKE_CONFLICT_RETRIES", "1")
    assert IntakeSettings.from_env() == IntakeSettings(
        max_days_ahead=14,
        join_now_adopt_ahead_s=900,
        lead_s=300,
        blocked_hosts=frozenset({"meet.abroadworks.com", "calls.example.org"}),
        max_active_entries=5,
        send_max_attempts=5,
        send_retry_backoff_s=90,
        conflict_retries=1,
    )


def test_an_empty_block_list_blocks_nothing(monkeypatch):
    monkeypatch.setenv("ENTRY_BLOCKED_HOSTS", "")
    assert IntakeSettings.from_env().blocked_hosts == frozenset()


async def test_an_unblocked_jitsi_host_is_accepted():
    h = make_harness(blocked_hosts=frozenset())
    reply = await h.put(meeting_url="https://meet.abroadworks.com/Deal4711")
    assert (
        reply["result"],
        reply["meeting"]["platform"],
        reply["meeting"]["room"],
    ) == (
        "created",
        "jitsi",
        "deal4711@meet.abroadworks.com",
    )


def test_every_intake_setting_is_declared_in_config_v1():
    for parent in Path(__file__).resolve().parents:
        path = parent / "src" / "meeting_api" / "config.v1.json"
        if path.is_file():
            break
    declared = {k["key"]: k for k in json.loads(path.read_text())["keys"]}
    assert declared["ENTRY_MAX_DAYS_AHEAD"]["default"] == "30"
    assert declared["JOIN_NOW_ADOPT_AHEAD_S"]["default"] == "3600"
    assert declared["ENTRY_BLOCKED_HOSTS"]["default"] == "meet.abroadworks.com"
    assert declared["INTAKE_MAX_ACTIVE_ENTRIES"]["default"] == "100000"
    assert "AUTO_JOIN_LEAD_S" in declared


# ── R12: an adopted meeting's failed instant join ────────────────────────────────────────────

ACCOUNT_LIMIT = SpawnOutcome("failed", "account_limit", "bot limit reached (45 of 45)")


async def test_join_now_adopting_a_shared_meeting_keeps_it_when_the_spawn_fails():
    h = make_harness("2026-09-29T09:00:00Z", spawn_failure=ACCOUNT_LIMIT)
    uuid = (await h.put(start="2026-09-29T10:00:00Z", end="2026-09-29T10:30:00Z"))[
        "meeting"
    ]["id"]
    h.clock.set("2026-09-29T09:45:00Z")
    mark = h.mark()
    reply = await h.instant("manual:1", GMEET)
    m = reply["meeting"]
    assert (reply["result"], m["id"], reply["entry"]["state"]) == (
        "joined_existing",
        uuid,
        "removed",
    )
    assert (m["status"], m["outcome"], m["completion_reason"]) == (
        "scheduled",
        None,
        None,
    )
    assert (m["start"], m["end"]) == ("2026-09-29T10:00:00Z", "2026-09-29T10:30:00Z")
    assert [e["external_id"] for e in m["entries"]] == ["google:3n5kq8example"]
    aw = h.meeting(uuid).aw
    assert (aw["last_error_code"], aw["last_error_message"]) == (
        "account_limit",
        "bot limit reached (45 of 45)",
    )
    assert h.store.find_entry(1, A, "manual:1").removed_reason == "not_sent"
    assert h.events(mark) == [(uuid, "meeting.updated"), (uuid, "meeting.updated")]
    assert h.published()[-1] == h.store.events[-1].event_id
    assert h.spawn.calls == [(1, h.meeting_id(uuid))]


async def test_join_now_never_adopts_an_entry_less_upstream_meeting():
    """§1.5 (V12 item 11): an entry-less upstream-planned row on the link is left to upstream;
    the paste gets its own meeting, which ends ``not_sent`` when its last send fails."""
    h = make_harness(
        "2026-09-29T09:45:00Z", spawn_failure=ACCOUNT_LIMIT, send_max_attempts=1
    )
    planned = h.store.seed_meeting(
        1,
        GROOM,
        status="scheduled",
        plan=Plan(ts("2026-09-29T10:00:00Z"), None, "Planned upstream", None, GMEET),
    )
    upstream = h.store.meetings[planned]["uuid"]
    reply = await h.instant("manual:1", GMEET)
    m = reply["meeting"]
    assert (reply["result"], m["status"]) == ("created", "failed")
    assert m["id"] != upstream
    assert m["outcome"] == {
        "kind": "not_sent",
        "detail": "account_limit",
        "message": "bot limit reached (45 of 45)",
        "at": "2026-09-29T09:45:00Z",
    }
    assert reply["entry"]["state"] == "closed"
    assert h.events() == [(m["id"], "meeting.scheduled"), (m["id"], "meeting.not_sent")]
    assert h.meeting(upstream).status == "scheduled"
    assert h.meeting(upstream).entries == ()
    assert h.store.aw[planned]["event_seq"] == 0


# ── the meeting's link is locked too ─────────────────────────────────────────────────────────


async def _live_with_link_changed(h, **fields) -> str:
    uuid = (await h.put())["meeting"]["id"]  # on GROOM
    h.clock.set("2026-09-29T09:01:00Z")
    h.set_status(uuid, "active")
    held = await h.put(meeting_url=GMEET_OTHER, **fields)
    assert held["result"] == "not_changed_live"
    assert h.store.find_entry(1, A, "google:3n5kq8example").room == GROOM_OTHER
    return uuid


async def test_remove_after_a_link_change_while_live_locks_the_meeting_link():
    h = make_harness()
    uuid = await _live_with_link_changed(h)
    locks = len(h.store.lock_log)
    reply = await h.remove(reason="cancelled")
    # discovery read; then the entry's link AND its live meeting's link; then the reply's read
    assert h.store.lock_log[locks:] == [(1, ()), (1, (GROOM_OTHER, GROOM)), (1, ())]
    assert (reply["result"], reply["meeting"]["status"]) == ("bot_stopping", "stopping")
    assert [c[1] for c in h.stop.calls] == [h.meeting_id(uuid)]


async def test_a_link_change_to_a_new_time_while_live_leaves_on_the_new_link():
    """R7 with a new link: the move takes both links' locks and lands on the new link at once."""
    h = make_harness()
    uuid = (await h.put())["meeting"]["id"]  # on GROOM, 09:00–09:30
    h.clock.set("2026-09-29T09:01:00Z")
    h.set_status(uuid, "active")
    locks = len(h.store.lock_log)
    reply = await h.put(
        meeting_url=GMEET_OTHER,
        start="2026-09-29T15:00:00Z",
        end="2026-09-29T15:30:00Z",
    )
    assert h.store.lock_log[locks:] == [
        (1, (GROOM_OTHER,)),
        (1, (GROOM_OTHER, GROOM)),
    ]
    assert (reply["result"], reply["previous_meeting_id"]) == ("created", uuid)
    new = h.meeting(reply["meeting"]["id"])
    assert (new.room, new.status) == (GROOM_OTHER, "scheduled")
    assert h.meeting(uuid).status == "active"
    assert [e.state for e in h.meeting(uuid).entries] == ["closed"]


async def test_a_link_change_while_live_then_the_same_time_is_closed_at_finish():
    h = make_harness()
    uuid = await _live_with_link_changed(h)
    h.clock.set("2026-09-29T09:20:00Z")
    h.set_status(uuid, "completed")
    assert h.store.find_entry(1, A, "google:3n5kq8example").state == "closed"
    again = await h.remove(reason="cancelled")
    assert again["result"] == "already_removed"


# ── shared invites (`_move`) ─────────────────────────────────────────────────────────────────


async def test_title_change_on_a_shared_meeting_stays_on_it():
    h = make_harness()
    uuid = (await h.put(title="Sync"))["meeting"]["id"]
    await h.put(user=B, title="Sync")
    mark = h.mark()
    reply = await h.put(title="Sync (agenda)")
    assert (reply["result"], reply["meeting"]["id"], reply["previous_meeting_id"]) == (
        "updated",
        uuid,
        None,
    )
    assert [e["user"] for e in reply["meeting"]["entries"]] == [A, B]
    assert h.events(mark) == [(uuid, "meeting.updated")]
    assert len(h.store.meetings) == 1


async def test_one_of_two_entries_moving_away_replans_the_old_meeting():
    h = make_harness()
    uuid = (await h.put(start="2026-09-29T09:00:00Z", end="2026-09-29T10:00:00Z"))[
        "meeting"
    ]["id"]
    await h.put(user=B, start="2026-09-29T09:30:00Z", end="2026-09-29T10:30:00Z")
    assert h.meeting(uuid).project(lead_s=300)["end"] == "2026-09-29T10:30:00Z"
    mark = h.mark()
    reply = await h.put(
        user=B, start="2026-09-29T14:00:00Z", end="2026-09-29T15:00:00Z"
    )
    new = reply["meeting"]["id"]
    assert (reply["result"], reply["previous_meeting_id"]) == ("updated", uuid)
    assert new != uuid and [e["user"] for e in reply["meeting"]["entries"]] == [B]
    old = h.meeting(uuid).project(lead_s=300)
    assert (old["status"], old["start"], old["end"]) == (
        "scheduled",
        "2026-09-29T09:00:00Z",
        "2026-09-29T10:00:00Z",
    )
    assert [e["user"] for e in old["entries"]] == [A]
    assert h.events(mark) == [(new, "meeting.scheduled"), (uuid, "meeting.updated")]


def _race_on_removal(h, status: str):
    """The meeting's status changes to ``status`` between intake's read and its conditional
    ``failed`` write — as a lifecycle callback holding no link lock would do."""
    original = h.store.write_status
    fired: list[int] = []

    def racing(meeting_id, to_status, **kwargs):
        if not fired and to_status == "failed":
            fired.append(meeting_id)
            h.store.meetings[meeting_id] = {
                **h.store.meetings[meeting_id],
                "status": status,
            }
        return original(meeting_id, to_status, **kwargs)

    h.store.write_status = racing  # type: ignore[method-assign]


async def test_last_entry_removed_as_the_meeting_goes_live_stops_the_bot():
    h = make_harness()
    uuid = (await h.put())["meeting"]["id"]
    _race_on_removal(h, "active")
    mark = h.mark()
    reply = await h.remove(reason="cancelled")
    assert (reply["result"], reply["meeting"]["status"]) == ("bot_stopping", "stopping")
    assert reply["meeting"]["outcome"]["kind"] == "cancelled_by_calendar"
    assert reply["meeting"]["completion_reason"] is None
    assert [c[1] for c in h.stop.calls] == [h.meeting_id(uuid)]
    assert h.events(mark) == [
        (uuid, "meeting.updated"),
        (uuid, "meeting.status_change"),
    ]


async def test_last_entry_removed_as_the_meeting_finishes_is_entry_removed():
    h = make_harness()
    uuid = (await h.put())["meeting"]["id"]
    _race_on_removal(h, "completed")
    mark = h.mark()
    reply = await h.remove(reason="cancelled")
    assert (reply["result"], reply["entry"]["state"]) == ("entry_removed", "removed")
    assert (reply["meeting"]["status"], reply["meeting"]["outcome"]) == (
        "completed",
        None,
    )
    assert reply["meeting"]["id"] == uuid
    assert h.events(mark) == [] and h.stop.calls == []


async def test_removing_an_entry_that_moved_while_live_leaves_the_live_meeting_alone():
    """After a move while live, remove acts on the entry's new meeting; the live meeting keeps its
    bot and its history."""
    h = make_harness()
    uuid = (await h.put(start="2026-09-29T09:00:00Z", end="2026-09-29T10:00:00Z"))[
        "meeting"
    ]["id"]
    h.clock.set("2026-09-29T09:00:00Z")
    h.set_status(uuid, "active")
    moved = await h.put(start="2026-09-29T15:00:00Z", end="2026-09-29T16:00:00Z")
    mark = h.mark()
    reply = await h.remove(reason="cancelled")
    assert (reply["result"], reply["meeting"]["id"]) == (
        "removed",
        moved["meeting"]["id"],
    )
    assert h.events(mark) == [(moved["meeting"]["id"], "meeting.removed")]
    assert h.meeting(uuid).status == "active" and h.stop.calls == []
    assert [e.state for e in h.meeting(uuid).entries] == ["closed"]


# ── minors ───────────────────────────────────────────────────────────────────────────────────


async def test_merge_into_live_refuses_another_accounts_meeting():
    h = make_harness("2026-09-29T09:00:00Z")
    live = (await h.instant("manual:1", GMEET))["meeting"]["id"]
    other = (
        await h.put(user_id=2, start="2026-09-29T11:00:00Z", end="2026-09-29T11:30:00Z")
    )["meeting"]["id"]
    mark = h.mark()
    assert (
        await h.service.merge_into_live(1, h.meeting_id(other), h.meeting_id(live))
        is False
    )
    assert h.meeting(other).status == "scheduled" and h.events(mark) == []


async def test_a_publish_failure_does_not_skip_the_stop():
    h = make_harness()
    uuid = (await h.put())["meeting"]["id"]
    h.clock.set("2026-09-29T09:01:00Z")
    h.set_status(uuid, "active")

    async def down(event_ids):
        raise ConnectionError("publisher unavailable")

    h.publisher.publish = down  # type: ignore[method-assign]
    reply = await h.remove(reason="cancelled")
    assert (reply["result"], reply["meeting"]["status"]) == ("bot_stopping", "stopping")
    assert [c[1] for c in h.stop.calls] == [h.meeting_id(uuid)]
    # the events stay in the outbox for the publisher to pick up
    assert h.store.events[-2].event_type == "meeting.updated"


async def test_a_blocked_host_with_a_trailing_dot_is_blocked():
    h = make_harness()
    for url in (
        "https://meet.abroadworks.com./Deal4711",
        "https://MEET.abroadworks.com/Deal",
    ):
        with pytest.raises(IntakeError) as err:
            await h.put(meeting_url=url)
        assert err.value.code == "platform_not_enabled"
    assert h.store.meetings == {}


# ── a constraint race is retried, then internal_error (§6.9 F-D) ────────────────────────────


class _Racing:
    """The in-memory store whose first ``lose`` link-locked transactions lose a constraint race at
    their commit (``ConstraintRace``, rolled back); ``tries`` counts those transactions.
    """

    def __init__(self, store, lose: int) -> None:
        self._store = store
        self.lose = lose
        self.tries = 0

    def __getattr__(self, name):
        return getattr(self._store, name)

    def room_lock(self, user_id, rooms):
        from contextlib import asynccontextmanager

        from meeting_api.intake.ports import ConstraintRace

        store = self._store

        @asynccontextmanager
        async def lock():
            async with store.room_lock(user_id, rooms) as tx:
                yield tx
                if rooms:
                    self.tries += 1
                    if self.tries <= self.lose:
                        raise ConstraintRace("uq_meeting_entries_user_source_external")

        return lock()


def _racing_harness(lose: int, *, retries: int = 3):
    from intake_builders import make_settings
    from meeting_api.intake.service import IntakeService

    h = make_harness()
    racing = _Racing(h.store, lose)
    naps: list[float] = []

    async def nap(seconds: float) -> None:
        naps.append(seconds)

    h.service = IntakeService(
        racing,
        h.spawn,
        h.stop,
        h.publisher,
        make_settings(conflict_retries=retries),
        clock=h.clock,
        sleep=nap,
    )
    return h, racing, naps


async def test_a_constraint_race_resolves_on_a_retry():
    h, racing, naps = _racing_harness(lose=2)
    reply = await h.put()
    assert reply["result"] == "created"
    assert racing.tries == 3 and len(naps) == 2
    assert all(0 < nap < 1 for nap in naps)
    assert len(h.store.meetings) == 1  # the lost tries rolled back
    assert h.events() == [(reply["meeting"]["id"], "meeting.scheduled")]


async def test_a_constraint_that_always_fails_is_internal_error_after_the_retries():
    h, racing, naps = _racing_harness(lose=100, retries=2)
    with pytest.raises(IntakeError) as err:
        await h.put()
    assert (err.value.code, err.value.http_status) == ("internal_error", 500)
    assert racing.tries == 3 and len(naps) == 2
    assert h.store.meetings == {} and h.events() == []
