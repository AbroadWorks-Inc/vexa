"""speaker-activity.jsonl -> speaker START/END events (spec §4.3).

The bot always writes this file (no audio, just who-spoke-when); there is no
fallback to the debug capture tape (spec: 2026-09-23-speaker-activity-design.md).
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

EventType = Literal["SPEAKER_START", "SPEAKER_END"]
Source = Literal["audio", "hint"]


@dataclass(slots=True, frozen=True)
class Frame:
    ts: int
    name: str | None
    rms: float
    duration_ms: int


@dataclass(slots=True, frozen=True)
class Hint:
    t: int
    name: str
    is_end: bool


@dataclass(slots=True, frozen=True)
class ActivityEvent:
    name: str
    relative_ms: int
    event_type: EventType
    source: Source


@dataclass(slots=True)
class Activity:
    """One parsed file. `frames` holds only what `speech_events` reads — named
    frames on the gmeet lane; every valid frame line is counted in
    `frame_count`, so a long meeting's unused frames never sit in memory."""

    lane: str
    started_at: str | None
    frames: list[Frame] = field(default_factory=list)
    hints: list[Hint] = field(default_factory=list)
    frame_count: int = 0
    capped: bool = False


def parse_activity(lines: Iterable[str]) -> Activity:
    """Parse speaker-activity.v1 lines into an Activity.

    Skips unparseable lines silently. Raises ValueError if no header found.
    A header-only file (the bot left before anyone spoke) is a valid, empty
    Activity — not an error. A `{"type":"capped"}` line sets `capped=True`
    and ends parsing; nothing after it was written by the bot either.
    """
    activity: Activity | None = None
    for line in lines:
        try:
            row: dict[str, Any] = json.loads(line)
        except (ValueError, TypeError):
            continue
        line_type = row.get("type")
        if line_type == "speaker_activity_header":
            activity = Activity(
                lane=str(row.get("lane", "gmeet")),
                started_at=row.get("started_at"),
            )
            continue
        if activity is None:
            continue
        if line_type == "capped":
            activity.capped = True
            break
        if line_type == "hint":
            if row.get("name"):
                try:
                    activity.hints.append(
                        Hint(
                            int(row["t"]),
                            str(row["name"]),
                            bool(row.get("isEnd", False)),
                        )
                    )
                except (KeyError, ValueError, TypeError):
                    continue
            continue
        if line_type is None and "t" in row:
            try:
                parsed = Frame(
                    ts=int(row["t"]),
                    name=row.get("name") or None,
                    rms=float(row["rms"]),
                    duration_ms=int(row["dur_ms"]),
                )
            except (KeyError, ValueError, TypeError):
                continue
            activity.frame_count += 1
            if activity.lane != "mixed" and parsed.name:
                activity.frames.append(parsed)
    if activity is None:
        raise ValueError("speaker-activity file has no speaker_activity_header")
    return activity


def names(activity: Activity) -> list[str]:
    """Return distinct named speakers in first-seen order."""
    seen: dict[str, None] = {}
    for f in activity.frames:
        if f.name:
            seen.setdefault(f.name)
    for h in activity.hints:
        seen.setdefault(h.name)
    return list(seen)


def speech_events(
    activity: Activity, origin_ms: int, rms_threshold: float, hangover_ms: int
) -> list[ActivityEvent]:
    """Extract speaker START/END events from activity.

    If lane is "mixed", emits point events from hints only (spec §4.3).
    Otherwise analyzes frames using RMS threshold and hangover duration.
    Returns events sorted by relative_ms, then name.
    """
    if activity.lane == "mixed":
        out = [
            ActivityEvent(
                h.name,
                h.t - origin_ms,
                "SPEAKER_END" if h.is_end else "SPEAKER_START",
                "hint",
            )
            for h in activity.hints
        ]
        return sorted(out, key=lambda e: (e.relative_ms, e.name))

    events: list[ActivityEvent] = []
    started: dict[str, int] = {}  # name -> start epoch ms
    last_voiced: dict[str, int] = {}  # name -> end of last voiced frame, epoch ms

    def close(name: str) -> None:
        events.append(
            ActivityEvent(name, last_voiced[name] - origin_ms, "SPEAKER_END", "audio")
        )
        del started[name]

    for f in sorted(activity.frames, key=lambda fr: fr.ts):
        for name in [n for n in started if f.ts - last_voiced[n] >= hangover_ms]:
            close(name)
        if not f.name or f.rms < rms_threshold:
            continue
        if f.name not in started:
            started[f.name] = f.ts
            events.append(
                ActivityEvent(f.name, f.ts - origin_ms, "SPEAKER_START", "audio")
            )
        last_voiced[f.name] = f.ts + f.duration_ms
    for name in list(started):
        close(name)
    return sorted(events, key=lambda e: (e.relative_ms, e.name))
