from __future__ import annotations

from notetaker_common.s3 import S3Client
from notetaker_common.schemas import MetadataFile, ParticipantsFile, SpeakerTimelineFile

__all__ = ["S3Writer"]


class S3Writer:
    """Writes all aw-integration artifacts to S3 (§13.15)."""

    def __init__(self, s3_client: S3Client | None = None) -> None:
        self._s3 = s3_client or S3Client()

    def write_audio(self, s3_key: str, wav_bytes: bytes) -> None:
        self._s3.put_object(
            key=f"{s3_key}audio.wav",
            body=wav_bytes,
            content_type="audio/wav",
        )

    def write_speaker_timeline(
        self, s3_key: str, timeline: SpeakerTimelineFile
    ) -> None:
        self._s3.put_json(
            key=f"{s3_key}speaker_timeline.json",
            data=timeline.model_dump(mode="json"),
        )

    def write_participants(self, s3_key: str, participants: ParticipantsFile) -> None:
        self._s3.put_json(
            key=f"{s3_key}participants.json",
            data=participants.model_dump(mode="json"),
        )

    def write_metadata(self, s3_key: str, metadata: MetadataFile) -> None:
        self._s3.put_json(
            key=f"{s3_key}metadata.json",
            data=metadata.model_dump(mode="json"),
        )

    def write_all(
        self,
        s3_key: str,
        wav_bytes: bytes,
        timeline: SpeakerTimelineFile,
        participants: ParticipantsFile,
        metadata: MetadataFile,
    ) -> None:
        self.write_audio(s3_key, wav_bytes)
        self.write_speaker_timeline(s3_key, timeline)
        self.write_participants(s3_key, participants)
        self.write_metadata(s3_key, metadata)
