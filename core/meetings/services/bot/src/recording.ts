/**
 * RecordingSink adapter (2b) — the recording.v1 PER-CHUNK durable upload path, behind the
 * orchestrator's RecordingSink port (`close(key)`).
 *
 * recording.v1 has TWO halves (both in @vexa/recording): ACQUIRE (the browser MediaRecorder tap →
 * timeslice chunks, capture-bridge.ts `startRecording`) and DELIVER (this file). #491/#412: the 0.12
 * bot ACCUMULATED every chunk in Node memory (createRecordingAssembler) and uploaded ONE master at
 * graceful close — so a SIGKILL/OOM mid-meeting lost the WHOLE recording (V2/#412), and a multi-hour
 * master rode one 30s-timeout POST that fails permanently on a slow link. This sink instead uploads
 * EACH timeslice the moment it is produced via RecordingService.uploadChunk (retry+backoff), so a
 * crash leaves every FINISHED part durable in object storage; the master is assembled SERVER-SIDE on
 * the first GET /recordings/{id}/master or …/raw (meeting-api finalize-on-read). No meeting-length
 * blob ever sits in Node memory or rides one long POST.
 *
 * Contract that must hold (or the recording 404s / splits / never completes):
 *   • session_uid == inv.connectionId — the eager-created MeetingSession the server resolves the
 *     upload against (bot_spawn). NOT nativeMeetingId / a master key (the old uploadMaster fallbacks
 *     would 404 SessionNotFound or fold into the wrong recording).
 *   • the empty is_final chunk is the COMPLETED signal — forward it (do NOT drop it).
 *   • close(key) is the final-signal FALLBACK: the live Stop race routinely drops the trailing
 *     is_final MediaRecorder chunk (the WS closes before it flushes), so on close we POST one empty
 *     is_final upload IF none was sent — the server then flips the recording to COMPLETED. Fires once.
 *   • uploads are serialized on an internal promise queue so parts land in seq order; a chunk that
 *     fails permanently is logged-and-skipped — the master assembles from the parts that DID arrive.
 *
 * L4-gated: the full page→Node→HTTP loss path is proven only by a live compose run. The SINK half
 * (per-chunk upload, correct seq/isFinal/session_uid, the close fallback) is offline-provable
 * (recording.test.ts) — the P22/#224-class regression pin the 0.12 in-memory bot lacked. The
 * assembler stays in @vexa/recording (the desktop composition root still uses it) — only the cloud
 * bot's wiring changes.
 */
import { RecordingService, type ChunkUploadExtra, type RecordingMasterFormat } from '@vexa/recording';
import type { Invocation } from './config.js';
import type { RecordingSink } from './ports.js';

/** Who a remote channel is. Sent with every chunk of the channel, so losing one chunk loses
 *  nothing; a field is fixed by the first chunk that knows it and never changed after. */
export interface ChannelChunkIdentity {
  recorder_start_epoch_ms?: number;
  channel_kind?: string;
  stream_id?: string;
  participant_id?: string;
  display_name?: string;
}

/** The RecordingSink extended with the chunk ingress the capture bridge's MediaRecorder tap pumps
 *  into. The orchestrator only sees close(key); the bridge holds the BotRecordingSink to feed chunks
 *  as they arrive from the page-side recorder. */
export interface BotRecordingSink extends RecordingSink {
  /** One recording.v1 chunk for `key`: monotonic seq, the COMPLETED-signal flag, format, bytes. */
  chunk(key: string, seq: number, isFinal: boolean, format: RecordingMasterFormat, bytes: Uint8Array): void;
  /** One chunk of remote channel `channel` (media type chN). Its own seq, apart from the mix. */
  channelChunk(channel: number, seq: number, isFinal: boolean, format: RecordingMasterFormat, bytes: Uint8Array, identity?: ChannelChunkIdentity): void;
}

/** Deliver ONE recording.v1 chunk. The default uploads to inv.recordingUploadUrl via
 *  RecordingService.uploadChunk; tests inject a fake to assert per-chunk delivery without HTTP.
 *  `extra` is set only for a channel chunk. */
export type ChunkUploader = (
  seq: number, isFinal: boolean, format: RecordingMasterFormat, bytes: Uint8Array, extra?: ChunkUploadExtra,
) => void | Promise<void>;

export interface RecordingSinkOptions {
  inv: Invocation;
  /** Override the chunk uploader (tests inject this to assert per-chunk upload without a live
   *  receiver). Default = HTTP upload to inv.recordingUploadUrl via RecordingService.uploadChunk. */
  uploadChunk?: ChunkUploader;
  log?: (msg: string) => void;
}

/** The default chunk uploader: POST each chunk to meeting-api's internal upload endpoint via the
 *  shipped RecordingService.uploadChunk (multipart, retry+backoff, structured chunk-loss logging).
 *  session_uid is inv.connectionId — the server resolves the eager-created MeetingSession by it. */
