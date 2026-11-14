/**
 * The bot's LiveKit session, as the capture bridge and the join layer see it.
 *
 * LiveKit's own vocabulary stays here: what crosses out is the session below, never a Room,
 * a reason number or a participant identity (a signed-in person's identity holds their email).
 * The session subscribes to audio only and publishes nothing. Audio itself is not handled here:
 * the shared WebRTC hook mirrors every received track into its own <audio> element.
 */
import { DisconnectReason, RoomEvent, Track } from "livekit-client";
import { createLivekitSpeakers, type LivekitSpeakers } from "./livekit-speakers.js";

/** The part of a livekit-client Room the session uses. */
export interface RoomLike {
  remoteParticipants: Map<string, ParticipantLike>;
  localParticipant: {
    sid: string;
    sendText(text: string, opts: { topic: string; destinationIdentities?: string[] }): Promise<unknown>;
  };
  isE2EEEnabled: boolean;
  on(event: string, handler: (...args: any[]) => void): unknown;
  connect(url: string, token: string, opts: { autoSubscribe: boolean }): Promise<void>;
  disconnect(): Promise<void>;
}

interface ParticipantLike {
  sid: string;
  identity: string;
  name?: string;
  trackPublications: Map<string, PublicationLike>;
}

interface PublicationLike {
  kind: string;
  isEncrypted: boolean;
  setSubscribed(subscribed: boolean): void;
}

interface TrackLike {
  kind: string;
  mediaStreamTrack: { id: string };
}

/** A subscribed remote audio track and whose it is. */
export interface LivekitSubscribedTrack {
  trackId: string;
  sid: string;
  name: string;
}

export interface LivekitRoster {
  participants: number;
  named: number;
  humans: number;
}

export interface LivekitSession {
  /** `e2ee`: a remote participant publishes end-to-end encrypted media; nothing of it is recorded. */
  connect(url: string, token: string): Promise<{ e2ee: boolean }>;
  state(): { connected: boolean; disconnectReason?: string };
  subscribedTracks(): LivekitSubscribedTrack[];
  onSpeaking(cb: (name: string, isEnd: boolean, tMs: number) => void): void;
  /** `ignored`: lower-case name fragments of other notetaker bots, which are not humans. */
  onRoster(ignored: readonly string[], cb: (roster: LivekitRoster & { tMs: number }) => void): void;
  /** Posts the recording notice into the meeting's chat now, and to each participant who joins
   *  later (a text stream reaches only the people connected when it is sent). Never throws. */
  announce(text: string): Promise<boolean>;
  disconnect(): Promise<void>;
}

export interface LivekitSessionOptions {
  log?: (message: string) => void;
}

/** Remote participants by name; an unnamed one is always counted as a human. */
export function countRoster(names: readonly string[], ignored: readonly string[]): LivekitRoster {
  const fragments = ignored.map((f) => f.toLowerCase()).filter((f) => f);
  return {
    participants: names.length,
    named: names.filter((n) => n).length,
    humans: names.filter((n) => !n || !fragments.some((f) => n.toLowerCase().includes(f))).length,
  };
}

/** Chat in Meet's frontend reads text streams on this topic (`@livekit/components-core` useChat). */
const CHAT_TOPIC = "lk.chat";
const HEARTBEAT_TICK_MS = 500;

