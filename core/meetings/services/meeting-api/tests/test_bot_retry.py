"""§6.9 F-K2 — a bot that fails while its meeting is on gets a new bot on the SAME meeting.

Offline, on the in-memory intake store: the decision (``retry.due_at``) and the one writer
(``retry.retry``). The Postgres wiring (the lifecycle write, ``fail_meeting``, the spawn port) is
in ``test_bot_retry_pg.py``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest

from intake_builders import GMEET, make_settings
from meeting_api.intake import retry
from meeting_api.intake.fakes import InMemoryIntakeStore
from meeting_api.intake.ports import Room
from meeting_api.intake.rules import Plan
from meeting_api.intake.status import Outcome
from meeting_api.intake.validation import parse_entry

UTC = timezone.utc
USER = 1
ROOM = Room("google_meet", "kxo-misr-avz")
NOW = datetime(2026, 9, 29, 9, 10, 0, tzinfo=UTC)
SETTINGS = make_settings(send_max_attempts=3, send_retry_backoff_s=60)


def _store() -> InMemoryIntakeStore:
    return InMemoryIntakeStore(clock=lambda: NOW, lead_s=300)


def _meeting(
    store: InMemoryIntakeStore,
    *,
    status: str = "active",
    end: Optional[datetime] = NOW + timedelta(minutes=20),
    entry_state: Optional[str] = "active",
) -> int:
    start = NOW - timedelta(minutes=10)
    body: dict = {
        "external_id": "google:cal-1",
        "user": "a@abroadworks.com",
        "meeting_url": GMEET,
    }
    if end is None:
        body["join_now"] = True  # a pasted link: open-ended
    else:
        body.update(start=start.isoformat(), end=end.isoformat())
    entry = parse_entry(body, now=start, max_days_ahead=30)
    return store.seed_meeting(
        USER,
        ROOM,
        status=status,
        plan=Plan(start, end, None, None, GMEET),
        entries=[(entry, entry_state)] if entry_state else [],
    )


def _failed(reason: Optional[str] = "join_failure", **kw) -> retry.Failure:
    kw.setdefault("message", "the bot could not join")
    kw.setdefault("stage", "joining")
    return retry.Failure(status="failed", reason=reason, **kw)


def _due(store: InMemoryIntakeStore, mid: int, failure: retry.Failure, **kw):
    kw.setdefault("settings", SETTINGS)
    return retry.due_at(store.view(mid), failure, now=NOW, **kw)


# ── the decision ────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "reason",
    [
        "join_failure",
        "awaiting_admission_timeout",
        "awaiting_admission_rejected",
        "start_failed",
        "left_alone",
        None,
    ],
)
@pytest.mark.parametrize(
    "status", ["requested", "joining", "awaiting_admission", "active", "needs_help"]
)
def test_a_failed_bot_on_an_entry_managed_live_meeting_is_retried(status, reason):
    store = _store()
    mid = _meeting(store, status=status)
    assert _due(store, mid, _failed(reason)) == NOW + timedelta(seconds=60)


@pytest.mark.parametrize("reason", ["stopped", "evicted", "startup_alone"])
def test_a_stop_a_host_removal_and_nobody_joining_are_not_retried(reason):
    store = _store()
    mid = _meeting(store)
    assert _due(store, mid, _failed(reason)) is None
    assert _due(store, mid, retry.Failure("completed", reason, "x", lost=True)) is None


def test_a_normal_end_is_not_retried_and_a_lost_bot_is():
    store = _store()
    mid = _meeting(store)
    assert _due(store, mid, retry.Failure("completed", "left_alone", "x")) is None
    lost = retry.Failure("completed", "left_alone", "x", lost=True)
    assert _due(store, mid, lost) == NOW + timedelta(seconds=60)


def test_a_meeting_without_active_entries_is_out_of_scope():
    store = _store()
    assert _due(store, _meeting(store, entry_state=None), _failed()) is None
    assert _due(store, _meeting(store, entry_state="removed"), _failed()) is None


def test_a_stopped_or_ended_meeting_is_not_retried():
    store = _store()
    stopping = _meeting(store, status="stopping")
    assert _due(store, stopping, _failed()) is None
    flagged = _meeting(store)
    store.meetings[flagged]["data"]["stop_requested"] = True
    assert _due(store, flagged, _failed()) is None
    removed = _meeting(store)
    store.aw[removed]["outcome_kind"] = "cancelled_by_calendar"
    assert _due(store, removed, _failed()) is None
    finished = _meeting(store, status="failed")
    assert _due(store, finished, _failed()) is None


def test_the_sends_are_bounded_by_the_attempts_setting():
    store = _store()
    mid = _meeting(store)
    store.aw[mid]["send_attempts"] = 1
    assert _due(store, mid, _failed()) == NOW + timedelta(seconds=60)
    store.aw[mid]["send_attempts"] = 2
    assert _due(store, mid, _failed()) is None
    store.aw[mid]["send_attempts"] = 0
    assert (
        _due(store, mid, _failed(), settings=make_settings(send_max_attempts=1)) is None
    )


def test_the_next_bot_must_go_before_the_planned_end():
    store = _store()
    soon = _meeting(store, end=NOW + timedelta(seconds=60))
    assert _due(store, soon, _failed()) is None
    later = _meeting(store, end=NOW + timedelta(seconds=61))
    assert _due(store, later, _failed()) == NOW + timedelta(seconds=60)
    open_ended = _meeting(store, end=None)
    assert _due(store, open_ended, _failed()) == NOW + timedelta(seconds=60)


def test_is_last_is_a_bot_failure_on_an_in_scope_meeting():
    store = _store()
    mid = _meeting(store)
    store.aw[mid]["send_attempts"] = 2
    lost = retry.Failure("completed", "left_alone", "x", lost=True)
    assert retry.is_last(store.view(mid), lost) is True
    assert (
        retry.is_last(store.view(mid), retry.Failure("completed", "left_alone", "x"))
        is False
    )


# ── the writer ──────────────────────────────────────────────────────────────────────────────


async def test_the_writer_sends_the_meeting_back_to_requested_with_bot_retry():
    store = _store()
    mid = _meeting(store, status="active")
    failure = _failed(
        "join_failure",
        message="the page never loaded",
        stage="joining",
        session="sess-1",
        workload="mtg-1-abcd1234",
    )
    async with store.room_lock(USER, [ROOM]) as tx:
        written = await retry.retry(
            tx,
            mid,
            failure,
            now=NOW,
            settings=SETTINGS,
            data_patch={
                "completion_reason": "join_failure",
                "failure_stage": "joining",
                "failure_reason": "the page never loaded",
                "status_transition": [{"from": "active", "to": "failed"}],
            },
        )
    assert written is not None
    view = store.view(mid)
    assert view.status == "requested"
    assert "completion_reason" not in view.data and "failure_stage" not in view.data
    assert "failure_reason" not in view.data
    assert view.data["status_transition"] == [{"from": "active", "to": "failed"}]
    assert view.data["bot_retry"] == {
        "reason": "join_failure",
        "stage": "joining",
        "message": "the page never loaded",
        "after_session": "sess-1",
        "workload": "mtg-1-abcd1234",
        "at": "2026-09-29T09:10:00Z",
        "due_at": "2026-09-29T09:11:00Z",
        "proven_gone": False,
    }
    assert (view.aw["send_attempts"], view.aw["last_error_code"]) == (1, "bot_failed")
    assert view.aw["last_error_message"] == "the page never loaded"
    event = store.events[-1]
    assert (event.event_type, event.sequence) == ("bot.retry", written.sequence)
    assert event.change == {
        "from": "active",
        "to": "requested",
        "reason": "join_failure",
        "at": "2026-09-29T09:10:00Z",
    }
    assert event.meeting["status"] == "requested"
    assert [e.state for e in view.entries] == ["active"]


async def test_the_writer_writes_nothing_for_a_failure_it_does_not_retry():
    store = _store()
    mid = _meeting(store)
    before = (dict(store.meetings[mid]), dict(store.aw[mid]), len(store.events))
    async with store.room_lock(USER, [ROOM]) as tx:
        assert (
            await retry.retry(tx, mid, _failed("evicted"), now=NOW, settings=SETTINGS)
            is None
        )
    assert (store.meetings[mid], store.aw[mid], len(store.events)) == before


async def test_a_failure_without_a_completion_reason_changes_with_its_code():
    store = _store()
    mid = _meeting(store, status="requested")
    failure = retry.Failure(
        "failed",
        None,
        "bot limit reached",
        stage="requested",
        code="account_limit",
        proven_gone=True,
    )
    async with store.room_lock(USER, [ROOM]) as tx:
        await retry.retry(tx, mid, failure, now=NOW, settings=SETTINGS)
    assert store.events[-1].change["reason"] == "account_limit"
    assert store.aw[mid]["last_error_code"] == "account_limit"
    assert store.view(mid).data["bot_retry"]["proven_gone"] is True


def test_marker_reads_only_a_mapping():
    assert retry.marker({"bot_retry": {"due_at": "x"}}) == {"due_at": "x"}
    assert retry.marker({"bot_retry": None}) is None
    assert retry.marker(None) is None


def test_an_outcome_on_the_meeting_is_an_ending():
    store = _store()
    mid = _meeting(store)
    store.aw[mid]["outcome_kind"] = Outcome("cancelled_by_calendar", None, None).kind
    assert retry.in_scope(store.view(mid)) is False
