/**
 * Escalation never opens the live view on its own.
 *
 * A stuck lobby (80% of its budget, or 10 s of unknown page state) used to start x11vnc with no
 * password on every interface plus websockify on 6080, and add five minutes to the lobby wait, on
 * every production bot. The live view is opened by startDebugView() alone, binds to loopback, and
 * only a watched join gets the extra five minutes. The "needs human help" report is unchanged.
 *
 * `child_process.spawn` is replaced, and the platform reads as Linux with a display, so the VNC
 * path runs exactly as it would in the bot image without starting anything.
 *
 * Run: npx tsx src/shared/escalation.test.ts
 */
import { triggerEscalation, getEscalationExtensionMs, resetEscalation, startDebugView } from "./escalation";
import { setHooks } from "../_host";

let passed = 0;
let failed = 0;
function check(name: string, ok: boolean, detail?: string) {
  if (ok) { console.log(`  \x1b[32mPASS\x1b[0m  ${name}`); passed++; }
  else { console.log(`  \x1b[31mFAIL\x1b[0m  ${name}${detail ? ` — ${detail}` : ""}`); failed++; }
}

const spawned: { cmd: string; args: string[] }[] = [];
// The module object itself: escalation.ts reads `spawn` from it at call time.
require("child_process").spawn = (cmd: string, args: string[]) => {
  spawned.push({ cmd, args });
  return { once: () => {}, unref: () => {} };
};
Object.defineProperty(process, "platform", { value: "linux" });
process.env.DISPLAY = ":99";

const states: string[] = [];
setHooks({ onState: (s) => { states.push(s); } });

async function main() {
  console.log("\n=== a stuck lobby with no live view: report only ===");
  {
    resetEscalation();
    await triggerEscalation({}, "unknown_blocking_state");
    check("the host is told the join needs a human", states.includes("needs_human_help"), JSON.stringify(states));
    check("nothing is spawned (no VNC, no websockify)", spawned.length === 0, JSON.stringify(spawned));
    check("the lobby budget is not extended", getEscalationExtensionMs() === 0, String(getEscalationExtensionMs()));
  }

  console.log("\n=== the live view, when asked for, listens on loopback only ===");
  {
    resetEscalation();
    await startDebugView();
    const vnc = spawned.find((s) => s.cmd === "x11vnc");
    const ws = spawned.find((s) => s.cmd === "websockify");
    check("x11vnc is bound to localhost", !!vnc && vnc.args.includes("-localhost"), JSON.stringify(vnc));
    check("websockify listens on 127.0.0.1:6080", !!ws && ws.args.includes("127.0.0.1:6080") && !ws.args.includes("6080"),
      JSON.stringify(ws));
    await triggerEscalation({}, "unknown_blocking_state");
    check("a watched stuck lobby gets the 5-minute extension", getEscalationExtensionMs() === 5 * 60 * 1000,
      String(getEscalationExtensionMs()));
  }

  console.log(`\n${passed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
}

main().catch((e) => { console.error(e); process.exit(1); });
