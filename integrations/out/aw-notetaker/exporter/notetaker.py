"""Hand-off to the existing notetaker-worker's /process (spec §4.2 step 7).

idempotency_key = meeting_id, so a redelivered webhook or a resumed pending job
never double-processes a folder. A rerun (`exporter.rerun`) sends `"rerun": true`:
the worker skips that gate and redoes the transcript. Retries any
httpx.TransportError (connect errors and all timeouts — /process is idempotent
via idempotency_key, so a retry after a timeout is safe) and 5xx (transient);
4xx fails immediately. That includes a rerun's 409 (the meeting's job is still
running), which the durable queue retries with its backoff; `exporter.rerun`
asks `transcribing` first, so a rerun is refused before anything is written.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import httpx

_TIMEOUT_S = 30.0
_BACKOFF_S = (2.0, 4.0, 8.0)


class NotetakerError(Exception):
    pass


class Notetaker:
    def __init__(
        self,
        base_url: str,
        http: httpx.Client,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._http = http
        self._sleep = sleep

    def process(
        self, meeting_id: str, s3_path: str, platform: str, *, rerun: bool = False
    ) -> None:
        body = {
            "meeting_id": meeting_id,
            "s3_path": s3_path,
            "platform": platform,
            "idempotency_key": meeting_id,
            "rerun": rerun,
        }
        tries = len(_BACKOFF_S) + 1
        for attempt in range(tries):
            try:
                resp = self._http.post(
                    f"{self._base_url}/process", json=body, timeout=_TIMEOUT_S
                )
            except httpx.TransportError as exc:
                if attempt < tries - 1:
                    self._sleep(_BACKOFF_S[attempt])
                    continue
                raise NotetakerError(f"transport error: {exc}") from exc
            if resp.status_code // 100 == 2:
                return
            if resp.status_code // 100 == 5:
                if attempt < tries - 1:
                    self._sleep(_BACKOFF_S[attempt])
                    continue
                raise NotetakerError(f"notetaker {resp.status_code} after retries")
            raise NotetakerError(f"notetaker {resp.status_code}")

    def transcribing(self, meeting_id: str) -> bool:
        """Whether notetaker-worker is transcribing the meeting now
        (`GET /status/{id}` reads `processing`). A worker that does not know
        the meeting (404, e.g. after its restart) is not transcribing it."""
        try:
            resp = self._http.get(
                f"{self._base_url}/status/{meeting_id}", timeout=_TIMEOUT_S
            )
        except httpx.TransportError as exc:
            raise NotetakerError(f"transport error: {exc}") from exc
        if resp.status_code == 404:
            return False
        if resp.status_code // 100 != 2:
            raise NotetakerError(f"notetaker {resp.status_code}")
        return bool(resp.json().get("status") == "processing")
