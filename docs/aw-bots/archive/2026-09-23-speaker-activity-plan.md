# Speaker activity file — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** aw-bots always writes and uploads a small, audio-free `speaker-activity.jsonl` (who was talking when). The exporter builds speaker names from it alone, so long meetings keep their names.

**Architecture:**
- A new writer module in the bot is fed at the two existing capture tap points, independently of the debug tape.
- It is uploaded at teardown as a new signal part, which meeting-api accepts.
- The exporter swaps its tape reader for an activity reader. The attribution rules and the clock origin are unchanged.

**Tech stack:** Bot = TypeScript (Node 20, `tsx` test scripts). meeting-api = Python 3.11 + pytest. Exporter = Python 3.11 (existing package).

**Spec:** [`2026-09-23-speaker-activity-design.md`](2026-09-23-speaker-activity-design.md). The parent spec [`2026-09-23-aw-rearchitecture-design.md`](2026-09-23-aw-rearchitecture-design.md) covers §4.2–§4.3.

## Global Constraints

- File name `speaker-activity.jsonl`; signal part name `speaker-activity`; header `type` = `"speaker_activity_header"`, `v` = 1.
- Line shapes are exactly as in the design's table. **No audio samples** in the file.
- Safety ceiling env `VEXA_SPEAKER_ACTIVITY_MAX_BYTES`, default `1073741824`. When reached, write ONE `{"type":"capped","t","bytes"}` line, then stop writing.
- The writer and taps must NEVER throw into capture. Every failure is swallowed and logged at most 5 times.
- The debug tape's behaviour is unchanged: same files, same gating, same upload.
- No fallback: the exporter reads only `speaker-activity.jsonl`. It never reads `captured-signal.jsonl`.
- Missing activity after the wait → exporter logs at ERROR with the stable text `speaker_activity_missing vexa_meeting_id=<id>`, state `missing`, and still hands off.
- Source comments state the designed present, never history (AGENTS.md).
- Git: work in the primary checkout on `feat/aw-rearchitecture`.
  - Never `git stash`/`reset`/`checkout -- <file>`/`rebase`/`amend`.
  - `git add` explicit paths only. Never stage `.gitignore` or `docs/call_analysis_22.09.2026.txt`.
  - One Conventional Commit per task, ending exactly with `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`.
- Checks:
  - bot: `cd core/meetings/services/bot && npx tsc --noEmit -p . && npx tsx src/<test>.ts`;
  - exporter: `pytest -q && black --check . && ruff check . && mypy exporter` from `integrations/out/aw-notetaker`;
  - repo: `node scripts/gates.mjs all`, with no NEW reds versus `.superpowers/sdd/2026-09-23-aw-exporter-plan/gates-baseline.txt`.
- No real participant names or audio in tests; synthetic names only.

## File Structure

```
core/meetings/services/bot/src/
  speaker-activity.ts            NEW  createSpeakerActivityWriter + makeSpeakerActivityTap/hint helper
  speaker-activity.test.ts       NEW  writer + tap tests
  capture-bridge.ts              EDIT feed frames/hints to the writer (independent of the tape)
  index.ts                       EDIT create writer always; close + upload at teardown
  signal-upload.ts               EDIT TapePart += 'speaker-activity'; uploadSpeakerActivity()
  signal-upload.test.ts          EDIT tests for the new upload
  teardown-sidecars.test.ts      EDIT the "every part the walk attempted is one the server knows" list
core/meetings/services/bot/package.json   EDIT add speaker-activity.test.ts to "test"
core/meetings/services/meeting-api/src/meeting_api/recordings/jsonb.py   EDIT SIGNAL_TAPE_PARTS
core/meetings/services/meeting-api/tests/test_signal_tapes.py            EDIT
integrations/out/aw-notetaker/exporter/
  activity.py                    NEW  (replaces tape.py) parse_activity / speech_events / names
  tape.py                        DELETE
  job.py, config.py              EDIT read speaker-activity; rename settings
  tests/test_activity.py         NEW  (replaces test_tape.py); tests/builders.py EDIT
docs: the parent spec §4.2/§4.3/§4.4/§7/§9; completion report; docs/changelog.d fragment (if gate requires)
```

