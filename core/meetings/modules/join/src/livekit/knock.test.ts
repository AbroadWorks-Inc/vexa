/**
 * The knock client against Meet's recorded waiting-room answers (fixtures/), on a fake clock.
 * No network: `fetch` serves the fixtures in the order a test scripts them.
 *
 * Run: npx tsx src/livekit/knock.test.ts
 */
import { readFileSync } from "fs";
import { join } from "path";
import { knockUntilAdmitted, parseLivekitUrl, type KnockOptions } from "./knock";
import { AdmissionError } from "../shared/admission";

let passed = 0;
let failed = 0;
function check(name: string, ok: boolean, detail?: string) {
  if (ok) { console.log(`  \x1b[32mPASS\x1b[0m  ${name}`); passed++; }
  else { console.log(`  \x1b[31mFAIL\x1b[0m  ${name}${detail ? ` — ${detail}` : ""}`); failed++; }
}

const fixture = (name: string) => readFileSync(join(__dirname, "fixtures", `${name}.json`), "utf8");
type Step = "waiting" | "accepted" | "denied" | "404" | "429" | "500" | "neterr" | "hang" | "html" | "badurl";

function harness(steps: Step[], over: Partial<KnockOptions> = {}) {
  let t = 0;
  const sleeps: number[] = [];
  const bodies: Record<string, unknown>[] = [];
  const logs: string[] = [];
  let waitingCalls = 0;
  let i = 0;
  const inits: { redirect?: string }[] = [];
  const fetch = (async (_url: string, init: { body: string; redirect?: string; signal?: AbortSignal }) => {
    bodies.push(JSON.parse(init.body));
    inits.push(init);
    const step = steps[Math.min(i++, steps.length - 1)];
    if (step === "neterr") throw new TypeError("fetch failed");
    // A server that accepts the request and never answers: only the request's own signal ends it.
    if (step === "hang") {
      return new Promise<Response>((_resolve, reject) => {
        if (!init.signal) return; // no signal: hangs forever, and the test's own timer fails it
        init.signal.addEventListener("abort", () => reject(init.signal!.reason));
      });
    }
    if (step === "429") return new Response("", { status: 429 });
    if (step === "500") return new Response("", { status: 500 });
    if (step === "404") return new Response(fixture("404"), { status: 404 });
    if (step === "html") return new Response("<html>maintenance SECRET-BODY</html>", { status: 200 });
    if (step === "badurl") return new Response(fixture("accepted").replace("https://livekit.example.org", "https://user:pw@10.0.0.5:9999"), { status: 200 });
    return new Response(fixture(step), { status: 200 });
  }) as unknown as typeof globalThis.fetch;
  const opts: KnockOptions = {
    host: "meet-next.example.org", code: "abc-defg-hij", botName: "Olanote", budgetMs: 600_000,
    onWaiting: () => { waitingCalls++; },
    cancelled: () => false,
    fetch, sleep: async (ms) => { sleeps.push(ms); t += ms; }, now: () => t, log: (m) => logs.push(m),
    ...over,
  };
  return { opts, sleeps, bodies, logs, inits, waiting: () => waitingCalls, fetches: () => i };
}

async function outcome(p: Promise<unknown>): Promise<string> {
  try { await p; return "resolved"; }
  catch (e) { return e instanceof AdmissionError ? `${e.outcome}: ${e.message}` : `other: ${String(e)}`; }
}

