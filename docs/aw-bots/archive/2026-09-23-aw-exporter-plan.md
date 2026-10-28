# aw-exporter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** On every Vexa `meeting.completed`, build `s3://aw-chatworks-transcribe/recordings/<platform>_<nativeId>_<startUTC>/` (master audio, meeting/recording/participant JSON, speaker timeline) and hand it to `notetaker-worker` `/process`.

**Architecture:** A small FastAPI service in `integrations/out/aw-notetaker/` receives Vexa's signed system webhook, durably enqueues the meeting in S3, and a worker loop exports it: Vexa assembles the audio master, the exporter transcodes it to 16 kHz WAV, derives speaker events from Vexa's `captured-signal.v1` tape, runs the attribution rules ported from the old `aw-integration` adapter, writes the folder, and calls `/process`. Pure logic (naming, signature, tape, attribution) is separated from I/O adapters (S3, meeting-api, notetaker, ffmpeg) so each is testable alone.

**Tech Stack:** Python 3.11, FastAPI, uvicorn, httpx, boto3, pydantic v2, ffmpeg (binary); pytest, moto[s3] (dev); black 25.1.0, ruff 0.15.3, mypy 1.17.1 (strict).

**Spec:** `integrations/out/aw-notetaker/docs/2026-09-23-aw-rearchitecture-design.md` (read §3, §4 before any task).

## Global Constraints

- Python `>=3.11,<3.12` (`/opt/homebrew/bin/python3.11` locally); type hints on every public function; mypy strict.
- Runtime deps are exactly: `fastapi`, `uvicorn[standard]`, `httpx`, `boto3`, `pydantic>=2`. Dev: `pytest`, `pytest-asyncio`, `moto[s3]`, `black==25.1.0`, `ruff==0.15.3`, `mypy==1.17.1`, `boto3-stubs[s3]`. Anything else is a surfaced deviation. All are MIT/BSD/Apache (FINOS Category A).
- NO change under `core/`, `clients/`, `deploy/` — this plan only adds `integrations/out/aw-notetaker/`.
- NO real participant audio, names or transcripts in committed fixtures — synthetic tapes only. The 2026-09-22 recording under `recordings/` (gitignored) is used by the spike only, never copied into the repo.
- NO secret values anywhere (code, tests, logs, docs): env var NAMES only. Test secrets are literal dummies (`"test-secret"`).
- Folder name: `<platform>_<nativeId>_<startUTC>`; `nativeId` chars outside `[A-Za-z0-9.-]` → `-`; `startUTC` = `start_time` as `%Y%m%dT%H%M%S` + 3-digit ms + `Z`.
- Webhook: `X-Webhook-Signature == "sha256=" + hex(HMAC_SHA256(secret, f"{X-Webhook-Timestamp}.".encode() + raw_body))`, constant-time compare, max age 300 s, fail closed without a secret.
- `/process` body: `{"meeting_id": "vexa-<id>", "s3_path": "recordings/<folder>/", "platform": <vexa platform>, "idempotency_key": "vexa-<id>"}`.
- Buckets/prefixes: Vexa bucket `VEXA_BUCKET` (aw-bots) is READ-ONLY to the exporter except `aw-exporter/`; export bucket `EXPORT_BUCKET` + `EXPORT_PREFIX` (`recordings/`).
- Commits: Conventional Commits, one per task, body cites the spec §; end with the `Co-Authored-By` line from the session. Never commit `.gitignore`/`docs/call_analysis_22.09.2026.txt` (not ours).

## File Structure

```
integrations/out/aw-notetaker/
  README.md                     what it is, env vars (names), how to run tests
  pyproject.toml                package aw-exporter, deps, tool config
  Dockerfile                    python:3.11-slim + ffmpeg
  docs/                         spec + this plan
  exporter/
    __init__.py
    config.py                   Settings.from_env()
    naming.py                   folder_name(meeting) -> str
    signature.py                verify(body, headers, secret, now) -> bool
    tape.py                     parse_tape(lines) -> Tape; speech_events(tape, origin_ms, ...) -> list[SpeakerEvent]
    schemas.py                  pydantic models mirroring notetaker_common (SpeakerTimelineFile, ParticipantsFile, …)
    attribution.py              build_speaker_timeline(...), build_participants(...)  (port of aw-integration)
    storage.py                  S3 adapter (get/put/copy/list/delete, line iterator)
    vexa_client.py              meeting-api client (recordings, master, live transcript)
    notetaker.py                /process client
    audio.py                    webm -> 16 kHz mono wav via ffmpeg
    job.py                      export_meeting(envelope, deps) -> ExportResult
    queue.py                    PendingQueue (S3 prefix) + run_worker loop
    app.py                      FastAPI: POST /hooks/vexa, GET /healthz
  tests/
    conftest.py                 moto S3 fixture, synthetic tape builders
    test_naming.py  test_signature.py  test_tape.py  test_attribution.py
    test_storage.py  test_clients.py  test_job.py  test_queue_app.py
    integration/test_compose_flow.py     (marked `integration`, needs compose MinIO)
```

---

### Task 1: Baseline the repo gates and scaffold the package

**Files:**
- Create: `integrations/out/aw-notetaker/pyproject.toml`, `README.md`, `exporter/__init__.py`, `tests/__init__.py`, `tests/test_smoke.py`

**Interfaces:**
- Produces: importable package `exporter` with `__version__ = "0.1.0"`; a venv at `integrations/out/aw-notetaker/.venv` (gitignored by the root `.gitignore` `.venv` rule — verify, else add to a package-local `.gitignore`).

- [ ] **Step 1: Record the upstream gate baseline BEFORE adding anything**

```bash
cd /Applications/XAMPP/xamppfiles/htdocs/mike/aw-notetaker/vexa-fork
pnpm install --frozen-lockfile
node scripts/gates.mjs all 2>&1 | tee /tmp/claude-gates-baseline.txt | tail -40
```
Expected: a pass/fail list. Save which gates are ALREADY red on this branch — only new reds introduced by this plan count against it.

- [ ] **Step 2: Write `pyproject.toml`**

