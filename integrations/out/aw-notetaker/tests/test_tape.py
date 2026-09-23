"""Tests for tape parsing and speech event detection."""

from aw_exporter.tape import TapeEvent, parse_tape, speech_events, names
from tests.builders import frame, header, hint

ORIGIN_MS = 1_000_000  # origin epoch ms


def ev(e: TapeEvent) -> tuple[str, int, str, str]:
    """Compact view of TapeEvent for assertions."""
    return (e.name, e.relative_ms, e.event_type, e.source)


def test_gmeet_one_utterance() -> None:
    t = parse_tape(
        [
            header(),
            frame(ORIGIN_MS + 0, "A", 0.2),
            frame(ORIGIN_MS + 256, "A", 0.2),
            frame(ORIGIN_MS + 512, "A", 0.0),
            frame(ORIGIN_MS + 1600, "A", 0.0),
        ]
    )
    assert [ev(e) for e in speech_events(t, ORIGIN_MS, 0.05, 700)] == [
        ("A", 0, "SPEAKER_START", "audio"),
        ("A", 512, "SPEAKER_END", "audio"),
    ]


def test_gmeet_gap_shorter_than_hangover_is_one_utterance() -> None:
    t = parse_tape(
        [
            header(),
            frame(ORIGIN_MS, "A", 0.2),
            frame(ORIGIN_MS + 256, "A", 0.0),
            frame(ORIGIN_MS + 512, "A", 0.2),
            frame(ORIGIN_MS + 3000, "A", 0.0),
        ]
    )
    out = [ev(e) for e in speech_events(t, ORIGIN_MS, 0.05, 700)]
    assert out == [
        ("A", 0, "SPEAKER_START", "audio"),
        ("A", 768, "SPEAKER_END", "audio"),
    ]


def test_gmeet_unnamed_frames_ignored_and_speakers_independent() -> None:
    t = parse_tape(
        [
            header(),
            frame(ORIGIN_MS, None, 0.9),
            frame(ORIGIN_MS, "A", 0.2, idx=0),
            frame(ORIGIN_MS + 100, "B", 0.2, idx=1),
            frame(ORIGIN_MS + 5000, "A", 0.0),
        ]
    )
    out = [ev(e) for e in speech_events(t, ORIGIN_MS, 0.05, 700)]
    assert ("A", 0, "SPEAKER_START", "audio") in out
    assert ("B", 100, "SPEAKER_START", "audio") in out
    assert all(e[0] in {"A", "B"} for e in out)


def test_gmeet_open_speech_closes_at_last_voiced_time() -> None:
    t = parse_tape([header(), frame(ORIGIN_MS, "A", 0.2)])
    assert [ev(e) for e in speech_events(t, ORIGIN_MS, 0.05, 700)][-1] == (
        "A",
        256,
        "SPEAKER_END",
        "audio",
    )


def test_mixed_lane_hints_are_points() -> None:
    t = parse_tape(
        [
            header("mixed"),
            hint(ORIGIN_MS + 10, "A"),
            hint(ORIGIN_MS + 900, "A", is_end=True),
            hint(ORIGIN_MS + 1000, "B"),
        ]
    )
    assert [ev(e) for e in speech_events(t, ORIGIN_MS, 0.05, 700)] == [
        ("A", 10, "SPEAKER_START", "hint"),
        ("A", 900, "SPEAKER_END", "hint"),
        ("B", 1000, "SPEAKER_START", "hint"),
    ]


def test_bad_line_skipped_and_names() -> None:
    t = parse_tape(
        [header(), "{not json", frame(ORIGIN_MS, "A", 0.2), frame(ORIGIN_MS, "B", 0.0)]
    )
    assert names(t) == ["A", "B"]


def test_no_header_raises() -> None:
    import pytest

    with pytest.raises(ValueError):
        parse_tape([frame(ORIGIN_MS, "A", 0.2)])
