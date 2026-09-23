/**
 * Speaker-activity writer — WHO WAS TALKING WHEN, with no audio (design doc
 * 2026-09-23-speaker-activity-design.md "The file"). Every captured frame and speaker
 * hint is recorded as a name + level + duration, never as PCM.
 *
 * Unlike the debug tape (captured-signal.jsonl, telemetry.ts), which carries a full copy
 * of the audio and is capped at 250 MB (~50 minutes on a long meeting), this file is
 * small (about 1–40 MB for a 3-hour meeting) and is meant to always be on: it is the
 * exporter's only source for a speaker's name across the whole meeting, not a debugging
 * tool.
 *
 * Same discipline as telemetry.ts createCaptureSignalRecorder: frame/hint are
 * fire-and-forget and MUST NOT throw or block; lines are buffered and flushed to disk on
 * a serialized promise chain, on a size/time threshold; a broken writer (e.g. an
 * unwritable dir) disables the whole session rather than affecting the meeting; every
 * writer fault is swallowed and logged, capped at 5 lines.
 */
import { appendFileSync, mkdirSync } from 'node:fs';
import { appendFile } from 'node:fs/promises';
import { join } from 'node:path';
import { isMixedLanePlatform, type Invocation } from './config.js';
import { rmsOf } from './capture-bridge.js';
import { imageVersion, signalEvent } from './telemetry.js';

export interface SpeakerActivityWriter {
  /** The session file path. */
  path: string;
  /** One captured audio frame. Meet names it at capture time; Zoom/Teams frames arrive
   *  unnamed and are attributed later from hints. Never throws; a no-op once close() has run. */
  frame(ch: number, pcm: Float32Array, ts: number, name?: string): void;
  /** A mixed-lane active-speaker signal (Zoom/Teams). Never throws; a no-op once close() has run. */
  hint(t: number, name: string, isEnd?: boolean): void;
  /** True once the size ceiling stopped the writer. The meeting is unaffected. */
  isCapped(): boolean;
  /** Flush any buffered lines and stop the flush timer. Idempotent; never throws. Capture
   *  already stopped by the time teardown calls this, so frame()/hint() become no-ops here too —
   *  no lines can arrive out of order after the file is finalized for upload. */
  close(): Promise<void>;
}

export interface SpeakerActivityOptions {
  /** Directory for the session file. Created if absent. */
  dir?: string;
  /** Hard ceiling on file bytes; past it the writer stops and the meeting continues. */
  maxBytes?: number;
  /** Flush cadence (ms); timer is unref'd so it never holds the process open. */
  flushMs?: number;
  /** Buffer cap (bytes) that forces an early flush between timer ticks. */
  maxBufferBytes?: number;
  log?: (m: string) => void;
  now?: () => number;
}

/** 1 GiB — about 25x a 3-hour meeting's expected size. Exists only so a runaway bug
 *  cannot fill the pod's disk; it is never expected to be reached. */
export const DEFAULT_MAX_ACTIVITY_BYTES = 1024 * 1024 * 1024;
const DEFAULT_DIR = process.env.VEXA_CAPTURE_SIGNAL_DIR ?? '/tmp/captured-signal';
const DEFAULT_FLUSH_MS = 2000;
const DEFAULT_MAX_BUFFER = 256 * 1024;

export function resolveMaxActivityBytes(
  raw: string | undefined = process.env.VEXA_SPEAKER_ACTIVITY_MAX_BYTES,
): number {
  if (raw === undefined || raw.trim() === '') return DEFAULT_MAX_ACTIVITY_BYTES;
  const n = Number(raw);
  // A garbled cap is not an instruction to record without bound — fall back to the default,
  // same rule as telemetry.ts's resolveMaxTapeBytes.
  if (!Number.isFinite(n) || n <= 0) return DEFAULT_MAX_ACTIVITY_BYTES;
  return n;
}

/** Create the writer for one bot session. The header line is written synchronously at
 *  creation, before capture starts; frames and hints are buffered and flushed
 *  asynchronously so the capture path never waits on I/O. */
