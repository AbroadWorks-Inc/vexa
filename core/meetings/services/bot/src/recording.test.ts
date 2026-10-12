/**
 * L3 — recording sink (recording.v1 PER-CHUNK durable upload). OFFLINE, NO disk (HTTP only to a
 * localhost capture server for the wire arm).
 *
 * #491/#412: the 0.12 bot accumulated the whole recording in Node memory and uploaded ONE master at
 * graceful close — a SIGKILL lost everything. This sink uploads EACH timeslice immediately. This
 * test is the P22/#224-class regression pin that absence lacked. It asserts:
 *   • each chunk() uploads IMMEDIATELY (before any close), in seq order, with bytes forwarded;
 *   • a simulated mid-meeting kill (close never called, no final) leaves every finished part
 *     ALREADY durable — no all-or-nothing loss (V2/#412);
 *   • the empty is_final chunk is FORWARDED (the COMPLETED signal), never dropped;
 *   • close() with no prior final sends EXACTLY ONE empty is_final fallback (the live Stop race),
 *     and fires at most once; a never-fed session's close is a no-op;
 *   • over the real RecordingService HTTP wire, session_uid == inv.connectionId on every chunk,
 *     seq monotone, only the last is_final (the split/404-guard contract).
 * Run: npx tsx src/recording.test.ts
 */
import http from 'node:http';
import type { AddressInfo } from 'node:net';
import { createBotRecordingSink, type BotRecordingSink, type ChannelChunkIdentity, type ChunkUploader } from './recording.js';
import { startRecording } from './capture-bridge.js';
import type { Invocation } from './config.js';
import type { ChunkUploadExtra, RecordingMasterFormat } from '@vexa/recording';

let failed = 0;
const check = (name: string, cond: boolean, detail = '') => {
  console.log(`  ${cond ? '✅' : '❌'} ${name}${cond ? '' : '  — ' + detail}`);
  if (!cond) failed++;
};

const inv = (over: Partial<Invocation> = {}): Invocation => ({
  platform: 'google_meet', meetingUrl: 'https://meet.google.com/abc-defg-hij', botName: 'Vexa',
  redisUrl: 'redis://localhost:6379', recordingEnabled: true, connectionId: 'conn-xyz', ...over,
});

/** A record of one delivered chunk (what the injected uploader saw). */
interface Seen { seq: number; isFinal: boolean; format: string; len: number }
function fakeUploader(): { seen: Seen[]; upload: ChunkUploader } {
  const seen: Seen[] = [];
  const upload: ChunkUploader = (seq, isFinal, format, bytes) => {
    seen.push({ seq, isFinal, format, len: bytes.length });
  };
  return { seen, upload };
}
/** Drain the sink's internal upload queue (microtasks) — a macrotask tick runs after all of them. */
const flush = (): Promise<void> => new Promise((r) => setTimeout(r, 0));

