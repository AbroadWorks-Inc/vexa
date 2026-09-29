"""The ``webhook.v1`` goldens are the real emitters' output (§1.8, §2.7).

Subscription deliveries (``MeetingEvent.*``, ``TestEvent.*``, ``SignatureHeaders.subscription`` and
``SignatureHeaders.rotated``): the envelopes are the outbox rows the status writer
(``intake.status.write_status`` / ``write_event``), the retry writer (``intake.retry.retry``, §6.9
F-K2) and the test writer (``intake.outbox.PostgresWebhookTests.queue_test``) store, run over an
in-memory session at a fixed instant; the headers are ``webhooks.signing.signed_headers`` over the
stored body of ``MeetingEvent.meeting-completed``. A golden re-serialised compactly with sorted keys
is exactly the stored body, so the headers verify against it.

Legacy deliveries (``Envelope.meeting-completed`` and ``Envelope.bot-failed``): what the lifecycle
callback sends the legacy system and per-user URLs: ``lifecycle.webhook.build_typed_envelope``
around ``app.legacy_meeting_projection`` of the meeting row, upstream's meeting block.

Each test builds its payload from fixed inputs and compares it with the golden file, so an emitter
change shows up here. The goldens are never edited by hand: to regenerate them after an intended
change, run this module with ``WEBHOOK_V1_GOLDENS_WRITE=1``.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from referencing import Registry, Resource

from meeting_api.app import legacy_meeting_projection
from meeting_api.bot_spawn.adapters import _with_projection
from meeting_api.intake.projection import project_meeting
from meeting_api.lifecycle.machine import LifecycleSink, MeetingStore, TransitionSource
from meeting_api.lifecycle.webhook import build_typed_envelope

UUID = "5f0c2b7e-8d1a-4c3e-9b6f-2a7d1e4c8b90"


def _golden_path(name: str) -> Path:
    rel = Path("meetings") / "contracts" / "webhook.v1" / "golden"
    for parent in Path(__file__).resolve().parents:
        if (parent / rel).is_dir():
            return parent / rel / f"{name}.json"
    raise FileNotFoundError(rel)


def _change(connection_id: str, *events: dict[str, Any]) -> Any:
    sink = LifecycleSink(store=MeetingStore())
    change = None
    for event in events:
        change = sink.apply_change(
            {"connection_id": connection_id, **event},
            transition_source=TransitionSource.BOT_CALLBACK,
        )
    return change


def _row(meeting: dict[str, Any], aw: dict[str, Any], entries: list[dict]) -> dict:
    """The row as ``SqlAlchemyMeetingRepo.update_meeting_status`` returns it."""
    stored = {
        **meeting,
        "uuid": UUID,
        "platform_specific_id": meeting["native_meeting_id"],
    }
    return _with_projection(meeting, project_meeting(stored, aw, entries, lead_s=300))


def _completed() -> dict:
    change = _change(
        "sess-golden-completed",
        {"status": "joining", "timestamp": "2026-06-18T09:59:40.000Z"},
        {"status": "active", "timestamp": "2026-06-18T10:00:00.000Z"},
        {
            "status": "completed",
            "completion_reason": "stopped",
            "timestamp": "2026-06-18T10:42:00.000Z",
        },
    )
    row = _row(
        {
            "id": 11367,
            "user_id": 7,
            "platform": "google_meet",
            "native_meeting_id": "abc-defg-hij",
            "constructed_meeting_url": "https://meet.google.com/abc-defg-hij",
            "status": "completed",
            "start_time": "2026-06-18T10:00:00.000Z",
            "end_time": "2026-06-18T10:42:00.000Z",
            "data": {
                "name": "Weekly sync",
                "completion_reason": "stopped",
                "service_provenance": {
                    "bot_admitted_at": "2026-06-18T10:00:00.000Z",
                    "bot_departed_at": "2026-06-18T10:42:00.000Z",
                    "bot_outcome": "served",
                    "transcription_provider": "customer",
                    "transcription_outcome": "served",
                    "lifecycle_contract_version": "2026-07-28",
                },
            },
            "created_at": "2026-06-18T09:59:30.000Z",
            "updated_at": "2026-06-18T10:42:00.000Z",
        },
        {"event_seq": 9, "outcome_kind": None},
        [
            {
                "external_id": "google:3n5kq8example",
                "source_user": "a@abroadworks.com",
                "attendees": ["a@abroadworks.com", "b@example.com"],
                "series_id": "google:series-weekly",
                "metadata": {"crm_id": "42"},
                "state": "closed",
            }
        ],
    )
    envelope = build_typed_envelope(
        change,
        meeting=legacy_meeting_projection(row),
        created_at="2026-06-18T10:42:00.000Z",
    )
    assert envelope is not None
    return envelope


def _bot_failed() -> dict:
    change = _change(
        "sess-golden-failed",
        {"status": "joining", "timestamp": "2026-06-18T10:02:00.000Z"},
        {"status": "awaiting_admission", "timestamp": "2026-06-18T10:02:30.000Z"},
        {
            "status": "failed",
            "failure_stage": "awaiting_admission",
            "completion_reason": "awaiting_admission_rejected",
            "reason": "host denied admission",
            "timestamp": "2026-06-18T10:03:12.000Z",
        },
    )
    row = _row(
        {
            "id": 11368,
            "user_id": 7,
            "platform": "zoom",
            "native_meeting_id": "98765432101",
            "constructed_meeting_url": "https://zoom.us/j/98765432101",
            "status": "failed",
            "start_time": None,
            "end_time": "2026-06-18T10:03:12.000Z",
            "data": {
                "completion_reason": "awaiting_admission_rejected",
                "failure_stage": "awaiting_admission",
            },
            "created_at": "2026-06-18T10:02:00.000Z",
            "updated_at": "2026-06-18T10:03:12.000Z",
        },
        {"event_seq": 4, "outcome_kind": None},
        [],
    )
    envelope = build_typed_envelope(
        change,
        meeting=legacy_meeting_projection(row),
        created_at="2026-06-18T10:03:12.000Z",
    )
    assert envelope is not None
    return envelope


@pytest.mark.parametrize(
    "name,build",
    [("Envelope.meeting-completed", _completed), ("Envelope.bot-failed", _bot_failed)],
)
def test_the_golden_is_the_builders_output(name, build):
    built = build()
    path = _golden_path(name)
    if os.getenv("WEBHOOK_V1_GOLDENS_WRITE") == "1":
        path.write_text(json.dumps(built, indent=2) + "\n")
    assert json.loads(path.read_text()) == built


# Upstream's meeting block (the parent's ``_build_meeting_event_data``): the §2.4 meeting's own
# keys are the subscription deliveries', never the legacy URLs'.
LEGACY_MEETING_KEYS = [
    "id",
    "user_id",
    "platform",
    "native_meeting_id",
    "constructed_meeting_url",
    "status",
    "completion_reason",
    "failure_stage",
    "service_provenance",
    "start_time",
    "end_time",
    "data",
    "created_at",
    "updated_at",
]


@pytest.mark.parametrize("build", [_completed, _bot_failed])
def test_the_legacy_meeting_block_is_upstreams(build):
    assert list(build()["data"]["meeting"]) == LEGACY_MEETING_KEYS


# ── subscription deliveries (§2.7) ───────────────────────────────────────────────────────────

NOW = datetime(2026, 9, 29, 5, 12, 41, 250000, tzinfo=timezone.utc)
MEETING_ID = 11367
SUBSCRIPTION_ID = "2d9f6c1e-4b7a-4e3d-8c5f-1a2b3c4d5e6f"
TEST_NONCE = uuid.UUID("0c4d8e2f-6a1b-4c3d-9e8f-7a6b5c4d3e2f")
SECRET = "whsec_demo_secret"
PREVIOUS_SECRET = "whsec_demo_previous_secret"
TIMESTAMP = 1790658761
# The lifecycle write's time stamps (naive UTC): the bot went active, the meeting finished.
ACTIVE_AT = datetime(2026, 9, 29, 4, 26, 11)
ENDED_AT = datetime(2026, 9, 29, 5, 12, 41)


@pytest.fixture
def orm():
    pytest.importorskip("sqlalchemy", reason="the ORM models need SQLAlchemy")
    from meeting_api.sessions import models

    return models


class _Rows:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def scalars(self) -> "_Rows":
        return self

    def all(self) -> list:
        return list(self._rows)

    def scalar_one_or_none(self) -> Any:
        return self._rows[0] if self._rows else None


class _Session:
    """The part of an ``AsyncSession`` the status writer and the test writer use: one meeting, its
    aw-state row and entries, one owned subscription, and every row added."""

    def __init__(
        self, orm, *, meeting=None, aw=None, entries=(), subscription=None
    ) -> None:
        self._orm = orm
        self._rows = {orm.Meeting: meeting, orm.MeetingAwState: aw}
        self._entries = list(entries)
        self._subscription = subscription
        self.added: list = []
        self.info: dict = (
            {}
        )  # AsyncSession.info: the status writer's after-commit actions

    async def __aenter__(self) -> "_Session":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def get(self, cls, ident, **_: Any) -> Any:
        return self._rows.get(cls)

    async def execute(self, stmt) -> _Rows:
        entity = stmt.column_descriptions[0]["entity"]
        if entity is self._orm.MeetingEntry:
            return _Rows(self._entries)
        if entity in (self._orm.Meeting, self._orm.MeetingAwState):
            row = self._rows.get(entity)
            return _Rows([] if row is None else [row])
        if entity is self._orm.WebhookSubscription:
            return _Rows([] if self._subscription is None else [self._subscription])
        raise AssertionError(f"unexpected statement: {stmt}")

    def add(self, obj) -> None:
        self.added.append(obj)
        if isinstance(obj, self._orm.MeetingAwState):
            self._rows[self._orm.MeetingAwState] = obj

    async def flush(self) -> None:
        pass

    async def commit(self) -> None:
        pass

    def body(self) -> str:
        """The one outbox row's ``payload_text``: the exact body a subscriber receives."""
        (row,) = [o for o in self.added if isinstance(o, self._orm.WebhookOutbox)]
        return row.payload_text


