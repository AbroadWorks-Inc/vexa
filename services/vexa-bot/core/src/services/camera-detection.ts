/**
 * Remote-camera detection — pure decision logic.
 *
 * Goal: label a Google Meet meeting "video" if ANY remote participant had
 * their camera on at any point, "audio" if not, and "unknown" if the bot
 * never had a real chance to tell. A wrong "audio" verdict on a genuine
 * video meeting is worse than no verdict at all, so this module is built
 * to fail toward `null` (unknown), never toward `false` (audio), whenever
 * the observation was inconclusive.
 *
 * This file is intentionally free of DOM/Playwright/browser globals so it
 * can run under plain `tsx` and be unit-tested in isolation — see
 * camera-detection.test.ts. The browser-side observation loop (in
 * platforms/googlemeet/recording.ts) only collects raw per-tick facts and
 * ships them to Node via an exposed function; ALL decision logic — what
 * counts as "own track", what counts as a camera, what counts as an
 * observation opportunity — lives here.
 *
 * Locked rules (per product decision):
 *   - Screen sharing does NOT count as video. A screen-share-only meeting
 *     is an AUDIO meeting. Callers gate this via `isPresentationActive`.
 *   - The bot's own outbound virtual-camera track (services/screen-content.ts,
 *     `__vexa_canvas_stream`) must never be mistaken for a participant's
 *     camera. Callers flag this per-element via `isOwnTrack`.
 */

/** One <video> element observed in the page during a single detection tick. */
export interface ObservedVideoElement {
  /**
   * The element's current decoded frame width in pixels (`HTMLVideoElement.videoWidth`).
   * 0 means no real frame is being rendered — e.g. a tile with camera off,
   * a hidden/detached element, or a stream that hasn't started decoding yet.
   */
  videoWidth: number;
  /**
   * True if this element/stream is the bot's own outbound virtual camera
   * (the screen-content.ts canvas track), NOT a remote participant's feed.
   * The caller determines this (e.g. by comparing MediaStreamTrack ids
   * against `__vexa_canvas_stream`), since that check is DOM/runtime-specific.
   */
  isOwnTrack: boolean;
}

/** Result of evaluating a single detection tick. */
export interface CameraTickResult {
  /**
   * True if this tick observed at least one remote (non-own) video element
   * with a real rendered frame, AND no presentation was active.
   */
  sawCamera: boolean;
  /**
   * True if this tick was a genuine opportunity to observe remote camera
   * video at all — i.e. remote participants were actually present. This is
   * independent of `sawCamera`: a tick can have an opportunity and still see
   * no camera (audio-only participants), but a tick with no opportunity can
   * never be used to conclude "audio".
   */
  hadObservationOpportunity: boolean;
}

/**
 * Evaluate one detection tick.
 *
 * `isPresentationActive` gates the ENTIRE tick: while a presentation is
 * active, Google Meet hides camera tiles and may surface the shared screen
 * as a `<video>` element, so nothing in `observedVideos` can be trusted as
 * camera evidence — the tick reports `sawCamera: false` unconditionally.
 *
 * `hasRemoteParticipants` should reflect genuine meeting occupancy (e.g. the
 * same "meeting has started" tile-count gate used elsewhere in the Google
 * Meet monitoring loop), not just "the bot is not alone in the lobby".
 */
export function evaluateRemoteCameraTick(
  observedVideos: ObservedVideoElement[],
  isPresentationActive: boolean,
  hasRemoteParticipants: boolean,
): CameraTickResult {
  const hadObservationOpportunity = hasRemoteParticipants;

  if (isPresentationActive) {
    return { sawCamera: false, hadObservationOpportunity };
  }

  const sawCamera = observedVideos.some((v) => !v.isOwnTrack && v.videoWidth > 0);
  return { sawCamera, hadObservationOpportunity };
}

/** Final verdict for the whole meeting. `null` means unknown — never label it "audio" by default. */
export type MediaStateVerdict = boolean | null;

/**
 * Accumulates per-tick results into a meeting-level verdict.
 *
 * `sawRemoteCamera` is a LATCH: once true, it never resets, giving the
 * "did anyone use video at any point during the meeting" semantics. It is
 * independent of whether the detection machinery ever had a genuine
 * opportunity to observe — `result()` folds both together.
 */
export class RemoteCameraLatch {
  private sawRemoteCamera = false;
  private couldObserve = false;

  /** Fold one tick's result into the running verdict. */
  observe(tick: CameraTickResult): void {
    if (tick.hadObservationOpportunity) {
      this.couldObserve = true;
    }
    if (tick.sawCamera) {
      this.sawRemoteCamera = true;
    }
  }

  /**
   * Final verdict:
   *   - `null`   — detection never had a usable opportunity to observe
   *                (e.g. the bot was alone, or the meeting had no video
   *                elements to enumerate). MUST NOT be reported as "audio".
   *   - `true`   — a remote camera was seen at least once.
   *   - `false`  — observation was possible and no remote camera was ever seen.
   */
  result(): MediaStateVerdict {
    if (!this.couldObserve) {
      return null;
    }
    return this.sawRemoteCamera;
  }
}

/**
 * Wrap an async action so it runs at most once, no matter how many times —
 * or from how many independent call sites — the returned function is
 * invoked. This exists because meeting-end is not a single event: Google
 * Meet's in-page "everyone left" timer, `beforeunload`/`visibilitychange`,
 * and the Node-side graceful-leave path (which itself covers admin removal,
 * host-ended meetings, alone-timeouts, error paths, and SIGTERM/SIGINT) can
 * all race to finalize the same meeting-level verdict. Calling the wrapped
 * action twice must be impossible.
 *
 * Semantics are "at most one ATTEMPT", not "at least one success": the
 * guard flips before `action()` runs, so a first call that throws/rejects
 * still permanently blocks later calls rather than retrying. A page that
 * is already torn down (the common case for e.g. `meeting_ended`) will not
 * become reachable on a second attempt, so retrying would only mask the
 * same failure again — one clean, logged failure is preferable to a silent
 * double-publish or a retry loop. The original error still propagates to
 * the first caller so it can be logged there.
 */
export function createOnceGuard<T>(
  action: () => Promise<T>,
): () => Promise<T | undefined> {
  let started = false;
  return async (): Promise<T | undefined> => {
    if (started) {
      return undefined;
    }
    started = true;
    return action();
  };
}