async function main(): Promise<void> {
  // ── 1) each timeslice uploads IMMEDIATELY, in seq order, bytes forwarded ─────────────────────
  {
    const { seen, upload } = fakeUploader();
    const sink = createBotRecordingSink({ inv: inv(), uploadChunk: upload });
    sink.chunk('google_meet/m1', 0, false, 'webm', new Uint8Array([1, 2, 3]));
    sink.chunk('google_meet/m1', 1, false, 'webm', new Uint8Array([4, 5]));
    sink.chunk('google_meet/m1', 2, false, 'webm', new Uint8Array([6]));
    await flush();
    check('per-chunk: 3 chunks → 3 immediate uploads BEFORE any close', seen.length === 3, String(seen.length));
    check('per-chunk: none accumulated as a whole-recording master', seen.every((s) => s.len <= 3), JSON.stringify(seen));
    check('per-chunk: all non-final', seen.every((s) => !s.isFinal), JSON.stringify(seen));
    check('per-chunk: seq order 0,1,2', seen.map((s) => s.seq).join(',') === '0,1,2', seen.map((s) => s.seq).join(','));
    check('per-chunk: bytes forwarded verbatim (3,2,1)', seen.map((s) => s.len).join(',') === '3,2,1', seen.map((s) => s.len).join(','));
  }

  // ── 2) kill-before-close: no close, no final → the N finished parts are ALREADY durable (#412) ──
  {
    const { seen, upload } = fakeUploader();
    const sink = createBotRecordingSink({ inv: inv(), uploadChunk: upload });
    for (let i = 0; i < 4; i++) sink.chunk('google_meet/m2', i, false, 'webm', new Uint8Array([i]));
    await flush();
    // simulate SIGKILL: close() is NEVER called — nothing was buffered waiting for it.
    check('crash: N=4 parts durable before any close (no all-or-nothing loss)', seen.length === 4, String(seen.length));
    check('crash: no final signal sent (recording stays in_progress; finalize-on-read still serves it)',
      seen.every((s) => !s.isFinal), JSON.stringify(seen));
  }

  // ── 3) the empty is_final chunk is FORWARDED (COMPLETED signal), not dropped ─────────────────
  {
    const { seen, upload } = fakeUploader();
    const sink = createBotRecordingSink({ inv: inv(), uploadChunk: upload });
    sink.chunk('google_meet/m3', 0, false, 'webm', new Uint8Array([9, 9]));
    sink.chunk('google_meet/m3', 1, true, 'webm', new Uint8Array(0)); // empty final = COMPLETED signal
    await flush();
    check('final: empty is_final forwarded (not dropped)', seen.length === 2, String(seen.length));
    const fin = seen[seen.length - 1];
    check('final: last upload is isFinal=true, 0 bytes', !!fin && fin.isFinal && fin.len === 0, JSON.stringify(fin));
  }

  // ── 4) close() with no prior final → EXACTLY ONE empty is_final fallback (live Stop race) ────
  {
    const { seen, upload } = fakeUploader();
    const sink = createBotRecordingSink({ inv: inv(), uploadChunk: upload });
    sink.chunk('google_meet/m4', 0, false, 'webm', new Uint8Array([1]));
    sink.chunk('google_meet/m4', 1, false, 'webm', new Uint8Array([2]));
    sink.close('google_meet/m4');   // host-side close (the WS dropped) — synthesize the final signal
    await flush();
    check('fallback: 2 data uploads + 1 synthesized final', seen.length === 3, String(seen.length));
    const fin = seen[2];
    check('fallback: final is isFinal=true, 0 bytes, seq after the last part (2)',
      !!fin && fin.isFinal && fin.len === 0 && fin.seq === 2, JSON.stringify(fin));
    sink.close('google_meet/m4');   // idempotent
    await flush();
    check('fallback: fires at most once (second close is a no-op)',
      seen.filter((s) => s.isFinal).length === 1, String(seen.filter((s) => s.isFinal).length));
  }

  // ── 5) close() AFTER a real final → no extra fallback (no double final) ──────────────────────
  {
    const { seen, upload } = fakeUploader();
    const sink = createBotRecordingSink({ inv: inv(), uploadChunk: upload });
    sink.chunk('google_meet/m5', 0, false, 'webm', new Uint8Array([1]));
    sink.chunk('google_meet/m5', 1, true, 'webm', new Uint8Array(0));
    sink.close('google_meet/m5');
    await flush();
    check('no-double-final: a real final was sent → close adds nothing',
      seen.filter((s) => s.isFinal).length === 1 && seen.length === 2, JSON.stringify(seen));
  }

  // ── 6) close() on a never-fed session is a no-op (no phantom recording) ──────────────────────
  {
    const { seen, upload } = fakeUploader();
    const sink = createBotRecordingSink({ inv: inv(), uploadChunk: upload });
    sink.close('google_meet/never');
    await flush();
    check('empty session: close is a no-op (no upload)', seen.length === 0, String(seen.length));
  }

  // ── 7) the DEFAULT uploader on the real RecordingService HTTP wire: session_uid == connectionId ──
  {
    interface Wire { session_uid?: string; chunk_seq?: number; is_final?: boolean; format?: string; size?: number }
    const wire: Wire[] = [];
    const server = http.createServer((req, res) => {
      const parts: Buffer[] = [];
      req.on('data', (d: Buffer) => parts.push(d));
      req.on('end', () => {
        const text = Buffer.concat(parts).toString('latin1');
        const m = text.match(/name="metadata"[\s\S]*?\r\n\r\n(\{[\s\S]*?\})\r\n/);
        let meta: Record<string, unknown> = {};
        if (m) { try { meta = JSON.parse(m[1]); } catch { /* ignore */ } }
        wire.push({
          session_uid: meta.session_uid as string, chunk_seq: meta.chunk_seq as number,
          is_final: meta.is_final as boolean, format: meta.format as string,
          size: meta.file_size_bytes as number,
        });
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ status: 'ok' }));
      });
    });
    await new Promise<void>((r) => server.listen(0, '127.0.0.1', () => r()));
    const url = `http://127.0.0.1:${(server.address() as AddressInfo).port}/internal/recordings/upload`;

    const sink = createBotRecordingSink({
      inv: inv({ connectionId: 'conn-xyz', meeting_id: 42, recordingUploadUrl: url, internalSecret: 's' }),
    });
    sink.chunk('google_meet/w', 0, false, 'webm', new Uint8Array([1, 2, 3, 4]));
    sink.chunk('google_meet/w', 1, false, 'webm', new Uint8Array([5, 6]));
    sink.chunk('google_meet/w', 2, true, 'webm', new Uint8Array(0));
    for (let i = 0; i < 100 && wire.length < 3; i++) await new Promise((r) => setTimeout(r, 10));
    await new Promise<void>((r) => server.close(() => r()));

    check('wire: 3 chunks POSTed to meeting-api', wire.length === 3, String(wire.length));
    check('wire: session_uid == inv.connectionId on EVERY chunk (never nativeMeetingId/master key)',
      wire.length === 3 && wire.every((w) => w.session_uid === 'conn-xyz'),
      JSON.stringify(wire.map((w) => w.session_uid)));
    check('wire: seq order 0,1,2', wire.map((w) => w.chunk_seq).join(',') === '0,1,2', wire.map((w) => w.chunk_seq).join(','));
    check('wire: only the LAST chunk is_final', wire.map((w) => w.is_final).join(',') === 'false,false,true',
      wire.map((w) => w.is_final).join(','));
  }

  // ── 8) a channel lane is its own seq, media type, and final. The mix lane is unchanged. ──
  {
    interface Seen { seq: number; isFinal: boolean; len: number; extra?: { mediaType?: string; metadata?: Record<string, string | number> } }
    const seen: Seen[] = [];
    const upload: ChunkUploader = (seq, isFinal, _format, bytes, extra) => {
      seen.push({ seq, isFinal, len: bytes.length, extra });
    };
    const sink = createBotRecordingSink({ inv: inv(), uploadChunk: upload });
    const ada: ChannelChunkIdentity = {
      recorder_start_epoch_ms: 1_700_000_000_000,
      channel_kind: 'jitsi',
      stream_id: 'remote-audio-3',
      participant_id: 'p1',
      display_name: 'Ada',
    };
    const bob: ChannelChunkIdentity = { ...ada, display_name: 'Bob' };
    sink.chunk('google_meet/ch', 0, false, 'webm', new Uint8Array([1]));
    sink.channelChunk(3, 1, false, 'webm', new Uint8Array([2]), ada);
    sink.channelChunk(3, 0, false, 'webm', new Uint8Array([3, 3]), bob);
    sink.channelChunk(3, 2, false, 'webm', new Uint8Array([4]), bob);
    sink.close('google_meet/ch');
    await flush();

    const audio = seen.filter((s) => !s.extra);
    const channel = seen.filter((s) => s.extra?.mediaType === 'ch3');
    check('channel: the mix lane carries no extra metadata', audio.length === 2 && audio[0]?.seq === 0 && !audio[0].isFinal && audio[1]?.isFinal && audio[1].seq === 1 && audio[1].len === 0, JSON.stringify(audio));
    const seq0 = channel.find((s) => s.seq === 0);
    const meta = seq0?.extra?.metadata;
    check('channel: seq 0 is media type ch3 with the first identity',
      !!seq0 && !seq0.isFinal && meta?.recorder_start_epoch_ms === 1_700_000_000_000 && meta.channel_kind === 'jitsi' && meta.stream_id === 'remote-audio-3' && meta.participant_id === 'p1' && meta.display_name === 'Ada',
      JSON.stringify(seq0));
    const later = channel.filter((s) => s.seq !== 0 && !s.isFinal);
    // F3: every chunk carries the identity, and the first value of a field wins (Bob never renames Ada).
    check('channel: every later chunk carries the same identity',
      later.length === 2 && later.every((s) => s.extra?.metadata?.display_name === 'Ada' && s.extra?.metadata?.stream_id === 'remote-audio-3'),
      JSON.stringify(later));
    const fin = channel.find((s) => s.isFinal);
    check('channel: close sends one empty final for the channel, with its identity',
      !!fin && fin.len === 0 && fin.seq === 3 && fin.extra?.metadata?.display_name === 'Ada', JSON.stringify(fin));
    sink.close('google_meet/ch');
    await flush();
    check('channel: a second close adds no further final', seen.filter((s) => s.isFinal).length === 2, String(seen.filter((s) => s.isFinal).length));
  }

  // ── 9) a bool clock is not identity; an over-long name is sent cut to 200 code points ──
  {
    const seen: { seq: number; extra?: { metadata?: Record<string, string | number> } }[] = [];
    const upload: ChunkUploader = (seq, _isFinal, _format, _bytes, extra) => { seen.push({ seq, extra }); };
    const sink = createBotRecordingSink({ inv: inv(), uploadChunk: upload });
    sink.channelChunk(0, 0, false, 'webm', new Uint8Array([1]), {
      recorder_start_epoch_ms: true as unknown as number,
      channel_kind: 'gmeet',
      stream_id: 'stream-a',
      display_name: 'x'.repeat(201),
      participant_id: '\u{1F600}'.repeat(201),
    });
    await flush();
    const meta = seen[0]?.extra?.metadata;
    check('channel: a boolean clock is dropped and a 201-char name is cut to 200',
      meta?.channel_kind === 'gmeet' && meta.stream_id === 'stream-a' && meta.recorder_start_epoch_ms === undefined && meta.display_name === 'x'.repeat(200),
      JSON.stringify(meta));
    check('channel: the cut counts code points and keeps every surrogate pair whole',
      meta?.participant_id === '\u{1F600}'.repeat(200), String(meta?.participant_id).length + ' UTF-16 units');
  }

  // ── 10) the default uploader writes media_type into the metadata JSON ──
  {
    const wire: Record<string, unknown>[] = [];
    const server = http.createServer((req, res) => {
      const parts: Buffer[] = [];
      req.on('data', (d: Buffer) => parts.push(d));
      req.on('end', () => {
        const text = Buffer.concat(parts).toString('latin1');
        const m = text.match(/name="metadata"[\s\S]*?\r\n\r\n(\{[\s\S]*?\})\r\n/);
        wire.push(m ? JSON.parse(m[1]) : {});
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ status: 'ok' }));
      });
    });
    await new Promise<void>((r) => server.listen(0, '127.0.0.1', () => r()));
    const url = `http://127.0.0.1:${(server.address() as AddressInfo).port}/internal/recordings/upload`;
    const sink = createBotRecordingSink({
      inv: inv({ connectionId: 'conn-xyz', meeting_id: 42, recordingUploadUrl: url, internalSecret: 's' }),
    });
    sink.chunk('jitsi/w', 0, false, 'webm', new Uint8Array([1]));
    sink.channelChunk(0, 0, true, 'webm', new Uint8Array(0), {
      recorder_start_epoch_ms: 42,
      channel_kind: 'gmeet',
      stream_id: 'stream-a',
    });
    for (let i = 0; i < 100 && wire.length < 2; i++) await new Promise((r) => setTimeout(r, 10));
    await new Promise<void>((r) => server.close(() => r()));
    const audio = wire.find((w) => w.media_type === undefined);
    const channel = wire.find((w) => w.media_type === 'ch0');
    check('wire channel: the mix chunk has no media_type', !!audio && audio.chunk_seq === 0 && audio.is_final === false, JSON.stringify(audio));
    check('wire channel: seq 0 carries ch0 and the clock',
      !!channel && channel.channel_kind === 'gmeet' && channel.stream_id === 'stream-a' && channel.recorder_start_epoch_ms === 42 && channel.is_final === true,
      JSON.stringify(channel));
  }

  // ── F3: a name Jitsi resolves after the recorder started reaches the server with the next chunk ──
{
  const seen: { seq: number; extra?: ChunkUploadExtra }[] = [];
  const sink = createBotRecordingSink({
    inv: inv(), uploadChunk: (seq, _f, _fmt, _b, extra) => { seen.push({ seq, extra }); },
  });
  const unnamed: ChannelChunkIdentity = { recorder_start_epoch_ms: 1_700_000_000_000, channel_kind: 'jitsi', stream_id: 'remote-audio-5' };
  sink.channelChunk(5, 0, false, 'webm', new Uint8Array([1]), unnamed);
  sink.channelChunk(5, 1, false, 'webm', new Uint8Array([2]), { ...unnamed, participant_id: 'p9', display_name: 'Grace' });
  await new Promise((r) => setTimeout(r, 0));
  await new Promise((r) => setTimeout(r, 0));
  check('late name: chunk 0 has no name', seen[0]?.extra?.metadata?.display_name === undefined, JSON.stringify(seen[0]));
  check('late name: the next chunk carries it',
    seen[1]?.extra?.metadata?.display_name === 'Grace' && seen[1]?.extra?.metadata?.participant_id === 'p9', JSON.stringify(seen[1]));
}

  // ── L1/D5: the page-side channel recorders' lifecycle (startRecording's page code, run in-process
  //    over a fake page: every browser object it touches is a fake below) ──
  await channelLifecycle();

