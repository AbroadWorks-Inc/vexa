"""Durable pending-export queue on S3, and the sweep worker that drains it
(spec §4.1).

Pending objects live in the VEXA bucket under `aw-exporter/pending/<id>.json`
(`<id>` is the meeting's UUID) as `{"envelope": {...}, "attempts": int,
"next_attempt_at": epoch seconds float, "last_error": str | None}`; a restart
re-lists that prefix, so an in-flight job always resumes. Failed ones
(>= max_attempts) move to `aw-exporter/failed/<id>.json`. Each queued event
is recorded under `aw-exporter/events/<event_id>.json` (`meeting_id`,
`event_type`, `sequence`), so a redelivery of it is recognised
(`seen`) after its meeting has left the queue.

Retries: a job that raises is recorded as a failure and becomes visible to
the sweep again only after its backoff (`next_attempt_at`:
`EXPORT_RETRY_BACKOFF_SECONDS` × 2**attempts), up to `EXPORT_MAX_ATTEMPTS`. The export result report (`export_result.py`) is a
step of the job, so an unaccepted report is retried the same way: the re-run
finds the folder's `_export.json` already `handed_off` and only reports
again. When the attempts run out, the item is quarantined: an unaccepted
report leaves the recorded outcome as it is; any other failure reads the
folder's `_export.json` first. A folder already `handed_off` keeps its marker
and reports `handed_off` once; once that report is accepted the item is
finished and leaves the queue (`done`), not `failed/`. Otherwise the `failed`
marker is written and `failed` reported once. A marker that can't be read is
left as it is. An item whose meeting is not the §2.4 meeting
(`NotAV2Meeting`) is quarantined on its first attempt and writes nothing to
the export bucket.

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

from exporter.export_result import ExportReportError, ReportState
from exporter.job import Deps, ExportResult, NotAV2Meeting, export_meeting
from exporter.naming import folder_name
from exporter.retention import METADATA
from exporter.storage import Storage

logger = logging.getLogger("exporter")

_PENDING_PREFIX = "aw-exporter/pending/"
_FAILED_PREFIX = "aw-exporter/failed/"
_EVENTS_PREFIX = "aw-exporter/events/"


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

    def seen(self, event_id: str) -> bool:
        """True when the event `event_id` was queued before."""
        key = f"{_EVENTS_PREFIX}{event_id}.json"
        return self._storage.size(self._bucket, key) is not None

    def record_event(self, envelope: dict[str, Any]) -> None:
        """Record the queued event, so a redelivery of it is a duplicate."""
        meeting = envelope["data"]["meeting"]
        self._storage.put_json(
            self._bucket,
            f"{_EVENTS_PREFIX}{envelope['event_id']}.json",
            {
                "meeting_id": meeting["id"],
                "event_type": envelope["event_type"],
                "sequence": meeting.get("sequence"),
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
        *,
        backoff_seconds: float,
        now: Callable[[], float] = time.time,
    ) -> int:
        """Count one failed attempt; the next waits `backoff_seconds` ×
        2**attempts."""
        item = self.load(meeting_id)
        if item is None:
            raise KeyError(meeting_id)
        attempts = int(item["attempts"]) + 1
        item["attempts"] = attempts
        item["last_error"] = error
        item["next_attempt_at"] = now() + backoff_seconds * 2**attempts
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
        folder = folder_name(m["platform"], m["room"], m["started_at"])
        vexa_meeting_id = m["upstream_id"]
    except (KeyError, ValueError, TypeError):
        return None
    return folder, {
        "state": "failed",
        "error": error,
        "attempts": attempts,
        "meeting_id": m.get("id"),
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
            except NotAV2Meeting as exc:
                attempts = queue.record_failure(
                    meeting_id,
                    str(exc),
                    backoff_seconds=settings.retry_backoff_seconds,
                    now=now,
                )
                logger.error(
                    "export job failed: not a v2 meeting meeting_id=%s "
                    "attempts=%s; moved to failed/, nothing exported",
                    meeting_id,
                    attempts,
                )
                queue.fail(meeting_id)
            except Exception as exc:  # noqa: BLE001 - contained per id, logged
                attempts = queue.record_failure(
                    meeting_id,
                    str(exc),
                    backoff_seconds=settings.retry_backoff_seconds,
                    now=now,
                )
                logger.warning(
                    "export job failed meeting_id=%s attempts=%s error_class=%s",
                    meeting_id,
                    attempts,
                    type(exc).__name__,
                )
                if attempts >= settings.max_attempts:
                    finished = False
                    if isinstance(exc, ExportReportError):
                        logger.error(
                            "quarantine: meeting_id=%s attempts=%s export result not "
                            "accepted; the outcome in _export.json stands, moved to "
                            "failed/",
                            meeting_id,
                            attempts,
                        )
                    else:
                        finished = await _quarantine(
                            envelope, attempts, str(exc), meeting_id, deps
                        )
                    if finished:
                        queue.done(meeting_id)
                    else:
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


async def _quarantine(
    envelope: dict[str, Any], attempts: int, error: str, meeting_id: str, deps: Deps
) -> bool:
    """Record the quarantined outcome and report it once. A folder whose
    `_export.json` is already `handed_off` (notetaker-worker has it) keeps
    that marker and reports `handed_off`; any other gets the `failed` marker
    and reports `failed`. If the marker can't be read, nothing is written or
    reported. The queue's retry budget for this meeting is spent, so any of
    these left undone is logged for an operator's re-enqueue, which runs the
    whole job again. True when the item is finished: the folder was handed
    off and that report was accepted; the caller then completes the item
    instead of moving it to failed/."""
    settings = deps.settings
    marker = _quarantine_marker(envelope, attempts, error)
    if marker is None:
        logger.error(
            "quarantine: meeting_id=%s attempts=%s - envelope too "
            "malformed to derive an export folder, skipping marker",
            meeting_id,
            attempts,
        )
        return False
    folder, body = marker
    base = settings.export_prefix + folder + "/"
    marker_key = base + "_export.json"
    try:
        existing = deps.storage.get_json(settings.export_bucket, marker_key)
    except Exception as exc:  # noqa: BLE001 - logged; the marker is left alone
        logger.error(
            "quarantine: meeting_id=%s attempts=%s could not read _export.json "
            "(error_class=%s); marker left as it is, moved to failed/ for an "
            "operator re-enqueue",
            meeting_id,
            attempts,
            type(exc).__name__,
        )
        return False
    state: ReportState
    report_error: str | None
    if isinstance(existing, dict) and existing.get("state") == "handed_off":
        state, report_error = "handed_off", None
        logger.warning(
            "quarantine: meeting_id=%s attempts=%s folder already handed off; "
            "marker kept, reporting handed_off",
            meeting_id,
            attempts,
        )
    else:
        state, report_error = "failed", error
        deps.storage.put_json(
            settings.export_bucket, marker_key, body, retention=METADATA
        )
        logger.error(
            "quarantine: meeting_id=%s attempts=%s moved to failed/",
            meeting_id,
            attempts,
        )
    meeting_uuid = body["meeting_id"]
    if not meeting_uuid:
        return False
    try:
        await asyncio.to_thread(
            deps.export_result.report,
            str(meeting_uuid),
            state,
            f"s3://{settings.export_bucket}/{base}",
            report_error,
        )
    except ExportReportError as exc:
        logger.error(
            "quarantine: meeting_id=%s export result not accepted (%s); "
            "moved to failed/",
            meeting_id,
            exc,
        )
        return False
    return state == "handed_off"


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
