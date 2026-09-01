/**
 * Zoom speaker resolution — the 2026-09-01 misattribution regression.
 *
 * Run: npx tsx services/vexa-bot/core/src/services/speaker-identity.test.ts
 *
 * A live 2-human Zoom meeting attributed ALL speech to one person: 66 timeline
 * intervals, every one the same participant, one speaker label in
 * `transcript.txt`. Three defects in the Zoom cascade produced it, and each has
 * a case below built to the exact shape the live log recorded:
 *
 *   1. `looksLikeName` rejected any candidate starting with a lowercase Latin
 *      letter. The second participant's display name was `"sujoy sarkar"`.
 *   2. Only two hardcoded layouts were consulted for the active speaker, and the
 *      bot logged `Could not confirm Speaker View after retries` — it was in
 *      GALLERY view, where neither existed.
 *   3. Zoom showed exactly ONE name at any instant (`roster=[Utpalendu Sarkar]`
 *      twenty times, then later `roster=[sujoy sarkar]`), and Zoom had no
 *      elimination layer, so a point-in-time roster could never name anybody.
 *
 * ── What this fake CANNOT prove ─────────────────────────────────────────────
 * The fake DOM matches selectors by exact STRING equality against a list each
 * node declares. It therefore exercises the CASCADE — which layer fires, what it
 * accepts, what it refuses — and cannot detect a mistyped or outdated selector.
 * Whether `.gallery-video-container__video-frame--active` is really Zoom's class,
 * and whether Zoom's per-participant `<audio>` elements sit inside their own tile
 * subtree at all, are answerable only by a live run.
 */

import {
  ZOOM_ROSTER_FRESHNESS_MS,
  getLockedMapping,
  isNameTaken,
  invalidateSpeakerName,
  clearSpeakerNameCache,
  clearZoomRoster,
  getZoomRosterNames,
  reportTrackAudio,
  resolveSpeakerName,
} from './speaker-identity';

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

// ─── Fake DOM ────────────────────────────────────────────────────────────────

/** Declarative node. `sel` lists the selectors this node answers to. */
interface NodeSpec {
  tag?: string;
  sel?: string[];
  /** textContent / innerText of this node. */
  text?: string;
  /** Adds a child <span> carrying this text — how Zoom renders a footer name. */
  spanText?: string;
  /** Make this a live media element with one audio track (what the reader counts). */
  media?: boolean;
  children?: NodeSpec[];
}

class FakeMediaStream {
  getAudioTracks(): unknown[] {
    return [{}];
  }
}

class FakeEl {
  tagName: string;
  private readonly sel: Set<string>;
  private readonly ownText: string | null;
  readonly childNodes: FakeEl[] = [];
  parentElement: FakeEl | null = null;
  paused = true;
  srcObject: unknown = null;

  constructor(spec: NodeSpec) {
    this.tagName = (spec.tag ?? 'div').toUpperCase();
    this.sel = new Set(spec.sel ?? []);
    this.ownText = spec.text ?? null;
    if (spec.media) {
      this.tagName = spec.tag ? this.tagName : 'AUDIO';
      this.paused = false;
      this.srcObject = new FakeMediaStream();
    }
    if (spec.spanText !== undefined) {
      this.append(new FakeEl({ tag: 'span', text: spec.spanText }));
    }
    for (const child of spec.children ?? []) this.append(new FakeEl(child));
  }

  private append(child: FakeEl): void {
    child.parentElement = this;
    this.childNodes.push(child);
  }

  get textContent(): string | null {
    if (this.ownText !== null) return this.ownText;
    const parts = this.childNodes.map((c) => c.textContent ?? '').filter((t) => t.length > 0);
    return parts.length > 0 ? parts.join('') : null;
  }

  get innerText(): string | null {
    return this.textContent;
  }

  matches(selector: string): boolean {
    for (const part of selector.split(',')) {
      const s = part.trim();
      if (!s) continue;
      if (this.sel.has(s)) return true;
      if (s.toUpperCase() === this.tagName) return true;
    }
    return false;
  }

  private descendants(): FakeEl[] {
    const out: FakeEl[] = [];
    for (const child of this.childNodes) {
      out.push(child);
      out.push(...child.descendants());
    }
    return out;
  }

  querySelectorAll(selector: string): FakeEl[] {
    return this.descendants().filter((el) => el.matches(selector));
  }

  querySelector(selector: string): FakeEl | null {
    return this.querySelectorAll(selector)[0] ?? null;
  }
}

/** A `document` with a `body`, which is where the walk-up loop terminates. */
function makeDocument(bodyChildren: NodeSpec[]): { body: FakeEl } & Pick<FakeEl, 'querySelector' | 'querySelectorAll'> {
  const body = new FakeEl({ tag: 'body', children: bodyChildren });
  return {
    body,
    querySelectorAll: (s: string) => body.querySelectorAll(s),
    querySelector: (s: string) => body.querySelector(s),
  };
}

/**
 * A fake Playwright Page. `evaluate` runs the callback in-process against the
 * stubbed `document` / `MediaStream`, which is the entire browser surface the
 * Zoom reader touches.
 */
function makePage(bodyChildren: NodeSpec[]): any {
  const doc = makeDocument(bodyChildren);
  return {
    async evaluate(fn: (arg?: any) => any, arg?: any) {
      const g = globalThis as any;
      const prevDoc = g.document;
      const prevMs = g.MediaStream;
      g.document = doc;
      g.MediaStream = FakeMediaStream;
      try {
        return fn(arg);
      } finally {
        g.document = prevDoc;
        g.MediaStream = prevMs;
      }
    },
  };
}

