"""§1.5, R2, R6 — the scheduler reads only what is due, waits for a busy link without a retry pause,
merges into an open-ended live meeting, and ends an unsent meeting ``not_sent`` with its cause.

Offline (the pure due filter, the in-memory repo's due read, the link check and the not-sent sweep
over ``InMemoryIntakeStore``), then Postgres: the due read's plan on ``ix_meeting_scheduled_due``,
3 200 future rows never read, and the tick end to end over the real repo, store, exact-row spawn
and entry service. The Postgres cases skip cleanly unless ``MEETING_API_TEST_DATABASE_URL`` is set;
see ``test_intake_pg_schema.py``'s docstring for the ephemeral SQLAlchemy/asyncpg install.

How "3 200 future rows are not read" is proven: the exact statement ``list_due_meetings`` sends is
captured from the engine and re-run under ``EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)`` with the same
parameters. No plan node scans ``meetings`` sequentially, and the index node on
``ix_meeting_scheduled_due`` returns exactly the due rows, with no rows removed by a filter.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import pytest

from intake_builders import GMEET, GMEET_OTHER, entry_body, make_harness, ts
from meeting_api.bot_spawn.auto_join import due_rows
from meeting_api.bot_spawn.fakes import FakeRuntimeClient, InMemoryMeetingRepo
from meeting_api.intake.ports import Room
from meeting_api.intake.rules import Plan
from meeting_api.intake.sweeps import (
    NOT_SENT_MESSAGES,
    RoomCheck,
    check_room,
    not_sent_cause,
    not_sent_tick,
)
from meeting_api.intake.validation import parse_entry
from meeting_api.sweeps.item_failures import InMemoryItemFailures

UTC = timezone.utc
USER = 1
NID = "kxo-misr-avz"
ROOM = Room("google_meet", NID)
NOW = datetime(2026, 9, 29, 9, 0, 0, tzinfo=UTC)
PG_URL = os.getenv("MEETING_API_TEST_DATABASE_URL")


def _row(
    start: datetime, *, managed: bool, end: Optional[datetime] = None, **data: Any
):
    return {
        "id": 1,
        "user_id": USER,
        "platform": "google_meet",
        "native_meeting_id": NID,
        "status": "scheduled",
        "data": {"scheduled_at": start.isoformat(), "auto_join": True, **data},
        "scheduled_end_at": end.isoformat() if end else None,
        "waiting_for_room_sent_at": None,
        "has_entries": managed,
    }


# ── the due filter (pure) ────────────────────────────────────────────────────────────────────


def test_a_late_entry_is_due_at_once():
    """R6: an entry-managed meeting is due from start − lead until its end, so an entry that
    arrives 20 minutes into its meeting gets a bot now; an entry-less row keeps the grace.
    """
    start = NOW - timedelta(minutes=20)
    managed = _row(start, managed=True, end=NOW + timedelta(minutes=40))
    upstream = _row(start, managed=False)
    assert due_rows([managed], now=NOW, lead_s=300, grace_s=600) == [managed]
    assert due_rows([upstream], now=NOW, lead_s=300, grace_s=600) == []


def test_an_entry_managed_meeting_is_due_from_the_lead_until_its_end():
    start, end = NOW + timedelta(minutes=10), NOW + timedelta(minutes=40)
    row = _row(start, managed=True, end=end)
    at = start - timedelta(seconds=300)
    assert due_rows([row], now=at - timedelta(seconds=1), lead_s=300) == []
    assert due_rows([row], now=at, lead_s=300) == [row]
    assert due_rows([row], now=end - timedelta(seconds=1), lead_s=300) == [row]
    assert due_rows([row], now=end, lead_s=300) == []


def test_an_open_ended_entry_managed_meeting_stays_due():
    row = _row(NOW - timedelta(hours=3), managed=True, end=None)
    assert due_rows([row], now=NOW, lead_s=300) == [row]


def test_an_entry_managed_meeting_backs_off_only_after_a_real_failure():
    """No retry pause for waiting (R2): the dispatch stamp alone never holds an entry-managed
    meeting; a spawn failure's ``auto_join_next_retry`` does, as upstream."""
    end = NOW + timedelta(minutes=30)
    attempted = _row(NOW, managed=True, end=end, auto_join_last_attempt=NOW.isoformat())
    failed = _row(
        NOW,
        managed=True,
        end=end,
        auto_join_next_retry=(NOW + timedelta(seconds=300)).isoformat(),
    )
    assert due_rows([attempted], now=NOW + timedelta(seconds=30)) == [attempted]
    assert due_rows([failed], now=NOW + timedelta(seconds=30)) == []
    assert due_rows([failed], now=NOW + timedelta(seconds=300)) == [failed]


def test_entry_less_rows_keep_the_dispatch_backoff():
    row = _row(NOW, managed=False, auto_join_last_attempt=NOW.isoformat())
    assert due_rows([row], now=NOW + timedelta(seconds=30)) == []


# ── the in-memory repo's due read ────────────────────────────────────────────────────────────


def _seed_repo(repo, mid, *, at, status="scheduled", **extra):
    repo._meetings[mid] = {
        "id": mid,
        "user_id": USER,
        "platform": "google_meet",
        "native_meeting_id": f"n{mid}",
        "platform_specific_id": f"n{mid}",
        "status": status,
        "bot_container_id": None,
        "start_time": None,
        "end_time": None,
        "data": {"scheduled_at": at.isoformat()},
        "created_at": "2026-09-01T09:00:00Z",
        "updated_at": "2026-09-01T09:00:00Z",
        **extra,
    }


async def test_the_fake_due_read_returns_only_rows_due_by_now_plus_lead():
    repo = InMemoryMeetingRepo()
    _seed_repo(repo, 1, at=NOW - timedelta(hours=2))
    _seed_repo(
        repo,
        2,
        at=NOW + timedelta(seconds=299),
        has_entries=True,
        scheduled_end_at=NOW + timedelta(hours=1),
    )
    _seed_repo(repo, 3, at=NOW + timedelta(seconds=301))
    _seed_repo(repo, 4, at=NOW, status="active")
    rows = await repo.list_due_meetings(NOW, 300)
    assert [r["id"] for r in rows] == [1, 2]
    assert rows[1]["has_entries"] is True
    assert rows[1]["scheduled_end_at"] == "2026-09-29T10:00:00Z"
    assert rows[0]["has_entries"] is False
    assert rows[0]["scheduled_end_at"] is None
    assert rows[0]["waiting_for_room_sent_at"] is None