```toml
[build-system]
requires = ["setuptools>=68", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "aw-exporter"
version = "0.1.0"
description = "Vexa meeting.completed -> AbroadWorks notetaker folder + /process hand-off"
requires-python = ">=3.11,<3.12"
dependencies = [
    "fastapi>=0.115,<1",
    "uvicorn[standard]>=0.30,<1",
    "httpx>=0.27,<1",
    "boto3>=1.34,<2",
    "pydantic>=2.7,<3",
]

[project.optional-dependencies]
dev = [
    "pytest>=8",
    "pytest-asyncio>=0.23",
    "moto[s3]>=5",
    "boto3-stubs[s3]>=1.34",
    "black==25.1.0",
    "ruff==0.15.3",
    "mypy==1.17.1",
]

[tool.setuptools.packages.find]
where = ["."]
include = ["exporter*"]

[tool.pytest.ini_options]
asyncio_mode = "strict"
markers = ["integration: needs the compose MinIO + stub notetaker"]
addopts = ["-m", "not integration"]

[tool.black]
line-length = 88
target-version = ["py311"]

[tool.ruff]
line-length = 88
target-version = "py311"

[tool.mypy]
python_version = "3.11"
strict = true
```

- [ ] **Step 3: Package init + smoke test**

`exporter/__init__.py`:
```python
"""Vexa meeting.completed -> AbroadWorks notetaker hand-off (see docs/)."""

__version__ = "0.1.0"
```
`tests/test_smoke.py`:
```python
import exporter


def test_version() -> None:
    assert exporter.__version__ == "0.1.0"
```

- [ ] **Step 4: Create venv, install, run**

```bash
cd integrations/out/aw-notetaker
/opt/homebrew/bin/python3.11 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest -q && black --check . && ruff check . && mypy exporter
```
Expected: `1 passed`; black/ruff/mypy clean.

- [ ] **Step 5: README.md** — purpose (2 lines), link to the spec, env var NAMES from spec §4.4, the Step 4 commands.

- [ ] **Step 6: Re-run gates; fix only NEW reds**

```bash
cd ../../.. && node scripts/gates.mjs all 2>&1 | diff /tmp/claude-gates-baseline.txt - | head -40
```
Expected: no new failures. If a gate objects to the new folder (e.g. README/isolation/CALM registration), follow that gate's error text; if it requires an `architecture.calm.json` node, add one and `pnpm seal:arch` (AGENTS.md P23). If a gate cannot be satisfied without touching `core/`, STOP and surface.

- [ ] **Step 7: Commit**

```bash
git add integrations/out/aw-notetaker
git commit -m "feat(aw-exporter): scaffold package under integrations/out/aw-notetaker" -m "Spec §4. Design + plan docs included."
```

---

### Task 2: Spike — clock origin and speech threshold on the 2026-09-22 recording (throwaway)

**Files:** scratchpad only (`$SCRATCH/spike_origin.py`); result recorded in the spec §4.3 (Modify).

**Interfaces:**
- Produces: the chosen `origin` rule (one of `"first_chunk"`, `"tape_started_at"`, `"recording_created_at"`) + measured error (ms), `RMS_SPEECH_THRESHOLD` default, and confirmation that Meet frames carry `speakerName`. Task 5 hard-codes the rule; Task 3's config default takes the threshold.

- [ ] **Step 1: Inputs (local, gitignored)**

```
R=recordings/recordings/1/855958819514/01ba075a-8a16-4e88-9288-09e0e427eff2/audio
T=recordings/signal/1/1/01ba075a-8a16-4e88-9288-09e0e427eff2/captured-signal.jsonl
ffmpeg -y -i $R/master.webm -ac 1 -ar 16000 -c:a pcm_s16le $SCRATCH/master.wav
```
Candidate epochs: tape header `started_at` (2026-09-22T17:01:59.687Z); recording `created_at` (17:02:49.811544Z, `recordings.json`); first-chunk time = the botlog line `[record-chunker] chunk 0` / `[Recording] Chunk uploaded ... chunk_seq":0` minus one chunk duration.

- [ ] **Step 2: Cross-correlate** — build a 50 ms energy envelope from (a) `master.wav` and (b) the tape: sum of `rms` of all frames per 50 ms bin at `ts`. Find the lag maximising normalised cross-correlation over ±120 s. The true epoch of wav t=0 = `tape_ts_of_bin0 + lag`. Print the lag and the residual against each candidate epoch.

- [ ] **Step 3: Threshold** — histogram frame `rms`; pick the valley between the silence mode and the speech mode; report the value and the % of named frames above it per speaker (sanity: every speaker in the transcript has speech frames; the speaker who talked only in the first minutes has frames in 17:02–17:05).

- [ ] **Step 4: Record in spec §4.3** — replace the "Clock origin" bullet with the measured rule + error, and the threshold default. Commit the spec edit only:

```bash
git add integrations/out/aw-notetaker/docs/2026-09-23-aw-rearchitecture-design.md
git commit -m "docs(aw-exporter): pin tape clock origin + speech threshold from measurement" -m "Spec §4.3; measured on the 2026-09-22 Meet recording (not committed)."
```
If NO candidate is within 250 ms, STOP: the exporter must compute the lag at runtime by the Step 2 method — surface this, it changes Task 5.

---

### Task 3: Config, folder naming, webhook signature

**Files:**
- Create: `exporter/config.py`, `exporter/naming.py`, `exporter/signature.py`
- Test: `tests/test_naming.py`, `tests/test_signature.py`

**Interfaces:**
- Produces:
  - `Settings` (frozen dataclass) fields: `meeting_api_url: str`, `webhook_secret: str`, `vexa_bucket: str`, `export_bucket: str`, `export_prefix: str`, `notetaker_url: str`, `debug: bool`, `concurrency: int`, `sweep_seconds: float`, `max_attempts: int`, `rms_speech_threshold: float`, `speech_hangover_ms: int`, `min_dominant_utterance_ms: int`; `Settings.from_env(env: Mapping[str, str]) -> Settings`.
  - `folder_name(platform: str, native_meeting_id: str, start_time: str) -> str`
  - `verify(body: bytes, headers: Mapping[str, str], secret: str, now: float, max_age_s: int = 300) -> bool`

- [ ] **Step 1: Failing tests**

