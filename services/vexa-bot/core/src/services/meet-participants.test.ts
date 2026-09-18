/**
 * Meet participant-identity rules — pure, no DOM, no playwright.
 *
 * Run: npx tsx src/services/meet-participants.test.ts
 *
 * WHY THIS EXISTS
 *
 * `countParticipantTiles()` counted DISTINCT `data-participant-id` values and
 * nothing else, so anything Meet gave an id to counted as a person. Two things
 * that are not people therefore disarmed the startup-alone timer, which is what
 * `meetingHasStarted` gates:
 *
 *   · "Backgrounds and effects" -- Meet's own visual-effects control. It carries
 *     a participant id (observed live 2026-09-18 as devices/107) and had already
 *     been seen reaching the TRANSCRIPT and participant list on 2026-09-03.
 *   · "read.ai meeting notes" -- another vendor's notetaker bot. Observed live
 *     2026-09-18: the participant count went 1 -> 2 four log lines after it
 *     appeared, flipping meetingHasStarted and disarming the 15-minute timer on
 *     a meeting NO HUMAN ever joined.
 *
 * A resolvable display name is therefore NOT sufficient: "read.ai meeting notes"
 * is a perfectly good name. Bots must be named and excluded.
 */
import {
  DEFAULT_MEET_BOT_DENYLIST,
  MEET_UI_LABEL_PATTERNS,
  isRealParticipantName,
  parseBotDenylist,
} from "./meet-participants";

let pass = 0;
let fail = 0;
function check(label: string, actual: unknown, expected: unknown): void {
  if (JSON.stringify(actual) === JSON.stringify(expected)) {
    pass++; console.log(`  ✅ ${label}`);
  } else {
    fail++; console.log(`  ❌ ${label}\n     expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
  }
}
const real = (n: string, deny = DEFAULT_MEET_BOT_DENYLIST) =>
  isRealParticipantName(n, MEET_UI_LABEL_PATTERNS, deny);

console.log("\n— real people are kept —");
check("an ordinary name", real("Kishan D"), true);
check("a single first name", real("Sujoy"), true);
check("a name shown as an email", real("kishan.d@voyantcs.com"), true);
check("our own bot counts as a participant", real("AW Notetaker"), true);

console.log("\n— Meet UI chrome is rejected —");
check("Backgrounds and effects", real("Backgrounds and effects"), false);
check("case-insensitive", real("BACKGROUNDS AND EFFECTS"), false);
check("as a substring of a scraped label", real("Apply visual effects to your video"), false);
check("Turn on captions", real("Turn on captions"), false);

console.log("\n— structural junk is rejected —");
check("the unresolved sentinel", real("Google Participant (devices/107)"), false);
check("a raw device path", real("spaces/SKeO1WYei7MB/devices/108"), false);
check("empty", real(""), false);
check("whitespace only", real("   "), false);

console.log("\n— other vendors' bots are rejected —");
check("read.ai meeting notes", real("read.ai meeting notes"), false);
check("Otter.ai", real("Otter.ai"), false);
check("Fireflies.ai Notetaker", real("Fireflies.ai Notetaker"), false);
check("Fathom Notetaker", real("Fathom Notetaker"), false);

console.log("\n— the denylist is configurable —");
check("an added bot is rejected", real("Acme Scribe", parseBotDenylist("acme scribe")), false);
check("a default bot survives a custom list (list REPLACES)", real("read.ai meeting notes", parseBotDenylist("acme scribe")), true);
check("blank env keeps the defaults", parseBotDenylist(""), DEFAULT_MEET_BOT_DENYLIST);
check("undefined env keeps the defaults", parseBotDenylist(undefined), DEFAULT_MEET_BOT_DENYLIST);
check("commas split, case/space normalised", parseBotDenylist(" Acme Scribe , OTTER "), ["acme scribe", "otter"]);
check("empty entries are dropped", parseBotDenylist("acme,,  ,otter"), ["acme", "otter"]);

console.log(`\n${pass} passed, ${fail} failed`);
if (fail > 0) process.exit(1);
