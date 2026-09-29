"""Per-meeting export job (spec §4.2-4.3, design §1.9); idempotent, keyed on
the export folder.

The meeting's UUID (`data.meeting.uuid`) is its id in every file written and
in the `/process` hand-off; the integer Vexa id is kept in `_export.json` and
used only to read the meeting's recordings and transcript. A webhook without
a UUID raises `MissingMeetingUuid` before anything is read or written.

The outcome is reported through the gateway (`export_result.py`) after it is
recorded in `_export.json`: `handed_off` after `/process`, `failed` when the
meeting has no audio or more recordings than `EXPORT_MAX_RECORDINGS`. A re-run of a handed-off folder only reports again.

A meeting can have several bot sessions (a bot failed and a new one joined,
§6.9 F-K2); each session with audio has its own recording. They make ONE
folder on one clock: t=0 is the first session's recording origin, each later
session sits at its own origin's offset from it, and the gaps are silence.
One session is exported from its recording as it stands.

`export_meeting` raises on retryable failure — the caller (the durable
queue) counts attempts and re-runs; `aw-bots` is never modified, so a re-run
always starts from the same inputs.
"""

from __future__ import annotations

import logging
import tempfile
import wave
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from exporter import __version__
from exporter.activity import ActivityEvent, parse_activity, speech_events
from exporter.activity import names as activity_names
from exporter.attribution import build_participants, build_speaker_timeline
from exporter.audio import join_wavs
from exporter.config import Settings
from exporter.export_result import ExportResultPort
from exporter.naming import folder_name, parse_utc
from exporter.notetaker import Notetaker
from exporter.retention import AUDIO, METADATA, RECORDING_MP4
from exporter.storage import Storage
from exporter.vexa_client import MeetingApi, TooManyRecordings

logger = logging.getLogger("exporter")

# An `ok` file with no speaker events on audio longer than this is suspect:
# someone almost certainly spoke, yet the bot recorded nobody.
EMPTY_ACTIVITY_AUDIO_S = 180.0

ACTIVITY_FILE = "speaker-activity.jsonl"

State = Literal["handed_off", "no_audio", "too_many_recordings", "already_done"]
ActivityState = Literal["ok", "missing", "invalid", "capped"]
# A meeting's `speaker_activity` is its worst session's, in this order.
_ACTIVITY_SEVERITY: tuple[ActivityState, ...] = ("ok", "capped", "invalid", "missing")


class MissingMeetingUuid(Exception):
    """The webhook's meeting has no `uuid`; retrying can't give it one."""


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
    export_result: ExportResultPort
    transcode: Callable[[Path, Path], None]
    now: Callable[[], datetime]
    join_webm: Callable[[list[tuple[Path, float]], Path], None]


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


@dataclass(frozen=True)
class _Session:
    """One bot session's recording and speaker-activity file."""

    recording_id: int
    storage_path: str
    session_uid: str
    origin_ms: int
    signal_prefix: str
    activity_exists: bool

    @property
    def activity_key(self) -> str:
        return self.signal_prefix + ACTIVITY_FILE


def _audio_recordings(
    recordings: list[dict[str, Any]], vexa_meeting_id: int
) -> list[dict[str, Any]]:
    """The recordings with an audio file, oldest first (meeting-api lists
    newest first); a recording without one is skipped with a log line."""
    audio: list[dict[str, Any]] = []
    for rec in recordings:
        if any(media.get("type") == "audio" for media in rec.get("media_files", [])):
            audio.append(rec)
        else:
            logger.info(
                "recording_skipped vexa_meeting_id=%s recording_id=%s reason=no_audio",
                vexa_meeting_id,
                rec.get("id"),
            )
    return sorted(audio, key=lambda rec: (parse_utc(str(rec["created_at"])), rec["id"]))


def _wav_duration_s(path: Path) -> float:
    with wave.open(str(path), "rb") as wav:
        return wav.getnframes() / wav.getframerate()


def _wav_frames(path: Path) -> tuple[int, int]:
    with wave.open(str(path), "rb") as wav:
        return wav.getnframes(), wav.getframerate()


