/**
 * Channel selection is pure: a stream id either is a Jitsi remote-audio channel
 * or it is not. Run: npx tsx src/channel-targets.test.ts
 */
import { jitsiChannel, selectJitsiChannels } from "./channel-targets.js";

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


if (failed) {
  console.error(`\n❌ channel-targets: ${failed} check(s) FAILED.`);
  process.exit(1);
}
console.log("\n✅ channel-targets: jitsi remote-audio ids are the channel numbers; other platforms select nothing.");
