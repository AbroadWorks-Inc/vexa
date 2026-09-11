/**
 * Tier 1 per-receiver audio capture — production capture of each participant's
 * OWN audio, tagged with that receiver's loudest CSRC id.
 *
 * See docs-utpal/PLANS/plan-meet-audio-element-multiplexing-root-fix.md.
 *
 * WHY THIS EXISTS. The DOM path pools ~3 `<audio>` elements across N
 * participants and re-points them, so "element #2" is not a stable person and
 * attribution oscillates (measured live: 7 people, 3 elements). This module
 * captures audio one level lower: straight off each `RTCRtpReceiver` track, so
 * every buffer belongs to exactly one sender, and it stamps each buffer with the
 * loudest CSRC id from that receiver's `getContributingSources()` — the SFU's
 * own answer to "who is speaking on this stream right now".
 *
 * DESIGN — why the in-page half is deliberately stupid.
 * Code inside `page.evaluate` cannot import this module, and nothing in a
 * browser context can be unit tested in this repo. So the in-page half does the
 * minimum that must happen there — enumerate receivers, run a ScriptProcessor,
 * read CSRC — and every DECISION it makes is mirrored by a pure exported
 * function here (`pickActiveCsrc`, `selectAudioReceivers`, `markConnected`),
 * which is what the tests cover.
 *
 * OFF BY DEFAULT. Set `TIER1_SPEAKER_CAPTURE=true` to enable. With the flag
 * unset the installer is a no-op, so a production bot behaves exactly as before.
 */

import { Page } from "playwright";
import { log } from "../utils";
import { SILENCE_LEVEL, RtpSourceLike, ReceiverLike } from "./rtp-speaker-source";

/** How often the page re-scans for newly-attached audio receivers. */
const SCAN_INTERVAL_MS = 3000;

/** ScriptProcessor buffer size — matches the DOM per-speaker path in index.ts. */
const BUFFER_SIZE = 4096;

/** Capture sample rate — matches the DOM per-speaker path (WhisperX wants 16 kHz). */
const TARGET_SAMPLE_RATE = 16000;

/**
 * Below this a captured buffer is treated as silence and never posted. Matches
 * the DOM path's gate so a receiver's comfort noise does not flood the pipeline.
 */
const CAPTURE_SILENCE = 0.005;

/** One buffer captured from a single receiver, tagged with its active CSRC. */
export interface ReceiverAudioPayload {
  /** The loudest CSRC id at capture time, or null when no CSRC was audible. */
  csrc: number | null;
  /** Peak absolute amplitude in this buffer (0.0–1.0). */
  audioLevel: number;
  /** The 16 kHz mono PCM buffer as a plain number[] (Float32 → Array). */
  samples: number[];
}

/**
 * The loudest CSRC id above the silence floor, or null if none is audible.
 *
 * A buffer whose receiver lists no audible contributor must be DROPPED, not
 * tagged onto the least-quiet id: attributing audio to a silent-but-listed
 * source (comfort noise, or the live-measured sentinel id 42) is the phantom-
 * speaker failure the DOM path already suffers. Uses `>=` at the floor and
 * strict `>` to break ties, matching `activeSourceAt` in rtp-speaker-source.ts.
 */
export function pickActiveCsrc(sources: RtpSourceLike[]): number | null {
  let bestSource: number | null = null;
  let bestLevel = 0;
  for (const s of sources) {
    // Chrome omits audioLevel until a packet has arrived; 0 keeps it below the
    // floor so an un-started source is never chosen.
    const level = s.audioLevel ?? 0;
    if (level < SILENCE_LEVEL) continue;
    if (bestSource === null || level > bestLevel) {
      bestSource = s.source;
      bestLevel = level;
    }
  }
  return bestSource;
}

/**
 * Keep only receivers carrying a live audio track.
 *
 * Video receivers carry no speaker audio and a receiver whose track has ended
 * (null / absent) has nothing to capture; building an AudioContext for either
 * wastes resources and pollutes the stream.
 */
