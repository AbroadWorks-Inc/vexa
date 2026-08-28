/**
 * Google Meet audio path — source-level contract guard.
 *
 * Run: npx tsx services/vexa-bot/core/src/platforms/googlemeet/recording-audio-path.test.ts
 *
 * WHY THIS EXISTS
 *
 * The Meet full-session recording is now captured Node-side: `parecord` reads
 * `${PULSE_SINK}.monitor` into a `RecordingService` WAV, which `upload()` POSTs
 * to the sidecar without a `chunk_seq` (the "this is the primary audio" half of
 * the contract). Two failure modes need guarding, in opposite directions:
 *
 *   ABSENT — the combined-blob path that made every transcript contain the
 *   meeting TWICE. MediaRecorder both uploaded each 30s chunk AND retained it in
 *   `__vexaRecordedChunks`; at shutdown those were combined into one blob and
 *   sent through `__vexaSaveRecordingBlob` → `RecordingService.writeBlob()`,
 *   landing the whole meeting on top of the chunks already uploaded. `writeBlob()`
 *   also repoints `filePath` at a `.webm`, which would now clobber the WAV.
 *   That fix was a DELETION, and a deletion leaves nothing behind to stop
 *   someone reinstating it — the identifiers read like a durability fallback.
 *
 *   PRESENT — the three call sites that make the WAV exist at all. Every test of
 *   the new primary path otherwise lives on the Python side of the wire, so
 *   deleting any one of these keeps the whole suite green while breaking the
 *   feature:
 *     · `startMeetAudioCapture()` in the `__vexaRecordingStarted` handler —
 *       delete it and there is NO WAV: `upload()` → `finalize()` rejects with
 *       "No write stream", the upload fails, and the bot falls back silently to
 *       the backup chunks. The entire primary path is gone.
 *     · `stopMeetAudioCapture()` in `performGracefulLeave` — delete it and the
 *       drain is skipped, the tail is lost, and parecord is leaked.
 *     · `drainMeetParecord()` inside `stopMeetAudioCapture` — delete it and the
 *       last stdout `data` arrives after `finalize()` and is silently discarded
 *       by the `isFinalized` guard. Truncation with no error anywhere.
 *
 * `platforms/googlemeet/recording.ts` and `index.ts` cannot be imported under
 * `tsx` (they pull in playwright-extra at module scope — the same constraint that
 * forced PcmChunker out into its own file), so this reads them as TEXT. No module
 * loading, no browser, no Playwright.
 */

import * as fs from 'fs';
import * as path from 'path';

// ─── Test harness ────────────────────────────────────────────────────────────

let passed = 0;
let failed = 0;

function assertEqual(actual: unknown, expected: unknown, label: string): void {
  const ok = JSON.stringify(actual) === JSON.stringify(expected);
  if (ok) {
    passed++;
    console.log(`  ✅ ${label}`);
  } else {
    failed++;
    console.log(`  ❌ ${label}`);
    console.log(`     expected: ${JSON.stringify(expected)}`);
    console.log(`     actual:   ${JSON.stringify(actual)}`);
  }
}

function pass(label: string): void {
  passed++;
  console.log(`  ✅ ${label}`);
}

function fail(label: string, detail: string): void {
  failed++;
  console.log(`  ❌ ${label}`);
  console.log(detail);
}

// ─── Source scanning ─────────────────────────────────────────────────────────

/**
 * Two index-aligned views of a source file, each the same length as the original
 * so an offset in one addresses the same character in the other.
 *
 * `noComments` — `//` and block comments blanked, newlines and string literals
 * kept. Used for searching: a reference can hide in a string
 * (`(window as any)["__vexaSaveRecordingBlob"]`), and a log line naming a banned
 * identifier is itself a sign the path came back.
 *
 * `skeleton` — comments AND string contents blanked. Used for brace matching: a
 * `{` inside a log message (or a template's `${…}`) must not throw off the depth
 * count when extracting a function body.
 */
