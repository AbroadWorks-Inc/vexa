"""Tests for aw_integration adapter, s3_writer, and notetaker_client (§13.15).

Run with:
    PYTHONPATH=../../../notetaker-common:. pytest tests/test_adapter.py
"""

from __future__ import annotations

from datetime import datetime, timedelta as _timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from aw_integration.adapter import (
    VexaSegment,
    VexaSession,
    VexaSpeakerEvent,
    VexaSessionAdapter,
)
from aw_integration.notetaker_client import NoteTakerClientWrapper
from aw_integration.s3_writer import S3Writer
from notetaker_common.schemas import (
    BotJob,
    ConsentConfig,
    JoinConfig,
    LiveStreamingConfig,
    MetadataFile,
    ParticipantsFile,
    ProcessRequest,
    RetryConfig,
    SpeakerTimelineFile,
)

# Stale-install guard. `speaker_intervals` is an ADDITIVE, defaulted field on
# `SpeakerTimelineFile`. Pydantic's default `extra='ignore'` means passing
# `speaker_intervals=` to a model built from a stale (pre-field) install of
# notetaker-common silently DROPS the kwarg — every assertion below about
# `speaker_intervals` would then pass vacuously against an empty default list
# rather than testing anything. Fail loudly, at collection time, instead.
assert "speaker_intervals" in SpeakerTimelineFile.model_fields, (
    "notetaker_common.schemas.SpeakerTimelineFile has no `speaker_intervals` "
    "field — the installed notetaker-common is stale. Reinstall from "
    "/var/www/html/aw-notetaker/notetaker-common before trusting any test "
    "in this file that touches speaker_intervals."
)

# ---------------------------------------------------------------------------
# Helpers / shared fixtures
# ---------------------------------------------------------------------------

SESSION_START = datetime(2024, 1, 15, 9, 0, 0, tzinfo=timezone.utc)
SESSION_END = datetime(2024, 1, 15, 9, 1, 0, tzinfo=timezone.utc)


def make_test_job() -> BotJob:
    return BotJob(
        job_id="test-job-ulid",
        meeting_id="42",
        platform="meet",
        scheduled_start_at=datetime(2026, 6, 22, 9, 0, 0, tzinfo=timezone.utc),
        expected_duration_min=30,
        hard_deadline_at=datetime(2026, 6, 22, 13, 0, 0, tzinfo=timezone.utc),
        join=JoinConfig(
            url="https://meet.google.com/abc-defg-hij",
            organizer_email="host@abroadworks.com",
            requires_admit=False,
        ),
        display_name="AW Notetaker",
        consent=ConsentConfig(
            state="explicit",
            recorded_at=datetime(2026, 6, 22, 8, 55, 0, tzinfo=timezone.utc),
            by_user_id="user-001",
        ),
        retry=RetryConfig(attempts=0, max_attempts=2),
        s3_key="recordings/meet_42_test-job-ulid/",
        live_streaming=LiveStreamingConfig(enabled=False),
    )


def make_test_session(
    with_speaker_events: bool = True,
    saw_remote_camera: bool | None = None,
) -> VexaSession:
    segments = [
        VexaSegment(
            speaker="Alice Chen",
            text="Good morning everyone, let us get started.",
            start=5.0,
            end=9.2,
            language="en",
            completed=True,
            segment_id="seg-001",
            absolute_start_time=datetime(2024, 1, 15, 9, 0, 5, tzinfo=timezone.utc),
            absolute_end_time=datetime(2024, 1, 15, 9, 0, 9, tzinfo=timezone.utc),
        ),
        VexaSegment(
            speaker="Bob Müller",
            text="Thanks Alice. I have reviewed the proposal.",
            start=20.0,
            end=25.5,
            language="en",
            completed=True,
            segment_id="seg-002",
            absolute_start_time=datetime(2024, 1, 15, 9, 0, 20, tzinfo=timezone.utc),
            absolute_end_time=datetime(2024, 1, 15, 9, 0, 25, tzinfo=timezone.utc),
        ),
    ]
    speaker_events = (
        [
            VexaSpeakerEvent(
                uid="conn-abc123def456",
                relative_ms=4500,
                event_type="SPEAKER_START",
                participant_name="Alice Chen",
                meeting_id="42",
            ),
            VexaSpeakerEvent(
                uid="conn-abc123def456",
                relative_ms=9500,
                event_type="SPEAKER_END",
                participant_name="Alice Chen",
                meeting_id="42",
            ),
            VexaSpeakerEvent(
                uid="conn-abc123def456",
                relative_ms=19800,
                event_type="SPEAKER_START",
                participant_name="Bob Müller",
                meeting_id="42",
            ),
        ]
        if with_speaker_events
        else []
    )
    return VexaSession(
        uid="conn-abc123def456",
        platform="google_meet",
        meeting_id="42",
        session_start_ts=SESSION_START,
        session_end_ts=SESSION_END,
        segments=segments,
        speaker_events=speaker_events,
        saw_remote_camera=saw_remote_camera,
    )


def make_adapter(
    tmp_path: Path,
    pcm_bytes: bytes | None = None,
    with_speaker_events: bool = True,
    saw_remote_camera: bool | None = None,
) -> tuple[VexaSessionAdapter, MagicMock, MagicMock]:
    """Return (adapter, mock_s3_writer, mock_notetaker_client)."""
    raw_audio = tmp_path / "audio.raw"
    if pcm_bytes is not None:
        raw_audio.write_bytes(pcm_bytes)

    s3_writer = MagicMock(spec=S3Writer)
    notetaker_client = MagicMock(spec=NoteTakerClientWrapper)
    notetaker_client.post_process = AsyncMock(return_value={"status": "ok"})

    adapter = VexaSessionAdapter(
        session=make_test_session(
            with_speaker_events=with_speaker_events,
            saw_remote_camera=saw_remote_camera,
        ),
        job=make_test_job(),
        audio_raw_path=raw_audio,
        s3_writer=s3_writer,
        notetaker_client=notetaker_client,
    )
    return adapter, s3_writer, notetaker_client


# ---------------------------------------------------------------------------
# VexaSessionAdapter.encode_wav
# ---------------------------------------------------------------------------


class TestEncodeWav:
    def test_raises_file_not_found_when_path_does_not_exist(
        self, tmp_path: Path
    ) -> None:
        adapter, _, _ = make_adapter(tmp_path)  # file not written — path missing
        with pytest.raises(FileNotFoundError):
            adapter.encode_wav()

    def test_raises_file_not_found_when_audio_file_is_empty(
        self, tmp_path: Path
    ) -> None:
        adapter, _, _ = make_adapter(tmp_path, pcm_bytes=b"")
        with pytest.raises(FileNotFoundError):
            adapter.encode_wav()

    def test_returns_bytes_for_valid_pcm_input(self, tmp_path: Path) -> None:
        silence = bytes(32000)  # 1 second of 16-bit silence at 16000 Hz
        adapter, _, _ = make_adapter(tmp_path, pcm_bytes=silence)
        result = adapter.encode_wav()
        assert isinstance(result, bytes)
        assert len(result) > 0

    def test_wav_bytes_start_with_riff_header(self, tmp_path: Path) -> None:
        silence = bytes(32000)
        adapter, _, _ = make_adapter(tmp_path, pcm_bytes=silence)
        result = adapter.encode_wav()
        assert result[:4] == b"RIFF"

    def test_wav_bytes_contain_wave_marker(self, tmp_path: Path) -> None:
        silence = bytes(32000)
        adapter, _, _ = make_adapter(tmp_path, pcm_bytes=silence)
        result = adapter.encode_wav()
        assert b"WAVE" in result


# ---------------------------------------------------------------------------
# VexaSessionAdapter.build_speaker_timeline
# ---------------------------------------------------------------------------


class TestBuildSpeakerTimeline:
    def test_returns_speaker_timeline_file_type(self, tmp_path: Path) -> None:
        adapter, _, _ = make_adapter(tmp_path, pcm_bytes=bytes(32000))
        result = adapter.build_speaker_timeline()
        assert isinstance(result, SpeakerTimelineFile)

    def test_meeting_id_matches_session(self, tmp_path: Path) -> None:
        adapter, _, _ = make_adapter(tmp_path, pcm_bytes=bytes(32000))
        result = adapter.build_speaker_timeline()
        assert result.meeting_id == "42"

    def test_platform_is_mapped_to_canonical_form(self, tmp_path: Path) -> None:
        adapter, _, _ = make_adapter(tmp_path, pcm_bytes=bytes(32000))
        result = adapter.build_speaker_timeline()
        assert result.platform == "meet"

    def test_duration_sec_equals_session_end_minus_start(self, tmp_path: Path) -> None:
        adapter, _, _ = make_adapter(tmp_path, pcm_bytes=bytes(32000))
        result = adapter.build_speaker_timeline()
        expected = (SESSION_END - SESSION_START).total_seconds()
        assert result.duration_sec == expected

    def test_speaker_timeline_has_one_entry_per_speaker_start_event(
        self, tmp_path: Path
    ) -> None:
        adapter, _, _ = make_adapter(
            tmp_path, pcm_bytes=bytes(32000), with_speaker_events=True
        )
        result = adapter.build_speaker_timeline()
        # fixture has 2 SPEAKER_START events (Alice and Bob)
        assert len(result.speaker_timeline) == 2

    def test_speaker_timeline_is_sorted_when_events_are_published_late(
        self, tmp_path: Path
    ) -> None:
        """Retroactively published SPEAKER_STARTs must not break timeline ordering.

        The bot emits an utterance's START with its ORIGINAL onset even when the
        speaker's name only resolves seconds later (vote/lock, or the 15s
        roster-order fallback). Redis returns insertion order via xrange, so the
        stream can carry an earlier onset AFTER a later one. Consumers attribute a
        transcript segment to the last event with relative_sec <= segment.start,
        which is only correct if the timeline is monotonic.
        """
        adapter, _, _ = make_adapter(
            tmp_path, pcm_bytes=bytes(32000), with_speaker_events=True
        )
        # Insertion order deliberately inverted vs. chronological order.
        adapter._session.speaker_events = [
            VexaSpeakerEvent(
                uid="conn-abc123def456",
                relative_ms=9000,
                event_type="SPEAKER_START",
                participant_name="Bob Müller",
                meeting_id="42",
            ),
            VexaSpeakerEvent(
                uid="conn-abc123def456",
                relative_ms=1000,  # earlier onset, published later
                event_type="SPEAKER_START",
                participant_name="Alice Chen",
                meeting_id="42",
            ),
        ]

        result = adapter.build_speaker_timeline()

        relatives = [e.relative_sec for e in result.speaker_timeline]
        assert relatives == sorted(relatives), f"timeline not monotonic: {relatives}"
        assert result.speaker_timeline[0].speaker_name == "Alice Chen"

    def test_timestamp_ms_is_origin_plus_relative_ms(self, tmp_path: Path) -> None:
        adapter, _, _ = make_adapter(
            tmp_path, pcm_bytes=bytes(32000), with_speaker_events=True
        )
        result = adapter.build_speaker_timeline()
        origin_ms = int(SESSION_START.timestamp() * 1000)
        alice_event = next(
            e for e in result.speaker_timeline if e.speaker_name == "Alice Chen"
        )
        # Fixture is google_meet → Zoom-gated anchor does not fire → Alice keeps her
        # 4.5s onset, so timestamp_ms is origin + 4500.
        assert alice_event.timestamp_ms == origin_ms + 4500

    def test_speaker_id_is_slug_of_participant_name(self, tmp_path: Path) -> None:
        adapter, _, _ = make_adapter(
            tmp_path, pcm_bytes=bytes(32000), with_speaker_events=True
        )
        result = adapter.build_speaker_timeline()
        alice_event = next(
            e for e in result.speaker_timeline if e.speaker_name == "Alice Chen"
        )
        assert alice_event.speaker_id == "alice_chen"

    def test_fallback_derives_timeline_from_segments_when_no_speaker_events(
        self, tmp_path: Path
    ) -> None:
        adapter, _, _ = make_adapter(
            tmp_path, pcm_bytes=bytes(32000), with_speaker_events=False
        )
        result = adapter.build_speaker_timeline()
        # Two unique speakers in segments -> two fallback events
        assert len(result.speaker_timeline) == 2

    def test_fallback_event_speaker_id_matches_segment_speaker_slug(
        self, tmp_path: Path
    ) -> None:
        adapter, _, _ = make_adapter(
            tmp_path, pcm_bytes=bytes(32000), with_speaker_events=False
        )
        result = adapter.build_speaker_timeline()
        speaker_ids = {e.speaker_id for e in result.speaker_timeline}
        assert "alice_chen" in speaker_ids
        assert "bob_müller" in speaker_ids


