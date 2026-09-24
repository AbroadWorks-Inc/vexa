"""Tests for exporter.attribution (spec §4.3).

Ported from aw-integration/tests/test_adapter.py (AbroadWorks' own Apache-2.0
fork, not Attendee). `VexaSpeakerEvent(relative_ms=..., event_type=...,
participant_name=..., source="audio"/None/"dom"/"caption")` becomes
`ActivityEvent(name, relative_ms, event_type, "audio"/"hint")` — speaker activity
carries one hint source per lane, so "dom", "caption" and untagged all collapse onto
"hint" here. `VexaSessionAdapter.build_speaker_timeline`/`build_participants`
become the module-level functions of the same name. Expected values are
unchanged from the reference where ported. See task-6-report.md for the full
list of what was skipped/dropped and why.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from exporter.attribution import build_participants, build_speaker_timeline
from exporter.schemas import ParticipantsFile, SpeakerEvent, SpeakerTimelineFile
from exporter.activity import ActivityEvent, EventType

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _tl(
    events: list[ActivityEvent], platform: str = "google_meet", seconds: int = 60
) -> SpeakerTimelineFile:
    return build_speaker_timeline(
        events,
        platform=platform,
        meeting_id="vexa-1",
        room_name="r",
        recording_started_at=T0,
        recording_ended_at=T0 + timedelta(seconds=seconds),
        min_dominant_utterance_ms=1500,
    )


def _audio(name: str, event_type: EventType, relative_ms: int) -> ActivityEvent:
    """A trusted, audio-activity-derived event (paired into intervals)."""
    return ActivityEvent(name, relative_ms, event_type, "audio")


def _hint(name: str, event_type: EventType, relative_ms: int) -> ActivityEvent:
    """A point-in-time claim: the mixed-lane hint source, and the reference's
    untagged/"dom"/"caption" point producers, which all collapse onto "hint"
    here — never paired into an interval."""
    return ActivityEvent(name, relative_ms, event_type, "hint")


def _attribute(timeline: list[SpeakerEvent], segment_start_sec: float) -> str | None:
    """Replicate notetaker-worker's mapping rule exactly: walk the sorted
    events and keep the last one at or before the segment's start."""
    current = None
    for ev in sorted(timeline, key=lambda e: e.relative_sec):
        if ev.relative_sec <= segment_start_sec:
            current = ev.speaker_name
        else:
            break
    return current


# ---------------------------------------------------------------------------
# New tests (brief-mandated minimum)
# ---------------------------------------------------------------------------


def test_blip_inside_long_turn_does_not_steal_it() -> None:
    ev = [
        ActivityEvent("A", 0, "SPEAKER_START", "audio"),
        ActivityEvent("B", 10_000, "SPEAKER_START", "audio"),
        ActivityEvent("B", 10_500, "SPEAKER_END", "audio"),
        ActivityEvent("A", 25_000, "SPEAKER_END", "audio"),
    ]
    tl = _tl(ev)
    assert [p.speaker_name for p in tl.speaker_timeline] == ["A"]
    assert [(i.speaker_name, i.start_sec, i.end_sec) for i in tl.speaker_intervals] == [
        ("A", 0.0, 25.0),
        ("B", 10.0, 10.5),
    ]


def test_hint_points_zoom_anchor_needs_two_speakers() -> None:
    one = _tl([ActivityEvent("A", 5_000, "SPEAKER_START", "hint")], platform="zoom")
    assert one.speaker_timeline[0].relative_sec == 5.0
    two = _tl(
        [
            ActivityEvent("A", 5_000, "SPEAKER_START", "hint"),
            ActivityEvent("B", 9_000, "SPEAKER_START", "hint"),
        ],
        platform="zoom",
    )
    assert two.speaker_timeline[0].relative_sec == 0.0


def test_teams_gets_intervals_from_points() -> None:
    tl = _tl(
        [
            ActivityEvent("A", 1_000, "SPEAKER_START", "hint"),
            ActivityEvent("B", 9_000, "SPEAKER_START", "hint"),
        ],
        platform="teams",
        seconds=20,
    )
    assert [(i.speaker_name, i.start_sec, i.end_sec) for i in tl.speaker_intervals] == [
        ("A", 0.0, 9.0),
        ("B", 9.0, 20.0),
    ]


def test_empty_events_give_empty_timeline() -> None:
    tl = _tl([])
    assert tl.speaker_timeline == [] and tl.speaker_intervals == []


