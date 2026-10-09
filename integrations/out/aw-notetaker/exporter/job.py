"""Per-meeting export job (spec §4.2-4.3, design §1.9); idempotent, keyed on
the export folder.

The envelope is an aw-bots subscription delivery (webhook.v1 `MeetingEvent`);
its `data.meeting` is the §2.4 meeting. The meeting's UUID (`id`) is its id in
every file written and in the `/process` hand-off; the integer Vexa id
(`upstream_id`) is kept in `_export.json` and used only to read the meeting's
recordings and transcript and to find its speaker-activity files, under the
owner the recording's storage path names. A meeting without both raises
`NotAV2Meeting` before anything is read or written.

The meeting is `meeting.completed`, or `bot.failed` after its bot recorded
part of the call (§6.9 F-K2); a `bot.failed` meeting with no recording is
skipped (logged; nothing written, nothing reported).

The outcome is reported through the gateway (`export_result.py`) after it is
recorded in `_export.json`: `handed_off` after `/process`, `failed` when a
completed meeting has no audio, a meeting has more recordings than
`EXPORT_MAX_RECORDINGS`, or a speaker with more than 10 s of timeline
coverage is missing from the wav (`audio_mismatch`). A re-run of a
handed-off folder only reports again. `audio_mismatch` is not `handed_off`,
so a later re-enqueue runs the check again.

A rerun (`rerun=True`, queued by `exporter.rerun`) exports even a handed-off
folder: every file is written again from what aw-bots holds now, so a file
an earlier export could not write (a channel whose master timed out) is
filled in, and the folder goes to notetaker-worker's `/process` with
`"rerun": true`, which redoes the transcript where a plain `/process` would be
an idempotent no-op.

A meeting can have several bot sessions (a bot failed and a new one joined,
§6.9 F-K2); each session with audio has its own recording. They make ONE
folder on one clock: t=0 is the first session's recording origin, each later
session sits at its own origin's offset from it, and the gaps are silence.
One session is exported from its recording as it stands.

A recording may also carry per-channel media (`ch0`, `ch1`, ...). Each
channel is written as its recorder made it, opus `channels/ch<N>.webm`, with
one `channels/index.json` row naming the `file` and its `offset_s` on the
meeting clock; nothing is decoded or padded (a 2-hour, 19-speaker meeting's
channels are ~270 MB as opus and would be ~3.5 GB as 16 kHz wav). One bot
session's channel is an S3 copy of its master; a channel several sessions
recorded is joined into one opus file, one channel at a time. Every frame
that parsed, named or not, is written to `speaker_activity_frames.json`,
except that a session with channel-tap frames (`"src":"channel"`) writes only
those. The mixed timeline does not read channel-tap frames. A meeting with no
channel media gets no index. The index goes last, so a channel that fails
(fetch, copy, join) writes no index; it is logged and the mixed export and
`/process` go on as without channels. The exporter deletes nothing here
(§1.9): a channel file no index names expires with its retention class.

`export_meeting` raises on retryable failure — the caller (the durable
queue) counts attempts and re-runs; `aw-bots` is never modified, so a re-run
always starts from the same inputs.
"""

from __future__ import annotations

import audioop
import logging
import math
import tempfile
import wave
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from exporter import __version__
from exporter.activity import (
    ActivityEvent,
    CapturedFrame,
    parse_activity,
    speech_events,
)
from exporter.activity import names as activity_names
from exporter.attribution import (
    _intervals_from_points,
    build_participants,
    build_speaker_timeline,
)
from exporter.schemas import SpeakerTimelineFile
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
FRAMES_FILE = "speaker_activity_frames.json"

State = Literal[
    "handed_off",
    "no_audio",
    "too_many_recordings",
    "skipped",
    "already_done",
    "audio_mismatch",
]
# A speaker must have strictly more than this much timeline coverage before
# their wav RMS can fail the export. Shorter turns stay a hand-off.
_MISMATCH_MIN_COVERAGE_S = 10.0
ActivityState = Literal["ok", "missing", "invalid", "capped"]
# A meeting's `speaker_activity` is its worst session's, in this order.
_ACTIVITY_SEVERITY: tuple[ActivityState, ...] = ("ok", "capped", "invalid", "missing")


# The data.meeting fields export_meeting/naming.folder_name need (intake.v1
# Meeting); a meeting missing any of these is refused before it is queued
# (webhook intake, rerun) rather than failing (or being quarantined) later.
_REQUIRED_MEETING_FIELDS = ("id", "platform", "room", "started_at")


