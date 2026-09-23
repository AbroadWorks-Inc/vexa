"""Tests for exporter.job — the per-meeting export job (spec §4.2-4.3)."""

from __future__ import annotations

import asyncio
import io
import logging
import wave
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import boto3
import pytest
from moto import mock_aws

from exporter.config import Settings
import exporter.job as job_module
from exporter.job import (
    ActivityNotReady,
    Deps,
    ExportResult,
    export_meeting,
    recording_origin_ms,
)
from exporter.queue import PendingQueue, sweep_once
from exporter.storage import Storage
from tests.builders import (
    capped,
    frame,
    header,
    two_speaker_gmeet_lines,
    write_silent_wav,
)

VEXA_BUCKET = "aw-bots"
EXPORT_BUCKET = "aw-chatworks-transcribe"
FOLDER = "google_meet_abc-defg-hij_20260618T100000000Z"
BASE = f"recordings/{FOLDER}/"

RECORDING_CREATED_AT = "2026-06-18T10:00:15.000Z"
TIMESLICE_MS = 15000


def _origin_ms() -> int:
    return recording_origin_ms({"created_at": RECORDING_CREATED_AT}, TIMESLICE_MS)


def _envelope(**meeting_overrides: Any) -> dict[str, Any]:
    meeting: dict[str, Any] = {
        "id": 11367,
        "user_id": 7,
        "platform": "google_meet",
        "native_meeting_id": "abc-defg-hij",
        "constructed_meeting_url": "https://meet.google.com/abc-defg-hij",
        "status": "completed",
        "start_time": "2026-06-18T10:00:00.000Z",
        "end_time": "2026-06-18T10:42:00.000Z",
        "data": {"name": "Weekly sync"},
    }
    meeting.update(meeting_overrides)
    return {
        "event_id": "evt_test",
        "event_type": "meeting.completed",
        "api_version": "2026-03-01",
        "created_at": "2026-06-18T10:42:00.000Z",
        "data": {"meeting": meeting},
    }


class FakeMeetingApi:
    def __init__(
        self,
        recordings: list[dict[str, Any]] | None = None,
        master: dict[str, Any] | None = None,
        transcript: dict[str, Any] | None = None,
    ) -> None:
        self.recordings = recordings if recordings is not None else []
        self._master = master
        self._transcript = transcript
        self.list_recordings_calls: list[tuple[int, int]] = []
        self.master_calls: list[tuple[int, int]] = []
        self.transcript_calls: list[tuple[int, int]] = []

    def list_recordings(self, user_id: int, meeting_id: int) -> list[dict[str, Any]]:
        self.list_recordings_calls.append((user_id, meeting_id))
        return self.recordings

    def master(self, user_id: int, recording_id: int) -> dict[str, Any]:
        self.master_calls.append((user_id, recording_id))
        assert self._master is not None
        return self._master

    def transcript(self, user_id: int, meeting_id: int) -> dict[str, Any] | None:
        self.transcript_calls.append((user_id, meeting_id))
        return self._transcript


class FakeNotetaker:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def process(self, meeting_id: str, s3_path: str, platform: str) -> None:
        self.calls.append((meeting_id, s3_path, platform))


class RaisingNotetaker:
    def process(self, meeting_id: str, s3_path: str, platform: str) -> None:
        raise RuntimeError("boom")


FAKE_WAV_SECONDS = 60.0
END_TIME = datetime(2026, 6, 18, 10, 42, 0, tzinfo=timezone.utc)


def _fake_transcode(src: Path, dst: Path) -> None:
    write_silent_wav(dst, FAKE_WAV_SECONDS)


def _transcode_of(duration_s: float) -> Callable[[Path, Path], None]:
    def transcode(src: Path, dst: Path) -> None:
        write_silent_wav(dst, duration_s)

    return transcode


@pytest.fixture
def storage(monkeypatch: pytest.MonkeyPatch) -> Iterator[Storage]:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=VEXA_BUCKET)
        client.create_bucket(Bucket=EXPORT_BUCKET)
        yield Storage(client)


def _settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "meeting_api_url": "http://meeting-api",
        "webhook_secret": "s",
        "vexa_bucket": VEXA_BUCKET,
        "export_bucket": EXPORT_BUCKET,
        "export_prefix": "recordings/",
        "notetaker_url": "http://notetaker",
    }
    base.update(overrides)
    return Settings(**base)


