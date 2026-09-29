"""§6.9 F-K2 — a bot that fails while its meeting is on gets a new bot on the SAME meeting.

Offline, on the in-memory fakes:
  * the decision (``retry.due_at``) and the one writer (``retry.retry``);
  * the lifecycle callback (``app._apply_lifecycle_event``) on a session whose terminal the repo
    turned into a retry, into the last lost bot's ``failed``, or refused as stale: the meeting-level
    side effects follow the row's persisted status, not the session's;
  * the newest-session guard of the in-memory repo;
  * the claim of a waiting meeting (the in-memory repo, the same rules as the SQL one), the
    workload-gone proof (``reconcile.prove_workload_gone``) and the runtime callback marking a
    waiting meeting's workload gone instead of driving a terminal;
  * a waiting meeting stopped or losing its last entry ends at once, with no leave command; its
    ``bot_joins_at`` is the retry's ``due_at`` (the ``intake.v1`` golden
    ``Meeting.retry-pending``); the reconcile sweep leaves it to the retry.

The Postgres wiring (the lifecycle write, ``fail_meeting``, the spawn port) is in
``test_bot_retry_pg.py``.
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
from meeting_api.sweeps.item_failures import InMemoryItemFailures

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


# ── the lifecycle callback follows the row, not the session ─────────────────────────────────

MARKER = {
    "reason": "join_failure",
    "due_at": "2026-09-29T09:11:00Z",
    "proven_gone": False,
}


class _Repo:
    """``InMemoryMeetingRepo`` whose session terminal comes back the way the SQL adapter writes
    it (``mode``): ``retry`` → the row ``requested`` with ``data.bot_retry``; ``last_lost`` → a
    lost ``completed`` as the row's ``failed``; ``stale`` → refused (``None``)."""

    def __new__(cls, mode: str):
        from meeting_api.bot_spawn.fakes import InMemoryMeetingRepo

        class Repo(InMemoryMeetingRepo):
            async def update_meeting_status(self, **kw):
                terminal = kw["status"] in ("completed", "failed")
                sess = next(
                    s for s in self.sessions if s["session_uid"] == kw["session_uid"]
                )
                row = self._meetings[sess["meeting_id"]]
                if terminal and mode == "stale":
                    return None
                if (
                    terminal
                    and mode == "retry"
                    and row["status"] not in ("completed", "failed")
                ):
                    row["status"] = "requested"
                    row["data"]["bot_retry"] = dict(MARKER)
                    return dict(row)
                if kw["status"] == "completed" and mode == "last_lost":
                    kw = {**kw, "status": "failed"}
                return await super().update_meeting_status(**kw)

        return Repo()


class _Sinks:
    def __init__(self) -> None:
        self.system: list[str] = []
        self.user: list[str] = []
        self.finalized: list[int] = []
        self.reaped: list[str] = []
        self.published: list[str] = []
        self.frames: list[tuple[str, dict]] = []

    async def deliver(self, *args, **kwargs):  # the system sink: (envelope, label=)
        from types import SimpleNamespace

        if len(args) == 1:
            self.system.append(args[0]["event_type"])
        else:  # the per-user sink: (url, envelope, secret, …)
            self.user.append(args[1]["event_type"])
        return SimpleNamespace(status="delivered", status_code=200, error=None)

    async def finalize(self, meeting_id: int) -> int:
        self.finalized.append(meeting_id)
        return 0

    async def publish(self, channel: str, data: str):
        import json

        self.frames.append((channel, json.loads(data)))
        return 1

    async def xadd(self, stream: str, payload: dict):
        self.reaped.append(stream)
        return "1-0"


class _UserSink:
    def __init__(self, sinks: _Sinks) -> None:
        self._sinks = sinks

    async def deliver(self, url, env, secret, **kw):
        return await self._sinks.deliver(url, env, secret)


async def _drive(mode: str, events: list[tuple[dict, bool]], monkeypatch) -> tuple:
    from meeting_api import create_app
    from meeting_api import events as events_mod
    from meeting_api.lifecycle.machine import TransitionSource

    sinks = _Sinks()

    async def publish(event_type, source_event_id, refs, **kw):
        sinks.published.append(event_type)
        return True

    monkeypatch.setattr(events_mod, "publish", publish)
    repo = _Repo(mode)
    meeting = await repo.create_meeting(
        user_id=USER,
        platform="google_meet",
        native_meeting_id="kxo-misr-avz",
        data={"webhook_url": "https://hooks.example/aw"},
    )
    await repo.create_session(meeting_id=meeting["id"], session_uid="sess-1")
    app = create_app(
        meeting_repo=repo,
        system_webhook_sink=sinks,
        webhook_sink=_UserSink(sinks),
        transcript_finalizer=sinks.finalize,
        redis=sinks,
    )
    for body, destroyed in events:
        status, content = await app.state.apply_lifecycle_event(
            {"connection_id": "sess-1", **body},
            transition_source=(
                TransitionSource.RUNTIME_DESTROY
                if destroyed
                else TransitionSource.BOT_CALLBACK
            ),
            force_terminal_on_destroy=destroyed,
        )
        assert status == 200, content
    return app, sinks, repo._meetings[meeting["id"]]


JOINING = ({"status": "joining"}, False)
ACTIVE = ({"status": "active"}, False)


async def test_a_retried_session_sends_bot_retry_and_no_meeting_level_side_effect(
    monkeypatch,
):
    failed = ({"status": "failed", "completion_reason": "join_failure"}, False)
    app, sinks, row = await _drive("retry", [JOINING, failed], monkeypatch)
    assert row["status"] == "requested"
    typed = app.state.typed_webhooks[-1]
    assert typed["event_type"] == "bot.retry"
    assert typed["data"]["meeting"]["status"] == "requested"
    assert typed["data"]["status_change"]["to"] == "failed"
    assert sinks.user[-2:] == ["meeting.status_change", "bot.retry"]
    assert sinks.system == []
    assert sinks.finalized == [] and sinks.reaped == [] and sinks.published == []


async def test_the_ws_frames_carry_the_rows_status_not_the_sessions(monkeypatch):
    failed = ({"status": "failed", "completion_reason": "join_failure"}, False)
    _, sinks, row = await _drive("retry", [JOINING, failed], monkeypatch)
    meeting, user = sinks.frames[-2], sinks.frames[-1]
    assert meeting[0] == f"bm:meeting:{row['id']}:status"
    assert meeting[1]["payload"]["status"] == "requested"
    assert (user[0], user[1]["status"]) == ("u:1:meetings", "requested")
    lost = ({"status": "completed", "completion_reason": "left_alone"}, True)
    _, sinks, row = await _drive("last_lost", [JOINING, ACTIVE, lost], monkeypatch)
    assert [
        f[1].get("status") or f[1]["payload"]["status"] for f in sinks.frames[-2:]
    ] == [
        "failed",
        "failed",
    ]


