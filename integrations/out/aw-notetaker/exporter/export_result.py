"""The export result, reported to meeting-api through the gateway (design §1.9).

`POST {GATEWAY_URL}/v2/meetings/{uuid}/export` with the exporter's own key
(`X-API-Key`, scope `export`) and the body
`{"state": "handed_off" | "failed", "s3_path": "s3://<bucket>/<folder>/",
"error": "..."}` (`error` only on `failed`, cut to `ERROR_MAX_CHARS`, the
longest meeting-api stores). meeting-api stores it on the
meeting and emits `export.handed_off` / `export.failed`; repeating an accepted
report changes nothing there, so a report may be sent as often as needed.

A report is accepted on a 2xx answer. Anything else (another status, a
connection error, a timeout) raises `ExportReportError` after one request:
the retry belongs to the durable queue, which runs the failed job step again
after its backoff (`queue.py`), up to `EXPORT_MAX_ATTEMPTS`.
"""

from __future__ import annotations

from typing import Literal, Protocol
from urllib.parse import quote

import httpx

_TIMEOUT_S = 30.0
ERROR_MAX_CHARS = 2000

ReportState = Literal["handed_off", "failed"]


class ExportReportError(Exception):
    """The gateway did not accept the export result; retryable."""


class ExportResultPort(Protocol):
    def report(
        self,
        meeting_uuid: str,
        state: ReportState,
        s3_path: str,
        error: str | None = None,
    ) -> None: ...


class ExportReporter:
    def __init__(self, gateway_url: str, api_key: str, http: httpx.Client) -> None:
        self._base_url = gateway_url.rstrip("/")
        self._api_key = api_key
        self._http = http

    def report(
        self,
        meeting_uuid: str,
        state: ReportState,
        s3_path: str,
        error: str | None = None,
    ) -> None:
        path = f"/v2/meetings/{quote(meeting_uuid, safe='')}/export"
        body: dict[str, str] = {"state": state, "s3_path": s3_path}
        if error is not None:
            body["error"] = error[:ERROR_MAX_CHARS]
        try:
            resp = self._http.post(
                f"{self._base_url}{path}",
                json=body,
                headers={"X-API-Key": self._api_key},
                timeout=_TIMEOUT_S,
            )
        except httpx.TransportError as exc:
            raise ExportReportError(
                f"gateway transport error {type(exc).__name__} {path}"
            ) from exc
        if resp.status_code // 100 != 2:
            raise ExportReportError(f"gateway {resp.status_code} {path}")
