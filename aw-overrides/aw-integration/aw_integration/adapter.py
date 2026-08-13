from __future__ import annotations

import io
import os
import wave
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal, cast

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

_MIN_DOMINANT_UTTERANCE_DEFAULT_MS = 1500


def _resolve_min_dominant_utterance_ms() -> int:
    """Shortest interval that may claim an instant away from a longer, overlapping one.

    Anything below this is treated as an interjection or noise (a cough, an
    "mm-hmm", a keyboard clatter) rather than a turn worth attributing. It only
    applies when a LONGER interval also covers the instant, so a standalone short
    utterance still owns its own span.

    The plausible RANGE is bounded below by the bot's 700ms silence hangover (a
    shorter interval cannot represent a settled turn) and above by the ~4s median
    real utterance seen in production. Within that range the default is a judgement
    call — nothing argues for 1500 over 900 or 2500. Treat it as a starting value to
    tune from live data, not a derived constant.

    KNOWN TRADE-OFF, quantified twice in review: a speaker whose turns are
    consistently sub-threshold loses those turns to a colleague whose longer
    interval covers them. A synthetic 200-turn model with 50% forced overlap
    retained ~42% of such a speaker's turns; an adversarial construction where a
    presenter's interval covers EVERY interjection retained 0% — 12 genuine short
    replies, none of them noise, all erased. So the floor is total loss, not graceful
    degradation. Where turns do not overlap, attribution is unaffected.

    Duration alone cannot separate a 0.5s real word from a 0.5s cough, so no fixed
    value removes this — it only moves where the failure lands. Hence the env
    override (mirrors SPEAKER_SILENCE_HANGOVER_MS on the bot side), so it can be
    lowered from a deployment without rebuilding the image.

    Note for live validation: in the meeting that motivated this work, two of three
    participants had a ~0.5s median burst. Watch specifically for their words
    disappearing during cross-talk, and lower this before concluding the collapse
    itself is at fault.
    """
    raw = os.environ.get("MIN_DOMINANT_UTTERANCE_MS")
    if not raw:
        return _MIN_DOMINANT_UTTERANCE_DEFAULT_MS
    try:
        parsed = int(raw)
    except ValueError:
        return _MIN_DOMINANT_UTTERANCE_DEFAULT_MS
    # Clamp: below the hangover it cannot represent a real turn; absurdly high
    # values would erase every short speaker entirely.
    if not 0 <= parsed <= 30_000:
        return _MIN_DOMINANT_UTTERANCE_DEFAULT_MS
    return parsed


