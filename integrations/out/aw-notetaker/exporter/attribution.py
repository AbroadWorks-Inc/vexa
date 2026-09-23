"""Tape events -> speaker timeline + participants (spec §4.3).

Ported from aw-integration's `VexaSessionAdapter` (AbroadWorks' own Apache-2.0
fork, not Attendee — see AGENTS.md §3.4/§18.2). Operates on `TapeEvent` instead
of `VexaSpeakerEvent`/Redis streams, and on a resolved `names` list instead of
`VexaSegment`/`speaker_events` for the participant roster.
"""

from __future__ import annotations

from datetime import datetime

from exporter.schemas import (
    HostInfo,
    ParticipantInfo,
    ParticipantsFile,
    SpeakerEvent,
    SpeakerInterval,
    SpeakerTimelineFile,
    TimelineParticipant,
)
from exporter.tape import TapeEvent

__all__ = ["build_speaker_timeline", "build_participants"]


def _slug(name: str) -> str:
    return name.strip().replace(" ", "_").lower()


def _build_dominant_speaker_timeline(
    ordered_events: list[TapeEvent],
    origin_ms: int,
    duration_sec: float,
    min_dominant_utterance_ms: int,
) -> tuple[list[SpeakerEvent], list[tuple[str, int, int]]]:
    """Collapse per-track audio bursts into dominant-speaker transitions.

    notetaker-worker attributes a transcript segment to the LAST timeline event
    at or before the segment's start, which is only correct if each event marks
    a genuine change of dominant speaker. The tape instead carries one
    START/END pair per audio burst per track, so a stray half-second burst on
    someone else's microphone would otherwise "win" as the most recent change
    and steal the remainder of another speaker's sentence.

    Two steps: (1) pair trusted (`source == "audio"`) START/END events into
    intervals — only the audio-activity state machine guarantees every START
    is named and eventually closed; (2) collapse overlaps so at most one
    speaker is dominant at any instant. Among intervals covering a span,
    sub-threshold ones (shorter than `min_dominant_utterance_ms`) are treated
    as noise; among the rest the SHORTEST wins (the most specific claim on
    that instant, so a nested genuine utterance is not swallowed by a
    persistently open mic); if everything covering the span is sub-threshold,
    the incumbent (earliest start) keeps the floor rather than a later,
    briefly-longer claim interrupting a turn in progress. Ties are fully
    ordered (duration, start, name) for a deterministic artifact.

    Returns `(timeline_events, intervals)`: the collapsed transitions, and the
    RAW paired intervals (overlap and all) for
    `SpeakerTimelineFile.speaker_intervals`. Returns `([], [])` when nothing
    pairable exists.
    """
    open_starts: dict[str, int] = {}
    intervals: list[tuple[str, int, int]] = []
    session_end_ms = max(0, int(round(duration_sec * 1000)))

    for ev in ordered_events:
        if ev.source != "audio":
            continue
        name = ev.name
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
            # An END with no matching START is ignored — never synthesise an
            # interval from an orphan.

    # Anything still open at session end (e.g. someone talking through the
    # final moment) closes at the session boundary.
    for name, start in open_starts.items():
        if session_end_ms > start:
            intervals.append((name, start, session_end_ms))

    if not intervals:
        return [], []

    # Every boundary is an interval endpoint, so within each consecutive pair
    # an interval either fully covers the span or does not intersect it.
    bounds = sorted({b for _, s, e in intervals for b in (s, e)})
    events: list[SpeakerEvent] = []
    last_name: str | None = None

    for span_start, span_end in zip(bounds, bounds[1:]):
        if span_end <= span_start:
            continue

        covering = [iv for iv in intervals if iv[1] <= span_start and iv[2] >= span_end]
        if not covering:
            # Silence: the previous transition simply remains in effect.
            continue

        substantial = [
            iv for iv in covering if (iv[2] - iv[1]) >= min_dominant_utterance_ms
        ]
        if substantial:
            dominant = min(substantial, key=lambda iv: ((iv[2] - iv[1]), iv[1], iv[0]))
        else:
            dominant = min(covering, key=lambda iv: (iv[1], -(iv[2] - iv[1]), iv[0]))
        name = dominant[0]

        if name == last_name:
            # Suppress consecutive same-speaker transitions: the artifact
            # carries changes, not samples.
            continue
        last_name = name

        events.append(
            SpeakerEvent(
                timestamp_ms=origin_ms + span_start,
                relative_sec=span_start / 1000.0,
                speaker_id=_slug(name),
                speaker_name=name,
            )
        )

    return events, intervals