# ---------------------------------------------------------------------------
# Dominant-speaker collapse (audio-derived intervals)
# ---------------------------------------------------------------------------


def _audio(name: str, event_type: str, relative_ms: int) -> VexaSpeakerEvent:
    """A trusted, audio-activity-derived event (paired into intervals)."""
    return VexaSpeakerEvent(
        uid="conn-abc123def456",
        relative_ms=relative_ms,
        event_type=event_type,
        participant_name=name,
        meeting_id="42",
        source="audio",
    )


def _untagged(name: str, event_type: str, relative_ms: int) -> VexaSpeakerEvent:
    """A point-in-time claim with no provenance tag.

    This is what Zoom and Teams emit for EVERY event (roster `joined` and caption
    `started_speaking`), and also what an older bot image emits. Never pairable.
    """
    return VexaSpeakerEvent(
        uid="conn-abc123def456",
        relative_ms=relative_ms,
        event_type=event_type,
        participant_name=name,
        meeting_id="42",
    )


def _dom(name: str, event_type: str, relative_ms: int) -> VexaSpeakerEvent:
    """A DOM/CSS-derived event — must never be paired into an interval."""
    return VexaSpeakerEvent(
        uid="conn-abc123def456",
        relative_ms=relative_ms,
        event_type=event_type,
        participant_name=name,
        meeting_id="42",
        source="dom",
    )


def _attribute(timeline: list, segment_start_sec: float) -> str | None:
    """Replicate notetaker-worker's mapping rule EXACTLY, unchanged.

    `map_segments_with_timeline` (talke, notetaker_worker.py:284-289) walks the
    sorted events and keeps the last one whose `relative_sec` is at or before
    the segment's start. These tests assert against that rule verbatim, because
    the whole point of the collapse is to make the EXISTING consumer correct
    rather than to change it.
    """
    current = None
    for ev in sorted(timeline, key=lambda e: e.relative_sec):
        if ev.relative_sec <= segment_start_sec:
            current = ev.speaker_name
        else:
            break
    return current


