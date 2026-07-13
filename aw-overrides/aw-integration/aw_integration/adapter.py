from __future__ import annotations

import io
import os
import wave
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

import redis

from notetaker_common.schemas import (
    BotJob,
    HostInfo,
    MetadataFile,
    ParticipantInfo,
    ParticipantsFile,
    SpeakerEvent,
    SpeakerTimelineFile,
    TimelineParticipant,
)

from aw_integration.notetaker_client import NoteTakerClientWrapper
from aw_integration.s3_writer import S3Writer

__all__ = [
    "VexaSegment",
    "VexaSpeakerEvent",
    "VexaSession",
    "VexaSessionAdapter",
    "run_from_redis",
]

_PLATFORM_MAP: dict[str, str] = {"google_meet": "meet"}


@dataclass
class VexaSegment:
    speaker: str
    text: str
    start: float
    end: float
    language: str
    completed: bool
    segment_id: str | None
    absolute_start_time: datetime | None
    absolute_end_time: datetime | None


@dataclass
class VexaSpeakerEvent:
    uid: str
    relative_ms: int
    event_type: str
    participant_name: str
    meeting_id: str


@dataclass
class VexaSession:
    uid: str
    platform: str
    meeting_id: str
    session_start_ts: datetime
    session_end_ts: datetime
    segments: list[VexaSegment] = field(default_factory=list)
    speaker_events: list[VexaSpeakerEvent] = field(default_factory=list)


class VexaSessionAdapter:
    """Converts a VexaSession + BotJob into S3 artifacts and triggers processing (§13.15)."""

    def __init__(
        self,
        session: VexaSession,
        job: BotJob,
        audio_raw_path: Path,
        s3_writer: S3Writer,
        notetaker_client: NoteTakerClientWrapper,
    ) -> None:
        self._session = session
        self._job = job
        self._audio_raw_path = audio_raw_path
        self._s3_writer = s3_writer
        self._notetaker_client = notetaker_client

    @property
    def _platform(self) -> str:
        return _PLATFORM_MAP.get(self._session.platform, self._session.platform)

    def _slug(self, name: str) -> str:
        return name.strip().replace(" ", "_").lower()

    def encode_wav(self) -> bytes:
        if (
            not self._audio_raw_path.exists()
            or self._audio_raw_path.stat().st_size == 0
        ):
            raise FileNotFoundError(
                f"Raw audio path missing or empty: {self._audio_raw_path}"
            )
        raw_pcm = self._audio_raw_path.read_bytes()
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(raw_pcm)
        return buf.getvalue()

    def build_speaker_timeline(self) -> SpeakerTimelineFile:
        session = self._session
        duration_sec = (
            session.session_end_ts - session.session_start_ts
        ).total_seconds()

        # Collect unique speaker names from both sources.
        speaker_names: dict[str, str] = {}
        for seg in session.segments:
            slug = self._slug(seg.speaker)
            speaker_names.setdefault(slug, seg.speaker)
        for ev in session.speaker_events:
            slug = self._slug(ev.participant_name)
            speaker_names.setdefault(slug, ev.participant_name)

        participants = [
            TimelineParticipant(id=slug, name=name)
            for slug, name in speaker_names.items()
        ]

        timeline_events: list[SpeakerEvent] = []
        if session.speaker_events:
            origin_ms = int(session.session_start_ts.timestamp() * 1000)
            for ev in session.speaker_events:
                if ev.event_type != "SPEAKER_START":
                    continue
                timeline_events.append(
                    SpeakerEvent(
                        timestamp_ms=origin_ms + ev.relative_ms,
                        relative_sec=ev.relative_ms / 1000.0,
                        speaker_id=self._slug(ev.participant_name),
                        speaker_name=ev.participant_name,
                    )
                )
        else:
            # Fallback: derive one event per unique speaker per segment start.
            seen: set[str] = set()
            for seg in session.segments:
                slug = self._slug(seg.speaker)
                if slug in seen:
                    continue
                seen.add(slug)
                origin_ms = int(session.session_start_ts.timestamp() * 1000)
                timeline_events.append(
                    SpeakerEvent(
                        timestamp_ms=origin_ms + int(seg.start * 1000),
                        relative_sec=seg.start,
                        speaker_id=slug,
                        speaker_name=seg.speaker,
                    )
                )

        return SpeakerTimelineFile(
            room_name=self._job.join.url,
            meeting_id=session.meeting_id,
            platform=self._platform,
            recording_started_at=session.session_start_ts,
            recording_ended_at=session.session_end_ts,
            duration_sec=duration_sec,
            start_time=session.session_start_ts.timestamp(),
            participants=participants,
            speaker_timeline=timeline_events,
        )

    def build_participants(self) -> ParticipantsFile:
        session = self._session
        organizer_email = self._job.join.organizer_email
        host_name = organizer_email.split("@")[0]

        host = HostInfo(id="host-0", name=host_name, email=organizer_email)

        # Earliest absolute_start_time per speaker slug for joined_at.
        joined_map: dict[str, datetime] = {}
        for seg in session.segments:
            slug = self._slug(seg.speaker)
            if seg.absolute_start_time is not None:
                existing = joined_map.get(slug)
                if existing is None or seg.absolute_start_time < existing:
                    joined_map[slug] = seg.absolute_start_time

        # Collect all unique speaker names.
        speaker_names: dict[str, str] = {}
        for seg in session.segments:
            slug = self._slug(seg.speaker)
            speaker_names.setdefault(slug, seg.speaker)
        for ev in session.speaker_events:
            slug = self._slug(ev.participant_name)
            speaker_names.setdefault(slug, ev.participant_name)

        participants = [
            ParticipantInfo(
                id=slug,
                name=name,
                joined_at=joined_map.get(slug, session.session_start_ts),
                left_at=None,
                is_external=False,
            )
            for slug, name in speaker_names.items()
        ]

        return ParticipantsFile(
            meeting_id=session.meeting_id,
            platform=self._platform,
            host=host,
            participants=participants,
        )

    def build_metadata(
        self,
        bot_left_reason: Literal[
            "host_ended", "last_participant", "hard_deadline", "error"
        ] = "host_ended",
    ) -> MetadataFile:
        session = self._session
        duration_sec = (
            session.session_end_ts - session.session_start_ts
        ).total_seconds()
        return MetadataFile(
            meeting_id=session.meeting_id,
            platform=self._platform,
            scheduled_start_at=self._job.scheduled_start_at,
            actual_start_at=session.session_start_ts,
            actual_end_at=session.session_end_ts,
            duration_sec=duration_sec,
            join_url=self._job.join.url,
            calendar_event_id=None,
            consent_state=self._job.consent.state,
            bot_pod_name=os.environ.get("HOSTNAME", "unknown"),
            bot_image=os.environ.get("BOT_IMAGE", "unknown"),
            bot_started_at=session.session_start_ts,
            bot_left_at=session.session_end_ts,
            bot_left_reason=bot_left_reason,
        )

    async def run(
        self,
        bot_left_reason: Literal[
            "host_ended", "last_participant", "hard_deadline", "error"
        ] = "host_ended",
    ) -> None:
        wav_bytes = self.encode_wav()
        timeline = self.build_speaker_timeline()
        participants = self.build_participants()
        metadata = self.build_metadata(bot_left_reason)
        self._s3_writer.write_all(
            self._job.s3_key, wav_bytes, timeline, participants, metadata
        )
        await self._notetaker_client.post_process(self._job)