def _deps(
    storage: Storage,
    meeting_api: Any,
    notetaker: Any,
    *,
    settings: Settings | None = None,
    now: Callable[[], datetime] | None = None,
    transcode: Callable[[Path, Path], None] = _fake_transcode,
) -> Deps:
    return Deps(
        settings=settings or _settings(),
        storage=storage,
        meeting_api=meeting_api,
        notetaker=notetaker,
        transcode=transcode,
        now=now or (lambda: datetime(2026, 6, 18, 11, 0, 0, tzinfo=timezone.utc)),
    )


def _audio_recording(rec_id: int, **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": rec_id,
        "status": "completed",
        "created_at": RECORDING_CREATED_AT,
        "media_files": [{"id": 1, "type": "audio", "format": "webm"}],
    }
    row.update(overrides)
    return row


def test_recording_origin_ms_subtracts_the_chunk_timeslice() -> None:
    origin = recording_origin_ms({"created_at": "2026-09-22T17:02:49.811544Z"}, 15000)
    expected = int(
        datetime(2026, 9, 22, 17, 2, 34, 811000, tzinfo=timezone.utc).timestamp() * 1000
    )
    assert origin == expected


def test_happy_path_writes_expected_keys_and_hands_off(storage: Storage) -> None:
    storage_path = "recordings/1/855958819514/01ba075a-test/audio/master.webm"
    storage.put_bytes(VEXA_BUCKET, storage_path, b"webm-bytes", "video/webm")

    origin_ms = _origin_ms()
    activity_key = "signal/7/11367/01ba075a-test/speaker-activity.jsonl"
    lines = [
        header(),
        frame(origin_ms, "Ann Lee", 0.2),
        frame(origin_ms + 256, "Ann Lee", 0.0),
    ]
    storage.put_bytes(
        VEXA_BUCKET,
        activity_key,
        ("\n".join(lines) + "\n").encode(),
        "application/x-ndjson",
    )

    meeting_api = FakeMeetingApi(
        recordings=[_audio_recording(855958819514)],
        master={"id": 1, "type": "audio", "storage_path": storage_path},
    )
    notetaker = FakeNotetaker()
    deps = _deps(storage, meeting_api, notetaker)

    result = export_meeting(_envelope(), deps)

    assert result == ExportResult("handed_off", FOLDER)
    keys = set(storage.list_keys(EXPORT_BUCKET, BASE))
    assert keys == {
        BASE + "master.webm",
        BASE + "audio.wav",
        BASE + "speaker_timeline.json",
        BASE + "participants.json",
        BASE + "meeting.json",
        BASE + "recordings.json",
        BASE + "_export.json",
    }
    assert notetaker.calls == [("vexa-11367", BASE, "google_meet")]
    assert storage.get_bytes(EXPORT_BUCKET, BASE + "master.webm") == b"webm-bytes"
    wav_bytes = storage.get_bytes(EXPORT_BUCKET, BASE + "audio.wav")
    with wave.open(io.BytesIO(wav_bytes), "rb") as wav:
        assert wav.getnframes() / wav.getframerate() == FAKE_WAV_SECONDS

    marker = storage.get_json(EXPORT_BUCKET, BASE + "_export.json")
    assert marker["state"] == "handed_off"
    assert marker["vexa_meeting_id"] == 11367
    assert marker["speaker_activity"] == "ok"
    assert marker["exported_at"] == "2026-06-18T11:00:00+00:00"
    assert marker["elapsed_s"] == 0.0
    assert marker["audio_recordings"] == 1

    meeting_json = storage.get_json(EXPORT_BUCKET, BASE + "meeting.json")
    assert meeting_json["id"] == 11367
    recordings_json = storage.get_json(EXPORT_BUCKET, BASE + "recordings.json")
    assert recordings_json == {"recordings": [_audio_recording(855958819514)]}