class TestDominantSpeakerCollapse:
    """Regression tests built from the two real misattributions of 2026-08-12.

    Both were reported by the owner against the B8 transcript, and both are
    reproduced here with the ACTUAL timestamps recovered from that meeting's
    `speaker_timeline.json` in S3.
    """

    def test_short_burst_inside_a_long_run_does_not_steal_the_sentence(
        self, tmp_path: Path
    ) -> None:
        """Live case 1: `[01:46]` was labelled Speaker A, truth was Speaker B.

        B8 shipped these raw points: 104.928 Speaker B, 105.700 Speaker A,
        107.928 Speaker B. The segment starts at 106.0s, so last-point-wins picked
        Speaker A's 0.5s blip. Speaker B actually held the floor 95.4s -> 120.9s.
        """
        adapter, _, _ = make_adapter(tmp_path, pcm_bytes=bytes(32000))
        adapter._session.speaker_events = [
            _audio("Speaker B", "SPEAKER_START", 95_400),
            _audio("Speaker A", "SPEAKER_START", 105_700),
            _audio("Speaker A", "SPEAKER_END", 106_200),
            _audio("Speaker B", "SPEAKER_END", 120_900),
        ]

        timeline = adapter.build_speaker_timeline().speaker_timeline

        assert _attribute(timeline, 106.0) == "Speaker B"
        # The nested blip must not produce a transition at all.
        assert all(e.speaker_name != "Speaker A" for e in timeline)

    def test_second_live_misattribution_is_fixed(self, tmp_path: Path) -> None:
        """Live case 2: `[02:28]` was labelled Speaker C, truth was Speaker B.

        B8 raw points: 146.428 Speaker B, 147.428 Speaker C, 149.428 Speaker B; segment
        starts at 148.0s.
        """
        adapter, _, _ = make_adapter(tmp_path, pcm_bytes=bytes(32000))
        adapter._session.speaker_events = [
            _audio("Speaker B", "SPEAKER_START", 138_900),
            _audio("Speaker C", "SPEAKER_START", 147_428),
            _audio("Speaker C", "SPEAKER_END", 147_900),
            _audio("Speaker B", "SPEAKER_END", 164_400),
        ]

        timeline = adapter.build_speaker_timeline().speaker_timeline

        assert _attribute(timeline, 148.0) == "Speaker B"

    def test_real_utterance_nested_in_a_very_long_interval_is_not_swallowed(
        self, tmp_path: Path
    ) -> None:
        """A naive "longest interval wins" rule regressed this case.

        Someone with a persistently open mic can produce one very long interval.
        Under longest-wins they dominate every instant, so a genuine 5s utterance
        nested inside it never becomes dominant, vanishes from the timeline
        entirely, and their whole contribution is credited to the noisy
        participant — far worse than the single-line bug this change fixes.

        The rule therefore ignores sub-threshold intervals as noise and otherwise
        prefers the SHORTEST covering interval, i.e. the most specific claim on
        that instant.
        """
        adapter, _, _ = make_adapter(tmp_path, pcm_bytes=bytes(32000))
        adapter._session.speaker_events = [
            _audio("Speaker A", "SPEAKER_START", 0),  # open mic, whole meeting
            _audio("Speaker A", "SPEAKER_END", 55_000),
            _audio("Speaker B", "SPEAKER_START", 20_000),  # a real 5s turn inside it
            _audio("Speaker B", "SPEAKER_END", 25_000),
        ]

        timeline = adapter.build_speaker_timeline().speaker_timeline

        assert _attribute(timeline, 22.0) == "Speaker B"
        # And control returns to the long-running speaker afterwards.
        assert _attribute(timeline, 30.0) == "Speaker A"

    def test_sub_threshold_blip_still_loses_to_a_longer_interval(
        self, tmp_path: Path
    ) -> None:
        """The other half of the rule: a brief interjection must NOT win.

        Three-way case that defeats both naive rules — longest-wins gives the
        open mic, shortest-wins gives the 0.4s blip, and only the
        threshold-then-shortest rule gives the real turn.
        """
        adapter, _, _ = make_adapter(tmp_path, pcm_bytes=bytes(32000))
        adapter._session.speaker_events = [
            _audio("Speaker A", "SPEAKER_START", 0),  # open mic
            _audio("Speaker A", "SPEAKER_END", 55_000),
            _audio("Speaker B", "SPEAKER_START", 20_000),  # real turn
            _audio("Speaker B", "SPEAKER_END", 25_000),
            _audio("Speaker C", "SPEAKER_START", 21_000),  # 0.4s cough
            _audio("Speaker C", "SPEAKER_END", 21_400),
        ]

        timeline = adapter.build_speaker_timeline().speaker_timeline

        assert _attribute(timeline, 21_200 / 1000.0) == "Speaker B"
        assert all(e.speaker_name != "Speaker C" for e in timeline)

    def test_competing_sub_threshold_claims_do_not_interrupt_the_incumbent(
        self, tmp_path: Path
    ) -> None:
        """The fallback branch used to recreate the original bug below 1.5s.

        When every interval covering a span is sub-threshold, the rule falls back
        rather than dropping them all. That fallback used to pick the LONGEST,
        which meant an 800ms blip overlapping the tail of a 600ms real turn stole
        it mid-utterance — a transition at 10.2s instead of 10.6s. Exactly the
        mechanism this change exists to kill, just at smaller scale.

        The incumbent now keeps the floor: a later claim can only take over once
        the turn in progress has actually ended. Found in review with a concrete
        repro; this is its regression guard.
        """
        adapter, _, _ = make_adapter(tmp_path, pcm_bytes=bytes(32000))
        adapter._session.speaker_events = [
            _audio("Speaker A", "SPEAKER_START", 10_000),
            _audio("Speaker A", "SPEAKER_END", 10_600),  # 600ms real turn
            _audio("Speaker B", "SPEAKER_START", 10_200),  # 800ms blip over its tail
            _audio("Speaker B", "SPEAKER_END", 11_000),
        ]

        timeline = adapter.build_speaker_timeline().speaker_timeline

        # A keeps its whole turn; B only takes over after A ends.
        assert _attribute(timeline, 10.4) == "Speaker A"
        b_events = [e for e in timeline if e.speaker_name == "Speaker B"]
        assert b_events, "B should still appear once A has finished"
        assert b_events[0].relative_sec >= 10.6

    def test_standalone_short_utterance_still_owns_its_span(
        self, tmp_path: Path
    ) -> None:
        """The threshold must not silence short turns that face no competition."""
        adapter, _, _ = make_adapter(tmp_path, pcm_bytes=bytes(32000))
        adapter._session.speaker_events = [
            _audio("Speaker A", "SPEAKER_START", 10_000),
            _audio("Speaker A", "SPEAKER_END", 10_400),  # 0.4s, nothing overlaps
        ]

        timeline = adapter.build_speaker_timeline().speaker_timeline

        assert [e.speaker_name for e in timeline] == ["Speaker A"]
        assert _attribute(timeline, 10.2) == "Speaker A"

    def test_genuine_alternation_yields_one_transition_per_change(
        self, tmp_path: Path
    ) -> None:
        adapter, _, _ = make_adapter(tmp_path, pcm_bytes=bytes(32000))
        adapter._session.speaker_events = [
            _audio("Alice Chen", "SPEAKER_START", 1_000),
            _audio("Alice Chen", "SPEAKER_END", 5_000),
            _audio("Bob Müller", "SPEAKER_START", 6_000),
            _audio("Bob Müller", "SPEAKER_END", 9_000),
        ]

        timeline = adapter.build_speaker_timeline().speaker_timeline

        assert [e.speaker_name for e in timeline] == ["Alice Chen", "Bob Müller"]
        assert _attribute(timeline, 2.0) == "Alice Chen"
        assert _attribute(timeline, 7.0) == "Bob Müller"

    def test_consecutive_same_speaker_intervals_collapse_to_one_point(
        self, tmp_path: Path
    ) -> None:
        """Prosody parity: it suppresses consecutive same-speaker events."""
        adapter, _, _ = make_adapter(tmp_path, pcm_bytes=bytes(32000))
        adapter._session.speaker_events = [
            _audio("Alice Chen", "SPEAKER_START", 1_000),
            _audio("Alice Chen", "SPEAKER_END", 3_000),
            _audio("Alice Chen", "SPEAKER_START", 4_000),
            _audio("Alice Chen", "SPEAKER_END", 6_000),
        ]

        timeline = adapter.build_speaker_timeline().speaker_timeline

        assert len(timeline) == 1
        assert timeline[0].speaker_name == "Alice Chen"

    def test_timeline_is_monotonic_and_deterministic(self, tmp_path: Path) -> None:
        adapter, _, _ = make_adapter(tmp_path, pcm_bytes=bytes(32000))
        events = [
            _audio("Alice Chen", "SPEAKER_START", 10_000),
            _audio("Alice Chen", "SPEAKER_END", 20_000),
            _audio("Bob Müller", "SPEAKER_START", 12_000),
            _audio("Bob Müller", "SPEAKER_END", 13_000),
            _audio("Alice Chen", "SPEAKER_START", 1_000),  # retroactive publish
            _audio("Alice Chen", "SPEAKER_END", 2_000),
        ]
        adapter._session.speaker_events = list(events)
        first = adapter.build_speaker_timeline().speaker_timeline

        rel = [e.relative_sec for e in first]
        assert rel == sorted(rel), f"not monotonic: {rel}"

        # Same input -> byte-identical output (tie-breaks fully ordered).
        adapter._session.speaker_events = list(events)
        second = adapter.build_speaker_timeline().speaker_timeline
        assert [(e.relative_sec, e.speaker_name) for e in first] == [
            (e.relative_sec, e.speaker_name) for e in second
        ]

    def test_unclosed_interval_is_closed_at_session_end(self, tmp_path: Path) -> None:
        """Someone still talking when the meeting ends still gets an interval."""
        adapter, _, _ = make_adapter(tmp_path, pcm_bytes=bytes(32000))
        adapter._session.speaker_events = [
            _audio("Alice Chen", "SPEAKER_START", 5_000),  # no END
        ]

        timeline = adapter.build_speaker_timeline().speaker_timeline

        assert [e.speaker_name for e in timeline] == ["Alice Chen"]
        # The 60s fixture session bounds it, so a later segment still resolves.
        assert _attribute(timeline, 50.0) == "Alice Chen"

    def test_orphan_end_never_invents_an_interval(self, tmp_path: Path) -> None:
        """An END with no START must not conjure speech for that speaker.

        Producer A cannot emit one (the `hadStart && closingName` guard), but the
        DOM path can, and a bot restart mid-meeting could too. Paired alongside a
        real interval so this asserts the invariant directly rather than falling
        through to the segment-derived fallback.
        """
        adapter, _, _ = make_adapter(tmp_path, pcm_bytes=bytes(32000))
        adapter._session.speaker_events = [
            _audio("Speaker B", "SPEAKER_START", 1_000),
            _audio("Speaker C", "SPEAKER_END", 5_000),  # orphan: no START
            _audio("Speaker B", "SPEAKER_END", 10_000),
        ]

        timeline = adapter.build_speaker_timeline().speaker_timeline

        assert [e.speaker_name for e in timeline] == ["Speaker B"]
        assert all(e.speaker_name != "Speaker C" for e in timeline)

    def test_no_usable_events_falls_through_to_segment_derived_timeline(
        self, tmp_path: Path
    ) -> None:
        """Last line of defence, and it is pre-existing behaviour.

        With nothing pairable AND no SPEAKER_START to fall back on, the timeline
        is derived from the transcript segments themselves. Documented here
        because the collapse makes this branch reachable in more situations than
        before, and an empty timeline would cost us names entirely.
        """
        adapter, _, _ = make_adapter(tmp_path, pcm_bytes=bytes(32000))
        adapter._session.speaker_events = [
            _audio("Alice Chen", "SPEAKER_END", 5_000),  # orphan END only
        ]

        timeline = adapter.build_speaker_timeline().speaker_timeline

        # Derived from the fixture's two segments, not from the orphan END.
        assert {e.speaker_name for e in timeline} == {"Alice Chen", "Bob Müller"}

    def test_dom_events_are_never_paired_into_intervals(self, tmp_path: Path) -> None:
        """The core of the fix: a stray DOM point must not steal a sentence.

        This is the exact shape that caused the live failures — but tagged
        'dom'. Because an audio interval covers the moment, the DOM blip must be
        excluded entirely rather than admitted as a competing point.
        """
        adapter, _, _ = make_adapter(tmp_path, pcm_bytes=bytes(32000))
        adapter._session.speaker_events = [
            _audio("Speaker B", "SPEAKER_START", 95_400),
            _audio("Speaker B", "SPEAKER_END", 120_900),
            _dom("Speaker A", "SPEAKER_START", 105_700),
            _dom("Speaker A", "SPEAKER_END", 106_200),
        ]

        timeline = adapter.build_speaker_timeline().speaker_timeline

        assert _attribute(timeline, 106.0) == "Speaker B"
        assert all(e.speaker_name != "Speaker A" for e in timeline)

    def test_teams_zoom_shape_is_not_paired_and_keeps_every_speaker(
        self, tmp_path: Path
    ) -> None:
        """The normal shape of every Teams/Zoom meeting must not be mangled.

        Teams and Zoom never arm the audio-activity boundary tracker, so 100% of
        their `speaker_events_relative` traffic is START-only, point-in-time
        claims — `joined` from the roster paths, `started_speaking` per caption
        line — with no matching close, ever.

        If those were treated as pairable, each would open an interval that only
        closes at the session boundary, the longest such interval would win every
        span, and every speaker after the first would VANISH from the timeline.
        That is worse than the bug this change fixes, and it would affect every
        multi-speaker Teams call rather than being an edge case.

        Leaving `source` untagged routes them to the point path, i.e. exactly
        their existing behaviour. Regression guard for a real defect found in
        review.
        """
        adapter, _, _ = make_adapter(tmp_path, pcm_bytes=bytes(32000))
        # Four speakers, one point-in-time claim each, no ENDs anywhere.
        adapter._session.speaker_events = [
            _untagged("Speaker A", "SPEAKER_START", 0),
            _untagged("Speaker B", "SPEAKER_START", 10_000),
            _untagged("Speaker C", "SPEAKER_START", 20_000),
            _untagged("Speaker D", "SPEAKER_START", 30_000),
        ]

        timeline = adapter.build_speaker_timeline().speaker_timeline

        # Every speaker survives, in order, as a point.
        assert [e.speaker_name for e in timeline] == [
            "Speaker A",
            "Speaker B",
            "Speaker C",
            "Speaker D",
        ]
        # And each still owns the span following its own claim.
        assert _attribute(timeline, 5.0) == "Speaker A"
        assert _attribute(timeline, 15.0) == "Speaker B"
        assert _attribute(timeline, 25.0) == "Speaker C"
        assert _attribute(timeline, 35.0) == "Speaker D"

    def test_audio_intervals_take_precedence_over_untagged_points(
        self, tmp_path: Path
    ) -> None:
        """Trusted intervals win outright; untrusted points are excluded, not merged.

        When any pairable audio interval exists, non-pairable claims — DOM/CSS
        events and untagged ones alike — are dropped rather than merged in as
        competing points. That exclusion IS the fix: a stray point surviving
        alongside intervals could become "the last event before a segment" and
        reintroduce the original misattribution.

        Untagged claims are therefore only used when there is nothing pairable at
        all, which is the Teams/Zoom case covered above.
        """
        adapter, _, _ = make_adapter(tmp_path, pcm_bytes=bytes(32000))
        adapter._session.speaker_events = [
            _audio("Speaker A", "SPEAKER_START", 0),
            _audio("Speaker A", "SPEAKER_END", 5_000),
            _untagged("Speaker B", "SPEAKER_START", 1_000),  # excluded
            _dom("Speaker C", "SPEAKER_START", 2_000),  # excluded
        ]

        timeline = adapter.build_speaker_timeline().speaker_timeline

        assert [e.speaker_name for e in timeline] == ["Speaker A"]
        assert _attribute(timeline, 2.0) == "Speaker A"

    def test_two_partially_overlapping_real_turns(self, tmp_path: Path) -> None:
        """Crosstalk between two genuine turns of different length.

        Not nested — they overlap at the edges, which is the shape the suite
        otherwise never covered. Each speaker must own the part of the span where
        they are the only one active.
        """
        adapter, _, _ = make_adapter(tmp_path, pcm_bytes=bytes(32000))
        adapter._session.speaker_events = [
            _audio("Speaker A", "SPEAKER_START", 0),
            _audio("Speaker A", "SPEAKER_END", 20_000),
            _audio("Speaker B", "SPEAKER_START", 15_000),  # cuts in at 15s
            _audio("Speaker B", "SPEAKER_END", 40_000),
        ]

        timeline = adapter.build_speaker_timeline().speaker_timeline

        assert _attribute(timeline, 5.0) == "Speaker A"  # A alone
        assert _attribute(timeline, 30.0) == "Speaker B"  # B alone, after A stops

    def test_untagged_events_fall_back_to_legacy_points(self, tmp_path: Path) -> None:
        """An older bot image publishes no `source`; must not crash or vanish."""
        adapter, _, _ = make_adapter(tmp_path, pcm_bytes=bytes(32000))
        adapter._session.speaker_events = [
            VexaSpeakerEvent(
                uid="conn-abc123def456",
                relative_ms=4_500,
                event_type="SPEAKER_START",
                participant_name="Alice Chen",
                meeting_id="42",
            ),
        ]

        timeline = adapter.build_speaker_timeline().speaker_timeline

        assert [e.speaker_name for e in timeline] == ["Alice Chen"]
        # Fixture is google_meet, so the Zoom-gated anchor does NOT fire — the sole
        # event keeps its true 4.5s onset. (Anchor behaviour is covered by the
        # Zoom-specific TestAnchorGate tests below.)
        assert timeline[0].relative_sec == pytest.approx(4.5)

    def test_dom_only_meeting_falls_back_rather_than_emitting_nothing(
        self, tmp_path: Path
    ) -> None:
        """Safety valve.

        An EMPTY timeline makes notetaker-worker bail ("Timeline has no speaker
        events") and the transcript reverts to raw SPEAKER_NN. A noisy timeline
        is strictly better than none, so DOM-only meetings keep legacy points.
        """
        adapter, _, _ = make_adapter(tmp_path, pcm_bytes=bytes(32000))
        adapter._session.speaker_events = [
            _dom("Alice Chen", "SPEAKER_START", 1_000),
            _dom("Alice Chen", "SPEAKER_END", 2_000),
            _dom("Bob Müller", "SPEAKER_START", 3_000),
        ]

        timeline = adapter.build_speaker_timeline().speaker_timeline

        assert len(timeline) == 2  # one per DOM SPEAKER_START
        assert {e.speaker_name for e in timeline} == {"Alice Chen", "Bob Müller"}

    def test_silence_gap_emits_no_event(self, tmp_path: Path) -> None:
        """Prosody discards silence; so must we."""
        adapter, _, _ = make_adapter(tmp_path, pcm_bytes=bytes(32000))
        adapter._session.speaker_events = [
            _audio("Alice Chen", "SPEAKER_START", 1_000),
            _audio("Alice Chen", "SPEAKER_END", 2_000),
            _audio("Bob Müller", "SPEAKER_START", 30_000),
            _audio("Bob Müller", "SPEAKER_END", 31_000),
        ]

        timeline = adapter.build_speaker_timeline().speaker_timeline

        # Mid-meeting silence is discarded — only two transitions, no event invented
        # for the 2-30s gap. Fixture is google_meet so the Zoom-gated anchor does not
        # fire; the earliest transition keeps its true 1.0s onset.
        assert [e.relative_sec for e in timeline] == [1.0, 30.0]
        # Inside the gap the previous transition remains in effect — matching
        # today's worker behaviour rather than inventing an "everyone silent".
        assert _attribute(timeline, 15.0) == "Alice Chen"

    def test_second_start_without_end_closes_the_previous_utterance(
        self, tmp_path: Path
    ) -> None:
        adapter, _, _ = make_adapter(tmp_path, pcm_bytes=bytes(32000))
        adapter._session.speaker_events = [
            _audio("Alice Chen", "SPEAKER_START", 1_000),
            _audio("Alice Chen", "SPEAKER_START", 5_000),  # no END between
            _audio("Alice Chen", "SPEAKER_END", 9_000),
            _audio("Bob Müller", "SPEAKER_START", 2_000),
            _audio("Bob Müller", "SPEAKER_END", 3_000),
        ]

        timeline = adapter.build_speaker_timeline().speaker_timeline

        # Alice's [1,5] and [5,9] both exist; Bob's 1s sits inside Alice's
        # 4s interval, so Alice stays dominant throughout and there is exactly
        # one transition.
        assert [e.speaker_name for e in timeline] == ["Alice Chen"]
        assert _attribute(timeline, 2.5) == "Alice Chen"