`tests/test_naming.py`:
```python
import pytest

from exporter.naming import folder_name


def test_meet_example() -> None:
    assert (
        folder_name("google_meet", "eyw-igia-bab", "2026-09-22T17:02:34.432140Z")
        == "google_meet_eyw-igia-bab_20260922T170234432Z"
    )


def test_offset_is_normalised_to_utc() -> None:
    assert (
        folder_name("zoom", "85012345678", "2026-09-22T22:32:34.001+05:30")
        == "zoom_85012345678_20260922T170234001Z"
    )


def test_no_fraction_means_000() -> None:
    assert folder_name("teams", "123", "2026-09-22T17:02:34Z").endswith("T170234000Z")


def test_native_id_sanitised_so_underscore_only_separates() -> None:
    assert (
        folder_name("teams", "meet/a_b c", "2026-09-22T17:02:34Z")
        == "teams_meet-a-b-c_20260922T170234000Z"
    )


def test_missing_start_time_raises() -> None:
    with pytest.raises(ValueError):
        folder_name("zoom", "1", "")
```
`tests/test_signature.py`:
```python
import hashlib
import hmac

from exporter.signature import verify

SECRET = "test-secret"
BODY = b'{"event_type":"meeting.completed"}'


def _headers(ts: str, secret: str = SECRET, body: bytes = BODY) -> dict[str, str]:
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256)
    return {"X-Webhook-Signature": f"sha256={mac.hexdigest()}", "X-Webhook-Timestamp": ts}


def test_valid() -> None:
    assert verify(BODY, _headers("1000"), SECRET, now=1100)


def test_wrong_secret() -> None:
    assert not verify(BODY, _headers("1000", secret="other"), SECRET, now=1100)


def test_tampered_body() -> None:
    assert not verify(BODY + b" ", _headers("1000"), SECRET, now=1100)


def test_stale() -> None:
    assert not verify(BODY, _headers("1000"), SECRET, now=1000 + 301)


def test_future_beyond_window() -> None:
    assert not verify(BODY, _headers("2000"), SECRET, now=1000)


def test_missing_headers() -> None:
    assert not verify(BODY, {}, SECRET, now=1000)


def test_empty_secret_fails_closed() -> None:
    assert not verify(BODY, _headers("1000", secret=""), "", now=1000)


def test_non_numeric_timestamp() -> None:
    assert not verify(BODY, _headers("abc"), SECRET, now=1000)


def test_header_lookup_is_case_insensitive() -> None:
    h = {k.lower(): v for k, v in _headers("1000").items()}
    assert verify(BODY, h, SECRET, now=1000)
```

- [ ] **Step 2: Run — expect ImportError failures**

`pytest tests/test_naming.py tests/test_signature.py -q` → FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement**

`exporter/naming.py`:
```python
"""Export folder name — a pure function of the Vexa meeting row (spec §3)."""

from __future__ import annotations

import re
from datetime import datetime, timezone

_UNSAFE = re.compile(r"[^A-Za-z0-9.-]")


def folder_name(platform: str, native_meeting_id: str, start_time: str) -> str:
    if not start_time:
        raise ValueError("meeting has no start_time")
    started = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    utc = started.astimezone(timezone.utc)
    stamp = utc.strftime("%Y%m%dT%H%M%S") + f"{utc.microsecond // 1000:03d}Z"
    return f"{platform}_{_UNSAFE.sub('-', native_meeting_id)}_{stamp}"
```
`exporter/signature.py`:
```python
"""Verify Vexa's webhook signature (meeting_api/webhooks/delivery.py sign_payload)."""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping


def _get(headers: Mapping[str, str], name: str) -> str | None:
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None


def verify(
    body: bytes,
    headers: Mapping[str, str],
    secret: str,
    now: float,
    max_age_s: int = 300,
) -> bool:
    if not secret:
        return False
    signature = _get(headers, "X-Webhook-Signature")
    timestamp = _get(headers, "X-Webhook-Timestamp")
    if not signature or not timestamp:
        return False
    try:
        sent_at = int(timestamp)
    except ValueError:
        return False
    if abs(now - sent_at) > max_age_s:
        return False
    mac = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256)
    return hmac.compare_digest(signature, f"sha256={mac.hexdigest()}")
```
`exporter/config.py`:
```python
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
    rms_speech_threshold: float = 0.01  # replaced by the Task 2 measurement
    speech_hangover_ms: int = 700
    min_dominant_utterance_ms: int = 1500

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
            rms_speech_threshold=float(env.get("RMS_SPEECH_THRESHOLD", "0.01")),
            speech_hangover_ms=int(env.get("SPEECH_HANGOVER_MS", "700")),
            min_dominant_utterance_ms=int(env.get("MIN_DOMINANT_UTTERANCE_MS", "1500")),
        )
```
Add to `tests/test_naming.py` (config coverage lives with naming — both are pure):
```python
from exporter.config import Settings

BASE = {
    "MEETING_API_URL": "http://meeting-api:8080/",
    "VEXA_WEBHOOK_SECRET": "test-secret",
    "VEXA_BUCKET": "aw-bots",
    "EXPORT_BUCKET": "aw-chatworks-transcribe",
    "NOTETAKER_URL": "http://notetaker-api:8080",
}


def test_settings_defaults() -> None:
    s = Settings.from_env(BASE)
    assert s.meeting_api_url == "http://meeting-api:8080"
    assert s.export_prefix == "recordings/"
    assert s.debug is False and s.concurrency == 4


def test_settings_missing_required() -> None:
    with pytest.raises(RuntimeError, match="VEXA_WEBHOOK_SECRET"):
        Settings.from_env({k: v for k, v in BASE.items() if k != "VEXA_WEBHOOK_SECRET"})
```

- [ ] **Step 4: Run** `pytest -q && black --check . && ruff check . && mypy exporter` → all pass.

- [ ] **Step 5: Commit** `feat(aw-exporter): folder naming, webhook signature, settings` (body: spec §3, §4.1, §4.4).

---

### Task 4: Output schemas (mirror of notetaker_common)

**Files:**
- Create: `exporter/schemas.py`
- Test: `tests/test_attribution.py` (schema section)

**Interfaces:**
- Produces pydantic models with EXACTLY these fields (copied from `aw-notetaker/notetaker-common/notetaker_common/schemas.py`, the shapes `notetaker-worker` reads):
  - `SpeakerEvent(timestamp_ms: int, relative_sec: float, speaker_id: str, speaker_name: str)`
  - `SpeakerInterval(speaker_id: str, speaker_name: str, start_sec: float, end_sec: float)`
  - `TimelineParticipant(id: str, name: str, joined_at: datetime | None = None)`
  - `SpeakerTimelineFile(room_name: str, meeting_id: str, platform: str, recording_started_at: datetime, recording_ended_at: datetime, duration_sec: float, start_time: float, participants: list[TimelineParticipant], speaker_timeline: list[SpeakerEvent], speaker_intervals: list[SpeakerInterval] = [])`
  - `HostInfo(id: str, name: str, email: str | None = None)`
  - `ParticipantInfo(id: str, name: str, email: str | None = None, joined_at: datetime, left_at: datetime | None = None, is_external: bool)`
  - `ParticipantsFile(meeting_id: str, platform: str, host: HostInfo, participants: list[ParticipantInfo])`