def test_participants() -> None:
    p = build_participants(
        ["Ann Lee", "Bo"],
        platform="zoom",
        meeting_id="vexa-1",
        joined_at=T0,
        host_email="host@example.com",
    )
    assert p.host.name == "host" and [x.id for x in p.participants] == [
        "ann_lee",
        "bo",
    ]


# ---------------------------------------------------------------------------
# Ported: clip to the recording (controller ruling — not in the reference)
# ---------------------------------------------------------------------------


def test_interval_starting_before_zero_is_clamped_to_zero() -> None:
    """A real Meet tape had a speaker's first START at -8ms relative to the
    recording origin. The point and the interval both clamp to 0.0 rather
    than carrying a negative offset."""
    tl = _tl([_audio("A", "SPEAKER_START", -8), _audio("A", "SPEAKER_END", 5_000)])
    assert tl.speaker_timeline[0].relative_sec == 0.0
    assert [(i.speaker_name, i.start_sec, i.end_sec) for i in tl.speaker_intervals] == [
        ("A", 0.0, 5.0)
    ]


def test_interval_entirely_before_zero_is_dropped() -> None:
    tl = _tl([_audio("A", "SPEAKER_START", -5_000), _audio("A", "SPEAKER_END", -1_000)])
    assert tl.speaker_intervals == []


def test_interval_running_past_duration_is_clipped_to_duration() -> None:
    tl = _tl(
        [_audio("A", "SPEAKER_START", 50_000), _audio("A", "SPEAKER_END", 70_000)],
        seconds=60,
    )
    assert [(i.speaker_name, i.start_sec, i.end_sec) for i in tl.speaker_intervals] == [
        ("A", 50.0, 60.0)
    ]


# ---------------------------------------------------------------------------
# Ported: basic shape of build_speaker_timeline
# ---------------------------------------------------------------------------


def test_timeline_returns_speaker_timeline_file_type() -> None:
    assert isinstance(_tl([]), SpeakerTimelineFile)


def test_timeline_meeting_id_matches_argument() -> None:
    assert _tl([]).meeting_id == "vexa-1"


def test_timeline_duration_sec_equals_end_minus_start() -> None:
    assert _tl([], seconds=60).duration_sec == 60.0


def test_timeline_has_one_entry_per_speaker_start_event() -> None:
    ev = [
        _hint("Ann Lee", "SPEAKER_START", 4_500),
        _hint("Ann Lee", "SPEAKER_END", 9_500),
        _hint("Bo", "SPEAKER_START", 19_800),
    ]
    tl = _tl(ev)
    # 2 SPEAKER_START events (Ann Lee and Bo); the END is not a START.
    assert len(tl.speaker_timeline) == 2


def test_timeline_is_sorted_when_events_are_published_late() -> None:
    """A retroactively published SPEAKER_START (earlier onset, later in the
    input) must not break timeline ordering."""
    ev = [
        _hint("Bo", "SPEAKER_START", 9_000),
        _hint("Ann Lee", "SPEAKER_START", 1_000),  # earlier onset, listed later
    ]
    tl = _tl(ev)
    relatives = [e.relative_sec for e in tl.speaker_timeline]
    assert relatives == sorted(relatives)
    assert tl.speaker_timeline[0].speaker_name == "Ann Lee"


def test_timeline_timestamp_ms_is_origin_plus_relative_ms() -> None:
    ev = [
        _hint("Ann Lee", "SPEAKER_START", 4_500),
        _hint("Ann Lee", "SPEAKER_END", 9_500),
        _hint("Bo", "SPEAKER_START", 19_800),
    ]
    tl = _tl(ev)
    origin_ms = int(T0.timestamp() * 1000)
    ann = next(e for e in tl.speaker_timeline if e.speaker_name == "Ann Lee")
    # google_meet -> the zoom/teams-gated anchor does not fire.
    assert ann.timestamp_ms == origin_ms + 4500


def test_timeline_speaker_id_is_slug_of_participant_name() -> None:
    ev = [_hint("Ann Lee", "SPEAKER_START", 4_500)]
    tl = _tl(ev)
    assert tl.speaker_timeline[0].speaker_id == "ann_lee"


# ---------------------------------------------------------------------------
# Ported: dominant-speaker collapse (audio-derived intervals)
# ---------------------------------------------------------------------------