---

### Task 1: The writer (bot)

**Files:** Create `core/meetings/services/bot/src/speaker-activity.ts` and `speaker-activity.test.ts`. Modify `core/meetings/services/bot/package.json` (append `&& tsx src/speaker-activity.test.ts` before `&& tsx mock/mock.test.ts`).

**Interfaces — produces:**
```ts
export interface SpeakerActivityWriter {
  path: string;
  frame(ch: number, pcm: Float32Array, ts: number, name?: string): void;   // never throws
  hint(t: number, name: string, isEnd?: boolean): void;                    // never throws
  isCapped(): boolean;
  close(): Promise<void>;                                                  // flush + drain; idempotent
}
export interface SpeakerActivityOptions { dir?: string; maxBytes?: number; flushMs?: number;
  maxBufferBytes?: number; log?: (m: string) => void; now?: () => number; }
export const DEFAULT_MAX_ACTIVITY_BYTES = 1024 * 1024 * 1024;
export function resolveMaxActivityBytes(raw?: string): number;   // env VEXA_SPEAKER_ACTIVITY_MAX_BYTES; invalid/empty → default
export function createSpeakerActivityWriter(inv: Invocation, opts?: SpeakerActivityOptions): SpeakerActivityWriter;
```

- [ ] **Step 1: Write the failing test** `speaker-activity.test.ts` in the repo's tsx style (a `check(name, cond)` helper, a `failed` counter, and `process.exit(failed ? 1 : 0)` at the end, as in `teardown-sidecars.test.ts`). The cases:
  1. **Header + a named frame + a hint.** Create with `dir` = `mkdtempSync` and a fixed `now`. Call `frame(0, Float32Array.of(0.5,-0.5,0.5,-0.5), 1000, 'Ann')` and `hint(2000, 'Bo', true)`, then `await close()`. The file has exactly 3 lines:
     - line 1 has `type:"speaker_activity_header"` and `v:1`, with `lane:"gmeet"` for `platform:'google_meet'`;
     - line 2 deep-equals `{t:1000, ch:0, name:'Ann', rms:0.5, dur_ms:0}` (4 samples at 16 kHz round to 0 ms);
     - line 3 deep-equals `{type:'hint', t:2000, name:'Bo', isEnd:true}`.
  2. **Unnamed frame** → no `name` key. A hint without `isEnd` → no `isEnd` key.
  3. **The line never contains `pcm`.** Assert `!raw.includes('pcm')`.
  4. **Ceiling.** With `maxBytes` = header length + 60, write 10 frames. The last line is `{type:'capped', …}`, it appears exactly once, `isCapped()` is true, and nothing follows it.
  5. **Lane.** `platform:'zoom'` or `'teams'` → header `lane:"mixed"` (use `isMixedLanePlatform` from `config.ts`).
  6. **Resilience.** `dir` set to a path under an existing FILE (so mkdir fails) → `frame`/`hint`/`close` do not throw.
  7. **Order.** 1,000 frames written quickly come out in call order (check the `t` sequence) after `close()`.
  8. **Env.** `resolveMaxActivityBytes('')` === `DEFAULT_MAX_ACTIVITY_BYTES`; `('abc')` → default; `('2048')` → 2048.
