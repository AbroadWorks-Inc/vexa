/**
 * PcmChunker — B1 audio-durability chunk-sequencing tests.
 *
 * Run: npx tsx services/vexa-bot/core/src/platforms/zoom/web/pcm-chunker.test.ts
 *
 * The chunker owns the invariants B1 depends on: monotonic gap-free chunk_seq
 * under interleaved byte-cap + timer flushes, `isFinal` always last, and trailing
 * data (the parecord tail after SIGTERM) included in the final chunk. `recording.ts`
 * cannot be loaded under `tsx` (it imports index.ts → playwright-extra at module
 * scope), so this logic was extracted here specifically to be testable.
 */

import { PcmChunker, PcmChunkUploader } from './pcm-chunker';

// ─── Test harness ────────────────────────────────────────────────────────────

let passed = 0;
let failed = 0;

function assertEqual(actual: unknown, expected: unknown, label: string): void {
  const ok = JSON.stringify(actual) === JSON.stringify(expected);
  if (ok) {
    passed++;
    console.log(`  ✅ ${label}`);
  } else {
    failed++;
    console.log(`  ❌ ${label}`);
    console.log(`     expected: ${JSON.stringify(expected)}`);
    console.log(`     actual:   ${JSON.stringify(actual)}`);
  }
}

interface Sent {
  seq: number;
  isFinal: boolean;
  bytes: number;
  payload: string;
}

/** A recording uploader that captures every chunk it is handed, in order. */
function recorder(): { upload: PcmChunkUploader; sent: Sent[] } {
  const sent: Sent[] = [];
  const upload: PcmChunkUploader = async (b, seq, isFinal) => {
    sent.push({ seq, isFinal, bytes: b.length, payload: b.toString('latin1') });
  };
  return { upload, sent };
}

function buf(s: string): Buffer {
  return Buffer.from(s, 'latin1');
}

