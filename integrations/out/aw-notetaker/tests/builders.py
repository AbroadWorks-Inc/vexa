"""Synthetic tape builders for tests."""

import json


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