def not_sent(meeting: Mapping[str, Any]) -> bool:
    """No bot was ever sent to this meeting: there is nothing to export."""
    outcome = meeting.get("outcome")
    return isinstance(outcome, dict) and outcome.get("kind") == "not_sent"


def meeting_is_valid(meeting: Mapping[str, Any]) -> bool:
    """The meeting carries every field the export needs."""
    for field in _REQUIRED_MEETING_FIELDS:
        value = meeting.get(field)
        if not isinstance(value, str) or not value.strip():
            return False
    upstream_id = meeting.get("upstream_id")
    return (
        isinstance(upstream_id, int)
        and not isinstance(upstream_id, bool)
        and upstream_id > 0
    )


class NotAV2Meeting(Exception):
    """The queued meeting is not the §2.4 meeting (a UUID `id` and an integer
    `upstream_id`); retrying can't change it."""


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
    media_files: tuple[Mapping[str, Any], ...] = ()

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


def _channel_number(media_type: object) -> int | None:
    """`ch` plus digits (`ch0`, `ch12`). `chunk`, `ch` and `audio` are not."""
    if not isinstance(media_type, str) or not media_type.startswith("ch"):
        return None
    rest = media_type[2:]
    if not rest.isdigit():
        return None
    return int(rest)


class ChannelIdentityMissing(Exception):
    """A channel reached the exporter without the identity its recorder stamps."""


def _channel_identity(channel: int, metadata: object) -> Mapping[str, Any]:
    """The identity a channel recorder stamps on its first chunk, which
    meeting-api keeps in the media file's `metadata` (full recording only):
    the kind (`gmeet`/`jitsi`) and the recorder's start on the epoch clock,
    plus the stream and the speaker's ids and name when known.

    Without kind and start a channel can be neither named nor placed on the
    meeting clock, so it raises instead of guessing; the caller then exports
    the meeting without channels.
    """
    meta = metadata if isinstance(metadata, Mapping) else {}
    if meta.get("channel_kind") not in ("gmeet", "jitsi") or (
        type(meta.get("recorder_start_epoch_ms")) is not int
    ):
        raise ChannelIdentityMissing(
            f"ch{channel} has no channel_kind/recorder_start_epoch_ms in its "
            f"recording metadata (keys: {sorted(meta)})"
        )
    return meta


def _channel_offset_ms(
    identity: Mapping[str, Any], span_start_ms: int, origin_ms: int
) -> int:
    """Where this channel's recorder sits on the meeting clock: the session's
    mixed audio was placed at `span_start_ms`, and the recorder started
    `recorder_start_epoch_ms - origin_ms` after the session origin (a Jitsi
    channel starts when its speaker joins)."""
    return span_start_ms + (int(identity["recorder_start_epoch_ms"]) - origin_ms)


def _channel_index_row(
    channel: int, identity: Mapping[str, Any], offset_ms: int
) -> dict[str, Any]:
    stream_id = identity.get("stream_id")
    row: dict[str, Any] = {
        "channel": channel,
        "kind": identity["channel_kind"],
        "stream_id": stream_id if isinstance(stream_id, str) else "",
        "offset_s": round(offset_ms / 1000, 3),
    }
    for key in ("participant_id", "display_name"):
        value = identity.get(key)
        if isinstance(value, str) and value:
            row[key] = value
    return row


@dataclass(frozen=True)
class _ChannelPiece:
    channel: int
    offset_ms: int
    storage_path: str
    row: dict[str, Any]
    session_uid: str
    recording_id: int


