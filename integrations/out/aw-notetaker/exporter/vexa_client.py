"""Vexa meeting-api reads, through the gateway with the exporter's own key
(design §1.9, §1.10).

Every request carries `X-API-Key: <EXPORTER_API_KEY>` (scopes `tx` +
`export`); the gateway checks the scope and tells meeting-api which account
is calling, so the exporter reads the meetings of its key's account.

Routes (forwarded by the gateway to meeting-api/src/meeting_api):
GET /v2/meetings/{id} (the §2.4 meeting a webhook carries, for a rerun),
GET /recordings (paged: `limit`/`offset` in, `has_more` out; every page is
read, up to the caller's cap), GET /recordings/{id}/master,
GET /transcripts/by-id/{meeting_id} (spec §4.2 steps 2-3; by-id chosen over
/transcripts/{platform}/{native_id} because one room link can host several
meetings — by-id is exact).
"""

from __future__ import annotations

from typing import Any

import httpx

_TIMEOUT_S = 30.0
# meeting-api's own default page size for GET /recordings.
_RECORDINGS_PAGE = 50


class MeetingApiError(Exception):
    def __init__(self, status: int, path: str) -> None:
        super().__init__(f"meeting-api {status} {path}")
        self.status = status
        self.path = path


class TooManyRecordings(Exception):
    """The meeting has more recordings than the exporter may read; exporting
    part of them would be a folder from part of the meeting."""

    def __init__(self, meeting_id: int, max_recordings: int) -> None:
        super().__init__(
            f"meeting {meeting_id} has more than {max_recordings} recordings"
        )
        self.meeting_id = meeting_id
        self.max_recordings = max_recordings


class RecordingsPagingStalled(Exception):
    """A page added no new recording while `has_more` still said more were
    coming: the paging does not advance, so reading on would never end."""


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

    def list_recordings(
        self, meeting_id: int, max_recordings: int
    ) -> list[dict[str, Any]]:
        """Every recording of the meeting, page by page until `has_more` is
        false. Raises `TooManyRecordings` as soon as more than
        `max_recordings` are read, and `RecordingsPagingStalled` when a page
        adds nothing while more are promised, so the read always ends. A
        recording seen on two pages (the list moved between reads) is listed
        once."""
        recordings: dict[Any, dict[str, Any]] = {}
        offset = 0
        while True:
            data = self._get(
                "/recordings",
                {
                    "meeting_id": meeting_id,
                    "limit": _RECORDINGS_PAGE,
                    "offset": offset,
                },
            ).json()
            page = list(data["recordings"])
            before = len(recordings)
            for rec in page:
                recordings.setdefault(rec.get("id"), rec)
            if len(recordings) > max_recordings:
                raise TooManyRecordings(meeting_id, max_recordings)
            if not data.get("has_more") or not page:
                return list(recordings.values())
            if len(recordings) == before:
                raise RecordingsPagingStalled(
                    f"GET /recordings for meeting {meeting_id} returned nothing "
                    f"new at offset {offset} with has_more"
                )
            offset += len(page)

    def meeting(self, meeting_id: str) -> dict[str, Any]:
        """The meeting as aw-bots holds it now: the same §2.4 meeting a webhook's
        `data.meeting` carries."""
        return dict(self._get(f"/v2/meetings/{meeting_id}").json())

    def master(self, recording_id: int, media_type: str = "audio") -> dict[str, Any]:
        path = f"/recordings/{recording_id}/master"
        data = self._get(path, {"type": media_type}).json()
        return dict(data)

    def transcript(self, meeting_id: int) -> dict[str, Any] | None:
        path = f"/transcripts/by-id/{meeting_id}"
        resp = self._request(path, None)
        if resp.status_code == 404:
            return None
        if resp.status_code // 100 != 2:
            raise MeetingApiError(resp.status_code, path)
        return dict(resp.json())
