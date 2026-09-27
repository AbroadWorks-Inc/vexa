"""§1.9 — ``POST /v2/meetings/{id}/export``: the exporter reports its result.

Body ``{"state": "handed_off" | "failed", "s3_path": str, "error"?: str}``. Only a finished meeting
of the caller's account takes a result (``meeting_not_found`` otherwise, ``meeting_not_finished``
for a scheduled or live one). The result is stored on ``meeting_aw_state.export_*`` and emitted as
``export.handed_off`` / ``export.failed``; the same state and path again changes nothing and emits
nothing. The reply is the meeting, whose ``export`` field shows the result. The route is scoped
``export`` in ``core/meetings/routes.v1.json`` (the gateway checks it). The last test runs on the
real Postgres store (skipped unless ``MEETING_API_TEST_DATABASE_URL`` is set).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from intake_builders import A, conforms, entry_body, http, intake_app, make_harness

from meeting_api.intake.fakes import InMemoryIntakeReads

ACCOUNT = {"x-user-id": "1"}
S3_PATH = "s3://aw-chatworks-transcribe/recordings/google_meet_kxo-misr-avz_x/"
HANDED_OFF = {"state": "handed_off", "s3_path": S3_PATH}
FAILED = {"state": "failed", "s3_path": S3_PATH, "error": "notetaker 422"}


class _World:
    def __init__(self) -> None:
        self.h = make_harness()
        self.reads = InMemoryIntakeReads(self.h.store)

    async def setup(self, status: str = "completed") -> str:
        reply = await self.h.put(attendees=[A])
        uuid = reply["meeting"]["id"]
        for step in {
            "scheduled": [],
            "active": ["requested", "active"],
            "completed": ["requested", "active", "completed"],
            "failed": ["failed"],
        }[status]:
            self.h.set_status(uuid, step)
        self.mid = self.h.meeting_id(uuid)
        return uuid

    def client(self):
        return http(intake_app(self.h.service, self.reads, self.h.stop))

    def export_events(self) -> list[Any]:
        return [
            e
            for e in self.h.store.events
            if e.meeting_id == self.mid and e.event_type.startswith("export.")
        ]

    def aw(self) -> dict[str, Any]:
        return dict(self.h.store.aw[self.mid])


def _error(response: Any, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    conforms(response.json(), "Error")
    assert response.json()["error"]["code"] == code


# ── scope ────────────────────────────────────────────────────────────────────────────────────


def _manifest_row(method: str, path: str) -> dict[str, Any]:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "meetings" / "routes.v1.json"
        if candidate.is_file():
            rows = json.loads(candidate.read_text())["routes"]
            return next(r for r in rows if (r["method"], r["path"]) == (method, path))
    raise FileNotFoundError("core/meetings/routes.v1.json")


def test_the_route_is_scoped_export_and_nothing_else():
    row = _manifest_row("POST", "/v2/meetings/{meeting_id}/export")
    assert row["scopes"] == ["export"]


async def test_without_an_identity_the_route_is_unauthorized():
    world = _World()
    uuid = await world.setup()
    async with world.client() as client:
        r = await client.post(f"/v2/meetings/{uuid}/export", json=HANDED_OFF)
    _error(r, 401, "unauthorized")
    assert world.export_events() == []


# ── the result is stored and emitted ─────────────────────────────────────────────────────────


async def test_a_handed_off_result_is_stored_and_emitted():
    world = _World()
    uuid = await world.setup("completed")
    seq_before = world.aw()["event_seq"]

    async with world.client() as client:
        r = await client.post(
            f"/v2/meetings/{uuid}/export", json=HANDED_OFF, headers=ACCOUNT
        )

    assert r.status_code == 200, r.text
    body = r.json()
    conforms(body, "Meeting")
    assert body["id"] == uuid
    assert body["export"] == {
        "state": "handed_off",
        "s3_path": S3_PATH,
        "error": None,
        "at": "2026-09-26T12:00:00Z",
    }
    aw = world.aw()
    assert (aw["export_state"], aw["export_s3_path"], aw["export_error"]) == (
        "handed_off",
        S3_PATH,
        None,
    )
    assert aw["event_seq"] == seq_before + 1
    [event] = world.export_events()
    assert event.event_type == "export.handed_off"
    assert event.meeting["export"] == body["export"]
    assert event.sequence == body["sequence"]


async def test_a_failed_result_carries_its_error():
    world = _World()
    uuid = await world.setup("failed")

    async with world.client() as client:
        r = await client.post(
            f"/v2/meetings/{uuid}/export", json=FAILED, headers=ACCOUNT
        )

    assert r.status_code == 200, r.text
    assert r.json()["export"]["state"] == "failed"
    assert r.json()["export"]["error"] == "notetaker 422"
    [event] = world.export_events()
    assert event.event_type == "export.failed"


async def test_the_same_state_and_path_again_emits_nothing():
    world = _World()
    uuid = await world.setup("completed")

    async with world.client() as client:
        first = await client.post(
            f"/v2/meetings/{uuid}/export", json=HANDED_OFF, headers=ACCOUNT
        )
        aw_after_first = world.aw()
        world.h.clock.advance(minutes=5)
        again = await client.post(
            f"/v2/meetings/{uuid}/export", json=HANDED_OFF, headers=ACCOUNT
        )

    assert again.status_code == 200, again.text
    assert again.json() == first.json()
    assert world.aw() == aw_after_first
    assert len(world.export_events()) == 1


async def test_a_changed_result_is_stored_and_emitted_again():
    world = _World()
    uuid = await world.setup("completed")

    async with world.client() as client:
        await client.post(
            f"/v2/meetings/{uuid}/export", json=HANDED_OFF, headers=ACCOUNT
        )
        world.h.clock.advance(minutes=5)
        r = await client.post(
            f"/v2/meetings/{uuid}/export", json=FAILED, headers=ACCOUNT
        )

    assert r.status_code == 200, r.text
    assert r.json()["export"] == {
        "state": "failed",
        "s3_path": S3_PATH,
        "error": "notetaker 422",
        "at": "2026-09-26T12:05:00Z",
    }
    assert [e.event_type for e in world.export_events()] == [
        "export.handed_off",
        "export.failed",
    ]


# ── account isolation and state ──────────────────────────────────────────────────────────────


async def test_another_accounts_or_an_unknown_meeting_is_404():
    world = _World()
    uuid = await world.setup("completed")
    aw_before = world.aw()
    async with world.client() as client:
        other = await client.post(
            f"/v2/meetings/{uuid}/export", json=HANDED_OFF, headers={"x-user-id": "2"}
        )
        unknown = await client.post(
            "/v2/meetings/5f0c2b7e-8d1a-4c3e-9b6f-2a7d1e4c8b90/export",
            json=HANDED_OFF,
            headers=ACCOUNT,
        )
        garbage = await client.post(
            "/v2/meetings/not-a-uuid/export", json=HANDED_OFF, headers=ACCOUNT
        )
    _error(other, 404, "meeting_not_found")
    _error(unknown, 404, "meeting_not_found")
    _error(garbage, 404, "meeting_not_found")
    assert world.aw() == aw_before
    assert world.export_events() == []


@pytest.mark.parametrize("status", ["scheduled", "active"])
async def test_an_unfinished_meeting_is_409_and_nothing_is_stored(status):
    world = _World()
    uuid = await world.setup(status)
    aw_before = world.aw()
    async with world.client() as client:
        r = await client.post(
            f"/v2/meetings/{uuid}/export", json=HANDED_OFF, headers=ACCOUNT
        )
    _error(r, 409, "meeting_not_finished")
    assert world.aw() == aw_before
    assert world.export_events() == []


# ── validation ───────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"s3_path": S3_PATH},
        {"state": "done", "s3_path": S3_PATH},
        {"state": "handed_off"},
        {"state": "handed_off", "s3_path": ""},
        {"state": "handed_off", "s3_path": 7},
        {"state": "handed_off", "s3_path": "s3://b/" + "x" * 1025},
        {"state": "failed", "s3_path": S3_PATH, "error": "x" * 2001},
        {"state": "handed_off", "s3_path": S3_PATH, "extra": "no"},
        ["handed_off"],
    ],
)
async def test_an_invalid_body_is_400_and_nothing_is_stored(body):
    world = _World()
    uuid = await world.setup("completed")
    async with world.client() as client:
        r = await client.post(f"/v2/meetings/{uuid}/export", json=body, headers=ACCOUNT)
    _error(r, 400, "invalid_request")
    assert "x" * 50 not in r.json()["error"]["message"]
    assert world.export_events() == []
    assert world.aw().get("export_state") is None


async def test_a_body_that_is_not_json_is_400():
    world = _World()
    uuid = await world.setup("completed")
    async with world.client() as client:
        r = await client.post(
            f"/v2/meetings/{uuid}/export",
            content=b"not json",
            headers={**ACCOUNT, "content-type": "application/json"},
        )
    _error(r, 400, "invalid_request")


# ── real Postgres ───────────────────────────────────────────────────────────────────────────


@pytest.mark.skipif(
    not os.getenv("MEETING_API_TEST_DATABASE_URL"),
    reason="real-Postgres export proof; set MEETING_API_TEST_DATABASE_URL to run",
)
async def test_pg_export_result_is_stored_once_with_one_outbox_event(
    intake_pg_engine, monkeypatch
):
    pytest.importorskip("sqlalchemy", reason="see test_intake_pg_schema.py's docstring")
    pytest.importorskip("asyncpg", reason="see test_intake_pg_schema.py's docstring")
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from admin_api.schema import models as admin_models
    from admin_api.schema import sync as admin_sync
    from intake_builders import FakeClock, make_settings, ts
    from meeting_api.intake import PostgresIntakeStore
    from meeting_api.intake import status as status_mod
    from meeting_api.intake.fakes import FakePublisher
    from meeting_api.intake.reads import PostgresIntakeReads
    from meeting_api.intake.service import IntakeService
    from meeting_api.intake.status import write_status

    await admin_sync.ensure_schema(intake_pg_engine, admin_models.Base)
    monkeypatch.setenv("AUTO_JOIN_LEAD_S", "300")
    clock = FakeClock(ts("2026-09-26T12:00:00Z"))
    monkeypatch.setattr(status_mod, "_now", clock)
    factory = async_sessionmaker(intake_pg_engine, expire_on_commit=False)

    class _Unused:
        async def spawn_exact(self, *a: Any) -> Any:
            raise AssertionError("never reached")

        async def stop_live(self, *a: Any, **k: Any) -> None:
            raise AssertionError("never reached")

    service = IntakeService(
        PostgresIntakeStore(factory),
        _Unused(),
        _Unused(),
        FakePublisher(),
        make_settings(),
        clock=clock,
    )

    async def one(sql: str, **params: Any) -> Any:
        async with intake_pg_engine.connect() as conn:
            return (await conn.execute(text(sql), params)).first()

    async with http(
        intake_app(service, PostgresIntakeReads(factory), _Unused())
    ) as client:
        done = (
            await client.put("/v2/entries", json=entry_body("done"), headers=ACCOUNT)
        ).json()["meeting"]["id"]
        live = (
            await client.put(
                "/v2/entries",
                json=entry_body(
                    "live", meeting_url="https://meet.google.com/abc-defg-hij"
                ),
                headers=ACCOUNT,
            )
        ).json()["meeting"]["id"]
        (done_id,) = await one(
            "SELECT id FROM meetings WHERE uuid = CAST(:u AS uuid)", u=done
        )
        async with factory() as db, db.begin():
            for step, before in (
                ("requested", "scheduled"),
                ("active", "requested"),
                ("completed", "active"),
            ):
                await write_status(db, done_id, step, expected_from={before})
        (seq_before,) = await one(
            "SELECT event_seq FROM meeting_aw_state WHERE meeting_id = :id", id=done_id
        )

        unfinished = await client.post(
            f"/v2/meetings/{live}/export", json=HANDED_OFF, headers=ACCOUNT
        )
        foreign = await client.post(
            f"/v2/meetings/{done}/export", json=HANDED_OFF, headers={"x-user-id": "2"}
        )
        first = await client.post(
            f"/v2/meetings/{done}/export", json=HANDED_OFF, headers=ACCOUNT
        )
        again = await client.post(
            f"/v2/meetings/{done}/export", json=HANDED_OFF, headers=ACCOUNT
        )
        read = await client.get(f"/v2/meetings/{done}", headers=ACCOUNT)

    _error(unfinished, 409, "meeting_not_finished")
    _error(foreign, 404, "meeting_not_found")
    assert first.status_code == 200, first.text
    conforms(first.json(), "Meeting")
    assert again.json() == first.json()
    exported = first.json()["export"]
    assert read.json()["export"] == exported
    assert (exported["state"], exported["s3_path"], exported["error"]) == (
        "handed_off",
        S3_PATH,
        None,
    )
    assert exported["at"] is not None
    state, path, error, at, seq = await one(
        "SELECT export_state, export_s3_path, export_error, export_at, event_seq "
        "FROM meeting_aw_state WHERE meeting_id = :id",
        id=done_id,
    )
    assert (state, path, error) == ("handed_off", S3_PATH, None)
    assert at is not None
    assert seq == seq_before + 1
    (count,) = await one(
        "SELECT count(*) FROM webhook_outbox "
        "WHERE meeting_id = :id AND event_type = 'export.handed_off'",
        id=done_id,
    )
    assert count == 1
    (payload,) = await one(
        "SELECT payload_text FROM webhook_outbox "
        "WHERE meeting_id = :id AND event_type = 'export.handed_off'",
        id=done_id,
    )
    envelope = json.loads(payload)
    assert envelope["data"]["meeting"]["export"] == first.json()["export"]
    assert envelope["data"]["meeting"]["sequence"] == seq
