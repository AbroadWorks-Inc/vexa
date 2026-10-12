"""Environment -> Settings (spec §4.4). Names only; values come from the deployment."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


def _bool(raw: str | None) -> bool:
    return (raw or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    gateway_url: str
    exporter_api_key: str
    # The exporter's `/v2/webhooks` subscription secret (EXPORTER_WEBHOOK_SECRET).
    webhook_secret: str
    vexa_bucket: str
    export_bucket: str
    export_prefix: str
    notetaker_url: str
    debug: bool = False
    concurrency: int = 4
    sweep_seconds: float = 60.0
    max_attempts: int = 5
    # A failed export waits this × 2**attempts seconds before its next try.
    retry_backoff_seconds: float = 30.0
    # Runs that stopped mid-export (the pod was killed or ran out of memory)
    # before the meeting is moved to failed/: a crash never raises, so it has
    # its own budget beside max_attempts.
    max_crashes: int = 2
    # A running export holds a lease, renewed while it runs; one that expires
    # unfinished counts as a crash.
    lease_seconds: float = 120.0
    # A job running longer than this has hung: /healthz fails, so the
    # liveness probe restarts the pod and the meeting is charged a crash.
    # Measured: decoding 1 h of opus takes ~7 CPU-s and encoding a join ~15
    # CPU-s; the worst real export (a 3 h meeting of several bot sessions,
    # 19 channels joined, a slower core shared four ways) is estimated at
    # under 3 h. Typical exports take minutes.
    job_timeout_seconds: float = 14400.0
    # On SIGTERM, how long running exports get to finish and record their
    # outcome; below the Deployment's terminationGracePeriodSeconds (120).
    stop_wait_seconds: float = 100.0
    rms_speech_threshold: float = (
        0.026  # measured on 2026-09-22 Meet recording (spec §4.3)
    )
    speech_hangover_ms: int = 700
    min_dominant_utterance_ms: int = 1500
    record_chunk_timeslice_ms: int = 15000
    # The bot uploads speaker-activity.jsonl in its teardown, AFTER
    # meeting.completed (spec §4.2 step 5): wait this long past ended_at
    # before "missing".
    activity_wait_seconds: float = 120.0
    # A meeting's recordings are read page by page up to this many; past it
    # the export fails rather than export part of the meeting.
    max_recordings: int = 50

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Settings:
        def req(name: str) -> str:
            value = env.get(name, "").strip()
            if not value:
                raise RuntimeError(f"{name} is required")
            return value

        prefix = env.get("EXPORT_PREFIX", "recordings/").strip() or "recordings/"
        return cls(
            gateway_url=req("GATEWAY_URL").rstrip("/"),
            exporter_api_key=req("EXPORTER_API_KEY"),
            webhook_secret=req("EXPORTER_WEBHOOK_SECRET"),
            vexa_bucket=req("VEXA_BUCKET"),
            export_bucket=req("EXPORT_BUCKET"),
            export_prefix=prefix if prefix.endswith("/") else prefix + "/",
            notetaker_url=req("NOTETAKER_URL").rstrip("/"),
            debug=_bool(env.get("EXPORT_DEBUG")),
            concurrency=int(env.get("EXPORT_CONCURRENCY", "4")),
            sweep_seconds=float(env.get("EXPORT_SWEEP_SECONDS", "60")),
            max_attempts=int(env.get("EXPORT_MAX_ATTEMPTS", "5")),
            retry_backoff_seconds=float(env.get("EXPORT_RETRY_BACKOFF_SECONDS", "30")),
            max_crashes=int(env.get("EXPORT_MAX_CRASHES", "2")),
            lease_seconds=float(env.get("EXPORT_LEASE_SECONDS", "120")),
            job_timeout_seconds=float(env.get("EXPORT_JOB_TIMEOUT_SECONDS", "14400")),
            stop_wait_seconds=float(env.get("EXPORT_STOP_WAIT_SECONDS", "100")),
            rms_speech_threshold=float(env.get("RMS_SPEECH_THRESHOLD", "0.026")),
            speech_hangover_ms=int(env.get("SPEECH_HANGOVER_MS", "700")),
            min_dominant_utterance_ms=int(env.get("MIN_DOMINANT_UTTERANCE_MS", "1500")),
            record_chunk_timeslice_ms=int(
                env.get("RECORD_CHUNK_TIMESLICE_MS", "15000")
            ),
            activity_wait_seconds=float(env.get("ACTIVITY_WAIT_SECONDS", "120")),
            max_recordings=int(env.get("EXPORT_MAX_RECORDINGS", "50")),
        )
