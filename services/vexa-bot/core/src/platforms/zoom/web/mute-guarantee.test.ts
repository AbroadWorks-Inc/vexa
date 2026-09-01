/**
 * Zoom recorder-bot mute guarantee — unit tests for both layers.
 *
 * Run: npx tsx services/vexa-bot/core/src/platforms/zoom/web/mute-guarantee.test.ts
 *
 * Layer 1 (readZoomMicState / selectZoomMicToggle): vocabulary-tolerant reading
 * of the in-meeting mic control. Layer 2 (silenceOutboundAudioTracks): the
 * DOM-independent, track-level silence guarantee.
 *
 * The FIRST fixture below is the real live DOM value from 2026-09-01
 * (aria-label="audio"). The shipped code compared aria-label with exact string
 * equality against 'Mute'/'Unmute', so a suite that only exercised those two
 * spellings would have been fully green while the bot sat unmuted in front of
 * participants for an entire meeting. That case is why this file exists.
 *
 * Matches alone-timer.test.ts / pcm-chunker.test.ts's standalone-harness
 * convention (no vitest).
 */

/**
 * MUTATION MANIFEST — every behaviour below was verified to FAIL when broken.
 * Reproduce one by making the edit in prepare.ts (or selectors.ts) and re-running
 * this file; the named test must go RED. Tests tagged [Mnn] map to an id here.
 *
 *  M1  prepare.ts readZoomMicState: `label.includes('unmute')` -> `label === 'unmute'`
 *      (and the same for 'mute')  === THE ORIGINALLY SHIPPED BUG
 *      red: '"unmute my microphone" => muted' (+3 more)
 *  M2  readZoomMicState: swap the two blocks so `includes('mute')` is tested first
 *      red: '"Unmute" => muted (substring order: unmute tested before mute)' (+4)
 *  M3  selectors.ts: swap zoomMicUnmutedClassHints / zoomMicMutedClassHints test order
 *      red: 'icon class "SvgAudioUnmuted" => unmuted' (+2)
 *  M4  readZoomMicState: add `if (probe.ariaPressed === 'false') return unmuted`
 *      red: 'aria-pressed="false" => unknown, NEVER unmuted'
 *  M5  readZoomMicState: non-toggle branch returns `unmuted` instead of `not-mute-toggle`
 *      red: 'LIVE FIXTURE aria-label="audio" reads not-mute-toggle' (+8)
 *  M6  selectZoomMicToggle: also accept `not-mute-toggle` as selectable
 *      red: 'LIVE FIXTURE aria-label="audio" is not selected as a toggle' (+4)
 *  M7  normaliseZoomMicText: drop `.toLowerCase()`            red: 15 tests
 *  M8  silenceOutboundAudioTracks: iterate getReceivers() instead of getSenders()
 *      red: 'INCOMING audio track is never touched'
 *  M9  silenceOutboundAudioTracks: remove the voiceAgentEnabled early return
 *      red: 'voiceAgentEnabled => outbound audio stays ENABLED' (+2)
 *  M10 silenceOutboundAudioTracks: drop `track.kind !== 'audio'` guard
 *      red: 'outbound VIDEO track is left alone' (+1)
 *  M11 silenceOutboundAudioTracks: add `break` to the per-peer catch
 *      red: 'a throwing peer does not stop later peers' (+1)
 *  M12 silenceOutboundAudioTracks: remove the `connectionState === 'closed'` skip
 *      red: 'closed peer connection is skipped'
 *  M13 silenceOutboundAudioTracks: set registryPresent = true before the Array.isArray check
 *      red: 'absent registry => registryPresent false' (+2)
 *  M14 silenceOutboundAudioTracks: delete `track.enabled = false`, keep the counter
 *      red: 'GUARANTEE: outbound AUDIO track is disabled' (+2)
 *  M15 readZoomMicState: move the aria-pressed check below the class hints
 *      red: 'aria-pressed="true" outranks a contradicting unmuted class hint'
 *  M16 readZoomMicState: move the non-toggle label check back above aria-pressed
 *      red: 'LIVE SHAPE aria-label="audio" + aria-pressed="true" => muted' (+2)
 *  M17 lockTrack: delete `track.enabled = false` so defineProperty runs FIRST
 *      red: '[M17] REAL audio state is stopped' (+8)   <- the ordering trap
 *  M18 lockTrack: delete the whole Object.defineProperty(...) seal
 *      red: '[M18] GUARANTEE: setting enabled=true does NOT unmute' (+7)
 *  M19 installOutboundAudioLockInPage: delete `lockTrack(args[0])` in the addTrack patch
 *      red: '[M19] ZERO-WINDOW: track added via addTrack is disabled' (+2)
 *  M20 replaceTrack patch: call orig BEFORE lockTrack
 *      red: '[M20] it was already disabled BEFORE the original replaceTrack ran'
 *  M21 installOutboundAudioLockInPage: remove the voiceAgentEnabled early return
 *      red: '[M21] voice agent: outbound audio stays ENABLED' (+3)
 *  M22 stepZoomMuteWatcher: `if (reading !== 'unmuted')` -> `if (reading === 'muted')`
 *      red: "[M22] 'unknown' => never clicks" (+2)
 *  M23 stepZoomMuteWatcher: disable the `consecutiveUnmuted < confirmations` guard
 *      red: '[M23] 1st unmuted reading => no click yet' (+2)
 *  M24 stepZoomMuteWatcher: disable the cooldown guard
 *      red: '[M24] second click refused inside the cooldown window' (+3)
 *  M25 getUserMedia patch: do not lockTrack the returned audio tracks
 *      red: '[M25] ZERO-WINDOW: mic track from getUserMedia is disabled'
 *  M26 readZoomMicState: delete the zoomNonMicLabelSubstrings guard entirely
 *      red: '[M26] "Ask to unmute" => not-mic-control' (+10)
 *  M27 readZoomMicState: move that guard BELOW the mute branches (W3's defect)
 *      red: '[M26] "Ask to unmute" => not-mic-control' (+10)
 *  M28 selectors.ts: replace the footer-scoped mute selectors with page-wide ones
 *      red: '[M28] every mute-vocabulary selector is scoped to the footer/toolbar'
 *  M28b selectors.ts: reintroduce a comma-list selector
 *      red: '[M28] no selector contains a comma list' (+2)
 *  M29 lockTrack: drop `state.errors++` from the catch
 *      red: '[M29] the sealing failure is counted (drives the guard re-sweep)'
 *  M30 startZoomMuteWatcher: delete `await revealZoomFooter(page)` (W4's defect —
 *      made the watcher permanently inert with the toolbar auto-hidden)
 *      red: '[M30] the watcher reveals the footer before probing'
 *  M30b startZoomMuteWatcher: delete the no-candidate log line
 *      red: '[M30] and it is LOGGED — an invisible no-op is the worst outcome'
 *
 *  M32 lockTrack: restore a WeakSet short-circuit in place of isSealed()
 *      (BLOCKER 1 — a de-sealed track was never re-sealed)
 *      red: '[M32] step 4: the re-install RE-SEALS and stops the audio again' (+2)
 *  M32b lockTrack: drop the `__vexaAudioLockGetter` marker on the getter
 *      red: '[M32] an intact seal is VERIFIED, not re-sealed' (+1)
 *  M34 installOutboundAudioLockInPage: remove the RTCPeerConnection ctor patch
 *      red: '[M34] the RTCPeerConnection constructor is patched' (+2)
 *  M34b ctor patch: write Meet's `__vexa_peer_connections` instead of the Zoom-local name
 *      red: '[M34] a ZOOM-LOCAL registry is created'
 *  M35 remove the addTransceiver patch
 *      red: '[M35] a track attached via addTransceiver is sealed at birth' (+1)
 *  M36 join.ts: delete the pre-navigation addInitScript arm (BLOCKER 2's timing)
 *      red: '[M36] join.ts arms the lock via addInitScript' (+1)
 *
 *  M37 parseZoomAudioLockEnv: flip the default to OFF (would silently ship an
 *      unprotected bot)   red: '[M37] DEFAULT: unset => seal ENABLED' (+6)
 *  M37b drop .toLowerCase() so opt-outs become case-sensitive
 *      red: '[M37] "OFF" => seal DISABLED' (+2)
 *  M37c remove 'disabled' from the opt-out list
 *      red: '[M37] "disabled" => seal DISABLED'
 *  M38 lockTrack: delete `if (!sealEnabled) return;` (seal applied even when OFF)
 *      red: '[M38] HONEST LIMIT: with the switch off an unmute CAN succeed'
 *  M38b delete the kill-switch early return before the at-birth patches
 *      red: '[M38] addTrack is NOT patched' (+1)
 *  M38c switch OFF also skips `enabled = false` (bot would be LIVE)
 *      red: '[M38] the track is STILL disabled (enabled=false is retained)'
 *  M39 make the voiceAgent bypass depend on the switch
 *      red: '[M21] voice agent: outbound audio stays ENABLED' (+5)
 *  M40 stop reporting seal-OFF mode in describeOutboundAudioLock
 *      red: '[M40] seal OFF: the description says so explicitly' (+1)
 *  M41 dead-code the decision log (`void 0 && log(...)`)
 *      red: '[M41] the decision is LOGGED at arm time (a real statement...)'
 *      NOTE: this SURVIVED a weaker `includes('log(')` guard; the assertion is
 *      now statement-position. Source guards are weak against clever mutations.
 *  M41b join.ts: hardcode `sealEnabled: true` instead of threading the switch
 *      red: '[M41] join.ts threads the switch into the page-load arm'
 *
 *  M42 silenceOutboundAudioTracks: read only `__vexa_peer_connections` again
 *      (the QA nit — the fallback sweep found nothing in the default config)
 *      red: '[M42] the ZOOM-LOCAL registry alone is enough for the sweep' (+4)
 *  M42b remove the `!peers.includes(peer)` dedup
 *      red: '[M42] a peer in both registries is counted ONCE' (+1)
 *  M43 join.ts preview: revert to `probe.ariaLabel === 'Mute'`
 *      red: '[M43] it clicks ONLY on a confident unmuted reading'
 *  M43b join.ts preview: click on a MUTED reading too (toggle hazard)
 *      red: '[M43] and explains why a muted reading is not clicked'
 *
 * Last run: 33 standing + 6 blocker + 10 kill-switch + 4 late = 53 attempted,
 *           53 caught, 0 survived, 0 stale anchors.
 *
 * NOTE on M30/M30b: startZoomMuteWatcher's body is Playwright + setInterval and
 * cannot be executed by a unit test, so those two are guarded by SOURCE
 * assertions (tests 54-55). That is weaker than a behavioural test and is not
 * presented as equivalent — the live-run checks in the report cover the rest.
 */

