"""Unit tests for knock_delay.py (delay the guest-join knock until start).

Every failure path must return 0 ("knock now", i.e. pre-change behaviour). A bug
that made this module sleep when it should not is the one way this change could
silently lose a recording, so the no-sleep cases are tested at least as hard as
the sleep case.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import knock_delay as kd  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 8, 18, 18, 20, 7, tzinfo=timezone.utc)  # the real spawn instant


def _job(start: str | None, **extra: object) -> str:
    body: dict[str, object] = dict(extra)
    if start is not None:
        body["scheduled_start_at"] = start
    return json.dumps(body)


class TestSleepsWhenItShould:
    def test_the_live_regression_case_sleeps_until_one_minute_before_start(
        self,
    ) -> None:
        """The exact job that failed live: spawn 18:20:07Z, start 00:00 IST.

        Start is 18:30:00Z, so the knock target is 18:29:00Z -> 533s.
        """
        seconds, reason = kd.seconds_until_knock(_job("2026-08-19T00:00:00+05:30"), NOW)
        assert seconds == 533
        assert "knocking 60s before" in reason

    def test_offset_is_honoured_not_stripped(self) -> None:
        """Same wall-clock string in two zones must give different sleeps.

        Guards against a mutation that parses the timestamp then ignores its
        offset (e.g. by re-reading it as UTC), which would be silently wrong by
        hours for any non-UTC user.
        """
        ist = kd.seconds_until_knock(_job("2026-08-19T00:00:00+05:30"), NOW)[0]
        utc = kd.seconds_until_knock(_job("2026-08-19T00:00:00+00:00"), NOW)[0]
        assert ist != utc
        assert ist == 533

    def test_z_suffix_is_accepted(self) -> None:
        seconds, _ = kd.seconds_until_knock(_job("2026-08-18T18:30:00Z"), NOW)
        assert seconds == 533

    def test_lead_seconds_shifts_the_target(self) -> None:
        base = kd.seconds_until_knock(_job("2026-08-18T18:30:00Z"), NOW)[0]
        longer = kd.seconds_until_knock(
            _job("2026-08-18T18:30:00Z"), NOW, lead_seconds=120
        )[0]
        assert longer == base - 60

    def test_zero_lead_sleeps_right_up_to_the_start(self) -> None:
        seconds, _ = kd.seconds_until_knock(
            _job("2026-08-18T18:30:00Z"), NOW, lead_seconds=0
        )
        assert seconds == 593

    def test_extra_unknown_job_fields_are_ignored(self) -> None:
        """A BotJob schema change elsewhere must not affect this decision."""
        seconds, _ = kd.seconds_until_knock(
            _job("2026-08-18T18:30:00Z", join={"url": "x"}, brand_new_field=[1, 2]), NOW
        )
        assert seconds == 533


class TestNeverSleeps:
    """Every one of these must yield 0 — identical to pre-change behaviour."""

    @pytest.mark.parametrize(
        "raw,label",
        [
            (None, "env unset"),
            ("", "empty string"),
            ("   ", "whitespace only"),
            ("not json at all", "not JSON"),
            ("[1,2,3]", "JSON array, not an object"),
            ('"a string"', "JSON string, not an object"),
            ("null", "JSON null"),
            ("42", "JSON number"),
            ("{}", "object with no scheduled_start_at"),
            ('{"scheduled_start_at": null}', "explicit null start"),
            ('{"scheduled_start_at": ""}', "empty start"),
            ('{"scheduled_start_at": "   "}', "whitespace start"),
            ('{"scheduled_start_at": 1755540000}', "start is a number"),
            ('{"scheduled_start_at": {"at": "x"}}', "start is an object"),
            ('{"scheduled_start_at": "tomorrow at noon"}', "unparseable start"),
            ('{"scheduled_start_at": "2026-13-45T99:00:00Z"}', "impossible date"),
            ('{"scheduled_start_at": "2026-08-19T00:00:00"}', "naive, no timezone"),
        ],
    )
    def test_bad_input_knocks_immediately(self, raw: str | None, label: str) -> None:
        seconds, reason = kd.seconds_until_knock(raw, NOW)
        assert seconds == 0, label
        assert "knocking now" in reason, label

    def test_start_already_passed(self) -> None:
        seconds, reason = kd.seconds_until_knock(_job("2026-08-18T18:00:00Z"), NOW)
        assert seconds == 0
        assert "already due" in reason

    def test_start_inside_the_lead_window(self) -> None:
        """30s before start: the knock target has passed, so do not sleep."""
        seconds, _ = kd.seconds_until_knock(
            _job((NOW + timedelta(seconds=30)).isoformat()), NOW
        )
        assert seconds == 0

    def test_exactly_at_the_knock_target(self) -> None:
        seconds, _ = kd.seconds_until_knock(
            _job((NOW + timedelta(seconds=60)).isoformat()), NOW
        )
        assert seconds == 0

    def test_timezone_naive_now_is_rejected(self) -> None:
        """A naive `now` would be compared against an absolute instant."""
        naive = NOW.replace(tzinfo=None)
        seconds, reason = kd.seconds_until_knock(_job("2026-08-18T18:30:00Z"), naive)
        assert seconds == 0
        assert "timezone-naive" in reason

    def test_negative_lead_is_rejected(self) -> None:
        seconds, reason = kd.seconds_until_knock(
            _job("2026-08-18T18:30:00Z"), NOW, lead_seconds=-60
        )
        assert seconds == 0
        assert "invalid lead_seconds" in reason


class TestCeiling:
    def test_far_future_start_is_clamped_not_skipped(self) -> None:
        seconds, reason = kd.seconds_until_knock(_job("2027-01-01T00:00:00Z"), NOW)
        assert seconds == kd.MAX_SLEEP_SECONDS
        assert "ceiling" in reason

    def test_just_over_the_ceiling_clamps(self) -> None:
        start = NOW + timedelta(seconds=kd.MAX_SLEEP_SECONDS + 61)
        seconds, _ = kd.seconds_until_knock(_job(start.isoformat()), NOW)
        assert seconds == kd.MAX_SLEEP_SECONDS

    def test_just_under_the_ceiling_is_exact(self) -> None:
        start = NOW + timedelta(seconds=kd.MAX_SLEEP_SECONDS + 59)
        seconds, _ = kd.seconds_until_knock(_job(start.isoformat()), NOW)
        assert seconds == kd.MAX_SLEEP_SECONDS - 1

    def test_exactly_at_the_ceiling_is_not_clamped(self) -> None:
        """delta == MAX takes the normal path, not the clamp path.

        Both paths return the same 900, so only the reason distinguishes them —
        which is what makes the `>` vs `>=` mutation observable at all.
        """
        start = NOW + timedelta(seconds=kd.MAX_SLEEP_SECONDS + 60)
        seconds, reason = kd.seconds_until_knock(_job(start.isoformat()), NOW)
        assert seconds == kd.MAX_SLEEP_SECONDS
        assert "ceiling" not in reason, "delta == MAX must not be reported as clamped"

    def test_stays_within_bounds_across_a_wide_sweep(self) -> None:
        """Never negative, never above the ceiling — for past AND future starts.

        The lower bound is the load-bearing half: a negative value would be
        handed to `sleep` in start.sh as `sleep -1267`, which fails the boot.
        Past starts are included precisely to pin that.
        """
        for minutes in range(-2880, 4321, 37):  # -2 days -> +3 days
            start = NOW + timedelta(minutes=minutes)
            seconds, _ = kd.seconds_until_knock(_job(start.isoformat()), NOW)
            assert 0 <= seconds <= kd.MAX_SLEEP_SECONDS, f"at {minutes} min"


class TestLeadFromEnv:
    """BOT_KNOCK_LEAD_SECONDS overrides the lead; bad values fall back to 60.

    Fail-open HERE means "the proven 60s default", NOT "knock now": a config typo
    must keep the hold this module exists to provide, not silently disable it. That
    is why an invalid env value resolves to DEFAULT_LEAD_SECONDS rather than 0 (a
    negative lead passed straight to `seconds_until_knock` is what yields 0).
    """

    def test_env_override_is_used_as_the_lead(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BOT_KNOCK_LEAD_SECONDS", "300")
        assert kd._lead_seconds_from_env() == 300

    def test_unset_falls_back_to_default_60(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("BOT_KNOCK_LEAD_SECONDS", raising=False)
        assert kd._lead_seconds_from_env() == kd.DEFAULT_LEAD_SECONDS == 60

    @pytest.mark.parametrize("raw", ["5m", "", "   ", "1.5", "sixty", "0x3c", "60s"])
    def test_invalid_falls_back_to_default_60(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        monkeypatch.setenv("BOT_KNOCK_LEAD_SECONDS", raw)
        assert kd._lead_seconds_from_env() == 60

    def test_zero_env_is_honoured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """0 is a valid, deliberate value (knock right at the start), not "bad"."""
        monkeypatch.setenv("BOT_KNOCK_LEAD_SECONDS", "0")
        assert kd._lead_seconds_from_env() == 0

    def test_surrounding_whitespace_is_tolerated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BOT_KNOCK_LEAD_SECONDS", "  120 ")
        assert kd._lead_seconds_from_env() == 120

    @pytest.mark.parametrize("raw", ["5m", "", "   ", "not-an-int", "300", "0"])
    def test_never_raises_on_any_env(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        monkeypatch.setenv("BOT_KNOCK_LEAD_SECONDS", raw)
        kd._lead_seconds_from_env()  # must not raise for any input


class TestMainThreadsEnvLead:
    """main() must feed the env-resolved lead into the decision, not the constant."""

    def test_env_lead_shifts_the_computed_sleep(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Start 400s out. lead=300 -> target=now+100 -> sleep ~100s. The default
        # lead of 60 would give ~340s, so this range is only reachable if the env
        # value actually reached seconds_until_knock.
        start = (datetime.now(timezone.utc) + timedelta(seconds=400)).isoformat()
        monkeypatch.setenv("BOT_JOB_JSON", _job(start))
        monkeypatch.setenv("BOT_KNOCK_LEAD_SECONDS", "300")
        assert kd.main() == 0
        out, err = capsys.readouterr()
        seconds = int(out.strip())
        assert 80 <= seconds <= 120, err
        assert "knocking 300s before" in err

    def test_invalid_env_lead_falls_back_to_60_in_main(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        start = (datetime.now(timezone.utc) + timedelta(seconds=400)).isoformat()
        monkeypatch.setenv("BOT_JOB_JSON", _job(start))
        monkeypatch.setenv("BOT_KNOCK_LEAD_SECONDS", "5m")
        assert kd.main() == 0
        out, err = capsys.readouterr()
        seconds = int(out.strip())
        assert 320 <= seconds <= 360, err
        assert "knocking 60s before" in err


class TestMainEntrypoint:
    """stdout must carry ONLY the integer — start.sh parses it."""

    def test_prints_integer_to_stdout_and_reason_to_stderr(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        far = (datetime.now(timezone.utc) + timedelta(seconds=300)).isoformat()
        monkeypatch.setenv("BOT_JOB_JSON", _job(far))
        assert kd.main() == 0
        out, err = capsys.readouterr()
        assert out.strip().isdigit()
        assert 200 <= int(out.strip()) <= 300
        assert "knock_delay" in err

    def test_prints_zero_when_env_missing(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.delenv("BOT_JOB_JSON", raising=False)
        assert kd.main() == 0
        out, _ = capsys.readouterr()
        assert out.strip() == "0"

    def test_prints_zero_on_garbage_env(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("BOT_JOB_JSON", "{{{not json")
        assert kd.main() == 0
        out, _ = capsys.readouterr()
        assert out.strip() == "0"
