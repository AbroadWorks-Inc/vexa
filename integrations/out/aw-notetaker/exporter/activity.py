"""speaker-activity.jsonl -> speaker START/END events (spec §4.3).

The bot always writes this file (no audio, just who-spoke-when); there is no
fallback to the debug capture tape (spec: 2026-09-23-speaker-activity-design.md).
"""

from __future__ import annotations

import heapq
import json
import tempfile
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import IO, Any, Literal

EventType = Literal["SPEAKER_START", "SPEAKER_END"]
Source = Literal["audio", "hint"]
HintKind = Literal["levels", "dominant"]

# Timeline frames held in memory at once; past it they go to disk in sorted runs.
_RUN_FRAMES = 50_000


@dataclass(slots=True, frozen=True)
class Frame:
    ts: int
    name: str | None
    rms: float
    duration_ms: int


@dataclass(slots=True, frozen=True)
class CapturedFrame:
    """One parsed activity frame, named or not, with its channel when present."""

    ts: int
    channel: int | None
    name: str | None
    rms: float
    duration_ms: int


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


def _ts(frame: Frame) -> int:
    return frame.ts


class TimelineFrames:
    """The named frames the timeline is built from, iterated in time order
    without holding them all in memory.

    Up to `_RUN_FRAMES` frames are kept in memory; each full run is sorted by
    `ts` and written to an anonymous temporary file, and iteration merges the
    runs. Both the sort and the merge are stable, so frames with the same `ts`
    keep their file order: iteration yields exactly `sorted(frames, key=ts)`.
    The file's own order is no guide, because `t` comes from several page-side
    clocks (each stream's accumulated audio time, or the page's `Date.now()`)
    and the lanes append their lines as they arrive. Memory is one run, plus
    one frame per run on disk while iterating. One iteration at a time.
    """

    __slots__ = ("_run", "_spilled", "_count", "_names")

    def __init__(self) -> None:
        self._run: list[Frame] = []
        self._spilled: list[IO[str]] = []
        self._count = 0
        self._names: dict[str, None] = {}

    def append(self, frame: Frame) -> None:
        self._count += 1
        if frame.name:
            self._names.setdefault(frame.name)
        self._run.append(frame)
        if len(self._run) >= _RUN_FRAMES:
            run = tempfile.TemporaryFile("w+", encoding="utf-8")
            for f in sorted(self._run, key=_ts):
                run.write(json.dumps([f.ts, f.name, f.rms, f.duration_ms]) + "\n")
            self._spilled.append(run)
            self._run = []

    def __len__(self) -> int:
        return self._count

    def names(self) -> list[str]:
        """Distinct names in first-seen (file) order."""
        return list(self._names)

    def __iter__(self) -> Iterator[Frame]:
        runs: list[Iterable[Frame]] = [_read_run(run) for run in self._spilled]
        runs.append(sorted(self._run, key=_ts))
        return heapq.merge(*runs, key=_ts)

    def close(self) -> None:
        """Drop the frames and the files they were written to."""
        for run in self._spilled:
            run.close()
        self._spilled = []
        self._run = []


def _read_run(run: IO[str]) -> Iterator[Frame]:
    run.seek(0)
    for line in run:
        ts, name, rms, duration_ms = json.loads(line)
        yield Frame(ts=ts, name=name, rms=rms, duration_ms=duration_ms)


@dataclass(slots=True)
class Activity:
    """One parsed file.

    `frames` holds named frames on every lane, in time order, on disk past
    `_RUN_FRAMES` (`close` drops them); speech events are built from those
    only. Every frame that parsed, including unnamed ones and the channel
    number, goes to `parse_activity`'s `capture` as it is read (for the
    per-channel transcript), and is not kept. On the mixed lane (Jitsi, Teams:
    one combined stream) a frame that carries a channel belongs to that
    channel's recording, not to the mix: it is captured only, never in
    `frames`.
    """

    lane: str
    started_at: str | None
    frames: TimelineFrames = field(default_factory=TimelineFrames)
    hints: list[Hint] = field(default_factory=list)
    frame_count: int = 0
    capped: bool = False

    def close(self) -> None:
        self.frames.close()


def parse_activity(
    lines: Iterable[str], capture: Callable[[CapturedFrame], None] | None = None
) -> Activity:
    """Parse speaker-activity.v1 lines into an Activity, handing every frame
    that parsed to `capture` as it is read.

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
            if capture is not None:
                capture(
                    CapturedFrame(
                        ts=parsed.ts,
                        channel=channel,
                        name=parsed.name,
                        rms=parsed.rms,
                        duration_ms=parsed.duration_ms,
                    )
                )
            # Named frames are the timeline on every lane. On the mixed lane
            # only the mix's own frames (no channel) are; unnamed frames stay
            # out of `frames` everywhere.
            if parsed.name and not (activity.lane == "mixed" and channel is not None):
                activity.frames.append(parsed)
    if activity is None:
        raise ValueError("speaker-activity file has no speaker_activity_header")
    return activity


def names(activity: Activity) -> list[str]:
    """Return distinct named speakers in first-seen order."""
    seen = dict.fromkeys(activity.frames.names())
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

    for f in activity.frames:
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