def _channel_file(
    group: list[_ChannelPiece],
    base: str,
    tmp: Path,
    deps: Deps,
    vexa_meeting_id: int,
) -> str:
    """Write one channel number's audio to `channels/`; return its file name.

    Pieces are in offset order and the file starts at the first piece's
    offset. One piece is its recorder's opus master, copied in S3 as it
    stands. Several are joined into one opus file with the silence between
    them; a piece that starts on top of the previous one follows it directly.
    Each piece's length is read from its decode, which is deleted at once, so
    the disk holds one channel's pieces at a time.
    """
    settings = deps.settings
    channel = group[0].channel
    name = f"ch{channel}.webm"
    key = f"{base}channels/{name}"
    if len(group) == 1:
        deps.storage.copy(
            settings.vexa_bucket,
            group[0].storage_path,
            settings.export_bucket,
            key,
            retention=AUDIO,
        )
        return name
    parts: list[tuple[Path, float]] = []
    cursor_ms = group[0].offset_ms
    for i, piece in enumerate(group):
        webm_path = tmp / f"ch{channel}-{i}.webm"
        wav_path = tmp / f"ch{channel}-{i}.wav"
        deps.storage.download_file(settings.vexa_bucket, piece.storage_path, webm_path)
        deps.transcode(webm_path, wav_path)
        frames, rate = _wav_frames(wav_path)
        wav_path.unlink()
        if piece.offset_ms < cursor_ms:
            logger.warning(
                "channel_overlap vexa_meeting_id=%s channel=%s recording_id=%s "
                "overlap_s=%.3f; placed after the previous piece",
                vexa_meeting_id,
                channel,
                piece.recording_id,
                (cursor_ms - piece.offset_ms) / 1000,
            )
        start_ms = max(piece.offset_ms, cursor_ms)
        parts.append((webm_path, (start_ms - cursor_ms) / 1000))
        cursor_ms = start_ms + round(frames * 1000 / rate)
    joined = tmp / name
    deps.join_webm(parts, joined)
    deps.storage.upload_file(
        joined, settings.export_bucket, key, "audio/webm", retention=AUDIO
    )
    for path, _silence in parts:
        path.unlink()
    joined.unlink()
    return name


def _export_channels(
    sessions: list[_Session],
    spans: list[tuple[int, int]],
    base: str,
    tmp: Path,
    deps: Deps,
    vexa_meeting_id: int,
) -> None:
    """Write each channel number's opus file and its `channels/index.json` row.

    One file per channel number across sessions. The earliest piece's identity
    and offset are the index row; a later session that names someone else is
    logged and does not rename the row. Every channel's identity is checked
    and every master asked for before any file is written, and the index is
    written last, so it never names a file this export did not write.
    """
    settings = deps.settings
    numbered: list[tuple[_Session, int, int, Mapping[str, Any]]] = []
    for session, (start_ms, _end_ms) in zip(sessions, spans):
        for media in session.media_files:
            number = _channel_number(media.get("type"))
            if number is not None:
                identity = _channel_identity(number, media.get("metadata"))
                numbered.append((session, start_ms, number, identity))
    by_channel: dict[int, list[_ChannelPiece]] = {}
    for session, start_ms, number, identity in numbered:
        master = deps.meeting_api.master(session.recording_id, media_type=f"ch{number}")
        offset_ms = _channel_offset_ms(identity, start_ms, session.origin_ms)
        by_channel.setdefault(number, []).append(
            _ChannelPiece(
                channel=number,
                offset_ms=offset_ms,
                storage_path=str(master["storage_path"]),
                row=_channel_index_row(number, identity, offset_ms),
                session_uid=session.session_uid,
                recording_id=session.recording_id,
            )
        )
    index: list[dict[str, Any]] = []
    for channel in sorted(by_channel):
        group = sorted(
            by_channel[channel],
            key=lambda piece: (piece.offset_ms, piece.recording_id),
        )
        name = _channel_file(group, base, tmp, deps, vexa_meeting_id)
        kept = {**group[0].row, "file": name}
        index.append(kept)
        for later in group[1:]:
            if later.row.get("display_name") != kept.get("display_name"):
                logger.warning(
                    "channel_display_name_differs vexa_meeting_id=%s channel=%s "
                    "session_uid=%s display_name=%s kept=%s",
                    vexa_meeting_id,
                    channel,
                    later.session_uid,
                    later.row.get("display_name"),
                    kept.get("display_name"),
                )
    if not index:
        return
    deps.storage.put_json(
        settings.export_bucket,
        base + "channels/index.json",
        index,
        retention=METADATA,
    )


def _activity_frame(
    frame: CapturedFrame, origin_ms: int, start_ms: int
) -> dict[str, Any]:
    """`t_rel` is the frame's end on the meeting clock. The bot stamps `t` at
    delivery, which is the end of the block, so the duration is not added again."""
    return {
        "t_rel": (frame.ts - origin_ms + start_ms) / 1000,
        "ch": frame.channel,
        "name": frame.name,
        "rms": frame.rms,
        "dur_ms": frame.duration_ms,
    }


def _session_activity(
    session: _Session, start_ms: int, deps: Deps, vexa_meeting_id: int
) -> tuple[ActivityState, list[ActivityEvent], list[str], list[CapturedFrame]]:
    """The session's speaker events, relative to the meeting clock on which
    the session starts at `start_ms`, its speaker names, and every parsed frame.

    Frames are returned only for a file that parsed (`ok` or `capped`). A
    missing or invalid file returns none, so the export does not write an
    empty frame list that would look like silence.
    """
    settings = deps.settings
    if not session.activity_exists:
        logger.error(
            "speaker_activity_missing vexa_meeting_id=%s session_uid=%s",
            vexa_meeting_id,
            session.session_uid,
        )
        return "missing", [], [], []
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
        return "invalid", [], [], []
    if activity.capped:
        logger.warning(
            "speaker activity capped vexa_meeting_id=%s session_uid=%s; "
            "attribution may stop early",
            vexa_meeting_id,
            session.session_uid,
        )
        return "capped", events, speaker_names, list(activity.captured)
    return "ok", events, speaker_names, list(activity.captured)


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


