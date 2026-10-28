"""Export finished meetings again and redo their transcripts — one command, run in
the exporter pod:

    kubectl -n aw-bots exec deploy/aw-exporter -- python -m exporter.rerun <meeting-id> [...]

Each id is the meeting's aw-bots UUID. The meeting is read from aw-bots as it is
now (`GET /v2/meetings/{id}`) and queued with `rerun` in the exporter's own
durable queue, so the running worker exports it like any meeting — retries,
backoff and quarantine included — but writes every file again, filling in any an
earlier export could not write, and hands the folder to notetaker-worker's
`/process` with `"rerun": true`, which moves the previous outputs aside and
redoes the transcript.

A meeting that has not finished, had no bot sent, or never had its bot in the
meeting is refused and nothing is queued for it, and so is one notetaker-worker
is transcribing now ("transcription in progress for this meeting"): a rerun
would rewrite the folder under the running job. A meeting still in the
exporter's queue is refused too: "export in progress for this meeting" while
its export has not finished, "rerun already queued" when a rerun is pending or
running; the check and the queuing are one conditional write. The export job
asks notetaker-worker again before it writes anything. An id that can't be
read or queued (aw-bots, notetaker-worker or S3 unreachable or failing, or an
answer that is not a JSON object) fails alone and the others go on. The exit
code is 1 when any id was refused or failed. Access to the pod is the
authority: the exporter opens no endpoint for this.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Mapping, Sequence
from typing import Any

import httpx
from botocore.exceptions import BotoCoreError, ClientError

from exporter.__main__ import build_deps
from exporter.config import Settings
from exporter.job import meeting_is_valid, not_sent
from exporter.notetaker import Notetaker, NotetakerError
from exporter.queue import PendingQueue
from exporter.storage import PreconditionFailed
from exporter.vexa_client import MeetingApi, MeetingApiError

# A finished meeting's status, and the webhook event that finished it.
_FINISHED_EVENT = {"completed": "meeting.completed", "failed": "bot.failed"}
# Why the queue would not take a rerun.
_PENDING = {
    "export_in_progress": "export in progress for this meeting",
    "rerun_queued": "rerun already queued",
}


class NotRerunnable(Exception):
    """The meeting cannot be exported again; the message says why."""


def rerun_envelope(meeting: Mapping[str, Any]) -> dict[str, Any]:
    """The queue envelope for a rerun: the meeting as its finishing webhook carries it."""
    event_type = _FINISHED_EVENT.get(str(meeting.get("status")))
    if event_type is None:
        raise NotRerunnable(f"status {meeting.get('status')!r} is not finished")
    if not_sent(meeting):
        raise NotRerunnable("no bot was sent to it")
    if not meeting.get("started_at"):
        raise NotRerunnable("its bot was never in the meeting")
    if not meeting_is_valid(meeting):
        raise NotRerunnable("it lacks a field the export needs")
    return {"event_type": event_type, "data": {"meeting": dict(meeting)}}


def request_reruns(
    meeting_ids: Sequence[str],
    meeting_api: MeetingApi,
    queue: PendingQueue,
    notetaker: Notetaker,
) -> list[str]:
    """Queue a rerun of each meeting; returns the ids that were refused or
    failed."""
    refused = []
    for meeting_id in meeting_ids:
        try:
            envelope = rerun_envelope(meeting_api.meeting(meeting_id))
            if notetaker.transcribing(meeting_id):
                raise NotRerunnable("transcription in progress for this meeting")
            pending = queue.request_rerun(envelope)
            if pending != "queued":
                raise NotRerunnable(_PENDING[pending])
        except (NotRerunnable, MeetingApiError, NotetakerError) as exc:
            print(f"refused {meeting_id}: {exc}")
            refused.append(meeting_id)
            continue
        except (
            httpx.TransportError,
            ClientError,
            BotoCoreError,
            PreconditionFailed,
        ) as exc:
            print(f"failed {meeting_id}: {type(exc).__name__}: {exc}")
            refused.append(meeting_id)
            continue
        meeting = envelope["data"]["meeting"]
        print(
            f"queued {meeting_id}: {meeting['platform']} {meeting['room']} "
            f"started {meeting['started_at']}"
        )
    return refused


def main(argv: Sequence[str] | None = None) -> int:
    meeting_ids = list(sys.argv[1:] if argv is None else argv)
    if not meeting_ids:
        print("usage: python -m exporter.rerun <meeting-id> [<meeting-id> ...]")
        return 2
    logging.basicConfig(level=logging.INFO)
    settings = Settings.from_env(os.environ)
    deps = build_deps(settings)
    queue = PendingQueue(deps.storage, settings.vexa_bucket)
    refused = request_reruns(meeting_ids, deps.meeting_api, queue, deps.notetaker)
    return 1 if refused else 0


if __name__ == "__main__":
    sys.exit(main())