const BOT = 'AW Notetaker';
/** A tile footer as Zoom renders it: the class, with the name in a <span>. */
const footer = (name: string): NodeSpec => ({
  sel: ['.video-avatar__avatar-footer', '[class*="avatar-footer"]'],
  spanText: name,
});
/** A bare live audio element — the shape Google Meet uses (no tile ancestry). */
const bareMedia = (): NodeSpec => ({ media: true });
/** A node matching ZOOM_TILE_SELECTOR — i.e. one that passes the shape guard. */
const tile = (children: NodeSpec[]): NodeSpec => ({ sel: ['[class*="video-avatar"]'], children });
/** A plain, non-tile-shaped wrapper. */
const wrap = (children: NodeSpec[]): NodeSpec => ({ children });
/** Nest `inner` `levels` deep inside plain wrappers. */
const nest = (levels: number, inner: NodeSpec): NodeSpec => {
  let node = inner;
  for (let i = 0; i < levels; i++) node = wrap([node]);
  return node;
};

/** Run `fn` with every `log()` line captured instead of printed. */
async function captureLogs<T>(fn: () => Promise<T>): Promise<{ result: T; lines: string[] }> {
  const lines: string[] = [];
  const real = console.log;
  console.log = (...args: unknown[]) => {
    lines.push(args.map(String).join(' '));
  };
  try {
    const result = await fn();
    return { result, lines };
  } finally {
    console.log = real;
  }
}

/** `resolveSpeakerName` for Zoom at a fixed time — keeps the cases readable. */
function resolveSsolveHelper(page: any, trackIndex: number): Promise<string> {
  return resolveSpeakerName(page, trackIndex, 'zoom', BOT, 1_000);
}

/** Reset every piece of module-level state this file depends on. */
function reset(): void {
  clearSpeakerNameCache();
  clearZoomRoster();
}

// ─── Tests ───────────────────────────────────────────────────────────────────

