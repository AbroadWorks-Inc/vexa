/**
 * zoom-roster — Zoom name candidacy and accumulated-roster elimination.
 *
 * Run: npx tsx services/vexa-bot/core/src/services/zoom-roster.test.ts
 *
 * These tests exist because a ONE-LINE in-page predicate silently deleted a real
 * participant for the whole of a live meeting. `looksLikeName` rejected any
 * candidate whose first character was a lowercase Latin letter; the second
 * human's Zoom display name was `"sujoy sarkar"`. Because the predicate lived
 * inside `page.evaluate`, no test could reach it without a browser, so nothing
 * caught it. Every judgement is now a pure function here, and the case rule is
 * pinned OPEN by test so it cannot come back unnoticed.
 */

import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';

import {
  ZOOM_NAME_MAX_LENGTH,
  createZoomRosterObservatory,
  isZoomSelfName,
  looksLikeZoomDisplayName,
  normalizeZoomCandidates,
  pickSoleUnclaimed,
  pickZoomActiveSpeaker,
  type ZoomActiveSpeakerRead,
} from './zoom-roster';

let passed = 0;
let failed = 0;

function check(label: string, actual: unknown, expected: unknown): void {
  if (JSON.stringify(actual) === JSON.stringify(expected)) {
    passed++;
    console.log(`  ✅ ${label}`);
  } else {
    failed++;
    console.log(`  ❌ ${label}\n       expected: ${JSON.stringify(expected)}\n       actual:   ${JSON.stringify(actual)}`);
  }
}

