# syntax=docker/dockerfile:1.4
# Build context: repo root (aw-notetaker/)
# docker build -f vexa-fork/aw-overrides/services/meet-bot/Dockerfile \
#              -t ghcr.io/voyantt-consultancy-services-llp/meet-bot:v0.4.0 .

# ── Stage 1: TypeScript build ────────────────────────────────────────────────
FROM node:20-slim AS ts-build
WORKDIR /build
COPY vexa-fork/services/vexa-bot/core/ ./
RUN npm install
# ^ deliberately NOT `--omit=dev`, for two reasons -- neither of which is "the
# build would otherwise fail". Verified on a clean tree 2026-08-27: `npm install
# --omit=dev` DOES produce a working build, because `typescript` arrives as a
# production transitive (puppeteer -> cosmiconfig -> typescript).
#
#   1. That availability is an accident of someone else's dependency tree. With
#      dev deps installed, `tsc` comes from OUR declared devDependency instead,
#      and the build survives cosmiconfig ever dropping typescript.
#   2. It keeps this stage's node_modules a superset of what previously-shipped
#      images contained. Those were built with the host's node_modules copied in
#      (dev deps included, since --omit=dev does not prune an existing tree), so
#      installing dev deps here means adding the repo-root .dockerignore cannot
#      silently remove a package from the runtime image.
#
# Not pinned: `core/` has no package-lock.json, so the resolved tsc version
# floats (host has 5.8.2; a clean install picked 5.9.3). Committing a lockfile is
# the actual fix for build reproducibility and is deliberately left out of an
# incident fix.
RUN npm run build

# Drop what the RUNTIME never loads, BEFORE stage 2 copies node_modules.
#
# This must happen HERE, in the build stage. Deleting the same paths in stage 2
# would save nothing: image layers are additive, so a later `rm` leaves the bytes
# in the earlier layer and the download size is unchanged. `COPY --from=ts-build`
# copies whatever exists at copy time, so pruning here genuinely shrinks the
# final image.
#
# Measured on meet-bot:v0.4.23 (2026-09-21) — node_modules was 670 MB, of which:
#   onnxruntime-node  548 MB  <- the whole reason for this step
#   typescript         23 MB
#   @types            2.7 MB
#
# Why removing onnxruntime-node is SAFE, not a gamble:
#   · it is loaded lazily via require() inside getOrt() (services/vad.ts:23),
#     never at import time;
#   · SileroVAD.create() calls getOrt() FIRST (vad.ts:72), and the only caller
#     (index.ts:1323) wraps it in try/catch which logs
#     "VAD not available — will send all audio" and sets vadModel = null;
#   · so a missing module degrades to "no VAD", and VAD was doing no work anyway:
#     telemetry reported `vad=0/0 (checked/rejected)` on runs WITH the module
#     present, i.e. it was initialised but never filtered a single audio chunk.
#
# CORRECTION (2026-09-21, from the first live run of this pruned image): an
# earlier version of this comment claimed the PerSpeaker/VAD block "does not run
# here". THAT WAS WRONG -- it was based on grepping only two pods. The block DOES
# run on every launch, and now logs:
#     [PerSpeaker] VAD not available (Cannot find module 'onnxruntime-node')
# which is the caught, expected path: the bot went on to join, record two named
# participants, finalise on the empty-room timer and upload, exit code 0.
# So this prune removes an INITIALISED-BUT-IDLE capability, not dead code.
#
# typescript and @types are build-only and `npm run build` has already finished.
# esbuild (12 MB) is deliberately LEFT IN PLACE: it may be a transitive runtime
# dependency and 12 MB is not worth that risk.
#
# If live transcription (VAD) is ever enabled on this path, REVERT this step --
# the bot will otherwise silently run without VAD instead of failing loudly.
#
# Evidence: docs-utpal/EXPLAINED/2026-09-21-meet-bot-cpu-memory-measured.md §10.3
RUN rm -rf node_modules/onnxruntime-node \
           node_modules/@jjhbw \
           node_modules/typescript \
           node_modules/@types \
 && echo "pruned runtime node_modules -> $(du -sh node_modules | cut -f1)"

