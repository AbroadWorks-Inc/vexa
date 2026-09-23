# AW rearchitecture — Vexa as the meeting platform, notetaker as the transcription pipeline (design)

Date: 2026-09-23 · Branch: `feat/aw-rearchitecture` (from `feat/aw-recording-lite`) · PR target: `development`

## 1. Context and goal

The portal lets users sign in with Google, brings in their calendar meetings, and a bot must be in
each meeting **before it starts** (Fireflies-style). The old cloud pipeline (calendar-dispatcher →
bot-orchestrator → per-platform bot images on Vexa v0.10.4, `reference/aw-overrides/`) is broken and
is kept only as a reference for *what* is needed — its contracts are not preserved.

New direction: **use what upstream Vexa (v0.12, full stack, k8s runtime backend) already offers and
extend it**, and keep transcription + summary in the existing talke `notetaker-worker` →
`transcriber` (faster-whisper large-v3, GPU) pipeline shared with Jitsi.

What Vexa gives us unchanged:

| Need | Vexa feature |
|---|---|
| Bot joins before start | `POST /meetings {scheduled_at, meeting_url}` → auto-join sweep spawns ahead of time (`AUTO_JOIN_LEAD_S`, `AUTO_JOIN_GRACE_S`, dedup, backoff) — `bot_spawn/auto_join.py`, `docs/docs/api/meetings.mdx#plan-a-meeting` |
| Concurrent bots | k8s runtime backend: one Pod per bot (`deploy/helm/charts/vexa`, `runtime.backend: k8s`) |
| Cheap elastic infra | bot Pods pinned to our Karpenter NodePool via `runtime.nodeSelector`/`runtime.tolerations` (→ `RUNTIME_K8S_*`), sized by `runtime.workloadResources.meetingBot` (request = limit) |
| Users / keys / caps | admin-api users + API keys, per-user `max_concurrent_bots` |
| Recording | browser MediaRecorder → webm chunks → S3 → master on read |
| Meeting-finished signal | system webhook `meeting.completed` (HMAC-signed, Redis retry + dead letter) |
| Live transcription | exists; **off by default** for us (`TRANSCRIBE_ENABLED=false`), per-bot override |

What we add:
1. **Portal → Vexa** (aw-notetaker repo): the portal pushes planned meetings from each user's Google
   Calendar (existing OAuth) into Vexa under one Vexa service account.
2. **aw-exporter** (this repo, `integrations/out/aw-notetaker/`): on `meeting.completed`, build the
   per-meeting folder and hand it to `notetaker-worker`.
3. **Deployment config** (deployment track): values/env listed in §7.

## 2. Decisions

| # | Decision | Status |
|---|---|---|
| D1 | Live transcription is a setting, default **off** (`TRANSCRIBE_ENABLED=false`); `transcribe_enabled` on `POST /bots`/plans still overrides. | agreed |
| D2 | Transcript + summary come from `notetaker-worker` `/process` (one pipeline for Meet/Zoom/Teams/Jitsi; model changes isolated). Vexa 0.12 has no post-meeting transcription. | agreed |
| D3 | No `audio_chunks/` in the export. Kept audio = `master.webm`; `audio.wav` is the worker's transient input. | agreed |
| D4 | Vexa storage = bucket **`aw-bots`** (`recordings/`, `signal/`, later `userdata/`); product folder = `aw-chatworks-transcribe/recordings/`. | agreed |
| D5 | `aw-bots` lifecycle: expire `recordings/` + `signal/` after **14 days**; `userdata/` never. Exporter deletes nothing. | agreed |
| D6 | Guest-join bots now; signed-in bots later (one stored session per concurrent bot). | agreed |
| D7 | Trigger = Vexa system webhook (`VEXA_SYSTEM_WEBHOOK_URL`/`_SECRET`). | agreed |
| D8 | Spawning/timing = Vexa auto-join (no custom dispatcher/orchestrator). Bot Pods on Karpenter. | agreed |
| D9 | Old folder contract `recordings/{platform}_{event_id}_{job_id}/` is dropped; new name in §3. | agreed |
| D10 | Portal keeps Google OAuth and pushes plans via `POST /meetings` (not Vexa ICS sync, which needs each user to paste a secret iCal URL). | agreed |
| D11 | ONE Vexa service account owns every meeting; portal users keep their own Google-login accounts and the portal decides who sees which meeting. Vexa's dedup is per (user, platform, native id) (`bot_spawn/adapters.py:492`), so a single account is what stops shared internal meetings getting one bot per attendee. Its `max_concurrent_bots` is raised with growth. | agreed |
| D12 | Exporter reads meeting-api **in-cluster** with `X-User-Id` = the webhook's `meeting.user_id` (the header the gateway injects after key auth); meeting-api is reachable only from the pods that must call it: the gateway, the exporter, the runtime, agent-api and the bot pods, which upload recordings and signal files through it (NetworkPolicy; the manifest is in the aw-notetaker repo, `deployment/base/aw-exporter/networkpolicy.yaml`). | agreed |

