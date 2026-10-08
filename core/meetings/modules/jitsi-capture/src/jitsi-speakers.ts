/**
 * Jitsi Meet dominant-speaker attribution — THE shared implementation.
 *
 * Pure browser code (no Node, no Playwright, no cross-file imports — the bot
 * bundles this file standalone). Consumed by the bot (bundled into
 * browser-utils.global.js; the capture bridge instantiates it post-admission)
 * and importable by any other embedder of a Jitsi page.
 *
 * Signal, layered newest-truth-first:
 *  1. Per-participant audio level. Remote audio tracks in
 *     `features/base/tracks` emit `track.audioLevelsChanged`. The loudest named
 *     participant above JITSI_SPEECH_LEVEL wins. A sample older than 1 s is
 *     absent, and a muted track reads as 0, so a cached loud value cannot keep
 *     the turn. A change, including silence, must hold for CONFIRM_POLLS before
 *     a hint is emitted. The dominant-speaker flag is not read while any level
 *     sample is present.
 *  2. The app's own redux state — `APP.store.getState()['features/base/participants']`
 *     carries `dominantSpeaker` (participant id) and the participant name map.
 *     Used only when no level has arrived. Polled (the store shape is stable
 *     across versions; the subscribe API is not worth coupling to).
 *  3. DOM fallback for builds that strip the APP global: the active tile carries a
 *     `dominant-speaker` class; its display-name node carries the name.
 *
 * Speaking start/stop events per participant feed the ChunkedTranscriber's name
 * binder as 'dom-active' hints (same protocol as the Teams/Zoom watchers) —
 * INCLUDING the ~2s heartbeat the binder's turn model requires: an open hint
 * turn decays after a short grace, so a speaker who KEEPS talking must be
 * re-asserted while dominant, or every commit past the grace loses its name.
 */

/** Jitsi torture treats a remote level above this as audible. Absolute, for the first live run. */
export const JITSI_SPEECH_LEVEL = 0.1;
/** A new speaker, or silence, must hold this many polls before a hint is emitted. */
export const CONFIRM_POLLS = 2;
/** lib-jitsi-meet emits this on each audio track. */
const TRACK_AUDIO_LEVEL = "track.audioLevelsChanged";
/** A cached level older than this is absent. Mute often stops the events. */
const LEVEL_FRESH_MS = 1000;

export type JitsiHintSource = "levels" | "dominant";

export interface JitsiLevelSample {
  id: string;
  level: number;
}

export interface JitsiNamedParticipant {
  id: string;
  name: string;
}

export interface JitsiSpeakersOptions {
  /** Local participant / bot display name — the bot's own tile is never reported. */
  selfName?: string;
  /** Speaking state change: isEnd=false → started speaking, isEnd=true → stopped.
   *  tMs = wall-clock at emit. `source` is omitted by older callers. */
  onSpeaking: (name: string, id: string, isEnd: boolean, tMs: number, source?: JitsiHintSource) => void;
  log?: (msg: string) => void;
  /** Poll interval (ms). Default 400 — dominant-speaker changes are second-scale. */
  pollMs?: number;
  /** Re-assert interval for a STILL-dominant speaker (ms). Default 2000 — the
   *  binder's heartbeat contract (must beat its open-turn grace). */
  heartbeatMs?: number;
  /** Latest per-participant levels. Empty, or omitted, keeps the dominant-speaker poll.
   *  A non-empty list is the speaker signal and the dominant flag is not read. */
  readLevels?: () => JitsiLevelSample[];
}

export interface JitsiSpeakers {
  destroy(): void;
  getState(): { mode: "redux" | "dom" | null; current: string | null; changes: number; source: JitsiHintSource | null };
}

/** The loud participant strictly above JITSI_SPEECH_LEVEL, or null.
 *  `selfId` is never returned. An id missing from `participants` is ignored.
 *  An exact tie on the winning level returns null. */
export function selectJitsiSpeaker(
  levels: readonly JitsiLevelSample[],
  participants: ReadonlyMap<string, JitsiNamedParticipant>,
  selfId: string | null,
): { id: string; name: string } | null {
  let best: { id: string; name: string; level: number } | null = null;
  let tie = false;
  for (const sample of levels) {
    if (selfId !== null && sample.id === selfId) continue;
    if (!(sample.level > JITSI_SPEECH_LEVEL)) continue;
    const person = participants.get(sample.id);
    const name = person?.name?.trim();
    if (!name) continue;
    if (best && sample.level === best.level && sample.id !== best.id) {
      tie = true;
      continue;
    }
    if (!best || sample.level > best.level) {
      best = { id: sample.id, name, level: sample.level };
      tie = false;
    }
  }
  if (!best || tie) return null;
  return { id: best.id, name: best.name };
}

