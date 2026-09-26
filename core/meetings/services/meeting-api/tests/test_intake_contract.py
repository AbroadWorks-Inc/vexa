"""§2/§2.7 — intake.v1 and webhook.v1 conform on BOTH runtimes.

`gate:schema` already proves every golden validates against its schema through Node/ajv
(`validate.mjs`). This is the Python half: the SAME goldens, run through `jsonschema`
(Draft202012Validator) — the identical engine `meeting_api.intake.validation` validates real
requests with (`_conforms`, seam-loaded by path exactly like `lifecycle/receiver.py`). Proving both
runtimes agree on the same fixtures is the point (task A4: "add a test that checks the goldens
against both"), not just re-stating what ajv already checked.

Also re-validates every EXISTING webhook.v1 golden after the §2.7 additions (new `EventType`
values, the optional `X-Webhook-Signature-Previous` header) — additive changes must not break a
single existing fixture.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
from referencing import Registry, Resource


def _find_contracts_dir() -> Path:
    """Walk up to the monorepo root — mirrors `intake/validation.py`'s `_load_intake_schema`."""
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "meetings" / "contracts"
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError("meetings/contracts not found")


_CONTRACTS = _find_contracts_dir()
_INTAKE_SCHEMA = _CONTRACTS / "intake.v1" / "intake.schema.json"
_INTAKE_GOLDEN = _CONTRACTS / "intake.v1" / "golden"
_WEBHOOK_SCHEMA = _CONTRACTS / "webhook.v1" / "webhook.schema.json"
_WEBHOOK_GOLDEN = _CONTRACTS / "webhook.v1" / "golden"


def _conforms(schema_path: Path, shape: str, obj: object) -> None:
    schema = json.loads(schema_path.read_text())
    registry = Registry().with_resource(schema["$id"], Resource.from_contents(schema))
    jsonschema.Draft202012Validator(
        {"$ref": f"{schema['$id']}#/$defs/{shape}"}, registry=registry
    ).validate(obj)


def _golden_cases(golden_dir: Path) -> list:
    return [
        pytest.param(path, path.name.split(".")[0], id=path.name)
        for path in sorted(golden_dir.glob("*.json"))
    ]


@pytest.mark.parametrize("path,shape", _golden_cases(_INTAKE_GOLDEN))
def test_intake_golden_conforms_in_python(path, shape):
    _conforms(_INTAKE_SCHEMA, shape, json.loads(path.read_text()))


@pytest.mark.parametrize("path,shape", _golden_cases(_WEBHOOK_GOLDEN))
def test_webhook_golden_still_conforms_in_python(path, shape):
    _conforms(_WEBHOOK_SCHEMA, shape, json.loads(path.read_text()))


def test_intake_golden_count_covers_every_reply_result_and_error_code():
    """One golden per §2.4 result (ten) and per §2.5 code (fourteen) — the task's own checklist."""
    names = {p.name for p in _INTAKE_GOLDEN.glob("*.json")}
    results = [
        "created",
        "joined_existing",
        "updated",
        "unchanged",
        "not_changed_live",
        "not_changed_finished",
        "removed",
        "entry_removed",
        "bot_stopping",
        "already_removed",
    ]
    for result in results:
        assert f"Reply.{result}.json" in names
    codes = [
        "invalid_request",
        "unrecognized_link",
        "platform_not_enabled",
        "too_far_ahead",
        "already_ended",
        "unauthorized",
        "forbidden",
        "entry_not_found",
        "meeting_not_found",
        "meeting_not_finished",
        "no_live_bot",
        "rate_limited",
        "quota_exceeded",
        "unavailable",
    ]
    for code in codes:
        assert f"Error.{code}.json" in names


def test_every_reply_golden_meeting_is_the_full_projection_shape():
    """Reply.meeting is never null and always carries the 16 §2.4 keys, for every result —
    including the removal-shaped results (removed/entry_removed/bot_stopping/already_removed).
    """
    expected_keys = {
        "id",
        "status",
        "completion_reason",
        "failure_stage",
        "outcome",
        "platform",
        "room",
        "meeting_url",
        "title",
        "start",
        "end",
        "time_zone",
        "bot_joins_at",
        "entries",
        "export",
        "sequence",
    }
    for path in sorted(_INTAKE_GOLDEN.glob("Reply.*.json")):
        data = json.loads(path.read_text())
        assert set(data["meeting"].keys()) == expected_keys, path.name


def test_intake_contract_is_not_sealed_yet():
    """Ruling R1: intake.v1 is a DRAFT — sealed later in task A8."""
    seal = json.loads(
        (_CONTRACTS.parent.parent.parent / "contracts.seal.json").read_text()
    )
    assert "core/meetings/contracts/intake.v1" not in seal
