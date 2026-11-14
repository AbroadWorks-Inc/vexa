/**
 * livekit-capture L2: the page logic with no browser. Drives createLivekitSpeakers on a fake clock,
 * countRoster, and createLivekitSession against a fake room that emits livekit-client's real event
 * names and reason numbers. Connecting to a real room is validated live.
 * Run: npm test  or  npx tsx src/livekit-capture.test.ts
 */
import { DisconnectReason, RoomEvent, Track } from "livekit-client";
import { countRoster, createLivekitSession, createLivekitSpeakers, type RoomLike } from "./index.js";

let failed = 0;
const check = (name: string, cond: boolean, detail = "") => {
  console.log(`  ${cond ? "✅" : "❌"} ${name}${cond ? "" : "  — " + detail}`);
  if (!cond) failed++;
};

// ── speakers: start, heartbeat, end ──
{
  let t = 1000;
  const out: string[] = [];
  const sp = createLivekitSpeakers({ onSpeaking: (n, end, tMs) => out.push(`${n}:${end ? "end" : "on"}@${tMs}`), heartbeatMs: 2000, now: () => t });
  const asha = { sid: "PA_a", name: "Asha" };
  const ben = { sid: "PA_b", name: "Ben" };
  sp.update([asha]);
  t = 2000; sp.tick();
  t = 3000; sp.tick();
  t = 3500; sp.update([asha, ben]);
  t = 4000; sp.update([ben]);
  check("speakers: start, a heartbeat after 2 s, a second speaker, an end", out.join(" ") === "Asha:on@1000 Asha:on@3000 Ben:on@3500 Asha:end@4000", out.join(" "));
  sp.destroy();
  check("speakers: destroy ends whoever is still speaking", out[out.length - 1] === "Ben:end@4000", out.join(" "));
}
{
  const out: string[] = [];
  const sp = createLivekitSpeakers({ onSpeaking: (n) => out.push(n), now: () => 0 });
  sp.update([{ sid: "PA_x", name: "" }]);
  check("speakers: a speaker with no name gives no hint", out.length === 0, out.join(","));
}
{
  const out: string[] = [];
  const sp = createLivekitSpeakers({ onSpeaking: (n, end) => out.push(`${n}:${end ? "end" : "on"}`), now: () => 0 });
  sp.update([{ sid: "PA_1", name: "Guest" }, { sid: "PA_2", name: "Guest" }]);
  sp.update([{ sid: "PA_2", name: "Guest" }]);
  check("speakers: two people with one name are two speakers", out.join(" ") === "Guest:on Guest:on Guest:end", out.join(" "));
}

// ── roster: other notetaker bots are not humans ──
{
  const r = countRoster(["Asha", "Fireflies Notetaker", ""], ["notetaker"]);
  check("roster: participants, named, humans", JSON.stringify(r) === JSON.stringify({ participants: 3, named: 2, humans: 2 }), JSON.stringify(r));
}

// ── session against a fake room ──
type Handler = (...args: unknown[]) => void;
function fakeRoom(opts: { e2ee?: boolean; sendText?: () => Promise<unknown> } = {}) {
  const handlers = new Map<string, Handler[]>();
  const subscribed: string[] = [];
  const sent: { text: string; topic?: string; to?: string[] }[] = [];
  const connects: unknown[][] = [];
  let disconnects = 0;
  const pub = (kind: string, trackId?: string, isEncrypted = false) => ({
    kind,
    isEncrypted,
    setSubscribed: (on: boolean) => { if (on) subscribed.push(`${kind}:${trackId ?? "-"}`); },
    track: trackId ? { mediaStreamTrack: { id: trackId } } : undefined,
  });
  const participant = (sid: string, name: string, identity: string, pubs: ReturnType<typeof pub>[]) => ({
    sid, name, identity, trackPublications: new Map(pubs.map((p, i) => [`${sid}-${i}`, p])),
  });
  const remoteParticipants = new Map<string, ReturnType<typeof participant>>();
  const room = {
    remoteParticipants,
    localParticipant: {
      sid: "PA_self",
      sendText: (text: string, o?: { topic?: string; destinationIdentities?: string[] }) => {
        sent.push({ text, topic: o?.topic, ...(o?.destinationIdentities ? { to: o.destinationIdentities } : {}) });
        return opts.sendText ? opts.sendText() : Promise.resolve({ id: "x" });
      },
    },
    isE2EEEnabled: !!opts.e2ee,
    on(event: string, h: Handler) { handlers.set(event, [...(handlers.get(event) ?? []), h]); return room; },
    async connect(...args: unknown[]) { connects.push(args); },
    async disconnect() { disconnects++; },
  };
  const emit = (event: string, ...args: unknown[]) => { for (const h of handlers.get(event) ?? []) h(...args); };
  return { room, emit, pub, participant, subscribed, sent, connects, disconnects: () => disconnects };
}