import {
  readZoomMicState,
  selectZoomMicToggle,
  describeZoomMicCandidates,
  normaliseZoomMicText,
  silenceOutboundAudioTracks,
  describeOutboundAudioSweep,
  ZoomMicProbe,
  ZoomMicCandidate,
  installOutboundAudioLockInPage,
  describeOutboundAudioLock,
  parseZoomAudioLockEnv,
  stepZoomMuteWatcher,
  zoomMuteWatcherInitialState,
} from './prepare';
import { zoomMicToggleSelectors, zoomNonMicLabelSubstrings } from './selectors';
import { readFileSync } from 'node:fs';

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

/** Build a probe; every field defaults to "absent" so each test varies exactly one thing. */
const probe = (p: Partial<ZoomMicProbe> = {}): ZoomMicProbe => ({
  ariaLabel: p.ariaLabel ?? null,
  ariaPressed: p.ariaPressed ?? null,
  className: p.className ?? null,
  descendantClassNames: p.descendantClassNames ?? [],
});

const candidate = (selector: string, index: number, p: Partial<ZoomMicProbe>): ZoomMicCandidate => ({
  selector,
  index,
  probe: probe(p),
});

console.log('\n=== LAYER 1: readZoomMicState — the live regression fixture ===');

// 1. THE BUG. Live DOM 2026-09-01: button.join-audio-container__btn had
//    aria-label="audio". Exact equality against 'Mute'/'Unmute' matched nothing.
{
  const r = readZoomMicState(probe({ ariaLabel: 'audio', className: 'footer-button-base__button ax-outline join-audio-container__btn' }));
  assertEqual(r.kind, 'not-mute-toggle', 'LIVE FIXTURE aria-label="audio" reads not-mute-toggle (never "unmuted")');
}

// 2. And critically: it must NOT be selected, so the bot does not click a
//    control it has not identified (clicking blind can UNMUTE it).
{
  const sel = selectZoomMicToggle([candidate('button.join-audio-container__btn', 0, { ariaLabel: 'audio' })]);
  assertEqual(sel, null, 'LIVE FIXTURE aria-label="audio" is not selected as a toggle');
}

console.log('\n=== LAYER 1: mute/unmute vocabulary + the substring trap ===');

// 3. "Unmute" is the OFFERED ACTION -> currently muted. Note "Unmute" contains
//    "mute": a naive substring test in the wrong order inverts this.
assertEqual(readZoomMicState(probe({ ariaLabel: 'Unmute' })).kind, 'muted', '"Unmute" => muted (substring order: unmute tested before mute)');
assertEqual(readZoomMicState(probe({ ariaLabel: 'Mute' })).kind, 'unmuted', '"Mute" => unmuted');

// 4. Vocabulary variants Zoom has shipped / may ship.
assertEqual(readZoomMicState(probe({ ariaLabel: 'unmute my microphone' })).kind, 'muted', '"unmute my microphone" => muted');
assertEqual(readZoomMicState(probe({ ariaLabel: 'mute my microphone' })).kind, 'unmuted', '"mute my microphone" => unmuted');
assertEqual(readZoomMicState(probe({ ariaLabel: '  UNMUTE  ' })).kind, 'muted', 'case + whitespace tolerant: "  UNMUTE  " => muted');
assertEqual(readZoomMicState(probe({ ariaLabel: 'Mute\n  (Alt+A)' })).kind, 'unmuted', 'collapses newlines: "Mute (Alt+A)" => unmuted');

// 5. Non-state audio vocabulary.
assertEqual(readZoomMicState(probe({ ariaLabel: 'Join Audio' })).kind, 'not-mute-toggle', '"Join Audio" => not-mute-toggle');
assertEqual(readZoomMicState(probe({ ariaLabel: 'Audio Settings' })).kind, 'not-mute-toggle', '"Audio Settings" => not-mute-toggle');

// 6. 'audio' is an EXACT-match-only non-toggle word — as a SUBSTRING it must not
//    override real mute vocabulary.
assertEqual(readZoomMicState(probe({ ariaLabel: 'unmute my audio' })).kind, 'muted', '"unmute my audio" => muted (the word "audio" does not win)');

// 7. No label at all, no other evidence.
assertEqual(readZoomMicState(probe({})).kind, 'unknown', 'no attributes at all => unknown');
assertEqual(readZoomMicState(probe({ ariaLabel: 'Reactions' })).kind, 'not-mute-toggle', 'unrelated label => not-mute-toggle');

console.log('\n=== LAYER 1: class-hint fallback (unmuted-before-muted trap) ===');

// 8. Every unmuted hint contains the substring 'muted' ('svgaudiounmuted', and
//    the bare-word 'unmuted' fallback). Checking the muted list first inverts
//    the reading and the bot never mutes itself. Load-bearing per mutation M3.
assertEqual(
  readZoomMicState(probe({ ariaLabel: null, descendantClassNames: ['zm-icon SvgAudioUnmuted'] })).kind,
  'unmuted',
  'icon class "SvgAudioUnmuted" => unmuted (NOT muted — it contains "muted")',
);
assertEqual(
  readZoomMicState(probe({ ariaLabel: null, descendantClassNames: ['zm-icon SvgAudioMuted'] })).kind,
  'muted',
  'icon class "SvgAudioMuted" => muted',
);
assertEqual(
  readZoomMicState(probe({ className: 'btn is-muted' })).kind,
  'muted',
  'button class "is-muted" => muted',
);
// 8b. The bare-word fallback: an unseen future Zoom class name still resolves.
assertEqual(
  readZoomMicState(probe({ descendantClassNames: ['zm-btn__icon--unmuted-state'] })).kind,
  'unmuted',
  'unseen class "...--unmuted-state" => unmuted via the bare-word fallback',
);
assertEqual(
  readZoomMicState(probe({ descendantClassNames: ['zm-btn__icon--muted-state'] })).kind,
  'muted',
  'unseen class "...--muted-state" => muted via the bare-word fallback',
);
// 9. A label with mute vocabulary must OUTRANK a contradicting class hint.
assertEqual(
  readZoomMicState(probe({ ariaLabel: 'Unmute', descendantClassNames: ['SvgAudioUnmuted'] })).kind,
  'muted',
  'aria-label outranks a contradicting class hint',
);

console.log('\n=== LAYER 1: precedence — the live "audio" element is now READABLE ===');

// 7b. THE POINT OF THE REORDER. The old code classified aria-label="audio" as
//     non-state and returned IMMEDIATELY, so aria-pressed and the class hints
//     were never consulted. An uninformative LABEL is not an uninformative
//     ELEMENT. These four cases are the live DOM shape plus one extra signal.
assertEqual(
  readZoomMicState(probe({ ariaLabel: 'audio', ariaPressed: 'true' })).kind,
  'muted',
  'LIVE SHAPE aria-label="audio" + aria-pressed="true" => muted (was: written off as non-state)',
);
assertEqual(
  readZoomMicState(probe({ ariaLabel: 'audio', descendantClassNames: ['SvgAudioMuted'] })).kind,
  'muted',
  'LIVE SHAPE aria-label="audio" + muted icon class => muted',
);
assertEqual(
  readZoomMicState(probe({ ariaLabel: 'audio', descendantClassNames: ['SvgAudioUnmuted'] })).kind,
  'unmuted',
  'LIVE SHAPE aria-label="audio" + unmuted icon class => unmuted (now clickable)',
);
// ...but a weak/absent signal still must NOT make it clickable.
assertEqual(
  readZoomMicState(probe({ ariaLabel: 'audio', ariaPressed: 'false' })).kind,
  'not-mute-toggle',
  'aria-label="audio" + aria-pressed="false" => still not-mute-toggle (no click)',
);

// 7c. aria-pressed outranks a CONTRADICTING class hint (it is real ARIA state,
//     the class names are guesses). Locks the new precedence.
assertEqual(
  readZoomMicState(probe({ ariaPressed: 'true', descendantClassNames: ['SvgAudioUnmuted'] })).kind,
  'muted',
  'aria-pressed="true" outranks a contradicting unmuted class hint',
);
// 7d. ...but mute VOCABULARY still outranks aria-pressed: it needs no polarity
//     assumption, so it stays the strongest signal.
assertEqual(
  readZoomMicState(probe({ ariaLabel: 'Mute', ariaPressed: 'true' })).kind,
  'unmuted',
  'aria-label "Mute" outranks aria-pressed="true" (label needs no polarity assumption)',
);

console.log('\n=== LAYER 1: aria-pressed is deliberately asymmetric ===');

// 10. Polarity of aria-pressed on Zoom's toggle is unconfirmed. A wrong
//     "unmuted" read causes a click that UNMUTES the bot; a wrong "muted" read
//     only leaves layer 2 in charge. So pressed=true may conclude muted, but
//     pressed=false must NOT conclude unmuted.
assertEqual(readZoomMicState(probe({ ariaPressed: 'true' })).kind, 'muted', 'aria-pressed="true" => muted');
assertEqual(readZoomMicState(probe({ ariaPressed: 'false' })).kind, 'unknown', 'aria-pressed="false" => unknown, NEVER unmuted (would trigger a click)');

console.log('\n=== LAYER 1: selectZoomMicToggle picks the first CONFIDENT candidate ===');

// 11. The real shape of the live failure: several matches, the first unreadable.
{
  const sel = selectZoomMicToggle([
    candidate('footer button[aria-label*="audio" i]', 0, { ariaLabel: 'audio' }),
    candidate('button[aria-label*="mute" i]', 2, { ariaLabel: 'Mute' }),
  ]);
  assertEqual(sel?.candidate.selector, 'button[aria-label*="mute" i]', 'skips the unreadable candidate and selects the readable one');
  assertEqual(sel?.candidate.index, 2, 'preserves nth index so the same element is clicked');
  assertEqual(sel?.reading.kind, 'unmuted', 'reports the reading that justified selection');
}
assertEqual(selectZoomMicToggle([]), null, 'no candidates => null');
{
  const sel = selectZoomMicToggle([candidate('a', 0, { ariaLabel: 'audio' }), candidate('b', 0, {})]);
  assertEqual(sel, null, 'all candidates unreadable => null (no click)');
}
// 12. Diagnostic digest must name every candidate — the missing diagnostic in the live run.
{
  const d = describeZoomMicCandidates([
    candidate('sel-a', 0, { ariaLabel: 'audio' }),
    candidate('sel-b', 1, { ariaLabel: 'Mute' }),
  ]);
  assertEqual(d.includes('sel-a[0]') && d.includes('audio') && d.includes('not-mute-toggle'), true, 'digest names the unreadable candidate and its reading');
  assertEqual(d.includes('sel-b[1]') && d.includes('unmuted'), true, 'digest names the readable candidate and its reading');
  assertEqual(d.includes('aria-pressed=absent'), true, 'digest reports aria-pressed (absent) — the fact a live run must settle');
  assertEqual(
    describeZoomMicCandidates([candidate('s', 0, { ariaLabel: 'audio', ariaPressed: 'true' })]).includes('aria-pressed=true'),
    true,
    'digest reports aria-pressed when present',
  );
  assertEqual(describeZoomMicCandidates([]), 'no mic-control candidates matched any selector', 'digest reports the empty case explicitly');
}