- [ ] **Step 2: Run** `npx tsx src/speaker-activity.test.ts` → fails (module missing).
- [ ] **Step 3: Implement** `speaker-activity.ts`:
  - File name: `${inv.connectionId ?? inv.nativeMeetingId ?? 'session'}.speaker-activity.jsonl` under `opts.dir ?? process.env.VEXA_CAPTURE_SIGNAL_DIR ?? '/tmp/captured-signal'`.
  - Writing the header: `mkdirSync(dir,{recursive:true})` then `appendFileSync` the header line, inside try/catch. On failure the writer is disabled: every method becomes a no-op.
  - Frames and hints are buffered and flushed on a serialized promise chain, every `flushMs` (default 2000; timer `unref`) or once `maxBufferBytes` is reached (default 256 KiB). `close()` flushes and awaits the chain. This mirrors the recorder in `telemetry.ts` `createCaptureSignalRecorder` (`flush` and `admit`).
  - `rms` is `Math.round(rmsOf(pcm) * 1e5) / 1e5`, using `rmsOf` from `capture-bridge.ts`. `dur_ms` is `Math.round(pcm.length / 16)` (16 kHz capture).
  - Ceiling: the header counts toward `written`. The first line that would exceed `maxBytes` is replaced by the `capped` line (written even if it slightly exceeds the ceiling), then `capped = true`, and `signalEvent('speaker-activity-capped', {path, max_bytes, bytes})` is emitted from `telemetry.ts`.
  - Logging: `console.log('[bot] speaker-activity: …')`, at most 5 fault lines.
  - One `signalEvent('speaker-activity-started', {path, max_bytes, platform})` at creation.
- [ ] **Step 4: Run** the test → PASS. Run `npx tsc --noEmit -p .` → clean.
- [ ] **Step 5: Commit** `feat(bot): speaker-activity writer — who-spoke-when without audio`.

---

### Task 2: Wire it into capture and teardown (bot)

**Files:** Modify `capture-bridge.ts`, `index.ts`, `signal-upload.ts`, `signal-upload.test.ts`, `teardown-sidecars.test.ts`. Add tap tests to `speaker-activity.test.ts`.

**Interfaces:**
- **Consumes:** `SpeakerActivityWriter` (Task 1).
- **Produces:**
  - `startCaptureBridge(page, inv, pipeline, telemetry?, onChat?, activity?, speakerActivity?: SpeakerActivityWriter)`: a new LAST optional parameter;
  - `makeSpeakerHintSink(pipeline, warn?, telemetry?, speakerActivity?)`: a new last optional parameter;
  - `export async function uploadSpeakerActivity(writer: SpeakerActivityWriter | null, opts: SignalUploadOptions): Promise<'uploaded'|'failed'|'skipped'>`, which never throws;
  - `TapePart` gains `'speaker-activity'`.

