# Zoom and Teams: missing audio, missing or distorted speaker intervals, and a bot that will not leave

- **Date:** 2026-10-01.
- **Status:** diagnosis complete from the S3 artefacts and the code; fixes specified, not started.
- **Scope:** aw-bots (`aw-notetaker/vexa-fork`, the bot's recording and capture paths) and aw-exporter (`integrations/out/aw-notetaker/exporter`). Nothing in this document is a notetaker-worker change; the worker's own fixes are in the talke repo, `deployment/docs/2026-10-01-cross-platform-transcription-accuracy.md`, and are already on a branch there.
- **Evidence meeting:** Zoom `92790577843`, aw-bots meeting `1c385c82-85f1-4f56-af01-23e272d10979` (upstream 153), session `73235dfd-d917-4d37-ba4f-3a3955036c1e`. Export: `s3://aw-chatworks-transcribe/recordings/zoom_92790577843_20261001T121036522Z/`. Activity: `s3://aw-bots/signal/1/153/73235dfd-d917-4d37-ba4f-3a3955036c1e/speaker-activity.jsonl`. Call write-up: `vexa-fork/recordings/call_analysis_zoom_01.10.2026.txt`.
- **For an implementing agent:** build test-first, and do not report a fault fixed until its gate in §6 has been run on a fresh two-person Zoom call. The call write-up's own checks ("every block has a name", "export completed") pass on the broken output; do not use them.

## 0. Summary

The transcript of the 1 Oct Zoom test names one speaker for the whole call. Sujoy held the floor for 156 s (47%) and has zero words. The call write-up attributes this to the empty `speaker_intervals` in `speaker_timeline.json`. That is true and it is the smaller of two faults. The larger one: **Sujoy's audio is not in `master.webm`.** The bot heard him on his own WebRTC track (his channel's activity frames are at full level) and the recording never carried him. No exporter or worker change can name speech that was never recorded.

Three faults, in order of severity, plus one missing safeguard:

| | fault | where | gate |
|---|---|---|---|
| A | the recording carries one participant | aw-bots recording path (PulseAudio sink tap) | mix RMS inside each speaker's turns comparable |
| B | `speaker_intervals` empty: the exporter discards Zoom's per-channel frames because the bot labels the lane `mixed`, and the points fallback is Teams-only | aw-bots lane label; aw-exporter `activity.py`, `attribution.py` | `speaker_intervals` non-empty with both names |
| C | a channel bound to the wrong person on 2 votes for 31 s | aw-bots `createTrackNameResolver` | no bind below a vote floor or margin |
| D | nothing checks the artefact against the audio before hand-off | aw-exporter `job.py` | an export whose timeline names a speaker with no energy in the mix is flagged, not handed off as clean |

## 1. Evidence

All measured from the S3 objects above on 2026-10-01 evening. Nothing was listened to.

**Timeline (points, from `speaker_timeline.json`, collapsed into runs):** Utpalendu 0–53.9 s, Sujoy 54.9–56.9, Utpalendu 58.4–60.4, Sujoy 60.9–90.9, Utpalendu 92.9, Sujoy 94.6–102.9, Utpalendu 105.1–107.2, **Sujoy 107.9–193.9**, Utpalendu 194.4–196.4, Sujoy 197.2–207.2, Utpalendu 208.1–216.1, Sujoy 217.6–221.7, Utpalendu 222.4–320.6, Sujoy 321.4–323.4, Utpalendu 324.1–330.1, Sujoy 332.1–336.1. 176 points, 95 Utpalendu, 81 Sujoy. `speaker_intervals: []`. `duration_sec: 330.74`.

**Mixed recording, decoded with the exporter's own command** (`ffmpeg -i master.webm -af aresample=async=1:first_pts=0 -ac 1 -ar 16000 -c:a pcm_s16le`), RMS per window:

| window | speaker on the timeline | mix RMS |
|---|---|---|
| 0–53.9 s | Utpalendu | 0.0988 |
| 60.9–90.9 s | Sujoy | **0.0002** |
| 107.9–193.9 s | Sujoy, one 86 s turn | **0.0126**; 1-second samples every 6 s read 0.000 except two blips (0.046 at ~138 s, 0.073 at ~180 s) |
| 197.2–207.2 s | Sujoy | 0.0393 (Utpalendu's 208 s turn starts inside the tail) |
| 208.1–216.1 s | Utpalendu | 0.1202 |
| 222.4–320.6 s | Utpalendu | 0.0904 |

The webm is stereo Opus at 48 kHz, but left and right are identical in every window (dual mono), so the mono downmix lost nothing. The file ends at 330.7 s with ffmpeg's "File ended prematurely" warning (the host ended the meeting and the bot was ejected); Sujoy's last run at 332–336 s is past the file end.

**Per-channel activity frames (`speaker-activity.jsonl`, header `lane: "mixed"`, `platform: "zoom"`, 176 hints, 1,123 frames of which 1,121 carry a name and a `ch` field):**

| channel | name on the frame | frames | span | voiced (≥ 0.026) | voiced median RMS | max |
|---|---|---|---|---|---|---|
| 0 | Utpalendu Sarkar | 397 | 0.3–206.6 s | 316 | 0.056 | 0.145 |
| 1 | Utpalendu Sarkar | 121 | 25.7–56.5 s | 52 | 0.065 | 0.199 |
| 1 | sujoy | 603 | 56.7–215.4 s | 285 | 0.054 | 0.198 |

Sujoy's channel is as loud as Utpalendu's. The 121 frames of channel 1 under Utpalendu's name are fault C.

**What Whisper got:** 26 segments, 290 words for 330.7 s of continuous speech. The stretch 54.7–207.1 s came back as **one segment of 14 words**, "go ahead and read something yeah yes assumptions yeah yeah okay yes ok…": Utpalendu's prompts plus the two blips. Every other segment lies inside Utpalendu's windows. Even the Jitsi point mapper the worker fell back to could not have named this segment for Sujoy: it starts at 54.7 s and the last point at or before that is Utpalendu's at 53.9 s.

## 2. Fault A — the recording carries one participant

### 2.1 Mechanism

From `core/meetings/services/bot/src/capture-bridge.ts` (the comment block at the `isMixed` branch) and `core/meetings/modules/mixed-capture-core/src/webrtc-audio-hook.ts`:

- Zoom rides the WebRTC hook, `installRemoteAudioHook`, installed before navigation. For every remote audio track it (a) pushes the stream into `window.__vexaCapturedRemoteAudioStreams`, which the per-track capture reads, and (b) creates a hidden `<audio data-vexa-injected>` element with `autoplay`, `muted = false`, `srcObject = stream`, calls `play()` and swallows a rejection ("autoplay may defer").
- The per-track capture (`setupPerTrack`) taps each stream with a `ScriptProcessor` on one shared 16 kHz `AudioContext`. This is what produced the activity frames above, so it saw both tracks: ch=0 from 0.3 s, ch=1 from 25.7 s.
- The **recording** does not come from those taps. Zoom uses `PulseAudioCapture` (`core/meetings/modules/recording/src/audio-pipeline.ts`): a `parecord --device=zoom_sink.monitor` subprocess. It records whatever the browser plays into the PulseAudio sink. The capture-bridge comment says so: the hidden element "is what the recorder taps".

So the recording is only as complete as the set of hidden elements that actually play into the sink. Channel 0 played; channel 1 did not.

### 2.2 Hypotheses, in the order to test them

1. **The second hidden element never played.** `play()` rejections are swallowed. The bot's Chromium flags may allow autoplay for the first element and not a later one, or the element may sit in `paused` because `srcObject` was set before `body` existed (the hook appends on `DOMContentLoaded` in that case). Diagnostic: after admission and again every 30 s, log `document.querySelectorAll('audio[data-vexa-injected]')` with `paused`, `readyState`, `muted`, `volume`, `srcObject.getAudioTracks().length` per element, plus `pactl list sink-inputs` on `zoom_sink` from Node. The 1 Oct bot log would have shown "[Audio Hook] mirrored remote audio track …" lines; the count against "[pertrack] capturing ch=1 (2 track(s))" tells whether the element was even created.
2. **Both tracks share one `MediaStream`.** `handleTrack` uses `event.streams[0]` when present. If Zoom delivers both remote tracks inside the same stream, the second hidden element is given the same stream as the first, and an `HTMLMediaElement` plays only one audio track of a multi-track stream. The per-track path would still see two different streams only if the fallback `new MediaStream([event.track])` ran. Diagnostic: log `event.streams.length` and `event.streams[0]?.id` per track in `handleTrack`.
3. **Zoom's own playback is not reaching the sink either.** The Zoom web client renders audio through its own pipeline; if it had played both participants into `zoom_sink`, Sujoy would be in the recording regardless of the mirrors. He is not, so either Zoom plays through a path PulseAudio does not see, or Zoom's own playback is muted in the bot. Diagnostic: `pactl list sink-inputs` while two people speak; count the clients.

### 2.3 Fix

Two options. The second is recommended.

- **A1, fix the sink path.** Make every mirrored element demonstrably play (retry `play()` on a timer, log and report a `pertrack-element-paused` observation when it does not, give each track its own `MediaStream` object), and verify with `pactl`. This keeps the current architecture: activity from the per-track taps, audio from the sink. It also keeps the failure mode: the two can disagree again, silently.
- **A2, record from the per-track PCM the bot already has.** `setupPerTrack` already receives every track's 16 kHz PCM, with a wall-clock timestamp per frame, on a stable channel per participant. Sum the channels in Node (or in the browser, on the same `AudioContext`) into one mixed stream and feed that to the recording pipeline instead of `parecord`. Then the recording and the activity file come from one source and cannot disagree: a track that is in the timeline is in the audio by construction. The cost is an encode hop in Node (the comment in `audio-pipeline.ts` already accepts that hop for PulseAudio) and a chunking scheme for the per-track frames; the existing wav path for Zoom (`audio-pipeline.ts`, the `channels` / s16le header code) is most of it.

Either way, **fault D below is what makes the next regression visible.** A1 without D reproduces today's silence.

### 2.4 Gate

On a fresh two-person Zoom call of at least five minutes with real turn-taking: decode `master.webm` as in §1 and compute RMS inside each speaker's turns taken from the activity points. Both speakers' voiced windows must be in the same range (within roughly 2× of each other). Record the numbers in the test report.

## 3. Fault B — `speaker_intervals` is empty for Zoom

### 3.1 Mechanism

- `capture-bridge.ts` line ~723: `const lane: 'gmeet' | 'mixed' = mixed ? 'mixed' : 'gmeet'`. Zoom is in the `isMixed` family (with Teams and Jitsi) and then takes the `isPerTrack` sub-branch, so its activity header says `lane: "mixed"` while every frame is per-channel and named.
- `exporter/activity.py` `parse_activity`: frames are stored only when `activity.lane != "mixed"`; on a mixed lane they are counted and dropped. `speech_events` on a mixed lane returns hint points only (`source: "hint"`).
- `exporter/attribution.py` `_build_dominant_speaker_timeline` pairs only `source == "audio"` events into intervals. With hints only it returns `([], [])`.
- The safety valve in `build_speaker_timeline` then writes a point per `SPEAKER_START` hint (the 176 points, one every ~2 s, that the file carries).
- `_intervals_from_points` is gated `if platform == "teams" and not paired_intervals and len(distinct_speakers) >= 2`. Zoom gets no intervals from either path.

The exporter threw away 1,121 usable per-channel frames because of a label.

### 3.2 Fix

1. **Bot: label the lane by what the data is.** Add a distinct lane value for the per-track Zoom path (for example `lane: "pertrack"`), or a boolean `per_track: true` in the header. `makeTelemetryTap` and the activity-header writer take the lane; both must carry the new value. The `'gmeet' | 'mixed'` union is in `capture-bridge.ts` at several sites and in the telemetry record types.
2. **Exporter: build intervals from named frames whenever they exist, whatever the lane says.** In `parse_activity`, keep frames that carry a name on any lane. In `speech_events`, prefer the frame path when `activity.frames` is non-empty and fall back to hints only when it is empty. This is the change that would have produced real `audio` START/END intervals for this call, from frames at full level.
3. **Exporter: make the points fallback platform-agnostic.** Replace `platform == "teams"` with "no paired intervals and at least two distinct speakers". Intervals from points are worse than intervals from frames, but they are never worse than nothing, and the worker's dominant-overlap mapper needs them to run at all.
4. **Tests to change, deliberately and in the same commit:** `tests/test_activity.py` `test_mixed_lane_with_frames_and_hints_returns_hints_only` and `test_mixed_lane_frames_are_counted_not_stored` pin the behaviour being removed. `tests/test_attribution.py` has the Teams gate. Add: a Zoom-shaped fixture (header `platform: zoom`, per-channel named frames, hints) that yields `audio` intervals for both names; a hints-only fixture that still yields points and then point-derived intervals.

### 3.3 Gate

On the same fresh Zoom call: `speaker_timeline.json` has `speaker_intervals` non-empty, with both names, built from frames (`speaker_activity: "ok"` in `_export.json`, and the intervals' edges match frame times, not 2 s hint ticks). Then the worker's word-level check from the talke document applies.

## 4. Fault C — a channel bound on two votes

From the 1 Oct bot log, quoted in the call write-up: `bound ch=1 → Utpalendu Sarkar (2 votes)` at 12:11:04.129, `bound ch=0 → Utpalendu Sarkar (68 votes)` 45 ms later, `bound ch=1 → sujoy (63 votes)` at 12:11:50.816. The activity frames confirm it: 121 frames of channel 1 under Utpalendu's name, 25.7–56.5 s. Sujoy's first turn starts at 54.9 s, so the loss here was ~1.6 s of his speech; on a call where the second person speaks early it would be their whole opening.

The resolver is `@vexa/zoom-capture` `createTrackNameResolver` (vote, margin hysteresis, 1:1 by identity; see the comment in `capture-bridge.ts`). Fix direction: a channel must not bind until it has a minimum vote count **and** a clear margin over the next candidate, and a channel must not bind to a name that another channel already holds with far more votes (68 vs 2 in the same second is the case to write the test from). Gate: on the fresh call, every `pertrack-bind` observation carries a substantial vote count and there is no `pertrack-flip`.

## 5. Fault D — no boundary check between the artefact and the audio

The export of this call reported `speaker_activity: "ok"`, `handed_off`, 7 of 7 webhooks delivered, and produced a transcript that asserts one participant. Nothing compared the timeline with the wav.

Add to `exporter/job.py`, after the wav and the timeline are built and before `/process`: for each speaker with more than, say, 10 s on the timeline, compute RMS of the wav inside that speaker's intervals (or runs of points). If any speaker's RMS is below a floor (the exporter already owns `rms_speech_threshold = 0.026`; the mix level for a present speaker here was 0.09–0.12), record `speaker_activity: "audio_mismatch"` with the per-speaker numbers in `_export.json`, report `failed` through `export_result`, and do not hand off. A transcript that is wrong with confidence is worse than no transcript; the call write-up says the same.

This is cheap (one pass over a 16 kHz mono wav) and it is the only item in this document that would have caught the 1 Oct Zoom export on the day.

## 5A. Teams (added from the 18:25 IST Teams test, `teams_937686681622_20261001T125542136Z`, aw-bots meeting `35ec0c4a-ffe4-4f4f-a9de-db88da688877`, upstream 154)

Write-up: `vexa-fork/recordings/call_analysis_teams_01.10.2026.txt`. Teams is the mixed lane proper (one server mix, speaker identity from the on-screen indicator), so the `_intervals_from_points` fallback ran and produced 102 intervals; both names reached the transcript. The S3 numbers below are the write-up's and are being re-measured the same way as §1; the mechanisms are confirmed from the code.

**Fault E — the final speaker run is closed at the end of the recording.** `attribution.py` `_intervals_from_points`: each run closes at the next speaker's first point and *the last run closes at `duration_sec`*. On this call the conversation ended at 259.7 s and the recording ran to 1560.7 s (fault F below), so one interval reads 259.7–1560.7 s, 21 min 41 s, under the last speaker. Talk time reported 95/5 for a 72/28 conversation. Any speech Whisper finds in that tail, real or invented, goes to that name; on this call it found none (0 segments after 259.7 s), which is luck. Fix: close the last run at its own last point plus `speech_hangover_ms` (700 ms, the same closure rule the frame path uses), never at the recording end, and clip every run the same way when the gap to the next speaker's first point exceeds the hangover. Test: a points-only fixture whose last point is far before `duration_sec` yields a last interval ending near the last point. The same closure rule should apply to the *gaps between* runs: a run that closes at the next speaker's first point also hands the silence between turns to the earlier speaker, which is the mechanism behind the Teams call's 19 of 30 two-speaker segments (63%) and 21.3% of words under the wrong name. Intervals from points should be [first point, last point + hangover] per run, and the worker's gap-word rule handles the silence between.

**Fault F — the bot does not leave an empty Teams meeting (bot, not exporter).** From the bot log in the write-up: `aloneness: capture-fault suspected (streams=1, no frames for window) — holding left_alone (frames_delivered=931, window_ms=600000)`, then `attempting one capture restart`, in a loop with no exit, while `[TeamsSpeakers] Scanned 1 participants, observing 0 with signal` said the room held only the bot. The deaf-capture guard treats silence as a possible capture fault and never consults the roster. Fix: when the roster independently reports zero participants with signal for the window, the guard must not hold `left_alone`. Also: Teams does not eject an anonymous guest when the host ends the meeting (Zoom does), so "end the meeting" is not an operational workaround on Teams; and the bot pod has no `activeDeadlineSeconds`, so a held bot runs forever (the old bot had a 4 h backstop). Restore a deadline.

**Fault G — the roster over-counts on Teams.** `Scanned 6 participants, observing 2 with signal` with three present, one parsed as the literal name "Error". The two real names were right, so attribution was unaffected; the participant count is what the leave logic reads, so it matters for F. Captions produced nothing (`[TeamsCaptions] health present=false`), so all naming rests on the visual indicator (`vdi-occlusion`), which worked here and is one Teams layout change from failing silently; that is a known risk, not a fault to fix now.

**Fault H — point-derived intervals are too coarse for word-level attribution, and the file does not say which kind it carries.** Measured on this call (re-measured from S3 on 2026-10-01 evening): both speakers are present in the mix (RMS 0.173 and 0.170 inside their turns; the tail after 259.7 s is 0.00000), 211 points, 132 speaker changes in the 260 s conversation (one every 2.0 s) and 82 of the 102 intervals shorter than 3 s, while the two people were reading alternating paragraphs. The worker's new word-level split (talke branch `fix/notetaker-transcription-accuracy`) replayed on these intervals turns 30 segments into 86 pieces and 63 blocks, alternating names every two to four words inside one person's sentence. Frame-derived intervals (Meet) are sharp to a 256 ms frame; point-derived ones (Teams, and the Zoom fallback once B lands) are sharp to a 2 s DOM tick and a lagging indicator. The worker has to treat them differently and today cannot tell them apart. Fix: `build_speaker_timeline` writes the provenance into the file, `speaker_intervals_source: "audio" | "points"` (add the field to `SpeakerTimelineFile` in `schemas.py`, additive and optional so the worker's existing reader is unaffected). The worker then disables word splitting for `"points"`. Beyond that, the only way to make Teams attribution sharper than the visual indicator is a better source: captions produced nothing on this call, and the CSRC turn spine the capture bridge describes for the mixed lane (RTP labels the server mix with its contributing sources) was not what the timeline used. Making CSRC edges the Teams timeline source is the real fix for the 21% word error, and it is bot work.

Gates for Teams: on a fresh two-person call that the host ends, the bot leaves within its window without a manual stop; `speaker_intervals` talk time per speaker within a few percent of the share measured over the conversation only; last interval end within one second of the last activity point; then the worker's word-level count.

## 6. Verification plan, in order

1. Land B (exporter side) first. It is pure Python with tests and it is needed regardless of how A is fixed.
2. Land D with B. Re-export the 1 Oct Zoom meeting (the exporter re-runs a handed-off folder only as "already_done"; clear `_export.json` state in a copy of the prefix, or use a fresh export prefix). Expected: `speaker_intervals` non-empty with both names **and** `audio_mismatch` on Sujoy. That is the proof D works, on the real failure.
3. Fix A (A2 preferred), with the §2.2 diagnostics left in as observations.
4. Fix C.
5. Run a fresh two-person Zoom call of ≥ 5 minutes with genuine back-and-forth, one person reading a long passage. Gates: §2.4, §3.3, §4, and `_export.json` with `speaker_activity: "ok"`. Then the worker's word-level count.
6. The 1 Oct Zoom meeting itself **cannot** be re-transcribed into a correct transcript: the audio is missing from the recording and the aw-bots chunks are the same audio. Do not spend time on `/reprocess` for it.

## 7. Measurement commands

Mixed-wav RMS per speaker window (what §1 used):

```bash
aws s3 cp s3://aw-chatworks-transcribe/recordings/<prefix>/master.webm .
aws s3 cp s3://aw-chatworks-transcribe/recordings/<prefix>/speaker_timeline.json .
ffmpeg -nostdin -y -i master.webm -af aresample=async=1:first_pts=0 -ac 1 -ar 16000 -c:a pcm_s16le audio.wav
python3 - <<'EOF'
import wave, array, math, json
w = wave.open("audio.wav"); sr = w.getframerate()
a = array.array("h", w.readframes(w.getnframes()))
tl = json.load(open("speaker_timeline.json"))
spans = [(i["start_sec"], i["end_sec"], i["speaker_name"]) for i in tl.get("speaker_intervals") or []]
if not spans:  # points only: collapse same-speaker runs, each run ends at the next speaker's first point
    pts = sorted(tl["speaker_timeline"], key=lambda e: e["relative_sec"])
    for i, p in enumerate(pts):
        if i and pts[i-1]["speaker_name"] == p["speaker_name"]:
            spans[-1] = (spans[-1][0], p["relative_sec"], p["speaker_name"])
        else:
            spans.append((p["relative_sec"], p["relative_sec"], p["speaker_name"]))
by = {}
for s, e, n in spans:
    seg = a[int(s*sr):int(e*sr)]
    if len(seg) > sr // 2:
        by.setdefault(n, []).append(math.sqrt(sum(x*x for x in seg)/len(seg))/32768)
for n, v in by.items():
    v.sort(); print(f"{n:22s} windows={len(v):3d} median_rms={v[len(v)//2]:.4f} max={v[-1]:.4f}")
EOF
```

Per-channel level from the activity file (what §1 used for the frames table):

```bash
aws s3 cp s3://aw-bots/signal/<owner>/<vexa_meeting_id>/<session_uid>/speaker-activity.jsonl .
python3 - <<'EOF'
import json, statistics
from collections import defaultdict
by = defaultdict(list); hdr = None
for line in open("speaker-activity.jsonl"):
    r = json.loads(line)
    if r.get("type") == "speaker_activity_header": hdr = r
    elif r.get("type") is None and "t" in r: by[(r.get("ch"), r.get("name"))].append(r["rms"])
print(hdr)
for (ch, name), v in sorted(by.items(), key=lambda kv: str(kv[0])):
    voiced = [x for x in v if x >= 0.026]
    print(f"ch={ch} {name!s:20s} frames={len(v):4d} voiced={len(voiced):4d} voiced_median={statistics.median(voiced) if voiced else 0:.3f} max={max(v):.3f}")
EOF
```

## 8. What to capture on the next Zoom test, before anything else

The bot pod is gone after the call and `EXPORT_DEBUG` was off, so the 1 Oct bot log survives only in the call write-up's quotes. For the next test, keep: the full bot log (`[Audio Hook] mirrored …`, `[pertrack] capturing …`, `[pertrack] bound …` lines with timestamps), `pactl list sink-inputs` sampled during speech, and the §2.2 element-state dump. Run the export with `EXPORT_DEBUG` on so `signal/` is copied next to the recording.

## 9. Related

- Worker-side plan and the Meet measurements: talke `deployment/docs/2026-10-01-cross-platform-transcription-accuracy.md` (its §"What the 1 Oct Zoom recording showed" is the short form of this document).
- Speaker-activity design: `docs/2026-09-23-speaker-activity-design.md`. The lane rule it specifies ("mixed lane → hints only") is what §3 changes; update that spec in the same change.