def test_short_burst_inside_a_long_run_does_not_steal_the_sentence() -> None:
    ev = [
        _audio("Speaker B", "SPEAKER_START", 95_400),
        _audio("Speaker A", "SPEAKER_START", 105_700),
        _audio("Speaker A", "SPEAKER_END", 106_200),
        _audio("Speaker B", "SPEAKER_END", 120_900),
    ]
    timeline = _tl(ev, seconds=130).speaker_timeline
    assert _attribute(timeline, 106.0) == "Speaker B"
    assert all(e.speaker_name != "Speaker A" for e in timeline)


def test_second_misattribution_shape_is_fixed() -> None:
    ev = [
        _audio("Speaker B", "SPEAKER_START", 138_900),
        _audio("Speaker C", "SPEAKER_START", 147_428),
        _audio("Speaker C", "SPEAKER_END", 147_900),
        _audio("Speaker B", "SPEAKER_END", 164_400),
    ]
    timeline = _tl(ev, seconds=170).speaker_timeline
    assert _attribute(timeline, 148.0) == "Speaker B"


def test_real_utterance_nested_in_a_very_long_interval_is_not_swallowed() -> None:
    """A naive "longest interval wins" rule regresses this: a persistently
    open mic must not swallow a genuine nested utterance."""
    ev = [
        _audio("Speaker A", "SPEAKER_START", 0),  # open mic, whole meeting
        _audio("Speaker A", "SPEAKER_END", 55_000),
        _audio("Speaker B", "SPEAKER_START", 20_000),  # a real 5s turn inside it
        _audio("Speaker B", "SPEAKER_END", 25_000),
    ]
    timeline = _tl(ev).speaker_timeline
    assert _attribute(timeline, 22.0) == "Speaker B"
    assert _attribute(timeline, 30.0) == "Speaker A"


def test_sub_threshold_blip_still_loses_to_a_longer_interval() -> None:
    ev = [
        _audio("Speaker A", "SPEAKER_START", 0),  # open mic
        _audio("Speaker A", "SPEAKER_END", 55_000),
        _audio("Speaker B", "SPEAKER_START", 20_000),  # real turn
        _audio("Speaker B", "SPEAKER_END", 25_000),
        _audio("Speaker C", "SPEAKER_START", 21_000),  # 0.4s cough
        _audio("Speaker C", "SPEAKER_END", 21_400),
    ]
    timeline = _tl(ev).speaker_timeline
    assert _attribute(timeline, 21_200 / 1000.0) == "Speaker B"
    assert all(e.speaker_name != "Speaker C" for e in timeline)


def test_competing_sub_threshold_claims_do_not_interrupt_the_incumbent() -> None:
    ev = [
        _audio("Speaker A", "SPEAKER_START", 10_000),
        _audio("Speaker A", "SPEAKER_END", 10_600),  # 600ms real turn
        _audio("Speaker B", "SPEAKER_START", 10_200),  # 800ms blip over its tail
        _audio("Speaker B", "SPEAKER_END", 11_000),
    ]
    timeline = _tl(ev).speaker_timeline
    assert _attribute(timeline, 10.4) == "Speaker A"
    b_events = [e for e in timeline if e.speaker_name == "Speaker B"]
    assert b_events, "B should still appear once A has finished"
    assert b_events[0].relative_sec >= 10.6


def test_standalone_short_utterance_still_owns_its_span() -> None:
    ev = [
        _audio("Speaker A", "SPEAKER_START", 10_000),
        _audio("Speaker A", "SPEAKER_END", 10_400),  # 0.4s, nothing overlaps
    ]
    timeline = _tl(ev).speaker_timeline
    assert [e.speaker_name for e in timeline] == ["Speaker A"]
    assert _attribute(timeline, 10.2) == "Speaker A"


def test_genuine_alternation_yields_one_transition_per_change() -> None:
    ev = [
        _audio("Ann Lee", "SPEAKER_START", 1_000),
        _audio("Ann Lee", "SPEAKER_END", 5_000),
        _audio("Bo", "SPEAKER_START", 6_000),
        _audio("Bo", "SPEAKER_END", 9_000),
    ]
    timeline = _tl(ev).speaker_timeline
    assert [e.speaker_name for e in timeline] == ["Ann Lee", "Bo"]
    assert _attribute(timeline, 2.0) == "Ann Lee"
    assert _attribute(timeline, 7.0) == "Bo"


def test_consecutive_same_speaker_intervals_collapse_to_one_point() -> None:
    ev = [
        _audio("Ann Lee", "SPEAKER_START", 1_000),
        _audio("Ann Lee", "SPEAKER_END", 3_000),
        _audio("Ann Lee", "SPEAKER_START", 4_000),
        _audio("Ann Lee", "SPEAKER_END", 6_000),
    ]
    timeline = _tl(ev).speaker_timeline
    assert len(timeline) == 1
    assert timeline[0].speaker_name == "Ann Lee"