def test_rerun_after_success_is_already_done_and_does_not_reprocess(
    storage: Storage,
) -> None:
    storage_path = "recordings/1/855958819514/01ba075a-test/audio/master.webm"
    storage.put_bytes(VEXA_BUCKET, storage_path, b"webm-bytes", "video/webm")
    meeting_api = FakeMeetingApi(
        recordings=[_audio_recording(855958819514)],
        master={"storage_path": storage_path},
    )
    notetaker = FakeNotetaker()
    deps = _deps(storage, meeting_api, notetaker)
    first = export_meeting(_envelope(), deps)
    assert first.state == "handed_off"

    meeting_api2 = FakeMeetingApi()
    notetaker2 = FakeNotetaker()
    deps2 = _deps(storage, meeting_api2, notetaker2)
    second = export_meeting(_envelope(), deps2)

    assert second == ExportResult("already_done", FOLDER)
    assert meeting_api2.list_recordings_calls == []
    assert notetaker2.calls == []


def test_no_audio_recording_marks_no_audio_and_does_not_process(
    storage: Storage,
) -> None:
    video_only = {
        "id": 1,
        "created_at": RECORDING_CREATED_AT,
        "media_files": [{"type": "video", "format": "mp4"}],
    }
    meeting_api = FakeMeetingApi(recordings=[video_only])
    notetaker = FakeNotetaker()
    deps = _deps(storage, meeting_api, notetaker)

    result = export_meeting(_envelope(), deps)

    assert result == ExportResult("no_audio", FOLDER)
    assert notetaker.calls == []
    assert storage.list_keys(EXPORT_BUCKET, BASE) == [BASE + "_export.json"]
    marker = storage.get_json(EXPORT_BUCKET, BASE + "_export.json")
    assert marker["state"] == "no_audio"


def test_debug_copies_signal_files(storage: Storage) -> None:
    storage_path = "recordings/1/2/uid-1/audio/master.webm"
    storage.put_bytes(VEXA_BUCKET, storage_path, b"webm-bytes", "video/webm")
    storage.put_bytes(
        VEXA_BUCKET,
        "signal/7/11367/uid-1/captured-signal.jsonl",
        (header() + "\n").encode(),
        "application/x-ndjson",
    )
    storage.put_bytes(
        VEXA_BUCKET, "signal/7/11367/uid-1/botlog.txt", b"log", "text/plain"
    )
    meeting_api = FakeMeetingApi(
        recordings=[_audio_recording(2)], master={"storage_path": storage_path}
    )
    notetaker = FakeNotetaker()
    deps = _deps(storage, meeting_api, notetaker, settings=_settings(debug=True))

    result = export_meeting(_envelope(), deps)

    assert result.state == "handed_off"
    assert (
        storage.get_bytes(EXPORT_BUCKET, BASE + "signal/captured-signal.jsonl")
        == (header() + "\n").encode()
    )
    assert storage.get_bytes(EXPORT_BUCKET, BASE + "signal/botlog.txt") == b"log"


def test_transcribe_enabled_writes_live_transcript(storage: Storage) -> None:
    storage_path = "recordings/1/3/uid-2/audio/master.webm"
    storage.put_bytes(VEXA_BUCKET, storage_path, b"webm-bytes", "video/webm")
    meeting_api = FakeMeetingApi(
        recordings=[_audio_recording(3)],
        master={"storage_path": storage_path},
        transcript={"segments": ["hi"]},
    )
    notetaker = FakeNotetaker()
    deps = _deps(storage, meeting_api, notetaker)

    result = export_meeting(
        _envelope(data={"name": "x", "transcribe_enabled": True}), deps
    )

    assert result.state == "handed_off"
    assert meeting_api.transcript_calls == [(7, 11367)]
    assert storage.get_json(EXPORT_BUCKET, BASE + "live_transcript.json") == {
        "segments": ["hi"]
    }


def test_notetaker_failure_propagates_and_leaves_no_handoff_marker(
    storage: Storage,
) -> None:
    storage_path = "recordings/1/4/uid-3/audio/master.webm"
    storage.put_bytes(VEXA_BUCKET, storage_path, b"webm-bytes", "video/webm")
    meeting_api = FakeMeetingApi(
        recordings=[_audio_recording(4)], master={"storage_path": storage_path}
    )
    deps = _deps(storage, meeting_api, RaisingNotetaker())

    with pytest.raises(RuntimeError):
        export_meeting(_envelope(), deps)

    assert storage.get_json(EXPORT_BUCKET, BASE + "_export.json") is None