async def test_the_sweeps_uncapped_opt_in_reaches_the_exact_row_spawn():
    """``AUTO_JOIN_ALLOW_UNCAPPED`` with no identity edge: the exact-row spawn sends the bot
    without a limit; without the opt-in it refuses (``internal_error``)."""
    from meeting_api.intake.ports import SpawnOutcome
    from meeting_api.intake.spawn import ExactRowSpawn

    repo, runtime = InMemoryMeetingRepo(), FakeRuntimeClient()
    _seed_repo(repo, 1, at=NOW)
    _seed_repo(repo, 2, at=NOW)
    h = make_harness()

    def port(allow: bool) -> ExactRowSpawn:
        return ExactRowSpawn(
            repo,
            runtime,
            store=h.store,
            fetch_bot_context=None,
            token_secret="s",
            redis_url="redis://r",
            allow_uncapped=allow,
        )

    assert await port(True).spawn_exact(USER, 1) == SpawnOutcome("sent")
    refused = await port(False).spawn_exact(USER, 2)
    assert (refused.result, refused.code) == ("failed", "internal_error")
    assert len(runtime.specs) == 1


# ── the tick reads in pages and bounds each row (§6.9 F-I) ─────────────────────────────────


async def _paged_tick(repo, runtime, **kw):
    from intake_builders import send_clock, sweep_intake

    from meeting_api.bot_spawn.auto_join import auto_join_tick

    with send_clock(NOW):
        return await auto_join_tick(
            repo,
            runtime,
            **sweep_intake(
                transcribe_gate=lambda: None,
                now=NOW,
                token_secret="s",
                redis_url="redis://r",
                allow_uncapped=True,
                **kw,
            ),
        )


def _seed_due(repo, n: int) -> list[int]:
    for mid in range(1, n + 1):
        _seed_repo(repo, mid, at=NOW - timedelta(minutes=n - mid))
        repo._meetings[mid]["data"]["auto_join"] = True
    return list(range(1, n + 1))


async def test_the_tick_reads_the_due_rows_in_pages_and_sends_every_page():
    repo, runtime = InMemoryMeetingRepo(), FakeRuntimeClient()
    mids = _seed_due(repo, 5)
    pages: list[tuple] = []
    read = repo.list_due_meetings

    async def spy(now, lead_s, **page):
        rows = await read(now, lead_s, **page)
        pages.append((page["limit"], page["after"] is None, [r["id"] for r in rows]))
        return rows

    repo.list_due_meetings = spy
    counters = await _paged_tick(repo, runtime, batch_size=2)
    assert counters["spawned"] == 5 and counters["due"] == 5
    assert pages == [(2, True, mids[:2]), (2, False, mids[2:4]), (2, False, mids[4:])]
    assert len(runtime.specs) == 5


async def test_a_poison_row_never_blocks_the_rest_and_is_given_up():
    """One row whose tick raises fails alone; after ``SWEEP_MAX_ITEM_FAILURES`` the tick gives
    it up and never tries it again."""
    repo, runtime = InMemoryMeetingRepo(), FakeRuntimeClient()
    _seed_due(repo, 3)
    stamp = repo.merge_meeting_data
    tried: list[int] = []

    async def merge(meeting_id, patch):
        if meeting_id == 2:
            tried.append(meeting_id)
            raise RuntimeError("poison row")
        await stamp(meeting_id, patch)

    repo.merge_meeting_data = merge
    failures = InMemoryItemFailures(max_failures=2)
    counters = await _paged_tick(repo, runtime, item_failures=failures)
    assert counters["spawned"] == 2
    assert {repo._meetings[m]["status"] for m in (1, 3)} == {"requested"}
    await _paged_tick(repo, runtime, item_failures=failures)
    assert await failures.given_up("auto-join", ["2"]) == {"2"}
    await _paged_tick(repo, runtime, item_failures=failures)
    assert tried == [2, 2] and repo._meetings[2]["status"] == "scheduled"


# ── the link check (under the link lock) ─────────────────────────────────────────────────────


async def _put(h, external_id, start: str, end: str, *, url=GMEET) -> int:
    reply = await h.put(external_id, meeting_url=url, start=start, end=end)
    return h.meeting_id(reply["meeting"]["id"])


async def test_a_free_link_is_free():
    h = make_harness()
    mid = await _put(h, "e1", "2026-09-29T09:00:00Z", "2026-09-29T09:30:00Z")
    assert await check_room(h.store, USER, mid, ROOM) == RoomCheck("free")


async def test_a_busy_link_sends_waiting_for_room_exactly_once():
    h = make_harness()
    live = await _put(h, "e1", "2026-09-29T08:30:00Z", "2026-09-29T09:00:00Z")
    h.store.write_status(live, "active", expected_from={"scheduled"})
    due = await _put(h, "e2", "2026-09-29T09:00:00Z", "2026-09-29T09:30:00Z")
    mark, published = h.mark(), len(h.published())
    assert await check_room(
        h.store, USER, due, ROOM, publisher=h.publisher
    ) == RoomCheck("waiting")
    assert await check_room(
        h.store, USER, due, ROOM, publisher=h.publisher
    ) == RoomCheck("waiting")
    view = h.store.view(due)
    assert h.events(mark) == [(view.uuid, "meeting.waiting_for_room")]
    assert h.published()[published:] == [h.store.events[-1].event_id]
    assert view.aw is not None and view.aw["waiting_for_room_sent_at"] is not None
    assert view.status == "scheduled"
    assert "auto_join_next_retry" not in view.data


async def test_an_open_ended_live_join_now_meeting_is_a_merge_target():
    h = make_harness()
    reply = await h.instant("paste:1", GMEET)
    live = h.meeting_id(reply["meeting"]["id"])
    assert h.store.meetings[live]["status"] == "requested"
    due = await _put(h, "e2", "2026-09-26T13:00:00Z", "2026-09-26T13:30:00Z")
    assert due != live
    assert await check_room(h.store, USER, due, ROOM) == RoomCheck("merge", live)


async def test_an_entry_less_live_meeting_is_not_a_merge_target():
    h = make_harness()
    live = h.store.seed_meeting(USER, ROOM, status="active")
    due = await _put(h, "e2", "2026-09-29T09:00:00Z", "2026-09-29T09:30:00Z")
    assert await check_room(h.store, USER, due, ROOM) == RoomCheck("waiting")
    assert live != due


async def test_a_stopping_join_now_meeting_is_busy_not_a_merge_target():
    """R2: a bot that is leaving never absorbs the due meeting; the link counts as busy until it
    finishes, then the due meeting goes."""
    h = make_harness()
    live = h.meeting_id((await h.instant("paste:1", GMEET))["meeting"]["id"])
    due = await _put(h, "e2", "2026-09-26T13:00:00Z", "2026-09-26T13:30:00Z")
    h.store.write_status(live, "stopping", expected_from={"requested"})
    assert await check_room(h.store, USER, due, ROOM) == RoomCheck("waiting")
    assert await h.service.merge_into_live(USER, due, live) is False
    assert h.store.meetings[due]["status"] == "scheduled"
    h.store.write_status(live, "completed", expected_from={"stopping"})
    assert await check_room(h.store, USER, due, ROOM) == RoomCheck("free")


