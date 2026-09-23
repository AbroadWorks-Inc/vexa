/**
 * O-TEL-1c — the tape's DELIVER half + the recorder's size cap. OFFLINE, NO browser/redis/whisper.
 *
 * Everything here is about ONE property: fixture collection is default ON in prod, so the tape must
 * be incapable of harming the meeting it is taping. That splits into two halves:
 *
 *   • the CAP (telemetry.ts) — a runaway tape stops writing and the meeting carries on. A bot that
 *     dies of a full disk is a lost MEETING; a capped tape is only a shorter fixture.
 *   • the UPLOAD (signal-upload.ts) — every failure path is logged and dropped: no rethrow, no
 *     retry, no change to the exit path, and no upload at all when collection is off.
 *
 * The uploader's real transport is exercised against a loopback http server (no network, no
 * mocks of node:http) so the multipart framing, the media_type=signal metadata, the streaming
 * and the timeout are proven as shipped rather than as described.
 * Run: npx tsx src/signal-upload.test.ts
 */
import { createServer, type IncomingMessage, type Server } from 'node:http';
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import {
  DEFAULT_UPLOAD_TIMEOUT_MS,
  sttTapePath,
  streamingTapeUploader,
  uploadSignalTapes,
  uploadSpeakerActivity,
  type TapePart,
} from './signal-upload.js';
import { createCaptureSignalRecorder, resolveMaxTapeBytes, DEFAULT_MAX_TAPE_BYTES } from './telemetry.js';
import { createSpeakerActivityWriter } from './speaker-activity.js';
import type { Invocation } from './config.js';
import type { CapturedFrame } from './ports.js';
import type { CsrcRecord, ObservationRecord, TeamsCaptionRecord } from './capture-bridge.js';

let failed = 0;
const check = (name: string, cond: boolean, detail = ''): void => {
  console.log(`  ${cond ? '✅' : '❌'} ${name}${cond ? '' : '  — ' + detail}`);
  if (!cond) failed++;
};

const inv = (over: Partial<Invocation> = {}): Invocation => ({
  platform: 'teams',
  meetingUrl: 'https://teams.microsoft.com/l/meetup-join/x',
  botName: 'VexaBot',
  redisUrl: 'redis://redis:6379/0',
  connectionId: 'conn-tape-1',
  nativeMeetingId: 'nat-1',
  meeting_id: 42,
  internalSecret: 'internal-secret',
  ...over,
} as Invocation);

const frame = (n: number): CapturedFrame => ({
  t: n, pcm: Buffer.alloc(320).toString('base64'), speaker: `S${n % 3}`, seq: n, rms: 0.1,
} as CapturedFrame);

const dirs: string[] = [];
const tmp = (): string => {
  const d = mkdtempSync(join(tmpdir(), 'vexa-signal-'));
  dirs.push(d);
  return d;
};

// ── the size cap ────────────────────────────────────────────────────────────────────────────────
console.log('\n── tape size cap ──');
{
  const dir = tmp();
  // A cap barely above the header: the first frame line must trip it.
  const rec = createCaptureSignalRecorder(inv(), { dir, maxBytes: 400 });
  const before = rec.bytesWritten();
  check('header counts toward the cap', before > 0 && before < 400, String(before));
  for (let i = 0; i < 200; i++) rec.sink.captureFrame(frame(i));
  check('cap stops the writer', rec.isCapped(), 'recorder kept writing past its cap');
  check('bytes never exceed the cap', rec.bytesWritten() <= 400, String(rec.bytesWritten()));
  // The whole point: capture keeps working. A capped sink must swallow, never throw.
  let threw = false;
  try { rec.sink.captureFrame(frame(999)); rec.sink.captureHint({ speaker: 'S1', t: 1 } as never); }
  catch { threw = true; }
  check('a capped tape never throws into capture', !threw);
  await rec.close();
}
{
  // A generous cap is not reached — the ordinary case must be unaffected by the gate's existence.
  const dir = tmp();
  const rec = createCaptureSignalRecorder(inv(), { dir, maxBytes: 10 * 1024 * 1024 });
  for (let i = 0; i < 50; i++) rec.sink.captureFrame(frame(i));
  check('an ordinary session is not capped', !rec.isCapped());
  check('bytes are counted', rec.bytesWritten() > 500, String(rec.bytesWritten()));
  await rec.close();
}
{
  check('cap default when env unset', resolveMaxTapeBytes(undefined) === DEFAULT_MAX_TAPE_BYTES);
  // The .env.example failure class (v0.12.5): a set-but-EMPTY line must read as unset, not as 0 —
  // and a garbled value must not read as "record without bound".
  check('cap default when env empty', resolveMaxTapeBytes('') === DEFAULT_MAX_TAPE_BYTES);
  check('cap default when env garbled', resolveMaxTapeBytes('lots') === DEFAULT_MAX_TAPE_BYTES);
  check('cap default when env <= 0', resolveMaxTapeBytes('0') === DEFAULT_MAX_TAPE_BYTES);
  check('explicit cap honored', resolveMaxTapeBytes('1024') === 1024);
}