- [ ] **Step 1: Failing test** — pin the field sets so drift from the worker contract is loud:
```python
from exporter import schemas


def test_field_sets_match_notetaker_contract() -> None:
    assert list(schemas.SpeakerInterval.model_fields) == [
        "speaker_id", "speaker_name", "start_sec", "end_sec"]
    assert list(schemas.SpeakerTimelineFile.model_fields) == [
        "room_name", "meeting_id", "platform", "recording_started_at",
        "recording_ended_at", "duration_sec", "start_time", "participants",
        "speaker_timeline", "speaker_intervals"]
    assert list(schemas.ParticipantsFile.model_fields) == [
        "meeting_id", "platform", "host", "participants"]
```
- [ ] **Step 2: Run** → FAIL (no module).
- [ ] **Step 3: Implement** `schemas.py` with the models above (`from pydantic import BaseModel, ConfigDict`; `model_config = ConfigDict(populate_by_name=True)` on each, as upstream does).
- [ ] **Step 4: Run** → PASS; black/ruff/mypy clean.
- [ ] **Step 5: Commit** `feat(aw-exporter): notetaker artifact schemas (mirror of notetaker_common)`.

---

### Task 5: Tape parsing → speaker events

**Files:**
- Create: `exporter/tape.py`
- Modify: `tests/conftest.py` (synthetic tape builders)
- Test: `tests/test_tape.py`

**Interfaces:**
- Consumes: Task 2's origin rule.
- Produces:
  - `@dataclass(frozen=True) class TapeEvent: name: str; relative_ms: int; event_type: Literal["SPEAKER_START","SPEAKER_END"]; source: Literal["audio","hint"]`
  - `@dataclass class Tape: lane: str; sample_rate: int; started_at: str | None; frames: list[Frame]; hints: list[Hint]`
  - `parse_tape(lines: Iterable[str]) -> Tape` (skips unparseable lines, never raises on one bad line; raises `ValueError` if no header)
  - `speech_events(tape: Tape, origin_ms: int, rms_threshold: float, hangover_ms: int) -> list[TapeEvent]` (sorted by `relative_ms`, then name)
  - `names(tape: Tape) -> list[str]` (distinct named speakers, first-seen order)

- [ ] **Step 1: conftest builders**
```python
import json


def header(lane: str = "gmeet") -> str:
    return json.dumps({"type": "captured_signal_header", "v": 1, "platform": "google_meet",
                       "lane": lane, "sample_rate": 16000,
                       "started_at": "2026-01-01T00:00:00.000Z"})


def frame(ts: int, name: str | None, rms: float, idx: int = 0,
          samples: int = 4096) -> str:
    row: dict[str, object] = {"seq": ts, "ts": ts, "speakerIndex": idx, "pcm": "",
                              "pcm_len": samples, "rms": rms, "lane": "gmeet"}
    if name is not None:
        row["speakerName"] = name
    return json.dumps(row)


def hint(t: int, name: str, is_end: bool = False) -> str:
    row: dict[str, object] = {"type": "hint", "t": t, "name": name, "lane": "mixed"}
    if is_end:
        row["isEnd"] = True
    return json.dumps(row)
```
(Frame duration = `pcm_len / sample_rate` = 256 ms for 4096 samples.)

- [ ] **Step 2: Failing tests** (`tests/test_tape.py`):
```python
from exporter.tape import parse_tape, speech_events, names
from tests.conftest import frame, header, hint

O = 1_000_000  # origin epoch ms


def ev(e):  # compact view
    return (e.name, e.relative_ms, e.event_type, e.source)


def test_gmeet_one_utterance() -> None:
    t = parse_tape([header(), frame(O + 0, "A", 0.2), frame(O + 256, "A", 0.2),
                    frame(O + 512, "A", 0.0), frame(O + 1600, "A", 0.0)])
    assert [ev(e) for e in speech_events(t, O, 0.05, 700)] == [
        ("A", 0, "SPEAKER_START", "audio"), ("A", 512, "SPEAKER_END", "audio")]


def test_gmeet_gap_shorter_than_hangover_is_one_utterance() -> None:
    t = parse_tape([header(), frame(O, "A", 0.2), frame(O + 256, "A", 0.0),
                    frame(O + 512, "A", 0.2), frame(O + 3000, "A", 0.0)])
    out = [ev(e) for e in speech_events(t, O, 0.05, 700)]
    assert out == [("A", 0, "SPEAKER_START", "audio"), ("A", 768, "SPEAKER_END", "audio")]


def test_gmeet_unnamed_frames_ignored_and_speakers_independent() -> None:
    t = parse_tape([header(), frame(O, None, 0.9), frame(O, "A", 0.2, idx=0),
                    frame(O + 100, "B", 0.2, idx=1), frame(O + 5000, "A", 0.0)])
    out = [ev(e) for e in speech_events(t, O, 0.05, 700)]
    assert ("A", 0, "SPEAKER_START", "audio") in out
    assert ("B", 100, "SPEAKER_START", "audio") in out
    assert all(e[0] in {"A", "B"} for e in out)


def test_gmeet_open_speech_closes_at_last_voiced_time() -> None:
    t = parse_tape([header(), frame(O, "A", 0.2)])
    assert [ev(e) for e in speech_events(t, O, 0.05, 700)][-1] == (
        "A", 256, "SPEAKER_END", "audio")


def test_mixed_lane_hints_are_points() -> None:
    t = parse_tape([header("mixed"), hint(O + 10, "A"), hint(O + 900, "A", is_end=True),
                    hint(O + 1000, "B")])
    assert [ev(e) for e in speech_events(t, O, 0.05, 700)] == [
        ("A", 10, "SPEAKER_START", "hint"), ("A", 900, "SPEAKER_END", "hint"),
        ("B", 1000, "SPEAKER_START", "hint")]


def test_bad_line_skipped_and_names() -> None:
    t = parse_tape([header(), "{not json", frame(O, "A", 0.2), frame(O, "B", 0.0)])
    assert names(t) == ["A", "B"]


def test_no_header_raises() -> None:
    import pytest
    with pytest.raises(ValueError):
        parse_tape([frame(O, "A", 0.2)])
```
- [ ] **Step 3: Run** → FAIL.
- [ ] **Step 4: Implement `tape.py`**

