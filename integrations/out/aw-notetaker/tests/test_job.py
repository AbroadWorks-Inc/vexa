"""Tests for exporter.job — the per-meeting export job (spec §4.2-4.3)."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import wave
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import boto3
import pytest
from moto import mock_aws

from exporter.config import Settings
import exporter.job as job_module
from exporter.activity import names as activity_names
from exporter.activity import parse_activity, speech_events
from exporter.attribution import build_participants, build_speaker_timeline
from exporter.job import (
    ActivityNotReady,
    Deps,
    ExportResult,
    MissingMeetingUuid,
    export_meeting,
    recording_origin_ms,
)
from exporter.export_result import ExportReportError
from exporter.queue import PendingQueue, sweep_once
from exporter.storage import Storage
from exporter.vexa_client import TooManyRecordings
from tests.builders import (
    capped,
    frame,
    header,
    two_speaker_gmeet_lines,
    wav_samples,
    write_constant_wav,
    write_silent_wav,
)

VEXA_BUCKET = "aw-bots"
EXPORT_BUCKET = "aw-chatworks-transcribe"
FOLDER = "google_meet_abc-defg-hij_20260618T100000000Z"
BASE = f"recordings/{FOLDER}/"

S3_PATH = f"s3://{EXPORT_BUCKET}/{BASE}"
MEETING_UUID = "5f0c2b7e-8d1a-4c3e-9b6f-2a7d1e4c8b90"

RECORDING_CREATED_AT = "2026-06-18T10:00:15.000Z"
TIMESLICE_MS = 15000


def _origin_ms() -> int:
    return recording_origin_ms({"created_at": RECORDING_CREATED_AT}, TIMESLICE_MS)


def _envelope(**meeting_overrides: Any) -> dict[str, Any]:
    meeting: dict[str, Any] = {
        "id": 11367,
        "uuid": MEETING_UUID,
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
        masters: dict[int, dict[str, Any]] | None = None,
    ) -> None:
        self.recordings = recordings if recordings is not None else []
        self._master = master
        self._masters = masters
        self._transcript = transcript
        self.list_recordings_calls: list[int] = []
        self.master_calls: list[int] = []
        self.transcript_calls: list[int] = []

    def list_recordings(
        self, meeting_id: int, max_recordings: int
    ) -> list[dict[str, Any]]:
        self.list_recordings_calls.append(meeting_id)
        if len(self.recordings) > max_recordings:
            raise TooManyRecordings(meeting_id, max_recordings)
        return self.recordings

    def master(self, recording_id: int) -> dict[str, Any]:
        self.master_calls.append(recording_id)
        if self._masters is not None:
            return self._masters[recording_id]
        assert self._master is not None
        return self._master

    def transcript(self, meeting_id: int) -> dict[str, Any] | None:
        self.transcript_calls.append(meeting_id)
        return self._transcript


class FakeExportResult:
    def __init__(self, failures: int = 0) -> None:
        self.failures = failures
        self.calls: list[tuple[str, str, str, str | None]] = []

    def report(
        self, meeting_uuid: str, state: str, s3_path: str, error: str | None = None
    ) -> None:
        self.calls.append((meeting_uuid, state, s3_path, error))
        if self.failures > 0:
            self.failures -= 1
            raise ExportReportError("gateway 503 /v2/meetings/x/export")


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


def _no_join(parts: list[tuple[Path, float]], dst: Path) -> None:
    raise AssertionError("a single-session meeting's master.webm is copied, not joined")


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
        "gateway_url": "http://gateway",
        "exporter_api_key": "test-exporter-key",
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
    export_result: Any = None,
    join_webm: Callable[[list[tuple[Path, float]], Path], None] = _no_join,
) -> Deps:
    return Deps(
        settings=settings or _settings(),
        storage=storage,
        meeting_api=meeting_api,
        notetaker=notetaker,
        export_result=export_result or FakeExportResult(),
        transcode=transcode,
        now=now or (lambda: datetime(2026, 6, 18, 11, 0, 0, tzinfo=timezone.utc)),
        join_webm=join_webm,
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
    assert notetaker.calls == [(MEETING_UUID, BASE, "google_meet")]
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


def test_the_meeting_uuid_is_the_id_in_every_exported_file_and_the_hand_off(
    storage: Storage,
) -> None:
    storage_path = _put_master(storage, 50, "uid-50")
    _put_activity(storage, "uid-50", two_speaker_gmeet_lines(_origin_ms()))
    notetaker = FakeNotetaker()
    export_result = FakeExportResult()
    deps = _deps(
        storage, _api_for(50, storage_path), notetaker, export_result=export_result
    )

    assert export_meeting(_envelope(), deps).state == "handed_off"

    assert notetaker.calls == [(MEETING_UUID, BASE, "google_meet")]
    for name in ("speaker_timeline.json", "participants.json", "_export.json"):
        written_file = storage.get_json(EXPORT_BUCKET, BASE + name)
        assert isinstance(written_file, dict)
        assert written_file["meeting_id"] == MEETING_UUID, name
        if name == "_export.json":
            assert written_file["vexa_meeting_id"] == 11367
    assert export_result.calls == [(MEETING_UUID, "handed_off", S3_PATH, None)]
    written = b"".join(
        storage.get_bytes(EXPORT_BUCKET, key)
        for key in storage.list_keys(EXPORT_BUCKET, BASE)
        if key.endswith(".json")
    )
    assert b"vexa-" not in written


def test_a_webhook_without_uuid_fails_loudly_and_writes_nothing(
    storage: Storage,
) -> None:
    storage_path = _put_master(storage, 51, "uid-51")
    notetaker = FakeNotetaker()
    meeting_api = _api_for(51, storage_path)
    export_result = FakeExportResult()
    deps = _deps(storage, meeting_api, notetaker, export_result=export_result)
    envelope = _envelope()
    del envelope["data"]["meeting"]["uuid"]

    with pytest.raises(MissingMeetingUuid, match="11367"):
        export_meeting(envelope, deps)

    assert storage.list_keys(EXPORT_BUCKET, "") == []
    assert meeting_api.list_recordings_calls == []
    assert notetaker.calls == []
    assert export_result.calls == []


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_a_blank_uuid_is_a_missing_uuid(storage: Storage, blank: Any) -> None:
    deps = _deps(storage, FakeMeetingApi(), FakeNotetaker())

    with pytest.raises(MissingMeetingUuid):
        export_meeting(_envelope(uuid=blank), deps)

    assert storage.list_keys(EXPORT_BUCKET, "") == []


def test_an_unaccepted_report_fails_the_job_after_the_hand_off_is_recorded(
    storage: Storage,
) -> None:
    storage_path = _put_master(storage, 52, "uid-52")
    notetaker = FakeNotetaker()
    deps = _deps(
        storage,
        _api_for(52, storage_path),
        notetaker,
        export_result=FakeExportResult(failures=1),
    )

    with pytest.raises(ExportReportError):
        export_meeting(_envelope(), deps)

    assert notetaker.calls == [(MEETING_UUID, BASE, "google_meet")]
    marker = storage.get_json(EXPORT_BUCKET, BASE + "_export.json")
    assert isinstance(marker, dict) and marker["state"] == "handed_off"


def test_a_rerun_after_the_hand_off_reports_again_without_re_exporting(
    storage: Storage,
) -> None:
    storage_path = _put_master(storage, 53, "uid-53")
    deps = _deps(storage, _api_for(53, storage_path), FakeNotetaker())
    assert export_meeting(_envelope(), deps).state == "handed_off"

    meeting_api = FakeMeetingApi()
    notetaker = FakeNotetaker()
    export_result = FakeExportResult()
    rerun = _deps(storage, meeting_api, notetaker, export_result=export_result)

    assert export_meeting(_envelope(), rerun).state == "already_done"
    assert export_result.calls == [(MEETING_UUID, "handed_off", S3_PATH, None)]
    assert meeting_api.list_recordings_calls == []
    assert notetaker.calls == []


def test_no_audio_is_reported_as_a_failed_export(storage: Storage) -> None:
    video_only = {
        "id": 1,
        "created_at": RECORDING_CREATED_AT,
        "media_files": [{"type": "video", "format": "mp4"}],
    }
    export_result = FakeExportResult()
    deps = _deps(
        storage,
        FakeMeetingApi(recordings=[video_only]),
        FakeNotetaker(),
        export_result=export_result,
    )

    assert export_meeting(_envelope(), deps).state == "no_audio"

    assert export_result.calls == [
        (MEETING_UUID, "failed", S3_PATH, "no audio recording")
    ]


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
    assert meeting_api.transcript_calls == [11367]
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
    assert notetaker.calls == [(MEETING_UUID, BASE, "google_meet")]
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
    assert notetaker.calls == [(MEETING_UUID, BASE, "google_meet")]
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
    assert notetaker.calls == [(MEETING_UUID, BASE, "google_meet")]
    marker = storage.get_json(EXPORT_BUCKET, BASE + "_export.json")
    assert marker["state"] == "handed_off" and marker["speaker_activity"] == "ok"


def test_header_less_activity_hands_off_with_invalid_marker(storage: Storage) -> None:
    storage_path = _put_master(storage, 26, "uid-26")
    _put_activity(storage, "uid-26", [frame(_origin_ms(), "Ann Lee", 0.2)])
    notetaker = FakeNotetaker()
    deps = _deps(storage, _api_for(26, storage_path), notetaker)

    result = export_meeting(_envelope(), deps)

    assert result.state == "handed_off"
    assert notetaker.calls == [(MEETING_UUID, BASE, "google_meet")]
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

    def put_bytes(
        self,
        bucket: str,
        key: str,
        data: bytes,
        content_type: str,
        retention: str | None = None,
    ) -> None:
        self.put_bytes_keys.append(key)
        super().put_bytes(bucket, key, data, content_type, retention=retention)

    def download_file(self, bucket: str, key: str, path: Path) -> None:
        self.downloads.append(key)
        super().download_file(bucket, key, path)

    def upload_file(
        self,
        path: Path,
        bucket: str,
        key: str,
        content_type: str,
        retention: str | None = None,
    ) -> None:
        self.uploads.append((key, content_type))
        super().upload_file(path, bucket, key, content_type, retention=retention)


def test_audio_is_streamed_via_files_not_held_as_bytes(storage: Storage) -> None:
    spy = _SpyStorage(storage)
    storage_path = _put_master(storage, 30, "uid-30")
    deps = _deps(spy, _api_for(30, storage_path), FakeNotetaker())

    export_meeting(_envelope(), deps)

    assert not [k for k in spy.get_bytes_keys if k.endswith((".webm", ".wav"))]
    assert not [k for k in spy.put_bytes_keys if k.endswith((".webm", ".wav"))]
    assert spy.downloads == [BASE + "master.webm"]
    assert spy.uploads == [(BASE + "audio.wav", "audio/wav")]


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


def test_export_tags_every_object_with_its_retention_class(storage: Storage) -> None:
    """Full export run (spec §3/§7): master.webm -> recording-mp4, audio.wav ->
    audio, every export-bucket JSON -> metadata, and (EXPORT_DEBUG) signal/*
    copies -> audio."""
    storage_path = _put_master(storage, 60, "uid-60")
    _put_activity(storage, "uid-60", [header(), frame(_origin_ms(), "Ann Lee", 0.2)])
    storage.put_bytes(
        VEXA_BUCKET, "signal/7/11367/uid-60/botlog.txt", b"log", "text/plain"
    )
    meeting_api = FakeMeetingApi(
        recordings=[_audio_recording(60)],
        master={"storage_path": storage_path},
        transcript={"segments": ["hi"]},
    )
    notetaker = FakeNotetaker()
    deps = _deps(storage, meeting_api, notetaker, settings=_settings(debug=True))

    result = export_meeting(
        _envelope(data={"name": "x", "transcribe_enabled": True}), deps
    )

    assert result.state == "handed_off"

    def tag_value(bucket: str, key: str) -> str | None:
        tags = storage._client.get_object_tagging(Bucket=bucket, Key=key)["TagSet"]
        by_key = {t["Key"]: t["Value"] for t in tags}
        return by_key.get("retention-class")

    assert tag_value(EXPORT_BUCKET, BASE + "master.webm") == "recording-mp4"
    assert tag_value(EXPORT_BUCKET, BASE + "audio.wav") == "audio"
    for name in (
        "meeting.json",
        "recordings.json",
        "participants.json",
        "speaker_timeline.json",
        "live_transcript.json",
        "_export.json",
    ):
        assert tag_value(EXPORT_BUCKET, BASE + name) == "metadata", name
    assert tag_value(EXPORT_BUCKET, BASE + "signal/botlog.txt") == "audio"


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


def test_a_single_session_folder_is_built_exactly_as_from_its_one_recording(
    storage: Storage,
) -> None:
    storage_path = _put_master(storage, 90, "uid-90")
    lines = two_speaker_gmeet_lines(_origin_ms())
    _put_activity(storage, "uid-90", lines)
    deps = _deps(storage, _api_for(90, storage_path), FakeNotetaker())

    assert export_meeting(_envelope(), deps).state == "handed_off"

    settings = _settings()
    started = datetime.fromtimestamp(_origin_ms() / 1000, tz=timezone.utc)
    activity = parse_activity(lines)
    events = speech_events(
        activity,
        _origin_ms(),
        settings.rms_speech_threshold,
        settings.speech_hangover_ms,
    )
    timeline = build_speaker_timeline(
        events,
        platform="google_meet",
        meeting_id=MEETING_UUID,
        room_name="https://meet.google.com/abc-defg-hij",
        recording_started_at=started,
        recording_ended_at=started + timedelta(seconds=FAKE_WAV_SECONDS),
        min_dominant_utterance_ms=settings.min_dominant_utterance_ms,
    )
    participants = build_participants(
        activity_names(activity),
        platform="google_meet",
        meeting_id=MEETING_UUID,
        joined_at=started,
        host_email=None,
    )
    assert storage.get_bytes(EXPORT_BUCKET, BASE + "speaker_timeline.json") == (
        json.dumps(timeline.model_dump(mode="json")).encode()
    )
    assert storage.get_bytes(EXPORT_BUCKET, BASE + "participants.json") == (
        json.dumps(participants.model_dump(mode="json")).encode()
    )
    assert storage.get_bytes(EXPORT_BUCKET, BASE + "master.webm") == b"webm-bytes"
    samples, rate = wav_samples(storage.get_bytes(EXPORT_BUCKET, BASE + "audio.wav"))
    assert len(samples) / rate == FAKE_WAV_SECONDS


# ---------------------------------------------------------------------------
# Several bot sessions in one meeting (§6.9 F-K2): one folder, one clock
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Session:
    rec_id: int
    uid: str
    created_at: str
    seconds: float
    value: int

    @property
    def origin_ms(self) -> int:
        return recording_origin_ms({"created_at": self.created_at}, TIMESLICE_MS)


# Origins 10:00:00 (A), 10:01:30 (B, 90 s in), 10:03:20 (C, 200 s in).
SESSION_A = _Session(70, "uid-a", "2026-06-18T10:00:15.000Z", 60.0, 1)
SESSION_B = _Session(71, "uid-b", "2026-06-18T10:01:45.000Z", 40.0, 2)
SESSION_C = _Session(72, "uid-c", "2026-06-18T10:03:35.000Z", 20.0, 3)

EXPECTED_KEYS = {
    BASE + name
    for name in (
        "master.webm",
        "audio.wav",
        "speaker_timeline.json",
        "participants.json",
        "meeting.json",
        "recordings.json",
        "_export.json",
    )
}


class FakeJoinWebm:
    def __init__(self) -> None:
        self.calls: list[list[tuple[bytes, float]]] = []

    def __call__(self, parts: list[tuple[Path, float]], dst: Path) -> None:
        self.calls.append([(path.read_bytes(), silence) for path, silence in parts])
        dst.write_bytes(b"joined-webm")


def _seed_sessions(
    storage: Storage,
    sessions: list[_Session],
    extra: tuple[dict[str, Any], ...] = (),
) -> tuple[FakeMeetingApi, Callable[[Path, Path], None]]:
    """Each session's master in the Vexa bucket, listed NEWEST FIRST as
    meeting-api lists them; the transcode writes each session's own wav."""
    masters: dict[int, dict[str, Any]] = {}
    by_body: dict[bytes, _Session] = {}
    for session in sessions:
        body = f"webm-{session.uid}".encode()
        path = f"recordings/1/{session.rec_id}/{session.uid}/audio/master.webm"
        storage.put_bytes(VEXA_BUCKET, path, body, "video/webm")
        masters[session.rec_id] = {"storage_path": path}
        by_body[body] = session
    listed = [
        _audio_recording(session.rec_id, created_at=session.created_at)
        for session in sessions
    ] + list(extra)
    listed.sort(key=lambda rec: (rec["created_at"], rec["id"]), reverse=True)

    def transcode(src: Path, dst: Path) -> None:
        session = by_body[src.read_bytes()]
        write_constant_wav(dst, session.seconds, session.value)

    return FakeMeetingApi(recordings=listed, masters=masters), transcode


def _speaks(origin_ms: int, name: str, at_ms: int) -> list[str]:
    """`name` voiced for 768 ms from `origin_ms + at_ms`."""
    return [frame(origin_ms + at_ms + i * 256, name, 0.2) for i in range(3)]


def _intervals(timeline: dict[str, Any]) -> list[tuple[str, float, float]]:
    return [
        (iv["speaker_name"], iv["start_sec"], iv["end_sec"])
        for iv in timeline["speaker_intervals"]
    ]


def test_two_sessions_with_a_gap_make_one_folder_on_one_clock(
    storage: Storage,
) -> None:
    meeting_api, transcode = _seed_sessions(storage, [SESSION_A, SESSION_B])
    _put_activity(storage, "uid-a", two_speaker_gmeet_lines(SESSION_A.origin_ms))
    _put_activity(
        storage,
        "uid-b",
        [header()]
        + _speaks(SESSION_B.origin_ms, "Speaker Gamma", 0)
        + _speaks(SESSION_B.origin_ms, "Speaker Alpha", 5000),
    )
    join = FakeJoinWebm()
    notetaker = FakeNotetaker()
    deps = _deps(storage, meeting_api, notetaker, transcode=transcode, join_webm=join)

    assert export_meeting(_envelope(), deps) == ExportResult("handed_off", FOLDER)

    assert meeting_api.master_calls == [70, 71]
    assert set(storage.list_keys(EXPORT_BUCKET, BASE)) == EXPECTED_KEYS
    assert notetaker.calls == [(MEETING_UUID, BASE, "google_meet")]

    samples, rate = wav_samples(storage.get_bytes(EXPORT_BUCKET, BASE + "audio.wav"))
    assert len(samples) / rate == 130.0
    assert samples == [1] * 6000 + [0] * 3000 + [2] * 4000
    assert join.calls == [[(b"webm-uid-a", 0.0), (b"webm-uid-b", 30.0)]]
    assert storage.get_bytes(EXPORT_BUCKET, BASE + "master.webm") == b"joined-webm"
    tags = storage._client.get_object_tagging(
        Bucket=EXPORT_BUCKET, Key=BASE + "master.webm"
    )["TagSet"]
    assert {t["Key"]: t["Value"] for t in tags} == {"retention-class": "recording-mp4"}

    timeline = storage.get_json(EXPORT_BUCKET, BASE + "speaker_timeline.json")
    assert timeline["duration_sec"] == 130.0
    assert datetime.fromisoformat(timeline["recording_started_at"]) == datetime(
        2026, 6, 18, 10, 0, 0, tzinfo=timezone.utc
    )
    assert _intervals(timeline) == [
        ("Speaker Alpha", 0.0, 0.768),
        ("Speaker Beta", 1.5, 2.268),
        ("Speaker Gamma", 90.0, 90.768),
        ("Speaker Alpha", 95.0, 95.768),
    ]
    participants = storage.get_json(EXPORT_BUCKET, BASE + "participants.json")
    assert [p["id"] for p in participants["participants"]] == [
        "speaker_alpha",
        "speaker_beta",
        "speaker_gamma",
    ]
    marker = storage.get_json(EXPORT_BUCKET, BASE + "_export.json")
    assert marker["audio_recordings"] == 2
    assert marker["speaker_activity"] == "ok"
    assert marker["speaker_activity_events"] == 8
    assert storage.get_json(EXPORT_BUCKET, BASE + "meeting.json") == (
        _envelope()["data"]["meeting"]
    )
    assert storage.get_json(EXPORT_BUCKET, BASE + "recordings.json") == {
        "recordings": meeting_api.recordings
    }


def test_no_speaker_event_lands_in_the_gap_between_sessions(
    storage: Storage,
) -> None:
    """Session A's activity runs past its audio and session B's starts before
    its audio: both are held to their own session's span."""
    meeting_api, transcode = _seed_sessions(storage, [SESSION_A, SESSION_B])
    _put_activity(
        storage,
        "uid-a",
        two_speaker_gmeet_lines(SESSION_A.origin_ms)
        + _speaks(SESSION_A.origin_ms, "Speaker Beta", 60_500),
    )
    _put_activity(
        storage,
        "uid-b",
        [header()]
        + _speaks(SESSION_B.origin_ms, "Speaker Gamma", -3000)
        + _speaks(SESSION_B.origin_ms, "Speaker Gamma", 0),
    )
    deps = _deps(
        storage,
        meeting_api,
        FakeNotetaker(),
        transcode=transcode,
        join_webm=FakeJoinWebm(),
    )

    assert export_meeting(_envelope(), deps).state == "handed_off"

    timeline = storage.get_json(EXPORT_BUCKET, BASE + "speaker_timeline.json")
    assert _intervals(timeline) == [
        ("Speaker Alpha", 0.0, 0.768),
        ("Speaker Beta", 1.5, 2.268),
        ("Speaker Gamma", 90.0, 90.768),
    ]
    for point in timeline["speaker_timeline"]:
        assert not 60.0 < point["relative_sec"] < 90.0, point
    for _, start, end in _intervals(timeline):
        assert end <= 60.0 or start >= 90.0


def test_three_sessions_are_joined_in_order_with_every_gap_padded(
    storage: Storage,
) -> None:
    meeting_api, transcode = _seed_sessions(storage, [SESSION_A, SESSION_B, SESSION_C])
    _put_activity(storage, "uid-a", two_speaker_gmeet_lines(SESSION_A.origin_ms))
    _put_activity(storage, "uid-b", [header()])
    _put_activity(
        storage,
        "uid-c",
        [header()] + _speaks(SESSION_C.origin_ms, "Speaker Delta", 1000),
    )
    join = FakeJoinWebm()
    deps = _deps(
        storage, meeting_api, FakeNotetaker(), transcode=transcode, join_webm=join
    )

    assert export_meeting(_envelope(), deps).state == "handed_off"

    assert meeting_api.master_calls == [70, 71, 72]
    samples, rate = wav_samples(storage.get_bytes(EXPORT_BUCKET, BASE + "audio.wav"))
    assert len(samples) / rate == 220.0
    assert samples == ([1] * 6000 + [0] * 3000 + [2] * 4000 + [0] * 7000 + [3] * 2000)
    assert join.calls == [
        [(b"webm-uid-a", 0.0), (b"webm-uid-b", 30.0), (b"webm-uid-c", 70.0)]
    ]
    timeline = storage.get_json(EXPORT_BUCKET, BASE + "speaker_timeline.json")
    assert timeline["duration_sec"] == 220.0
    assert _intervals(timeline) == [
        ("Speaker Alpha", 0.0, 0.768),
        ("Speaker Beta", 1.5, 2.268),
        ("Speaker Delta", 201.0, 201.768),
    ]
    marker = storage.get_json(EXPORT_BUCKET, BASE + "_export.json")
    assert marker["audio_recordings"] == 3
    assert marker["speaker_activity"] == "ok"


def test_a_session_without_a_recording_is_skipped_with_a_log_line(
    storage: Storage, caplog: pytest.LogCaptureFixture
) -> None:
    """The bot of the middle session failed before it recorded: its recording
    row carries no audio file. The folder is built from the other two."""
    failed = {
        "id": 80,
        "status": "failed",
        "created_at": "2026-06-18T10:01:05.000Z",
        "media_files": [],
    }
    meeting_api, transcode = _seed_sessions(
        storage, [SESSION_A, SESSION_B], extra=(failed,)
    )
    _put_activity(storage, "uid-a", two_speaker_gmeet_lines(SESSION_A.origin_ms))
    _put_activity(storage, "uid-b", [header()])
    notetaker = FakeNotetaker()
    deps = _deps(
        storage, meeting_api, notetaker, transcode=transcode, join_webm=FakeJoinWebm()
    )

    with caplog.at_level(logging.INFO, logger="exporter"):
        assert export_meeting(_envelope(), deps).state == "handed_off"

    assert meeting_api.master_calls == [70, 71]
    assert notetaker.calls == [(MEETING_UUID, BASE, "google_meet")]
    assert set(storage.list_keys(EXPORT_BUCKET, BASE)) == EXPECTED_KEYS
    samples, rate = wav_samples(storage.get_bytes(EXPORT_BUCKET, BASE + "audio.wav"))
    assert len(samples) / rate == 130.0
    marker = storage.get_json(EXPORT_BUCKET, BASE + "_export.json")
    assert marker["audio_recordings"] == 2
    assert [
        r.getMessage()
        for r in caplog.records
        if r.getMessage().startswith("recording_skipped")
    ] == ["recording_skipped vexa_meeting_id=11367 recording_id=80 reason=no_audio"]


def test_a_session_starting_inside_the_previous_audio_follows_it_directly(
    storage: Storage, caplog: pytest.LogCaptureFixture
) -> None:
    early_b = _Session(71, "uid-b", "2026-06-18T10:00:45.000Z", 40.0, 2)
    meeting_api, transcode = _seed_sessions(storage, [SESSION_A, early_b])
    _put_activity(storage, "uid-a", [header()])
    _put_activity(
        storage, "uid-b", [header()] + _speaks(early_b.origin_ms, "Speaker Gamma", 0)
    )
    join = FakeJoinWebm()
    deps = _deps(
        storage, meeting_api, FakeNotetaker(), transcode=transcode, join_webm=join
    )

    with caplog.at_level(logging.WARNING, logger="exporter"):
        assert export_meeting(_envelope(), deps).state == "handed_off"

    samples, _ = wav_samples(storage.get_bytes(EXPORT_BUCKET, BASE + "audio.wav"))
    assert samples == [1] * 6000 + [2] * 4000
    assert join.calls == [[(b"webm-uid-a", 0.0), (b"webm-uid-b", 0.0)]]
    timeline = storage.get_json(EXPORT_BUCKET, BASE + "speaker_timeline.json")
    assert _intervals(timeline) == [("Speaker Gamma", 60.0, 60.768)]
    assert any(
        r.getMessage().startswith("session_overlap vexa_meeting_id=11367")
        and "recording_id=71" in r.getMessage()
        for r in caplog.records
    )


def test_one_session_missing_its_activity_marks_the_export_missing(
    storage: Storage, caplog: pytest.LogCaptureFixture
) -> None:
    meeting_api, transcode = _seed_sessions(storage, [SESSION_A, SESSION_B])
    _put_activity(storage, "uid-a", two_speaker_gmeet_lines(SESSION_A.origin_ms))
    deps = _deps(
        storage,
        meeting_api,
        FakeNotetaker(),
        transcode=transcode,
        join_webm=FakeJoinWebm(),
        now=lambda: END_TIME + timedelta(seconds=121),
    )

    with caplog.at_level(logging.ERROR, logger="exporter"):
        assert export_meeting(_envelope(), deps).state == "handed_off"

    marker = storage.get_json(EXPORT_BUCKET, BASE + "_export.json")
    assert marker["speaker_activity"] == "missing"
    assert marker["speaker_activity_events"] == 4
    timeline = storage.get_json(EXPORT_BUCKET, BASE + "speaker_timeline.json")
    assert [name for name, _, _ in _intervals(timeline)] == [
        "Speaker Alpha",
        "Speaker Beta",
    ]
    assert [
        r.getMessage()
        for r in caplog.records
        if "speaker_activity_missing" in r.getMessage()
    ] == ["speaker_activity_missing vexa_meeting_id=11367 session_uid=uid-b"]


def test_any_session_activity_not_yet_uploaded_waits_before_anything_is_written(
    storage: Storage,
) -> None:
    meeting_api, transcode = _seed_sessions(storage, [SESSION_A, SESSION_B])
    _put_activity(storage, "uid-b", [header()])
    notetaker = FakeNotetaker()
    deps = _deps(
        storage,
        meeting_api,
        notetaker,
        transcode=transcode,
        join_webm=FakeJoinWebm(),
        now=lambda: END_TIME + timedelta(seconds=60),
    )

    with pytest.raises(ActivityNotReady, match="uid-a"):
        export_meeting(_envelope(), deps)

    assert storage.list_keys(EXPORT_BUCKET, "") == []
    assert notetaker.calls == []


def test_debug_copies_each_sessions_signal_files_under_its_session_uid(
    storage: Storage,
) -> None:
    meeting_api, transcode = _seed_sessions(storage, [SESSION_A, SESSION_B])
    for uid in ("uid-a", "uid-b"):
        _put_activity(storage, uid, [header()])
        storage.put_bytes(
            VEXA_BUCKET, f"signal/7/11367/{uid}/botlog.txt", uid.encode(), "text/plain"
        )
    deps = _deps(
        storage,
        meeting_api,
        FakeNotetaker(),
        settings=_settings(debug=True),
        transcode=transcode,
        join_webm=FakeJoinWebm(),
    )

    assert export_meeting(_envelope(), deps).state == "handed_off"

    signal = sorted(
        key[len(BASE) :] for key in storage.list_keys(EXPORT_BUCKET, BASE + "signal/")
    )
    assert signal == [
        "signal/uid-a/botlog.txt",
        "signal/uid-a/speaker-activity.jsonl",
        "signal/uid-b/botlog.txt",
        "signal/uid-b/speaker-activity.jsonl",
    ]
    assert (
        storage.get_bytes(EXPORT_BUCKET, BASE + "signal/uid-b/botlog.txt") == b"uid-b"
    )


def test_a_meeting_over_the_recordings_cap_is_a_failed_export(
    storage: Storage, caplog: pytest.LogCaptureFixture
) -> None:
    """Never a folder from part of the meeting: past EXPORT_MAX_RECORDINGS
    nothing is exported, the failure is logged and reported."""
    meeting_api, transcode = _seed_sessions(storage, [SESSION_A, SESSION_B, SESSION_C])
    notetaker = FakeNotetaker()
    export_result = FakeExportResult()
    deps = _deps(
        storage,
        meeting_api,
        notetaker,
        settings=_settings(max_recordings=2),
        transcode=transcode,
        join_webm=FakeJoinWebm(),
        export_result=export_result,
    )

    with caplog.at_level(logging.ERROR, logger="exporter"):
        result = export_meeting(_envelope(), deps)

    assert result == ExportResult("too_many_recordings", FOLDER)
    error = "more than 2 recordings (EXPORT_MAX_RECORDINGS)"
    assert export_result.calls == [(MEETING_UUID, "failed", S3_PATH, error)]
    assert notetaker.calls == []
    assert meeting_api.master_calls == []
    assert storage.list_keys(EXPORT_BUCKET, BASE) == [BASE + "_export.json"]
    marker = storage.get_json(EXPORT_BUCKET, BASE + "_export.json")
    assert marker == {
        "state": "too_many_recordings",
        "meeting_id": MEETING_UUID,
        "vexa_meeting_id": 11367,
        "max_recordings": 2,
        "error": error,
    }
    assert [
        (r.levelno, r.getMessage())
        for r in caplog.records
        if r.getMessage().startswith("too_many_recordings")
    ] == [
        (
            logging.ERROR,
            "too_many_recordings vexa_meeting_id=11367 max_recordings=2; "
            "nothing exported",
        )
    ]


def test_a_meeting_at_the_recordings_cap_is_exported(storage: Storage) -> None:
    meeting_api, transcode = _seed_sessions(storage, [SESSION_A, SESSION_B])
    for uid in ("uid-a", "uid-b"):
        _put_activity(storage, uid, [header()])
    deps = _deps(
        storage,
        meeting_api,
        FakeNotetaker(),
        settings=_settings(max_recordings=2),
        transcode=transcode,
        join_webm=FakeJoinWebm(),
    )

    assert export_meeting(_envelope(), deps).state == "handed_off"