async def test_a_live_meeting_whose_join_now_entry_was_removed_is_not_a_merge_target():
    """R2: the target must still hold an ACTIVE ``join_now`` entry."""
    h = make_harness("2026-09-29T09:00:00Z")
    live_uuid = (
        await h.put(
            "google:a", start="2026-09-29T10:00:00Z", end="2026-09-29T10:30:00Z"
        )
    )["meeting"]["id"]
    h.clock.set("2026-09-29T09:20:00Z")
    assert (await h.instant("paste:1", GMEET))["meeting"]["id"] == live_uuid
    live = h.meeting_id(live_uuid)
    assert (await h.remove("paste:1"))["result"] == "entry_removed"
    view = h.store.view(live)
    assert view.status == "requested" and view.end is None
    due = await _put(h, "google:b", "2026-09-29T11:00:00Z", "2026-09-29T11:30:00Z")
    assert await check_room(h.store, USER, due, ROOM) == RoomCheck("waiting")
    assert await h.service.merge_into_live(USER, due, live) is False
    assert h.store.meetings[due]["status"] == "scheduled"


async def test_a_row_no_longer_scheduled_or_moved_is_gone():
    h = make_harness()
    mid = await _put(h, "e1", "2026-09-29T09:00:00Z", "2026-09-29T09:30:00Z")
    assert await check_room(
        h.store, USER, mid, Room("google_meet", "abc-defg-hij")
    ) == RoomCheck("gone")
    h.store.write_status(mid, "requested", expected_from={"scheduled"})
    assert await check_room(h.store, USER, mid, ROOM) == RoomCheck("gone")


# ── the not-sent sweep ───────────────────────────────────────────────────────────────────────


def test_each_not_sent_cause_and_its_exact_message():
    assert not_sent_cause(None) == (
        "ended_before_sent",
        "the meeting ended before a bot was sent",
    )
    assert not_sent_cause({"waiting_for_room_sent_at": NOW}) == (
        "room_busy",
        "another bot was still on this meeting link when the meeting ended",
    )
    assert not_sent_cause(
        {
            "last_error_code": "account_limit",
            "last_error_message": "bot limit reached (45 of 45)",
            "waiting_for_room_sent_at": NOW,
        }
    ) == ("account_limit", "bot limit reached (45 of 45)")
    assert not_sent_cause({"last_error_code": "spawn_error"}) == (
        "spawn_error",
        "the bot was not sent (spawn_error)",
    )
    assert NOT_SENT_MESSAGES == {
        "ended_before_sent": "the meeting ended before a bot was sent",
        "room_busy": "another bot was still on this meeting link when the meeting ended",
    }


async def _sweep(h, at: str, **kw) -> int:
    h.clock.set(at)
    kw.setdefault("failures", InMemoryItemFailures(max_failures=5))
    return await not_sent_tick(h.store, publisher=h.publisher, now=h.clock(), **kw)


def _outcome(h, mid):
    aw = h.store.aw[mid]
    return (
        h.store.meetings[mid]["status"],
        aw["outcome_kind"],
        aw["outcome_detail"],
        aw["outcome_message"],
    )


async def test_not_sent_ended_before_sent():
    h = make_harness()
    mid = await _put(h, "e1", "2026-09-29T09:00:00Z", "2026-09-29T09:30:00Z")
    assert await _sweep(h, "2026-09-29T09:29:59Z") == 0
    mark = h.mark()
    assert await _sweep(h, "2026-09-29T09:30:00Z") == 1
    assert _outcome(h, mid) == (
        "failed",
        "not_sent",
        "ended_before_sent",
        "the meeting ended before a bot was sent",
    )
    uuid = h.store.meetings[mid]["uuid"]
    assert h.events(mark) == [(uuid, "meeting.not_sent")]
    assert h.store.events[-1].change["reason"] == "ended_before_sent"
    assert h.store.meetings[mid]["data"].get("completion_reason") is None
    assert h.published()[-1] == h.store.events[-1].event_id
    assert [e.state for e in h.store.view(mid).entries] == ["closed"]


async def test_not_sent_room_busy():
    h = make_harness()
    live = await _put(h, "e1", "2026-09-29T08:30:00Z", "2026-09-29T09:00:00Z")
    h.store.write_status(live, "active", expected_from={"scheduled"})
    due = await _put(h, "e2", "2026-09-29T09:00:00Z", "2026-09-29T09:30:00Z")
    assert await check_room(h.store, USER, due, ROOM) == RoomCheck("waiting")
    await _sweep(h, "2026-09-29T09:30:00Z")
    assert _outcome(h, due) == (
        "failed",
        "not_sent",
        "room_busy",
        "another bot was still on this meeting link when the meeting ended",
    )
    assert h.store.meetings[live]["status"] == "active"


async def test_not_sent_carries_the_last_spawn_error():
    h = make_harness()
    mid = await _put(h, "e1", "2026-09-29T09:00:00Z", "2026-09-29T09:30:00Z")
    async with h.store.room_lock(USER, [ROOM]) as tx:
        await tx.record_spawn_error(
            mid, "account_limit", "bot limit reached (45 of 45)"
        )
    await _sweep(h, "2026-09-29T09:31:00Z")
    assert _outcome(h, mid) == (
        "failed",
        "not_sent",
        "account_limit",
        "bot limit reached (45 of 45)",
    )


def _open_ended(h) -> int:
    """A pasted meeting (no end) that has had no bot since 09:00."""
    start = ts("2026-09-29T09:00:00Z")
    paste = parse_entry(
        {
            "external_id": "paste:1",
            "user": "a@abroadworks.com",
            "meeting_url": GMEET,
            "join_now": True,
        },
        now=start,
        max_days_ahead=30,
    )
    return h.store.seed_meeting(
        USER,
        ROOM,
        status="scheduled",
        plan=Plan(start, None, None, None, GMEET),
        entries=[(paste, "active")],
    )


async def test_an_open_ended_meeting_is_never_ended_by_time():
    """F-K: an open-ended meeting has no end to pass; its send attempts bound it, never
    ``JOIN_NOW_ADOPT_AHEAD_S``."""
    h = make_harness()
    mid = _open_ended(h)
    assert await _sweep(h, "2026-09-29T10:00:00Z") == 0
    assert await _sweep(h, "2026-10-01T10:00:00Z") == 0
    assert h.store.meetings[mid]["status"] == "scheduled"


