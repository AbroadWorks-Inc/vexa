"""Process entrypoint: build dependencies from the environment and serve
(spec §4.1, §4.4). S3 access is via IRSA — no static keys."""

from __future__ import annotations

import os
from datetime import datetime, timezone

import boto3
import httpx
import uvicorn

from aw_exporter.app import create_app
from aw_exporter.audio import webm_to_wav
from aw_exporter.config import Settings
from aw_exporter.job import Deps
from aw_exporter.notetaker import Notetaker
from aw_exporter.queue import PendingQueue
from aw_exporter.storage import Storage
from aw_exporter.vexa_client import MeetingApi


def main() -> None:
    settings = Settings.from_env(os.environ)
    s3_client = boto3.client("s3", region_name=os.environ.get("AWS_REGION"))
    storage = Storage(s3_client)
    http_client = httpx.Client(timeout=30)
    deps = Deps(
        settings=settings,
        storage=storage,
        meeting_api=MeetingApi(settings.meeting_api_url, http_client),
        notetaker=Notetaker(settings.notetaker_url, http_client),
        transcode=webm_to_wav,
        now=lambda: datetime.now(timezone.utc),
    )
    queue = PendingQueue(storage, settings.vexa_bucket)
    app = create_app(settings, queue, deps)
    uvicorn.run(app, host="0.0.0.0", port=8080)


if __name__ == "__main__":
    main()