def _speaker_levels(
    wav_path: Path, timeline: SpeakerTimelineFile, hangover_ms: int
) -> dict[str, dict[str, float]]:
    """RMS of the mono s16 wav inside each speaker's coverage.

    Intervals are the windows. When the timeline has none, point runs use the
    same closure as the interval fallback (last point plus hangover, clipped
    to the next speaker). A speaker is included only when that coverage is
    strictly longer than 10 s. The wav is read span by span; the whole file
    is not held as a Python list.
    """
    if timeline.speaker_intervals:
        spans = [
            (
                iv.speaker_name,
                int(round(iv.start_sec * 1000)),
                int(round(iv.end_sec * 1000)),
            )
            for iv in timeline.speaker_intervals
        ]
    else:
        spans = _intervals_from_points(
            timeline.speaker_timeline, timeline.duration_sec, hangover_ms
        )
    by_name: dict[str, list[tuple[int, int]]] = {}
    for name, start_ms, end_ms in spans:
        by_name.setdefault(name, []).append((start_ms, end_ms))

    levels: dict[str, dict[str, float]] = {}
    with wave.open(str(wav_path), "rb") as wav:
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
            logger.warning(
                "audio_mismatch_skipped reason=wav_not_mono_s16 channels=%s width=%s",
                wav.getnchannels(),
                wav.getsampwidth(),
            )
            return {}
        rate = wav.getframerate()
        nframes = wav.getnframes()
        for name, windows in by_name.items():
            sum_sq = 0.0
            count = 0
            for start_ms, end_ms in windows:
                start = max(0, int(start_ms * rate / 1000))
                end = min(nframes, int(end_ms * rate / 1000))
                if end <= start:
                    continue
                wav.setpos(start)
                raw = wav.readframes(end - start)
                samples = len(raw) // 2
                if samples == 0:
                    continue
                rms = audioop.rms(raw, 2)
                sum_sq += float(rms) * float(rms) * samples
                count += samples
            coverage_s = count / rate if rate else 0.0
            if coverage_s <= _MISMATCH_MIN_COVERAGE_S or count == 0:
                continue
            levels[name] = {
                "coverage_s": round(coverage_s, 3),
                "rms": round(math.sqrt(sum_sq / count) / 32768.0, 6),
            }
    return levels