# ── bounded send attempts (F-K) ──────────────────────────────────────────────────────────────

LIMIT = ("account_limit", "bot limit reached (45 of 45)")


async def test_each_failed_send_is_counted_and_backs_off_by_the_setting():
    h = make_harness()
    mid = _open_ended(h)
    h.clock.set("2026-09-29T09:00:00Z")
    assert await h.service.send_failed(USER, mid, *LIMIT, now=h.clock()) is False
    view = h.store.view(mid)
    assert view.status == "scheduled"
    assert (view.aw["send_attempts"], view.aw["last_error_code"]) == (
        1,
        "account_limit",
    )
    assert view.aw["last_error_message"] == "bot limit reached (45 of 45)"
    assert view.data["auto_join_next_retry"] == "2026-09-29T09:01:00+00:00"
    assert view.data["auto_join_error"] == "bot limit reached (45 of 45)"


async def test_the_last_failed_send_ends_the_meeting_not_sent_with_its_code():
    h = make_harness()
    mid = _open_ended(h)
    for minute in (0, 1):
        h.clock.set(f"2026-09-29T09:0{minute}:00Z")
        assert await h.service.send_failed(USER, mid, *LIMIT, now=h.clock()) is False
    mark = h.mark()
    h.clock.set("2026-09-29T09:02:00Z")
    assert (
        await h.service.send_failed(
            USER, mid, "spawn_error", "kernel said no", now=h.clock()
        )
        is True
    )
    assert _outcome(h, mid) == ("failed", "not_sent", "spawn_error", "kernel said no")
    uuid = h.store.meetings[mid]["uuid"]
    assert h.events(mark) == [(uuid, "meeting.not_sent")]
    assert h.store.events[-1].change["reason"] == "spawn_error"
    assert h.published()[-1] == h.store.events[-1].event_id
    assert h.store.aw[mid]["send_attempts"] == 3


async def test_the_attempt_limit_is_the_setting():
    h = make_harness(send_max_attempts=1)
    mid = _open_ended(h)
    h.clock.set("2026-09-29T09:00:00Z")
    assert await h.service.send_failed(USER, mid, *LIMIT, now=h.clock()) is True
    assert _outcome(h, mid) == ("failed", "not_sent", *LIMIT)


async def test_a_failed_send_on_a_meeting_no_longer_scheduled_changes_nothing():
    h = make_harness()
    mid = _open_ended(h)
    h.store.write_status(mid, "requested", expected_from={"scheduled"})
    mark = h.mark()
    assert await h.service.send_failed(USER, mid, *LIMIT, now=h.clock()) is False
    assert h.store.aw[mid]["send_attempts"] == 0 and h.events(mark) == []


async def test_the_not_sent_sweep_reads_in_pages_and_ends_every_page():
    """§6.9 F-I: the overdue read is paged (``SWEEP_BATCH_SIZE``) in id order, and every page is
    worked in the same tick."""
    h = make_harness()
    mids = [
        await _put(
            h,
            f"e{i}",
            "2026-09-29T09:00:00Z",
            "2026-09-29T09:30:00Z",
            url=f"https://meet.google.com/aaa-bbbb-cc{letter}",
        )
        for i, letter in enumerate("abcde")
    ]
    reads: list[tuple] = []
    read = h.store.overdue_meetings

    async def spy(now, **kw):
        page = await read(now, **kw)
        reads.append((kw, [v.id for v in page]))
        return page

    h.store.overdue_meetings = spy
    assert await _sweep(h, "2026-09-29T09:30:00Z", batch_size=2) == 5
    assert reads == [
        ({"after": None, "limit": 2}, mids[:2]),
        ({"after": mids[1], "limit": 2}, mids[2:4]),
        ({"after": mids[3], "limit": 2}, mids[4:]),
    ]


async def test_a_poison_meeting_never_blocks_the_rest_and_is_given_up():
    """§6.9 F-I: a meeting whose end can't be written fails alone; after
    ``SWEEP_MAX_ITEM_FAILURES`` it is given up and never tried again."""
    h = make_harness()
    poison = await _put(h, "e1", "2026-09-29T09:00:00Z", "2026-09-29T09:30:00Z")
    good = await _put(
        h, "e2", "2026-09-29T09:00:00Z", "2026-09-29T09:30:00Z", url=GMEET_OTHER
    )
    tried: list[int] = []

    def refuse(store, rooms):
        if rooms == (ROOM,):
            tried.append(poison)
            raise RuntimeError("poison row")

    h.store.on_lock = refuse
    failures = InMemoryItemFailures(max_failures=2)
    assert await _sweep(h, "2026-09-29T09:30:00Z", failures=failures) == 1
    assert h.store.meetings[good]["status"] == "failed"
    assert await _sweep(h, "2026-09-29T09:31:00Z", failures=failures) == 0
    assert await failures.given_up("not-sent", [str(poison)]) == {str(poison)}
    assert await _sweep(h, "2026-09-29T09:32:00Z", failures=failures) == 0
    assert len(tried) == 2 and h.store.meetings[poison]["status"] == "scheduled"


async def test_the_not_sent_sweep_leaves_entry_less_and_live_meetings_alone():
    h = make_harness()
    upstream = h.store.seed_meeting(
        USER,
        ROOM,
        status="scheduled",
        plan=Plan(
            ts("2026-09-29T09:00:00Z"), ts("2026-09-29T09:30:00Z"), None, None, GMEET
        ),
    )
    live = await _put(
        h, "e1", "2026-09-29T09:00:00Z", "2026-09-29T09:30:00Z", url=GMEET_OTHER
    )
    h.store.write_status(live, "active", expected_from={"scheduled"})
    assert await _sweep(h, "2026-09-29T12:00:00Z") == 0
    assert h.store.meetings[upstream]["status"] == "scheduled"
    assert h.store.meetings[live]["status"] == "active"


# ══ Postgres ════════════════════════════════════════════════════════════════════════════════

pg_only = pytest.mark.skipif(
    not PG_URL,
    reason="real-Postgres proofs for §1.5; set MEETING_API_TEST_DATABASE_URL to run",
)


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


async def _ctx(_user_id: int) -> dict:
    return {"max_concurrent": 45}


