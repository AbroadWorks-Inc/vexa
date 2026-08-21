import { Page } from 'playwright';
import { BotConfig } from '../../../types';
import { RecordingService } from '../../../services/recording';
import { setActiveRecordingService, getRawCaptureService } from '../../../index';
import { log } from '../../../utils';
import { spawn, ChildProcess } from 'child_process';
import { zoomParticipantNameSelector } from './selectors';
import { dismissZoomPopups } from './prepare';
import { startZoomRichObservation } from './observe';
import { PcmChunker } from './pcm-chunker';

let recordingService: RecordingService | null = null;
let recordingStopResolver: (() => void) | null = null;
let parecordProcess: ChildProcess | null = null;
let speakerPollInterval: NodeJS.Timeout | null = null;
let lastActiveSpeaker: string | null = null;
let activeBotConfig: BotConfig | null = null;
let popupDismissInterval: NodeJS.Timeout | null = null;

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

export async function startZoomWebRecording(page: Page | null, botConfig: BotConfig): Promise<void> {
  if (!page) throw new Error('[Zoom Web] Page required for recording');

  activeBotConfig = botConfig;

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
      const speakerName = await page.evaluate((footerSelector: string) => {
        function nameFromContainer(container: Element | null): string | null {
          if (!container) return null;
          const footer = container.querySelector(footerSelector);
          if (!footer) return null;
          const span = footer.querySelector('span');
          return (span?.textContent?.trim() || (footer as HTMLElement).innerText?.trim()) || null;
        }

        // Layout 1: Normal view — active speaker has a dedicated full-size container
        const name1 = nameFromContainer(document.querySelector('.speaker-active-container__video-frame'));
        if (name1) return name1;

        // Layout 2: Screen-share view — active speaker tile has the --active modifier class
        const name2 = nameFromContainer(document.querySelector('.speaker-bar-container__video-frame--active'));
        if (name2) return name2;

        return null;
      }, zoomParticipantNameSelector);

      if (speakerName && speakerName !== lastActiveSpeaker) {
        // Speaker changed — log to raw capture if active
        const rawCapture = getRawCaptureService();
        if (rawCapture) {
          rawCapture.logSpeakerEvent(lastActiveSpeaker, speakerName);
        }
        if (lastActiveSpeaker) {
          log(`🔇 [Zoom Web] SPEAKER_END: ${lastActiveSpeaker}`);
        }
        lastActiveSpeaker = speakerName;
        log(`🎤 [Zoom Web] SPEAKER_START: ${speakerName}`);
      } else if (!speakerName && lastActiveSpeaker) {
        // No active speaker
        log(`🔇 [Zoom Web] SPEAKER_END: ${lastActiveSpeaker}`);
        lastActiveSpeaker = null;
      }
    } catch {
      // Page may be navigating — ignore
    }
  }, 250);
}