def test_missing_speaker_activity_still_hands_off_with_missing_marker(
    storage: Storage,
) -> None:
    storage_path = "recordings/1/5/uid-4/audio/master.webm"
    storage.put_bytes(VEXA_BUCKET, storage_path, b"webm-bytes", "video/webm")
    meeting_api = FakeMeetingApi(
        recordings=[_audio_recording(5)], master={"storage_path": storage_path}
    )
    notetaker = FakeNotetaker()
    deps = _deps(storage, meeting_api, notetaker)

    result = export_meeting(_envelope(), deps)

    assert result.state == "handed_off"
    assert notetaker.calls == [("vexa-11367", BASE, "google_meet")]
    marker = storage.get_json(EXPORT_BUCKET, BASE + "_export.json")
    assert marker["speaker_activity"] == "missing"
    timeline = storage.get_json(EXPORT_BUCKET, BASE + "speaker_timeline.json")
    assert timeline["speaker_timeline"] == []
    assert timeline["participants"] == []
    participants = storage.get_json(EXPORT_BUCKET, BASE + "participants.json")
    assert participants["participants"] == []


def test_captured_signal_present_without_speaker_activity_is_missing_no_fallback(
    storage: Storage,
) -> None:
    """Proves there is no fallback: even though the debug tape file exists at
    the same prefix, the exporter never reads it for attribution."""
    storage_path = "recordings/1/5/uid-4b/audio/master.webm"
    storage.put_bytes(VEXA_BUCKET, storage_path, b"webm-bytes", "video/webm")
    storage.put_bytes(
        VEXA_BUCKET,
        "signal/7/11367/uid-4b/captured-signal.jsonl",
        (header() + "\n").encode(),
        "application/x-ndjson",
    )
    meeting_api = FakeMeetingApi(
        recordings=[_audio_recording(5)], master={"storage_path": storage_path}
    )
    notetaker = FakeNotetaker()
    deps = _deps(storage, meeting_api, notetaker)

    result = export_meeting(_envelope(), deps)

    assert result.state == "handed_off"
    marker = storage.get_json(EXPORT_BUCKET, BASE + "_export.json")
    assert marker["speaker_activity"] == "missing"


def test_export_marker_counts_speaker_activity_events(storage: Storage) -> None:
    storage_path = _put_master(storage, 40, "uid-40")
    _put_activity(storage, "uid-40", two_speaker_gmeet_lines(_origin_ms()))
    deps = _deps(storage, _api_for(40, storage_path), FakeNotetaker())

    assert export_meeting(_envelope(), deps).state == "handed_off"

    marker = storage.get_json(EXPORT_BUCKET, BASE + "_export.json")
    assert marker["speaker_activity"] == "ok"
    assert marker["speaker_activity_events"] == 4


def test_export_marker_counts_zero_events_when_activity_is_missing(
    storage: Storage,
) -> None:
    storage_path = _put_master(storage, 41, "uid-41")
    deps = _deps(storage, _api_for(41, storage_path), FakeNotetaker())

    assert export_meeting(_envelope(), deps).state == "handed_off"

    marker = storage.get_json(EXPORT_BUCKET, BASE + "_export.json")
    assert marker["speaker_activity"] == "missing"
    assert marker["speaker_activity_events"] == 0


def test_ok_activity_with_no_events_on_long_audio_warns_empty(
    storage: Storage, caplog: pytest.LogCaptureFixture
) -> None:
    storage_path = _put_master(storage, 42, "uid-42")
    _put_activity(storage, "uid-42", [header()])
    deps = _deps(
        storage,
        _api_for(42, storage_path),
        FakeNotetaker(),
        transcode=_transcode_of(200.0),
    )

    with caplog.at_level(logging.WARNING, logger="exporter"):
        assert export_meeting(_envelope(), deps).state == "handed_off"

    marker = storage.get_json(EXPORT_BUCKET, BASE + "_export.json")
    assert marker["speaker_activity"] == "ok"
    assert marker["speaker_activity_events"] == 0
    empty = [
        r
        for r in caplog.records
        if r.getMessage() == "speaker_activity_empty vexa_meeting_id=11367 audio_s=200"
    ]
    assert len(empty) == 1 and empty[0].levelno == logging.WARNING


