"""§1.6 — entry-managed meetings are edited only through ``/v2/entries``.

A meeting with at least one ``meeting_entries`` row takes its plan from its entries: a direct edit
through an upstream route would be undone by the next recomputation, and a hard delete would
orphan the entries (``ON DELETE RESTRICT``). So upstream ``PATCH``/``DELETE /meetings/{id}``, their
native forms and ``PUT …/intent`` answer 409 with ``managed_by_entries`` as upstream's ``detail``.
Reads, ``annotate`` and ``share`` stay open. The planned-meeting store itself refuses them under
the row lock (Ruling R21), so upstream calendar sync skips such a row instead of editing it.

Drives the SHIPPED collector app over the in-memory fake (a row seeded with ``has_entries``), then
the SQLAlchemy store against real Postgres with a real ``meeting_entries`` row (skips cleanly
unless ``MEETING_API_TEST_DATABASE_URL`` is set).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from intake_builders import seed_link_row
from meeting_api.calendar_sync import parse_ics, sync_user
from meeting_api.collector import create_app
from meeting_api.collector.fakes import InMemoryTranscriptStore
from meeting_api.intake.resolver import ManagedByEntries

USER = 7
H = {"x-user-id": str(USER)}
PLAT, NID = "google_meet", "kxo-misr-avz"
MANAGED = {"detail": "managed_by_entries"}


class _CaptureRedis:
    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []

    async def publish(self, channel, data):
        self.published.append((channel, data))


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _client(*, status: str = "scheduled", managed: bool = True, user_id: int = USER):
    store = InMemoryTranscriptStore()
    when = datetime.now(timezone.utc) + timedelta(days=1)
    mid = store.seed_meeting(
        user_id=user_id,
        platform=PLAT,
        native_meeting_id=NID,
        status=status,
        start_time=None if status == "scheduled" else _iso(when - timedelta(days=3)),
        data=(
            {"scheduled_at": _iso(when), "title": "Weekly"}
            if status == "scheduled"
            else {}
        ),
        has_entries=managed,
    )
    redis: Any = _CaptureRedis()
    return TestClient(create_app(store, redis=redis)), store, mid, redis


EDITS = [
    ("PATCH", "/meetings/{mid}", {"title": "renamed"}),
    ("DELETE", "/meetings/{mid}", None),
    ("PATCH", f"/meetings/{PLAT}/{NID}", {"title": "renamed"}),
    ("DELETE", f"/meetings/{PLAT}/{NID}", None),
    ("PUT", f"/meetings/{PLAT}/{NID}/intent", {"intent": "idle"}),
]


@pytest.mark.parametrize("method,path,body", EDITS)
def test_upstream_edits_of_an_entry_managed_meeting_answer_409(method, path, body):
    client, store, mid, redis = _client()
    before = (store._meetings[mid]["status"], dict(store._meetings[mid]["data"]))
    r = client.request(method, path.format(mid=mid), headers=H, json=body)
    assert r.status_code == 409, r.text
    assert r.json() == MANAGED
    assert (
        mid in store._meetings
    ), "an entry-managed meeting is never hard-deleted upstream"
    assert (store._meetings[mid]["status"], store._meetings[mid]["data"]) == before
    assert redis.published == []


@pytest.mark.parametrize("method,path,body", EDITS)
def test_the_same_edits_of_an_entry_less_meeting_keep_upstream_behaviour(
    method, path, body
):
    client, store, mid, _redis = _client(managed=False)
    r = client.request(method, path.format(mid=mid), headers=H, json=body)
    assert r.status_code in (200, 204), r.text
    if method == "DELETE":
        assert mid not in store._meetings
    elif method == "PATCH":
        assert store._meetings[mid]["data"]["title"] == "renamed"
    else:
        assert store._meetings[mid]["status"] == "idle"


def test_deleting_a_finished_entry_managed_meeting_answers_409_too():
    """The upstream DELETE of a finished meeting erases its artifacts; for an entry-managed
    meeting that is ``/v2``'s erasure, never the upstream route."""
    client, store, mid, _redis = _client(status="completed")
    for path in (f"/meetings/{mid}", f"/meetings/{PLAT}/{NID}"):
        r = client.delete(path, headers=H)
        assert r.status_code == 409, r.text
        assert r.json() == MANAGED
    assert "artifact_deletion" not in store._meetings[mid]["data"]


