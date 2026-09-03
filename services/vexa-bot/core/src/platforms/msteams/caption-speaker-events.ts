/**
 * Teams caption-derived speaker events, published to `speaker_events_relative`
 * with explicit provenance `source: "caption"`.
 *
 * ── WHY THIS FILE EXISTS ─────────────────────────────────────────────────────
 * Teams derives speakers from two producers of very different quality:
 *
 *   1. LIVE CAPTIONS — `[data-tid="author"]` in the caption renderer, i.e.
 *      Microsoft's own server-side diarisation.
 *   2. A DOM VOICE-LEVEL DETECTOR — `[data-tid="voice-level-stream-outline"]`
 *      plus the `vdi-frame-occlusion` class, a Virtual-Desktop-Infrastructure
 *      frame marker being read as "this person is talking". It is the least
 *      trustworthy signal in the Teams bot.
 *
 * Downstream, `aw-integration/adapter.py` decides speaker attribution from the
 * `speaker_events_relative` stream, and it can only prefer (1) over (2) if the
 * two are distinguishable on the wire. They were not:
 *
 *   - the DOM detector reaches Redis through the `__vexaSpeakerEvents` bridge in
 *     `src/index.ts`, which stamps every event `source: "dom"`;
 *   - the caption path reaches Redis through `handleTeamsCaptionData` →
 *     `SegmentPublisher.publishSpeakerEvent`, which writes NO `source` field at
 *     all (`SpeakerEvent.source` is typed `'audio'` and is reserved for the
 *     guaranteed-paired audio-activity tracker — tagging caption events
 *     `'audio'` would make the adapter build never-closing intervals that erase
 *     every other speaker).
 *
 * Relying on "absence of a source field means caption" is exactly the fragile
 * shape this codebase has been bitten by, so this module publishes the caption
 * boundary POSITIVELY tagged instead, from inside `msteams/` — no shared file
 * changes, no new dependency (`redis` is already a bot dependency).
 *
 * ── WHAT IT DOES NOT DO ──────────────────────────────────────────────────────
 * It emits SPEAKER_START only, one per caption SPEAKER CHANGE — a
 * dominant-speaker transition, which is the shape `notetaker-worker`'s
 * "last event at or before the segment start" rule expects. It never emits
 * SPEAKER_END and never claims `source: "audio"`, so the adapter keeps treating
 * Teams as points-only with `speaker_intervals: []`.
 *
 * The pre-existing untagged duplicate from `handleTeamsCaptionData` is left in
 * place (`src/index.ts` is outside this change's scope). It is harmless: the
 * adapter's Teams branch keeps ONLY `source == "caption"` events when any
 * exist, so the untagged twin is discarded with the DOM points.
 */

import { createClient, RedisClientType } from 'redis';
import { log } from '../../utils';
import { SPEAKER_EVENT_STREAM_MAXLEN } from '../../services/segment-publisher';

/**
 * Stream key, mirroring `SegmentPublisher`'s default. Duplicated across a
 * module boundary the same way `adapter.py` duplicates the MAXLEN, and for the
 * same reason: the alternative is exporting mutable state out of a shared file.
 */
const SPEAKER_EVENT_STREAM_KEY = 'speaker_events_relative';

/** Provenance written by this module and by nothing else. */
export const CAPTION_EVENT_SOURCE = 'caption';

/** Inputs for one caption-derived speaker boundary. */
export interface CaptionSpeakerEventInput {
  /** Session uid the adapter filters on — must equal the bot's connectionId. */
  uid: string;
  /** Best-effort; the adapter does not filter on it. */
  meetingId: string;
  /** Caption author, verbatim from Teams. */
  participantName: string;
  /** Milliseconds since audio (session) start. */
  relativeMs: number;
}

/**
 * Build the Redis stream fields for one caption boundary, or `null` when the
 * input cannot produce a usable event.
 *
 * Returning `null` rather than writing a degraded row is deliberate: the
 * adapter reads `participant_name` straight into `speaker_timeline.json`, so an
 * empty or non-finite row would surface as a nameless speaker rather than as an
 * error. A negative offset is clamped to 0 — it would otherwise sort ahead of
 * every transcript segment and capture the opening of the meeting (the same
 * clamp the audio-activity publisher applies in `src/index.ts`).
 */
