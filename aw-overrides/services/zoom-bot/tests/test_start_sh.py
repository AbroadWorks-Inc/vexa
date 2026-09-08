"""Static guards on start.sh."""

from __future__ import annotations

import re
from pathlib import Path


def test_the_sentinel_window_is_wider_than_the_drain_bound() -> None:
    """Two timeouts in two files, and the tighter one silently wins.

    start.sh waits `N x 0.5s` for /tmp/pipeline_done and then KILLS the sidecar.
    The sidecar, before writing that sentinel, waits up to
    `_FULL_AUDIO_DRAIN_TIMEOUT_S` for the playback encode. If start.sh's window
    is the smaller of the two, the sidecar's bound is unreachable -- the number
    in the Python is not the number that fires, and the encode dies mid-write
    with the artifact silently absent.

    This is the same defect shape as a generous-looking termination grace being
    shadowed by a tighter timeout upstream. Pin the RELATIONSHIP, not either
    number, so raising one without the other fails here.
    """
    import aw_output_hook as hook

    script = (Path(__file__).resolve().parent.parent / "start.sh").read_text()
    m = re.search(r"for _ in \$\(seq 1 (\d+)\); do", script)
    assert m, "the sentinel wait loop is gone or reshaped -- re-derive this guard"
    window_s = int(m.group(1)) * 0.5

    assert window_s > hook._FULL_AUDIO_DRAIN_TIMEOUT_S, (
        f"start.sh waits {window_s:.0f}s but the sidecar's drain bound is "
        f"{hook._FULL_AUDIO_DRAIN_TIMEOUT_S:.0f}s -- the drain can never reach its "
        "own timeout, so a long encode is killed rather than abandoned cleanly"
    )
    # ...and enough slack above it for the pipeline that runs BEFORE the drain
    # (concat + a ~461 MB S3 PUT, measured at 40-70s).
    assert window_s - hook._FULL_AUDIO_DRAIN_TIMEOUT_S >= 100, (
        f"only {window_s - hook._FULL_AUDIO_DRAIN_TIMEOUT_S:.0f}s of slack above "
        "the drain bound; the pipeline runs first and needs 40-70s of it"
    )
