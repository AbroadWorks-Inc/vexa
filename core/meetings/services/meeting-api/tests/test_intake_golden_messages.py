"""The ``intake.v1`` error goldens say what meeting-api actually sends.

Each ``golden/Error.<code>.json`` whose code meeting-api answers on a ``/v2`` route is reproduced
here through the routes (the in-memory harness), and the reply must carry the golden's code and
message exactly. The codes other services send to clients are listed with their sender, so a new
golden is either reproduced here or named as someone else's.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Awaitable, Callable

import pytest

from intake_builders import (
    A,
    entry_body,
    http,
    intake_app,
    make_harness,
)
from meeting_api.intake.fakes import InMemoryIntakeReads

ACCOUNT = {"x-user-id": "1"}
UNKNOWN_UUID = "5f0c2b7e-8d1a-4c3e-9b6f-2a7d1e4c8b90"

#: Codes whose client-facing reply another service writes (meeting-api never answers them on a
#: /v2 route, or only to a caller that bypassed the gateway).
OTHER_SENDERS = {
    "unauthorized": "gateway",
    "forbidden": "gateway",
    "rate_limited": "gateway",
    "account_not_found": "admin-api",
    "webhook_not_found": "admin-api",
}


def _golden_dir() -> Path:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "meetings" / "contracts" / "intake.v1" / "golden"
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError("intake.v1 goldens not found")


GOLDENS = {
    path.name[len("Error.") : -len(".json")]: json.loads(path.read_text())["error"]
    for path in sorted(_golden_dir().glob("Error.*.json"))
}

Scenario = Callable[[Any], Awaitable[Any]]


async def _put(client: Any, **fields: Any) -> Any:
    return await client.put("/v2/entries", json=entry_body(**fields), headers=ACCOUNT)


async def _scheduled(client: Any) -> str:
    reply = await _put(client)
    assert reply.status_code == 200, reply.text
    return reply.json()["meeting"]["id"]


async def _not_finished(client: Any) -> Any:
    return await client.delete(
        f"/v2/meetings/{await _scheduled(client)}", headers=ACCOUNT
    )


async def _no_live_bot(client: Any) -> Any:
    uuid = await _scheduled(client)
    return await client.post(f"/v2/meetings/{uuid}/stop", headers=ACCOUNT)


SCENARIOS: dict[str, tuple[dict[str, Any], Scenario]] = {
    "already_ended": (
        {},
        lambda c: _put(c, start="2026-09-25T09:00:00Z", end="2026-09-25T09:30:00Z"),
    ),
    "too_far_ahead": (
        {},
        lambda c: _put(c, start="2026-10-27T13:00:00Z", end="2026-10-27T14:00:00Z"),
    ),
    "invalid_request": ({}, lambda c: _put(c, start="2026-09-29T09:00:00")),
    "unrecognized_link": (
        {},
        lambda c: _put(c, meeting_url="https://example.com/not-a-meeting"),
    ),
    "platform_not_enabled": ({"blocked_hosts": frozenset({"meet.google.com"})}, _put),
    "quota_exceeded": ({"active_entries": 100_000}, _put),
    "entry_not_found": (
        {},
        lambda c: c.post(
            "/v2/entries/remove",
            json={"external_id": "google:nothing", "user": A, "reason": "cancelled"},
            headers=ACCOUNT,
        ),
    ),
    "meeting_not_found": (
        {},
        lambda c: c.get(f"/v2/meetings/{UNKNOWN_UUID}", headers=ACCOUNT),
    ),
    "meeting_not_finished": ({}, _not_finished),
    "no_live_bot": ({}, _no_live_bot),
    "unavailable": ({"on_lock": "down"}, _put),
    "internal_error": ({"on_lock": "race"}, _put),
}


def test_every_golden_code_is_reproduced_or_named_as_another_services():
    assert set(GOLDENS) == set(SCENARIOS) | set(OTHER_SENDERS)


@pytest.mark.parametrize("code", sorted(SCENARIOS))
async def test_the_golden_message_is_the_one_meeting_api_sends(code, monkeypatch):
    from meeting_api.intake import fakes
    from meeting_api.intake.ports import ConstraintRace

    setup, scenario = SCENARIOS[code]
    h = make_harness(
        **(
            {"blocked_hosts": setup["blocked_hosts"]}
            if "blocked_hosts" in setup
            else {}
        )
    )
    if "active_entries" in setup:
        count = setup["active_entries"]

        async def full(self: Any, user_id: int) -> int:
            return count

        monkeypatch.setattr(fakes._FakeTx, "count_active_entries", full)

    def down(store: Any, rooms: Any) -> None:
        raise ConnectionRefusedError("database is down")

    def race(store: Any, rooms: Any) -> None:
        if rooms:
            raise ConstraintRace("uq_meeting_entries_user_source_external")

    on_lock = {"down": down, "race": race}.get(setup.get("on_lock", ""))
    async with http(
        intake_app(h.service, InMemoryIntakeReads(h.store), h.stop)
    ) as client:
        if on_lock is not None:
            h.store.on_lock = on_lock
        reply = await scenario(client)
    assert reply.json()["error"] == GOLDENS[code], reply.text