// ── the Teams CC sidecar ────────────────────────────────────────────────────────────────────────
console.log('\n── caption sidecar ──');
{
  const dir = tmp();
  const rec = createCaptureSignalRecorder(inv(), { dir });
  const caption = (name: string, text: string): TeamsCaptionRecord => ({
    type: 'caption', t: Date.now(), platform: 'teams', name, text, stable: true, lane: 'mixed',
  });
  rec.sink.captureCaption?.(caption('Jacob', 'hello there'));
  rec.sink.captureCaption?.(caption('Preeti', 'hi Jacob'));
  rec.sink.captureFrame(frame(0));
  // No sleep: close() drains the caption chain, because the teardown upload reads this file the
  // instant close() returns. A test that had to sleep here would be hiding that contract.
  await rec.close();

  const lines = readFileSync(rec.captionsPath, 'utf8').trim().split('\n').map((l) => JSON.parse(l));
  // A sidecar travels alone constantly — an S3 listing, a curator's download, a bug report with one
  // file attached — so it opens with its own mini-header naming the session and the build.
  check('the sidecar opens with a self-identifying mini-header',
    lines[0].type === 'sidecar_header' && lines[0].part === 'captions'
    && lines[0].session_uid === 'conn-tape-1' && typeof lines[0].image_version === 'string',
    JSON.stringify(lines[0]));
  check('captions land in the sidecar', lines.length === 3, String(lines.length));
  check('caption records are stored verbatim',
    lines[1].name === 'Jacob' && lines[1].text === 'hello there' && lines[1].type === 'caption',
    JSON.stringify(lines[1]));

  // The tape itself must stay contract-clean — captured-signal.v1 is sealed at three records, and
  // a caption line inside it would make every stored session fail its own schema.
  const tape = readFileSync(rec.path, 'utf8').trim().split('\n').map((l) => JSON.parse(l));
  check('no caption ever enters the tape', tape.every((l) => l.type !== 'caption'),
    JSON.stringify(tape.map((l) => l.type)));
  check('the sidecar sits beside the tape',
    rec.captionsPath === rec.path.replace('.captured-signal.jsonl', '.captions.jsonl'), rec.captionsPath);
}
{
  // Captions share the ONE budget: a chatty caption stream can fill a disk exactly like a chatty
  // audio stream, so the cap must cover it rather than only the frames.
  const dir = tmp();
  const written: string[] = [];
  const rec = createCaptureSignalRecorder(inv(), {
    dir, maxBytes: 400, captionWriter: (l) => { written.push(l); },
  });
  for (let i = 0; i < 50; i++) {
    rec.sink.captureCaption?.({ type: 'caption', t: 1, platform: 'teams', name: `S${i}`,
                                text: 'x'.repeat(40), stable: true, lane: 'mixed' });
  }
  check('the cap covers captions too', rec.isCapped(), 'captions wrote past the cap');
  check('captions stop being written once capped', written.length < 50, String(written.length));
  check('caption bytes count toward the cap', rec.bytesWritten() <= 400, String(rec.bytesWritten()));
  await rec.close();
}
{
  // A recorder that never got a writer (collection off / init failure) must not create a sidecar.
  const dir = tmp();
  const rec = createCaptureSignalRecorder(inv(), { dir });
  await rec.close();
  check('no captions → no sidecar file', !existsSync(rec.captionsPath));
}