```python
"""captured-signal.v1 tape -> speaker START/END events (spec §4.3)."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

EventType = Literal["SPEAKER_START", "SPEAKER_END"]
Source = Literal["audio", "hint"]


@dataclass(frozen=True)
class Frame:
    ts: int
    name: str | None
    rms: float
    duration_ms: int


@dataclass(frozen=True)
class Hint:
    t: int
    name: str
    is_end: bool


@dataclass(frozen=True)
class TapeEvent:
    name: str
    relative_ms: int
    event_type: EventType
    source: Source


@dataclass
class Tape:
    lane: str
    sample_rate: int
    started_at: str | None
    frames: list[Frame] = field(default_factory=list)
    hints: list[Hint] = field(default_factory=list)


def parse_tape(lines: Iterable[str]) -> Tape:
    tape: Tape | None = None
    for line in lines:
        try:
            row: dict[str, Any] = json.loads(line)
        except (ValueError, TypeError):
            continue
        if row.get("type") == "captured_signal_header":
            tape = Tape(lane=str(row.get("lane", "gmeet")),
                        sample_rate=int(row.get("sample_rate", 16000)),
                        started_at=row.get("started_at"))
            continue
        if tape is None:
            continue
        if row.get("type") == "hint":
            if row.get("name"):
                tape.hints.append(Hint(int(row["t"]), str(row["name"]),
                                       bool(row.get("isEnd", False))))
            continue
        if "ts" in row:
            samples = int(row.get("pcm_len", 0))
            tape.frames.append(Frame(
                ts=int(row["ts"]),
                name=row.get("speakerName") or None,
                rms=float(row.get("rms", 0.0)),
                duration_ms=int(round(samples * 1000 / tape.sample_rate)),
            ))
    if tape is None:
        raise ValueError("tape has no captured_signal_header")
    return tape


def names(tape: Tape) -> list[str]:
    seen: dict[str, None] = {}
    for f in tape.frames:
        if f.name:
            seen.setdefault(f.name)
    for h in tape.hints:
        seen.setdefault(h.name)
    return list(seen)


def speech_events(tape: Tape, origin_ms: int, rms_threshold: float,
                  hangover_ms: int) -> list[TapeEvent]:
    if tape.hints:
        out = [TapeEvent(h.name, h.t - origin_ms,
                         "SPEAKER_END" if h.is_end else "SPEAKER_START", "hint")
               for h in tape.hints]
        return sorted(out, key=lambda e: (e.relative_ms, e.name))

    events: list[TapeEvent] = []
    started: dict[str, int] = {}      # name -> start epoch ms
    last_voiced: dict[str, int] = {}  # name -> end of last voiced frame, epoch ms

    def close(name: str) -> None:
        events.append(TapeEvent(name, last_voiced[name] - origin_ms,
                                "SPEAKER_END", "audio"))
        del started[name]

    for f in sorted(tape.frames, key=lambda fr: fr.ts):
        for name in [n for n in started if f.ts - last_voiced[n] >= hangover_ms]:
            close(name)
        if not f.name or f.rms < rms_threshold:
            continue
        if f.name not in started:
            started[f.name] = f.ts
            events.append(TapeEvent(f.name, f.ts - origin_ms, "SPEAKER_START", "audio"))
        last_voiced[f.name] = f.ts + f.duration_ms
    for name in list(started):
        close(name)
    return sorted(events, key=lambda e: (e.relative_ms, e.name))
```
- [ ] **Step 5: Run** → PASS (adjust expectations only if a test encodes a wrong assumption — say which in the commit body); black/ruff/mypy.
- [ ] **Step 6: Replay the real tape locally (not committed)** — `python -c` over `recordings/signal/…/captured-signal.jsonl` with Task 2's origin + threshold: print per-speaker event counts; speaker A (the speaker who talked only in the first minutes) must have START events before 180 000 ms.
- [ ] **Step 7: Commit** `feat(aw-exporter): captured-signal tape -> speaker events`.

---

### Task 6: Attribution — port of the aw-integration adapter

**Files:**
- Create: `exporter/attribution.py`
- Test: `tests/test_attribution.py`
- Reference (read-only, gitignored): `reference/aw-overrides/aw-integration/aw_integration/adapter.py`, `reference/aw-overrides/aw-integration/tests/test_adapter.py`

**Interfaces:**
- Consumes: `TapeEvent` (Task 5), schemas (Task 4).
- Produces:
  - `build_speaker_timeline(events: list[TapeEvent], *, platform: str, meeting_id: str, room_name: str, recording_started_at: datetime, recording_ended_at: datetime, min_dominant_utterance_ms: int) -> SpeakerTimelineFile`
  - `build_participants(names: list[str], *, platform: str, meeting_id: str, joined_at: datetime, host_email: str | None) -> ParticipantsFile`