async def test_a_lost_bot_on_its_last_attempt_is_bot_failed_everywhere(monkeypatch):
    lost = ({"status": "completed", "completion_reason": "left_alone"}, True)
    app, sinks, row = await _drive("last_lost", [JOINING, ACTIVE, lost], monkeypatch)
    assert row["status"] == "failed"
    assert app.state.typed_webhooks[-1]["event_type"] == "bot.failed"
    assert sinks.system == ["bot.failed"]
    assert sinks.user[-1] == "bot.failed"
    assert sinks.finalized == [row["id"]] and len(sinks.reaped) == 1
    assert "meeting.completed" not in sinks.published


async def test_a_refused_stale_terminal_reaches_no_system_sink_and_no_flows(
    monkeypatch,
):
    done = ({"status": "completed", "completion_reason": "left_alone"}, False)
    _, sinks, _ = await _drive("stale", [JOINING, ACTIVE, done], monkeypatch)
    assert sinks.system == []
    assert "meeting.completed" not in sinks.published
    assert sinks.finalized == [] and sinks.reaped == []


async def test_a_normal_end_still_reaches_every_sink_once(monkeypatch):
    done = ({"status": "completed", "completion_reason": "left_alone"}, False)
    _, sinks, row = await _drive("normal", [JOINING, ACTIVE, done, done], monkeypatch)
    assert row["status"] == "completed"
    assert sinks.system == ["meeting.completed"]
    assert sinks.published.count("meeting.completed") == 1
    assert sinks.finalized == [row["id"]]


# ── a meeting the retry ends runs every finish step the lifecycle runs ──────────────────────


@pytest.mark.parametrize(
    ("status", "event", "flows"),
    [
        ("failed", "bot.failed", []),
        ("completed", "meeting.completed", ["meeting.completed"]),
    ],
)
async def test_a_meeting_ended_outside_the_lifecycle_runs_every_finish_step(
    monkeypatch, status, event, flows
):
    from meeting_api import create_app
    from meeting_api import events as events_mod
    from meeting_api.bot_spawn.fakes import InMemoryMeetingRepo

    sinks = _Sinks()

    async def publish(event_type, source_event_id, refs, **kw):
        sinks.published.append(event_type)
        return True

    monkeypatch.setattr(events_mod, "publish", publish)
    repo = InMemoryMeetingRepo()
    meeting = await repo.create_meeting(
        user_id=USER,
        platform="google_meet",
        native_meeting_id="kxo-misr-avz",
        data={"webhook_url": "https://hooks.example/aw"},
    )
    await repo.create_session(meeting_id=meeting["id"], session_uid="sess-1")
    repo._meetings[meeting["id"]]["status"] = status
    app = create_app(
        meeting_repo=repo,
        system_webhook_sink=sinks,
        webhook_sink=_UserSink(sinks),
        transcript_finalizer=sinks.finalize,
        redis=sinks,
    )
    await app.state.finish_meeting(meeting["id"])
    assert sinks.finalized == [meeting["id"]]
    assert sinks.system == [event]
    assert sinks.user == [event]
    assert sinks.published == flows
    assert sinks.reaped == [f"tc:meeting:{meeting['id']}"]


async def test_a_meeting_end_is_finished_once_however_often_the_finish_is_asked():
    """M7: a second finish of the same meeting end is a no-op: one system-hook post, one
    reap. A continued meeting's next end is a new end, finished again."""
    from meeting_api import create_app
    from meeting_api.bot_spawn.fakes import InMemoryMeetingRepo

    sinks = _Sinks()
    repo = InMemoryMeetingRepo()
    meeting = await repo.create_meeting(
        user_id=USER,
        platform="google_meet",
        native_meeting_id="kxo-misr-avz",
        data={},
    )
    mid = meeting["id"]
    await repo.create_session(meeting_id=mid, session_uid="sess-1")
    repo._meetings[mid]["status"] = "failed"
    app = create_app(
        meeting_repo=repo,
        system_webhook_sink=sinks,
        transcript_finalizer=sinks.finalize,
        redis=sinks,
    )
    await app.state.finish_meeting(mid)
    await app.state.finish_meeting(mid, stopped=True)
    assert sinks.system == ["bot.failed"]
    assert sinks.finalized == [mid] and sinks.reaped == [f"tc:meeting:{mid}"]
    await repo.create_session(
        meeting_id=mid, session_uid="sess-2"
    )  # continued, ended again
    await app.state.finish_meeting(mid)
    assert sinks.system == ["bot.failed", "bot.failed"]
    assert len(sinks.reaped) == 2


# ── the newest session speaks for the meeting ───────────────────────────────────────────────


async def test_a_session_that_is_not_the_meetings_newest_writes_nothing():
    from meeting_api.bot_spawn.fakes import InMemoryMeetingRepo

    repo = InMemoryMeetingRepo()
    meeting = await repo.create_meeting(
        user_id=USER, platform="google_meet", native_meeting_id="kxo-misr-avz", data={}
    )
    await repo.create_session(meeting_id=meeting["id"], session_uid="sess-old")
    await repo.create_session(meeting_id=meeting["id"], session_uid="sess-new")
    assert (
        await repo.update_meeting_status(session_uid="sess-old", status="joining")
        is None
    )
    assert (
        await repo.update_meeting_status(
            session_uid="sess-old", status="requested", data={"x": 1}
        )
        is None
    )
    assert repo._meetings[meeting["id"]]["status"] == "requested"
    assert "x" not in repo._meetings[meeting["id"]]["data"]
    row = await repo.update_meeting_status(session_uid="sess-new", status="joining")
    assert row["status"] == "joining"


# ── the claim of a waiting meeting ──────────────────────────────────────────────────────────


def _waiting_repo(*, proven: bool, status: str = "requested"):
    from meeting_api.bot_spawn.fakes import InMemoryMeetingRepo

    repo = InMemoryMeetingRepo()
    repo._meetings[5] = {
        "id": 5,
        "user_id": USER,
        "platform": "google_meet",
        "native_meeting_id": "kxo-misr-avz",
        "platform_specific_id": "kxo-misr-avz",
        "status": status,
        "bot_container_id": "mtg-5-old",
        "start_time": None,
        "end_time": None,
        "created_at": "2026-09-29T09:00:00Z",
        "updated_at": "2026-09-01T09:00:00Z",
        "data": {
            "title": "standup",
            "bot_retry": {
                "reason": "join_failure",
                "stage": "joining",
                "message": "no page",
                "after_session": "sess-old",
                "workload": "mtg-5-old",
                "due_at": "2026-09-29T09:11:00Z",
                "proven_gone": proven,
            },
        },
    }
    return repo


async def _claim(repo, **kw):
    return await repo.create_meeting_guarded(
        user_id=USER,
        platform="google_meet",
        native_meeting_id="kxo-misr-avz",
        data={"k": "v"},
        claim_meeting_id=5,
        **kw,
    )


async def test_the_claim_takes_a_waiting_meeting_whose_workload_is_proven_gone():
    repo = _waiting_repo(proven=True)
    row = await _claim(repo, max_concurrent=1)  # the row does not count against itself
    assert row["id"] == 5 and row["status"] == "requested"
    assert row["bot_container_id"] is None
    data = row["data"]
    assert "bot_retry" not in data and data["k"] == "v" and data["title"] == "standup"
    assert "auto_join_last_attempt" in data
    archived = data["completion_history"][-1]
    assert (archived["completion_reason"], archived["failure_stage"]) == (
        "join_failure",
        "joining",
    )
    assert archived["failure_reason"] == "no page"
    assert archived["after_session"] == "sess-old"
    assert "completion_reason" not in data


