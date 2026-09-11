/**
 * RTP-layer speaker-source PROBE — instrumentation, not attribution.
 *
 * Answers the open question in
 * `docs-utpal/PLANS/plan-meet-audio-element-multiplexing-root-fix.md` Option 5:
 * does the RTP source id identify a PERSON stably for a whole session, and is
 * it CSRC or SSRC that Meet populates?
 *
 * Meet pools ~3 `<audio>` elements across N participants and re-points them
 * (measured live: 7 people, 3 elements), so "element #2" is not a stable
 * person. If the RTP source id is stable it can replace the element index as
 * the attribution key — which is the browser-side equivalent of the SFU signal
 * that makes Jitsi transcripts attribute better.
 *
 * DESIGN — why the in-page half is deliberately stupid.
 * Code inside `page.evaluate` cannot import this module, and nothing in a
 * browser context can be unit tested in this repo. So the in-page half does
 * the minimum that must happen there — read the receivers — and ships RAW
 * readings out. Every decision (CSRC-vs-SSRC preference, the silence floor,
 * stability maths) lives in `rtp-speaker-source.ts`, which is fully tested.
 *
 * OFF BY DEFAULT. Set `RTP_SPEAKER_PROBE=true` to enable. It reads receivers
 * and logs; it publishes no speaker events and changes no attribution, so a
 * production bot with the flag unset behaves exactly as before.
 */

import { Page } from "playwright";
import { log } from "../utils";
import {
  sampleReceivers,
  sourceStability,
  sourceActivity,
  concurrentActivity,
  RtpSample,
  ReceiverLike,
} from "./rtp-speaker-source";

/** How often the page samples its audio receivers. */
const SAMPLE_INTERVAL_MS = 250;

/** How often a report is written to the log. */
const REPORT_INTERVAL_MS = 60_000;

/**
 * Hard cap on retained samples (~40 min at 250 ms with a handful of receivers).
 * A bot can sit in a long meeting; an unbounded array in a long-lived page is
 * how a probe turns into an outage.
 */
const MAX_SAMPLES = 200_000;

/** One receiver's raw readings, exactly as the page saw them. */
interface RawReceiverReading {
  kind: string | null;
  csrc: Array<{ source: number; audioLevel?: number }>;
  ssrc: Array<{ source: number; audioLevel?: number }>;
}

const collected: RtpSample[] = [];
let reportTimer: ReturnType<typeof setInterval> | null = null;
let droppedForCap = 0;

/** Turn one page reading into the shape the tested sampler expects. */
function toReceiverLike(reading: RawReceiverReading): ReceiverLike {
  return {
    track: reading.kind === null ? null : { kind: reading.kind },
    getContributingSources: () => reading.csrc,
    getSynchronizationSources: () => reading.ssrc,
  };
}

/** Called from the page with one batch of readings. */
function onReadings(readings: RawReceiverReading[], tMs: number): void {
  if (collected.length >= MAX_SAMPLES) {
    droppedForCap += 1;
    return;
  }
  collected.push(...sampleReceivers(readings.map(toReceiverLike), tMs));
}

/**
 * Write what we have learned so far.
 *
 * Logged rather than stored: bot pods are GC'd about an hour after finishing,
 * so this must be readable from `kubectl logs` during or just after the
 * meeting. Reported periodically as well as at the end so an abrupt pod exit
 * does not lose everything.
 */
export function reportRtpSpeakerProbe(): void {
  if (collected.length === 0) {
    log("[RtpProbe] no samples collected — receivers exposed no CSRC or SSRC entries");
    return;
  }

  const stability = sourceStability(collected);
  const activity = sourceActivity(collected);
  const concurrency = concurrentActivity(collected);
  const csrcCount = collected.filter((s) => s.api === "csrc").length;
  const ssrcCount = collected.filter((s) => s.api === "ssrc").length;
  const audible = collected.filter((s) => s.audioLevel > 0).length;

  // audioLevel-per-source, so a real speaker is told from a still-listed or
  // sentinel id: `active` counts samples above the silence floor, `peak` the
  // loudest level seen.
  const activeById = new Map(activity.map((a) => [a.sourceId, a]));

  // One line, parseable, so it survives log scraping.
  log(
    `[RtpProbe] ${JSON.stringify({
      samples: collected.length,
      droppedForCap,
      api: { csrc: csrcCount, ssrc: ssrcCount },
      samplesWithAudio: audible,
      distinctSources: stability.length,
      // Overlap-separation proof: ticksWithOverlap > 0 means Meet forwarded two
      // speakers audible at the same instant as distinct ids.
      concurrency,
      sources: stability.map((s) => {
        const a = activeById.get(s.sourceId);
        return {
          id: s.sourceId,
          firstMs: Math.round(s.firstSeenMs),
          lastMs: Math.round(s.lastSeenMs),
          spanMs: Math.round(s.spanMs),
          n: s.samples,
          // Samples above the silence floor and the peak level. active ~= 0
          // with high n is the sentinel shape (id present but never speaking).
          active: a?.activeSamples ?? 0,
          peak: a ? Math.round(a.peakLevel * 1000) / 1000 : 0,
        };
      }),
    })}`,
  );
}

