/**
 * L3 boundary — the mixed-lane recorder reads the LIVE audio graph, not a DOM snapshot.
 *
 * The 2026-10-05 Jitsi RCA: `startRecording` snapshotted the page's media elements at admission
 * and recorded that mix for the whole meeting. On Jitsi the participants' WebRTC tracks arrive
 * seconds after `isJoined()`, so the snapshot held 45 preloaded sound-effect `<audio src>` elements
 * (or the JVB's silent placeholder track) and never the people. The capture lane, which reads the
 * live mix with a 2 s rescan, heard everything. Two consumers, two audio graphs.
 *
 * Drives the REAL bridge + REAL recorder over a Jitsi-shaped fixture, no meeting and no display:
 *   1. a page with a sound collection (file-backed `<audio preload="auto" src>` elements, as
 *      jitsi-meet's SoundCollection renders) and NO remote tracks at admission;
 *   2. startCaptureBridge (platform jitsi) then startRecording, exactly in the pipeline's order;
 *   3. a source that arrives AFTER the recorder started (an oscillator on the mix graph, standing in
 *      for a late WebRTC track) must land in the recording: the chunk recorded while it plays is
 *      several times the size of the chunk recorded before it (Opus on silence vs on a tone);
 *   4. the capture-stop path must NOT close the audio graph while the recorder is live (the
 *      pipeline stops capture before recording); the recording stop closes it after the final chunk.
 *
 * Where headless Chromium cannot launch the test SKIPS LOUDLY with exit 0 (same shape as
 * capture-bridge.boundary.test.ts). Run: npx tsx src/recording-source.boundary.test.ts
 */
