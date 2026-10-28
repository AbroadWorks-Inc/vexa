/**
 * L3 boundary — Jitsi per-channel recording in a real Chromium: two remote speakers.
 *
 * Drives the REAL bridge + REAL recorder + the bundled level worklet over a page with two remote
 * audio streams named the way Jitsi names them (`remote-audio-<n>`), each a tone:
 *   1. both are recorded as their own channel, every chunk carrying the channel's identity;
 *   2. each channel's loudness arrives from the level worklet (one value per block, no samples);
 *   3. a stream detached for 3 s (at least one scan, less than the grace) keeps its recorder;
 *   4. a stream gone for longer is released (its final chunk sent), and the other keeps recording;
 *   5. a new bridge session: every old track ends, and new stream objects arrive under the SAME
 *      ids (`remote-audio-1`, `remote-audio-2`). Each gets a new channel number and records;
 *   6. an element re-pointed to a new stream under its id while the old track never ends: the new
 *      stream records on a new channel, and the old one is released after the grace;
 *   7. an element left holding an ended stream, ahead of a live stream with the same id, and
 *   8. after it: either way the live one is recorded.
 *
 * Where headless Chromium cannot launch the test SKIPS LOUDLY with exit 0 (same shape as
 * recording-source.boundary.test.ts). Run: npx tsx src/channel-recording.boundary.test.ts
 */
