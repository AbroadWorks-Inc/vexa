"""Shared builders for the entry-service tests (§1.3, §2.6): a controllable clock, request bodies,
the settings, the request helpers (``Requests``) and a harness wiring ``IntakeService`` to the
in-memory fakes. ``test_intake_adapter_pg.py`` builds its Postgres harness from the same pieces.

For the ``/v2`` routes: ``intake_app`` mounts ``build_intake_router`` on a bare FastAPI app,
``http`` is an in-process client for it (same event loop as the test, so a real Postgres engine
works too), and ``conforms`` validates a body against an ``intake.v1`` shape.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import httpx
import jsonschema
from fastapi import FastAPI
from referencing import Registry, Resource

from meeting_api.intake.fakes import (
    FakePublisher,
    FakeSpawn,
    FakeStop,
    InMemoryIntakeStore,
)
from meeting_api.intake.ports import IntakeReads, MeetingView, SpawnOutcome, StopPort
from meeting_api.intake.router import build_intake_router
from meeting_api.intake.service import IntakeService
from meeting_api.intake.settings import IntakeSettings
from meeting_api.intake.status import Outcome, WrittenEvent

UTC = timezone.utc
A = "a@abroadworks.com"
B = "b@abroadworks.com"
GMEET = "https://meet.google.com/kxo-misr-avz"
GMEET_OTHER = "https://meet.google.com/abc-defg-hij"
ZOOM = "https://us02web.zoom.us/j/12345678901?pwd=abc"


def ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class FakeClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def set(self, value: str) -> None:
        self.now = ts(value)

    def advance(self, **kwargs: float) -> None:
        self.now = self.now + timedelta(**kwargs)


def entry_body(
    external_id: str = "google:3n5kq8example",
    *,
    user: str = A,
    meeting_url: str = GMEET,
    start: Optional[str] = "2026-09-29T09:00:00Z",
    end: Optional[str] = "2026-09-29T09:30:00Z",
    **extra: Any,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "external_id": external_id,
        "user": user,
        "meeting_url": meeting_url,
        "start": start,
        "end": end,
        **extra,
    }
    return {k: v for k, v in body.items() if v is not None}


def instant_body(external_id: str, meeting_url: str = ZOOM, *, user: str = A) -> dict:
    return {
        "external_id": external_id,
        "user": user,
        "meeting_url": meeting_url,
        "join_now": True,
    }


class Requests:
    """``PUT`` / instant join / remove through ``self.service``, whatever store it runs on."""

    service: IntakeService

    async def put(
        self,
        external_id: str = "google:3n5kq8example",
        *,
        user_id: int = 1,
        **fields: Any,
    ) -> dict:
        return await self.service.put_entry(user_id, entry_body(external_id, **fields))

    async def instant(
        self,
        external_id: str,
        meeting_url: str = ZOOM,
        *,
        user_id: int = 1,
        user: str = A,
    ) -> dict:
        return await self.service.put_entry(
            user_id, instant_body(external_id, meeting_url, user=user)
        )

    async def remove(
        self,
        external_id: str = "google:3n5kq8example",
        *,
        user: str = A,
        reason: Optional[str] = None,
        user_id: int = 1,
    ) -> dict:
        body: dict[str, Any] = {"external_id": external_id, "user": user}
        if reason is not None:
            body["reason"] = reason
        return await self.service.remove_entry(user_id, body)


@dataclass
class Harness(Requests):
    clock: FakeClock
    settings: IntakeSettings
    store: InMemoryIntakeStore
    spawn: FakeSpawn
    stop: FakeStop
    publisher: FakePublisher
    service: IntakeService

    def meeting_id(self, uuid: str) -> int:
        return next(
            mid for mid, row in self.store.meetings.items() if row["uuid"] == uuid
        )

    def meeting(self, uuid: str) -> MeetingView:
        return self.store.view(self.meeting_id(uuid))

    def mark(self) -> int:
        return len(self.store.events)

    def events(self, since: int = 0) -> list[tuple[str, str]]:
        """``(meeting uuid, event type)`` for every event written since ``since``, in order."""
        return [(e.meeting_uuid, e.event_type) for e in self.store.events[since:]]

    def published(self) -> list[str]:
        return [event_id for batch in self.publisher.batches for event_id in batch]

    def set_status(
        self, uuid: str, status: str, *, outcome: Optional[Outcome] = None
    ) -> WrittenEvent:
        """A status change from outside intake (the bot lifecycle), through the one writer."""
        mid = self.meeting_id(uuid)
        return self.store.write_status(
            mid,
            status,
            expected_from={self.store.meetings[mid]["status"]},
            outcome=outcome,
        )


def make_settings(
    *,
    lead_s: int = 300,
    max_active_entries: int = 100_000,
    blocked_hosts: frozenset[str] = frozenset({"meet.abroadworks.com"}),
) -> IntakeSettings:
    return IntakeSettings(
        max_days_ahead=30,
        join_now_adopt_ahead_s=3600,
        lead_s=lead_s,
        blocked_hosts=blocked_hosts,
        max_active_entries=max_active_entries,
    )


def make_harness(
    now: str = "2026-09-26T12:00:00Z",
    *,
    lead_s: int = 300,
    max_active_entries: int = 100_000,
    blocked_hosts: frozenset[str] = frozenset({"meet.abroadworks.com"}),
    spawn_failure: Optional[SpawnOutcome] = None,
) -> Harness:
    clock = FakeClock(ts(now))
    settings = make_settings(
        lead_s=lead_s,
        max_active_entries=max_active_entries,
        blocked_hosts=blocked_hosts,
    )
    store = InMemoryIntakeStore(clock=clock, lead_s=lead_s)
    spawn = FakeSpawn(store, failure=spawn_failure)
    stop = FakeStop(store)
    publisher = FakePublisher()
    service = IntakeService(store, spawn, stop, publisher, settings, clock=clock)
    return Harness(clock, settings, store, spawn, stop, publisher, service)


# ── the /v2 routes ───────────────────────────────────────────────────────────────────────────


def _intake_schema() -> dict:
    for parent in Path(__file__).resolve().parents:
        candidate = (
            parent / "meetings" / "contracts" / "intake.v1" / "intake.schema.json"
        )
        if candidate.is_file():
            return json.loads(candidate.read_text())
    raise FileNotFoundError("intake.v1 schema not found")


_SCHEMA = _intake_schema()
_REGISTRY = Registry().with_resource(_SCHEMA["$id"], Resource.from_contents(_SCHEMA))


def conforms(body: Any, shape: str) -> None:
    """Raise unless ``body`` is a valid ``intake.v1#/$defs/<shape>``."""
    jsonschema.Draft202012Validator(
        {"$ref": f"{_SCHEMA['$id']}#/$defs/{shape}"}, registry=_REGISTRY
    ).validate(body)


def intake_app(
    service: IntakeService,
    reads: IntakeReads,
    stop: StopPort,
    *,
    artifact_store: Any = None,
    artifact_deleter: Any = None,
    lead_s: int = 300,
    app: Optional[FastAPI] = None,
) -> FastAPI:
    from meeting_api.collector.fakes import InMemoryTranscriptStore

    app = app or FastAPI()
    app.include_router(
        build_intake_router(
            service,
            reads,
            stop,
            artifact_store=(
                artifact_store
                if artifact_store is not None
                else InMemoryTranscriptStore()
            ),
            artifact_deleter=artifact_deleter,
            lead_s=lead_s,
        )
    )
    return app


def http(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://meeting-api",
    )
