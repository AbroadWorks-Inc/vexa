/**
 * meet-participants — who counts as a PERSON in a Google Meet call.
 *
 * Extracted as a pure module (no DOM, no playwright) so the rules are unit
 * testable, matching `zoom-roster.ts` and `meet-auth.ts`. The Meet participant
 * counter runs INSIDE `page.evaluate`, which cannot import, so these values are
 * passed into the page as data alongside `selectors` -- the same way the
 * selector lists already travel. That keeps ONE definition instead of a second
 * copy inlined in the page code, which is how the two lists would drift.
 *
 * WHY: `countParticipantTiles()` counted distinct `data-participant-id` values
 * and nothing else, so anything Meet gave an id to counted as a person. That
 * count drives `meetingHasStarted`, which permanently disarms the
 * startup-alone timer. Two non-people were observed doing exactly that:
 *
 *   · "Backgrounds and effects" -- Meet's own visual-effects control, carrying
 *     a participant id (live 2026-09-18, devices/107). It had already been seen
 *     reaching the transcript on 2026-09-03.
 *   · "read.ai meeting notes" -- another vendor's notetaker. Live 2026-09-18 the
 *     count went 1 -> 2 four lines after it appeared, disarming the 15-minute
 *     timer on a meeting no human ever joined.
 *
 * So "has a resolvable display name" is NOT enough: read.ai's name is perfectly
 * resolvable. Bots have to be named and excluded.
 */

/**
 * Google Meet CHROME that the tile scraper can read as a participant name.
 *
 * Matched as a lower-cased SUBSTRING, because these strings arrive wrapped in
 * surrounding text ("Apply visual effects to your video"). Every entry is
 * multi-word Meet chrome, so substring matching cannot collide with a real
 * display name. Only add control/menu/panel strings here -- never a person's
 * name, and never a single common word.
 *
 * Kept in step with the same list in `speaker-identity.ts`, which filters the
 * SPEAKER path; this one filters the COUNT path.
 */
export const MEET_UI_LABEL_PATTERNS: readonly string[] = [
  'let participants', 'send messages', 'turn on captions', 'turn off captions',
  'backgrounds and effects', 'visual effects', 'apply visual effects',
  'more options', 'raise hand', 'lower hand', 'present now', 'stop presenting',
  'turn on microphone', 'turn off microphone', 'turn on camera', 'turn off camera',
  'leave call', 'meeting details', 'chat with everyone', 'add people',
  'call controls', 'host controls',
];

/**
 * Other vendors' meeting bots, lower-cased, matched as a SUBSTRING so version
 * or suffix changes ("read.ai meeting notes", "Read.ai Notetaker") still match.
 *
 * OUR OWN BOT IS DELIBERATELY ABSENT from this list, but NOT because we are
 * counted: the caller now excludes our display name by value and requires
 * `others >= 1`.
 *
 * The earlier note here claimed the bot "holds its own tile and its own display
 * name" and that the `> 1` threshold counted it as #1. That was FALSE and it cost
 * a live meeting: on 2026-09-20 (meet-bot-8d9a850180e44013) the roster resolved
 * the human 84 times and our own name ZERO times, so a 1:1 meeting sat at 1 and
 * the gate never armed. Do not reintroduce a threshold that assumes we are in
 * the roster -- whether Meet renders a self-tile is not ours to control.
 */
export const DEFAULT_MEET_BOT_DENYLIST: readonly string[] = [
  'read.ai',
  'otter',
  'fireflies',
  'fathom',
  'avoma',
  'grain',
  'sembly',
  'tl;dv',
  'tldv',
  'notetaker bot',
  'meeting notes bot',
];

/**
 * Read the operator-configurable denylist.
 *
 * REPLACES the defaults rather than extending them, so an operator can remove an
 * entry as well as add one -- the owner asked to be able to do both. Unset or
 * blank therefore means "use the defaults", not "no denylist at all": an empty
 * list would silently let every vendor bot back in.
 */
export function parseBotDenylist(raw: string | undefined): readonly string[] {
  if (!raw || !raw.trim()) return DEFAULT_MEET_BOT_DENYLIST;
  const entries = raw
    .split(',')
    .map((s) => s.trim().toLowerCase())
    .filter((s) => s.length > 0);
  return entries.length > 0 ? entries : DEFAULT_MEET_BOT_DENYLIST;
}

/** Structural junk `getGoogleParticipantName` emits when it cannot resolve a name. */
function isStructuralJunk(name: string): boolean {
  return /^Google Participant \(/.test(name) || /spaces\//.test(name) || /devices\//.test(name);
}

/**
 * Does this resolved tile name belong to a real participant?
 *
 * A name containing `@` is accepted without further checks beyond junk/UI/bot:
 * Meet renders a bare email address for people outside the directory, and that
 * is a person.
 */
export function isRealParticipantName(
  name: string,
  uiLabelPatterns: readonly string[],
  botDenylist: readonly string[],
): boolean {
  const trimmed = (name || '').trim();
  if (!trimmed) return false;
  if (isStructuralJunk(trimmed)) return false;

  const lower = trimmed.toLowerCase();
  if (uiLabelPatterns.some((p) => lower.includes(p))) return false;
  if (botDenylist.some((b) => lower.includes(b))) return false;
  return true;
}