def test_another_users_entry_managed_meeting_is_still_404():
    """The refusal never tells a non-owner the meeting exists."""
    client, _store, mid, _redis = _client(user_id=USER + 1)
    assert (
        client.patch(f"/meetings/{mid}", headers=H, json={"title": "x"}).status_code
        == 404
    )
    assert client.delete(f"/meetings/{mid}", headers=H).status_code == 404


def test_reads_annotate_and_share_stay_open_on_an_entry_managed_meeting():
    client, store, mid, _redis = _client(status="completed")
    assert client.get(f"/transcripts/{PLAT}/{NID}", headers=H).status_code == 200
    assert (
        client.get(f"/meetings/{PLAT}/{NID}/participants", headers=H).status_code == 200
    )
    r = client.post(f"/meetings/{mid}/annotate", headers=H, json={"title": "Acme"})
    assert r.status_code == 200, r.text
    r = client.post(
        f"/meetings/{PLAT}/{NID}/annotate", headers=H, json={"metadata": {"k": 1}}
    )
    assert r.status_code == 200, r.text
    assert store._meetings[mid]["data"]["title"] == "Acme"
    r = client.post(f"/meetings/{mid}/share", headers=H, json={})
    assert r.status_code == 200, r.text
    r = client.post(f"/meetings/{PLAT}/{NID}/share", headers=H, json={})
    assert r.status_code == 200, r.text
    assert r.json()["token"].startswith(f"{mid}.")


# ── real Postgres ────────────────────────────────────────────────────────────────────────────


async def test_pg_store_knows_which_meetings_entries_manage(link_pg_engine):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from meeting_api.collector.adapters import SqlAlchemyTranscriptStore

    managed = await seed_link_row(
        link_pg_engine,
        "scheduled",
        managed=True,
        scheduled_at=_iso(datetime.now(timezone.utc) + timedelta(days=1)),
    )
    free = await seed_link_row(link_pg_engine, "completed", native="abc-defg-hij")
    theirs = await seed_link_row(
        link_pg_engine,
        "scheduled",
        managed=True,
        user_id=USER + 1,
        native="xyz-abcd-efg",
    )
    store = SqlAlchemyTranscriptStore(
        async_sessionmaker(link_pg_engine, expire_on_commit=False)
    )
    assert await store.entry_managed(USER, managed) is True
    assert await store.entry_managed(USER, free) is False
    assert await store.entry_managed(USER, theirs) is False
    assert await store.entry_managed(USER, 999_999) is False


async def test_pg_set_intent_refuses_an_entry_managed_meeting(link_pg_engine):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from meeting_api.collector.adapters import SqlAlchemyTranscriptStore
    from meeting_api.intake.resolver import ManagedByEntries

    when = _iso(datetime.now(timezone.utc) + timedelta(days=1))
    mid = await seed_link_row(
        link_pg_engine, "scheduled", managed=True, scheduled_at=when
    )
    store = SqlAlchemyTranscriptStore(
        async_sessionmaker(link_pg_engine, expire_on_commit=False)
    )
    with pytest.raises(ManagedByEntries):
        await store.set_intent(USER, PLAT, NID, "idle")
    async with link_pg_engine.connect() as conn:
        status = (
            await conn.execute(
                text("SELECT status FROM meetings WHERE id = :m"), {"m": mid}
            )
        ).scalar_one()
    assert status == "scheduled"


# ── the store is the guarantee (Ruling R21): upstream calendar sync skips the row ────────────


def _feed(summary: str, when: datetime) -> dict:
    stamp = when.strftime("%Y%m%dT%H%M%SZ")
    ics = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
        f"BEGIN:VEVENT\r\nUID:uid-1\r\nDTSTAMP:20260701T000000Z\r\nDTSTART:{stamp}\r\n"
        f"SUMMARY:{summary}\r\nLOCATION:https://meet.google.com/{NID}\r\nEND:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    return parse_ics(ics, now=datetime.now(timezone.utc))


def _when() -> datetime:
    return (datetime.now(timezone.utc) + timedelta(days=1)).replace(microsecond=0)


