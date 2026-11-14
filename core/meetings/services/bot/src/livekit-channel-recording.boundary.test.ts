/**
 * L3 boundary — LiveKit per-channel recording, speaker hints and roster in a real Chromium.
 *
 * Drives the REAL bridge + REAL recorder over a page with a stub `window.VexaLivekit` (the session
 * @vexa/livekit-capture puts there) and three remote audio streams, each a tone, as the WebRTC hook
 * leaves them: two whose tracks are subscribed (Asha, Ben) and one pre-allocated receiver that
 * carries nobody.
 *   1. the two subscribed tracks are recorded as channels 0 and 1, kind `livekit`, participant id =
 *      the LiveKit sid, the name from the session; the pre-allocated stream is never recorded;
 *   2. LiveKit reuses Asha's receiver for Cy: Cy gets a new channel at once, Asha's is released at
 *      once (no grace: the stream now carries someone else) and sends its final chunk;
 *   3. Ben is unsubscribed: his channel is released;
 *   4. a LiveKit active speaker reaches the pipeline as a hint; the roster reaches the leave rule
 *      without other notetaker bots.
 *
 * Where headless Chromium cannot launch the test SKIPS LOUDLY with exit 0.
 * Run: npx tsx src/livekit-channel-recording.boundary.test.ts
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
import type { RemoteAudioActivityTap } from './aloneness.js';

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

  const dataDir = mkdtempSync(join(tmpdir(), 'vexa-livekit-channels-'));
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

    const hints: string[] = [];
    const pipeline = {
      async start() { /* stub */ }, async stop() { /* stub */ },
      feedAudio() { /* stub */ }, feedMixedAudio() { /* stub */ },
      recordHint(name: string, _t: number, isEnd: boolean) { hints.push(`${name}:${isEnd ? 'end' : 'on'}`); },
    } as unknown as BotPipeline;
    const rosterCounts: number[] = [];
    const activity: RemoteAudioActivityTap = {
      ready() { /* stub */ }, observeRemoteEnergy() { /* stub */ }, unavailable() { /* stub */ },
      observeRoster: (n: number) => { rosterCounts.push(n); },
    };
    const inv = {
      platform: 'livekit', meetingUrl: 'https://meet-next.fixture.test/abc-defg-hij', botName: 'Olanote',
      nativeMeetingId: 'abc-defg-hij', redisUrl: 'redis://localhost:6379', transcribeEnabled: false,
      perChannelRecordingEnabled: true,
    } as Invocation;
    const chunks: { channel: number; seq: number; isFinal: boolean; bytes: number; identity?: ChannelChunkIdentity; at: number }[] = [];
    const sink: BotRecordingSink = {
      chunk() { /* the mix lane: not under test */ },
      channelChunk(channel, seq, isFinal, _format, bytes, identity) {
        chunks.push({ channel, seq, isFinal, bytes: bytes.length, identity, at: Date.now() });
      },
      close() { /* stub */ },
    };

    const server = createServer((_req, res) => {
      res.writeHead(200, { 'Content-Type': 'text/html' });
      res.end('<!doctype html><html><body></body></html>');
    });
    await new Promise<void>((r) => server.listen(0, '127.0.0.1', () => r()));
    await page.goto(`http://127.0.0.1:${(server.address() as AddressInfo).port}/`);
    server.close();
    await page.addScriptTag({ path: BUNDLE });
    await page.evaluate('globalThis.__name = globalThis.__name || ((t, v) => t);');

    // Three streams as the hook leaves them, and the session's view of who is subscribed.
    await page.evaluate(async () => {
      const w = globalThis as any;
      const ctx = new w.AudioContext();
      await ctx.resume();
      const tracks: string[] = [];
      for (const freq of [440, 660, 330]) {
        const osc = ctx.createOscillator();
        osc.frequency.value = freq;
        const dest = ctx.createMediaStreamDestination();
        osc.connect(dest);
        osc.start();
        const el = w.document.createElement('audio');
        el.dataset.vexaInjected = 'true';
        el.srcObject = dest.stream;
        el.autoplay = true;
        w.document.body.appendChild(el);
        try { await el.play(); } catch { /* autoplay flag is on */ }
        tracks.push(dest.stream.getAudioTracks()[0].id);
      }
      w.__tracks = tracks;
      w.__subscribed = [
        { trackId: tracks[0], sid: 'PA_a', name: 'Asha' },
        { trackId: tracks[1], sid: 'PA_b', name: 'Ben' },
      ];
      w.VexaLivekit = {
        subscribedTracks: () => w.__subscribed,
        onSpeaking: (cb: (n: string, e: boolean, t: number) => void) => { w.__speak = cb; },
        onRoster: (ignored: string[], cb: (r: object) => void) => { w.__ignored = ignored; w.__roster = cb; },
        state: () => ({ connected: true }),
      };
    });

    process.env.VEXA_RECORDING_TIMESLICE_MS = '1000';
    process.env.VEXA_CHANNEL_GONE_MS = '30000';
    process.env.BOT_IGNORED_PARTICIPANTS = 'notetaker';
    const stopCapture = await startCaptureBridge(page, inv, pipeline, undefined, undefined, activity);
    const stopRecording = await startRecording(page, inv, sink);

    await sleep(5000);
    const ch = (n: number) => chunks.filter((c) => c.channel === n);
    check('Asha is recorded as channel 0', ch(0).filter((c) => c.bytes > 0).length >= 2, String(ch(0).length));
    check('Ben is recorded as channel 1', ch(1).filter((c) => c.bytes > 0).length >= 2, String(ch(1).length));
    check('the pre-allocated receiver is never recorded', !chunks.some((c) => c.channel >= 2), JSON.stringify([...new Set(chunks.map((c) => c.channel))]));
    const idOk = (c: (typeof chunks)[number], sid: string, name: string) =>
      c.identity?.channel_kind === 'livekit' && c.identity?.participant_id === sid && c.identity?.display_name === name;
    check('channel 0 chunks say livekit, PA_a, Asha', ch(0).length > 0 && ch(0).every((c) => idOk(c, 'PA_a', 'Asha')), JSON.stringify(ch(0)[0]?.identity));
    check('channel 1 chunks say livekit, PA_b, Ben', ch(1).length > 0 && ch(1).every((c) => idOk(c, 'PA_b', 'Ben')), JSON.stringify(ch(1)[0]?.identity));

    // LiveKit gives Asha's receiver to Cy.
    await page.evaluate(() => {
      const w = globalThis as any;
      w.__subscribed = [{ trackId: w.__tracks[0], sid: 'PA_c', name: 'Cy' }, w.__subscribed[1]];
    });
    const moved = Date.now();
    await sleep(5000);
    check('Cy gets a new channel at once', ch(2).filter((c) => c.bytes > 0 && c.at > moved).length >= 2 && ch(2).every((c) => idOk(c, 'PA_c', 'Cy')), JSON.stringify(ch(2).slice(0, 2)));
    check('Asha\'s channel is released at once with its final chunk', ch(0).some((c) => c.isFinal && c.at - moved < 4500), JSON.stringify(ch(0).slice(-2)));
    check('no Asha chunk after the release', !ch(0).some((c) => c.at > moved + 4500 && !c.isFinal), JSON.stringify(ch(0).slice(-2)));

    // Ben leaves.
    await page.evaluate(() => { const w = globalThis as any; w.__subscribed = [w.__subscribed[0]]; });
    const left = Date.now();
    await sleep(4000);
    check('Ben\'s channel is released when he is unsubscribed', ch(1).some((c) => c.isFinal && c.at > left), JSON.stringify(ch(1).slice(-2)));
    check('Cy keeps recording', ch(2).some((c) => c.at > left + 2000 && c.bytes > 0), JSON.stringify(ch(2).slice(-2)));

    // Speaker hints and the roster.
    await page.evaluate(() => {
      const w = globalThis as any;
      w.__speak?.('Cy', false, Date.now());
      w.__speak?.('Cy', true, Date.now());
      w.__roster?.({ participants: 2, named: 2, humans: 1, tMs: Date.now() });
    });
    await sleep(500);
    check('a LiveKit active speaker reaches the pipeline as a hint', hints.join(' ') === 'Cy:on Cy:end', hints.join(' '));
    check('the session gets the ignored-bot fragments', JSON.stringify(await page.evaluate(() => (globalThis as any).__ignored)) === JSON.stringify(['notetaker']));
    check('the roster reaches the leave rule without other notetaker bots', rosterCounts[rosterCounts.length - 1] === 1, JSON.stringify(rosterCounts));

    await stopRecording();
    await stopCapture();
    const ids = JSON.stringify(chunks.map((c) => c.identity));
    check('no chunk carries anything but the sid as participant id', !ids.includes('@'), ids.slice(0, 200));
  } finally {
    await context.close().catch(() => { /* */ });
    rmSync(dataDir, { recursive: true, force: true });
  }
  if (failed) { console.error(`\n❌ livekit channel recording (L3): ${failed} check(s) FAILED.`); process.exit(1); }
  console.log('\n✅ livekit channel recording (L3): subscribed tracks are channels named from the session, a reused receiver is a new channel, a left person is released, hints and roster cross.');
}

main().catch((e) => { console.error(e); process.exit(1); });