@pytest.mark.parametrize(
    ("lines_of", "audio_s"),
    [
        (lambda origin: [header()], 180.0),
        (two_speaker_gmeet_lines, 200.0),
    ],
    ids=["short-audio", "has-events"],
)
def test_speaker_activity_empty_is_not_logged_for_short_audio_or_real_events(
    storage: Storage,
    caplog: pytest.LogCaptureFixture,
    lines_of: Callable[[int], list[str]],
    audio_s: float,
) -> None:
    storage_path = _put_master(storage, 43, "uid-43")
    _put_activity(storage, "uid-43", lines_of(_origin_ms()))
    deps = _deps(
        storage,
        _api_for(43, storage_path),
        FakeNotetaker(),
        transcode=_transcode_of(audio_s),
    )

    with caplog.at_level(logging.WARNING, logger="exporter"):
        assert export_meeting(_envelope(), deps).state == "handed_off"

    assert not any("speaker_activity_empty" in r.getMessage() for r in caplog.records)


def test_every_timeline_speaker_id_has_a_matching_participant(
    storage: Storage,
) -> None:
    """Job-level check for Ruling T6a: speaker_timeline.json's participants
    and participants.json must agree on speaker ids, with no duplicate
    participant records even when the file carries two spellings of one
    name (same slug)."""
    storage_path = "recordings/1/6/uid-5/audio/master.webm"
    storage.put_bytes(VEXA_BUCKET, storage_path, b"webm-bytes", "video/webm")
    origin_ms = _origin_ms()
    activity_key = "signal/7/11367/uid-5/speaker-activity.jsonl"
    lines = [
        header(),
        frame(origin_ms, "Ann Lee", 0.2),
        frame(origin_ms + 256, "Ann Lee", 0.0),
        frame(origin_ms + 5000, "ann lee ", 0.2),
        frame(origin_ms + 5256, "ann lee ", 0.0),
    ]
    storage.put_bytes(
        VEXA_BUCKET,
        activity_key,
        ("\n".join(lines) + "\n").encode(),
        "application/x-ndjson",
    )
    meeting_api = FakeMeetingApi(
        recordings=[_audio_recording(6)], master={"storage_path": storage_path}
    )
    notetaker = FakeNotetaker()
    deps = _deps(storage, meeting_api, notetaker)

    result = export_meeting(_envelope(), deps)

    assert result.state == "handed_off"
    timeline = storage.get_json(EXPORT_BUCKET, BASE + "speaker_timeline.json")
    participants = storage.get_json(EXPORT_BUCKET, BASE + "participants.json")

    timeline_ids = {p["speaker_id"] for p in timeline["speaker_timeline"]} | {
        p["id"] for p in timeline["participants"]
    }
    participant_ids = [p["id"] for p in participants["participants"]]
    assert timeline_ids <= set(participant_ids)
    assert len(participant_ids) == len(set(participant_ids))


# ---------------------------------------------------------------------------
# Final-review fixes (spec §4.2 activity wait / activity states, §4.3 clip)
# ---------------------------------------------------------------------------


def _put_master(storage: Storage, rec_id: int, uid: str) -> str:
    storage_path = f"recordings/1/{rec_id}/{uid}/audio/master.webm"
    storage.put_bytes(VEXA_BUCKET, storage_path, b"webm-bytes", "video/webm")
    return storage_path


def _put_activity(storage: Storage, uid: str, lines: list[str]) -> bytes:
    body = ("\n".join(lines) + "\n").encode()
    storage.put_bytes(
        VEXA_BUCKET,
        f"signal/7/11367/{uid}/speaker-activity.jsonl",
        body,
        "application/x-ndjson",
    )
    return body


def _api_for(rec_id: int, storage_path: str) -> FakeMeetingApi:
    return FakeMeetingApi(
        recordings=[_audio_recording(rec_id)], master={"storage_path": storage_path}
    )


def test_absent_activity_before_deadline_raises_activity_not_ready_and_does_not_process(
    storage: Storage,
) -> None:
    storage_path = _put_master(storage, 20, "uid-20")
    notetaker = FakeNotetaker()
    deps = _deps(
        storage,
        _api_for(20, storage_path),
        notetaker,
        now=lambda: END_TIME + timedelta(seconds=60),
    )

    with pytest.raises(ActivityNotReady):
        export_meeting(_envelope(), deps)

    assert notetaker.calls == []
    assert storage.get_json(EXPORT_BUCKET, BASE + "_export.json") is None