interface Scanned {
  noComments: string;
  skeleton: string;
}

/**
 * Single-pass scanner tracking code / line-comment / block-comment / `'` / `"` /
 * backtick states. JSDoc continuation lines (leading `*`) need no special case —
 * they are inside a block comment already.
 *
 * It does not track regex literals. Neither target file contains one (verified by
 * grep), and the positive controls below fail loudly if it ever over-strips.
 */
function scan(source: string): Scanned {
  type Mode = 'code' | 'line' | 'block' | 'squote' | 'dquote' | 'template';
  let mode: Mode = 'code';
  let noComments = '';
  let skeleton = '';
  let i = 0;

  const emit = (kept: string, bare: string): void => {
    noComments += kept;
    skeleton += bare;
  };

  while (i < source.length) {
    const c = source[i];
    const c2 = source[i + 1];

    if (mode === 'code') {
      if (c === '/' && c2 === '/') {
        mode = 'line';
        emit('  ', '  ');
        i += 2;
        continue;
      }
      if (c === '/' && c2 === '*') {
        mode = 'block';
        emit('  ', '  ');
        i += 2;
        continue;
      }
      if (c === "'") mode = 'squote';
      else if (c === '"') mode = 'dquote';
      else if (c === '`') mode = 'template';
      // The opening quote itself is structurally inert — keep it in both views.
      emit(c, c);
      i++;
      continue;
    }

    if (mode === 'line') {
      if (c === '\n') {
        mode = 'code';
        emit('\n', '\n');
      } else {
        emit(' ', ' ');
      }
      i++;
      continue;
    }

    if (mode === 'block') {
      if (c === '*' && c2 === '/') {
        mode = 'code';
        emit('  ', '  ');
        i += 2;
        continue;
      }
      emit(c === '\n' ? '\n' : ' ', c === '\n' ? '\n' : ' ');
      i++;
      continue;
    }

    // Inside a string / template literal: keep the text, blank the skeleton.
    if (c === '\\') {
      emit(c + (c2 ?? ''), '  ');
      i += 2;
      continue;
    }
    const closes =
      (mode === 'squote' && c === "'") ||
      (mode === 'dquote' && c === '"') ||
      (mode === 'template' && c === '`');
    if (closes) mode = 'code';
    emit(c, closes ? c : c === '\n' ? '\n' : ' ');
    i++;
  }

  return { noComments, skeleton };
}

/** 1-based line number of a character offset. */
function lineOf(text: string, offset: number): number {
  let line = 1;
  for (let i = 0; i < offset && i < text.length; i++) {
    if (text[i] === '\n') line++;
  }
  return line;
}

/** Every 1-based line number in `source` (comments stripped) mentioning `needle`. */
function liveHits(source: string, needle: string): number[] {
  const lines = scan(source).noComments.split('\n');
  const hits: number[] = [];
  lines.forEach((line, idx) => {
    if (line.includes(needle)) hits.push(idx + 1);
  });
  return hits;
}

interface Block {
  /** Body text with comments stripped, from the opening `{` to its match. */
  code: string;
  startLine: number;
  endLine: number;
}

/**
 * Extract one function/handler body: locate `anchor`, then brace-match from the
 * first `{` at or after it. Anchors are matched in `noComments` (so a string
 * literal like `exposeFunction("__vexaRecordingStarted"` works as an anchor) but
 * braces are counted in `skeleton` (so a `{` inside a log message cannot skew the
 * depth). Returns null if the anchor is missing or the braces never balance —
 * both are reported as failures rather than passing vacuously.
 */
