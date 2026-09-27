"""§1.13 erasure — upstream's completed-artifact deletion as one callable, and ``DELETE /v2/meetings/{id}``.

``collector.app.delete_completed_artifacts(store, deleter, user_id, meeting_id)`` is the terminal
branch of upstream's ``DELETE /meetings/{id}``: storage first, then the transcript rows, the
meeting row kept. Both routes call it.

``DELETE /v2/meetings/{id}`` (scope ``erase``) works on finished meetings only. It runs that
callable, then removes the meeting's delivery rows, outbox rows and entries in one transaction;
the meeting row and ``meeting_aw_state`` stay and no event is written. The last test runs it on
the real Postgres store (skipped unless ``MEETING_API_TEST_DATABASE_URL`` is set).
"""

from __future__ import annotations

import inspect
import os
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException

from intake_builders import A, conforms, entry_body, http, intake_app, make_harness

from meeting_api.collector import app as collector_app
from meeting_api.collector.app import delete_completed_artifacts
from meeting_api.intake.fakes import InMemoryIntakeReads
from meeting_api.collector.fakes import InMemoryTranscriptStore
from meeting_api.recordings.deletion import delete_recording_objects
from meeting_api.recordings.fakes import InMemoryStorage

OWNER = 7
OTHER = 8
MEETING_ID = 41
RECORDING_ID = 9001
PREFIX = f"recordings/{OWNER}/{RECORDING_ID}/sess-41/audio/"


def _recording() -> dict:
    return {
        "id": RECORDING_ID,
        "meeting_id": MEETING_ID,
        "user_id": OWNER,
        "session_uid": "sess-41",
        "status": "completed",
        "media_files": [
            {
                "id": 22,
                "type": "audio",
                "format": "wav",
                "storage_path": f"{PREFIX}master.wav",
            }
        ],
    }


def _artifact_store(status: str = "completed") -> InMemoryTranscriptStore:
    store = InMemoryTranscriptStore()
    store.seed_meeting(
        meeting_id=MEETING_ID,
        user_id=OWNER,
        platform="google_meet",
        native_meeting_id="private-room",
        status=status,
        data={"recordings": [_recording()], "notes": "derived summary"},
        segments=[
            {
                "segment_id": "s1",
                "start": 0,
                "end": 1,
                "text": "confidential",
                "language": "en",
            }
        ],
    )
    return store


def _storage() -> InMemoryStorage:
    storage = InMemoryStorage()
    storage.blobs[f"{PREFIX}000000.wav"] = b"chunk"
    storage.blobs[f"{PREFIX}master.wav"] = b"master"
    return storage


def _deleter(storage: InMemoryStorage):
    async def delete(recording: dict) -> list[str]:
        return await delete_recording_objects(storage, recording)

    return delete


# ── the callable ─────────────────────────────────────────────────────────────────────────────


async def test_the_callable_deletes_objects_then_transcripts_and_keeps_the_row():
    store, storage = _artifact_store(), _storage()

    result = await delete_completed_artifacts(
        store, _deleter(storage), OWNER, MEETING_ID
    )

    assert result == {
        "kind": "artifacts",
        "objects_deleted": 2,
        "already_deleted": False,
    }
    assert storage.blobs == {}
    meeting = store._meetings[MEETING_ID]
    assert meeting["status"] == "completed"
    assert meeting["segments"] == {}
    assert "recordings" not in meeting["data"]


async def test_the_callable_refuses_a_live_meeting_and_another_account():
    live, storage = _artifact_store("active"), _storage()
    with pytest.raises(HTTPException) as conflict:
        await delete_completed_artifacts(live, _deleter(storage), OWNER, MEETING_ID)
    assert conflict.value.status_code == 409

    with pytest.raises(HTTPException) as missing:
        await delete_completed_artifacts(
            _artifact_store(), _deleter(storage), OTHER, MEETING_ID
        )
    assert missing.value.status_code == 404
    assert len(storage.blobs) == 2


async def test_the_callable_needs_a_deleter_when_there_are_recordings():
    store = _artifact_store()
    with pytest.raises(HTTPException) as unavailable:
        await delete_completed_artifacts(store, None, OWNER, MEETING_ID)
    assert unavailable.value.status_code == 503
    assert store._meetings[MEETING_ID]["segments"]["s1"]["text"] == "confidential"


# ── DELETE /v2/meetings/{id} ─────────────────────────────────────────────────────────────────

