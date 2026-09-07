# syntax=docker/dockerfile:1.4
# Build context: repo root (aw-notetaker/)
# docker build -f vexa-fork/aw-overrides/services/teams-bot/Dockerfile \
#              -t ghcr.io/voyantt-consultancy-services-llp/teams-bot:v0.1.0 .
#
# amd64 image (the Teams bot is browser-based; runs on ng-bots-133). Copied from
# the zoom-bot image, which is the meet-bot image EXCEPT: no cookie-refresh tool
# and no Python Playwright — the Teams guest bot joins anonymously with no stored
# session (msteams/join.ts: navigate → "Continue on this browser" → display name
# → "Join now"), so there is nothing to refresh.
#
# The one addition over zoom-bot is MICROSOFT EDGE. See its own block below.
# amd64 is not incidental here: Edge stable for Linux is amd64-only (Playwright's
# own reinstall_msedge_stable_linux.sh aborts on aarch64), so this image cannot be
# built for arm64 with the Edge path intact.

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

# ── Microsoft Edge — the one addition over the zoom-bot image ────────────────
#
# WHY IT IS HERE AT ALL. index.ts launches Teams with `channel: 'msedge'` FIRST
# and only falls back to bundled Chromium inside a catch that logs "MS Edge
# launch failed for Teams (...). Falling back to bundled Chromium." Vexa
# developed the Teams connector on Edge; platforms/msteams/ carries zero
# AW-authored tests and its newest upstream commit is months old against a
# moving DOM. Shipping without Edge means the first live Teams test carries two
# unknowns — "is the connector stale?" and "does the Chromium path behave like
# the path this code was written against?" — and no way to separate them. This
# installs the path upstream actually exercises; the fallback stays as a
# runtime guard.
#
# WHY apt AND NOT `playwright install msedge`. They are the same operation.
# playwright-core/bin/reinstall_msedge_stable_linux.sh — the script that
# `playwright install msedge` shells out to on Linux — adds Microsoft's GPG key,
# adds `deb [arch=amd64] .../repos/edge stable main`, and `apt-get install -y
# microsoft-edge-stable`. That is reproduced verbatim below. Going through the
# CLI instead would mean adding a dependency purely to reach it: this runtime
# stage installs neither the `playwright` pip package (that is meet-bot's, for
# its cookie-refresh tool) nor any node CLI on PATH, so it would cost either a
# pip dep outside §10A.2 or a reliance on the copied node_modules layout. The
# channel itself needs no Playwright registry state — `channel: 'msedge'`
# resolves to the fixed path /opt/microsoft/msedge/msedge on Linux, which is
# exactly where the apt package lands.
#
# WHAT IT COSTS — measured from Microsoft's own apt index, not estimated:
# microsoft-edge-stable 151.0.4129.107-1 is a 195 MB .deb declaring
# Installed-Size: 656430 (~641 MiB). The build plan's decision table guessed
# "~150 MB"; the real installed footprint is roughly 4x that, and it roughly
# doubles this image. That is the honest price of removing the unknown above.
# The true layer delta is unverified here because building the image is outside
# this task's remit.
#
# NOT DONE, deliberately: pruning /opt/microsoft/msedge/locales/*.pak down to
# en-US would recover only ~50 MB (~7%) and would introduce a fresh
# browser-startup unknown into the very image whose purpose is to REMOVE an
# unknown. If the size becomes a real problem, that prune is the first lever,
# and it should be pulled with a live join test attached.
RUN apt-get update -o Acquire::AllowInsecureRepositories=true \
    && apt-get install -y --allow-unauthenticated --no-install-recommends \
    ca-certificates \
    curl \
    gnupg \
    && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc \
    | gpg --dearmor -o /etc/apt/trusted.gpg.d/microsoft.gpg \
    && echo "deb [arch=amd64] https://packages.microsoft.com/repos/edge stable main" \
    > /etc/apt/sources.list.d/microsoft-edge-stable.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends microsoft-edge-stable \
    && rm -rf /var/lib/apt/lists/* \
    && /opt/microsoft/msedge/msedge --version

WORKDIR /app

COPY --from=ts-build /build/dist ./dist
COPY --from=ts-build /build/node_modules ./node_modules
COPY --from=ts-build /usr/local/bin/node /usr/local/bin/node

COPY notetaker-common/pyproject.toml notetaker-common/pyproject.toml
COPY notetaker-common/notetaker_common/ notetaker-common/notetaker_common/
# --ignore-installed: the v1.56.0-noble base now ships pip 25.3, which REFUSES to
# uninstall apt-managed packages (no RECORD file) — and pydantic==2.5.0 pulls a
# newer typing_extensions than the apt one in /usr/lib/python3/dist-packages, so a
# plain install dies with `uninstall-no-record-file`. Installing pip-managed copies
# ALONGSIDE the apt ones (shadowing them in site-packages) sidesteps the uninstall
# entirely. Reinstalling the full dependency closure this way also shadows every
# apt-managed package pip touches (certifi, idna, urllib3, python-dateutil,
# typing_extensions), so the later pip layers hit no further uninstall errors.
# Pins are preserved — this is a local pinned package, so re-resolution keeps
# pydantic==2.5.0 et al.
#
# Carried over from meet-bot (vexa-fork cc9e758b). teams-bot was copied from
# zoom-bot's Dockerfile, which still PREDATES that fix, so it inherited the bug
# and failed its first build here at exactly this line.
RUN pip install --no-cache-dir --ignore-installed ./notetaker-common

COPY vexa-fork/aw-overrides/aw-integration/pyproject.toml aw-integration/pyproject.toml
COPY vexa-fork/aw-overrides/aw-integration/aw_integration/ aw-integration/aw_integration/
# NOT --ignore-installed here, deliberately: aw-integration depends on
# notetaker-common, which is a LOCAL package installed in the step above and is on
# no index. --ignore-installed would make pip re-resolve that dependency from PyPI
# and fail with "No matching distribution for notetaker-common". A plain install
# reuses the already-installed copy, and the apt-managed packages are already
# shadowed by the step above, so there is no uninstall risk left to defend against.
RUN pip install --no-cache-dir ./aw-integration

RUN pip install --no-cache-dir "fastapi==0.109.0" "uvicorn[standard]==0.27.0" "python-multipart" "prometheus-fastapi-instrumentator==6.1.0"

COPY vexa-fork/aw-overrides/services/teams-bot/aw_output_hook.py /app/aw_output_hook.py
# Shared, platform-agnostic knock-delay (holds until the meeting is nearly due
# before opening the page, so the guest bot knocks when a human can admit it).
COPY vexa-fork/aw-overrides/services/teams-bot/knock_delay.py /app/knock_delay.py
COPY vexa-fork/aw-overrides/services/teams-bot/start.sh /app/start.sh
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