if (failed) { console.error(`\n❌ recording (L3): ${failed} check(s) FAILED.`); process.exit(1); }
  console.log('\n✅ recording (L3): each recording.v1 timeslice uploads immediately (seq-ordered, session_uid==connectionId); a mid-meeting kill leaves every finished part durable; the empty is_final signal is forwarded; close() synthesizes the final signal at most once (injected uploader + real RecordingService HTTP wire · no whole-recording buffering).');
}

/** One fake Jitsi page: remote-audio streams, recorder taps whose start/stop the test gates, and a
 *  level worklet whose creation it can hold. `startRecording`'s page function runs against it. */
function channelPage(streamIds: string[]) {
  const g = globalThis as unknown as Record<string, any>;
  const log: string[] = [];
  const finals: { channel: number; at: number }[] = [];
  const taps: { streamId: string; started: boolean; stops: number }[] = [];
  const sources: { connected: boolean; disconnected: boolean }[] = [];
  const contexts: unknown[] = [];
  const gates = {
    start: new Map<string, Promise<void>>(),
    level: new Map<string, Promise<void>>(),
    stopMs: 0,
  };
  const listeners = new Map<string, () => void>();
  class FakeMediaStream {
    constructor(public id: string) {}
    getAudioTracks() {
      return [{ addEventListener: (type: string, cb: () => void) => { if (type === 'ended') listeners.set(this.id, cb); } }];
    }
  }
  const elements = streamIds.map((id) => ({ srcObject: new FakeMediaStream(id), paused: false }));
  const saved = { setInterval: g.setInterval, clearInterval: g.clearInterval };
  g.setInterval = () => 1;
  g.clearInterval = () => { /* the scan is driven by the initial run only */ };
  g.MediaStream = FakeMediaStream;
  g.document = { querySelectorAll: () => elements };
  g.logBot = (m: string) => log.push(m);
  g.AudioContext = class {
    destination = {};
    constructor() { contexts.push(this); }
    async resume() { /* running */ }
    createMediaStreamSource(stream: FakeMediaStream) {
      const src = { stream: stream.id, connected: false, disconnected: false,
        connect() { src.connected = true; }, disconnect() { src.disconnected = true; } };
      sources.push(src);
      return src;
    }
    close() { /* closed */ }
  };
  g.VexaBrowserUtils = {
    selectJitsiChannels: (present: { streamId: string }[]) =>
      present.map((p) => ({ streamId: p.streamId, channel: Number(p.streamId.split('-').pop()) })),
    createLevelNode: async (_ctx: unknown, _cb: unknown) => {
      const streamId = (sources[sources.length - 1] as unknown as { stream: string }).stream;
      await gates.level.get(streamId);
      return { connect() { /* */ }, disconnect() { /* */ } };
    },
    createRecordingTap: (opts: { stream: FakeMediaStream; onChunk: (c: object) => Promise<boolean> }) => {
      const tap = { streamId: opts.stream.id, started: false, stops: 0 };
      taps.push(tap);
      return {
        async start() { await gates.start.get(tap.streamId); tap.started = true; },
        async stop() {
          tap.stops++;
          await new Promise((r) => setTimeout(r, gates.stopMs));
          await opts.onChunk({ base64: '', chunkSeq: 1, isFinal: true, mimeType: 'audio/webm' });
        },
      };
    },
  };
  const page = {
    async exposeFunction(name: string, fn: unknown) { g[name] = fn; },
    async evaluate(fn: (arg: never) => unknown, arg?: unknown) { return fn(arg as never); },
  };
  const sink: BotRecordingSink = {
    chunk() { /* the mix lane is not under test */ },
    channelChunk(channel, _seq, isFinal) { if (isFinal) finals.push({ channel, at: Date.now() }); },
    close() { /* */ },
  };
  const start = () => startRecording(page as never, {
    platform: 'jitsi', meetingUrl: 'https://meet.example/room', botName: 'B', redisUrl: 'redis://r:6379',
    perChannelRecordingEnabled: true,
  } as Invocation, sink);
  const restore = () => {
    g.setInterval = saved.setInterval;
    g.clearInterval = saved.clearInterval;
    for (const key of ['MediaStream', 'document', 'logBot', 'AudioContext', 'VexaBrowserUtils', '__vexaChannels',
      '__vexaChannelScan', '__vexaChannelActivityCtx', '__vexaChannelsStopped', '__vexaChannelReleases',
      '__vexaRecordingTap', '__vexaRecordingChunk', '__vexaChannelChunk']) delete g[key];
  };
  const ended = (streamId: string) => listeners.get(streamId)?.();
  return { g, log, finals, taps, sources, contexts, gates, start, restore, ended };
}