export function selectAudioReceivers(receivers: ReceiverLike[]): ReceiverLike[] {
  return receivers.filter((r) => r.track?.kind === "audio");
}

/**
 * Record that `trackId` now has a capture context, returning whether it is new.
 *
 * The in-page loop re-scans every few seconds to catch late joiners. Without a
 * dedup ledger it would build a SECOND AudioContext for a receiver it already
 * captured, doubling that participant's audio — the duplication bug the DOM path
 * fought. Keyed by track id: one context per receiver, ever.
 */
export function markConnected(trackId: string, connected: Set<string>): boolean {
  if (connected.has(trackId)) return false;
  connected.add(trackId);
  return true;
}

/**
 * Optional sink for captured buffers. Integration (index.ts) registers this to
 * forward buffers into the transcription pipeline; unset, buffers are dropped.
 * Kept here so this module owns the `exposeFunction` bridge without editing
 * index.ts.
 */
let receiverAudioSink: ((payload: ReceiverAudioPayload) => void) | null = null;

/** Register (or clear, with null) the handler for captured receiver buffers. */
export function setReceiverAudioSink(
  sink: ((payload: ReceiverAudioPayload) => void) | null,
): void {
  receiverAudioSink = sink;
}

/** Called from the page with one captured, CSRC-tagged buffer. */
function onReceiverAudio(payload: ReceiverAudioPayload): void {
  if (receiverAudioSink) receiverAudioSink(payload);
}

/**
 * Install per-receiver audio capture. Non-fatal by contract: every failure logs
 * and returns, so a broken capture can never stop a meeting being recorded.
 *
 * Depends on `window.__vexa_peer_connections`, installed by
 * `installRtpPcRegistry` (rtp-speaker-probe.ts), which is gated on the same flag.
 */
export async function startReceiverTrackCapture(page: Page): Promise<void> {
  if (process.env.TIER1_SPEAKER_CAPTURE !== "true") return;

  log("[Tier1Capture] enabled — capturing per-receiver audio tagged with CSRC ids");

  try {
    await page.exposeFunction("__vexaReceiverAudio", onReceiverAudio);
  } catch (err: any) {
    if (!String(err?.message).includes("has been already registered")) {
      log(`[Tier1Capture] could not expose callback, capture disabled: ${err?.message || err}`);
      return;
    }
  }

  try {
    await page.evaluate(
      ({ scanMs, bufferSize, sampleRate, captureSilence, silenceFloor }) => {
        const w = window as any;
        if (w.__vexaReceiverCaptureStarted) return;
        w.__vexaReceiverCaptureStarted = true;

        // ONE shared AudioContext for the whole page. Chromium caps concurrent
        // AudioContexts (~6/page): a per-receiver context made the 7th receiver in
        // the measured 7-person case throw, un-mark its track, and silently never
        // capture that person. A single context feeds every receiver's node graph.
        const ctx = new AudioContext({ sampleRate });
        w.__vexaReceiverCaptureCtx = ctx;

        // Live source/processor nodes, kept for teardown (disconnect on stop).
        const nodes: Array<{ src: any; processor: any; trackId: string }> = [];
        w.__vexaReceiverCaptureNodes = nodes;

        // Dedup ledger: one source+processor per receiver track, ever.
        const connectedTrackIds: Set<string> = new Set();

        const connectReceiver = (r: any): void => {
          const track = r.track;
          if (!track || track.kind !== "audio") return;
          const trackId: string = track.id;
          if (!trackId || connectedTrackIds.has(trackId)) return;
          connectedTrackIds.add(trackId);

          try {
            const src = ctx.createMediaStreamSource(new MediaStream([track]));
            const processor = ctx.createScriptProcessor(bufferSize, 1, 1);

            processor.onaudioprocess = (e: any) => {
              const data: Float32Array = e.inputBuffer.getChannelData(0);
              let peak = 0;
              for (let i = 0; i < data.length; i++) {
                const a = Math.abs(data[i]);
                if (a > peak) peak = a;
              }
              if (peak <= captureSilence) return;

              // Loudest CSRC above the floor owns this buffer; none audible → null,
              // and the buffer is still posted so the sink can drop it rather than
              // this dumb half deciding attribution.
              // MIRRORS the pure `pickActiveCsrc` (this module) — its tested twin;
              // keep the two in lockstep on any edit.
              let csrc: number | null = null;
              let bestLevel = 0;
              const sources =
                typeof r.getContributingSources === "function" ? r.getContributingSources() : [];
              for (const s of sources) {
                const level = typeof s.audioLevel === "number" ? s.audioLevel : 0;
                if (level < silenceFloor) continue;
                if (csrc === null || level > bestLevel) {
                  csrc = s.source;
                  bestLevel = level;
                }
              }

              w.__vexaReceiverAudio({ csrc, audioLevel: peak, samples: Array.from(data) });
            };

            src.connect(processor);
            processor.connect(ctx.destination);
            nodes.push({ src, processor, trackId });

            track.addEventListener("ended", () => {
              connectedTrackIds.delete(trackId);
            });
          } catch {
            // Building failed — un-mark so a later re-scan can retry.
            connectedTrackIds.delete(trackId);
          }
        };

        const scan = (): void => {
          try {
            const pcs = (w.__vexa_peer_connections || []) as RTCPeerConnection[];
            for (const pc of pcs) {
              if (typeof pc.getReceivers !== "function") continue;
              for (const r of pc.getReceivers()) connectReceiver(r);
            }
          } catch {
            // Capture must never throw inside the page — an uncaught error here
            // would surface in the meeting tab, not just our logs.
          }
        };

        scan();
        const interval = setInterval(scan, scanMs);
        w.__vexaReceiverCaptureIntervals = [interval];
      },
      {
        scanMs: SCAN_INTERVAL_MS,
        bufferSize: BUFFER_SIZE,
        sampleRate: TARGET_SAMPLE_RATE,
        captureSilence: CAPTURE_SILENCE,
        silenceFloor: SILENCE_LEVEL,
      },
    );
  } catch (err: any) {
    log(`[Tier1Capture] could not install page capture: ${err?.message || err}`);
    return;
  }
}

