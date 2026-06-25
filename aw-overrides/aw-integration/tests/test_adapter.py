"""Tests for aw_integration adapter, s3_writer, and notetaker_client (§13.15).

Run with:
    PYTHONPATH=../../../notetaker-common:. pytest tests/test_adapter.py
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from aw_integration.adapter import (
    VexaSegment,
    VexaSession,
    VexaSpeakerEvent,
    VexaSessionAdapter,
)
from aw_integration.notetaker_client import NoteTakerClientWrapper
from aw_integration.s3_writer import S3Writer
from notetaker_common.schemas import (
    BotJob,
    ConsentConfig,
    JoinConfig,
    LiveStreamingConfig,
    MetadataFile,
    ParticipantsFile,
    ProcessRequest,
    RetryConfig,
    SpeakerTimelineFile,
)

# ---------------------------------------------------------------------------
# Helpers / shared fixtures
# ---------------------------------------------------------------------------

SESSION_START = datetime(2024, 1, 15, 9, 0, 0, tzinfo=timezone.utc)
SESSION_END = datetime(2024, 1, 15, 9, 1, 0, tzinfo=timezone.utc)


def make_test_job() -> BotJob:
    return BotJob(
        job_id="test-job-ulid",
        meeting_id="42",
        platform="meet",
        scheduled_start_at=datetime(2026, 6, 22, 9, 0, 0, tzinfo=timezone.utc),
        expected_duration_min=30,
        hard_deadline_at=datetime(2026, 6, 22, 13, 0, 0, tzinfo=timezone.utc),
        join=JoinConfig(
            url="https://meet.google.com/abc-defg-hij",
            organizer_email="host@abroadworks.com",
            requires_admit=False,
        ),
        display_name="AW Notetaker",
        consent=ConsentConfig(
            state="explicit",
            recorded_at=datetime(2026, 6, 22, 8, 55, 0, tzinfo=timezone.utc),
            by_user_id="user-001",
        ),
        retry=RetryConfig(attempts=0, max_attempts=2),
        s3_key="recordings/meet_42_test-job-ulid/",
        live_streaming=LiveStreamingConfig(enabled=False),
    )


def make_test_session(
    with_speaker_events: bool = True,
) -> VexaSession:
    segments = [
        VexaSegment(
            speaker="Alice Chen",
            text="Good morning everyone, let us get started.",
            start=5.0,
            end=9.2,
            language="en",
            completed=True,
            segment_id="seg-001",
            absolute_start_time=datetime(2024, 1, 15, 9, 0, 5, tzinfo=timezone.utc),
            absolute_end_time=datetime(2024, 1, 15, 9, 0, 9, tzinfo=timezone.utc),
        ),
        VexaSegment(
            speaker="Bob Müller",
            text="Thanks Alice. I have reviewed the proposal.",
            start=20.0,
            end=25.5,
            language="en",
            completed=True,
            segment_id="seg-002",
            absolute_start_time=datetime(2024, 1, 15, 9, 0, 20, tzinfo=timezone.utc),
            absolute_end_time=datetime(2024, 1, 15, 9, 0, 25, tzinfo=timezone.utc),
        ),
    ]
    speaker_events = (
        [
            VexaSpeakerEvent(
                uid="conn-abc123def456",
                relative_ms=4500,
                event_type="SPEAKER_START",
                participant_name="Alice Chen",
                meeting_id="42",
            ),
            VexaSpeakerEvent(
                uid="conn-abc123def456",
                relative_ms=9500,
                event_type="SPEAKER_END",
                participant_name="Alice Chen",
                meeting_id="42",
            ),
            VexaSpeakerEvent(
                uid="conn-abc123def456",
                relative_ms=19800,
                event_type="SPEAKER_START",
                participant_name="Bob Müller",
                meeting_id="42",
            ),
        ]
        if with_speaker_events
        else []
    )
    return VexaSession(
        uid="conn-abc123def456",
        platform="google_meet",
        meeting_id="42",
        session_start_ts=SESSION_START,
        session_end_ts=SESSION_END,
        segments=segments,
        speaker_events=speaker_events,
    )


def make_adapter(
    tmp_path: Path,
    pcm_bytes: bytes | None = None,
    with_speaker_events: bool = True,
) -> tuple[VexaSessionAdapter, MagicMock, MagicMock]:
    """Return (adapter, mock_s3_writer, mock_notetaker_client)."""
    raw_audio = tmp_path / "audio.raw"
    if pcm_bytes is not None:
        raw_audio.write_bytes(pcm_bytes)

    s3_writer = MagicMock(spec=S3Writer)
    notetaker_client = MagicMock(spec=NoteTakerClientWrapper)
    notetaker_client.post_process = AsyncMock(return_value={"status": "ok"})

    adapter = VexaSessionAdapter(
        session=make_test_session(with_speaker_events=with_speaker_events),
        job=make_test_job(),
        audio_raw_path=raw_audio,
        s3_writer=s3_writer,
        notetaker_client=notetaker_client,
    )
    return adapter, s3_writer, notetaker_client


# ---------------------------------------------------------------------------
# VexaSessionAdapter.encode_wav
# ---------------------------------------------------------------------------


class TestEncodeWav:
    def test_raises_file_not_found_when_path_does_not_exist(
        self, tmp_path: Path
    ) -> None:
        adapter, _, _ = make_adapter(tmp_path)  # file not written — path missing
        with pytest.raises(FileNotFoundError):
            adapter.encode_wav()

    def test_raises_file_not_found_when_audio_file_is_empty(
        self, tmp_path: Path
    ) -> None:
        adapter, _, _ = make_adapter(tmp_path, pcm_bytes=b"")
        with pytest.raises(FileNotFoundError):
            adapter.encode_wav()

    def test_returns_bytes_for_valid_pcm_input(self, tmp_path: Path) -> None:
        silence = bytes(32000)  # 1 second of 16-bit silence at 16000 Hz
        adapter, _, _ = make_adapter(tmp_path, pcm_bytes=silence)
        result = adapter.encode_wav()
        assert isinstance(result, bytes)
        assert len(result) > 0

    def test_wav_bytes_start_with_riff_header(self, tmp_path: Path) -> None:
        silence = bytes(32000)
        adapter, _, _ = make_adapter(tmp_path, pcm_bytes=silence)
        result = adapter.encode_wav()
        assert result[:4] == b"RIFF"

    def test_wav_bytes_contain_wave_marker(self, tmp_path: Path) -> None:
        silence = bytes(32000)
        adapter, _, _ = make_adapter(tmp_path, pcm_bytes=silence)
        result = adapter.encode_wav()
        assert b"WAVE" in result


# ---------------------------------------------------------------------------
# VexaSessionAdapter.build_speaker_timeline
# ---------------------------------------------------------------------------


class TestBuildSpeakerTimeline:
    def test_returns_speaker_timeline_file_type(self, tmp_path: Path) -> None:
        adapter, _, _ = make_adapter(tmp_path, pcm_bytes=bytes(32000))
        result = adapter.build_speaker_timeline()
        assert isinstance(result, SpeakerTimelineFile)

    def test_meeting_id_matches_session(self, tmp_path: Path) -> None:
        adapter, _, _ = make_adapter(tmp_path, pcm_bytes=bytes(32000))
        result = adapter.build_speaker_timeline()
        assert result.meeting_id == "42"

    def test_platform_is_mapped_to_canonical_form(self, tmp_path: Path) -> None:
        adapter, _, _ = make_adapter(tmp_path, pcm_bytes=bytes(32000))
        result = adapter.build_speaker_timeline()
        assert result.platform == "meet"

    def test_duration_sec_equals_session_end_minus_start(self, tmp_path: Path) -> None:
        adapter, _, _ = make_adapter(tmp_path, pcm_bytes=bytes(32000))
        result = adapter.build_speaker_timeline()
        expected = (SESSION_END - SESSION_START).total_seconds()
        assert result.duration_sec == expected

    def test_speaker_timeline_has_one_entry_per_speaker_start_event(
        self, tmp_path: Path
    ) -> None:
        adapter, _, _ = make_adapter(
            tmp_path, pcm_bytes=bytes(32000), with_speaker_events=True
        )
        result = adapter.build_speaker_timeline()
        # fixture has 2 SPEAKER_START events (Alice and Bob)
        assert len(result.speaker_timeline) == 2

    def test_timestamp_ms_is_origin_plus_relative_ms(self, tmp_path: Path) -> None:
        adapter, _, _ = make_adapter(
            tmp_path, pcm_bytes=bytes(32000), with_speaker_events=True
        )
        result = adapter.build_speaker_timeline()
        origin_ms = int(SESSION_START.timestamp() * 1000)
        alice_event = next(
            e for e in result.speaker_timeline if e.speaker_name == "Alice Chen"
        )
        assert alice_event.timestamp_ms == origin_ms + 4500

    def test_speaker_id_is_slug_of_participant_name(self, tmp_path: Path) -> None:
        adapter, _, _ = make_adapter(
            tmp_path, pcm_bytes=bytes(32000), with_speaker_events=True
        )
        result = adapter.build_speaker_timeline()
        alice_event = next(
            e for e in result.speaker_timeline if e.speaker_name == "Alice Chen"
        )
        assert alice_event.speaker_id == "alice_chen"

    def test_fallback_derives_timeline_from_segments_when_no_speaker_events(
        self, tmp_path: Path
    ) -> None:
        adapter, _, _ = make_adapter(
            tmp_path, pcm_bytes=bytes(32000), with_speaker_events=False
        )
        result = adapter.build_speaker_timeline()
        # Two unique speakers in segments -> two fallback events
        assert len(result.speaker_timeline) == 2

    def test_fallback_event_speaker_id_matches_segment_speaker_slug(
        self, tmp_path: Path
    ) -> None:
        adapter, _, _ = make_adapter(
            tmp_path, pcm_bytes=bytes(32000), with_speaker_events=False
        )
        result = adapter.build_speaker_timeline()
        speaker_ids = {e.speaker_id for e in result.speaker_timeline}
        assert "alice_chen" in speaker_ids
        assert "bob_müller" in speaker_ids


# ---------------------------------------------------------------------------
# VexaSessionAdapter.build_participants
# ---------------------------------------------------------------------------


class TestBuildParticipants:
    def test_returns_participants_file_type(self, tmp_path: Path) -> None:
        adapter, _, _ = make_adapter(tmp_path, pcm_bytes=bytes(32000))
        result = adapter.build_participants()
        assert isinstance(result, ParticipantsFile)

    def test_meeting_id_matches_session(self, tmp_path: Path) -> None:
        adapter, _, _ = make_adapter(tmp_path, pcm_bytes=bytes(32000))
        result = adapter.build_participants()
        assert result.meeting_id == "42"

    def test_platform_is_mapped_to_canonical_form(self, tmp_path: Path) -> None:
        adapter, _, _ = make_adapter(tmp_path, pcm_bytes=bytes(32000))
        result = adapter.build_participants()
        assert result.platform == "meet"

    def test_host_id_is_host_zero(self, tmp_path: Path) -> None:
        adapter, _, _ = make_adapter(tmp_path, pcm_bytes=bytes(32000))
        result = adapter.build_participants()
        assert result.host.id == "host-0"

    def test_host_name_is_local_part_of_organizer_email(self, tmp_path: Path) -> None:
        adapter, _, _ = make_adapter(tmp_path, pcm_bytes=bytes(32000))
        result = adapter.build_participants()
        assert result.host.name == "host"

    def test_participants_list_contains_unique_speakers(self, tmp_path: Path) -> None:
        adapter, _, _ = make_adapter(tmp_path, pcm_bytes=bytes(32000))
        result = adapter.build_participants()
        participant_names = {p.name for p in result.participants}
        assert "Alice Chen" in participant_names
        assert "Bob Müller" in participant_names

    def test_participant_id_is_slug_of_name(self, tmp_path: Path) -> None:
        adapter, _, _ = make_adapter(tmp_path, pcm_bytes=bytes(32000))
        result = adapter.build_participants()
        alice = next(p for p in result.participants if p.name == "Alice Chen")
        assert alice.id == "alice_chen"

    def test_no_duplicate_participants(self, tmp_path: Path) -> None:
        # Alice appears in segments AND speaker_events — should deduplicate
        adapter, _, _ = make_adapter(
            tmp_path, pcm_bytes=bytes(32000), with_speaker_events=True
        )
        result = adapter.build_participants()
        ids = [p.id for p in result.participants]
        assert len(ids) == len(set(ids))


# ---------------------------------------------------------------------------
# VexaSessionAdapter.build_metadata
# ---------------------------------------------------------------------------


class TestBuildMetadata:
    def test_returns_metadata_file_type(self, tmp_path: Path) -> None:
        adapter, _, _ = make_adapter(tmp_path, pcm_bytes=bytes(32000))
        result = adapter.build_metadata()
        assert isinstance(result, MetadataFile)

    def test_actual_start_at_equals_session_start_ts(self, tmp_path: Path) -> None:
        adapter, _, _ = make_adapter(tmp_path, pcm_bytes=bytes(32000))
        result = adapter.build_metadata()
        assert result.actual_start_at == SESSION_START

    def test_bot_left_reason_default_is_host_ended(self, tmp_path: Path) -> None:
        adapter, _, _ = make_adapter(tmp_path, pcm_bytes=bytes(32000))
        result = adapter.build_metadata()
        assert result.bot_left_reason == "host_ended"

    def test_bot_left_reason_is_passed_through(self, tmp_path: Path) -> None:
        adapter, _, _ = make_adapter(tmp_path, pcm_bytes=bytes(32000))
        result = adapter.build_metadata(bot_left_reason="hard_deadline")
        assert result.bot_left_reason == "hard_deadline"

    def test_consent_state_matches_job(self, tmp_path: Path) -> None:
        adapter, _, _ = make_adapter(tmp_path, pcm_bytes=bytes(32000))
        result = adapter.build_metadata()
        assert result.consent_state == "explicit"

    def test_meeting_id_matches_session(self, tmp_path: Path) -> None:
        adapter, _, _ = make_adapter(tmp_path, pcm_bytes=bytes(32000))
        result = adapter.build_metadata()
        assert result.meeting_id == "42"


# ---------------------------------------------------------------------------
# VexaSessionAdapter.run (integration of the full pipeline)
# ---------------------------------------------------------------------------


class TestAdapterRun:
    @pytest.mark.asyncio
    async def test_run_calls_s3_writer_write_all_exactly_once(
        self, tmp_path: Path
    ) -> None:
        adapter, mock_s3, _ = make_adapter(tmp_path, pcm_bytes=bytes(32000))
        await adapter.run()
        mock_s3.write_all.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_passes_correct_s3_key_to_write_all(self, tmp_path: Path) -> None:
        adapter, mock_s3, _ = make_adapter(tmp_path, pcm_bytes=bytes(32000))
        await adapter.run()
        args, _ = mock_s3.write_all.call_args
        assert args[0] == "recordings/meet_42_test-job-ulid/"

    @pytest.mark.asyncio
    async def test_run_calls_notetaker_client_post_process_exactly_once(
        self, tmp_path: Path
    ) -> None:
        adapter, _, mock_client = make_adapter(tmp_path, pcm_bytes=bytes(32000))
        await adapter.run()
        mock_client.post_process.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_run_passes_job_to_post_process(self, tmp_path: Path) -> None:
        adapter, _, mock_client = make_adapter(tmp_path, pcm_bytes=bytes(32000))
        job = make_test_job()
        await adapter.run()
        called_job = mock_client.post_process.call_args.args[0]
        assert called_job.job_id == job.job_id
        assert called_job.meeting_id == job.meeting_id


# ---------------------------------------------------------------------------
# S3Writer.write_all
# ---------------------------------------------------------------------------


class TestS3WriterWriteAll:
    def test_write_all_calls_write_audio_once(self) -> None:
        mock_s3_client = MagicMock()
        writer = S3Writer(s3_client=mock_s3_client)
        writer.write_audio = MagicMock()
        writer.write_speaker_timeline = MagicMock()
        writer.write_participants = MagicMock()
        writer.write_metadata = MagicMock()

        wav = b"RIFF\x00\x00\x00\x00WAVEfmt "
        timeline = MagicMock(spec=SpeakerTimelineFile)
        participants = MagicMock(spec=ParticipantsFile)
        metadata = MagicMock(spec=MetadataFile)

        writer.write_all("some/key/", wav, timeline, participants, metadata)

        writer.write_audio.assert_called_once_with("some/key/", wav)

    def test_write_all_calls_write_speaker_timeline_once(self) -> None:
        mock_s3_client = MagicMock()
        writer = S3Writer(s3_client=mock_s3_client)
        writer.write_audio = MagicMock()
        writer.write_speaker_timeline = MagicMock()
        writer.write_participants = MagicMock()
        writer.write_metadata = MagicMock()

        wav = b"RIFF\x00\x00\x00\x00WAVEfmt "
        timeline = MagicMock(spec=SpeakerTimelineFile)
        participants = MagicMock(spec=ParticipantsFile)
        metadata = MagicMock(spec=MetadataFile)

        writer.write_all("some/key/", wav, timeline, participants, metadata)

        writer.write_speaker_timeline.assert_called_once_with("some/key/", timeline)

    def test_write_all_calls_write_participants_once(self) -> None:
        mock_s3_client = MagicMock()
        writer = S3Writer(s3_client=mock_s3_client)
        writer.write_audio = MagicMock()
        writer.write_speaker_timeline = MagicMock()
        writer.write_participants = MagicMock()
        writer.write_metadata = MagicMock()

        wav = b"RIFF\x00\x00\x00\x00WAVEfmt "
        timeline = MagicMock(spec=SpeakerTimelineFile)
        participants = MagicMock(spec=ParticipantsFile)
        metadata = MagicMock(spec=MetadataFile)

        writer.write_all("some/key/", wav, timeline, participants, metadata)

        writer.write_participants.assert_called_once_with("some/key/", participants)

    def test_write_all_calls_write_metadata_once(self) -> None:
        mock_s3_client = MagicMock()
        writer = S3Writer(s3_client=mock_s3_client)
        writer.write_audio = MagicMock()
        writer.write_speaker_timeline = MagicMock()
        writer.write_participants = MagicMock()
        writer.write_metadata = MagicMock()

        wav = b"RIFF\x00\x00\x00\x00WAVEfmt "
        timeline = MagicMock(spec=SpeakerTimelineFile)
        participants = MagicMock(spec=ParticipantsFile)
        metadata = MagicMock(spec=MetadataFile)

        writer.write_all("some/key/", wav, timeline, participants, metadata)

        writer.write_metadata.assert_called_once_with("some/key/", metadata)


# ---------------------------------------------------------------------------
# NoteTakerClientWrapper.post_process
# ---------------------------------------------------------------------------


class TestNoteTakerClientWrapper:
    @pytest.mark.asyncio
    async def test_post_process_calls_underlying_client_once(self) -> None:
        mock_inner = MagicMock()
        mock_inner.post_process = AsyncMock(return_value={"status": "queued"})
        wrapper = NoteTakerClientWrapper(client=mock_inner)
        job = make_test_job()
        await wrapper.post_process(job)
        mock_inner.post_process.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_post_process_builds_request_with_correct_meeting_id(self) -> None:
        mock_inner = MagicMock()
        mock_inner.post_process = AsyncMock(return_value={})
        wrapper = NoteTakerClientWrapper(client=mock_inner)
        job = make_test_job()
        await wrapper.post_process(job)
        req: ProcessRequest = mock_inner.post_process.call_args.args[0]
        assert req.meeting_id == job.meeting_id

    @pytest.mark.asyncio
    async def test_post_process_builds_request_with_correct_platform(self) -> None:
        mock_inner = MagicMock()
        mock_inner.post_process = AsyncMock(return_value={})
        wrapper = NoteTakerClientWrapper(client=mock_inner)
        job = make_test_job()
        await wrapper.post_process(job)
        req: ProcessRequest = mock_inner.post_process.call_args.args[0]
        assert req.platform == job.platform

    @pytest.mark.asyncio
    async def test_post_process_builds_request_with_correct_idempotency_key(
        self,
    ) -> None:
        mock_inner = MagicMock()
        mock_inner.post_process = AsyncMock(return_value={})
        wrapper = NoteTakerClientWrapper(client=mock_inner)
        job = make_test_job()
        await wrapper.post_process(job)
        _, kwargs = mock_inner.post_process.call_args
        assert kwargs.get("idempotency_key") == job.job_id
