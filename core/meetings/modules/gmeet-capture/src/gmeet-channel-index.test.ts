/**
 * The channel map published by createGmeetCapture. Index assignment is the
 * existing connect/rescan rule; this pins that a recorder can read it.
 * Run: npx tsx src/gmeet-channel-index.test.ts
 */
class FakeStream {
  id: string;
  ended: (() => void) | null = null;
  constructor(id: string) { this.id = id; }
  getAudioTracks(): { id: string; addEventListener: (ev: string, fn: () => void) => void }[] {
    return [{
      id: this.id + "-t",
      addEventListener: (ev: string, fn: () => void) => { if (ev === "ended") this.ended = fn; },
    }];
  }
}

const elements: { paused: boolean; srcObject: FakeStream }[] = [];
(globalThis as any).MediaStream = FakeStream;
(globalThis as any).document = { querySelectorAll: () => elements };
(globalThis as any).AudioWorkletNode = class {
  port: { onmessage: ((ev: { data: Float32Array }) => void) | null } = { onmessage: null };
  connect(): void { /* stub */ }
  disconnect(): void { /* stub */ }
};
const urlWithBlob = URL as unknown as { createObjectURL?: (b: Blob) => string; revokeObjectURL?: (u: string) => void };
if (typeof urlWithBlob.createObjectURL !== "function") urlWithBlob.createObjectURL = () => "blob:fake";
if (typeof urlWithBlob.revokeObjectURL !== "function") urlWithBlob.revokeObjectURL = () => { /* stub */ };
(globalThis as any).AudioContext = class {
  state = "running";
  destination = {};
  audioWorklet = { addModule(): Promise<void> { return Promise.resolve(); } };
  resume(): Promise<void> { return Promise.resolve(); }
  createMediaStreamSource(): { connect: () => void } { return { connect() { /* stub */ } }; }
  close(): Promise<void> { return Promise.resolve(); }
};

import { createGmeetCapture } from "./gmeet-capture.js";

let failed = 0;
const check = (name: string, cond: boolean, detail = "") => {
  console.log(`  ${cond ? "✅" : "❌"} ${name}${cond ? "" : "  — " + detail}`);
  if (!cond) failed++;
};
const waitFor = async (cond: () => boolean): Promise<boolean> => {
  const start = Date.now();
  while (!cond()) {
    if (Date.now() - start > 1000) return false;
    await new Promise((r) => setTimeout(r, 20));
  }
  return true;
};
const el = (id: string) => ({ paused: false, srcObject: new FakeStream(id) });

async function main(): Promise<void> {
  const a = el("stream-a");
  const b = el("stream-b");
  elements.push(a, b);
  const cap = createGmeetCapture({
    onAudio() { /* unused */ },
    log() { /* quiet */ },
    findRetries: 1,
    findDelayMs: 1,
    rescanMs: 30,
  });
  try {
    await cap.start();
    check(
      "the first two streams keep the element indexes",
      JSON.stringify(cap.channels()) === JSON.stringify([
        { streamId: "stream-a", index: 0 },
        { streamId: "stream-b", index: 1 },
      ]),
      JSON.stringify(cap.channels()),
    );

    const stable = JSON.stringify(cap.channels());
    await new Promise((r) => setTimeout(r, 80));
    check("a rescan does not renumber a stream that is still connected", JSON.stringify(cap.channels()) === stable);

    elements.push(el("stream-c"));
    check(
      "a late joiner gets the next index",
      await waitFor(() => cap.channels().some((row) => row.streamId === "stream-c" && row.index === 2)),
      JSON.stringify(cap.channels()),
    );

    a.srcObject.ended?.();
    check(
      "a track that ends and is still on the page is connected again at a new index",
      await waitFor(() => cap.channels().some((row) => row.streamId === "stream-a" && row.index === 3)),
      JSON.stringify(cap.channels()),
    );
    check(
      "the old index is not kept beside the new one",
      cap.channels().filter((row) => row.streamId === "stream-a").length === 1,
      JSON.stringify(cap.channels()),
    );
  } finally {
    cap.stop();
    elements.length = 0;
  }

  if (failed) {
    console.error(`\n❌ gmeet channel index: ${failed} check(s) FAILED.`);
    process.exit(1);
  }
  console.log("\n✅ gmeet channel index: channels() reports the stream id and the index connectElement assigned.");
}

void main();