def _meeting(
    orm,
    status: str,
    *,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    **data: Any,
):
    """The meeting row; ``start_time``/``end_time`` as the lifecycle write stamps them (naive
    UTC): when the bot went ``active``, and when the meeting finished."""
    return orm.Meeting(
        id=MEETING_ID,
        uuid=uuid.UUID(UUID),
        user_id=7,
        platform="google_meet",
        platform_specific_id="abc-defg-hij",
        status=status,
        data={
            "title": "Weekly sync",
            "constructed_meeting_url": "https://meet.google.com/abc-defg-hij",
            "scheduled_at": "2026-09-29T04:30:00Z",
            **data,
        },
        start_time=start_time,
        end_time=end_time,
        created_at=datetime(2026, 9, 29, 4, 20, 5),
    )


def _aw(orm, event_seq: int):
    return orm.MeetingAwState(
        meeting_id=MEETING_ID,
        event_seq=event_seq,
        scheduled_end_at=datetime(2026, 9, 29, 5, 0, tzinfo=timezone.utc),
        time_zone="Asia/Kolkata",
    )


def _entries(orm) -> list:
    return [
        orm.MeetingEntry(
            id=1,
            user_id=7,
            source_user="a@abroadworks.com",
            external_id="google:3n5kq8example",
            meeting_id=MEETING_ID,
            meeting_url="https://meet.google.com/abc-defg-hij",
            platform="google_meet",
            native_meeting_id="abc-defg-hij",
            start_at=datetime(2026, 9, 29, 4, 30, tzinfo=timezone.utc),
            end_at=datetime(2026, 9, 29, 5, 0, tzinfo=timezone.utc),
            series_id="google:series-weekly",
            attendees=["a@abroadworks.com", "b@example.com"],
            metadata_={"crm_id": "42"},
            content_hash="h" * 64,
            state="active",
            title="Weekly sync",
            time_zone="Asia/Kolkata",
            join_now=False,
            removed_reason=None,
        )
    ]


