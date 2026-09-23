/**
 * The speaker-activity writer — WHO WAS TALKING WHEN, with no audio (design doc
 * 2026-09-23-speaker-activity-design.md "The file"). Always on, unlike the debug tape
 * (captured-signal.jsonl), which carries audio and is capped at 250 MB: this file is
 * meant to be the exporter's only source for a speaker's name across a whole meeting.
 *
 * The load-bearing checks: no PCM ever lands in the file, the ceiling stops the writer
 * cleanly with exactly one capped line, and a broken writer (mkdir fails) never throws
 * into the caller.
 *
 * Run: npx tsx src/speaker-activity.test.ts
 */
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import {
  createSpeakerActivityWriter,
  resolveMaxActivityBytes,
  DEFAULT_MAX_ACTIVITY_BYTES,
} from './speaker-activity.js';
import type { Invocation } from './config.js';

let failed = 0;
const check = (name: string, cond: boolean, detail?: string): void => {
  console.log(`  ${cond ? '✅' : '❌'} ${name}${cond || !detail ? '' : ` — ${detail}`}`);
  if (!cond) failed++;
};

const invOf = (platform: string, connectionId = 'sess-1'): Invocation =>
  ({ platform, connectionId, nativeMeetingId: 'x', botName: 'bot', meetingUrl: null, redisUrl: 'redis://x' } as unknown as Invocation);

/** Read every JSONL line as a parsed object, in file order. */
const readLines = (path: string): Record<string, unknown>[] =>
  readFileSync(path, 'utf8').split('\n').filter((l) => l.length > 0).map((l) => JSON.parse(l));

/** True when `obj` has exactly `expected`'s keys and values (order-independent). */
const sameShape = (obj: unknown, expected: Record<string, unknown>): boolean => {
  if (typeof obj !== 'object' || obj === null) return false;
  const o = obj as Record<string, unknown>;
  const keys = Object.keys(o).sort().join(',');
  const expKeys = Object.keys(expected).sort().join(',');
  return keys === expKeys && Object.keys(expected).every((k) => o[k] === expected[k]);
};

// ── 1) Header + a named frame + a hint ────────────────────────────────────────────────────────────
{
  const dir = mkdtempSync(join(tmpdir(), 'vexa-speaker-activity-'));
  const inv = invOf('google_meet');
  const w = createSpeakerActivityWriter(inv, { dir, now: () => 500 });
  w.frame(0, Float32Array.of(0.5, -0.5, 0.5, -0.5), 1000, 'Ann');
  w.hint(2000, 'Bo', true);
  await w.close();

  const lines = readLines(w.path);
  check('the file has exactly 3 lines', lines.length === 3, `${lines.length}`);
  const header = lines[0] as Record<string, unknown>;
  check('line 1 is the header', header.type === 'speaker_activity_header' && header.v === 1, JSON.stringify(header));
  check('line 1 has lane:"gmeet" for platform google_meet', header.lane === 'gmeet', JSON.stringify(header));
  check('line 2 deep-equals the named frame record',
    sameShape(lines[1], { t: 1000, ch: 0, name: 'Ann', rms: 0.5, dur_ms: 0 }), JSON.stringify(lines[1]));
  check('line 3 deep-equals the hint record',
    sameShape(lines[2], { type: 'hint', t: 2000, name: 'Bo', isEnd: true }), JSON.stringify(lines[2]));
}

// ── 2) Unnamed frame / hint without isEnd omit their keys ────────────────────────────────────────
{
  const dir = mkdtempSync(join(tmpdir(), 'vexa-speaker-activity-'));
  const inv = invOf('google_meet');
  const w = createSpeakerActivityWriter(inv, { dir, now: () => 0 });
  w.frame(1, Float32Array.of(0.1, -0.1), 3000);
  w.hint(4000, 'Cy');
  await w.close();

  const lines = readLines(w.path);
  check('an unnamed frame has no "name" key', !('name' in lines[1]), JSON.stringify(lines[1]));
  check('a hint without isEnd has no "isEnd" key', !('isEnd' in lines[2]), JSON.stringify(lines[2]));
}

