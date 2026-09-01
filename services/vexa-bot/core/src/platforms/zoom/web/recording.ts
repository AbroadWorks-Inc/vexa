import { Page } from 'playwright';
import { BotConfig } from '../../../types';
import { RecordingService } from '../../../services/recording';
import { setActiveRecordingService, getRawCaptureService, getSegmentPublisher, armSpeakerBoundaries } from '../../../index';
import { log } from '../../../utils';
import { spawn, ChildProcess } from 'child_process';
import { zoomParticipantNameSelector, zoomGallerySpeakingSelectors } from './selectors';
import { pickZoomActiveSpeaker, type ZoomActiveSpeakerRead, type ZoomActiveSpeakerSource } from '../../../services/zoom-roster';
import { dismissZoomPopups } from './prepare';
import { startZoomRichObservation } from './observe';
import { PcmChunker } from './pcm-chunker';

let recordingService: RecordingService | null = null;
let recordingStopResolver: (() => void) | null = null;
let parecordProcess: ChildProcess | null = null;
let speakerPollInterval: NodeJS.Timeout | null = null;
let lastActiveSpeaker: string | null = null;
/** Which DOM layout last produced a name — logged on change, not per poll. */
let lastActiveSpeakerSource: ZoomActiveSpeakerSource | null = null;
let activeBotConfig: BotConfig | null = null;
let popupDismissInterval: NodeJS.Timeout | null = null;

// ---- Speaker-event bridging (Part C) ----
// Accumulates every genuine speaker change AND periodic heartbeat detected by
// startSpeakerPolling, so index.ts's graceful-leave bridge (isZoomWeb branch)
// can push them into the speaker_events_relative Redis stream. Without this,
// gallery/speaker-view DOM detections never leave this module — see
// getZoomWebSpeakerEvents().
let zoomWebSpeakerEvents: any[] = [];

// A continuously-speaking participant only produces ONE change event (the
// initial SPEAKER_START) — nothing re-affirms them until they stop. Re-emit a
// SPEAKER_START on this cadence for the currently active speaker so a long
// utterance doesn't silently vanish from speaker_timeline.json. Deliberately
// NOT applied to SPEAKER_END (that is a genuine, one-shot transition).
const ZOOM_SPEAKER_HEARTBEAT_MS = 15_000;
let lastHeartbeatMs = 0;

// ---- B1 audio crash-durability: incremental chunk upload ----
// Upstream Vexa buffered the whole meeting into one local /tmp WAV and uploaded
// it in a single POST at graceful leave — a pod SIGKILL mid-meeting lost ALL
// audio. Mirror the Meet/Teams path: stream fixed-size PCM windows to the sidecar
// as they are captured, so already-uploaded chunks survive an ungraceful exit.
// The local WAV (appendPCMBuffer + finalize) is KEPT as a defense-in-depth backup
// but is deduplicated out at the sidecar (it arrives without a chunk_seq).
// The buffering/sequencing itself lives in the pure, unit-tested PcmChunker.
let pcmChunker: PcmChunker | null = null;
let pcmFlushTimer: NodeJS.Timeout | null = null;

// parecord emits s16le / 16 kHz / mono => 2 bytes/sample. 30 s per chunk matches
// Meet's 30 s MediaRecorder timeslice: bounds worst-case loss on SIGKILL to ~30 s
// while keeping upload overhead low. A byte cap and a wall-clock timer both flush,
// so a quiet meeting (which never reaches the byte cap) still checkpoints on time.
const ZOOM_PCM_BYTES_PER_SEC = 16000 * 2;
const ZOOM_CHUNK_SECONDS = 30;
const ZOOM_CHUNK_FLUSH_BYTES = ZOOM_PCM_BYTES_PER_SEC * ZOOM_CHUNK_SECONDS;

// Bound the wait for parecord to drain after SIGTERM before the final flush.
const ZOOM_PARECORD_DRAIN_MS = 2000;