def _intervals_from_points(
    events: list[SpeakerEvent], duration_sec: float
) -> list[tuple[str, int, int]]:
    """Collapse runs of same-speaker points into closed (name, start_ms, end_ms).

    Used for Teams: one mixed audio stream means the per-track audio-activity
    boundary machine never arms, so speaker changes have to be read off the
    point timeline instead. Each run closes at the next speaker's first
    point; the final run closes at the recording's `duration_sec`.

    Zero-length runs are dropped (the worker requires `end > start`). A
    genuine tie — two different speakers at the same instant — is resolved
    deterministically: the sort key puts the lower `speaker_id` first, that
    run has zero length and is dropped, and the instant goes to the other
    speaker.
    """
    ordered = sorted(events, key=lambda ev: (ev.relative_sec, ev.speaker_id))
    session_end_ms = max(0, int(round(duration_sec * 1000)))
    out: list[tuple[str, int, int]] = []
    i = 0
    while i < len(ordered):
        name = ordered[i].speaker_name
        start_ms = int(round(ordered[i].relative_sec * 1000))
        j = i + 1
        while j < len(ordered) and ordered[j].speaker_name == name:
            j += 1
        stop_ms = (
            int(round(ordered[j].relative_sec * 1000))
            if j < len(ordered)
            else session_end_ms
        )
        if stop_ms > start_ms:
            out.append((name, start_ms, stop_ms))
        i = j
    return out