## 3. Output folder

```
s3://aw-chatworks-transcribe/recordings/<platform>_<nativeMeetingId>_<startUTC>/
  master.webm              kept    — Vexa audio master (opus)
  audio.wav                temp    — 16 kHz mono PCM; notetaker-worker deletes it after transcribing
  meeting.json             kept    — Vexa meeting row (webhook data.meeting)
  recordings.json          kept    — Vexa recording index for this meeting
  participants.json        kept    — names observed speaking (from speaker-activity.jsonl)
  speaker_timeline.json    kept    — speaker_timeline points + speaker_intervals (worker's mapper input)
  notes.json, transcript.txt, summary.json[, transcript_en.txt]   — written by notetaker-worker
  live_transcript.json     only if the bot ran with live transcription on
  signal/                  only when EXPORT_DEBUG=1 (botlog, captured-signal, …)
  _export.json             kept    — exporter marker (state, speaker_activity[_events], audio_recordings, timings); written last
```
- Every object the exporter writes here carries a `retention-class` S3 tag
  (`exporter/retention.py`) that the bucket's lifecycle rules expire against — an untagged
  object never expires: `master.webm` → `recording-mp4` (30 days); `audio.wav` and the debug
  `signal/*` copies → `audio` (7 days, they can contain audio); every JSON (`meeting.json`,
  `recordings.json`, `participants.json`, `speaker_timeline.json`, `live_transcript.json`,
  `_export.json` incl. the queue's failure marker) → `metadata` (365 days). `notetaker-worker`'s
  own `summary.json` gets a fourth class, `summary` (7 years), that the exporter does not write.
  Objects the exporter writes into the **Vexa** bucket (`aw-exporter/pending/`, `failed/`) are
  NOT tagged — that bucket has its own prefix lifecycle.
- `platform` = Vexa's value (`google_meet`, `zoom`, `teams`), passed unchanged to `/process`
  (any non-`jitsi` value takes the worker's bot path).
- `nativeMeetingId` reduced to `[A-Za-z0-9.-]` (anything else → `-`), so `_` only separates fields.
- `startUTC` = meeting `start_time` as `YYYYMMDDTHHMMSSmmmZ` (e.g. `20260922T170234432Z`).
- The name is a pure function of the Vexa meeting row, so the portal computes it without a lookup.
- `transcript.json` from the original request = the worker's `notes.json.transcript_segments` +
  `transcript.txt` (the portal already reads these).

## 4. aw-exporter

Python 3.11 service: FastAPI + uvicorn, httpx, boto3, pydantic; `ffmpeg` binary in the image. One
replica; per-meeting jobs are independent.

### 4.1 Intake — `POST /hooks/vexa`
- Verify `X-Webhook-Signature: sha256=HMAC(secret, "<X-Webhook-Timestamp>." + raw_body)` with
  `hmac.compare_digest`; reject timestamps older than 300 s; fail closed if the secret is unset.
- `meeting.completed` → enqueue; any other event → 200 no-op (logged).
- Durable enqueue: `PUT s3://aw-bots/aw-exporter/pending/<meeting_id>.json` (the envelope), then
  **202**. Enqueue failure → 503 so Vexa's retry queue redelivers.
- Worker loop (`EXPORT_CONCURRENCY`, default 4) drains the pending prefix; on startup and every
  `EXPORT_SWEEP_SECONDS` (default 60) it re-lists it, so a restart mid-job resumes. The pending
  object is removed only when the job reaches a terminal state.

### 4.2 Job (idempotent; key = Vexa meeting id)
1. `<folder>/_export.json` with `state=handed_off` exists → drop pending, done.
2. meeting-api `GET /recordings?meeting_id=<id>` (header `X-User-Id: <user_id>`). No audio →
   `_export.json {state:"no_audio"}`, done.
3. `GET /recordings/<rid>/master?type=audio` → Vexa assembles the master (upstream finalize-on-read)
   and returns `storage_path`; server-side copy `aw-bots/<storage_path>` → `<folder>/master.webm`.
4. `ffmpeg -nostdin -i master.webm -af aresample=async=1:first_pts=0 -ac 1 -ar 16000 -c:a
   pcm_s16le audio.wav` → upload. **`-af aresample=async=1:first_pts=0` is mandatory** (not a
   plain `-i ... -ac 1 -ar 16000` decode): a plain decode drops Opus DTX (discontinuous
   transmission) gaps, non-linearly compressing the wav timeline — measured on the 2026-09-22
   recording, a plain decode produced 1901.50 s against a 1935.65 s packet PTS span (the
   gap-filled decode reproduces 1935.62 s, matching the PTS span; ~34 s dropped across ~220
   inter-packet gaps) — which silently shifts every downstream speaker-interval timestamp
   computed relative to this wav's t=0. See §4.3 clock origin.
5. `speaker-activity.jsonl` (`aw-bots/signal/<user_id>/<meeting_id>/<session_uid>/speaker-activity.jsonl`,
   session uid from `storage_path`) → speaker events (§4.3) → `speaker_timeline.json`,
   `participants.json`. This is aw-bots' own always-on who-spoke-when file — no audio in it — and
   the *only* source of names; there is **no fallback** to the debug capture tape
   (`captured-signal.jsonl`, off by default, §7). See
   [speaker-activity design](2026-09-23-speaker-activity-design.md). **Activity wait:** aw-bots
   uploads it in its teardown, *after* it emits `meeting.completed`, so an absent file is not yet
   "missing". While `now < meeting.end_time + ACTIVITY_WAIT_SECONDS` (default 120; naive
   `end_time` read as UTC) the job raises a retryable `ActivityNotReady` before copying anything
   or calling `/process`, and the queue's backoff re-runs it. Past the deadline (or with no
   `end_time`, which gives no fixed deadline) it proceeds without it. Problems degrade
   attribution, never fail the export — the resulting state is recorded in
   `_export.json.speaker_activity`:
   - `ok` — parsed; events used. A header-only file (nobody spoke before the bot left, or the
     meeting ended before anyone did) is also `ok`, with zero events — not an error; so is a file
     with a valid header whose other rows partly fail to parse (those rows are skipped, so it
     yields fewer events). An `ok` file that yields zero events while `audio.wav` is longer than
     180 s logs the WARNING `speaker_activity_empty vexa_meeting_id=<id> audio_s=<n>` — someone
     almost certainly spoke, yet the bot recorded nobody.
   - `missing` — still absent after the wait → empty timeline/participants; logged as the ERROR
     `speaker_activity_missing` (§9).
   - `invalid` — present but has no `speaker_activity_header` line, or cannot be read/parsed at
     all → empty timeline/participants, warning logged.
   - `capped` — parsed and used, but the file itself carries aw-bots' own `{"type":"capped"}`
     marker line (written once, when `VEXA_SPEAKER_ACTIVITY_MAX_BYTES` is reached; nothing after
     it was written), so attribution may stop before the meeting ended; warning logged. This is
     read from the marker, not computed from the file's byte size.
   `_export.json.speaker_activity_events` records how many speaker events were derived (0 for
   `missing`/`invalid`), so an empty-but-`ok` export is visible without opening the timeline.
   The timeline's `recording_ended_at` / clip bound is `recording_started_at` + the transcoded
   `audio.wav`'s own duration (not `meeting.end_time`), so intervals never outrun the audio.
   `room_name` = `data.constructed_meeting_url`, else top-level `constructed_meeting_url`, else
   the native id. Audio moves through local temp files (`master.webm` download, `audio.wav`
   upload), never as whole-file bytes in memory.
6. Write `meeting.json`, `recordings.json`; `live_transcript.json` if the meeting had
   `transcribe_enabled`; copy `signal/*` if `EXPORT_DEBUG=1`.
7. `POST {NOTETAKER_URL}/process {meeting_id:"vexa-<id>", s3_path:"recordings/<folder>/",
   platform:<platform>, idempotency_key:"vexa-<id>"}`; backoff retry on connect errors/5xx.
8. `_export.json {state:"handed_off", vexa_meeting_id, exported_at, elapsed_s, exporter_version,
   speaker_activity, speaker_activity_events, audio_recordings}` last; delete pending. `audio_recordings` counts the recordings with
   an audio media file; when it is > 1 (a multi-session meeting) a warning is logged and only
   the first is exported (see §9).

After `EXPORT_MAX_ATTEMPTS` (default 5, exponential backoff): `_export.json {state:"failed", error}`,
pending moved to `aw-exporter/failed/`. `aw-bots` is never modified, so a re-run is always possible.

### 4.3 Speaker attribution (port of the reference adapter, new event source)
The attribution rules in `reference/aw-overrides/aw-integration/aw_integration/adapter.py` were
tuned on live meetings and are **ported, not re-invented**: dominant-speaker collapse with
`MIN_DOMINANT_UTTERANCE_MS` (default 1500), raw paired intervals emitted as `speaker_intervals`,
the Zoom/Teams t=0 anchor gated on ≥2 named speakers, Teams intervals-from-points, and the file
shapes of `notetaker_common.schemas` (`SpeakerTimelineFile`, `ParticipantsFile`). Only the INPUT
changes: v0.10.4's Redis `speaker_events_relative` stream has no writer in 0.12, and the debug
capture tape (`captured-signal.jsonl`) caps at `VEXA_CAPTURE_SIGNAL_MAX_BYTES` and stops carrying
names once it does, so events come from aw-bots' own always-on `speaker-activity.jsonl` — no
fallback to the tape (§4.2 step 5, [speaker-activity design](2026-09-23-speaker-activity-design.md)):

- **gmeet lane** (Meet — per-frame `{t, ch, name?, rms, dur_ms}`): per speaker, a frame with
  `rms ≥ RMS_SPEECH_THRESHOLD` opens/extends speech; `SPEECH_HANGOVER_MS` (default 700, the bot's
  silence hangover) of sub-threshold frames closes it → `SPEAKER_START`/`SPEAKER_END` with
  `source="audio"` (the trusted, pairable provenance).
- **mixed lane** (Zoom/Teams — `{type:"hint", t, name, isEnd}`): each hint → a point event
  (`SPEAKER_START`, or `SPEAKER_END` when `isEnd`), `source="hint"` (point-only; not paired).
- **Clock origin (re-measured 2026-09-22, fix round 1 — PINNED, N=1, see caveat):** the round-1
  measurement was invalidated by a plain-decode artifact: `ffmpeg -i master.webm -ac 1 -ar 16000
  ...` drops Opus DTX (discontinuous transmission) gaps, non-linearly compressing the wav
  timeline (packet PTS span 1935.65 s vs. a plain-decode 1901.50 s wav; the gap-filled decode
  reproduces 1935.62 s, matching the PTS span — ~34 s dropped across ~220 inter-packet gaps),
  which explained both the weak correlation and the ~32 s disagreement between analysis windows.
  Re-decoded with the **mandatory** timestamp-faithful filter — `ffmpeg -nostdin -i master.webm
  -af aresample=async=1:first_pts=0 -ac 1 -ar 16000 -c:a pcm_s16le` (1935.619 s) — and re-ran the
  same mean-centered (Pearson) NCC over ±120 s, this time spreading each tape frame's `rms` across
  its own duration (`pcm_len/16000 s`) onto the 50 ms grid instead of dropping it into one bin.
  Result: whole-recording NCC peak = **0.389, z≈10.4** (second-best only 0.122 — clearly
  dominant, not near-tied); first-180s-only NCC peak = **0.601, z≈17.9**. **Both windows now
  agree exactly** (same +35,250 ms lag, well inside the 100 ms agreement bar), resolving the
  round-1 instability. Measured origin: **2026-09-22T17:02:34.937Z**. Residual against
  `recording.created_at − 15,000 ms` (the record-chunker's own `MediaRecorder(...,
  timeslice=15000ms)`; `recording.created_at` is written once chunk_seq=0 finishes uploading,
  i.e. structurally one full timeslice after `MediaRecorder` start — not a constant fitted to
  this recording) = **−125.5 ms**, inside the 250 ms bar. **Pinned rule:** `origin_epoch =
  recording.created_at − RECORD_CHUNK_TIMESLICE_MS` (default 15000; must match the bot's
  `MediaRecorder` timeslice). This is the only candidate confirmed within 250 ms among the
  runtime-available fields (tape header `started_at` −35,250 ms; `meeting.start_time` −505 ms;
  `service_provenance.bot_admitted_at` −531 ms; raw `recording.created_at` +14,875 ms all miss);
  Task 5 hard-codes this rule rather than computing the lag live per meeting.
  **Caveat — this is an N=1 measurement, not a validated constant:** the −125.5 ms residual is
  not pure timeslice offset; it also absorbs chunk-0's upload latency, since
  `recording.created_at` is stamped when chunk_seq=0 *finishes uploading*, not at the instant
  `MediaRecorder` reaches the 15,000 ms boundary — the true relationship is `MediaRecorder start
  + one timeslice + chunk-0 upload latency`. Known failure modes this single recording cannot
  rule out: (a) **variable upload latency** — a slower or queued chunk-0 upload on a different
  recording (worse network, backend contention) would widen the residual, since that latency is
  network-dependent, not fixed; (b) **a changed `MediaRecorder` timeslice** — the rule only holds
  if `RECORD_CHUNK_TIMESLICE_MS` tracks whatever timeslice the bot is actually configured with; a
  drift between the two silently reintroduces this exact class of error. **Task 11's live
  meetings must re-measure this residual on additional recordings before the rule is treated as
  fixed**; if a wider spread of residuals turns up there, this bullet needs another revision.
  `RMS_SPEECH_THRESHOLD` default: **0.026** (unchanged — the tape's own rms histogram is
  unaffected by the wav decode fix; valley between a silence mode ≈0.005 and a speech mode
  ≈0.108). Meet frames confirmed to carry `speakerName` (6 distinct named speakers observed;
  notably including a speaker missing from the transcript-derived `participants.json` due to
  that meeting's STT degradation — confirms the audio-lane `speakerName` is the correct,
  transcript-independent dependency for attribution).

### 4.4 Config (names only)
`MEETING_API_URL`, `VEXA_WEBHOOK_SECRET`, `VEXA_BUCKET` (aw-bots), `EXPORT_BUCKET`
(aw-chatworks-transcribe), `EXPORT_PREFIX` (recordings/), `NOTETAKER_URL`, `EXPORT_DEBUG`,
`EXPORT_CONCURRENCY`, `EXPORT_SWEEP_SECONDS`, `EXPORT_MAX_ATTEMPTS`, `RMS_SPEECH_THRESHOLD`,
`SPEECH_HANGOVER_MS`, `MIN_DOMINANT_UTTERANCE_MS`, `RECORD_CHUNK_TIMESLICE_MS` (default 15000),
`ACTIVITY_WAIT_SECONDS` (default 120, replaces `TAPE_WAIT_SECONDS`), `AWS_REGION`. S3 via IRSA
(no static keys). aw-bots' own safety ceiling for `speaker-activity.jsonl`,
`VEXA_SPEAKER_ACTIVITY_MAX_BYTES` (default 128 MiB — about 3× the ~40 MB upper estimate for a
3-hour meeting, and small enough to upload inside the bot's 8 s teardown bound in-cluster), is a
bot-pod env var, not an exporter one — see §7 and the
[speaker-activity design](2026-09-23-speaker-activity-design.md).

## 5. Portal → Vexa (aw-notetaker repo; own plan)
- One Vexa service account (created once by an operator via admin-api); its API key lives in a K8s
  Secret the portal reads. `max_concurrent_bots` on that account ≥ the NodePool's planned peak.
- Calendar poll (existing OAuth read) → for each upcoming event with a Meet/Zoom/Teams link:
  `POST /meetings {title, scheduled_at, meeting_url}`; `409` = the meeting is already planned (another
  attendee's calendar got there first) → link this portal user to the existing Vexa meeting.
  Changes/cancels via the plan-edit routes; a cancel from one attendee must not cancel a meeting other
  attendees still have.
- The portal stores Vexa meeting id ↔ portal users (visibility) and reads `notes.json` /
  `transcript.txt` at the §3 prefix computed from the Vexa meeting.
- Meeting pages read `notes.json`/`transcript.txt` at the §3 prefix computed from the Vexa meeting.
- Retires calendar-dispatcher's spawn path and bot-orchestrator.

## 6. Testing
- Unit (pytest): signature (good/bad/stale/missing secret), event filter, folder naming/sanitising,
  speaker-activity → events for both lanes (synthetic speaker-activity files — no real participant
  audio or names in fixtures),
  ported attribution rules (port the relevant reference tests), idempotency, pending-sweep resume.
- Integration (compose): MinIO as both buckets, stub `/process`; replay a signed envelope; assert the
  folder byte-for-byte.
- Live: Meet meeting on compose with transcription off → folder + stub call; then real worker on EKS;
  then one Zoom and one Teams meeting (mixed lane).

## 7. Deployment config (deployment track)
- meeting-api: `TRANSCRIBE_ENABLED=false`, `RECORDING_ENABLED=true`, `MINIO_BUCKET=aw-bots`,
  `S3_ENDPOINT=https://s3.<region>.amazonaws.com` (no static keys → IRSA),
  `VEXA_SYSTEM_WEBHOOK_URL=http://aw-exporter.<ns>.svc.cluster.local:8080/hooks/vexa`,
  `VEXA_SYSTEM_WEBHOOK_SECRET` (Secret), `VEXA_SYSTEM_WEBHOOK_ALLOW_PRIVATE_HTTP=true`,
  `AUTO_JOIN_LEAD_S` sized to cover node provisioning + bot image pull + browser boot (see risks).
- **Deploy order: meeting-api → bot → exporter.** A new bot against an old meeting-api gets a 422
  on the `speaker-activity` signal part (the file is lost); the new exporter against old bots finds
  no `speaker-activity.jsonl` and has no fallback, so every meeting exports `missing`.
- **Rollout step, in the same change that ships the new bot:** admin-api platform diagnostics
  `capture_signal=false` — the debug capture tape (`captured-signal.jsonl`) off by default for every
  meeting, since naming no longer depends on it (§4.2 step 5, §4.3). This is required, not a later
  nicety: meeting-api's signal janitor evicts whole `signal/<u>/<m>/<s>/` prefixes, which include
  `speaker-activity.jsonl` (§9). The alternative is raising the janitor budget
  (`SIGNAL_TAPE_BUDGET_BYTES`). Switch the tape on per platform or per user only to investigate a
  specific meeting (`_resolve_capture_signal`, `core/identity/services/admin-api`).
- runtime: `nodeSelector`/`tolerations` → the bots Karpenter NodePool `aw-bots-bots` (label and taint `workload=aw-bots-bots`; manifest in aw-notetaker `deployment/base/aw-bots/bots-nodepool.yaml`);
  `workloadResources.meetingBot` per §7.1.

### 7.1 Bot sizing and NodePool (applied once the bot is functional)
Measured on the OLD bot (v0.10.4, 85 meetings): memory 0.48 GiB (bot alone) → **1.82 GiB peak**
(11+ participants); CPU 0.21 → **0.88 core peak**. Re-measure on the 0.12 bot during live validation
(it runs per-speaker capture and writes the signal tape) before locking values.

- **Per bot (Guaranteed QoS, request = limit):** `cpu: 1`, `memoryMb: 2560`. Memory is a hard OOM
  ceiling, so it carries ~40 % headroom over the 1.82 GiB peak; the chart default 2048 leaves ~11 %.
  CPU over the limit only throttles. Local disk: ≥ 1 GiB ephemeral for `speaker-activity.jsonl`
  (≤ 128 MiB) + the debug tape when switched on (≤ 250 MB, `DEFAULT_MAX_TAPE_BYTES`) + browser
  profile.
- **Load (assuming the 165k Meet + 29k Zoom minutes are per MONTH):** 194k min ≈ 3,230 bot-hours/month.
  Over ~22 working days × 10 h that is **~15 concurrent bots on average**; top-of-hour clustering
  gives a planning peak of **~45**, burstable to ~60. Load grows with onboarding: NodePool `limits`
  and the service account's `max_concurrent_bots` are the two knobs to raise.
- **NodePool:** `karpenter.sh/capacity-type: on-demand` only (**never spot**); general-purpose
  families gen ≥ 6 (m6i/m7i, also c/m for Karpenter's cheapest-fit), sizes 2xlarge–4xlarge. Per
  bot at 1 vCPU / 2.5 GiB, `m*.2xlarge` fits ~7 bots and `m*.4xlarge` ~15 (CPU-bound), which is
  cheaper per bot than compute-optimised (`c*.2xlarge` fits ~5, memory-bound). Peak ≈ 3–4 × 4xlarge
  or 7 × 2xlarge. Check whether the bot image ships arm64 (Graviton is ~20 % cheaper).
- **Never evict a live bot:** bot Pods carry labels only (`runtime_kernel/k8s_backend.py:196`), so
  `karpenter.sh/do-not-disrupt` cannot be set without patching core. Use
  `disruption.consolidationPolicy: WhenEmpty` and `expireAfter: Never` on the bots NodePool (nodes
  leave only when empty); revisit a pod-annotation patch only if the empty-node waste proves costly.
- **Cold start:** bot image cached on node storage from S3 (the transcriber pattern) and/or a small
  warm floor during business hours — a deployment task after the bot is functional.
- Order-of-magnitude compute: ~3,230 bot-hours × ~0.06 USD per bot-hour (on-demand, well packed)
  ≈ 200 USD/month before packing loss and any warm floor. An estimate to check, not a quote.
- IAM: meeting-api rw `aw-bots/{recordings,signal}/*`; exporter r `aw-bots/*`, rw
  `aw-bots/aw-exporter/*`, rw `aw-chatworks-transcribe/recordings/*` **plus
  `s3:PutObjectTagging` on `aw-chatworks-transcribe`** (the exporter tags every object it
  writes there with its retention class, §3). Lifecycle per D5.
- Network: exporter → meeting-api (internal), → `notetaker-api.notetaker:8080`; NetworkPolicy per D12.
- Exporter Deployment: single replica with `strategy: Recreate` — a rolling update would briefly
  run two pods, i.e. two overlapping pending-queue sweepers.
- Alerting + recovery: alert on intake 401/400 responses — Vexa's webhook delivery drops non-429
  4xx permanently, so a secret mismatch or a rejected envelope loses that meeting's trigger. Keep
  a backfill path: list completed meetings from meeting-api and re-enqueue them (the job is
  idempotent on the Vexa meeting id).

## 8. Out of scope
Signed-in bots; Jitsi via Vexa (Jibri path unchanged); `notetaker-worker` changes; `/process`'s
single-pod in-process queueing (noted); EKS manifests for the Vexa stack itself.

## 9. Risks
- **Join-before-start on a cold node:** Karpenter provisioning (~1 min) + a ~3.6 GB bot image +
  browser boot can exceed the default 120 s lead. Handled in deployment (§7.1: S3-cached image /
  warm floor), then `AUTO_JOIN_LEAD_S` is set from a measured cold start. Too large a lead on guest
  joins knocks on an empty lobby (the old bot hit this on 2026-08-19).
- **Bots never run on spot** (§7.1) — a reclaim would kill the meeting.
- **Memory sizing is from the old bot** — re-measured on 0.12 before §7.1 values are locked.
- **Clock alignment** of tape vs master (S1 measures it).
- **Speaker names with transcription off** — seen only on a transcription-on tape; verified early.
- meeting-api trusting `X-User-Id` makes network isolation load-bearing (D12).
- Mixed-lane hint density for Zoom/Teams untested here — validated live.
- One service account serialises spawns through Vexa's per-user advisory lock (held for the
  create transaction only) — measured at a top-of-hour burst during live validation.
- `speaker-activity.jsonl` must be present for names to survive — alert on the ERROR log
  `speaker_activity_missing` (§4.2 step 5). The export still succeeds either way:
  `_export.json.speaker_activity` records `"missing"`, and the timeline/participants come back
  empty rather than failing the job.
- **The signal janitor can evict `speaker-activity.jsonl`.** meeting-api's janitor
  (`recordings/signal_janitor.py`: 50 GiB budget, oldest first, minimum age 600 s) deletes whole
  `signal/<u>/<m>/<s>/` prefixes, and `speaker-activity.jsonl` lives in that prefix. While the debug
  tape is ON (`capture_signal` defaults ON) only about 200 sessions fit the budget, so a late retry
  or a backfill can find the file gone and export `missing`. Mitigation: `capture_signal=false` as
  a rollout step (§7), or a larger `SIGNAL_TAPE_BUDGET_BYTES`.
- Multi-session meetings (the bot rejoined → several audio recordings) export only one session;
  the count is recorded in `_export.json.audio_recordings` and logged, the other sessions' audio
  is not exported.
- With one Vexa service account (D11), every meeting's recordings JSONB lives under one user, so
  the recordings load meeting-api does per request grows with the total number of meetings.
- Task 11 must also measure: the mixed-lane (Zoom/Teams) clock-origin residual (§4.3's rule is
  measured on Meet only), the speaker-activity upload lag after `meeting.completed` (to size
  `ACTIVITY_WAIT_SECONDS`), speaker-activity MB/hour (to check `VEXA_SPEAKER_ACTIVITY_MAX_BYTES`
  and the bot pods' ephemeral disk), and the exporter's memory on a long meeting.