# ---------------------------------------------------------------------------
# VexaSessionAdapter.build_speaker_timeline — speaker_intervals (additive)
# ---------------------------------------------------------------------------


class TestSpeakerIntervals:
    """`speaker_intervals` carries the RAW paired audio intervals — overlap and
    all — alongside the collapsed `speaker_timeline`. It must never change
    `speaker_timeline`'s output; that invariant is the regression test that
    matters most here.
    """

    def test_overlapping_intervals_are_both_emitted_with_overlap_intact(
        self, tmp_path: Path
    ) -> None:
        """The whole reason the type exists: overlap must survive, unmerged."""
        adapter, _, _ = make_adapter(tmp_path, pcm_bytes=bytes(32000))
        adapter._session.speaker_events = [
            _audio("Speaker A", "SPEAKER_START", 0),
            _audio("Speaker A", "SPEAKER_END", 20_000),
            _audio("Speaker B", "SPEAKER_START", 15_000),  # overlaps A's tail
            _audio("Speaker B", "SPEAKER_END", 40_000),
        ]

        result = adapter.build_speaker_timeline()

        assert [
            (iv.speaker_name, iv.start_sec, iv.end_sec)
            for iv in result.speaker_intervals
        ] == [
            ("Speaker A", 0.0, 20.0),
            ("Speaker B", 15.0, 40.0),
        ]
        # The overlap itself: both cover [15, 20).
        a, b = result.speaker_intervals
        assert a.start_sec < b.start_sec < a.end_sec < b.end_sec

    def test_intervals_are_sorted_by_start_sec(self, tmp_path: Path) -> None:
        adapter, _, _ = make_adapter(tmp_path, pcm_bytes=bytes(32000))
        # Constructed out of chronological order to prove the output is sorted
        # rather than merely reflecting input order.
        adapter._session.speaker_events = [
            _audio("Speaker C", "SPEAKER_START", 20_000),
            _audio("Speaker C", "SPEAKER_END", 21_000),
            _audio("Speaker A", "SPEAKER_START", 0),
            _audio("Speaker A", "SPEAKER_END", 1_000),
            _audio("Speaker B", "SPEAKER_START", 10_000),
            _audio("Speaker B", "SPEAKER_END", 11_000),
        ]

        result = adapter.build_speaker_timeline()

        starts = [iv.start_sec for iv in result.speaker_intervals]
        assert starts == sorted(starts)
        assert [iv.speaker_name for iv in result.speaker_intervals] == [
            "Speaker A",
            "Speaker B",
            "Speaker C",
        ]

    def test_collapsed_speaker_timeline_is_byte_identical_to_pre_change_output(
        self, tmp_path: Path
    ) -> None:
        """Regression guard: emitting speaker_intervals must not alter
        `speaker_timeline` at all.

        Expected values below were captured by running this exact fixture
        against the adapter BEFORE this change (git stash of the
        `speaker_intervals=` addition), i.e. the collapse logic untouched.
        The fixture deliberately mixes overlap (A/B), a nested sub-threshold
        interval (C, 1s < MIN_DOMINANT_UTTERANCE_MS), an excluded DOM point
        (D), and an excluded untagged point (E) — the same shape exercised by
        `TestDominantSpeakerCollapse` and `TestSpeakerIntervals` elsewhere in
        this file.
        """
        adapter, _, _ = make_adapter(tmp_path, pcm_bytes=bytes(32000))
        adapter._session.speaker_events = [
            _audio("Speaker A", "SPEAKER_START", 0),
            _audio("Speaker A", "SPEAKER_END", 20_000),
            _audio("Speaker B", "SPEAKER_START", 15_000),
            _audio("Speaker B", "SPEAKER_END", 40_000),
            _audio("Speaker C", "SPEAKER_START", 5_000),
            _audio("Speaker C", "SPEAKER_END", 6_000),
            _dom("Speaker D", "SPEAKER_START", 1_000),
            _untagged("Speaker E", "SPEAKER_START", 2_000),
        ]

        result = adapter.build_speaker_timeline()

        origin_ms = int(SESSION_START.timestamp() * 1000)
        assert [
            (e.timestamp_ms, e.relative_sec, e.speaker_id, e.speaker_name)
            for e in result.speaker_timeline
        ] == [
            (origin_ms + 0, 0.0, "speaker_a", "Speaker A"),
            (origin_ms + 20_000, 20.0, "speaker_b", "Speaker B"),
        ]
        # And, additively, the raw intervals are all still there.
        assert [
            (iv.speaker_name, iv.start_sec, iv.end_sec)
            for iv in result.speaker_intervals
        ] == [
            ("Speaker A", 0.0, 20.0),
            ("Speaker C", 5.0, 6.0),
            ("Speaker B", 15.0, 40.0),
        ]

    def test_dom_sourced_events_are_not_turned_into_intervals(
        self, tmp_path: Path
    ) -> None:
        adapter, _, _ = make_adapter(tmp_path, pcm_bytes=bytes(32000))
        adapter._session.speaker_events = [
            _dom("Alice Chen", "SPEAKER_START", 1_000),
            _dom("Alice Chen", "SPEAKER_END", 2_000),
            _dom("Bob Müller", "SPEAKER_START", 3_000),
        ]

        result = adapter.build_speaker_timeline()

        # Safety-valve path: speaker_timeline still gets legacy points...
        assert len(result.speaker_timeline) == 2
        # ...but nothing was pairable, so no intervals.
        assert result.speaker_intervals == []

    def test_segment_derived_fallback_emits_no_intervals(self, tmp_path: Path) -> None:
        """No speaker_events at all -> segment-derived fallback -> no intervals."""
        adapter, _, _ = make_adapter(
            tmp_path, pcm_bytes=bytes(32000), with_speaker_events=False
        )

        result = adapter.build_speaker_timeline()

        assert len(result.speaker_timeline) == 2  # unchanged fallback behaviour
        assert result.speaker_intervals == []

    def test_unpaired_start_is_bounded_by_session_end(self, tmp_path: Path) -> None:
        """Mirrors the collapse's own handling of an unclosed interval."""
        adapter, _, _ = make_adapter(tmp_path, pcm_bytes=bytes(32000))
        adapter._session.speaker_events = [
            _audio("Alice Chen", "SPEAKER_START", 5_000),  # no END
        ]

        result = adapter.build_speaker_timeline()

        duration_sec = (SESSION_END - SESSION_START).total_seconds()
        assert len(result.speaker_intervals) == 1
        iv = result.speaker_intervals[0]
        assert iv.speaker_name == "Alice Chen"
        assert iv.start_sec == pytest.approx(5.0)
        assert iv.end_sec == pytest.approx(duration_sec)

    def test_second_start_without_end_produces_two_intervals(
        self, tmp_path: Path
    ) -> None:
        """The collapse closes a re-opened START at the new START's time; the
        same two intervals must appear in speaker_intervals, unmerged."""
        adapter, _, _ = make_adapter(tmp_path, pcm_bytes=bytes(32000))
        adapter._session.speaker_events = [
            _audio("Alice Chen", "SPEAKER_START", 1_000),
            _audio("Alice Chen", "SPEAKER_START", 5_000),  # no END between
            _audio("Alice Chen", "SPEAKER_END", 9_000),
        ]

        result = adapter.build_speaker_timeline()

        assert [(iv.start_sec, iv.end_sec) for iv in result.speaker_intervals] == [
            (1.0, 5.0),
            (5.0, 9.0),
        ]

    def test_untagged_events_yield_no_intervals(self, tmp_path: Path) -> None:
        """The Teams/Zoom point-only shape has nothing pairable."""
        adapter, _, _ = make_adapter(tmp_path, pcm_bytes=bytes(32000))
        adapter._session.speaker_events = [
            _untagged("Speaker A", "SPEAKER_START", 0),
            _untagged("Speaker B", "SPEAKER_START", 10_000),
        ]

        result = adapter.build_speaker_timeline()

        assert result.speaker_intervals == []

    def test_audio_intervals_present_excludes_untrusted_points_from_intervals(
        self, tmp_path: Path
    ) -> None:
        """When any pairable audio interval exists, untrusted points are
        dropped from the timeline (existing behaviour) and were never eligible
        for intervals in the first place."""
        adapter, _, _ = make_adapter(tmp_path, pcm_bytes=bytes(32000))
        adapter._session.speaker_events = [
            _audio("Speaker A", "SPEAKER_START", 0),
            _audio("Speaker A", "SPEAKER_END", 5_000),
            _untagged("Speaker B", "SPEAKER_START", 1_000),
            _dom("Speaker C", "SPEAKER_START", 2_000),
        ]

        result = adapter.build_speaker_timeline()

        assert [
            (iv.speaker_name, iv.start_sec, iv.end_sec)
            for iv in result.speaker_intervals
        ] == [("Speaker A", 0.0, 5.0)]

    def test_no_speaker_events_object_yields_no_intervals(self, tmp_path: Path) -> None:
        """Belt-and-braces: an empty speaker_events list never populates
        speaker_intervals — the `if session.speaker_events:` guard is never
        entered, so `paired_intervals` stays its initial `[]`."""
        adapter, _, _ = make_adapter(
            tmp_path, pcm_bytes=bytes(32000), with_speaker_events=False
        )
        adapter._session.segments = []  # also empty segments -> nothing at all

        result = adapter.build_speaker_timeline()

        assert result.speaker_timeline == []
        assert result.speaker_intervals == []


