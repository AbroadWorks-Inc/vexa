"""Static guards over the Dockerfile's pip layers.

These are build-time-only failures: no runtime test can see them, and the image
that is live today was built before the base moved to pip 25.3, so nothing looked
broken until the next rebuild.

This bug has now bitten THREE TIMES -- meet-bot (fixed in cc9e758b), teams-bot
(inherited by being modelled on zoom-bot's Dockerfile), and zoom-bot itself, whose
rebuild failed after ~104s with `uninstall-no-record-file`. Hence a guard rather
than a fourth fix.
"""

from pathlib import Path

import pytest

_DOCKERFILE = Path(__file__).resolve().parent.parent / "Dockerfile"


@pytest.fixture()
def dockerfile() -> str:
    assert _DOCKERFILE.is_file(), f"Dockerfile not found at {_DOCKERFILE}"
    return _DOCKERFILE.read_text()


def _pip_line(dockerfile: str, package: str) -> str:
    line = next(
        (
            ln
            for ln in dockerfile.splitlines()
            if ln.startswith("RUN pip install") and package in ln
        ),
        None,
    )
    assert line is not None, f"no `RUN pip install ... {package}` line found"
    return line


def test_notetaker_common_uses_ignore_installed(dockerfile: str) -> None:
    """Without the flag, pip 25.3 refuses to uninstall the apt-managed
    typing_extensions that pydantic==2.5.0 needs to replace, and the build dies."""
    line = _pip_line(dockerfile, "./notetaker-common")
    assert "--ignore-installed" in line, (
        "notetaker-common must be installed with --ignore-installed or the build "
        f"fails with uninstall-no-record-file; got: {line}"
    )


def test_aw_integration_does_NOT_use_ignore_installed(dockerfile: str) -> None:
    """The opposite mistake, which a well-meaning tidy-up would introduce.

    aw-integration depends on notetaker-common, a LOCAL package on no index.
    --ignore-installed makes pip re-resolve it from PyPI and fail with "No
    matching distribution for notetaker-common". The two lines MUST differ.
    """
    line = _pip_line(dockerfile, "./aw-integration")
    assert "--ignore-installed" not in line, (
        "aw-integration must NOT use --ignore-installed; pip would re-resolve "
        f"notetaker-common from PyPI and fail. Got: {line}"
    )
