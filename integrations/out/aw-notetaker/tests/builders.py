"""Synthetic tape builders for tests."""

import json
import wave
from pathlib import Path


def header(lane: str = "gmeet") -> str:
    return json.dumps(
        {
            "type": "captured_signal_header",
            "v": 1,
            "platform": "google_meet",
            "lane": lane,
            "sample_rate": 16000,
            "started_at": "2026-01-01T00:00:00.000Z",
        }
    )


def frame(
    ts: int, name: str | None, rms: float, idx: int = 0, samples: int = 4096
) -> str:
    row: dict[str, object] = {
        "seq": ts,
        "ts": ts,
        "speakerIndex": idx,
        "pcm": "",
        "pcm_len": samples,
        "rms": rms,
        "lane": "gmeet",
    }
    if name is not None:
        row["speakerName"] = name
    return json.dumps(row)


def hint(t: int, name: str, is_end: bool = False) -> str:
    row: dict[str, object] = {"type": "hint", "t": t, "name": name, "lane": "mixed"}
    if is_end:
        row["isEnd"] = True
    return json.dumps(row)


def two_speaker_gmeet_lines(origin_ms: int) -> list[str]:
    """A synthetic gmeet tape: "Speaker Alpha" talks for 768 ms from the
    origin, then "Speaker Beta" for 768 ms from origin + 1500 ms (hangover
    700 ms closes Alpha before Beta starts) -> one interval each."""
    return [
        header(),
        frame(origin_ms, "Speaker Alpha", 0.2, idx=0),
        frame(origin_ms + 256, "Speaker Alpha", 0.2, idx=0),
        frame(origin_ms + 512, "Speaker Alpha", 0.2, idx=0),
        frame(origin_ms + 1500, "Speaker Beta", 0.2, idx=1),
        frame(origin_ms + 1756, "Speaker Beta", 0.2, idx=1),
        frame(origin_ms + 2012, "Speaker Beta", 0.2, idx=1),
    ]


def write_silent_wav(path: Path, duration_s: float, rate: int = 100) -> None:
    """A valid mono 16-bit wav of `duration_s` seconds (low `rate` keeps a
    meeting-length file tiny)."""
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\x00\x00" * int(round(duration_s * rate)))