// ── the transport (CSRC) sidecar ────────────────────────────────────────────────────────────────
console.log('\n── csrc sidecar ──');
{
  const dir = tmp();
  const rec = createCaptureSignalRecorder(inv(), { dir });
  const edge = (csrc: number, active: boolean, t: number): CsrcRecord =>
    ({ type: 'csrc', t, csrc, active, audioLevel: 0.5, rtpTimestamp: csrc * 10, lane: 'mixed' });
  // Order is the whole content of this stream: an activation filed after its own deactivation
  // would describe a meeting that never happened.
  rec.sink.captureCsrc?.(edge(101, true, 1_700_000_000_000));
  rec.sink.captureCsrc?.(edge(202, true, 1_700_000_000_100));
  rec.sink.captureCsrc?.(edge(101, false, 1_700_000_000_500));
  rec.sink.captureFrame(frame(0));
  // No sleep: close() drains the csrc chain, because the teardown upload reads this file the
  // instant close() returns. A test that had to sleep here would be hiding that contract.
  await rec.close();

  const all = readFileSync(rec.csrcPath, 'utf8').trim().split('\n').map((l) => JSON.parse(l));
  check('the sidecar opens with its own mini-header naming part + build',
    all[0].type === 'sidecar_header' && all[0].part === 'csrc' && typeof all[0].image_version === 'string',
    JSON.stringify(all[0]));
  const lines = all.slice(1);
  check('transitions land in the sidecar, in arrival order',
    lines.length === 3 && lines.map((l) => `${l.csrc}:${l.active}`).join(',') === '101:true,202:true,101:false',
    JSON.stringify(lines.map((l) => `${l.csrc}:${l.active}`)));
  check('the record is stored verbatim (epoch t, level and rtp timestamp carried)',
    lines[0].type === 'csrc' && lines[0].t === 1_700_000_000_000 && lines[0].lane === 'mixed'
    && lines[0].audioLevel === 0.5 && lines[0].rtpTimestamp === 1010, JSON.stringify(lines[0]));

  // captured-signal.v1 is sealed at three records — a transition inside the tape would make every
  // stored session fail its own schema.
  const tape = readFileSync(rec.path, 'utf8').trim().split('\n').map((l) => JSON.parse(l));
  check('no transition ever enters the tape', tape.every((l) => l.type !== 'csrc'),
    JSON.stringify(tape.map((l) => l.type)));
  check('the sidecar sits beside the tape',
    rec.csrcPath === rec.path.replace('.captured-signal.jsonl', '.csrc.jsonl'), rec.csrcPath);
}
{
  // A 100ms sensor over a busy meeting can fill a disk exactly like a chatty caption stream, so it
  // shares the ONE budget rather than getting its own.
  const dir = tmp();
  const written: string[] = [];
  const rec = createCaptureSignalRecorder(inv(), {
    dir, maxBytes: 400, csrcWriter: (l) => { written.push(l); },
  });
  for (let i = 0; i < 200; i++) {
    rec.sink.captureCsrc?.({ type: 'csrc', t: 1, csrc: i, active: i % 2 === 0, lane: 'mixed' });
  }
  check('the cap covers transitions too', rec.isCapped(), 'csrc wrote past the cap');
  check('transitions stop being written once capped', written.length < 200, String(written.length));
  check('csrc bytes count toward the cap', rec.bytesWritten() <= 400, String(rec.bytesWritten()));
  let threw = false;
  try { rec.sink.captureCsrc?.({ type: 'csrc', t: 1, csrc: 9, active: true, lane: 'mixed' }); }
  catch { threw = true; }
  check('a capped csrc sink never throws into capture', !threw);
  await rec.close();
}
{
  // A meeting that observed no transport sources (the gmeet lane, or a client that never mixes
  // server-side) must leave no sidecar at all — an empty file would upload as a fixture that lies.
  const dir = tmp();
  const rec = createCaptureSignalRecorder(inv(), { dir });
  await rec.close();
  check('no transitions → no sidecar file', !existsSync(rec.csrcPath));
}

