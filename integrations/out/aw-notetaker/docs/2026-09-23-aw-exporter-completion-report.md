# aw-exporter — completion report

Date: 2026-09-23 · Branch: `feat/aw-rearchitecture` (local, **not pushed**) · Range: `7b71ab6c..3455c289` (21 commits, 41 files, all under `integrations/out/aw-notetaker/`)

Read this next to:
- **Plan:** [`2026-09-23-aw-exporter-plan.md`](2026-09-23-aw-exporter-plan.md) — task numbers below are the plan's.
- **Spec:** [`2026-09-23-aw-rearchitecture-design.md`](2026-09-23-aw-rearchitecture-design.md) — binding authority; it was updated during execution (see §3 below).

## 1. Summary

| | |
|---|---|
| Tasks done | 1–10 of 12. **11 (live meetings) and 12 (push/PR) wait on you** — §5. |
| Tests | 178 unit tests pass; black, ruff and mypy (strict) clean. 1 integration test passes against the real image, MinIO and ffmpeg. |
| Repo gates | No new failures from this package. Gates already red before this work: 6 because `uv` isn't installed here, and `gate:readme` on the gitignored `recordings/` and `reference/`. |
| Core changes | None. Nothing under `core/`, `clients/` or `deploy/` changed. |
| Code | `aw_exporter/` is 1,593 lines across 14 modules. |

What it does, end to end:
1. Vexa sends its signed `meeting.completed` system webhook to `POST /hooks/vexa`.
2. The exporter verifies the signature on the raw bytes, validates the envelope, and saves the meeting to `aw-bots/aw-exporter/pending/<id>.json`. Only then does it answer 202.
3. A worker loop builds `recordings/<platform>_<nativeId>_<startUTC>/` in the export bucket: `master.webm`, `audio.wav` (16 kHz mono, gaps filled), `meeting.json`, `recordings.json`, `participants.json` and `speaker_timeline.json`.
4. It then calls `notetaker-worker` `/process` and writes the `_export.json` marker last.

Failures back off and retry. After 5 attempts the meeting moves to `failed/`. The worker can't die from a single bad meeting.

## 2. Task by task

| Plan task | What was built | How it was verified | Commits | Differs from plan? |
|---|---|---|---|---|
| 1 Scaffold + gate baseline | Package, `pyproject.toml`, smoke test, READMEs | Gate baseline before/after (no new reds); pytest/black/ruff/mypy | `1d68dd26` `d0dd9010` | Added READMEs in `aw_exporter/`, `tests/`, `docs/` (upstream `gate:readme` needs one in every folder) |
| 2 Timing spike | Measured how tape timestamps map onto the audio's t=0, on the 2026-09-22 Meet recording (throwaway scripts, not committed) | Energy cross-correlation over two windows that agree exactly (lag +35 250 ms, z 10.4 / 17.9) | `683c89ce` `1069394b` `e04a3836` | **Yes, big one** — §3 D1 |
| 3 Config / naming / signature | `Settings.from_env`, `folder_name`, `verify` (HMAC, 300 s window, fails closed) | 16 tests, written test-first | `f65e0537` | Speech threshold default 0.026 (measured, not 0.01); added `RECORD_CHUNK_TIMESLICE_MS` |
| 4 Output schemas | Pydantic mirror of the `notetaker_common` file formats | Field-order test + round-trip test, checked field by field against `notetaker_common/schemas.py` | `552de54a` | Tests in `test_schemas.py`, not `test_attribution.py` |
| 5 Tape → events | `parse_tape`, `speech_events` (Meet: loudness + 700 ms hangover; Zoom/Teams: hints) | 12 tests + a local replay of the real tape (235 events, 5 speakers; the early speaker's speech is found) | `e2436104` `44d1e0ad` | Bad lines are skipped instead of crashing; events chosen by lane, not by "has hints" (both bugs were in the plan's own code) |
| 6 Attribution port | The tuned rules from the old `aw-integration` adapter, now fed by tape events | 50 reference tests ported with unchanged expected values, 8 new; reviewed line by line against the reference | `bfdbe243` | 20 reference tests not ported (platform remapping, segment fallback, DOM/caption preference — none apply to 0.12 tapes); results clipped to the recording |
| 7 I/O adapters | S3 `Storage`, `MeetingApi` (`X-User-Id`), `Notetaker` (retries), `webm_to_wav` | moto S3, `httpx.MockTransport`, real-ffmpeg test | `71a3a67e` `23158fe1` | Transcript read by meeting id; retry on every transport error (timeouts too); ffmpeg gap-filling flag |
| 8 Export job | `export_meeting` (idempotent), `recording_origin_ms` | 9 job tests on moto buckets | `d087527a` | Participants deduped by slug; `meeting_id` = `vexa-<id>` in the JSON files |
| 9 Queue + intake | S3-backed `PendingQueue`, `sweep_once` / `run_worker`, FastAPI app, `__main__` | 23 tests, including the worker surviving bad meetings and redelivery | `5a24828b` `79a47266` | Envelope validation at intake, logging, a repeat delivery doesn't reset the retry count, dead in-flight guard removed |
| 10 Image + integration test | Dockerfile (`python:3.11-slim` + apt ffmpeg, non-root), `.dockerignore`, end-to-end test | Built image; MinIO + stub meeting-api/notetaker; two-speaker tape; exact key set and `/process` body checked | `ce08667b` `c4c6fce0` | MinIO endpoint via boto3's `AWS_ENDPOINT_URL_S3` (no code change) |
| Final review fixes | Wait for the late tape; tape states; streaming; multi-session count; 400s for non-object bodies | Whole-branch review, then one fix wave and one re-review (all addressed) | `dbebd77e` `2d2b338f` `c4c6fce0` `3455c289` | §3 D2–D4 |
| 11 Live validation | — | — | — | **Waiting on you** (§5) |
| 12 Docs / push / PR | This report | — | this commit | Push and PR wait on you (§5) |

