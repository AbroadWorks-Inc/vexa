"""Vexa meeting-api client — trusts X-User-Id like the gateway would inject it.

Routes (meeting-api/src/meeting_api): GET /recordings, GET /recordings/{id}/master,
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
    def __init__(self, base_url: str, http: httpx.Client) -> None:
        self._base_url = base_url.rstrip("/")
        self._http = http

    def _get(
        self, user_id: int, path: str, params: dict[str, Any] | None = None
    ) -> httpx.Response:
        resp = self._http.get(
            f"{self._base_url}{path}",
            headers={"X-User-Id": str(user_id)},
            params=params,
            timeout=_TIMEOUT_S,
        )
        if resp.status_code // 100 != 2:
            raise MeetingApiError(resp.status_code, path)
        return resp

    def list_recordings(self, user_id: int, meeting_id: int) -> list[dict[str, Any]]:
        data = self._get(user_id, "/recordings", {"meeting_id": meeting_id}).json()
        return list(data["recordings"])

    def master(self, user_id: int, recording_id: int) -> dict[str, Any]:
        path = f"/recordings/{recording_id}/master"
        data = self._get(user_id, path, {"type": "audio"}).json()
        return dict(data)

    def transcript(self, user_id: int, meeting_id: int) -> dict[str, Any] | None:
        path = f"/transcripts/by-id/{meeting_id}"
        resp = self._http.get(
            f"{self._base_url}{path}",
            headers={"X-User-Id": str(user_id)},
            timeout=_TIMEOUT_S,
        )
        if resp.status_code == 404:
            return None
        if resp.status_code // 100 != 2:
            raise MeetingApiError(resp.status_code, path)
        return dict(resp.json())