@pytest.fixture
def fixed(monkeypatch):
    from meeting_api.intake import outbox, status

    monkeypatch.setattr(status, "_now", lambda: NOW)
    monkeypatch.setattr(outbox.uuid, "uuid4", lambda: TEST_NONCE)
    monkeypatch.delenv("AUTO_JOIN_LEAD_S", raising=False)


async def _subscription_completed(orm) -> str:
    from meeting_api.intake.status import write_status

    db = _Session(
        orm,
        meeting=_meeting(
            orm,
            "stopping",
            start_time=ACTIVE_AT,
            end_time=ENDED_AT,
            auto_join_last_attempt="2026-09-29T04:25:00Z",
        ),
        aw=_aw(orm, 8),
        entries=_entries(orm),
    )
    await write_status(
        db,
        MEETING_ID,
        "completed",
        expected_from={"stopping"},
        data_patch={"completion_reason": "stopped"},
        change_reason="stopped",
    )
    return db.body()


async def _subscription_bot_failed(orm) -> str:
    from meeting_api.intake.status import write_status

    db = _Session(
        orm,
        meeting=_meeting(
            orm,
            "awaiting_admission",
            end_time=ENDED_AT,
            auto_join_last_attempt="2026-09-29T04:25:00Z",
        ),
        aw=_aw(orm, 3),
        entries=_entries(orm),
    )
    await write_status(
        db,
        MEETING_ID,
        "failed",
        expected_from={"awaiting_admission"},
        data_patch={
            "completion_reason": "awaiting_admission_rejected",
            "failure_stage": "awaiting_admission",
        },
        change_reason="host denied admission",
    )
    return db.body()


