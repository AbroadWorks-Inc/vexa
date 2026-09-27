"""Vexa meeting-api reads, through the gateway with the exporter's own key
(design §1.9, §1.10).

Every request carries `X-API-Key: <EXPORTER_API_KEY>` (scopes `tx` +
`export`); the gateway checks the scope and tells meeting-api which account
is calling, so the exporter reads the meetings of its key's account.

Routes (forwarded by the gateway to meeting-api/src/meeting_api):
GET /recordings, GET /recordings/{id}/master,
GET /transcripts/by-id/{meeting_id} (spec §4.2 steps 2-3; by-id chosen over
/transcripts/{platform}/{native_id} because one room link can host several
meetings — by-id is exact).
"""

from __future__ import annotations

from typing import Any

import httpx

_TIMEOUT_S = 30.0


class MeetingApiError(Exception):
    def __init__(self, status: int, path: str) -> None:
        super().__init__(f"meeting-api {status} {path}")
        self.status = status
        self.path = path


class MeetingApi:
    def __init__(self, gateway_url: str, api_key: str, http: httpx.Client) -> None:
        self._base_url = gateway_url.rstrip("/")
        self._api_key = api_key
        self._http = http

    def _request(self, path: str, params: dict[str, Any] | None) -> httpx.Response:
        return self._http.get(
            f"{self._base_url}{path}",
            headers={"X-API-Key": self._api_key},
            params=params,
            timeout=_TIMEOUT_S,
        )

    def _get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        resp = self._request(path, params)
        if resp.status_code // 100 != 2:
            raise MeetingApiError(resp.status_code, path)
        return resp

    def list_recordings(self, meeting_id: int) -> list[dict[str, Any]]:
        data = self._get("/recordings", {"meeting_id": meeting_id}).json()
        return list(data["recordings"])

    def master(self, recording_id: int) -> dict[str, Any]:
        path = f"/recordings/{recording_id}/master"
        data = self._get(path, {"type": "audio"}).json()
        return dict(data)

    def transcript(self, meeting_id: int) -> dict[str, Any] | None:
        path = f"/transcripts/by-id/{meeting_id}"
        resp = self._request(path, None)
        if resp.status_code == 404:
            return None
        if resp.status_code // 100 != 2:
            raise MeetingApiError(resp.status_code, path)
        return dict(resp.json())