class Pg:
    """The real repo, intake store, exact-row spawn and entry service over one database."""

    def __init__(self, engine: Any, *, send_max_attempts: int = 3) -> None:
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from intake_builders import make_settings
        from meeting_api.bot_spawn.adapters import SqlAlchemyMeetingRepo
        from meeting_api.intake import ExactRowSpawn, PostgresIntakeStore
        from meeting_api.intake.fakes import FakePublisher
        from meeting_api.intake.service import IntakeService
        from meeting_api.intake.fakes import NoStop

        self.engine = engine
        self.session_factory = async_sessionmaker(engine, expire_on_commit=False)
        self.repo = SqlAlchemyMeetingRepo(self.session_factory)
        self.store = PostgresIntakeStore(self.session_factory)
        self.publisher = FakePublisher()
        self.runtime = FakeRuntimeClient()
        spawn = ExactRowSpawn(
            self.repo,
            self.runtime,
            store=self.store,
            fetch_bot_context=_ctx,
            publisher=self.publisher,
            token_secret="s",
            redis_url="redis://r",
        )
        self.service = IntakeService(
            self.store,
            spawn,
            NoStop(),
            self.publisher,
            make_settings(lead_s=300, send_max_attempts=send_max_attempts),
        )

    async def tick(self, now: datetime, **kw: Any) -> dict:
        from meeting_api.bot_spawn.auto_join import auto_join_tick

        from meeting_api.sweeps.item_failures import InMemoryItemFailures

        kw.setdefault("fetch_bot_context", _ctx)
        kw.setdefault("item_failures", InMemoryItemFailures(max_failures=5))
        return await auto_join_tick(
            self.repo,
            kw.pop("runtime", self.runtime),
            store=self.store,
            intake=self.service,
            publisher=self.publisher,
            transcribe_gate=lambda: None,
            now=now,
            lead_s=300,
            grace_s=600,
            retry_backoff_s=300,
            token_secret="s",
            redis_url="redis://r",
            **kw,
        )

    async def not_sent(self, now: datetime) -> int:
        from meeting_api.sweeps.item_failures import PostgresItemFailures

        return await not_sent_tick(
            self.store,
            publisher=self.publisher,
            now=now,
            failures=PostgresItemFailures(self.session_factory, max_failures=5),
        )

    async def put(
        self, external_id: str, start: datetime, end: datetime, *, url: str = GMEET
    ) -> int:
        reply = await self.service.put_entry(
            USER,
            entry_body(external_id, meeting_url=url, start=_iso(start), end=_iso(end)),
        )
        return await self.id_of(reply["meeting"]["id"])

    async def id_of(self, uuid: str) -> int:
        return int(await self.scalar("SELECT id FROM meetings WHERE uuid = :u", u=uuid))

    async def scalar(self, sql: str, **params: Any) -> Any:
        from sqlalchemy import text

        async with self.engine.connect() as conn:
            return (await conn.execute(text(sql), params)).scalar()

    async def row(self, mid: int) -> dict:
        from sqlalchemy import text

        async with self.engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT m.status, m.data, a.outcome_kind, a.outcome_detail, "
                        "a.outcome_message, a.last_error_code, a.last_error_message, "
                        "a.waiting_for_room_sent_at, a.send_attempts FROM meetings m "
                        "LEFT JOIN meeting_aw_state a ON a.meeting_id = m.id WHERE m.id = :m"
                    ),
                    {"m": mid},
                )
            ).one()
        return dict(row._mapping)

    async def events(self, mid: int) -> list[str]:
        from sqlalchemy import text

        async with self.engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT event_type FROM webhook_outbox WHERE meeting_id = :m "
                        "ORDER BY sequence"
                    ),
                    {"m": mid},
                )
            ).all()
        return [r[0] for r in rows]

    async def seed_upstream(
        self, start: datetime, *, status: str = "scheduled", native: str = NID
    ) -> int:
        from sqlalchemy import text

        async with self.engine.begin() as conn:
            return int(
                (
                    await conn.execute(
                        text(
                            "INSERT INTO meetings (user_id, platform, platform_specific_id, "
                            "status, data) VALUES (:u, 'google_meet', :n, :s, "
                            "CAST(:d AS jsonb)) RETURNING id"
                        ),
                        {
                            "u": USER,
                            "n": native,
                            "s": status,
                            "d": json.dumps(
                                {
                                    "scheduled_at": _iso(start),
                                    "auto_join": True,
                                    "constructed_meeting_url": f"https://meet.google.com/{native}",
                                }
                            ),
                        },
                    )
                ).scalar_one()
            )

    async def set_status(self, mid: int, status: str) -> None:
        async with self.store.room_lock(USER, ()) as tx:
            current = (await tx.meeting(mid)).status
            await tx.status(mid, status, expected_from={current})


@pytest.fixture
async def pg():
    if not PG_URL:
        pytest.skip("set MEETING_API_TEST_DATABASE_URL to run")
    pytest.importorskip("sqlalchemy", reason="see test_intake_pg_schema.py's docstring")
    pytest.importorskip("asyncpg", reason="see test_intake_pg_schema.py's docstring")
    from sqlalchemy.ext.asyncio import create_async_engine

    from admin_api.schema import models as admin_models
    from admin_api.schema import sync as admin_sync

    engine = create_async_engine(PG_URL)
    async with engine.begin() as conn:
        await conn.run_sync(admin_models.Base.metadata.drop_all)
    await admin_sync.ensure_schema(engine, admin_models.Base)
    yield Pg(engine)
    async with engine.begin() as conn:
        await conn.run_sync(admin_models.Base.metadata.drop_all)
    await engine.dispose()


# ── the due read ─────────────────────────────────────────────────────────────────────────────


async def _explain_due_read(pg: Pg, now: datetime) -> tuple[list[dict], dict]:
    """Run ``list_due_meetings`` capturing its statement, then EXPLAIN ANALYZE that statement."""
    from sqlalchemy import event

    captured: list[tuple[str, Any]] = []

    def grab(conn, cursor, statement, parameters, context, executemany):
        if "meeting_event_time" in statement:
            captured.append((statement, parameters))

    event.listen(pg.engine.sync_engine, "before_cursor_execute", grab)
    try:
        rows = await pg.repo.list_due_meetings(now, 300)
    finally:
        event.remove(pg.engine.sync_engine, "before_cursor_execute", grab)
    assert len(captured) == 1, captured
    statement, parameters = captured[0]
    async with pg.engine.connect() as conn:
        plan = (
            await conn.exec_driver_sql(
                "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + statement, parameters
            )
        ).scalar()
    if isinstance(plan, str):
        plan = json.loads(plan)
    return rows, plan[0]["Plan"]


def _nodes(plan: dict) -> list[dict]:
    out = [plan]
    for child in plan.get("Plans", ()):
        out.extend(_nodes(child))
    return out


