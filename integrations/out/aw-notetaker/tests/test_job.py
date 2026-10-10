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
    NotAV2Meeting,
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
    jitsi_lines,
    meeting_event,
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
    """A `meeting.completed` subscription delivery (webhook.v1 MeetingEvent)."""
    return meeting_event(**meeting_overrides)


# meeting-api's GET /recordings keeps only these keys of each media file
# (meeting_api/recordings/router.py LIST_MEDIA_FILE_KEYS); `metadata` is on
# GET /recordings/{id} alone. The fake lists the same way, so a test that
# needs a channel's identity proves the job reads the full recording.
LIST_MEDIA_FILE_KEYS = ("id", "type", "format", "duration_seconds", "file_size_bytes")


def _list_row(recording: dict[str, Any]) -> dict[str, Any]:
    return {
        **recording,
        "media_files": [
            {k: v for k, v in media.items() if k in LIST_MEDIA_FILE_KEYS}
            for media in recording.get("media_files", [])
        ],
    }


class FakeMeetingApi:
    def __init__(
        self,
        recordings: list[dict[str, Any]] | None = None,
        master: dict[str, Any] | None = None,
        transcript: dict[str, Any] | None = None,
        masters: dict[int, dict[str, Any]] | None = None,
        channel_masters: dict[tuple[int, str], dict[str, Any]] | None = None,
    ) -> None:
        self.recordings = recordings if recordings is not None else []
        self._master = master
        self._masters = masters
        self._transcript = transcript
        self.list_recordings_calls: list[int] = []
        self.master_calls: list[int] = []
        self.channel_master_calls: list[tuple[int, str]] = []
        self.transcript_calls: list[int] = []
        self._channel_masters = channel_masters or {}

    def list_recordings(
        self, meeting_id: int, max_recordings: int
    ) -> list[dict[str, Any]]:
        self.list_recordings_calls.append(meeting_id)
        if len(self.recordings) > max_recordings:
            raise TooManyRecordings(meeting_id, max_recordings)
        return [_list_row(rec) for rec in self.recordings]

    def recording(self, recording_id: int) -> dict[str, Any]:
        return next(rec for rec in self.recordings if rec["id"] == recording_id)

    def master(self, recording_id: int, media_type: str = "audio") -> dict[str, Any]:
        if media_type != "audio":
            self.channel_master_calls.append((recording_id, media_type))
            return self._channel_masters[(recording_id, media_type)]
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
        self.reruns: list[bool] = []

    def process(
        self, meeting_id: str, s3_path: str, platform: str, *, rerun: bool = False
    ) -> None:
        self.calls.append((meeting_id, s3_path, platform))
        self.reruns.append(rerun)


class RaisingNotetaker:
    def process(
        self, meeting_id: str, s3_path: str, platform: str, *, rerun: bool = False
    ) -> None:
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
    storage_path = "recordings/7/855958819514/01ba075a-test/audio/master.webm"
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
        BASE + "speaker_activity_frames.json",
        BASE + "live_transcript.json",
        BASE + "channels/index.json",
        BASE + "_export.json",
    }
    assert storage.get_json(EXPORT_BUCKET, BASE + "channels/index.json") == []
    assert notetaker.calls == [(MEETING_UUID, BASE, "google_meet")]
    assert storage.get_bytes(EXPORT_BUCKET, BASE + "master.webm") == b"webm-bytes"
    assert storage.get_json(EXPORT_BUCKET, BASE + "speaker_activity_frames.json") == [
        {"t_rel": 0.0, "ch": 0, "name": "Ann Lee", "rms": 0.2, "dur_ms": 256},
        {"t_rel": 0.256, "ch": 0, "name": "Ann Lee", "rms": 0.0, "dur_ms": 256},
    ]
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
    assert meeting_json == _envelope()["data"]["meeting"]
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


@pytest.mark.parametrize("missing", ["id", "upstream_id"])
def test_a_meeting_without_its_ids_fails_loudly_and_writes_nothing(
    storage: Storage, missing: str
) -> None:
    storage_path = _put_master(storage, 51, "uid-51")
    notetaker = FakeNotetaker()
    meeting_api = _api_for(51, storage_path)
    export_result = FakeExportResult()
    deps = _deps(storage, meeting_api, notetaker, export_result=export_result)
    envelope = _envelope()
    del envelope["data"]["meeting"][missing]

    with pytest.raises(NotAV2Meeting, match="not a v2 meeting"):
        export_meeting(envelope, deps)

    assert storage.list_keys(EXPORT_BUCKET, "") == []
    assert meeting_api.list_recordings_calls == []
    assert notetaker.calls == []
    assert export_result.calls == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"id": ""},
        {"id": "   "},
        {"id": None},
        {"id": 11367},  # the old system hook's integer id
        {"upstream_id": "11367"},
        {"upstream_id": True},
    ],
)
def test_a_meeting_whose_ids_are_not_the_v2_ones_is_not_exported(
    storage: Storage, overrides: dict[str, Any]
) -> None:
    deps = _deps(storage, FakeMeetingApi(), FakeNotetaker())

    with pytest.raises(NotAV2Meeting):
        export_meeting(_envelope(**overrides), deps)

    assert storage.list_keys(EXPORT_BUCKET, "") == []