# ── Stage 2: Runtime ─────────────────────────────────────────────────────────
FROM mcr.microsoft.com/playwright/python:v1.56.0-noble

RUN apt-get update -o Acquire::AllowInsecureRepositories=true \
    && apt-get install -y --allow-unauthenticated --no-install-recommends \
    ffmpeg \
    tini \
    pulseaudio \
    pulseaudio-utils \
    x11vnc \
    python3-websockify \
    novnc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=ts-build /build/dist ./dist
COPY --from=ts-build /build/node_modules ./node_modules
COPY --from=ts-build /usr/local/bin/node /usr/local/bin/node

COPY notetaker-common/pyproject.toml notetaker-common/pyproject.toml
COPY notetaker-common/notetaker_common/ notetaker-common/notetaker_common/
# --ignore-installed: the v1.56.0-noble base now ships pip 25.3, which REFUSES to
# uninstall apt-managed packages (no RECORD file) — and pydantic==2.5.0 pulls a
# newer typing_extensions than the apt one in /usr/lib/python3/dist-packages,
# so a plain install dies with `uninstall-no-record-file`. Installing pip-managed
# copies ALONGSIDE the apt ones (shadowing them in site-packages) sidesteps the
# uninstall entirely. Reinstalling notetaker-common's full dependency closure this
# way also shadows every apt-managed Python package pip touches (certifi, idna,
# urllib3, python-dateutil, typing_extensions), so the later pip layers below hit
# no further uninstall-no-record-file errors. Pins are preserved — this is a local
# pinned package, so re-resolution keeps pydantic==2.5.0 et al.
RUN pip install --no-cache-dir --ignore-installed ./notetaker-common

COPY vexa-fork/aw-overrides/aw-integration/pyproject.toml aw-integration/pyproject.toml
COPY vexa-fork/aw-overrides/aw-integration/aw_integration/ aw-integration/aw_integration/
# NOT --ignore-installed here: aw-integration depends on notetaker-common, which is
# a LOCAL package installed in the step above (not on any index). --ignore-installed
# would make pip re-resolve that dep from PyPI and fail ("No matching distribution
# for notetaker-common"). A plain install reuses the already-installed copy, and by
# now the apt-managed packages are already shadowed by line 53, so there is no
# uninstall-no-record-file risk left to defend against.
RUN pip install --no-cache-dir ./aw-integration

RUN pip install --no-cache-dir "fastapi==0.109.0" "uvicorn[standard]==0.27.0" "python-multipart" "prometheus-fastapi-instrumentator==6.1.0"

# Python Playwright for the cookie-refresh tool (refresh_storage_state.py). The
# base image ships browsers at /ms-playwright (chromium-1194 = Playwright 1.56) but
# not the Python pip package. Version matches the base browsers (deviation from the
# §10A.2-pinned 1.50, already established by the v1.56.0-noble base bump). No
# `playwright install` needed — PLAYWRIGHT_BROWSERS_PATH=/ms-playwright is preset.
RUN pip install --no-cache-dir "playwright==1.56.0"

COPY vexa-fork/aw-overrides/services/meet-bot/aw_output_hook.py /app/aw_output_hook.py
# Cookie-refresh CronJob tool (design §13.13). Reuses this image; patches the
# meet-bot-secrets Secret via the K8s REST API using httpx (already installed via
# notetaker-common) — no kubernetes client dep (that is orchestrator-only, §10A.2).
COPY vexa-fork/aw-overrides/services/meet-bot/refresh_storage_state.py /app/refresh_storage_state.py
COPY vexa-fork/aw-overrides/services/meet-bot/knock_delay.py /app/knock_delay.py
COPY vexa-fork/aw-overrides/services/meet-bot/start.sh /app/start.sh
RUN chmod +x /app/start.sh

# The Vexa bot writes checkpoint screenshots to /app/storage/screenshots (join.ts)
# and runs as uid 1000; /app is root-owned, so pre-create + chown the dir tree.
RUN mkdir -p /app/storage/screenshots && chown -R 1000:1000 /app/storage

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    NODE_ENV=production \
    DISPLAY=:99

USER ubuntu

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/app/start.sh"]