async function runAsyncTests(): Promise<void> {
  console.log('PcmChunker tests\n');

  // 1. Byte-cap auto-flush: append past the cap triggers a non-final chunk.
  {
    const { upload, sent } = recorder();
    const c = new PcmChunker(4, upload); // 4-byte cap
    c.append(buf('AB')); // 2 bytes, under cap
    assertEqual(c.pendingBytes, 2, 'buffers under the cap without flushing');
    c.append(buf('CD')); // hits 4 → auto-flush
    await c.flush(false); // drain the queued auto-flush
    assertEqual(sent.length, 1, 'byte cap triggers exactly one flush');
    assertEqual(sent[0], { seq: 0, isFinal: false, bytes: 4, payload: 'ABCD' }, 'flushed chunk holds all buffered bytes in order');
  }

  // 2. Empty non-final flush sends nothing and consumes no seq; the next real
  //    chunk is still seq 0 (no gap from a wasted seq).
  {
    const { upload, sent } = recorder();
    const c = new PcmChunker(1000, upload);
    await c.flush(false); // buffer empty, not final → no-op
    assertEqual(sent.length, 0, 'empty non-final flush sends nothing');
    c.append(buf('X'));
    await c.flush(false);
    assertEqual(sent.map((s) => s.seq), [0], 'first real chunk is still seq 0 (no wasted seq)');
  }

  // 3. Monotonic, gap-free seq across interleaved timer + byte-cap flushes.
  {
    const { upload, sent } = recorder();
    const c = new PcmChunker(4, upload);
    c.append(buf('aa')); // 2
    await c.flush(false); // manual (timer-style) flush → seq 0 = "aa"
    c.append(buf('bbbb')); // hits cap → auto-flush seq 1
    await c.flush(false); // empty now → no-op
    c.append(buf('cc'));
    await c.flush(false); // seq 2 = "cc"
    assertEqual(sent.map((s) => s.seq), [0, 1, 2], 'seq is monotonic with no gaps or dupes');
    assertEqual(sent.map((s) => s.payload), ['aa', 'bbbb', 'cc'], 'payloads preserved in capture order');
    assertEqual(sent.every((s) => !s.isFinal), true, 'none of the interim chunks are final');
  }

  // 4. isFinal is always the LAST chunk, with the highest seq.
  {
    const { upload, sent } = recorder();
    const c = new PcmChunker(1000, upload);
    c.append(buf('one'));
    await c.flush(false); // seq 0
    c.append(buf('two'));
    await c.flush(true); // seq 1, final
    assertEqual(sent.length, 2, 'two chunks sent');
    assertEqual(sent[sent.length - 1].isFinal, true, 'the last chunk is the final one');
    assertEqual(sent[sent.length - 1].seq, 1, 'final chunk carries the highest seq');
    assertEqual(sent.filter((s) => s.isFinal).length, 1, 'exactly one final chunk');
  }

  // 5. W1: trailing data appended AFTER the last periodic flush is included in
  //    the final chunk (the parecord tail after SIGTERM must not be lost).
  {
    const { upload, sent } = recorder();
    const c = new PcmChunker(1000, upload);
    c.append(buf('body'));
    await c.flush(false); // seq 0 = "body"
    c.append(buf('TAIL')); // arrives during drain, before the final flush
    await c.flush(true); // seq 1, final, must include the tail
    assertEqual(sent[1], { seq: 1, isFinal: true, bytes: 4, payload: 'TAIL' }, 'trailing data is delivered in the final chunk');
  }

  // 6. Final flush with an empty buffer still sends a final marker (clean EOS).
  {
    const { upload, sent } = recorder();
    const c = new PcmChunker(1000, upload);
    c.append(buf('data'));
    await c.flush(false); // seq 0
    await c.flush(true); // empty but final → seq 1 marker
    assertEqual(sent.length, 2, 'a final flush on an empty buffer still emits a marker');
    assertEqual(sent[1], { seq: 1, isFinal: true, bytes: 0, payload: '' }, 'empty final chunk carries is_final with zero bytes');
  }

  // 7a. Serialization across awaited flushes: distinct byte-cap flushes stay
  //     ordered and gap-free (each callback resets before the next append).
  {
    const { upload, sent } = recorder();
    const c = new PcmChunker(2, upload);
    c.append(buf('11')); // cap → auto-flush
    await c.flush(false); // drain: seq 0 = "11"
    c.append(buf('22')); // cap → auto-flush
    await c.flush(false); // drain: seq 1 = "22"
    await c.flush(true); // seq 2, final marker
    assertEqual(sent.map((s) => s.seq), [0, 1, 2], 'awaited byte-cap flushes are ordered and gap-free');
    assertEqual(sent.map((s) => s.payload), ['11', '22', ''], 'each awaited flush carries its own slice');
    assertEqual(sent[2].isFinal, true, 'only the trailing flush is final');
  }

  // 7b. Rapid synchronous cap-hits COALESCE safely: the first queued flush grabs
  //     all bytes buffered so far, later queued flushes find an empty buffer and
  //     no-op. Fewer/larger chunks, but never a gap, dupe, or lost byte.
  {
    const { upload, sent } = recorder();
    const c = new PcmChunker(2, upload);
    c.append(buf('11')); // queues flush A
    c.append(buf('22')); // queues flush B
    c.append(buf('33')); // queues flush C
    await c.flush(true); // final drains the whole chain
    assertEqual(sent.map((s) => s.seq), [0, 1], 'coalesced flushes stay gap-free (A sends all buffered, B/C no-op)');
    assertEqual(sent.map((s) => s.payload), ['112233', ''], 'no byte is lost — all buffered data lands in one chunk');
    assertEqual(sent[sent.length - 1].isFinal, true, 'the trailing flush is the only final chunk');
  }

  // 8. An upload error is routed to onError and never throws; sequencing continues.
  {
    const sent: Sent[] = [];
    const errors: number[] = [];
    const flaky = async (b: Buffer, seq: number, isFinal: boolean): Promise<void> => {
      if (seq === 0) throw new Error('boom');
      sent.push({ seq, isFinal, bytes: b.length, payload: b.toString('latin1') });
    };
    const c = new PcmChunker(1000, flaky, (_e, seq) => errors.push(seq));
    c.append(buf('lost'));
    await c.flush(false); // seq 0 → throws → onError
    c.append(buf('kept'));
    await c.flush(true); // seq 1 → succeeds
    assertEqual(errors, [0], 'the failing chunk is reported to onError');
    assertEqual(sent.map((s) => s.seq), [1], 'a later chunk still sends after an error (no chain break)');
  }

  // 9. reset() clears buffers and returns seq to 0 for a fresh session.
  {
    const { upload, sent } = recorder();
    const c = new PcmChunker(1000, upload);
    c.append(buf('old'));
    await c.flush(false); // seq 0
    c.reset();
    c.append(buf('new'));
    await c.flush(false); // seq 0 again after reset
    assertEqual(sent.map((s) => s.seq), [0, 0], 'reset returns seq to 0');
    assertEqual(sent[1].payload, 'new', 'reset drops the pre-reset buffer');
  }
}

runAsyncTests().then(() => {
  console.log(`\n${passed} passed, ${failed} failed`);
  if (failed > 0) {
    process.exit(1);
  }
});
