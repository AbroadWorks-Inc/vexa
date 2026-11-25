/**
 * lifecycle.v1 egress ADAPTER — HTTP callback to meeting-api.
 *
 * Implements the `LifecycleSink` port by POSTing each lifecycle.v1 event verbatim to the
 * meeting-api callback URL (`inv.meetingApiCallbackUrl`). 0.11 convention:
 *   • headers: `content-type: application/json` + (if set) `x-internal-secret: <internalSecret>`
 *   • body: the lifecycle.v1 event JSON, as-is (no envelope)
 *
 * L3-testable via an INJECTED `fetchImpl` (defaults to Node 22's native `fetch` — NO new dep).
 * The composition root builds the live adapter; the test injects a recording/failing fake.
 *
 * Robustness (P14): a lifecycle POST failure must NEVER crash the bot. `emit` retries with a
 * bounded backoff on a network error or a non-2xx response, then logs + gives up — it never
 * throws fatally out of `emit`. (A dropped status report is regrettable but must not strand a
 * seated bot or mask the terminal exit.)
 */
import type { LifecycleEvent } from '../contracts.js';
import type { LifecycleSink, PrimaryReachability } from '../ports.js';

/** The minimal fetch shape we depend on (a subset of the WHATWG `fetch`), so the test can
 *  inject a fake without pulling in DOM/undici types. */
export type FetchLike = (
  url: string,
  init: { method: string; headers: Record<string, string>; body: string; signal?: AbortSignal },
) => Promise<{ ok: boolean; status: number }>;

export interface HttpLifecycleSinkOptions {
  /** meeting-api's lifecycle.v1 callback URL (invocation.v1 `meetingApiCallbackUrl`). */
  callbackUrl: string;
  /** SECRET — sent as `x-internal-secret` when present (0.11 internal auth). */
  internalSecret?: string;
  /** Injected for the L3 test; defaults to Node 22's native global `fetch`. */
  fetchImpl?: FetchLike;
  /** Max POST attempts (1 try + retries). Default 3. */
  retries?: number;
  /** Base backoff (ms) between attempts; doubles each retry (bounded). Default 200ms. */
  backoffMs?: number;
  /** Sleep impl (injected so the test runs instantly). Default real setTimeout. */
  sleep?: (ms: number) => Promise<void>;
}

const realSleep = (ms: number): Promise<void> => new Promise((r) => setTimeout(r, ms));

/** One reachability probe's time limit. The orchestrator repeats the probe until its own deadline
 *  (orchestrator.ts CONTROL_PLANE_REACH_DEADLINE_MS), so a probe that hangs must not outlive it. */
const REACH_ATTEMPT_TIMEOUT_MS = 5_000;

/** Build the live HTTP lifecycle sink. `emit` POSTs the event with bounded retry/backoff and
 *  never throws — a permanent failure is logged and swallowed (the bot keeps running). */
export function createHttpLifecycleSink(opts: HttpLifecycleSinkOptions): LifecycleSink {
  const {
    callbackUrl,
    internalSecret,
    fetchImpl = globalThis.fetch as unknown as FetchLike,
    // 5 × 500ms exponential ≈ 7.5s horizon. The old 3×200ms (~0.6s) horizon lost events across
    // any >1s meeting-api blip; the reaper then attributed a failure to a bot that was seated and
    // healthy (hosted 07-14→07-17: callback losses were the dominant "joining" failure class).
    retries = 5,
    backoffMs = 500,
    sleep = realSleep,
  } = opts;

  const headers: Record<string, string> = { 'content-type': 'application/json' };
  if (internalSecret) headers['x-internal-secret'] = internalSecret;

  const attempts = Math.max(1, retries);

  async function emit(event: LifecycleEvent): Promise<void> {
    const body = JSON.stringify(event);
    let lastErr: string | undefined;
    for (let attempt = 1; attempt <= attempts; attempt++) {
      try {
        const res = await fetchImpl(callbackUrl, { method: 'POST', headers, body });
        if (res.ok) return; // 2xx — delivered
        lastErr = `HTTP ${res.status}`;
      } catch (e) {
        lastErr = (e as Error)?.message ?? String(e);
      }
      // Bounded exponential backoff before the next attempt (none after the last).
      if (attempt < attempts) await sleep(backoffMs * 2 ** (attempt - 1));
    }
    // Give up — log, never throw (a lifecycle POST failure must not crash the bot, P14).
    console.error(
      `[bot] lifecycle.v1 ${event.status} POST failed after ${attempts} attempt(s): ${lastErr ?? 'unknown'} (giving up)`,
    );
  }

  // The reachability gate's first-emit probe (#530): ONE time-limited attempt that emits the
  // (joining) event AND reports whether the primary channel is REACHABLE. Any HTTP response proves
  // the channel is up (P18) and returns `reachable`. A 5xx answer means meeting-api was reached but
  // did not store the event, so the event is delivered through `emit`'s bounded retry before the
  // verdict is returned. No response (a network error or the time limit — the CNI-lag signature) is
  // `unreachable`; the orchestrator owns the retries and the deadline. Never throws (P14).
  async function emitReachable(event: LifecycleEvent): Promise<PrimaryReachability> {
    let res: { ok: boolean; status: number };
    try {
      res = await fetchImpl(callbackUrl, {
        method: 'POST', headers, body: JSON.stringify(event), signal: AbortSignal.timeout(REACH_ATTEMPT_TIMEOUT_MS),
      });
    } catch (e) {
      console.error(
        `[bot] lifecycle.v1 ${event.status} reachability probe: primary channel UNREACHABLE: ${(e as Error)?.message ?? String(e)}`,
      );
      return 'unreachable';
    }
    if (res.status >= 500) await emit(event);
    return 'reachable';
  }

  return { emit, emitReachable };
}