async def test_the_claim_refuses_a_waiting_meeting_not_proven_gone():
    from meeting_api.bot_spawn.ports import DuplicateMeeting

    repo = _waiting_repo(proven=False)
    with pytest.raises(DuplicateMeeting):
        await _claim(repo)
    assert repo._meetings[5]["data"]["bot_retry"]["proven_gone"] is False


async def test_a_waiting_meeting_holds_a_bot_slot_for_every_other_spawn():
    from meeting_api.bot_spawn.ports import MaxBotsExceeded

    repo = _waiting_repo(proven=True)
    with pytest.raises(MaxBotsExceeded):
        await repo.create_meeting_guarded(
            user_id=USER,
            platform="google_meet",
            native_meeting_id="abc-defg-hij",
            data={},
            max_concurrent=1,
        )


async def test_a_signed_in_bot_is_not_busy_with_the_meeting_it_retries(monkeypatch):
    from meeting_api.bot_spawn.fakes import FakeRuntimeClient
    from meeting_api.bot_spawn.service import request_bot

    for name, value in {
        "BOT_AUTHENTICATED": "true",
        "BOT_USERDATA_S3_PATH": "s3://userdata/bot",
        "BOT_S3_ENDPOINT": "http://s3",
        "BOT_S3_BUCKET": "userdata",
    }.items():
        monkeypatch.setenv(name, value)
    repo = _waiting_repo(proven=True)
    repo._meetings[5]["data"]["auth_userdata_path"] = "s3://userdata/bot"
    runtime = FakeRuntimeClient()
    await request_bot(
        repo,
        runtime,
        user_id=USER,
        platform="google_meet",
        native_meeting_id="kxo-misr-avz",
        token_secret="s",
        redis_url="redis://r",
        claim_meeting_id=5,
    )
    assert len(runtime.specs) == 1
    assert "bot_retry" not in repo._meetings[5]["data"]


# ── the workload-gone proof ─────────────────────────────────────────────────────────────────


class _Log:
    def warning(self, *a, **k): ...

    def error(self, *a, **k): ...


async def _proven(runtime, workload="w-1", *, failed_s_ago=60.0, grace=600.0) -> bool:
    from meeting_api.lifecycle.reconcile import prove_workload_gone

    verdict, _why = await prove_workload_gone(
        runtime,
        workload,
        meeting_id=5,
        since=NOW - timedelta(seconds=failed_s_ago),
        now=NOW,
        untracked_grace=grace,
        log=_Log(),
    )
    return verdict == "proven"


async def test_a_workload_the_kernel_reports_terminal_is_gone():
    from meeting_api.bot_spawn.fakes import FakeRuntimeClient

    assert await _proven(FakeRuntimeClient(workloads={"w-1": {"state": "destroyed"}}))


async def test_a_live_workload_is_gone_once_its_delete_is_confirmed():
    from meeting_api.bot_spawn.fakes import FakeRuntimeClient

    runtime = FakeRuntimeClient(workloads={"w-1": {"state": "running"}})
    assert await _proven(runtime) and runtime.deleted == ["w-1"]

    class Refuses(FakeRuntimeClient):
        async def delete_workload(self, workload_id):
            raise RuntimeError("down")

    assert not await _proven(Refuses(workloads={"w-1": {"state": "running"}}))


async def test_a_404_is_gone_only_after_the_untracked_grace():
    from meeting_api.bot_spawn.fakes import FakeRuntimeClient

    runtime = FakeRuntimeClient(workloads={})
    assert not await _proven(runtime, failed_s_ago=600)
    assert await _proven(runtime, failed_s_ago=601)


async def test_an_unknown_answer_or_an_unknown_workload_is_not_proof():
    from meeting_api.bot_spawn.fakes import FakeRuntimeClient

    class Broken(FakeRuntimeClient):
        async def get_workload(self, workload_id):
            raise RuntimeError("timeout")

    assert not await _proven(Broken())
    assert not await _proven(None)
    assert not await _proven(
        FakeRuntimeClient(workloads={}), workload=None, failed_s_ago=9999
    )


class _Resp:
    def __init__(self, code: int) -> None:
        self.status_code = code
        self.text = "busy"

    def json(self):
        return {"detail": "busy"}


class _Http:
    def __init__(self, code: int) -> None:
        self.code = code

    async def post(self, url, json=None, timeout=None):
        return _Resp(self.code)


@pytest.mark.parametrize(
    "code,refused", [(400, True), (422, True), (500, False), (503, False)]
)
async def test_a_runtime_5xx_is_not_a_refusal(code, refused):
    from meeting_api.bot_spawn.adapters import HttpRuntimeClient
    from meeting_api.bot_spawn.ports import SpawnFailed

    with pytest.raises(SpawnFailed) as caught:
        await HttpRuntimeClient(_Http(code), "http://runtime").create_workload({})
    assert caught.value.refused is refused


# ── the runtime callback on a waiting meeting ───────────────────────────────────────────────


async def test_a_runtime_destroy_of_a_waiting_meetings_workload_proves_it_gone():
    from meeting_api.lifecycle.reconcile import synthesize_terminal_for_dead_workload

    repo = _waiting_repo(proven=False)
    await repo.create_session(meeting_id=5, session_uid="sess-old")
    driven: list[dict] = []

    async def drive(body):
        driven.append(body)

    assert not await synthesize_terminal_for_dead_workload(
        repo, "mtg-5-old", "destroyed", drive, log=_Log()
    )
    assert driven == []
    assert repo._meetings[5]["data"]["bot_retry"]["proven_gone"] is True
    assert repo._meetings[5]["status"] == "requested"


async def test_proving_gone_needs_the_marker_on_that_workload():
    repo = _waiting_repo(proven=False)
    assert not await repo.prove_retry_gone(meeting_id=5, workload="mtg-5-other")
    assert repo._meetings[5]["data"]["bot_retry"]["proven_gone"] is False
    assert await repo.prove_retry_gone(meeting_id=5, workload="mtg-5-old")
    del repo._meetings[5]["data"]["bot_retry"]
    assert not await repo.prove_retry_gone(meeting_id=5, workload="mtg-5-old")


# ── a waiting meeting: stop, removal, projection, reconcile ─────────────────────────────────