def test_timeline_is_monotonic_and_deterministic() -> None:
    ev = [
        _audio("Ann Lee", "SPEAKER_START", 10_000),
        _audio("Ann Lee", "SPEAKER_END", 20_000),
        _audio("Bo", "SPEAKER_START", 12_000),
        _audio("Bo", "SPEAKER_END", 13_000),
        _audio("Ann Lee", "SPEAKER_START", 1_000),  # retroactive publish
        _audio("Ann Lee", "SPEAKER_END", 2_000),
    ]
    first = _tl(list(ev)).speaker_timeline
    rel = [e.relative_sec for e in first]
    assert rel == sorted(rel), f"not monotonic: {rel}"

    # Same input -> byte-identical output (tie-breaks fully ordered).
    second = _tl(list(ev)).speaker_timeline
    assert [(e.relative_sec, e.speaker_name) for e in first] == [
        (e.relative_sec, e.speaker_name) for e in second
    ]


def test_unclosed_interval_is_closed_at_session_end() -> None:
    """Someone still talking when the meeting ends still gets an interval."""
    timeline = _tl([_audio("Ann Lee", "SPEAKER_START", 5_000)]).speaker_timeline
    assert [e.speaker_name for e in timeline] == ["Ann Lee"]
    assert _attribute(timeline, 50.0) == "Ann Lee"


def test_orphan_end_never_invents_an_interval() -> None:
    ev = [
        _audio("Speaker B", "SPEAKER_START", 1_000),
        _audio("Speaker C", "SPEAKER_END", 5_000),  # orphan: no START
        _audio("Speaker B", "SPEAKER_END", 10_000),
    ]
    timeline = _tl(ev).speaker_timeline
    assert [e.speaker_name for e in timeline] == ["Speaker B"]
    assert all(e.speaker_name != "Speaker C" for e in timeline)


def test_hint_events_are_never_paired_into_intervals() -> None:
    """The core of the collapse: a stray non-audio point must not steal a
    sentence, because an audio interval covers the moment."""
    ev = [
        _audio("Speaker B", "SPEAKER_START", 95_400),
        _audio("Speaker B", "SPEAKER_END", 120_900),
        _hint("Speaker A", "SPEAKER_START", 105_700),
        _hint("Speaker A", "SPEAKER_END", 106_200),
    ]
    timeline = _tl(ev, seconds=130).speaker_timeline
    assert _attribute(timeline, 106.0) == "Speaker B"
    assert all(e.speaker_name != "Speaker A" for e in timeline)


def test_point_only_shape_is_not_paired_and_keeps_every_speaker() -> None:
    """The normal shape of a Teams/Zoom meeting (or Meet before pairing
    resolves): point-in-time claims only, no matching close, ever. If these
    were treated as pairable, the longest such interval would win every span
    and every speaker after the first would vanish."""
    ev = [
        _hint("Speaker A", "SPEAKER_START", 0),
        _hint("Speaker B", "SPEAKER_START", 10_000),
        _hint("Speaker C", "SPEAKER_START", 20_000),
        _hint("Speaker D", "SPEAKER_START", 30_000),
    ]
    timeline = _tl(ev).speaker_timeline
    assert [e.speaker_name for e in timeline] == [
        "Speaker A",
        "Speaker B",
        "Speaker C",
        "Speaker D",
    ]
    assert _attribute(timeline, 5.0) == "Speaker A"
    assert _attribute(timeline, 15.0) == "Speaker B"
    assert _attribute(timeline, 25.0) == "Speaker C"
    assert _attribute(timeline, 35.0) == "Speaker D"


def test_audio_intervals_take_precedence_over_hint_points() -> None:
    """When any pairable audio interval exists, hint points are excluded
    entirely rather than merged in as competing points."""
    ev = [
        _audio("Speaker A", "SPEAKER_START", 0),
        _audio("Speaker A", "SPEAKER_END", 5_000),
        _hint("Speaker B", "SPEAKER_START", 1_000),  # excluded
        _hint("Speaker C", "SPEAKER_START", 2_000),  # excluded
    ]
    timeline = _tl(ev).speaker_timeline
    assert [e.speaker_name for e in timeline] == ["Speaker A"]
    assert _attribute(timeline, 2.0) == "Speaker A"