def test_the_speaker_activity_is_read_under_the_recordings_owner(
    storage: Storage,
) -> None:
    """The §2.4 meeting has no user id: the bot's signal files are keyed by
    the owner its recording's storage path names."""
    storage_path = "recordings/42/77/uid-owner/audio/master.webm"
    storage.put_bytes(VEXA_BUCKET, storage_path, b"webm-bytes", "video/webm")
    storage.put_bytes(
        VEXA_BUCKET,
        "signal/42/11367/uid-owner/speaker-activity.jsonl",
        ("\n".join(two_speaker_gmeet_lines(_origin_ms())) + "\n").encode(),
        "application/x-ndjson",
    )
    deps = _deps(storage, _api_for(77, storage_path), FakeNotetaker())

    export_meeting(_envelope(), deps)

    marker = storage.get_json(EXPORT_BUCKET, BASE + "_export.json")
    assert marker["speaker_activity"] == "ok"
    assert marker["speaker_activity_events"] > 0


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
    storage_path = "recordings/7/855958819514/01ba075a-test/audio/master.webm"
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


def test_a_rerun_exports_a_handed_off_folder_again_and_hands_it_on_as_a_rerun(
    storage: Storage,
) -> None:
    storage_path = "recordings/7/855958819514/01ba075a-test/audio/master.webm"
    storage.put_bytes(VEXA_BUCKET, storage_path, b"webm-bytes", "video/webm")
    first = FakeNotetaker()
    export_meeting(
        _envelope(),
        _deps(
            storage,
            FakeMeetingApi(
                recordings=[_audio_recording(855958819514)],
                master={"storage_path": storage_path},
            ),
            first,
        ),
    )
    storage.delete(EXPORT_BUCKET, f"recordings/{FOLDER}/audio.wav")

    meeting_api = FakeMeetingApi(
        recordings=[_audio_recording(855958819514)],
        master={"storage_path": storage_path},
    )
    rerun = FakeNotetaker()
    result = export_meeting(_envelope(), _deps(storage, meeting_api, rerun), True)

    assert result == ExportResult("handed_off", FOLDER)
    assert meeting_api.list_recordings_calls != []
    assert storage.size(EXPORT_BUCKET, f"recordings/{FOLDER}/audio.wav") is not None
    assert (first.reruns, rerun.reruns) == ([False], [True])


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
    storage_path = "recordings/7/2/uid-1/audio/master.webm"
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


def test_a_transcript_with_segments_is_written_as_live_transcript(
    storage: Storage,
) -> None:
    storage_path = "recordings/7/3/uid-2/audio/master.webm"
    storage.put_bytes(VEXA_BUCKET, storage_path, b"webm-bytes", "video/webm")
    meeting_api = FakeMeetingApi(
        recordings=[_audio_recording(3)],
        master={"storage_path": storage_path},
        transcript={"segments": ["hi"]},
    )
    notetaker = FakeNotetaker()
    deps = _deps(storage, meeting_api, notetaker)

    result = export_meeting(_envelope(), deps)

    assert result.state == "handed_off"
    assert meeting_api.transcript_calls == [11367]
    assert storage.get_json(EXPORT_BUCKET, BASE + "live_transcript.json") == {
        "segments": ["hi"]
    }


@pytest.mark.parametrize("transcript", [None, {"segments": []}, {}])
def test_a_meeting_without_transcript_segments_gets_an_empty_live_transcript(
    storage: Storage, transcript: dict[str, Any] | None
) -> None:
    """A bot that ran without live transcription leaves no segments (or no
    transcript at all, a 404): `live_transcript.json` has no segments, so a
    rerun never leaves an earlier run's transcript behind."""
    storage_path = _put_master(storage, 3, "uid-2")
    meeting_api = FakeMeetingApi(
        recordings=[_audio_recording(3)],
        master={"storage_path": storage_path},
        transcript=transcript,
    )
    deps = _deps(storage, meeting_api, FakeNotetaker())

    result = export_meeting(_envelope(), deps)

    assert result.state == "handed_off"
    assert meeting_api.transcript_calls == [11367]
    assert storage.get_json(EXPORT_BUCKET, BASE + "live_transcript.json") == {
        "segments": []
    }


