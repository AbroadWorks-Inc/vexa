/**
 * Which page streams are per-speaker channels.
 *
 * A Jitsi channel is a remote audio MediaStream whose id is remote-audio-<n>.
 * The digits are the channel number, so a frame and a file agree without a
 * second enumeration. mixedmslabel, a file-backed sound, and every other id
 * are not channels. Teams and Zoom are not selected here.
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

/** Per-channel recorders for this platform. Teams, Zoom, and Meet return none. */
export function selectChannelTargets(platform: string, candidates: readonly ChannelCandidate[]): ChannelTarget[] {
  if (platform === "jitsi") return selectJitsiChannels(candidates);
  return [];
}