async def _waiting_intake():
    """A calendar meeting whose bot failed in the call and now waits for its next bot, behind the
    real ``IntakeStop``."""
    from intake_builders import entry_body, make_harness
    from meeting_api.bot_spawn.fakes import FakeRuntimeClient
    from meeting_api.intake.service import IntakeService
    from meeting_api.intake.stop import IntakeStop
    from meeting_api.lifecycle.stop_router import InMemoryCommandPublisher

    h = make_harness("2026-09-29T09:00:00Z")
    commands, runtime = InMemoryCommandPublisher(), FakeRuntimeClient()
    stop = IntakeStop(h.store, commands, runtime, publisher=h.publisher)
    service = IntakeService(
        h.store, h.spawn, stop, h.publisher, h.settings, clock=h.clock
    )
    reply = await service.put_entry(
        1, entry_body(start="2026-09-29T08:55:00Z", end="2026-09-29T09:30:00Z")
    )
    uuid = reply["meeting"]["id"]
    mid = h.meeting_id(uuid)
    h.set_status(uuid, "active")
    h.store.meetings[mid] = {**h.store.meetings[mid], "bot_container_id": "wl-old"}
    async with h.store.room_lock(1, [ROOM]) as tx:
        await retry.retry(
            tx,
            mid,
            _failed("left_alone", stage="active", workload="wl-old"),
            now=h.clock(),
            settings=h.settings,
        )
    assert h.store.meetings[mid]["status"] == "requested"
    return h, service, stop, commands, runtime, mid


async def test_a_stop_on_a_waiting_meeting_ends_it_at_once_without_a_leave():
    h, _, stop, commands, runtime, mid = await _waiting_intake()
    await stop.stop_live(1, mid, outcome=None)
    view = h.store.view(mid)
    assert (view.status, view.data["completion_reason"]) == ("failed", "stopped")
    assert view.data["failure_stage"] == "active" and not view.data.get("bot_retry")
    assert view.aw["outcome_kind"] is None
    assert h.store.events[-1].event_type == "bot.failed"
    assert h.store.events[-1].change["reason"] == "stopped"
    assert commands.published == [] and runtime.deleted == []


async def test_removing_the_last_entry_of_a_waiting_meeting_ends_it_cancelled():
    from intake_builders import A

    h, service, _, commands, runtime, mid = await _waiting_intake()
    reply = await service.remove_entry(
        1, {"external_id": "google:3n5kq8example", "user": A, "reason": "deleted"}
    )
    assert reply["result"] == "removed"
    meeting = reply["meeting"]
    assert (meeting["status"], meeting["completion_reason"]) == ("failed", "stopped")
    assert (meeting["outcome"]["kind"], meeting["outcome"]["detail"]) == (
        "cancelled_by_calendar",
        "deleted",
    )
    assert [e.event_type for e in h.store.events[-2:]] == [
        "meeting.updated",
        "bot.failed",
    ]
    assert commands.published == [] and runtime.deleted == []


async def test_removing_the_last_entry_of_a_stopped_waiting_meeting_keeps_the_outcome():
    """DELETE /bots flagged the waiting meeting first (data only); the removal still ends it now,
    with R5's ``cancelled_by_calendar``."""
    from intake_builders import A

    h, service, _, commands, _, mid = await _waiting_intake()
    row = h.store.meetings[mid]
    h.store.meetings[mid] = {**row, "data": {**row["data"], "stop_requested": True}}
    reply = await service.remove_entry(
        1, {"external_id": "google:3n5kq8example", "user": A, "reason": "cancelled"}
    )
    meeting = reply["meeting"]
    assert (reply["result"], meeting["status"]) == ("removed", "failed")
    assert (meeting["outcome"]["kind"], meeting["outcome"]["detail"]) == (
        "cancelled_by_calendar",
        "cancelled",
    )
    assert commands.published == []


async def test_a_waiting_meeting_shows_when_its_next_bot_goes():
    h, *_, mid = await _waiting_intake()
    projected = h.store.view(mid).project(lead_s=300)
    assert projected["status"] == "requested"
    assert projected["bot_joins_at"] == "2026-09-29T09:01:00Z"


async def test_the_reconcile_sweep_leaves_a_waiting_meeting_to_the_retry():
    from meeting_api.bot_spawn.fakes import FakeRuntimeClient
    from meeting_api.lifecycle.reconcile import reconcile_stale_nonterminal_sweep

    repo = _waiting_repo(proven=False)
    soon = datetime.now(UTC) + timedelta(minutes=1)
    repo._meetings[5]["data"]["bot_retry"]["due_at"] = soon.isoformat()
    await repo.create_session(meeting_id=5, session_uid="sess-old")
    assert await repo.list_stale_nonterminal(stop_grace=0, active_grace=0) == []
    posted: list[dict] = []

    async def post(body):
        posted.append(body)

    runtime = FakeRuntimeClient(workloads={"mtg-5-old": {"state": "destroyed"}})
    await reconcile_stale_nonterminal_sweep(
        repo,
        runtime,
        post,
        stop_grace=0,
        active_grace=0,
        log=_Log(),
        item_failures=InMemoryItemFailures(max_failures=5),
    )
    assert posted == [] and runtime.deleted == []
    assert repo._meetings[5]["status"] == "requested"


async def test_the_reconcile_sweep_ends_a_waiting_meeting_past_its_deadline():
    """Past ``due_at`` + ``MEETING_UNTRACKED_GRACE_SEC`` a waiting meeting ends, whatever the retry
    driver did (it may have given the item up)."""
    from meeting_api.bot_spawn.fakes import FakeRuntimeClient
    from meeting_api.lifecycle.reconcile import reconcile_stale_nonterminal_sweep

    repo = _waiting_repo(proven=False)
    late = datetime.now(UTC) - timedelta(seconds=601)
    repo._meetings[5]["data"]["bot_retry"]["due_at"] = late.isoformat()
    posted: list[dict] = []

    async def post(body):
        posted.append(body)

    await reconcile_stale_nonterminal_sweep(
        repo,
        FakeRuntimeClient(),
        post,
        stop_grace=0,
        active_grace=0,
        log=_Log(),
        untracked_grace=600,
        item_failures=InMemoryItemFailures(max_failures=5),
    )
    row = repo._meetings[5]
    assert (row["status"], row["data"]["completion_reason"]) == (
        "failed",
        "join_failure",
    )
    assert not row["data"].get("bot_retry") and posted == []
    assert "not proven gone" in row["data"]["failure_reason"]


def test_the_intake_retry_golden_is_the_projection_of_a_waiting_meeting():
    import json
    from pathlib import Path

    from meeting_api.intake.projection import project_meeting

    rel = (
        Path("meetings")
        / "contracts"
        / "intake.v1"
        / "golden"
        / "Meeting.retry-pending.json"
    )
    path = next(
        p / rel for p in Path(__file__).resolve().parents if (p / rel).is_file()
    )
    meeting = {
        "id": 11367,
        "uuid": "5f0c2b7e-8d1a-4c3e-9b6f-2a7d1e4c8b90",
        "status": "requested",
        "platform": "google_meet",
        "platform_specific_id": "kxo-misr-avz",
        "start_time": datetime(2026, 9, 29, 9, 1, 5),
        "data": {
            "title": "Weekly sync",
            "constructed_meeting_url": GMEET,
            "scheduled_at": "2026-09-29T09:00:00Z",
            "auto_join_last_attempt": "2026-09-29T08:55:00Z",
            "bot_retry": {
                "reason": "left_alone",
                "stage": None,
                "message": "stopped (workload destroyed, confirmed by runtime)",
                "after_session": "sess-1",
                "workload": "mtg-11367-5c1d2e3f",
                "at": "2026-09-29T09:12:00Z",
                "due_at": "2026-09-29T09:13:00Z",
                "proven_gone": True,
            },
        },
    }
    aw = {
        "scheduled_end_at": datetime(2026, 9, 29, 9, 30, tzinfo=UTC),
        "time_zone": "Asia/Kolkata",
        "event_seq": 6,
        "outcome_kind": None,
        "export_state": None,
    }
    entries = [
        {
            "external_id": "google:3n5kq8example",
            "source_user": "a@abroadworks.com",
            "attendees": ["a@abroadworks.com", "b@abroadworks.com", "c@client.com"],
            "series_id": None,
            "metadata": None,
            "state": "active",
        }
    ]
    assert project_meeting(meeting, aw, entries, lead_s=300) == json.loads(
        path.read_text()
    )