export function buildCaptionSpeakerEventFields(
  input: CaptionSpeakerEventInput
): Record<string, string> | null {
  const name = (input.participantName || '').trim();
  if (!name) return null;
  if (!input.uid) return null;
  if (typeof input.relativeMs !== 'number' || !Number.isFinite(input.relativeMs)) return null;

  return {
    uid: input.uid,
    relative_client_timestamp_ms: String(Math.max(0, Math.round(input.relativeMs))),
    event_type: 'SPEAKER_START',
    participant_name: name,
    meeting_id: input.meetingId,
    source: CAPTION_EVENT_SOURCE,
  };
}

/**
 * Lazily-connected publisher for caption boundaries. One connection per bot
 * session, opened on the first caption and closed when recording ends.
 *
 * Every failure path is fail-open and logged: if this publisher never manages
 * to write anything, the stream contains only the DOM points it contains today
 * and the adapter's Teams branch finds no caption events, so attribution
 * degrades to exactly the pre-change behaviour rather than breaking.
 */
export class CaptionSpeakerEventPublisher {
  private client: RedisClientType | null = null;
  private connecting: Promise<RedisClientType | null> | null = null;
  private closed = false;
  private published = 0;
  private failures = 0;

  constructor(
    private readonly redisUrl: string,
    private readonly uid: string,
    private readonly meetingId: string
  ) {}

  /** Number of events successfully written. Exposed for the shutdown log. */
  get publishedCount(): number {
    return this.published;
  }

  private async connect(): Promise<RedisClientType | null> {
    if (this.closed) return null;
    if (this.client) return this.client;
    if (!this.connecting) {
      this.connecting = (async () => {
        try {
          const client = createClient({ url: this.redisUrl }) as RedisClientType;
          // Without a handler a transient error becomes an unhandled 'error'
          // event and takes the whole bot process down mid-meeting.
          client.on('error', (err: any) => {
            log(`[Teams Captions] Redis client error: ${err?.message || err}`);
          });
          await client.connect();
          this.client = client;
          log('[Teams Captions] Speaker-event publisher connected');
          return client;
        } catch (err: any) {
          log(`[Teams Captions] Speaker-event publisher connect FAILED: ${err?.message || err}`);
          this.connecting = null;
          return null;
        }
      })();
    }
    return this.connecting;
  }

  /**
   * Publish one caption boundary. Never throws.
   *
   * @returns true when a row reached Redis.
   */
  async publish(participantName: string, relativeMs: number): Promise<boolean> {
    if (this.closed) return false;
    const fields = buildCaptionSpeakerEventFields({
      uid: this.uid,
      meetingId: this.meetingId,
      participantName,
      relativeMs,
    });
    if (!fields) return false;

    const client = await this.connect();
    if (!client) return false;

    try {
      await client.xAdd(SPEAKER_EVENT_STREAM_KEY, '*', fields, {
        TRIM: {
          strategy: 'MAXLEN',
          strategyModifier: '~',
          // Same cap as every other writer to this global stream — the three
          // must not be able to drift, or a reader's newest-first window
          // silently stops covering a session's own events.
          threshold: SPEAKER_EVENT_STREAM_MAXLEN,
        },
      });
      this.published++;
      return true;
    } catch (err: any) {
      this.failures++;
      // Log the first failure and then every 20th, so a broken Redis cannot
      // flood the pod log for the length of a meeting.
      if (this.failures === 1 || this.failures % 20 === 0) {
        log(`[Teams Captions] Speaker-event publish FAILED (${this.failures}): ${err?.message || err}`);
      }
      return false;
    }
  }

  /** Close the connection. Idempotent; never throws. */
  async close(): Promise<void> {
    this.closed = true;
    const client = this.client;
    this.client = null;
    this.connecting = null;
    if (!client) return;
    log(
      `[Teams Captions] Speaker-event publisher closing ` +
        `(${this.published} published, ${this.failures} failed)`
    );
    try {
      if (client.isOpen) await client.quit();
    } catch {
      /* the process is on its way out; a failed quit changes nothing */
    }
  }
}