/** Walk up from the cwd until `src/services/zoom-roster.ts` is found. */
function findSrcRoot(): string {
  const marker = join('src', 'services', 'zoom-roster.ts');
  let dir = resolve(process.cwd());
  for (let hops = 0; hops < 8; hops++) {
    if (existsSync(join(dir, marker))) return join(dir, 'src');
    const parent = dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  throw new Error(`could not locate ${marker} from ${process.cwd()} — run this test from the bot core directory`);
}

function run(): void {
  console.log('\n=== zoom-roster ===');

  // ── 1. THE live regression: a lowercase display name is a valid name ──────
  console.log('\nLive regression — lowercase display names:');
  check('accepts "sujoy sarkar" (the name the old heuristic deleted)', looksLikeZoomDisplayName('sujoy sarkar'), true);
  check('accepts "Utpalendu Sarkar" (the name that survived)', looksLikeZoomDisplayName('Utpalendu Sarkar'), true);
  // The two live names differ ONLY in leading case. If any case rule ever comes
  // back, this equality breaks — which is the whole point of asserting it.
  check(
    'candidacy does not depend on leading case',
    looksLikeZoomDisplayName('sujoy sarkar') === looksLikeZoomDisplayName('Sujoy Sarkar'),
    true,
  );
  check('accepts a lowercase single-word name', looksLikeZoomDisplayName('rahul'), true);
  check('accepts a name with a date in it ("Mr. Mason 2/5/02")', looksLikeZoomDisplayName('Mr. Mason 2/5/02'), true);
  check('accepts a non-Latin name', looksLikeZoomDisplayName('日本語の名前'), true);

  console.log('\nJunk rejection:');
  check('rejects empty', looksLikeZoomDisplayName(''), false);
  check('rejects whitespace only', looksLikeZoomDisplayName('   '), false);
  check(`rejects longer than ${ZOOM_NAME_MAX_LENGTH}`, looksLikeZoomDisplayName('x'.repeat(ZOOM_NAME_MAX_LENGTH + 1)), false);
  check(`accepts exactly ${ZOOM_NAME_MAX_LENGTH}`, looksLikeZoomDisplayName('x'.repeat(ZOOM_NAME_MAX_LENGTH)), true);
  // A multi-line candidate means the read grabbed a container, not a name — this
  // is the check that actually keeps concatenated tile text out.
  check('rejects multi-line text', looksLikeZoomDisplayName('Alice\nBob'), false);
  check('rejects tab-separated text', looksLikeZoomDisplayName('Alice\tBob'), false);
  check('rejects the exact UI label "Mute All"', looksLikeZoomDisplayName('Mute All'), false);
  check('rejects the exact UI label "you"', looksLikeZoomDisplayName('you'), false);
  // Whole-string comparison, never substring: a person may be called "Viewfinder".
  check('does NOT reject a name merely containing a UI word', looksLikeZoomDisplayName('Viewfinder Vince'), true);

  console.log('\nSelf-name detection:');
  check('matches the bot exactly', isZoomSelfName('AW Notetaker', 'AW Notetaker'), true);
  check('matches a decorated bot tile', isZoomSelfName('AW Notetaker (Me)', 'AW Notetaker'), true);
  check('matches a truncated bot tile', isZoomSelfName('AW Note', 'AW Notetaker'), true);
  check('does not match a real participant', isZoomSelfName('sujoy sarkar', 'AW Notetaker'), false);
  // Both live participants share a surname with each other but not with the bot;
  // an over-eager self filter would have been a second way to lose one of them.
  check('does not match either live participant', isZoomSelfName('Utpalendu Sarkar', 'AW Notetaker'), false);
  check('an absent bot name disables the test, it does not match everything', isZoomSelfName('anyone', undefined), false);
  check('an empty bot name disables the test', isZoomSelfName('anyone', '   '), false);

  console.log('\nCandidate normalization:');
  check(
    'trims, drops junk and the bot, de-duplicates, keeps first-seen order',
    normalizeZoomCandidates(
      ['  sujoy sarkar  ', 'AW Notetaker', 'Mute All', 'sujoy sarkar', '', null, undefined, 'Utpalendu Sarkar'],
      'AW Notetaker',
    ),
    ['sujoy sarkar', 'Utpalendu Sarkar'],
  );
  check('an all-junk read yields nothing', normalizeZoomCandidates(['', '  ', 'chat'], 'Bot'), []);

  // ── 2. Accumulation over time — the second live cause ────────────────────
  //
  // Zoom exposed exactly ONE name per instant: the live log read
  // `roster=[Utpalendu Sarkar]` twenty times, then later `roster=[sujoy sarkar]`,
  // not once both together in 21 observations. Each individual read was therefore
  // useless for elimination; the
  // union is not. These cases feed reads in exactly that shape.
  console.log('\nAccumulation over time (one name per read, as Zoom actually behaves):');
  {
    const obs = createZoomRosterObservatory();
    check('starts empty', obs.known(), []);
    check('a first read is all-new', obs.observe(['Utpalendu Sarkar'], 1_000), ['Utpalendu Sarkar']);
    check('re-observing the same name reports nothing new', obs.observe(['Utpalendu Sarkar'], 2_000), []);
    check('a different name later is new', obs.observe(['sujoy sarkar'], 3_000), ['sujoy sarkar']);
    check('the union holds both, in first-seen order', obs.known(), ['Utpalendu Sarkar', 'sujoy sarkar']);
    check('size counts distinct names', obs.size(), 2);
    check('last-seen tracks the most recent observation', obs.lastSeenMs('Utpalendu Sarkar'), 2_000);
    check('last-seen is null for a name never observed', obs.lastSeenMs('nobody'), null);
    check('clear forgets everything', (obs.clear(), obs.known()), []);
  }
  {
    // Freshness: a departed participant cannot be detected from a DOM that never
    // lists more than one person, so the bound is what stops their name being
    // handed to somebody else later.
    const obs = createZoomRosterObservatory();
    obs.observe(['Departed Dan'], 1_000);
    obs.observe(['Present Pat'], 100_000);
    check('unfiltered union holds both', obs.known(), ['Departed Dan', 'Present Pat']);
    check('a freshness bound drops the stale name', obs.known(50_000), ['Present Pat']);
    check('a name last seen exactly at the bound is kept', obs.known(1_000), ['Departed Dan', 'Present Pat']);
    check('a bound past everything drops everything', obs.known(200_000), []);
    // Re-observing revives a name: someone quiet is not someone gone.
    obs.observe(['Departed Dan'], 150_000);
    check('re-observation makes a stale name fresh again', obs.known(140_000), ['Departed Dan']);
  }

  // ── 3. Elimination, and its fail-safe ────────────────────────────────────
  console.log('\nElimination (fail-safe by design):');
  {
    const taken = new Set(['Utpalendu Sarkar']);
    const isTaken = (n: string) => taken.has(n);
    check(
      'exactly one unclaimed name resolves — the live two-person case',
      pickSoleUnclaimed(['Utpalendu Sarkar', 'sujoy sarkar'], isTaken),
      { name: 'sujoy sarkar', unclaimed: ['sujoy sarkar'] },
    );
    check(
      'an empty roster assigns nothing and says why',
      pickSoleUnclaimed([], isTaken),
      { name: null, reason: 'empty-roster', unclaimed: [] },
    );
    check(
      'every name claimed assigns nothing and says why',
      pickSoleUnclaimed(['Utpalendu Sarkar'], isTaken),
      { name: null, reason: 'all-claimed', unclaimed: [] },
    );
    check(
      'two unclaimed names assign nothing — a wrong real name is worse than an unknown',
      pickSoleUnclaimed(['Alice', 'Bob'], isTaken),
      { name: null, reason: 'ambiguous', unclaimed: ['Alice', 'Bob'] },
    );
    check(
      'three unclaimed names are ambiguous too',
      pickSoleUnclaimed(['Alice', 'Bob', 'Carol'], isTaken).name,
      null,
    );
    check(
      'a single unclaimed name in a one-name roster resolves',
      pickSoleUnclaimed(['sujoy sarkar'], isTaken),
      { name: 'sujoy sarkar', unclaimed: ['sujoy sarkar'] },
    );
  }

  // ── 4. Active-speaker selection, shared with the recording poll ──────────
  //
  // `platforms/zoom/web/recording.ts` used to inline its own copy of the name
  // rules ("mirrored from services/speaker-identity.ts", per its own comment) and
  // that copy kept the case rule after the original lost it. These cases cover
  // the single shared implementation it now calls.
  console.log('\nActive-speaker selection (the recording poll\'s logic, now testable):');
  const emptyRead = (over: Partial<ZoomActiveSpeakerRead> = {}): ZoomActiveSpeakerRead => ({
    layout1: null,
    layout2: null,
    gallery: [],
    ...over,
  });
  {
    check(
      'THE regression: a lowercase name is accepted on the recording poll path too',
      pickZoomActiveSpeaker(emptyRead({ layout1: 'sujoy sarkar' }), 'AW Notetaker'),
      { name: 'sujoy sarkar', source: 'layout1' },
    );
    check(
      'nothing anywhere yields null',
      pickZoomActiveSpeaker(emptyRead(), 'AW Notetaker'),
      null,
    );
    // Precedence is load-bearing: it is the pre-existing behaviour and must not
    // shift while the predicate underneath it changes.
    check(
      'layout1 wins over layout2',
      pickZoomActiveSpeaker(emptyRead({ layout1: 'First', layout2: 'Second' }), 'Bot')?.name,
      'First',
    );
    check(
      'layout2 is used when layout1 is absent',
      pickZoomActiveSpeaker(emptyRead({ layout2: 'Second' }), 'Bot'),
      { name: 'Second', source: 'layout2' },
    );
    check(
      'layout2 wins over gallery',
      pickZoomActiveSpeaker(
        emptyRead({ layout2: 'Second', gallery: [{ selector: '.g', raw: ['Gallery Person'] }] }),
        'Bot',
      )?.name,
      'Second',
    );
    // A layout1 candidate that is JUNK must not block the gallery — the old code
    // fell through on a rejected candidate and this must still do so.
    check(
      'a junk layout1 candidate falls through to gallery rather than blocking it',
      pickZoomActiveSpeaker(
        emptyRead({ layout1: 'Mute All', gallery: [{ selector: '.g', raw: ['sujoy sarkar'] }] }),
        'Bot',
      ),
      { name: 'sujoy sarkar', source: 'gallery:.g' },
    );
    check(
      'the bot on layout1 falls through too',
      pickZoomActiveSpeaker(
        emptyRead({ layout1: 'AW Notetaker', gallery: [{ selector: '.g', raw: ['Alice'] }] }),
        'AW Notetaker',
      )?.name,
      'Alice',
    );
  }
  {
    // The gallery rule: a selector is usable ONLY when it yields exactly one
    // distinct accepted name. Two names means the selector is too broad on this
    // Zoom version, and picking one would be a wrong confident bind.
    check(
      'a gallery selector matching TWO distinct names is refused, and the next is tried',
      pickZoomActiveSpeaker(
        emptyRead({
          gallery: [
            { selector: '.broad', raw: ['Alice', 'Bob'] },
            { selector: '.narrow', raw: ['Alice'] },
          ],
        }),
        'Bot',
      ),
      { name: 'Alice', source: 'gallery:.narrow' },
    );
    check(
      'every gallery selector being too broad yields null, never a guess',
      pickZoomActiveSpeaker(
        emptyRead({
          gallery: [
            { selector: '.a', raw: ['Alice', 'Bob'] },
            { selector: '.b', raw: ['Carol', 'Dave', 'Erin'] },
          ],
        }),
        'Bot',
      ),
      null,
    );
    // De-duplication happens before counting, so the SAME name on several tiles
    // is one distinct name and is usable.
    check(
      'the same name on three tiles is ONE distinct name, so the selector is usable',
      pickZoomActiveSpeaker(
        emptyRead({ gallery: [{ selector: '.g', raw: ['Alice', 'Alice', 'Alice'] }] }),
        'Bot',
      )?.name,
      'Alice',
    );
    check(
      'a selector whose only extra candidate is the BOT still counts as one name',
      pickZoomActiveSpeaker(
        emptyRead({ gallery: [{ selector: '.g', raw: ['Alice', 'AW Notetaker'] }] }),
        'AW Notetaker',
      )?.name,
      'Alice',
    );
    check(
      'nulls in a gallery read are ignored, not counted as candidates',
      pickZoomActiveSpeaker(
        emptyRead({ gallery: [{ selector: '.g', raw: [null, 'Alice', null] }] }),
        'Bot',
      )?.name,
      'Alice',
    );
    check(
      'a gallery selector matching only junk is refused',
      pickZoomActiveSpeaker(
        emptyRead({ gallery: [{ selector: '.g', raw: ['Participants', '   '] }] }),
        'Bot',
      ),
      null,
    );
    check(
      'the winning selector is named in the result, so a live log says which one worked',
      pickZoomActiveSpeaker(
        emptyRead({ gallery: [{ selector: '[class*="speaking"]', raw: ['Alice'] }] }),
        'Bot',
      )?.source,
      'gallery:[class*="speaking"]',
    );
  }

  // ── 5. Divergence guard ──────────────────────────────────────────────────
  //
  // The defect this whole section exists for was not the case rule itself but the
  // HAND-MIRRORED copy of it: `recording.ts` carried its own
  // `looksLikeName()/isSelfName()/accept()` trio, so deleting the rule from
  // `speaker-identity.ts` left the copy intact and `"sujoy sarkar"` still
  // unreturnable on that path. Re-fixing the copy without a guard leaves the same
  // trap for the next person, so these two cases scan the actual source tree.
  console.log('\nDivergence guard (scans the real source tree):');
  {
    // Located by searching upward for the marker file rather than from
    // `__dirname` or `import.meta.url`: this file must load identically under the
    // CommonJS `tsc` target and under tsx's ESM loader, and each of those
    // identifiers is a hard error in the other. (`production-replay.test.ts` and
    // `replay-meeting.test.ts` are both broken on aw/main for exactly this reason.)
    const srcRoot = findSrcRoot();
    const files: string[] = [];
    const walk = (dir: string): void => {
      for (const entry of readdirSync(dir)) {
        const full = join(dir, entry);
        if (statSync(full).isDirectory()) {
          // Skip the vendored Zoom Native SDK headers — C++, not our TypeScript.
          if (entry === 'native' || entry === 'node_modules') continue;
          walk(full);
          continue;
        }
        if (!entry.endsWith('.ts')) continue;
        if (entry.endsWith('.test.ts')) continue; // a test may quote the rule
        files.push(full);
      }
    };
    walk(srcRoot);
    // Sanity: the walk must actually be finding files, or both guards below pass
    // vacuously. This is the control, and it is deliberately a real number.
    check('the guard scans a non-trivial number of source files', files.length > 40, true);

    // Guard A — the leading-lowercase reject must exist NOWHERE. Matched on the
    // comparison itself rather than on a function name, because the next copy
    // will not necessarily be called `looksLikeName`.
    const caseRule = /\bfirst\s*[><]=?\s*'[az]'|charCodeAt\(0\)\s*[><]=?\s*(?:97|122)/;
    const offenders = files.filter((f) => caseRule.test(readFileSync(f, 'utf8')));
    check('no source file rejects a name for starting with a lowercase letter', offenders.map((f) => f.replace(srcRoot, 'src')), []);

    // Guard B — a USE COUNT, not a spelling check. Exactly one file still defines
    // its own in-page Zoom name predicate: `src/index.ts`'s Zoom roster fallback.
    // That copy is currently CORRECT (its comment reads "Allow any case") and sits
    // outside this change's allowlist, so it is recorded as known debt rather than
    // fixed. If a SECOND one appears — or that one is removed — this count moves
    // and the test goes red, which is the point.
    const inlinePredicate = files.filter((f) => /const looksLikeName\s*=|function looksLikeName\s*\(/.test(readFileSync(f, 'utf8')));
    check(
      'exactly one file still inlines its own name predicate (src/index.ts, known debt)',
      inlinePredicate.map((f) => f.replace(srcRoot, 'src')),
      ['src/index.ts'],
    );
    // And the shared definition must be defined exactly once.
    const shared = files.filter((f) => /export function looksLikeZoomDisplayName/.test(readFileSync(f, 'utf8')));
    check('the shared predicate has exactly one definition', shared.map((f) => f.replace(srcRoot, 'src')), ['src/services/zoom-roster.ts']);
  }

  console.log(`\n=== ${passed} passed, ${failed} failed ===\n`);
  if (failed > 0) process.exit(1);
}

run();
