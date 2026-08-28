import { Page } from "playwright";
import { spawn, ChildProcess } from "child_process";
import { log } from "../../utils";
import { BotConfig } from "../../types";
import { RecordingService } from "../../services/recording";
import { setActiveRecordingService, getSegmentPublisher, armSpeakerBoundaries } from "../../index";
import { ensureBrowserUtils } from "../../utils/injection";
import {
  evaluateRemoteCameraTick,
  RemoteCameraLatch,
  ObservedVideoElement,
  createOnceGuard,
} from "../../services/camera-detection";
import {
  googleParticipantSelectors,
  googleSpeakingClassNames,
  googleSilenceClassNames,
  googleParticipantContainerSelectors,
  googleNameSelectors,
  googleSpeakingIndicators,
  googlePeopleButtonSelectors
} from "./selectors";

// ---- Node-side continuous audio capture (parecord) ----
//
// The full-session recording is captured in the POD, not in the page: `parecord`
// is a child_process and RecordingService writes with fs.createWriteStream, so
// neither dies with the renderer. This matters because when the HOST ends a Meet
// call the page context is destroyed instantly — the in-page MediaRecorder flush
// never runs and, before this, no full recording was produced at all.
//
// It also removes the audio-duplication bug: MediaRecorder used to BOTH upload
// 30 s chunks and accumulate every chunk into `__vexaRecordedChunks`, whose
// combined blob was then saved on top of those chunks — the meeting twice. The
// 30 s chunk uploads are KEPT (they are the S3 backup and are unchanged); only
// the combined-blob path is gone, and the primary audio is now this WAV.
//
// Deliberately mirrors platforms/zoom/web/recording.ts (the proven production
// implementation) — read the two side by side when changing either.
let meetRecordingService: RecordingService | null = null;
let meetParecordProcess: ChildProcess | null = null;
let meetCaptureStarted = false;
let meetCaptureStopped = false;

// Bound the wait for parecord to drain after SIGTERM before finalizing the WAV.
const MEET_PARECORD_DRAIN_MS = 2000;

/**
 * The PulseAudio monitor source parecord reads from.
 *
 * `meet-bot/start.sh` creates `module-null-sink sink_name=meet_sink` and makes it
 * the default sink, so Chromium's output lands there and `meet_sink.monitor`
 * carries the meeting audio. PULSE_SINK is exported by that same script; the
 * fallback keeps a hand-run bot working if it is unset.
 */
function meetPulseMonitorDevice(): string {
  return `${process.env.PULSE_SINK || "meet_sink"}.monitor`;
}

/**
 * Wait for a killed parecord child to actually drain before finalizing.
 *
 * SIGTERM is asynchronous: parecord can emit one last stdout `data` event AFTER
 * `kill()` returns. Finalizing immediately would drop that tail slice from the
 * WAV. All `data` events precede stdout `end`, so awaiting `end`/`exit`/`close`
 * guarantees the tail has reached appendPCMBuffer first. Bounded by a timeout so
 * teardown can never hang.
 */
function drainMeetParecord(proc: ChildProcess, timeoutMs: number): Promise<void> {
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
    if (proc.stdout) proc.stdout.once("end", finish);
    proc.once("exit", finish);
    proc.once("close", finish);
  });
}

/**
 * Open the WAV and start streaming `meet_sink.monitor` into it.
 *
 * Called from the `__vexaRecordingStarted` bridge — i.e. at the exact moment the
 * in-page MediaRecorder starts, which is also where `publisher.resetSessionStart()`
 * runs. That shared instant is the point: the transcript is produced by WhisperX
 * over this WAV, and aw-integration maps speaker names by comparing segment
 * offsets into the WAV against `relative_ms` measured from `sessionStartMs`. Both
 * clocks must have the same zero or every name lands on the wrong utterance.
 * Starting capture earlier (e.g. at startGoogleRecording) would offset the WAV
 * from the speaker timeline by the media-element wait — 2 s at best, up to 30 s
 * when findMediaElements has to retry.
 *
 * Idempotent: the bridge can fire more than once (recorder restart), and a second
 * `RecordingService.start()` would truncate the file already being written.
 */
async function startMeetAudioCapture(): Promise<void> {
  if (meetCaptureStarted) return;
  const svc = meetRecordingService;
  if (!svc) {
    log("[Google Recording] No recording service — Node-side audio capture skipped.");
    return;
  }
  meetCaptureStarted = true;

  svc.start();
  log("[Google Recording] Recording service started (Node-side WAV)");

  const device = meetPulseMonitorDevice();
  await new Promise<void>((resolve, reject) => {
    const proc = spawn("parecord", [
      "--raw",
      "--format=s16le",
      "--rate=16000",
      "--channels=1",
      `--device=${device}`,
    ]);
    meetParecordProcess = proc;

    if (!proc.stdout) {
      reject(new Error("[Google Recording] Failed to start parecord"));
      return;
    }

    let started = false;

    proc.stdout.on("data", (chunk: Buffer) => {
      if (!started) {
        log(`[Google Recording] PulseAudio capture receiving audio from ${device}`);
        started = true;
        resolve();
      }
      // Audio recording only — transcription is handled by the per-speaker
      // pipeline in index.ts (browser ScriptProcessor → handlePerSpeakerAudioData).
      svc.appendPCMBuffer(chunk);
    });

    proc.stderr?.on("data", (data: Buffer) => {
      log(`[Google Recording] parecord stderr: ${data.toString().trim()}`);
    });

    proc.on("error", (err: Error) => {
      log(`[Google Recording] parecord error: ${err.message}`);
      if (!started) reject(err);
    });

    proc.on("exit", (code, signal) => {
      log(`[Google Recording] parecord exited: code=${code}, signal=${signal}`);
      if (meetParecordProcess === proc) meetParecordProcess = null;
    });

    // Optimistic resolve after 1s even with no data yet — a silent meeting must
    // not block the recording chain.
    setTimeout(() => {
      if (!started) {
        log("[Google Recording] PulseAudio capture started (waiting for data)");
        resolve();
      }
    }, 1000);
  });
}

