from __future__ import annotations

from typing import Any

from notetaker_common.notetaker_client import NoteTakerClient
from notetaker_common.schemas import BotJob, ProcessRequest

__all__ = ["NoteTakerClientWrapper"]


class NoteTakerClientWrapper:
    """Thin wrapper that builds a ProcessRequest from BotJob and delegates (§13.15)."""

    def __init__(self, client: NoteTakerClient | None = None) -> None:
        self._client = client or NoteTakerClient()

    async def post_process(self, job: BotJob) -> dict[str, Any]:
        request = ProcessRequest(
            meeting_id=job.meeting_id,
            s3_path=job.s3_key,
            room_name=job.join.url,
            language=job.language_hint,
            platform=job.platform,
            idempotency_key=job.job_id,
        )
        return await self._client.post_process(request, idempotency_key=job.job_id)