// ── the observations sidecar ────────────────────────────────────────────────────────────────────
console.log('\n── observations sidecar ──');
{
  const dir = tmp();
  const rec = createCaptureSignalRecorder(inv(), { dir });
  const obs = (source: string, o: Record<string, unknown>, t: number): ObservationRecord =>
    ({ type: 'observation', t, source, lane: 'mixed', observation: o });
  rec.sink.captureObservation?.(obs('teams-speakers', { type: 'signal-absent', tiles: 4 }, 1_700_000_000_000));
  rec.sink.captureObservation?.(obs('mixed', { type: 'mix-topology', streams: 1 }, 1_700_000_000_050));
  rec.sink.captureObservation?.(obs('csrc', { kind: 'csrc-poll-error', where: 'receivers' }, 1_700_000_000_100));
  await rec.close();

  const all = readFileSync(rec.observationsPath, 'utf8').trim().split('\n').map((l) => JSON.parse(l));
  check('the observations sidecar opens with its mini-header',
    all[0].type === 'sidecar_header' && all[0].part === 'observations'
    && all[0].platform === 'teams' && typeof all[0].image_version === 'string', JSON.stringify(all[0]));
  const lines = all.slice(1);
  check('observations land in arrival order, one per line',
    lines.length === 3 && lines.map((l) => l.source).join(',') === 'teams-speakers,mixed,csrc',
    JSON.stringify(lines.map((l) => l.source)));
  // The producer's payload is the evidence; a bridge that normalized it would be deciding at
  // capture time what a later analysis is allowed to see.
  check('the producer payload is carried VERBATIM under `observation`',
    lines[0].observation.type === 'signal-absent' && lines[0].observation.tiles === 4
    && lines[1].observation.streams === 1 && lines[2].observation.where === 'receivers',
    JSON.stringify(lines.map((l) => l.observation)));
  check('each observation carries the epoch clock the audio shares',
    lines[0].t === 1_700_000_000_000 && lines[2].t === 1_700_000_000_100, JSON.stringify(lines.map((l) => l.t)));

  const tape = readFileSync(rec.path, 'utf8').trim().split('\n').map((l) => JSON.parse(l));
  check('no observation ever enters the tape', tape.every((l) => l.type !== 'observation'),
    JSON.stringify(tape.map((l) => l.type)));
}
{
  // Observations share the ONE budget like every other sidecar.
  const dir = tmp();
  const written: string[] = [];
  const rec = createCaptureSignalRecorder(inv(), {
    dir, maxBytes: 400, observationWriter: (l) => { written.push(l); },
  });
  for (let i = 0; i < 100; i++) {
    rec.sink.captureObservation?.({ type: 'observation', t: 1, source: 'csrc', lane: 'mixed',
                                    observation: { kind: 'csrc-poll-error', n: i, pad: 'x'.repeat(40) } });
  }
  check('the cap covers observations too', rec.isCapped(), 'observations wrote past the cap');
  check('observation bytes count toward the cap', rec.bytesWritten() <= 400, String(rec.bytesWritten()));
  let threw = false;
  try { rec.sink.captureObservation?.({ type: 'observation', t: 1, source: 'x', lane: 'mixed', observation: {} }); }
  catch { threw = true; }
  check('a capped observation sink never throws into capture', !threw);
  await rec.close();
}
{
  const dir = tmp();
  const rec = createCaptureSignalRecorder(inv(), { dir });
  await rec.close();
  check('no observations → no sidecar file (an empty file would claim a stream we never saw)',
    !existsSync(rec.observationsPath));
}

// ── the build stamp ─────────────────────────────────────────────────────────────────────────────
console.log('\n── image version ──');
{
  const saved = process.env.VEXA_IMAGE_VERSION;
  process.env.VEXA_IMAGE_VERSION = 'a1b2c3d';
  const dir = tmp();
  const rec = createCaptureSignalRecorder(inv(), { dir });
  rec.sink.captureCsrc?.({ type: 'csrc', t: 1, csrc: 1, active: true, lane: 'mixed' });
  await rec.close();
  const header = JSON.parse(readFileSync(rec.path, 'utf8').split('\n')[0]);
  check('the tape header names the build that produced it', header.image_version === 'a1b2c3d',
    JSON.stringify(header));
  const side = JSON.parse(readFileSync(rec.csrcPath, 'utf8').split('\n')[0]);
  check('each sidecar carries the same build stamp, so it is self-identifying when separated',
    side.image_version === 'a1b2c3d', JSON.stringify(side));

  // An unstamped image must say so rather than guess.
  process.env.VEXA_IMAGE_VERSION = '';
  const rec2 = createCaptureSignalRecorder(inv(), { dir: tmp() });
  await rec2.close();
  const header2 = JSON.parse(readFileSync(rec2.path, 'utf8').split('\n')[0]);
  check("an unstamped build records 'unknown', never a guess", header2.image_version === 'unknown',
    JSON.stringify(header2));
  if (saved === undefined) delete process.env.VEXA_IMAGE_VERSION; else process.env.VEXA_IMAGE_VERSION = saved;
}