# ---------------------------------------------------------------------------
# VexaSessionAdapter.build_speaker_timeline — earliest-event anchor gate
# ---------------------------------------------------------------------------


class TestAnchorGate:
    """The earliest-event anchor (fixes leading SPEAKER_NN) is gated to
    Zoom AND >= 2 distinct named speakers:
      - Zoom-only keeps Meet/Teams byte-for-byte unchanged (meet-bot untouched).
      - >= 2 speakers prevents the live failure (2026-08-25, session c90bfaad…)
        where only one speaker resolved a name and anchoring it to t=0 funnelled
        the whole meeting — including the unnamed speaker's words — onto the one
        named speaker (everything clogged under the host).
    """

    def test_zoom_anchors_earliest_event_when_two_speakers(
        self, tmp_path: Path
    ) -> None:
        adapter, _, _ = make_adapter(tmp_path, pcm_bytes=bytes(32000))
        adapter._session.platform = "zoom"
        adapter._session.speaker_events = [
            _audio("Alice Chen", "SPEAKER_START", 2_900),
            _audio("Alice Chen", "SPEAKER_END", 6_000),
            _audio("Bob Müller", "SPEAKER_START", 10_000),
            _audio("Bob Müller", "SPEAKER_END", 12_000),
        ]

        timeline = adapter.build_speaker_timeline().speaker_timeline

        # Earliest transition pulled to t=0 with the first speaker's name...
        assert timeline[0].relative_sec == pytest.approx(0.0)
        assert timeline[0].speaker_name == "Alice Chen"
        # ...so a segment at 2.0s (before Alice's true 2.9s onset) maps to Alice.
        assert _attribute(timeline, 2.0) == "Alice Chen"
        # Only the earliest is anchored; later transitions keep true onsets.
        assert timeline[-1].speaker_name == "Bob Müller"
        assert timeline[-1].relative_sec == pytest.approx(10.0)

    def test_zoom_does_not_anchor_when_only_one_speaker(self, tmp_path: Path) -> None:
        adapter, _, _ = make_adapter(tmp_path, pcm_bytes=bytes(32000))
        adapter._session.platform = "zoom"
        adapter._session.speaker_events = [
            _audio("Alice Chen", "SPEAKER_START", 2_900),
            _audio("Alice Chen", "SPEAKER_END", 6_000),
        ]

        timeline = adapter.build_speaker_timeline().speaker_timeline

        assert [e.speaker_name for e in timeline] == ["Alice Chen"]
        # NOT anchored — keeps the true 2.9s onset, so a segment at 2.0s stays
        # unattributed (SPEAKER_NN) instead of being donated to Alice.
        assert timeline[0].relative_sec == pytest.approx(2.9)
        assert _attribute(timeline, 2.0) is None

    def test_non_zoom_never_anchors_even_with_two_speakers(
        self, tmp_path: Path
    ) -> None:
        adapter, _, _ = make_adapter(tmp_path, pcm_bytes=bytes(32000))
        adapter._session.platform = "google_meet"
        adapter._session.speaker_events = [
            _audio("Alice Chen", "SPEAKER_START", 2_900),
            _audio("Alice Chen", "SPEAKER_END", 6_000),
            _audio("Bob Müller", "SPEAKER_START", 10_000),
            _audio("Bob Müller", "SPEAKER_END", 12_000),
        ]

        timeline = adapter.build_speaker_timeline().speaker_timeline

        # Meet is untouched by the Zoom-gated anchor — earliest keeps its onset.
        assert timeline[0].relative_sec == pytest.approx(2.9)
        assert timeline[0].speaker_name == "Alice Chen"


# ---------------------------------------------------------------------------
# VexaSessionAdapter.build_participants
# ---------------------------------------------------------------------------


class TestBuildParticipants:
    def test_returns_participants_file_type(self, tmp_path: Path) -> None:
        adapter, _, _ = make_adapter(tmp_path, pcm_bytes=bytes(32000))
        result = adapter.build_participants()
        assert isinstance(result, ParticipantsFile)

    def test_meeting_id_matches_session(self, tmp_path: Path) -> None:
        adapter, _, _ = make_adapter(tmp_path, pcm_bytes=bytes(32000))
        result = adapter.build_participants()
        assert result.meeting_id == "42"

    def test_platform_is_mapped_to_canonical_form(self, tmp_path: Path) -> None:
        adapter, _, _ = make_adapter(tmp_path, pcm_bytes=bytes(32000))
        result = adapter.build_participants()
        assert result.platform == "meet"

    def test_host_id_is_host_zero(self, tmp_path: Path) -> None:
        adapter, _, _ = make_adapter(tmp_path, pcm_bytes=bytes(32000))
        result = adapter.build_participants()
        assert result.host.id == "host-0"

    def test_host_name_is_local_part_of_organizer_email(self, tmp_path: Path) -> None:
        adapter, _, _ = make_adapter(tmp_path, pcm_bytes=bytes(32000))
        result = adapter.build_participants()
        assert result.host.name == "host"

    def test_participants_list_contains_unique_speakers(self, tmp_path: Path) -> None:
        adapter, _, _ = make_adapter(tmp_path, pcm_bytes=bytes(32000))
        result = adapter.build_participants()
        participant_names = {p.name for p in result.participants}
        assert "Alice Chen" in participant_names
        assert "Bob Müller" in participant_names

    def test_participant_id_is_slug_of_name(self, tmp_path: Path) -> None:
        adapter, _, _ = make_adapter(tmp_path, pcm_bytes=bytes(32000))
        result = adapter.build_participants()
        alice = next(p for p in result.participants if p.name == "Alice Chen")
        assert alice.id == "alice_chen"

    def test_no_duplicate_participants(self, tmp_path: Path) -> None:
        # Alice appears in segments AND speaker_events — should deduplicate
        adapter, _, _ = make_adapter(
            tmp_path, pcm_bytes=bytes(32000), with_speaker_events=True
        )
        result = adapter.build_participants()
        ids = [p.id for p in result.participants]
        assert len(ids) == len(set(ids))


# ---------------------------------------------------------------------------
# VexaSessionAdapter.build_metadata
# ---------------------------------------------------------------------------


class TestBuildMetadata:
    def test_returns_metadata_file_type(self, tmp_path: Path) -> None:
        adapter, _, _ = make_adapter(tmp_path, pcm_bytes=bytes(32000))
        result = adapter.build_metadata()
        assert isinstance(result, MetadataFile)

    def test_actual_start_at_equals_session_start_ts(self, tmp_path: Path) -> None:
        adapter, _, _ = make_adapter(tmp_path, pcm_bytes=bytes(32000))
        result = adapter.build_metadata()
        assert result.actual_start_at == SESSION_START

    def test_bot_left_reason_default_is_host_ended(self, tmp_path: Path) -> None:
        adapter, _, _ = make_adapter(tmp_path, pcm_bytes=bytes(32000))
        result = adapter.build_metadata()
        assert result.bot_left_reason == "host_ended"

    def test_bot_left_reason_is_passed_through(self, tmp_path: Path) -> None:
        adapter, _, _ = make_adapter(tmp_path, pcm_bytes=bytes(32000))
        result = adapter.build_metadata(bot_left_reason="hard_deadline")
        assert result.bot_left_reason == "hard_deadline"

    def test_consent_state_matches_job(self, tmp_path: Path) -> None:
        adapter, _, _ = make_adapter(tmp_path, pcm_bytes=bytes(32000))
        result = adapter.build_metadata()
        assert result.consent_state == "explicit"

    def test_meeting_id_matches_session(self, tmp_path: Path) -> None:
        adapter, _, _ = make_adapter(tmp_path, pcm_bytes=bytes(32000))
        result = adapter.build_metadata()
        assert result.meeting_id == "42"

    def test_saw_remote_camera_true_maps_to_video(self, tmp_path: Path) -> None:
        adapter, _, _ = make_adapter(
            tmp_path, pcm_bytes=bytes(32000), saw_remote_camera=True
        )
        result = adapter.build_metadata()
        assert result.media_kind == "video"

    def test_saw_remote_camera_false_maps_to_audio(self, tmp_path: Path) -> None:
        adapter, _, _ = make_adapter(
            tmp_path, pcm_bytes=bytes(32000), saw_remote_camera=False
        )
        result = adapter.build_metadata()
        assert result.media_kind == "audio"

    def test_saw_remote_camera_none_maps_to_none(self, tmp_path: Path) -> None:
        adapter, _, _ = make_adapter(
            tmp_path, pcm_bytes=bytes(32000), saw_remote_camera=None
        )
        result = adapter.build_metadata()
        assert result.media_kind is None


# ---------------------------------------------------------------------------
# VexaSessionAdapter.run (integration of the full pipeline)
# ---------------------------------------------------------------------------


class TestAdapterRun:
    @pytest.mark.asyncio
    async def test_run_calls_s3_writer_write_all_exactly_once(
        self, tmp_path: Path
    ) -> None:
        adapter, mock_s3, _ = make_adapter(tmp_path, pcm_bytes=bytes(32000))
        await adapter.run()
        mock_s3.write_all.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_passes_correct_s3_key_to_write_all(self, tmp_path: Path) -> None:
        adapter, mock_s3, _ = make_adapter(tmp_path, pcm_bytes=bytes(32000))
        await adapter.run()
        args, _ = mock_s3.write_all.call_args
        assert args[0] == "recordings/meet_42_test-job-ulid/"

    @pytest.mark.asyncio
    async def test_run_calls_notetaker_client_post_process_exactly_once(
        self, tmp_path: Path
    ) -> None:
        adapter, _, mock_client = make_adapter(tmp_path, pcm_bytes=bytes(32000))
        await adapter.run()
        mock_client.post_process.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_run_passes_job_to_post_process(self, tmp_path: Path) -> None:
        adapter, _, mock_client = make_adapter(tmp_path, pcm_bytes=bytes(32000))
        job = make_test_job()
        await adapter.run()
        called_job = mock_client.post_process.call_args.args[0]
        assert called_job.job_id == job.job_id
        assert called_job.meeting_id == job.meeting_id