def test_two_partially_overlapping_real_turns() -> None:
    """Crosstalk between two genuine turns, overlapping at the edges (not
    nested). Each speaker owns the part of the span where they alone are
    active."""
    ev = [
        _audio("Speaker A", "SPEAKER_START", 0),
        _audio("Speaker A", "SPEAKER_END", 20_000),
        _audio("Speaker B", "SPEAKER_START", 15_000),  # cuts in at 15s
        _audio("Speaker B", "SPEAKER_END", 40_000),
    ]
    timeline = _tl(ev).speaker_timeline
    assert _attribute(timeline, 5.0) == "Speaker A"
    assert _attribute(timeline, 30.0) == "Speaker B"


def test_hint_events_fall_back_to_legacy_points() -> None:
    timeline = _tl([_hint("Ann Lee", "SPEAKER_START", 4_500)]).speaker_timeline
    assert [e.speaker_name for e in timeline] == ["Ann Lee"]
    # google_meet -> the zoom/teams-gated anchor does not fire.
    assert timeline[0].relative_sec == 4.5


def test_hint_only_meeting_falls_back_rather_than_emitting_nothing() -> None:
    """Safety valve: an EMPTY timeline makes notetaker-worker bail
    ("Timeline has no speaker events") and the transcript reverts to raw
    SPEAKER_NN. A noisy timeline is strictly better than none."""
    ev = [
        _hint("Ann Lee", "SPEAKER_START", 1_000),
        _hint("Ann Lee", "SPEAKER_END", 2_000),
        _hint("Bo", "SPEAKER_START", 3_000),
    ]
    timeline = _tl(ev).speaker_timeline
    assert len(timeline) == 2  # one per SPEAKER_START
    assert {e.speaker_name for e in timeline} == {"Ann Lee", "Bo"}


def test_silence_gap_emits_no_event() -> None:
    """Prosody discards silence; so must this."""
    ev = [
        _audio("Ann Lee", "SPEAKER_START", 1_000),
        _audio("Ann Lee", "SPEAKER_END", 2_000),
        _audio("Bo", "SPEAKER_START", 30_000),
        _audio("Bo", "SPEAKER_END", 31_000),
    ]
    timeline = _tl(ev).speaker_timeline
    assert [e.relative_sec for e in timeline] == [1.0, 30.0]
    assert _attribute(timeline, 15.0) == "Ann Lee"


def test_second_start_without_end_closes_the_previous_utterance() -> None:
    ev = [
        _audio("Ann Lee", "SPEAKER_START", 1_000),
        _audio("Ann Lee", "SPEAKER_START", 5_000),  # no END between
        _audio("Ann Lee", "SPEAKER_END", 9_000),
        _audio("Bo", "SPEAKER_START", 2_000),
        _audio("Bo", "SPEAKER_END", 3_000),
    ]
    timeline = _tl(ev).speaker_timeline
    # Ann Lee's [1,5] and [5,9] both exist; Bo's 1s sits inside Ann Lee's 4s
    # interval, so Ann Lee stays dominant throughout.
    assert [e.speaker_name for e in timeline] == ["Ann Lee"]
    assert _attribute(timeline, 2.5) == "Ann Lee"


# ---------------------------------------------------------------------------
# Ported: speaker_intervals (raw paired audio intervals, additive to the
# collapsed speaker_timeline)
# ---------------------------------------------------------------------------


def test_overlapping_intervals_are_both_emitted_with_overlap_intact() -> None:
    ev = [
        _audio("Speaker A", "SPEAKER_START", 0),
        _audio("Speaker A", "SPEAKER_END", 20_000),
        _audio("Speaker B", "SPEAKER_START", 15_000),  # overlaps A's tail
        _audio("Speaker B", "SPEAKER_END", 40_000),
    ]
    result = _tl(ev)
    assert [
        (iv.speaker_name, iv.start_sec, iv.end_sec) for iv in result.speaker_intervals
    ] == [("Speaker A", 0.0, 20.0), ("Speaker B", 15.0, 40.0)]
    a, b = result.speaker_intervals
    assert a.start_sec < b.start_sec < a.end_sec < b.end_sec


def test_intervals_are_sorted_by_start_sec() -> None:
    ev = [
        _audio("Speaker C", "SPEAKER_START", 20_000),
        _audio("Speaker C", "SPEAKER_END", 21_000),
        _audio("Speaker A", "SPEAKER_START", 0),
        _audio("Speaker A", "SPEAKER_END", 1_000),
        _audio("Speaker B", "SPEAKER_START", 10_000),
        _audio("Speaker B", "SPEAKER_END", 11_000),
    ]
    result = _tl(ev)
    starts = [iv.start_sec for iv in result.speaker_intervals]
    assert starts == sorted(starts)
    assert [iv.speaker_name for iv in result.speaker_intervals] == [
        "Speaker A",
        "Speaker B",
        "Speaker C",
    ]