// The dominant tile's marker class (stock jitsi filmstrip) + display-name nodes.
export const jitsiDominantTileSelectors: string[] = [
  ".dominant-speaker",
  '[class*="dominant-speaker"]',
];
export const jitsiTileNameSelectors: string[] = [
  ".displayname",
  '[class*="displayname" i]',
  '[data-testid="videoContainerName"]',
  '[class*="display-name"]',
];

/** Defensive read of the app's participants state → the dominant speaker's name+id. */
function dominantFromRedux(): { id: string; name: string } | null {
  try {
    const app = (globalThis as any).APP;
    const state = app?.store?.getState?.();
    const p = state?.["features/base/participants"];
    const id: string | undefined = p?.dominantSpeaker;
    if (!id) return null;
    // `remote` is a Map<id, participant>; `local` a plain participant object.
    let participant: any = null;
    if (p.local?.id === id) participant = p.local;
    else if (typeof p.remote?.get === "function") participant = p.remote.get(id);
    const name = (participant?.name || "").trim();
    return name ? { id: String(id), name } : null;
  } catch {
    return null;
  }
}

/** DOM fallback: the tile marked dominant-speaker → its display-name text. */
function dominantFromDom(): { id: string; name: string } | null {
  try {
    for (const tileSel of jitsiDominantTileSelectors) {
      const tile = document.querySelector(tileSel);
      if (!tile) continue;
      for (const nameSel of jitsiTileNameSelectors) {
        const name = tile.querySelector(nameSel)?.textContent?.trim();
        if (name) return { id: `dom:${name}`, name };
      }
    }
    return null;
  } catch {
    return null;
  }
}

function participantsFromStore(): { map: Map<string, JitsiNamedParticipant>; selfId: string | null } {
  const map = new Map<string, JitsiNamedParticipant>();
  let selfId: string | null = null;
  try {
    const app = (globalThis as any).APP;
    const p = app?.store?.getState?.()?.["features/base/participants"];
    if (p?.local?.id) selfId = String(p.local.id);
    if (typeof p?.remote?.forEach === "function") {
      p.remote.forEach((participant: { id?: string; name?: string }, key: string) => {
        const id = String(participant?.id ?? key);
        const name = (participant?.name || "").trim();
        if (name) map.set(id, { id, name });
      });
    }
  } catch { /* store missing or mid-replace */ }
  return { map, selfId };
}

export interface JitsiStreamName {
  participantId?: string;
  displayName?: string;
}

function jitsiTrackStreamId(track: any): string {
  try {
    const id = track?.getOriginalStream?.()?.id || track?.stream?.id || track?.streamId;
    return typeof id === "string" ? id : "";
  } catch {
    return "";
  }
}

/**
 * Who owns this remote-audio stream, from the conference store.
 * No match returns null. A match with no name still returns the participant id.
 */
export function jitsiNameForStream(state: unknown, streamId: string): JitsiStreamName | null {
  if (typeof streamId !== "string" || !streamId) return null;
  const root = state as {
    ["features/base/participants"]?: { remote?: { forEach?: (fn: (participant: { id?: string; name?: string }, key: string) => void) => void } };
    ["features/base/tracks"]?: any[];
  } | null;
  const tracks = root?.["features/base/tracks"];
  if (!Array.isArray(tracks)) return null;
  const names = new Map<string, string>();
  const remote = root?.["features/base/participants"]?.remote;
  if (remote && typeof remote.forEach === "function") {
    remote.forEach((participant, key) => {
      const id = String(participant?.id ?? key);
      const name = (participant?.name || "").trim();
      if (name) names.set(id, name);
    });
  }
  for (const entry of tracks) {
    if (!entry || entry.local === true) continue;
    if (entry.mediaType && entry.mediaType !== "audio") continue;
    if (jitsiTrackStreamId(entry.jitsiTrack) !== streamId) continue;
    const participantId = typeof entry.participantId === "string" ? entry.participantId : "";
    const displayName = participantId ? (names.get(participantId) || "") : "";
    if (!participantId && !displayName) return null;
    const found: JitsiStreamName = {};
    if (participantId) found.participantId = participantId;
    if (displayName) found.displayName = displayName;
    return found;
  }
  return null;
}

/** Subscribe to remote audio tracks' level events. The redux list is the public
 *  track set; levels themselves are not stored there. A cached level is used
 *  for LEVEL_FRESH_MS only. `muted: true` reads as 0 even when the cache is loud. */