# ---------------------------------------------------------------------------
# S3Writer.write_all
# ---------------------------------------------------------------------------


class TestS3WriterWriteAll:
    def test_write_all_calls_write_audio_once(self) -> None:
        mock_s3_client = MagicMock()
        writer = S3Writer(s3_client=mock_s3_client)
        writer.write_audio = MagicMock()
        writer.write_speaker_timeline = MagicMock()
        writer.write_participants = MagicMock()
        writer.write_metadata = MagicMock()

        wav = b"RIFF\x00\x00\x00\x00WAVEfmt "
        timeline = MagicMock(spec=SpeakerTimelineFile)
        participants = MagicMock(spec=ParticipantsFile)
        metadata = MagicMock(spec=MetadataFile)

        writer.write_all("some/key/", wav, timeline, participants, metadata)

        writer.write_audio.assert_called_once_with("some/key/", wav)

    def test_write_all_calls_write_speaker_timeline_once(self) -> None:
        mock_s3_client = MagicMock()
        writer = S3Writer(s3_client=mock_s3_client)
        writer.write_audio = MagicMock()
        writer.write_speaker_timeline = MagicMock()
        writer.write_participants = MagicMock()
        writer.write_metadata = MagicMock()

        wav = b"RIFF\x00\x00\x00\x00WAVEfmt "
        timeline = MagicMock(spec=SpeakerTimelineFile)
        participants = MagicMock(spec=ParticipantsFile)
        metadata = MagicMock(spec=MetadataFile)

        writer.write_all("some/key/", wav, timeline, participants, metadata)

        writer.write_speaker_timeline.assert_called_once_with("some/key/", timeline)

    def test_write_all_calls_write_participants_once(self) -> None:
        mock_s3_client = MagicMock()
        writer = S3Writer(s3_client=mock_s3_client)
        writer.write_audio = MagicMock()
        writer.write_speaker_timeline = MagicMock()
        writer.write_participants = MagicMock()
        writer.write_metadata = MagicMock()

        wav = b"RIFF\x00\x00\x00\x00WAVEfmt "
        timeline = MagicMock(spec=SpeakerTimelineFile)
        participants = MagicMock(spec=ParticipantsFile)
        metadata = MagicMock(spec=MetadataFile)

        writer.write_all("some/key/", wav, timeline, participants, metadata)

        writer.write_participants.assert_called_once_with("some/key/", participants)

    def test_write_all_calls_write_metadata_once(self) -> None:
        mock_s3_client = MagicMock()
        writer = S3Writer(s3_client=mock_s3_client)
        writer.write_audio = MagicMock()
        writer.write_speaker_timeline = MagicMock()
        writer.write_participants = MagicMock()
        writer.write_metadata = MagicMock()

        wav = b"RIFF\x00\x00\x00\x00WAVEfmt "
        timeline = MagicMock(spec=SpeakerTimelineFile)
        participants = MagicMock(spec=ParticipantsFile)
        metadata = MagicMock(spec=MetadataFile)

        writer.write_all("some/key/", wav, timeline, participants, metadata)

        writer.write_metadata.assert_called_once_with("some/key/", metadata)


# ---------------------------------------------------------------------------
# NoteTakerClientWrapper.post_process
# ---------------------------------------------------------------------------


class TestNoteTakerClientWrapper:
    @pytest.mark.asyncio
    async def test_post_process_calls_underlying_client_once(self) -> None:
        mock_inner = MagicMock()
        mock_inner.post_process = AsyncMock(return_value={"status": "queued"})
        wrapper = NoteTakerClientWrapper(client=mock_inner)
        job = make_test_job()
        await wrapper.post_process(job)
        mock_inner.post_process.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_post_process_builds_request_with_correct_meeting_id(self) -> None:
        mock_inner = MagicMock()
        mock_inner.post_process = AsyncMock(return_value={})
        wrapper = NoteTakerClientWrapper(client=mock_inner)
        job = make_test_job()
        await wrapper.post_process(job)
        req: ProcessRequest = mock_inner.post_process.call_args.args[0]
        assert req.meeting_id == job.meeting_id

    @pytest.mark.asyncio
    async def test_post_process_builds_request_with_correct_platform(self) -> None:
        mock_inner = MagicMock()
        mock_inner.post_process = AsyncMock(return_value={})
        wrapper = NoteTakerClientWrapper(client=mock_inner)
        job = make_test_job()
        await wrapper.post_process(job)
        req: ProcessRequest = mock_inner.post_process.call_args.args[0]
        assert req.platform == job.platform

    @pytest.mark.asyncio
    async def test_post_process_builds_request_with_correct_idempotency_key(
        self,
    ) -> None:
        mock_inner = MagicMock()
        mock_inner.post_process = AsyncMock(return_value={})
        wrapper = NoteTakerClientWrapper(client=mock_inner)
        job = make_test_job()
        await wrapper.post_process(job)
        _, kwargs = mock_inner.post_process.call_args
        assert kwargs.get("idempotency_key") == job.job_id


# ── Tests for run_from_redis() ─────────────────────────────────────────────

import json as _json  # noqa: E402
import os as _os  # noqa: E402
from pathlib import Path as _Path  # noqa: E402
from unittest.mock import (
    AsyncMock as _AsyncMock,
    MagicMock as _MagicMock,
    patch as _patch,
)  # noqa: E402

import fakeredis as _fakeredis  # noqa: E402

_FIXTURE = _json.loads(
    (_Path(__file__).parent / "fixtures/vexa_v0.10.4_meet_sample.json").read_text()
)
_SESSION_UID = "conn-abc123def456"
_JOB = make_test_job()


def _seed_fake_redis(r: _fakeredis.FakeRedis) -> None:
    for entry in _FIXTURE["redis_messages"]["transcription_segments"]:
        r.xadd("transcription_segments", {"payload": _json.dumps(entry["payload"])})
    for entry in _FIXTURE["redis_messages"]["speaker_events_relative"]:
        r.xadd("speaker_events_relative", entry["fields"])


@pytest.mark.asyncio
async def test_run_from_redis_calls_adapter_run(tmp_path: _Path) -> None:
    """run_from_redis drains streams, assembles session, calls VexaSessionAdapter.run()."""
    audio_path = tmp_path / "audio.raw"
    audio_path.write_bytes(b"\x00\x01" * 1600)

    fake_r = _fakeredis.FakeRedis(decode_responses=True)
    _seed_fake_redis(fake_r)

    mock_s3 = _MagicMock()
    mock_client = _AsyncMock()

    from aw_integration.adapter import run_from_redis

    with _patch("aw_integration.adapter.redis.Redis.from_url", return_value=fake_r):
        await run_from_redis(
            session_uid=_SESSION_UID,
            job=_JOB,
            audio_raw_path=audio_path,
            session_end_wall_clock=SESSION_END,
            redis_url="redis://localhost:6379",
            bot_left_reason="last_participant",
            s3_writer=mock_s3,
            notetaker_client=mock_client,
        )

    mock_s3.write_all.assert_called_once()
    mock_client.post_process.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_from_redis_filters_by_session_uid(tmp_path: _Path) -> None:
    """Entries with a different uid are ignored."""
    audio_path = tmp_path / "audio.raw"
    audio_path.write_bytes(b"\x00\x01" * 1600)

    fake_r = _fakeredis.FakeRedis(decode_responses=True)
    fake_r.xadd(
        "transcription_segments",
        {
            "payload": _json.dumps(
                {
                    "type": "transcription",
                    "uid": "OTHER_SESSION",
                    "meeting_id": "99",
                    "platform": "google_meet",
                    "segments": [
                        {
                            "start": 0.0,
                            "end": 1.0,
                            "text": "noise",
                            "language": "en",
                            "completed": True,
                            "speaker": "Eve",
                            "segment_id": "x",
                            "absolute_start_time": "2024-01-15T09:00:00Z",
                            "absolute_end_time": "2024-01-15T09:00:01Z",
                        }
                    ],
                }
            )
        },
    )

    mock_s3 = _MagicMock()
    mock_client = _AsyncMock()

    from aw_integration.adapter import run_from_redis

    with _patch("aw_integration.adapter.redis.Redis.from_url", return_value=fake_r):
        await run_from_redis(
            session_uid=_SESSION_UID,
            job=_JOB,
            audio_raw_path=audio_path,
            session_end_wall_clock=SESSION_END,
            redis_url="redis://localhost:6379",
            bot_left_reason="host_ended",
            s3_writer=mock_s3,
            notetaker_client=mock_client,
        )

    mock_s3.write_all.assert_called_once()
    mock_client.post_process.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_from_redis_parses_the_source_field_end_to_end(
    tmp_path: _Path,
) -> None:
    """Guards the dict-parsing of `source`, which the collapse tests skip.

    Every collapse test constructs `VexaSpeakerEvent(source=...)` directly, so none
    of them exercise `run_from_redis`'s `fields.get("source")` read. A regression
    there — e.g. `fields["source"]` instead of `.get()` — would raise KeyError,
    which the surrounding `except (KeyError, ValueError)` swallows, SILENTLY
    dropping every event and reverting transcripts to SPEAKER_NN with no error
    anywhere. This asserts tagged events survive the parse and reach the timeline
    as a collapsed interval, and that untagged ones still survive as points.
    """
    audio_path = tmp_path / "audio.raw"
    audio_path.write_bytes(b"\x00\x01" * 1600)

    fake_r = _fakeredis.FakeRedis(decode_responses=True)
    # A paired, audio-tagged utterance...
    for fields in (
        {
            "uid": _SESSION_UID,
            "relative_client_timestamp_ms": "2000",
            "event_type": "SPEAKER_START",
            "participant_name": "Speaker A",
            "meeting_id": "42",
            "source": "audio",
        },
        {
            "uid": _SESSION_UID,
            "relative_client_timestamp_ms": "9000",
            "event_type": "SPEAKER_END",
            "participant_name": "Speaker A",
            "meeting_id": "42",
            "source": "audio",
        },
        # ...and an untagged claim, as Teams/Zoom and older bot images emit.
        {
            "uid": _SESSION_UID,
            "relative_client_timestamp_ms": "20000",
            "event_type": "SPEAKER_START",
            "participant_name": "Speaker B",
            "meeting_id": "42",
        },
    ):
        fake_r.xadd("speaker_events_relative", fields)

    captured: dict[str, object] = {}

    def _capture_write_all(
        s3_key, wav, timeline, participants, metadata
    ):  # noqa: ANN001
        captured["timeline"] = timeline

    mock_s3 = _MagicMock()
    mock_s3.write_all.side_effect = _capture_write_all
    mock_client = _AsyncMock()

    from aw_integration.adapter import run_from_redis

    with _patch("aw_integration.adapter.redis.Redis.from_url", return_value=fake_r):
        await run_from_redis(
            session_uid=_SESSION_UID,
            job=_JOB,
            audio_raw_path=audio_path,
            session_end_wall_clock=SESSION_END,
            redis_url="redis://localhost:6379",
            bot_left_reason="host_ended",
            s3_writer=mock_s3,
            notetaker_client=mock_client,
        )

    timeline = captured["timeline"].speaker_timeline  # type: ignore[union-attr]
    names = [e.speaker_name for e in timeline]

    # `source` survived the parse and licensed interval-building: the tagged pair
    # collapsed into a Speaker A interval, so the UNTAGGED Speaker B claim was
    # excluded (the safety valve only fires when interval-building yields nothing,
    # and it did not). Had `source` been dropped, Speaker A would have fallen to the
    # safety valve too and Speaker B would appear as a point. Asserted on membership
    # rather than onset — this guards the source parse regardless of the anchor.
    assert "Speaker A" in names
    assert "Speaker B" not in names