def _single_session_audio(
    session: _Session, base: str, tmp: Path, deps: Deps
) -> tuple[Path, float]:
    """`master.webm` is the session's master; `audio.wav` its transcode."""
    settings = deps.settings
    deps.storage.copy(
        settings.vexa_bucket,
        session.storage_path,
        settings.export_bucket,
        base + "master.webm",
        retention=RECORDING_MP4,
    )
    webm_path = tmp / "master.webm"
    wav_path = tmp / "audio.wav"
    deps.storage.download_file(settings.export_bucket, base + "master.webm", webm_path)
    deps.transcode(webm_path, wav_path)
    return wav_path, _wav_duration_s(wav_path)


def _joined_sessions_audio(
    sessions: list[_Session], base: str, tmp: Path, deps: Deps, vexa_meeting_id: int
) -> tuple[Path, float, list[tuple[int, int]]]:
    """Every session's audio in order on the meeting clock, silence between.

    A session starts at its origin's offset from the first session's origin;
    one whose origin falls inside the previous session's audio follows that
    audio directly. Returns the joined wav, its duration and each session's
    (start_ms, end_ms) on the meeting clock."""
    settings = deps.settings
    decoded: list[tuple[Path, Path, int, int]] = []
    for i, session in enumerate(sessions):
        webm_path = tmp / f"session-{i}.webm"
        wav_path = tmp / f"session-{i}.wav"
        deps.storage.download_file(
            settings.vexa_bucket, session.storage_path, webm_path
        )
        deps.transcode(webm_path, wav_path)
        frames, rate = _wav_frames(wav_path)
        decoded.append((webm_path, wav_path, frames, rate))

    rate = decoded[0][3]
    first_origin_ms = sessions[0].origin_ms
    cursor = 0
    wav_parts: list[tuple[Path, int]] = []
    webm_parts: list[tuple[Path, float]] = []
    spans: list[tuple[int, int]] = []
    for session, (webm_path, wav_path, frames, _) in zip(sessions, decoded):
        offset = round((session.origin_ms - first_origin_ms) * rate / 1000)
        if offset < cursor:
            logger.warning(
                "session_overlap vexa_meeting_id=%s recording_id=%s session_uid=%s "
                "overlap_s=%.3f; placed after the previous session",
                vexa_meeting_id,
                session.recording_id,
                session.session_uid,
                (cursor - offset) / rate,
            )
        start = max(offset, cursor)
        silence = start - cursor
        wav_parts.append((wav_path, silence))
        webm_parts.append((webm_path, silence / rate))
        spans.append(
            (round(start * 1000 / rate), round((start + frames) * 1000 / rate))
        )
        logger.info(
            "session_placed vexa_meeting_id=%s recording_id=%s session_uid=%s "
            "start_s=%.3f duration_s=%.3f",
            vexa_meeting_id,
            session.recording_id,
            session.session_uid,
            start / rate,
            frames / rate,
        )
        cursor = start + frames

    wav_path = tmp / "audio.wav"
    join_wavs(wav_parts, wav_path)
    webm_path = tmp / "master.webm"
    deps.join_webm(webm_parts, webm_path)
    deps.storage.upload_file(
        webm_path,
        settings.export_bucket,
        base + "master.webm",
        "audio/webm",
        retention=RECORDING_MP4,
    )
    return wav_path, cursor / rate, spans


def _session_activity(
    session: _Session, start_ms: int, deps: Deps, vexa_meeting_id: int
) -> tuple[ActivityState, list[ActivityEvent], list[str]]:
    """The session's speaker events, relative to the meeting clock on which
    the session starts at `start_ms`, and its speaker names."""
    settings = deps.settings
    if not session.activity_exists:
        logger.error(
            "speaker_activity_missing vexa_meeting_id=%s session_uid=%s",
            vexa_meeting_id,
            session.session_uid,
        )
        return "missing", [], []
    try:
        activity = parse_activity(
            deps.storage.iter_lines(settings.vexa_bucket, session.activity_key)
        )
        events = speech_events(
            activity,
            session.origin_ms - start_ms,
            settings.rms_speech_threshold,
            settings.speech_hangover_ms,
        )
        speaker_names = activity_names(activity)
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning(
            "speaker activity invalid vexa_meeting_id=%s session_uid=%s "
            "error_class=%s; exporting without attribution",
            vexa_meeting_id,
            session.session_uid,
            type(exc).__name__,
        )
        return "invalid", [], []
    if activity.capped:
        logger.warning(
            "speaker activity capped vexa_meeting_id=%s session_uid=%s; "
            "attribution may stop early",
            vexa_meeting_id,
            session.session_uid,
        )
        return "capped", events, speaker_names
    return "ok", events, speaker_names


