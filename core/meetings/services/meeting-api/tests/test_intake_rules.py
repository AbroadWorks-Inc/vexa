"""§1.1 R1 — the pure matching rules (`meeting_api.intake.rules`).

No storage, no clock: every function takes its times as arguments. The meeting and entry stand-ins
below carry only the attributes the rules read, which is all the rules' protocols ask for.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest

from meeting_api.intake.rules import (
    Plan,
    finished_window,
    is_future_move,
    is_rerun,
    join_now_target,
    match_entry,
    meeting_start,
    meeting_window,
    overlaps,
    recompute,
)

UTC = timezone.utc
H = timedelta(hours=1)
M15 = timedelta(minutes=15)
LEAD = 300


def at(hh: int, mm: int = 0, day: int = 29) -> datetime:
    return datetime(2026, 9, day, hh, mm, tzinfo=UTC)


@dataclass(frozen=True)
class M:
    id: int
    status: str
    start: Optional[datetime]
    end: Optional[datetime]


@dataclass(frozen=True)
class E:
    start: datetime
    end: Optional[datetime]
    title: Optional[str] = None
    time_zone: Optional[str] = None
    meeting_url: str = "https://meet.google.com/kxo-misr-avz"


# ── overlaps ─────────────────────────────────────────────────────────────────────────────────


def test_touching_times_do_not_overlap():
    assert not overlaps(at(15), at(16), at(16), at(17))
    assert not overlaps(at(16), at(17), at(15), at(16))


def test_overlap_is_half_open_and_covers_containment():
    assert overlaps(at(15), at(16), at(15, 30), at(17))
    assert overlaps(at(15), at(18), at(16), at(17))
    assert overlaps(at(16), at(17), at(15), at(18))


def test_a_missing_end_is_unbounded():
    assert overlaps(at(15), None, at(20), at(21))
    assert overlaps(at(20), at(21), at(15), None)
    assert overlaps(at(15), None, at(9), None)
    assert not overlaps(at(21), None, at(20), at(21))
    assert not overlaps(at(9), at(10), at(10), None)


# ── meeting_start / meeting_window ───────────────────────────────────────────────────────────


def test_meeting_start_prefers_scheduled_at_then_start_time_then_created_at():
    created = at(8)
    started = at(9)
    assert meeting_start(
        {"scheduled_at": "2026-09-29T10:00:00Z"}, started, created
    ) == at(10)
    assert meeting_start({}, started, created) == started
    assert meeting_start(None, None, created) == created
    assert meeting_start({}, None, None) is None
    # naive database stamps are UTC
    assert meeting_start({}, datetime(2026, 9, 29, 9, 0), None) == at(9)


def test_bounded_meeting_window_is_its_stored_time():
    m = M(1, "scheduled", at(10), at(10, 30))
    assert meeting_window(m, now=at(9)) == (at(10), at(10, 30))


def test_open_ended_live_meeting_window_ends_now():
    m = M(1, "active", at(9), None)
    assert meeting_window(m, now=at(9, 40)) == (at(9), at(9, 40))


def test_open_ended_meeting_not_yet_live_is_unbounded():
    m = M(1, "scheduled", at(9), None)
    assert meeting_window(m, now=at(9, 1)) == (at(9), None)


# ── match_entry ──────────────────────────────────────────────────────────────────────────────


def test_overlapping_entry_matches():
    m = M(1, "scheduled", at(9), at(9, 30))
    assert match_entry(E(at(9), at(9, 30)), [m], now=at(8), lead_s=LEAD) is m
    assert match_entry(E(at(9, 15), at(10)), [m], now=at(8), lead_s=LEAD) is m


def test_back_to_back_entry_does_not_match():
    m = M(1, "scheduled", at(15), at(16))
    assert match_entry(E(at(16), at(17)), [m], now=at(8), lead_s=LEAD) is None
    assert match_entry(E(at(14), at(15)), [m], now=at(8), lead_s=LEAD) is None


def test_finished_meetings_never_match():
    m = M(1, "completed", at(9), at(10))
    assert match_entry(E(at(9), at(10)), [m], now=at(8), lead_s=LEAD) is None


def test_open_ended_live_meeting_matches_only_started_or_due_entries():
    live = M(1, "active", at(9), None)
    now = at(9, 40)
    # already started
    assert match_entry(E(at(9, 30), at(10)), [live], now=now, lead_s=LEAD) is live
    # due: starts within the lead (inclusive bound)
    assert (
        match_entry(
            E(now + timedelta(seconds=LEAD), at(11)), [live], now=now, lead_s=LEAD
        )
        is live
    )
    # not yet due
    later = now + timedelta(seconds=LEAD + 1)
    assert match_entry(E(later, at(11)), [live], now=now, lead_s=LEAD) is None
    # ended before the live meeting began
    assert match_entry(E(at(8), at(9)), [live], now=now, lead_s=LEAD) is None


def test_match_tie_break_is_earliest_start_then_lowest_id():
    a = M(7, "scheduled", at(9, 15), at(10))
    b = M(3, "scheduled", at(9), at(10))
    c = M(2, "scheduled", at(9), at(10))
    assert match_entry(E(at(9, 30), at(9, 45)), [a, b, c], now=at(8), lead_s=LEAD) is c


# ── join_now_target ──────────────────────────────────────────────────────────────────────────


def test_join_now_at_0945_adopts_the_1000_meeting():
    m = M(1, "scheduled", at(10), at(10, 30))
    assert join_now_target([m], now=at(9, 45), adopt_ahead_s=3600) is m


def test_join_now_bound_is_inclusive_at_the_adopt_ahead_limit():
    m = M(1, "scheduled", at(10), at(10, 30))
    assert join_now_target([m], now=at(9), adopt_ahead_s=3600) is m
    assert (
        join_now_target([m], now=at(9) - timedelta(seconds=1), adopt_ahead_s=3600)
        is None
    )


def test_join_now_at_1020_does_not_adopt_tomorrow():
    today = M(1, "scheduled", at(10), at(10, 15))
    tomorrow = M(2, "scheduled", at(10, day=30), at(10, 15, day=30))
    assert (
        join_now_target([today, tomorrow], now=at(10, 20), adopt_ahead_s=3600) is None
    )


def test_join_now_skips_finished_and_ended_meetings():
    finished = M(1, "completed", at(10), at(11))
    ended = M(2, "scheduled", at(9), at(10))
    assert join_now_target([finished, ended], now=at(10), adopt_ahead_s=3600) is None


def test_join_now_adopts_an_open_ended_live_meeting():
    live = M(1, "active", at(9), None)
    assert join_now_target([live], now=at(11), adopt_ahead_s=3600) is live


def test_join_now_adopts_a_live_meeting_running_past_its_planned_end():
    live = M(1, "active", at(9), at(10))
    assert join_now_target([live], now=at(10, 5), adopt_ahead_s=3600) is live


def test_join_now_takes_the_earliest_then_the_lowest_id():
    a = M(5, "scheduled", at(10), at(10, 30))
    b = M(9, "scheduled", at(9, 50), at(10, 30))
    c = M(4, "scheduled", at(9, 50), at(10, 30))
    assert join_now_target([a, b, c], now=at(9, 45), adopt_ahead_s=3600) is c


# ── recompute ────────────────────────────────────────────────────────────────────────────────


def test_recompute_spans_earliest_start_to_latest_end():
    plan = recompute(
        [
            E(at(9, 30), at(10), title="B", time_zone="Europe/Berlin"),
            E(at(9), at(9, 45), title="A", time_zone="Asia/Kolkata"),
        ]
    )
    assert plan == Plan(
        start=at(9),
        end=at(10),
        title="A",
        time_zone="Asia/Kolkata",
        meeting_url="https://meet.google.com/kxo-misr-avz",
    )


def test_recompute_is_open_ended_if_any_entry_is():
    plan = recompute([E(at(9), at(10)), E(at(9, 45), None)])
    assert (plan.start, plan.end) == (at(9), None)


def test_recompute_takes_the_first_title_and_zone_present_in_start_order():
    plan = recompute(
        [E(at(9), at(10)), E(at(9, 5), at(10), title="Sync", time_zone="Asia/Kolkata")]
    )
    assert (plan.title, plan.time_zone) == ("Sync", "Asia/Kolkata")


def test_recompute_keeps_input_order_on_equal_starts():
    plan = recompute(
        [
            E(
                at(9),
                at(10),
                title="first",
                meeting_url="https://zoom.us/j/12345678901",
            ),
            E(
                at(9),
                at(10),
                title="second",
                meeting_url="https://zoom.us/j/12345678901?pwd=x",
            ),
        ]
    )
    assert (plan.title, plan.meeting_url) == ("first", "https://zoom.us/j/12345678901")


def test_recompute_needs_an_entry():
    with pytest.raises(ValueError):
        recompute([])


# ── R7 / R10: finished windows ───────────────────────────────────────────────────────────────


def test_finished_window_is_start_to_finish_clamped():
    assert finished_window(at(9), finish=at(9, 40)) == (at(9), at(9, 40))
    assert finished_window(at(10), finish=at(9, 50)) == (at(10), at(10))
    assert finished_window(None, finish=at(9, 50)) == (at(9, 50), at(9, 50))


def test_rerun_needs_no_overlap_a_later_start_and_a_future_start():
    window = (at(9), at(9, 20))
    assert is_rerun(at(9, 30), at(10), window, finish=at(9, 20))
    assert not is_rerun(at(9, 10), at(9, 30), window, finish=at(9, 20))  # overlaps
    assert not is_rerun(
        at(8), at(8, 30), window, finish=at(9, 20)
    )  # before the meeting
    assert not is_rerun(None, None, window, finish=at(9, 20))


def test_same_time_after_finish_is_not_a_future_move():
    finished = M(1, "failed", at(9), at(10))
    assert not is_future_move(E(at(9), at(10)), finished, now=at(11))


def test_future_time_after_finish_is_a_future_move():
    finished = M(1, "failed", at(9), at(10))
    assert is_future_move(E(at(15), at(16)), finished, now=at(11))


def test_a_past_time_is_never_a_future_move():
    finished = M(1, "completed", at(9), at(10))
    assert not is_future_move(E(at(10, 30), at(10, 45)), finished, now=at(11))


def test_an_entry_not_after_the_meeting_start_belongs_to_it():
    # finished before its planned start (an early instant join); the same time is not new
    finished = M(1, "completed", at(10), at(10, 30))
    assert not is_future_move(E(at(10), at(10, 30)), finished, now=at(9, 55))
    assert is_future_move(E(at(10, 5), at(10, 30)), finished, now=at(9, 55))