async def test_the_retired_session_of_a_claimed_meeting_writes_nothing():
    repo = _waiting_repo(proven=True)
    await repo.create_session(meeting_id=5, session_uid="sess-old")
    await _claim(repo)
    assert (
        await repo.update_meeting_status(
            session_uid="sess-old", status="failed", completion_reason="join_failure"
        )
        is None
    )
    assert repo._meetings[5]["status"] == "requested"


async def test_the_sweep_finishes_only_a_meeting_its_end_actually_ended():
    from meeting_api.lifecycle.reconcile import end_overdue_retries

    finished: list[int] = []

    async def finish(meeting_id: int, **kw) -> None:
        finished.append(meeting_id)

    def overdue_repo():
        repo = _waiting_repo(proven=False)
        late = datetime.now(UTC) - timedelta(seconds=601)
        repo._meetings[5]["data"]["bot_retry"]["due_at"] = late.isoformat()
        return repo

    repo = overdue_repo()
    assert (
        await end_overdue_retries(
            repo,
            untracked_grace=600,
            log=_Log(),
            finish_meeting=finish,
            failures=InMemoryItemFailures(max_failures=5),
        )
        == 1
    )
    assert finished == [5]

    raced = overdue_repo()

    async def already_ended(**kw):
        return None  # the driver or a stop ended it between the listing and the lock

    raced.end_retry = already_ended
    finished.clear()
    assert (
        await end_overdue_retries(
            raced,
            untracked_grace=600,
            log=_Log(),
            finish_meeting=finish,
            failures=InMemoryItemFailures(max_failures=5),
        )
        == 0
    )
    assert finished == []


async def test_a_failing_finish_after_a_committed_stop_does_not_fail_the_stop():
    h, _, stop, commands, _, mid = await _waiting_intake()

    async def broken_finish(meeting_id: int, **kw) -> None:
        raise RuntimeError("the database went away")

    stop.finish_meeting = broken_finish
    await stop.stop_live(
        1, mid, outcome=None
    )  # the route answers the meeting, not a 500
    assert h.store.view(mid).status == "failed"
    assert commands.published == []


def test_an_overdue_reason_never_names_a_missing_workload():
    limit = datetime(2026, 9, 29, 9, 21, tzinfo=UTC)
    due = "2026-09-29T09:11:00Z"  # + 600 s of grace: the deadline is ``limit``

    def overdue(**mark):
        return retry.overdue({"due_at": due, **mark}, 600, limit)

    code, message = overdue(workload=None, proven_gone=False)
    assert code == "workload_not_proven" and "None" not in message
    assert message == (
        "the failed bot's start recorded no workload, so none could be proven gone by "
        "2026-09-29T09:21:00Z"
    )
    code, message = overdue(workload="mtg-5-ab", proven_gone=False)
    assert message == (
        "the failed bot's workload mtg-5-ab was not proven gone by 2026-09-29T09:21:00Z"
    )
    assert overdue(workload=None, proven_gone=True) == (
        "retry_not_sent",
        "no new bot was sent by 2026-09-29T09:21:00Z",
    )
    early = limit - timedelta(seconds=1)
    assert retry.overdue({"due_at": due, "workload": "w"}, 600, early) is None
    assert (
        retry.overdue({"workload": "w"}, 600, limit) is None
    )  # no due_at: no deadline


@pytest.mark.parametrize("since", [None, "not a time"])
async def test_a_pending_teardown_without_a_readable_since_is_bounded(since):
    """A 404 waits out the grace only from a known ``since``; without one it counts, and is
    given up, rather than waiting forever."""
    from meeting_api.bot_spawn.fakes import FakeRuntimeClient
    from meeting_api.lifecycle.reconcile import retry_unproven_teardowns

    repo = _waiting_repo(proven=True, status="failed")
    pending = (
        {"workload": "mtg-5-old"}
        if since is None
        else {"workload": "mtg-5-old", "since": since}
    )
    repo._meetings[5]["data"]["unproven_teardown"] = [pending]
    failures = InMemoryItemFailures(max_failures=2)
    runtime = FakeRuntimeClient(workloads={})  # 404
    for _ in range(3):
        await retry_unproven_teardowns(
            repo, runtime, untracked_grace=600, log=_Log(), failures=failures
        )
    assert await failures.given_up("unproven-teardown", ["5:mtg-5-old"]) == {
        "5:mtg-5-old"
    }


def _teardown_items(result: str) -> float:
    from meeting_api.metrics import registry

    value = registry().get_sample_value(
        "aw_sweep_items_total", {"sweep": "unproven-teardown", "result": result}
    )
    return value or 0.0


def _pending_since(seconds_ago: float):
    repo = _waiting_repo(proven=True, status="failed")
    since = datetime.now(UTC) - timedelta(seconds=seconds_ago)
    repo._meetings[5]["data"]["unproven_teardown"] = [
        {"workload": "mtg-5-old", "since": since.isoformat()}
    ]
    return repo


class _Unreachable:
    """A runtime that doesn't answer while ``down``, then a live workload it deletes."""

    def __init__(self) -> None:
        from meeting_api.bot_spawn.fakes import FakeRuntimeClient

        self.down = True
        self.real = FakeRuntimeClient(workloads={"mtg-5-old": {"state": "running"}})

    async def get_workload(self, workload_id):
        if self.down:
            raise OSError("connection refused")
        return await self.real.get_workload(workload_id)

    async def delete_workload(self, workload_id):
        if self.down:
            raise OSError("connection refused")
        await self.real.delete_workload(workload_id)


async def test_a_runtime_outage_never_gives_a_pending_teardown_up():
    """I1: a runtime that can't be reached is not the item's failure. Two minutes of outage
    (eight passes at 15 s) count nothing, and the delete goes once the runtime answers.
    """
    from meeting_api.lifecycle.reconcile import retry_unproven_teardowns

    repo = _pending_since(30)
    failures = InMemoryItemFailures(max_failures=5)
    runtime = _Unreachable()
    before = _teardown_items("runtime_unreachable")
    for _ in range(8):
        await retry_unproven_teardowns(
            repo, runtime, untracked_grace=600, log=_Log(), failures=failures
        )
    assert failures.counts == {} and failures.gave_up == set()
    assert _teardown_items("runtime_unreachable") == before + 8
    runtime.down = False
    assert (
        await retry_unproven_teardowns(
            repo, runtime, untracked_grace=600, log=_Log(), failures=failures
        )
        == 1
    )
    assert runtime.real.deleted == ["mtg-5-old"]
    assert repo._meetings[5]["data"].get("unproven_teardown") is None


