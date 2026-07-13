#!/bin/bash
# start.sh — Start aw_output_hook sidecar, then Vexa's Node bot (foreground).
# K8s tracks the Node process PID for Job completion.
set -euo pipefail

python /app/aw_output_hook.py &
HOOK_PID=$!

for i in $(seq 1 60); do
    if curl -sf http://localhost:8080/healthz > /dev/null 2>&1; then
        break
    fi
    sleep 0.5
done

node /app/dist/docker.js

kill "$HOOK_PID" 2>/dev/null || true