MIN_DOMINANT_UTTERANCE_MS = _resolve_min_dominant_utterance_ms()


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
    # Which bot producer emitted this. "audio" = the per-track audio-activity
    # state machine, whose START/END pairs are reliable enough to form intervals.
    # "dom" = the DOM/CSS speaking-indicator bridge, whose pairing is not.
    # None = an older bot image that predates provenance tagging; treated as
    # untrusted (point-only), never paired.
    source: str | None = None


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
            # Sort by relative time, NOT Redis insertion order.
            #
            # The bot may publish a SPEAKER_START retroactively: an utterance whose
            # onset is known immediately but whose speaker name only resolves later
            # (vote/lock, or the 15s roster-order fallback) is emitted with its
            # ORIGINAL onset, and therefore lands in the stream after events that
            # started later. `xrange` returns insertion order, so iterating it
            # directly yields a timeline that is non-monotonic in `relative_sec`.
            #
            # Consumers map a transcript segment to the last event with
            # `relative_sec <= segment.start`, which assumes chronological order.
            # The notetaker-worker does sort defensively, but this artifact must be
            # correct on its own rather than depending on that.
            ordered_events = sorted(session.speaker_events, key=lambda e: e.relative_ms)
            timeline_events = self._build_dominant_speaker_timeline(
                ordered_events, origin_ms, duration_sec
            )

        if not timeline_events and session.speaker_events:
            # Safety valve. If the collapse produced nothing — e.g. a meeting where
            # the audio-activity path never resolved a name, so there are no
            # pairable events — emit the legacy one-point-per-START timeline from
            # whatever events exist, DOM included. A noisy timeline still yields
            # names; an EMPTY one makes notetaker-worker bail out
            # ("Timeline has no speaker events, cannot map speakers") and the
            # transcript reverts to raw SPEAKER_NN.
            origin_ms = int(session.session_start_ts.timestamp() * 1000)
            for ev in sorted(session.speaker_events, key=lambda e: e.relative_ms):
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

        if not timeline_events:
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

    def _build_dominant_speaker_timeline(
        self,
        ordered_events: list[VexaSpeakerEvent],
        origin_ms: int,
        duration_sec: float,
    ) -> list[SpeakerEvent]:
        """Collapse per-track audio bursts into dominant-speaker transitions.

        WHY THIS EXISTS
        ---------------
        `speaker_timeline.json` is consumed by notetaker-worker, whose rule is
        "attribute a transcript segment to the LAST event at or before the
        segment's start". That rule is correct for the contract it was written
        against: Prosody (Jitsi) emits *dominant-speaker transitions*, and it
        deliberately discards silence and suppresses consecutive same-speaker
        events, so each event means "the dominant speaker changed to X".

        The bot, by contrast, emitted one event per audio burst per track, for
        every participant simultaneously. Under a last-event-wins rule, a
        half-second noise on someone else's microphone becomes "the most recent
        speaker change" and captures the remainder of another person's sentence.
        Observed live on 2026-08-12: a 0.5s burst at 105.700s stole a sentence
        starting at 106s from a speaker who held the floor from 95.4s to 120.9s.

        So this converts our raw bursts into the shape the contract expects,
        rather than changing the consumer. Two steps:

        1. PAIR trusted (`source == "audio"`) START/END events into intervals.
           Only the audio-activity state machine is paired, because it
           guarantees every START is named and eventually closed. DOM/CSS
           events are excluded entirely — their pairing is unreliable
           (two state machines can double-emit one utterance, there is no
           terminal flush, and a tile removed mid-utterance never closes). They
           must not be admitted even as points, or a stray DOM point
           reintroduces exactly the bug above.
        2. COLLAPSE overlaps so at most one speaker is dominant at any instant:
           the longest interval covering a span wins, and a transition is
           emitted only when the dominant speaker changes.

        Returns [] when nothing pairable exists; the caller then falls back to
        the legacy one-point-per-START timeline rather than emitting nothing.
        """
        # ── 1. Pair trusted events into intervals ────────────────────────────
        open_starts: dict[str, int] = {}
        intervals: list[tuple[str, int, int]] = []
        # `relative_ms` is relative to session_start_ts (the caller's origin),
        # so the session's own duration bounds any still-open interval.
        session_end_ms = max(0, int(round(duration_sec * 1000)))

        for ev in ordered_events:
            if ev.source != "audio":
                continue
            name = ev.participant_name
            if not name:
                continue

            if ev.event_type == "SPEAKER_START":
                prior = open_starts.get(name)
                if prior is not None and ev.relative_ms > prior:
                    # A second START with no intervening END. Close the previous
                    # utterance here rather than discarding it.
                    intervals.append((name, prior, ev.relative_ms))
                open_starts[name] = ev.relative_ms
            elif ev.event_type == "SPEAKER_END":
                prior = open_starts.pop(name, None)
                if prior is not None and ev.relative_ms > prior:
                    intervals.append((name, prior, ev.relative_ms))
                # An END with no matching START is ignored. Never synthesise an
                # interval from an orphan — that would invent speech.

        # Anything still open when the meeting ended (e.g. someone talking
        # through the final moment) closes at the session boundary.
        for name, start in open_starts.items():
            if session_end_ms > start:
                intervals.append((name, start, session_end_ms))

        if not intervals:
            return []

        # ── 2. Collapse to one dominant speaker per instant ──────────────────
        # Every boundary is an interval endpoint, so within each consecutive
        # pair an interval either fully covers the span or does not intersect it.
        bounds = sorted({b for _, s, e in intervals for b in (s, e)})
        events: list[SpeakerEvent] = []
        last_name: str | None = None

        for span_start, span_end in zip(bounds, bounds[1:]):
            if span_end <= span_start:
                continue

            covering = [
                iv for iv in intervals if iv[1] <= span_start and iv[2] >= span_end
            ]
            if not covering:
                # Silence. Prosody records none, so neither do we; the previous
                # transition simply remains in effect.
                continue

            # Pick the dominant speaker for this span.
            #
            # Naive "longest interval wins" fixes the reported bug (a 0.5s blip
            # inside a 25s run must not steal the sentence) but REGRESSES a real
            # case: a genuine 5s utterance nested inside one very long interval —
            # someone with a persistently open mic — would never become dominant
            # and would vanish from the timeline entirely, handing their whole
            # contribution to the noisy participant. Verified by probe before
            # settling on the rule below.
            #
            # So: ignore sub-threshold intervals as noise, and among what remains
            # prefer the SHORTEST — the most specific claim on this instant, i.e.
            # the person actively speaking in this narrow window rather than
            # whoever happens to have a long-running interval. If everything
            # covering the span is sub-threshold, fall back to the longest, which
            # keeps a standalone short utterance owning its own span.
            #
            # Tie-breaks are fully ordered (duration, then earliest start, then
            # name) so identical input always yields a byte-identical artifact.
            substantial = [
                iv for iv in covering if (iv[2] - iv[1]) >= MIN_DOMINANT_UTTERANCE_MS
            ]
            if substantial:
                dominant = min(
                    substantial, key=lambda iv: ((iv[2] - iv[1]), iv[1], iv[0])
                )
            else:
                # Everything covering this span is sub-threshold. Let the INCUMBENT
                # keep the floor: earliest start wins.
                #
                # This branch used to pick the longest, which quietly recreated the
                # very bug this change exists to kill — just below 1.5s instead of
                # above it. A 600ms real utterance overlapped by an 800ms blip
                # produced a spurious transition to the blip mid-utterance
                # (transition at 10.2s instead of 10.6s). Preferring the earliest
                # start means a later, briefly-longer claim cannot interrupt a turn
                # already in progress; it can only take over once that turn ends.
                dominant = min(
                    covering, key=lambda iv: (iv[1], -(iv[2] - iv[1]), iv[0])
                )
            name = dominant[0]

            if name == last_name:
                # Match Prosody's consecutive-same-speaker suppression: the
                # artifact carries transitions, not samples.
                continue
            last_name = name

            events.append(
                SpeakerEvent(
                    timestamp_ms=origin_ms + span_start,
                    relative_sec=span_start / 1000.0,
                    speaker_id=self._slug(name),
                    speaker_name=name,
                )
            )

        return events

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

    raw_ts = cast(
        list[tuple[str, dict[str, str]]],
        r.xrange("transcription_segments", count=50000),
    )
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

    raw_se = cast(
        list[tuple[str, dict[str, str]]],
        r.xrange("speaker_events_relative", count=50000),
    )
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
                    # Deliberately `.get()`, not `fields[...]`: events published
                    # by a bot image older than provenance tagging have no
                    # `source`, and must degrade to point-only rather than being
                    # dropped by the KeyError guard below.
                    source=fields.get("source"),
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