assertEqual(normaliseZoomMicText(null), '', 'normalise(null) => ""');
assertEqual(normaliseZoomMicText('  A   B '), 'a b', 'normalise lowercases and collapses whitespace');

console.log('\n=== W3: other buttons that merely CONTAIN mute vocabulary ===');

// 42. [M26] "Ask to unmute" contains "unmute". Before the fix it read as MUTED,
//     so ensureZoomMutedInMeeting returned {muted:true} and logged "Mic already
//     muted" WITHOUT EVER CLICKING — a silent failure in the safe-looking
//     direction, with the layer-1 WARNING never firing.
assertEqual(
  readZoomMicState(probe({ ariaLabel: 'Ask to unmute' })).kind,
  'not-mic-control',
  '[M26] "Ask to unmute" => not-mic-control (was: read as muted => silent false success)',
);
// 43. [M26] "Mute All" contains "mute". Before the fix it read as UNMUTED, so the
//     watcher would CLICK it — muting every other participant in the meeting.
assertEqual(
  readZoomMicState(probe({ ariaLabel: 'Mute All' })).kind,
  'not-mic-control',
  '[M26] "Mute All" => not-mic-control (was: read as unmuted => watcher would mute EVERYONE)',
);
for (const label of ['Unmute All', 'Ask all to unmute', 'Mute everyone', 'Unmute Participant 3']) {
  assertEqual(readZoomMicState(probe({ ariaLabel: label })).kind, 'not-mic-control', `"${label}" => not-mic-control`);
}
// 44. [M27] The rejection must return IMMEDIATELY — not fall through to the class
//     hints. A "Mute All" button can carry its own muted-looking icon class.
assertEqual(
  readZoomMicState(probe({ ariaLabel: 'Mute All', descendantClassNames: ['SvgAudioMuted'] })).kind,
  'not-mic-control',
  '[M27] a non-mic label is NOT rescued by a muted class hint',
);
assertEqual(
  readZoomMicState(probe({ ariaLabel: 'Mute All', ariaPressed: 'true' })).kind,
  'not-mic-control',
  '[M27] a non-mic label is NOT rescued by aria-pressed either',
);
// 45. A non-mic control is never selectable, so it can never be clicked.
assertEqual(
  selectZoomMicToggle([candidate('footer button[aria-label*="unmute" i]', 0, { ariaLabel: 'Ask to unmute' })]),
  null,
  'a non-mic control alone => no selection, no click',
);

// 46. [M26] THE REQUIRED FIXTURE: a page-wide "Ask to unmute" present AND the real
//     join-audio-container__btn behind it. The non-mic button is deliberately
//     placed FIRST — the worst case — and the REAL control must still win.
{
  const sel = selectZoomMicToggle([
    candidate('footer button[aria-label*="unmute" i]', 0, { ariaLabel: 'Ask to unmute' }),
    candidate('footer button[aria-label*="mute" i]', 1, { ariaLabel: 'Mute All' }),
    candidate('button.join-audio-container__btn', 0, { ariaLabel: 'Mute' }), // the real mic control
  ]);
  assertEqual(sel?.candidate.selector, 'button.join-audio-container__btn', '[M26] the REAL mic control wins over "Ask to unmute" / "Mute All"');
  assertEqual(sel?.reading.kind, 'unmuted', '[M26] and it is read as unmuted, so it WILL be clicked');
}

// 47. The live-observed shape survives the new guard: aria-label="audio" on the
//     real control is still classified as before (not swallowed by the new list).
assertEqual(readZoomMicState(probe({ ariaLabel: 'audio' })).kind, 'not-mute-toggle', 'the live "audio" label is unaffected by the non-mic list');
// 48. And a genuine mic label containing "unmute" is NOT caught by the new list.
assertEqual(readZoomMicState(probe({ ariaLabel: 'unmute my microphone' })).kind, 'muted', 'a real mic label is not mistaken for a non-mic control');

console.log('\n=== W3: selector-list shape guards ===');

// 49. [M28] No comma lists: a candidate is re-addressed as .nth(index) after being
//     probed with querySelectorAll, so a comma list would make that index depend
//     on two engines ordering a union identically.
assertEqual(
  zoomMicToggleSelectors.filter((sel) => sel.includes(',')),
  [],
  '[M28] no selector contains a comma list',
);
// 50. [M28] No page-wide mute-vocabulary selector: every entry carrying mute
//     vocabulary must also be scoped to the footer/toolbar, or "Ask to unmute"
//     anywhere on the page becomes a candidate again.
{
  const pageWideMuteVocab = zoomMicToggleSelectors.filter(
    (sel) => /mute/i.test(sel) && !/footer/i.test(sel) && !/join-audio-container/i.test(sel),
  );
  assertEqual(pageWideMuteVocab, [], '[M28] every mute-vocabulary selector is scoped to the footer/toolbar');
}
assertEqual(zoomMicToggleSelectors[0], 'button.join-audio-container__btn', 'the live-confirmed control is probed FIRST');
assertEqual(zoomNonMicLabelSubstrings.length >= 8, true, 'the non-mic vocabulary list is populated');

console.log('\n=== LAYER 2: silenceOutboundAudioTracks ===');

type FakeTrack = { kind: string; enabled: boolean };
type FakePeer = {
  connectionState?: string;
  getSenders?: () => { track: FakeTrack | null }[];
  getReceivers?: () => { track: FakeTrack }[];
};

function withRegistry<T>(registry: unknown, fn: () => T): T {
  const g = globalThis as unknown as { __vexa_peer_connections?: unknown };
  const had = '__vexa_peer_connections' in g;
  const prev = g.__vexa_peer_connections;
  if (registry === undefined) delete g.__vexa_peer_connections;
  else g.__vexa_peer_connections = registry;
  try {
    return fn();
  } finally {
    if (had) g.__vexa_peer_connections = prev;
    else delete g.__vexa_peer_connections;
  }
}

// 13. Registry absent (video-block init script not injected) — must be REPORTED,
//     not silently treated as success.
{
  const r = withRegistry(undefined, () => silenceOutboundAudioTracks(false));
  assertEqual([r.registryPresent, r.tracksDisabled], [false, 0], 'absent registry => registryPresent false, nothing disabled');
  assertEqual(describeOutboundAudioSweep(r).includes('registry absent'), true, 'absent registry is described as such');
}

// 14. The core guarantee: an enabled outbound audio track gets disabled.
{
  const mic: FakeTrack = { kind: 'audio', enabled: true };
  const cam: FakeTrack = { kind: 'video', enabled: true };
  const peer: FakePeer = { connectionState: 'connected', getSenders: () => [{ track: mic }, { track: cam }, { track: null }] };
  const r = withRegistry([peer], () => silenceOutboundAudioTracks(false));
  assertEqual(mic.enabled, false, 'GUARANTEE: outbound AUDIO track is disabled');
  assertEqual(cam.enabled, true, 'outbound VIDEO track is left alone (video is handled elsewhere)');
  assertEqual(
    [r.registryPresent, r.peerConnections, r.audioSendersFound, r.tracksDisabled, r.alreadyDisabled, r.errors],
    [true, 1, 1, 1, 0, 0],
    'counters report exactly one audio sender disabled',
  );
}

// 15. Incoming audio is what the transcription pipeline depends on. If someone
//     ever swaps getSenders for getReceivers, this goes red.
{
  const remote: FakeTrack = { kind: 'audio', enabled: true };
  const peer: FakePeer = { connectionState: 'connected', getSenders: () => [], getReceivers: () => [{ track: remote }] };
  withRegistry([peer], () => silenceOutboundAudioTracks(false));
  assertEqual(remote.enabled, true, 'INCOMING audio track is never touched (recording must keep working)');
}

// 16. voiceAgentEnabled: a voice agent legitimately transmits TTS.
{
  const mic: FakeTrack = { kind: 'audio', enabled: true };
  const peer: FakePeer = { connectionState: 'connected', getSenders: () => [{ track: mic }] };
  const r = withRegistry([peer], () => silenceOutboundAudioTracks(true));
  assertEqual(mic.enabled, true, 'voiceAgentEnabled => outbound audio stays ENABLED');
  assertEqual([r.skippedVoiceAgent, r.audioSendersFound], [true, 0], 'voiceAgentEnabled => skipped before touching the registry');
  assertEqual(describeOutboundAudioSweep(r).includes('voice agent'), true, 'voice-agent skip is described as such');
}

// 17. Idempotent re-sweep: an already-disabled track is counted separately, not
//     re-reported as a fresh disable (the guard logs only real disables).
{
  const mic: FakeTrack = { kind: 'audio', enabled: false };
  const peer: FakePeer = { connectionState: 'connected', getSenders: () => [{ track: mic }] };
  const r = withRegistry([peer], () => silenceOutboundAudioTracks(false));
  assertEqual([r.tracksDisabled, r.alreadyDisabled], [0, 1], 'already-disabled track counts as alreadyDisabled, not tracksDisabled');
}

// 18. A closed peer is skipped entirely.
{
  const mic: FakeTrack = { kind: 'audio', enabled: true };
  const peer: FakePeer = { connectionState: 'closed', getSenders: () => [{ track: mic }] };
  const r = withRegistry([peer], () => silenceOutboundAudioTracks(false));
  assertEqual([r.peerConnections, r.audioSendersFound], [0, 0], 'closed peer connection is skipped');
}

// 19. One bad peer must never abort the sweep — the LAST peer still gets silenced.
{
  const mic: FakeTrack = { kind: 'audio', enabled: true };
  const thrower: FakePeer = { connectionState: 'connected', getSenders: () => { throw new Error('context destroyed'); } };
  const good: FakePeer = { connectionState: 'connected', getSenders: () => [{ track: mic }] };
  const r = withRegistry([thrower, {} as FakePeer, null, good], () => silenceOutboundAudioTracks(false));
  assertEqual(mic.enabled, false, 'a throwing peer does not stop later peers from being silenced');
  assertEqual([r.tracksDisabled, r.errors], [1, 3], 'errors counted (throw + no-getSenders + null) while the good peer still disabled');
}

