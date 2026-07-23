# syntax=docker/dockerfile:1.4
# Build context: repo root (aw-notetaker/)
# docker build -f vexa-fork/aw-overrides/services/meet-bot/Dockerfile \
#              -t ghcr.io/voyantt-consultancy-services-llp/meet-bot:v0.4.0 .

# ── Stage 1: TypeScript build ────────────────────────────────────────────────
FROM node:20-slim AS ts-build
WORKDIR /build
COPY vexa-fork/services/vexa-bot/core/ ./
RUN npm install --omit=dev
RUN npm run build

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
RUN pip install --no-cache-dir ./notetaker-common

COPY vexa-fork/aw-overrides/aw-integration/pyproject.toml aw-integration/pyproject.toml
COPY vexa-fork/aw-overrides/aw-integration/aw_integration/ aw-integration/aw_integration/
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