async def _subscription_bot_retry(orm) -> str:
    from meeting_api.intake import retry
    from meeting_api.intake.adapters import PostgresIntakeTx
    from meeting_api.intake.settings import IntakeSettings

    aw = _aw(orm, 5)
    aw.scheduled_end_at = datetime(2026, 9, 29, 6, 0, tzinfo=timezone.utc)
    aw.send_attempts = 0
    db = _Session(
        orm,
        meeting=_meeting(
            orm,
            "active",
            start_time=ACTIVE_AT,
            auto_join_last_attempt="2026-09-29T04:25:00Z",
        ),
        aw=aw,
        entries=_entries(orm),
    )
    written = await retry.retry(
        PostgresIntakeTx(db),  # type: ignore[arg-type]
        MEETING_ID,
        retry.Failure(
            "completed",
            "left_alone",
            "stopped (workload destroyed, confirmed by runtime)",
            lost=True,
            session="sess-golden-retry",
            workload="mtg-11367-5c1d2e3f",
            proven_gone=True,
        ),
        now=NOW.replace(microsecond=0),
        settings=IntakeSettings(
            max_days_ahead=30,
            join_now_adopt_ahead_s=3600,
            lead_s=300,
            blocked_hosts=frozenset(),
            max_active_entries=100_000,
            send_max_attempts=3,
            send_retry_backoff_s=60,
            conflict_retries=3,
        ),
    )
    assert written is not None
    return db.body()


async def _subscription_updated(orm) -> str:
    from meeting_api.intake.status import write_event

    db = _Session(
        orm, meeting=_meeting(orm, "scheduled"), aw=_aw(orm, 1), entries=_entries(orm)
    )
    await write_event(db, MEETING_ID, "meeting.updated")
    return db.body()


async def _subscription_test(orm) -> str:
    from meeting_api.intake.outbox import PostgresWebhookTests

    db = _Session(orm, subscription=uuid.UUID(SUBSCRIPTION_ID))
    await PostgresWebhookTests(lambda: db, clock=lambda: NOW).queue_test(
        7, SUBSCRIPTION_ID
    )
    return db.body()


SUBSCRIPTION_GOLDENS = [
    ("MeetingEvent.meeting-completed", _subscription_completed),
    ("MeetingEvent.bot-failed", _subscription_bot_failed),
    ("MeetingEvent.bot-retry", _subscription_bot_retry),
    ("MeetingEvent.meeting-updated", _subscription_updated),
    ("TestEvent.webhook-test", _subscription_test),
]


def _check(name: str, built: Any) -> None:
    path = _golden_path(name)
    if os.getenv("WEBHOOK_V1_GOLDENS_WRITE") == "1":
        path.write_text(json.dumps(built, indent=2) + "\n")
    assert json.loads(path.read_text()) == built


def _compact(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), sort_keys=True)


@pytest.mark.parametrize("name,build", SUBSCRIPTION_GOLDENS)
async def test_the_subscription_golden_is_the_stored_body(orm, fixed, name, build):
    body = await build(orm)
    _check(name, json.loads(body))
    assert _compact(json.loads(_golden_path(name).read_text())) == body