function defaultChunkUploader(inv: Invocation, log: (m: string) => void): ChunkUploader {
  const url = inv.recordingUploadUrl;
  const meetingId = inv.meeting_id ?? 0;
  const sessionUid = inv.connectionId ?? '';
  const token = inv.internalSecret ?? '';
  const svc = new RecordingService(meetingId, sessionUid);
  return async (seq, isFinal, format, bytes, extra) => {
    if (!url) {
      log(`recording: no recordingUploadUrl — chunk ${seq} (${bytes.length}B, isFinal=${isFinal}) NOT uploaded`);
      return;
    }
    await svc.uploadChunk(url, token, Buffer.from(bytes), seq, isFinal, format, extra);
  };
}

/**
 * Build the recording sink. Each chunk(...) uploads immediately (serialized in seq order); close(key)
 * sends the empty is_final fallback exactly once if the tap never delivered its own final chunk.
 */
const IDENTITY_TEXT_LIMIT = 200;

function textField(value: unknown): string | undefined {
  if (typeof value !== 'string') return undefined;
  const trimmed = value.trim();
  if (!trimmed || trimmed.length > IDENTITY_TEXT_LIMIT) return undefined;
  return trimmed;
}

/** media_type chN and the channel's identity so far, on every channel chunk. */
function channelExtra(channel: number, identity: ChannelChunkIdentity | undefined): ChunkUploadExtra {
  const extra: ChunkUploadExtra = { mediaType: `ch${channel}` };
  if (!identity) return extra;
  const metadata: Record<string, string | number> = {};
  const start = identity.recorder_start_epoch_ms;
  if (typeof start === 'number' && Number.isInteger(start) && start > 0) metadata.recorder_start_epoch_ms = start;
  if (identity.channel_kind === 'gmeet' || identity.channel_kind === 'jitsi') metadata.channel_kind = identity.channel_kind;
  const streamId = textField(identity.stream_id);
  if (streamId) metadata.stream_id = streamId;
  const participantId = textField(identity.participant_id);
  if (participantId) metadata.participant_id = participantId;
  const displayName = textField(identity.display_name);
  if (displayName) metadata.display_name = displayName;
  if (Object.keys(metadata).length) extra.metadata = metadata;
  return extra;
}

/** `known` with every field `next` adds that `known` lacks: first value wins, gaps fill in. */
function withIdentity(
  known: ChannelChunkIdentity | undefined, next: ChannelChunkIdentity | undefined,
): ChannelChunkIdentity | undefined {
  if (!next) return known;
  const merged: ChannelChunkIdentity = { ...known };
  for (const [key, value] of Object.entries(next) as [keyof ChannelChunkIdentity, never][]) {
    if (value !== undefined && merged[key] === undefined) merged[key] = value;
  }
  return merged;
}

interface UploadLane {
  queue: Promise<void>;
  anyChunk: boolean;
  finalSent: boolean;
  maxSeq: number;
  lastFormat: RecordingMasterFormat;
  identity?: ChannelChunkIdentity;
}

export function createBotRecordingSink(opts: RecordingSinkOptions): BotRecordingSink {
  const log = opts.log ?? (() => { /* silent by default */ });
  const upload = opts.uploadChunk ?? defaultChunkUploader(opts.inv, log);

  const makeLane = (): UploadLane => ({
    queue: Promise.resolve(),
    anyChunk: false,
    finalSent: false,
    maxSeq: -1,
    lastFormat: 'webm',
  });
  const audio = makeLane();
  const channels = new Map<number, UploadLane>();

  const enqueue = (lane: UploadLane, seq: number, isFinal: boolean, format: RecordingMasterFormat, bytes: Uint8Array, extra?: ChunkUploadExtra): void => {
    lane.anyChunk = true;
    if (isFinal) lane.finalSent = true;
    if (seq > lane.maxSeq) lane.maxSeq = seq;
    lane.lastFormat = format;
    lane.queue = lane.queue
      .then(() => upload(seq, isFinal, format, bytes, extra))
      .catch((e) => { log(`recording: chunk ${seq} (isFinal=${isFinal}) upload failed — continuing: ${String(e)}`); });
  };

  const fallback = (lane: UploadLane, extra?: ChunkUploadExtra): void => {
    if (!lane.anyChunk || lane.finalSent) return;
    enqueue(lane, lane.maxSeq + 1, true, lane.lastFormat, new Uint8Array(0), extra);
  };

  return {
    chunk: (_key, seq, isFinal, format, bytes) => { enqueue(audio, seq, isFinal, format, bytes); },
    channelChunk: (channel, seq, isFinal, format, bytes, identity) => {
      if (typeof channel !== 'number' || !Number.isInteger(channel) || channel < 0) {
        log(`recording: channel ${String(channel)} ignored`);
        return;
      }
      let lane = channels.get(channel);
      if (!lane) {
        lane = makeLane();
        channels.set(channel, lane);
      }
      lane.identity = withIdentity(lane.identity, identity);
      enqueue(lane, seq, isFinal, format, bytes, channelExtra(channel, lane.identity));
    },
    close: (_key) => {
      // Final-signal FALLBACK: if the live Stop race dropped the trailing is_final chunk, send one
      // empty is_final so the server flips the recording COMPLETED. No-op for a never-fed session
      // (no phantom recording), and at most once (a real is_final already set finalSent).
      // Each channel lane does the same for its own media type. The mix lane stays media type audio.
      fallback(audio);
      for (const [channel, lane] of channels) fallback(lane, channelExtra(channel, lane.identity));
    },
  };
}
