#!/bin/bash
# start.sh — Bootstrap the zoom-bot runtime (Xvfb + PulseAudio), start the
# aw_output_hook sidecar, then run Vexa's Node bot (foreground).
# K8s tracks the Node process PID for Job completion.
#
# Mirrors the meet-bot bootstrap (design doc §5.2.4 steps 2-5) with three Zoom
# differences: the PulseAudio sink is named `zoom_sink` (Zoom Web routes audio
# through PulseAudio; recording.ts captures `${PULSE_SINK}.monitor` via parecord);
# there is NO cookie-refresh — the Zoom Web guest bot joins with no stored
# session (meeting number + passcode + display name only); and Zoom additionally
# needs a CAPTURE device to complete its audio join at all, which meet-bot does
# not (see the microphone block below — it is silent by construction). Background:
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

# --- Microphone: a real CAPTURE device, fed from its OWN silent sink ---
#
# WHY THIS EXISTS: this script created a null SINK and no capture SOURCE at all,
# so Chromium had no microphone to offer. Vexa's own entrypoint.sh records the
# reason a remap source is required: "Without this, Chromium only sees monitor
# sources (which it ignores for mic input)." The consequence chain, observed live
# 2026-09-02: no capture device -> Zoom never joins audio for SENDING ->
# audioSenders=0 for the whole meeting -> Zoom has no audio session for this
# participant -> the participant-list mute state has nothing to describe. That is
# why two watcher clicks landed on a real, enabled, hit-testable element and
# changed nothing, and why no DOM or track-level action could reach it.
#
# WHY NOT `master=${PULSE_SINK}.monitor` (the obvious implementation, and the
# shape entrypoint.sh uses): ${PULSE_SINK}.monitor carries THE MEETING AUDIO —
# it is what parecord records. Deriving the microphone from it would make the bot
# capable of transmitting the entire meeting back into the meeting: a feedback
# loop, and far worse than the bug being fixed. entrypoint.sh can safely remap
# from tts_sink.monitor only because a voice agent deliberately plays TTS into
# that sink and nothing else ever reaches it.
#
# So the mic gets a DEDICATED null sink that nothing is ever played into. A null
# sink with no inputs emits digital zeros on its monitor, so this microphone is
# silent BY CONSTRUCTION rather than by a flag that something could clear. The
# two mutes below are defence in depth, not the guarantee.
#
# Names are hardcoded, deliberately NOT env-overridable: an override is exactly
# the footgun this design exists to prevent (MIC_MASTER=zoom_sink.monitor would
# reintroduce the feedback loop silently). The source is NOT called virtual_mic
# because services/tts-playback.ts unmutes a source by that literal name — a
# distinct name means the TTS path can never unmute this one.
MIC_SILENCE_SINK="mic_silence_sink"   # never the default sink; nothing ever plays here
BOT_MIC_SOURCE="bot_mic"              # what Chromium sees as the microphone
pactl load-module module-null-sink sink_name="$MIC_SILENCE_SINK" \
    sink_properties=device.description="SilentMicFeed" || true
# The module index is captured so a failed verification can UNLOAD exactly this
# module rather than every remap-source in the daemon.
MIC_SRC_MODULE="$(pactl load-module module-remap-source master="${MIC_SILENCE_SINK}.monitor" \
    source_name="$BOT_MIC_SOURCE" source_properties=device.description="BotMicrophone" 2>/dev/null || echo '')"
pactl set-default-source "$BOT_MIC_SOURCE" || true
# Muted BEFORE node starts, so the very first getUserMedia receives zeros.
#
# WHICH GUARANTEE IS WHICH (this block used to contradict itself): silence is by
# CONSTRUCTION — the mic's master is a null sink nothing ever plays into, so its
# monitor emits digital zeros whatever any flag says. The mutes below are DEFENCE
# IN DEPTH on top of that, and of the two the SOURCE mute is the one that matters:
# entrypoint.sh records that muting the sink alone was insufficient because "the
# remap source still passes a low-level signal to WebRTC", and that "muting the
# source cuts it at capture level".
#
# By-construction is the half an operator should judge ZOOM_AUDIO_LOCK=none on.
# Neither mute is ever traded for Zoom's own mute flag — that flag is the thing
# observed flipping mid-meeting at 06:49 on 2026-09-02.
pactl set-source-mute "$BOT_MIC_SOURCE" 1 || true
pactl set-sink-mute "$MIC_SILENCE_SINK" 1 || true