import { execSync } from 'node:child_process';
import { existsSync, mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { launchPersistentBrowser, type BrowserContext } from '@vexa/remote-browser';
import { startCaptureBridge, startRecording } from './capture-bridge.js';
import type { BotRecordingSink } from './recording.js';
import type { BotPipeline, HintCounters } from './pipeline.js';
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

/** A 100 ms silent 8 kHz 8-bit PCM wav as a data URL: a file-backed sound, like jitsi's chimes. */
function silentWavDataUrl(): string {
  const samples = 800;
  const header = Buffer.alloc(44);
  header.write('RIFF', 0); header.writeUInt32LE(36 + samples, 4); header.write('WAVE', 8);
  header.write('fmt ', 12); header.writeUInt32LE(16, 16); header.writeUInt16LE(1, 20); header.writeUInt16LE(1, 22);
  header.writeUInt32LE(8000, 24); header.writeUInt32LE(8000, 28); header.writeUInt16LE(1, 32); header.writeUInt16LE(8, 34);
  header.write('data', 36); header.writeUInt32LE(samples, 40);
  return 'data:audio/wav;base64,' + Buffer.concat([header, Buffer.alloc(samples, 0x80)]).toString('base64');
}

const SOUND_COUNT = 12;
const FIXTURE = `<!doctype html><html><body>
  <div id="largeVideoContainer"></div>
  <!-- jitsi-meet BaseApp mounts <SoundCollection/>: one preloaded file-backed audio element per
       registered sound, present from page load, long before any participant track exists. -->
  <div id="sounds">${Array.from({ length: SOUND_COUNT }, (_, i) =>
    `<audio id="sound-${i}" preload="auto" src="${silentWavDataUrl()}"></audio>`).join('\n    ')}</div>
</body></html>`;

async function main(): Promise<void> {
  if (!existsSync(BUNDLE)) execSync('node build-browser-utils.mjs', { cwd: BOT_DIR, stdio: 'inherit' });
  const bundleHasTap = execSync(`grep -c createRecordingTap ${JSON.stringify(BUNDLE)} || true`).toString().trim() !== '0';
  check('browser bundle exposes createRecordingTap', bundleHasTap);

  const dataDir = mkdtempSync(join(tmpdir(), 'vexa-recsrc-'));
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

    const hintCounters: HintCounters = { received: 0, matched: 0, missed: 0 };
    const pipeline: BotPipeline = {
      async start() { /* stub */ }, async stop() { /* stub */ },
      feedAudio() { /* stub */ }, feedMixedAudio() { /* stub */ },
      recordHint() { hintCounters.received++; },
      hintCounters,
    };
    const inv: Invocation = {
      platform: 'jitsi', meetingUrl: 'https://meet.fixture.test/room', botName: 'Vexa',
      nativeMeetingId: 'room@meet.fixture.test', redisUrl: 'redis://localhost:6379', transcribeEnabled: false,
    };
    const chunks: { seq: number; isFinal: boolean; bytes: number; at: number }[] = [];
    const sink: BotRecordingSink = {
      chunk(_key, seq, isFinal, _format, bytes) { chunks.push({ seq, isFinal, bytes: bytes.length, at: Date.now() }); },
      close() { /* stub */ },
    };

    await page.setContent(FIXTURE);
    await page.addScriptTag({ path: BUNDLE });
    await page.evaluate('globalThis.__name = globalThis.__name || ((t, v) => t);');

    // 1 s timeslices so the silent-vs-tone comparison needs seconds, not minutes.
    process.env.VEXA_RECORDING_TIMESLICE_MS = '1000';
    // The pipeline's order: capture first, recording second (pipeline.ts start()).
    const stopCapture = await startCaptureBridge(page, inv, pipeline);
    const stopRecording = await startRecording(page, inv, sink);

    // ── the recorder reads the mixed-lane graph, never the element snapshot ──
    const graph = await page.evaluate(() => {
      const w = (globalThis as any) as Record<string, any>;
      return {
        hasMixDest: !!w.__vexaMixDest?.stream, ctxState: w.__vexaMixCtx?.state ?? null, hasTap: !!w.__vexaRecordingTap,
      };
    });
    check('mixed-lane audio graph exists before any remote track (eager, so the recorder can start on it)', graph.hasMixDest, JSON.stringify(graph));
    check('recording tap started', graph.hasTap, JSON.stringify(graph));
    check('recorder did not snapshot the page\'s sound-effect elements',
      !pageLogs.some((l) => /\[record-chunker\] \d+ media elements/.test(l)), JSON.stringify(pageLogs.filter((l) => l.includes('record-chunker'))));
    check('recorder reports it records the live mix', pageLogs.some((l) => /recording the live mix/.test(l)), JSON.stringify(pageLogs.filter((l) => /\[mixed\]|record/.test(l)).slice(0, 6)));

    // ── a source that arrives AFTER the recorder started lands in the recording ──
    // Headless Chromium keeps a fresh AudioContext suspended until something resumes it; a live
    // meeting page runs it. Resume it here so the recorder sees a running (silent) graph first.
    const running = await page.evaluate(async () => {
      const ctx = (globalThis as any).__vexaMixCtx;
      try { await ctx?.resume?.(); } catch { /* */ }
      return ctx?.state ?? 'absent';
    });
    await sleep(2300);                                   // ≥ 2 silent chunks
    const silentUpTo = chunks.length;
    const tone = await page.evaluate(async () => {
      const w = (globalThis as any) as Record<string, any>;
      const ctx = w.__vexaMixCtx, dest = w.__vexaMixDest;
      if (!ctx || !dest) return { ok: false, why: 'mix graph absent' };
      if (ctx.state !== 'running') return { ok: false, why: `context ${ctx.state}` };
      const osc = ctx.createOscillator(); osc.frequency.value = 440;
      const gain = ctx.createGain(); gain.gain.value = 0.5;
      osc.connect(gain); gain.connect(dest); osc.start();
      w.__vexaTestOsc = osc;
      return { ok: true, why: '' };
    });
    if (running !== 'running' || (!tone.ok && tone.why.startsWith('context'))) {
      console.log(`  ⚠️ SKIP tone assertion — AudioContext cannot run here (state ${running}; ${tone.why})`);
    } else {
      check('late source could be connected to the mix graph', tone.ok, tone.why);
      await sleep(3300);                                 // ≥ 2 chunks while the tone plays
      const silent = chunks.slice(0, silentUpTo).filter((c) => !c.isFinal && c.seq > 0).map((c) => c.bytes);
      const withTone = chunks.slice(silentUpTo).filter((c) => !c.isFinal).map((c) => c.bytes);
      const silentMax = Math.max(0, ...silent), toneMax = Math.max(0, ...withTone);
      check('chunks arrived before and after the late source', silent.length >= 1 && withTone.length >= 1, JSON.stringify(chunks));
      check('the chunk recorded while the late source plays is several times a silent chunk (the source is IN the recording)',
        toneMax >= 2048 && toneMax >= 3 * silentMax, `silent=${JSON.stringify(silent)} tone=${JSON.stringify(withTone)}`);
    }

    // ── teardown ownership: capture stop leaves the graph to the live recorder ──
    await stopCapture();
    const afterCaptureStop = await page.evaluate(() => ((globalThis as any).__vexaMixCtx?.state ?? 'absent'));
    check('capture stop does not close the audio graph while the recorder is live', afterCaptureStop !== 'closed' && afterCaptureStop !== 'absent', `state=${afterCaptureStop}`);
    await stopRecording();
    const afterRecordingStop = await page.evaluate(() => ((globalThis as any).__vexaMixCtx?.state ?? 'absent'));
    check('recording stop emits the final chunk', chunks.some((c) => c.isFinal), JSON.stringify(chunks.slice(-2)));
    check('recording stop closes the audio graph', afterRecordingStop === 'closed' || afterRecordingStop === 'absent', `state=${afterRecordingStop}`);
  } finally {
    await context.close().catch(() => { /* best-effort */ });
    rmSync(dataDir, { recursive: true, force: true });
  }

  console.log(failed === 0 ? '\n✅ recording-source boundary: all green' : `\n❌ recording-source boundary: ${failed} failure(s)`);
  process.exit(failed === 0 ? 0 : 1);
}

main().catch((e) => { console.error('❌ FAIL —', e?.stack || e); process.exit(1); });
