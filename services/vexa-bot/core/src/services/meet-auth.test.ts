/**
 * meet-auth — decide whether the Meet bot loads its saved Google session and
 * joins authenticated, or drops it and joins anonymously as a guest.
 *
 * Run: npx tsx services/vexa-bot/core/src/services/meet-auth.test.ts
 *
 * Phase 1 "force guest": Google revokes the saved storage_state roughly every
 * 14 days, taking the Meet bot down. When the dispatcher marks a job
 * joinMode="guest", the bot must join anonymously EVEN IF a valid session
 * secret is present — that is the whole point of this ticket. Zoom/Teams
 * already join as guests; this decision is Meet-only.
 */

import { decideMeetStorageState } from './meet-auth';

let passed = 0;
let failed = 0;

function check(label: string, actual: unknown, expected: unknown): void {
  if (JSON.stringify(actual) === JSON.stringify(expected)) {
    passed++;
    console.log(`  ✅ ${label}`);
  } else {
    failed++;
    console.log(
      `  ❌ ${label}\n       expected: ${JSON.stringify(expected)}\n       actual:   ${JSON.stringify(actual)}`,
    );
  }
}

// A syntactically valid Playwright storage_state — the two accepted seeding
// formats. NOT a real secret; empty cookie/origin arrays.
const VALID_JSON = '{"cookies":[],"origins":[]}';
const VALID_B64 = Buffer.from(VALID_JSON, 'utf8').toString('base64');

function run(): void {
  console.log('\n=== meet-auth ===');

  console.log('\nThe ticket — guest mode drops a present, valid session:');
  {
    // MAIN new behaviour: a usable storage_state is available, but joinMode is
    // "guest", so the bot MUST NOT authenticate. This is what stops the
    // 14-day-revocation outage.
    check(
      'guest + valid raw-JSON secret -> anonymous (not authenticated)',
      decideMeetStorageState('guest', VALID_JSON),
      { authenticated: false, reason: 'guest_forced' },
    );
    check(
      'guest + valid base64 secret -> anonymous (not authenticated)',
      decideMeetStorageState('guest', VALID_B64),
      { authenticated: false, reason: 'guest_forced' },
    );
    check(
      'guest + no secret at all -> anonymous',
      decideMeetStorageState('guest', undefined),
      { authenticated: false, reason: 'guest_forced' },
    );
  }

  console.log('\nControl — non-guest is the unchanged legacy path:');
  {
    // CONTROL: with joinMode NOT guest and a valid secret, the bot stays
    // authenticated exactly as before this change.
    check(
      'authenticated + valid raw-JSON secret -> authenticated, carries decoded json',
      decideMeetStorageState('authenticated', VALID_JSON),
      { authenticated: true, storageStateJson: VALID_JSON },
    );
    check(
      'authenticated + valid base64 secret -> authenticated, decodes to json',
      decideMeetStorageState('authenticated', VALID_B64),
      { authenticated: true, storageStateJson: VALID_JSON },
    );
    // Legacy jobs carry no joinMode at all (the field is new). Absent joinMode
    // must behave exactly like the old code: honour a valid secret.
    check(
      'undefined joinMode + valid secret -> authenticated (legacy default)',
      decideMeetStorageState(undefined, VALID_JSON),
      { authenticated: true, storageStateJson: VALID_JSON },
    );
  }

  console.log('\nControl — non-guest with a bad or missing secret is unchanged:');
  {
    check(
      'undefined joinMode + no secret -> anonymous (no_secret)',
      decideMeetStorageState(undefined, undefined),
      { authenticated: false, reason: 'no_secret' },
    );
    check(
      'undefined joinMode + blank secret -> anonymous (no_secret)',
      decideMeetStorageState(undefined, '   '),
      { authenticated: false, reason: 'no_secret' },
    );
    check(
      'undefined joinMode + too-short secret (<=8 chars) -> anonymous (no_secret)',
      decideMeetStorageState(undefined, 'abc'),
      { authenticated: false, reason: 'no_secret' },
    );
    check(
      'authenticated + present-but-unparseable secret -> anonymous (invalid_secret)',
      decideMeetStorageState('authenticated', 'not-valid-json-or-base64-{{{'),
      { authenticated: false, reason: 'invalid_secret' },
    );
  }

  console.log(`\n=== ${passed} passed, ${failed} failed ===\n`);
  if (failed > 0) process.exit(1);
}

run();
