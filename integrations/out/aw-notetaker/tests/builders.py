"""Synthetic speaker-activity builders and aw-bots webhook envelopes for tests."""

import io
import json
import wave
from pathlib import Path
from typing import Any

MEETING_UUID = "5f0c2b7e-8d1a-4c3e-9b6f-2a7d1e4c8b90"
EVENT_ID = "evt_" + "7c1e0b0a" * 8


def meeting_v2(**overrides: Any) -> dict[str, Any]:
    """An intake.v1 `Meeting` (design §2.4) as a finished meeting's
    webhook carries it: every key, in order."""
    meeting: dict[str, Any] = {
        "id": MEETING_UUID,
        "upstream_id": 11367,
        "status": "completed",
        "completion_reason": "stopped",
        "failure_stage": None,
        "outcome": None,
        "platform": "google_meet",
        "room": "abc-defg-hij",
        "meeting_url": "https://meet.google.com/abc-defg-hij",
        "title": "Weekly sync",
        "start": "2026-06-18T10:00:00Z",
        "end": "2026-06-18T10:45:00Z",
        "time_zone": "Asia/Kolkata",
        "bot_joins_at": "2026-06-18T09:58:00Z",
        "started_at": "2026-06-18T10:00:00Z",
        "ended_at": "2026-06-18T10:42:00Z",
        "entries": [
            {
                "external_id": "google:3n5kq8example",
                "user": "a@abroadworks.com",
                "attendees": ["a@abroadworks.com", "b@example.com"],
                "series_id": None,
                "metadata": None,
            }
        ],
        "export": None,
        "sequence": 9,
    }
    meeting.update(overrides)
    return meeting


def meeting_event(
    event_type: str = "meeting.completed",
    *,
    event_id: str = EVENT_ID,
    **meeting_overrides: Any,
) -> dict[str, Any]:
    """A webhook.v1 `MeetingEvent`: a subscription delivery (design §2.7)."""
    return {
        "event_id": event_id,
        "event_type": event_type,
        "api_version": "2026-09-25",
        "created_at": "2026-06-18T10:42:00Z",
        "data": {"meeting": meeting_v2(**meeting_overrides)},
    }


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


def hint(t: int, name: str, is_end: bool = False, kind: str | None = None) -> str:
    row: dict[str, object] = {"type": "hint", "t": t, "name": name}
    if is_end:
        row["isEnd"] = True
    if kind is not None:
        row["kind"] = kind
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


def write_constant_wav(
    path: Path, duration_s: float, value: int, rate: int = 100
) -> None:
    """A mono 16-bit wav whose every sample is `value`, so a joined file shows
    which input each span came from."""
    sample = value.to_bytes(2, "little", signed=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(sample * int(round(duration_s * rate)))


def wav_samples(data: bytes) -> tuple[list[int], int]:
    """(every sample, frame rate) of a mono 16-bit wav held as bytes."""
    with wave.open(io.BytesIO(data), "rb") as wav:
        frames = wav.readframes(wav.getnframes())
        rate = wav.getframerate()
    samples = [
        int.from_bytes(frames[i : i + 2], "little", signed=True)
        for i in range(0, len(frames), 2)
    ]
    return samples, rate
