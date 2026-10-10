"""Durable pending-export queue on S3, and the sweep worker that drains it
(spec §4.1).

Pending objects live in the VEXA bucket under `aw-exporter/pending/<id>.json`
(`<id>` is the meeting's UUID) as `{"envelope": {...}, "attempts": int,
"next_attempt_at": epoch seconds float, "last_error": str | None,
"rerun": bool, "crashes": int, "lease": {"owner", "expires_at"} | absent}`; a
restart re-lists that prefix, so an in-flight job always resumes. `rerun` (queued by `exporter.rerun`) makes the job export the
meeting again and hand it to notetaker-worker as a rerun; a webhook
redelivered for the same meeting keeps it. Failed ones
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

Crashes: a job is leased before it starts (`lease`, renewed every third of
`EXPORT_LEASE_SECONDS` while it runs) and the lease is cleared when the job
ends either way. A lease found expired means the run that held it stopped
without finishing (the pod was killed or ran out of memory, which raises
nothing): that counts one crash, and at `EXPORT_MAX_CRASHES` the meeting is
quarantined like one out of attempts and never runs again on its own. A
worker that is stopped releases the leases of the jobs it is running, so a
deploy is not a crash.

The worker is a continuous pool: every `EXPORT_SWEEP_SECONDS` it starts each
pending meeting that is not already running and is due, up to
`EXPORT_CONCURRENCY` jobs at once; one long export never holds back the
others.

The worker must never die: a bad envelope, a broken S3 call for one id, or
an unexpected exception anywhere is logged and contained to that id so every
other pending meeting keeps draining.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
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

    def enqueue(self, envelope: dict[str, Any], *, rerun: bool = False) -> None:
        """Durably enqueue `envelope`. A redelivery of an id already pending
        refreshes the stored envelope only — attempts/next_attempt_at/
        last_error are left alone, so a redelivered webhook never resets an
        in-progress backoff, and a pending rerun stays a rerun."""
        meeting_id = str(envelope["data"]["meeting"]["id"])
        existing = self.load(meeting_id)
        if existing is not None:
            existing["envelope"] = envelope
            existing["rerun"] = bool(existing.get("rerun")) or rerun
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
                "rerun": rerun,
            },
        )
        logger.info("enqueue: new pending meeting_id=%s rerun=%s", meeting_id, rerun)

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
        item.pop("lease", None)
        item["next_attempt_at"] = now() + backoff_seconds * 2**attempts
        self._storage.put_json(self._bucket, self._pending_key(meeting_id), item)
        return attempts

    def done(self, meeting_id: str) -> None:
        self._storage.delete(self._bucket, self._pending_key(meeting_id))

    def fail(self, meeting_id: str) -> None:
        item = self.load(meeting_id)
        if item is not None:
            item.pop("lease", None)
            self._storage.put_json(self._bucket, self._failed_key(meeting_id), item)
        self._storage.delete(self._bucket, self._pending_key(meeting_id))

    def lease(
        self, meeting_id: str, owner: str, *, now: float, lease_seconds: float
    ) -> bool:
        """Take or renew `owner`'s lease until `now` + `lease_seconds`. False
        when the item is gone or another owner's lease has not expired."""
        item = self.load(meeting_id)
        if item is None:
            return False
        held = item.get("lease")
        if (
            held is not None
            and held.get("owner") != owner
            and float(held.get("expires_at") or 0.0) > now
        ):
            return False
        item["lease"] = {"owner": owner, "expires_at": now + lease_seconds}
        self._storage.put_json(self._bucket, self._pending_key(meeting_id), item)
        return True

    def release(self, meeting_id: str, owner: str) -> None:
        """Drop `owner`'s lease without counting anything (a stopped worker)."""
        item = self.load(meeting_id)
        if item is not None and (item.get("lease") or {}).get("owner") == owner:
            item.pop("lease")
            self._storage.put_json(self._bucket, self._pending_key(meeting_id), item)

    def record_crash(self, meeting_id: str) -> int:
        """Count a run that stopped without finishing; clears its lease."""
        item = self.load(meeting_id)
        if item is None:
            raise KeyError(meeting_id)
        crashes = int(item.get("crashes") or 0) + 1
        item["crashes"] = crashes
        item.pop("lease", None)
        self._storage.put_json(self._bucket, self._pending_key(meeting_id), item)
        return crashes


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


Job = Callable[[dict[str, Any], Deps, bool], ExportResult]