ACCOUNT = {"x-user-id": "1"}


class _World:
    """A harness meeting with one entry, a transcript row and a recording in the transcript store
    under the same meeting id, two recording objects in storage, and delivery rows for its events.
    """

    def __init__(self, *, storage_cls: type = InMemoryStorage):
        self.h = make_harness()
        self.reads = InMemoryIntakeReads(self.h.store)
        self.transcripts = InMemoryTranscriptStore()
        self.storage = storage_cls()
        self.deleted: list[dict] = []

    async def setup(self, status: str = "completed") -> str:
        reply = await self.h.put(attendees=[A, "b@abroadworks.com"])
        uuid = reply["meeting"]["id"]
        for step in {
            "scheduled": [],
            "active": ["requested", "active"],
            "completed": ["requested", "active", "completed"],
            "failed": ["failed"],
        }[status]:
            self.h.set_status(uuid, step)
        self.mid = self.h.meeting_id(uuid)
        prefix = f"recordings/1/{RECORDING_ID}/sess/audio/"
        self.transcripts.seed_meeting(
            meeting_id=self.mid,
            user_id=1,
            platform="google_meet",
            native_meeting_id="kxo-misr-avz",
            status=status,
            data={
                "recordings": [
                    {
                        "id": RECORDING_ID,
                        "meeting_id": self.mid,
                        "user_id": 1,
                        "session_uid": "sess",
                        "status": "completed",
                        "media_files": [
                            {
                                "id": 1,
                                "type": "audio",
                                "format": "wav",
                                "storage_path": f"{prefix}master.wav",
                            }
                        ],
                    }
                ]
            },
            segments=[
                {
                    "segment_id": "s1",
                    "start": 0,
                    "end": 1,
                    "text": "kept",
                    "language": "en",
                }
            ],
        )
        self.storage.blobs[f"{prefix}000000.wav"] = b"chunk"
        self.storage.blobs[f"{prefix}master.wav"] = b"master"
        for event in self.h.store.events:
            if event.meeting_id == self.mid:
                self.reads.deliveries[event.event_id] = 2
        return uuid

    async def deleter(self, recording: dict) -> list[str]:
        self.deleted.append(recording)
        return await delete_recording_objects(self.storage, recording)

    def client(self):
        return http(
            intake_app(
                self.h.service,
                self.reads,
                self.h.stop,
                artifact_store=self.transcripts,
                artifact_deleter=self.deleter,
            )
        )

    def events(self) -> int:
        return sum(1 for e in self.h.store.events if e.meeting_id == self.mid)


