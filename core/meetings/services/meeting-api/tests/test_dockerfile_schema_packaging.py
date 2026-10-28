"""Packaging integrity: every sealed/draft contract schema `meeting_api` loads BY PATH at
import time (P8, the seam — `_load_schema`/`_load_lifecycle_schema`/`_load_intake_schema`/etc.,
one per contract, all the same `Path("<domain>") / "contracts" / "<contract>" / "<file>"` shape)
must have a matching `COPY` line in the Dockerfile. Miss one and the image doesn't fail the
build — it fails on FIRST IMPORT inside the running container, since the walk-up-by-path loader
finds nothing (this is exactly the gap intake.v1 exposed: the schema existed, `validation.py`
loaded it by path, and nothing copied it into the image until this fix).

Mirrors `core/agent/services/agent-api/tests/test_packaging.py`'s Dockerfile-integrity pattern,
but checks the OPPOSITE direction that test doesn't: not "does every COPY source exist in the
repo", but "does everything the package loads by path have a COPY line".
"""

from __future__ import annotations

import re
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parents[1]
SRC = SERVICE_DIR / "src" / "meeting_api"
DOCKERFILE = SERVICE_DIR / "Dockerfile"

#: `Path("meetings") / "contracts" / "webhook.v1" / "webhook.schema.json"` (whitespace/newlines
#: between tokens are fine — `\s` matches them without needing DOTALL).
_LOAD_PATTERN = re.compile(
    r'Path\(\s*"([\w.-]+)"\s*\)\s*/\s*"contracts"\s*/\s*"([\w.-]+)"\s*/\s*"([\w.-]+\.schema\.json)"'
)

#: `core/meetings/contracts/webhook.v1/webhook.schema.json` as a Dockerfile COPY source.
_COPY_PATTERN = re.compile(r"core/(\w+)/contracts/([\w.-]+)/([\w.-]+\.schema\.json)")


def _schemas_loaded_by_path() -> set[tuple[str, str, str]]:
    """(domain, contract, filename) for every by-path schema load found under src/."""
    found: set[tuple[str, str, str]] = set()
    for path in SRC.rglob("*.py"):
        for m in _LOAD_PATTERN.finditer(path.read_text(encoding="utf-8")):
            found.add((m.group(1), m.group(2), m.group(3)))
    return found


def _dockerfile_copied_schemas() -> set[tuple[str, str, str]]:
    """(domain, contract, filename) for every matching COPY source in the Dockerfile."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    return {(m.group(1), m.group(2), m.group(3)) for m in _COPY_PATTERN.finditer(text)}


def test_every_schema_loaded_by_path_is_copied_into_the_image():
    loaded = _schemas_loaded_by_path()
    assert (
        loaded
    ), "no by-path schema loads found under src/ — regex drifted, update this test"
    missing = sorted(loaded - _dockerfile_copied_schemas())
    assert not missing, (
        f"Dockerfile has no COPY line for: {missing} — the image would boot without the "
        "schema and crash on first import (the walk-up-by-path loader finds nothing)"
    )


def test_dockerfile_copies_nothing_the_package_never_loads():
    """The reverse direction: a COPY line for a schema nothing loads is dead weight, not a bug,
    but flagging it keeps the two lists (and this test) honest as contracts come and go.
    """
    extra = sorted(_dockerfile_copied_schemas() - _schemas_loaded_by_path())
    assert not extra, (
        f"Dockerfile COPYs schema(s) no by-path loader references: {extra} — either a loader "
        "was removed and this line is stale, or this test's regex missed a loader"
    )