// 20. A non-array registry must not throw.
{
  const r = withRegistry({ nope: true }, () => silenceOutboundAudioTracks(false));
  assertEqual([r.registryPresent, r.errors], [false, 0], 'non-array registry => treated as absent, no throw');
}

// 21. Multiple peers, multiple audio senders.
{
  const a: FakeTrack = { kind: 'audio', enabled: true };
  const b: FakeTrack = { kind: 'audio', enabled: true };
  const r = withRegistry(
    [
      { connectionState: 'connected', getSenders: () => [{ track: a }] } as FakePeer,
      { connectionState: 'connecting', getSenders: () => [{ track: b }] } as FakePeer,
    ],
    () => silenceOutboundAudioTracks(false),
  );
  assertEqual([a.enabled, b.enabled], [false, false], 'every peer’s outbound audio is silenced');
  assertEqual([r.peerConnections, r.tracksDisabled], [2, 2], 'counters aggregate across peers');
  assertEqual(describeOutboundAudioSweep(r), 'pcs=2 audioSenders=2 disabled=2 alreadyDisabled=0 errors=0', 'sweep description reports the real counters');
}

console.log('\n=== LAYER 2 (primary): the IRREVOCABLE lock ===');

/**
 * A fake MediaStreamTrack whose `enabled` is a REAL accessor over a backing
 * field, so `real()` reports whether audio is genuinely stopped — independently
 * of whatever the lock's own getter later claims. That separation is the whole
 * point: it is what makes "sealed but still transmitting" a detectable bug
 * rather than an invisible one.
 */
function makeTrack(kind: string): { track: { kind: string; enabled: boolean }; real: () => boolean } {
  let real = true;
  // `enabled` lives on the PROTOTYPE, as it does on a real MediaStreamTrack. That
  // matters: the lock shadows it with an OWN accessor, so deleting the seal must
  // reveal the prototype accessor again. The earlier fake put `enabled` directly
  // on the instance, where defineProperty REPLACED it outright — which is exactly
  // why the de-seal case (BLOCKER 1) went unnoticed by 138 passing tests.
  const proto = {
    get enabled(): boolean {
      return real;
    },
    set enabled(v: boolean) {
      real = v;
    },
  };
  const track = Object.create(proto) as { kind: string; enabled: boolean };
  track.kind = kind;
  return { track, real: () => real };
}

type FakeSenderProto = { replaceTrack?: unknown };
type FakePcProto = { addTrack?: unknown };

/** Install fake globals, always starting from a clean lock state, then restore. */
function withGlobals(patch: Record<string, unknown>, fn: () => void): void {
  const g = globalThis as unknown as Record<string, unknown>;
  const saved = new Map<string, PropertyDescriptor | undefined>();
  for (const k of Object.keys(patch)) {
    saved.set(k, Object.getOwnPropertyDescriptor(g, k));
    Object.defineProperty(g, k, { value: patch[k], writable: true, configurable: true, enumerable: false });
  }
  const lockDesc = Object.getOwnPropertyDescriptor(g, '__vexa_zoom_audio_lock');
  delete g.__vexa_zoom_audio_lock;
  try {
    fn();
  } finally {
    delete g.__vexa_zoom_audio_lock;
    if (lockDesc) Object.defineProperty(g, '__vexa_zoom_audio_lock', lockDesc);
    for (const [k, d] of saved) {
      if (d) Object.defineProperty(g, k, d);
      else delete g[k];
    }
  }
}

/** A fake RTCPeerConnection CONSTRUCTOR (a function, as the real global is), so
 *  the constructor patch that builds the Zoom-local registry actually engages.
 *  A plain `{ prototype }` object does not — `typeof` must be 'function'. */
function makeFakePcCtor(proto: Record<string, unknown>): unknown {
  const Ctor = function (this: unknown) { /* no-op peer */ } as unknown as { prototype: Record<string, unknown> };
  Ctor.prototype = proto;
  return Ctor;
}

const peerWith = (tracks: { kind: string; enabled: boolean }[]) => ({
  connectionState: 'connected',
  getSenders: () => tracks.map((t) => ({ track: t })),
});

// 22. [M17] ORDERING: the real audio must be stopped BEFORE the seal goes on.
//     If defineProperty ran first, `track.enabled` would read false while the
//     underlying track kept transmitting — a lie, not a guarantee. real() is
//     the only witness that can tell those two apart.
{
  const mic = makeTrack('audio');
  withGlobals({ __vexa_peer_connections: [peerWith([mic.track])] }, () => {
    const r = installOutboundAudioLockInPage({ voiceAgentEnabled: false, sealEnabled: true });
    assertEqual(mic.real(), false, '[M17] REAL audio state is stopped (enabled=false applied BEFORE sealing)');
    assertEqual(mic.track.enabled, false, 'sealed getter reports false');
    assertEqual([r.tracksLocked, r.registryPresent], [1, true], 'existing outbound audio track is locked');
  });
}

// 23. [M18] "CANNOT BE UNMUTED": an assignment of true must not take effect.
{
  const mic = makeTrack('audio');
  withGlobals({ __vexa_peer_connections: [peerWith([mic.track])] }, () => {
    installOutboundAudioLockInPage({ voiceAgentEnabled: false, sealEnabled: true });
    mic.track.enabled = true; // <- the unmute attempt
    assertEqual(mic.track.enabled, false, '[M18] GUARANTEE: setting enabled=true does NOT unmute (getter still false)');
    assertEqual(mic.real(), false, '[M18] GUARANTEE: real audio state still stopped after the unmute attempt');
    const again = installOutboundAudioLockInPage({ voiceAgentEnabled: false, sealEnabled: true });
    assertEqual(again.blockedUnmutes, 1, '[M18] the refused unmute attempt is counted and reportable');
  });
}

// 24. Repeated unmute attempts are all refused and all counted.
{
  const mic = makeTrack('audio');
  withGlobals({ __vexa_peer_connections: [peerWith([mic.track])] }, () => {
    installOutboundAudioLockInPage({ voiceAgentEnabled: false, sealEnabled: true });
    mic.track.enabled = true;
    mic.track.enabled = true;
    mic.track.enabled = true;
    assertEqual(mic.real(), false, 'three unmute attempts all refused');
    assertEqual(installOutboundAudioLockInPage({ voiceAgentEnabled: false, sealEnabled: true }).blockedUnmutes, 3, 'all three attempts counted');
  });
}

// 25. Setting enabled=false is NOT counted as a blocked unmute (it agrees with us).
{
  const mic = makeTrack('audio');
  withGlobals({ __vexa_peer_connections: [peerWith([mic.track])] }, () => {
    installOutboundAudioLockInPage({ voiceAgentEnabled: false, sealEnabled: true });
    mic.track.enabled = false;
    assertEqual(installOutboundAudioLockInPage({ voiceAgentEnabled: false, sealEnabled: true }).blockedUnmutes, 0, 'enabled=false is not a blocked unmute');
  });
}

// 26. Outbound VIDEO is never sealed — the voice-agent canvas path injects video
//     tracks via addTrack and must keep working.
{
  const cam = makeTrack('video');
  withGlobals({ __vexa_peer_connections: [peerWith([cam.track])] }, () => {
    const r = installOutboundAudioLockInPage({ voiceAgentEnabled: false, sealEnabled: true });
    assertEqual(cam.real(), true, 'outbound VIDEO track is left enabled and unsealed');
    assertEqual(r.tracksLocked, 0, 'no video track counted as locked');
    cam.track.enabled = true;
    assertEqual(cam.real(), true, 'video track remains settable');
  });
}

// 27. [M19] addTrack: a NEWLY ADDED outbound audio track is silenced AT BIRTH —
//     no polling interval has elapsed, and it is disabled BEFORE the original
//     addTrack runs, so there is no transmit window at all.
{
  const fresh = makeTrack('audio');
  const seen: { enabledAtAttach: boolean }[] = [];
  const pcProto: FakePcProto = {
    addTrack(track: { enabled: boolean }) {
      seen.push({ enabledAtAttach: track.enabled });
      return { track };
    },
  };
  withGlobals({ RTCPeerConnection: { prototype: pcProto }, __vexa_peer_connections: [] }, () => {
    const r = installOutboundAudioLockInPage({ voiceAgentEnabled: false, sealEnabled: true });
    assertEqual(r.patchedAddTrack, true, '[M19] addTrack was patched');
    (pcProto.addTrack as (t: unknown) => unknown)(fresh.track);
    assertEqual(fresh.real(), false, '[M19] ZERO-WINDOW: track added via addTrack is disabled with no interval elapsed');
    assertEqual(seen[0].enabledAtAttach, false, '[M19] it was already disabled BEFORE the original addTrack ran');
    fresh.track.enabled = true;
    assertEqual(fresh.real(), false, '[M19] the newly added track is also sealed against unmuting');
  });
}

// 28. [M20] replaceTrack: a REPLACED mic track is silenced at birth. This is the
//     hole a 10s poll cannot close — Zoom does replace the mic track.
{
  const replacement = makeTrack('audio');
  let receivedEnabled: boolean | null = null;
  const senderProto: FakeSenderProto = {
    replaceTrack(track: { enabled: boolean }) {
      receivedEnabled = track.enabled;
      return Promise.resolve();
    },
  };
  withGlobals({ RTCRtpSender: { prototype: senderProto }, __vexa_peer_connections: [] }, () => {
    const r = installOutboundAudioLockInPage({ voiceAgentEnabled: false, sealEnabled: true });
    assertEqual(r.patchedReplaceTrack, true, '[M20] replaceTrack was patched');
    void (senderProto.replaceTrack as (t: unknown) => unknown)(replacement.track);
    assertEqual(replacement.real(), false, '[M20] ZERO-WINDOW: replacement mic track is disabled with no interval elapsed');
    assertEqual(receivedEnabled, false, '[M20] it was already disabled BEFORE the original replaceTrack ran');
    replacement.track.enabled = true;
    assertEqual(replacement.real(), false, '[M20] the replacement track is also sealed');
  });
}

