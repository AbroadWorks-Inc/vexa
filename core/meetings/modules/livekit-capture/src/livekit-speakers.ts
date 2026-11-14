/**
 * Active-speaker hints from LiveKit's server-side speaker detection.
 *
 * `update` takes the remote speakers LiveKit reports (`ActiveSpeakersChanged`); each new participant
 * emits a start under their name, each one that drops out emits an end. Speakers are keyed by
 * participant sid, so two people with the same name stay two speakers. `tick` re-asserts a still-active speaker every
 * `heartbeatMs`, so a consumer that started mid-turn learns who is talking without waiting for the
 * next change (the same 2 s heartbeat as the Jitsi hints). A speaker with no name gives no hint.
 */

export interface LivekitSpeakersOptions {
  onSpeaking: (name: string, isEnd: boolean, tMs: number) => void;
  heartbeatMs?: number;
  now?: () => number;
}

export interface LivekitSpeaker {
  sid: string;
  name: string;
}

export interface LivekitSpeakers {
  update(speakers: readonly LivekitSpeaker[]): void;
  tick(): void;
  /** Ends every active speaker. */
  destroy(): void;
}

export function createLivekitSpeakers(opts: LivekitSpeakersOptions): LivekitSpeakers {
  const heartbeatMs = opts.heartbeatMs ?? 2000;
  const now = opts.now ?? Date.now;
  // sid -> the name it started under and when it was last emitted
  const active = new Map<string, { name: string; last: number }>();
  return {
    update(speakers) {
      const t = now();
      const speaking = new Map(speakers.filter((s) => s.name).map((s) => [s.sid, s.name]));
      for (const [sid, entry] of [...active]) {
        if (speaking.has(sid)) continue;
        active.delete(sid);
        opts.onSpeaking(entry.name, true, t);
      }
      for (const [sid, name] of speaking) {
        if (active.has(sid)) continue;
        active.set(sid, { name, last: t });
        opts.onSpeaking(name, false, t);
      }
    },
    tick() {
      const t = now();
      for (const entry of active.values()) {
        if (t - entry.last < heartbeatMs) continue;
        entry.last = t;
        opts.onSpeaking(entry.name, false, t);
      }
    },
    destroy() {
      const t = now();
      for (const entry of active.values()) opts.onSpeaking(entry.name, true, t);
      active.clear();
    },
  };
}