def test_absent_activity_after_deadline_hands_off_with_missing_marker(
    storage: Storage, caplog: pytest.LogCaptureFixture
) -> None:
    storage_path = _put_master(storage, 21, "uid-21")
    notetaker = FakeNotetaker()
    deps = _deps(
        storage,
        _api_for(21, storage_path),
        notetaker,
        now=lambda: END_TIME + timedelta(seconds=121),
    )

    with caplog.at_level(logging.ERROR, logger="exporter"):
        result = export_meeting(_envelope(), deps)

    assert result.state == "handed_off"
    assert notetaker.calls == [("vexa-11367", BASE, "google_meet")]
    marker = storage.get_json(EXPORT_BUCKET, BASE + "_export.json")
    assert marker["speaker_activity"] == "missing"
    assert any(
        "speaker_activity_missing vexa_meeting_id=11367" in r.getMessage()
        and r.levelno == logging.ERROR
        for r in caplog.records
    )


def test_activity_wait_respects_activity_wait_seconds_setting(storage: Storage) -> None:
    storage_path = _put_master(storage, 22, "uid-22")
    deps = _deps(
        storage,
        _api_for(22, storage_path),
        FakeNotetaker(),
        settings=_settings(activity_wait_seconds=30.0),
        now=lambda: END_TIME + timedelta(seconds=60),
    )

    assert export_meeting(_envelope(), deps).state == "handed_off"


def test_naive_end_time_is_treated_as_utc_for_the_activity_wait(
    storage: Storage,
) -> None:
    storage_path = _put_master(storage, 23, "uid-23")
    deps = _deps(
        storage,
        _api_for(23, storage_path),
        FakeNotetaker(),
        now=lambda: END_TIME + timedelta(seconds=60),
    )

    with pytest.raises(ActivityNotReady):
        export_meeting(_envelope(end_time="2026-06-18T10:42:00"), deps)


def test_null_end_time_does_not_wait_forever_for_speaker_activity(
    storage: Storage,
) -> None:
    """A null end_time has no fixed deadline to wait against (anchoring on
    deps.now() would move the deadline on every retry), so the job does not
    wait: it hands off with speaker_activity "missing"."""
    storage_path = _put_master(storage, 24, "uid-24")
    notetaker = FakeNotetaker()
    deps = _deps(storage, _api_for(24, storage_path), notetaker)

    result = export_meeting(_envelope(end_time=None), deps)

    assert result.state == "handed_off"
    assert (
        storage.get_json(EXPORT_BUCKET, BASE + "_export.json")["speaker_activity"]
        == "missing"
    )


def test_activity_not_ready_then_retry_with_activity_present_hands_off_once(
    storage: Storage,
) -> None:
    """failure -> retry -> success through the durable queue: the first
    sweep raises ActivityNotReady (backoff recorded), the file lands, the
    next sweep hands off; /process is called exactly once."""
    storage_path = _put_master(storage, 25, "uid-25")
    notetaker = FakeNotetaker()
    job_now = [END_TIME + timedelta(seconds=30)]
    deps = _deps(storage, _api_for(25, storage_path), notetaker, now=lambda: job_now[0])
    queue = PendingQueue(storage, VEXA_BUCKET)
    queue.enqueue(_envelope())

    asyncio.run(sweep_once(queue, deps, now=lambda: 1000.0))

    item = queue.load("11367")
    assert item is not None and item["attempts"] == 1
    assert "speaker activity not ready" in item["last_error"]
    assert notetaker.calls == []

    _put_activity(
        storage,
        "uid-25",
        [header(), frame(_origin_ms(), "Ann Lee", 0.2)],
    )
    job_now[0] = END_TIME + timedelta(seconds=90)
    asyncio.run(sweep_once(queue, deps, now=lambda: 5000.0))

    assert queue.pending_ids() == []
    assert notetaker.calls == [("vexa-11367", BASE, "google_meet")]
    marker = storage.get_json(EXPORT_BUCKET, BASE + "_export.json")
    assert marker["state"] == "handed_off" and marker["speaker_activity"] == "ok"


