/**
 * Other notetaker bots — the real bridge carries BOT_IGNORED_PARTICIPANTS into the Teams watcher,
 * and the watcher's humans count (not its participant count) into the leave decision.
 *
 * The watcher's matching is proven in @vexa/teams-capture; this proves the two hops around it:
 *   env → createTeamsSpeakers({ ignoredParticipants })   and   roster-coverage.humans → activity.
 * Speaker naming (pipeline.recordRosterCoverage) keeps the full participant count.
 *
 * Run: pnpm --filter @vexa/bot exec tsx src/ignored-participants-wiring.test.ts
 */
import { readFileSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { startCaptureBridge } from './capture-bridge.js';
import { createRemoteAudioActivityTap } from './aloneness.js';
import type { Invocation } from './config.js';
import type { BotPipeline } from './pipeline.js';

let failed = 0;
const check = (name: string, cond: boolean, detail?: string): void => {
  console.log(`  ${cond ? '✅' : '❌'} ${name}${cond || !detail ? '' : ` — ${detail}`}`);
  if (!cond) failed++;
};

const BUNDLE = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', 'dist', 'browser-utils.global.js');
if (!existsSync(BUNDLE)) {
  console.error(`❌ missing ${BUNDLE} — build the capture bundle first (pnpm --filter @vexa/bot build).`);
  process.exit(1);
}

// ── Page-context shims on the REAL globalThis (the fake page.evaluate runs in-process) ───────────
const g = globalThis as unknown as Record<string, unknown>;
const savedDocument = g.document;
const realSetInterval = globalThis.setInterval;
const realClearInterval = globalThis.clearInterval;
(g as any).setInterval = () => 0;
(g as any).clearInterval = () => { /* controlled clock */ };
g.document = { body: {}, querySelector: () => null, querySelectorAll: () => [] };
g.MutationObserver = class { observe() { /* unused here */ } disconnect() { /* */ } };
g.window = g;
class FakeAudioContext {
  destination = {};
  createMediaStreamDestination(): unknown { return { stream: { id: 'mix', getAudioTracks: () => [{ id: 'mainAudio-mix' }] } }; }
  createMediaStreamSource(): unknown { return { connect: () => { /* connected */ } }; }
  createConstantSource(): unknown { return { offset: { value: 1 }, connect: () => { /* connected */ }, start: () => { /* started */ } }; }
  resume(): void { /* no-op */ }
  close(): void { /* no-op */ }
}
(g as any).AudioContext = FakeAudioContext;
g.__vexaCapturedRemoteAudioStreams = [{ id: 'stream-1', getAudioTracks: () => [{ id: 'mainAudio-abc' }] }];
g.__vexa_peer_connections = [];

// Load the REAL bundle, then stand in for the Teams watcher so the test holds what the bridge gave it.
new Function(readFileSync(BUNDLE, 'utf8'))();
const utils = g.VexaBrowserUtils as Record<string, unknown>;
check('bundle: createTeamsSpeakers is exported', typeof utils?.createTeamsSpeakers === 'function');
let watcherOpts: Record<string, any> | undefined;
utils.createTeamsSpeakers = (opts: Record<string, any>) => {
  watcherOpts = opts;
  return { getSpeaking: () => [], health: () => undefined, destroy: () => { /* stopped */ } };
};

const page = {
  async exposeFunction(name: string, fn: unknown): Promise<void> { g[name] = fn; },
  async evaluate(fn: (arg: never) => unknown, arg?: unknown): Promise<unknown> { return fn(arg as never); },
} as never;

const coverage: Array<{ named: number; participants: number }> = [];
const pipeline: BotPipeline = {
  async start() { /* not driven */ },
  async stop() { /* not driven */ },
  feedAudio() { /* not driven */ },
  feedMixedAudio() { /* not driven */ },
  recordHint() { /* not driven */ },
  recordRosterCoverage: (named, participants) => coverage.push({ named, participants }),
};
const activity = createRemoteAudioActivityTap();
const inv = { platform: 'teams', botName: 'Olanote', connectionId: 'test' } as unknown as Invocation;

const savedEnv = process.env.BOT_IGNORED_PARTICIPANTS;
process.env.BOT_IGNORED_PARTICIPANTS = 'Fireflies.ai Notetaker, otter.ai';
const stop = await startCaptureBridge(page, inv, pipeline, undefined, undefined, activity);

check('the configured list reached the Teams watcher, parsed',
  JSON.stringify(watcherOpts?.ignoredParticipants) === JSON.stringify(['fireflies.ai notetaker', 'otter.ai']),
  JSON.stringify(watcherOpts?.ignoredParticipants));

const now = Date.now();
watcherOpts?.onObservation?.({ type: 'roster-coverage', platform: 'teams', participants: 1, named: 1, humans: 0, tMs: now });
check('the leave decision sees humans=0, not the one ignored bot',
  activity.snapshot().rosterParticipants === 0, JSON.stringify(activity.snapshot()));
check('speaker naming still receives the full participant count',
  coverage.length === 1 && coverage[0].participants === 1 && coverage[0].named === 1, JSON.stringify(coverage));

watcherOpts?.onObservation?.({ type: 'roster-coverage', platform: 'teams', participants: 2, named: 2, tMs: now });
check('a coverage report without humans keeps the participant count for the leave decision',
  activity.snapshot().rosterParticipants === 2, JSON.stringify(activity.snapshot()));

await stop();
if (savedEnv === undefined) delete process.env.BOT_IGNORED_PARTICIPANTS;
else process.env.BOT_IGNORED_PARTICIPANTS = savedEnv;
(g as any).setInterval = realSetInterval;
(g as any).clearInterval = realClearInterval;
g.document = savedDocument;

if (failed) { console.error(`\n❌ ignored-participants-wiring: ${failed} checks FAILED.`); process.exit(1); }
console.log('\n✅ ignored-participants-wiring: the configured bot names reach the Teams watcher, and its humans count reaches the leave decision.');
