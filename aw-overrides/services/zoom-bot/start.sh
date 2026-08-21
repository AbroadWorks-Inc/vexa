#!/bin/bash
# start.sh — Bootstrap the zoom-bot runtime (Xvfb + PulseAudio), start the
# aw_output_hook sidecar, then run Vexa's Node bot (foreground).
# K8s tracks the Node process PID for Job completion.
#
# Mirrors the meet-bot bootstrap (design doc §5.2.4 steps 2-5) with two Zoom
# differences: the PulseAudio sink is named `zoom_sink` (Zoom Web routes audio
# through PulseAudio; recording.ts captures `${PULSE_SINK}.monitor` via parecord),
# and there is NO cookie-refresh — the Zoom Web guest bot joins with no stored
# session (meeting number + passcode + display name only). Background:
# docs-utpal/meet-bot-display-audio-gap-analysis.md
set -euo pipefail

# --- Display: Xvfb virtual framebuffer on :99 (matches ENV DISPLAY=:99) ---
Xvfb :99 -screen 0 1280x720x24 -nolisten tcp &
XVFB_PID=$!

# --- Audio: PulseAudio user daemon + a null sink the Zoom Web client renders
# into. parecord captures `${PULSE_SINK}.monitor` (recording.ts) — so the sink
# name here and PULSE_SINK below must agree. ---
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/tmp/pulse-$(id -u)}"
export PULSE_SINK="${PULSE_SINK:-zoom_sink}"
mkdir -p "$XDG_RUNTIME_DIR"
pulseaudio --start --exit-idle-time=-1 || true
sleep 1
pactl load-module module-null-sink sink_name="$PULSE_SINK" \
    sink_properties=device.description="$PULSE_SINK" || true
pactl set-default-sink "$PULSE_SINK" || true

# --- Our output-hook sidecar ---
python /app/aw_output_hook.py &
HOOK_PID=$!

for i in $(seq 1 60); do
    if curl -sf http://localhost:8080/healthz > /dev/null 2>&1; then
        break
    fi
    sleep 0.5
done

# --- Knock at the door only when the meeting is nearly due ---
# The pod is spawned ~10 min early so the runtime (Xvfb, PulseAudio, the sidecar,
# and shortly the browser) is warm before the meeting starts. But the guest bot is
# never invited, so Zoom drops it into a waiting room and a human must admit it --
# and knocking 10 min early means asking to join an EMPTY room, burning the
# admission budget before anyone can let the bot in. So: wait here, then let the
# browser open the meeting ~1 min before the start. knock_delay.py is shared with
# meet-bot (platform-agnostic; reads scheduled_start_at from BOT_JOB_JSON) and
# fails open (prints 0 = knock now) on every bad-input path.
#
# `|| echo 0` covers the script failing to run; the `case` covers it printing a
# non-integer. `set -e` would otherwise abort the whole boot. Bare `python` matches
# line 40 above (the interpreter this image is PROVEN to resolve). Only stdout is
# captured; the module's stderr reasoning flows straight to the pod log.
KNOCK_SLEEP="$(python /app/knock_delay.py || echo 0)"
case "$KNOCK_SLEEP" in
    ''|*[!0-9]*) echo "[start.sh] knock delay unusable ('$KNOCK_SLEEP'); knocking now"; KNOCK_SLEEP=0 ;;
esac
if [ "$KNOCK_SLEEP" -gt 0 ]; then
    echo "[start.sh] holding ${KNOCK_SLEEP}s before opening the meeting page"
    sleep "$KNOCK_SLEEP"
    echo "[start.sh] hold complete; launching bot"
fi

# --- Vexa Node bot core (foreground; its PID drives Job completion) ---
# Capture the bot's exit code without letting `set -e` abort teardown.
rm -f /tmp/pipeline_done 2>/dev/null || true
node /app/dist/docker.js && BOT_RC=0 || BOT_RC=$?

# §2 end-of-meeting handling: on graceful leave the bot POSTs a session-end
# callback that kicks off the output pipeline (assemble -> S3 -> /process) in the
# sidecar. Killing the sidecar immediately would cut that off and lose the
# recording. Wait (bounded ~120s) for the sidecar to signal /tmp/pipeline_done,
# or exit early if it's already gone.
for _ in $(seq 1 240); do
    [ -f /tmp/pipeline_done ] && break
    kill -0 "$HOOK_PID" 2>/dev/null || break
    sleep 0.5
done

kill "$HOOK_PID" "$XVFB_PID" 2>/dev/null || true
exit "${BOT_RC:-0}"
