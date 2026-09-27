"""§2.1–§2.5 — the ``/v2`` meeting routes (``meeting_api.intake.router.build_intake_router``).

Driven over HTTP on a bare app that mounts only the intake router (meeting-api's production app
mounts it once the real spawn and stop exist). The service runs on the in-memory fakes; every
success and error body is validated against ``intake.v1``. The last group runs the same routes on
the real Postgres store (skipped unless ``MEETING_API_TEST_DATABASE_URL`` is set).
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from typing import Any

import pytest
from fastapi import FastAPI

from intake_builders import (
    A,
    B,
    GMEET,
    GMEET_OTHER,
    ZOOM,
    conforms,
    entry_body,
    http,
    instant_body,
    intake_app,
    make_harness,
)
from meeting_api.collector.app import build_router as build_collector_router
from meeting_api.collector.fakes import InMemoryTranscriptStore
from meeting_api.intake.fakes import InMemoryIntakeReads
from meeting_api.intake.ports import SpawnOutcome
from meeting_api.intake.router import MAX_LIMIT
from meeting_api.intake.service import _Done

C = "c@client.com"
ACCOUNT = {"x-user-id": "1"}
OTHER_ACCOUNT = {"x-user-id": "2"}
NOT_A_UUID = "not-a-uuid"


def _client(h: Any, **kwargs: Any):
    reads = kwargs.pop("reads", None) or InMemoryIntakeReads(h.store)
    return http(intake_app(h.service, reads, h.stop, lead_s=300, **kwargs)), reads


def _error(response: Any, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    body = response.json()
    conforms(body, "Error")
    assert body["error"]["code"] == code


async def _put(
    client: Any,
    external_id: str = "google:3n5kq8example",
    *,
    headers: dict = ACCOUNT,
    **fields: Any,
) -> dict:
    r = await client.put(
        "/v2/entries", json=entry_body(external_id, **fields), headers=headers
    )
    assert r.status_code == 200, r.text
    conforms(r.json(), "Reply")
    return r.json()


# ── PUT /v2/entries and POST /v2/entries/remove ─────────────────────────────────────────────


async def test_put_creates_and_the_reply_is_intake_v1():
    h = make_harness()
    client, _ = _client(h)
    async with client:
        reply = await _put(client, title="Weekly sync", attendees=[A, B, C])
        again = await _put(client, title="Weekly sync", attendees=[A, B, C])

    assert reply["result"] == "created"
    assert reply["entry"] == {
        "external_id": "google:3n5kq8example",
        "user": A,
        "state": "active",
    }
    assert reply["meeting"]["status"] == "scheduled"
    assert again["result"] == "unchanged"
    assert again["meeting"]["id"] == reply["meeting"]["id"]


@pytest.mark.parametrize(
    "body,code",
    [
        ({"external_id": "x", "meeting_url": GMEET}, "invalid_request"),
        (
            entry_body(start="2026-09-29T09:00:00", end="2026-09-29T09:30:00"),
            "invalid_request",
        ),
        (
            entry_body(start="2026-09-29T09:30:00Z", end="2026-09-29T09:00:00Z"),
            "invalid_request",
        ),
        (
            entry_body(meeting_url="https://example.com/not-a-meeting"),
            "unrecognized_link",
        ),
        (
            entry_body(meeting_url="https://meet.abroadworks.com/Deal4711"),
            "platform_not_enabled",
        ),
        (
            entry_body(start="2026-11-29T09:00:00Z", end="2026-11-29T09:30:00Z"),
            "too_far_ahead",
        ),
        (
            entry_body(start="2026-09-20T09:00:00Z", end="2026-09-20T09:30:00Z"),
            "already_ended",
        ),
    ],
)
async def test_put_refusals_are_400_with_the_code(body, code):
    h = make_harness()
    client, _ = _client(h)
    async with client:
        r = await client.put("/v2/entries", json=body, headers=ACCOUNT)
    _error(r, 400, code)
    assert h.store.meetings == {}


async def test_a_body_that_is_not_json_is_invalid_request():
    h = make_harness()
    client, _ = _client(h)
    async with client:
        r = await client.put("/v2/entries", content=b"{not json", headers=ACCOUNT)
    _error(r, 400, "invalid_request")


async def test_an_error_message_never_echoes_metadata():
    h = make_harness()
    client, _ = _client(h)
    async with client:
        r = await client.put(
            "/v2/entries",
            json=entry_body(metadata={"secret-ish": "x" * 17000}),
            headers=ACCOUNT,
        )
    _error(r, 400, "invalid_request")
    assert "secret-ish" not in r.text and "xxxx" not in r.text


async def test_no_account_identity_is_401_unauthorized():
    h = make_harness()
    client, _ = _client(h)
    async with client:
        for response in (
            await client.put("/v2/entries", json=entry_body()),
            await client.get("/v2/entries", params={"user": A}),
            await client.get("/v2/meetings/x"),
        ):
            _error(response, 401, "unauthorized")


async def test_the_active_entry_quota_is_429_quota_exceeded():
    h = make_harness(max_active_entries=1)
    client, _ = _client(h)
    async with client:
        await _put(client, "one")
        r = await client.put(
            "/v2/entries",
            json=entry_body("two", meeting_url=GMEET_OTHER),
            headers=ACCOUNT,
        )
    _error(r, 429, "quota_exceeded")
    assert "Retry-After" not in r.headers


async def test_remove_replies_and_refusals():
    h = make_harness()
    client, _ = _client(h)
    async with client:
        await _put(client, "one")
        await _put(client, "two", user=B)
        missing = await client.post(
            "/v2/entries/remove",
            json={"external_id": "never-sent", "user": A},
            headers=ACCOUNT,
        )
        bad = await client.post("/v2/entries/remove", json={"user": A}, headers=ACCOUNT)
        first = await client.post(
            "/v2/entries/remove",
            json={"external_id": "one", "user": A, "reason": "cancelled"},
            headers=ACCOUNT,
        )
        last = await client.post(
            "/v2/entries/remove",
            json={"external_id": "two", "user": B},
            headers=ACCOUNT,
        )
        again = await client.post(
            "/v2/entries/remove",
            json={"external_id": "two", "user": B},
            headers=ACCOUNT,
        )

    _error(missing, 404, "entry_not_found")
    _error(bad, 400, "invalid_request")
    for response in (first, last, again):
        assert response.status_code == 200
        conforms(response.json(), "Reply")
    assert first.json()["result"] == "entry_removed"
    assert first.json()["entry"]["state"] == "removed"
    assert last.json()["result"] == "removed"
    assert last.json()["meeting"]["status"] == "failed"
    assert again.json()["result"] == "already_removed"


async def test_join_now_is_spawned_on_the_exact_row_through_the_spawn_port():
    h = make_harness()
    client, _ = _client(h)
    async with client:
        r = await client.put(
            "/v2/entries", json=instant_body("manual:1"), headers=ACCOUNT
        )
    assert r.status_code == 200
    reply = r.json()
    conforms(reply, "Reply")
    assert reply["result"] == "created"
    assert reply["meeting"]["status"] == "requested"
    assert h.spawn.calls == [(1, h.meeting_id(reply["meeting"]["id"]))]


async def test_a_failed_instant_join_carries_the_typed_outcome():
    h = make_harness(
        spawn_failure=SpawnOutcome(
            "failed", "account_limit", "bot limit reached (45 of 45)"
        )
    )
    client, _ = _client(h)
    async with client:
        r = await client.put(
            "/v2/entries", json=instant_body("manual:2"), headers=ACCOUNT
        )
    reply = r.json()
    conforms(reply, "Reply")
    assert reply["meeting"]["status"] == "failed"
    assert reply["meeting"]["outcome"]["kind"] == "not_sent"
    assert reply["meeting"]["outcome"]["detail"] == "account_limit"


async def test_the_reply_entry_state_is_never_null():
    """If the entry isn't on the meeting the reply shows, the service raises instead of replying
    with ``state: null`` (intake.v1 ``ReplyEntry.state`` is one of the entry states)."""
    h = make_harness()
    reply = await h.put()
    view = h.meeting(reply["meeting"]["id"])
    stripped = replace(view, entries=())
    with pytest.raises(LookupError):
        h.service._reply(_Done("unchanged", stripped), A, "google:3n5kq8example")


# ── GET /v2/entries ─────────────────────────────────────────────────────────────────────────


async def test_get_entries_pages_the_senders_active_entries_for_one_user():
    h = make_harness()
    client, _ = _client(h)
    async with client:
        for i in range(5):
            await _put(
                client,
                f"google:e{i}",
                start=f"2026-09-2{7 + i % 3}T0{i}:00:00Z",
                end=f"2026-09-2{7 + i % 3}T0{i}:30:00Z",
                meeting_url=ZOOM if i % 2 else GMEET,
                metadata={"n": i},
            )
        await _put(client, "google:b-entry", user=B)
        await _put(client, "google:removed")
        await client.post(
            "/v2/entries/remove",
            json={"external_id": "google:removed", "user": A},
            headers=ACCOUNT,
        )
        await _put(client, "google:other-account", headers=OTHER_ACCOUNT)

        pages, cursor = [], None
        while True:
            params = {"user": A.upper(), "limit": 2}
            if cursor:
                params["cursor"] = cursor
            r = await client.get("/v2/entries", params=params, headers=ACCOUNT)
            assert r.status_code == 200, r.text
            body = r.json()
            conforms(body, "EntryPage")
            pages.append([e["external_id"] for e in body["entries"]])
            cursor = body["next_cursor"]
            if cursor is None:
                break

    assert pages == [
        ["google:e0", "google:e1"],
        ["google:e2", "google:e3"],
        ["google:e4"],
    ]
    stored = h.store.find_entry(1, A, "google:e3")
    r_all = None
    async with _client(h)[0] as client:
        r_all = await client.get("/v2/entries", params={"user": A}, headers=ACCOUNT)
    row = next(e for e in r_all.json()["entries"] if e["external_id"] == "google:e3")
    assert row["content_hash"] == stored.content_hash
    assert row["state"] == "active"
    assert row["metadata"] == {"n": 3}
    assert row["meeting_url"] == ZOOM


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"user": "not-an-email"},
        {"user": A, "limit": MAX_LIMIT + 1},
        {"user": A, "limit": 0},
        {"user": A, "limit": "many"},
        {"user": A, "cursor": "!!not-base64!!"},
        {"user": A, "cursor": "WzEsMl0="},
    ],
)
async def test_get_entries_refusals_are_400(params):
    h = make_harness()
    client, _ = _client(h)
    async with client:
        r = await client.get("/v2/entries", params=params, headers=ACCOUNT)
    _error(r, 400, "invalid_request")


# ── GET /v2/meetings and GET /v2/meetings/{id} ───────────────────────────────────────────────


async def _visibility_world(client: Any, h: Any) -> dict[str, str]:
    """Owned (A, attendee B), removed (A's entry removed), other account (A again)."""
    owned = await _put(client, "owned", attendees=[A, B])
    removed = await _put(
        client,
        "removed",
        meeting_url=GMEET_OTHER,
        start="2026-09-30T09:00:00Z",
        end="2026-09-30T09:30:00Z",
    )
    await client.post(
        "/v2/entries/remove",
        json={"external_id": "removed", "user": A},
        headers=ACCOUNT,
    )
    other = await _put(client, "elsewhere", headers=OTHER_ACCOUNT)
    return {
        "owned": owned["meeting"]["id"],
        "removed": removed["meeting"]["id"],
        "other": other["meeting"]["id"],
    }


async def test_single_read_visibility_owner_attendee_removed_stranger():
    h = make_harness()
    client, _ = _client(h)
    async with client:
        ids = await _visibility_world(client, h)

        async def read(uuid: str, user: str | None, headers: dict = ACCOUNT):
            params = {"user": user} if user is not None else {}
            return await client.get(
                f"/v2/meetings/{uuid}", params=params, headers=headers
            )

        owner = await read(ids["owned"], A)
        attendee = await read(ids["owned"], B)
        removed_entry = await read(ids["removed"], A)
        no_user = await read(ids["owned"], None)
        stranger = await read(ids["owned"], C)
        removed_stranger = await read(ids["removed"], B)
        other_account = await read(ids["other"], None)
        other_account_as_owner = await read(ids["other"], A, OTHER_ACCOUNT)
        not_uuid = await read(NOT_A_UUID, None)

    for ok in (owner, attendee, removed_entry, no_user, other_account_as_owner):
        assert ok.status_code == 200, ok.text
        conforms(ok.json(), "Meeting")
    assert owner.json()["id"] == ids["owned"]
    assert removed_entry.json()["status"] == "failed"
    for missing in (stranger, removed_stranger, other_account, not_uuid):
        _error(missing, 404, "meeting_not_found")


async def test_meeting_list_visibility_order_and_filters():
    h = make_harness()
    client, _ = _client(h)
    async with client:
        ids = await _visibility_world(client, h)

        async def listing(**params: Any) -> list[str]:
            r = await client.get("/v2/meetings", params=params, headers=ACCOUNT)
            assert r.status_code == 200, r.text
            conforms(r.json(), "MeetingPage")
            assert r.json()["next_cursor"] is None
            return [m["id"] for m in r.json()["meetings"]]

        as_owner = await listing(user=A)
        as_attendee = await listing(user=B)
        as_stranger = await listing(user=C)
        finished = await listing(user=A, status="failed")
        by_external = await listing(user=A, external_id="owned")
        window = await listing(
            user=A,
            **{"from": "2026-09-30T00:00:00Z", "to": "2026-10-01T00:00:00+00:00"},
        )
        before = await listing(user=A, to="2026-09-30T00:00:00Z")
        naive = await client.get(
            "/v2/meetings",
            params={"user": A, "from": "2026-09-30T00:00:00"},
            headers=ACCOUNT,
        )
        no_user = await client.get("/v2/meetings", headers=ACCOUNT)

    assert as_owner == [ids["removed"], ids["owned"]]
    assert as_attendee == [ids["owned"]]
    assert as_stranger == []
    assert finished == [ids["removed"]]
    assert by_external == [ids["owned"]]
    assert window == [ids["removed"]]
    assert before == [ids["owned"]]
    _error(naive, 400, "invalid_request")
    _error(no_user, 400, "invalid_request")


async def test_meeting_list_cursor_pages_newest_first_without_gaps():
    h = make_harness()
    client, _ = _client(h)
    async with client:
        expected = []
        for day in range(1, 8):
            reply = await _put(
                client,
                f"google:day{day}",
                start=f"2026-10-0{day}T09:00:00Z",
                end=f"2026-10-0{day}T09:30:00Z",
            )
            expected.insert(0, reply["meeting"]["id"])
        seen, cursor = [], None
        while True:
            params: dict[str, Any] = {"user": A, "limit": 3}
            if cursor:
                params["cursor"] = cursor
            r = await client.get("/v2/meetings", params=params, headers=ACCOUNT)
            body = r.json()
            conforms(body, "MeetingPage")
            assert len(body["meetings"]) <= 3
            seen += [m["id"] for m in body["meetings"]]
            cursor = body["next_cursor"]
            if cursor is None:
                break
        wrong_kind = await client.get(
            "/v2/meetings",
            params={"user": A, "cursor": "Imdvb2dsZTpkYXkxIg=="},
            headers=ACCOUNT,
        )
        too_many = await client.get(
            "/v2/meetings", params={"user": A, "limit": MAX_LIMIT + 1}, headers=ACCOUNT
        )
    assert seen == expected
    _error(wrong_kind, 400, "invalid_request")
    _error(too_many, 400, "invalid_request")


# ── POST /v2/meetings/{id}/stop ─────────────────────────────────────────────────────────────


async def test_stop_scheduled_or_finished_is_409_no_live_bot():
    h = make_harness()
    client, _ = _client(h)
    async with client:
        scheduled = await _put(client, "s")
        finished = await _put(client, "f", meeting_url=GMEET_OTHER)
        h.set_status(finished["meeting"]["id"], "failed")
        r1 = await client.post(
            f"/v2/meetings/{scheduled['meeting']['id']}/stop", headers=ACCOUNT
        )
        r2 = await client.post(
            f"/v2/meetings/{finished['meeting']['id']}/stop", headers=ACCOUNT
        )
    _error(r1, 409, "no_live_bot")
    _error(r2, 409, "no_live_bot")
    assert h.stop.calls == []


async def test_stop_a_live_meeting_calls_the_stop_port_with_no_outcome():
    h = make_harness()
    client, _ = _client(h)
    async with client:
        reply = await _put(client)
        uuid = reply["meeting"]["id"]
        h.set_status(uuid, "requested")
        h.set_status(uuid, "active")
        r = await client.post(f"/v2/meetings/{uuid}/stop", headers=ACCOUNT)
        other = await client.post(f"/v2/meetings/{uuid}/stop", headers=OTHER_ACCOUNT)
    assert r.status_code == 200, r.text
    conforms(r.json(), "Meeting")
    assert r.json()["status"] == "stopping"
    assert h.stop.calls == [(1, h.meeting_id(uuid), None)]
    _error(other, 404, "meeting_not_found")


# ── storage down, races, and the upstream routes ────────────────────────────────────────────


class _DownReads(InMemoryIntakeReads):
    async def meeting_by_uuid(self, user_id: int, uuid: str):
        raise ConnectionRefusedError("database is down")

    async def entries(self, *args: Any, **kwargs: Any):
        raise TimeoutError("database timed out")


async def test_storage_down_is_503_unavailable():
    h = make_harness()

    def refuse(store: Any, rooms: Any) -> None:
        raise ConnectionRefusedError("database is down")

    client, _ = _client(h, reads=_DownReads(h.store))
    async with client:
        h.store.on_lock = refuse
        put = await client.put("/v2/entries", json=entry_body(), headers=ACCOUNT)
        read = await client.get(f"/v2/meetings/{NOT_A_UUID}", headers=ACCOUNT)
        listing = await client.get("/v2/entries", params={"user": A}, headers=ACCOUNT)
    for response in (put, read, listing):
        _error(response, 503, "unavailable")


async def test_a_programming_error_is_not_dressed_up_as_unavailable():
    class _Broken(InMemoryIntakeReads):
        async def meeting_by_uuid(self, user_id: int, uuid: str):
            raise KeyError("bug")

    h = make_harness()
    client, _ = _client(h, reads=_Broken(h.store))
    async with client:
        r = await client.get(f"/v2/meetings/{NOT_A_UUID}", headers=ACCOUNT)
    assert r.status_code == 500


async def test_upstream_routes_keep_422_on_the_same_app():
    h = make_harness()
    app = FastAPI()
    app.include_router(build_collector_router(InMemoryTranscriptStore(), None))
    intake_app(h.service, InMemoryIntakeReads(h.store), h.stop, app=app)
    async with http(app) as client:
        upstream = await client.get(
            "/meetings", params={"limit": "many"}, headers=ACCOUNT
        )
        mine = await client.get(
            "/v2/meetings", params={"user": A, "limit": "many"}, headers=ACCOUNT
        )
    assert upstream.status_code == 422
    assert "detail" in upstream.json()
    _error(mine, 400, "invalid_request")


# ── real Postgres ───────────────────────────────────────────────────────────────────────────

needs_pg = pytest.mark.skipif(
    not os.getenv("MEETING_API_TEST_DATABASE_URL"),
    reason="real-Postgres route proofs; set MEETING_API_TEST_DATABASE_URL to run",
)


@pytest.fixture
async def pg_routes(intake_pg_engine, monkeypatch):
    pytest.importorskip("sqlalchemy", reason="see test_intake_pg_schema.py's docstring")
    pytest.importorskip("asyncpg", reason="see test_intake_pg_schema.py's docstring")
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from admin_api.schema import models as admin_models
    from admin_api.schema import sync as admin_sync
    from intake_builders import FakeClock, make_settings, ts
    from meeting_api.intake import PostgresIntakeStore
    from meeting_api.intake import status as status_mod
    from meeting_api.intake.fakes import FakePublisher
    from meeting_api.intake.reads import PostgresIntakeReads
    from meeting_api.intake.service import IntakeService

    await admin_sync.ensure_schema(intake_pg_engine, admin_models.Base)
    monkeypatch.setenv("AUTO_JOIN_LEAD_S", "300")
    clock = FakeClock(ts("2026-09-26T12:00:00Z"))
    monkeypatch.setattr(status_mod, "_now", clock)
    factory = async_sessionmaker(intake_pg_engine, expire_on_commit=False)
    store = PostgresIntakeStore(factory)

    class _NoStop:
        calls: list = []

        async def stop_live(
            self, user_id: int, meeting_id: int, *, outcome: Any
        ) -> None:
            self.calls.append((user_id, meeting_id, outcome))

    class _NoSpawn:
        async def spawn_exact(self, user_id: int, meeting_id: int) -> SpawnOutcome:
            return SpawnOutcome("already_live")

    service = IntakeService(
        store, _NoSpawn(), _NoStop(), FakePublisher(), make_settings(), clock=clock
    )
    reads = PostgresIntakeReads(factory)
    return service, reads, _NoStop(), intake_pg_engine


@needs_pg
async def test_pg_the_same_entry_on_two_links_at_once_is_503_then_retry_works(
    pg_routes, monkeypatch
):
    """A7's carry: two PUTs of one entry with different links take different link locks, both find
    no entry and both insert it; the loser's unique violation is a retryable 503, never a 500, and
    it rolls back everything it wrote."""
    from meeting_api.intake.adapters import PostgresIntakeTx

    service, reads, stop, engine = pg_routes
    barrier = asyncio.Barrier(2)
    original = PostgresIntakeTx.save_entry

    async def save_entry(self, *args: Any, **kwargs: Any):
        await barrier.wait()
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(PostgresIntakeTx, "save_entry", save_entry)
    async with http(intake_app(service, reads, stop)) as client:
        results = await asyncio.gather(
            client.put("/v2/entries", json=entry_body("race"), headers=ACCOUNT),
            client.put(
                "/v2/entries",
                json=entry_body("race", meeting_url=GMEET_OTHER),
                headers=ACCOUNT,
            ),
        )
        monkeypatch.setattr(PostgresIntakeTx, "save_entry", original)
        codes = sorted(r.status_code for r in results)
        assert codes == [200, 503], [r.text for r in results]
        loser = next(r for r in results if r.status_code == 503)
        _error(loser, 503, "unavailable")
        winner = next(r for r in results if r.status_code == 200).json()

        from sqlalchemy import text

        async with engine.connect() as conn:
            meetings = (
                await conn.execute(text("SELECT count(*) FROM meetings"))
            ).scalar()
            outbox = (
                await conn.execute(text("SELECT count(*) FROM webhook_outbox"))
            ).scalar()
        assert (meetings, outbox) == (1, 1)

        retry_body = (
            entry_body("race")
            if winner["meeting"]["room"] != "kxo-misr-avz"
            else entry_body("race", meeting_url=GMEET_OTHER)
        )
        retry = await client.put("/v2/entries", json=retry_body, headers=ACCOUNT)
    assert retry.status_code == 200
    assert retry.json()["result"] == "updated"


@needs_pg
async def test_pg_reads_visibility_paging_and_entries(pg_routes):
    service, reads, stop, _ = pg_routes
    async with http(intake_app(service, reads, stop)) as client:
        ids = await _visibility_world(client, None)
        for day in range(1, 5):
            await _put(
                client,
                f"google:day{day}",
                start=f"2026-10-0{day}T09:00:00.123456Z",
                end=f"2026-10-0{day}T09:30:00Z",
            )
        seen, cursor = [], None
        while True:
            params: dict[str, Any] = {"user": A, "limit": 2}
            if cursor:
                params["cursor"] = cursor
            r = await client.get("/v2/meetings", params=params, headers=ACCOUNT)
            assert r.status_code == 200, r.text
            conforms(r.json(), "MeetingPage")
            seen.append(len(r.json()["meetings"]))
            cursor = r.json()["next_cursor"]
            if cursor is None:
                break
        attendee = await client.get("/v2/meetings", params={"user": B}, headers=ACCOUNT)
        stranger = await client.get(
            f"/v2/meetings/{ids['owned']}", params={"user": C}, headers=ACCOUNT
        )
        removed = await client.get(
            f"/v2/meetings/{ids['removed']}", params={"user": A}, headers=ACCOUNT
        )
        other = await client.get(f"/v2/meetings/{ids['other']}", headers=ACCOUNT)
        not_uuid = await client.get(f"/v2/meetings/{NOT_A_UUID}", headers=ACCOUNT)
        entries = await client.get(
            "/v2/entries", params={"user": A, "limit": 3}, headers=ACCOUNT
        )
        entries_2 = await client.get(
            "/v2/entries",
            params={"user": A, "limit": 3, "cursor": entries.json()["next_cursor"]},
            headers=ACCOUNT,
        )
    assert seen == [2, 2, 2]
    assert [m["id"] for m in attendee.json()["meetings"]] == [ids["owned"]]
    _error(stranger, 404, "meeting_not_found")
    assert removed.status_code == 200
    conforms(removed.json(), "Meeting")
    _error(other, 404, "meeting_not_found")
    _error(not_uuid, 404, "meeting_not_found")
    conforms(entries.json(), "EntryPage")
    conforms(entries_2.json(), "EntryPage")
    assert [e["external_id"] for e in entries.json()["entries"]] == [
        "google:day1",
        "google:day2",
        "google:day3",
    ]
    assert [e["external_id"] for e in entries_2.json()["entries"]] == [
        "google:day4",
        "owned",
    ]
    assert entries_2.json()["next_cursor"] is None


@needs_pg
async def test_pg_a_database_that_cannot_be_reached_is_503():
    pytest.importorskip("sqlalchemy")
    pytest.importorskip("asyncpg")
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from intake_builders import make_settings
    from meeting_api.intake import PostgresIntakeStore
    from meeting_api.intake.fakes import FakePublisher
    from meeting_api.intake.reads import PostgresIntakeReads
    from meeting_api.intake.service import IntakeService

    engine = create_async_engine(
        "postgresql+asyncpg://postgres:test@127.0.0.1:1/postgres"
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    store = PostgresIntakeStore(factory)

    class _Unused:
        async def spawn_exact(self, *a: Any) -> SpawnOutcome:
            raise AssertionError("never reached")

        async def stop_live(self, *a: Any, **k: Any) -> None:
            raise AssertionError("never reached")

    service = IntakeService(
        store, _Unused(), _Unused(), FakePublisher(), make_settings()
    )
    try:
        async with http(
            intake_app(service, PostgresIntakeReads(factory), _Unused())
        ) as client:
            put = await client.put("/v2/entries", json=entry_body(), headers=ACCOUNT)
            read = await client.get("/v2/meetings", params={"user": A}, headers=ACCOUNT)
    finally:
        await engine.dispose()
    _error(put, 503, "unavailable")
    _error(read, 503, "unavailable")