async def test_the_fake_store_refuses_an_entry_managed_row():
    store = InMemoryTranscriptStore()
    mid = store.seed_meeting(
        user_id=USER,
        platform=PLAT,
        native_meeting_id=NID,
        status="scheduled",
        start_time=None,
        data={"scheduled_at": _iso(_when())},
        has_entries=True,
    )
    assert await store.update_planned_meeting(USER, mid, {"title": "x"}) == {
        "error": "managed_by_entries"
    }
    with pytest.raises(ManagedByEntries):
        await store.delete_planned_meeting(USER, mid)
    assert store._meetings[mid]["data"].get("title") is None


async def _sync_all_paths(store: Any) -> list[dict]:
    """Calendar sync meeting an entry-managed row on every write path: the UID match (update),
    the adoption of a plan on the same link (update), and the vanished UID (delete)."""
    when = _when()
    results = [await sync_user(store, USER, _feed("Renamed by the feed", when))]
    results.append(
        await sync_user(store, USER, {"events": [], "cancelled_uids": ["uid-1"]})
    )
    return results


async def test_calendar_sync_skips_an_entry_managed_row_on_the_fake():
    store = InMemoryTranscriptStore()
    when = _iso(_when())
    by_uid = store.seed_meeting(
        user_id=USER,
        platform=PLAT,
        native_meeting_id=NID,
        status="scheduled",
        start_time=None,
        data={"scheduled_at": when, "calendar_uid": "uid-1", "title": "Weekly"},
        has_entries=True,
    )
    before = (store._meetings[by_uid]["status"], dict(store._meetings[by_uid]["data"]))
    for result in await _sync_all_paths(store):
        assert result["counts"]["updated"] == 0 and result["counts"]["cancelled"] == 0
    assert (
        store._meetings[by_uid]["status"],
        store._meetings[by_uid]["data"],
    ) == before


async def test_calendar_sync_never_adopts_an_entry_managed_plan_on_the_fake():
    store = InMemoryTranscriptStore()
    manual = store.seed_meeting(
        user_id=USER,
        platform=PLAT,
        native_meeting_id=NID,
        status="scheduled",
        start_time=None,
        data={"scheduled_at": _iso(_when() + timedelta(hours=1)), "title": "Weekly"},
        has_entries=True,
    )
    before = dict(store._meetings[manual]["data"])
    result = await sync_user(store, USER, _feed("Imported", _when()))
    assert result["counts"]["updated"] == 0 and result["counts"]["created"] == 0
    assert store._meetings[manual]["data"] == before


async def test_pg_store_refuses_an_entry_managed_row(link_pg_engine):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from meeting_api.collector.adapters import SqlAlchemyTranscriptStore

    mid = await seed_link_row(
        link_pg_engine, "scheduled", managed=True, scheduled_at=_iso(_when())
    )
    store = SqlAlchemyTranscriptStore(
        async_sessionmaker(link_pg_engine, expire_on_commit=False)
    )
    assert await store.update_planned_meeting(USER, mid, {"title": "x"}) == {
        "error": "managed_by_entries"
    }
    with pytest.raises(ManagedByEntries):
        await store.delete_planned_meeting(USER, mid)
    row = (await store.list_meetings(USER))[0]
    assert row["id"] == mid and row["data"].get("title") is None


async def test_pg_calendar_sync_skips_an_entry_managed_row(link_pg_engine):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from meeting_api.collector.adapters import SqlAlchemyTranscriptStore

    mid = await seed_link_row(
        link_pg_engine,
        "scheduled",
        managed=True,
        scheduled_at=_iso(_when()),
        data={"calendar_uid": "uid-1", "title": "Weekly"},
    )
    store = SqlAlchemyTranscriptStore(
        async_sessionmaker(link_pg_engine, expire_on_commit=False)
    )
    before = (await store.list_meetings(USER))[0]
    for result in await _sync_all_paths(store):
        assert result["counts"]["updated"] == 0 and result["counts"]["cancelled"] == 0
    (after,) = await store.list_meetings(USER)
    assert after["id"] == mid
    assert (after["status"], after["data"]) == (before["status"], before["data"])