def test_collapsed_speaker_timeline_is_byte_identical_regardless_of_intervals() -> None:
    """Regression guard: emitting speaker_intervals must not alter
    speaker_timeline. Mixes overlap (A/B), a nested sub-threshold interval
    (C), and two excluded hint points (D, E)."""
    ev = [
        _audio("Speaker A", "SPEAKER_START", 0),
        _audio("Speaker A", "SPEAKER_END", 20_000),
        _audio("Speaker B", "SPEAKER_START", 15_000),
        _audio("Speaker B", "SPEAKER_END", 40_000),
        _audio("Speaker C", "SPEAKER_START", 5_000),
        _audio("Speaker C", "SPEAKER_END", 6_000),
        _hint("Speaker D", "SPEAKER_START", 1_000),
        _hint("Speaker E", "SPEAKER_START", 2_000),
    ]
    result = _tl(ev)
    origin_ms = int(T0.timestamp() * 1000)
    assert [
        (e.timestamp_ms, e.relative_sec, e.speaker_id, e.speaker_name)
        for e in result.speaker_timeline
    ] == [
        (origin_ms + 0, 0.0, "speaker_a", "Speaker A"),
        (origin_ms + 20_000, 20.0, "speaker_b", "Speaker B"),
    ]
    assert [
        (iv.speaker_name, iv.start_sec, iv.end_sec) for iv in result.speaker_intervals
    ] == [("Speaker A", 0.0, 20.0), ("Speaker C", 5.0, 6.0), ("Speaker B", 15.0, 40.0)]


def test_hint_sourced_events_are_not_turned_into_intervals() -> None:
    ev = [
        _hint("Ann Lee", "SPEAKER_START", 1_000),
        _hint("Ann Lee", "SPEAKER_END", 2_000),
        _hint("Bo", "SPEAKER_START", 3_000),
    ]
    result = _tl(ev)
    assert len(result.speaker_timeline) == 2  # safety-valve path still runs
    assert result.speaker_intervals == []  # nothing was pairable


def test_unpaired_start_is_bounded_by_session_end() -> None:
    result = _tl([_audio("Ann Lee", "SPEAKER_START", 5_000)])
    assert len(result.speaker_intervals) == 1
    iv = result.speaker_intervals[0]
    assert iv.speaker_name == "Ann Lee"
    assert iv.start_sec == 5.0
    assert iv.end_sec == 60.0


def test_second_start_without_end_produces_two_intervals() -> None:
    ev = [
        _audio("Ann Lee", "SPEAKER_START", 1_000),
        _audio("Ann Lee", "SPEAKER_START", 5_000),  # no END between
        _audio("Ann Lee", "SPEAKER_END", 9_000),
    ]
    result = _tl(ev)
    assert [(iv.start_sec, iv.end_sec) for iv in result.speaker_intervals] == [
        (1.0, 5.0),
        (5.0, 9.0),
    ]


def test_hint_events_yield_no_intervals() -> None:
    ev = [
        _hint("Speaker A", "SPEAKER_START", 0),
        _hint("Speaker B", "SPEAKER_START", 10_000),
    ]
    assert _tl(ev).speaker_intervals == []


def test_audio_intervals_present_excludes_hint_points_from_intervals() -> None:
    ev = [
        _audio("Speaker A", "SPEAKER_START", 0),
        _audio("Speaker A", "SPEAKER_END", 5_000),
        _hint("Speaker B", "SPEAKER_START", 1_000),
        _hint("Speaker C", "SPEAKER_START", 2_000),
    ]
    result = _tl(ev)
    assert [
        (iv.speaker_name, iv.start_sec, iv.end_sec) for iv in result.speaker_intervals
    ] == [("Speaker A", 0.0, 5.0)]


# ---------------------------------------------------------------------------
# Ported: earliest-event anchor gate (zoom/teams, >= 2 distinct speakers)
# ---------------------------------------------------------------------------


