"""Schema validation and serialization tests."""

from datetime import datetime

from exporter import schemas


def test_field_sets_match_notetaker_contract() -> None:
    """Verify schema field order matches notetaker_common contract."""
    assert list(schemas.SpeakerInterval.model_fields) == [
        "speaker_id",
        "speaker_name",
        "start_sec",
        "end_sec",
    ]
    assert list(schemas.SpeakerTimelineFile.model_fields) == [
        "room_name",
        "meeting_id",
        "platform",
        "recording_started_at",
        "recording_ended_at",
        "duration_sec",
        "start_time",
        "participants",
        "speaker_timeline",
        "speaker_intervals",
    ]
    assert list(schemas.ParticipantsFile.model_fields) == [
        "meeting_id",
        "platform",
        "host",
        "participants",
    ]


def test_speaker_timeline_file_roundtrip_and_defaults() -> None:
    """Round-trip SpeakerTimelineFile with SpeakerInterval through JSON serialization."""
    now = datetime(2026, 9, 23, 10, 30, 0)
    interval = schemas.SpeakerInterval(
        speaker_id="speaker1",
        speaker_name="Alice",
        start_sec=10.5,
        end_sec=15.3,
    )
    original = schemas.SpeakerTimelineFile(
        room_name="test-room",
        meeting_id="meeting123",
        platform="meet",
        recording_started_at=now,
        recording_ended_at=datetime(2026, 9, 23, 10, 45, 0),
        duration_sec=900.0,
        start_time=0.0,
        participants=[
            schemas.TimelineParticipant(id="p1", name="Alice", joined_at=now),
        ],
        speaker_timeline=[
            schemas.SpeakerEvent(
                timestamp_ms=10500,
                relative_sec=10.5,
                speaker_id="speaker1",
                speaker_name="Alice",
            ),
        ],
        speaker_intervals=[interval],
    )

    # Round-trip through JSON
    dumped = original.model_dump(mode="json")
    reconstructed = schemas.SpeakerTimelineFile.model_validate(dumped)

    # Assert equality
    assert reconstructed == original

    # Assert speaker_intervals defaults to []
    default_timeline = schemas.SpeakerTimelineFile(
        room_name="default-room",
        meeting_id="meeting456",
        platform="zoom",
        recording_started_at=now,
        recording_ended_at=datetime(2026, 9, 23, 11, 0, 0),
        duration_sec=1800.0,
        start_time=0.0,
        participants=[],
        speaker_timeline=[],
    )
    assert default_timeline.speaker_intervals == []