Port rules (keep the reference docstrings' reasoning as short comments; drop incident archaeology per AGENTS.md "source states the designed present"):
1. `_slug(name) = name.strip().replace(" ", "_").lower()`.
2. `_build_dominant_speaker_timeline` (reference lines 565-729) verbatim in logic: pair `source=="audio"` START/END into intervals (second START closes the prior; orphan END ignored; still-open closes at session end); collapse with the substantial/shortest rule and earliest-start fallback; suppress consecutive same-speaker points. Returns `(points, intervals)`.
3. If no points from (2) and events exist: point timeline from every `SPEAKER_START` (the reference "safety valve", lines 315-334) — this is the mixed-lane (`hint`) path.
4. t=0 anchor on the earliest point for `platform in ("zoom", "teams")` with ≥ 2 distinct speakers (reference 380-385).
5. Teams (`platform == "teams"`), no paired intervals, ≥ 2 speakers: `_intervals_from_points` (reference 466-519).
6. `speaker_intervals` sorted by `(start, end, name)`; `participants` = `TimelineParticipant(id=slug, name=name)` for each distinct name in first-seen order; `start_time = recording_started_at.timestamp()`; `duration_sec = (ended - started).total_seconds()`.
7. Caption/DOM preference (`_preferred_point_events`) is NOT ported: the 0.12 tape has one hint source per lane. Note this in a one-line comment.
8. `build_participants`: `HostInfo(id="host-0", name=host_email.split("@")[0] if host_email else "", email=host_email)`; one `ParticipantInfo(id=slug, name, joined_at=joined_at, is_external=False)` per name.

- [ ] **Step 1: Port the tests first.** From `reference/…/tests/test_adapter.py`, port every test of `_build_dominant_speaker_timeline`, `_intervals_from_points`, the anchor gate and `build_participants`, rewriting inputs from `VexaSpeakerEvent(relative_ms=…, event_type=…, participant_name=…, source="audio")` to `TapeEvent(name, relative_ms, event_type, "audio")`, and outputs to the new function signatures. Drop tests of Redis reading, `encode_wav`, metadata, and DOM/caption preference. Keep each ported test's expected values unchanged — they are the tuned behaviour. Add at minimum these new ones:
```python
from datetime import datetime, timedelta, timezone

from exporter.attribution import build_participants, build_speaker_timeline
from exporter.tape import TapeEvent

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _tl(events, platform="google_meet", seconds=60):
    return build_speaker_timeline(
        events, platform=platform, meeting_id="vexa-1", room_name="r",
        recording_started_at=T0, recording_ended_at=T0 + timedelta(seconds=seconds),
        min_dominant_utterance_ms=1500)


def test_blip_inside_long_turn_does_not_steal_it() -> None:
    ev = [TapeEvent("A", 0, "SPEAKER_START", "audio"),
          TapeEvent("B", 10_000, "SPEAKER_START", "audio"),
          TapeEvent("B", 10_500, "SPEAKER_END", "audio"),
          TapeEvent("A", 25_000, "SPEAKER_END", "audio")]
    tl = _tl(ev)
    assert [p.speaker_name for p in tl.speaker_timeline] == ["A"]
    assert [(i.speaker_name, i.start_sec, i.end_sec) for i in tl.speaker_intervals] == [
        ("A", 0.0, 25.0), ("B", 10.0, 10.5)]


def test_hint_points_zoom_anchor_needs_two_speakers() -> None:
    one = _tl([TapeEvent("A", 5_000, "SPEAKER_START", "hint")], platform="zoom")
    assert one.speaker_timeline[0].relative_sec == 5.0
    two = _tl([TapeEvent("A", 5_000, "SPEAKER_START", "hint"),
               TapeEvent("B", 9_000, "SPEAKER_START", "hint")], platform="zoom")
    assert two.speaker_timeline[0].relative_sec == 0.0


def test_teams_gets_intervals_from_points() -> None:
    tl = _tl([TapeEvent("A", 1_000, "SPEAKER_START", "hint"),
              TapeEvent("B", 9_000, "SPEAKER_START", "hint")], platform="teams", seconds=20)
    assert [(i.speaker_name, i.start_sec, i.end_sec) for i in tl.speaker_intervals] == [
        ("A", 0.0, 9.0), ("B", 9.0, 20.0)]


def test_empty_events_give_empty_timeline() -> None:
    tl = _tl([])
    assert tl.speaker_timeline == [] and tl.speaker_intervals == []


def test_participants() -> None:
    p = build_participants(["Ann Lee", "Bo"], platform="zoom", meeting_id="vexa-1",
                           joined_at=T0, host_email="host@example.com")
    assert p.host.name == "host" and [x.id for x in p.participants] == ["ann_lee", "bo"]
```
- [ ] **Step 2: Run** → FAIL (no module).
- [ ] **Step 3: Implement `attribution.py`** per the port rules, copying the reference function bodies and changing only field access (`ev.participant_name` → `ev.name`, `ev.relative_ms` unchanged, `self._platform` → `platform` argument, module constant → `min_dominant_utterance_ms` argument). This is AbroadWorks' own code (Apache-2.0 fork), not Attendee.
- [ ] **Step 4: Run** → all ported + new tests PASS; black/ruff/mypy.
- [ ] **Step 5: Commit** `feat(aw-exporter): port speaker attribution from aw-integration onto tape events` (body: spec §4.3; list the dropped reference behaviours and why).

---

### Task 7: I/O adapters — S3, meeting-api, notetaker, ffmpeg

**Files:**
- Create: `exporter/storage.py`, `exporter/vexa_client.py`, `exporter/notetaker.py`, `exporter/audio.py`
- Test: `tests/test_storage.py`, `tests/test_clients.py`

**Interfaces:**
- Produces:
  - `class Storage: __init__(self, client: S3Client)`; `exists(bucket, key) -> bool`; `get_bytes(bucket, key) -> bytes`; `put_bytes(bucket, key, data: bytes, content_type: str) -> None`; `put_json(bucket, key, obj: object) -> None`; `get_json(bucket, key) -> Any | None` (None on 404); `copy(src_bucket, src_key, dst_bucket, dst_key) -> None`; `list_keys(bucket, prefix) -> list[str]` (paginated); `delete(bucket, key) -> None`; `iter_lines(bucket, key) -> Iterator[str]` (streamed).
  - `class MeetingApi: __init__(self, base_url: str, http: httpx.Client)`; `list_recordings(user_id: int, meeting_id: int) -> list[dict[str, Any]]`; `master(user_id: int, recording_id: int) -> dict[str, Any]` (`GET /recordings/{id}/master?type=audio`); `transcript(user_id: int, platform: str, native_id: str) -> dict[str, Any] | None`. Every request sends `X-User-Id: <user_id>`. Non-2xx → `MeetingApiError(status, path)`.
  - `class Notetaker: __init__(self, base_url: str, http: httpx.Client)`; `process(meeting_id: str, s3_path: str, platform: str) -> None` (idempotency_key = meeting_id). Retry: connect errors + 5xx, 4 tries, backoff 2/4/8 s (sleep injectable); 4xx → `NotetakerError` immediately.
  - `webm_to_wav(src: Path, dst: Path) -> None` — runs `ffmpeg -nostdin -y -i src -ac 1 -ar 16000 -c:a pcm_s16le dst`; non-zero exit → `RuntimeError` with the last 20 stderr lines.

- [ ] **Step 1: Failing tests.** `tests/test_storage.py` uses `moto.mock_aws` with two buckets; asserts put/get JSON roundtrip, `get_json` → None on missing key, `copy`, `list_keys` over 1,005 keys (pagination), `iter_lines` yields the lines of a 3-line object. `tests/test_clients.py` uses `httpx.MockTransport`:
```python
import httpx
import pytest

from exporter.notetaker import Notetaker, NotetakerError
from exporter.vexa_client import MeetingApi, MeetingApiError


def test_meeting_api_sends_user_header_and_parses() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"recordings": [{"id": 7}]})

    api = MeetingApi("http://m", httpx.Client(transport=httpx.MockTransport(handler)))
    assert api.list_recordings(user_id=3, meeting_id=9) == [{"id": 7}]
    assert seen[0].headers["X-User-Id"] == "3"
    assert seen[0].url.params["meeting_id"] == "9"


def test_meeting_api_error() -> None:
    api = MeetingApi("http://m", httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(404))))
    with pytest.raises(MeetingApiError):
        api.master(user_id=1, recording_id=2)


def test_notetaker_body_and_retry_on_5xx() -> None:
    calls: list[bytes] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.content)
        return httpx.Response(503 if len(calls) < 3 else 200, json={"status": "accepted"})

    nt = Notetaker("http://n", httpx.Client(transport=httpx.MockTransport(handler)),
                   sleep=lambda s: None)
    nt.process("vexa-1", "recordings/x/", "google_meet")
    assert len(calls) == 3
    import json
    assert json.loads(calls[0]) == {"meeting_id": "vexa-1", "s3_path": "recordings/x/",
                                    "platform": "google_meet", "idempotency_key": "vexa-1"}


def test_notetaker_4xx_not_retried() -> None:
    n = {"c": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        n["c"] += 1
        return httpx.Response(422)

    nt = Notetaker("http://n", httpx.Client(transport=httpx.MockTransport(handler)),
                   sleep=lambda s: None)
    with pytest.raises(NotetakerError):
        nt.process("vexa-1", "recordings/x/", "zoom")
    assert n["c"] == 1
```
Plus `test_webm_to_wav` (skip if `shutil.which("ffmpeg") is None`): generate a 1 s sine webm with ffmpeg (`-f lavfi -i sine=d=1 -c:a libopus`), convert, assert WAV header 16 kHz mono via `wave`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** the four modules to the interfaces above (boto3 `get_paginator("list_objects_v2")`; `copy_object` with `CopySource`; `iter_lines` over `get_object()["Body"].iter_lines()` decoded UTF-8; `httpx` timeouts 30 s; `subprocess.run([...], capture_output=True, text=True, check=False)`).
- [ ] **Step 4: Run** → PASS; black/ruff/mypy.
- [ ] **Step 5: Commit** `feat(aw-exporter): S3, meeting-api, notetaker and ffmpeg adapters`.

---

### Task 8: Export job

**Files:**
- Create: `exporter/job.py`
- Test: `tests/test_job.py`

**Interfaces:**
- Consumes: everything above.
- Produces:
  - `@dataclass class Deps: settings: Settings; storage: Storage; meeting_api: MeetingApi; notetaker: Notetaker; transcode: Callable[[Path, Path], None]; now: Callable[[], datetime]`
  - `@dataclass class ExportResult: state: Literal["handed_off","no_audio","already_done"]; folder: str`
  - `export_meeting(envelope: dict[str, Any], deps: Deps) -> ExportResult` — raises on retryable failure (the queue counts attempts).

Algorithm (spec §4.2) — `m = envelope["data"]["meeting"]`; `folder = folder_name(m["platform"], m["native_meeting_id"], m["start_time"])`; `base = settings.export_prefix + folder + "/"`:
1. `get_json(export_bucket, base + "_export.json")` has `state == "handed_off"` → `already_done`.
2. `recs = list_recordings(m["user_id"], m["id"])`; first recording with a media file of `type == "audio"`; none → put `_export.json {"state":"no_audio", …}` → `no_audio`.
3. `master = meeting_api.master(user_id, rec["id"])`; `storage_path = master["storage_path"]`; `copy(vexa_bucket, storage_path, export_bucket, base + "master.webm")`.
4. In a `tempfile.TemporaryDirectory`: download master bytes, `transcode(webm, wav)`, `put_bytes(… base + "audio.wav", "audio/wav")`.
5. `session_uid = storage_path.split("/")[3]`; tape key `f"signal/{user_id}/{meeting_id}/{session_uid}/captured-signal.jsonl"`; if it exists → `parse_tape(iter_lines(...))`, origin per Task 2, `speech_events(...)`, `build_speaker_timeline(...)` (`room_name = m.get("constructed_meeting_url") or m["native_meeting_id"]`, `recording_started_at` = origin as datetime, `recording_ended_at = m["end_time"]`), `build_participants(names(tape), …, host_email=(m.get("data") or {}).get("organizer_email"))`. Missing tape → timeline with empty lists + participants with no entries, and `_export.json` records `"tape": "missing"`.
6. `put_json` `speaker_timeline.json`, `participants.json`, `meeting.json` (= `m`), `recordings.json` (= `{"recordings": recs}`). If `(m.get("data") or {}).get("transcribe_enabled")`: `transcript(...)` → `live_transcript.json` when not None. If `settings.debug`: copy every key under `signal/{user_id}/{meeting_id}/{session_uid}/` to `base + "signal/" + <basename>`.
7. `notetaker.process(f"vexa-{m['id']}", base, m["platform"])`.
8. `put_json(base + "_export.json", {"state": "handed_off", "vexa_meeting_id": m["id"], "exported_at": now().isoformat(), "exporter_version": __version__, "tape": "ok"|"missing"})` → `handed_off`.

- [ ] **Step 1: Failing tests** with moto buckets + fake `MeetingApi`/`Notetaker` (simple classes recording calls) + `transcode` fake that writes `b"RIFF"`: (a) happy path writes exactly `{master.webm, audio.wav, speaker_timeline.json, participants.json, meeting.json, recordings.json, _export.json}` and calls `process` once with `("vexa-11367", "recordings/google_meet_abc-defg-hij_20260618T100000000Z/", "google_meet")`; (b) re-run after success → `already_done`, no second `process`; (c) no audio recording → `no_audio`, no `process`; (d) `debug=True` copies `signal/*`; (e) `transcribe_enabled` → `live_transcript.json`; (f) notetaker raising → exception propagates and `_export.json` is NOT `handed_off`; (g) missing tape → still hands off, `_export.json.tape == "missing"`. Use the upstream golden envelope shape (`core/meetings/contracts/webhook.v1/golden/Envelope.meeting-completed.json`) with synthetic values.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** `job.py`.
- [ ] **Step 4: Run** → PASS; black/ruff/mypy.
- [ ] **Step 5: Commit** `feat(aw-exporter): per-meeting export job (idempotent)`.

---

### Task 9: Durable queue + HTTP intake

**Files:**
- Create: `exporter/queue.py`, `exporter/app.py`
- Test: `tests/test_queue_app.py`

**Interfaces:**
- Produces:
  - `class PendingQueue: __init__(self, storage: Storage, bucket: str)`; `enqueue(envelope: dict) -> None` (key `aw-exporter/pending/<meeting_id>.json`, stores `{"envelope": …, "attempts": 0}`); `pending_ids() -> list[str]`; `load(id) -> dict | None`; `record_failure(id, error: str) -> int` (increments attempts, returns new count); `done(id) -> None` (delete); `fail(id) -> None` (move to `aw-exporter/failed/`).
  - `async def run_worker(queue: PendingQueue, deps: Deps, stop: asyncio.Event) -> None` — every `sweep_seconds`: for each pending id (bounded by `asyncio.Semaphore(concurrency)`, job run in `asyncio.to_thread`), `export_meeting`; success → `done`; exception → `record_failure`; attempts ≥ `max_attempts` → write `_export.json {"state":"failed","error":…}` and `fail`. Skips an id whose `next_attempt_at` (stored, exponential `2**attempts * 30 s`) is in the future.
  - `def create_app(settings: Settings, queue: PendingQueue, deps: Deps, clock: Callable[[], float] = time.time) -> FastAPI` with `POST /hooks/vexa` (raw body → `verify` → 401 on failure; `event_type != "meeting.completed"` → 200 `{"status":"ignored"}`; enqueue → 202; enqueue error → 503) and `GET /healthz` → 200. Lifespan starts `run_worker`.

- [ ] **Step 1: Failing tests** (FastAPI `TestClient`, moto): unsigned → 401; signed `bot.failed` → 200 ignored, nothing queued; signed `meeting.completed` → 202 + pending object exists; S3 put failing (patch `enqueue` to raise) → 503; worker: one pending envelope + fake job success → pending gone; fake job always raising with `max_attempts=2` → after two sweeps the item is under `failed/` and `_export.json.state == "failed"`; restart semantics: a fresh `PendingQueue` over the same bucket sees the still-pending id.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** `queue.py`, `app.py` (+ `exporter/__main__.py`: `Settings.from_env(os.environ)`, build boto3/httpx clients, `uvicorn.run(create_app(...), host="0.0.0.0", port=8080)`).
- [ ] **Step 4: Run** → PASS; black/ruff/mypy.
- [ ] **Step 5: Commit** `feat(aw-exporter): signed webhook intake + S3-backed durable queue`.

---

### Task 10: Container image + compose integration test

**Files:**
- Create: `integrations/out/aw-notetaker/Dockerfile`, `tests/integration/test_compose_flow.py`, `tests/integration/stub_notetaker.py`

- [ ] **Step 1: Dockerfile**
```dockerfile
FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml ./
COPY exporter/ exporter/
RUN pip install --no-cache-dir .
USER 65534
EXPOSE 8080
CMD ["python", "-m", "exporter"]
```
Record the ffmpeg package's license in `image-licenses.json` if the image-licensing gate requires it (check the gate output; Debian's ffmpeg is GPL-enabled — if the gate refuses GPL in images, STOP and surface).
- [ ] **Step 2: Build** `docker build -t aw-exporter:dev integrations/out/aw-notetaker` → succeeds; `docker run --rm aw-exporter:dev ffmpeg -version | head -1` prints a version.
- [ ] **Step 3: Integration test** — local MinIO (`quay.io/minio/minio`) with buckets `aw-bots` + `aw-chatworks-transcribe`; seed a synthetic master.webm (ffmpeg sine) at `recordings/1/2/sess-1/audio/master.webm` and a synthetic tape at `signal/1/99/sess-1/captured-signal.jsonl`; a stub meeting-api (FastAPI, returns the recordings/master JSON pointing at that path) and `stub_notetaker.py` (records `/process` bodies); run the exporter container against them; POST a correctly signed envelope; poll until `_export.json` is `handed_off`; assert the exact key set and the stub's recorded body. Mark `@pytest.mark.integration`; run with `pytest -m integration`.
- [ ] **Step 4: Commit** `feat(aw-exporter): container image + compose integration test`.