// ── teardown upload ─────────────────────────────────────────────────────────────────────────────
console.log('\n── teardown upload ──');
{
  // Recorder off (collection disabled) → nothing is attempted at all.
  const calls: TapePart[] = [];
  const out = await uploadSignalTapes(null, {
    inv: inv({ recordingUploadUrl: 'http://127.0.0.1:1/x' }),
    upload: async (p) => { calls.push(p); },
  });
  check('no recorder → no upload', calls.length === 0 && out.uploaded.length === 0);
}
{
  const dir = tmp();
  const rec = createCaptureSignalRecorder(inv(), { dir });
  for (let i = 0; i < 5; i++) rec.sink.captureFrame(frame(i));
  await rec.close();
  writeFileSync(sttTapePath(rec.path), '{"ok":true}\n');
  writeFileSync(rec.csrcPath, '{"type":"csrc","t":1,"csrc":7,"active":true,"lane":"mixed"}\n');
  writeFileSync(rec.observationsPath, '{"type":"observation","t":1,"source":"csrc","lane":"mixed","observation":{}}\n');

  const seen: Array<[TapePart, number]> = [];
  const out = await uploadSignalTapes(rec, {
    inv: inv({ recordingUploadUrl: 'http://127.0.0.1:1/x' }),
    upload: async (part, _p, size) => { seen.push([part, size]); },
  });
  check('every present tape part uploads, transport sidecar included',
    out.uploaded.join(',') === 'captured-signal,stt,csrc,observations', out.uploaded.join(','));
  check('sizes are passed through', seen.every(([, s]) => s > 0), JSON.stringify(seen));
}
{
  // The stt tape only exists when transcription ran — its absence is normal, not a failure.
  const dir = tmp();
  const rec = createCaptureSignalRecorder(inv(), { dir });
  rec.sink.captureFrame(frame(0));
  await rec.close();
  const out = await uploadSignalTapes(rec, {
    inv: inv({ recordingUploadUrl: 'http://127.0.0.1:1/x' }),
    upload: async () => { /* accept */ },
  });
  check('absent sidecars are skipped, not failed',
    out.uploaded.join(',') === 'captured-signal'
      && out.skipped.join(',') === 'stt,captions,csrc,observations,botlog,transcript' && out.failed.length === 0,
    JSON.stringify(out));
}
{
  // THE contract with teardown: a failing upload must not reject, so the finally block that calls
  // it cannot throw and the exit code the orchestrator already returned stands.
  const dir = tmp();
  const rec = createCaptureSignalRecorder(inv(), { dir });
  rec.sink.captureFrame(frame(0));
  await rec.close();
  let rejected = false;
  const out = await uploadSignalTapes(rec, {
    inv: inv({ recordingUploadUrl: 'http://127.0.0.1:1/x' }),
    upload: async () => { throw new Error('object store is down'); },
  }).catch(() => { rejected = true; return null; });
  check('a failed upload never rejects', !rejected && out !== null);
  check('a failed upload is recorded as failed', out !== null && out.failed.includes('captured-signal'));
}
{
  // ONE attempt. Retrying inside the SIGTERM grace risks turning a clean exit into a SIGKILL, which
  // costs the meeting — the thing the fixture exists to serve.
  const dir = tmp();
  const rec = createCaptureSignalRecorder(inv(), { dir });
  rec.sink.captureFrame(frame(0));
  await rec.close();
  let attempts = 0;
  await uploadSignalTapes(rec, {
    inv: inv({ recordingUploadUrl: 'http://127.0.0.1:1/x' }),
    upload: async (part) => { if (part === 'captured-signal') { attempts++; throw new Error('nope'); } },
  });
  check('upload is attempted exactly once', attempts === 1, `attempts=${attempts}`);
}
{
  // Oversized → SKIPPED, never truncated: a truncated tape reads as a complete one to a replay.
  const dir = tmp();
  const rec = createCaptureSignalRecorder(inv(), { dir });
  for (let i = 0; i < 20; i++) rec.sink.captureFrame(frame(i));
  await rec.close();
  let attempted = false;
  const out = await uploadSignalTapes(rec, {
    inv: inv({ recordingUploadUrl: 'http://127.0.0.1:1/x' }),
    maxBytes: 10,
    upload: async () => { attempted = true; },
  });
  check('an oversized tape is skipped, not truncated', !attempted && out.skipped.includes('captured-signal'));
}
{
  // No control plane (the local hot loop) — quiet skip, not a failure.
  const dir = tmp();
  const rec = createCaptureSignalRecorder(inv(), { dir });
  rec.sink.captureFrame(frame(0));
  await rec.close();
  const out = await uploadSignalTapes(rec, { inv: inv() });   // no recordingUploadUrl
  check('no upload URL → skipped', out.uploaded.length === 0 && out.failed.length === 0);
}

