"""§1.11, §6.9 — every setting a sweep or an item reads after boot is checked when meeting-api
boots: a value it can't run on refuses to start (``SettingsError``, naming the key), and the
defaults start. ``build_production_app`` is built for real, with the config.v1 preflight skipped
and dummy connection settings (nothing connects at build time).
"""

from __future__ import annotations

import pytest

from meeting_api.settings import SettingsError

KEYS = (
    "UNPROVEN_TEARDOWN_MAX_AGE_S",
    "SWEEP_BATCH_SIZE",
    "SWEEP_MAX_ITEM_FAILURES",
    "SWEEP_ITEM_FAILURES_RETENTION_S",
    "BOT_SEND_MAX_ATTEMPTS",
    "BOT_SEND_RETRY_BACKOFF_S",
    "INTAKE_CONFLICT_RETRIES",
    "INTAKE_CONFLICT_DELAY_MIN_S",
    "INTAKE_CONFLICT_DELAY_MAX_S",
    "INTAKE_STOP_LINK_RETRIES",
    "MEETING_UNTRACKED_GRACE_SEC",
)


def _production_env(monkeypatch: pytest.MonkeyPatch) -> None:
    import meeting_api.__main__ as main_mod

    monkeypatch.setattr(main_mod, "_require_config", lambda env=None: None)
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql+asyncpg://nobody:dummy@127.0.0.1:1/none"
    )
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin-token")
    monkeypatch.setenv("ADMIN_API_URL", "http://admin-api.test")
    monkeypatch.setenv("INTERNAL_API_SECRET", "test-internal-secret")
    for key in KEYS:
        monkeypatch.delenv(key, raising=False)


@pytest.mark.parametrize(
    "key, raw",
    [
        ("UNPROVEN_TEARDOWN_MAX_AGE_S", "6h"),
        ("UNPROVEN_TEARDOWN_MAX_AGE_S", "0"),
        ("UNPROVEN_TEARDOWN_MAX_AGE_S", "-21600"),
        ("UNPROVEN_TEARDOWN_MAX_AGE_S", "nan"),
        ("UNPROVEN_TEARDOWN_MAX_AGE_S", "inf"),
        ("SWEEP_BATCH_SIZE", "many"),
        ("SWEEP_BATCH_SIZE", "0"),
        ("SWEEP_BATCH_SIZE", "2.5"),
        ("SWEEP_MAX_ITEM_FAILURES", "five"),
        ("SWEEP_MAX_ITEM_FAILURES", "0"),
        ("SWEEP_ITEM_FAILURES_RETENTION_S", "a week"),
        ("SWEEP_ITEM_FAILURES_RETENTION_S", "0"),
        ("BOT_SEND_MAX_ATTEMPTS", "three"),
        ("BOT_SEND_MAX_ATTEMPTS", "0"),
        ("BOT_SEND_RETRY_BACKOFF_S", "1m"),
        ("BOT_SEND_RETRY_BACKOFF_S", "0"),
        ("INTAKE_CONFLICT_RETRIES", "some"),
        ("INTAKE_CONFLICT_RETRIES", "-1"),
        ("INTAKE_CONFLICT_DELAY_MIN_S", "soon"),
        ("INTAKE_CONFLICT_DELAY_MIN_S", "-0.01"),
        ("INTAKE_CONFLICT_DELAY_MAX_S", "later"),
        ("INTAKE_CONFLICT_DELAY_MAX_S", "0"),
        ("INTAKE_STOP_LINK_RETRIES", "once"),
        ("INTAKE_STOP_LINK_RETRIES", "-1"),
        ("MEETING_UNTRACKED_GRACE_SEC", "ten minutes"),
        ("MEETING_UNTRACKED_GRACE_SEC", "0"),
    ],
)
def test_a_setting_it_cannot_run_on_refuses_to_boot(monkeypatch, key, raw):
    import meeting_api.__main__ as main_mod

    _production_env(monkeypatch)
    monkeypatch.setenv(key, raw)
    with pytest.raises(SettingsError, match=key):
        main_mod.build_production_app()


def test_a_conflict_pause_whose_least_is_above_its_most_refuses_to_boot(monkeypatch):
    import meeting_api.__main__ as main_mod

    _production_env(monkeypatch)
    monkeypatch.setenv("INTAKE_CONFLICT_DELAY_MIN_S", "0.5")
    monkeypatch.setenv("INTAKE_CONFLICT_DELAY_MAX_S", "0.1")
    with pytest.raises(
        SettingsError, match="INTAKE_CONFLICT_DELAY_MIN_S.*INTAKE_CONFLICT_DELAY_MAX_S"
    ):
        main_mod.build_production_app()


def test_the_defaults_boot(monkeypatch):
    import meeting_api.__main__ as main_mod

    _production_env(monkeypatch)
    main_mod.build_production_app()


def test_no_more_runs_and_no_least_pause_boot(monkeypatch):
    """A count of *more* runs may be 0 (the write or stop runs once), and the pause's least may
    be 0: the code needs neither above 0."""
    import meeting_api.__main__ as main_mod

    _production_env(monkeypatch)
    monkeypatch.setenv("INTAKE_CONFLICT_RETRIES", "0")
    monkeypatch.setenv("INTAKE_STOP_LINK_RETRIES", "0")
    monkeypatch.setenv("INTAKE_CONFLICT_DELAY_MIN_S", "0")
    main_mod.build_production_app()


def test_an_empty_setting_takes_its_default(monkeypatch):
    from meeting_api.lifecycle.reconcile import unproven_teardown_max_age_s
    from meeting_api.sweeps.item_failures import sweep_batch_size

    monkeypatch.setenv("UNPROVEN_TEARDOWN_MAX_AGE_S", "")
    monkeypatch.setenv("SWEEP_BATCH_SIZE", " ")
    assert (unproven_teardown_max_age_s(), sweep_batch_size()) == (21600.0, 200)
