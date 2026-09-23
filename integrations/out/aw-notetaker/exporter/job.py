"""Per-meeting export job (spec §4.2-4.3); idempotent, keyed on the Vexa
meeting id.

`export_meeting` raises on retryable failure — the caller (the durable
queue) counts attempts and re-runs; `aw-bots` is never modified, so a re-run
always starts from the same inputs.
"""

from __future__ import annotations

import logging
import tempfile
import wave
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from exporter import __version__
from exporter.activity import ActivityEvent, parse_activity, speech_events
from exporter.activity import names as activity_names
from exporter.attribution import build_participants, build_speaker_timeline
from exporter.config import Settings
from exporter.naming import folder_name, parse_utc
from exporter.notetaker import Notetaker
from exporter.storage import Storage
from exporter.vexa_client import MeetingApi

logger = logging.getLogger("exporter")

# An `ok` file with no speaker events on audio longer than this is suspect:
# someone almost certainly spoke, yet the bot recorded nobody.
EMPTY_ACTIVITY_AUDIO_S = 180.0

State = Literal["handed_off", "no_audio", "already_done"]
ActivityState = Literal["ok", "missing", "invalid", "capped"]


class ActivityNotReady(Exception):
    """`speaker-activity.jsonl` is absent but the bot may still be uploading
    it (it does so in teardown, after `meeting.completed`, before the debug
    tape); retryable (spec §4.2)."""


@dataclass
class Deps:
    settings: Settings
    storage: Storage
    meeting_api: MeetingApi
    notetaker: Notetaker
    transcode: Callable[[Path, Path], None]
    now: Callable[[], datetime]


@dataclass
class ExportResult:
    state: State
    folder: str


def recording_origin_ms(recording: Mapping[str, Any], timeslice_ms: int) -> int:
    """Clock origin for speaker-activity alignment (spec §4.3, measured, pinned):

    `origin_epoch = recording.created_at - RECORD_CHUNK_TIMESLICE_MS`.
    """
    created_at = str(recording["created_at"])
    created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    return int(created.timestamp() * 1000) - timeslice_ms


def _audio_recordings(recordings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        rec
        for rec in recordings
        if any(media.get("type") == "audio" for media in rec.get("media_files", []))
    ]


def _wav_duration_s(path: Path) -> float:
    with wave.open(str(path), "rb") as wav:
        return wav.getnframes() / wav.getframerate()