// 29. The patches are idempotent — re-installing must not double-wrap (which
//     would make every counter double-count and stack wrappers all call long).
{
  const pcProto: FakePcProto = { addTrack: (t: unknown) => t };
  withGlobals({ RTCPeerConnection: { prototype: pcProto }, __vexa_peer_connections: [] }, () => {
    const first = installOutboundAudioLockInPage({ voiceAgentEnabled: false, sealEnabled: true });
    const wrapperAfterFirst = pcProto.addTrack;
    const second = installOutboundAudioLockInPage({ voiceAgentEnabled: false, sealEnabled: true });
    assertEqual([first.alreadyInstalled, second.alreadyInstalled], [false, true], 're-install reports alreadyInstalled');
    assertEqual(second.patchedAddTrack, true, 'patch state persists across re-install');
    assertEqual(pcProto.addTrack === wrapperAfterFirst, true, 'addTrack was NOT wrapped a second time');
  });
}

// 30. An already-locked track is not re-locked (WeakSet identity), so counters
//     stay meaningful across the periodic re-install.
{
  const mic = makeTrack('audio');
  withGlobals({ __vexa_peer_connections: [peerWith([mic.track])] }, () => {
    assertEqual(installOutboundAudioLockInPage({ voiceAgentEnabled: false, sealEnabled: true }).tracksLocked, 1, 'first install locks the track');
    assertEqual(installOutboundAudioLockInPage({ voiceAgentEnabled: false, sealEnabled: true }).tracksLocked, 1, 'second install does not re-count it');
  });
}

// 31. [M21] voiceAgentEnabled bypasses EVERY part of the lock — no seal, no
//     prototype patching. A voice agent must be able to transmit TTS.
{
  const mic = makeTrack('audio');
  const pcProto: FakePcProto = { addTrack: (t: unknown) => t };
  const original = pcProto.addTrack;
  withGlobals({ RTCPeerConnection: { prototype: pcProto }, __vexa_peer_connections: [peerWith([mic.track])] }, () => {
    const r = installOutboundAudioLockInPage({ voiceAgentEnabled: true, sealEnabled: true });
    assertEqual(mic.real(), true, '[M21] voice agent: outbound audio stays ENABLED');
    assertEqual(pcProto.addTrack === original, true, '[M21] voice agent: addTrack is NOT patched');
    assertEqual(
      [r.skippedVoiceAgent, r.tracksLocked, r.patchedAddTrack],
      [true, 0, false],
      '[M21] voice agent: nothing locked, nothing patched',
    );
    mic.track.enabled = true;
    assertEqual(mic.real(), true, '[M21] voice agent: the track remains settable (can unmute)');
  });
}

// 32. Inbound audio is never touched, even by the lock.
{
  const remote = makeTrack('audio');
  const peer = { connectionState: 'connected', getSenders: () => [], getReceivers: () => [{ track: remote.track }] };
  withGlobals({ __vexa_peer_connections: [peer] }, () => {
    installOutboundAudioLockInPage({ voiceAgentEnabled: false, sealEnabled: true });
    assertEqual(remote.real(), true, 'INCOMING audio track is never sealed (transcript must keep working)');
    remote.track.enabled = false;
    remote.track.enabled = true;
    assertEqual(remote.real(), true, 'incoming track stays fully settable');
  });
}

// 33. Missing APIs must not throw — the lock has to degrade, not crash.
{
  withGlobals({ __vexa_peer_connections: [] }, () => {
    const r = installOutboundAudioLockInPage({ voiceAgentEnabled: false, sealEnabled: true });
    assertEqual(
      [r.patchedAddTrack, r.patchedReplaceTrack, r.errors],
      [false, false, 0],
      'absent RTCPeerConnection / RTCRtpSender => no patches, no errors',
    );
  });
}

// 34. describeOutboundAudioLock reports what actually happened.
{
  const pcProto: FakePcProto = { addTrack: (t: unknown) => t };
  withGlobals({ RTCPeerConnection: makeFakePcCtor(pcProto as Record<string, unknown>) }, () => {
    const r = installOutboundAudioLockInPage({ voiceAgentEnabled: false, sealEnabled: true });
    assertEqual(
      describeOutboundAudioLock(r),
      'tracksLocked=0 verified=0 resealed=0 blockedUnmutes=0 patched=[ctor,addTrack] registry=ABSENT errors=0',
      'lock description reports the patches applied; registry is ABSENT on the FIRST pass because it is read before the ctor patch creates it',
    );
    // ...and PRESENT on the next pass, once the constructor patch has created it.
    assertEqual(
      describeOutboundAudioLock(installOutboundAudioLockInPage({ voiceAgentEnabled: false, sealEnabled: true })).includes('registry=present'),
      true,
      'the Zoom-local registry exists from the second pass onward',
    );
  });
  assertEqual(
    describeOutboundAudioLock({
      sealEnabled: true, skippedVoiceAgent: true, alreadyInstalled: false, registryPresent: false, tracksLocked: 0,
      blockedUnmutes: 0, tracksVerified: 0, tracksResealed: 0, patchedConstructor: false,
      patchedAddTrack: false, patchedAddTransceiver: false, patchedReplaceTrack: false,
      patchedGetUserMedia: false, errors: 0,
    }).includes('UNLOCKED'),
    true,
    'voice-agent lock description says UNLOCKED',
  );
}

console.log('\n=== QA nit: the fallback SWEEP must read both registries ===');

// 70. [M42] THE NIT. silenceOutboundAudioTracks read only Meet's registry, which
//     is cameraEnabled-gated and absent by default — so the `errors > 0` fallback
//     for an unsealable track invoked a sweep that found no peers and disabled
//     nothing. Reproduces QA's case C: the ZOOM-LOCAL registry alone must work.
{
  const mic = makeTrack('audio');
  const g5 = globalThis as unknown as Record<string, unknown>;
  const savedZ = Object.getOwnPropertyDescriptor(g5, '__vexa_zoom_peer_connections');
  const savedM = Object.getOwnPropertyDescriptor(g5, '__vexa_peer_connections');
  g5.__vexa_zoom_peer_connections = [peerWith([mic.track])];
  delete g5.__vexa_peer_connections; // DEFAULT CONFIG: Meet's registry absent
  try {
    const r = silenceOutboundAudioTracks(false);
    assertEqual(r.registryPresent, true, '[M42] the ZOOM-LOCAL registry alone is enough for the sweep');
    assertEqual([r.audioSendersFound, r.tracksDisabled], [1, 1], '[M42] the track is found and disabled with Meet’s registry absent');
    assertEqual(mic.real(), false, '[M42] real audio is stopped by the sweep');
  } finally {
    delete g5.__vexa_zoom_peer_connections;
    if (savedZ) Object.defineProperty(g5, '__vexa_zoom_peer_connections', savedZ);
    if (savedM) Object.defineProperty(g5, '__vexa_peer_connections', savedM);
  }
}

// 71. [M42] A peer present in BOTH registries is swept ONCE, not twice — dedup by
//     identity, so the counters stay truthful when cameraEnabled is on.
{
  const mic = makeTrack('audio');
  const shared = peerWith([mic.track]);
  const g5 = globalThis as unknown as Record<string, unknown>;
  const savedZ = Object.getOwnPropertyDescriptor(g5, '__vexa_zoom_peer_connections');
  const savedM = Object.getOwnPropertyDescriptor(g5, '__vexa_peer_connections');
  g5.__vexa_zoom_peer_connections = [shared];
  g5.__vexa_peer_connections = [shared];
  try {
    const r = silenceOutboundAudioTracks(false);
    assertEqual(r.peerConnections, 1, '[M42] a peer in both registries is counted ONCE');
    assertEqual(r.audioSendersFound, 1, '[M42] and its sender is swept once, not twice');
  } finally {
    delete g5.__vexa_zoom_peer_connections;
    delete g5.__vexa_peer_connections;
    if (savedZ) Object.defineProperty(g5, '__vexa_zoom_peer_connections', savedZ);
    if (savedM) Object.defineProperty(g5, '__vexa_peer_connections', savedM);
  }
}

// 72. [M42] End-to-end for QA's case C: an UNSEALABLE track, re-enabled, is
//     recovered by the sweep — the cover the comment claims, now actually there.
{
  const u = makeUnsealableTrack();
  const g5 = globalThis as unknown as Record<string, unknown>;
  const savedZ = Object.getOwnPropertyDescriptor(g5, '__vexa_zoom_peer_connections');
  const savedLock = Object.getOwnPropertyDescriptor(g5, '__vexa_zoom_audio_lock');
  delete g5.__vexa_zoom_audio_lock;
  delete g5.__vexa_peer_connections;
  g5.__vexa_zoom_peer_connections = [peerWith([u.track])];
  try {
    const lock = installOutboundAudioLockInPage({ voiceAgentEnabled: false, sealEnabled: true });
    assertEqual([lock.tracksLocked, lock.errors], [0, 1], '[M42] unsealable: locked=0, errors=1 (routes to the sweep)');
    u.track.enabled = true;
    assertEqual(u.real(), true, '[M42] CONTROL: an unsealable track really can be re-enabled');
    const sweep = silenceOutboundAudioTracks(false);
    assertEqual(sweep.tracksDisabled, 1, '[M42] the fallback sweep RECOVERS it (was 0 before the fix)');
    assertEqual(u.real(), false, '[M42] audio is silenced again');
  } finally {
    delete g5.__vexa_zoom_audio_lock;
    delete g5.__vexa_zoom_peer_connections;
    if (savedZ) Object.defineProperty(g5, '__vexa_zoom_peer_connections', savedZ);
    if (savedLock) Object.defineProperty(g5, '__vexa_zoom_audio_lock', savedLock);
  }
}

console.log('\n=== Item A: preview mute must not be skipped on a brittle label ===');

// 73. [M43] The preview step now reads state the same tolerant way the in-meeting
//     path does. Source guard, because it is Playwright code: the exact-match
//     `=== 'Mute'` must be gone, and a muted reading must NOT click.
{
  const joinSrc = readZoomWebSource('join.ts');
  assertEqual(joinSrc.includes("muteAriaLabel === 'Mute'"), false, "[M43] the brittle exact-match `=== 'Mute'` is gone");
  assertEqual(joinSrc.includes('readZoomMicState(probe)'), true, '[M43] the preview step uses the tolerant reader');
  const previewIdx = joinSrc.indexOf('readZoomMicState(probe)');
  const tail = joinSrc.slice(previewIdx);
  assertEqual(tail.includes("if (reading.kind === 'unmuted') {"), true, '[M43] it clicks ONLY on a confident unmuted reading');
  assertEqual(tail.includes('NOT clicking (a click here would unmute)'), true, '[M43] and explains why a muted reading is not clicked (toggle hazard)');
  assertEqual(tail.includes('unreadable'), true, '[M43] an unreadable state is logged, not silently skipped');
}

