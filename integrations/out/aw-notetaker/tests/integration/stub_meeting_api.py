"""Stand-in gateway for the compose integration test (task 10, design §1.9).

Serves what the exporter reaches through the gateway in this scenario, each
only with the exporter's key (`X-API-Key`): `GET /recordings` and
`GET /recordings/{id}/master` (spec §4.2 steps 2-3), and the export result
`POST /v2/meetings/{id}/export`, whose calls it records in `app.state.reports`.
Runs via uvicorn on the host; the exporter container under test reaches it at
`host.docker.internal:<port>`.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request


def create_app(
    recordings: list[dict[str, Any]], storage_paths: dict[int, str], api_key: str
) -> FastAPI:
    app = FastAPI()
    app.state.reports = []

    def check(key: str | None) -> None:
        if key != api_key:
            raise HTTPException(status_code=401, detail="invalid api key")

    @app.get("/recordings")
    async def list_recordings(
        x_api_key: str | None = Header(default=None),
    ) -> dict[str, Any]:
        check(x_api_key)
        return {"recordings": recordings}

    @app.get("/recordings/{recording_id}/master")
    async def master(
        recording_id: int, x_api_key: str | None = Header(default=None)
    ) -> dict[str, Any]:
        check(x_api_key)
        return {"storage_path": storage_paths[recording_id]}

    @app.post("/v2/meetings/{meeting_id}/export")
    async def export(
        meeting_id: str, request: Request, x_api_key: str | None = Header(default=None)
    ) -> dict[str, Any]:
        check(x_api_key)
        app.state.reports.append({"meeting_id": meeting_id, **(await request.json())})
        return {"id": meeting_id}

    return app
