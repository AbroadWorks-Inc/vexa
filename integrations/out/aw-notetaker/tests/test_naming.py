import pytest

from exporter.config import Settings
from exporter.naming import folder_name


def test_meet_example() -> None:
    assert (
        folder_name("google_meet", "eyw-igia-bab", "2026-09-22T17:02:34.432140Z")
        == "google_meet_eyw-igia-bab_20260922T170234432Z"
    )


def test_offset_is_normalised_to_utc() -> None:
    assert (
        folder_name("zoom", "85012345678", "2026-09-22T22:32:34.001+05:30")
        == "zoom_85012345678_20260922T170234001Z"
    )


def test_no_fraction_means_000() -> None:
    assert folder_name("teams", "123", "2026-09-22T17:02:34Z").endswith("T170234000Z")


def test_native_id_sanitised_so_underscore_only_separates() -> None:
    assert (
        folder_name("teams", "meet/a_b c", "2026-09-22T17:02:34Z")
        == "teams_meet-a-b-c_20260922T170234000Z"
    )


def test_missing_start_time_raises() -> None:
    with pytest.raises(ValueError):
        folder_name("zoom", "1", "")


BASE = {
    "MEETING_API_URL": "http://meeting-api:8080/",
    "VEXA_WEBHOOK_SECRET": "test-secret",
    "VEXA_BUCKET": "aw-bots",
    "EXPORT_BUCKET": "aw-chatworks-transcribe",
    "NOTETAKER_URL": "http://notetaker-api:8080",
}


def test_settings_defaults() -> None:
    s = Settings.from_env(BASE)
    assert s.meeting_api_url == "http://meeting-api:8080"
    assert s.export_prefix == "recordings/"
    assert s.debug is False and s.concurrency == 4
    assert s.rms_speech_threshold == 0.026
    assert s.record_chunk_timeslice_ms == 15000
    assert s.tape_wait_seconds == 120.0
    assert s.tape_max_bytes == 262144000


def test_settings_tape_env_overrides() -> None:
    s = Settings.from_env({**BASE, "TAPE_WAIT_SECONDS": "30", "TAPE_MAX_BYTES": "1000"})
    assert s.tape_wait_seconds == 30.0
    assert s.tape_max_bytes == 1000


def test_settings_missing_required() -> None:
    with pytest.raises(RuntimeError, match="VEXA_WEBHOOK_SECRET"):
        Settings.from_env({k: v for k, v in BASE.items() if k != "VEXA_WEBHOOK_SECRET"})