/** True only when a sidecar upload target is configured (production path). */
function incrementalUploadEnabled(): boolean {
  return !!(activeBotConfig?.recordingUploadUrl && activeBotConfig?.token);
}

/**
 * Wait for a killed parecord child to actually drain before the final flush.
 *
 * SIGTERM is asynchronous: parecord can emit one last stdout `data` event AFTER
 * `kill()` returns. Flushing the final chunk immediately would drop that tail
 * slice (and it cannot fall back to the local WAV — the sidecar ignores the
 * chunk-less legacy upload). All `data` events precede stdout `end`, so awaiting
 * `end`/`exit`/`close` guarantees the tail has reached PcmChunker.append first.
 * Bounded by a timeout so teardown can never hang.
 */
function drainParecord(proc: ChildProcess, timeoutMs: number): Promise<void> {
  // Already exited → nothing to wait for.
  if (proc.exitCode !== null || proc.signalCode !== null) return Promise.resolve();
  return new Promise<void>((resolve) => {
    let settled = false;
    const finish = (): void => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve();
    };
    const timer = setTimeout(finish, timeoutMs);
    if (proc.stdout) proc.stdout.once('end', finish);
    proc.once('exit', finish);
    proc.once('close', finish);
  });
}

/** Current DOM-polled active speaker — used by per-speaker pipeline as fallback name */
export function getLastActiveSpeaker(): string | null {
  return lastActiveSpeaker;
}

/**
 * Get accumulated Zoom Web speaker events for persistence via the bot exit
 * callback. Read by index.ts's graceful-leave bridge (isZoomWeb branch) in
 * place of the native-SDK `getZoomSpeakerEvents()`, which is always empty for
 * the web bot.
 */
export function getZoomWebSpeakerEvents(): any[] {
  return zoomWebSpeakerEvents;
}

/**
 * Push a speaker event whose relative-time origin matches the local WAV/PCM
 * capture's zero — `RecordingService.start()` (see services/recording.ts),
 * the same start() call whose `this.startTime` seeds every PCM byte
 * subsequently appended via appendPCMBuffer(). This mirrors the origin the
 * native-Zoom path uses (`audioSessionStartTime`, set right after recording
 * start) and the Google Meet path uses (`audioService.getSessionAudioStartTime()`)
 * — the aw-integration timestamp-overlap mapper assumes events and audio
 * share one zero, so reusing this exact clock (never inventing a new one) is
 * what keeps names aligned to the transcript.
 *
 * No-ops (never throws) when recording isn't active — with no audio session
 * there is no meaningful zero to measure against, and nothing downstream
 * would consume the event anyway.
 *
 * Deliberately NOT `segmentPublisher.sessionStartMs`: in the batch-WhisperX
 * config the transcript is produced by the worker running WhisperX on the
 * uploaded `audio.wav` (whose sample-0 IS this RecordingService.start() zero),
 * and aw-integration's `build_speaker_timeline` emits `relative_sec =
 * relative_ms / 1000` as a PASS-THROUGH (session_start_ts only labels the output
 * timestamp_ms, never the mapping key). So the worker's `map_segments_with_timeline`
 * compares `segment.start` (seconds into the WAV) against `relative_sec`, and the
 * only clock that aligns is the WAV's own. GMeet/Teams reach that same WAV zero
 * indirectly via `SegmentPublisher.resetSessionStart()` (called when their audio
 * capture begins); Zoom Web reaches it directly here.
 *
 * Do NOT "fix" this to `segmentPublisher.sessionStartMs`: that publisher is
 * constructed at pipeline-init (before admission) and, unlike GMeet/Teams, is
 * never `resetSessionStart()`'d for Zoom — so it holds a STALE pre-admission
 * zero, off by the entire admission-wait. `recordingService.getStartTime()` is
 * the only clock tied to the actual recorded WAV.
 */