async def test_a_pending_teardown_past_its_max_age_is_given_up_logged_and_counted(
    monkeypatch, capsys
):
    """I1: the one bound on a runtime that doesn't answer is the teardown's age:
    ``UNPROVEN_TEARDOWN_MAX_AGE_S`` past its ``since`` it is given up at once, at error level
    with both ids, and counted."""
    import json

    from meeting_api.lifecycle.reconcile import retry_unproven_teardowns

    monkeypatch.setenv("UNPROVEN_TEARDOWN_MAX_AGE_S", "3600")
    repo = _pending_since(3601)
    failures = InMemoryItemFailures(max_failures=5)
    before = _teardown_items("given_up")
    await retry_unproven_teardowns(
        repo, _Unreachable(), untracked_grace=600, log=_Log(), failures=failures
    )
    assert failures.gave_up == {("unproven-teardown", "5:mtg-5-old")}
    assert _teardown_items("given_up") == before + 1
    lines = [
        json.loads(x) for x in capsys.readouterr().out.splitlines() if x.startswith("{")
    ]
    given = [x for x in lines if x["event"] == "sweep_item_given_up"]
    assert len(given) == 1 and given[0]["level"] == "error"
    assert given[0]["fields"]["item_id"] == "5:mtg-5-old"
    assert given[0]["fields"]["error"] == "UnprovenTeardownExpired"


async def test_the_max_age_defaults_to_six_hours(monkeypatch):
    from meeting_api.lifecycle.reconcile import unproven_teardown_max_age_s

    monkeypatch.delenv("UNPROVEN_TEARDOWN_MAX_AGE_S", raising=False)
    assert unproven_teardown_max_age_s() == 21600.0


async def test_a_delete_the_runtime_refuses_counts_toward_the_max_failures():
    """I1: a definite answer that isn't a proof (the runtime refuses the delete) is one of the
    item's bounded failures."""
    from meeting_api.bot_spawn.fakes import FakeRuntimeClient
    from meeting_api.bot_spawn.ports import SpawnFailed
    from meeting_api.lifecycle.reconcile import retry_unproven_teardowns

    class Refuses(FakeRuntimeClient):
        async def delete_workload(self, workload_id):
            raise SpawnFailed(
                "runtime kernel delete_workload returned 409", refused=True
            )

    repo = _pending_since(30)
    failures = InMemoryItemFailures(max_failures=2)
    runtime = Refuses(workloads={"mtg-5-old": {"state": "running"}})
    for _ in range(2):
        await retry_unproven_teardowns(
            repo, runtime, untracked_grace=600, log=_Log(), failures=failures
        )
    assert failures.gave_up == {("unproven-teardown", "5:mtg-5-old")}


class _DeleteResp:
    def __init__(self, code: int) -> None:
        self.status_code = code


class _DeleteHttp:
    def __init__(self, code: int) -> None:
        self.code = code

    async def delete(self, url, timeout=None):
        return _DeleteResp(self.code)


@pytest.mark.parametrize(
    "code,refused", [(400, True), (409, True), (500, False), (503, False)]
)
async def test_a_runtime_5xx_on_a_delete_is_not_a_refusal(code, refused):
    from meeting_api.bot_spawn.adapters import HttpRuntimeClient
    from meeting_api.bot_spawn.ports import SpawnFailed

    with pytest.raises(SpawnFailed) as caught:
        await HttpRuntimeClient(_DeleteHttp(code), "http://runtime").delete_workload(
            "w"
        )
    assert caught.value.refused is refused


# ── every read has its own give-up record (§6.9 F-I) ─────────────────────────────────────


class _BothReads:
    """One meeting listed by the backstop's two reads: waiting past its deadline, then a spawn
    that died before its session write. Ending it as a waiting meeting keeps failing."""

    def __init__(self) -> None:
        self.ended: list[int] = []

    async def list_retry_meetings(self, *, after=None, limit=None):
        mark = {"due_at": "2026-01-01T00:00:00Z", "workload": "w", "proven_gone": True}
        return (
            [] if after else [{"id": 5, "user_id": USER, "data": {"bot_retry": mark}}]
        )

    async def end_retry(self, *, meeting_id, change_reason=None, message=None):
        raise RuntimeError("the database flapped")

    async def list_unfinished_spawns(self, *, after=None, limit=None):
        plan = {"session": "sess-new", "at": "2026-01-01T00:00:00Z"}
        row = {"id": 5, "user_id": USER, "data": {"spawn_session": plan}}
        return [] if after else [{**row, "written": False, "newest_session": None}]

    async def end_unfinished_spawn(self, *, meeting_id, untracked_grace):
        self.ended.append(meeting_id)
        return "evt-1"


async def test_a_meeting_given_up_by_one_backstop_read_is_still_ended_by_the_other():
    """M2: the waiting read gives meeting 5 up; the unfinished-spawn read still ends it."""
    from meeting_api.lifecycle.reconcile import end_overdue_retries

    repo = _BothReads()
    failures = InMemoryItemFailures(max_failures=1)
    ended = await end_overdue_retries(
        repo, untracked_grace=600, log=_Log(), failures=failures
    )
    assert failures.gave_up == {("retry-overdue", "waiting:5")}
    assert repo.ended == [5] and ended == 1


async def test_a_meeting_given_up_by_the_due_read_is_still_driven_by_the_retry_read():
    """M2: the auto-join tick's due read gives meeting 5 up; its retry read still drives it."""
    from intake_builders import send_clock, sweep_intake
    from meeting_api.bot_spawn.auto_join import auto_join_tick
    from meeting_api.bot_spawn.fakes import FakeRuntimeClient

    repo = _waiting_repo(proven=False)
    scheduled = {"title": "standup", "scheduled_at": NOW.isoformat()}
    due_row = {
        **repo._meetings[5],
        "status": "scheduled",
        "has_entries": False,
        "data": scheduled,
    }

    async def due(now, lead_s, *, after=None, limit=None):
        return [] if after else [{**due_row, "event_time": NOW}]

    proofs: list[int] = []

    async def prove_retry_gone(*, meeting_id, workload):
        proofs.append(meeting_id)
        return False

    async def merge(meeting_id, patch):
        raise RuntimeError("poison row")  # the due read's first write

    repo.list_due_meetings = due
    repo.prove_retry_gone = prove_retry_gone
    repo.merge_meeting_data = merge
    failures = InMemoryItemFailures(max_failures=1)
    with send_clock(NOW):
        await auto_join_tick(
            repo,
            FakeRuntimeClient(workloads={"mtg-5-old": {"state": "destroyed"}}),
            **sweep_intake(
                transcribe_gate=lambda: None,
                now=NOW + timedelta(minutes=5),
                token_secret="s",
                redis_url="redis://r",
                allow_uncapped=True,
                item_failures=failures,
            ),
        )
    assert failures.gave_up == {("auto-join", "due:5")}
    assert proofs == [5]


# ── the upstream reconcile loops: paged, each row bounded (§6.9 F-I) ──────────────────────