def test_notetaker_failure_propagates_and_leaves_no_handoff_marker(
    storage: Storage,
) -> None:
    storage_path = "recordings/7/4/uid-3/audio/master.webm"
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
    storage_path = "recordings/7/5/uid-4/audio/master.webm"
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
    assert storage.get_json(EXPORT_BUCKET, BASE + "speaker_activity_frames.json") == []
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
    storage_path = "recordings/7/5/uid-4b/audio/master.webm"
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
    storage_path = "recordings/7/6/uid-5/audio/master.webm"
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
    storage_path = f"recordings/7/{rec_id}/{uid}/audio/master.webm"
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


def test_naive_ended_at_is_treated_as_utc_for_the_activity_wait(
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
        export_meeting(_envelope(ended_at="2026-06-18T10:42:00"), deps)


def test_null_ended_at_does_not_wait_forever_for_speaker_activity(
    storage: Storage,
) -> None:
    """A null ended_at has no fixed deadline to wait against (anchoring on
    deps.now() would move the deadline on every retry), so the job does not
    wait: it hands off with speaker_activity "missing"."""
    storage_path = _put_master(storage, 24, "uid-24")
    notetaker = FakeNotetaker()
    deps = _deps(storage, _api_for(24, storage_path), notetaker)

    result = export_meeting(_envelope(ended_at=None), deps)

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

    item = queue.load(MEETING_UUID)
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
    assert storage.get_json(EXPORT_BUCKET, BASE + "speaker_activity_frames.json") == []
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
    assert spy.uploads == [
        (BASE + "audio.wav", "audio/wav"),
        (BASE + "speaker_activity_frames.json", "application/json"),
    ]


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
            {"meeting_url": "https://meet.google.com/abc-defg-hij"},
            "https://meet.google.com/abc-defg-hij",
        ),
        ({"meeting_url": None}, "abc-defg-hij"),
    ],
)
def test_room_name_is_the_meeting_url_else_the_room(
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

    result = export_meeting(_envelope(), deps)

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
        "speaker_activity_frames.json",
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
        "speaker_activity_frames.json",
        "live_transcript.json",
        "channels/index.json",
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
        path = f"recordings/7/{session.rec_id}/{session.uid}/audio/master.webm"
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


# ---------------------------------------------------------------------------
# A meeting that ended `failed` after its bot recorded (§6.9 F-K2, item 60)
# ---------------------------------------------------------------------------


def _failed_envelope(**meeting_overrides: Any) -> dict[str, Any]:
    envelope = _envelope(
        status="failed", completion_reason="bot_crashed", **meeting_overrides
    )
    envelope["event_type"] = "bot.failed"
    return envelope


def test_a_failed_meeting_with_recordings_is_exported_like_a_completed_one(
    storage: Storage,
) -> None:
    meeting_api, transcode = _seed_sessions(storage, [SESSION_A, SESSION_B])
    _put_activity(storage, "uid-a", two_speaker_gmeet_lines(SESSION_A.origin_ms))
    _put_activity(
        storage, "uid-b", [header()] + _speaks(SESSION_B.origin_ms, "Speaker Gamma", 0)
    )
    notetaker = FakeNotetaker()
    export_result = FakeExportResult()
    deps = _deps(
        storage,
        meeting_api,
        notetaker,
        transcode=transcode,
        join_webm=FakeJoinWebm(),
        export_result=export_result,
    )

    assert export_meeting(_failed_envelope(), deps) == ExportResult(
        "handed_off", FOLDER
    )

    assert set(storage.list_keys(EXPORT_BUCKET, BASE)) == EXPECTED_KEYS
    assert notetaker.calls == [(MEETING_UUID, BASE, "google_meet")]
    assert export_result.calls == [(MEETING_UUID, "handed_off", S3_PATH, None)]
    samples, rate = wav_samples(storage.get_bytes(EXPORT_BUCKET, BASE + "audio.wav"))
    assert len(samples) / rate == 130.0
    timeline = storage.get_json(EXPORT_BUCKET, BASE + "speaker_timeline.json")
    assert _intervals(timeline) == [
        ("Speaker Alpha", 0.0, 0.768),
        ("Speaker Beta", 1.5, 2.268),
        ("Speaker Gamma", 90.0, 90.768),
    ]
    assert storage.get_json(EXPORT_BUCKET, BASE + "meeting.json")["status"] == "failed"


def test_a_failed_meeting_without_a_recording_is_skipped_with_a_log_line(
    storage: Storage, caplog: pytest.LogCaptureFixture
) -> None:
    failed = {
        "id": 80,
        "status": "failed",
        "created_at": RECORDING_CREATED_AT,
        "media_files": [],
    }
    notetaker = FakeNotetaker()
    export_result = FakeExportResult()
    meeting_api = FakeMeetingApi(recordings=[failed])
    deps = _deps(storage, meeting_api, notetaker, export_result=export_result)

    with caplog.at_level(logging.INFO, logger="exporter"):
        result = export_meeting(_failed_envelope(), deps)

    assert result == ExportResult("skipped", FOLDER)
    assert storage.list_keys(EXPORT_BUCKET, "") == []
    assert notetaker.calls == []
    assert export_result.calls == []
    assert meeting_api.master_calls == []
    assert any(
        r.getMessage()
        == "bot_failed_skipped vexa_meeting_id=11367 reason=no_recording; "
        "nothing exported"
        for r in caplog.records
    )


def test_a_completed_meeting_without_audio_still_reports_a_failed_export(
    storage: Storage,
) -> None:
    export_result = FakeExportResult()
    deps = _deps(
        storage, FakeMeetingApi(), FakeNotetaker(), export_result=export_result
    )

    assert export_meeting(_envelope(), deps).state == "no_audio"
    assert export_result.calls == [
        (MEETING_UUID, "failed", S3_PATH, "no audio recording")
    ]


def test_duplicate_and_crossed_events_for_one_meeting_export_once(
    storage: Storage,
) -> None:
    """bot.failed twice and a meeting.completed for the same meeting, through
    the durable queue: one folder, one /process."""
    meeting_api, transcode = _seed_sessions(storage, [SESSION_A])
    _put_activity(storage, "uid-a", two_speaker_gmeet_lines(SESSION_A.origin_ms))
    notetaker = FakeNotetaker()
    deps = _deps(
        storage, meeting_api, notetaker, transcode=transcode, join_webm=FakeJoinWebm()
    )
    queue = PendingQueue(storage, VEXA_BUCKET)

    queue.enqueue(_failed_envelope())
    queue.enqueue(_failed_envelope())
    assert queue.pending_ids() == [MEETING_UUID]
    asyncio.run(sweep_once(queue, deps, now=lambda: 1000.0))
    queue.enqueue(_envelope())
    asyncio.run(sweep_once(queue, deps, now=lambda: 2000.0))
    queue.enqueue(_failed_envelope())
    asyncio.run(sweep_once(queue, deps, now=lambda: 3000.0))

    assert notetaker.calls == [(MEETING_UUID, BASE, "google_meet")]
    assert meeting_api.master_calls == [70]
    assert queue.pending_ids() == []
    marker = storage.get_json(EXPORT_BUCKET, BASE + "_export.json")
    assert marker["state"] == "handed_off"


def _split_wav(dst: Path, loud_s: float, silent_s: float, value: int = 3000) -> None:
    """Mono s16: `loud_s` of `value`, then `silent_s` of silence. Rate 100."""
    rate = 100
    loud = value.to_bytes(2, "little", signed=True) * int(loud_s * rate)
    silent = b"\x00\x00" * int(silent_s * rate)
    with wave.open(str(dst), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(loud + silent)


def test_a_quiet_long_turn_is_audio_mismatch_and_is_not_handed_off(
    storage: Storage,
) -> None:
    """Ann is loud for her 15 s; Bo's 15 s is silence. Both intervals are
    written. /process is not called. The report is failed."""
    storage_path = _put_master(storage, 91, "uid-91")
    origin = _origin_ms()
    _put_activity(
        storage,
        "uid-91",
        [
            header("pertrack"),
            frame(origin, "Ann Lee", 0.1, ch=0, dur_ms=15_000),
            frame(origin + 15_000, "Bo", 0.1, ch=1, dur_ms=15_000),
        ],
    )

    def transcode(src: Path, dst: Path) -> None:
        _split_wav(dst, 15, 15)

    notetaker = FakeNotetaker()
    export_result = FakeExportResult()
    deps = _deps(
        storage,
        _api_for(91, storage_path),
        notetaker,
        transcode=transcode,
        export_result=export_result,
    )

    result = export_meeting(_envelope(), deps)

    assert result.state == "audio_mismatch"
    assert notetaker.calls == []
    assert export_result.calls[0][0] == MEETING_UUID
    assert export_result.calls[0][1] == "failed"
    assert export_result.calls[0][2] == S3_PATH
    assert export_result.calls[0][3] is not None
    assert "Bo" in export_result.calls[0][3]
    marker = storage.get_json(EXPORT_BUCKET, BASE + "_export.json")
    assert marker["state"] == "audio_mismatch"
    assert marker["speaker_activity"] == "audio_mismatch"
    assert set(marker["speaker_levels"]) == {"Ann Lee", "Bo"}
    assert marker["speaker_levels"]["Bo"]["rms"] < 0.026
    assert marker["speaker_levels"]["Ann Lee"]["rms"] >= 0.026
    timeline = storage.get_json(EXPORT_BUCKET, BASE + "speaker_timeline.json")
    assert {i["speaker_name"] for i in timeline["speaker_intervals"]} == {
        "Ann Lee",
        "Bo",
    }
    assert timeline["speaker_intervals_source"] == "audio"


def test_both_long_turns_loud_still_hands_off(storage: Storage) -> None:
    storage_path = _put_master(storage, 92, "uid-92")
    origin = _origin_ms()
    _put_activity(
        storage,
        "uid-92",
        [
            header("pertrack"),
            frame(origin, "Ann Lee", 0.1, ch=0, dur_ms=15_000),
            frame(origin + 15_000, "Bo", 0.1, ch=1, dur_ms=15_000),
        ],
    )

    def transcode(src: Path, dst: Path) -> None:
        write_constant_wav(dst, 30, 3000)

    notetaker = FakeNotetaker()
    deps = _deps(storage, _api_for(92, storage_path), notetaker, transcode=transcode)

    assert export_meeting(_envelope(), deps).state == "handed_off"
    assert notetaker.calls == [(MEETING_UUID, BASE, "google_meet")]
    marker = storage.get_json(EXPORT_BUCKET, BASE + "_export.json")
    assert marker["speaker_activity"] == "ok"


def test_channels_are_written_as_recorded_with_their_offset(
    storage: Storage, caplog: pytest.LogCaptureFixture
) -> None:
    """Two sessions share ch0. Session B's recorder starts inside A's piece,
    so the two are joined into one opus file with B's piece right after A's,
    and the kept name stays Ada's. ch1 has one piece: its recorder's master is
    copied as it stands, and its row says it starts 90 s into the meeting,
    where B's mixed audio sits. A `chunk` media file is not a channel. Only the
    joined channel's pieces are decoded, to measure them."""
    origin_a = SESSION_A.origin_ms
    origin_b = SESSION_B.origin_ms
    recorder_b = origin_b - 85_000  # places ch0 at 5 s, inside A's 10 s piece

    def put(key: str, body: bytes) -> None:
        storage.put_bytes(VEXA_BUCKET, key, body, "video/webm")

    audio_a = f"recordings/7/{SESSION_A.rec_id}/{SESSION_A.uid}/audio/master.webm"
    audio_b = f"recordings/7/{SESSION_B.rec_id}/{SESSION_B.uid}/audio/master.webm"
    ch0_a = f"recordings/7/{SESSION_A.rec_id}/{SESSION_A.uid}/ch0/master.webm"
    ch0_b = f"recordings/7/{SESSION_B.rec_id}/{SESSION_B.uid}/ch0/master.webm"
    ch1_b = f"recordings/7/{SESSION_B.rec_id}/{SESSION_B.uid}/ch1/master.webm"
    put(audio_a, b"webm-a")
    put(audio_b, b"webm-b")
    put(ch0_a, b"ch0-a")
    put(ch0_b, b"ch0-b")
    put(ch1_b, b"ch1-b")
    _put_activity(
        storage,
        SESSION_A.uid,
        [
            header(),
            frame(origin_a, "Speaker Alpha", 0.2, ch=0),
            json.dumps({"t": origin_a + 1000, "rms": 0.4, "dur_ms": 256}),
        ],
    )
    _put_activity(
        storage,
        SESSION_B.uid,
        [header(), frame(origin_b, "Speaker Gamma", 0.3, ch=1)],
    )

    def recording(
        session: _Session, media_files: list[dict[str, Any]]
    ) -> dict[str, Any]:
        return {
            "id": session.rec_id,
            "status": "completed",
            "created_at": session.created_at,
            "media_files": media_files,
        }

    meeting_api = FakeMeetingApi(
        recordings=[
            recording(
                SESSION_B,
                [
                    {"type": "audio", "format": "webm"},
                    {
                        "type": "ch0",
                        "format": "webm",
                        "metadata": {
                            "recorder_start_epoch_ms": recorder_b,
                            "channel_kind": "jitsi",
                            "stream_id": "remote-audio-9",
                            "display_name": "Bob",
                            "ignored": "nope",
                        },
                    },
                    {
                        "type": "ch1",
                        "format": "webm",
                        "metadata": {
                            "recorder_start_epoch_ms": origin_b,
                            "channel_kind": "jitsi",
                            "stream_id": "remote-audio-1",
                        },
                    },
                ],
            ),
            recording(
                SESSION_A,
                [
                    {"type": "audio", "format": "webm"},
                    {"type": "chunk", "format": "webm"},
                    {
                        "type": "ch0",
                        "format": "webm",
                        "metadata": {
                            "recorder_start_epoch_ms": origin_a,
                            "channel_kind": "jitsi",
                            "stream_id": "remote-audio-0",
                            "participant_id": "p-a",
                            "display_name": "Ada",
                            "ignored": "nope",
                        },
                    },
                ],
            ),
        ],
        masters={
            SESSION_A.rec_id: {"storage_path": audio_a},
            SESSION_B.rec_id: {"storage_path": audio_b},
        },
        channel_masters={
            (SESSION_A.rec_id, "ch0"): {"storage_path": ch0_a},
            (SESSION_B.rec_id, "ch0"): {"storage_path": ch0_b},
            (SESSION_B.rec_id, "ch1"): {"storage_path": ch1_b},
        },
    )
    bodies = {
        b"webm-a": (60.0, 1),
        b"webm-b": (40.0, 2),
        b"ch0-a": (10.0, 7),
        b"ch0-b": (10.0, 8),
        b"ch1-b": (5.0, 9),
    }

    decoded: list[bytes] = []

    def transcode(src: Path, dst: Path) -> None:
        body = src.read_bytes()
        decoded.append(body)
        seconds, value = bodies[body]
        write_constant_wav(dst, seconds, value)

    join = FakeJoinWebm()
    deps = _deps(
        storage,
        meeting_api,
        FakeNotetaker(),
        transcode=transcode,
        join_webm=join,
    )
    with caplog.at_level(logging.WARNING, logger="exporter"):
        assert export_meeting(_envelope(), deps).state == "handed_off"

    assert meeting_api.master_calls == [SESSION_A.rec_id, SESSION_B.rec_id]
    assert meeting_api.channel_master_calls == [
        (SESSION_A.rec_id, "ch0"),
        (SESSION_B.rec_id, "ch0"),
        (SESSION_B.rec_id, "ch1"),
    ]
    assert [body for body in decoded if body.startswith(b"ch")] == [b"ch0-a", b"ch0-b"]
    assert join.calls[-1] == [(b"ch0-a", 0.0), (b"ch0-b", 0.0)]
    assert (
        storage.get_bytes(EXPORT_BUCKET, BASE + "channels/ch0.webm") == b"joined-webm"
    )
    assert storage.get_bytes(EXPORT_BUCKET, BASE + "channels/ch1.webm") == b"ch1-b"
    assert storage.list_keys(EXPORT_BUCKET, BASE + "channels/") == [
        BASE + "channels/ch0.webm",
        BASE + "channels/ch1.webm",
        BASE + "channels/index.json",
    ]
    assert storage.get_json(EXPORT_BUCKET, BASE + "channels/index.json") == [
        {
            "channel": 0,
            "kind": "jitsi",
            "stream_id": "remote-audio-0",
            "offset_s": 0.0,
            "participant_id": "p-a",
            "display_name": "Ada",
            "file": "ch0.webm",
        },
        {
            "channel": 1,
            "kind": "jitsi",
            "stream_id": "remote-audio-1",
            "offset_s": 90.0,
            "file": "ch1.webm",
        },
    ]
    assert storage.get_json(EXPORT_BUCKET, BASE + "speaker_activity_frames.json") == [
        {
            "t_rel": 0.0,
            "ch": 0,
            "name": "Speaker Alpha",
            "rms": 0.2,
            "dur_ms": 256,
        },
        {"t_rel": 1.0, "ch": None, "name": None, "rms": 0.4, "dur_ms": 256},
        {
            "t_rel": 90.0,
            "ch": 1,
            "name": "Speaker Gamma",
            "rms": 0.3,
            "dur_ms": 256,
        },
    ]
    messages = [record.getMessage() for record in caplog.records]
    assert any(message.startswith("channel_overlap ") for message in messages)
    assert any(
        message.startswith("channel_display_name_differs ")
        and "display_name=Bob" in message
        and "kept=Ada" in message
        for message in messages
    )

    def tag_value(key: str) -> dict[str, str]:
        tags = storage._client.get_object_tagging(Bucket=EXPORT_BUCKET, Key=key)[
            "TagSet"
        ]
        return {tag["Key"]: tag["Value"] for tag in tags}

    assert tag_value(BASE + "channels/ch0.webm") == {"retention-class": "audio"}
    assert tag_value(BASE + "channels/ch1.webm") == {"retention-class": "audio"}
    # The index expires with the files it names.
    assert tag_value(BASE + "channels/index.json") == {"retention-class": "audio"}


def _gmeet_channel(channel: int) -> dict[str, Any]:
    """The identity a Meet channel recorder stamps on its first chunk."""
    return {
        "recorder_start_epoch_ms": _origin_ms(),
        "channel_kind": "gmeet",
        "stream_id": f"meet-stream-{channel}",
    }


def test_a_channel_without_its_recorder_identity_is_not_exported(
    storage: Storage, caplog: pytest.LogCaptureFixture
) -> None:
    """A channel that cannot be named or placed is not exported: no index,
    the reason logged, and the mixed export handed off as usual."""
    storage_path = _put_master(storage, 21, "uid-21")
    _put_activity(storage, "uid-21", two_speaker_gmeet_lines(_origin_ms()))
    ch0_path = "recordings/7/21/uid-21/ch0/master.webm"
    storage.put_bytes(VEXA_BUCKET, ch0_path, b"ch0", "video/webm")
    meeting_api = FakeMeetingApi(
        recordings=[
            _audio_recording(
                21,
                media_files=[
                    {"type": "audio", "format": "webm"},
                    {
                        "type": "ch0",
                        "format": "webm",
                        "metadata": {"sample_rate": 48000},
                    },
                ],
            )
        ],
        master={"storage_path": storage_path},
        channel_masters={(21, "ch0"): {"storage_path": ch0_path}},
    )
    notetaker = FakeNotetaker()
    deps = _deps(storage, meeting_api, notetaker)
    with caplog.at_level(logging.WARNING, logger="exporter"):
        assert export_meeting(_envelope(), deps).state == "handed_off"

    assert storage.list_keys(EXPORT_BUCKET, BASE + "channels/") == [
        BASE + "channels/index.json"
    ]
    assert storage.get_json(EXPORT_BUCKET, BASE + "channels/index.json") == []
    assert notetaker.calls == [(MEETING_UUID, BASE, "google_meet")]
    assert any(
        "error_class=ChannelIdentityMissing" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.parametrize("failure", ["master", "copy", "recording"])
def test_a_failed_channel_still_hands_off_the_mixed_export_with_an_empty_index(
    storage: Storage, caplog: pytest.LogCaptureFixture, failure: str
) -> None:
    """ch0 is fine; ch1's master cannot be built (every master is asked for
    before any file is written, so nothing is), or its object is missing and
    the copy fails after ch0 is copied (ch0.webm is left; without an index the
    worker ignores it), or the full recording that holds the channels'
    identities cannot be read. The mixed export is handed off as usual and
    `channels/index.json` is `[]`, so no earlier run's index survives."""
    storage_path = _put_master(storage, 20, "uid-20")
    _put_activity(storage, "uid-20", two_speaker_gmeet_lines(_origin_ms()))
    ch0_path = "recordings/7/20/uid-20/ch0/master.webm"
    ch1_path = "recordings/7/20/uid-20/ch1/master.webm"
    storage.put_bytes(VEXA_BUCKET, ch0_path, b"ch0", "video/webm")
    channel_masters = {(20, "ch0"): {"storage_path": ch0_path}}
    if failure == "copy":
        channel_masters[(20, "ch1")] = {"storage_path": ch1_path}
    meeting_api = FakeMeetingApi(
        recordings=[
            _audio_recording(
                20,
                media_files=[
                    {"type": "audio", "format": "webm"},
                    {"type": "ch0", "format": "webm", "metadata": _gmeet_channel(0)},
                    {"type": "ch1", "format": "webm", "metadata": _gmeet_channel(1)},
                ],
            )
        ],
        master={"storage_path": storage_path},
        channel_masters=channel_masters,
    )
    if failure == "recording":

        def unreadable(recording_id: int) -> dict[str, Any]:
            raise RuntimeError("gateway 503 /recordings/20")

        meeting_api.recording = unreadable  # type: ignore[method-assign]
    notetaker = FakeNotetaker()
    deps = _deps(storage, meeting_api, notetaker)
    with caplog.at_level(logging.WARNING, logger="exporter"):
        assert export_meeting(_envelope(), deps).state == "handed_off"

    assert notetaker.calls == [(MEETING_UUID, BASE, "google_meet")]
    expected_left = [BASE + "channels/ch0.webm"] if failure == "copy" else []
    assert storage.list_keys(EXPORT_BUCKET, BASE + "channels/") == sorted(
        [BASE + "channels/index.json", *expected_left]
    )
    assert storage.get_json(EXPORT_BUCKET, BASE + "channels/index.json") == []
    assert storage.get_json(EXPORT_BUCKET, BASE + "_export.json")["state"] == (
        "handed_off"
    )
    assert storage.exists(EXPORT_BUCKET, BASE + "audio.wav")
    assert any(
        record.getMessage().startswith(
            f"channel_export_failed meeting_id={MEETING_UUID} vexa_meeting_id=11367 "
        )
        for record in caplog.records
    )


def test_a_rerun_over_a_wav_export_indexes_only_the_opus_files(
    storage: Storage,
) -> None:
    """A rerun over an earlier export: the new index names only this export's
    opus files. The earlier wavs are not deleted (the exporter deletes
    nothing); no index names them and they expire with their retention."""
    storage_path = _put_master(storage, 22, "uid-22")
    _put_activity(storage, "uid-22", two_speaker_gmeet_lines(_origin_ms()))
    ch0_path = "recordings/7/22/uid-22/ch0/master.webm"
    storage.put_bytes(VEXA_BUCKET, ch0_path, b"ch0", "video/webm")
    for stale in ("ch0.wav", "ch5.wav"):
        storage.put_bytes(EXPORT_BUCKET, BASE + "channels/" + stale, b"x", "audio/wav")
    storage.put_json(EXPORT_BUCKET, BASE + "channels/index.json", [{"channel": 5}])
    meeting_api = FakeMeetingApi(
        recordings=[
            _audio_recording(
                22,
                media_files=[
                    {"type": "audio", "format": "webm"},
                    {"type": "ch0", "format": "webm", "metadata": _gmeet_channel(0)},
                ],
            )
        ],
        master={"storage_path": storage_path},
        channel_masters={(22, "ch0"): {"storage_path": ch0_path}},
    )
    deps = _deps(storage, meeting_api, FakeNotetaker())
    assert export_meeting(_envelope(), deps).state == "handed_off"

    assert storage.list_keys(EXPORT_BUCKET, BASE + "channels/") == [
        BASE + "channels/ch0.wav",
        BASE + "channels/ch0.webm",
        BASE + "channels/ch5.wav",
        BASE + "channels/index.json",
    ]
    assert [
        row["file"]
        for row in storage.get_json(EXPORT_BUCKET, BASE + "channels/index.json")
    ] == ["ch0.webm"]


@pytest.mark.parametrize("mix_named", [True, False])
def test_channel_tap_frames_change_no_timeline_and_are_the_only_frames_written(
    storage: Storage, mix_named: bool
) -> None:
    """A Jitsi session with tap frames: speaker_timeline.json and
    participants.json match the same file without them, and
    speaker_activity_frames.json holds only the tap frames."""
    origin = _origin_ms()
    storage_path = _put_master(storage, 20, "uid-20")

    def export(taps: bool) -> tuple[Any, Any, Any]:
        storage.delete(EXPORT_BUCKET, BASE + "_export.json")
        _put_activity(
            storage, "uid-20", jitsi_lines(origin, mix_named=mix_named, taps=taps)
        )
        deps = _deps(storage, _api_for(20, storage_path), FakeNotetaker())
        assert export_meeting(_envelope(), deps).state == "handed_off"
        return (
            storage.get_json(EXPORT_BUCKET, BASE + "speaker_timeline.json"),
            storage.get_json(EXPORT_BUCKET, BASE + "participants.json"),
            storage.get_json(EXPORT_BUCKET, BASE + "speaker_activity_frames.json"),
        )

    timeline, participants, frames = export(taps=True)
    plain_timeline, plain_participants, plain_frames = export(taps=False)

    assert timeline == plain_timeline
    assert timeline["speaker_timeline"] or timeline["speaker_intervals"]
    assert participants == plain_participants
    assert frames == [
        {"t_rel": 0.1, "ch": 0, "name": "Tap Zero", "rms": 0.3, "dur_ms": 256},
        {"t_rel": 0.356, "ch": 0, "name": None, "rms": 0.01, "dur_ms": 256},
        {"t_rel": 1.6, "ch": 1, "name": "Tap One", "rms": 0.4, "dur_ms": 256},
        {"t_rel": 5.0, "ch": 1, "name": "Tap One", "rms": 0.4, "dur_ms": 256},
    ]
    assert len(plain_frames) == 3
    assert all(row["ch"] == 0 for row in plain_frames)


def test_exactly_ten_seconds_of_silent_coverage_still_hands_off(
    storage: Storage,
) -> None:
    """The gate is strict: 10 s of silence is not a mismatch."""
    storage_path = _put_master(storage, 93, "uid-93")
    _put_activity(
        storage,
        "uid-93",
        [header(), frame(_origin_ms(), "Ann Lee", 0.1, dur_ms=10_000)],
    )
    notetaker = FakeNotetaker()
    deps = _deps(storage, _api_for(93, storage_path), notetaker)

    assert export_meeting(_envelope(), deps).state == "handed_off"
    assert notetaker.calls == [(MEETING_UUID, BASE, "google_meet")]