def test_zoom_anchors_earliest_event_when_two_speakers() -> None:
    ev = [
        _audio("Ann Lee", "SPEAKER_START", 2_900),
        _audio("Ann Lee", "SPEAKER_END", 6_000),
        _audio("Bo", "SPEAKER_START", 10_000),
        _audio("Bo", "SPEAKER_END", 12_000),
    ]
    timeline = _tl(ev, platform="zoom").speaker_timeline
    assert timeline[0].relative_sec == 0.0
    assert timeline[0].speaker_name == "Ann Lee"
    assert _attribute(timeline, 2.0) == "Ann Lee"
    # Only the earliest is anchored; later transitions keep true onsets.
    assert timeline[-1].speaker_name == "Bo"
    assert timeline[-1].relative_sec == 10.0


def test_zoom_does_not_anchor_when_only_one_speaker() -> None:
    ev = [
        _audio("Ann Lee", "SPEAKER_START", 2_900),
        _audio("Ann Lee", "SPEAKER_END", 6_000),
    ]
    timeline = _tl(ev, platform="zoom").speaker_timeline
    assert [e.speaker_name for e in timeline] == ["Ann Lee"]
    # NOT anchored — a segment at 2.0s stays unattributed rather than being
    # donated to Ann Lee.
    assert timeline[0].relative_sec == 2.9
    assert _attribute(timeline, 2.0) is None


def test_non_zoom_never_anchors_even_with_two_speakers() -> None:
    ev = [
        _audio("Ann Lee", "SPEAKER_START", 2_900),
        _audio("Ann Lee", "SPEAKER_END", 6_000),
        _audio("Bo", "SPEAKER_START", 10_000),
        _audio("Bo", "SPEAKER_END", 12_000),
    ]
    timeline = _tl(ev, platform="google_meet").speaker_timeline
    assert timeline[0].relative_sec == 2.9
    assert timeline[0].speaker_name == "Ann Lee"


# ---------------------------------------------------------------------------
# Ported: Teams earliest-event anchor
# ---------------------------------------------------------------------------


def test_teams_anchors_earliest_event_when_two_speakers() -> None:
    ev = [
        _hint("Ann Lee", "SPEAKER_START", 11_342),  # e.g. late-flowing captions
        _hint("Bo", "SPEAKER_START", 30_000),
    ]
    timeline = _tl(ev, platform="teams").speaker_timeline
    assert timeline[0].relative_sec == 0.0
    assert timeline[0].speaker_name == "Ann Lee"
    assert _attribute(timeline, 0.0) == "Ann Lee"
    assert _attribute(timeline, 5.0) == "Ann Lee"
    assert timeline[-1].relative_sec == 30.0


def test_teams_does_not_anchor_with_only_one_speaker() -> None:
    timeline = _tl(
        [_hint("Ann Lee", "SPEAKER_START", 11_342)], platform="teams"
    ).speaker_timeline
    assert [e.speaker_name for e in timeline] == ["Ann Lee"]
    assert timeline[0].relative_sec == 11.342
    assert _attribute(timeline, 0.0) is None


def test_meet_is_still_not_anchored() -> None:
    ev = [
        _hint("Ann Lee", "SPEAKER_START", 11_342),
        _hint("Bo", "SPEAKER_START", 30_000),
    ]
    timeline = _tl(ev, platform="google_meet").speaker_timeline
    assert timeline[0].relative_sec == 11.342


# ---------------------------------------------------------------------------
# Ported: Teams intervals-from-points
# ---------------------------------------------------------------------------


def test_teams_derives_intervals_from_hint_runs() -> None:
    """The worker picks its speaker mapper on the PRESENCE of
    speaker_intervals. Without them, Teams gets the legacy "owner at the
    segment's first instant" rule, which misattributes across a speaker
    change mid-segment. With intervals present the worker splits on overlap."""
    ev = [
        _hint("Ann Lee", "SPEAKER_START", 2_000),
        _hint("Bo", "SPEAKER_START", 10_000),
    ]
    ivs = _tl(ev, platform="teams").speaker_intervals
    assert [(iv.speaker_name, iv.start_sec, iv.end_sec) for iv in ivs] == [
        # Ann Lee starts at 0.0, not 2.0: the t=0 anchor already pulled the
        # earliest point to the origin, and the interval follows it.
        ("Ann Lee", 0.0, 10.0),
        ("Bo", 10.0, 60.0),  # closed at the next speaker's first point
    ]


