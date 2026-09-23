"""Synthetic speaker-activity builders for tests."""

import json
import wave
from pathlib import Path


def header(lane: str = "gmeet") -> str:
    return json.dumps(
        {
            "type": "speaker_activity_header",
            "v": 1,
            "session_uid": "test-session",
            "platform": "google_meet",
            "lane": lane,
            "native_meeting_id": "test-native",
            "started_at": "2026-01-01T00:00:00.000Z",
            "image_version": "test",
        }
    )


def frame(t: int, name: str | None, rms: float, ch: int = 0, dur_ms: int = 256) -> str:
    row: dict[str, object] = {"t": t, "ch": ch, "rms": rms, "dur_ms": dur_ms}
    if name is not None:
        row["name"] = name
    return json.dumps(row)


def hint(t: int, name: str, is_end: bool = False) -> str:
    row: dict[str, object] = {"type": "hint", "t": t, "name": name}
    if is_end:
        row["isEnd"] = True
    return json.dumps(row)


def capped(t: int, size: int = 0) -> str:
    return json.dumps({"type": "capped", "t": t, "bytes": size})


def two_speaker_gmeet_lines(origin_ms: int) -> list[str]:
    """A synthetic speaker-activity file: "Speaker Alpha" talks for 768 ms from
    the origin, then "Speaker Beta" for 768 ms from origin + 1500 ms (hangover
    700 ms closes Alpha before Beta starts) -> one interval each."""
    return [
        header(),
        frame(origin_ms, "Speaker Alpha", 0.2),
        frame(origin_ms + 256, "Speaker Alpha", 0.2),
        frame(origin_ms + 512, "Speaker Alpha", 0.2),
        frame(origin_ms + 1500, "Speaker Beta", 0.2),
        frame(origin_ms + 1756, "Speaker Beta", 0.2),
        frame(origin_ms + 2012, "Speaker Beta", 0.2),
    ]


def write_silent_wav(path: Path, duration_s: float, rate: int = 100) -> None:
    """A valid mono 16-bit wav of `duration_s` seconds (low `rate` keeps a
    meeting-length file tiny)."""
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\x00\x00" * int(round(duration_s * rate)))