async def _seed_future_and_due(pg: Pg, now: datetime, *, future: int, due: int) -> None:
    from sqlalchemy import text

    async with pg.engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO meetings (user_id, platform, platform_specific_id, status, data) "
                "SELECT 1, 'google_meet', 'f' || g, 'scheduled', jsonb_build_object("
                "'scheduled_at', to_char((CAST(:now AS timestamptz) + g * interval '1 hour') "
                "AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"'), 'auto_join', true) "
                "FROM generate_series(1, :n) AS g"
            ),
            {"now": now, "n": future},
        )
        await conn.execute(
            text(
                "INSERT INTO meetings (user_id, platform, platform_specific_id, status, data) "
                "SELECT 1, 'google_meet', 'd' || g, 'scheduled', jsonb_build_object("
                "'scheduled_at', to_char((CAST(:now AS timestamptz) - g * interval '1 minute') "
                "AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"'), 'auto_join', true) "
                "FROM generate_series(1, :n) AS g"
            ),
            {"now": now, "n": due},
        )
        await conn.execute(
            text(
                "INSERT INTO meetings (user_id, platform, platform_specific_id, status, data) "
                "SELECT 1, 'google_meet', 'h' || g, 'completed', '{}'::jsonb "
                "FROM generate_series(1, :n) AS g"
            ),
            {"n": future},
        )
        await conn.execute(text("ANALYZE meetings"))


@pg_only
async def test_explain_the_due_read_uses_the_partial_index(pg):
    now = _now()
    await _seed_future_and_due(pg, now, future=3200, due=4)
    _, plan = await _explain_due_read(pg, now)
    nodes = _nodes(plan)
    assert any(n.get("Index Name") == "ix_meeting_scheduled_due" for n in nodes), plan
    assert not any(
        n["Node Type"] == "Seq Scan" and n.get("Relation Name") == "meetings"
        for n in nodes
    ), plan


@pg_only
async def test_3200_future_rows_are_not_read(pg):
    now = _now()
    await _seed_future_and_due(pg, now, future=3200, due=4)
    rows, plan = await _explain_due_read(pg, now)
    assert len(rows) == 4
    (index_node,) = [
        n for n in _nodes(plan) if n.get("Index Name") == "ix_meeting_scheduled_due"
    ]
    assert index_node["Actual Rows"] == 4, index_node
    assert index_node["Actual Loops"] == 1
    assert index_node.get("Rows Removed by Filter", 0) == 0
    assert index_node.get("Rows Removed by Index Recheck", 0) == 0


@pg_only
async def test_the_due_read_carries_the_aw_state_and_whether_entries_manage_it(pg):
    now = _now()
    managed = await pg.put(
        "e1", now + timedelta(minutes=2), now + timedelta(minutes=30)
    )
    upstream = await pg.seed_upstream(now - timedelta(minutes=1), native="abc-defg-hij")
    rows = {r["id"]: r for r in await pg.repo.list_due_meetings(now, 300)}
    assert set(rows) == {managed, upstream}
    assert rows[managed]["has_entries"] is True
    assert rows[managed]["scheduled_end_at"] == _iso(now + timedelta(minutes=30))
    assert rows[managed]["waiting_for_room_sent_at"] is None
    assert rows[upstream]["has_entries"] is False
    assert rows[upstream]["scheduled_end_at"] is None


# ── the tick end to end ──────────────────────────────────────────────────────────────────────


@pg_only
async def test_pg_a_late_entry_gets_a_bot_at_once(pg):
    now = _now()
    late = await pg.put(
        "late", now - timedelta(minutes=20), now + timedelta(minutes=40)
    )
    stale = await pg.seed_upstream(now - timedelta(minutes=20), native="abc-defg-hij")
    counters = await pg.tick(now)
    assert counters["spawned"] == 1
    assert (await pg.row(late))["status"] == "requested"
    assert (await pg.row(stale))["status"] == "scheduled"
    assert len(pg.runtime.specs) == 1


@pg_only
async def test_pg_a_waiting_meeting_goes_on_the_first_tick_after_its_sibling_finishes(
    pg,
):
    now = _now()
    first = await pg.put(
        "first", now - timedelta(minutes=10), now + timedelta(minutes=2)
    )
    assert (await pg.tick(now))["spawned"] == 1
    due = await pg.put(
        "second", now + timedelta(minutes=2), now + timedelta(minutes=30)
    )
    assert due != first

    counters = await pg.tick(now)
    assert counters["spawned"] == 0 and counters["skipped_live"] == 1
    row = await pg.row(due)
    assert row["status"] == "scheduled"
    assert row["waiting_for_room_sent_at"] is not None
    assert "auto_join_next_retry" not in row["data"]
    assert "auto_join_error" not in row["data"]
    assert (await pg.events(due))[-1] == "meeting.waiting_for_room"

    await pg.tick(now + timedelta(seconds=30))
    assert (await pg.events(due)).count("meeting.waiting_for_room") == 1

    await pg.set_status(first, "completed")
    counters = await pg.tick(now + timedelta(seconds=60))
    assert counters["spawned"] == 1
    assert (await pg.row(due))["status"] == "requested"
    assert len(pg.runtime.specs) == 2


@pg_only
async def test_pg_the_merge_into_an_open_ended_live_join_now_meeting(pg):
    from intake_builders import instant_body

    now = _now()
    reply = await pg.service.put_entry(USER, instant_body("paste:1", GMEET))
    live = await pg.id_of(reply["meeting"]["id"])
    assert (await pg.row(live))["status"] == "requested"
    due = await pg.put("cal", now + timedelta(minutes=10), now + timedelta(minutes=40))
    assert due != live

    counters = await pg.tick(now + timedelta(minutes=6))
    assert counters["spawned"] == 0
    row = await pg.row(due)
    live_uuid = await pg.scalar(
        "SELECT CAST(uuid AS text) FROM meetings WHERE id = :m", m=live
    )
    assert (row["status"], row["outcome_kind"], row["outcome_detail"]) == (
        "failed",
        "merged_into_live",
        live_uuid,
    )
    assert (await pg.events(due))[-1] == "meeting.removed"
    assert (
        await pg.scalar(
            "SELECT meeting_id FROM meeting_entries WHERE external_id = 'cal'"
        )
        == live
    )
    assert len(pg.runtime.specs) == 1


@pg_only
async def test_pg_a_stopping_join_now_meeting_makes_the_due_meeting_wait(pg):
    from intake_builders import instant_body

    now = _now()
    reply = await pg.service.put_entry(USER, instant_body("paste:1", GMEET))
    live = await pg.id_of(reply["meeting"]["id"])
    await pg.set_status(live, "stopping")
    due = await pg.put("cal", now + timedelta(minutes=10), now + timedelta(minutes=40))

    counters = await pg.tick(now + timedelta(minutes=6))
    assert counters["skipped_live"] == 1 and counters["already"] == 0
    row = await pg.row(due)
    assert row["status"] == "scheduled" and row["outcome_kind"] is None
    assert (await pg.events(due))[-1] == "meeting.waiting_for_room"

    await pg.set_status(live, "completed")
    counters = await pg.tick(now + timedelta(minutes=6, seconds=30))
    assert counters["spawned"] == 1
    assert (await pg.row(due))["status"] == "requested"
    assert len(pg.runtime.specs) == 2


