"""webm -> wav transcode (spec §4.2 step 4), and the join of a meeting's
bot sessions into one timeline (§6.9 F-K2).

`-af aresample=async=1:first_pts=0` is mandatory, not a plain decode: a plain
`ffmpeg -i ... -ac 1 -ar 16000` drops Opus DTX (discontinuous transmission)
gaps, non-linearly compressing the wav timeline — measured 34 s lost on a
32-min meeting (docs/2026-09-23-aw-rearchitecture-design.md §4.2 step 4),
which silently shifts every downstream speaker-interval timestamp computed
relative to this wav's t=0. The join decodes every part with the same filter.
"""

from __future__ import annotations

import subprocess
import wave
from collections.abc import Callable
from pathlib import Path

_STDERR_TAIL_LINES = 20
_DTX_FILL = "aresample=async=1:first_pts=0"
# The joined master's one format: every part and every silence is converted
# to it, because `concat` needs identical inputs.
_JOIN_FORMAT = "aformat=sample_fmts=flt:sample_rates=48000:channel_layouts=mono"
_COPY_FRAMES = 1 << 16


def _check(result: subprocess.CompletedProcess[str]) -> None:
    if result.returncode != 0:
        tail = "\n".join(result.stderr.splitlines()[-_STDERR_TAIL_LINES:])
        raise RuntimeError(f"ffmpeg exited {result.returncode}: {tail}")


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
            _DTX_FILL,
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
    _check(result)


def join_webm(
    parts: list[tuple[Path, float]],
    dst: Path,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    """One opus webm of `parts` in order, each after its `silence` seconds.

    Each part is decoded with the DTX-filling filter, as `webm_to_wav` does,
    so a part lasts here exactly as long as its wav."""
    chains: list[str] = []
    labels: list[str] = []
    for i, (_, silence) in enumerate(parts):
        if silence > 0:
            chains.append(
                f"anullsrc=r=48000:cl=mono,atrim=duration={silence:.6f},"
                f"{_JOIN_FORMAT}[g{i}]"
            )
            labels.append(f"[g{i}]")
        chains.append(f"[{i}:a]{_DTX_FILL},{_JOIN_FORMAT}[a{i}]")
        labels.append(f"[a{i}]")
    chains.append(f"{''.join(labels)}concat=n={len(labels)}:v=0:a=1[out]")
    inputs = [arg for path, _ in parts for arg in ("-i", str(path))]
    result = run(
        [
            "ffmpeg",
            "-nostdin",
            "-y",
            *inputs,
            "-filter_complex",
            ";".join(chains),
            "-map",
            "[out]",
            "-c:a",
            "libopus",
            str(dst),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    _check(result)


def join_wavs(parts: list[tuple[Path, int]], dst: Path) -> None:
    """One wav of `parts` in order, each after its `silence` frames of
    silence; every part must have the same channels, width and rate."""
    with wave.open(str(dst), "wb") as out:
        params: tuple[int, int, int] | None = None
        for path, silence in parts:
            with wave.open(str(path), "rb") as src:
                part = (src.getnchannels(), src.getsampwidth(), src.getframerate())
                if params is None:
                    params = part
                    out.setnchannels(part[0])
                    out.setsampwidth(part[1])
                    out.setframerate(part[2])
                elif part != params:
                    raise ValueError(
                        f"wav format {part} of {path.name} differs from {params}"
                    )
                frame_bytes = part[0] * part[1]
                while silence > 0:
                    n = min(silence, _COPY_FRAMES)
                    out.writeframes(b"\x00" * (n * frame_bytes))
                    silence -= n
                while chunk := src.readframes(_COPY_FRAMES):
                    out.writeframes(chunk)
