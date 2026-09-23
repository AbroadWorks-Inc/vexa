"""Hand-off to the existing notetaker-worker's /process (spec §4.2 step 7).

idempotency_key = meeting_id, so a redelivered webhook or a resumed pending job
never double-processes a folder. Retries any httpx.TransportError (connect
errors and all timeouts — /process is idempotent via idempotency_key, so a
retry after a timeout is safe) and 5xx (transient); 4xx fails immediately
(the request itself is wrong, retrying won't help).
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

    def process(self, meeting_id: str, s3_path: str, platform: str) -> None:
        body = {
            "meeting_id": meeting_id,
            "s3_path": s3_path,
            "platform": platform,
            "idempotency_key": meeting_id,
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