# --- VERIFY the mute actually took; if not, REMOVE the microphone ---
# Every pactl call above is `|| true`, which makes TOTAL failure safe (no source
# exists at all, exactly as before this fix). PARTIAL failure is the dangerous
# case: if load-module succeeded and set-source-mute did not, the pod boots with
# a live, unmuted, default microphone. The topology echo below would print that
# and act on nothing. No mic is strictly safer than an unmuted one, so an
# unverifiable mute unloads the source.
#
# EXISTENCE AND MUTE STATE ARE SEPARATE QUESTIONS. The previous version asked
# only "is it muted?", using a predicate that returns false for BOTH "exists and
# is unmuted" and "does not exist" — and it reused that predicate as the
# post-unload verdict. Three materially different states printed one string, so
# the message carried no information in any direction; worse, the two branches
# were INVERTED, so a source that was present and MUTED (safe) was reported as
# able to transmit, while a source left present and UNMUTED (the one dangerous
# state) got the reassuring "capture source removed" line.
#
# `pactl get-source-mute` only exists on newer pactl, so there is a fallback to
# parsing the source block. A state that cannot be determined reports `unknown`
# rather than being folded into either answer.
#
# Prints exactly one of: absent | muted | unmuted | unknown
zoom_mic_state() {
    local short mute
    short="$(pactl list short sources 2>/dev/null || true)"
    if [ -z "$short" ]; then echo unknown; return; fi
    if ! printf '%s\n' "$short" | awk -v s="$BOT_MIC_SOURCE" '$2==s{f=1} END{exit !f}'; then
        echo absent; return
    fi
    mute="$(pactl get-source-mute "$BOT_MIC_SOURCE" 2>/dev/null || true)"
    case "$mute" in *[Yy]es*) echo muted; return ;; *[Nn]o*) echo unmuted; return ;; esac
    mute="$(pactl list sources 2>/dev/null \
        | awk -v s="$BOT_MIC_SOURCE" '$1=="Name:"{cur=($2==s)} cur&&$1=="Mute:"{print $2; exit}' || true)"
    case "$mute" in [Yy]es) echo muted ;; [Nn]o) echo unmuted ;; *) echo unknown ;; esac
}

case "$(zoom_mic_state)" in
    muted)
        echo "[start.sh] bot mic VERIFIED muted at the capture level (source=$BOT_MIC_SOURCE)"
        ;;
    absent)
        # SAFE, but the fix did not take. This is the likeliest way the whole
        # change silently fails, so it gets its own prominent line instead of
        # being reported as a privacy incident.
        echo "[start.sh] WARNING: MIC FIX NOT IN FORCE — capture source $BOT_MIC_SOURCE DOES NOT EXIST, so Chromium has no microphone. The pod is SAFE (it cannot transmit; identical to the pre-fix boot) but Zoom will not join audio for SENDING and this meeting will reproduce the original bug. Check the remap-source load line above."
        ;;
    *)
        echo "[start.sh] WARNING: $BOT_MIC_SOURCE exists and is NOT verified muted — removing the capture source entirely; no microphone is safer than an unmuted one"
        if [ -n "$MIC_SRC_MODULE" ]; then
            pactl unload-module "$MIC_SRC_MODULE" || true
        else
            echo "[start.sh] WARNING: no module index was captured, so the source could not be unloaded by index"
        fi
        case "$(zoom_mic_state)" in
            absent)
                echo "[start.sh] capture source REMOVED; Zoom will see no microphone (audio SEND will not join, as before this fix)"
                ;;
            muted)
                echo "[start.sh] capture source is still present but now reports MUTED; the unload did not take, but the mic is silent at capture level"
                ;;
            *)
                echo "[start.sh] DANGER: $BOT_MIC_SOURCE is still PRESENT and NOT muted after the unload attempt — treat this pod as capable of transmitting"
                ;;
        esac
        ;;
esac

# DEFAULT SINK IS SET LAST, ON PURPOSE. Some PulseAudio configurations load
# module-switch-on-connect, which promotes a newly appearing sink to default; if
# that module is present in this image, loading mic_silence_sink after this line
# would silently steal the default and Zoom would render into the silent sink
# instead of $PULSE_SINK — i.e. an empty recording. Setting the default sink
# after every sink exists makes the explicit assignment the final word either
# way. mic_silence_sink is NEVER made the default sink.
pactl set-default-sink "$PULSE_SINK" || true

# Record the resulting topology in the pod log. Without this, "which sink was
# default?" and "did the mic source exist?" are unanswerable after the fact —
# the same class of unobservability that made the 2026-09-02 run unreadable.
echo "[start.sh] PulseAudio topology (recording sink must be the default sink):"
pactl info 2>/dev/null | grep -E 'Default (Sink|Source)' || true
pactl list short sinks 2>/dev/null || true
pactl list short sources 2>/dev/null || true

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
