/**
 * Google Meet re-knock — asking again when the join request expires.
 *
 * THE BUG (measured 2026-11-16, meetings 1515 and 1516):
 * Google keeps a guest's join request visible for about 65 seconds.
 *
 *   1515 dac77447  clicked 09:11:23 → waiting room gone 09:12:31 (+65 s)
 *   1516 f3910f87  clicked 09:20:42 → waiting room gone 09:21:51 (+66 s)
 *
 * The bot treats that as final: after a 20 s grace it throws `lobby_timeout` and
 * exits. Meeting 1516's bot was gone by 14:52:44; the host was due at 14:54. The
 * one-minute window in which the request existed fell entirely before anyone was
 * there to answer it.
 *
 * WHAT THIS FILE PINS
 *   1. Admitted on the first ask → one join, no re-knock.
 *   2. An EXPIRED knock is re-asked; admission on a later ask still succeeds.
 *   3. Only an expired knock is retried. A denial, an auth failure or any other
 *      error propagates immediately — re-asking after a host said no would be
 *      pestering them, and after an auth failure it can never succeed.
 *   4. The TOTAL budget is honoured across all asks, not per ask. Each admission
 *      wait is handed only the time that remains.
 *   5. A knock cap bounds the loop even when budget remains.
 *   6. The last error is what surfaces — never a synthesised one.
 *
 * Deterministic: injected clock, no real timers, no browser, no Google.
 *
 * Run: npx tsx src/googlemeet/reknock.test.ts
 */

import { joinWithReknock } from './reknock';

let failures = 0;
function assert(cond: boolean, msg: string): void {
  if (cond) {
    console.log(`  ok   ${msg}`);
  } else {
    failures++;
    console.error(`  FAIL ${msg}`);
  }
}

class FakeExpiredKnock extends Error {}
class FakeDenial extends Error {}

function harness(opts: {
  outcomes: Array<'admit' | 'expire' | 'deny'>;
  totalBudgetMs?: number;
  maxKnocks?: number;
  costPerAttemptMs?: number;
}) {
  let joins = 0;
  let waits = 0;
  let clock = 0;
  const remainingSeen: number[] = [];
  const logs: string[] = [];
  const cost = opts.costPerAttemptMs ?? 1000;

  return {
    joins: () => joins,
    waits: () => waits,
    remainingSeen: () => remainingSeen,
    logs: () => logs,
    run: () =>
      joinWithReknock(
        {
          join: async () => { joins++; clock += cost; },
          waitForAdmission: async (remainingMs: number) => {
            remainingSeen.push(remainingMs);
            const outcome = opts.outcomes[waits] ?? 'expire';
            waits++;
            clock += cost;
            if (outcome === 'admit') return true;
            if (outcome === 'deny') throw new FakeDenial('host said no');
            throw new FakeExpiredKnock('knock expired');
          },
          isExpiredKnock: (e: unknown) => e instanceof FakeExpiredKnock,
          now: () => clock,
          log: (m: string) => { logs.push(m); },
        },
        {
          totalBudgetMs: opts.totalBudgetMs ?? 600000,
          maxKnocks: opts.maxKnocks ?? 5,
        }
      ),
  };
}

async function run(): Promise<void> {
  console.log('joinWithReknock — asks again when the request expires');

  // 1. Straight through.
  {
    const h = harness({ outcomes: ['admit'] });
    const admitted = await h.run();
    assert(admitted === true, 'admitted on the first ask');
    assert(h.joins() === 1, 'joined exactly once when admitted immediately');
  }

  // 2. Expired then admitted — the whole point of the change.
  {
    const h = harness({ outcomes: ['expire', 'expire', 'admit'] });
    const admitted = await h.run();
    assert(admitted === true, 'admitted on the third ask after two expiries');
    assert(h.joins() === 3, `re-knocked twice (joins=${h.joins()})`);
    assert(
      h.logs().some((l) => /re-knock/i.test(l)),
      'says out loud that it is re-knocking'
    );
  }

  // 3. A denial must NOT be retried.
  {
    const h = harness({ outcomes: ['deny', 'admit'] });
    let threw: unknown = null;
    try { await h.run(); } catch (e) { threw = e; }
    assert(threw instanceof FakeDenial, 'a denial propagates instead of re-knocking');
    assert(h.joins() === 1, 'did not re-knock after a denial');
  }

  // 4. Total budget honoured across asks, and each wait gets only what is left.
  {
    const h = harness({ outcomes: ['expire', 'expire', 'expire'], totalBudgetMs: 10000, costPerAttemptMs: 2000 });
    try { await h.run(); } catch { /* expected */ }
    const seen = h.remainingSeen();
    assert(seen.length >= 2, `made more than one ask (${seen.length})`);
    assert(
      seen.every((v, i) => i === 0 || v < seen[i - 1]),
      `remaining budget shrinks on each ask (${seen.join(' → ')})`
    );
    assert(seen.every((v) => v > 0), 'never asks with a non-positive budget');
  }

  // 5. Knock cap bounds the loop even with budget to spare.
  {
    const h = harness({ outcomes: ['expire', 'expire', 'expire', 'expire', 'expire', 'expire'], maxKnocks: 3, totalBudgetMs: 10_000_000 });
    let threw: unknown = null;
    try { await h.run(); } catch (e) { threw = e; }
    assert(threw instanceof FakeExpiredKnock, 'surfaces the real expiry error, not a synthesised one');
    assert(h.joins() === 3, `stopped at the knock cap (joins=${h.joins()})`);
  }

  // 6. Budget exhaustion stops the loop before the cap.
  {
    const h = harness({ outcomes: ['expire', 'expire', 'expire', 'expire'], totalBudgetMs: 5000, costPerAttemptMs: 2000, maxKnocks: 99 });
    let threw: unknown = null;
    try { await h.run(); } catch (e) { threw = e; }
    assert(threw instanceof FakeExpiredKnock, 'surfaces the expiry error when the budget runs out');
    assert(h.joins() < 99, `budget stopped the loop well before the cap (joins=${h.joins()})`);
  }
}

run().then(() => {
  console.log(failures === 0 ? '\nPASS' : `\nFAIL — ${failures} assertion(s)`);
  process.exit(failures === 0 ? 0 : 1);
});
