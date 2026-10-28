/**
 * Which page streams are per-speaker channels.
 *
 * A Jitsi channel is a remote audio MediaStream whose id is remote-audio-<n>.
 * mixedmslabel, a file-backed sound, and every other id are not channels.
 * Teams and Zoom are not selected here.
 *
 * The digits are the channel number for the first stream with that id. A new
 * bridge session numbers its streams from remote-audio-1 again, as new
 * MediaStream objects; each of those gets the next number never used, so a
 * channel number names one stream object for the whole bot session.
 */

export interface ChannelCandidate {
  streamId: string;
  paused: boolean;
  audioTracks: number;
}

export interface ChannelTarget {
  streamId: string;
  channel: number;
}

const JITSI_REMOTE_AUDIO = /^remote-audio-(\d+)$/;

/** The channel number carried in a Jitsi remote-audio stream id, or null. */
export function jitsiChannel(streamId: string): number | null {
  const match = JITSI_REMOTE_AUDIO.exec(streamId);
  if (!match) return null;
  return Number(match[1]);
}

/** Jitsi candidates that are live remote-audio streams, one row per stream id. */
export function selectJitsiChannels(candidates: readonly ChannelCandidate[]): ChannelTarget[] {
  const seen = new Set<string>();
  const out: ChannelTarget[] = [];
  for (const candidate of candidates) {
    if (candidate.paused || candidate.audioTracks < 1) continue;
    const channel = jitsiChannel(candidate.streamId);
    if (channel === null || seen.has(candidate.streamId)) continue;
    seen.add(candidate.streamId);
    out.push({ streamId: candidate.streamId, channel });
  }
  return out;
}

/** Channel numbers for Jitsi streams, one per stream object, never reused in a session. */
export interface JitsiChannelNumbers {
  /** The stream's channel number, or null when its id is not a remote-audio channel. */
  channelFor(stream: object, streamId: string): number | null;
}

export function createJitsiChannelNumbers(): JitsiChannelNumbers {
  const byStream = new WeakMap<object, number>();
  const used = new Set<number>();
  let highest = -1;
  return {
    channelFor(stream, streamId) {
      const known = byStream.get(stream);
      if (known !== undefined) return known;
      const digits = jitsiChannel(streamId);
      if (digits === null) return null;
      const channel = used.has(digits) ? highest + 1 : digits;
      byStream.set(stream, channel);
      used.add(channel);
      highest = Math.max(highest, channel);
      return channel;
    },
  };
}
