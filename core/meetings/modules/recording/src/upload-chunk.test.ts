/**
 * A channel upload carries media_type and identity. A master upload does not.
 * Run: npx tsx src/upload-chunk.test.ts
 */
import http from "node:http";
import type { AddressInfo } from "node:net";
import { RecordingService } from "./recording";

let failed = 0;
const check = (name: string, cond: boolean, detail = "") => {
  console.log(`  ${cond ? "✅" : "❌"} ${name}${cond ? "" : "  — " + detail}`);
  if (!cond) failed++;
};

function listen(): Promise<{ url: string; metas: Record<string, unknown>[]; close: () => Promise<void> }> {
  const metas: Record<string, unknown>[] = [];
  const server = http.createServer((req, res) => {
    const parts: Buffer[] = [];
    req.on("data", (d: Buffer) => parts.push(d));
    req.on("end", () => {
      const text = Buffer.concat(parts).toString("latin1");
      const m = text.match(/name="metadata"[\s\S]*?\r\n\r\n(\{[\s\S]*?\})\r\n/);
      metas.push(m ? JSON.parse(m[1]) : {});
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ status: "ok" }));
    });
  });
  return new Promise((resolve) => {
    server.listen(0, "127.0.0.1", () => {
      const url = `http://127.0.0.1:${(server.address() as AddressInfo).port}/internal/recordings/upload`;
      resolve({
        url,
        metas,
        close: () => new Promise((r) => server.close(() => r())),
      });
    });
  });
}

async function main(): Promise<void> {
  const svc = new RecordingService(7, "sess-1");
  const server = await listen();
  try {
    await svc.uploadChunk(server.url, "tok", Buffer.from([1, 2]), 0, false, "webm");
    await svc.uploadChunk(server.url, "tok", Buffer.from([3]), 0, false, "webm", {
      mediaType: "ch0",
      metadata: {
        recorder_start_epoch_ms: 1700000000000,
        channel_kind: "jitsi",
        stream_id: "remote-audio-0",
        participant_id: "p1",
        display_name: "Ada",
        chunk_seq: 99,
      },
    });
  } finally {
    await server.close();
  }

  const audio = server.metas[0];
  const channel = server.metas[1];
  check("two uploads", server.metas.length === 2, JSON.stringify(server.metas));
  check(
    "a master chunk has the fixed fields and no media_type",
    JSON.stringify(audio) === JSON.stringify({
      meeting_id: 7,
      session_uid: "sess-1",
      format: "webm",
      sample_rate: 16000,
      channels: 1,
      file_size_bytes: 2,
      chunk_seq: 0,
      is_final: false,
    }),
    JSON.stringify(audio),
  );
  check("a channel chunk is media_type ch0", channel?.media_type === "ch0", JSON.stringify(channel));
  check("channel identity is on the metadata", channel?.channel_kind === "jitsi" && channel?.stream_id === "remote-audio-0" && channel?.display_name === "Ada" && channel?.participant_id === "p1" && channel?.recorder_start_epoch_ms === 1700000000000, JSON.stringify(channel));
  check("extra metadata cannot replace chunk_seq", channel?.chunk_seq === 0, JSON.stringify(channel));

  if (failed) {
    console.error(`\n❌ upload-chunk: ${failed} check(s) FAILED.`);
    process.exit(1);
  }
  console.log("\n✅ upload-chunk: master metadata is unchanged; a channel upload adds media_type and identity.");
}

void main();
