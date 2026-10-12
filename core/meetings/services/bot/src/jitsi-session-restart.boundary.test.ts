/**
 * L3 boundary — a Jitsi bridge session restart, end to end in a real Chromium with real peer
 * connections.
 *
 * When the bot's connection to the bridge breaks for longer than ICE allows, lib-jitsi-meet
 * closes the peer connection (every remote track ends) and builds a new one. With SSRC rewriting
 * the new connection's streams AND tracks are named `remote-audio-<n>` again, from 1. They are new
 * objects under ids the page has already seen. The bot must take them up on both lanes:
 *   - the mixed lane: the remote-audio hook mirrors the new track, the mix connects it, and the ONE
 *     mixed recorder carries it (no second chunk 0, chunks back to tone size);
 *   - the channel lane: the new stream records on a NEW channel number (the old one is released).
 *
 * The fixture is a loopback pair pcA → pcB built AFTER the hook is installed (as on a meeting page),
 * with pcA's offer munged to Jitsi's shape (`a=msid:remote-audio-1 remote-audio-1`). pcB's handler
 * plays `event.streams[0]` in an `<audio>` as Jitsi does; the restart re-uses that element for the
 * new stream. Where Chromium does not name the receiver track after the msid, the reused-id
 * condition cannot be built and the test SKIPS LOUDLY. Where headless Chromium cannot launch, the
 * test SKIPS LOUDLY with exit 0. Run: npx tsx src/jitsi-session-restart.boundary.test.ts
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

  const dataDir = mkdtempSync(join(tmpdir(), 'vexa-jitsi-restart-'));
  let context: BrowserContext;
  let page;
  try {
    ({ context, page } = await launchPersistentBrowser({
      dataDir,
      // Plain host candidates: two peer connections in one page connect over loopback without mDNS.
      args: ['--no-sandbox', '--mute-audio', '--autoplay-policy=no-user-gesture-required',
        '--disable-features=WebRtcHideLocalIpsWithMdns'],
      headless: true,
    }));
  } catch (e) {
    console.log(`  ⚠️ SKIP — headless Chromium unavailable in this environment: ${(e as Error).message?.split('\n')[0]}`);
    process.exit(0);
  }
  try {
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
    const mixed: { seq: number; isFinal: boolean; bytes: number; at: number }[] = [];
    const channels: { channel: number; seq: number; isFinal: boolean; bytes: number; identity?: ChannelChunkIdentity; at: number }[] = [];
    const sink: BotRecordingSink = {
      chunk(_key, seq, isFinal, _format, bytes) { mixed.push({ seq, isFinal, bytes: bytes.length, at: Date.now() }); },
      channelChunk(channel, seq, isFinal, _format, bytes, identity) {
        channels.push({ channel, seq, isFinal, bytes: bytes.length, identity, at: Date.now() });
      },
      close() { /* stub */ },
    };

    // A secure context (AudioWorklet, as on the https meeting page): 127.0.0.1 is one.
    const server = createServer((_req, res) => {
      res.writeHead(200, { 'Content-Type': 'text/html' });
      res.end('<!doctype html><html><body></body></html>');
    });
    await new Promise<void>((r) => server.listen(0, '127.0.0.1', () => r()));
    await page.goto(`http://127.0.0.1:${(server.address() as AddressInfo).port}/`);
    server.close();
    await page.addScriptTag({ path: BUNDLE });
    await page.evaluate('globalThis.__name = globalThis.__name || ((t, v) => t);');
    // The same call production's init script makes, before the page builds any peer connection.
    await page.evaluate(() => (globalThis as any).VexaBrowserUtils.installRemoteAudioHook({}));

    // One bridge session: a tone sent pcA → pcB under Jitsi's ids; pcB plays it in the Jitsi element.
    await page.evaluate(() => {
      const w = globalThis as any;
      w.__connectSession = async (freq: number): Promise<{ trackId: string; streamId: string }> => {
        const ctx = (w.__toneCtx ??= new w.AudioContext());
        await ctx.resume();
        const osc = ctx.createOscillator();
        osc.frequency.value = freq;
        const dest = ctx.createMediaStreamDestination();
        osc.connect(dest);
        osc.start();
        const pcA = new w.RTCPeerConnection();
        const pcB = new w.RTCPeerConnection();
        pcA.onicecandidate = (e: any) => { if (e.candidate) void pcB.addIceCandidate(e.candidate); };
        pcB.onicecandidate = (e: any) => { if (e.candidate) void pcA.addIceCandidate(e.candidate); };
        const received = new Promise<{ trackId: string; streamId: string }>((resolve) => {
          pcB.ontrack = (e: any) => {
            const el = (w.__jitsiElement ??= w.document.body.appendChild(w.document.createElement('audio')));
            el.autoplay = true;
            el.srcObject = e.streams[0];
            void el.play?.().catch?.(() => { /* autoplay flag is on */ });
            resolve({ trackId: e.track.id, streamId: e.streams[0]?.id ?? 'none' });
          };
        });
        pcA.addTrack(dest.stream.getAudioTracks()[0], dest.stream);
        const offer = await pcA.createOffer();
        await pcA.setLocalDescription(offer);
        const sdp = offer.sdp
          .replace(/^a=msid:.*$/gm, 'a=msid:remote-audio-1 remote-audio-1')
          .replace(/^(a=ssrc:\d+ msid:).*$/gm, '$1remote-audio-1 remote-audio-1');
        await pcB.setRemoteDescription({ type: 'offer', sdp });
        const answer = await pcB.createAnswer();
        await pcB.setLocalDescription(answer);
        await pcA.setRemoteDescription(answer);
        w.__session = { pcA, pcB, osc };
        return received;
      };
      // ICE failure: lib-jitsi-meet closes the connection, and with it every remote track.
      w.__closeSession = (): void => {
        const s = w.__session;
        s.pcB.close();
        s.pcA.close();
        s.osc.stop();
      };
    });

    process.env.VEXA_RECORDING_TIMESLICE_MS = '1000';
    process.env.VEXA_CHANNEL_GONE_MS = '4000';
    const stopCapture = await startCaptureBridge(page, inv, pipeline);
    const stopRecording = await startRecording(page, inv, sink);
    // Headless Chromium leaves a fresh AudioContext suspended; a meeting page runs it.
    await page.evaluate(async () => { try { await (globalThis as any).__vexaMixCtx?.resume?.(); } catch { /* */ } });
    await sleep(2500);
    const silentBefore = mixed.filter((c) => !c.isFinal && c.seq > 0).map((c) => c.bytes);

    const first = await page.evaluate(() => (globalThis as any).__connectSession(440));
    if (first.trackId !== 'remote-audio-1' || first.streamId !== 'remote-audio-1') {
      console.log(`  ⚠️ SKIP — this Chromium does not name the receiver after the msid (track=${first.trackId} stream=${first.streamId}); the reused-id condition cannot be built here`);
      await stopCapture();
      await stopRecording();
      return;
    }
    check('precondition: the receiver track and stream are named remote-audio-1, as on our Jitsi', true);
    await sleep(6000);
    const mirrored = () => pageLogs.filter((l) => /\[Audio Hook\] mirrored/.test(l));
    const toneMin = 3 * Math.max(1, ...silentBefore);
    check('session 1: the track is mirrored once', mirrored().length === 1, JSON.stringify(mirrored()));
    check('session 1: the mix connects it', pageLogs.some((l) => /\[mixed\] connected remote stream 1\b/.test(l)), JSON.stringify(pageLogs.filter((l) => /\[mixed\]/.test(l))));
    const toneHeard = mixed.some((c) => !c.isFinal && c.bytes >= toneMin);
    if (!toneHeard) {
      console.log('  diagnostics:', JSON.stringify(await page.evaluate(async () => {
        const w = globalThis as any;
        const inbound: any[] = [];
        (await w.__session.pcB.getStats()).forEach((r: any) => { if (r.type === 'inbound-rtp') inbound.push({ bytes: r.bytesReceived, energy: r.totalAudioEnergy }); });
        return { conn: w.__session.pcB.connectionState, inbound, mixCtx: w.__vexaMixCtx?.state, frames: w.__vexaMixFrames ?? null };
      })));
    }
    check('session 1: the mix records the tone', toneHeard, `silent=${JSON.stringify(silentBefore)} now=${JSON.stringify(mixed.slice(-3).map((c) => c.bytes))}`);
    check('session 1: channel 1 records remote-audio-1', pageLogs.some((l) => /recording ch1 remote-audio-1\b/.test(l)), JSON.stringify(pageLogs.filter((l) => /\[channels\]/.test(l))));

    // The bridge session ends. Every remote track ends with its connection.
    const closedAt = Date.now();
    await page.evaluate(() => (globalThis as any).__closeSession());
    await sleep(4000);
    const gap = mixed.filter((c) => !c.isFinal && c.at > closedAt + 2000);
    check('during the gap the mix records silence (it has lost its only source)',
      gap.length >= 1 && gap.every((c) => c.bytes < toneMin), JSON.stringify(mixed.slice(-4).map((c) => c.bytes)));

    // The new session: new objects, same ids.
    const second = await page.evaluate(() => (globalThis as any).__connectSession(660));
    const restart = Date.now();
    check('precondition: the new session reuses remote-audio-1', second.trackId === 'remote-audio-1' && second.streamId === 'remote-audio-1', JSON.stringify(second));
    await sleep(8000);
    check('session 2: the new track object is mirrored although its id was seen', mirrored().length === 2, JSON.stringify(mirrored()));
    check('session 2: the mix connects it', pageLogs.some((l) => /\[mixed\] connected remote stream 2\b/.test(l)), JSON.stringify(pageLogs.filter((l) => /\[mixed\]/.test(l))));
    check('session 2: mixed chunks are back to tone size', mixed.some((c) => !c.isFinal && c.at > restart + 3000 && c.bytes >= toneMin),
      `toneMin=${toneMin} after=${JSON.stringify(mixed.filter((c) => c.at > restart).map((c) => c.bytes))}`);
    check('one mixed recorder: a single chunk 0 and no gap in seq',
      mixed.filter((c) => c.seq === 0).length === 1 && mixed.every((c, i) => i === 0 || c.seq === mixed[i - 1].seq + 1 || c.isFinal || mixed[i - 1].isFinal),
      JSON.stringify(mixed.map((c) => c.seq)));
    check('channel 1 is released', pageLogs.some((l) => /released ch1 remote-audio-1\b/.test(l)), JSON.stringify(pageLogs.filter((l) => /\[channels\]/.test(l))));
    check('the new stream records on channel 2', pageLogs.some((l) => /recording ch2 remote-audio-1\b/.test(l)), JSON.stringify(pageLogs.filter((l) => /\[channels\]/.test(l))));
    const ch2 = channels.filter((c) => c.channel === 2);
    check('channel 2 uploads audio named remote-audio-1', ch2.filter((c) => c.bytes > 0).length >= 2 && ch2.every((c) => c.identity?.stream_id === 'remote-audio-1'),
      JSON.stringify(ch2.slice(0, 2)));

    await stopCapture();
    await stopRecording();
    check('stop flushes the mix once', mixed.filter((c) => c.isFinal).length === 1, JSON.stringify(mixed.filter((c) => c.isFinal)));
  } finally {
    await context.close().catch(() => { /* */ });
    rmSync(dataDir, { recursive: true, force: true });
  }
  if (failed) { console.error(`\n❌ jitsi session restart: ${failed} check(s) FAILED.`); process.exit(1); }
  console.log('\n✅ jitsi session restart (L3): a new bridge session under reused ids is mirrored, mixed into the one recording, and recorded on a new channel.');
}

main().catch((e) => { console.error(e); process.exit(1); });