function extractBlock(source: string, anchor: string): Block | null {
  const s = scan(source);
  const at = s.noComments.indexOf(anchor);
  if (at < 0) return null;
  const open = s.skeleton.indexOf('{', at);
  if (open < 0) return null;

  let depth = 0;
  let end = -1;
  for (let i = open; i < s.skeleton.length; i++) {
    const ch = s.skeleton[i];
    if (ch === '{') depth++;
    else if (ch === '}') {
      depth--;
      if (depth === 0) {
        end = i;
        break;
      }
    }
  }
  if (end < 0) return null;

  return {
    code: s.noComments.slice(open, end + 1),
    startLine: lineOf(s.noComments, open),
    endLine: lineOf(s.noComments, end),
  };
}

/**
 * Locate a source file by walking up from the working directory.
 *
 * Not `__dirname`: tsc compiles this project as CommonJS but `tsx` executes the
 * file as an ES module, where `__dirname` is undefined — and `import.meta.url` is
 * the mirror-image problem (tsc rejects it under `module: commonjs`). Walking up
 * sidesteps both and works from the repo root, from `core/`, or from this
 * directory.
 */
function findSource(...segments: string[]): string {
  const fromCore = path.join('src', ...segments);
  const fromRepo = path.join('services', 'vexa-bot', 'core', fromCore);
  let dir = process.cwd();
  for (let depth = 0; depth < 10; depth++) {
    for (const rel of [fromCore, fromRepo]) {
      const candidate = path.join(dir, rel);
      if (fs.existsSync(candidate)) return candidate;
    }
    const parent = path.dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  throw new Error(
    `Could not locate ${fromCore} by walking up from ${process.cwd()}. ` +
      'Run from the repo root, from services/vexa-bot/core, or from this directory.',
  );
}

// ─── What must be ABSENT ─────────────────────────────────────────────────────

/**
 * Re-adding any of these doubles every Meet transcript. Each entry explains what
 * specifically breaks, so a failure tells the next person why rather than just
 * that a string reappeared.
 */
const BANNED: ReadonlyArray<{ needle: string; why: string }> = [
  {
    needle: '__vexaSaveRecordingBlob',
    why:
      'the Node-side sink for the full-session combined blob. Exposing it again ' +
      'sends the whole meeting to the sidecar a SECOND time, on top of the 30s ' +
      'backup chunks already uploaded — every transcript contains the meeting twice.',
  },
  {
    needle: '__vexaRecordedChunks',
    why:
      'the in-page array that retained every MediaRecorder blob so it could be ' +
      'combined at shutdown. Retaining them is what made the duplicate blob ' +
      'possible (and held ~200MB of renderer heap on a 1-hour call). Chunks are ' +
      'uploaded and dropped; nothing re-reads them.',
  },
  {
    needle: 'writeBlob',
    why:
      'the RecordingService method that repoints filePath at a `.webm`. That ' +
      'clobbers the `.wav` written by Node-side parecord capture — the primary ' +
      'full-session audio — so upload() would send the duplicate webm instead.',
  },
  {
    needle: 'new Blob(',
    why:
      'the combining step itself, and this check is NAME-INDEPENDENT on purpose. ' +
      'The three checks above ban identifiers, so reinstating the duplication ' +
      'under fresh names (__vexaKeptBlobs + __vexaSaveFullBlob, say) would slip ' +
      'straight past them — but it still has to build one Blob out of the retained ' +
      'parts. Nothing else on the Meet path needs to construct a Blob: the ' +
      'backup-chunk upload reads event.data.arrayBuffer() directly, and BlobEvent ' +
      '(the ondataavailable parameter type) is a type, not a constructor call.',
  },
];

// ─── What must be PRESENT ────────────────────────────────────────────────────

/**
 * A call site that must exist inside a specific enclosing block. Scoping matters:
 * `stopMeetAudioCapture` appearing SOMEWHERE in index.ts proves nothing — it has
 * to be reached from `performGracefulLeave`, the one teardown path every ending
 * shares.
 */
interface Wiring {
  /** Human label for the assertion line. */
  label: string;
  /** Which file: path segments under `src/`. */
  file: string[];
  /** Text locating the enclosing block; brace matching starts at the next `{`. */
  anchor: string;
  /** Must appear inside that block. */
  needle: string;
  /** What breaks if it is missing. */
  breaks: string;
  /**
   * A marker that must ALSO appear in the extracted block. Proves the brace
   * matching spanned the real body rather than stopping early on a nested `{`
   * — without it, a truncated extraction would fail confusingly or, worse, a
   * runaway one would make the needle check meaningless.
   */
  spanMarker: string;
}

const WIRING: ReadonlyArray<Wiring> = [
  {
    label: 'startMeetAudioCapture() is called from the __vexaRecordingStarted handler',
    file: ['platforms', 'googlemeet', 'recording.ts'],
    anchor: 'exposeFunction("__vexaRecordingStarted"',
    needle: 'startMeetAudioCapture(',
    spanMarker: 'resetSessionStart()',
    breaks:
      'NO WAV IS EVER OPENED. RecordingService.start() is never called, so ' +
      'upload() → finalize() rejects with "No write stream — recording was not ' +
      'started", the audio upload fails, and the bot falls back silently to the ' +
      '30s backup chunks. The entire primary audio path is gone, and nothing in ' +
      'the TypeScript suite notices. This handler is also the ONLY correct place ' +
      'for it: it is where publisher.resetSessionStart() runs, so the WAV\'s ' +
      'sample 0 and the speaker timeline\'s zero are the same instant. Starting ' +
      'capture anywhere earlier offsets them by the media-element wait (2s best ' +
      'case, up to 30s when findMediaElements retries) and every speaker name ' +
      'lands on the wrong utterance.',
  },
  {
    label: 'stopMeetAudioCapture() is called from performGracefulLeave',
    file: ['index.ts'],
    anchor: 'async function performGracefulLeave(',
    needle: 'stopMeetAudioCapture(',
    spanMarker: 'process.exit(finalCallbackExitCode)',
    breaks:
      'parecord is never SIGTERMed and the WAV is never finalized on the shared ' +
      'teardown path: the tail is lost, the child process is leaked, and the WAV ' +
      'header keeps its placeholder size unless upload() happens to finalize it ' +
      'lazily. It must live HERE and not in leaveGoogleMeet(): every ending other ' +
      'than a bot-initiated leave arrives with the page already destroyed (the ' +
      'host ended the call) or without startGoogleRecording ever returning (the ' +
      'removal monitor wins the Promise.race in meetingFlow.ts; SIGTERM/SIGINT).',
  },
  {
    label: 'drainMeetParecord() is called from stopMeetAudioCapture',
    file: ['platforms', 'googlemeet', 'recording.ts'],
    anchor: 'export async function stopMeetAudioCapture(',
    needle: 'drainMeetParecord(',
    spanMarker: '.finalize()',
    breaks:
      'SIGTERM is asynchronous — parecord can emit one last stdout `data` AFTER ' +
      'kill() returns. Without the drain, finalize() runs first and that tail is ' +
      'silently discarded by RecordingService\'s isFinalized guard. The recording ' +
      'is truncated with no error logged anywhere.',
  },
  {
    label: 'the WAV is opened inside startMeetAudioCapture (RecordingService.start)',
    file: ['platforms', 'googlemeet', 'recording.ts'],
    anchor: 'async function startMeetAudioCapture(',
    needle: '.start()',
    spanMarker: 'spawn(',
    breaks:
      'RecordingService.start() is what creates the write stream and stamps ' +
      'startTime. Without it appendPCMBuffer() no-ops (it early-returns on a null ' +
      'writeStream), finalize() rejects, and getStartTime() returns 0 — which also ' +
      'corrupts the audio/video mux delay in performGracefulLeave.',
  },
  {
    label: 'captured PCM is written to the WAV (appendPCMBuffer on the parecord stream)',
    file: ['platforms', 'googlemeet', 'recording.ts'],
    anchor: 'async function startMeetAudioCapture(',
    needle: '.appendPCMBuffer(',
    spanMarker: 'parecord',
    breaks:
      'parecord would run and produce audio that goes nowhere — a valid but ' +
      'EMPTY WAV would be uploaded as the primary audio, and the transcript would ' +
      'come back blank with no error on any path.',
  },
];

// ─── Tests ───────────────────────────────────────────────────────────────────

function checkAbsent(source: string, target: string): void {
  // Positive control. If scan() ever over-strips (a scanner bug, an unparsed
  // regex literal), every absence check would pass vacuously.
  // `__vexaSaveRecordingChunk` is the backup-chunk sink and MUST be live here.
  const control = liveHits(source, '__vexaSaveRecordingChunk');
  if (control.length === 0) {
    fail(
      'positive control: __vexaSaveRecordingChunk is still live in recording.ts',
      '     Either the per-chunk upload path was removed (a product requirement —\n' +
        '     the 30s webm chunks are the S3 backup), or scan() is over-stripping and\n' +
        '     every absence check below is vacuous. Fix before trusting this file.',
    );
  } else {
    pass(`positive control: __vexaSaveRecordingChunk live at line(s) ${control.join(', ')}`);
  }

  for (const { needle, why } of BANNED) {
    const hits = liveHits(source, needle);
    if (hits.length === 0) {
      pass(`no live reference to ${needle}`);
    } else {
      fail(
        `no live reference to ${needle}`,
        `     Found on line(s) ${hits.join(', ')} of\n` +
          `       ${target}\n\n` +
          `     RE-ADDING THIS DOUBLES EVERY GOOGLE MEET TRANSCRIPT.\n\n` +
          `     ${needle} is ${why}\n\n` +
          `     The primary full-session audio is the Node-side WAV written by\n` +
          `     parecord (startMeetAudioCapture in recording.ts). It survives the\n` +
          `     page being destroyed when the host ends the call, which the in-page\n` +
          `     blob path never could. If you need a durability fallback, it belongs\n` +
          `     on that WAV — not on a second copy of the browser recording.`,
      );
    }
  }
}

function checkWired(w: Wiring): void {
  const target = findSource(...w.file);
  const source = fs.readFileSync(target, 'utf8');
  const block = extractBlock(source, w.anchor);

  if (!block) {
    fail(
      w.label,
      `     Could not locate the enclosing block.\n` +
        `       file:   ${target}\n` +
        `       anchor: ${w.anchor}\n\n` +
        `     Either the anchor was renamed (update this test) or the braces never\n` +
        `     balanced (a scanner bug). Not treated as a pass — the wiring below is\n` +
        `     unverified either way.\n\n` +
        `     ${w.breaks}`,
    );
    return;
  }

  if (!block.code.includes(w.spanMarker)) {
    fail(
      `${w.label} — block extraction sanity`,
      `     Extracted lines ${block.startLine}-${block.endLine} of ${target},\n` +
        `     but the block does not contain the expected marker "${w.spanMarker}".\n` +
        `     Brace matching probably stopped early, so a needle check here would\n` +
        `     be meaningless. Fix the extraction before trusting this assertion.`,
    );
    return;
  }

  if (block.code.includes(w.needle)) {
    pass(`${w.label}  [lines ${block.startLine}-${block.endLine}]`);
  } else {
    fail(
      w.label,
      `     "${w.needle}" is NOT present in ${w.anchor}\n` +
        `       file:  ${target}\n` +
        `       block: lines ${block.startLine}-${block.endLine}\n\n` +
        `     IF THIS IS MISSING: ${w.breaks}`,
    );
  }
}

function selfTests(): void {
  // The absence detector must be able to fail.
  // The last line is the rename case: fresh identifiers throughout, caught only
  // by the name-independent `new Blob(` check.
  const reinstated = [
    '(window as any).__vexaRecordedChunks.push(event.data);',
    'await (window as any).__vexaSaveRecordingBlob({ base64 });',
    'await recordingService.writeBlob(buf, "webm");',
    'const full = new Blob((window as any).__vexaKeptBlobs, { type: mime });',
  ].join('\n');
  for (const { needle } of BANNED) {
    assertEqual(
      liveHits(reinstated, needle).length > 0,
      true,
      `detector catches ${needle} when it is reinstated`,
    );
  }

  // Comments must not trip it — recording.ts documents all three in prose.
  const commentsOnly = [
    '// __vexaSaveRecordingBlob is deliberately NOT exposed any more.',
    '/* It called RecordingService.writeBlob(), which doubled the audio. */',
    '/**',
    ' * __vexaRecordedChunks is gone; nothing re-reads the blobs.',
    ' * The old shutdown path did `new Blob(recorded)` — that is what doubled it.',
    ' */',
    'const stillCode = 1;',
  ].join('\n');
  for (const { needle } of BANNED) {
    assertEqual(liveHits(commentsOnly, needle), [], `comment mentioning ${needle} does not trip the guard`);
  }

  // Line numbering survives stripping, so reported lines match the file.
  assertEqual(
    liveHits(['const a = 1;', '/* two', '   three */', 'const b = __vexaRecordedChunks;'].join('\n'), '__vexaRecordedChunks'),
    [4],
    'reported line numbers match the source',
  );

  // A string literal is code, not a comment.
  assertEqual(
    liveHits('const k = "__vexaSaveRecordingBlob";', '__vexaSaveRecordingBlob'),
    [1],
    'an identifier inside a string literal still counts',
  );

  // A `//` inside a string is not a comment start.
  assertEqual(
    liveHits('const u = "https://x"; const v = writeBlob;', 'writeBlob'),
    [1],
    'a URL in a string does not blank the rest of the line',
  );

  // extractBlock scopes to the right function — a call in a NEIGHBOUR must not
  // satisfy an assertion about this one. This is the property the whole
  // presence half rests on.
  const twoFns = [
    'async function alpha() {',
    '  helper();',
    '}',
    'async function beta() {',
    '  other();',
    '}',
  ].join('\n');
  assertEqual(
    extractBlock(twoFns, 'async function beta(')?.code.includes('helper()'),
    false,
    'extractBlock does not leak a call from a neighbouring function',
  );
  assertEqual(
    extractBlock(twoFns, 'async function beta(')?.code.includes('other()'),
    true,
    'extractBlock finds the call inside the anchored function',
  );

  // A brace inside a log string must not end the block early.
  const braceInString = ['function f() {', '  log("a { brace");', '  wanted();', '}'].join('\n');
  assertEqual(
    extractBlock(braceInString, 'function f(')?.code.includes('wanted()'),
    true,
    'a brace inside a string does not truncate the extracted block',
  );

  // Nested blocks are spanned, not stopped at.
  const nested = ['function g() {', '  if (x) { inner(); }', '  tail();', '}'].join('\n');
  assertEqual(
    extractBlock(nested, 'function g(')?.code.includes('tail()'),
    true,
    'extractBlock spans nested braces',
  );

  // A missing anchor returns null (reported as a failure, never a silent pass).
  assertEqual(extractBlock(twoFns, 'function gamma('), null, 'a missing anchor yields null, not a vacuous pass');
}

function run(): void {
  console.log('Google Meet audio path — source contract\n');

  const recordingPath = findSource('platforms', 'googlemeet', 'recording.ts');
  const recordingSource = fs.readFileSync(recordingPath, 'utf8');

  console.log('  — the duplication path must be ABSENT —');
  checkAbsent(recordingSource, recordingPath);

  console.log('\n  — the Node-side capture path must be PRESENT and wired —');
  for (const w of WIRING) checkWired(w);

  console.log('\n  — the detector itself must be able to fail —');
  selfTests();
}

run();
console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) {
  process.exit(1);
}