function pushZoomWebSpeakerEvent(eventType: 'SPEAKER_START' | 'SPEAKER_END', participantName: string): void {
  const sessionStartMs = recordingService?.getStartTime();
  if (!sessionStartMs) return;
  zoomWebSpeakerEvents.push({
    event_type: eventType,
    participant_name: participantName,
    relative_timestamp_ms: Date.now() - sessionStartMs,
  });
}

export async function startZoomWebRecording(page: Page | null, botConfig: BotConfig): Promise<void> {
  if (!page) throw new Error('[Zoom Web] Page required for recording');

  activeBotConfig = botConfig;

  // Reset per-session speaker state up-front (defensive against any future
  // process-reuse; today it is one meeting per pod). Deliberately reset HERE and
  // NOT in stopZoomWebRecording(): stop runs (via leaveZoomWeb) BEFORE index.ts's
  // graceful-leave bridge reads getZoomWebSpeakerEvents() (index.ts:654 then :794),
  // so clearing on stop would drop every event before it is bridged.
  zoomWebSpeakerEvents = [];
  lastHeartbeatMs = 0;
  lastActiveSpeakerSource = null;

  // Recording service
  const wantsAudioCapture =
    !!botConfig.recordingEnabled &&
    (!Array.isArray(botConfig.captureModes) || botConfig.captureModes.includes('audio'));
  const sessionUid = botConfig.connectionId || `zoom-web-${Date.now()}`;

  if (wantsAudioCapture) {
    recordingService = new RecordingService(botConfig.meeting_id, sessionUid);
    setActiveRecordingService(recordingService);
    recordingService.start();
    log('[Zoom Web] Recording service started');

    // Align the per-speaker pipeline's clock to the recording start (mirror
    // googlemeet/recording.ts + msteams/recording.ts). SegmentPublisher is
    // constructed pre-admission, so its speaker events (speaker_events_relative,
    // the stream the worker maps names from) would otherwise be offset by the
    // ENTIRE admission wait — landing past the WhisperX transcript's time range
    // and never mapping (observed live 2026-08-24: speaker events at 654s/767s vs
    // a 0-416s transcript → all SPEAKER_NN despite names being captured). Reset
    // here makes those events recording-relative — the same zero the
    // WhisperX-on-audio.wav transcript uses — so names align and map.
    const publisher = getSegmentPublisher();
    if (publisher) {
      publisher.resetSessionStart();
      log('[Zoom Web] SegmentPublisher session start reset — speaker events now aligned to the recording clock');
    }

    // Arm the audio-activity speaker-boundary machine (mirror
    // googlemeet/recording.ts). Without this, handlePerSpeakerAudioData's
    // markTrackAudioActivity() calls warn "boundaries are NOT armed" and produce
    // no paired source:'audio' START/END events — so the timeline degrades to the
    // coarse one-point-per-`joined` safety valve and the transcript collapses to
    // one block per participant. Arming here + the per-speaker sweep (already
    // started in initPerSpeakerPipeline) yields a dense dominant-speaker timeline.
    armSpeakerBoundaries();
    log('[Zoom Web] Speaker-boundary machine armed — dense per-utterance timeline enabled');

    // B1: arm incremental durability. A wall-clock flush guarantees a checkpoint
    // every ZOOM_CHUNK_SECONDS even if the byte cap is never reached (quiet call).
    pcmChunker = null;
    if (incrementalUploadEnabled()) {
      // url/token are fixed for the session — capture them in the uploader.
      const uploadUrl = botConfig.recordingUploadUrl!;
      const token = botConfig.token!;
      const svc = recordingService;
      pcmChunker = new PcmChunker(
        ZOOM_CHUNK_FLUSH_BYTES,
        // format='pcm': raw headerless s16le/16k/mono. The sidecar concatenates
        // pcm chunks directly into the raw audio the pipeline expects (no ffmpeg).
        (buf, seq, isFinal) => svc.uploadChunk(uploadUrl, token, buf, seq, isFinal, 'pcm'),
        (err, seq, isFinal) =>
          log(`[Zoom Web] PCM chunk ${seq}${isFinal ? ' (final)' : ''} upload failed: ${(err as any)?.message || err}`),
      );
      pcmFlushTimer = setInterval(() => {
        void pcmChunker?.flush(false);
      }, ZOOM_CHUNK_SECONDS * 1000);
      log(`[Zoom Web] Incremental audio durability enabled (${ZOOM_CHUNK_SECONDS}s chunks)`);
    } else {
      log('[Zoom Web] No recordingUploadUrl/token — incremental upload disabled; local WAV only');
    }
  }

  // Start PulseAudio capture from zoom_sink monitor.
  // Zoom web client routes audio through PulseAudio null sink (same as native SDK fallback).
  await startPulseAudioCapture();

  // Start speaker detection polling via DOM
  startSpeakerPolling(page, botConfig);

  // Periodically dismiss popups (AI Companion, chat guest tooltip, etc.)
  popupDismissInterval = setInterval(() => {
    dismissZoomPopups(page).catch(() => {});
  }, 2000);

  // Optional: rich observation harness — enabled by ZOOM_OBSERVE=true
  // Dumps WebRTC stats / per-element audio levels / WebSocket frames /
  // DOM badge / caption availability every 2s for architecture research.
  if (process.env.ZOOM_OBSERVE === 'true') {
    try {
      await startZoomRichObservation(page);
    } catch (e: any) {
      log(`[Zoom Web] ZOOM_OBSERVE harness failed to install: ${e.message}`);
    }
  }

  // Block until stopZoomWebRecording() is called
  await new Promise<void>((resolve) => {
    recordingStopResolver = resolve;
  });
}

