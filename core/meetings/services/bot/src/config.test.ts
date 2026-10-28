/**
 * L1/L2 — invocation.v1 boot config (ARCHITECTURE.md §5). Drives the real ajv-backed
 * parser against the PUBLISHED goldens (P8: the goldens are the spec) and asserts:
 *   • every committed golden (minimal + full + jitsi) parses;
 *   • the env helper round-trips VEXA_BOT_CONFIG;
 *   • off-contract input (missing required, unknown action, bad enum, non-JSON, absent)
 *     fails fast with an InvocationError (P14).
 * No browser / redis / STT. Run: npx tsx src/config.test.ts
 */
import { readFileSync, readdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { parseInvocation, loadInvocation, InvocationError, speakerStreamConfigFromEnv, recordingSource } from './config.js';

const HERE = dirname(fileURLToPath(import.meta.url));
const GOLDEN_DIR = join(HERE, '..', '..', '..', 'contracts', 'invocation.v1', 'golden');

let failed = 0;
const check = (name: string, cond: boolean, detail = '') => {
  console.log(`  ${cond ? '✅' : '❌'} ${name}${cond ? '' : '  — ' + detail}`);
  if (!cond) failed++;
};
const throws = (fn: () => unknown): Error | null => { try { fn(); return null; } catch (e) { return e as Error; } };

// ── every committed invocation.v1 golden parses ──
const goldens = readdirSync(GOLDEN_DIR).filter((n) => n.startsWith('Invocation.') && n.endsWith('.json'));
check('found all invocation goldens', goldens.length === 3, `got ${goldens.join(', ')}`);
for (const g of goldens) {
  const raw = readFileSync(join(GOLDEN_DIR, g), 'utf8');
  const err = throws(() => parseInvocation(raw));
  check(`golden ${g} parses`, err === null, err?.message ?? '');
}

// ── typed access on the full golden ──
{
  const full = parseInvocation(readFileSync(join(GOLDEN_DIR, 'Invocation.full.json'), 'utf8'));
  check('full: platform = google_meet', full.platform === 'google_meet', full.platform);
  check('full: recordingEnabled true', full.recordingEnabled === true);
  check('full: automaticLeave threaded', full.automaticLeave?.waitingRoomTimeout === 300000, String(full.automaticLeave?.waitingRoomTimeout));
  check('full: secret token present (not logged)', typeof full.token === 'string' && full.token.length > 0);
  check('full: transcriptionModel threaded (#522)', full.transcriptionModel === 'whisper-large-v3-turbo', String(full.transcriptionModel));
  check('full: captureSignalEnabled threaded (O-TEL-1)', full.captureSignalEnabled === true, String(full.captureSignalEnabled));
  check('full: perChannelRecordingEnabled threaded', full.perChannelRecordingEnabled === true, String(full.perChannelRecordingEnabled));
}

// ── typed access on the jitsi golden (the platform enum accepts jitsi) ──
{
  const jitsi = parseInvocation(readFileSync(join(GOLDEN_DIR, 'Invocation.jitsi.json'), 'utf8'));
  check('jitsi: platform = jitsi', jitsi.platform === 'jitsi', jitsi.platform);
  check('jitsi: meetingUrl carries the deployment host', jitsi.meetingUrl === 'https://meet.jit.si/VexaStandup', String(jitsi.meetingUrl));
}

// ── the env helper (P7: config by env) ──
{
  const minimal = readFileSync(join(GOLDEN_DIR, 'Invocation.minimal.json'), 'utf8');
  const inv = loadInvocation({ VEXA_BOT_CONFIG: minimal } as NodeJS.ProcessEnv);
  check('loadInvocation reads VEXA_BOT_CONFIG', inv.botName === 'Vexa', inv.botName);
}

// ── fail-fast (P14) ──
{
  check('missing env → InvocationError', throws(() => loadInvocation({} as NodeJS.ProcessEnv)) instanceof InvocationError);
  check('empty string → InvocationError', throws(() => parseInvocation('   ')) instanceof InvocationError);
  check('non-JSON → InvocationError', throws(() => parseInvocation('not json {')) instanceof InvocationError);
  check('missing required field → InvocationError',
    throws(() => parseInvocation(JSON.stringify({ platform: 'google_meet', botName: 'B' }))) instanceof InvocationError);
  check('unknown property (additionalProperties:false) → InvocationError',
    throws(() => parseInvocation(JSON.stringify({ platform: 'google_meet', meetingUrl: 'x', botName: 'B', redisUrl: 'redis://r', bogus: 1 }))) instanceof InvocationError);
  check('bad platform enum → InvocationError',
    throws(() => parseInvocation(JSON.stringify({ platform: 'webex', meetingUrl: 'x', botName: 'B', redisUrl: 'redis://r' }))) instanceof InvocationError);
}

if (failed) { console.error(`\n❌ config (L1/L2): ${failed} check(s) FAILED.`); process.exit(1); }
console.log('\n✅ config (L1/L2): the goldens parse, the env helper round-trips, and off-contract input fails fast (ajv ≡ invocation.v1).');

// ── speaker-stream env tuning ─────────────────────────────────────────────────
{
  const warnings: string[] = [];
  const config = speakerStreamConfigFromEnv({
    BOT_SPEAKER_MIN_AUDIO_SEC: '1',
    BOT_SPEAKER_SUBMIT_INTERVAL_SEC: '1.5',
    BOT_SPEAKER_CONFIRM_THRESHOLD: '1',
    BOT_SPEAKER_MAX_BUFFER_SEC: '30',
    BOT_SPEAKER_IDLE_TIMEOUT_SEC: '15',
  }, (message) => warnings.push(message));
  check('speaker-stream env values reach the config', config?.minAudioDuration === 1 && config.submitInterval === 1.5 && config.confirmThreshold === 1 && config.maxBufferDuration === 30 && config.idleTimeoutSec === 15);
  check('valid speaker-stream env emits no warnings', warnings.length === 0, warnings.join('; '));

  const invalidWarnings: string[] = [];
  const invalid = speakerStreamConfigFromEnv({
    BOT_SPEAKER_MIN_AUDIO_SEC: 'nope',
    BOT_SPEAKER_CONFIRM_THRESHOLD: '1.5',
  }, (message) => invalidWarnings.push(message));
  check('invalid speaker-stream values fall back loudly', invalid === undefined && invalidWarnings.length === 2, invalidWarnings.join('; '));
}

// ── recordingSource: the ONE predicate that says where the page-side recorder reads its audio ──
// Zoom records the live per-track mix; Teams and Jitsi record the live mixed-lane graph (the same
// destination the PCM capture reads); only the gmeet lane keeps the media-element snapshot. A mixed
// lane that snapshots elements records whatever the page had at admission — on Jitsi that was 45
// preloaded sound effects or the JVB's silent placeholder, never the participants (2026-10-05 RCA).
{
  const rs = (p: string): string => { try { return String(recordingSource(p)); } catch (e) { return `threw: ${(e as Error).message}`; } };
  check('recordingSource: zoom records the per-track mix', rs('zoom') === 'pertrack-mix', rs('zoom'));
  check('recordingSource: teams records the mixed-lane graph', rs('teams') === 'mixed-mix', rs('teams'));
  check('recordingSource: jitsi records the mixed-lane graph', rs('jitsi') === 'mixed-mix', rs('jitsi'));
  check('recordingSource: google_meet keeps the element snapshot', rs('google_meet') === 'element-snapshot', rs('google_meet'));
}

// ── per-channel recorders: read from the invocation only; absent = off, never from the bot env ──
{
  const minimal = JSON.parse(readFileSync(join(GOLDEN_DIR, 'Invocation.minimal.json'), 'utf8')) as Record<string, unknown>;
  const absent = parseInvocation(JSON.stringify(minimal));
  check('per-channel recording is absent (off) when the invocation omits it', absent.perChannelRecordingEnabled === undefined);
  const off = parseInvocation(JSON.stringify({ ...minimal, perChannelRecordingEnabled: false }));
  check('per-channel recording false rides as false', off.perChannelRecordingEnabled === false);
  const err = throws(() => parseInvocation(JSON.stringify({ ...minimal, perChannelRecordingEnabled: 'true' })));
  check('per-channel recording must be a boolean', err instanceof InvocationError, err?.message ?? 'no error');
}

console.log(failed === 0 ? '\n✅ config: all green' : `\n❌ config: ${failed} failure(s)`);
process.exit(failed === 0 ? 0 : 1);