/**
 * Stop parecord, drain its tail, and finalize the WAV header.
 *
 * Called from performGracefulLeave in index.ts rather than from
 * leaveGoogleMeet(): every ending except a bot-initiated leave arrives either
 * with the page already destroyed (host ended the call) or without
 * startGoogleRecording ever returning (the removal monitor wins the
 * Promise.race in meetingFlow.ts; SIGTERM/SIGINT). performGracefulLeave is the
 * one path all of them share, and it runs before anything reads the file.
 *
 * Idempotent and never throws for "not started" — finalize() is a no-op once the
 * service is already finalized, and RecordingService.upload() finalizes lazily
 * anyway, so a double call cannot corrupt the header.
 */
export async function stopMeetAudioCapture(): Promise<void> {
  if (meetCaptureStopped) return;
  meetCaptureStopped = true;

  const drainingProc = meetParecordProcess;
  meetParecordProcess = null;
  if (drainingProc) {
    log("[Google Recording] Stopping PulseAudio capture");
    drainingProc.kill("SIGTERM");
    await drainMeetParecord(drainingProc, MEET_PARECORD_DRAIN_MS);
  }

  const svc = meetRecordingService;
  meetRecordingService = null;
  if (svc && meetCaptureStarted) {
    try {
      await svc.finalize();
      log("[Google Recording] Recording finalized");
    } catch (err: any) {
      log(`[Google Recording] Error finalizing recording: ${err?.message || err}`);
    }
  }
}