# ---------------------------------------------------------------------------
# run_from_redis: `media_state` message -> MetadataFile.media_kind
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_from_redis_media_state_true_maps_to_video(
    tmp_path: _Path,
) -> None:
    fake_r = _fakeredis.FakeRedis(decode_responses=True)
    fake_r.xadd(
        "transcription_segments",
        {
            "payload": _json.dumps(
                {
                    "type": "media_state",
                    "token": "jwt-irrelevant",
                    "uid": _SESSION_UID,
                    "meeting_id": "42",
                    "sawRemoteCamera": True,
                }
            )
        },
    )

    audio_path = tmp_path / "audio.raw"
    audio_path.write_bytes(b"\x00\x01" * 1600)
    captured: dict[str, object] = {}

    def _capture_write_all(
        s3_key, wav, timeline, participants, metadata
    ):  # noqa: ANN001
        captured["metadata"] = metadata

    mock_s3 = _MagicMock()
    mock_s3.write_all.side_effect = _capture_write_all
    mock_client = _AsyncMock()

    from aw_integration.adapter import run_from_redis

    with _patch("aw_integration.adapter.redis.Redis.from_url", return_value=fake_r):
        await run_from_redis(
            session_uid=_SESSION_UID,
            job=_JOB,
            audio_raw_path=audio_path,
            session_end_wall_clock=SESSION_END,
            redis_url="redis://localhost:6379",
            bot_left_reason="host_ended",
            s3_writer=mock_s3,
            notetaker_client=mock_client,
        )

    metadata = captured["metadata"]
    assert metadata.media_kind == "video"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_run_from_redis_media_state_false_maps_to_audio(
    tmp_path: _Path,
) -> None:
    fake_r = _fakeredis.FakeRedis(decode_responses=True)
    fake_r.xadd(
        "transcription_segments",
        {
            "payload": _json.dumps(
                {
                    "type": "media_state",
                    "token": "jwt-irrelevant",
                    "uid": _SESSION_UID,
                    "meeting_id": "42",
                    "sawRemoteCamera": False,
                }
            )
        },
    )

    audio_path = tmp_path / "audio.raw"
    audio_path.write_bytes(b"\x00\x01" * 1600)
    captured: dict[str, object] = {}

    def _capture_write_all(
        s3_key, wav, timeline, participants, metadata
    ):  # noqa: ANN001
        captured["metadata"] = metadata

    mock_s3 = _MagicMock()
    mock_s3.write_all.side_effect = _capture_write_all
    mock_client = _AsyncMock()

    from aw_integration.adapter import run_from_redis

    with _patch("aw_integration.adapter.redis.Redis.from_url", return_value=fake_r):
        await run_from_redis(
            session_uid=_SESSION_UID,
            job=_JOB,
            audio_raw_path=audio_path,
            session_end_wall_clock=SESSION_END,
            redis_url="redis://localhost:6379",
            bot_left_reason="host_ended",
            s3_writer=mock_s3,
            notetaker_client=mock_client,
        )

    metadata = captured["metadata"]
    assert metadata.media_kind == "audio"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_run_from_redis_media_state_absent_maps_to_none(
    tmp_path: _Path,
) -> None:
    """No `media_state` message at all (hard-deadline / explicit-removal exit
    paths never send one) must NOT be coerced to "audio"."""
    fake_r = _fakeredis.FakeRedis(decode_responses=True)
    _seed_fake_redis(fake_r)  # the base fixture has no media_state entry

    audio_path = tmp_path / "audio.raw"
    audio_path.write_bytes(b"\x00\x01" * 1600)
    captured: dict[str, object] = {}

    def _capture_write_all(
        s3_key, wav, timeline, participants, metadata
    ):  # noqa: ANN001
        captured["metadata"] = metadata

    mock_s3 = _MagicMock()
    mock_s3.write_all.side_effect = _capture_write_all
    mock_client = _AsyncMock()

    from aw_integration.adapter import run_from_redis

    with _patch("aw_integration.adapter.redis.Redis.from_url", return_value=fake_r):
        await run_from_redis(
            session_uid=_SESSION_UID,
            job=_JOB,
            audio_raw_path=audio_path,
            session_end_wall_clock=SESSION_END,
            redis_url="redis://localhost:6379",
            bot_left_reason="host_ended",
            s3_writer=mock_s3,
            notetaker_client=mock_client,
        )

    metadata = captured["metadata"]
    assert metadata.media_kind is None  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_run_from_redis_media_state_malformed_does_not_raise(
    tmp_path: _Path,
) -> None:
    """A non-boolean `sawRemoteCamera` (producer bug, truncated payload, etc.)
    must degrade to `None` rather than raising or being coerced to "audio"."""
    fake_r = _fakeredis.FakeRedis(decode_responses=True)
    fake_r.xadd(
        "transcription_segments",
        {
            "payload": _json.dumps(
                {
                    "type": "media_state",
                    "token": "jwt-irrelevant",
                    "uid": _SESSION_UID,
                    "meeting_id": "42",
                    "sawRemoteCamera": "not-a-bool",
                }
            )
        },
    )

    audio_path = tmp_path / "audio.raw"
    audio_path.write_bytes(b"\x00\x01" * 1600)
    captured: dict[str, object] = {}

    def _capture_write_all(
        s3_key, wav, timeline, participants, metadata
    ):  # noqa: ANN001
        captured["metadata"] = metadata

    mock_s3 = _MagicMock()
    mock_s3.write_all.side_effect = _capture_write_all
    mock_client = _AsyncMock()

    from aw_integration.adapter import run_from_redis

    with _patch("aw_integration.adapter.redis.Redis.from_url", return_value=fake_r):
        await run_from_redis(
            session_uid=_SESSION_UID,
            job=_JOB,
            audio_raw_path=audio_path,
            session_end_wall_clock=SESSION_END,
            redis_url="redis://localhost:6379",
            bot_left_reason="host_ended",
            s3_writer=mock_s3,
            notetaker_client=mock_client,
        )

    metadata = captured["metadata"]
    assert metadata.media_kind is None  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_run_from_redis_media_state_ignores_other_session_uid(
    tmp_path: _Path,
) -> None:
    """A `media_state` message for a different session must not leak in."""
    fake_r = _fakeredis.FakeRedis(decode_responses=True)
    fake_r.xadd(
        "transcription_segments",
        {
            "payload": _json.dumps(
                {
                    "type": "media_state",
                    "token": "jwt-irrelevant",
                    "uid": "OTHER_SESSION",
                    "meeting_id": "99",
                    "sawRemoteCamera": True,
                }
            )
        },
    )

    audio_path = tmp_path / "audio.raw"
    audio_path.write_bytes(b"\x00\x01" * 1600)
    captured: dict[str, object] = {}

    def _capture_write_all(
        s3_key, wav, timeline, participants, metadata
    ):  # noqa: ANN001
        captured["metadata"] = metadata

    mock_s3 = _MagicMock()
    mock_s3.write_all.side_effect = _capture_write_all
    mock_client = _AsyncMock()

    from aw_integration.adapter import run_from_redis

    with _patch("aw_integration.adapter.redis.Redis.from_url", return_value=fake_r):
        await run_from_redis(
            session_uid=_SESSION_UID,
            job=_JOB,
            audio_raw_path=audio_path,
            session_end_wall_clock=SESSION_END,
            redis_url="redis://localhost:6379",
            bot_left_reason="host_ended",
            s3_writer=mock_s3,
            notetaker_client=mock_client,
        )

    metadata = captured["metadata"]
    assert metadata.media_kind is None  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Zoom platform mapping (§13.8 zoom-bot) — _PLATFORM_MAP["zoom"] = "zoom"
# ---------------------------------------------------------------------------


class TestZoomPlatformMapping:
    def test_platform_map_has_zoom_identity_entry(self) -> None:
        from aw_integration.adapter import _PLATFORM_MAP

        assert _PLATFORM_MAP["zoom"] == "zoom"
        # Meet's remap is unchanged by the addition.
        assert _PLATFORM_MAP["google_meet"] == "meet"

    def test_zoom_session_reports_zoom_platform_in_artifacts(
        self, tmp_path: Path
    ) -> None:
        raw_audio = tmp_path / "audio.raw"
        raw_audio.write_bytes(bytes(32000))
        session = VexaSession(
            uid="conn-zoom-1",
            platform="zoom",
            meeting_id="99",
            session_start_ts=SESSION_START,
            session_end_ts=SESSION_END,
            segments=[],
            speaker_events=[],
        )
        adapter = VexaSessionAdapter(
            session=session,
            job=make_test_job(),
            audio_raw_path=raw_audio,
            s3_writer=MagicMock(spec=S3Writer),
            notetaker_client=MagicMock(spec=NoteTakerClientWrapper),
        )
        assert adapter.build_speaker_timeline().platform == "zoom"
        assert adapter.build_participants().platform == "zoom"
        assert adapter.build_metadata().platform == "zoom"


