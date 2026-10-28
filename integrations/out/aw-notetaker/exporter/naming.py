"""Export folder name — a pure function of the Vexa meeting row (spec §3)."""

from __future__ import annotations

import re
from datetime import datetime, timezone

_UNSAFE = re.compile(r"[^A-Za-z0-9.-]")


def parse_utc(value: str) -> datetime:
    """ISO-8601 -> aware UTC datetime; a naive value is taken as UTC."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def folder_name(platform: str, native_meeting_id: str, start_time: str) -> str:
    if not start_time:
        raise ValueError("meeting has no start_time")
    utc = parse_utc(start_time)
    stamp = utc.strftime("%Y%m%dT%H%M%S") + f"{utc.microsecond // 1000:03d}Z"
    return f"{platform}_{_UNSAFE.sub('-', native_meeting_id)}_{stamp}"