/**
 * Start the probe. Non-fatal by contract: every failure logs and returns, so a
 * broken probe can never stop a meeting being recorded.
 */
export async function startRtpSpeakerProbe(page: Page): Promise<void> {
  if (process.env.RTP_SPEAKER_PROBE !== "true") return;

  log("[RtpProbe] enabled — sampling RTP source ids (instrumentation only, no attribution change)");

  try {
    await page.exposeFunction("__vexaRtpReadings", onReadings);
  } catch (err: any) {
    if (!String(err?.message).includes("has been already registered")) {
      log(`[RtpProbe] could not expose callback, probe disabled: ${err?.message || err}`);
      return;
    }
  }

  try {
    await page.evaluate((intervalMs: number) => {
      const w = window as any;
      if (w.__vexaRtpProbeStarted) return;
      w.__vexaRtpProbeStarted = true;

      const startedAt = Date.now();

      setInterval(() => {
        try {
          const pcs = (w.__vexa_peer_connections || []) as RTCPeerConnection[];
          const readings: any[] = [];

          for (const pc of pcs) {
            // `getReceivers` is absent on a closed connection.
            if (typeof pc.getReceivers !== "function") continue;
            for (const r of pc.getReceivers()) {
              const anyR = r as any;
              // Read BOTH. Which one Meet populates is the open question, and
              // guessing here would destroy the evidence we came for.
              const csrc =
                typeof anyR.getContributingSources === "function"
                  ? anyR.getContributingSources()
                  : [];
              const ssrc =
                typeof anyR.getSynchronizationSources === "function"
                  ? anyR.getSynchronizationSources()
                  : [];
              if (csrc.length === 0 && ssrc.length === 0) continue;

              readings.push({
                kind: r.track ? r.track.kind : null,
                csrc: csrc.map((x: any) => ({ source: x.source, audioLevel: x.audioLevel })),
                ssrc: ssrc.map((x: any) => ({ source: x.source, audioLevel: x.audioLevel })),
              });
            }
          }

          if (readings.length > 0) {
            w.__vexaRtpReadings(readings, Date.now() - startedAt);
          }
        } catch {
          // A probe must never throw inside the page: an uncaught error here
          // would surface in the meeting tab, not just in our logs.
        }
      }, intervalMs);
    }, SAMPLE_INTERVAL_MS);
  } catch (err: any) {
    log(`[RtpProbe] could not install page sampler: ${err?.message || err}`);
    return;
  }

  if (reportTimer === null) {
    reportTimer = setInterval(reportRtpSpeakerProbe, REPORT_INTERVAL_MS);
  }
}

/** Stop sampling and write the final report. */
export function stopRtpSpeakerProbe(): void {
  if (reportTimer !== null) {
    clearInterval(reportTimer);
    reportTimer = null;
  }
  if (process.env.RTP_SPEAKER_PROBE === "true") {
    log("[RtpProbe] final report:");
    reportRtpSpeakerProbe();
  }
}

/**
 * Install the peer-connection registry the probe depends on.
 *
 * The probe reads `window.__vexa_peer_connections`. On a plain recording bot that
 * array is never created — it is otherwise populated only by the virtual-camera
 * init (cameraEnabled bots), so on 2026-09-11 the probe found ZERO receivers and
 * no CSRC/SSRC on a recording bot. This installs a MINIMAL RTCPeerConnection
 * registry as an INIT script, so it runs BEFORE the meeting page creates its
 * connections (a constructor patch cannot capture already-open connections).
 *
 * OFF unless RTP_SPEAKER_PROBE=true, and a no-op if the camera init already owns
 * the registry — so it never changes a production bot's behaviour. Call it before
 * navigating to the meeting.
 */
export async function installRtpPcRegistry(page: Page): Promise<void> {
  if (process.env.RTP_SPEAKER_PROBE !== "true") return;
  try {
    await page.addInitScript(() => {
      const w = window as any;
      // Already owned by the virtual-camera init — leave it alone.
      if (Array.isArray(w.__vexa_peer_connections)) return;
      const OrigPC = w.RTCPeerConnection;
      if (typeof OrigPC !== "function") return;
      w.__vexa_peer_connections = [];
      const Patched: any = function (this: any, ...args: any[]) {
        const pc = new OrigPC(...args);
        try {
          w.__vexa_peer_connections.push(pc);
        } catch {
          /* never let bookkeeping break a real connection */
        }
        return pc;
      };
      Patched.prototype = OrigPC.prototype;
      try {
        Object.setPrototypeOf(Patched, OrigPC);
      } catch {
        /* static-prop copy is best-effort */
      }
      w.RTCPeerConnection = Patched;
      if (w.webkitRTCPeerConnection) w.webkitRTCPeerConnection = Patched;
      // eslint-disable-next-line no-console
      console.log("[Vexa][RtpProbe] RTCPeerConnection registry installed");
    });
    log("[RtpProbe] peer-connection registry init installed (recording-bot path)");
  } catch (err: any) {
    log(`[RtpProbe] could not install PC registry; probe will see no receivers: ${err?.message || err}`);
  }
}