export async function stopZoomWebRecording(): Promise<void> {
  log('[Zoom Web] Stopping recording');

  // Stop speaker polling
  if (speakerPollInterval) {
    clearInterval(speakerPollInterval);
    speakerPollInterval = null;
  }

  // Stop popup dismissal
  if (popupDismissInterval) {
    clearInterval(popupDismissInterval);
    popupDismissInterval = null;
  }

  lastActiveSpeaker = null;

  // Unblock the blocking wait
  if (recordingStopResolver) {
    recordingStopResolver();
    recordingStopResolver = null;
  }

  // B1: stop the periodic flush before tearing down capture.
  if (pcmFlushTimer) {
    clearInterval(pcmFlushTimer);
    pcmFlushTimer = null;
  }

  // Stop PulseAudio capture, then WAIT for it to drain before the final flush.
  // SIGTERM is async — parecord can emit one last stdout `data` after the kill,
  // and that tail must reach the chunker before we send is_final (W1 fix).
  const drainingProc = parecordProcess;
  parecordProcess = null;
  if (drainingProc) {
    drainingProc.kill('SIGTERM');
    await drainParecord(drainingProc, ZOOM_PARECORD_DRAIN_MS);
  }

  // B1: flush whatever PCM remains as the final chunk (is_final=true) so the
  // sidecar sees a clean end-of-stream. The final flush also drains the whole
  // serialized queue, so every prior chunk has completed by the time it returns.
  if (pcmChunker) {
    try {
      await pcmChunker.flush(true);
    } catch (err: any) {
      log(`[Zoom Web] Final PCM chunk flush error: ${err?.message || err}`);
    }
    pcmChunker = null;
  }

  activeBotConfig = null;

  if (recordingService) {
    try {
      await recordingService.finalize();
      log('[Zoom Web] Recording finalized');
    } catch (err: any) {
      log(`[Zoom Web] Error finalizing recording: ${err.message}`);
    }
    recordingService = null;
  }
}

export async function reconfigureZoomWebRecording(language: string | null, task: string | null): Promise<void> {
  // Language/task changes are handled at the per-speaker pipeline level.
  log(`[Zoom Web] reconfigure: ignoring (lang=${language}, task=${task})`);
}

export function getZoomWebRecordingService(): RecordingService | null {
  return recordingService;
}

// ---- PulseAudio capture ----

