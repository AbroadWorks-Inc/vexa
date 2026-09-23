"""captured-signal.v1 tape -> speaker START/END events (spec §4.3)."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

EventType = Literal["SPEAKER_START", "SPEAKER_END"]
Source = Literal["audio", "hint"]


@dataclass(frozen=True)
class Frame:
    ts: int
    name: str | None
    rms: float
    duration_ms: int


@dataclass(frozen=True)
class Hint:
    t: int
    name: str
    is_end: bool


@dataclass(frozen=True)
class TapeEvent:
    name: str
    relative_ms: int
    event_type: EventType
    source: Source


@dataclass
class Tape:
    lane: str
    sample_rate: int
    started_at: str | None
    frames: list[Frame] = field(default_factory=list)
    hints: list[Hint] = field(default_factory=list)


def parse_tape(lines: Iterable[str]) -> Tape:
    """Parse captured-signal.v1 tape from JSONL lines.

    Skips unparseable lines silently. Raises ValueError if no header found.
    """
    tape: Tape | None = None
    for line in lines:
        try:
            row: dict[str, Any] = json.loads(line)
        except (ValueError, TypeError):
            continue
        if row.get("type") == "captured_signal_header":
            sample_rate = int(row.get("sample_rate", 16000))
            if sample_rate <= 0:
                sample_rate = 16000
            tape = Tape(
                lane=str(row.get("lane", "gmeet")),
                sample_rate=sample_rate,
                started_at=row.get("started_at"),
            )
            continue
        if tape is None:
            continue
        if row.get("type") == "hint":
            if row.get("name"):
                try:
                    tape.hints.append(
                        Hint(
                            int(row["t"]),
                            str(row["name"]),
                            bool(row.get("isEnd", False)),
                        )
                    )
                except (KeyError, ValueError, TypeError):
                    continue
            continue
        if "ts" in row:
            try:
                samples = int(row.get("pcm_len", 0))
                tape.frames.append(
                    Frame(
                        ts=int(row["ts"]),
                        name=row.get("speakerName") or None,
                        rms=float(row.get("rms", 0.0)),
                        duration_ms=int(round(samples * 1000 / tape.sample_rate)),
                    )
                )
            except (KeyError, ValueError, TypeError):
                continue
    if tape is None:
        raise ValueError("tape has no captured_signal_header")
    return tape


def names(tape: Tape) -> list[str]:
    """Return distinct named speakers in first-seen order."""
    seen: dict[str, None] = {}
    for f in tape.frames:
        if f.name:
            seen.setdefault(f.name)
    for h in tape.hints:
        seen.setdefault(h.name)
    return list(seen)


def speech_events(
    tape: Tape, origin_ms: int, rms_threshold: float, hangover_ms: int
) -> list[TapeEvent]:
    """Extract speaker START/END events from tape.

    If lane is "mixed", emits point events from hints only (spec §4.3).
    Otherwise analyzes frames using RMS threshold and hangover duration.
    Returns events sorted by relative_ms, then name.
    """
    if tape.lane == "mixed":
        out = [
            TapeEvent(
                h.name,
                h.t - origin_ms,
                "SPEAKER_END" if h.is_end else "SPEAKER_START",
                "hint",
            )
            for h in tape.hints
        ]
        return sorted(out, key=lambda e: (e.relative_ms, e.name))

    events: list[TapeEvent] = []
    started: dict[str, int] = {}  # name -> start epoch ms
    last_voiced: dict[str, int] = {}  # name -> end of last voiced frame, epoch ms

    def close(name: str) -> None:
        events.append(
            TapeEvent(name, last_voiced[name] - origin_ms, "SPEAKER_END", "audio")
        )
        del started[name]

    for f in sorted(tape.frames, key=lambda fr: fr.ts):
        for name in [n for n in started if f.ts - last_voiced[n] >= hangover_ms]:
            close(name)
        if not f.name or f.rms < rms_threshold:
            continue
        if f.name not in started:
            started[f.name] = f.ts
            events.append(TapeEvent(f.name, f.ts - origin_ms, "SPEAKER_START", "audio"))
        last_voiced[f.name] = f.ts + f.duration_ms
    for name in list(started):
        close(name)
    return sorted(events, key=lambda e: (e.relative_ms, e.name))