- [ ] **Step 1: Failing tests.**
  - **Tap tests** (in `speaker-activity.test.ts`):
    - `makeSpeakerHintSink(pipelineStub, noop, undefined, writer)` receiving a hint writes a hint line even though `telemetry` is `undefined`, and still calls `pipeline.recordHint`.
    - Hint skew re-stamping applies to the written `t`, as it does for the tape.
  - **Upload tests** (in `signal-upload.test.ts`, using the file's existing injected `upload` spy pattern):
    - `uploadSpeakerActivity(writer, {inv, upload: spy})` uploads part `'speaker-activity'` with the writer's path when the file is non-empty.
    - It returns `'skipped'` when there is no `recordingUploadUrl` or the writer is `null`.
    - It returns `'failed'` (and doesn't throw) when the spy rejects.
    - It works while `uploadSignalTapes(null, …)` uploads nothing, which proves it's independent of the tape.
  - **Server-known list** (in `teardown-sidecars.test.ts`): add `speaker-activity` to the list of parts the server knows.
- [ ] **Step 2: Run** those three test files → the new checks fail.
- [ ] **Step 3: Implement.**
  - **`capture-bridge.ts`:**
    - In `startCaptureBridge`, build `const recordActivity = (ch, pcm, ts, name?) => { try { speakerActivity?.frame(ch, pcm, ts, name); } catch { } }`.
    - Call it next to `tee(...)` in both `onPerSpeakerAudio` (no name) and `onNamedAudio` (with `glowName`).
    - Pass `speakerActivity` to `makeSpeakerHintSink`, which calls `speakerActivity?.hint(t, name, isEnd)` in its own try/catch, after the skew guard.
  - **`index.ts`:**
    - Just after `signalRecorder` is created, add `const speakerActivity = createSpeakerActivityWriter(inv);`. This is **not** gated on `captureSignalEnabled`.
    - Pass it to `startCaptureBridge(..., remoteAudioActivity, speakerActivity)`.
    - In `finally`, after `pipeline.stop()`: `await speakerActivity.close().catch(() => {});` then `await uploadSpeakerActivity(speakerActivity, { inv });`. Both happen **before** `signalRecorder?.close()` and `uploadSignalTapes`, so the small, important file ships first inside the grace window.
  - **`signal-upload.ts`:**
    - `uploadSpeakerActivity` reuses `streamingTapeUploader`. Its size guard is `resolveMaxActivityBytes()`.
    - It emits the same `signalEvent` names the tape uses (`tape-uploaded` / `tape-upload-failed` / `tape-upload-skipped`, with `part:'speaker-activity'`).
    - Add `'speaker-activity'` to `TapePart` and to the skipped list only where that list names all parts.
- [ ] **Step 4: Run** `npx tsc --noEmit -p .`. Then run `npx tsx` on each of `speaker-activity.test.ts`, `signal-upload.test.ts`, `teardown-sidecars.test.ts`, `capture-bridge.boundary.test.ts`, `speaker-hints.test.ts`, `telemetry.test.ts` and `telemetry-recorder.test.ts` → all pass.
- [ ] **Step 5: Commit** `feat(bot): always capture + upload speaker activity, independent of the debug tape`.

---

### Task 3: meeting-api accepts the part

**Files:** Modify `core/meetings/services/meeting-api/src/meeting_api/recordings/jsonb.py` (`SIGNAL_TAPE_PARTS`, ~line 191) and `core/meetings/services/meeting-api/tests/test_signal_tapes.py`.

- [ ] **Step 1: Failing tests** in `test_signal_tapes.py`:
  - `signal_tape_key(..., part="speaker-activity", fmt="jsonl")` returns `signal/<owner>/<meeting>/<session>/speaker-activity.jsonl`.
  - The parametrised "every part shares one prefix" test includes it.
  - An upload with `part="speaker-activity"` is accepted, not given 422.
- [ ] **Step 2: Run** with a throwaway venv outside the repo. Expect them to fail.
  ```bash
  /opt/homebrew/bin/python3.11 -m venv $SCRATCH/ma-venv && . $SCRATCH/ma-venv/bin/activate
  pip install -q "fastapi>=0.110,<1" "httpx>=0.27,<1" "jsonschema>=4.21,<5" "referencing>=0.34,<1" "redis>=5,<6" "fakeredis>=2.21,<3" "croniter>=2,<4" python-multipart "pytest>=8" "pytest-asyncio>=0.23,<2" "icalendar>=6,<7" "python-dateutil>=2.9,<3" sqlalchemy
  cd core/meetings/services/meeting-api && python -m pytest -q tests/test_signal_tapes.py
  ```
  (Add any further import the tests need to the pip line; report what was needed.)
- [ ] **Step 3: Implement:** add `"speaker-activity"` to the tuple (its format is jsonl by default).
- [ ] **Step 4: Run** the file, plus any other test that pins the part tuple (grep for `SIGNAL_TAPE_PARTS` under `tests/`) → pass.
- [ ] **Step 5: Commit** `feat(meeting-api): accept the speaker-activity signal part`.

---

### Task 4: The exporter reads speaker activity (no fallback)

**Files:**
- Create `exporter/activity.py` and `tests/test_activity.py`. Delete `exporter/tape.py` and `tests/test_tape.py` with `git rm`.
- Modify `exporter/job.py`, `exporter/config.py`, `tests/builders.py`, `tests/test_job.py`, `tests/test_naming.py` (settings defaults), `tests/integration/test_compose_flow.py`, `README.md` and `exporter/README.md`.

**Interfaces:**
- `activity.py` keeps the dataclasses `Frame`, `Hint`, `TapeEvent`.
  - Rename `TapeEvent` → `ActivityEvent`, and update `attribution.py`'s import and annotations. The fields are unchanged.
  - Rename the container `Tape` → `Activity`, with fields `lane`, `started_at`, `frames`, `hints`, `capped: bool`.
- `parse_activity(lines) -> Activity`:
  - The header is `type == "speaker_activity_header"`; a missing header raises `ValueError`.
  - A frame line (has `t`, no `type`) becomes `Frame(ts=int(t), name=row.get("name") or None, rms=float(rms), duration_ms=int(dur_ms))`.
  - A hint line becomes `Hint(t, name, isEnd)`.
  - A `{"type":"capped"}` line sets `capped=True`.
  - A bad line is skipped (KeyError/ValueError/TypeError), never raised.
- `speech_events(activity, origin_ms, rms_threshold, hangover_ms)` and `names(activity)`: same behaviour as today's tape versions (lane-keyed).
- Settings: `tape_wait_seconds` → `activity_wait_seconds` (env `ACTIVITY_WAIT_SECONDS`, default 120). Remove `tape_max_bytes` / `TAPE_MAX_BYTES`.

- [ ] **Step 1: Failing tests.**
  - Port every `tests/test_tape.py` case to the new line shapes. Update the builders:
    - `header(lane)` gives `{"type":"speaker_activity_header","v":1,"lane":…}`;
    - `frame(t, name, rms, ch=0, dur_ms=256)` gives `{"t","ch","name"?,"rms","dur_ms"}`;
    - `hint(t, name, is_end)` gives `{"type":"hint",…}`.
  - Keep the expected values.
  - Add: a `capped` line sets `activity.capped`, and nothing after it is used.
  - Add: a frame without `dur_ms` is skipped.
  - In `test_job.py`:
    - seed `signal/<user>/<meeting>/<session>/speaker-activity.jsonl`;
    - `_export.json` has `speaker_activity` ∈ {`ok`,`missing`,`invalid`,`capped`} (there is no `tape` key any more);
    - missing past the deadline → ERROR log containing `speaker_activity_missing vexa_meeting_id=`;
    - a `captured-signal.jsonl` present WITHOUT `speaker-activity.jsonl` → `missing`, which proves there is no fallback;
    - absent before the deadline → `ActivityNotReady` (renamed from `TapeNotReady`).
  - The integration test seeds `speaker-activity.jsonl` instead of the tape.
- [ ] **Step 2: Run** `pytest -q` → failures.
- [ ] **Step 3: Implement.**
  - The job's key is `signal_prefix + "speaker-activity.jsonl"`.
  - The size check uses `storage.size` only to learn whether the file exists; `capped` now comes from the file's own marker line.
  - Replace the old `TapeState` with `ActivityState`.
  - Add the ERROR log for `missing`.
  - Update `README.md`/`exporter/README.md` (module table, env names) and delete `tape.py`/`test_tape.py`.
- [ ] **Step 4: Run** `pytest -q && black --check . && ruff check . && mypy exporter` and `pytest -m integration -q tests/integration` → pass.
- [ ] **Step 5: Commit** `feat(exporter): names come from speaker-activity.jsonl only`.

---

### Task 5: Docs + gates

- [ ] **Parent spec:**
  - §4.2 step 5 and §4.3: activity file instead of tape; states; no fallback.
  - §4.4: env names.
  - §7: turn the debug tape off by default (admin-api platform diagnostics `capture_signal=false`), and remove the "raise `VEXA_CAPTURE_SIGNAL_MAX_BYTES`" item.
  - §9: the tape-size risk is replaced by "the activity file must be present; alert on `speaker_activity_missing`".
- [ ] **Completion report:** new §7 "Speaker activity (addendum)", covering what was built, how it was verified, and deviations.
- [ ] **Changelog fragment:** `docs/changelog.d/<slug>.md`, following `docs/changelog.d/README.md`, if `node scripts/gates.mjs all` (docs-current) asks for one because `core/` changed. Follow each gate's error text. Any new red beyond the baseline must be fixed or reported.
- [ ] **Commit** `docs(aw-bots): speaker activity replaces the debug tape for naming`.

---

### Final: whole-change review, then live validation (you)

Whole-branch review of this plan's commits (most capable model) → one fix wave → one re-review.

Then **live (you):** one long Meet call with the debug tape OFF. Confirm:
- `speaker-activity.jsonl` is in S3;
- its size per hour;
- names on the transcript through the whole call;
- the upload landed within the grace window.
