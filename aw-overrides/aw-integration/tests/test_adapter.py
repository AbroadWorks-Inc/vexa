"""Tests for aw_integration adapter, s3_writer, and notetaker_client (§13.15).

Run with:
    PYTHONPATH=../../../notetaker-common:. pytest tests/test_adapter.py
"""

from __future__ import annotations

from datetime import datetime, timezone
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
    )


def make_adapter(
    tmp_path: Path,
    pcm_bytes: bytes | None = None,
    with_speaker_events: bool = True,
) -> tuple[VexaSessionAdapter, MagicMock, MagicMock]:
    """Return (adapter, mock_s3_writer, mock_notetaker_client)."""
    raw_audio = tmp_path / "audio.raw"
    if pcm_bytes is not None:
        raw_audio.write_bytes(pcm_bytes)

    s3_writer = MagicMock(spec=S3Writer)
    notetaker_client = MagicMock(spec=NoteTakerClientWrapper)
    notetaker_client.post_process = AsyncMock(return_value={"status": "ok"})

    adapter = VexaSessionAdapter(
        session=make_test_session(with_speaker_events=with_speaker_events),
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

    def test_unclosed_interval_is_closed_at_session_end(
        self, tmp_path: Path
    ) -> None:
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

    def test_dom_events_are_never_paired_into_intervals(
        self, tmp_path: Path
    ) -> None:
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

    def test_untagged_events_fall_back_to_legacy_points(
        self, tmp_path: Path
    ) -> None:
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
