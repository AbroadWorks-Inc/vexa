/**
 * jitsi-capture L2 — the PURE state logic, no browser. Drives the real
 * createJitsiSpeakers / createJitsiChat against a FAKE `APP.store` (the redux
 * primary source), and pins the send path + the exported selector arrays (the
 * DOM-fallback surface). The DOM observers themselves are fallback-only and
 * live-validated. Run: npm test  or  npx tsx src/jitsi-capture.test.ts
 */
import {
  createJitsiSpeakers,
  createJitsiChat,
  sendJitsiChatMessage,
  selectJitsiSpeaker,
  JITSI_SPEECH_LEVEL,
  CONFIRM_POLLS,
  jitsiDominantTileSelectors,
  jitsiTileNameSelectors,
  jitsiChatContainerSelectors,
  jitsiChatMessageSelectors,
  jitsiChatSenderSelectors,
  jitsiChatTextSelectors,
} from "./index.js";

let failed = 0;
const check = (name: string, cond: boolean, detail = "") => {
  console.log(`  ${cond ? "✅" : "❌"} ${name}${cond ? "" : "  — " + detail}`);
  if (!cond) failed++;
};
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

// ── Fake APP.store — the shape createJitsiSpeakers/Chat defensively read ──────
type Participant = { id: string; name: string };
const fakeState: any = {
  "features/base/participants": {
    local: { id: "self1", name: "Vexa" } as Participant,
    remote: new Map<string, Participant>([
      ["p1", { id: "p1", name: "Alice" }],
      ["p2", { id: "p2", name: "Bob" }],
    ]),
    dominantSpeaker: undefined as string | undefined,
  },
  "features/chat": { messages: [] as any[] },
};
(globalThis as any).APP = {
  store: { getState: () => fakeState },
  conference: { _room: { sendTextMessage: (t: string) => sent.push(t) } },
};
const sent: string[] = [];