async def run_from_redis(
    session_uid: str,
    job: BotJob,
    audio_raw_path: Path,
    session_end_wall_clock: datetime,
    redis_url: str,
    bot_left_reason: Literal[
        "host_ended", "last_participant", "hard_deadline", "error"
    ] = "host_ended",
    s3_writer: S3Writer | None = None,
    notetaker_client: NoteTakerClientWrapper | None = None,
) -> None:
    """Drain Redis streams for session_uid and run the output pipeline. §13.7 Phase 2."""
    import json as _json

    r = redis.Redis.from_url(redis_url, decode_responses=True)

    raw_ts = r.xrange("transcription_segments", count=50000)
    segments: list[VexaSegment] = []
    session_start_ts: datetime | None = None

    for _eid, fields in raw_ts:
        try:
            payload = _json.loads(fields["payload"])
        except (KeyError, ValueError):
            continue
        if payload.get("uid") != session_uid:
            continue

        msg_type = payload.get("type")
        if msg_type == "session_start":
            raw_ts_str = payload.get("start_timestamp")
            if raw_ts_str and session_start_ts is None:
                session_start_ts = datetime.fromisoformat(
                    raw_ts_str.replace("Z", "+00:00")
                )
        elif msg_type == "transcription":
            for seg_dict in payload.get("segments", []):
                try:
                    abs_start_raw = seg_dict.get("absolute_start_time")
                    abs_end_raw = seg_dict.get("absolute_end_time")
                    abs_start = (
                        datetime.fromisoformat(abs_start_raw.replace("Z", "+00:00"))
                        if abs_start_raw
                        else None
                    )
                    abs_end = (
                        datetime.fromisoformat(abs_end_raw.replace("Z", "+00:00"))
                        if abs_end_raw
                        else None
                    )
                    segments.append(
                        VexaSegment(
                            speaker=seg_dict["speaker"],
                            text=seg_dict["text"],
                            start=float(seg_dict["start"]),
                            end=float(seg_dict["end"]),
                            language=seg_dict.get("language", "en"),
                            completed=bool(seg_dict.get("completed", True)),
                            segment_id=seg_dict.get("segment_id"),
                            absolute_start_time=abs_start,
                            absolute_end_time=abs_end,
                        )
                    )
                except (KeyError, ValueError):
                    continue

    raw_se = r.xrange("speaker_events_relative", count=50000)
    speaker_events: list[VexaSpeakerEvent] = []
    for _eid, fields in raw_se:
        if fields.get("uid") != session_uid:
            continue
        try:
            speaker_events.append(
                VexaSpeakerEvent(
                    uid=fields["uid"],
                    relative_ms=int(fields["relative_client_timestamp_ms"]),
                    event_type=fields["event_type"],
                    participant_name=fields["participant_name"],
                    meeting_id=str(fields["meeting_id"]),
                )
            )
        except (KeyError, ValueError):
            continue

    if session_start_ts is None and segments:
        earliest = min(
            (s.absolute_start_time for s in segments if s.absolute_start_time),
            default=None,
        )
        if earliest:
            session_start_ts = earliest

    if session_start_ts is None:
        session_start_ts = session_end_wall_clock - timedelta(
            minutes=job.expected_duration_min
        )

    vexa_session = VexaSession(
        uid=session_uid,
        platform=job.platform,
        meeting_id=job.meeting_id,
        session_start_ts=session_start_ts,
        session_end_ts=session_end_wall_clock,
        segments=segments,
        speaker_events=speaker_events,
    )

    adapter = VexaSessionAdapter(
        session=vexa_session,
        job=job,
        audio_raw_path=audio_raw_path,
        s3_writer=s3_writer or S3Writer(),
        notetaker_client=notetaker_client or NoteTakerClientWrapper(),
    )
    await adapter.run(bot_left_reason=bot_left_reason)