def _error(response: Any, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    conforms(response.json(), "Error")
    assert response.json()["error"]["code"] == code


async def test_erase_a_finished_meeting_removes_its_data_and_keeps_the_row():
    world = _World()
    uuid = await world.setup("completed")
    events_before = world.events()
    aw_before = dict(world.h.store.aw[world.mid])

    async with world.client() as client:
        r = await client.delete(f"/v2/meetings/{uuid}", headers=ACCOUNT)

    assert r.status_code == 200, r.text
    body = r.json()
    conforms(body, "Erased")
    assert body["deleted"] == {
        "objects": 2,
        "entries": 1,
        "outbox": events_before,
        "deliveries": 2 * events_before,
    }
    assert body["meeting"]["id"] == uuid
    assert body["meeting"]["status"] == "completed"
    assert body["meeting"]["entries"] == []
    assert world.storage.blobs == {}
    assert world.transcripts._meetings[world.mid]["segments"] == {}
    assert world.h.store.meetings[world.mid]["status"] == "completed"
    assert world.h.store.aw[world.mid] == aw_before, "meeting_aw_state stays, no event"
    assert world.events() == 0
    assert world.h.store.entries_of(world.mid, "closed") == []
    assert world.reads.deliveries == {}
    assert [rec["id"] for rec in world.deleted] == [RECORDING_ID]


@pytest.mark.parametrize("status", ["scheduled", "active"])
async def test_erase_an_unfinished_meeting_is_409_and_touches_nothing(status):
    world = _World()
    uuid = await world.setup(status)
    events_before = world.events()
    async with world.client() as client:
        r = await client.delete(f"/v2/meetings/{uuid}", headers=ACCOUNT)
    _error(r, 409, "meeting_not_finished")
    assert len(world.storage.blobs) == 2
    assert world.deleted == []
    assert world.events() == events_before
    assert world.h.store.entries_of(world.mid) != []


async def test_erase_after_a_failed_start_works_too():
    world = _World()
    uuid = await world.setup("failed")
    async with world.client() as client:
        r = await client.delete(f"/v2/meetings/{uuid}", headers=ACCOUNT)
    assert r.status_code == 200, r.text
    conforms(r.json(), "Erased")


async def test_a_storage_failure_aborts_before_any_row_is_removed():
    class FailsOnce(InMemoryStorage):
        def __init__(self) -> None:
            super().__init__()
            self.fail = True

        async def delete(self, key: str) -> None:
            if self.fail:
                self.fail = False
                raise RuntimeError("injected object-store failure")
            await super().delete(key)

    world = _World(storage_cls=FailsOnce)
    uuid = await world.setup("completed")
    events_before = world.events()
    async with world.client() as client:
        first = await client.delete(f"/v2/meetings/{uuid}", headers=ACCOUNT)
        _error(first, 503, "unavailable")
        assert world.events() == events_before
        assert world.h.store.entries_of(world.mid, "closed") != []
        assert world.reads.deliveries != {}
        assert (
            world.transcripts._meetings[world.mid]["segments"]["s1"]["text"] == "kept"
        )
        assert world.transcripts._meetings[world.mid]["data"]["recordings"]

        retry = await client.delete(f"/v2/meetings/{uuid}", headers=ACCOUNT)
    assert retry.status_code == 200, retry.text
    assert world.events() == 0


async def test_a_bug_in_the_deleter_is_a_500_not_unavailable():
    """A programming error is not a storage outage: it stays a 500 (and nothing is removed)."""

    class Broken(InMemoryStorage):
        async def delete(self, key: str) -> None:
            raise TypeError("delete() got an unexpected keyword argument")

    world = _World(storage_cls=Broken)
    uuid = await world.setup("completed")
    events_before = world.events()
    async with world.client() as client:
        r = await client.delete(f"/v2/meetings/{uuid}", headers=ACCOUNT)
    assert r.status_code == 500
    assert world.events() == events_before
    assert world.h.store.entries_of(world.mid, "closed") != []


async def test_erase_another_accounts_or_an_unknown_meeting_is_404():
    world = _World()
    uuid = await world.setup("completed")
    async with world.client() as client:
        other = await client.delete(f"/v2/meetings/{uuid}", headers={"x-user-id": "2"})
        unknown = await client.delete("/v2/meetings/not-a-uuid", headers=ACCOUNT)
    _error(other, 404, "meeting_not_found")
    _error(unknown, 404, "meeting_not_found")
    assert len(world.storage.blobs) == 2


def test_erasure_never_reaches_the_exporter_or_the_notetaker_bucket():
    """§1.13: nothing in aw-bots deletes from aw-chatworks-transcribe, and erasure calls neither the
    exporter nor the notetaker. The erase path is the router, the reads and upstream's callable;
    its only object deletes go through the injected deleter (upstream's recording deleter).
    """
    intake = Path(collector_app.__file__).resolve().parents[1] / "intake"
    sources = {
        "router.py": (intake / "router.py").read_text(),
        "reads.py": (intake / "reads.py").read_text(),
        "delete_completed_artifacts": inspect.getsource(delete_completed_artifacts),
    }
    for name, text in sources.items():
        lowered = text.lower()
        for needle in ("aw-chatworks-transcribe", "exporter", "notetaker", "/process"):
            assert needle not in lowered, f"{name} mentions {needle!r}"
        assert "delete_object" not in text and "s3" not in lowered, name


# ── real Postgres ───────────────────────────────────────────────────────────────────────────


@pytest.mark.skipif(
    not os.getenv("MEETING_API_TEST_DATABASE_URL"),
    reason="real-Postgres erasure proof; set MEETING_API_TEST_DATABASE_URL to run",
)
async def test_pg_erase_deletes_deliveries_attempts_outbox_and_entries_only(
    intake_pg_engine, monkeypatch
):
    pytest.importorskip("sqlalchemy", reason="see test_intake_pg_schema.py's docstring")
    pytest.importorskip("asyncpg", reason="see test_intake_pg_schema.py's docstring")
    import json

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from admin_api.schema import models as admin_models
    from admin_api.schema import sync as admin_sync
    from intake_builders import FakeClock, make_settings, ts
    from meeting_api.collector.adapters import SqlAlchemyTranscriptStore
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
    storage = _storage()

    async def deleter(recording: dict) -> list[str]:
        return await delete_recording_objects(storage, recording)

    async def rows(sql: str, **params: Any) -> Any:
        async with intake_pg_engine.connect() as conn:
            return (await conn.execute(text(sql), params)).scalar()

    async with http(
        intake_app(
            service,
            PostgresIntakeReads(factory),
            _Unused(),
            artifact_store=SqlAlchemyTranscriptStore(factory),
            artifact_deleter=deleter,
        )
    ) as client:
        done = (
            await client.put("/v2/entries", json=entry_body("done"), headers=ACCOUNT)
        ).json()
        kept = (
            await client.put(
                "/v2/entries",
                json=entry_body(
                    "kept",
                    meeting_url="https://meet.google.com/abc-defg-hij",
                ),
                headers=ACCOUNT,
            )
        ).json()
        done_id = await rows(
            "SELECT id FROM meetings WHERE uuid = CAST(:u AS uuid)",
            u=done["meeting"]["id"],
        )
        kept_id = await rows(
            "SELECT id FROM meetings WHERE uuid = CAST(:u AS uuid)",
            u=kept["meeting"]["id"],
        )
        async with factory() as db, db.begin():
            for step, before in (
                ("requested", "scheduled"),
                ("active", "requested"),
                ("completed", "active"),
            ):
                await write_status(db, done_id, step, expected_from={before})
            recording = _recording() | {"meeting_id": done_id}
            await db.execute(
                text(
                    "UPDATE meetings SET data = data || CAST(:d AS jsonb) WHERE id = :id"
                ),
                {"d": json.dumps({"recordings": [recording]}), "id": done_id},
            )
            await db.execute(
                text(
                    "INSERT INTO transcriptions (meeting_id, start_time, end_time, text) "
                    "VALUES (:id, 0, 1, 'confidential')"
                ),
                {"id": done_id},
            )
            await db.execute(
                text(
                    "INSERT INTO webhook_deliveries "
                    "(event_id, subscription_id, user_id, state, next_attempt_at) "
                    "SELECT event_id, gen_random_uuid(), 1, 'delivered', now() "
                    "FROM webhook_outbox"
                )
            )
            await db.execute(
                text(
                    "INSERT INTO webhook_delivery_attempts (delivery_id, attempt, outcome) "
                    "SELECT id, 1, 'delivered' FROM webhook_deliveries"
                )
            )
        outbox_done = await rows(
            "SELECT count(*) FROM webhook_outbox WHERE meeting_id = :id", id=done_id
        )
        outbox_kept = await rows(
            "SELECT count(*) FROM webhook_outbox WHERE meeting_id = :id", id=kept_id
        )
        seq_before = await rows(
            "SELECT event_seq FROM meeting_aw_state WHERE meeting_id = :id", id=done_id
        )

        scheduled = await client.delete(
            f"/v2/meetings/{kept['meeting']['id']}", headers=ACCOUNT
        )
        r = await client.delete(
            f"/v2/meetings/{done['meeting']['id']}", headers=ACCOUNT
        )

    _error(scheduled, 409, "meeting_not_finished")
    assert r.status_code == 200, r.text
    conforms(r.json(), "Erased")
    assert r.json()["deleted"] == {
        "objects": 2,
        "entries": 1,
        "outbox": outbox_done,
        "deliveries": outbox_done,
    }
    assert storage.blobs == {}
    assert await rows("SELECT status FROM meetings WHERE id = :id", id=done_id) == (
        "completed"
    )
    assert (
        await rows(
            "SELECT event_seq FROM meeting_aw_state WHERE meeting_id = :id", id=done_id
        )
        == seq_before
    ), "meeting_aw_state stays and no event is written"
    assert (
        await rows(
            "SELECT count(*) FROM transcriptions WHERE meeting_id = :id", id=done_id
        )
        == 0
    )
    for table, where in (
        ("meeting_entries", "meeting_id = :id"),
        ("webhook_outbox", "meeting_id = :id"),
    ):
        assert (
            await rows(f"SELECT count(*) FROM {table} WHERE {where}", id=done_id) == 0
        )
        assert await rows(f"SELECT count(*) FROM {table} WHERE {where}", id=kept_id) > 0
    assert await rows("SELECT count(*) FROM webhook_deliveries") == outbox_kept
    assert await rows("SELECT count(*) FROM webhook_delivery_attempts") == outbox_kept
