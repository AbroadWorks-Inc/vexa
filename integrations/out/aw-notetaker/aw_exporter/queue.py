"""Durable pending-export queue on S3, and the sweep worker that drains it
(spec §4.1).

Pending objects live in the VEXA bucket under `aw-exporter/pending/<id>.json`
as `{"envelope": {...}, "attempts": int, "next_attempt_at": epoch seconds
float, "last_error": str | None}`; a restart re-lists that prefix, so an
in-flight job always resumes. Failed ones (>= max_attempts) move to
`aw-exporter/failed/<id>.json`.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

from aw_exporter.job import Deps, ExportResult, export_meeting
from aw_exporter.naming import folder_name
from aw_exporter.storage import Storage

_PENDING_PREFIX = "aw-exporter/pending/"
_FAILED_PREFIX = "aw-exporter/failed/"


class PendingQueue:
    def __init__(self, storage: Storage, bucket: str) -> None:
        self._storage = storage
        self._bucket = bucket
        self._in_flight: set[str] = set()

    def _pending_key(self, meeting_id: str) -> str:
        return f"{_PENDING_PREFIX}{meeting_id}.json"

    def _failed_key(self, meeting_id: str) -> str:
        return f"{_FAILED_PREFIX}{meeting_id}.json"

    def enqueue(self, envelope: dict[str, Any]) -> None:
        meeting_id = str(envelope["data"]["meeting"]["id"])
        self._storage.put_json(
            self._bucket,
            self._pending_key(meeting_id),
            {
                "envelope": envelope,
                "attempts": 0,
                "next_attempt_at": 0.0,
                "last_error": None,
            },
        )

    def pending_ids(self) -> list[str]:
        keys = self._storage.list_keys(self._bucket, _PENDING_PREFIX)
        ids = []
        for key in keys:
            name = key[len(_PENDING_PREFIX) :]
            if name.endswith(".json"):
                ids.append(name[: -len(".json")])
        return ids

    def load(self, meeting_id: str) -> dict[str, Any] | None:
        item: dict[str, Any] | None = self._storage.get_json(
            self._bucket, self._pending_key(meeting_id)
        )
        return item

    def record_failure(
        self,
        meeting_id: str,
        error: str,
        now: Callable[[], float] = time.time,
    ) -> int:
        item = self.load(meeting_id)
        if item is None:
            raise KeyError(meeting_id)
        attempts = int(item["attempts"]) + 1
        item["attempts"] = attempts
        item["last_error"] = error
        item["next_attempt_at"] = now() + 30 * 2**attempts
        self._storage.put_json(self._bucket, self._pending_key(meeting_id), item)
        return attempts

    def done(self, meeting_id: str) -> None:
        self._storage.delete(self._bucket, self._pending_key(meeting_id))

    def fail(self, meeting_id: str) -> None:
        item = self.load(meeting_id)
        if item is not None:
            self._storage.put_json(self._bucket, self._failed_key(meeting_id), item)
        self._storage.delete(self._bucket, self._pending_key(meeting_id))


async def sweep_once(
    queue: PendingQueue,
    deps: Deps,
    job: Callable[[dict[str, Any], Deps], ExportResult] = export_meeting,
    now: Callable[[], float] = time.time,
) -> None:
    settings = deps.settings
    semaphore = asyncio.Semaphore(settings.concurrency)

    async def process_one(meeting_id: str) -> None:
        if meeting_id in queue._in_flight:
            return
        item = queue.load(meeting_id)
        if item is None:
            return
        if float(item.get("next_attempt_at") or 0.0) > now():
            return
        envelope: dict[str, Any] = item["envelope"]
        queue._in_flight.add(meeting_id)
        try:
            async with semaphore:
                await asyncio.to_thread(job, envelope, deps)
        except Exception as exc:  # noqa: BLE001 - retried/backed off, never swallowed
            attempts = queue.record_failure(meeting_id, str(exc), now=now)
            if attempts >= settings.max_attempts:
                m = envelope["data"]["meeting"]
                folder = folder_name(
                    m["platform"], m["native_meeting_id"], m["start_time"]
                )
                deps.storage.put_json(
                    settings.export_bucket,
                    settings.export_prefix + folder + "/_export.json",
                    {
                        "state": "failed",
                        "error": str(exc),
                        "attempts": attempts,
                        "vexa_meeting_id": m["id"],
                    },
                )
                queue.fail(meeting_id)
        else:
            queue.done(meeting_id)
        finally:
            queue._in_flight.discard(meeting_id)

    await asyncio.gather(*(process_one(mid) for mid in queue.pending_ids()))


async def run_worker(queue: PendingQueue, deps: Deps, stop: asyncio.Event) -> None:
    while not stop.is_set():
        await sweep_once(queue, deps)
        try:
            await asyncio.wait_for(stop.wait(), timeout=deps.settings.sweep_seconds)
        except asyncio.TimeoutError:
            pass