def _reconcile_items(sweep: str, result: str) -> float:
    from meeting_api.metrics import registry

    value = registry().get_sample_value(
        "aw_sweep_items_total", {"sweep": sweep, "result": result}
    )
    return value or 0.0


class _PagedStopping:
    """A repo whose stale ``stopping`` rows are read a page at a time by meeting id."""

    def __init__(self, rows) -> None:
        self.rows = sorted(rows)
        self.reads: list[tuple] = []

    async def list_stale_stopping(self, *, older_than_seconds, after=None, limit=None):
        self.reads.append((after, limit))
        rows = [r for r in self.rows if after is None or r[0] > after]
        return rows if limit is None else rows[:limit]


class _DeleteDown:
    """A runtime whose every delete gets a 5xx; its workloads report ``state``."""

    def __init__(self, state: str = "running") -> None:
        self.state = state
        self.deleted: list[str] = []

    async def get_workload(self, workload_id):
        return {"workloadId": workload_id, "state": self.state}

    async def delete_workload(self, workload_id):
        from meeting_api.bot_spawn.ports import SpawnFailed

        self.deleted.append(workload_id)
        raise SpawnFailed("runtime kernel delete_workload returned 503", refused=False)


async def _ok(body):
    return 200


async def test_the_stale_stopping_sweep_reads_its_rows_in_pages(monkeypatch):
    from meeting_api.bot_spawn.fakes import FakeRuntimeClient
    from meeting_api.lifecycle.reconcile import reconcile_stale_stopping_sweep

    monkeypatch.setenv("SWEEP_BATCH_SIZE", "2")
    repo = _PagedStopping([(i, f"sess-{i}", f"mtg-{i}-w") for i in (3, 5, 8)])
    runtime = FakeRuntimeClient()
    n = await reconcile_stale_stopping_sweep(
        repo,
        runtime,
        _ok,
        stop_grace=45,
        log=_Log(),
        item_failures=InMemoryItemFailures(max_failures=5),
    )
    assert n == 3 and runtime.deleted == ["mtg-3-w", "mtg-5-w", "mtg-8-w"]
    assert repo.reads == [(None, 2), (5, 2)]


async def test_a_stopping_row_whose_delete_keeps_failing_is_given_up_and_counted():
    """I2: a stuck ``stopping`` row whose delete keeps failing is one of the sweep's bounded
    items: counted each pass, given up after ``SWEEP_MAX_ITEM_FAILURES``, skipped after.
    """
    from meeting_api.lifecycle.reconcile import reconcile_stale_stopping_sweep

    repo = _PagedStopping([(9, "sess-9", "mtg-9-x")])
    runtime = _DeleteDown()
    failures = InMemoryItemFailures(max_failures=2)
    failed = _reconcile_items("stale-stopping", "failed")
    given_up = _reconcile_items("stale-stopping", "given_up")
    for _ in range(3):
        assert (
            await reconcile_stale_stopping_sweep(
                repo,
                runtime,
                _ok,
                stop_grace=45,
                log=_Log(),
                item_failures=failures,
            )
            == 0
        )
    assert runtime.deleted == ["mtg-9-x", "mtg-9-x"]  # the third pass skips it
    assert failures.gave_up == {("stale-stopping", "9")}
    assert _reconcile_items("stale-stopping", "failed") == failed + 1
    assert _reconcile_items("stale-stopping", "given_up") == given_up + 1


async def test_a_stopping_row_the_runtime_404s_is_left_to_the_general_escalation():
    """A 404 is not the stopping sweep's failure: the general sweep's bounded untracked
    escalation owns it, so it is never given up here."""
    from meeting_api.bot_spawn.fakes import FakeRuntimeClient
    from meeting_api.lifecycle.reconcile import reconcile_stale_stopping_sweep

    repo = _PagedStopping([(1, "sess-1", "mtg-1-w")])
    failures = InMemoryItemFailures(max_failures=1)
    for _ in range(2):
        await reconcile_stale_stopping_sweep(
            repo,
            FakeRuntimeClient(workloads={}),
            _ok,
            stop_grace=45,
            log=_Log(),
            item_failures=failures,
        )
    assert failures.counts == {}


def _stale_repo(n: int = 1):
    from meeting_api.bot_spawn.fakes import InMemoryMeetingRepo

    repo = InMemoryMeetingRepo()
    for i in range(1, n + 1):
        repo._meetings[i] = {
            "id": i,
            "user_id": USER,
            "platform": "google_meet",
            "native_meeting_id": f"kxo-misr-av{i}",
            "platform_specific_id": f"kxo-misr-av{i}",
            "status": "stopping",
            "bot_container_id": f"mtg-{i}-w",
            "data": {},
            "updated_at": "2026-09-01T09:00:00Z",
        }
        repo.sessions.append({"meeting_id": i, "session_uid": f"sess-{i}"})
    return repo


async def test_the_general_reconcile_sweep_reads_its_rows_in_pages(monkeypatch):
    from meeting_api.bot_spawn.fakes import FakeRuntimeClient
    from meeting_api.lifecycle.reconcile import reconcile_stale_nonterminal_sweep

    monkeypatch.setenv("SWEEP_BATCH_SIZE", "2")
    repo = _stale_repo(3)
    reads: list[tuple] = []
    real = repo.list_stale_nonterminal

    async def paged(**kw):
        reads.append((kw.get("after"), kw.get("limit")))
        return await real(**kw)

    repo.list_stale_nonterminal = paged
    posted: list[dict] = []

    async def post(body):
        posted.append(body)
        return 200

    runtime = FakeRuntimeClient()
    await reconcile_stale_nonterminal_sweep(
        repo,
        runtime,
        post,
        stop_grace=45,
        active_grace=300,
        log=_Log(),
        item_failures=InMemoryItemFailures(max_failures=5),
    )
    assert [b["connection_id"] for b in posted] == ["sess-1", "sess-2", "sess-3"]
    assert reads == [(None, 2), (2, 2)]


async def test_a_stale_row_whose_delete_keeps_failing_is_given_up_and_counted():
    """I2: the general sweep's row whose delete keeps failing is counted each pass and given
    up after ``SWEEP_MAX_ITEM_FAILURES``, under its own sweep name."""
    from meeting_api.lifecycle.reconcile import reconcile_stale_nonterminal_sweep

    repo = _stale_repo()
    runtime = _DeleteDown()
    failures = InMemoryItemFailures(max_failures=2)
    given_up = _reconcile_items("stale-nonterminal", "given_up")
    for _ in range(3):
        await reconcile_stale_nonterminal_sweep(
            repo,
            runtime,
            _ok,
            stop_grace=45,
            active_grace=300,
            log=_Log(),
            item_failures=failures,
        )
    assert runtime.deleted == ["mtg-1-w", "mtg-1-w"]
    assert failures.gave_up == {("stale-nonterminal", "1")}
    assert _reconcile_items("stale-nonterminal", "given_up") == given_up + 1
    assert repo._meetings[1]["status"] == "stopping"