await (async () => {
  const f = fakeRoom();
  const present = f.participant("PA_a", "Asha", "f:1:asha@example.com", [f.pub(Track.Kind.Audio, "t1"), f.pub(Track.Kind.Video, "v1")]);
  f.room.remoteParticipants.set("PA_a", present);
  const s = createLivekitSession(f.room as unknown as RoomLike);
  const r = await s.connect("wss://lk.example", "TOKEN");
  check("connect: autoSubscribe off", JSON.stringify(f.connects[0]) === JSON.stringify(["wss://lk.example", "TOKEN", { autoSubscribe: false }]), JSON.stringify(f.connects[0]));
  check("connect: reports whether the room is encrypted", r.e2ee === false);
  check("connect: audio already in the room is subscribed, video is not", JSON.stringify(f.subscribed) === JSON.stringify(["audio:t1"]), f.subscribed.join(","));
  f.emit(RoomEvent.TrackPublished, f.pub(Track.Kind.Video, "v2"), present);
  f.emit(RoomEvent.TrackPublished, f.pub(Track.Kind.Audio, "t2"), present);
  check("published later: audio subscribed, video not", JSON.stringify(f.subscribed) === JSON.stringify(["audio:t1", "audio:t2"]), f.subscribed.join(","));

  // a subscription lands
  f.emit(RoomEvent.TrackSubscribed, { kind: Track.Kind.Audio, mediaStreamTrack: { id: "t1" } }, {}, present);
  const tracks = s.subscribedTracks();
  check("subscribedTracks: track id, sid and name", JSON.stringify(tracks) === JSON.stringify([{ trackId: "t1", sid: "PA_a", name: "Asha" }]), JSON.stringify(tracks));
  check("subscribedTracks never carries the identity", !JSON.stringify(tracks).includes("@"));
  f.emit(RoomEvent.TrackUnsubscribed, { kind: Track.Kind.Audio, mediaStreamTrack: { id: "t1" } }, {}, present);
  check("an unsubscribed track leaves the list", s.subscribedTracks().length === 0);

  // roster
  const rosters: string[] = [];
  s.onRoster(["notetaker"], (x) => rosters.push(`${x.participants}/${x.named}/${x.humans}`));
  f.room.remoteParticipants.set("PA_b", f.participant("PA_b", "Read.ai Notetaker", "u2", []));
  f.emit(RoomEvent.ParticipantConnected, f.room.remoteParticipants.get("PA_b"));
  check("roster: emitted at once and on every join", rosters.join(" ") === "1/1/1 2/2/1", rosters.join(" "));

  // speakers: our own participant is never a speaker
  const said: string[] = [];
  s.onSpeaking((n, end) => said.push(`${n}:${end ? "end" : "on"}`));
  f.emit(RoomEvent.ActiveSpeakersChanged, [{ sid: "PA_self", name: "Olanote" }, { sid: "PA_a", name: "Asha" }]);
  check("speakers: remote speakers only", said.join(" ") === "Asha:on", said.join(" "));

  // the recording notice: everyone now, and each later joiner on their own
  check("announce: returns true", (await s.announce("notice")) === true);
  check("announce: text stream on lk.chat", JSON.stringify(f.sent[0]) === JSON.stringify({ text: "notice", topic: "lk.chat" }), JSON.stringify(f.sent[0]));
  f.room.remoteParticipants.set("PA_d", f.participant("PA_d", "Di", "u-di", []));
  f.emit(RoomEvent.ParticipantConnected, f.room.remoteParticipants.get("PA_d"));
  await new Promise((r) => setTimeout(r, 0));
  check("announce: a later joiner gets the notice addressed to them", JSON.stringify(f.sent[1]) === JSON.stringify({ text: "notice", topic: "lk.chat", to: ["u-di"] }), JSON.stringify(f.sent[1]));

  // a name change re-counts the roster
  (f.room.remoteParticipants.get("PA_b") as { name: string }).name = "";
  f.emit(RoomEvent.ParticipantNameChanged, "", f.room.remoteParticipants.get("PA_b"));
  check("roster: a name change is counted again", rosters[rosters.length - 1] === "3/2/3", rosters.join(" "));

  // disconnect reason
  check("state: connected before any Disconnected", JSON.stringify(s.state()) === JSON.stringify({ connected: true }));
  f.emit(RoomEvent.Disconnected, DisconnectReason.PARTICIPANT_REMOVED);
  check("state: Disconnected carries the reason name", JSON.stringify(s.state()) === JSON.stringify({ connected: false, disconnectReason: "PARTICIPANT_REMOVED" }), JSON.stringify(s.state()));
  await s.disconnect();
  check("disconnect calls room.disconnect", f.disconnects() === 1);
})();

await (async () => {
  const f = fakeRoom({ sendText: () => Promise.reject(new Error("no data channel")) });
  const enc = f.participant("PA_e", "Eve", "u-e", [f.pub(Track.Kind.Audio, "t9", true)]);
  f.room.remoteParticipants.set("PA_e", enc);
  const s = createLivekitSession(f.room as unknown as RoomLike);
  check("connect: a remote encrypted track makes the room encrypted", (await s.connect("u", "t")).e2ee === true);
  check("connect: an encrypted track is never subscribed", f.subscribed.length === 0, f.subscribed.join(","));
  f.emit(RoomEvent.TrackPublished, f.pub(Track.Kind.Audio, "t8", true), enc);
  check("published later: an encrypted track is never subscribed", f.subscribed.length === 0, f.subscribed.join(","));
  check("announce: a failed send returns false, never throws", (await s.announce("notice")) === false);
})();

if (failed) {
  console.error(`\n❌ livekit-capture: ${failed} check(s) FAILED.`);
  process.exit(1);
}
console.log("\n✅ livekit-capture: audio-only subscription, subscribed tracks without identity, roster, speaker hints with heartbeat, chat on lk.chat, disconnect reason by name.");