// ── 3) The line never contains raw PCM ────────────────────────────────────────────────────────────
{
  const dir = mkdtempSync(join(tmpdir(), 'vexa-speaker-activity-'));
  const inv = invOf('google_meet');
  const w = createSpeakerActivityWriter(inv, { dir, now: () => 0 });
  w.frame(0, Float32Array.of(0.9, -0.9, 0.3, -0.3, 0.2), 1000, 'Ann');
  await w.close();
  const raw = readFileSync(w.path, 'utf8');
  check('the file never contains "pcm"', !raw.includes('pcm'), raw);
}

// ── 4) The ceiling stops the writer with exactly one capped line, nothing after ──────────────────
{
  // Probe first, to learn this platform+session's exact header length (byte-identical header).
  const probeDir = mkdtempSync(join(tmpdir(), 'vexa-speaker-activity-'));
  const inv = invOf('google_meet', 'cap-sess');
  const probe = createSpeakerActivityWriter(inv, { dir: probeDir, now: () => 0 });
  await probe.close();
  const headerLen = readFileSync(probe.path, 'utf8').length;

  const dir = mkdtempSync(join(tmpdir(), 'vexa-speaker-activity-'));
  const w = createSpeakerActivityWriter(inv, { dir, maxBytes: headerLen + 60, now: () => 0 });
  for (let i = 0; i < 10; i++) w.frame(0, Float32Array.of(0.1, -0.1), 5000 + i * 100);
  await w.close();

  const lines = readLines(w.path);
  const cappedLines = lines.filter((l) => l.type === 'capped');
  check('exactly one capped line was written', cappedLines.length === 1, JSON.stringify(lines));
  check('the capped line is the LAST line', lines[lines.length - 1].type === 'capped', JSON.stringify(lines));
  check('isCapped() is true', w.isCapped() === true);
}

// ── 5) Lane: zoom/teams header to lane:"mixed" ────────────────────────────────────────────────────
{
  for (const platform of ['zoom', 'teams']) {
    const dir = mkdtempSync(join(tmpdir(), 'vexa-speaker-activity-'));
    const w = createSpeakerActivityWriter(invOf(platform), { dir, now: () => 0 });
    await w.close();
    const header = readLines(w.path)[0];
    check(`platform ${platform} → header lane:"mixed"`, header.lane === 'mixed', JSON.stringify(header));
  }
}

// ── 6) Resilience: a broken writer (mkdir fails under an existing FILE) never throws ─────────────
{
  const dir = mkdtempSync(join(tmpdir(), 'vexa-speaker-activity-'));
  const blocker = join(dir, 'blocker-file');
  writeFileSync(blocker, 'not a directory');
  let threw = false;
  try {
    const w = createSpeakerActivityWriter(invOf('google_meet'), { dir: join(blocker, 'subdir'), now: () => 0 });
    w.frame(0, Float32Array.of(0.1), 1000, 'Ann');
    w.hint(2000, 'Bo', true);
    await w.close();
  } catch {
    threw = true;
  }
  check('frame/hint/close never throw when the writer is disabled', !threw);
}

// ── 7) Order: 1,000 frames written quickly come out in call order ────────────────────────────────
{
  const dir = mkdtempSync(join(tmpdir(), 'vexa-speaker-activity-'));
  const w = createSpeakerActivityWriter(invOf('google_meet'), { dir, now: () => 0 });
  for (let i = 0; i < 1000; i++) w.frame(0, Float32Array.of(0.1), 1000 + i);
  await w.close();
  const lines = readLines(w.path).slice(1); // drop header
  const ts = lines.map((l) => l.t as number);
  const expected = Array.from({ length: 1000 }, (_, i) => 1000 + i);
  check('1,000 frames land in call order', ts.length === 1000 && ts.every((t, i) => t === expected[i]),
    `${ts.length} lines; first=${ts[0]} last=${ts[ts.length - 1]}`);
}

// ── 8) resolveMaxActivityBytes: env parsing rule ──────────────────────────────────────────────────
{
  check('empty string → default', resolveMaxActivityBytes('') === DEFAULT_MAX_ACTIVITY_BYTES);
  check('garbage string → default', resolveMaxActivityBytes('abc') === DEFAULT_MAX_ACTIVITY_BYTES);
  check('a valid number string → that number', resolveMaxActivityBytes('2048') === 2048);
}

if (failed) { console.error(`\n❌ speaker-activity: ${failed} check(s) FAILED.`); process.exit(1); }
console.log('\n✅ speaker-activity: header + frame + hint shapes hold, the ceiling caps cleanly, no PCM ever lands, and a broken writer never throws.');