// ── speaker-activity upload — independent of the debug tape ────────────────────────────────────
console.log('\n── speaker-activity upload ──');
{
  // The file is non-empty (a header + one frame) → uploads as part 'speaker-activity'.
  const dir = tmp();
  const w = createSpeakerActivityWriter(inv(), { dir });
  w.frame(0, Float32Array.of(0.1, -0.1), 1000, 'Ann');
  await w.close();

  const seen: Array<[TapePart, string, number]> = [];
  const out = await uploadSpeakerActivity(w, {
    inv: inv({ recordingUploadUrl: 'http://127.0.0.1:1/x' }),
    upload: async (part, path, size) => { seen.push([part, path, size]); },
  });
  check("uploads part 'speaker-activity' with the writer's own path",
    out === 'uploaded' && seen.length === 1 && seen[0][0] === 'speaker-activity' && seen[0][1] === w.path && seen[0][2] > 0,
    JSON.stringify({ out, seen }));
}
{
  // No recordingUploadUrl → skipped, same as the tape's own quiet skip.
  const dir = tmp();
  const w = createSpeakerActivityWriter(inv(), { dir });
  w.frame(0, Float32Array.of(0.1), 1000, 'Ann');
  await w.close();
  let called = false;
  const out = await uploadSpeakerActivity(w, { inv: inv(), upload: async () => { called = true; } });
  check('no recordingUploadUrl → skipped, nothing attempted', out === 'skipped' && !called);
}
{
  // A null writer (should never happen — the writer is created unconditionally — but the
  // function must degrade the same way the tape's does) → skipped.
  let called = false;
  const out = await uploadSpeakerActivity(null, {
    inv: inv({ recordingUploadUrl: 'http://127.0.0.1:1/x' }),
    upload: async () => { called = true; },
  });
  check('a null writer → skipped, nothing attempted', out === 'skipped' && !called);
}
{
  // A rejecting transport → 'failed', never a thrown/rejected promise.
  const dir = tmp();
  const w = createSpeakerActivityWriter(inv(), { dir });
  w.frame(0, Float32Array.of(0.1), 1000, 'Ann');
  await w.close();
  let rejected = false;
  const out = await uploadSpeakerActivity(w, {
    inv: inv({ recordingUploadUrl: 'http://127.0.0.1:1/x' }),
    upload: async () => { throw new Error('object store is down'); },
  }).catch(() => { rejected = true; return null; });
  check("a failing upload never rejects, and resolves 'failed'", !rejected && out === 'failed');
}
{
  // Independent of the debug tape: the tape recorder is off (null) and uploads nothing, while the
  // speaker-activity writer still ships on its own.
  const dir = tmp();
  const w = createSpeakerActivityWriter(inv(), { dir });
  w.frame(0, Float32Array.of(0.1), 1000, 'Ann');
  await w.close();
  const tapeCalls: TapePart[] = [];
  const activityCalls: TapePart[] = [];
  const tapeOut = await uploadSignalTapes(null, {
    inv: inv({ recordingUploadUrl: 'http://127.0.0.1:1/x' }),
    upload: async (p) => { tapeCalls.push(p); },
  });
  const activityOut = await uploadSpeakerActivity(w, {
    inv: inv({ recordingUploadUrl: 'http://127.0.0.1:1/x' }),
    upload: async (p) => { activityCalls.push(p); },
  });
  check('the debug tape (recorder null) uploads nothing', tapeCalls.length === 0 && tapeOut.uploaded.length === 0);
  check('speaker-activity uploads independently of the tape being off',
    activityOut === 'uploaded' && activityCalls.join(',') === 'speaker-activity');
}
{
  // A capped file overshoots the ceiling by the capped line itself (design: "written even if it
  // slightly exceeds the ceiling") — the upload guard must not read that overshoot as "too large
  // to ship": a capped file is exactly the case the exporter most needs to receive.
  const dir = tmp();
  const tinyMax = 220;
  const w = createSpeakerActivityWriter(inv(), { dir, maxBytes: tinyMax });
  for (let i = 0; i < 50; i++) w.frame(0, Float32Array.of(0.1, -0.1), 1000 + i, 'Ann');
  await w.close();
  check('the writer capped (precondition)', w.isCapped());
  const fileBytes = readFileSync(w.path, 'utf8').length;
  check('the capped file overshoots its own ceiling (precondition: the upload guard must tolerate that overshoot)',
    fileBytes > tinyMax, `${fileBytes} vs ${tinyMax}`);

  const seen: Array<[TapePart, string, number]> = [];
  const out = await uploadSpeakerActivity(w, {
    inv: inv({ recordingUploadUrl: 'http://127.0.0.1:1/x' }),
    maxBytes: tinyMax,
    upload: async (part, path, size) => { seen.push([part, path, size]); },
  });
  check('a capped speaker-activity file still uploads (never misread as missing)',
    out === 'uploaded' && seen.length === 1 && seen[0][0] === 'speaker-activity', JSON.stringify({ out, seen }));
}