// 74. [M43] The same readings the preview step branches on, exercised directly —
//     so the branch conditions are covered by behaviour, not only by grep.
assertEqual(readZoomMicState(probe({ ariaLabel: 'Mute' })).kind, 'unmuted', '[M43] preview "Mute" => unmuted => WILL click');
assertEqual(readZoomMicState(probe({ ariaLabel: 'Unmute' })).kind, 'muted', '[M43] preview "Unmute" => muted => will NOT click');
assertEqual(readZoomMicState(probe({ ariaLabel: 'audio' })).kind, 'not-mute-toggle', '[M43] preview "audio" => unreadable => will NOT click');
assertEqual(readZoomMicState(probe({ ariaLabel: 'mute my microphone' })).kind, 'unmuted', '[M43] preview label variant still resolves');

console.log('\n=== N1: ZOOM_AUDIO_LOCK kill switch ===');

// 63. [M37] DEFAULT IS ON. This is the assertion that matters most: if the
//     default ever flips, an unprotected bot ships silently. Unset, empty and
//     unrecognised values must all mean ENABLED — a typo must never disable the
//     seal.
assertEqual(parseZoomAudioLockEnv(undefined), true, '[M37] DEFAULT: unset => seal ENABLED');
assertEqual(parseZoomAudioLockEnv(''), true, '[M37] DEFAULT: empty => seal ENABLED');
assertEqual(parseZoomAudioLockEnv('   '), true, '[M37] DEFAULT: whitespace => seal ENABLED');
for (const typo of ['offf', 'nope', 'disable', 'true', '1', 'on', 'yes', 'ON']) {
  assertEqual(parseZoomAudioLockEnv(typo), true, `[M37] unrecognised/affirmative "${typo}" => seal ENABLED (a typo must not disable it)`);
}

// 64. [M37] The documented opt-outs, case- and whitespace-insensitive.
for (const off of ['off', 'OFF', ' Off ', '0', 'false', 'FALSE', 'no', 'disabled']) {
  assertEqual(parseZoomAudioLockEnv(off), false, `[M37] "${off}" => seal DISABLED`);
}

// 65. [M38] SWITCH OFF: no seal, no at-birth patches — but the track is STILL
//     disabled, and an unmute is genuinely possible again. That is the pre-seal
//     behaviour, i.e. the configuration proven not to disturb audio-join.
{
  const mic = makeTrack('audio');
  const pcProto: FakePcProto & { addTransceiver?: unknown } = {
    addTrack: (t: unknown) => t,
    addTransceiver: (t: unknown) => t,
  };
  const originalAddTrack = pcProto.addTrack;
  withGlobals(
    { RTCPeerConnection: makeFakePcCtor(pcProto as Record<string, unknown>), __vexa_peer_connections: [peerWith([mic.track])] },
    () => {
      const r = installOutboundAudioLockInPage({ voiceAgentEnabled: false, sealEnabled: false });
      assertEqual(r.sealEnabled, false, '[M38] the result reports the seal as disabled');
      assertEqual(mic.real(), false, '[M38] the track is STILL disabled (enabled=false is retained)');
      assertEqual(pcProto.addTrack === originalAddTrack, true, '[M38] addTrack is NOT patched');
      assertEqual(
        [r.patchedAddTrack, r.patchedAddTransceiver, r.patchedReplaceTrack, r.patchedGetUserMedia],
        [false, false, false, false],
        '[M38] NONE of the at-birth patches are installed',
      );
      // The honest consequence, asserted rather than glossed:
      mic.track.enabled = true;
      assertEqual(mic.real(), true, '[M38] HONEST LIMIT: with the switch off an unmute CAN succeed');
    },
  );
}

// 66. [M38] ...but the constructor registry patch IS still installed, because
//     without it `enabled = false` would have no senders to act on in the default
//     configuration and OFF would degrade to DOM-mute-only.
{
  const pcProto: FakePcProto = { addTrack: (t: unknown) => t };
  withGlobals({ RTCPeerConnection: makeFakePcCtor(pcProto as Record<string, unknown>) }, () => {
    const g4 = globalThis as unknown as Record<string, unknown>;
    delete g4.__vexa_zoom_peer_connections;
    const r = installOutboundAudioLockInPage({ voiceAgentEnabled: false, sealEnabled: false });
    assertEqual(r.patchedConstructor, true, '[M38] the ctor registry patch survives the kill switch (it is observationally inert)');
    const Ctor = g4.RTCPeerConnection as new () => object;
    const pc = new Ctor();
    assertEqual((g4.__vexa_zoom_peer_connections as unknown[]).includes(pc), true, '[M38] so senders remain enumerable for the enabled=false sweep');
    delete g4.__vexa_zoom_peer_connections;
  });
}

// 67. [M39] voiceAgentEnabled bypasses everything INDEPENDENTLY of the switch —
//     both switch states must leave a voice agent able to transmit.
for (const sealEnabled of [true, false]) {
  const mic = makeTrack('audio');
  withGlobals({ __vexa_peer_connections: [peerWith([mic.track])] }, () => {
    const r = installOutboundAudioLockInPage({ voiceAgentEnabled: true, sealEnabled });
    assertEqual(r.skippedVoiceAgent, true, `[M39] voice agent bypasses the lock (sealEnabled=${sealEnabled})`);
    assertEqual(mic.real(), true, `[M39] voice agent audio stays ENABLED (sealEnabled=${sealEnabled})`);
  });
}

// 68. [M40] The mode must be visible in the log line, in BOTH states — a silent
//     kill switch is worse than none.
{
  const on = describeOutboundAudioLock({
    sealEnabled: true, skippedVoiceAgent: false, alreadyInstalled: false, registryPresent: true,
    tracksLocked: 1, blockedUnmutes: 0, tracksVerified: 0, tracksResealed: 0, patchedConstructor: true,
    patchedAddTrack: true, patchedAddTransceiver: true, patchedReplaceTrack: true, patchedGetUserMedia: true, errors: 0,
  });
  assertEqual(on.includes('tracksLocked=1') && !on.includes('SEAL DISABLED'), true, '[M40] seal ON: the description reports the locked track');
  const off = describeOutboundAudioLock({
    sealEnabled: false, skippedVoiceAgent: false, alreadyInstalled: false, registryPresent: true,
    tracksLocked: 1, blockedUnmutes: 0, tracksVerified: 0, tracksResealed: 0, patchedConstructor: true,
    patchedAddTrack: false, patchedAddTransceiver: false, patchedReplaceTrack: false, patchedGetUserMedia: false, errors: 0,
  });
  assertEqual(off.includes('SEAL DISABLED by ZOOM_AUDIO_LOCK'), true, '[M40] seal OFF: the description says so explicitly');
  assertEqual(off.includes('an unmute CAN succeed'), true, '[M40] seal OFF: and states the consequence plainly');
}