def test_a_run_of_points_becomes_one_interval_not_one_each() -> None:
    """Points arrive ~1/sec, so per-point intervals would be useless — runs
    are the unit."""
    ev = [
        _hint("Ann Lee", "SPEAKER_START", 1_000),
        _hint("Ann Lee", "SPEAKER_START", 2_000),
        _hint("Ann Lee", "SPEAKER_START", 3_000),
        _hint("Bo", "SPEAKER_START", 20_000),
        _hint("Bo", "SPEAKER_START", 21_000),
    ]
    ivs = _tl(ev, platform="teams").speaker_intervals
    assert len(ivs) == 2, f"5 points must collapse to 2 runs, got {len(ivs)}"
    assert [(iv.speaker_name, iv.start_sec, iv.end_sec) for iv in ivs] == [
        ("Ann Lee", 0.0, 20.0),
        ("Bo", 20.0, 60.0),
    ]


def test_a_cross_speaker_timestamp_tie_is_deterministic_and_never_zero_length() -> None:
    """Two different speakers at the same instant: pick one, always the same
    one. No zero-length interval reaches the artifact, and the same input
    always produces the same output."""

    def build() -> list[tuple[str, float, float]]:
        ev = [
            # Deliberately NOT the earliest point: the t=0 anchor breaks a
            # tie there, so a tie has to be mid-conversation to reach this.
            _hint("Speaker A", "SPEAKER_START", 1_000),
            _hint("Speaker B", "SPEAKER_START", 10_000),
            _hint("Speaker C", "SPEAKER_START", 10_000),  # exact tie
            _hint("Speaker D", "SPEAKER_START", 20_000),
        ]
        return [
            (iv.speaker_name, iv.start_sec, iv.end_sec)
            for iv in _tl(ev, platform="teams").speaker_intervals
        ]

    first = build()
    assert all(
        end > start for _, start, end in first
    ), f"a zero-length interval reached the artifact: {first}"
    assert build() == first, "the same input produced different intervals"
    assert len(first) == 3, f"expected 3 runs from 4 points with a tie: {first}"
    names = [n for n, _, _ in first]
    assert len(set(names)) == 3, f"one speaker must lose the tie: {names}"


def test_teams_with_one_named_speaker_still_emits_no_intervals() -> None:
    """Below the 2-speaker gate, stay points-only: an interval spanning the
    session would donate unattributed speech to the one named speaker."""
    ev = [
        _hint("Ann Lee", "SPEAKER_START", 2_000),
        _hint("Ann Lee", "SPEAKER_START", 9_000),
    ]
    assert _tl(ev, platform="teams").speaker_intervals == []


# ---------------------------------------------------------------------------
# Ported: build_participants
# ---------------------------------------------------------------------------


def test_participants_returns_participants_file_type() -> None:
    p = build_participants(
        [], platform="zoom", meeting_id="vexa-1", joined_at=T0, host_email=None
    )
    assert isinstance(p, ParticipantsFile)


def test_participants_meeting_id_matches_argument() -> None:
    p = build_participants(
        [], platform="zoom", meeting_id="vexa-1", joined_at=T0, host_email=None
    )
    assert p.meeting_id == "vexa-1"


def test_participants_host_id_is_host_zero() -> None:
    p = build_participants(
        [], platform="zoom", meeting_id="vexa-1", joined_at=T0, host_email=None
    )
    assert p.host.id == "host-0"


def test_participants_host_name_is_local_part_of_organizer_email() -> None:
    p = build_participants(
        [],
        platform="zoom",
        meeting_id="vexa-1",
        joined_at=T0,
        host_email="host@abroadworks.com",
    )
    assert p.host.name == "host"


def test_participants_list_contains_every_given_speaker() -> None:
    p = build_participants(
        ["Ann Lee", "Bo"],
        platform="zoom",
        meeting_id="vexa-1",
        joined_at=T0,
        host_email=None,
    )
    names = {x.name for x in p.participants}
    assert "Ann Lee" in names
    assert "Bo" in names


def test_participant_id_is_slug_of_name() -> None:
    p = build_participants(
        ["Ann Lee"], platform="zoom", meeting_id="vexa-1", joined_at=T0, host_email=None
    )
    assert p.participants[0].id == "ann_lee"


def test_participants_deduplicated_by_slug_first_occurrence_wins() -> None:
    """Ruling T6a: two spellings of one name (same slug) must yield exactly
    one participant record, keeping the first-seen spelling."""
    p = build_participants(
        ["Ann Lee", "ann lee "],
        platform="zoom",
        meeting_id="vexa-1",
        joined_at=T0,
        host_email=None,
    )
    assert [x.id for x in p.participants] == ["ann_lee"]
    assert p.participants[0].name == "Ann Lee"
