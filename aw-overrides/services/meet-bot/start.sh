#!/bin/bash
# start.sh — Bootstrap the bot runtime (Xvfb + PulseAudio), start the
# aw_output_hook sidecar, then run Vexa's Node bot (foreground).
# K8s tracks the Node process PID for Job completion.
#
# The Xvfb + PulseAudio bootstrap replicates the parts of Vexa's original
# entrypoint.sh that our own-container build must provide (design doc §5.2.4
# steps 2-5). Background + rationale:
# docs-utpal/meet-bot-display-audio-gap-analysis.md
set -euo pipefail

# --- Display: Xvfb virtual framebuffer on :99 (matches ENV DISPLAY=:99) ---
Xvfb :99 -screen 0 1280x720x24 -nolisten tcp &
XVFB_PID=$!

# --- Audio: PulseAudio user daemon + a default null sink for the browser.
# Non-fatal: Meet audio is captured in-browser (MediaRecorder); PulseAudio is
# for Chromium's audio subsystem / getUserMedia and Vexa parity. ---
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/tmp/pulse-$(id -u)}"
mkdir -p "$XDG_RUNTIME_DIR"
pulseaudio --start --exit-idle-time=-1 || true
sleep 1
pactl load-module module-null-sink sink_name=meet_sink \
    sink_properties=device.description=meet_sink || true
pactl set-default-sink meet_sink || true

# --- Our output-hook sidecar ---
python /app/aw_output_hook.py &
HOOK_PID=$!

for i in $(seq 1 60); do
    if curl -sf http://localhost:8080/healthz > /dev/null 2>&1; then
        break
    fi
    sleep 0.5
done

# --- Vexa Node bot core (foreground; its PID drives Job completion) ---
# Capture the bot's exit code without letting `set -e` abort teardown.
rm -f /tmp/pipeline_done 2>/dev/null || true
node /app/dist/docker.js && BOT_RC=0 || BOT_RC=$?

# §2 end-of-meeting handling: on graceful leave the bot POSTs a session-end
# callback that kicks off the output pipeline (convert -> S3 -> /process) in the
# sidecar. Killing the sidecar immediately (as before) cut that off and lost the
# recording. Wait (bounded ~120s) for the sidecar to signal /tmp/pipeline_done,
# or exit early if it's already gone.
for _ in $(seq 1 240); do
    [ -f /tmp/pipeline_done ] && break
    kill -0 "$HOOK_PID" 2>/dev/null || break
    sleep 0.5
done

kill "$HOOK_PID" "$XVFB_PID" 2>/dev/null || true
exit "${BOT_RC:-0}"
