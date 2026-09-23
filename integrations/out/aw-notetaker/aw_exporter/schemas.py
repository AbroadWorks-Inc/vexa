"""Output schemas mirroring notetaker_common contracts."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class SpeakerEvent(BaseModel):
    """A point in time where the dominant speaker changed."""

    model_config = ConfigDict(populate_by_name=True)

    timestamp_ms: int
    relative_sec: float
    speaker_id: str
    speaker_name: str


class TimelineParticipant(BaseModel):
    """A participant in the meeting timeline."""

    model_config = ConfigDict(populate_by_name=True)

    id: str
    name: str
    joined_at: datetime | None = None


class SpeakerInterval(BaseModel):
    """One participant audibly speaking over a closed time span."""

    model_config = ConfigDict(populate_by_name=True)

    speaker_id: str
    speaker_name: str
    start_sec: float
    end_sec: float


class SpeakerTimelineFile(BaseModel):
    """Timeline of speakers and events from a meeting recording."""

    model_config = ConfigDict(populate_by_name=True)

    room_name: str
    meeting_id: str
    platform: str
    recording_started_at: datetime
    recording_ended_at: datetime
    duration_sec: float
    start_time: float
    participants: list[TimelineParticipant]
    speaker_timeline: list[SpeakerEvent]
    speaker_intervals: list[SpeakerInterval] = []


class HostInfo(BaseModel):
    """Information about the meeting host."""

    model_config = ConfigDict(populate_by_name=True)

    id: str
    name: str
    email: str | None = None


class ParticipantInfo(BaseModel):
    """Information about a meeting participant."""

    model_config = ConfigDict(populate_by_name=True)

    id: str
    name: str
    email: str | None = None
    joined_at: datetime
    left_at: datetime | None = None
    is_external: bool


class ParticipantsFile(BaseModel):
    """Participant roster from a meeting."""

    model_config = ConfigDict(populate_by_name=True)

    meeting_id: str
    platform: str
    host: HostInfo
    participants: list[ParticipantInfo]
