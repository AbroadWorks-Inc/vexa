"""Tests for speaker-activity parsing and speech event detection."""

import json

import pytest

from exporter.activity import ActivityEvent, names, parse_activity, speech_events
from tests.builders import capped, frame, header, hint

ORIGIN_MS = 1_000_000  # origin epoch ms


def ev(e: ActivityEvent) -> tuple[str, int, str, str]:
    """Compact view of ActivityEvent for assertions."""
    return (e.name, e.relative_ms, e.event_type, e.source)


def test_gmeet_one_utterance() -> None:
    a = parse_activity(
        [
            header(),
            frame(ORIGIN_MS + 0, "A", 0.2),
            frame(ORIGIN_MS + 256, "A", 0.2),
            frame(ORIGIN_MS + 512, "A", 0.0),
            frame(ORIGIN_MS + 1600, "A", 0.0),
        ]
    )
    assert [ev(e) for e in speech_events(a, ORIGIN_MS, 0.05, 700)] == [
        ("A", 0, "SPEAKER_START", "audio"),
        ("A", 512, "SPEAKER_END", "audio"),
    ]


def test_gmeet_gap_shorter_than_hangover_is_one_utterance() -> None:
    a = parse_activity(
        [
            header(),
            frame(ORIGIN_MS, "A", 0.2),
            frame(ORIGIN_MS + 256, "A", 0.0),
            frame(ORIGIN_MS + 512, "A", 0.2),
            frame(ORIGIN_MS + 3000, "A", 0.0),
        ]
    )
    out = [ev(e) for e in speech_events(a, ORIGIN_MS, 0.05, 700)]
    assert out == [
        ("A", 0, "SPEAKER_START", "audio"),
        ("A", 768, "SPEAKER_END", "audio"),
    ]


def test_gmeet_unnamed_frames_ignored_and_speakers_independent() -> None:
    a = parse_activity(
        [
            header(),
            frame(ORIGIN_MS, None, 0.9),
            frame(ORIGIN_MS, "A", 0.2, ch=0),
            frame(ORIGIN_MS + 100, "B", 0.2, ch=1),
            frame(ORIGIN_MS + 5000, "A", 0.0),
        ]
    )
    out = [ev(e) for e in speech_events(a, ORIGIN_MS, 0.05, 700)]
    assert ("A", 0, "SPEAKER_START", "audio") in out
    assert ("B", 100, "SPEAKER_START", "audio") in out
    assert all(e[0] in {"A", "B"} for e in out)


def test_gmeet_open_speech_closes_at_last_voiced_time() -> None:
    a = parse_activity([header(), frame(ORIGIN_MS, "A", 0.2)])
    assert [ev(e) for e in speech_events(a, ORIGIN_MS, 0.05, 700)][-1] == (
        "A",
        256,
        "SPEAKER_END",
        "audio",
    )


def test_mixed_lane_hints_are_points() -> None:
    a = parse_activity(
        [
            header("mixed"),
            hint(ORIGIN_MS + 10, "A"),
            hint(ORIGIN_MS + 900, "A", is_end=True),
            hint(ORIGIN_MS + 1000, "B"),
        ]
    )
    assert [ev(e) for e in speech_events(a, ORIGIN_MS, 0.05, 700)] == [
        ("A", 10, "SPEAKER_START", "hint"),
        ("A", 900, "SPEAKER_END", "hint"),
        ("B", 1000, "SPEAKER_START", "hint"),
    ]


def test_bad_line_skipped_and_names() -> None:
    a = parse_activity(
        [header(), "{not json", frame(ORIGIN_MS, "A", 0.2), frame(ORIGIN_MS, "B", 0.0)]
    )
    assert names(a) == ["A", "B"]


def test_no_header_raises() -> None:
    with pytest.raises(ValueError):
        parse_activity([frame(ORIGIN_MS, "A", 0.2)])