def build_speaker_timeline(
    events: list[TapeEvent],
    *,
    platform: str,
    meeting_id: str,
    room_name: str,
    recording_started_at: datetime,
    recording_ended_at: datetime,
    min_dominant_utterance_ms: int,
) -> SpeakerTimelineFile:
    duration_sec = (recording_ended_at - recording_started_at).total_seconds()
    origin_ms = int(recording_started_at.timestamp() * 1000)

    # Distinct speaker names in first-seen order, from `events` as given (not
    # the time-sorted copy built below).
    speaker_names: dict[str, str] = {}
    for ev in events:
        speaker_names.setdefault(_slug(ev.name), ev.name)
    participants = [
        TimelineParticipant(id=slug, name=name) for slug, name in speaker_names.items()
    ]

    timeline_events: list[SpeakerEvent] = []
    paired_intervals: list[tuple[str, int, int]] = []
    if events:
        ordered_events = sorted(events, key=lambda e: e.relative_ms)
        timeline_events, paired_intervals = _build_dominant_speaker_timeline(
            ordered_events, origin_ms, duration_sec, min_dominant_utterance_ms
        )

    if not timeline_events and events:
        # Safety valve: the mixed-lane ("hint") path, and Meet's own fallback
        # when nothing paired. A noisy timeline still yields names; an empty
        # one makes notetaker-worker bail out and the transcript reverts to
        # raw SPEAKER_NN. Not ported: the reference adapter's caption/DOM
        # preference between competing point producers — the 0.12 tape has
        # exactly one hint source per lane, so there is nothing to prefer
        # between.
        for ev in sorted(events, key=lambda e: e.relative_ms):
            if ev.event_type != "SPEAKER_START":
                continue
            timeline_events.append(
                SpeakerEvent(
                    timestamp_ms=origin_ms + ev.relative_ms,
                    relative_sec=ev.relative_ms / 1000.0,
                    speaker_id=_slug(ev.name),
                    speaker_name=ev.name,
                )
            )

    # Anchor the earliest event to the recording origin so a leading
    # transcript segment attributes to the first speaker instead of a raw
    # SPEAKER_NN. Gated to Zoom/Teams (Meet's timeline stays untouched) and to
    # >= 2 distinct named speakers — with one, anchoring it to t=0 would
    # donate the whole meeting, including any unnamed speaker's words, to
    # that one name.
    distinct_speakers = {ev.speaker_id for ev in timeline_events}
    if platform in ("zoom", "teams") and len(distinct_speakers) >= 2:
        earliest = min(timeline_events, key=lambda ev: ev.relative_sec)
        if earliest.relative_sec > 0:
            earliest.relative_sec = 0.0
            earliest.timestamp_ms = origin_ms

    # Teams: derive intervals from the point runs. One mixed audio stream
    # means the audio-activity path never arms for Teams, so without this the
    # worker falls back to "whoever spoke at the segment's first instant owns
    # the whole segment". Gated the same as the anchor above, and must run
    # after it: the anchor may already have zeroed the earliest point, and
    # the first interval should start there.
    if platform == "teams" and not paired_intervals and len(distinct_speakers) >= 2:
        paired_intervals = _intervals_from_points(timeline_events, duration_sec)

    # Clip to the recording: a tape event may start slightly before audio
    # t=0 (a real Meet tape had a speaker's first START at -8ms). Clamp
    # rather than emit a negative offset, or an interval hanging outside
    # [0, duration_sec]; an interval that clamps to zero length is dropped.
    for point in timeline_events:
        if point.relative_sec < 0:
            point.relative_sec = 0.0
            point.timestamp_ms = origin_ms

    duration_ms = max(0, int(round(duration_sec * 1000)))
    clipped_intervals: list[tuple[str, int, int]] = []
    for name, start_ms, end_ms in paired_intervals:
        start_ms = max(start_ms, 0)
        end_ms = min(end_ms, duration_ms)
        if end_ms > start_ms:
            clipped_intervals.append((name, start_ms, end_ms))

    speaker_intervals = [
        SpeakerInterval(
            speaker_id=_slug(name),
            speaker_name=name,
            start_sec=start_ms / 1000.0,
            end_sec=end_ms / 1000.0,
        )
        for name, start_ms, end_ms in sorted(
            clipped_intervals, key=lambda iv: (iv[1], iv[2], iv[0])
        )
    ]

    return SpeakerTimelineFile(
        room_name=room_name,
        meeting_id=meeting_id,
        platform=platform,
        recording_started_at=recording_started_at,
        recording_ended_at=recording_ended_at,
        duration_sec=duration_sec,
        start_time=recording_started_at.timestamp(),
        participants=participants,
        speaker_timeline=timeline_events,
        speaker_intervals=speaker_intervals,
    )


def build_participants(
    names: list[str],
    *,
    platform: str,
    meeting_id: str,
    joined_at: datetime,
    host_email: str | None,
) -> ParticipantsFile:
    host = HostInfo(
        id="host-0",
        name=host_email.split("@")[0] if host_email else "",
        email=host_email,
    )
    # De-duplicate by slug, first occurrence wins — same rule as the timeline's
    # own participant roster (Ruling T6a): two spellings of one name (e.g.
    # "Ann Lee" / "ann lee ") must not produce two participant records with
    # the same id.
    seen: dict[str, str] = {}
    for name in names:
        seen.setdefault(_slug(name), name)
    participants = [
        ParticipantInfo(
            id=slug,
            name=name,
            joined_at=joined_at,
            is_external=False,
        )
        for slug, name in seen.items()
    ]
    return ParticipantsFile(
        meeting_id=meeting_id,
        platform=platform,
        host=host,
        participants=participants,
    )
