"""Process entrypoint: build dependencies from the environment and serve
(spec §4.1, §4.4). S3 access is via IRSA — no static keys."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import boto3
import httpx
import uvicorn

from exporter.app import create_app
from exporter.audio import join_webm, webm_to_wav
from exporter.config import Settings
from exporter.export_result import ExportReporter
from exporter.job import Deps
from exporter.notetaker import Notetaker
from exporter.queue import PendingQueue
from exporter.storage import Storage
from exporter.vexa_client import MeetingApi


def build_deps(settings: Settings) -> Deps:
    """The production dependencies, shared by the server and `exporter.rerun`."""
    s3_client = boto3.client("s3", region_name=os.environ.get("AWS_REGION"))
    storage = Storage(s3_client)
    http_client = httpx.Client(timeout=30)
    return Deps(
        settings=settings,
        storage=storage,
        meeting_api=MeetingApi(
            settings.gateway_url, settings.exporter_api_key, http_client
        ),
        notetaker=Notetaker(settings.notetaker_url, http_client),
        export_result=ExportReporter(
            settings.gateway_url, settings.exporter_api_key, http_client
        ),
        transcode=webm_to_wav,
        now=lambda: datetime.now(timezone.utc),
        join_webm=join_webm,
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = Settings.from_env(os.environ)
    deps = build_deps(settings)
    queue = PendingQueue(deps.storage, settings.vexa_bucket)
    app = create_app(settings, queue, deps)
    uvicorn.run(app, host="0.0.0.0", port=8080)


if __name__ == "__main__":
    main()