export function createSpeakerActivityWriter(inv: Invocation, opts: SpeakerActivityOptions = {}): SpeakerActivityWriter {
  const log = opts.log ?? ((m: string) => console.log(`[bot] speaker-activity: ${m}`));
  const now = opts.now ?? Date.now;
  const dir = opts.dir ?? DEFAULT_DIR;
  const sessionUid = inv.connectionId ?? inv.nativeMeetingId ?? 'session';
  const path = join(dir, `${sessionUid}.speaker-activity.jsonl`);
  const maxBytes = opts.maxBytes ?? resolveMaxActivityBytes();
  const maxBuffer = opts.maxBufferBytes ?? DEFAULT_MAX_BUFFER;

  const header = {
    type: 'speaker_activity_header',
    v: 1,
    session_uid: sessionUid,
    platform: inv.platform,
    lane: isMixedLanePlatform(inv.platform) ? 'mixed' : 'gmeet',
    native_meeting_id: inv.nativeMeetingId ?? sessionUid,
    started_at: new Date(now()).toISOString(),
    image_version: imageVersion(),
  };
  const headerLine = JSON.stringify(header) + '\n';

  // A broken writer (unwritable dir, full disk at boot) disables the whole session —
  // never the meeting. Every method below becomes a no-op.
  let disabled = false;
  try {
    mkdirSync(dir, { recursive: true });
    appendFileSync(path, headerLine, 'utf8');
  } catch (e) {
    disabled = true;
    log(`disabled (header write failed): ${String(e)}`);
  }

  let written = disabled ? 0 : headerLine.length;
  let capped = false;
  let faults = 0;
  let buf: string[] = [];
  let bufBytes = 0;
  let flushing: Promise<void> = Promise.resolve();

  /** Gate one JSONL line against the ceiling. The header already counts toward `written`.
   *  The first line that would exceed `maxBytes` is replaced by the capped line (written
   *  even if it slightly exceeds the ceiling); nothing is buffered after it. */
  const admit = (line: string): boolean => {
    if (capped) return false;
    if (written + line.length > maxBytes) {
      capped = true;
      const bytes = written;
      const cappedLine = JSON.stringify({ type: 'capped', t: now(), bytes }) + '\n';
      written += cappedLine.length;
      buf.push(cappedLine);
      bufBytes += cappedLine.length;
      signalEvent('speaker-activity-capped', { path, max_bytes: maxBytes, bytes });
      return false;
    }
    written += line.length;
    return true;
  };

  const flush = (): Promise<void> => {
    if (disabled || buf.length === 0) return flushing;
    const chunk = buf.join('');
    buf = [];
    bufBytes = 0;
    // Serialize flushes so lines never interleave out of order.
    flushing = flushing
      .then(() => appendFile(path, chunk, 'utf8'))
      .catch((e) => { if (faults++ < 5) log(`flush failed (lines dropped): ${String(e)}`); });
    return flushing;
  };

  const timer = disabled ? null : setInterval(() => { void flush(); }, opts.flushMs ?? DEFAULT_FLUSH_MS);
  timer?.unref?.();

  if (!disabled) signalEvent('speaker-activity-started', { path, max_bytes: maxBytes, platform: inv.platform });

  const push = (line: string): void => {
    buf.push(line);
    bufBytes += line.length;
    if (bufBytes >= maxBuffer) void flush();
  };

  let closed = false;
  return {
    path,
    frame(ch, pcm, ts, name): void {
      if (disabled || capped || closed) return;
      try {
        const rec: Record<string, unknown> = {
          t: ts,
          ch,
          ...(name !== undefined ? { name } : {}),
          rms: Math.round(rmsOf(pcm) * 1e5) / 1e5,
          dur_ms: Math.round(pcm.length / 16), // 16 kHz capture
        };
        const line = JSON.stringify(rec) + '\n';
        if (!admit(line)) return;
        push(line);
      } catch (e) { if (faults++ < 5) log(`frame write failed: ${String(e)}`); }
    },
    hint(t, name, isEnd): void {
      if (disabled || capped || closed) return;
      try {
        const rec: Record<string, unknown> = { type: 'hint', t, name, ...(isEnd !== undefined ? { isEnd } : {}) };
        const line = JSON.stringify(rec) + '\n';
        if (!admit(line)) return;
        push(line);
      } catch (e) { if (faults++ < 5) log(`hint write failed: ${String(e)}`); }
    },
    isCapped: () => capped,
    async close(): Promise<void> {
      if (closed) return;
      closed = true;
      if (timer) clearInterval(timer);
      try {
        await flush();
      } catch (e) {
        if (faults++ < 5) log(`close failed: ${String(e)}`);
      }
    },
  };
}