async function startPulseAudioCapture(): Promise<void> {
  return new Promise((resolve, reject) => {
    parecordProcess = spawn('parecord', [
      '--raw',
      '--format=s16le',
      '--rate=16000',
      '--channels=1',
      `--device=${process.env.PULSE_SINK || 'zoom_sink'}.monitor`,
    ]);

    if (!parecordProcess?.stdout) {
      reject(new Error('[Zoom Web] Failed to start parecord'));
      return;
    }

    let started = false;

    parecordProcess.stdout.on('data', (chunk: Buffer) => {
      if (!started) {
        log('[Zoom Web] PulseAudio capture receiving audio');
        started = true;
        resolve();
      }
      // Audio recording only — transcription is handled by the per-speaker pipeline
      // in index.ts (startPerSpeakerAudioCapture → browser ScriptProcessor → handlePerSpeakerAudioData)
      if (recordingService) {
        // Local WAV backup (feeds finalize() + the legacy single-shot upload path).
        recordingService.appendPCMBuffer(chunk);
        // B1: also accumulate for incremental sidecar upload (PcmChunker copies
        // the buffer + auto-flushes on the byte cap).
        pcmChunker?.append(chunk);
      }
    });

    parecordProcess.stderr?.on('data', (data: Buffer) => {
      log(`[Zoom Web] parecord stderr: ${data.toString().trim()}`);
    });

    parecordProcess.on('error', (err: Error) => {
      log(`[Zoom Web] parecord error: ${err.message}`);
      if (!started) reject(err);
    });

    parecordProcess.on('exit', (code, signal) => {
      log(`[Zoom Web] parecord exited: code=${code}, signal=${signal}`);
      parecordProcess = null;
    });

    // Optimistic resolve after 1s even with no data yet
    setTimeout(() => {
      if (!started) {
        log('[Zoom Web] PulseAudio capture started (waiting for data)');
        resolve();
      }
    }, 1000);
  });
}

// ---- Speaker detection via DOM polling ----