// Modified to use new services - Google Meet recording functionality
export async function startGoogleRecording(page: Page, botConfig: BotConfig): Promise<void> {
  log("Starting Google Meet recording");

  // Reset per-session capture state up-front (defensive against future process
  // reuse; today it is one meeting per pod).
  meetRecordingService = null;
  meetParecordProcess = null;
  meetCaptureStarted = false;
  meetCaptureStopped = false;

  // Reset segment publisher session start to align with recording start.
  // SegmentPublisher was created pre-admission; recording starts post-admission.
  // Without this reset, segment.start_time would be offset by the admission wait time.
  const publisher = getSegmentPublisher();
  if (publisher) {
    publisher.resetSessionStart();
    log(`[Recording] Session start reset to ${new Date(publisher.sessionStartMs).toISOString()}`);
    // The origin is now recording-relative, so audio-derived speaker boundaries may
    // publish. Any onset buffered during the admission wait is flushed by the next
    // sweep against this corrected origin. __vexaRecordingStarted re-aligns again on
    // real MediaRecorder start; residual error for the earliest events is the
    // recording-start → MediaRecorder-start gap (sub-second), not the admission wait.
    armSpeakerBoundaries();
  }

  // Remote-camera detection: a latched, meeting-lifetime verdict on whether any
  // remote participant's camera was ever seen on. Independent of audio capture —
  // media_state describes what participants showed, not whether we recorded audio.
  // ALL decision logic (own-track exclusion, screen-share exclusion, unknown vs.
  // audio vs. video) lives in services/camera-detection.ts and is unit-tested there;
  // this file only ships raw per-tick DOM facts across the exposed function below.
  const cameraLatch = new RemoteCameraLatch();

  await page.exposeFunction(
    "__vexaReportCameraTick",
    (observedVideos: ObservedVideoElement[], isPresentationActive: boolean, hasRemoteParticipants: boolean) => {
      cameraLatch.observe(evaluateRemoteCameraTick(observedVideos, isPresentationActive, hasRemoteParticipants));
    },
  );

  // Called at meeting end, once the whole-meeting verdict is final. Every exit
  // path that has a chance to reach the page (the in-page "everyone left"
  // timer, beforeunload/visibilitychange, AND the Node-side graceful-leave
  // path used by every other ending — admin removal, host-ended meetings,
  // alone-timeouts, error paths, SIGTERM/SIGINT) calls this same function, so
  // it is wrapped in a once-guard: the verdict is published at most once per
  // meeting, no matter which caller(s) reach it or in what order.
  const finalizeCameraDetectionOnce = createOnceGuard(async () => {
    const verdict = cameraLatch.result();
    log(`[Google Recording] Camera detection finalized: sawRemoteCamera=${verdict === null ? "unknown" : verdict}`);
    if (publisher) {
      await publisher.publishMediaState(verdict);
    }
  });
  await page.exposeFunction("__vexaFinalizeCameraDetection", finalizeCameraDetectionOnce);

  const wantsAudioCapture =
    !!botConfig.recordingEnabled &&
    (!Array.isArray(botConfig.captureModes) || botConfig.captureModes.includes("audio"));
  const sessionUid = botConfig.connectionId || `gm-${Date.now()}`;
  let recordingService: RecordingService | null = null;

  if (wantsAudioCapture) {
    recordingService = new RecordingService(botConfig.meeting_id, sessionUid);
    setActiveRecordingService(recordingService);
    // Hand the same service to the Node-side capture path; startMeetAudioCapture()
    // opens the WAV on it when the in-page MediaRecorder starts.
    meetRecordingService = recordingService;

    // NOTE: `__vexaSaveRecordingBlob` is deliberately NOT exposed any more.
    // It called RecordingService.writeBlob(), which (a) landed a full-session
    // combined blob in the sidecar's chunk store ON TOP of the 30 s chunks that
    // had already been uploaded — every Meet transcript contained the meeting
    // twice — and (b) repointed RecordingService.filePath at a `.webm`, which
    // would now clobber the WAV that is the primary audio. The in-page half of
    // that path is gone too (see flushBrowserRecordingBlob below).

    // Pack B (issue #218): incremental chunk upload — each MediaRecorder
    // chunk uploads immediately, so ungraceful exits leave already-uploaded
    // chunks durable in MinIO. `is_final=false` on intermediate chunks;
    // shutdown-path flush sends the last chunk with `is_final=true`.
    await page.exposeFunction(
      "__vexaSaveRecordingChunk",
      async (payload: { base64: string; chunkSeq: number; isFinal: boolean; mimeType?: string }) => {
        try {
          if (!recordingService) {
            log("[Google Recording] Recording service not initialized; dropping chunk.");
            return false;
          }
          if (!botConfig.recordingUploadUrl || !botConfig.token) {
            // No upload URL configured — fall back to the buffered path.
            return false;
          }
          const mimeType = (payload?.mimeType || "").toLowerCase();
          let format = "webm";
          if (mimeType.includes("wav")) format = "wav";
          else if (mimeType.includes("ogg")) format = "ogg";
          else if (mimeType.includes("mp4") || mimeType.includes("m4a")) format = "m4a";

          const buf = Buffer.from(payload.base64 || "", "base64");
          if (!buf.length) return false;

          await recordingService.uploadChunk(
            botConfig.recordingUploadUrl,
            botConfig.token,
            buf,
            payload.chunkSeq,
            !!payload.isFinal,
            format,
          );
          return true;
        } catch (error: any) {
          log(`[Google Recording] uploadChunk failed (seq=${payload?.chunkSeq}): ${error?.message || String(error)}`);
          return false;
        }
      },
    );
  } else {
    log("[Google Recording] Audio capture disabled by config.");
  }

  // Expose callback so the browser can signal when MediaRecorder actually starts.
  // This re-aligns sessionStartMs with the recording, fixing click-to-seek offset.
  //
  // It is also where Node-side PulseAudio capture begins, so the WAV's sample 0
  // and the speaker timeline's zero are the same instant — see
  // startMeetAudioCapture() for why that shared origin is load-bearing.
  await page.exposeFunction("__vexaRecordingStarted", async () => {
    if (publisher) {
      publisher.resetSessionStart();
      log(`[Recording] Session start re-aligned to MediaRecorder start: ${new Date(publisher.sessionStartMs).toISOString()}`);
    }
    if (wantsAudioCapture) {
      try {
        await startMeetAudioCapture();
      } catch (err: any) {
        // Non-fatal: the 30 s chunk uploads are unaffected and remain the backup.
        log(`[Google Recording] Node-side audio capture failed to start: ${err?.message || err}`);
      }
    }
  });

  await ensureBrowserUtils(page, require('path').join(__dirname, '../../browser-utils.global.js'));

  // Pass the necessary config fields and the resolved URL into the page context
  await page.evaluate(
    async (pageArgs: {
      botConfigData: BotConfig;
      selectors: {
        participantSelectors: string[];
        speakingClasses: string[];
        silenceClasses: string[];
        containerSelectors: string[];
        nameSelectors: string[];
        speakingIndicators: string[];
        peopleButtonSelectors: string[];
      };
    }) => {
      const { botConfigData, selectors } = pageArgs;

      // Use browser utility classes from the global bundle
      const browserUtils = (window as any).VexaBrowserUtils;
      (window as any).logBot(`Browser utils available: ${Object.keys(browserUtils || {}).join(', ')}`);

      const audioService = new browserUtils.BrowserAudioService({
        targetSampleRate: 16000,
        bufferSize: 4096,
        inputChannels: 1,
        outputChannels: 1
      });

      (window as any).__vexaAudioService = audioService;
      (window as any).__vexaBotConfig = botConfigData;
      (window as any).__vexaMediaRecorder = null;
      (window as any).__vexaRecordingFlushed = false;
      // NOTE: `__vexaRecordedChunks` is gone. Each MediaRecorder blob is uploaded
      // as a 30 s backup chunk and then dropped; nothing re-reads them. Keeping
      // them was what produced the doubled audio (the combined blob was saved on
      // top of the same chunks), and it also held the whole meeting in renderer
      // heap — ~200 MB on a 1-hour call.

      const isAudioRecordingEnabled =
        !!(botConfigData as any)?.recordingEnabled &&
        (!Array.isArray((botConfigData as any)?.captureModes) ||
          (botConfigData as any)?.captureModes.includes("audio"));

      const getSupportedMediaRecorderMimeType = (): string => {
        const candidates = [
          "audio/webm;codecs=opus",
          "audio/webm",
          "audio/ogg;codecs=opus",
          "audio/ogg",
        ];
        for (const mime of candidates) {
          try {
            if ((window as any).MediaRecorder?.isTypeSupported?.(mime)) {
              return mime;
            }
          } catch {}
        }
        return "";
      };

      const flushBrowserRecordingBlob = async (reason: string): Promise<void> => {
        if (!isAudioRecordingEnabled) return;
        if ((window as any).__vexaRecordingFlushed) return;

        try {
          const recorder: MediaRecorder | null = (window as any).__vexaMediaRecorder;
          // Number of backup chunks emitted so far. Replaces the old
          // `__vexaRecordedChunks.length` check below: `__vexaChunkSeq` is
          // incremented by exactly the same `ondataavailable` events that used to
          // push into that array, so `> 0` is the identical condition without
          // retaining the blobs.
          const emittedChunks: number = ((window as any).__vexaChunkSeq as number) ?? 0;

          const finalizeAndSend = async () => {
            if ((window as any).__vexaRecordingFlushed) return;
            (window as any).__vexaRecordingFlushed = true;

            try {
              // Pack B (issue #218): the shutdown path is a single final-chunk
              // POST with is_final=true. The `recorder.stop()` call just before
              // this flush triggers one last dataavailable event that already
              // uploaded as is_final=false; now we send an empty-body "finalizer"
              // with the next chunk_seq so the sidecar sees a clean end of the
              // backup-chunk stream.
              //
              // The legacy full-blob fallback that used to follow is GONE: it
              // combined every retained chunk into one blob and posted it through
              // `__vexaSaveRecordingBlob`, landing the whole meeting a second time
              // on top of the chunks already uploaded. The primary full-session
              // audio is now the Node-side WAV (startMeetAudioCapture), which also
              // survives the page being destroyed when the host ends the call —
              // something this in-page path never could.
              const mimeType =
                (window as any).__vexaMediaRecorder?.mimeType || "audio/webm";
              const chunkSeq = ((window as any).__vexaChunkSeq as number) ?? 0;

              if (typeof (window as any).__vexaSaveRecordingChunk === "function") {
                try {
                  await (window as any).__vexaSaveRecordingChunk({
                    base64: "",
                    chunkSeq,
                    isFinal: true,
                    mimeType,
                  });
                  (window as any).logBot?.(
                    `[Google Recording] Finalized recording (chunk_seq=${chunkSeq}, ${reason}).`
                  );
                } catch (err: any) {
                  (window as any).logBot?.(
                    `[Google Recording] Chunk finalizer failed: ${err?.message || err}`
                  );
                }
              } else {
                (window as any).logBot?.(
                  `[Google Recording] Chunk sink not available — nothing to finalize in-page (${reason}).`
                );
              }
            } catch (err: any) {
              (window as any).logBot?.(
                `[Google Recording] Failed to flush blob: ${err?.message || err}`
              );
            }
          };

          if (recorder && recorder.state !== "inactive") {
            await new Promise<void>((resolveStop) => {
              const onStop = async () => {
                recorder.removeEventListener("stop", onStop as any);
                await finalizeAndSend();
                resolveStop();
              };
              recorder.addEventListener("stop", onStop as any, { once: true });
              try {
                recorder.stop();
              } catch {
                // Recorder may already be stopping; resolve after a short delay.
                setTimeout(async () => {
                  await finalizeAndSend();
                  resolveStop();
                }, 200);
              }
            });
          } else if (emittedChunks > 0) {
            await finalizeAndSend();
          }
        } catch (err: any) {
          (window as any).logBot?.(
            `[Google Recording] Unexpected flush error: ${err?.message || err}`
          );
        }
      };

      (window as any).__vexaFlushRecordingBlob = flushBrowserRecordingBlob;

      await new Promise<void>((resolve, reject) => {
        try {
          (window as any).logBot("Starting Google Meet recording process with new services.");
          
          // Wait a bit for media elements to initialize after admission, then start the chain
          (async () => {
            let degradedNoMedia = false;
            // Wait 2 seconds for media elements to initialize after admission
            (window as any).logBot("Waiting 2 seconds for media elements to initialize after admission...");
            await new Promise(resolve => setTimeout(resolve, 2000));
            
            // Find and create combined audio stream with enhanced retry logic
            // Use 10 retries with 3s delay = 30s total wait time
            audioService.findMediaElements(10, 3000).then(async (mediaElements: HTMLMediaElement[]) => {
            if (mediaElements.length === 0) {
              degradedNoMedia = true;
              (window as any).logBot(
                "[Google Meet BOT Warning] No active media elements found after retries; " +
                "continuing in degraded monitoring mode (session remains active)."
              );
              return undefined;
            }

            // Create combined audio stream
            return await audioService.createCombinedAudioStream(mediaElements);
          }).then(async (combinedStream: MediaStream | undefined) => {
            if (!combinedStream) {
              if (!degradedNoMedia) {
                reject(new Error("[Google Meet BOT Error] Failed to create combined audio stream"));
                return;
              }
              return null;
            }

            if (isAudioRecordingEnabled) {
              try {
                const mimeType = getSupportedMediaRecorderMimeType();
                const recorderOptions = mimeType ? ({ mimeType } as MediaRecorderOptions) : undefined;
                const recorder = recorderOptions
                  ? new MediaRecorder(combinedStream, recorderOptions)
                  : new MediaRecorder(combinedStream);

                (window as any).__vexaMediaRecorder = recorder;
                (window as any).__vexaRecordingFlushed = false;
                (window as any).__vexaChunkSeq = 0;

                // Pack B (issue #218): upload each chunk immediately to MinIO
                // via meeting-api, rather than buffering the whole WebM until
                // shutdown. On ungraceful exit, already-uploaded chunks are
                // durable. These 30 s webm chunks are the S3 backup copy; the
                // primary full-session audio is the Node-side WAV. The chunk is
                // NOT retained after upload — retaining it was half of the
                // audio-duplication bug (see flushBrowserRecordingBlob).
                recorder.ondataavailable = async (event: BlobEvent) => {
                  if (!(event.data && event.data.size > 0)) {
                    (window as any).logBot?.("[Google Recording] dataavailable fired with empty data (skipping)");
                    return;
                  }

                  // Best-effort immediate upload; do NOT block the recorder.
                  const chunkSeq = (window as any).__vexaChunkSeq as number;
                  (window as any).__vexaChunkSeq = chunkSeq + 1;
                  try {
                    const mimeType = recorder.mimeType || "audio/webm";
                    const buffer = await event.data.arrayBuffer();
                    const bytes = new Uint8Array(buffer);
                    let binary = "";
                    const encodeChunkSize = 0x8000;
                    for (let i = 0; i < bytes.length; i += encodeChunkSize) {
                      binary += String.fromCharCode(...bytes.subarray(i, i + encodeChunkSize));
                    }
                    const base64 = btoa(binary);
                    if (typeof (window as any).__vexaSaveRecordingChunk === "function") {
                      (window as any).logBot?.(
                        `[Google Recording] Uploading chunk ${chunkSeq} (${bytes.length} bytes)`
                      );
                      // Await so a thrown error surfaces. Failures here
                      // previously went silent (fire-and-forget), which
                      // masked compose recording regression in Bug A 2026-04-21.
                      const ok = await (window as any).__vexaSaveRecordingChunk({
                        base64,
                        chunkSeq,
                        isFinal: false,
                        mimeType,
                      });
                      if (!ok) {
                        (window as any).logBot?.(
                          `[Google Recording] Chunk ${chunkSeq} upload returned false — bot-side sink rejected (see bot-container logs)`
                        );
                      }
                    } else {
                      (window as any).logBot?.(
                        `[Google Recording] __vexaSaveRecordingChunk not exposed — backup chunk ${chunkSeq} dropped (the Node-side WAV is unaffected)`
                      );
                    }
                  } catch (err: any) {
                    (window as any).logBot?.(
                      `[Google Recording] Chunk ${chunkSeq} upload prep/send FAILED: ${err?.message || err}`
                    );
                  }
                };

                // 30-second chunks: balances upload overhead with data-loss
                // granularity on SIGKILL. Rarely shorter chunks are also fine
                // — the server-side endpoint appends by chunk_seq.
                recorder.start(30000);
                // Signal Node.js that recording started — re-aligns segment timestamps
                (window as any).__vexaRecordingStarted?.();
                (window as any).logBot?.(
                  `[Google Recording] MediaRecorder started (${recorder.mimeType || mimeType || "default"}).`
                );
              } catch (err: any) {
                (window as any).logBot?.(
                  `[Google Recording] Failed to start MediaRecorder: ${err?.message || err}`
                );
              }
            }

            // Initialize audio processor
            return await audioService.initializeAudioProcessor(combinedStream);
          }).then(async (processor: any) => {
            if (!processor) {
              return null;
            }
            // Setup audio data processing
            // Audio data processor — no-op now; per-speaker pipeline handles transcription
            audioService.setupAudioDataProcessor(async (_audioData: Float32Array, _sessionStartTime: number | null) => {
              // Per-speaker pipeline (speaker-streams.ts) handles transcription.
              // This processor is kept for MediaRecorder / recording only.
            });

            return null;
          }).then(() => {
            // Initialize Google-specific speaker detection (Teams-style with Google selectors)
            if (!degradedNoMedia) {
              (window as any).logBot("Initializing Google Meet speaker detection...");
            }

            const initializeGoogleSpeakerDetection = (audioService: any, botConfigData: any) => {
              const selectorsTyped = selectors as any;

              const speakingStates = new Map<string, string>();
              function hashStr(s: string): string {
                // small non-crypto hash to avoid logging PII
                let h = 5381;
                for (let i = 0; i < s.length; i++) h = ((h << 5) + h) ^ s.charCodeAt(i);
                return (h >>> 0).toString(16).slice(0, 8);
              }

              function getGoogleParticipantId(element: HTMLElement) {
                let id = element.getAttribute('data-participant-id');
                if (!id) {
                  const stableChild = element.querySelector('[jsinstance]') as HTMLElement | null;
                  if (stableChild) {
                    id = stableChild.getAttribute('jsinstance') || undefined as any;
                  }
                }
                if (!id) {
                  if (!(element as any).dataset.vexaGeneratedId) {
                    (element as any).dataset.vexaGeneratedId = 'gm-id-' + Math.random().toString(36).substr(2, 9);
                  }
                  id = (element as any).dataset.vexaGeneratedId;
                }
                return id as string;
              }

              function getGoogleParticipantName(participantElement: HTMLElement) {
                // Prefer explicit Meet name spans
                const notranslate = participantElement.querySelector('span.notranslate') as HTMLElement | null;
                if (notranslate && notranslate.textContent && notranslate.textContent.trim()) {
                  const t = notranslate.textContent.trim();
                  if (t.length > 1 && t.length < 50) return t;
                }

                // Try configured name selectors
                const nameSelectors: string[] = selectorsTyped.nameSelectors || [];
                for (const sel of nameSelectors) {
                  const el = participantElement.querySelector(sel) as HTMLElement | null;
                  if (el) {
                    let nameText = el.textContent || el.innerText || el.getAttribute('data-self-name') || el.getAttribute('aria-label') || '';
                    if (nameText) {
                      nameText = nameText.trim();
                      if (nameText && nameText.length > 1 && nameText.length < 50) return nameText;
                    }
                  }
                }

                // Helper: reject junk names (fallback-generated IDs, not real names)
                const isJunkName = (name: string): boolean => {
                  return /^Google Participant \(/.test(name) ||
                         /spaces\//.test(name) ||
                         /devices\//.test(name);
                };

                // Fallbacks
                const selfName = participantElement.getAttribute('data-self-name');
                if (selfName && selfName.trim() && !isJunkName(selfName.trim())) return selfName.trim();

                // aria-label on the container or any descendant (catches Spaces/Chat device participants)
                const ariaLabel = participantElement.getAttribute('aria-label');
                if (ariaLabel && ariaLabel.trim().length > 1 && ariaLabel.trim().length < 50 && !isJunkName(ariaLabel.trim())) return ariaLabel.trim();
                const ariaChild = participantElement.querySelector('[aria-label]') as HTMLElement | null;
                if (ariaChild) {
                  const childLabel = ariaChild.getAttribute('aria-label')?.trim();
                  if (childLabel && childLabel.length > 1 && childLabel.length < 50 && !isJunkName(childLabel)) return childLabel;
                }

                // data-tooltip on any descendant
                const tooltipEl = participantElement.querySelector('[data-tooltip]') as HTMLElement | null;
                if (tooltipEl) {
                  const tooltip = tooltipEl.getAttribute('data-tooltip')?.trim();
                  if (tooltip && tooltip.length > 1 && tooltip.length < 50 && !isJunkName(tooltip)) return tooltip;
                }

                const idToDisplay = getGoogleParticipantId(participantElement);
                return `Google Participant (${idToDisplay})`;
              }

              function isVisible(el: HTMLElement): boolean {
                const cs = getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                const ariaHidden = el.getAttribute('aria-hidden') === 'true';
                return (
                  rect.width > 0 &&
                  rect.height > 0 &&
                  cs.display !== 'none' &&
                  cs.visibility !== 'hidden' &&
                  cs.opacity !== '0' &&
                  !ariaHidden
                );
              }

              function hasSpeakingIndicator(container: HTMLElement): boolean {
                const indicators: string[] = selectorsTyped.speakingIndicators || [];
                for (const sel of indicators) {
                  const ind = container.querySelector(sel) as HTMLElement | null;
                  if (ind && isVisible(ind)) return true;
                }
                return false;
              }

              function inferSpeakingFromClasses(container: HTMLElement, mutatedClassList?: DOMTokenList): { speaking: boolean } {
                const speakingClasses: string[] = selectorsTyped.speakingClasses || [];
                const silenceClasses: string[] = selectorsTyped.silenceClasses || [];

                const classList = mutatedClassList || container.classList;
                const descendantSpeaking = speakingClasses.some(cls => container.querySelector('.' + cls));
                const hasSpeaking = speakingClasses.some(cls => classList.contains(cls)) || descendantSpeaking;
                const hasSilent = silenceClasses.some(cls => classList.contains(cls));
                if (hasSpeaking) return { speaking: true };
                if (hasSilent) return { speaking: false };
                return { speaking: false };
              }

              function sendGoogleSpeakerEvent(eventType: string, participantElement: HTMLElement) {
                const sessionStartTime = audioService.getSessionAudioStartTime();
                if (sessionStartTime === null) {
                  return;
                }
                const relativeTimestampMs = Date.now() - sessionStartTime;
                const participantId = getGoogleParticipantId(participantElement);
                const participantName = getGoogleParticipantName(participantElement);
                // Accumulate for persistence (direct bot accumulation)
                (window as any).__vexaSpeakerEvents = (window as any).__vexaSpeakerEvents || [];
                (window as any).__vexaSpeakerEvents.push({
                  event_type: eventType,
                  participant_name: participantName,
                  participant_id: participantId,
                  relative_timestamp_ms: relativeTimestampMs,
                });
              }

              // Debug: log all class mutations to discover current Google Meet speaking classes
              let classMutationCount = 0;
              function debugClassMutation(participantElement: HTMLElement, mutatedClassList?: DOMTokenList) {
                classMutationCount++;
                // Log first 20 mutations and then every 50th to avoid spam
                if (classMutationCount <= 20 || classMutationCount % 50 === 0) {
                  const id = getGoogleParticipantId(participantElement);
                  const name = getGoogleParticipantName(participantElement);
                  const classes = mutatedClassList ? Array.from(mutatedClassList).join(' ') : '(no classList)';
                  (window as any).logBot(`[SpeakerDebug] #${classMutationCount} ${name} (${id}): classes=[${classes}]`);
                }
              }

              function logGoogleSpeakerEvent(participantElement: HTMLElement, mutatedClassList?: DOMTokenList) {
                const participantId = getGoogleParticipantId(participantElement);
                const participantName = getGoogleParticipantName(participantElement);
                const previousLogicalState = speakingStates.get(participantId) || 'silent';

                // Debug: log class mutations
                debugClassMutation(participantElement, mutatedClassList);

                // Primary: indicators; Fallback: classes
                const indicatorSpeaking = hasSpeakingIndicator(participantElement);
                const classInference = inferSpeakingFromClasses(participantElement, mutatedClassList);
                const isCurrentlySpeaking = indicatorSpeaking || classInference.speaking;

                if (isCurrentlySpeaking) {
                  if (previousLogicalState !== 'speaking') {
                    (window as any).logBot(`[SpeakerDebug] SPEAKING START: ${participantName} (indicator=${indicatorSpeaking}, classInference=${classInference.speaking})`);
                    sendGoogleSpeakerEvent('SPEAKER_START', participantElement);
                  }
                  speakingStates.set(participantId, 'speaking');
                } else {
                  if (previousLogicalState === 'speaking') {
                    (window as any).logBot(`[SpeakerDebug] SPEAKING END: ${participantName}`);
                    sendGoogleSpeakerEvent('SPEAKER_END', participantElement);
                  }
                  speakingStates.set(participantId, 'silent');
                }
              }

              function observeGoogleParticipant(participantElement: HTMLElement) {
                const participantId = getGoogleParticipantId(participantElement);
                speakingStates.set(participantId, 'silent');

                // Initial scan
                logGoogleSpeakerEvent(participantElement);

                const callback = function(mutationsList: MutationRecord[]) {
                  for (const mutation of mutationsList) {
                    if (mutation.type === 'attributes' && mutation.attributeName === 'class') {
                      const targetElement = mutation.target as HTMLElement;
                      if (participantElement.contains(targetElement) || participantElement === targetElement) {
                        logGoogleSpeakerEvent(participantElement, targetElement.classList);
                      }
                    }
                  }
                };

                const observer = new MutationObserver(callback);
                observer.observe(participantElement, {
                  attributes: true,
                  attributeFilter: ['class'],
                  subtree: true
                });

                if (!(participantElement as any).dataset.vexaObserverAttached) {
                  (participantElement as any).dataset.vexaObserverAttached = 'true';
                }
              }

              function scanForAllGoogleParticipants() {
                const participantSelectors: string[] = selectorsTyped.participantSelectors || [];
                // Debug: dump participant tile structure on first scan
                (window as any).logBot(`[SpeakerDebug] Scanning for participants with selectors: ${participantSelectors.join(', ')}`);
                let foundCount = 0;
                for (const sel of participantSelectors) {
                  document.querySelectorAll(sel).forEach((el) => {
                    foundCount++;
                    const elh = el as HTMLElement;
                    const outerClasses = elh.className;
                    const childClasses = Array.from(elh.querySelectorAll('*')).slice(0, 5).map(c => (c as HTMLElement).className).filter(Boolean);
                    (window as any).logBot(`[SpeakerDebug] Participant tile (${sel}): classes=[${outerClasses}], children=[${childClasses.join(' | ')}], innerHTML=${elh.innerHTML.substring(0, 200)}`);
                  });
                }
                (window as any).logBot(`[SpeakerDebug] Found ${foundCount} participant tiles total`);
                for (const sel of participantSelectors) {
                  document.querySelectorAll(sel).forEach((el) => {
                    const elh = el as HTMLElement;
                    if (!(elh as any).dataset.vexaObserverAttached) {
                      observeGoogleParticipant(elh);
                    }
                  });
                }
              }

              // Attempt to click People button to stabilize DOM if available
              try {
                const peopleSelectors: string[] = selectorsTyped.peopleButtonSelectors || [];
                for (const sel of peopleSelectors) {
                  const btn = document.querySelector(sel) as HTMLElement | null;
                  if (btn && isVisible(btn)) { btn.click(); break; }
                }
              } catch {}

              // Initialize
              scanForAllGoogleParticipants();

              // Expose participant name lookup to Node (used by speaker-identity.ts)
              // Returns a map of all known participant names from DOM tiles,
              // keyed by participant-id, plus a list of currently-speaking names.
              (window as any).__vexaGetAllParticipantNames = (): { names: Record<string, string>; speaking: string[] } => {
                const names: Record<string, string> = {};
                const speaking: string[] = [];
                const participantSelectors: string[] = selectorsTyped.participantSelectors || [];
                const seen = new Set<string>();
                participantSelectors.forEach(sel => {
                  document.querySelectorAll(sel).forEach(el => {
                    const elh = el as HTMLElement;
                    const id = getGoogleParticipantId(elh);
                    if (seen.has(id)) return;
                    seen.add(id);
                    const name = getGoogleParticipantName(elh);
                    names[id] = name;
                    if (speakingStates.get(id) === 'speaking') {
                      speaking.push(name);
                    }
                  });
                });
                return { names, speaking };
              };

              // Polling fallback to catch speaking indicators not driven by class mutations
              const lastSpeakingById = new Map<string, boolean>();
              setInterval(() => {
                const participantSelectors: string[] = selectorsTyped.participantSelectors || [];
                const elements: HTMLElement[] = [];
                participantSelectors.forEach(sel => {
                  document.querySelectorAll(sel).forEach(el => elements.push(el as HTMLElement));
                });
                elements.forEach((container) => {
                  const id = getGoogleParticipantId(container);
                  const indicatorSpeaking = hasSpeakingIndicator(container) || inferSpeakingFromClasses(container).speaking;
                  const prev = lastSpeakingById.get(id) || false;
                  if (indicatorSpeaking && !prev) {
                    // Poll speaker start — debug level
                    sendGoogleSpeakerEvent('SPEAKER_START', container);
                    lastSpeakingById.set(id, true);
                    speakingStates.set(id, 'speaking');
                  } else if (!indicatorSpeaking && prev) {
                    // Poll speaker end — debug level
                    sendGoogleSpeakerEvent('SPEAKER_END', container);
                    lastSpeakingById.set(id, false);
                    speakingStates.set(id, 'silent');
                  } else if (!lastSpeakingById.has(id)) {
                    lastSpeakingById.set(id, indicatorSpeaking);
                  }
                });
              }, 500);
            };

            if (!degradedNoMedia) {
              initializeGoogleSpeakerDetection(audioService, botConfigData);
            }

            // Participant counting: uses data-participant-id tiles, but falls back to
            // "Leave call" button visibility to avoid false-positive "alone" during screen share.
            // Google Meet removes participant tiles from the DOM during presentation mode,
            // but the "Leave call" button remains visible as long as the bot is in the meeting.
            (window as any).logBot("Initializing participant counting (data-participant-id + leave-button fallback)...");

            let lastKnownParticipantCount = 0;

            const countParticipantTiles = (): number => {
              const participantElements = document.querySelectorAll('[data-participant-id]');
              const ids = new Set<string>();
              participantElements.forEach((el: Element) => {
                const id = el.getAttribute('data-participant-id');
                if (id) ids.add(id);
              });
              return ids.size;
            };

            const isBotStillInMeeting = (): boolean => {
              // The in-call control bar (Leave call / mic / camera) AUTO-HIDES during
              // presentation and idle, so keying "in meeting" off a single "Leave call"
              // selector gives false negatives — this is what caused the screen-share
              // early-exit bug (log: `0 tiles, inMeeting=false`). Instead: we are still
              // in the meeting UNLESS the explicit "left / removed / rejoin" screen shows.
              const bodyText = document.body ? document.body.innerText : '';
              const leftScreen =
                document.querySelector('button[aria-label*="Rejoin"]') !== null ||
                document.querySelector('button[aria-label*="Ask to rejoin"]') !== null ||
                document.querySelector('button[aria-label*="Return to home"]') !== null ||
                /you left the meeting|you've been removed|removed you from the meeting|return to home screen/i.test(bodyText);
              if (leftScreen) return false;
              // Positive confirmation of a live call surface that survives toolbar hiding:
              // participant tiles, the shared-screen/camera <video>, the meeting title,
              // or the (possibly hidden) Leave button.
              const onCallSurface =
                document.querySelector('[data-participant-id]') !== null ||
                document.querySelector('video') !== null ||
                document.querySelector('[data-meeting-title]') !== null ||
                document.querySelector('button[aria-label*="Leave call"]') !== null;
              return onCallSurface;
            };

            (window as any).getGoogleMeetActiveParticipants = () => {
              const tileCount = countParticipantTiles();
              const inMeeting = isBotStillInMeeting();
              // If tiles show 0 but we're still in the meeting (e.g. screen share mode),
              // keep the last known count (minimum 2) to avoid false "alone" triggers
              if (tileCount === 0 && inMeeting && lastKnownParticipantCount > 1) {
                (window as any).logBot(`🔍 [Google Meet Participants] 0 tiles but Leave button present — keeping last count ${lastKnownParticipantCount} (screen share mode)`);
                return new Array(lastKnownParticipantCount).fill('placeholder');
              }
              if (tileCount > 0) {
                lastKnownParticipantCount = tileCount;
              }
              // Only log participant count changes, not every poll
              if (tileCount !== lastKnownParticipantCount) {
                (window as any).logBot(`🔍 [Google Meet Participants] ${tileCount} tiles, inMeeting=${inMeeting}`);
              }
              return new Array(tileCount).fill('placeholder');
            };
            (window as any).getGoogleMeetActiveParticipantsCount = () => {
              return (window as any).getGoogleMeetActiveParticipants().length;
            };

            // Detect an active presentation / screen-share. A share hides the participant
            // tiles, so the "everyone left" finalize below must NOT treat tiles==0 as empty
            // while a presentation is active (that was the old false-empty bug).
            const isPresentationActive = () => {
              if (
                document.querySelector('button[aria-label*="Stop presenting"]') !== null ||
                document.querySelector('[aria-label*="is presenting"]') !== null ||
                document.querySelector('[aria-label*="Pin presentation"]') !== null ||
                document.querySelector('[aria-label*="Unpin presentation"]') !== null
              ) return true;
              const bodyText = document.body ? document.body.innerText : '';
              return /\bis presenting\b|you are presenting|presenting to everyone/i.test(bodyText);
            };

            // Remote-camera detection (raw DOM facts only — see camera-detection.ts on the
            // Node side for the actual decision logic). DOM-structural + track-identity checks
            // only: NOT CSS class names, which Meet rotates (see selectors.ts history).
            //
            // "Own track" = the bot's own outbound virtual camera (services/screen-content.ts,
            // window.__vexa_canvas_stream). We identify it by MediaStreamTrack id rather than
            // any DOM position/class, since that survives Meet UI changes and correctly
            // excludes the bot's own tile even if Meet ever renders a local self-view from it.
            const collectObservedVideos = (): Array<{ videoWidth: number; isOwnTrack: boolean }> => {
              const canvasStream = (window as any).__vexa_canvas_stream as MediaStream | undefined;
              const ownTrackIds = new Set<string>(
                canvasStream && typeof canvasStream.getVideoTracks === 'function'
                  ? canvasStream.getVideoTracks().map((t) => t.id)
                  : []
              );
              const videos = Array.from(document.querySelectorAll('video')) as HTMLVideoElement[];
              return videos.map((v) => {
                let isOwnTrack = false;
                const srcObject = v.srcObject as MediaStream | null;
                if (srcObject && typeof srcObject.getVideoTracks === 'function') {
                  isOwnTrack = srcObject.getVideoTracks().some((t) => ownTrackIds.has(t.id));
                }
                return { videoWidth: v.videoWidth || 0, isOwnTrack };
              });
            };

            // Setup Google Meet meeting monitoring (browser context)
            const setupGoogleMeetingMonitoring = (botConfigData: any, audioService: any, resolve: any) => {
              (window as any).logBot("Setting up Google Meet meeting monitoring...");
              
              let lastParticipantCount = -1;
              let monitoringStopped = false;

              const stopWithFlush = async (
                reason: string,
                finish: () => void
              ) => {
                if (monitoringStopped) return;
                monitoringStopped = true;
                clearInterval(checkInterval);
                try {
                  if (typeof (window as any).__vexaFlushRecordingBlob === "function") {
                    await (window as any).__vexaFlushRecordingBlob(reason);
                  }
                } catch (flushErr: any) {
                  (window as any).logBot?.(
                    `[Google Recording] Flush error during shutdown (${reason}): ${flushErr?.message || flushErr}`
                  );
                }
                try {
                  if (typeof (window as any).__vexaFinalizeCameraDetection === "function") {
                    await (window as any).__vexaFinalizeCameraDetection();
                  }
                } catch (camErr: any) {
                  (window as any).logBot?.(
                    `[CameraDetection] Finalize error during shutdown (${reason}): ${camErr?.message || camErr}`
                  );
                }
                audioService.disconnect();
                finish();
              };

              // POLICY: the bot records continuously and does NOT leave on transient blips
              // or screen-share. It finalizes (leaves + produces the transcript) when the
              // meeting is genuinely over: (a) everyone has left the room for a continuous
              // grace period (below) — this is how the host hitting "Leave"/red on mobile
              // still ends it, since mobile has no "End for everyone"; (b) explicit removal /
              // meeting end (removal monitor + page teardown in meetingFlow.ts); or (c) the
              // K8s hard deadline. The empty check uses the RAW tile count plus a presentation
              // guard, so an active screen-share (which hides tiles) can never false-finalize.
              let emptyTicks = 0;
              let notStartedTicks = 0;
              // The "everyone left" finalize must only arm AFTER the meeting has genuinely
              // started (>=1 other participant, or someone presenting). Before that, the bot
              // has joined early and is waiting alone: rawTiles<=1 means "not started yet",
              // NOT "everyone left". Without this guard the 90s empty grace fired on an
              // early-joining bot and completed the pod before the meeting ever started.
              let meetingHasStarted = false;
              const EMPTY_GRACE_TICKS = 90; // 90 x 1s = 90s continuous empty before finalizing
              const checkInterval = setInterval(() => {
                const currentParticipantCount = (window as any).getGoogleMeetActiveParticipantsCount
                  ? (window as any).getGoogleMeetActiveParticipantsCount()
                  : 0;
                if (currentParticipantCount !== lastParticipantCount) {
                  (window as any).logBot(`Participant check: ${currentParticipantCount} participant(s) visible.`);
                  lastParticipantCount = currentParticipantCount;
                }
                // Finalize-on-empty: RAW tile count (not the screen-share-guarded count) so a
                // genuine empty is detected, but gated by isPresentationActive() so a share
                // never false-triggers it. Grace period tolerates brief drops / a rejoin.
                const rawTiles = countParticipantTiles();
                const presenting = isPresentationActive();

                // Remote-camera detection tick — ships raw facts to Node, which owns all
                // decision logic (own-track exclusion, screen-share exclusion, the latch).
                // hasRemoteParticipants uses the same >=2-tiles threshold as meetingHasStarted
                // below, so "opportunity to observe" tracks genuine occupancy, not the bot
                // being alone in an empty or not-yet-started room.
                try {
                  const reportTick = (window as any).__vexaReportCameraTick;
                  if (typeof reportTick === 'function') {
                    Promise.resolve(reportTick(collectObservedVideos(), presenting, rawTiles >= 2)).catch(
                      (camErr: any) => {
                        (window as any).logBot?.(`[CameraDetection] Tick reporting rejected: ${camErr?.message || camErr}`);
                      }
                    );
                  }
                } catch (camErr: any) {
                  (window as any).logBot?.(`[CameraDetection] Tick reporting failed: ${camErr?.message || camErr}`);
                }

                // Arm the finalize only once the meeting is genuinely underway.
                if (!meetingHasStarted && (rawTiles >= 2 || presenting)) {
                  meetingHasStarted = true;
                  (window as any).logBot(`Meeting has started (participants present) — empty-room finalize is now armed.`);
                }
                if (rawTiles <= 1 && !presenting) {
                  if (!meetingHasStarted) {
                    // Joined early; meeting not started. Keep waiting — do NOT finalize.
                    emptyTicks = 0;
                    notStartedTicks++;
                    if (notStartedTicks % 30 === 0) {
                      (window as any).logBot(`Waiting for the meeting to start — bot alone ${notStartedTicks}s (will not finalize until the meeting has begun).`);
                    }
                  } else {
                    emptyTicks++;
                    if (emptyTicks >= EMPTY_GRACE_TICKS) {
                      (window as any).logBot(`All participants left (empty ${emptyTicks}s, no presentation) — finalizing meeting.`);
                      void stopWithFlush("everyone_left", () => resolve());
                    } else if (emptyTicks % 15 === 0) {
                      (window as any).logBot(`Room empty ${emptyTicks}s (will finalize at ${EMPTY_GRACE_TICKS}s if it stays empty)...`);
                    }
                  }
                } else {
                  emptyTicks = 0;
                  notStartedTicks = 0;
                }
              }, 1000);

              // Listen for page unload
              window.addEventListener("beforeunload", () => {
                (window as any).logBot("Page is unloading. Stopping recorder...");
                void stopWithFlush("beforeunload", () => resolve());
              });

              document.addEventListener("visibilitychange", () => {
                if (document.visibilityState === "hidden") {
                  (window as any).logBot("Document is hidden. Stopping recorder...");
                  void stopWithFlush("visibility_hidden", () => resolve());
                }
              });
            };

            setupGoogleMeetingMonitoring(botConfigData, audioService, resolve);
          }).catch((err: any) => {
            reject(err);
          });
          })(); // Close async IIFE

        } catch (error: any) {
          return reject(new Error("[Google Meet BOT Error] " + error.message));
        }
      });

    },
    {
      botConfigData: botConfig,
      selectors: {
        participantSelectors: googleParticipantSelectors,
        speakingClasses: googleSpeakingClassNames,
        silenceClasses: googleSilenceClassNames,
        containerSelectors: googleParticipantContainerSelectors,
        nameSelectors: googleNameSelectors,
        speakingIndicators: googleSpeakingIndicators,
        peopleButtonSelectors: googlePeopleButtonSelectors
      } as any
    }
  );
}
