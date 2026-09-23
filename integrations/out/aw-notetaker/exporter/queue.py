"""Durable pending-export queue on S3, and the sweep worker that drains it
(spec §4.1).

Pending objects live in the VEXA bucket under `aw-exporter/pending/<id>.json`
as `{"envelope": {...}, "attempts": int, "next_attempt_at": epoch seconds
float, "last_error": str | None}`; a restart re-lists that prefix, so an
in-flight job always resumes. Failed ones (>= max_attempts) move to
`aw-exporter/failed/<id>.json`.

The worker must never die: a bad envelope, a broken S3 call for one id, or
an unexpected exception anywhere in a single sweep is logged and contained
to that id/sweep so every other pending meeting keeps draining.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

from exporter.job import Deps, ExportResult, export_meeting
from exporter.naming import folder_name
from exporter.storage import Storage

logger = logging.getLogger("exporter")

_PENDING_PREFIX = "aw-exporter/pending/"
_FAILED_PREFIX = "aw-exporter/failed/"


class PendingQueue:
    def __init__(self, storage: Storage, bucket: str) -> None:
        self._storage = storage
        self._bucket = bucket

    def _pending_key(self, meeting_id: str) -> str:
        return f"{_PENDING_PREFIX}{meeting_id}.json"

    def _failed_key(self, meeting_id: str) -> str:
        return f"{_FAILED_PREFIX}{meeting_id}.json"

    def enqueue(self, envelope: dict[str, Any]) -> None:
        """Durably enqueue `envelope`. A redelivery of an id already pending
        refreshes the stored envelope only — attempts/next_attempt_at/
        last_error are left alone, so a redelivered webhook never resets an
        in-progress backoff."""
        meeting_id = str(envelope["data"]["meeting"]["id"])
        existing = self.load(meeting_id)
        if existing is not None:
            existing["envelope"] = envelope
            self._storage.put_json(
                self._bucket, self._pending_key(meeting_id), existing
            )
            logger.info(
                "enqueue: refreshed pending envelope meeting_id=%s attempts=%s",
                meeting_id,
                existing["attempts"],
            )
            return
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
        logger.info("enqueue: new pending meeting_id=%s", meeting_id)

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


def _quarantine_marker(
    envelope: dict[str, Any], attempts: int, error: str
) -> tuple[str, dict[str, Any]] | None:
    """The export-bucket `_export.json` failure marker's (folder, body) for a
    meeting that has exhausted its attempts; `None` if the envelope is too
    malformed to derive a folder name (spec §4.1 quarantine) — the caller
    must still quarantine the pending item even when this returns `None`."""
    try:
        m = envelope["data"]["meeting"]
        folder = folder_name(m["platform"], m["native_meeting_id"], m["start_time"])
        vexa_meeting_id = m["id"]
    except (KeyError, ValueError, TypeError):
        return None
    return folder, {
        "state": "failed",
        "error": error,
        "attempts": attempts,
        "vexa_meeting_id": vexa_meeting_id,
    }


async def sweep_once(
    queue: PendingQueue,
    deps: Deps,
    job: Callable[[dict[str, Any], Deps], ExportResult] = export_meeting,
    now: Callable[[], float] = time.time,
) -> None:
    settings = deps.settings
    semaphore = asyncio.Semaphore(settings.concurrency)

    async def process_one(meeting_id: str) -> None:
        try:
            item = queue.load(meeting_id)
            if item is None:
                return
            if float(item.get("next_attempt_at") or 0.0) > now():
                return
            envelope: dict[str, Any] = item["envelope"]
            try:
                async with semaphore:
                    await asyncio.to_thread(job, envelope, deps)
            except Exception as exc:  # noqa: BLE001 - contained per id, logged
                attempts = queue.record_failure(meeting_id, str(exc), now=now)
                logger.warning(
                    "export job failed meeting_id=%s attempts=%s error_class=%s",
                    meeting_id,
                    attempts,
                    type(exc).__name__,
                )
                if attempts >= settings.max_attempts:
                    marker = _quarantine_marker(envelope, attempts, str(exc))
                    if marker is None:
                        logger.error(
                            "quarantine: meeting_id=%s attempts=%s - envelope too "
                            "malformed to derive an export folder, skipping marker",
                            meeting_id,
                            attempts,
                        )
                    else:
                        folder, body = marker
                        deps.storage.put_json(
                            settings.export_bucket,
                            settings.export_prefix + folder + "/_export.json",
                            body,
                        )
                        logger.error(
                            "quarantine: meeting_id=%s attempts=%s moved to failed/",
                            meeting_id,
                            attempts,
                        )
                    queue.fail(meeting_id)
            else:
                queue.done(meeting_id)
        except Exception:  # noqa: BLE001 - one id's bug must not sink the sweep
            logger.exception(
                "sweep_once: unhandled error processing meeting_id=%s", meeting_id
            )

    await asyncio.gather(
        *(process_one(meeting_id) for meeting_id in queue.pending_ids()),
        return_exceptions=True,
    )


async def run_worker(queue: PendingQueue, deps: Deps, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await sweep_once(queue, deps)
        except Exception:  # noqa: BLE001 - the worker loop must never die
            logger.exception("run_worker: sweep_once raised; continuing")
        try:
            await asyncio.wait_for(stop.wait(), timeout=deps.settings.sweep_seconds)
        except asyncio.TimeoutError:
            pass
