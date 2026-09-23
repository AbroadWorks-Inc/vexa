"""Export folder name — a pure function of the Vexa meeting row (spec §3)."""

from __future__ import annotations

import re
from datetime import datetime, timezone

_UNSAFE = re.compile(r"[^A-Za-z0-9.-]")


def folder_name(platform: str, native_meeting_id: str, start_time: str) -> str:
    if not start_time:
        raise ValueError("meeting has no start_time")
    started = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    utc = started.astimezone(timezone.utc)
    stamp = utc.strftime("%Y%m%dT%H%M%S") + f"{utc.microsecond // 1000:03d}Z"
    return f"{platform}_{_UNSAFE.sub('-', native_meeting_id)}_{stamp}"