async def test_the_sweep_reads_the_waiting_meetings_in_pages(monkeypatch):
    import copy

    from meeting_api.lifecycle.reconcile import end_overdue_retries

    monkeypatch.setenv("SWEEP_BATCH_SIZE", "1")
    repo = _waiting_repo(proven=False)
    late = datetime.now(UTC) - timedelta(seconds=601)
    repo._meetings[5]["data"]["bot_retry"]["due_at"] = late.isoformat()
    repo._meetings[6] = copy.deepcopy({**repo._meetings[5], "id": 6})
    reads: list[tuple] = []
    real = repo.list_retry_meetings

    async def paged(**kw):
        reads.append((kw.get("after"), kw.get("limit")))
        return await real(**kw)

    repo.list_retry_meetings = paged
    assert (
        await end_overdue_retries(
            repo,
            untracked_grace=600,
            log=_Log(),
            failures=InMemoryItemFailures(max_failures=5),
        )
        == 2
    )
    assert reads[0] == (None, 1) and all(limit == 1 for _, limit in reads)
    assert [repo._meetings[i]["status"] for i in (5, 6)] == ["failed", "failed"]


async def test_a_waiting_meeting_the_sweep_cannot_end_is_given_up():
    """§6.9 F-I: the backstop's item that keeps failing is counted and given up under its own
    sweep name, never retried forever; the retry driver's give-ups don't reach it."""
    from meeting_api.lifecycle.reconcile import OVERDUE_RETRY_SWEEP, end_overdue_retries

    repo = _waiting_repo(proven=False)
    late = datetime.now(UTC) - timedelta(seconds=601)
    repo._meetings[5]["data"]["bot_retry"]["due_at"] = late.isoformat()
    tries: list[int] = []

    async def broken(**kw):
        tries.append(kw["meeting_id"])
        raise RuntimeError("the database went away")

    repo.end_retry = broken
    failures = InMemoryItemFailures(max_failures=2)
    await failures.failed("auto-join", "retry:5", RuntimeError("the driver gave it up"))
    await failures.failed("auto-join", "retry:5", RuntimeError("the driver gave it up"))
    for _ in range(3):
        await end_overdue_retries(
            repo, untracked_grace=600, log=_Log(), failures=failures
        )
    assert tries == [5, 5]
    assert await failures.given_up(OVERDUE_RETRY_SWEEP, ["waiting:5"]) == {"waiting:5"}


# ── a session with no recorded workload: the sweeps reach the workload it was sent as ─────────


class _QuietLog:
    def __getattr__(self, name):
        return lambda *a, **k: None


def _containerless(status: str):
    """A stale row with a session and no ``bot_container_id`` (a cancel between the session
    write and the container write); returns the repo, the meeting id and that workload's id.
    """
    from meeting_api.bot_spawn.fakes import InMemoryMeetingRepo
    from meeting_api.bot_spawn.ports import workload_id_for

    repo = InMemoryMeetingRepo()
    repo._meetings[5] = {
        "id": 5,
        "user_id": USER,
        "platform": "google_meet",
        "native_meeting_id": "kxo-misr-avz",
        "platform_specific_id": "kxo-misr-avz",
        "status": status,
        "bot_container_id": None,
        "start_time": None,
        "end_time": None,
        "created_at": "2026-09-01T09:00:00Z",
        "updated_at": "2026-09-01T09:00:00Z",
        "data": {},
    }
    repo.sessions.append({"meeting_id": 5, "session_uid": "sess-abcdef12-9"})
    return repo, 5, workload_id_for(5, "sess-abcdef12-9")


async def _general_sweep(repo, runtime):
    from meeting_api.lifecycle.reconcile import reconcile_stale_nonterminal_sweep

    posted: list[dict] = []

    async def post(body):
        posted.append(body)

    await reconcile_stale_nonterminal_sweep(
        repo,
        runtime,
        post,
        stop_grace=0,
        active_grace=0,
        log=_QuietLog(),
        untracked_grace=600,
        untracked_since={},
        item_failures=InMemoryItemFailures(max_failures=5),
    )
    return posted


async def test_a_containerless_session_whose_workload_is_gone_is_deleted_then_reaped():
    from meeting_api.bot_spawn.fakes import FakeRuntimeClient

    repo, _, workload = _containerless("joining")
    runtime = FakeRuntimeClient(workloads={workload: {"state": "destroyed"}})
    posted = await _general_sweep(repo, runtime)
    assert runtime.deleted == [workload]
    assert [p["status"] for p in posted] == ["failed"]


async def test_a_containerless_session_whose_workload_runs_is_not_reaped():
    from meeting_api.bot_spawn.fakes import FakeRuntimeClient

    repo, _, workload = _containerless("joining")
    runtime = FakeRuntimeClient(workloads={workload: {"state": "running"}})
    assert await _general_sweep(repo, runtime) == []
    assert runtime.deleted == []


async def test_a_containerless_session_the_runtime_never_heard_of_waits_for_the_grace():
    from meeting_api.bot_spawn.fakes import FakeRuntimeClient

    repo, _, _ = _containerless("joining")
    assert await _general_sweep(repo, FakeRuntimeClient(workloads={})) == []


async def test_a_containerless_stopping_row_has_its_workload_deleted_before_it_completes():
    from meeting_api.bot_spawn.fakes import FakeRuntimeClient
    from meeting_api.lifecycle.reconcile import reconcile_stale_stopping_sweep

    repo, mid, workload = _containerless("stopping")

    async def stale(**kw):
        return [(mid, "sess-abcdef12-9", None)]

    repo.list_stale_stopping = stale
    runtime = FakeRuntimeClient(workloads={workload: {"state": "running"}})
    posted: list[dict] = []

    async def post(body):
        posted.append(body)

    await reconcile_stale_stopping_sweep(
        repo,
        runtime,
        post,
        stop_grace=0,
        log=_QuietLog(),
        item_failures=InMemoryItemFailures(max_failures=5),
    )
    assert runtime.deleted == [workload]
    assert [p["status"] for p in posted] == ["completed"]


def test_an_unfinished_spawn_names_its_workload_past_its_deadline():
    at = datetime(2026, 9, 29, 9, 21, tzinfo=UTC)  # the plan + 600 s
    plan = {"session": "new-sess-1", "at": "2026-09-29T09:11:00Z"}
    claimed = {
        "spawn_session": plan,
        "completion_history": [
            {"completion_reason": "left_alone", "after_session": "old"}
        ],
    }

    def ending(data, *, written=False, newest="old", now=at):
        return retry.unfinished_spawn(
            5,
            data,
            written=written,
            newest_session=newest,
            untracked_grace=600,
            now=now,
        )

    code, patch = ending(claimed)
    assert code == "retry_not_sent" and patch["completion_reason"] == "left_alone"
    assert patch["failure_reason"] == (
        "no new bot was sent by 2026-09-29T09:21:00Z; its workload mtg-5-new-sess may still run"
    )
    assert [t["workload"] for t in patch["unproven_teardown"]] == ["mtg-5-new-sess"]
    code, patch = ending({"spawn_session": plan}, newest=None)
    assert (code, patch["completion_reason"]) == ("start_failed", "start_failed")
    assert ending(claimed, now=at - timedelta(seconds=1)) is None
    assert ending(claimed, written=True) is None  # its session was written
    assert ending({}) is None  # no planned session