async function run(): Promise<void> {
  console.log('\n=== Zoom speaker resolution ===');

  // ── Defect 1: the lowercase display name ─────────────────────────────────
  console.log('\nDefect 1 — a lowercase display name must resolve:');
  {
    reset();
    reportTrackAudio(0);
    // The audio element sits INSIDE its tile, and the tile carries one footer.
    const page = makePage([{ children: [footer('sujoy sarkar'), bareMedia()] }]);
    const name = await resolveSpeakerName(page, 0, 'zoom', BOT, 1_000);
    check('resolves "sujoy sarkar" from its own tile footer', name, 'sujoy sarkar');
  }
  {
    reset();
    reportTrackAudio(0);
    const page = makePage([{ children: [footer('Utpalendu Sarkar'), bareMedia()] }]);
    const name = await resolveSpeakerName(page, 0, 'zoom', BOT, 1_000);
    // The control for the case above: live, THIS name resolved and the lowercase
    // one did not. Both must now resolve identically.
    check('resolves "Utpalendu Sarkar" the same way (the name that always worked)', name, 'Utpalendu Sarkar');
  }
  {
    reset();
    reportTrackAudio(0);
    // An ancestor holding two footers is not this participant's tile, so the
    // walk-up must skip it rather than hand over an arbitrary neighbour's name.
    // Both names then stay unclaimed, so elimination is ambiguous too → unknown.
    const page = makePage([
      { children: [footer('sujoy sarkar'), footer('Utpalendu Sarkar'), bareMedia()] },
    ]);
    const name = await resolveSpeakerName(page, 0, 'zoom', BOT, 1_000);
    check('an ancestor with TWO footers names nobody (never a neighbour\'s name)', name, '');
    check('but both names are still learned for later elimination', getZoomRosterNames().sort(), ['Utpalendu Sarkar', 'sujoy sarkar']);
  }

  // ── Defect 2: gallery view / renamed classes ─────────────────────────────
  //
  // Each case keeps TWO unclaimed roster names on the page so elimination is
  // ambiguous and cannot resolve the track. Only the active-speaker layer can,
  // which is what isolates it.
  console.log('\nDefect 2 — the active speaker beyond the two hardcoded layouts:');
  const activeLayouts: Array<[string, string]> = [
    ['speaker view (pre-existing)', '.speaker-active-container__video-frame'],
    ['screen-share filmstrip (pre-existing)', '.speaker-bar-container__video-frame--active'],
    ['GALLERY view — the live failure', '.gallery-video-container__video-frame--active'],
    ['a renamed active class', '[class*="video-frame--active"]'],
    ['an active video-avatar', '[class*="video-avatar"][class*="--active"]'],
    ['a generic active-speaker container', '[class*="active-speaker"]'],
  ];
  for (const [label, selector] of activeLayouts) {
    reset();
    reportTrackAudio(1);
    const page = makePage([
      bareMedia(),
      bareMedia(), // index 1 — a bare element, so the tile layer cannot fire
      { sel: [selector], children: [footer('sujoy sarkar')] },
      { children: [footer('Third Person')] }, // keeps elimination ambiguous
    ]);
    const name = await resolveSpeakerName(page, 1, 'zoom', BOT, 1_000);
    check(`resolves via ${label}`, name, 'sujoy sarkar');
  }
  {
    reset();
    // The active-speaker layer is credited ONLY to the most recently active
    // track. Track 3 never reported audio, so it must not claim the highlight.
    const page = makePage([
      bareMedia(),
      bareMedia(),
      bareMedia(),
      bareMedia(), // index 3, never reported
      { sel: ['.speaker-active-container__video-frame'], children: [footer('sujoy sarkar')] },
      { children: [footer('Third Person')] },
    ]);
    const name = await resolveSpeakerName(page, 3, 'zoom', BOT, 1_000);
    check('a track that never emitted audio does not claim the active highlight', name, '');
  }

  // ── Defect 3: accumulation over time ─────────────────────────────────────
  //
  // The live sequence, in three polls. The third poll's DOM is EMPTY, so only the
  // accumulated union can name the second speaker — which is exactly the point.
  console.log('\nDefect 3 — elimination over the roster accumulated over time:');
  {
    reset();
    reportTrackAudio(0);

    // Poll 1 — both names visible, neither claimed ⇒ ambiguous ⇒ nobody named.
    const poll1 = makePage([
      bareMedia(),
      { children: [footer('Utpalendu Sarkar')] },
      { children: [footer('sujoy sarkar')] },
    ]);
    check('poll 1: two unclaimed names name nobody', await resolveSpeakerName(poll1, 0, 'zoom', BOT, 1_000), '');
    check('poll 1: both names are learned', getZoomRosterNames().sort(), ['Utpalendu Sarkar', 'sujoy sarkar']);

    // Poll 2 — one name is highlighted, so track 0 takes it.
    const poll2 = makePage([
      bareMedia(),
      { sel: ['.speaker-active-container__video-frame'], children: [footer('Utpalendu Sarkar')] },
    ]);
    check('poll 2: track 0 takes the highlighted name', await resolveSpeakerName(poll2, 0, 'zoom', BOT, 2_000), 'Utpalendu Sarkar');

    // Poll 3 — the DOM shows NOTHING. A point-in-time roster is empty here; only
    // the union knows "sujoy sarkar" still exists, and only elimination can tell
    // that it must be track 1.
    reportTrackAudio(1);
    const poll3 = makePage([bareMedia(), bareMedia()]);
    check(
      'poll 3: with an EMPTY DOM, the accumulated union still names track 1',
      await resolveSpeakerName(poll3, 1, 'zoom', BOT, 3_000),
      'sujoy sarkar',
    );
    // The property the user cares about: two humans, two names, not one.
    check(
      'the two tracks hold two DIFFERENT real names',
      await resolveSpeakerName(poll3, 0, 'zoom', BOT, 3_100),
      'Utpalendu Sarkar',
    );
  }
  {
    reset();
    reportTrackAudio(0);
    // Fail-safe: three unclaimed names is not an elimination.
    const page = makePage([
      bareMedia(),
      { children: [footer('Alice')] },
      { children: [footer('Bob')] },
      { children: [footer('Carol')] },
    ]);
    check('three unclaimed names name nobody', await resolveSpeakerName(page, 0, 'zoom', BOT, 1_000), '');
  }

  // ── The freshness bound, as a matched pair ────────────────────────────────
  //
  // Both halves are identical except for the observation time, so the pair proves
  // the bound is what decides — not something ambient.
  console.log('\nFreshness bound on the accumulated roster:');
  for (const stale of [false, true]) {
    reset();
    // Learn "Ghost One" at t=0 via a track that never reported audio, so it is
    // observed but never assigned.
    const seed = makePage([bareMedia(), bareMedia(), bareMedia(), bareMedia(), bareMedia(), { children: [footer('Ghost One')] }]);
    await resolveSpeakerName(seed, 4, 'zoom', BOT, 0);
    check(`${stale ? 'stale' : 'fresh'}: the name was learned at t=0`, getZoomRosterNames(), ['Ghost One']);

    reportTrackAudio(0);
    const later = stale ? ZOOM_ROSTER_FRESHNESS_MS + 1 : ZOOM_ROSTER_FRESHNESS_MS;
    const empty = makePage([bareMedia()]);
    const name = await resolveSpeakerName(empty, 0, 'zoom', BOT, later);
    check(
      stale
        ? 'a name last seen beyond the freshness bound is NOT eliminable (they may have left)'
        : 'a name last seen exactly at the bound IS still eliminable',
      name,
      stale ? '' : 'Ghost One',
    );
  }

  // ── Names that must never be assigned ────────────────────────────────────
  console.log('\nNames that must never be assigned:');
  {
    reset();
    reportTrackAudio(0);
    const page = makePage([{ children: [footer(BOT), bareMedia()] }]);
    check('the bot never resolves to itself', await resolveSpeakerName(page, 0, 'zoom', BOT, 1_000), '');
    check('and the bot name never enters the roster', getZoomRosterNames(), []);
  }
  {
    reset();
    reportTrackAudio(0);
    const page = makePage([{ children: [footer('Mute All'), bareMedia()] }]);
    check('a toolbar label is never a speaker', await resolveSpeakerName(page, 0, 'zoom', BOT, 1_000), '');
  }
  {
    reset();
    reportTrackAudio(0);
    const page = makePage([{ children: [footer('Alice\nBob'), bareMedia()] }]);
    check('concatenated multi-line tile text is never a speaker', await resolveSpeakerName(page, 0, 'zoom', BOT, 1_000), '');
  }

  // ── Observability: the 278 identical lines ───────────────────────────────
  console.log('\nObservability — a stuck failure must be countable, not deafening:');
  {
    reset();
    // 60 polls of the same hopeless DOM, at the live cadence (500ms apart).
    const page = makePage([bareMedia(), bareMedia(), bareMedia(), bareMedia(), bareMedia(), bareMedia(), bareMedia()]);
    const { lines } = await captureLogs(async () => {
      for (let i = 0; i < 60; i++) {
        await resolveSpeakerName(page, 6, 'zoom', BOT, i * 500);
      }
    });
    const misses = lines.filter((l) => l.includes('UNRESOLVED'));
    check('60 identical polls do not produce 60 identical lines', misses.length < 5, true);
    check('but the failure is reported at least once', misses.length >= 1, true);
    // The point of the rewrite: the line now says WHY, not just THAT.
    const first = misses[0] ?? '';
    check(
      'the report carries the DOM state that caused the miss',
      first.includes('media=7') && first.includes('footers=0') && first.includes('active=none'),
      true,
    );
    const generic = lines.filter((l) => l.includes('not yet mapped'));
    check('the content-free line is collapsed too', generic.length < 5, true);
  }
  {
    reset();
    // A miss whose CAUSE changes must speak up immediately, not wait out the
    // heartbeat — that moment is the diagnosis.
    reportTrackAudio(0); // so the elimination layer is actually entered
    const before = makePage([bareMedia()]);
    const after = makePage([bareMedia(), { children: [footer('Alice')] }, { children: [footer('Bob')] }]);
    const { lines } = await captureLogs(async () => {
      await resolveSpeakerName(before, 0, 'zoom', BOT, 0);
      await resolveSpeakerName(before, 0, 'zoom', BOT, 100);
      await resolveSpeakerName(after, 0, 'zoom', BOT, 200);
    });
    const misses = lines.filter((l) => l.includes('UNRESOLVED'));
    check('a changed DOM census emits a second report at once', misses.length, 2);
    check('the first report saw no footers', (misses[0] ?? '').includes('footers=0'), true);
    check('the first report attributes the miss to an empty roster, not to a skipped layer', (misses[0] ?? '').includes('elim=empty-roster'), true);
    const second = misses[1] ?? '';
    check('the second reports the changed cause', second.includes('footers=2') && second.includes('elim=ambiguous'), true);
  }

  // ── The walk-up must never bind a NEIGHBOUR's name ───────────────────────
  //
  // Gate finding, reproduced before it was fixed. The old rule "an ancestor with
  // exactly one footer is this tile" is sound in one direction only: when just
  // ONE footer exists on the whole page — the state measured live, where the
  // roster read `roster=[Utpalendu Sarkar]` twenty times — every ancestor up to
  // the app root contains exactly one. So the walk ran to the root and bound
  // whichever single name was rendered, at vote weight 1.0, and locked it.
  //
  // Note on fixture shape: the earlier cases in this file put the media element
  // one level under its OWN tile, so the walk always terminated on the right
  // ancestor and could never expose this. These cases deliberately put the media
  // element and the footer in DIFFERENT branches.
  console.log('\nWalk-up containment — a neighbour name must never be bound:');
  {
    reset();
    // NO reportTrackAudio: Layer Z2 needs no audio activity, while Z3 and Z4 both
    // require `isMostRecentlyActiveTrack`. Withholding it leaves Z2 as the only
    // layer that can resolve, which is what isolates it here. (Z4 would otherwise
    // name this track by elimination — a separate path, labelled LOW CONFIDENCE
    // whenever the roster cannot cover every talking track.)
    // Track 0's media is nested three levels down one branch of the app root; the
    // page's ONLY footer belongs to somebody else, in a sibling branch.
    const page = makePage([
      {
        sel: ['#root'],
        children: [nest(3, bareMedia()), tile([footer('sujoy sarkar')])],
      },
    ]);
    const name = await resolveSpeakerName(page, 0, 'zoom', BOT, 1_000);
    check('a sibling branch only-footer is NOT bound to this track', name, '');
    // The assertion above would also pass if some other string came back, so pin
    // the actual hazard by name.
    check('the neighbour name is not returned', name === 'sujoy sarkar', false);
  }
  {
    reset();
    // NO reportTrackAudio: Layer Z2 needs no audio activity, while Z3 and Z4 both
    // require `isMostRecentlyActiveTrack`. Withholding it leaves Z2 as the only
    // layer that can resolve, which is what isolates it here. (Z4 would otherwise
    // name this track by elimination — a separate path, labelled LOW CONFIDENCE
    // whenever the roster cannot cover every talking track.)
    // The app root is tile-SHAPED (a plausible class collision) and holds three
    // live media elements. Shape alone would pass; exclusivity must not.
    const page = makePage([
      tile([nest(2, bareMedia()), bareMedia(), bareMedia(), footer('sujoy sarkar')]),
    ]);
    const name = await resolveSpeakerName(page, 0, 'zoom', BOT, 1_000);
    check('a tile-shaped container holding THREE media elements binds nobody', name, '');
  }
  {
    reset();
    // NO reportTrackAudio: Layer Z2 needs no audio activity, while Z3 and Z4 both
    // require `isMostRecentlyActiveTrack`. Withholding it leaves Z2 as the only
    // layer that can resolve, which is what isolates it here. (Z4 would otherwise
    // name this track by elimination — a separate path, labelled LOW CONFIDENCE
    // whenever the roster cannot cover every talking track.)
    // A genuine tile far enough up to exceed the depth cap. Shape and exclusivity
    // both pass; only the cap stops it.
    const page = makePage([tile([nest(8, bareMedia()), footer('Deep Person')])]);
    const name = await resolveSpeakerName(page, 0, 'zoom', BOT, 1_000);
    check('a tile beyond the depth cap binds nobody', name, '');
  }
  {
    reset();
    // NO reportTrackAudio: Layer Z2 needs no audio activity, while Z3 and Z4 both
    // require `isMostRecentlyActiveTrack`. Withholding it leaves Z2 as the only
    // layer that can resolve, which is what isolates it here. (Z4 would otherwise
    // name this track by elimination — a separate path, labelled LOW CONFIDENCE
    // whenever the roster cannot cover every talking track.)
    // POSITIVE CONTROL for all three cases above: a genuine tile, exclusive media,
    // within the cap, DOES bind. Without this the guards could simply have
    // disabled Layer Z2 altogether and every case above would pass for the wrong
    // reason.
    const page = makePage([
      { sel: ['#root'], children: [tile([nest(2, bareMedia()), footer('sujoy sarkar')])] },
    ]);
    const name = await resolveSpeakerName(page, 0, 'zoom', BOT, 1_000);
    check('CONTROL: a real tile with exclusive media, within the cap, DOES bind', name, 'sujoy sarkar');
  }
  {
    reset();
    // NO reportTrackAudio: Layer Z2 needs no audio activity, while Z3 and Z4 both
    // require `isMostRecentlyActiveTrack`. Withholding it leaves Z2 as the only
    // layer that can resolve, which is what isolates it here. (Z4 would otherwise
    // name this track by elimination — a separate path, labelled LOW CONFIDENCE
    // whenever the roster cannot cover every talking track.)
    // A non-tile-shaped wrapper with exclusive media and one footer. Exclusivity
    // and cap both pass; only the shape guard refuses. Z4 may still name the track
    // by elimination, so this asserts Z2 specifically via the resolution layer.
    const page = makePage([wrap([nest(1, bareMedia()), footer('Shapeless Sam')])]);
    const { lines } = await captureLogs(async () =>
      resolveSpeakerName(page, 0, 'zoom', BOT, 1_000),
    );
    check(
      'a non-tile-shaped ancestor is refused by Layer Z2 (no zoom-tile resolution)',
      lines.some((l) => l.includes('layer=zoom-tile')),
      false,
    );
  }
  {
    reset();
    // Diagnostics: the report must name WHY the walk found nothing, so a live run
    // distinguishes "no tile above the media element" from "the tile is shared".
    const shared = makePage([tile([bareMedia(), bareMedia(), footer('Someone')])]);
    const { lines } = await captureLogs(async () => {
      await resolveSpeakerName(shared, 0, 'zoom', BOT, 1_000);
    });
    const miss = lines.filter((l) => l.includes('UNRESOLVED'))[0] ?? '';
    check('the miss report names the shared-container cause', miss.includes('tileReject=shared-container'), true);
    check('and reports no tile was matched', miss.includes('tileDepth=-1'), true);
  }
  {
    reset();
    const noTile = makePage([wrap([bareMedia(), footer('Someone')])]);
    const { lines } = await captureLogs(async () => {
      await resolveSpeakerName(noTile, 0, 'zoom', BOT, 1_000);
    });
    const miss = lines.filter((l) => l.includes('UNRESOLVED'))[0] ?? '';
    check('a missing tile ancestor is reported as reached-body, not a shared container', miss.includes('tileReject=reached-body'), true);
  }

  // ── Cameras off, and the honesty of a one-name roster ────────────────────
  //
  // The common case for our users is everybody's video OFF. Layer Z3's selectors
  // are all `…video-frame…` shaped and cannot fire then; Z2/Z4 read the AVATAR
  // tile's footer, which is what Zoom renders for a camera-off participant, and
  // which the failing live run proves was present. These cases pin that: no
  // active-speaker container anywhere, and resolution must still work.
  console.log('\nCameras off — no active-speaker container exists at all:');
  {
    reset();
    reportTrackAudio(0);
    reportTrackAudio(1);
    // Both names on avatar tiles, no video-frame container in the document.
    const poll1 = makePage([bareMedia(), bareMedia(), { children: [footer('Utpalendu Sarkar')] }]);
    // Track 0 is the only one whose name can be pinned first: one known name,
    // two talking tracks ⇒ assumed, not eliminated.
    const { result: first, lines } = await captureLogs(async () =>
      resolveSsolveHelper(poll1, 0),
    );
    check('a camera-off avatar footer still names a track', first, 'Utpalendu Sarkar');
    check(
      'and it is labelled LOW CONFIDENCE — 1 known name, 2 talking tracks',
      lines.some((l) => l.includes('LOW CONFIDENCE') && l.includes('1 known name(s) for 2 audio-active track(s)')),
      true,
    );

    // The second name appears on its own avatar tile a poll later.
    const poll2 = makePage([bareMedia(), bareMedia(), { children: [footer('sujoy sarkar')] }]);
    const { result: second, lines: lines2 } = await captureLogs(async () =>
      resolveSsolveHelper(poll2, 1),
    );
    check('the second camera-off participant resolves too', second, 'sujoy sarkar');
    // Now the roster covers every talking track, so this IS an elimination and
    // must NOT be labelled. This is the negative half of the pair: it proves the
    // label tracks the coverage check and is not simply always emitted.
    check(
      'a covered roster is a true elimination and carries NO low-confidence label',
      lines2.some((l) => l.includes('LOW CONFIDENCE')),
      false,
    );
    check(
      'two camera-off humans end up with two distinct real names',
      [first, second],
      ['Utpalendu Sarkar', 'sujoy sarkar'],
    );
  }
  {
    reset();
    // Teardown must drop audio telemetry, or every track from the previous
    // meeting still looks "recently active" and Layer Z4 trusts a dead track.
    reportTrackAudio(0);
    clearZoomRoster();
    const page = makePage([bareMedia(), { children: [footer('Alice')] }]);
    check(
      'after a teardown clear, a track with no fresh audio claims nothing',
      await resolveSpeakerName(page, 0, 'zoom', BOT, 1_000),
      '',
    );
  }

  {
    reset();
    // A track with no audio history skips the elimination layer entirely. That is
    // a different diagnosis from "the roster was empty", and the report must say
    // so — conflating them is what makes a log useless.
    const page = makePage([bareMedia(), { children: [footer('Alice')] }]);
    const { lines } = await captureLogs(async () => {
      await resolveSpeakerName(page, 0, 'zoom', BOT, 1_000);
    });
    const miss = lines.filter((l) => l.includes('UNRESOLVED'))[0] ?? '';
    check('a skipped elimination layer is reported as not-attempted', miss.includes('elim=not-attempted'), true);
    check('and it is not misreported as an empty roster', miss.includes('elim=empty-roster'), false);
    check('the report still names the roster it did learn', miss.includes('roster=1'), true);
  }

  // ── An assumption must never become permanent ────────────────────────────
  //
  // Gate finding (critical 2). `covered = knownNames >= audioTracks` was true in
  // the MOST COMMON case — early in a 2-person meeting only one track has spoken,
  // so one known name gave `1 >= 1` — and the bind was logged as a TRUE
  // elimination at full vote weight. A one-name/one-track reading carries ZERO
  // elimination information: it rests entirely on the unproven assumption that
  // the single footer Zoom renders belongs to the SPEAKING participant. With
  // LOCK_THRESHOLD = 2 that locked on poll two, and on Zoom a lock is forever.
  console.log('\nUncovered elimination — may claim, may never lock:');
  {
    reset();
    reportTrackAudio(0); // exactly ONE audio-active track
    const page = makePage([bareMedia(), { children: [footer('sujoy sarkar')] }]);
    const { result, lines } = await captureLogs(async () =>
      resolveSpeakerName(page, 0, 'zoom', BOT, 1_000),
    );
    check('a single-track reading still yields the name', result, 'sujoy sarkar');
    // THE assertion for this finding: 1 name / 1 track is NOT a true elimination.
    check(
      'a ONE-name ONE-track reading is labelled LOW CONFIDENCE, not a true elimination',
      lines.some((l) => l.includes('LOW CONFIDENCE') && l.includes('1 known name(s) for 1 audio-active track(s)')),
      true,
    );
    check(
      'and it is NOT logged as layer=zoom-elimination',
      lines.some((l) => l.includes('layer=zoom-elimination')),
      false,
    );
    check('a single uncovered reading does not lock', getLockedMapping(0), null);
  }
  {
    reset();
    reportTrackAudio(0);
    // Ten consecutive uncovered polls. A repeated 0.5 vote WOULD reach
    // LOCK_THRESHOLD on the fourth (measured against recordTrackVote directly), so
    // this is the case that proves the claim is cast once and capped, rather than
    // merely de-weighted.
    const page = makePage([bareMedia(), { children: [footer('sujoy sarkar')] }]);
    const { lines } = await captureLogs(async () => {
      for (let poll = 0; poll < 10; poll++) {
        await resolveSpeakerName(page, 0, 'zoom', BOT, 1_000 + poll);
      }
    });
    check('TEN uncovered polls still do not lock', getLockedMapping(0), null);
    check(
      'the LOW CONFIDENCE line is emitted once, not ten times',
      lines.filter((l) => l.includes('LOW CONFIDENCE')).length,
      1,
    );
  }
  {
    reset();
    reportTrackAudio(0);
    // An assumed claim must stay DISPLACEABLE: a later confident signal for a
    // different name must be able to outvote it and lock. That is the whole
    // argument for claiming at all instead of refusing.
    const assumed = makePage([bareMedia(), { children: [footer('Wrong Guess')] }]);
    await resolveSpeakerName(assumed, 0, 'zoom', BOT, 1_000);
    check('precondition: the assumed name is claimed but unlocked', getLockedMapping(0), null);

    // Two confident tile reads for the real name (Layer Z2, full weight).
    const real = makePage([tile([bareMedia(), footer('Real Owner')])]);
    await resolveSpeakerName(real, 0, 'zoom', BOT, 2_000);
    const finalName = await resolveSpeakerName(real, 0, 'zoom', BOT, 3_000);
    check('a confident signal outvotes the assumption and locks the real name', getLockedMapping(0), 'Real Owner');
    check('and that is what resolution returns', finalName, 'Real Owner');
  }
  {
    reset();
    // NO DEADLOCK: two tracks, two known names, nothing claimed. Hard refusal
    // would leave every elimination ambiguous forever and nobody would ever get a
    // real name — this is why the uncovered path claims rather than refuses.
    reportTrackAudio(0);
    reportTrackAudio(1);
    const both = makePage([
      bareMedia(),
      bareMedia(),
      { children: [footer('Utpalendu Sarkar')] },
      { children: [footer('sujoy sarkar')] },
    ]);
    // Track 0 bootstraps with an assumed claim (2 names, 2 tracks, but both
    // unclaimed ⇒ ambiguous), so nothing binds yet.
    check('poll 1: both names unclaimed ⇒ ambiguous ⇒ nobody named', await resolveSpeakerName(both, 0, 'zoom', BOT, 1_000), '');
    // Give track 0 its name from its own tile so a claim exists.
    const t0 = makePage([tile([bareMedia(), footer('Utpalendu Sarkar')]), bareMedia()]);
    check('track 0 takes its own tile name', await resolveSpeakerName(t0, 0, 'zoom', BOT, 2_000), 'Utpalendu Sarkar');
    // Now track 1 has a genuine, COVERED elimination: 2 known names, 2 tracks,
    // one claimed.
    const { result: t1, lines } = await captureLogs(async () =>
      resolveSpeakerName(makePage([bareMedia(), bareMedia()]), 1, 'zoom', BOT, 3_000),
    );
    check('track 1 is named by a genuine elimination', t1, 'sujoy sarkar');
    check(
      'and it is NOT labelled LOW CONFIDENCE, because it is covered',
      lines.some((l) => l.includes('LOW CONFIDENCE')),
      false,
    );
    check('two humans, two distinct real names', [await resolveSpeakerName(makePage([bareMedia(), bareMedia()]), 0, 'zoom', BOT, 3_100), t1], ['Utpalendu Sarkar', 'sujoy sarkar']);
  }
  {
    reset();
    reportTrackAudio(0);
    // An assumed claim must be RE-CAST after a cache clear. `clearSpeakerNameCache`
    // wipes the votes, so if the claim-once bookkeeping survived it, the claim
    // could never be re-established: `isNameTaken` would report the name as free,
    // a second track could take it too, and Layer Z4 would deadlock afterwards.
    const page = makePage([bareMedia(), bareMedia(), { children: [footer('sujoy sarkar')] }]);
    const first = await captureLogs(async () => resolveSpeakerName(page, 0, 'zoom', BOT, 1_000));
    check('precondition: the claim is cast and the name is taken', isNameTaken('sujoy sarkar', 1), true);
    check('precondition: LOW CONFIDENCE was logged', first.lines.filter((l) => l.includes('LOW CONFIDENCE')).length, 1);

    clearSpeakerNameCache(); // wipes votes — the claim is gone with them
    check('after the clear the name is free again', isNameTaken('sujoy sarkar', 1), false);

    const again = await captureLogs(async () => resolveSpeakerName(page, 0, 'zoom', BOT, 2_000));
    check('the claim is RE-CAST after a cache clear', isNameTaken('sujoy sarkar', 1), true);
    check('and it is re-announced, not silently skipped', again.lines.filter((l) => l.includes('LOW CONFIDENCE')).length, 1);
    check('still without locking', getLockedMapping(0), null);
  }
  {
    reset();
    // A wrong Layer Z2 bind is invisible unless the resolution log says how far up
    // the walk went, so the depth must be in the line, not only in the miss census.
    const page = makePage([
      { sel: ['#root'], children: [tile([nest(2, bareMedia()), footer('sujoy sarkar')])] },
    ]);
    const { result, lines } = await captureLogs(async () =>
      resolveSpeakerName(page, 0, 'zoom', BOT, 1_000),
    );
    check('precondition: Layer Z2 resolved it', result, 'sujoy sarkar');
    const line = lines.filter((l) => l.includes('layer=zoom-tile')).join(' ');
    check('the zoom-tile resolution log reports the walk depth', /layer=zoom-tile\(depth=\d+\)/.test(line), true);
    // And a real number, not a placeholder: the media element is nested 2 wrappers
    // below the tile, so the tile is 3 ancestors up.
    check('the reported depth is the real one, not a constant', /depth=3\)/.test(line), true);
  }

  // ── More audio elements than humans ──────────────────────────────────────
  //
  // The live run had THREE live audio elements for TWO humans, so at least one
  // element had no participant behind it. That matters specifically for
  // elimination: a non-human element (Zoom's mixed remote audio is the leading
  // candidate) is active whenever ANYONE speaks, so it is very often the
  // most-recently-active track — systematically favoured by the one input Layer Z4
  // trusts. If it could take the sole unclaimed name, it would DENY that name to
  // the human who owns it, permanently.
  //
  // The coverage check added for the uncovered-elimination finding already closes
  // this, and these cases pin that it does: more audio-active tracks than known
  // names can never be "covered", so nothing locks.
  console.log('\nMore audio elements than known names — nothing may lock:');
  {
    reset();
    // Three audio-active tracks, two known names — the live shape exactly.
    reportTrackAudio(0);
    reportTrackAudio(1);
    reportTrackAudio(2);
    const page = makePage([
      bareMedia(),
      bareMedia(),
      bareMedia(),
      { children: [footer('Utpalendu Sarkar')] },
      { children: [footer('sujoy sarkar')] },
    ]);
    // Give track 0 a real claim so exactly one name is unclaimed and elimination
    // would otherwise fire.
    const claimed = makePage([tile([bareMedia(), footer('Utpalendu Sarkar')]), bareMedia(), bareMedia()]);
    await resolveSpeakerName(claimed, 0, 'zoom', BOT, 1_000);
    await resolveSpeakerName(claimed, 0, 'zoom', BOT, 1_100);
    check('precondition: track 0 is locked to its own tile name', getLockedMapping(0), 'Utpalendu Sarkar');

    const { result, lines } = await captureLogs(async () =>
      resolveSpeakerName(page, 2, 'zoom', BOT, 2_000),
    );
    check('the sole unclaimed name is still offered', result, 'sujoy sarkar');
    // The load-bearing assertion: with 3 audio-active tracks and only 2 known
    // names, this is NOT a covered elimination, so it may never lock.
    check(
      'with more audio-active tracks than known names it is LOW CONFIDENCE',
      lines.some((l) => l.includes('LOW CONFIDENCE') && l.includes('2 known name(s) for 3 audio-active track(s)')),
      true,
    );
    check('and it does not lock, so a real human can still outvote it', getLockedMapping(2), null);
  }
  {
    reset();
    // The matched control: SAME two names, but only two audio-active tracks. Now
    // the roster covers every track, so it IS a covered elimination and locks.
    // Without this control the case above could pass because nothing ever locks.
    reportTrackAudio(0);
    reportTrackAudio(1);
    const claimed = makePage([tile([bareMedia(), footer('Utpalendu Sarkar')]), bareMedia()]);
    await resolveSpeakerName(claimed, 0, 'zoom', BOT, 1_000);
    await resolveSpeakerName(claimed, 0, 'zoom', BOT, 1_100);
    const page = makePage([bareMedia(), bareMedia(), { children: [footer('sujoy sarkar')] }]);
    const { lines } = await captureLogs(async () => {
      await resolveSpeakerName(page, 1, 'zoom', BOT, 2_000);
      await resolveSpeakerName(page, 1, 'zoom', BOT, 2_100);
    });
    check('CONTROL: 2 names for 2 tracks IS covered, and locks', getLockedMapping(1), 'sujoy sarkar');
    check('CONTROL: and carries no LOW CONFIDENCE label', lines.some((l) => l.includes('LOW CONFIDENCE')), false);
  }

  {
    reset();
    reportTrackAudio(0);
    // N2: `invalidateSpeakerName` deletes the track's votes and lock, so its claim
    // is released — but the once-per-track+name record of the assumed claim must
    // go with them. Left behind, the claim could never be re-cast and Layer Z4
    // would be permanently dead for this track: the same deadlock
    // `clearSpeakerNameCache` already guards against, in its sibling function.
    const page = makePage([bareMedia(), bareMedia(), { children: [footer('sujoy sarkar')] }]);
    const first = await captureLogs(async () => resolveSpeakerName(page, 0, 'zoom', BOT, 1_000));
    check('precondition: the claim is cast and the name is taken', isNameTaken('sujoy sarkar', 1), true);
    check('precondition: LOW CONFIDENCE was logged once', first.lines.filter((l) => l.includes('LOW CONFIDENCE')).length, 1);

    invalidateSpeakerName('zoom', 0);
    check('after invalidation the name is free again', isNameTaken('sujoy sarkar', 1), false);

    const again = await captureLogs(async () => resolveSpeakerName(page, 0, 'zoom', BOT, 2_000));
    check('the claim is RE-CAST after invalidateSpeakerName', isNameTaken('sujoy sarkar', 1), true);
    check('and it is re-announced, not silently skipped', again.lines.filter((l) => l.includes('LOW CONFIDENCE')).length, 1);
    check('still without locking', getLockedMapping(0), null);
  }
  {
    reset();
    reportTrackAudio(0);
    reportTrackAudio(1);
    // The prefix scan must be track-scoped, and the direction that actually breaks
    // is index 1 vs index 11: with the trailing colon dropped from the pattern,
    // `'11:Name'.startsWith('1')` is TRUE, so invalidating track 1 would silently
    // wipe track ELEVEN's claim RECORD. (The mirror direction cannot break —
    // `'1:Name'.startsWith('11')` is false either way.)
    //
    // The observable is the RECORD, not the vote: wiping the record does not
    // release the vote, it lets the assumed claim be CAST AGAIN. Repeated, that
    // accumulates 0.5 at a time toward LOCK_THRESHOLD — the exact permanence the
    // claim-once cap exists to prevent. So the assertion is that track 11 does not
    // re-announce, i.e. its record survived. (An earlier version of this test
    // asserted on `isNameTaken` and was vacuous: the mutation left it green.)
    const media = Array.from({ length: 12 }, () => bareMedia());
    const page = makePage([...media, { children: [footer('Track Eleven Person')] }]);
    reportTrackAudio(11);
    const firstClaim = await captureLogs(async () => resolveSpeakerName(page, 11, 'zoom', BOT, 1_000));
    check('precondition: track 11 claims once', firstClaim.lines.filter((l) => l.includes('LOW CONFIDENCE')).length, 1);

    invalidateSpeakerName('zoom', 1); // a shorter index that is a string prefix of 11
    const afterOther = await captureLogs(async () => resolveSpeakerName(page, 11, 'zoom', BOT, 2_000));
    check(
      'invalidating track 1 leaves track 11\'s claim record intact (no re-claim)',
      afterOther.lines.filter((l) => l.includes('LOW CONFIDENCE')).length,
      0,
    );

    invalidateSpeakerName('zoom', 11); // its own index
    const afterSelf = await captureLogs(async () => resolveSpeakerName(page, 11, 'zoom', BOT, 3_000));
    check(
      'invalidating track 11 DOES clear its own record, so it re-claims',
      afterSelf.lines.filter((l) => l.includes('LOW CONFIDENCE')).length,
      1,
    );
  }

  // ── Other platforms are untouched ────────────────────────────────────────
  console.log('\nOther platforms are untouched:');
  {
    reset();
    const name = await resolveSpeakerName(makePage([bareMedia()]), 0, 'nosuchplatform', BOT, 1_000);
    check('an unknown platform still returns empty', name, '');
  }

  console.log(`\n=== ${passed} passed, ${failed} failed ===\n`);
  if (failed > 0) process.exit(1);
}

run().catch((err) => {
  console.error(err);
  process.exit(1);
});