def _signed(previous: bool) -> dict[str, str]:
    from meeting_api.webhooks.signing import signed_headers

    body = _compact(
        json.loads(_golden_path("MeetingEvent.meeting-completed").read_text())
    )
    return signed_headers(
        body.encode("utf-8"),
        secret=SECRET,
        timestamp=TIMESTAMP,
        previous_secret=PREVIOUS_SECRET if previous else None,
    )


@pytest.mark.parametrize(
    "name,previous",
    [("SignatureHeaders.subscription", False), ("SignatureHeaders.rotated", True)],
)
def test_the_subscription_header_golden_is_the_signers_output(name, previous):
    _check(name, _signed(previous))


def test_the_subscription_headers_verify_and_carry_no_authorization():
    from meeting_api.webhooks.delivery import sign_payload, verify_signature

    body = _compact(
        json.loads(_golden_path("MeetingEvent.meeting-completed").read_text())
    ).encode("utf-8")
    rotated = json.loads(_golden_path("SignatureHeaders.rotated").read_text())
    assert "Authorization" not in rotated
    assert verify_signature(body, rotated, SECRET)
    assert rotated["X-Webhook-Signature-Previous"] == sign_payload(
        body, PREVIOUS_SECRET, rotated["X-Webhook-Timestamp"]
    )


def _contracts() -> Path:
    return _golden_path("MeetingEvent.meeting-completed").parents[2]


def _validator(shape: str) -> jsonschema.Draft202012Validator:
    registry = Registry()
    for rel in ("webhook.v1/webhook.schema.json", "intake.v1/intake.schema.json"):
        schema = json.loads((_contracts() / rel).read_text())
        registry = registry.with_resource(schema["$id"], Resource.from_contents(schema))
    ref = (
        "https://vexa.ai/schemas/intake.v1#/$defs/Meeting"
        if shape == "Meeting"
        else f"https://vexa.ai/schemas/webhook.v1#/$defs/{shape}"
    )
    return jsonschema.Draft202012Validator({"$ref": ref}, registry=registry)


@pytest.mark.parametrize("name", [n for n, _ in SUBSCRIPTION_GOLDENS])
def test_the_subscription_golden_conforms(name):
    golden = json.loads(_golden_path(name).read_text())
    _validator(name.split(".")[0]).validate(golden)
    if name.startswith("MeetingEvent."):
        _validator("Meeting").validate(golden["data"]["meeting"])


def test_the_subscription_event_is_one_typed_event_with_the_change_and_the_v2_id():
    from meeting_api.intake.status import derive_event_id_v2

    completed = json.loads(_golden_path("MeetingEvent.meeting-completed").read_text())

    assert completed["event_type"] == "meeting.completed"
    assert completed["event_id"] == derive_event_id_v2(UUID, "meeting.completed", 9)
    assert completed["data"]["change"] == {
        "from": "stopping",
        "to": "completed",
        "reason": "stopped",
        "at": "2026-09-29T05:12:41Z",
    }
    assert completed["data"]["meeting"]["id"] == UUID
    assert completed["data"]["meeting"]["sequence"] == 9
    assert "status_change" not in completed["data"]


def test_the_retry_event_sends_the_meeting_back_to_requested_with_the_reason():
    retried = json.loads(_golden_path("MeetingEvent.bot-retry").read_text())
    assert retried["event_type"] == "bot.retry"
    assert retried["data"]["change"] == {
        "from": "active",
        "to": "requested",
        "reason": "left_alone",
        "at": "2026-09-29T05:12:41Z",
    }
    meeting = retried["data"]["meeting"]
    assert (meeting["status"], meeting["completion_reason"], meeting["outcome"]) == (
        "requested",
        None,
        None,
    )
    assert meeting["bot_joins_at"] == "2026-09-29T05:13:41Z"


def test_the_legacy_goldens_do_not_satisfy_the_subscription_shape():
    """The two producers differ on the wire: a legacy envelope is not a subscription event."""
    legacy = json.loads(_golden_path("Envelope.meeting-completed").read_text())
    assert not _validator("MeetingEvent").is_valid(legacy)