const tick = (ms = 0): Promise<void> => new Promise((r) => setTimeout(r, ms));
const gate = (): { wait: Promise<void>; open: () => void } => {
  let open = (): void => { /* */ };
  const wait = new Promise<void>((r) => { open = r; });
  return { wait, open };
};

async function channelLifecycle(): Promise<void> {
  // L1: stop flushes every channel at once, not one after another.
  {
    const rig = channelPage(['remote-audio-1', 'remote-audio-2', 'remote-audio-3']);
    rig.gates.stopMs = 150;
    const stop = await rig.start();
    await tick(10);
    const t0 = Date.now();
    await stop();
    const took = Date.now() - t0;
    check('channels: stop flushes all three recorders in parallel (~150 ms, not 450)', took < 300, `${took} ms`);
    check('channels: every final chunk is in before stop returns', rig.finals.length === 3, JSON.stringify(rig.finals));
    rig.restore();
  }

  // An open in flight when stop runs: its recorder is stopped, and the scan opens nothing after.
  {
    const rig = channelPage(['remote-audio-1', 'remote-audio-2']);
    const held = gate();
    rig.gates.start.set('remote-audio-1', held.wait);
    const stop = await rig.start();
    await tick(10);
    await stop();
    held.open();
    await tick(10);
    const ch2 = rig.taps.find((t) => t.streamId === 'remote-audio-2');
    check('channels: a recorder opened by a scan after stop is never started', !ch2 || (!ch2.started || ch2.stops > 0), JSON.stringify(rig.taps));
    check('channels: the open in flight at stop left its recorder stopped', rig.taps.find((t) => t.streamId === 'remote-audio-1')?.stops === 1, JSON.stringify(rig.taps));
    check('channels: no AudioContext is made after stop', rig.g.__vexaChannelActivityCtx == null && rig.contexts.length === 0, String(rig.contexts.length));
    check('channels: nothing is left in the channel map', rig.g.__vexaChannels.size === 0, String(rig.g.__vexaChannels.size));
    rig.restore();
  }

  // D5: a channel released (its track ended) while its activity tap is being made.
  {
    const rig = channelPage(['remote-audio-4']);
    const held = gate();
    rig.gates.level.set('remote-audio-4', held.wait);
    const stop = await rig.start();
    await tick(10);
    rig.ended('remote-audio-4');
    await tick(10);
    held.open();
    await tick(10);
    const src = rig.sources[0];
    check('channels: a released channel\'s late activity source is disconnected, never connected',
      !!src && src.disconnected && !src.connected, JSON.stringify(rig.sources));
    check('channels: the release stopped its recorder once', rig.taps[0]?.stops === 1, JSON.stringify(rig.taps));
    await stop();
    rig.restore();
  }

  // A release still flushing when stop runs: stop waits for its final chunk.
  {
    const rig = channelPage(['remote-audio-5']);
    rig.gates.stopMs = 150;
    const stop = await rig.start();
    await tick(10);
    rig.ended('remote-audio-5');
    await stop();
    const stoppedAt = Date.now();
    check('channels: the leaving speaker\'s final chunk arrives before stop returns',
      rig.finals.length === 1 && rig.finals[0].at <= stoppedAt, JSON.stringify({ finals: rig.finals, stoppedAt }));
    rig.restore();
  }
}

void main();