def test_header_only_is_valid_empty_activity() -> None:
    """A bot that left before anyone spoke uploads a header-only file; this
    is a valid, empty session — not invalid."""
    a = parse_activity([header()])
    assert a.frames == []
    assert a.hints == []
    assert a.capped is False
    assert names(a) == []
    assert speech_events(a, ORIGIN_MS, 0.05, 700) == []


def test_capped_marker_sets_capped_and_stops_parsing() -> None:
    a = parse_activity(
        [
            header(),
            frame(ORIGIN_MS, "A", 0.2),
            capped(ORIGIN_MS + 1000),
            frame(ORIGIN_MS + 2000, "B", 0.2),
        ]
    )
    assert a.capped is True
    assert names(a) == ["A"]


def test_frame_without_dur_ms_is_skipped() -> None:
    bad_frame = json.dumps({"t": ORIGIN_MS, "name": "A", "rms": 0.2})
    good_frame = frame(ORIGIN_MS + 256, "A", 0.2)
    a = parse_activity([header(), bad_frame, good_frame])
    assert names(a) == ["A"]
    events = [ev(e) for e in speech_events(a, ORIGIN_MS, 0.05, 700)]
    assert ("A", 256, "SPEAKER_START", "audio") in events


def test_hint_missing_t_skipped_next_valid_hint_parses() -> None:
    """Hint without 't' field raises KeyError; should skip and continue."""
    bad_hint = json.dumps({"type": "hint", "name": "A"})  # missing 't'
    good_hint = json.dumps({"type": "hint", "t": ORIGIN_MS + 100, "name": "B"})
    a = parse_activity([header("mixed"), bad_hint, good_hint])
    assert names(a) == ["B"]
    events = [ev(e) for e in speech_events(a, ORIGIN_MS, 0.05, 700)]
    assert ("B", 100, "SPEAKER_START", "hint") in events


def test_frame_with_non_numeric_t_skipped() -> None:
    """Frame with non-numeric 't' raises ValueError; should skip."""
    bad_frame = json.dumps(
        {"t": "not_a_number", "name": "A", "rms": 0.1, "dur_ms": 256}
    )
    good_frame = frame(ORIGIN_MS + 256, "A", 0.2)
    a = parse_activity([header(), bad_frame, good_frame])
    assert names(a) == ["A"]
    events = [ev(e) for e in speech_events(a, ORIGIN_MS, 0.05, 700)]
    assert ("A", 256, "SPEAKER_START", "audio") in events


def test_gmeet_with_frames_and_stray_hint_ignores_hint() -> None:
    """gmeet lane should use frame events only, ignoring any hints (spec §4.3)."""
    a = parse_activity(
        [
            header("gmeet"),
            frame(ORIGIN_MS, "A", 0.2),
            frame(ORIGIN_MS + 1000, "A", 0.0),
            hint(ORIGIN_MS + 500, "B"),  # stray hint in gmeet lane
        ]
    )
    events = [ev(e) for e in speech_events(a, ORIGIN_MS, 0.05, 700)]
    assert ("A", 0, "SPEAKER_START", "audio") in events
    assert ("A", 256, "SPEAKER_END", "audio") in events
    assert all(e[3] == "audio" for e in events)  # source is audio, not hint
    assert not any(e[0] == "B" for e in events)  # no B speaker


def test_mixed_lane_with_frames_and_hints_returns_hints_only() -> None:
    """mixed lane should use hint events only, ignoring frames (spec §4.3)."""
    a = parse_activity(
        [
            header("mixed"),
            frame(ORIGIN_MS, "A", 0.9),  # high RMS frame in mixed lane
            hint(ORIGIN_MS + 100, "B"),  # but B is in hints
        ]
    )
    events = [ev(e) for e in speech_events(a, ORIGIN_MS, 0.05, 700)]
    assert ("B", 100, "SPEAKER_START", "hint") in events
    assert all(e[3] == "hint" for e in events)  # source is hint only
    assert not any(e[0] == "A" for e in events)  # no A speaker
