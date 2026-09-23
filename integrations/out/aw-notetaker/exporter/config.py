"""Environment -> Settings (spec §4.4). Names only; values come from the deployment."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


def _bool(raw: str | None) -> bool:
    return (raw or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    meeting_api_url: str
    webhook_secret: str
    vexa_bucket: str
    export_bucket: str
    export_prefix: str
    notetaker_url: str
    debug: bool = False
    concurrency: int = 4
    sweep_seconds: float = 60.0
    max_attempts: int = 5
    rms_speech_threshold: float = (
        0.026  # measured on 2026-09-22 Meet recording (spec §4.3)
    )
    speech_hangover_ms: int = 700
    min_dominant_utterance_ms: int = 1500
    record_chunk_timeslice_ms: int = 15000
    # The bot uploads speaker-activity.jsonl in its teardown, AFTER
    # meeting.completed (spec §4.2 step 5): wait this long past end_time
    # before "missing".
    activity_wait_seconds: float = 120.0

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Settings:
        def req(name: str) -> str:
            value = env.get(name, "").strip()
            if not value:
                raise RuntimeError(f"{name} is required")
            return value

        prefix = env.get("EXPORT_PREFIX", "recordings/").strip() or "recordings/"
        return cls(
            meeting_api_url=req("MEETING_API_URL").rstrip("/"),
            webhook_secret=req("VEXA_WEBHOOK_SECRET"),
            vexa_bucket=req("VEXA_BUCKET"),
            export_bucket=req("EXPORT_BUCKET"),
            export_prefix=prefix if prefix.endswith("/") else prefix + "/",
            notetaker_url=req("NOTETAKER_URL").rstrip("/"),
            debug=_bool(env.get("EXPORT_DEBUG")),
            concurrency=int(env.get("EXPORT_CONCURRENCY", "4")),
            sweep_seconds=float(env.get("EXPORT_SWEEP_SECONDS", "60")),
            max_attempts=int(env.get("EXPORT_MAX_ATTEMPTS", "5")),
            rms_speech_threshold=float(env.get("RMS_SPEECH_THRESHOLD", "0.026")),
            speech_hangover_ms=int(env.get("SPEECH_HANGOVER_MS", "700")),
            min_dominant_utterance_ms=int(env.get("MIN_DOMINANT_UTTERANCE_MS", "1500")),
            record_chunk_timeslice_ms=int(
                env.get("RECORD_CHUNK_TIMESLICE_MS", "15000")
            ),
            activity_wait_seconds=float(env.get("ACTIVITY_WAIT_SECONDS", "120")),
        )
