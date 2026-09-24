"""Stub Vexa meeting-api for the compose integration test (task 10).

Serves the two routes `exporter.vexa_client.MeetingApi` needs for this
scenario (spec §4.2 steps 2-3): `GET /recordings` and
`GET /recordings/{id}/master`. Runs via uvicorn on the host; the exporter
container under test reaches it at `host.docker.internal:<port>`.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI


def create_app(recording: dict[str, Any], storage_path: str) -> FastAPI:
    app = FastAPI()

    @app.get("/recordings")
    async def list_recordings() -> dict[str, Any]:
        return {"recordings": [recording]}

    @app.get("/recordings/{recording_id}/master")
    async def master(recording_id: int) -> dict[str, Any]:
        return {"storage_path": storage_path}

    return app