@pytest.mark.parametrize(
    "context,message",
    [
        (None, "the bot limit could not be read: no identity edge is configured"),
        ("unreachable", "the bot limit could not be read: identity is unavailable"),
    ],
)
@pg_only
async def test_pg_a_row_skipped_for_its_bot_limit_records_why(pg, context, message):
    """A meeting skipped because its bot limit can't be read records ``internal_error`` with the
    refusal's exact text, so it ends ``not_sent`` with that cause, not ``ended_before_sent``.
    """
    now = _now()
    mid = await pg.put("e1", now, now + timedelta(minutes=30))

    async def unreachable(_user_id: int) -> None:
        return None

    fetch = None if context is None else unreachable
    counters = await pg.tick(now, fetch_bot_context=fetch)
    assert counters["spawned"] == 0
    row = await pg.row(mid)
    assert row["status"] == "scheduled"
    assert (row["last_error_code"], row["last_error_message"]) == (
        "internal_error",
        message,
    )

    assert await pg.not_sent(now + timedelta(minutes=30)) == 1
    row = await pg.row(mid)
    assert (row["outcome_detail"], row["outcome_message"]) == (
        "internal_error",
        message,
    )


@pg_only
@pytest.mark.parametrize("change", ["moved", "removed"])
async def test_pg_a_meeting_changed_between_the_due_read_and_the_claim_gets_no_bot(
    pg, change
):
    """M2 (§1.5): the tick read the meeting as due; before its claim takes the link lock, a PUT
    moves the meeting to tomorrow, or a remove ends it (R8). The claim re-checks under the lock:
    no bot, and the meeting keeps the change."""
    now = _now()
    mid = await pg.put("e1", now, now + timedelta(minutes=30))
    tomorrow = now + timedelta(days=1)

    async def context_after_the_change(user_id: int) -> dict:
        if change == "moved":
            await pg.put("e1", tomorrow, tomorrow + timedelta(minutes=30))
        else:
            await pg.service.remove_entry(
                USER,
                {"external_id": "e1", "user": "a@abroadworks.com", "reason": "deleted"},
            )
        return await _ctx(user_id)

    counters = await pg.tick(now, fetch_bot_context=context_after_the_change)
    assert (counters["due"], counters["spawned"]) == (1, 0)
    assert pg.runtime.specs == []
    row = await pg.row(mid)
    if change == "moved":
        assert row["status"] == "scheduled"
        assert row["data"]["scheduled_at"] == _iso(tomorrow)
        assert "meeting.status_change" not in await pg.events(mid)
    else:
        assert row["status"] == "failed"
        assert row["outcome_kind"] == "cancelled_by_calendar"


@pg_only
async def test_pg_a_spawn_failure_records_its_code_and_backs_off(pg):
    now = _now()
    for i in range(45):
        await pg.seed_upstream(
            now - timedelta(hours=1), status="active", native=f"n{i:02d}-aaaa-bbb"
        )
    mid = await pg.put("e1", now, now + timedelta(minutes=30))
    counters = await pg.tick(now)
    assert counters["errors"] == 1
    row = await pg.row(mid)
    assert row["status"] == "scheduled"
    assert row["last_error_code"] == "account_limit"
    assert row["last_error_message"] == "bot limit reached (45 of 45)"
    assert row["data"]["auto_join_error"] == "bot limit reached (45 of 45)"
    assert (
        row["data"]["auto_join_next_retry"] == (now + timedelta(seconds=60)).isoformat()
    )
    assert row["send_attempts"] == 1

    assert (await pg.tick(now + timedelta(seconds=30)))["due"] == 0

    assert await pg.not_sent(now + timedelta(minutes=30)) == 1
    row = await pg.row(mid)
    assert (
        row["status"],
        row["outcome_kind"],
        row["outcome_detail"],
        row["outcome_message"],
    ) == ("failed", "not_sent", "account_limit", "bot limit reached (45 of 45)")
    assert (await pg.events(mid))[-1] == "meeting.not_sent"


@pg_only
async def test_pg_not_sent_ended_before_sent_and_room_busy(pg):
    now = _now()
    quiet = await pg.put(
        "quiet",
        now + timedelta(minutes=10),
        now + timedelta(minutes=20),
        url=GMEET_OTHER,
    )
    first = await pg.put(
        "first", now - timedelta(minutes=10), now + timedelta(minutes=2)
    )
    await pg.tick(now)
    busy = await pg.put("busy", now + timedelta(minutes=2), now + timedelta(minutes=20))
    await pg.tick(now)
    assert (await pg.row(busy))["waiting_for_room_sent_at"] is not None

    assert await pg.not_sent(now + timedelta(minutes=20)) == 2
    for mid, detail, message in (
        (quiet, "ended_before_sent", "the meeting ended before a bot was sent"),
        (
            busy,
            "room_busy",
            "another bot was still on this meeting link when the meeting ended",
        ),
    ):
        row = await pg.row(mid)
        assert (
            row["status"],
            row["outcome_kind"],
            row["outcome_detail"],
            row["outcome_message"],
        ) == ("failed", "not_sent", detail, message)
        assert (await pg.events(mid))[-1] == "meeting.not_sent"
    assert (await pg.row(first))["status"] == "requested"


@pg_only
async def test_pg_entry_less_rows_behave_as_before(pg):
    now = _now()
    due = await pg.seed_upstream(now + timedelta(seconds=60))
    stale = await pg.seed_upstream(now - timedelta(minutes=11), native="abc-defg-hij")
    live = await pg.seed_upstream(
        now - timedelta(hours=1), status="active", native="qqq-wwww-eee"
    )
    blocked = await pg.seed_upstream(now, native="qqq-wwww-eee")

    counters = await pg.tick(now)
    assert counters["spawned"] == 1 and counters["skipped_live"] == 1
    assert (await pg.row(due))["status"] == "requested"
    assert (await pg.row(stale))["status"] == "scheduled"
    row = await pg.row(blocked)
    assert row["status"] == "scheduled"
    assert f"meeting {live}" in row["data"]["auto_join_error"]
    assert (
        row["data"]["auto_join_next_retry"]
        == (now + timedelta(seconds=300)).isoformat()
    )
    assert row["waiting_for_room_sent_at"] is None
    assert await pg.events(blocked) == []
    assert await pg.not_sent(now + timedelta(days=1)) == 0