---

### Task 11: Live validation on the compose stack (human bar: solo/small-group meeting)

**Files:** none committed except findings in the spec §9 and the PR's observation bundle.

- [ ] **Step 1:** Bring up upstream compose (`deploy/compose`) with `RUNTIME_BACKEND=docker`, `TRANSCRIBE_ENABLED=false`, `RECORDING_ENABLED=true`, `VEXA_SYSTEM_WEBHOOK_URL=http://aw-exporter:8080/hooks/vexa`, `VEXA_SYSTEM_WEBHOOK_SECRET`/`VEXA_WEBHOOK_SECRET` set to the same locally generated value (never printed), `VEXA_SYSTEM_WEBHOOK_ALLOW_PRIVATE_HTTP=true`, and the exporter + stub notetaker on the same network. MinIO stands in for both buckets.
- [ ] **Step 2:** Plan a meeting via `POST /meetings` (`scheduled_at` = now + 3 min, a real Meet link); confirm auto-join spawns before start (record spawn → lobby timestamps).
- [ ] **Step 3:** Two humans speak in turn, one of them in the first 60 s; end the meeting.
- [ ] **Step 4:** Verify: the folder exists with the spec §3 key set; `participants.json` has both names (**this is the transcription-OFF name check**); `speaker_timeline.json` intervals line up with who spoke (spot-check 5 turns against `master.webm`); the stub got one `/process`. Record the bot container's peak memory/CPU (`docker stats`) for spec §7.1.
- [ ] **Step 5:** Two meetings at once (two Meet links) → two folders, no cross-talk. Then one Zoom and one Teams meeting (mixed lane).
- [ ] **Step 6:** Update the spec §9 with measured results; commit `docs(aw-exporter): live validation findings`.