// 69. [M41] Source guard: the decision must be logged at arm time, and read via
//     the memoised accessor (so it is read once per process, not per call).
{
  const src = readPrepareSource();
  const start = src.indexOf('export function isZoomAudioSealEnabled');
  const fn = src.slice(start, src.indexOf('\n}\n', start));
  assertEqual(fn.includes('process.env.ZOOM_AUDIO_LOCK'), true, '[M41] the env var is read');
  assertEqual(fn.includes('zoomAudioSealDecision !== null'), true, '[M41] the decision is memoised (read once per process)');
  // Statement position, not merely "the text log( appears": a dead-coded
  // `void 0 && log(...)` satisfies a bare includes() check, and did survive that
  // weaker assertion during mutation testing.
  assertEqual(/\n  log\(/.test(fn), true, '[M41] the decision is LOGGED at arm time (a real statement, not dead-coded)');
  assertEqual(fn.includes('SEAL: ENABLED') && fn.includes('SEAL: DISABLED'), true, '[M41] both modes are logged plainly');
  const joinSrc = readZoomWebSource('join.ts');
  assertEqual(joinSrc.includes('sealEnabled: isZoomAudioSealEnabled()'), true, '[M41] join.ts threads the switch into the page-load arm');
}

console.log('\n=== BLOCKER 1: a de-sealed track must be RE-SEALED ===');

// 56. [M32] THE BLOCKER. The seal is `configurable: true`, so it can be removed.
//     The old code short-circuited on WeakSet membership, so once removed the
//     track was never re-sealed: `enabled` went back to true and audio
//     transmitted indefinitely, while the guard reported errors:0 so even its
//     fallback sweep never fired. Reproduces the QA gate's exact sequence.
{
  const mic = makeTrack('audio');
  withGlobals({ __vexa_peer_connections: [peerWith([mic.track])] }, () => {
    // 1. after install
    const first = installOutboundAudioLockInPage({ voiceAgentEnabled: false, sealEnabled: true });
    assertEqual([first.tracksLocked, mic.real()], [1, false], '[M32] step 1: sealed, real audio stopped');

    // 2. unmute attempt while sealed
    mic.track.enabled = true;
    assertEqual(mic.real(), false, '[M32] step 2: seal holds against an unmute attempt');

    // 3. seal REMOVED, then unmute -> audio live
    delete (mic.track as { enabled?: boolean }).enabled;
    mic.track.enabled = true;
    assertEqual(mic.real(), true, '[M32] step 3: CONTROL — with the seal removed the track really does go live');

    // 4. guard re-install MUST recover it
    const second = installOutboundAudioLockInPage({ voiceAgentEnabled: false, sealEnabled: true });
    assertEqual(mic.real(), false, '[M32] step 4: the re-install RE-SEALS and stops the audio again');
    assertEqual(second.tracksResealed, 1, '[M32] step 4: the re-seal is reported (tracksResealed=1)');

    // 5. and the restored seal holds
    mic.track.enabled = true;
    assertEqual(mic.real(), false, '[M32] step 5: the restored seal holds against a further unmute attempt');
  });
}

// 57. [M32] A seal that is STILL intact is verified, not re-applied — so the
//     counters stay meaningful across a long meeting's guard ticks.
{
  const mic = makeTrack('audio');
  withGlobals({ __vexa_peer_connections: [peerWith([mic.track])] }, () => {
    installOutboundAudioLockInPage({ voiceAgentEnabled: false, sealEnabled: true });
    const again = installOutboundAudioLockInPage({ voiceAgentEnabled: false, sealEnabled: true });
    assertEqual([again.tracksVerified, again.tracksResealed], [1, 0], '[M32] an intact seal is VERIFIED, not re-sealed');
  });
}

// 58. [M32] A foreign accessor is not mistaken for our seal — the marker on the
//     getter is what identifies it, not merely "there is a getter here".
{
  const mic = makeTrack('audio');
  withGlobals({ __vexa_peer_connections: [peerWith([mic.track])] }, () => {
    let hidden = true;
    Object.defineProperty(mic.track, 'enabled', {
      configurable: true,
      get: () => hidden,
      set: (v: boolean) => { hidden = v; },
    });
    const r = installOutboundAudioLockInPage({ voiceAgentEnabled: false, sealEnabled: true });
    assertEqual(r.tracksLocked, 1, '[M32] a non-lock accessor is replaced by a real seal');
    mic.track.enabled = true;
    assertEqual(mic.track.enabled, false, '[M32] and that seal refuses an unmute');
  });
}

console.log('\n=== BLOCKER 2: the DEFAULT bot configuration (registry ABSENT) ===');

// 59. [M33] THE OTHER BLOCKER. `__vexa_peer_connections` is written only by
//     services/screen-content.ts's getVirtualCameraInitScript, installed solely
//     when cameraEnabled is true — default FALSE. So in the standard recorder
//     configuration the registry does NOT exist, and the existing-track step
//     sealed nothing. The mic track must therefore be caught AT BIRTH by the
//     getUserMedia patch, with NO registry present at all.
{
  const mic = makeTrack('audio');
  const stream = { getAudioTracks: () => [mic.track] };
  const md = { getUserMedia: (_c: unknown) => Promise.resolve(stream) };
  const g2 = globalThis as unknown as Record<string, unknown>;
  const savedNav = Object.getOwnPropertyDescriptor(g2, 'navigator');
  const savedLock = Object.getOwnPropertyDescriptor(g2, '__vexa_zoom_audio_lock');
  const savedZoomReg = Object.getOwnPropertyDescriptor(g2, '__vexa_zoom_peer_connections');
  delete g2.__vexa_zoom_audio_lock;
  delete g2.__vexa_zoom_peer_connections;
  delete g2.__vexa_peer_connections; // DEFAULT CONFIG: no shared registry
  Object.defineProperty(g2, 'navigator', { value: { mediaDevices: md }, writable: true, configurable: true });
  try {
    const r = installOutboundAudioLockInPage({ voiceAgentEnabled: false, sealEnabled: true });
    assertEqual(r.registryPresent, false, '[M33] DEFAULT CONFIG: no peer-connection registry exists');
    assertEqual(r.patchedGetUserMedia, true, '[M33] the getUserMedia patch is installed anyway');
    // The mic track is acquired AFTER the patch — exactly what arming at page
    // load guarantees, since the patch precedes every Zoom script.
    void (md.getUserMedia as (c: unknown) => Promise<unknown>)({ audio: true }).then(() => {
      assertEqual(mic.real(), false, '[M33] GUARANTEE WITHOUT A REGISTRY: the mic track is sealed at birth');
      mic.track.enabled = true;
      assertEqual(mic.real(), false, '[M33] and it cannot be unmuted');
    });
  } finally {
    delete g2.__vexa_zoom_audio_lock;
    delete g2.__vexa_zoom_peer_connections;
    if (savedZoomReg) Object.defineProperty(g2, '__vexa_zoom_peer_connections', savedZoomReg);
    if (savedLock) Object.defineProperty(g2, '__vexa_zoom_audio_lock', savedLock);
    if (savedNav) Object.defineProperty(g2, 'navigator', savedNav);
    else delete g2.navigator;
  }
}

// 60. [M34] The constructor patch builds a ZOOM-LOCAL registry so senders can be
//     enumerated without Meet's. It must NOT write Meet's global name.
{
  const pcProto: FakePcProto = { addTrack: (t: unknown) => t };
  withGlobals({ RTCPeerConnection: makeFakePcCtor(pcProto as Record<string, unknown>) }, () => {
    const g3 = globalThis as unknown as Record<string, unknown>;
    delete g3.__vexa_zoom_peer_connections;
    delete g3.__vexa_peer_connections;
    const r = installOutboundAudioLockInPage({ voiceAgentEnabled: false, sealEnabled: true });
    assertEqual(r.patchedConstructor, true, '[M34] the RTCPeerConnection constructor is patched');
    const Ctor = g3.RTCPeerConnection as new () => object;
    const pc = new Ctor();
    assertEqual(Array.isArray(g3.__vexa_zoom_peer_connections), true, '[M34] a ZOOM-LOCAL registry is created');
    assertEqual((g3.__vexa_zoom_peer_connections as unknown[]).includes(pc), true, '[M34] new peers are recorded in it');
    assertEqual('__vexa_peer_connections' in g3, false, "[M34] Meet's shared registry name is NEVER written");
    delete g3.__vexa_zoom_peer_connections;
  });
}

// 61. [M35] addTransceiver is a fourth attach path and must also seal at birth.
{
  const fresh = makeTrack('audio');
  let seenEnabled: boolean | null = null;
  const pcProto: FakePcProto & { addTransceiver?: unknown } = {
    addTrack: (t: unknown) => t,
    addTransceiver(track: { enabled: boolean }) { seenEnabled = track.enabled; return {}; },
  };
  withGlobals({ RTCPeerConnection: makeFakePcCtor(pcProto as Record<string, unknown>) }, () => {
    const r = installOutboundAudioLockInPage({ voiceAgentEnabled: false, sealEnabled: true });
    assertEqual(r.patchedAddTransceiver, true, '[M35] addTransceiver is patched');
    (pcProto.addTransceiver as (t: unknown) => unknown)(fresh.track);
    assertEqual(fresh.real(), false, '[M35] a track attached via addTransceiver is sealed at birth');
    assertEqual(seenEnabled, false, '[M35] it was disabled BEFORE the original addTransceiver ran');
  });
}

console.log('\n=== W8: sealing-failure fallback (defineProperty throws) ===');

/**
 * A track whose `enabled` is WRITABLE but NON-CONFIGURABLE: assignment works, so
 * the audio can still be stopped, but Object.defineProperty throws. makeTrack()
 * always yields a configurable accessor, so none of the other cases ever reach
 * the catch branch — this fixture is the only one that executes it.
 */
function makeUnsealableTrack(): { track: { kind: string; enabled: boolean }; real: () => boolean } {
  const track = { kind: 'audio' } as { kind: string; enabled: boolean };
  Object.defineProperty(track, 'enabled', { value: true, writable: true, configurable: false, enumerable: true });
  return { track, real: () => track.enabled };
}

// 51. [M29] The fixture must genuinely throw, or the test below proves nothing.
{
  const u = makeUnsealableTrack();
  let threw = false;
  try {
    Object.defineProperty(u.track, 'enabled', { get: () => false, configurable: true });
  } catch {
    threw = true;
  }
  assertEqual(threw, true, '[M29] CONTROL: defineProperty really does throw on this fixture');
}

// 52. [M29] When sealing fails, silence is still applied — because `enabled =
//     false` runs BEFORE the seal — and the failure is REPORTED (errors > 0),
//     which is what routes the track to the guard's periodic re-sweep.
{
  const u = makeUnsealableTrack();
  withGlobals({ __vexa_peer_connections: [peerWith([u.track])] }, () => {
    const r = installOutboundAudioLockInPage({ voiceAgentEnabled: false, sealEnabled: true });
    assertEqual(u.real(), false, '[M29] unsealable track is still DISABLED (step 1 ran before the seal threw)');
    assertEqual(r.errors, 1, '[M29] the sealing failure is counted (drives the guard re-sweep)');
    assertEqual(r.tracksLocked, 0, '[M29] it is NOT counted as locked — the lock genuinely did not take');
  });
}

// 53. An unsealable track really is revocable — this is the honest limit of the
//     fallback, asserted so nobody mistakes it for a guarantee.
{
  const u = makeUnsealableTrack();
  withGlobals({ __vexa_peer_connections: [peerWith([u.track])] }, () => {
    installOutboundAudioLockInPage({ voiceAgentEnabled: false, sealEnabled: true });
    u.track.enabled = true;
    assertEqual(u.real(), true, 'an UNSEALABLE track can be re-enabled — only the periodic sweep covers it');
  });
}

console.log('\n=== LAYER 3: stepZoomMuteWatcher (persistent visual mute) ===');

const WCFG = { confirmations: 2, cooldownMs: 30_000 };
const initial = zoomMuteWatcherInitialState;

// 35. A confident 'muted' reading does nothing and resets any streak.
{
  const r = stepZoomMuteWatcher({ consecutiveUnmuted: 1, lastClickAtMs: null, clicks: 0 }, 'muted', 1000, WCFG);
  assertEqual([r.action, r.state.consecutiveUnmuted], ['none', 0], "'muted' => no action, streak reset");
}

// 36. [M22] Non-confident readings NEVER click and never accumulate — the same
//     asymmetry readZoomMicState uses. Clicking an unidentified control could
//     UNMUTE a muted bot.
for (const kind of ['unknown', 'not-mute-toggle', 'not-mic-control', 'none'] as const) {
  const r = stepZoomMuteWatcher({ consecutiveUnmuted: 1, lastClickAtMs: null, clicks: 0 }, kind, 1000, WCFG);
  assertEqual([r.action, r.state.consecutiveUnmuted], ['none', 0], `[M22] '${kind}' => never clicks, streak reset`);
}

// 37. [M23] Confirmations are required: one 'unmuted' reading is not enough.
//     This is what absorbs a lagging aria-label instead of oscillating on it.
{
  const first = stepZoomMuteWatcher(initial, 'unmuted', 1000, WCFG);
  assertEqual([first.action, first.state.consecutiveUnmuted], ['none', 1], '[M23] 1st unmuted reading => no click yet');
  const second = stepZoomMuteWatcher(first.state, 'unmuted', 2000, WCFG);
  assertEqual(second.action, 'click', '[M23] 2nd consecutive unmuted reading => click');
  assertEqual([second.state.clicks, second.state.lastClickAtMs, second.state.consecutiveUnmuted], [1, 2000, 0], 'click recorded, streak reset');
}

// 38. An interrupted streak does not click — this is the anti-oscillation guard.
{
  const a = stepZoomMuteWatcher(initial, 'unmuted', 1000, WCFG);
  const b = stepZoomMuteWatcher(a.state, 'muted', 2000, WCFG);
  const c = stepZoomMuteWatcher(b.state, 'unmuted', 3000, WCFG);
  assertEqual(c.action, 'none', 'unmuted -> muted -> unmuted never reaches 2 CONSECUTIVE readings');
}

// 39. [M24] Cooldown: even sustained 'unmuted' cannot produce a click train.
{
  let st = stepZoomMuteWatcher(initial, 'unmuted', 1000, WCFG).state;
  st = stepZoomMuteWatcher(st, 'unmuted', 2000, WCFG).state; // clicked at 2000
  const soonA = stepZoomMuteWatcher(st, 'unmuted', 3000, WCFG);
  const soonB = stepZoomMuteWatcher(soonA.state, 'unmuted', 4000, WCFG);
  assertEqual(soonB.action, 'none', '[M24] second click refused inside the cooldown window');
  assertEqual(soonB.reason.includes('cooldown'), true, '[M24] the refusal names the cooldown');
  const laterA = stepZoomMuteWatcher(soonB.state, 'unmuted', 40_000, WCFG);
  assertEqual(laterA.action, 'click', '[M24] a click is allowed once the cooldown has expired');
  assertEqual(laterA.state.clicks, 2, 'click count accumulates');
}

// 40. Ten consecutive unmuted readings inside one cooldown produce exactly ONE
//     click. Stated as a bound because an even number of clicks would leave the
//     bot unmuted — the precise failure the one-shot latch exists to prevent.
{
  let st = initial;
  let clicks = 0;
  for (let i = 1; i <= 10; i++) {
    const r = stepZoomMuteWatcher(st, 'unmuted', i * 1000, WCFG);
    st = r.state;
    if (r.action === 'click') clicks++;
  }
  assertEqual(clicks, 1, 'ten unmuted readings within one cooldown => exactly one click (never an even number)');
}

/**
 * Locate prepare.ts for the source guards below. Both documented run locations
 * are tried (repo root and the core dir). If neither resolves this FAILS loudly
 * rather than skipping — a source guard that quietly stops running is worse than
 * no guard at all.
 */
function readZoomWebSource(file: string): string {
  const candidates = [
    `src/platforms/zoom/web/${file}`,
    `services/vexa-bot/core/src/platforms/zoom/web/${file}`,
    `vexa-fork/services/vexa-bot/core/src/platforms/zoom/web/${file}`,
  ];
  for (const rel of candidates) {
    try {
      return readFileSync(rel, 'utf8');
    } catch {
      /* try the next candidate */
    }
  }
  throw new Error(`could not locate ${file} from cwd=${process.cwd()} (tried: ${candidates.join(', ')})`);
}

function readPrepareSource(): string {
  return readZoomWebSource('prepare.ts');
}

console.log('\n=== W4: source guards for the watcher driver (not unit-testable) ===');

// 54. [M30] startZoomMuteWatcher's body is Playwright + setInterval, so no unit
//     test can execute it. It had a real defect: it probed WITHOUT revealing the
//     footer, and probeZoomMicCandidates skips zero-area elements — so with the
//     toolbar auto-hidden every pass found nothing and the watcher was inert,
//     silently, forever. A source guard is the only check available here; it is
//     weaker than a behavioural test and is not presented as equivalent.
{
  const src = readPrepareSource();
  const start = src.indexOf('export function startZoomMuteWatcher');
  assertEqual(start > -1, true, 'startZoomMuteWatcher is present in prepare.ts');
  const body = src.slice(start);
  const end = body.indexOf('\n}\n');
  const fn = body.slice(0, end);

  assertEqual(fn.includes('await revealZoomFooter(page)'), true, '[M30] the watcher reveals the footer before probing (else every pass is blind)');
  // Match the CALL, not the bare identifier: the explanatory comment above the
  // reveal also names probeZoomMicCandidates, and matching that made this
  // assertion fail against correct code.
  assertEqual(
    fn.indexOf('await revealZoomFooter(page)') < fn.indexOf('await probeZoomMicCandidates(page'),
    true,
    '[M30] the reveal happens BEFORE the probe, not after',
  );
  assertEqual(fn.includes('noCandidatePasses'), true, '[M30] a no-candidate pass is tracked, not swallowed');
  assertEqual(
    fn.includes('visual re-mute is INACTIVE'),
    true,
    '[M30] and it is LOGGED — an invisible no-op is the worst outcome for a watcher',
  );
}

// 55. The guard/watcher must forward the closure flag, never a hardcoded false:
//     if the voice-agent early return were ever removed, a hardcoded false would
//     silently force-mute an agent that legitimately transmits.
{
  const src = readPrepareSource();
  const start = src.indexOf('export function startZoomOutboundAudioGuard');
  const fn = src.slice(start, src.indexOf('\n}\n', start));
  assertEqual(fn.includes('installZoomOutboundAudioLock(page, voiceAgentEnabled)'), true, 'the guard forwards voiceAgentEnabled to the lock');
  assertEqual(fn.includes('sweepZoomOutboundAudio(page, voiceAgentEnabled)'), true, 'the guard forwards voiceAgentEnabled to the sweep');
  assertEqual(fn.includes('(page, false)'), false, 'no hardcoded false remains in the guard');
}

console.log('\n=== BLOCKER 2 (timing): the lock must be armed BEFORE navigation ===');

// 62. [M36] The part of BLOCKER 2 that no unit test can execute: WHERE the lock
//     is installed. Arming it at afterAdmission was the defect — the bot joins
//     audio during preview/join, so the real mic track already exists by then,
//     and finding an existing track needs a peer registry that does not exist in
//     the default configuration. Arming at page load means no outbound audio
//     track can pre-exist the patches. This is an ordering property of join.ts,
//     so a source guard is the only available check; it is weaker than a
//     behavioural test and is not offered as equivalent.
{
  const src = readZoomWebSource('join.ts');
  assertEqual(src.includes('addInitScript(installOutboundAudioLockInPage'), true, '[M36] join.ts arms the lock via addInitScript');
  const armIdx = src.indexOf('addInitScript(installOutboundAudioLockInPage');
  const gotoIdx = src.indexOf('page.goto(');
  assertEqual(armIdx > -1 && gotoIdx > -1, true, '[M36] both the arm and the navigation are present');
  assertEqual(armIdx < gotoIdx, true, '[M36] the lock is armed BEFORE the first page.goto — otherwise the mic track predates the patches');
  // The voice agent must not be locked silent by the page-load arm either.
  assertEqual(src.includes('botConfig.voiceAgentEnabled'), true, '[M36] the page-load arm is gated on voiceAgentEnabled');
}

console.log('\n=== LAYER 2: getUserMedia patch (async) ===');

// 41. [M25] getUserMedia: a mic track acquired later is silenced at birth.
async function runAsyncTests(): Promise<void> {
  const acquired = makeTrack('audio');
  const stream = { getAudioTracks: () => [acquired.track] };
  let patchedFn: ((c: unknown) => Promise<unknown>) | null = null;
  const mediaDevices = { getUserMedia: (_c: unknown) => Promise.resolve(stream) };

  const g = globalThis as unknown as Record<string, unknown>;
  const savedNav = Object.getOwnPropertyDescriptor(g, 'navigator');
  const savedLock = Object.getOwnPropertyDescriptor(g, '__vexa_zoom_audio_lock');
  delete g.__vexa_zoom_audio_lock;
  Object.defineProperty(g, 'navigator', { value: { mediaDevices }, writable: true, configurable: true });
  try {
    const r = installOutboundAudioLockInPage({ voiceAgentEnabled: false, sealEnabled: true });
    assertEqual(r.patchedGetUserMedia, true, '[M25] getUserMedia was patched');
    patchedFn = mediaDevices.getUserMedia as (c: unknown) => Promise<unknown>;
    const returned = await patchedFn({ audio: true });
    assertEqual(returned === stream, true, '[M25] the original stream is still returned to the caller');
    assertEqual(acquired.real(), false, '[M25] ZERO-WINDOW: mic track from getUserMedia is disabled on acquisition');
    acquired.track.enabled = true;
    assertEqual(acquired.real(), false, '[M25] the acquired track is also sealed against unmuting');
  } finally {
    delete g.__vexa_zoom_audio_lock;
    if (savedLock) Object.defineProperty(g, '__vexa_zoom_audio_lock', savedLock);
    if (savedNav) Object.defineProperty(g, 'navigator', savedNav);
    else delete g.navigator;
  }

  // 41b. [M31] A getUserMedia stream can carry BOTH kinds. Only the audio track
  //      may be touched — a video track in the same stream must pass through
  //      untouched, and a stream with no audio at all must be returned intact.
  //      Guards the constraint that nothing here perturbs anything but outbound
  //      audio.
  const mixedAudio = makeTrack('audio');
  const mixedVideo = makeTrack('video');
  const mixedStream = {
    getAudioTracks: () => [mixedAudio.track],
    getVideoTracks: () => [mixedVideo.track],
  };
  const videoOnly = { getAudioTracks: () => [] };
  const savedNav2 = Object.getOwnPropertyDescriptor(g, 'navigator');
  const savedLock2 = Object.getOwnPropertyDescriptor(g, '__vexa_zoom_audio_lock');
  delete g.__vexa_zoom_audio_lock;
  let handed: unknown = null;
  const md2 = { getUserMedia: (_c: unknown) => Promise.resolve(handed) };
  Object.defineProperty(g, 'navigator', { value: { mediaDevices: md2 }, writable: true, configurable: true });
  try {
    installOutboundAudioLockInPage({ voiceAgentEnabled: false, sealEnabled: true });
    const fn2 = md2.getUserMedia as (c: unknown) => Promise<unknown>;

    handed = mixedStream;
    const outMixed = await fn2({ audio: true, video: true });
    assertEqual(mixedAudio.real(), false, '[M31] audio track in a mixed stream is disabled');
    assertEqual(mixedVideo.real(), true, '[M31] VIDEO track in the same stream is left untouched');
    assertEqual(outMixed === mixedStream, true, '[M31] the mixed stream is returned intact');

    handed = videoOnly;
    const outVideoOnly = await fn2({ video: true });
    assertEqual(outVideoOnly === videoOnly, true, '[M31] a stream with no audio tracks passes through unchanged');
  } finally {
    delete g.__vexa_zoom_audio_lock;
    if (savedLock) Object.defineProperty(g, '__vexa_zoom_audio_lock', savedLock);
    if (savedNav) Object.defineProperty(g, 'navigator', savedNav);
    else delete g.navigator;
  }
}

runAsyncTests()
  .catch((e) => {
    failed++;
    console.log(`  ❌ async suite threw: ${e?.message ?? e}`);
  })
  .then(() => {
    console.log(`\n${failed === 0 ? '✅' : '❌'} mute-guarantee: ${passed} passed, ${failed} failed\n`);
    process.exit(failed === 0 ? 0 : 1);
  });