## 3. Deviations from the plan and why

**D1 — The plan's ffmpeg command was wrong. It drops silence and shifts every timestamp (Task 2).**
- The plan decoded with `ffmpeg -i master.webm -ac 1 -ar 16000`. The browser's Opus recorder writes no packets during silence, and a plain decode closes those gaps.
- Measured: the WebM's own timestamps span 1935.65 s; the plain decode came out at 1901.50 s; the gap-filling decode (`-af aresample=async=1:first_pts=0`) gave 1935.62 s. About 34 s went missing across roughly 220 gaps.
- Effect: the WAV sent to `notetaker-worker` would have drifted up to ~34 s from real time. That drift breaks speaker attribution and would also have skewed transcript times.
- With the fixed decode, the measurement pinned one rule: **audio t=0 = `recording.created_at` − 15 000 ms**. The residual is −125.5 ms, from a single recording, so Task 11 must re-measure it.
- The spec now requires the gap-filling flag (§4.2 step 4) and `aw_exporter/audio.py` uses it.

**D2 — Wait for the capture tape (final review, Critical).**
- The Vexa bot uploads the capture tape during teardown, after it emits `meeting.completed`.
- Fix: the job now waits for the tape, up to `TAPE_WAIT_SECONDS` (120) after `end_time`, and only then records `tape: "missing"`. This wasn't in the plan.

**D3 — Tape problems now reduce attribution instead of failing the export (final review).**
- The tape is recorded in `_export.json` as `ok`, `missing`, `invalid` or `capped`.
- `capped` means the tape reached Vexa's 250 MB per-meeting limit: at the measured density that's about 50 minutes of meeting.
- `master.webm` and `audio.wav` are streamed through disk rather than held in memory.

**D4 — Spec additions (§7, §9) not foreseen by the plan.** Deployment:
- raise `VEXA_CAPTURE_SIGNAL_MAX_BYTES` on bot pods and size their local disk to match;
- deploy the exporter with `strategy: Recreate`;
- alert on intake 401/400, because Vexa drops those deliveries for good;
- keep a backfill path.

Risks recorded:
- the signal janitor can delete a tape before export;
- a meeting with several sessions exports only the newest (counted in `_export.json`);
- reading a meeting's recordings gets slower as the single account's total meeting count grows;
- the measurements Task 11 still owes.

Smaller deviations are listed in §4, with the reason and cost for each.

## 4. Every ruling I made on your behalf

Each line: what I decided — why — what it costs if wrong.

**Setup**
- **P1** — Worked on `feat/aw-rearchitecture` in the main checkout, not a separate worktree. Implementers staged explicit paths only, because your uncommitted `.gitignore` edit and `docs/call_analysis_22.09.2026.txt` are in that tree. — Cost if wrong: an accidental commit, undone with a soft reset. (It never happened; checked after each task.)
- **P2** — Added `.superpowers/` (the plan-execution scratch folder) to the local, uncommitted `.git/info/exclude`. — Cost: none.
- **P3** — The timing rule measured in Task 2 went into Task 8 as a concrete function. — Cost: Task 8 rework.
- **P4 → T3a** — Speech threshold default 0.026 (measured) instead of 0.01; added `RECORD_CHUNK_TIMESLICE_MS`. — Cost: trivial.
- **P5** — `Notetaker(…, sleep=)` is injectable, matching the plan's tests. — Cost: none.
- **P6** — Synthetic tape builders live in `tests/builders.py`, not `conftest.py`. — Cost: trivial.
- **P7** — Task 11 needs people in a live meeting, so it's handed to you. — Cost: n/a.
- **P8** — Editing `aw-notetaker/CLAUDE.md` is a change to another repo, so I'm asking first. — Cost: n/a.
- **G1** — Implementers may not use `git stash`, which could sweep your uncommitted `.gitignore`. — Cost: none.

