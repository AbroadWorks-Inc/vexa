"""webm -> wav transcode (spec §4.2 step 4).

`-af aresample=async=1:first_pts=0` is mandatory, not a plain decode: a plain
`ffmpeg -i ... -ac 1 -ar 16000` drops Opus DTX (discontinuous transmission)
gaps, non-linearly compressing the wav timeline — measured 34 s lost on a
32-min meeting (docs/2026-09-23-aw-rearchitecture-design.md §4.2 step 4),
which silently shifts every downstream speaker-interval timestamp computed
relative to this wav's t=0.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

_STDERR_TAIL_LINES = 20


def webm_to_wav(
    src: Path,
    dst: Path,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    result = run(
        [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-i",
            str(src),
            "-af",
            "aresample=async=1:first_pts=0",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(dst),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        tail = "\n".join(result.stderr.splitlines()[-_STDERR_TAIL_LINES:])
        raise RuntimeError(f"ffmpeg exited {result.returncode}: {tail}")
