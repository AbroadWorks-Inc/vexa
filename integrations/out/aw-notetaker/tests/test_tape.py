"""Tests for tape parsing and speech event detection."""

from exporter.tape import TapeEvent, parse_tape, speech_events, names
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


def test_hint_missing_t_skipped_next_valid_hint_parses() -> None:
    """Hint without 't' field raises KeyError; should skip and continue."""
    import json

    bad_hint = json.dumps({"type": "hint", "name": "A", "lane": "mixed"})  # missing 't'
    good_hint = json.dumps(
        {"type": "hint", "t": ORIGIN_MS + 100, "name": "B", "lane": "mixed"}
    )
    t = parse_tape([header("mixed"), bad_hint, good_hint])
    assert names(t) == ["B"]
    events = [ev(e) for e in speech_events(t, ORIGIN_MS, 0.05, 700)]
    assert ("B", 100, "SPEAKER_START", "hint") in events


def test_frame_with_non_numeric_ts_skipped() -> None:
    """Frame with non-numeric 'ts' raises ValueError; should skip."""
    import json

    bad_frame = json.dumps(
        {
            "ts": "not_a_number",
            "speakerName": "A",
            "rms": 0.1,
            "pcm_len": 4096,
            "lane": "gmeet",
        }
    )
    good_frame = frame(ORIGIN_MS + 256, "A", 0.2)
    t = parse_tape([header(), bad_frame, good_frame])
    assert names(t) == ["A"]
    events = [ev(e) for e in speech_events(t, ORIGIN_MS, 0.05, 700)]
    assert ("A", 256, "SPEAKER_START", "audio") in events


def test_header_sample_rate_zero_falls_back_to_16000() -> None:
    """Header with sample_rate 0 should not cause ZeroDivisionError; fall back to 16000."""
    import json

    bad_header = json.dumps(
        {"type": "captured_signal_header", "v": 1, "sample_rate": 0, "lane": "gmeet"}
    )
    good_frame = frame(ORIGIN_MS, "A", 0.2, samples=4096)
    t = parse_tape([bad_header, good_frame])
    assert t.sample_rate == 16000
    assert t.frames[0].duration_ms == 256  # 4096 / 16000 * 1000


def test_gmeet_with_frames_and_stray_hint_ignores_hint() -> None:
    """gmeet lane should use frame events only, ignoring any hints (spec §4.3)."""
    t = parse_tape(
        [
            header("gmeet"),
            frame(ORIGIN_MS, "A", 0.2),
            frame(ORIGIN_MS + 1000, "A", 0.0),
            hint(ORIGIN_MS + 500, "B"),  # stray hint in gmeet lane
        ]
    )
    events = [ev(e) for e in speech_events(t, ORIGIN_MS, 0.05, 700)]
    assert ("A", 0, "SPEAKER_START", "audio") in events
    assert ("A", 256, "SPEAKER_END", "audio") in events
    assert all(e[3] == "audio" for e in events)  # source is audio, not hint
    assert not any(e[0] == "B" for e in events)  # no B speaker


def test_mixed_lane_with_frames_and_hints_returns_hints_only() -> None:
    """mixed lane should use hint events only, ignoring frames (spec §4.3)."""
    t = parse_tape(
        [
            header("mixed"),
            frame(ORIGIN_MS, "A", 0.9),  # high RMS frame in mixed lane
            hint(ORIGIN_MS + 100, "B"),  # but B is in hints
        ]
    )
    events = [ev(e) for e in speech_events(t, ORIGIN_MS, 0.05, 700)]
    assert ("B", 100, "SPEAKER_START", "hint") in events
    assert all(e[3] == "hint" for e in events)  # source is hint only
    assert not any(e[0] == "A" for e in events)  # no A speaker


def test_header_non_numeric_sample_rate_falls_back_to_16000() -> None:
    import json

    bad_header = json.dumps(
        {"type": "captured_signal_header", "v": 1, "sample_rate": "abc"}
    )
    t = parse_tape([bad_header, frame(ORIGIN_MS, "A", 0.2, samples=4096)])
    assert t.sample_rate == 16000
    assert t.frames[0].duration_ms == 256


def test_header_null_sample_rate_falls_back_to_16000() -> None:
    import json

    bad_header = json.dumps(
        {"type": "captured_signal_header", "v": 1, "sample_rate": None}
    )
    t = parse_tape([bad_header, frame(ORIGIN_MS, "A", 0.2, samples=4096)])
    assert t.sample_rate == 16000
