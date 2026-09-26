"""§2 — `meeting_api.intake.validation`: `parse_entry`/`parse_remove`, the ONLY way a
`PUT /v2/entries` / `POST /v2/entries/remove` body becomes a typed, normalised value (the future
routes call these; they don't exist yet — task A5+). Covers the task brief's own checklist: the
goldens parse cleanly, every error path, `+05:30` normalisation, naive-time rejection, the
16384/16385 byte metadata boundary, and `content_hash` stability/sensitivity.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest

from meeting_api.intake import EntryIn, IntakeError, RemoveIn, parse_entry, parse_remove
from meeting_api.intake.validation import _content_hash

NOW = datetime(2026, 9, 20, 0, 0, 0, tzinfo=timezone.utc)


def _entry_body(**over: Any) -> dict:
    base = {
        "external_id": "google:3n5kq8example",
        "user": "A@AbroadWorks.com",
        "meeting_url": "https://meet.google.com/kxo-misr-avz",
        "start": "2026-09-29T09:00:00+05:30",
        "end": "2026-09-29T09:30:00+05:30",
        "time_zone": "Asia/Kolkata",
        "title": "Weekly sync",
        "attendees": ["A@abroadworks.com", "B@Client.com"],
    }
    base.update(over)
    return base


def _golden_dir() -> Path:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "meetings" / "contracts" / "intake.v1" / "golden"
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError("intake.v1/golden not found")


# ── happy path + normalisation ──────────────────────────────────────────────


def test_parses_a_full_entry_and_normalises_utc_and_case():
    e = parse_entry(_entry_body(), now=NOW, max_days_ahead=30)
    assert isinstance(e, EntryIn)
    assert e.external_id == "google:3n5kq8example"
    assert e.user == "a@abroadworks.com"
    assert e.attendees == ("a@abroadworks.com", "b@client.com")
    assert e.start == datetime(2026, 9, 29, 3, 30, tzinfo=timezone.utc)
    assert e.end == datetime(2026, 9, 29, 4, 0, tzinfo=timezone.utc)
    assert e.join_now is False
    assert len(e.content_hash) == 64


def test_join_now_overrides_start_and_end():
    e = parse_entry(
        {
            "external_id": "x",
            "user": "a@b.com",
            "meeting_url": "https://meet.google.com/abc",
            "join_now": True,
        },
        now=NOW,
        max_days_ahead=30,
    )
    assert e.join_now is True
    assert e.start == NOW
    assert e.end is None


def test_join_now_ignores_a_submitted_start_and_end():
    e = parse_entry(_entry_body(join_now=True), now=NOW, max_days_ahead=30)
    assert e.start == NOW
    assert e.end is None


def test_join_now_never_triggers_already_ended_or_too_far_ahead():
    e = parse_entry(
        {
            "external_id": "x",
            "user": "a@b.com",
            "meeting_url": "https://y",
            "join_now": True,
        },
        now=NOW,
        max_days_ahead=0,
    )
    assert e.start == NOW and e.end is None


# ── step 1: JSON Schema ──────────────────────────────────────────────────────


@pytest.mark.parametrize("missing", ["external_id", "user", "meeting_url"])
def test_missing_required_field_is_invalid_request(missing):
    body = _entry_body()
    del body[missing]
    with pytest.raises(IntakeError) as exc:
        parse_entry(body, now=NOW, max_days_ahead=30)
    assert exc.value.code == "invalid_request"
    assert exc.value.http_status == 400
    assert exc.value.retry_after_s is None


def test_missing_start_and_end_without_join_now_is_invalid_request():
    body = _entry_body()
    del body["start"]
    del body["end"]
    with pytest.raises(IntakeError) as exc:
        parse_entry(body, now=NOW, max_days_ahead=30)
    assert exc.value.code == "invalid_request"


def test_malformed_user_is_invalid_request():
    with pytest.raises(IntakeError) as exc:
        parse_entry(_entry_body(user="not-an-email"), now=NOW, max_days_ahead=30)
    assert exc.value.code == "invalid_request"


def test_too_many_attendees_is_invalid_request():
    body = _entry_body(attendees=[f"u{i}@x.com" for i in range(101)])
    with pytest.raises(IntakeError) as exc:
        parse_entry(body, now=NOW, max_days_ahead=30)
    assert exc.value.code == "invalid_request"


def test_unknown_additional_field_is_invalid_request():
    with pytest.raises(IntakeError) as exc:
        parse_entry(_entry_body(unexpected_field="x"), now=NOW, max_days_ahead=30)
    assert exc.value.code == "invalid_request"


def test_schema_error_message_never_echoes_metadata_value():
    body = _entry_body(metadata="not-an-object")  # wrong type -> schema violation
    with pytest.raises(IntakeError) as exc:
        parse_entry(body, now=NOW, max_days_ahead=30)
    assert exc.value.code == "invalid_request"
    assert "not-an-object" not in exc.value.message


# ── step 2: naive time ───────────────────────────────────────────────────────


@pytest.mark.parametrize("field", ["start", "end"])
def test_naive_timestamp_is_refused(field):
    body = _entry_body(**{field: "2026-09-29T09:00:00"})
    with pytest.raises(IntakeError) as exc:
        parse_entry(body, now=NOW, max_days_ahead=30)
    assert exc.value.code == "invalid_request"
    assert "naive" in exc.value.message


def test_unparseable_timestamp_is_invalid_request():
    with pytest.raises(IntakeError) as exc:
        parse_entry(_entry_body(start="not-a-date"), now=NOW, max_days_ahead=30)
    assert exc.value.code == "invalid_request"


# ── step 5: end <= start ─────────────────────────────────────────────────────


def test_end_equal_to_start_is_invalid_request():
    body = _entry_body(start="2026-09-29T09:00:00Z", end="2026-09-29T09:00:00Z")
    with pytest.raises(IntakeError) as exc:
        parse_entry(body, now=NOW, max_days_ahead=30)
    assert exc.value.code == "invalid_request"


def test_end_before_start_is_invalid_request():
    body = _entry_body(start="2026-09-29T09:30:00Z", end="2026-09-29T09:00:00Z")
    with pytest.raises(IntakeError) as exc:
        parse_entry(body, now=NOW, max_days_ahead=30)
    assert exc.value.code == "invalid_request"


# ── steps 7-8: already_ended / too_far_ahead ─────────────────────────────────


def test_already_ended():
    body = _entry_body(start="2020-01-01T00:00:00Z", end="2020-01-01T01:00:00Z")
    with pytest.raises(IntakeError) as exc:
        parse_entry(body, now=NOW, max_days_ahead=30)
    assert exc.value.code == "already_ended"
    assert exc.value.http_status == 400
    assert exc.value.retry_after_s is None


def test_too_far_ahead():
    body = _entry_body(start="2030-01-01T00:00:00Z", end="2030-01-01T01:00:00Z")
    with pytest.raises(IntakeError) as exc:
        parse_entry(body, now=NOW, max_days_ahead=30)
    assert exc.value.code == "too_far_ahead"
    assert exc.value.http_status == 400
    assert exc.value.retry_after_s is None


# ── step 4: metadata byte boundary ───────────────────────────────────────────


def _metadata_of_bytes(n: int) -> dict:
    base_len = len(json.dumps({"k": ""}, separators=(",", ":")).encode("utf-8"))
    return {"k": "x" * (n - base_len)}


def test_metadata_exactly_16384_bytes_is_accepted():
    body = _entry_body(metadata=_metadata_of_bytes(16384))
    e = parse_entry(body, now=NOW, max_days_ahead=30)
    assert e.metadata is not None
    assert len(json.dumps(e.metadata, separators=(",", ":")).encode("utf-8")) == 16384


def test_metadata_16385_bytes_is_invalid_request():
    body = _entry_body(metadata=_metadata_of_bytes(16385))
    with pytest.raises(IntakeError) as exc:
        parse_entry(body, now=NOW, max_days_ahead=30)
    assert exc.value.code == "invalid_request"
    assert "16384" in exc.value.message


# ── step 9: content_hash ──────────────────────────────────────────────────────


def test_content_hash_stable_under_metadata_key_order():
    kwargs = dict(
        meeting_url="https://x",
        start=NOW,
        end=None,
        time_zone=None,
        title=None,
        attendees=("a@x.com",),
        series_id=None,
        join_now=False,
    )
    h1 = _content_hash(metadata={"a": 1, "b": 2}, **kwargs)
    h2 = _content_hash(metadata={"b": 2, "a": 1}, **kwargs)
    assert h1 == h2


@pytest.mark.parametrize(
    "field,value",
    [
        ("meeting_url", "https://different"),
        ("title", "Different title"),
        ("time_zone", "UTC"),
        ("series_id", "other-series"),
        ("join_now", True),
    ],
)
def test_content_hash_changes_when_a_field_changes(field, value):
    base = dict(
        meeting_url="https://x",
        start=NOW,
        end=None,
        time_zone=None,
        title=None,
        attendees=("a@x.com",),
        series_id=None,
        join_now=False,
        metadata=None,
    )
    h1 = _content_hash(**base)
    h2 = _content_hash(**dict(base, **{field: value}))
    assert h1 != h2


def test_content_hash_changes_when_attendees_change():
    kwargs = dict(
        meeting_url="https://x",
        start=NOW,
        end=None,
        time_zone=None,
        title=None,
        series_id=None,
        join_now=False,
        metadata=None,
    )
    h1 = _content_hash(attendees=("a@x.com",), **kwargs)
    h2 = _content_hash(attendees=("a@x.com", "b@x.com"), **kwargs)
    assert h1 != h2


def test_content_hash_excludes_external_id_and_user():
    e1 = parse_entry(
        _entry_body(external_id="one", user="a@x.com"), now=NOW, max_days_ahead=30
    )
    e2 = parse_entry(
        _entry_body(external_id="two", user="b@x.com"), now=NOW, max_days_ahead=30
    )
    assert e1.content_hash == e2.content_hash


def test_two_calls_with_identical_body_produce_the_same_hash():
    e1 = parse_entry(_entry_body(), now=NOW, max_days_ahead=30)
    e2 = parse_entry(_entry_body(), now=NOW, max_days_ahead=30)
    assert e1.content_hash == e2.content_hash


def test_content_hash_matches_the_shared_vector():
    vector = json.loads((_golden_dir().parent / "content-hash-vector.json").read_text())
    canonical = json.dumps(vector["normalised"], sort_keys=True, separators=(",", ":"))
    assert canonical == vector["canonical_json"]
    assert sha256(canonical.encode("utf-8")).hexdigest() == vector["content_hash"]
    e = parse_entry(vector["raw_input"], now=NOW, max_days_ahead=30)
    assert e.content_hash == vector["content_hash"]


# ── parse_remove ──────────────────────────────────────────────────────────────


def test_parse_remove_lower_cases_user():
    r = parse_remove({"external_id": "x", "user": "A@B.com", "reason": "cancelled"})
    assert isinstance(r, RemoveIn)
    assert r.user == "a@b.com"
    assert r.external_id == "x"
    assert r.reason == "cancelled"


def test_parse_remove_reason_is_optional():
    r = parse_remove({"external_id": "x", "user": "a@b.com"})
    assert r.reason is None


def test_parse_remove_missing_external_id_is_invalid_request():
    with pytest.raises(IntakeError) as exc:
        parse_remove({"user": "a@b.com"})
    assert exc.value.code == "invalid_request"
    assert exc.value.http_status == 400


def test_parse_remove_free_text_reason_allowed():
    r = parse_remove(
        {
            "external_id": "x",
            "user": "a@b.com",
            "reason": "the customer asked us to stop",
        }
    )
    assert r.reason == "the customer asked us to stop"


# ── IntakeError ────────────────────────────────────────────────────────────────


def test_intake_error_http_status_matches_2_5_table():
    table = {
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
    for code, status in table.items():
        err = IntakeError(code, "x")
        assert err.http_status == status
        assert err.retry_after_s is None
        assert err.code == code
        assert err.message == "x"


def test_intake_error_rejects_unknown_code():
    with pytest.raises(ValueError):
        IntakeError("not_a_real_code", "x")


def test_intake_error_carries_retry_after_s_when_given():
    err = IntakeError("rate_limited", "slow down", retry_after_s=30)
    assert err.retry_after_s == 30


# ── goldens parse cleanly through the real parser ────────────────────────────


def test_entry_goldens_parse_cleanly():
    d = _golden_dir()
    for name in ("Entry.scheduled.json", "Entry.join-now.json"):
        body = json.loads((d / name).read_text())
        e = parse_entry(body, now=NOW, max_days_ahead=30)
        assert isinstance(e, EntryIn)


def test_remove_goldens_parse_cleanly():
    d = _golden_dir()
    for name in ("Remove.with-reason.json", "Remove.no-reason.json"):
        body = json.loads((d / name).read_text())
        r = parse_remove(body)
        assert isinstance(r, RemoveIn)