async def _run_item(
    queue: PendingQueue,
    deps: Deps,
    meeting_id: str,
    job: Job,
    now: Callable[[], float],
    semaphore: asyncio.Semaphore,
    owner: str,
) -> None:
    """One pending meeting: count a crash its last run left behind, then run
    it when due, under a lease, and record the outcome."""
    settings = deps.settings
    try:
        item = queue.load(meeting_id)
        if item is None:
            return
        lease = item.get("lease")
        if lease is not None:
            if float(lease.get("expires_at") or 0.0) > now():
                return  # running
            crashes = queue.record_crash(meeting_id)
            logger.error(
                "export run stopped without finishing meeting_id=%s crashes=%s",
                meeting_id,
                crashes,
            )
            if crashes >= settings.max_crashes:
                error = (
                    f"the exporter stopped {crashes} times while exporting this "
                    "meeting (pod killed or out of memory)"
                )
                await _give_up(
                    queue,
                    deps,
                    meeting_id,
                    item["envelope"],
                    int(item.get("attempts") or 0),
                    error,
                )
                return
        if float(item.get("next_attempt_at") or 0.0) > now():
            return
        envelope: dict[str, Any] = item["envelope"]
        async with semaphore:
            if not queue.lease(
                meeting_id, owner, now=now(), lease_seconds=settings.lease_seconds
            ):
                return
            renewing = asyncio.create_task(
                _renew(queue, meeting_id, owner, settings.lease_seconds, now)
            )
            try:
                await asyncio.to_thread(job, envelope, deps, bool(item.get("rerun")))
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
                return
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
                    if isinstance(exc, ExportReportError):
                        logger.error(
                            "quarantine: meeting_id=%s attempts=%s export result not "
                            "accepted; the outcome in _export.json stands, moved to "
                            "failed/",
                            meeting_id,
                            attempts,
                        )
                        queue.fail(meeting_id)
                    else:
                        await _give_up(
                            queue, deps, meeting_id, envelope, attempts, str(exc)
                        )
                return
            finally:
                renewing.cancel()
            queue.done(meeting_id)
    except Exception:  # noqa: BLE001 - one id's bug must not sink the worker
        logger.exception("unhandled error processing meeting_id=%s", meeting_id)


async def _renew(
    queue: PendingQueue,
    meeting_id: str,
    owner: str,
    lease_seconds: float,
    now: Callable[[], float],
) -> None:
    while True:
        await asyncio.sleep(lease_seconds / 3)
        try:
            await asyncio.to_thread(
                queue.lease, meeting_id, owner, now=now(), lease_seconds=lease_seconds
            )
        except Exception:  # noqa: BLE001 - the next renewal tries again
            logger.exception("lease renewal failed meeting_id=%s", meeting_id)


async def _give_up(
    queue: PendingQueue,
    deps: Deps,
    meeting_id: str,
    envelope: dict[str, Any],
    attempts: int,
    error: str,
) -> None:
    """Quarantine a meeting that is out of attempts or crashes."""
    finished = await _quarantine(envelope, attempts, error, meeting_id, deps)
    if finished:
        queue.done(meeting_id)
    else:
        queue.fail(meeting_id)


async def sweep_once(
    queue: PendingQueue,
    deps: Deps,
    job: Job = export_meeting,
    now: Callable[[], float] = time.time,
) -> None:
    """Run every pending meeting that is due, and wait for all of them."""
    semaphore = asyncio.Semaphore(deps.settings.concurrency)
    owner = uuid.uuid4().hex
    await asyncio.gather(
        *(
            _run_item(queue, deps, meeting_id, job, now, semaphore, owner)
            for meeting_id in queue.pending_ids()
        ),
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


async def run_worker(
    queue: PendingQueue,
    deps: Deps,
    stop: asyncio.Event,
    job: Job = export_meeting,
    now: Callable[[], float] = time.time,
) -> None:
    """Continuous pool: every `sweep_seconds` start each pending meeting that
    is not already running here; at most `concurrency` export at once. On
    stop, release the leases of the jobs still running, so the next worker
    runs them again without counting a crash."""
    semaphore = asyncio.Semaphore(deps.settings.concurrency)
    owner = uuid.uuid4().hex
    running: dict[str, asyncio.Task[None]] = {}
    while not stop.is_set():
        running = {m: t for m, t in running.items() if not t.done()}
        try:
            for meeting_id in await asyncio.to_thread(queue.pending_ids):
                if meeting_id not in running:
                    running[meeting_id] = asyncio.create_task(
                        _run_item(queue, deps, meeting_id, job, now, semaphore, owner)
                    )
        except Exception:  # noqa: BLE001 - the worker loop must never die
            logger.exception("run_worker: listing pending meetings failed; continuing")
        try:
            await asyncio.wait_for(stop.wait(), timeout=deps.settings.sweep_seconds)
        except asyncio.TimeoutError:
            pass
    for meeting_id, task in running.items():
        if task.done():
            continue
        try:
            await asyncio.to_thread(queue.release, meeting_id, owner)
        except Exception:  # noqa: BLE001 - its lease expires on its own
            logger.exception("run_worker: could not release meeting_id=%s", meeting_id)