// ── speaker-activity upload events — its own names, so alerting can key on them ─────────────────
console.log('\n── speaker-activity upload events ──');
/** Run `fn` with console.log captured; return the `event` field of every JSON line it printed. */
const eventsDuring = async (fn: () => Promise<unknown>): Promise<string[]> => {
  const seen: string[] = [];
  const original = console.log;
  console.log = (...args: unknown[]): void => {
    try {
      const obj = JSON.parse(String(args[0])) as { event?: unknown };
      if (typeof obj.event === 'string') seen.push(obj.event);
    } catch { /* not a signal event */ }
  };
  try { await fn(); } finally { console.log = original; }
  return seen;
};
{
  const dir = tmp();
  const w = createSpeakerActivityWriter(inv(), { dir });
  w.frame(0, Float32Array.of(0.1), 1000, 'Ann');
  await w.close();
  const url = 'http://127.0.0.1:1/x';

  const ok = await eventsDuring(() => uploadSpeakerActivity(w, {
    inv: inv({ recordingUploadUrl: url }), upload: async () => { /* delivered */ } }));
  const bad = await eventsDuring(() => uploadSpeakerActivity(w, {
    inv: inv({ recordingUploadUrl: url }), upload: async () => { throw new Error('object store is down'); } }));
  const noUrl = await eventsDuring(() => uploadSpeakerActivity(w, { inv: inv() }));
  // The oversize case needs the file to exceed maxBytes + the upload slack; pad it past that.
  const bigPath = join(dir, 'big.speaker-activity.jsonl');
  writeFileSync(bigPath, 'x'.repeat(70 * 1024));
  const oversize = await eventsDuring(() => uploadSpeakerActivity({ ...w, path: bigPath }, {
    inv: inv({ recordingUploadUrl: url }), maxBytes: 1, upload: async () => { /* never reached */ } }));

  check("a delivered file emits 'speaker_activity_uploaded'",
    ok.includes('capture_signal.speaker_activity_uploaded'), JSON.stringify(ok));
  check("a failed upload emits 'speaker_activity_upload_failed'",
    bad.includes('capture_signal.speaker_activity_upload_failed'), JSON.stringify(bad));
  check("no upload URL emits 'speaker_activity_upload_skipped'",
    noUrl.includes('capture_signal.speaker_activity_upload_skipped'), JSON.stringify(noUrl));
  check("an oversize file emits 'speaker_activity_upload_skipped'",
    oversize.includes('capture_signal.speaker_activity_upload_skipped'), JSON.stringify(oversize));
  check('the speaker-activity path never emits a debug-tape (tape_*) event',
    [...ok, ...bad, ...noUrl, ...oversize].every((e) => !e.startsWith('capture_signal.tape_')),
    JSON.stringify([...ok, ...bad, ...noUrl, ...oversize]));
}

// ── the real transport, against a loopback receiver ─────────────────────────────────────────────
console.log('\n── streamed multipart transport ──');

interface Received { body: string; headers: IncomingMessage['headers']; }

async function withServer(
  handler: (req: IncomingMessage, received: Received) => number,
  run: (url: string, received: Received) => Promise<void>,
): Promise<void> {
  const received: Received = { body: '', headers: {} };
  const server: Server = createServer((req, res) => {
    const chunks: Buffer[] = [];
    req.on('data', (c) => chunks.push(c as Buffer));
    req.on('end', () => {
      received.body = Buffer.concat(chunks).toString('utf8');
      received.headers = req.headers;
      const code = handler(req, received);
      if (code === 0) return;           // 0 = never answer (the timeout case)
      res.writeHead(code); res.end('{}');
    });
  });
  await new Promise<void>((r) => server.listen(0, '127.0.0.1', r));
  const port = (server.address() as { port: number }).port;
  try {
    await run(`http://127.0.0.1:${port}/internal/recordings/upload`, received);
  } finally {
    server.closeAllConnections?.();
    await new Promise<void>((r) => server.close(() => r()));
  }
}