function startSpeakerPolling(page: Page, botConfig: BotConfig): void {
  speakerPollInterval = setInterval(async () => {
    if (!page || page.isClosed()) return;
    try {
      // The browser side is DELIBERATELY DUMB: it returns raw candidate strings
      // and nothing else. Every judgement — length, junk, UI labels, the bot's own
      // name, and which layout wins — happens in Node, in `pickZoomActiveSpeaker`,
      // against the SAME predicate `services/speaker-identity.ts` uses.
      //
      // This replaces a hand-copied `looksLikeName()/isSelfName()/accept()` trio
      // whose own comment said it was "mirrored from services/speaker-identity.ts".
      // The mirror drifted: when the leading-lowercase reject was deleted from the
      // source (it made a real participant named "sujoy sarkar" unreturnable), the
      // copy kept it. A predicate passed to `page.evaluate` is serialised into the
      // page and cannot close over an import, so an inline copy is also untestable
      // without a real browser — which is exactly how the drift went unnoticed.
      // Returning raw strings fixes both problems at once.
      const read: ZoomActiveSpeakerRead = await page.evaluate(
        ({ footerSelector, gallerySelectors }: { footerSelector: string; gallerySelectors: string[] }) => {
          function rawFromContainer(container: Element | null): string | null {
            if (!container) return null;
            const footer = container.querySelector(footerSelector);
            if (!footer) return null;
            const span = footer.querySelector('span');
            return (span?.textContent?.trim() || (footer as HTMLElement).innerText?.trim()) || null;
          }

          // Gallery view has no dedicated active-speaker container, so scan the CSS
          // hooks Zoom uses to mark a tile as speaking. A name may sit on the tile's
          // own footer span, on the nearest enclosing avatar tile's footer span, or
          // in an aria-label of the form "X is speaking" / "X is talking".
          function rawFromGalleryElement(el: Element): string | null {
            const directFooter = el.querySelector?.('.video-avatar__avatar-footer');
            if (directFooter) {
              const span = directFooter.querySelector('span');
              const raw = (span?.textContent?.trim() || (directFooter as HTMLElement).innerText?.trim()) || null;
              if (raw) return raw;
            }
            const tile = el.closest?.('.video-avatar__avatar, [class*="video-avatar"]');
            if (tile) {
              const footer = tile.querySelector('.video-avatar__avatar-footer');
              if (footer) {
                const span = footer.querySelector('span');
                const raw = (span?.textContent?.trim() || (footer as HTMLElement).innerText?.trim()) || null;
                if (raw) return raw;
              }
            }
            const aria = el.getAttribute?.('aria-label');
            if (aria) {
              const match = aria.match(/^(.*?)\s+is\s+(?:speaking|talking)$/i);
              if (match?.[1]) return match[1].trim();
            }
            return null;
          }

          const gallery: Array<{ selector: string; raw: Array<string | null> }> = [];
          for (const selector of gallerySelectors) {
            try {
              const matches = Array.from(document.querySelectorAll(selector));
              if (matches.length === 0) continue;
              gallery.push({ selector, raw: matches.map((el) => rawFromGalleryElement(el)) });
            } catch {
              // Malformed selector on this Zoom Web version — skip it.
              continue;
            }
          }

          return {
            // Layout 1: normal view — the active speaker has a full-size container.
            layout1: rawFromContainer(document.querySelector('.speaker-active-container__video-frame')),
            // Layout 2: screen-share view — the active tile carries --active.
            layout2: rawFromContainer(document.querySelector('.speaker-bar-container__video-frame--active')),
            gallery,
          };
        },
        { footerSelector: zoomParticipantNameSelector, gallerySelectors: zoomGallerySpeakingSelectors }
      );

      const picked = pickZoomActiveSpeaker(read, botConfig.botName);
      const speakerName = picked?.name ?? null;
      // Log WHICH layout won, once per change of source. Live, this poll accepted
      // no name for a whole meeting and said nothing about why; the bridge reported
      // "Read 0 speaker events from Zoom Web module" and that was the only clue.
      if (picked && picked.source !== lastActiveSpeakerSource) {
        lastActiveSpeakerSource = picked.source;
        log(`[Zoom Web] Active-speaker source is now ${picked.source}`);
      }

      const now = Date.now();
      if (speakerName && speakerName !== lastActiveSpeaker) {
        // Speaker changed — log to raw capture if active
        const rawCapture = getRawCaptureService();
        if (rawCapture) {
          rawCapture.logSpeakerEvent(lastActiveSpeaker, speakerName);
        }
        if (lastActiveSpeaker) {
          log(`🔇 [Zoom Web] SPEAKER_END: ${lastActiveSpeaker}`);
          pushZoomWebSpeakerEvent('SPEAKER_END', lastActiveSpeaker);
        }
        lastActiveSpeaker = speakerName;
        lastHeartbeatMs = now;
        log(`🎤 [Zoom Web] SPEAKER_START: ${speakerName}`);
        pushZoomWebSpeakerEvent('SPEAKER_START', speakerName);
      } else if (!speakerName && lastActiveSpeaker) {
        // No active speaker
        log(`🔇 [Zoom Web] SPEAKER_END: ${lastActiveSpeaker}`);
        pushZoomWebSpeakerEvent('SPEAKER_END', lastActiveSpeaker);
        lastActiveSpeaker = null;
      } else if (speakerName && speakerName === lastActiveSpeaker && now - lastHeartbeatMs >= ZOOM_SPEAKER_HEARTBEAT_MS) {
        // Same speaker still active for a long stretch — re-affirm so the
        // utterance doesn't silently vanish from speaker_timeline.json
        // (the branches above only fire on a transition). Never heartbeats
        // SPEAKER_END — that stays a one-shot transition.
        pushZoomWebSpeakerEvent('SPEAKER_START', speakerName);
        lastHeartbeatMs = now;
      }
    } catch {
      // Page may be navigating — ignore
    }
  }, 250);
}