@pg_only
async def test_pg_two_replicas_run_each_sweep_once(pg):
    """Two replicas' advisory locks on one database: each sweep's body runs on one of them."""
    from meeting_api.sweeps.single_flight import (
        PgAdvisoryLock,
        run_single_flight,
        sweep_lock_key,
    )

    now = _now()
    await pg.put("e1", now, now + timedelta(minutes=30))
    ended = await pg.put(
        "e2", now + timedelta(minutes=10), now + timedelta(minutes=15), url=GMEET_OTHER
    )

    replica_a = PgAdvisoryLock(pg.session_factory)
    replica_b = PgAdvisoryLock(pg.session_factory)
    results: dict[str, list] = {}

    async def auto_join() -> None:
        results.setdefault("auto-join", []).append(await pg.tick(now))

    async def not_sent() -> None:
        results.setdefault("not-sent", []).append(
            await pg.not_sent(now + timedelta(minutes=15))
        )

    for name, body in (("auto-join", auto_join), ("not-sent", not_sent)):
        key = sweep_lock_key(name)
        holding, done = asyncio.Event(), asyncio.Event()

        async def on_a(body=body, holding=holding, done=done) -> None:
            holding.set()
            await done.wait()
            await body()

        async def replica_b_tick(
            key=key, body=body, holding=holding, done=done
        ) -> bool:
            await holding.wait()
            try:
                return await run_single_flight(replica_b, key, body)
            finally:
                done.set()

        ran = await asyncio.gather(
            run_single_flight(replica_a, key, on_a), replica_b_tick()
        )
        assert ran == [True, False], name
        assert len(results[name]) == 1, name

    assert results["auto-join"][0]["spawned"] == 1
    assert results["not-sent"] == [1]
    assert (await pg.row(ended))["outcome_detail"] == "ended_before_sent"
    assert len(pg.runtime.specs) == 1


# ── bounded send attempts on Postgres (F-K) ──────────────────────────────────────────────────


async def _full_account(pg: Pg, now: datetime) -> list[int]:
    return [
        await pg.seed_upstream(
            now - timedelta(hours=1), status="active", native=f"n{i:02d}-aaaa-bbb"
        )
        for i in range(45)
    ]


def _not_sent_count(detail: str) -> float:
    from meeting_api.metrics import registry

    value = registry().get_sample_value(
        "aw_meetings_not_sent_total", {"detail": detail, "user_id": str(USER)}
    )
    return value or 0.0


@pg_only
async def test_pg_send_attempts_are_counted_across_ticks_and_replicas(pg):
    """Each failed send is one attempt, stored on the meeting, so a second replica (its own repo,
    store and service over the same database) continues the count; the third ends the meeting
    ``not_sent`` with the last code and message (``meeting.not_sent``, the counter)."""
    now = _now()
    await _full_account(pg, now)
    mid = await pg.put("e1", now, now + timedelta(minutes=30))
    before = _not_sent_count("account_limit")

    assert (await pg.tick(now))["errors"] == 1
    row = await pg.row(mid)
    assert (row["status"], row["send_attempts"]) == ("scheduled", 1)
    assert (await pg.tick(now + timedelta(seconds=30)))["due"] == 0

    replica = Pg(pg.engine)
    assert (await replica.tick(now + timedelta(seconds=60)))["errors"] == 1
    assert (await pg.row(mid))["send_attempts"] == 2

    assert (await pg.tick(now + timedelta(seconds=120)))["errors"] == 1
    row = await pg.row(mid)
    assert (
        row["status"],
        row["outcome_kind"],
        row["outcome_detail"],
        row["outcome_message"],
        row["send_attempts"],
    ) == ("failed", "not_sent", "account_limit", "bot limit reached (45 of 45)", 3)
    assert (await pg.events(mid))[-1] == "meeting.not_sent"
    assert _not_sent_count("account_limit") == before + 1
    assert pg.runtime.specs == [] and replica.runtime.specs == []


@pg_only
async def test_pg_a_send_that_succeeds_on_its_second_attempt_joins_normally(pg):
    now = _now()
    live = await _full_account(pg, now)
    mid = await pg.put("e1", now, now + timedelta(minutes=30))
    assert (await pg.tick(now))["errors"] == 1
    await pg.set_status(live[0], "completed")

    counters = await pg.tick(now + timedelta(seconds=60))
    assert counters["spawned"] == 1
    row = await pg.row(mid)
    assert (row["status"], row["outcome_kind"], row["send_attempts"]) == (
        "requested",
        None,
        1,
    )
    assert "auto_join_error" not in row["data"]
    assert len(pg.runtime.specs) == 1


@pg_only
async def test_pg_the_attempt_limit_is_the_setting(pg):
    one = Pg(pg.engine, send_max_attempts=1)
    now = _now()
    await _full_account(one, now)
    mid = await one.put("e1", now, now + timedelta(minutes=30))
    await one.tick(now)
    row = await one.row(mid)
    assert (row["status"], row["outcome_detail"], row["send_attempts"]) == (
        "failed",
        "account_limit",
        1,
    )


# ── paged, bounded sweeps on Postgres (§6.9 F-I) ─────────────────────────────────────────────


@pg_only
async def test_pg_the_due_read_pages_by_meeting_time_then_id(pg):
    now = _now()
    ids = [
        await pg.seed_upstream(now - timedelta(minutes=1), native=f"p{n}-aaaa-bbb")
        for n in range(3)
    ] + [await pg.seed_upstream(now, native=f"q{n}-aaaa-bbb") for n in range(2)]
    first = await pg.repo.list_due_meetings(now, 300, after=None, limit=2)
    rest = await pg.repo.list_due_meetings(
        now, 300, after=(first[-1]["event_time"], first[-1]["id"]), limit=10
    )
    assert [r["id"] for r in first + rest] == ids
    counters = await pg.tick(now, batch_size=2)
    assert counters["spawned"] == 5 and len(pg.runtime.specs) == 5


@pg_only
async def test_pg_the_not_sent_read_pages_by_id_and_a_given_up_meeting_is_skipped(pg):
    from meeting_api.sweeps.item_failures import PostgresItemFailures

    now = _now()
    mids = [
        await pg.put(
            f"e{n}",
            now - timedelta(minutes=30),
            now + timedelta(minutes=1),
            url=f"https://meet.google.com/aaa-bbbb-cc{letter}",
        )
        for n, letter in enumerate("abc")
    ]
    failures = PostgresItemFailures(pg.session_factory, max_failures=1)
    await failures.failed("not-sent", str(mids[1]), RuntimeError("poison"))
    ended = await not_sent_tick(
        pg.store,
        publisher=pg.publisher,
        now=now + timedelta(minutes=2),
        failures=failures,
        batch_size=1,
    )
    assert ended == 2
    assert [(await pg.row(m))["status"] for m in mids] == [
        "failed",
        "scheduled",
        "failed",
    ]