def export_meeting(envelope: dict[str, Any], deps: Deps) -> ExportResult:
    settings = deps.settings
    storage = deps.storage
    started = deps.now()
    m = envelope["data"]["meeting"]
    folder = folder_name(m["platform"], m["native_meeting_id"], m["start_time"])
    base = settings.export_prefix + folder + "/"

    existing = storage.get_json(settings.export_bucket, base + "_export.json")
    if existing and existing.get("state") == "handed_off":
        return ExportResult("already_done", folder)

    user_id = m["user_id"]
    vexa_meeting_id = m["id"]
    meeting_id = f"vexa-{vexa_meeting_id}"
    platform = m["platform"]

    recs = deps.meeting_api.list_recordings(user_id, vexa_meeting_id)
    audio_recs = _audio_recordings(recs)
    if not audio_recs:
        storage.put_json(
            settings.export_bucket,
            base + "_export.json",
            {"state": "no_audio", "audio_recordings": 0},
        )
        return ExportResult("no_audio", folder)
    if len(audio_recs) > 1:
        logger.warning(
            "multi-session meeting vexa_meeting_id=%s audio_recordings=%d; "
            "exporting the first only",
            vexa_meeting_id,
            len(audio_recs),
        )
    rec = audio_recs[0]

    master = deps.meeting_api.master(user_id, rec["id"])
    storage_path = str(master["storage_path"])
    session_uid = storage_path.split("/")[3]
    signal_prefix = f"signal/{user_id}/{vexa_meeting_id}/{session_uid}/"
    activity_key = signal_prefix + "speaker-activity.jsonl"

    activity_exists = storage.size(settings.vexa_bucket, activity_key) is not None
    if not activity_exists:
        end_time = m.get("end_time")
        if end_time:
            deadline = parse_utc(str(end_time)) + timedelta(
                seconds=settings.activity_wait_seconds
            )
            if deps.now() < deadline:
                raise ActivityNotReady(
                    f"speaker activity not ready for vexa_meeting_id="
                    f"{vexa_meeting_id}; waiting until {deadline.isoformat()}"
                )

    storage.copy(
        settings.vexa_bucket, storage_path, settings.export_bucket, base + "master.webm"
    )
    with tempfile.TemporaryDirectory() as tmp_dir:
        webm_path = Path(tmp_dir) / "master.webm"
        wav_path = Path(tmp_dir) / "audio.wav"
        storage.download_file(settings.export_bucket, base + "master.webm", webm_path)
        deps.transcode(webm_path, wav_path)
        wav_duration_s = _wav_duration_s(wav_path)
        storage.upload_file(
            wav_path, settings.export_bucket, base + "audio.wav", "audio/wav"
        )

    origin_ms = recording_origin_ms(rec, settings.record_chunk_timeslice_ms)
    recording_started_at = datetime.fromtimestamp(origin_ms / 1000, tz=timezone.utc)
    recording_ended_at = recording_started_at + timedelta(seconds=wav_duration_s)
    meeting_data = m.get("data") or {}
    room_name = str(
        meeting_data.get("constructed_meeting_url")
        or m.get("constructed_meeting_url")
        or m["native_meeting_id"]
    )
    host_email = meeting_data.get("organizer_email")

    events: list[ActivityEvent] = []
    speaker_names: list[str] = []
    activity_state: ActivityState
    if not activity_exists:
        activity_state = "missing"
        logger.error("speaker_activity_missing vexa_meeting_id=%s", vexa_meeting_id)
    else:
        try:
            activity = parse_activity(
                storage.iter_lines(settings.vexa_bucket, activity_key)
            )
            events = speech_events(
                activity,
                origin_ms,
                settings.rms_speech_threshold,
                settings.speech_hangover_ms,
            )
            speaker_names = activity_names(activity)
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning(
                "speaker activity invalid vexa_meeting_id=%s error_class=%s; "
                "exporting without attribution",
                vexa_meeting_id,
                type(exc).__name__,
            )
            events, speaker_names = [], []
            activity_state = "invalid"
        else:
            activity_state = "capped" if activity.capped else "ok"
            if activity.capped:
                logger.warning(
                    "speaker activity capped vexa_meeting_id=%s; "
                    "attribution may stop early",
                    vexa_meeting_id,
                )
            elif not events and wav_duration_s > EMPTY_ACTIVITY_AUDIO_S:
                logger.warning(
                    "speaker_activity_empty vexa_meeting_id=%s audio_s=%d",
                    vexa_meeting_id,
                    round(wav_duration_s),
                )

    timeline = build_speaker_timeline(
        events,
        platform=platform,
        meeting_id=meeting_id,
        room_name=room_name,
        recording_started_at=recording_started_at,
        recording_ended_at=recording_ended_at,
        min_dominant_utterance_ms=settings.min_dominant_utterance_ms,
    )
    participants = build_participants(
        speaker_names,
        platform=platform,
        meeting_id=meeting_id,
        joined_at=recording_started_at,
        host_email=host_email,
    )

    storage.put_json(
        settings.export_bucket,
        base + "speaker_timeline.json",
        timeline.model_dump(mode="json"),
    )
    storage.put_json(
        settings.export_bucket,
        base + "participants.json",
        participants.model_dump(mode="json"),
    )
    storage.put_json(settings.export_bucket, base + "meeting.json", m)
    storage.put_json(
        settings.export_bucket, base + "recordings.json", {"recordings": recs}
    )

    if (m.get("data") or {}).get("transcribe_enabled"):
        transcript = deps.meeting_api.transcript(user_id, vexa_meeting_id)
        if transcript is not None:
            storage.put_json(
                settings.export_bucket, base + "live_transcript.json", transcript
            )

    if settings.debug:
        for key in storage.list_keys(settings.vexa_bucket, signal_prefix):
            basename = key[len(signal_prefix) :]
            storage.copy(
                settings.vexa_bucket,
                key,
                settings.export_bucket,
                base + "signal/" + basename,
            )

    deps.notetaker.process(meeting_id, base, platform)

    finished = deps.now()
    storage.put_json(
        settings.export_bucket,
        base + "_export.json",
        {
            "state": "handed_off",
            "vexa_meeting_id": vexa_meeting_id,
            "exported_at": finished.isoformat(),
            "elapsed_s": (finished - started).total_seconds(),
            "exporter_version": __version__,
            "speaker_activity": activity_state,
            "speaker_activity_events": len(events),
            "audio_recordings": len(audio_recs),
        },
    )
    return ExportResult("handed_off", folder)