async function main() {
  {
    const h = harness(["waiting", "waiting", "accepted"]);
    const r = await knockUntilAdmitted(h.opts);
    check("admitted: returns the LiveKit url and token", r.url === "https://livekit.example.org" && r.token === "TEST-TOKEN-NOT-REAL");
    check("first knock sends the bot's name only", JSON.stringify(h.bodies[0]) === JSON.stringify({ username: "Olanote" }), JSON.stringify(h.bodies[0]));
    check("later knocks carry the participant id", JSON.stringify(h.bodies[1]) === JSON.stringify({ username: "Olanote", participant_id: "signed-participant-id:fixture" }), JSON.stringify(h.bodies[1]));
    check("onWaiting is called once", h.waiting() === 1, String(h.waiting()));
    check("knocks every 3 s", JSON.stringify(h.sleeps) === JSON.stringify([3000, 3000]), JSON.stringify(h.sleeps));
    check("the token never reaches the log", !h.logs.join("\n").includes("TEST-TOKEN-NOT-REAL"), h.logs.join(" | "));
    check("redirects are refused, never followed", h.inits.every((x) => x.redirect === "error"), JSON.stringify(h.inits));
  }
  check("denied → denial", (await outcome(knockUntilAdmitted(harness(["waiting", "denied"]).opts))).startsWith("denial"));
  {
    const o = await outcome(knockUntilAdmitted(harness(["404"]).opts));
    check("404 → join_failure naming the status", o.startsWith("join_failure") && o.includes("404"), o);
  }
  {
    const h = harness(["waiting", "429", "accepted"]);
    await knockUntilAdmitted(h.opts);
    check("429: backs off 4 s, then continues", JSON.stringify(h.sleeps) === JSON.stringify([3000, 4000]), JSON.stringify(h.sleeps));
  }
  {
    const h = harness(["neterr", "neterr", "500", "waiting", "accepted"]);
    await knockUntilAdmitted(h.opts);
    check("network errors and 5xx back off 4, 5, 5 s (under Meet's 6 s drop) and continue", JSON.stringify(h.sleeps) === JSON.stringify([4000, 5000, 5000, 3000]), JSON.stringify(h.sleeps));
    check("the first answer that says waiting calls onWaiting", h.waiting() === 1);
  }
  {
    const h = harness(["waiting", "hang", "accepted"]);
    // The guard also keeps the process alive: AbortSignal.timeout's own timer does not.
    let timer: ReturnType<typeof setTimeout> | undefined;
    const guard = new Promise<string>((r) => { timer = setTimeout(() => r("still hanging after 10 s"), 10_000); });
    const o = await Promise.race([outcome(knockUntilAdmitted(h.opts)), guard]);
    clearTimeout(timer);
    check("a knock Meet never answers is abandoned within 3 s and backs off 4 s", o === "resolved" && JSON.stringify(h.sleeps) === JSON.stringify([3000, 4000]), `${o} sleeps=${JSON.stringify(h.sleeps)}`);
    check("the abandoned knock is logged as a timeout", h.logs.some((m) => m.includes("TimeoutError")), h.logs.join(" | "));
  }
  {
    const o = await outcome(knockUntilAdmitted(harness(["html"]).opts));
    check("a non-JSON answer → join_failure that does not quote the body", o.startsWith("join_failure") && !o.includes("SECRET-BODY"), o);
  }
  {
    const o = await outcome(knockUntilAdmitted(harness(["badurl"]).opts));
    check("a LiveKit URL with credentials or not wss/https → join_failure", o.startsWith("join_failure"), o);
  }
  {
    const h = harness(["waiting"], { budgetMs: 10_000 });
    const o = await outcome(knockUntilAdmitted(h.opts));
    check("waiting past the budget → lobby_timeout", o.startsWith("lobby_timeout"), o);
  }
  {
    const h = harness(["neterr"], { budgetMs: 20_000 });
    const o = await outcome(knockUntilAdmitted(h.opts));
    check("never reached the waiting room within the budget → join_failure", o.startsWith("join_failure"), o);
  }
  {
    let cancel = false;
    const h = harness(["waiting", "waiting", "accepted"], { cancelled: () => cancel, onWaiting: () => { cancel = true; } });
    const o = await outcome(knockUntilAdmitted(h.opts));
    check("withdrawn while waiting → stops knocking", o.startsWith("join_failure") && o.includes("withdrawn") && h.fetches() === 1, `${o} fetches=${h.fetches()}`);
  }

  const parsed = (u: string) => { try { return JSON.stringify(parseLivekitUrl(u)); } catch (e) { return e instanceof AdmissionError ? `error:${e.outcome}` : `other:${e}`; } };
  const want = JSON.stringify({ host: "meet-next.olamee.com", code: "abc-defg-hij" });
  check("parse: plain room URL", parsed("https://meet-next.olamee.com/abc-defg-hij") === want);
  check("parse: upper case and a trailing slash", parsed("https://Meet-Next.olamee.com/ABC-DEFG-HIJ/") === want);
  check("parse: a query string is ignored", parsed("https://meet-next.olamee.com/abc-defg-hij?lang=en") === want);
  check("parse: a second path segment is not a room", parsed("https://meet-next.olamee.com/abc-defg-hij/extra") === "error:join_failure");
  check("parse: not a room code", parsed("https://meet-next.olamee.com/standup") === "error:join_failure");
  check("parse: http is refused", parsed("http://meet-next.olamee.com/abc-defg-hij") === "error:join_failure");
  check("parse: a port is refused", parsed("https://meet-next.olamee.com:8443/abc-defg-hij") === "error:join_failure");

  console.log(`\nknock: ${passed} passed, ${failed} failed`);
  if (failed) process.exit(1);
}

main().catch((e) => { console.error(e); process.exit(1); });
