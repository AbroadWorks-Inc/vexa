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
HintKind = Literal["levels", "dominant"]


@dataclass(slots=True, frozen=True)
class Frame:
    ts: int
    name: str | None
    rms: float
    duration_ms: int


@dataclass(slots=True, frozen=True)
class CapturedFrame:
    """One parsed activity frame, named or not, with its channel when present.

    `source` is the line's `src`: `"channel"` for a per-channel tap frame,
    None for every other frame (the field is absent).
    """

    ts: int
    channel: int | None
    name: str | None
    rms: float
    duration_ms: int
    source: str | None = None


@dataclass(slots=True, frozen=True)
class Hint:
    t: int
    name: str
    is_end: bool
    kind: HintKind | None = None


@dataclass(slots=True, frozen=True)
class ActivityEvent:
    name: str
    relative_ms: int
    event_type: EventType
    source: Source
    kind: HintKind | None = None


@dataclass(slots=True)
class Activity:
    """One parsed file.

    `frames` holds named frames on every lane; speech events are built from
    those only. `captured` holds every frame that parsed, including unnamed
    ones and the channel number, for the per-channel transcript. Channel-tap
    frames (`"src":"channel"`) are kept in `captured` only, never in `frames`:
    on Jitsi they share ch 0 with the mixed lane and must not change it.
    """

    lane: str
    started_at: str | None
    frames: list[Frame] = field(default_factory=list)
    hints: list[Hint] = field(default_factory=list)
    captured: list[CapturedFrame] = field(default_factory=list)
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
                    raw_kind = row.get("kind")
                    kind: HintKind | None = None
                    if raw_kind == "levels":
                        kind = "levels"
                    elif raw_kind == "dominant":
                        kind = "dominant"
                    activity.hints.append(
                        Hint(
                            int(row["t"]),
                            str(row["name"]),
                            bool(row.get("isEnd", False)),
                            kind,
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
            # A missing or non-numeric channel is kept as None. The frame
            # itself still counts: the channel transcript skips it later.
            raw_channel = row.get("ch")
            try:
                channel = None if raw_channel is None else int(raw_channel)
            except (TypeError, ValueError):
                channel = None
            raw_source = row.get("src")
            source = raw_source if isinstance(raw_source, str) else None
            activity.captured.append(
                CapturedFrame(
                    ts=parsed.ts,
                    channel=channel,
                    name=parsed.name,
                    rms=parsed.rms,
                    duration_ms=parsed.duration_ms,
                    source=source,
                )
            )
            # Named frames are the timeline on every lane, including mixed and
            # pertrack. Unnamed frames and channel-tap frames stay out of
            # `frames`.
            if parsed.name and source != "channel":
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

    Named frames win on every lane. Hints are used only when the file stored
    no named frame (Teams' mixed lane, or a Zoom file that never named a
    channel). Returns events sorted by relative_ms, then name.
    """
    if not activity.frames:
        out = [
            ActivityEvent(
                h.name,
                h.t - origin_ms,
                "SPEAKER_END" if h.is_end else "SPEAKER_START",
                "hint",
                h.kind,
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
