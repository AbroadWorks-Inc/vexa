"""Stub notetaker-worker for the compose integration test (task 10).

Records every `POST /process` body it receives so the test can assert the
exporter's hand-off request, matching `aw_exporter.notetaker.Notetaker`'s
spec (§4.2 step 7). Runs via uvicorn on the host; the exporter container
under test reaches it at `host.docker.internal:<port>`.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI


def create_app() -> FastAPI:
    app = FastAPI()
    app.state.calls = []

    @app.post("/process")
    async def process(body: dict[str, Any]) -> dict[str, str]:
        app.state.calls.append(body)
        return {"status": "ok"}

    return app