function installLevelReader(): { read: () => JitsiLevelSample[]; stop: () => void } {
  const cache = new Map<string, { level: number; at: number }>();
  const bound = new Map<object, { id: string; off: () => void }>();

  const trackList = (): any[] => {
    const app = (globalThis as any).APP;
    const tracks = app?.store?.getState?.()?.["features/base/tracks"];
    return Array.isArray(tracks) ? tracks : [];
  };
  const remoteAudio = (entry: any): boolean => {
    if (!entry || entry.local === true) return false;
    if (entry.mediaType && entry.mediaType !== "audio") return false;
    return typeof entry.participantId === "string" && !!entry.jitsiTrack;
  };

  const sync = () => {
    const live = new Set<object>();
    for (const entry of trackList()) {
      if (!remoteAudio(entry)) continue;
      const id = entry.participantId as string;
      const jt = entry.jitsiTrack;
      live.add(jt);
      if (bound.has(jt)) continue;
      const cb = (level: number) => {
        if (typeof level === "number") cache.set(id, { level, at: Date.now() });
      };
      const listen = jt.on || jt.addListener || jt.addEventListener;
      if (typeof listen !== "function") continue;
      listen.call(jt, TRACK_AUDIO_LEVEL, cb);
      const off = () => {
        try { jt.off?.(TRACK_AUDIO_LEVEL, cb); } catch { /* already gone */ }
        try { jt.removeListener?.(TRACK_AUDIO_LEVEL, cb); } catch { /* already gone */ }
        try { jt.removeEventListener?.(TRACK_AUDIO_LEVEL, cb); } catch { /* already gone */ }
      };
      bound.set(jt, { id, off });
    }
    for (const [jt, rec] of bound) {
      if (live.has(jt)) continue;
      rec.off();
      bound.delete(jt);
    }
    const still = new Set<string>();
    for (const rec of bound.values()) still.add(rec.id);
    for (const id of cache.keys()) if (!still.has(id)) cache.delete(id);
  };

  return {
    read: () => {
      try {
        sync();
        const now = Date.now();
        const out: JitsiLevelSample[] = [];
        for (const entry of trackList()) {
          if (!remoteAudio(entry)) continue;
          const id = entry.participantId as string;
          if (entry.muted === true) {
            out.push({ id, level: 0 });
            continue;
          }
          const cached = cache.get(id);
          if (!cached || now - cached.at > LEVEL_FRESH_MS) continue;
          out.push({ id, level: cached.level });
        }
        return out;
      } catch {
        return [];
      }
    },
    stop: () => {
      for (const rec of bound.values()) rec.off();
      bound.clear();
      cache.clear();
    },
  };
}

export function createJitsiSpeakers(opts: JitsiSpeakersOptions): JitsiSpeakers {
  const log = opts.log || (() => {});
  const self = (opts.selfName || "").trim().toLowerCase();
  let current: { id: string; name: string } | null = null;
  let mode: "redux" | "dom" | null = null;
  let changes = 0;
  let activeSource: JitsiHintSource | null = null;

  const heartbeatMs = opts.heartbeatMs ?? 2000;
  let lastAssertMs = 0;
  let pendingKey: string | null = null;
  let pendingCount = 0;
  const levels = opts.readLevels ? null : installLevelReader();

  const emit = (name: string, id: string, isEnd: boolean, source: JitsiHintSource) => {
    try { opts.onSpeaking(name, id, isEnd, Date.now(), source); } catch { /* never break capture */ }
  };

  const consider = (next: { id: string; name: string } | null, source: JitsiHintSource) => {
    const key = next?.id ?? null;
    if (key === pendingKey) pendingCount++;
    else { pendingKey = key; pendingCount = 1; }
    if (pendingCount < CONFIRM_POLLS) return;
    activeSource = source;
    if ((current?.id ?? null) === key) {
      if (current && Date.now() - lastAssertMs >= heartbeatMs) {
        emit(current.name, current.id, false, source);
        lastAssertMs = Date.now();
      }
      return;
    }
    if (current) emit(current.name, current.id, true, source);
    if (next) {
      emit(next.name, next.id, false, source);
      lastAssertMs = Date.now();
      log(source === "levels" ? `level speaker → ${next.name}` : `dominant speaker → ${next.name}`);
    }
    current = next;
    changes++;
  };

  const tick = () => {
    let samples: JitsiLevelSample[] = [];
    let levelReadFailed = false;
    try {
      samples = (opts.readLevels ?? levels?.read)?.() ?? [];
    } catch {
      levelReadFailed = true;
      samples = [];
    }
    if (!levelReadFailed && samples.length > 0) {
      mode = "redux";
      const { map, selfId } = participantsFromStore();
      consider(selectJitsiSpeaker(samples, map, selfId), "levels");
      return;
    }

    let d = dominantFromRedux();
    if (d) mode = "redux";
    else { d = dominantFromDom(); if (d) mode = mode ?? "dom"; }
    if (d && self && d.name.trim().toLowerCase() === self) d = null;
    consider(d, "dominant");
  };

  const poll = setInterval(tick, opts.pollMs ?? 400);
  tick();

  return {
    destroy() {
      clearInterval(poll);
      levels?.stop();
      if (current) emit(current.name, current.id, true, activeSource ?? "dominant");
      current = null;
    },
    getState() {
      return { mode, current: current?.name ?? null, changes, source: activeSource };
    },
  };
}
