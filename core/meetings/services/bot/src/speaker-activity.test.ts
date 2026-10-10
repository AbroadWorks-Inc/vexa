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
import { mkdtempSync, readFileSync, statSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import {
  createSpeakerActivityWriter,
  resolveMaxActivityBytes,
  DEFAULT_MAX_ACTIVITY_BYTES,
} from './speaker-activity.js';
import { makeSpeakerHintSink, makeSpeakerActivityFrameTap, makeChannelActivitySink, startCaptureBridge } from './capture-bridge.js';
import type { BotPipeline } from './pipeline.js';
import { ACTIVITY_UPLOAD_SLACK_BYTES } from './signal-upload.js';
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

// ── 5) Lane: zoom is pertrack; teams stays mixed ──────────────────────────────────────────────────
{
  const expected: Record<string, string> = { zoom: 'pertrack', teams: 'mixed' };
  for (const platform of ['zoom', 'teams']) {
    const dir = mkdtempSync(join(tmpdir(), 'vexa-speaker-activity-'));
    const w = createSpeakerActivityWriter(invOf(platform), { dir, now: () => 0 });
    await w.close();
    const header = readLines(w.path)[0];
    check(`platform ${platform} → header lane:"${expected[platform]}"`, header.lane === expected[platform], JSON.stringify(header));
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

// ── 9) closed guard: frame()/hint() are no-ops once close() has run ──────────────────────────────
{
  const dir = mkdtempSync(join(tmpdir(), 'vexa-speaker-activity-'));
  // A tiny buffer cap: with NO closed guard, a single post-close frame line would trip an
  // immediate fire-and-forget flush (push() → bufBytes >= maxBuffer → flush()), independent of
  // the timer (cleared by close()) and of calling close() a second time — so this proves frame()
  // itself is a no-op rather than merely that a second close() re-flushes nothing.
  const w = createSpeakerActivityWriter(invOf('google_meet'), { dir, now: () => 0, maxBufferBytes: 1 });
  w.frame(0, Float32Array.of(0.1, -0.1), 1000, 'Ann');
  await w.close();
  const beforeLen = readFileSync(w.path, 'utf8').length;

  let threw = false;
  try {
    for (let i = 0; i < 20; i++) w.frame(0, Float32Array.of(0.2, -0.2), 2000 + i, 'Bo');
    w.hint(3000, 'Cy');
  } catch { threw = true; }
  // Give any wrongly-fired fire-and-forget flush a chance to land on disk before we check.
  await new Promise((r) => setTimeout(r, 100));

  const afterLen = readFileSync(w.path, 'utf8').length;
  check('frame()/hint() after close() never throw', !threw);
  check('frame()/hint() after close() are TRUE no-ops (nothing reaches the file, even past the buffer threshold)',
    afterLen === beforeLen, `${beforeLen} -> ${afterLen}`);
}

// ── 10) makeSpeakerHintSink taps the writer: a hint line is written even with telemetry unset ────
{
  const dir = mkdtempSync(join(tmpdir(), 'vexa-speaker-activity-'));
  const w = createSpeakerActivityWriter(invOf('teams'), { dir, now: () => 0 });
  const received: Array<{ name: string; t: number; isEnd?: boolean }> = [];
  const pipelineStub = { recordHint: (name: string, t: number, isEnd?: boolean): void => { received.push({ name, t, isEnd }); } };
  const { sink } = makeSpeakerHintSink(pipelineStub, () => { /* quiet */ }, undefined, w);
  const epoch = Date.now();
  sink('Ann', epoch, true);
  await w.close();

  check('pipeline.recordHint still fires when telemetry is undefined',
    received.length === 1 && received[0].name === 'Ann' && received[0].isEnd === true, JSON.stringify(received));
  const hintLines = readLines(w.path).filter((l) => l.type === 'hint');
  check('a hint line lands in speaker-activity.jsonl even though telemetry is undefined',
    hintLines.length === 1 && hintLines[0].name === 'Ann' && hintLines[0].t === epoch && hintLines[0].isEnd === true,
    JSON.stringify(hintLines));
}

// ── 10b) kind is written only when the hint names its source ─────────────────────────────────────
{
  const dir = mkdtempSync(join(tmpdir(), 'vexa-speaker-activity-'));
  const w = createSpeakerActivityWriter(invOf('jitsi'), { dir, now: () => 0 });
  w.hint(1000, 'Alice', false, 'levels');
  w.hint(2000, 'Bob', false);
  const received: Array<{ name: string; t: number; isEnd?: boolean; kind?: string }> = [];
  const pipelineStub = {
    recordHint: (name: string, t: number, isEnd?: boolean): void => { received.push({ name, t, isEnd }); },
  };
  const { sink } = makeSpeakerHintSink(pipelineStub, () => { /* quiet */ }, undefined, w);
  sink('Cara', Date.now(), true, 'levels');
  await w.close();

  const hintLines = readLines(w.path).filter((l) => l.type === 'hint');
  check('a levels hint writes kind',
    hintLines.some((l) => l.name === 'Alice' && l.kind === 'levels'), JSON.stringify(hintLines));
  check('a hint without a source has no kind key',
    hintLines.some((l) => l.name === 'Bob' && !('kind' in l)), JSON.stringify(hintLines));
  check('makeSpeakerHintSink forwards kind levels',
    hintLines.some((l) => l.name === 'Cara' && l.kind === 'levels' && l.isEnd === true), JSON.stringify(hintLines));
  check('kind is not passed into recordHint',
    received.length === 1 && received[0].name === 'Cara' && !('kind' in received[0]), JSON.stringify(received));
}

// ── 11) makeSpeakerHintSink taps the writer: skew re-stamping applies to the written t ───────────
{
  const dir = mkdtempSync(join(tmpdir(), 'vexa-speaker-activity-'));
  const w = createSpeakerActivityWriter(invOf('teams'), { dir, now: () => 0 });
  const warns: string[] = [];
  const pipelineStub = { recordHint: (): void => { /* not under test */ } };
  const { sink } = makeSpeakerHintSink(pipelineStub, (m) => warns.push(m), undefined, w);
  sink('Bo', 12345); // performance.now()-shaped — implausible skew off the epoch clock
  await w.close();

  const hintLine = readLines(w.path).find((l) => l.type === 'hint') as Record<string, unknown> | undefined;
  check('the skew warns loudly', warns.length === 1 && /hint-clock-skew/.test(warns[0] ?? ''), JSON.stringify(warns));
  check('the written hint t is re-stamped to epoch, not the implausible 12345',
    typeof hintLine?.t === 'number' && Math.abs((hintLine.t as number) - Date.now()) < 5000, JSON.stringify(hintLine));
}

// ── 12) makeSpeakerActivityFrameTap: a named and an unnamed frame both land in the file ─────────
// This is the tap startCaptureBridge feeds from both audio callbacks — on Meet it is the naming
// source, so a frame that never reaches the writer is a name the transcript never gets.
{
  const dir = mkdtempSync(join(tmpdir(), 'vexa-speaker-activity-'));
  const w = createSpeakerActivityWriter(invOf('google_meet'), { dir, now: () => 0 });
  const tap = makeSpeakerActivityFrameTap(w);
  tap(0, Float32Array.of(0.5, -0.5), 1000, 'Ann');
  tap(1, Float32Array.of(0.1, -0.1), 1100);
  await w.close();

  const frames = readLines(w.path).slice(1);
  check('the tap writes exactly the two frames', frames.length === 2, JSON.stringify(frames));
  check('the named frame lands with its name, channel and time',
    sameShape(frames[0], { t: 1000, ch: 0, name: 'Ann', rms: 0.5, dur_ms: 0 }), JSON.stringify(frames[0]));
  check('the unnamed frame lands with no name key',
    sameShape(frames[1], { t: 1100, ch: 1, rms: 0.1, dur_ms: 0 }), JSON.stringify(frames[1]));
}

// ── 13) makeSpeakerActivityFrameTap never throws: no writer, or a writer that throws ─────────────
{
  let threw = false;
  try {
    makeSpeakerActivityFrameTap(undefined)(0, Float32Array.of(0.1), 1000, 'Ann');
    const exploding = {
      path: '/dev/null',
      frame: (): void => { throw new Error('writer broke'); },
      hint: (): void => { /* unused */ },
      isCapped: () => false,
      close: async (): Promise<void> => { /* unused */ },
    };
    makeSpeakerActivityFrameTap(exploding)(0, Float32Array.of(0.1), 1000, 'Ann');
  } catch { threw = true; }
  check('the frame tap never throws (no writer, or a writer that throws)', !threw);
}

// ── 14) The ceiling counts UTF-8 bytes, not string length ────────────────────────────────────────
{
  // A synthetic multi-byte display name: each CJK character is 3 bytes on disk but 1 in .length.
  const name = '試験話者'.repeat(25);
  const maxBytes = 200_000;
  const dir = mkdtempSync(join(tmpdir(), 'vexa-speaker-activity-'));
  const w = createSpeakerActivityWriter(invOf('google_meet', 'utf8-sess'), { dir, maxBytes, now: () => 0 });
  for (let i = 0; i < 5000; i++) w.frame(0, Float32Array.of(0.1, -0.1), 1000 + i, name);
  await w.close();

  const onDisk = statSync(w.path).size;
  check('the writer capped (precondition)', w.isCapped());
  check('on-disk bytes stay within ceiling + upload slack for a non-ASCII name',
    onDisk <= maxBytes + ACTIVITY_UPLOAD_SLACK_BYTES, `${onDisk} > ${maxBytes} + ${ACTIVITY_UPLOAD_SLACK_BYTES}`);
  const capLine = readLines(w.path).find((l) => l.type === 'capped') as Record<string, unknown> | undefined;
  check('the capped line reports the bytes actually on disk before it',
    typeof capLine?.bytes === 'number' && (capLine.bytes as number) <= maxBytes
      && onDisk - (capLine.bytes as number) === Buffer.byteLength(JSON.stringify(capLine) + '\n', 'utf8'),
    `${JSON.stringify(capLine)} onDisk=${onDisk}`);
}

// ── 15) The default ceiling is 128 MiB ───────────────────────────────────────────────────────────
{
  check('DEFAULT_MAX_ACTIVITY_BYTES is 128 MiB (134217728)', DEFAULT_MAX_ACTIVITY_BYTES === 134217728,
    `${DEFAULT_MAX_ACTIVITY_BYTES}`);
}

// ── 16) A channel-tap frame carries src:"channel"; every other frame is byte-identical to before ──
{
  const dir = mkdtempSync(join(tmpdir(), 'vexa-speaker-activity-'));
  const w = createSpeakerActivityWriter(invOf('jitsi'), { dir, now: () => 0 });
  w.frame(0, Float32Array.of(0.5, -0.5), 1000);
  w.frame(0, Float32Array.of(0.5, -0.5), 1000, undefined, 'channel');
  await w.close();
  const raw = readFileSync(w.path, 'utf8').split('\n').filter((l) => l.length > 0).slice(1);
  check('a mix frame line has no src key (unchanged bytes)',
    raw[0] === '{"t":1000,"ch":0,"rms":0.5,"dur_ms":0}', raw[0]);
  check('a channel frame line carries src:"channel"',
    raw[1] === '{"t":1000,"ch":0,"rms":0.5,"dur_ms":0,"src":"channel"}', raw[1]);
}

// ── 17) The __vexaChannelActivity sink writes src:"channel" and rejects a bad channel ───────────
{
  const dir = mkdtempSync(join(tmpdir(), 'vexa-speaker-activity-'));
  const w = createSpeakerActivityWriter(invOf('jitsi'), { dir, now: () => 0 });
  const sink = makeChannelActivitySink(w);
  sink(3, 0.5, 4096, 1000);
  sink(-1, 0.5, 4096, 1000);
  sink(1.5, 0.5, 4096, 1000);
  sink(2, Number.NaN, 4096, 1000);
  await w.close();
  const frames = readLines(w.path).slice(1);
  check('the channel sink writes exactly the one valid frame', frames.length === 1, JSON.stringify(frames));
  check('the channel sink frame is src:"channel" on its channel',
    sameShape(frames[0], { t: 1000, ch: 3, rms: 0.5, dur_ms: 256, src: 'channel' }), JSON.stringify(frames[0]));
}

// ── 18) Jitsi with per-channel recording on: the mix activity is still written, channel frames are added ──
const bridgeCase = async (perChannel: boolean | undefined): Promise<{ lines: Record<string, unknown>[]; exposed: string[] }> => {
  const dir = mkdtempSync(join(tmpdir(), 'vexa-speaker-activity-'));
  const inv = { ...invOf('jitsi'), ...(perChannel === undefined ? {} : { perChannelRecordingEnabled: perChannel }) } as Invocation;
  const w = createSpeakerActivityWriter(inv, { dir, now: () => 0 });
  const fns: Record<string, (...a: unknown[]) => unknown> = {};
  const page = {
    async exposeFunction(name: string, fn: (...a: unknown[]) => unknown): Promise<void> { fns[name] = fn; },
    async evaluate(): Promise<unknown> { return undefined; },   // no page: only the Node-side sinks are driven
  } as never;
  const pipeline = {
    async start() { /* not driven */ }, async stop() { /* not driven */ },
    feedAudio() { /* not driven */ }, feedMixedAudio() { /* not driven */ }, recordHint() { /* not driven */ },
  } as unknown as BotPipeline;
  const stop = await startCaptureBridge(page, inv, pipeline, undefined, undefined, undefined, w);
  fns.__vexaPerSpeakerAudioData?.(0, [0.5, -0.5], 1000);
  fns.__vexaChannelActivity?.(0, 0.25, 4096, 1100);
  await stop();
  await w.close();
  return { lines: readLines(w.path).slice(1), exposed: Object.keys(fns) };
};
{
  const on = await bridgeCase(true);
  check('flag on: __vexaChannelActivity is exposed for jitsi', on.exposed.includes('__vexaChannelActivity'), on.exposed.join(','));
  check('flag on: the mix frame is still written, with no src and no channel number',
    on.lines.some((l) => sameShape(l, { t: 1000, rms: 0.5, dur_ms: 0 })), JSON.stringify(on.lines));
  check('flag on: the channel frame is added with src:"channel"',
    on.lines.some((l) => sameShape(l, { t: 1100, ch: 0, rms: 0.25, dur_ms: 256, src: 'channel' })), JSON.stringify(on.lines));
  const off = await bridgeCase(undefined);
  check('flag absent: __vexaChannelActivity is not exposed', !off.exposed.includes('__vexaChannelActivity'), off.exposed.join(','));
  check('flag absent: the mix frame is written, with no channel number', off.lines.some((l) => sameShape(l, { t: 1000, rms: 0.5, dur_ms: 0 })),
    JSON.stringify(off.lines));
}

if (failed) { console.error(`\n❌ speaker-activity: ${failed} check(s) FAILED.`); process.exit(1); }
console.log('\n✅ speaker-activity: header + frame + hint shapes hold, the ceiling caps cleanly, no PCM ever lands, and a broken writer never throws.');
