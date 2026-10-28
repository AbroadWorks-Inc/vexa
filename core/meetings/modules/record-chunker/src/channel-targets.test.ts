/**
 * Channel selection is pure: a stream id either is a Jitsi remote-audio channel
 * or it is not. Run: npx tsx src/channel-targets.test.ts
 */
import { createJitsiChannelNumbers, jitsiChannel, selectJitsiChannels } from "./channel-targets.js";

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


if (failed) {
  console.error(`\n❌ channel-targets: ${failed} check(s) FAILED.`);
  process.exit(1);
}
console.log("\n✅ channel-targets: a first jitsi remote-audio stream keeps its digits, a new stream under a used id gets a new number; other platforms select nothing.");
