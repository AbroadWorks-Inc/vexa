/**
 * Channel selection is pure: a stream id either is a Jitsi remote-audio channel
 * or it is not. Run: npx tsx src/channel-targets.test.ts
 */
import {
  createJitsiChannelNumbers,
  createLivekitChannelNumbers,
  jitsiChannel,
  selectJitsiChannels,
  selectLivekitChannels,
} from "./channel-targets.js";

let failed = 0;
const check = (name: string, cond: boolean, detail = "") => {
  console.log(`  ${cond ? "✅" : "❌"} ${name}${cond ? "" : "  — " + detail}`);
  if (!cond) failed++;
};

check("remote-audio-2 is channel 2", jitsiChannel("remote-audio-2") === 2);
check("remote-audio-0 is channel 0", jitsiChannel("remote-audio-0") === 0);
check("mixedmslabel is not a channel", jitsiChannel("mixedmslabel") === null);
check("an element id is not a channel", jitsiChannel("remoteAudio_abc") === null);
check("a prefix is not a channel", jitsiChannel("remote-audio-2extra") === null);
check("a bare prefix is not a channel", jitsiChannel("remote-audio-") === null);

const selected = selectJitsiChannels([
  { streamId: "mixedmslabel", paused: false, audioTracks: 1 },
  { streamId: "remote-audio-4", paused: false, audioTracks: 1 },
  { streamId: "remote-audio-4", paused: false, audioTracks: 1 },
  { streamId: "remote-audio-1", paused: true, audioTracks: 1 },
  { streamId: "remote-audio-7", paused: false, audioTracks: 0 },
  { streamId: "remote-audio-0", paused: false, audioTracks: 2 },
]);
check(
  "jitsi keeps live remote-audio streams in order, once",
  JSON.stringify(selected) === JSON.stringify([
    { streamId: "remote-audio-4", channel: 4 },
    { streamId: "remote-audio-0", channel: 0 },
  ]),
  JSON.stringify(selected),
);

// A new bridge session numbers its streams from remote-audio-1 again, as new MediaStream
// objects. The number follows the object, so a reused id gets a channel never used before.
const numbers = createJitsiChannelNumbers();
const first = [{}, {}, {}];
check("a first stream keeps its digits", numbers.channelFor(first[0], "remote-audio-1") === 1);
check("the same object keeps its number", numbers.channelFor(first[0], "remote-audio-1") === 1);
check("first streams 2 and 3 keep their digits",
  numbers.channelFor(first[1], "remote-audio-2") === 2 && numbers.channelFor(first[2], "remote-audio-3") === 3);
check("a non-channel id has no number and uses none", numbers.channelFor({}, "mixedmslabel") === null);
const second = [{}, {}, {}];
const renumbered = second.map((s, i) => numbers.channelFor(s, `remote-audio-${i + 1}`));
check("a new object for a used id gets the next free numbers",
  JSON.stringify(renumbered) === JSON.stringify([4, 5, 6]), JSON.stringify(renumbered));
check("the new object then keeps its number", numbers.channelFor(second[0], "remote-audio-1") === 4);
check("the old object keeps its old number", numbers.channelFor(first[0], "remote-audio-1") === 1);
const genuine4 = numbers.channelFor({}, "remote-audio-4");
check("a later genuine id whose digits are taken gets the next free number", genuine4 === 7, String(genuine4));
check("a later id whose digits are free keeps them", numbers.channelFor({}, "remote-audio-9") === 9);
check("after that the next free number is above the highest", numbers.channelFor({}, "remote-audio-2") === 10);
check("remote-audio-0 keeps 0 on a fresh session", createJitsiChannelNumbers().channelFor({}, "remote-audio-0") === 0);


// LiveKit: a channel is a subscribed track, matched to the hook's element by track id. The hook
// also mirrors receivers LiveKit pre-allocates, which carry nobody until a subscription lands.
const lk = (streamId: string, trackId: string, paused = false, audioTracks = 1) => ({ streamId, trackId, paused, audioTracks });
const lkSelected = selectLivekitChannels(
  [lk("s1", "t1"), lk("s2", "t2"), lk("s9", "t9"), lk("s3", "t3", true), lk("s4", "t4", false, 0), lk("s1b", "t1")],
  [
    { trackId: "t1", sid: "PA_a", name: "Asha" },
    { trackId: "t2", sid: "PA_b", name: "" },
    { trackId: "t3", sid: "PA_c", name: "Cy" },
    { trackId: "t4", sid: "PA_d", name: "Di" },
  ],
);
check(
  "livekit keeps only live subscribed tracks, once per key",
  JSON.stringify(lkSelected) === JSON.stringify([
    { streamId: "s1", key: "PA_a|t1", sid: "PA_a" },
    { streamId: "s2", key: "PA_b|t2", sid: "PA_b" },
  ]),
  JSON.stringify(lkSelected),
);
check("livekit selects nothing when nothing is subscribed", selectLivekitChannels([lk("s1", "t1")], []).length === 0);

const lkNumbers = createLivekitChannelNumbers();
check("livekit numbers start at 0", lkNumbers.channelFor("PA_a|t1") === 0);
check("a second key gets the next number", lkNumbers.channelFor("PA_b|t2") === 1);
check("a known key keeps its number", lkNumbers.channelFor("PA_a|t1") === 0);
check("a reused receiver carrying another person is a new channel", lkNumbers.channelFor("PA_c|t1") === 2);
lkNumbers.forget("PA_a|t1");
check("a forgotten key that returns gets a number never used before", lkNumbers.channelFor("PA_a|t1") === 3);

if (failed) {
  console.error(`\n❌ channel-targets: ${failed} check(s) FAILED.`);
  process.exit(1);
}
console.log("\n✅ channel-targets: a first jitsi remote-audio stream keeps its digits, a new stream under a used id gets a new number; other platforms select nothing.");