import { execSync } from 'node:child_process';
import { createServer } from 'node:http';
import type { AddressInfo } from 'node:net';
import { existsSync, mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { launchPersistentBrowser, type BrowserContext } from '@vexa/remote-browser';
import { startCaptureBridge, startRecording } from './capture-bridge.js';
import type { BotRecordingSink, ChannelChunkIdentity } from './recording.js';
import type { SpeakerActivityWriter } from './speaker-activity.js';
import type { BotPipeline } from './pipeline.js';
import type { Invocation } from './config.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const BOT_DIR = join(HERE, '..');
const BUNDLE = join(BOT_DIR, 'dist', 'browser-utils.global.js');

let failed = 0;
const check = (name: string, cond: boolean, detail = '') => {
  console.log(`  ${cond ? '✅' : '❌'} ${name}${cond ? '' : '  — ' + detail}`);
  if (!cond) failed++;
};
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

async function main(): Promise<void> {
  if (!existsSync(BUNDLE)) execSync('node build-browser-utils.mjs', { cwd: BOT_DIR, stdio: 'inherit' });

  const dataDir = mkdtempSync(join(tmpdir(), 'vexa-channels-'));
  let context: BrowserContext;
  let page;
  try {
    ({ context, page } = await launchPersistentBrowser({
      dataDir,
      args: ['--no-sandbox', '--mute-audio', '--autoplay-policy=no-user-gesture-required'],
      headless: true,
    }));
  } catch (e) {
    console.log(`  ⚠️ SKIP — headless Chromium unavailable in this environment: ${(e as Error).message?.split('\n')[0]}`);
    process.exit(0);
  }
  try {
    await context.addInitScript({ path: BUNDLE });
    const pageLogs: string[] = [];
    await context.exposeFunction('logBot', (m: string) => pageLogs.push(String(m)));

    const pipeline = {
      async start() { /* stub */ }, async stop() { /* stub */ },
      feedAudio() { /* stub */ }, feedMixedAudio() { /* stub */ }, recordHint() { /* stub */ },
    } as unknown as BotPipeline;
    const inv = {
      platform: 'jitsi', meetingUrl: 'https://meet.fixture.test/room', botName: 'Vexa',
      nativeMeetingId: 'room@meet.fixture.test', redisUrl: 'redis://localhost:6379', transcribeEnabled: false,
      perChannelRecordingEnabled: true,
    } as Invocation;

    const levels: { ch: number | undefined; rms: number; src?: string }[] = [];
    const writer: SpeakerActivityWriter = {
      path: '/dev/null',
      frame() { /* the mix lane: not under test */ },
      level(ch, rms, _dur, _ts, _name, src) { levels.push({ ch, rms, src }); },
      hint() { /* not under test */ },
      isCapped: () => false,
      async close() { /* nothing buffered */ },
    };
    const chunks: { channel: number; seq: number; isFinal: boolean; bytes: number; identity?: ChannelChunkIdentity; at: number }[] = [];
    const sink: BotRecordingSink = {
      chunk() { /* the mix lane: not under test */ },
      channelChunk(channel, seq, isFinal, _format, bytes, identity) {
        chunks.push({ channel, seq, isFinal, bytes: bytes.length, identity, at: Date.now() });
      },
      close() { /* stub */ },
    };

    // AudioWorklet exists only in a secure context: a meeting page is https; here 127.0.0.1 is.
    const server = createServer((_req, res) => {
      res.writeHead(200, { 'Content-Type': 'text/html' });
      res.end('<!doctype html><html><body></body></html>');
    });
    await new Promise<void>((r) => server.listen(0, '127.0.0.1', () => r()));
    await page.goto(`http://127.0.0.1:${(server.address() as AddressInfo).port}/`);
    server.close();
    await page.addScriptTag({ path: BUNDLE });
    await page.evaluate('globalThis.__name = globalThis.__name || ((t, v) => t);');

    // Two remote speakers: a tone each, on a MediaStream named as Jitsi names its remote audio.
    await page.evaluate(async () => {
      const w = globalThis as any;
      const ctx = new w.AudioContext();
      await ctx.resume();
      for (const [n, freq] of [[1, 440], [2, 660]] as const) {
        const osc = ctx.createOscillator();
        osc.frequency.value = freq;
        const dest = ctx.createMediaStreamDestination();
        osc.connect(dest);
        osc.start();
        const stream = dest.stream;
        Object.defineProperty(stream, 'id', { value: `remote-audio-${n}` });
        const el = w.document.createElement('audio');
        el.id = `ra${n}`;
        el.srcObject = stream;
        el.autoplay = true;
        w.document.body.appendChild(el);
        try { await el.play(); } catch { /* autoplay flag is on */ }
        w[`__speaker${n}`] = el;
      }
    });

    process.env.VEXA_RECORDING_TIMESLICE_MS = '1000';
    process.env.VEXA_CHANNEL_GONE_MS = '4000';
    const stopCapture = await startCaptureBridge(page, inv, pipeline, undefined, undefined, undefined, writer);
    const stopRecording = await startRecording(page, inv, sink);

    await sleep(6000);
    const ch = (n: number) => chunks.filter((c) => c.channel === n);
    check('speaker 1 is recorded as channel 1', ch(1).filter((c) => c.bytes > 0).length >= 2, JSON.stringify(ch(1).length));
    check('speaker 2 is recorded as channel 2', ch(2).filter((c) => c.bytes > 0).length >= 2, JSON.stringify(ch(2).length));
    // channel -> the stream id it records. 3-7 are new stream objects under used ids (phases 5-8).
    const streamOf: Record<number, string> = {
      1: 'remote-audio-1', 2: 'remote-audio-2', 3: 'remote-audio-1', 4: 'remote-audio-2', 5: 'remote-audio-1', 6: 'remote-audio-2', 7: 'remote-audio-1',
    };
    const identityOk = (c: (typeof chunks)[number]) => c.identity?.channel_kind === 'jitsi' && c.identity?.stream_id === streamOf[c.channel];
    check('every channel chunk carries its identity', chunks.length > 0 && chunks.every(identityOk), JSON.stringify(chunks.slice(0, 3)));
    const lv = (n: number) => levels.filter((l) => l.ch === n && l.src === 'channel');
    if (!levels.length) {
      const state = await page.evaluate(() => {
        const w = globalThis as any;
        return { ctx: w.__vexaChannelActivityCtx?.state ?? null, rate: w.__vexaChannelActivityCtx?.sampleRate ?? null,
          hasLevel: typeof w.VexaBrowserUtils?.createLevelNode, nodes: Array.from(w.__vexaChannels?.values?.() ?? []).map((e: any) => !!e.node) };
      });
      console.log('  diagnostics:', JSON.stringify(state), '| page logs:', pageLogs.filter((l) => /channels|activity|level/i.test(l)).join(' | '));
    }
    check('channel 1 loudness arrives from the level worklet', lv(1).length >= 3 && lv(1).every((l) => l.rms > 0.01), JSON.stringify(lv(1).slice(0, 3)));
    check('channel 2 loudness arrives from the level worklet', lv(2).length >= 3 && lv(2).every((l) => l.rms > 0.01), JSON.stringify(lv(2).slice(0, 3)));

    // A brief detach (shorter than the 4 s grace): the recorder must survive it.
    await page.evaluate(async () => {
      const w = globalThis as any;
      const el = w.__speaker2;
      el.remove();
      await new Promise((r) => setTimeout(r, 3000)); // spans at least one 2 s scan
      w.document.body.appendChild(el);
      try { await el.play(); } catch { /* */ }
    });
    const afterBlip = Date.now();
    await sleep(5000);
    check('a 3 s detach does not release channel 2', !pageLogs.some((l) => /released ch2\b/.test(l)), pageLogs.filter((l) => /released/.test(l)).join(' | '));
    check('channel 2 keeps recording after the detach', ch(2).some((c) => c.at > afterBlip + 1000 && c.bytes > 0 && !c.isFinal), JSON.stringify(ch(2).slice(-3)));

    // Speaker 1 leaves for good: released after the grace, its final chunk sent; speaker 2 goes on.
    await page.evaluate(() => { (globalThis as any).__speaker1.remove(); });
    const left = Date.now();
    await sleep(9000);
    check('channel 1 is released after the grace', pageLogs.some((l) => /released ch1\b/.test(l)), pageLogs.filter((l) => /\[channels\]/.test(l)).join(' | '));
    check('channel 1 sent its final chunk', ch(1).some((c) => c.isFinal && c.at > left), JSON.stringify(ch(1).slice(-2)));
    check('channel 2 is still recording after channel 1 left', ch(2).some((c) => c.at > left + 6000 && c.bytes > 0), JSON.stringify(ch(2).slice(-2)));

    // A new bridge session (ICE failure or a bridge restart): the old connection closes, so speaker
    // 2's track ends (`stop()` fires no `ended`, so it is dispatched as the browser does on close) and
    // its element goes. The new session's streams are new objects under the ids already used.
    await page.evaluate(async () => {
      const w = globalThis as any;
      const el = w.__speaker2;
      for (const t of el.srcObject.getAudioTracks()) { t.stop(); t.dispatchEvent(new Event('ended')); }
      el.remove();
      const ctx = new w.AudioContext();
      await ctx.resume();
      for (const [n, freq] of [[1, 550], [2, 770]] as const) {
        const osc = ctx.createOscillator();
        osc.frequency.value = freq;
        const dest = ctx.createMediaStreamDestination();
        osc.connect(dest);
        osc.start();
        const stream = dest.stream;
        Object.defineProperty(stream, 'id', { value: `remote-audio-${n}` });
        const el2 = w.document.createElement('audio');
        el2.srcObject = stream;
        el2.autoplay = true;
        w.document.body.appendChild(el2);
        try { await el2.play(); } catch { /* autoplay flag is on */ }
      }
    });
    const restart = Date.now();
    await sleep(6000);
    check('the ended channel 2 is released', pageLogs.some((l) => /released ch2\b/.test(l)), pageLogs.filter((l) => /\[channels\]/.test(l)).join(' | '));
    check('the new remote-audio-1 is recorded as channel 3', pageLogs.some((l) => /recording ch3 remote-audio-1\b/.test(l)), pageLogs.filter((l) => /\[channels\]/.test(l)).join(' | '));
    check('the new remote-audio-2 is recorded as channel 4', pageLogs.some((l) => /recording ch4 remote-audio-2\b/.test(l)), pageLogs.filter((l) => /\[channels\]/.test(l)).join(' | '));
    for (const n of [3, 4]) {
      check(`channel ${n} uploads audio after the restart`, ch(n).filter((c) => c.bytes > 0).length >= 2, JSON.stringify(ch(n).length));
      check(`channel ${n}'s recorder started after the restart`,
        ch(n).length > 0 && ch(n).every((c) => c.identity?.recorder_start_epoch_ms === undefined || Number(c.identity.recorder_start_epoch_ms) >= restart),
        JSON.stringify(ch(n).slice(0, 2)));
      check(`channel ${n} loudness arrives from the level worklet`, lv(n).length >= 3 && lv(n).every((l) => l.rms > 0.01), JSON.stringify(lv(n).slice(0, 3)));
    }
    // A released number stays released: the new session's streams do not land on channel 1 or 2.
    for (const n of [1, 2]) {
      check(`no chunk on channel ${n} after its release`, ch(n).every((c) => c.at < restart + 1500), JSON.stringify(ch(n).slice(-2)));
    }

    // A tone on a new MediaStream named as Jitsi names its remote audio.
    await page.evaluate(() => {
      const w = globalThis as any;
      w.__toneStream = async (freq: number, id: string): Promise<MediaStream> => {
        const ctx = (w.__toneCtx ??= new w.AudioContext());
        await ctx.resume();
        const osc = ctx.createOscillator();
        osc.frequency.value = freq;
        const dest = ctx.createMediaStreamDestination();
        osc.connect(dest);
        osc.start();
        Object.defineProperty(dest.stream, 'id', { value: id });
        return dest.stream;
      };
      w.__elementFor = (id: string): any => Array.from(w.document.querySelectorAll('audio'))
        .find((e: any) => e.srcObject?.id === id && e.srcObject.getAudioTracks().some((t: any) => t.readyState === 'live'));
    });

    // Channel 3's element is re-pointed to a new stream with the same id. The old track never ends.
    await page.evaluate(async () => {
      const w = globalThis as any;
      const el = w.__elementFor('remote-audio-1');
      el.srcObject = await w.__toneStream(880, 'remote-audio-1');
      try { await el.play(); } catch { /* autoplay flag is on */ }
    });
    const repointed = Date.now();
    await sleep(8000);
    check('the re-pointed stream records on channel 5', pageLogs.some((l) => /recording ch5 remote-audio-1\b/.test(l)), pageLogs.filter((l) => /\[channels\]/.test(l)).join(' | '));
    check('channel 3, whose stream left the page, is released after the grace', pageLogs.some((l) => /released ch3\b/.test(l)), pageLogs.filter((l) => /\[channels\]/.test(l)).join(' | '));
    check('channel 5 uploads audio', ch(5).filter((c) => c.bytes > 0 && c.at > repointed).length >= 2, JSON.stringify(ch(5).length));

    // Channel 4's track ends and its element stays on the page, ahead of a live stream with its id.
    await page.evaluate(async () => {
      const w = globalThis as any;
      const old = w.__elementFor('remote-audio-2');
      for (const t of old.srcObject.getAudioTracks()) { t.stop(); t.dispatchEvent(new Event('ended')); }
      w.document.body.prepend(old);
      const el = w.document.createElement('audio');
      el.srcObject = await w.__toneStream(990, 'remote-audio-2');
      el.autoplay = true;
      w.document.body.appendChild(el);
      try { await el.play(); } catch { /* autoplay flag is on */ }
    });
    const lingering = Date.now();
    await sleep(5000);
    check('the live stream behind an ended one records on channel 6', pageLogs.some((l) => /recording ch6 remote-audio-2\b/.test(l)), pageLogs.filter((l) => /\[channels\]/.test(l)).join(' | '));
    check('channel 6 uploads audio', ch(6).filter((c) => c.bytes > 0 && c.at > lingering).length >= 2, JSON.stringify(ch(6).length));

    // Channel 5's track ends and its element stays on the page, after a live stream with its id.
    await page.evaluate(async () => {
      const w = globalThis as any;
      const old = w.__elementFor('remote-audio-1');
      const el = w.document.createElement('audio');
      el.srcObject = await w.__toneStream(1100, 'remote-audio-1');
      el.autoplay = true;
      w.document.body.prepend(el);
      try { await el.play(); } catch { /* autoplay flag is on */ }
      for (const t of old.srcObject.getAudioTracks()) { t.stop(); t.dispatchEvent(new Event('ended')); }
      w.document.body.appendChild(old);
    });
    const trailing = Date.now();
    await sleep(5000);
    check('the live stream ahead of an ended one records on channel 7', pageLogs.some((l) => /recording ch7 remote-audio-1\b/.test(l)), pageLogs.filter((l) => /\[channels\]/.test(l)).join(' | '));
    check('channel 7 uploads audio', ch(7).filter((c) => c.bytes > 0 && c.at > trailing).length >= 2, JSON.stringify(ch(7).length));

    await stopCapture();
    await stopRecording();
    check('stop flushes channels 6 and 7', ch(6).some((c) => c.isFinal) && ch(7).some((c) => c.isFinal), JSON.stringify([ch(6).slice(-1), ch(7).slice(-1)]));
    check('every chunk of every channel carries its identity', chunks.every(identityOk),
      JSON.stringify(chunks.filter((c) => !identityOk(c)).slice(0, 3)));
  } finally {
    await context.close().catch(() => { /* */ });
    rmSync(dataDir, { recursive: true, force: true });
  }
  if (failed) { console.error(`\n❌ channel recording: ${failed} check(s) FAILED.`); process.exit(1); }
  console.log('\n✅ channel recording (L3): two speakers recorded with identity and worklet loudness; a brief detach survives; a leaving speaker is released; a new stream under a used id records on a new channel.');
}

main().catch((e) => { console.error(e); process.exit(1); });