**Tasks**
- **T1a** — The missing `docs/` README was this branch's fault, so Task 1 fixed it. — Cost: one README.
- **T2a** — Every WebM → WAV conversion uses `aresample=async=1:first_pts=0` (D1). — Cost: one more spike round.
- **T3b** — Commit `f65e0537` is signed `Co-Authored-By: Claude Haiku 4.5` instead of the session's attribution line. I left it rather than rewriting history. — Cost: one reword if the branch is rewritten anyway (§5).
- **T5a** — Tape events can start slightly before audio t=0 (−8 ms seen), so intervals are clipped to the recording. — Cost: tiny attribution loss in the first milliseconds.
- **T5b** — Fixed two bugs in the plan's own tape code: bad lines crashed the parse, and events were chosen by "has hints" instead of by lane. — Cost: small rework.
- **T6a** — Participants are deduped by slug, as the old adapter did, so the roster matches the timeline. — Cost: duplicate participant ids.
- **T7a** — The live transcript is read with `GET /transcripts/by-id/{id}`, because one room link can host several meetings. — Cost: a route change.
- **T7b** — `/process` is retried on every transport error, timeouts included. It's safe to repeat: the worker dedups by `idempotency_key`. — Cost: a duplicate call the worker ignores.
- **T8a** — `origin_ms = created_at − RECORD_CHUNK_TIMESLICE_MS` drives both the speaker events and `recording_started_at`. — Cost: an attribution offset.
- **T8b** — The JSON files use `meeting_id = "vexa-<id>"`, the same id `/process` receives. — Cost: a string change.
- **T9a** — Queue hardening:
  1. Envelopes missing required fields get 400 at intake.
  2. The give-up path can't raise.
  3. The worker loop can't die.
  4. A repeat delivery keeps its retry count.
  5. Logging covers ids and counts only.
  6. The dead in-flight guard is removed.
  7. The S3 enqueue is off the event loop.

  — Cost: small rework.
- **T10a** — ffmpeg from apt in the image is allowed. Upstream's gate (`scripts/gates.mjs:466`) treats apt tools as mere aggregation. — Cost: one manifest entry.
- **T10b** — The integration test reaches MinIO through boto3's standard endpoint env var with dummy credentials. It never touches your `aw-recording-*` containers. — Cost: test rework.
- **T10 parked** — Reviewer worried the boto3 ≥1.34 floor predates endpoint-env support. It doesn't: support arrived in 1.28. — Cost: none.

**Final review**
- **F1** — The fix wave in D2/D3 plus the minor items:
  - `end_time` parsing treats a timestamp with no timezone as UTC;
  - intervals are clipped to the WAV's real length;
  - `room_name` comes from `data.constructed_meeting_url`;
  - JSON bodies that aren't objects get 400;
  - the marker gains `exported_at`, `elapsed_s`, `tape` and `audio_recordings`;
  - the README lists every env var.

  — Cost: one more fix wave.
- **F2** — A missing `end_time` means no tape wait. This overrules my F1: using "now" as the deadline would move it on every retry and use up all attempts. — Cost: a rare early `missing`.
- **Parked:**
  - `recording_origin_ms` reads a `created_at` with no timezone as local time. Vexa always sends `Z`, so it's harmless in practice.
  - Tape-wait retries use up queue attempts: at most 2 of 5 with the defaults.

**Deferred minors** (for later, none blocking):
- The Dockerfile base and the test's MinIO image are unpinned.
- The image is 639 MB, mostly ffmpeg's apt dependencies.
- Each sweep waits for its slowest job.
- `transcript()` duplicates the other requests' code.
- `copy_object` has a 5 GB ceiling; masters are ~30 MB.
- A stub test thread doesn't warn if it fails to stop.
- Intake validation accepts a falsy non-string such as `0`.
- Report and commit prose contains small miscounts.

## 5. What needs you

1. **A real participant's name is in one commit message** (`e2436104`, body). I removed it from every file, but history still has it.
   - Before any push, rewrite this unpushed branch (for example, squash the 21 commits into a few), or squash-merge the PR so the message never reaches `development`.
   - A rewrite also fixes the Haiku attribution line on `f65e0537`. I won't rewrite history without your approval.
2. **Task 11: live validation (plan Task 11).**
   - It needs the full Vexa compose stack. Its `.env` holds real credentials I'm not allowed to read, so you'd set it up or approve the approach. Settings: `TRANSCRIBE_ENABLED=false`, the system webhook pointing at the exporter, and the stub or real notetaker.
   - Then two people in a Meet meeting, one of them speaking in the first minute, plus one Zoom and one Teams meeting.
   - It measures: the timing residual on more recordings and on Zoom/Teams, how long after `completed` the tape upload lands, tape MB per minute, and bot memory/CPU for the NodePool sizing.
3. **Push and PR into `development`** once item 1 is settled.
4. **`aw-notetaker/CLAUDE.md`** (another repo): record the actual branch convention (`feat/`, `fix/`) and `development` as the working branch, if you want that.

## 6. How to run

```bash
cd integrations/out/aw-notetaker
/opt/homebrew/bin/python3.11 -m venv .venv && . .venv/bin/activate && pip install -e '.[dev]'
pytest -q && black --check . && ruff check . && mypy aw_exporter     # 178 passed
pytest -m integration -q tests/integration                          # needs Docker
docker build -t aw-exporter:dev .
```
Env var names are listed in `README.md` and spec §4.4 (names only; no values in the repo).
