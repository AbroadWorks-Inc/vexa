/**
 * PcmChunker — pure chunk-sequencing for the Zoom bot's B1 audio durability.
 *
 * Extracted from `recording.ts` for the same reason the speaker-boundary state
 * machine was extracted from `index.ts`: `recording.ts` imports `../../../index`
 * (which imports `playwright-extra` at module scope) and so cannot be loaded by a
 * plain `tsx` test run. This module imports nothing but `Buffer`, so it IS
 * testable — see `pcm-chunker.test.ts`.
 *
 * Responsibility: accumulate captured PCM slices and flush them as ordered chunks
 * to an injected uploader, so a pod SIGKILL mid-meeting only loses the audio since
 * the last flush (mirrors the Meet/Teams incremental `uploadChunk` path).
 *
 * Invariants (regression-tested):
 *   - chunk_seq is monotonic with NO gaps: every flush that actually sends
 *     consumes exactly one seq; an empty non-final flush sends nothing and
 *     consumes no seq.
 *   - the `isFinal` chunk is always the last one sent (flushes are serialized,
 *     and the caller awaits the final flush last).
 *   - trailing data appended before the final flush is included in it.
 */

/** Uploads one assembled chunk. `isFinal` marks end-of-stream. */
export type PcmChunkUploader = (
  buf: Buffer,
  seq: number,
  isFinal: boolean,
) => Promise<void>;

export class PcmChunker {
  private parts: Buffer[] = [];
  private bytes = 0;
  private seq = 0;
  // Serializes flushes so the periodic timer, the byte-cap trigger, and the
  // final stop-flush never interleave (which would scramble chunk_seq order).
  private inFlight: Promise<void> = Promise.resolve();

  /**
   * @param flushBytes byte threshold at which `append` auto-flushes a non-final chunk.
   * @param upload injected sink (production: RecordingService.uploadChunk bound to url/token/'pcm').
   * @param onError optional hook invoked when an upload rejects; the chunker never throws.
   */
  constructor(
    private readonly flushBytes: number,
    private readonly upload: PcmChunkUploader,
    private readonly onError?: (
      err: unknown,
      seq: number,
      isFinal: boolean,
    ) => void,
  ) {}

  /** Buffer a captured PCM slice; auto-flush (non-final) once the byte cap is reached. */
  append(chunk: Buffer): void {
    // Copy: a streamed Buffer may be reused by the source after this returns.
    this.parts.push(Buffer.from(chunk));
    this.bytes += chunk.length;
    if (this.bytes >= this.flushBytes) {
      void this.flush(false);
    }
  }

  /** Bytes buffered but not yet flushed (test/introspection aid). */
  get pendingBytes(): number {
    return this.bytes;
  }

  /**
   * Flush the buffered PCM as one chunk. Returns a promise that resolves after
   * THIS flush (and every prior queued flush) completes, so awaiting the final
   * flush drains the whole chain. Best-effort: an upload error goes to `onError`,
   * never propagates.
   */
  flush(isFinal: boolean): Promise<void> {
    this.inFlight = this.inFlight.then(async () => {
      // Nothing buffered and not the terminal flush → send nothing, consume no seq.
      if (this.bytes === 0 && !isFinal) return;
      const buf = this.parts.length ? Buffer.concat(this.parts) : Buffer.alloc(0);
      this.parts = [];
      this.bytes = 0;
      const seq = this.seq++;
      try {
        await this.upload(buf, seq, isFinal);
      } catch (err) {
        this.onError?.(err, seq, isFinal);
      }
    });
    return this.inFlight;
  }

  /** Reset all state for a fresh session (seq back to 0, buffers cleared). */
  reset(): void {
    this.parts = [];
    this.bytes = 0;
    this.seq = 0;
    this.inFlight = Promise.resolve();
  }
}
