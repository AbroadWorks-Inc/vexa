# captured-signal.v1

The **raw capture signal** teed at the bot's capture bridge (`services/bot/src/capture-bridge.ts`)
**BEFORE** the pipeline consumes it (O-TEL-1) — the exact stream a live bug rides on, stored so it
**replays through the same pipeline OFFLINE** (O-TEL-2, `eval/src/replay.mjs`). It turns a live
meeting bug into a reproducible offline test.

A session on disk is a JSONL stream: a **`SessionHeader`** line (`type:"captured_signal_header"`,
the topology to pick the lane) followed by N **`CapturedFrame`** lines. Each frame mirrors the
[`@vexa/capture-codec`](../../modules/capture-codec) binary frame shape — `pcm` is base64 of the
Float32 PCM bytes (little-endian), exactly what the codec puts on the wire — so a frame
**round-trips** through `encodeAudioFrame`→`decodeAudioFrame` to the same PCM (asserted in
`services/bot/src/telemetry.test.ts`).

The `TelemetrySink` port (`services/bot/src/ports.ts`) is the optional dual-sink the bridge tees
frames into; when unset the tap is a single truthiness check — **zero-overhead**, the proven O6
capture path is never altered.

The header's optional **`trace_id`** carries the meeting's distributed trace (the same
[logevent.v1](../../../gateway/contracts/logevent.v1) `trace_id` threaded across
gateway→meeting-api→runtime→bot) — so a captured session links back to every structured log of the
meeting it was captured under, and a [flagged-issue.v1](../flagged-issue.v1) with the **same**
`trace_id` ties the bug record, the raw signal, and the cross-system trace into one repro key
(asserted in `eval/flag.test.mjs`).

`gate:schema` validates goldens ≡ schema. Sealed in `contracts.seal.json`. `Lane` includes
`pertrack` for Zoom's named per-track frames; `gmeet` and `mixed` are unchanged.