@pytest.mark.asyncio
async def test_run_from_redis_reads_newest_not_oldest_when_stream_exceeds_count(
    tmp_path: _Path,
) -> None:
    """A session at the END of an over-long global stream must still be read.

    This is the regression test for the 2026-08-27 incident. `transcription_segments`
    and `speaker_events_relative` are global streams shared by every bot ever run,
    and nothing trims them; `speaker_events_relative` measured 64,135 entries. The
    original code read them with `xrange(count=50000)`, which returns the OLDEST N
    entries — so once a stream passed 50,000, the current session's events fell
    outside the window, zero events were read, `speaker_timeline.json` was written
    empty, and every transcript silently reverted to SPEAKER_00/01/02. No exception,
    no log line, no failing test.

    `_STREAM_READ_COUNT` is monkeypatched small so the shape can be exercised
    without seeding 50,000 entries. Under `xrange` this test fails; the assertions
    below pin the ordering too, so a well-meaning revert to xrange cannot pass.
    """
    audio_path = tmp_path / "audio.raw"
    audio_path.write_bytes(b"\x00\x01" * 1600)

    fake_r = _fakeredis.FakeRedis(decode_responses=True)

    # OLDER traffic from OTHER sessions — the entries that would fill the window.
    for i in range(12):
        fake_r.xadd(
            "speaker_events_relative",
            {
                "uid": f"someone-elses-session-{i}",
                "relative_client_timestamp_ms": str(1000 + i),
                "event_type": "SPEAKER_START",
                "participant_name": "Somebody Else",
                "meeting_id": "999",
                "source": "audio",
            },
        )

    # THEN this session's events, newest in the stream — as in production, where
    # the bridge writes them seconds before run_from_redis reads.
    for ms, etype, name in (
        (1000, "SPEAKER_START", "Alice"),
        (4000, "SPEAKER_END", "Alice"),
        (4200, "SPEAKER_START", "Bob"),
        (7000, "SPEAKER_END", "Bob"),
    ):
        fake_r.xadd(
            "speaker_events_relative",
            {
                "uid": _SESSION_UID,
                "relative_client_timestamp_ms": str(ms),
                "event_type": etype,
                "participant_name": name,
                "meeting_id": "123",
                "source": "audio",
            },
        )

    import aw_integration.adapter as _adapter_mod
    from aw_integration.adapter import run_from_redis

    captured: dict[str, object] = {}
    real_init = _adapter_mod.VexaSessionAdapter.__init__

    def _spy_init(self: object, *args: object, **kwargs: object) -> None:
        real_init(self, *args, **kwargs)  # type: ignore[arg-type]
        captured["session"] = kwargs.get("session")

    with (
        _patch("aw_integration.adapter.redis.Redis.from_url", return_value=fake_r),
        _patch("aw_integration.adapter._STREAM_READ_COUNT", 5),
        _patch.object(_adapter_mod.VexaSessionAdapter, "__init__", _spy_init),
        _patch.object(_adapter_mod.VexaSessionAdapter, "run", _AsyncMock()),
    ):
        await run_from_redis(
            session_uid=_SESSION_UID,
            job=_JOB,
            audio_raw_path=audio_path,
            session_end_wall_clock=SESSION_END,
            redis_url="redis://localhost:6379",
            bot_left_reason="last_participant",
            s3_writer=_MagicMock(),
            notetaker_client=_AsyncMock(),
        )

    session = captured["session"]
    assert session is not None
    events = session.speaker_events  # type: ignore[union-attr]

    # The bug: this was 0, and nothing anywhere reported it.
    assert len(events) == 4, (
        f"expected this session's 4 events, got {len(events)} — the read window is "
        "anchored to the OLDEST entries again, so a long global stream hides the "
        "current session and every transcript silently becomes SPEAKER_NN"
    )
    assert all(e.uid == _SESSION_UID for e in events)
    # Ascending insertion order must survive the newest-first read; the dominant
    # collapse and the interval pairing both depend on it.
    assert [e.relative_ms for e in events] == [1000, 4000, 4200, 7000]
    assert [e.participant_name for e in events] == ["Alice", "Alice", "Bob", "Bob"]


@pytest.mark.asyncio
async def test_run_from_redis_reads_newest_segments_and_keeps_first_last_wins_order(
    tmp_path: _Path,
) -> None:
    """The `transcription_segments` half of the newest-first read, and its ordering.

    Companion to the speaker-event test above. That one left this half completely
    uncovered: reverting only the `transcription_segments` read (and deleting its
    `.reverse()`) kept the whole suite green, because nothing padded that stream.

    This half fails worse than missing speaker names. An empty `segments` list
    also means `session_start_ts` falls through to
    `session_end_wall_clock - expected_duration_min` — a FABRICATED recording
    origin that shifts every timestamp in the artifact.

    The `.reverse()` is asserted directly, because the two consumers of `raw_ts`
    are order-dependent in OPPOSITE directions: `session_start_ts` is first-wins
    (guarded by `is None`), `saw_remote_camera` is last-wins (unguarded). Drop the
    reverse and both silently invert with nothing else failing.
    """
    audio_path = tmp_path / "audio.raw"
    audio_path.write_bytes(b"\x00\x01" * 1600)

    fake_r = _fakeredis.FakeRedis(decode_responses=True)

    # OLDER traffic from OTHER sessions, enough to fill a small read window.
    for i in range(12):
        fake_r.xadd(
            "transcription_segments",
            {
                "payload": _json.dumps(
                    {
                        "uid": f"someone-elses-session-{i}",
                        "type": "transcription",
                        "segments": [
                            {
                                "speaker": "Somebody Else",
                                "text": "not ours",
                                "start": 0.0,
                                "end": 1.0,
                            }
                        ],
                    }
                )
            },
        )

    # THEN this session's traffic, newest in the stream.
    real_origin = "2026-08-27T11:46:20.733000+00:00"
    for payload in (
        {"uid": _SESSION_UID, "type": "session_start", "start_timestamp": real_origin},
        # Two media_state messages: LAST one must win.
        {"uid": _SESSION_UID, "type": "media_state", "sawRemoteCamera": False},
        {"uid": _SESSION_UID, "type": "media_state", "sawRemoteCamera": True},
        {
            "uid": _SESSION_UID,
            "type": "transcription",
            "segments": [
                {"speaker": "Alice", "text": "ours one", "start": 0.5, "end": 2.0},
                {"speaker": "Bob", "text": "ours two", "start": 2.1, "end": 4.0},
            ],
        },
    ):
        fake_r.xadd("transcription_segments", {"payload": _json.dumps(payload)})

    import aw_integration.adapter as _adapter_mod
    from aw_integration.adapter import run_from_redis

    captured: dict[str, object] = {}
    real_init = _adapter_mod.VexaSessionAdapter.__init__

    def _spy_init(self: object, *args: object, **kwargs: object) -> None:
        real_init(self, *args, **kwargs)  # type: ignore[arg-type]
        captured["session"] = kwargs.get("session")

    with (
        _patch("aw_integration.adapter.redis.Redis.from_url", return_value=fake_r),
        _patch("aw_integration.adapter._STREAM_READ_COUNT", 5),
        _patch.object(_adapter_mod.VexaSessionAdapter, "__init__", _spy_init),
        _patch.object(_adapter_mod.VexaSessionAdapter, "run", _AsyncMock()),
    ):
        await run_from_redis(
            session_uid=_SESSION_UID,
            job=_JOB,
            audio_raw_path=audio_path,
            session_end_wall_clock=SESSION_END,
            redis_url="redis://localhost:6379",
            bot_left_reason="last_participant",
            s3_writer=_MagicMock(),
            notetaker_client=_AsyncMock(),
        )

    session = captured["session"]
    assert session is not None

    # The bug: this was 0, and the transcript came out empty.
    segs = session.segments  # type: ignore[union-attr]
    assert len(segs) == 2, (
        f"expected this session's 2 segments, got {len(segs)} — the read window is "
        "anchored to the OLDEST entries again, so a long global stream hides the "
        "current session's transcript entirely"
    )
    assert [s.text for s in segs] == ["ours one", "ours two"]

    # The real origin must be used, NOT the duration-guess fallback.
    fabricated = SESSION_END - _timedelta(minutes=_JOB.expected_duration_min)
    start_ts = session.session_start_ts  # type: ignore[union-attr]
    assert start_ts == datetime.fromisoformat(real_origin), (
        f"session_start_ts is {start_ts}; the published session_start was lost and "
        "the origin was fabricated from expected_duration_min"
    )
    assert start_ts != fabricated

    # last-wins on media_state must survive the newest-first read + reverse.
    assert session.saw_remote_camera is True, (  # type: ignore[union-attr]
        "saw_remote_camera is not the LAST published media_state — iteration order "
        "is inverted, which also inverts first-wins on session_start_ts"
    )


def test_stream_read_count_covers_writer_cap() -> None:
    """The read window must be at least the writer-side MAXLEN, in BOTH languages.

    This closes the blind spot that the two regression tests above do not: they
    monkeypatch `_STREAM_READ_COUNT`, so the value that actually SHIPS is never
    exercised by them. Six independent mutations of the shipped width — including
    restoring the literal `50000` that caused the 2026-08-27 incident — passed the
    entire suite before this test existed.

    The invariant: everything Redis still retains for `speaker_events_relative`
    must be readable. If the writer's MAXLEN exceeds the reader's window, entries
    are retained but structurally invisible — the same silent failure as the
    original bug, entering from the other end, with no error anywhere.

    The cap is declared in TypeScript and the window in Python, so the TS value is
    parsed out of the source here. Prose in two files across a language boundary
    cannot stop the two drifting; only an assertion can.
    """
    import re

    import aw_integration.adapter as _adapter

    assert _adapter._STREAM_READ_COUNT >= _adapter._SPEAKER_EVENT_STREAM_MAXLEN, (
        f"read window {_adapter._STREAM_READ_COUNT} is below the writer cap "
        f"{_adapter._SPEAKER_EVENT_STREAM_MAXLEN}: Redis would retain entries that "
        "this reader can never see, silently losing speaker names again"
    )

    # A floor-clamp, not advice: an operator typo must not be able to reintroduce
    # the incident. (`_resolve_stream_read_count` has no ceiling — widening is safe.)
    _os_environ_backup = _os.environ.get("AW_STREAM_READ_COUNT")
    try:
        _os.environ["AW_STREAM_READ_COUNT"] = "2000"  # typo for 200000
        assert (
            _adapter._resolve_stream_read_count()
            >= _adapter._SPEAKER_EVENT_STREAM_MAXLEN
        ), "a too-small override was accepted; the floor clamp is gone"
        _os.environ["AW_STREAM_READ_COUNT"] = "not-a-number"
        assert (
            _adapter._resolve_stream_read_count() == _adapter._STREAM_READ_COUNT_DEFAULT
        ), "a non-numeric override must fall back, not raise"
    finally:
        if _os_environ_backup is None:
            _os.environ.pop("AW_STREAM_READ_COUNT", None)
        else:
            _os.environ["AW_STREAM_READ_COUNT"] = _os_environ_backup

    # Cross-language: parse the TypeScript constant the Python mirror claims to track.
    ts = (
        _Path(__file__).resolve().parents[3]
        / "services"
        / "vexa-bot"
        / "core"
        / "src"
        / "services"
        / "segment-publisher.ts"
    )
    if ts.is_file():
        m = re.search(
            r"SPEAKER_EVENT_STREAM_MAXLEN\s*=\s*(\d+)", ts.read_text(encoding="utf-8")
        )
        assert m, f"SPEAKER_EVENT_STREAM_MAXLEN not found in {ts}"
        ts_cap = int(m.group(1))
        assert ts_cap == _adapter._SPEAKER_EVENT_STREAM_MAXLEN, (
            f"TypeScript cap {ts_cap} != Python mirror "
            f"{_adapter._SPEAKER_EVENT_STREAM_MAXLEN} — the two have drifted"
        )
        assert _adapter._STREAM_READ_COUNT >= ts_cap, (
            f"read window {_adapter._STREAM_READ_COUNT} < TypeScript writer cap "
            f"{ts_cap}: Redis retains what this reader cannot see"
        )