def export_meeting(
    envelope: dict[str, Any], deps: Deps, rerun: bool = False
) -> ExportResult:
    settings = deps.settings
    storage = deps.storage
    started = deps.now()
    m = envelope["data"]["meeting"]
    vexa_meeting_id = m.get("upstream_id")
    meeting_uuid = m.get("id")
    if (
        not isinstance(meeting_uuid, str)
        or not meeting_uuid.strip()
        or not isinstance(vexa_meeting_id, int)
        or isinstance(vexa_meeting_id, bool)
    ):
        raise NotAV2Meeting(
            f"meeting id={meeting_uuid!r} upstream_id={vexa_meeting_id!r} is not "
            "a v2 meeting (a UUID id and an integer upstream_id)"
        )
    folder = folder_name(m["platform"], m["room"], m["started_at"])
    base = settings.export_prefix + folder + "/"
    s3_path = f"s3://{settings.export_bucket}/{base}"

    existing = storage.get_json(settings.export_bucket, base + "_export.json")
    if existing and existing.get("state") == "handed_off" and not rerun:
        deps.export_result.report(meeting_uuid, "handed_off", s3_path)
        return ExportResult("already_done", folder)

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
    if not audio_recs and envelope.get("event_type") == "bot.failed":
        # The meeting failed before its bot recorded anything: there is no
        # export to report on, and the meeting's own `bot.failed` says why.
        logger.info(
            "bot_failed_skipped vexa_meeting_id=%s reason=no_recording; "
            "nothing exported",
            vexa_meeting_id,
        )
        return ExportResult("skipped", folder)
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
        # recordings/<owner>/<recording>/<session>/…; the bot's signal files
        # are keyed by the same owner and session.
        parts = storage_path.split("/")
        owner, session_uid = parts[1], parts[3]
        signal_prefix = f"signal/{owner}/{vexa_meeting_id}/{session_uid}/"
        # The list keeps only each media file's id, type, format and size;
        # a channel's identity and recorder clock are in the full recording.
        full = deps.meeting_api.recording(rec["id"])
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
                media_files=tuple(full.get("media_files") or ()),
            )
        )

    not_uploaded = [s.session_uid for s in sessions if not s.activity_exists]
    if not_uploaded:
        ended_at = m.get("ended_at")
        if ended_at:
            deadline = parse_utc(str(ended_at)) + timedelta(
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
        room_name = str(m.get("meeting_url") or m["room"])

        # Each session's events stay inside its own span, so none lands in a gap;
        # the timeline clips the outer ends to the recording.
        events: list[ActivityEvent] = []
        speaker_names: list[str] = []
        states: list[ActivityState] = []
        exported_frames: list[dict[str, Any]] = []
        last = len(sessions) - 1
        for i, (session, (start_ms, end_ms)) in enumerate(zip(sessions, spans)):
            state, session_events, session_names, captured = _session_activity(
                session, start_ms, deps, vexa_meeting_id
            )
            events += _held_to(
                session_events,
                start_ms if i > 0 else None,
                end_ms if i < last else None,
            )
            speaker_names += session_names
            states.append(state)
            if state in ("ok", "capped"):
                # A session with channel-tap frames (Jitsi) exports only those:
                # their `ch` matches `channels/ch<N>.wav`, while its mixed-lane
                # frames reuse ch 0. Otherwise (Meet) every frame is per channel.
                taps = [frame for frame in captured if frame.source == "channel"]
                exported_frames.extend(
                    _activity_frame(frame, session.origin_ms, start_ms)
                    for frame in (taps or captured)
                )
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
            hangover_ms=settings.speech_hangover_ms,
        )
        participants = build_participants(
            speaker_names,
            platform=platform,
            meeting_id=meeting_uuid,
            joined_at=recording_started_at,
            host_email=None,
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
        try:
            _export_channels(
                sessions, spans, base, Path(tmp_dir), deps, vexa_meeting_id
            )
        except Exception as exc:
            logger.warning(
                "channel_export_failed meeting_id=%s vexa_meeting_id=%s "
                "error_class=%s error=%s; exporting the mixed audio only",
                meeting_uuid,
                vexa_meeting_id,
                type(exc).__name__,
                exc,
            )
        if any(state in ("ok", "capped") for state in states):
            storage.put_json(
                settings.export_bucket,
                base + FRAMES_FILE,
                exported_frames,
                retention=METADATA,
            )

        # A meeting whose bot ran with live transcription has segments; one
        # without has none, and gets no file.
        transcript = deps.meeting_api.transcript(vexa_meeting_id)
        if transcript is not None and transcript.get("segments"):
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
                for key in storage.list_keys(
                    settings.vexa_bucket, session.signal_prefix
                ):
                    storage.copy(
                        settings.vexa_bucket,
                        key,
                        settings.export_bucket,
                        signal_dst + key[len(session.signal_prefix) :],
                        retention=AUDIO,
                    )

        levels = _speaker_levels(wav_path, timeline, settings.speech_hangover_ms)
        quiet = {
            name: row
            for name, row in levels.items()
            if row["rms"] < settings.rms_speech_threshold
        }
        if quiet:
            finished = deps.now()
            detail = ", ".join(
                f"{name} rms={row['rms']:.4f} coverage_s={row['coverage_s']}"
                for name, row in sorted(levels.items())
            )
            error = f"audio_mismatch: {detail}"
            logger.error(
                "audio_mismatch vexa_meeting_id=%s %s",
                vexa_meeting_id,
                detail,
            )
            storage.put_json(
                settings.export_bucket,
                base + "_export.json",
                {
                    "state": "audio_mismatch",
                    "meeting_id": meeting_uuid,
                    "vexa_meeting_id": vexa_meeting_id,
                    "exported_at": finished.isoformat(),
                    "elapsed_s": (finished - started).total_seconds(),
                    "exporter_version": __version__,
                    "speaker_activity": "audio_mismatch",
                    "speaker_activity_events": len(events),
                    "audio_recordings": len(audio_recs),
                    "speaker_levels": levels,
                },
                retention=METADATA,
            )
            deps.export_result.report(meeting_uuid, "failed", s3_path, error)
            return ExportResult("audio_mismatch", folder)

        deps.notetaker.process(meeting_uuid, base, platform, rerun=rerun)

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