{
  const dir = tmp();
  const path = join(dir, 'x.captured-signal.jsonl');
  const payload = '{"type":"captured_signal_header","v":1}\n{"seq":0}\n';
  writeFileSync(path, payload);

  await withServer(() => 200, async (url, received) => {
    const upload = streamingTapeUploader(inv({ recordingUploadUrl: url }), url, DEFAULT_UPLOAD_TIMEOUT_MS);
    await upload('captured-signal', path, payload.length);
    check('server saw the file bytes', received.body.includes('"seq":0'), received.body.slice(0, 200));
    check('metadata declares media_type=signal', received.body.includes('"media_type":"signal"'));
    check('metadata declares the jsonl format', received.body.includes('"media_format":"jsonl"'));
    check('metadata names the part', received.body.includes('"part":"captured-signal"'));
    check('metadata carries the session_uid', received.body.includes('"session_uid":"conn-tape-1"'));
    check('bearer is the internal secret',
      received.headers.authorization === 'Bearer internal-secret', String(received.headers.authorization));
    check('multipart content-type with boundary',
      String(received.headers['content-type']).startsWith('multipart/form-data; boundary=----VexaSignalTape'),
      String(received.headers['content-type']));
    check('content-length matches the framed body',
      Number(received.headers['content-length']) === Buffer.byteLength(received.body),
      `${received.headers['content-length']} vs ${Buffer.byteLength(received.body)}`);
  });
}
{
  const dir = tmp();
  const path = join(dir, 'y.captured-signal.jsonl');
  writeFileSync(path, 'x\n');
  await withServer(() => 500, async (url) => {
    const upload = streamingTapeUploader(inv({ recordingUploadUrl: url }), url, DEFAULT_UPLOAD_TIMEOUT_MS);
    let msg = '';
    await upload('captured-signal', path, 2).catch((e) => { msg = String(e); });
    check('a non-2xx is a typed failure', msg.includes('HTTP 500'), msg);
  });
}
{
  const dir = tmp();
  const path = join(dir, 'z.captured-signal.jsonl');
  writeFileSync(path, 'x\n');
  await withServer(() => 0, async (url) => {
    // A receiver that never answers must not hold the teardown open — the whole reason the upload
    // carries its own timeout inside the SIGTERM grace.
    const upload = streamingTapeUploader(inv({ recordingUploadUrl: url }), url, 250);
    const t0 = Date.now();
    let msg = '';
    await upload('captured-signal', path, 2).catch((e) => { msg = String(e); });
    check('a hung receiver times out', msg.includes('timed out'), msg);
    check('the timeout is bounded', Date.now() - t0 < 5_000, `${Date.now() - t0}ms`);
  });
}
{
  // A malformed URL must fail as a rejected upload, not as an uncaught throw out of the constructor.
  const dir = tmp();
  const path = join(dir, 'w.captured-signal.jsonl');
  writeFileSync(path, 'x\n');
  const upload = streamingTapeUploader(inv(), 'not a url', 1000);
  let msg = '';
  await upload('captured-signal', path, 2).catch((e) => { msg = String(e); });
  check('a bad upload URL rejects cleanly', msg.includes('bad recordingUploadUrl'), msg);
}
{
  // The wall-clock bound is independent of req.setTimeout's IDLE semantics: a receiver that
  // accepts the connection but never responds keeps the socket "active" from the idle timer's
  // point of view, so only a wall-clock deadline can catch it. The idle timeout here is
  // deliberately generous — if it fired first, this test would prove nothing about the new bound.
  const dir = tmp();
  const path = join(dir, 'wallclock.captured-signal.jsonl');
  writeFileSync(path, 'x\n');
  await withServer(() => 0, async (url) => {
    const upload = streamingTapeUploader(inv({ recordingUploadUrl: url }), url, 5_000, 300);
    const t0 = Date.now();
    let msg = '';
    await upload('captured-signal', path, 2).catch((e) => { msg = String(e); });
    check('a wall-clock bound rejects a stalled upload even under a generous idle timeout',
      msg.includes('upload exceeded 300ms wall clock'), msg);
    check('the wall-clock rejection fires promptly (~300ms, not the 5s idle timeout)',
      Date.now() - t0 < 1_000, `${Date.now() - t0}ms`);
  });
}
{
  // uploadSpeakerActivity's own default wiring passes the wall clock through to the real
  // transport — proven end-to-end against a receiver that accepts the connection but never
  // answers, with no injected upload spy standing in for the real one.
  const dir = tmp();
  const w = createSpeakerActivityWriter(inv(), { dir });
  w.frame(0, Float32Array.of(0.1), 1000, 'Ann');
  await w.close();
  await withServer(() => 0, async (url) => {
    const t0 = Date.now();
    const out = await uploadSpeakerActivity(w, { inv: inv({ recordingUploadUrl: url }), wallClockMs: 300 });
    check("uploadSpeakerActivity against a stalled receiver resolves 'failed' within the wall clock",
      out === 'failed');
    check('and resolves promptly rather than riding the default 60s idle timeout',
      Date.now() - t0 < 1_000, `${Date.now() - t0}ms`);
  });
}

for (const d of dirs) rmSync(d, { recursive: true, force: true });

console.log(
  failed === 0
    ? '\n✅ signal-upload (O-TEL-1c): the tape is capped, shipped once, and incapable of changing how the meeting ended.'
    : `\n❌ ${failed} check(s) failed`,
);
process.exit(failed === 0 ? 0 : 1);