def _held_to(
    events: list[ActivityEvent], low_ms: int | None, high_ms: int | None
) -> list[ActivityEvent]:
    """Each event moved into [low_ms, high_ms]; a None bound is open (the
    timeline itself clips to the recording)."""
    held: list[ActivityEvent] = []
    for ev in events:
        at = ev.relative_ms
        if low_ms is not None:
            at = max(at, low_ms)
        if high_ms is not None:
            at = min(at, high_ms)
        held.append(ev if at == ev.relative_ms else replace(ev, relative_ms=at))
    return held


def export_meeting(envelope: dict[str, Any], deps: Deps) -> ExportResult:
    settings = deps.settings
    storage = deps.storage
    started = deps.now()
    m = envelope["data"]["meeting"]
    vexa_meeting_id = m["id"]
    meeting_uuid = str(m.get("uuid") or "").strip()
    if not meeting_uuid:
        raise MissingMeetingUuid(
            f"webhook for vexa_meeting_id={vexa_meeting_id} has no meeting uuid"
        )
    folder = folder_name(m["platform"], m["native_meeting_id"], m["start_time"])
    base = settings.export_prefix + folder + "/"
    s3_path = f"s3://{settings.export_bucket}/{base}"

    existing = storage.get_json(settings.export_bucket, base + "_export.json")
    if existing and existing.get("state") == "handed_off":
        deps.export_result.report(meeting_uuid, "handed_off", s3_path)
        return ExportResult("already_done", folder)

    user_id = m["user_id"]
    platform = m["platform"]

    try:
        recs = deps.meeting_api.list_recordings(
            vexa_meeting_id, settings.max_recordings
        )
    except TooManyRecordings:
        error = (
            f"more than {settings.max_recordings} recordings (EXPORT_MAX_RECORDINGS)"
        )
        logger.error(
            "too_many_recordings vexa_meeting_id=%s max_recordings=%d; "
            "nothing exported",
            vexa_meeting_id,
            settings.max_recordings,
        )
        storage.put_json(
            settings.export_bucket,
            base + "_export.json",
            {
                "state": "too_many_recordings",
                "meeting_id": meeting_uuid,
                "vexa_meeting_id": vexa_meeting_id,
                "max_recordings": settings.max_recordings,
                "error": error,
            },
            retention=METADATA,
        )
        deps.export_result.report(meeting_uuid, "failed", s3_path, error)
        return ExportResult("too_many_recordings", folder)
    audio_recs = _audio_recordings(recs, vexa_meeting_id)
    if not audio_recs:
        storage.put_json(
            settings.export_bucket,
            base + "_export.json",
            {
                "state": "no_audio",
                "meeting_id": meeting_uuid,
                "vexa_meeting_id": vexa_meeting_id,
                "audio_recordings": 0,
            },
            retention=METADATA,
        )
        deps.export_result.report(meeting_uuid, "failed", s3_path, "no audio recording")
        return ExportResult("no_audio", folder)

    sessions: list[_Session] = []
    for rec in audio_recs:
        master = deps.meeting_api.master(rec["id"])
        storage_path = str(master["storage_path"])
        session_uid = storage_path.split("/")[3]
        signal_prefix = f"signal/{user_id}/{vexa_meeting_id}/{session_uid}/"
        sessions.append(
            _Session(
                recording_id=rec["id"],
                storage_path=storage_path,
                session_uid=session_uid,
                origin_ms=recording_origin_ms(rec, settings.record_chunk_timeslice_ms),
                signal_prefix=signal_prefix,
                activity_exists=storage.size(
                    settings.vexa_bucket, signal_prefix + ACTIVITY_FILE
                )
                is not None,
            )
        )

    not_uploaded = [s.session_uid for s in sessions if not s.activity_exists]
    if not_uploaded:
        end_time = m.get("end_time")
        if end_time:
            deadline = parse_utc(str(end_time)) + timedelta(
                seconds=settings.activity_wait_seconds
            )
            if deps.now() < deadline:
                raise ActivityNotReady(
                    f"speaker activity not ready for vexa_meeting_id="
                    f"{vexa_meeting_id} session_uid={','.join(not_uploaded)}; "
                    f"waiting until {deadline.isoformat()}"
                )

    with tempfile.TemporaryDirectory() as tmp_dir:
        if len(sessions) == 1:
            wav_path, wav_duration_s = _single_session_audio(
                sessions[0], base, Path(tmp_dir), deps
            )
            spans = [(0, round(wav_duration_s * 1000))]
        else:
            wav_path, wav_duration_s, spans = _joined_sessions_audio(
                sessions, base, Path(tmp_dir), deps, vexa_meeting_id
            )
        storage.upload_file(
            wav_path,
            settings.export_bucket,
            base + "audio.wav",
            "audio/wav",
            retention=AUDIO,
        )

    recording_started_at = datetime.fromtimestamp(
        sessions[0].origin_ms / 1000, tz=timezone.utc
    )
    recording_ended_at = recording_started_at + timedelta(seconds=wav_duration_s)
    meeting_data = m.get("data") or {}
    room_name = str(
        meeting_data.get("constructed_meeting_url")
        or m.get("constructed_meeting_url")
        or m["native_meeting_id"]
    )
    host_email = meeting_data.get("organizer_email")

    # Each session's events stay inside its own span, so none lands in a gap;
    # the timeline clips the outer ends to the recording.
    events: list[ActivityEvent] = []
    speaker_names: list[str] = []
    states: list[ActivityState] = []
    last = len(sessions) - 1
    for i, (session, (start_ms, end_ms)) in enumerate(zip(sessions, spans)):
        state, session_events, session_names = _session_activity(
            session, start_ms, deps, vexa_meeting_id
        )
        events += _held_to(
            session_events,
            start_ms if i > 0 else None,
            end_ms if i < last else None,
        )
        speaker_names += session_names
        states.append(state)
    activity_state = max(states, key=_ACTIVITY_SEVERITY.index)
    if (
        activity_state == "ok"
        and not events
        and wav_duration_s > EMPTY_ACTIVITY_AUDIO_S
    ):
        logger.warning(
            "speaker_activity_empty vexa_meeting_id=%s audio_s=%d",
            vexa_meeting_id,
            round(wav_duration_s),
        )

    timeline = build_speaker_timeline(
        events,
        platform=platform,
        meeting_id=meeting_uuid,
        room_name=room_name,
        recording_started_at=recording_started_at,
        recording_ended_at=recording_ended_at,
        min_dominant_utterance_ms=settings.min_dominant_utterance_ms,
    )
    participants = build_participants(
        speaker_names,
        platform=platform,
        meeting_id=meeting_uuid,
        joined_at=recording_started_at,
        host_email=host_email,
    )

    storage.put_json(
        settings.export_bucket,
        base + "speaker_timeline.json",
        timeline.model_dump(mode="json"),
        retention=METADATA,
    )
    storage.put_json(
        settings.export_bucket,
        base + "participants.json",
        participants.model_dump(mode="json"),
        retention=METADATA,
    )
    storage.put_json(
        settings.export_bucket, base + "meeting.json", m, retention=METADATA
    )
    storage.put_json(
        settings.export_bucket,
        base + "recordings.json",
        {"recordings": recs},
        retention=METADATA,
    )

    if (m.get("data") or {}).get("transcribe_enabled"):
        transcript = deps.meeting_api.transcript(vexa_meeting_id)
        if transcript is not None:
            storage.put_json(
                settings.export_bucket,
                base + "live_transcript.json",
                transcript,
                retention=METADATA,
            )

    if settings.debug:
        for session in sessions:
            signal_dst = base + "signal/"
            if len(sessions) > 1:
                signal_dst += session.session_uid + "/"
            for key in storage.list_keys(settings.vexa_bucket, session.signal_prefix):
                storage.copy(
                    settings.vexa_bucket,
                    key,
                    settings.export_bucket,
                    signal_dst + key[len(session.signal_prefix) :],
                    retention=AUDIO,
                )

    deps.notetaker.process(meeting_uuid, base, platform)

    finished = deps.now()
    storage.put_json(
        settings.export_bucket,
        base + "_export.json",
        {
            "state": "handed_off",
            "meeting_id": meeting_uuid,
            "vexa_meeting_id": vexa_meeting_id,
            "exported_at": finished.isoformat(),
            "elapsed_s": (finished - started).total_seconds(),
            "exporter_version": __version__,
            "speaker_activity": activity_state,
            "speaker_activity_events": len(events),
            "audio_recordings": len(audio_recs),
        },
        retention=METADATA,
    )
    deps.export_result.report(meeting_uuid, "handed_off", s3_path)
    return ExportResult("handed_off", folder)