/**
 * Tear down per-receiver capture: clear the scan interval(s), disconnect every
 * source/processor node, close the shared AudioContext, and reset the in-page
 * guards so a same-page restart works. Non-fatal by contract — every failure is
 * caught and logged — and a no-op when capture was never started (empty guards).
 */
export async function stopReceiverTrackCapture(page: Page | null): Promise<void> {
  // Drop the Node-side sink first so a late in-page buffer, if any still fires
  // before the page teardown lands, is discarded rather than forwarded.
  setReceiverAudioSink(null);

  if (!page || page.isClosed()) return;

  try {
    await page.evaluate(() => {
      const w = window as any;
      try {
        const intervals = (w.__vexaReceiverCaptureIntervals || []) as any[];
        for (const id of intervals) clearInterval(id);
        w.__vexaReceiverCaptureIntervals = [];

        const nodes = (w.__vexaReceiverCaptureNodes || []) as Array<{
          src: any;
          processor: any;
        }>;
        for (const n of nodes) {
          try {
            if (n.processor) {
              n.processor.onaudioprocess = null;
              n.processor.disconnect();
            }
          } catch {}
          try {
            if (n.src) n.src.disconnect();
          } catch {}
        }
        w.__vexaReceiverCaptureNodes = [];

        const ctx = w.__vexaReceiverCaptureCtx;
        if (ctx && typeof ctx.close === "function") {
          try {
            ctx.close();
          } catch {}
        }
        w.__vexaReceiverCaptureCtx = null;

        // Reset the guard so a same-page restart re-installs cleanly.
        w.__vexaReceiverCaptureStarted = false;
      } catch {
        // Teardown must never throw inside the page — swallow and let the bot exit.
      }
    });
  } catch (err: any) {
    log(`[Tier1Capture] teardown failed (non-fatal): ${err?.message || err}`);
    return;
  }
}
