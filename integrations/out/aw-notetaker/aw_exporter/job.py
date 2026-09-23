"""Per-meeting export job (spec §4.2-4.3); idempotent, keyed on the Vexa
meeting id.

`export_meeting` raises on retryable failure — the caller (the durable
queue) counts attempts and re-runs; `aw-bots` is never modified, so a re-run
always starts from the same inputs.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from aw_exporter import __version__
from aw_exporter.attribution import build_participants, build_speaker_timeline
from aw_exporter.config import Settings
from aw_exporter.naming import folder_name
from aw_exporter.notetaker import Notetaker
from aw_exporter.storage import Storage
from aw_exporter.tape import names as tape_names
from aw_exporter.tape import parse_tape, speech_events
from aw_exporter.vexa_client import MeetingApi

State = Literal["handed_off", "no_audio", "already_done"]


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
    """Clock origin for tape alignment (spec §4.3, measured, pinned):

    `origin_epoch = recording.created_at - RECORD_CHUNK_TIMESLICE_MS`.
    """
    created_at = str(recording["created_at"])
    created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    return int(created.timestamp() * 1000) - timeslice_ms


def _first_audio_recording(recordings: list[dict[str, Any]]) -> dict[str, Any] | None:
    for rec in recordings:
        media_files = rec.get("media_files", [])
        if any(media.get("type") == "audio" for media in media_files):
            return rec
    return None


def export_meeting(envelope: dict[str, Any], deps: Deps) -> ExportResult:
    settings = deps.settings
    storage = deps.storage
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
    rec = _first_audio_recording(recs)
    if rec is None:
        storage.put_json(
            settings.export_bucket, base + "_export.json", {"state": "no_audio"}
        )
        return ExportResult("no_audio", folder)

    master = deps.meeting_api.master(user_id, rec["id"])
    storage_path = str(master["storage_path"])
    storage.copy(
        settings.vexa_bucket, storage_path, settings.export_bucket, base + "master.webm"
    )

    with tempfile.TemporaryDirectory() as tmp_dir:
        webm_path = Path(tmp_dir) / "master.webm"
        wav_path = Path(tmp_dir) / "audio.wav"
        webm_path.write_bytes(
            storage.get_bytes(settings.export_bucket, base + "master.webm")
        )
        deps.transcode(webm_path, wav_path)
        storage.put_bytes(
            settings.export_bucket,
            base + "audio.wav",
            wav_path.read_bytes(),
            "audio/wav",
        )

    session_uid = storage_path.split("/")[3]
    signal_prefix = f"signal/{user_id}/{vexa_meeting_id}/{session_uid}/"
    tape_key = signal_prefix + "captured-signal.jsonl"

    origin_ms = recording_origin_ms(rec, settings.record_chunk_timeslice_ms)
    recording_started_at = datetime.fromtimestamp(origin_ms / 1000, tz=timezone.utc)
    recording_ended_at = datetime.fromisoformat(
        str(m["end_time"]).replace("Z", "+00:00")
    )
    room_name = str(m.get("constructed_meeting_url") or m["native_meeting_id"])
    host_email = (m.get("data") or {}).get("organizer_email")

    if storage.exists(settings.vexa_bucket, tape_key):
        tape = parse_tape(storage.iter_lines(settings.vexa_bucket, tape_key))
        events = speech_events(
            tape,
            origin_ms,
            settings.rms_speech_threshold,
            settings.speech_hangover_ms,
        )
        speaker_names = tape_names(tape)
        tape_state: Literal["ok", "missing"] = "ok"
    else:
        events = []
        speaker_names = []
        tape_state = "missing"

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

    storage.put_json(
        settings.export_bucket,
        base + "_export.json",
        {
            "state": "handed_off",
            "vexa_meeting_id": vexa_meeting_id,
            "exported_at": deps.now().isoformat(),
            "exporter_version": __version__,
            "tape": tape_state,
        },
    )
    return ExportResult("handed_off", folder)