async function main() {
  // ── selector: loudest named remote above the threshold ───────────────────────
  const people = new Map<string, { id: string; name: string }>([
    ["p1", { id: "p1", name: "Alice" }],
    ["p2", { id: "p2", name: "Bob" }],
    ["p3", { id: "p3", name: "Cara" }],
  ]);
  check("threshold constant is 0.1", JITSI_SPEECH_LEVEL === 0.1, String(JITSI_SPEECH_LEVEL));
  check("a change holds for two polls", CONFIRM_POLLS === 2, String(CONFIRM_POLLS));

  const alice = selectJitsiSpeaker(
    [{ id: "p1", level: 0.4 }, { id: "p2", level: 0.05 }],
    people,
    "self1",
  );
  check("louder remote above threshold wins", alice?.name === "Alice" && alice.id === "p1", JSON.stringify(alice));

  const again = selectJitsiSpeaker([{ id: "p1", level: 0.55 }], people, "self1");
  check("same participant id keeps Alice after a new sample", again?.name === "Alice", JSON.stringify(again));

  const quiet = selectJitsiSpeaker(
    [{ id: "p1", level: 0.09 }, { id: "p2", level: 0.0 }],
    people,
    "self1",
  );
  check("everyone under the threshold yields null", quiet === null, JSON.stringify(quiet));

  const selfLoud = selectJitsiSpeaker(
    [{ id: "self1", level: 0.9 }, { id: "p2", level: 0.2 }],
    people,
    "self1",
  );
  check("the bot's own level is ignored", selfLoud?.name === "Bob", JSON.stringify(selfLoud));

  const unknown = selectJitsiSpeaker(
    [{ id: "mixed-placeholder", level: 0.8 }, { id: "p3", level: 0.3 }],
    people,
    "self1",
  );
  check("an id with no name is skipped", unknown?.name === "Cara", JSON.stringify(unknown));

  const tied = selectJitsiSpeaker(
    [{ id: "p1", level: 0.4 }, { id: "p2", level: 0.4 }],
    people,
    "self1",
  );
  check("an exact level tie yields null", tied === null, JSON.stringify(tied));

  // ── speakers: dominant transitions → debut/end hint pairs ────────────────────
  const events: Array<{ name: string; isEnd: boolean; source?: string }> = [];
  const speakers = createJitsiSpeakers({
    selfName: "Vexa",
    pollMs: 10,
    heartbeatMs: 30,
    onSpeaking: (name, _id, isEnd, _tMs, source) => events.push({ name, isEnd, source }),
  });

  fakeState["features/base/participants"].dominantSpeaker = "p1";
  await sleep(40);
  check("speaker start emitted for Alice", events.some((e) => e.name === "Alice" && !e.isEnd), JSON.stringify(events));
  check("dominant hints are marked dominant", events.some((e) => e.name === "Alice" && !e.isEnd && e.source === "dominant"), JSON.stringify(events));

  // A STILL-dominant speaker keeps being re-asserted (the binder's heartbeat contract):
  // an open hint turn decays after a grace, so an unchanged speaker must re-emit.
  const assertsBefore = events.filter((e) => e.name === "Alice" && !e.isEnd).length;
  await sleep(100);
  const assertsAfter = events.filter((e) => e.name === "Alice" && !e.isEnd).length;
  check("still-dominant speaker heartbeats", assertsAfter > assertsBefore, `${assertsBefore} → ${assertsAfter}`);

  fakeState["features/base/participants"].dominantSpeaker = "p2";
  await sleep(40);
  check("Alice ended when Bob took over", events.some((e) => e.name === "Alice" && e.isEnd), JSON.stringify(events));
  check("Bob start emitted", events.some((e) => e.name === "Bob" && !e.isEnd), JSON.stringify(events));

  // The bot's own dominant-speaker state is never reported (and ends the previous).
  events.length = 0;
  fakeState["features/base/participants"].dominantSpeaker = "self1";
  await sleep(40);
  check("self (bot) never reported as a speaker", !events.some((e) => e.name === "Vexa"), JSON.stringify(events));
  check("previous speaker ended on self takeover", events.some((e) => e.name === "Bob" && e.isEnd), JSON.stringify(events));

  check("speakers mode = redux", speakers.getState().mode === "redux", speakers.getState().mode ?? "null");
  check("dominant path reports source dominant", speakers.getState().source === "dominant", String(speakers.getState().source));
  speakers.destroy();

  // ── levels: two-poll hold, flicker dropped, silence ends the turn ────────────
  const levelEvents: Array<{ name: string; isEnd: boolean; source?: string }> = [];
  let levelSamples: Array<{ id: string; level: number }> = [{ id: "p1", level: 0.5 }];
  let levelPolls = 0;
  const byLevel = createJitsiSpeakers({
    selfName: "Vexa",
    pollMs: 10,
    heartbeatMs: 1000,
    readLevels: () => { levelPolls++; return levelSamples; },
    onSpeaking: (name, _id, isEnd, _tMs, source) => levelEvents.push({ name, isEnd, source }),
  });
  const waitPolls = async (n: number) => {
    const start = levelPolls;
    const deadline = Date.now() + 1000;
    while (levelPolls < start + n) {
      if (Date.now() > deadline) throw new Error(`timed out waiting for ${n} polls (saw ${levelPolls - start})`);
      await sleep(5);
    }
  };

  await waitPolls(2);
  check("level names Alice without dominantSpeaker", levelEvents.some((e) => e.name === "Alice" && !e.isEnd), JSON.stringify(levelEvents));
  check("level hints are marked levels", levelEvents.some((e) => e.name === "Alice" && e.source === "levels"), JSON.stringify(levelEvents));
  check("source is levels", byLevel.getState().source === "levels", String(byLevel.getState().source));

  levelEvents.length = 0;
  levelSamples = [{ id: "p2", level: 0.6 }];
  await waitPolls(1);
  levelSamples = [{ id: "p1", level: 0.5 }];
  await waitPolls(2);
  check("single-poll flicker emits no hint", levelEvents.length === 0, JSON.stringify(levelEvents));

  levelEvents.length = 0;
  for (let i = 0; i < 6; i++) {
    levelSamples = [{ id: i % 2 === 0 ? "p1" : "p2", level: 0.5 }];
    await waitPolls(1);
  }
  check("alternating polls emit nothing", levelEvents.length === 0, JSON.stringify(levelEvents));

  levelSamples = [{ id: "p1", level: 0.5 }];
  await waitPolls(2);
  levelEvents.length = 0;
  levelSamples = [{ id: "p1", level: 0.05 }];
  await waitPolls(1);
  check("one quiet poll does not end Alice", levelEvents.length === 0, JSON.stringify(levelEvents));
  await waitPolls(1);
  check(
    "two quiet polls end Alice and name nobody",
    levelEvents.length === 1 && levelEvents[0].name === "Alice" && levelEvents[0].isEnd && levelEvents[0].source === "levels",
    JSON.stringify(levelEvents),
  );

  levelEvents.length = 0;
  fakeState["features/base/participants"].remote.set("p3", { id: "p3", name: "Cara" });
  levelSamples = [{ id: "no-such-person", level: 0.9 }, { id: "p3", level: 0.4 }];
  await waitPolls(2);
  check(
    "unknown id is ignored and Cara starts",
    levelEvents.some((e) => e.name === "Cara" && !e.isEnd && e.source === "levels") && !levelEvents.some((e) => e.name === "no-such-person"),
    JSON.stringify(levelEvents),
  );

  byLevel.destroy();
  fakeState["features/base/participants"].remote.delete("p3");
  fakeState["features/base/participants"].dominantSpeaker = undefined;

  // ── real level reader: no injected readLevels. A mute stops Jitsi's events,
  // so a cached loud value must not keep the turn. ─────────────────────────────
  const realNow = Date.now;
  let now = realNow();
  Date.now = () => now;
  const waitFor = async (cond: () => boolean) => {
    const start = realNow();
    while (!cond()) {
      if (realNow() - start > 500) return false;
      await sleep(5);
    }
    return true;
  };
  const fakeTrack = () => {
    const listeners = new Set<(level: number) => void>();
    let offs = 0;
    return {
      on(event: string, cb: (level: number) => void) {
        if (event === "track.audioLevelsChanged") listeners.add(cb);
      },
      off(event: string, cb: (level: number) => void) {
        if (event === "track.audioLevelsChanged") {
          offs++;
          listeners.delete(cb);
        }
      },
      emit(level: number) {
        for (const cb of [...listeners]) cb(level);
      },
      get offs() { return offs; },
    };
  };
  const aliceTrack = fakeTrack();
  const bobTrack = fakeTrack();
  const aliceEntry: any = {
    participantId: "p1", mediaType: "audio", local: false, muted: false, jitsiTrack: aliceTrack,
  };
  const bobEntry: any = {
    participantId: "p2", mediaType: "audio", local: false, muted: false, jitsiTrack: bobTrack,
  };
  fakeState["features/base/tracks"] = [aliceEntry, bobEntry];
  const trackEvents: Array<{ name: string; isEnd: boolean; source?: string }> = [];
  const fromTracks = createJitsiSpeakers({
    selfName: "Vexa",
    pollMs: 10,
    heartbeatMs: 60_000,
    onSpeaking: (name, _id, isEnd, _tMs, source) => trackEvents.push({ name, isEnd, source }),
  });
  try {
    aliceTrack.emit(0.5);
    check(
      "a level event names the speaker after the hold",
      await waitFor(() => trackEvents.some((e) => e.name === "Alice" && !e.isEnd && e.source === "levels")),
      JSON.stringify(trackEvents),
    );

    const freshCount = trackEvents.length;
    now += 1000;
    await sleep(40);
    check(
      "a level 1s old still names Alice",
      fromTracks.getState().current === "Alice" && trackEvents.length === freshCount,
      JSON.stringify(trackEvents),
    );
    now += 1;
    check(
      "an entry whose last event is older than 1s stops winning",
      await waitFor(() => fromTracks.getState().current === null && trackEvents.some((e) => e.name === "Alice" && e.isEnd)),
      `current=${String(fromTracks.getState().current)} ${JSON.stringify(trackEvents)}`,
    );

    aliceTrack.emit(0.9);
    bobTrack.emit(0.3);
    check(
      "Alice's fresh level beats Bob",
      await waitFor(() => fromTracks.getState().current === "Alice"),
      JSON.stringify(trackEvents),
    );
    trackEvents.length = 0;
    aliceEntry.muted = true;
    check(
      "a muted track does not win",
      await waitFor(() =>
        fromTracks.getState().current === "Bob"
        && trackEvents.some((e) => e.name === "Alice" && e.isEnd && e.source === "levels")
        && trackEvents.some((e) => e.name === "Bob" && !e.isEnd && e.source === "levels")
        && !trackEvents.some((e) => e.name === "Alice" && !e.isEnd)),
      JSON.stringify(trackEvents),
    );

    aliceEntry.muted = false;
    aliceTrack.emit(0.95);
    check(
      "Alice wins again from a new level",
      await waitFor(() => fromTracks.getState().current === "Alice"),
      JSON.stringify(trackEvents),
    );
    const aliceOffs = aliceTrack.offs;
    trackEvents.length = 0;
    fakeState["features/base/tracks"] = [bobEntry];
    check(
      "removing a track calls off",
      await waitFor(() => aliceTrack.offs === aliceOffs + 1),
      `offs ${aliceTrack.offs}`,
    );
    check(
      "a removed track stops winning",
      await waitFor(() => trackEvents.some((e) => e.name === "Alice" && e.isEnd) && fromTracks.getState().current === "Bob"),
      JSON.stringify(trackEvents),
    );
    trackEvents.length = 0;
    fakeState["features/base/tracks"] = [aliceEntry, bobEntry];
    await sleep(60);
    check(
      "removing a track clears its cache",
      !trackEvents.some((e) => e.name === "Alice") && fromTracks.getState().current === "Bob",
      JSON.stringify(trackEvents),
    );

    const aliceBeforeDestroy = aliceTrack.offs;
    const bobBeforeDestroy = bobTrack.offs;
    fromTracks.destroy();
    check(
      "destroy() calls off on every bound track",
      aliceTrack.offs === aliceBeforeDestroy + 1 && bobTrack.offs === bobBeforeDestroy + 1,
      `alice ${aliceTrack.offs} bob ${bobTrack.offs}`,
    );
  } finally {
    fromTracks.destroy();
    Date.now = realNow;
    delete fakeState["features/base/tracks"];
    fakeState["features/base/participants"].dominantSpeaker = undefined;
  }

  // ── chat: history primes silently; new messages emit once ────────────────────
  fakeState["features/chat"].messages = [
    { id: "m1", displayName: "Alice", message: "hello from before the bot joined", messageType: "remote", timestamp: 1 },
  ];
  const got: Array<{ sender: string; text: string }> = [];
  const chat = createJitsiChat({ pollMs: 10, onMessage: (m) => got.push(m) });
  await sleep(40);
  check("pre-join history is primed, not emitted", got.length === 0, JSON.stringify(got));

  fakeState["features/chat"].messages = [
    ...fakeState["features/chat"].messages,
    { id: "m2", displayName: "Bob", message: "agenda is in the doc", messageType: "remote", timestamp: 2 },
    { id: "m3", displayName: "", message: "anonymous ping", messageType: "remote", timestamp: 3 },
    { id: "m4", displayName: "Eve", message: "boom", messageType: "error", timestamp: 4 },
    { id: "m5", displayName: "Vexa", message: "sent by the bot itself", messageType: "local", timestamp: 5 },
  ];
  await sleep(40);
  check("new message emitted", got.some((m) => m.sender === "Bob" && m.text === "agenda is in the doc"), JSON.stringify(got));
  check("missing displayName → Unknown", got.some((m) => m.sender === "Unknown" && m.text === "anonymous ping"), JSON.stringify(got));
  check("error-type messages filtered", !got.some((m) => m.text === "boom"), JSON.stringify(got));
  check("the bot's own (local) messages never echo back", !got.some((m) => m.text === "sent by the bot itself"), JSON.stringify(got));

  const before = got.length;
  await sleep(30);
  check("no duplicate emissions on re-poll", got.length === before, `${got.length} vs ${before}`);

  // ── store replacement (reconnect / p2p↔JVB move / history cap): the array shrinks to a
  // retained tail — already-delivered messages must NOT re-emit, and new ones still do. ──
  fakeState["features/chat"].messages = [
    { id: "m2", displayName: "Bob", message: "agenda is in the doc", messageType: "remote", timestamp: 2 },
    { id: "m5", displayName: "Vexa", message: "sent by the bot itself", messageType: "local", timestamp: 5 },
  ];
  await sleep(30);
  check("store replacement re-emits nothing", got.length === before, `${got.length} vs ${before}`);
  fakeState["features/chat"].messages = [
    ...fakeState["features/chat"].messages,
    { id: "m6", displayName: "Carol", message: "fresh after the resync", messageType: "remote", timestamp: 6 },
  ];
  await sleep(30);
  check(
    "post-resync message emits once",
    got.filter((m) => m.text === "fresh after the resync").length === 1,
    JSON.stringify(got),
  );

  check("chat mode = redux", chat.getState().mode === "redux", chat.getState().mode ?? "null");
  chat.destroy();

  // ── send path ────────────────────────────────────────────────────────────────
  check("sendJitsiChatMessage uses the conference API", sendJitsiChatMessage("hi room") === true && sent[0] === "hi room", JSON.stringify(sent));
  delete (globalThis as any).APP.conference;
  check("send returns false when the API is absent", sendJitsiChatMessage("nope") === false);

  // ── DOM-fallback selector surface is exported + non-empty ────────────────────
  for (const [name, arr] of Object.entries({
    jitsiDominantTileSelectors, jitsiTileNameSelectors,
    jitsiChatContainerSelectors, jitsiChatMessageSelectors,
    jitsiChatSenderSelectors, jitsiChatTextSelectors,
  })) {
    check(`${name} exported non-empty`, Array.isArray(arr) && arr.length > 0);
  }

  if (failed) { console.error(`\n❌ jitsi-capture (L2): ${failed} check(s) FAILED.`); process.exit(1); }
  console.log("\n✅ jitsi-capture (L2): speakers + chat drive the fake APP.store correctly; send path + selector surface pinned.");
}

void main();