---

### Task 12: Docs and hand-off

- [ ] **Step 1:** Changelog fragment `docs/changelog.d/aw-exporter.md` only if the docs-current gate demands it for this folder (AGENTS.md hot-file rule); otherwise none.
- [ ] **Step 2:** In the aw-notetaker repo (separate commit there, with approval): `CLAUDE.md` branch convention → `feat/<topic>` / `fix/<topic>` for vexa-fork (observed practice); record the rearchitecture direction and the `development` branch.
- [ ] **Step 3:** Full check: `pytest -q && black --check . && ruff check . && mypy exporter` in the package; `node scripts/gates.mjs all` vs the baseline.
- [ ] **Step 4:** With explicit user approval only: `git push -u origin feat/aw-rearchitecture` and open a PR into `development` with the observation bundle from Task 11.

---

## Self-review

- Spec coverage: §3 folder (Tasks 3, 8), §4.1 intake (Tasks 3, 9), §4.2 job (Task 8), §4.3 attribution (Tasks 2, 5, 6), §4.4 config (Task 3), §6 testing (Tasks 3–11), §9 risks measured (Tasks 2, 11). §5 portal and §7 deployment are other plans by design.
- Decision points that can halt the plan: Task 1 Step 6 (gate needs a core change), Task 2 Step 4 (no epoch within 250 ms), Task 10 Step 1 (GPL ffmpeg refused by the image gate).