export function createLivekitSession(room: RoomLike, opts: LivekitSessionOptions = {}): LivekitSession {
  const log = (m: string) => { try { opts.log?.(m); } catch { /* logging never breaks the session */ } };
  let connected = false;
  let disconnectReason: string | undefined;
  const subscribed = new Map<string, { trackId: string; participant: ParticipantLike }>();
  let speakers: LivekitSpeakers | null = null;
  let heartbeat: ReturnType<typeof setInterval> | undefined;
  const rosterListeners: (() => void)[] = [];
  let notice: string | undefined;

  const send = async (text: string, to?: string[]): Promise<boolean> => {
    try {
      await room.localParticipant.sendText(text, to ? { topic: CHAT_TOPIC, destinationIdentities: to } : { topic: CHAT_TOPIC });
      return true;
    } catch (e) {
      log(`chat send failed: ${String(e)}`);
      return false;
    }
  };
  // Audio only, and never an end-to-end encrypted track: the bot holds no key, so it would record noise.
  const subscribeAudio = (pub: PublicationLike) => {
    if (pub.kind !== Track.Kind.Audio) return;
    if (pub.isEncrypted) { log("an end-to-end encrypted audio track is not recorded"); return; }
    pub.setSubscribed(true);
  };
  const rosterChanged = () => { for (const l of rosterListeners) l(); };

  room.on(RoomEvent.TrackPublished, (pub: PublicationLike) => subscribeAudio(pub));
  room.on(RoomEvent.TrackSubscribed, (track: TrackLike, _pub: unknown, participant: ParticipantLike) => {
    if (track.kind !== Track.Kind.Audio) return;
    subscribed.set(track.mediaStreamTrack.id, { trackId: track.mediaStreamTrack.id, participant });
    log(`subscribed audio of ${participant.sid}`);
  });
  room.on(RoomEvent.TrackUnsubscribed, (track: TrackLike, _pub: unknown, participant: ParticipantLike) => {
    if (subscribed.get(track.mediaStreamTrack.id)?.participant === participant) subscribed.delete(track.mediaStreamTrack.id);
  });
  room.on(RoomEvent.ParticipantConnected, (p: ParticipantLike) => {
    log(`participant joined ${p.sid}`);
    rosterChanged();
    if (notice !== undefined) void send(notice, [p.identity]);
  });
  room.on(RoomEvent.ParticipantDisconnected, (p: ParticipantLike) => {
    log(`participant left ${p.sid}`);
    rosterChanged();
  });
  room.on(RoomEvent.ParticipantNameChanged, () => rosterChanged());
  room.on(RoomEvent.ActiveSpeakersChanged, (active: { sid: string; name?: string }[]) => {
    speakers?.update(active.filter((p) => p.sid !== room.localParticipant.sid).map((p) => ({ sid: p.sid, name: p.name ?? "" })));
  });
  room.on(RoomEvent.Reconnecting, () => log("reconnecting"));
  room.on(RoomEvent.Reconnected, () => log("reconnected"));
  room.on(RoomEvent.Disconnected, (reason?: number) => {
    connected = false;
    disconnectReason = reason === undefined ? "UNKNOWN_REASON" : (DisconnectReason[reason] ?? String(reason));
    log(`disconnected: ${disconnectReason}`);
    if (heartbeat !== undefined) clearInterval(heartbeat);
    speakers?.destroy();
  });

  return {
    async connect(url, token) {
      await room.connect(url, token, { autoSubscribe: false });
      connected = true;
      let e2ee = room.isE2EEEnabled;
      for (const p of room.remoteParticipants.values()) {
        for (const pub of p.trackPublications.values()) {
          if (pub.isEncrypted) e2ee = true;
          subscribeAudio(pub);
        }
      }
      log(`connected; ${room.remoteParticipants.size} other participant(s)`);
      return { e2ee };
    },
    state() {
      return disconnectReason === undefined ? { connected } : { connected, disconnectReason };
    },
    subscribedTracks() {
      return [...subscribed.values()].map(({ trackId, participant }) => ({ trackId, sid: participant.sid, name: participant.name ?? "" }));
    },
    onSpeaking(cb) {
      if (speakers) return;
      const s = createLivekitSpeakers({ onSpeaking: cb });
      speakers = s;
      heartbeat = setInterval(() => s.tick(), HEARTBEAT_TICK_MS);
    },
    onRoster(ignored, cb) {
      let last = "";
      const emit = () => {
        const roster = countRoster([...room.remoteParticipants.values()].map((p) => p.name ?? ""), ignored);
        const key = JSON.stringify(roster);
        if (key === last) return;
        last = key;
        cb({ ...roster, tMs: Date.now() });
      };
      rosterListeners.push(emit);
      emit();
    },
    async announce(text) {
      notice = text;
      return send(text);
    },
    async disconnect() {
      await room.disconnect();
    },
  };
}
