"""Request validation for the intake surface (§2, task A4) — the DRAFT `intake.v1` contract.

`parse_entry` and `parse_remove` are the ONLY way a `PUT /v2/entries` / `POST /v2/entries/remove`
body becomes a typed, normalised value; a route handler never re-implements a field check. Both
validate AT THE SEAM (jsonschema by path against the sealed-in-shape-but-still-unsealed
`intake.v1` schema — the same `conforms`-style discipline as `lifecycle/receiver.py`), then run the
§2 validation order in Python (steps a JSON Schema cannot express: naive-time rejection, UTC
normalisation, the metadata byte-size cap, `end <= start`, the `join_now` override,
`already_ended` / `too_far_ahead`, and finally lower-casing + `content_hash`).

Validation order (§2):
  1. JSON Schema -> ``invalid_request`` (never echoing ``metadata`` or the URL query string — the
     schema-violation message below names the failed field/rule, never the offending value).
  2. Naive time -> ``invalid_request``.
  3. Normalise to UTC.
  4. ``metadata`` over 16384 bytes -> ``invalid_request``.
  5. ``end <= start`` -> ``invalid_request``.
  6. ``join_now`` -> ``start`` = ``now``, ``end`` = ``None``.
  7. ``already_ended`` (skipped when ``join_now``: ``end`` is ``None``).
  8. ``too_far_ahead`` (skipped when ``join_now``: ``start`` is ``now``).
  9. Lower-case (``user``, ``attendees``), then compute ``content_hash``.

``content_hash`` (documented in full in ``../../../../contracts/intake.v1/README.md``, the
cross-language contract with the calendar module): sha256 hex of the canonical JSON
(``json.dumps(..., sort_keys=True, separators=(",", ":"))``) over the normalised
(lower-cased + UTC) fields ``attendees, end, join_now, meeting_url, metadata, series_id, start,
time_zone, title`` — deliberately EXCLUDING ``external_id``/``user`` (the entry's identity key,
used to look the row up, not its content).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Optional

import jsonschema
from referencing import Registry, Resource

__all__ = ["EntryIn", "RemoveIn", "IntakeError", "parse_entry", "parse_remove"]

#: §2.2 — "metadata | object <= 16 KB". A wire-contract limit, not a per-deployment setting, so it
#: is not in config.v1.json (mirrors how the schema itself bakes in maxLength/maxItems).
_MAX_METADATA_BYTES = 16384


def _load_intake_schema() -> dict:
    """Locate the (unsealed, draft) intake.v1 schema by walking up to the monorepo root — the same
    by-path seam discipline as ``lifecycle/receiver.py``'s ``_load_lifecycle_schema``.
    """
    rel = Path("meetings") / "contracts" / "intake.v1" / "intake.schema.json"
    for parent in Path(__file__).resolve().parents:
        candidate = parent / rel
        if candidate.is_file():
            return json.loads(candidate.read_text())
    raise FileNotFoundError(f"monorepo root with {rel} not found")


_SCHEMA = _load_intake_schema()
_REGISTRY = Registry().with_resource(_SCHEMA["$id"], Resource.from_contents(_SCHEMA))


def _conforms(obj: Any, shape: str) -> None:
    """Validate `obj` against `intake.v1#/$defs/<shape>` (raises `jsonschema.ValidationError`)."""
    jsonschema.Draft202012Validator(
        {"$ref": f"{_SCHEMA['$id']}#/$defs/{shape}"}, registry=_REGISTRY
    ).validate(obj)


def _describe_schema_error(e: jsonschema.ValidationError) -> str:
    """A schema-violation message naming the field and the failed rule — NEVER the offending
    value, so a bad ``metadata`` blob (or any other field's content) is never echoed back to the
    sender (§2: "Error messages never echo metadata or the URL query string")."""
    path = ".".join(str(p) for p in e.absolute_path) or "<body>"
    v = e.validator
    if v == "required":
        # `e.validator_value` is the schema's FULL declared `required` list, not the specific
        # missing propert(y/ies) — `e.message` (e.g. "'user' is a required property") names only
        # the one actually-missing property, and is safe: a property NAME, never its value.
        return f"{path}: {e.message}"
    if v == "type":
        return f"{path}: expected type {e.validator_value}"
    if v in ("maxLength", "minLength", "maxItems", "minItems"):
        return f"{path}: violates {v} {e.validator_value}"
    if v == "pattern":
        return f"{path}: does not match the required pattern"
    if v == "enum":
        return f"{path}: must be one of {e.validator_value}"
    if v == "additionalProperties":
        return f"{path}: unexpected additional field(s)"
    return f"{path}: failed '{v}'"


@dataclass(frozen=True)
class EntryIn:
    external_id: str
    user: str
    meeting_url: str
    start: datetime
    end: Optional[datetime]
    time_zone: Optional[str]
    title: Optional[str]
    attendees: tuple[str, ...]
    series_id: Optional[str]
    join_now: bool
    metadata: Optional[dict[str, Any]]
    content_hash: str


@dataclass(frozen=True)
class RemoveIn:
    external_id: str
    user: str
    reason: Optional[str]


class IntakeError(Exception):
    """One §2.5 error, carrying its own HTTP status. ``parse_entry``/``parse_remove`` raise only
    the validation-order codes below (``invalid_request``, ``already_ended``, ``too_far_ahead``),
    always with ``retry_after_s=None`` — but the FULL §2.5 code -> status table lives here so a
    later route handler raises this SAME class for every other code (``unauthorized``,
    ``entry_not_found``, ``meeting_not_finished``, ``rate_limited``, ...) without re-deriving it.
    """

    #: §2.5, verbatim.
    HTTP_STATUS: dict[str, int] = {
        "invalid_request": 400,
        "unrecognized_link": 400,
        "platform_not_enabled": 400,
        "too_far_ahead": 400,
        "already_ended": 400,
        "unauthorized": 401,
        "forbidden": 403,
        "entry_not_found": 404,
        "meeting_not_found": 404,
        "meeting_not_finished": 409,
        "no_live_bot": 409,
        "rate_limited": 429,
        "quota_exceeded": 429,
        "unavailable": 503,
    }

    def __init__(
        self, code: str, message: str, *, retry_after_s: Optional[int] = None
    ) -> None:
        if code not in self.HTTP_STATUS:
            raise ValueError(f"unknown intake.v1 error code: {code!r}")
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = self.HTTP_STATUS[code]
        self.retry_after_s = retry_after_s


def _iso_utc(dt: datetime) -> str:
    """A UTC-aware ``datetime`` -> its UTC ISO-8601 string with a trailing ``Z`` (mirrors
    ``intake/projection.py``'s ``_iso_utc``, so every intake surface renders timestamps the same
    way)."""
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_timestamp(raw: str, field: str) -> datetime:
    """Step 2 + 3: reject a malformed or NAIVE timestamp, else normalise to UTC."""
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise IntakeError(
            "invalid_request", f"{field}: not a valid ISO-8601 timestamp"
        ) from exc
    if dt.tzinfo is None:
        raise IntakeError(
            "invalid_request",
            f"{field}: naive timestamps are not allowed (an explicit UTC offset is required)",
        )
    return dt.astimezone(timezone.utc)


def _content_hash(
    *,
    meeting_url: str,
    start: Optional[datetime],
    end: Optional[datetime],
    time_zone: Optional[str],
    title: Optional[str],
    attendees: tuple[str, ...],
    series_id: Optional[str],
    join_now: bool,
    metadata: Optional[dict[str, Any]],
) -> str:
    """sha256 hex of the canonical JSON over the normalised entry fields. See the module docstring
    and ``contracts/intake.v1/README.md`` for the exact field list and form (a shared test vector
    lives at ``contracts/intake.v1/content-hash-vector.json``)."""
    fields = {
        "attendees": list(attendees),
        "end": _iso_utc(end) if end is not None else None,
        "join_now": join_now,
        "meeting_url": meeting_url,
        "metadata": metadata,
        "series_id": series_id,
        "start": _iso_utc(start) if start is not None else None,
        "time_zone": time_zone,
        "title": title,
    }
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"))
    return sha256(canonical.encode("utf-8")).hexdigest()


def parse_entry(body: Any, *, now: datetime, max_days_ahead: int) -> EntryIn:
    """The §2.2 `PUT /v2/entries` body -> a normalised `EntryIn`, or raises `IntakeError`."""
    # 1. JSON Schema.
    try:
        _conforms(body, "Entry")
    except jsonschema.ValidationError as e:
        raise IntakeError("invalid_request", _describe_schema_error(e)) from e

    now_utc = (
        now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)
    )
    join_now = bool(body.get("join_now", False))

    # 2-3. Naive-time rejection + UTC normalisation (only for whichever of start/end is present —
    #      the schema already required both unless join_now).
    raw_start, raw_end = body.get("start"), body.get("end")
    start = _parse_timestamp(raw_start, "start") if raw_start is not None else None
    end = _parse_timestamp(raw_end, "end") if raw_end is not None else None

    # 4. metadata size.
    metadata: Optional[dict[str, Any]] = body.get("metadata")
    if metadata is not None:
        size = len(json.dumps(metadata, separators=(",", ":")).encode("utf-8"))
        if size > _MAX_METADATA_BYTES:
            raise IntakeError(
                "invalid_request", f"metadata exceeds {_MAX_METADATA_BYTES} bytes"
            )

    # 5. end <= start.
    if start is not None and end is not None and end <= start:
        raise IntakeError("invalid_request", "end must be after start")

    # 6. join_now overrides start/end regardless of whatever was submitted.
    if join_now:
        start, end = now_utc, None
    else:
        # 7. already_ended (join_now already made end None above, so this never fires for it).
        if end is not None and end <= now_utc:
            raise IntakeError("already_ended", "end is not after the current time")
        # 8. too_far_ahead (join_now already pinned start = now above, so this never fires for it).
        if start is not None and start > now_utc + timedelta(days=max_days_ahead):
            raise IntakeError(
                "too_far_ahead", f"start is more than {max_days_ahead} days ahead"
            )

    # 9. Lower-case, then content_hash.
    user = str(body["user"]).lower()
    attendees = tuple(str(a).lower() for a in (body.get("attendees") or []))
    time_zone = body.get("time_zone")
    title = body.get("title")
    series_id = body.get("series_id")
    meeting_url = body["meeting_url"]

    content_hash = _content_hash(
        meeting_url=meeting_url,
        start=start,
        end=end,
        time_zone=time_zone,
        title=title,
        attendees=attendees,
        series_id=series_id,
        join_now=join_now,
        metadata=metadata,
    )

    return EntryIn(
        external_id=body["external_id"],
        user=user,
        meeting_url=meeting_url,
        start=start,  # type: ignore[arg-type]  # always set: join_now branch, or schema-required
        end=end,
        time_zone=time_zone,
        title=title,
        attendees=attendees,
        series_id=series_id,
        join_now=join_now,
        metadata=metadata,
        content_hash=content_hash,
    )


def parse_remove(body: Any) -> RemoveIn:
    """The §2.3 `POST /v2/entries/remove` body -> a normalised `RemoveIn`, or raises `IntakeError`."""
    try:
        _conforms(body, "Remove")
    except jsonschema.ValidationError as e:
        raise IntakeError("invalid_request", _describe_schema_error(e)) from e
    return RemoveIn(
        external_id=body["external_id"],
        user=str(body["user"]).lower(),
        reason=body.get("reason"),
    )
