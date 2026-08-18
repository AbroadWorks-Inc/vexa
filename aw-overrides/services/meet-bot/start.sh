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

# --- Knock at the door only when the meeting is nearly due ---
# The pod is spawned ~10 min early so all of the above (Xvfb, PulseAudio, the
# sidecar, and shortly the browser) is warm before the meeting starts. But in the
# guest-join flow the bot is never invited, so Meet makes it "Ask to join" and a
# human must admit it -- and knocking 10 min early means knocking on an EMPTY
# room, burning the 15 min admission budget before anyone can let the bot in.
# Observed live 2026-08-19: knocked 23:50, owner joined 00:00, gave up 00:05.
#
# So: wait here, then let the browser knock ~1 min before the start. Same budget,
# aimed at the window where a human is actually present.
#
# knock_delay.py prints ONLY an integer on stdout and its reasoning on stderr, and
# fails open (prints 0 = knock now, the pre-change behaviour) on every bad-input
# path. Both guards below exist because `sleep` must never receive a non-integer:
# `|| echo 0` covers the script failing to run at all, and the `case` covers it
# printing something unexpected. `set -e` would otherwise abort the whole boot.
# Only stdout is captured here; the module's stderr reasoning flows straight to
# the pod log, which is where it is wanted.
#
# Bare `python`, not `python3`: it is what line 28 above already uses to launch the
# sidecar, so it is the interpreter this image is PROVEN to resolve. Both exist in
# the runtime image (verified: /usr/bin/python and /usr/bin/python3, both 3.12.3),
# but if the invocation ever failed, `|| echo 0` would fail open and the bot would
# silently knock early again -- the exact bug this block fixes. Matching the proven
# convention removes that class of silent regression.
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