def test_header_less_activity_hands_off_with_invalid_marker(storage: Storage) -> None:
    storage_path = _put_master(storage, 26, "uid-26")
    _put_activity(storage, "uid-26", [frame(_origin_ms(), "Ann Lee", 0.2)])
    notetaker = FakeNotetaker()
    deps = _deps(storage, _api_for(26, storage_path), notetaker)

    result = export_meeting(_envelope(), deps)

    assert result.state == "handed_off"
    assert notetaker.calls == [("vexa-11367", BASE, "google_meet")]
    marker = storage.get_json(EXPORT_BUCKET, BASE + "_export.json")
    assert marker["speaker_activity"] == "invalid"
    timeline = storage.get_json(EXPORT_BUCKET, BASE + "speaker_timeline.json")
    assert timeline["speaker_timeline"] == [] and timeline["speaker_intervals"] == []
    participants = storage.get_json(EXPORT_BUCKET, BASE + "participants.json")
    assert participants["participants"] == []


@pytest.mark.parametrize("exc", [KeyError("t"), TypeError("x"), ValueError("x")])
def test_speech_events_failure_degrades_to_invalid_marker(
    storage: Storage, monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    storage_path = _put_master(storage, 27, "uid-27")
    _put_activity(storage, "uid-27", [header(), frame(_origin_ms(), "Ann Lee", 0.2)])

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise exc

    monkeypatch.setattr(job_module, "speech_events", boom)
    deps = _deps(storage, _api_for(27, storage_path), FakeNotetaker())

    assert export_meeting(_envelope(), deps).state == "handed_off"
    marker = storage.get_json(EXPORT_BUCKET, BASE + "_export.json")
    assert marker["speaker_activity"] == "invalid"
    participants = storage.get_json(EXPORT_BUCKET, BASE + "participants.json")
    assert participants["participants"] == []


def test_capped_marker_is_marked_capped_and_events_after_it_are_ignored(
    storage: Storage, caplog: pytest.LogCaptureFixture
) -> None:
    storage_path = _put_master(storage, 28, "uid-28")
    origin_ms = _origin_ms()
    lines = two_speaker_gmeet_lines(origin_ms) + [
        capped(origin_ms + 3000),
        frame(origin_ms + 4000, "Speaker Gamma", 0.2),
    ]
    _put_activity(storage, "uid-28", lines)
    deps = _deps(storage, _api_for(28, storage_path), FakeNotetaker())

    with caplog.at_level(logging.WARNING, logger="exporter"):
        assert export_meeting(_envelope(), deps).state == "handed_off"

    marker = storage.get_json(EXPORT_BUCKET, BASE + "_export.json")
    assert marker["speaker_activity"] == "capped"
    timeline = storage.get_json(EXPORT_BUCKET, BASE + "speaker_timeline.json")
    assert len(timeline["speaker_intervals"]) == 2
    names_seen = {p["name"] for p in timeline["participants"]}
    assert "Speaker Gamma" not in names_seen
    assert any("capped" in r.getMessage() for r in caplog.records)


class _SpyStorage(Storage):
    def __init__(self, inner: Storage) -> None:
        super().__init__(inner._client)
        self.get_bytes_keys: list[str] = []
        self.put_bytes_keys: list[str] = []
        self.uploads: list[tuple[str, str]] = []
        self.downloads: list[str] = []

    def get_bytes(self, bucket: str, key: str) -> bytes:
        self.get_bytes_keys.append(key)
        return super().get_bytes(bucket, key)

    def put_bytes(self, bucket: str, key: str, data: bytes, content_type: str) -> None:
        self.put_bytes_keys.append(key)
        super().put_bytes(bucket, key, data, content_type)

    def download_file(self, bucket: str, key: str, path: Path) -> None:
        self.downloads.append(key)
        super().download_file(bucket, key, path)

    def upload_file(self, path: Path, bucket: str, key: str, content_type: str) -> None:
        self.uploads.append((key, content_type))
        super().upload_file(path, bucket, key, content_type)


def test_audio_is_streamed_via_files_not_held_as_bytes(storage: Storage) -> None:
    spy = _SpyStorage(storage)
    storage_path = _put_master(storage, 30, "uid-30")
    deps = _deps(spy, _api_for(30, storage_path), FakeNotetaker())

    export_meeting(_envelope(), deps)

    assert not [k for k in spy.get_bytes_keys if k.endswith((".webm", ".wav"))]
    assert not [k for k in spy.put_bytes_keys if k.endswith((".webm", ".wav"))]
    assert spy.downloads == [BASE + "master.webm"]
    assert spy.uploads == [(BASE + "audio.wav", "audio/wav")]


def test_multiple_audio_recordings_are_counted_and_warned(
    storage: Storage, caplog: pytest.LogCaptureFixture
) -> None:
    storage_path = _put_master(storage, 31, "uid-31")
    meeting_api = FakeMeetingApi(
        recordings=[_audio_recording(31), _audio_recording(32)],
        master={"storage_path": storage_path},
    )
    deps = _deps(storage, meeting_api, FakeNotetaker())

    with caplog.at_level(logging.WARNING, logger="exporter"):
        export_meeting(_envelope(), deps)

    assert meeting_api.master_calls == [(7, 31)]
    marker = storage.get_json(EXPORT_BUCKET, BASE + "_export.json")
    assert marker["audio_recordings"] == 2
    assert any("audio_recordings=2" in r.getMessage() for r in caplog.records)


def test_single_audio_recording_logs_no_multi_session_warning(
    storage: Storage, caplog: pytest.LogCaptureFixture
) -> None:
    storage_path = _put_master(storage, 33, "uid-33")
    deps = _deps(storage, _api_for(33, storage_path), FakeNotetaker())

    with caplog.at_level(logging.WARNING, logger="exporter"):
        export_meeting(_envelope(), deps)

    assert not any("audio_recordings" in r.getMessage() for r in caplog.records)


def test_intervals_are_clipped_at_the_wav_duration(storage: Storage) -> None:
    storage_path = _put_master(storage, 34, "uid-34")
    origin_ms = _origin_ms()
    _put_activity(
        storage,
        "uid-34",
        [header()] + [frame(origin_ms + i * 256, "Ann Lee", 0.2) for i in range(20)],
    )
    deps = _deps(
        storage,
        _api_for(34, storage_path),
        FakeNotetaker(),
        transcode=_transcode_of(2.0),
    )

    export_meeting(_envelope(), deps)

    timeline = storage.get_json(EXPORT_BUCKET, BASE + "speaker_timeline.json")
    assert timeline["duration_sec"] == 2.0
    assert [(i["start_sec"], i["end_sec"]) for i in timeline["speaker_intervals"]] == [
        (0.0, 2.0)
    ]
    started = datetime.fromisoformat(timeline["recording_started_at"])
    ended = datetime.fromisoformat(timeline["recording_ended_at"])
    assert (ended - started).total_seconds() == 2.0


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        (
            {
                "constructed_meeting_url": "https://meet.google.com/top-level",
                "data": {"constructed_meeting_url": "https://meet.google.com/in-data"},
            },
            "https://meet.google.com/in-data",
        ),
        (
            {"constructed_meeting_url": "https://meet.google.com/top-level"},
            "https://meet.google.com/top-level",
        ),
        ({"constructed_meeting_url": None, "data": None}, "abc-defg-hij"),
    ],
)
def test_room_name_prefers_data_constructed_meeting_url(
    storage: Storage, overrides: dict[str, Any], expected: str
) -> None:
    storage_path = _put_master(storage, 35, "uid-35")
    deps = _deps(storage, _api_for(35, storage_path), FakeNotetaker())

    export_meeting(_envelope(**overrides), deps)

    timeline = storage.get_json(EXPORT_BUCKET, BASE + "speaker_timeline.json")
    assert timeline["room_name"] == expected


def test_export_marker_records_elapsed_wall_time(storage: Storage) -> None:
    storage_path = _put_master(storage, 36, "uid-36")
    _put_activity(storage, "uid-36", [header()])
    t0 = datetime(2026, 6, 18, 11, 0, 0, tzinfo=timezone.utc)
    ticks = iter([t0])

    def clock() -> datetime:
        return next(ticks, t0 + timedelta(seconds=7.5))

    deps = _deps(storage, _api_for(36, storage_path), FakeNotetaker(), now=clock)

    export_meeting(_envelope(), deps)

    marker = storage.get_json(EXPORT_BUCKET, BASE + "_export.json")
    assert marker["elapsed_s"] == 7.5
    assert marker["exported_at"] == "2026-06-18T11:00:07.500000+00:00"
    assert marker["speaker_activity"] == "ok"
    assert marker["audio_recordings"] == 1
