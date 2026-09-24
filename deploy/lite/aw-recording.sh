#!/usr/bin/env bash
# aw-recording.sh — drive a locally running Vexa Lite (branch feat/aw-recording-lite) from the host.
#
#   ./aw-recording.sh send   <native_meeting_id> [bot_name] [platform]  # POST /bots — transcribe + record
#   ./aw-recording.sh status <native_meeting_id> [platform]             # segment / speaker counts so far
#   ./aw-recording.sh export <native_meeting_id> [platform]             # after the call (see below)
#
# `export` pulls what upstream keeps OUTSIDE object storage (transcript, participants, meeting row,
# recording index) as JSON, issues GET /recordings/<id>/master so meeting-api assembles the audio
# master (finalize-on-read — upstream code, nothing stitched here), then copies the JSON into the
# local bucket under exports/<platform>_<code>_m<id>_<startUTC>/ so BOTH mirrors (RECORDING_DIR + S3) carry it.
#
# The API key is read from /run/vexa/key.env INSIDE the app container and never printed or copied out.
set -euo pipefail

APP="${APP_CONTAINER:-aw-recording}"
NETWORK="${NETWORK:-aw-recording-net}"
MINIO_CONTAINER="${MINIO_CONTAINER:-aw-recording-minio}"
MINIO_ACCESS_KEY="${MINIO_ACCESS_KEY:-aw-recording-access-key}"
MINIO_SECRET_KEY="${MINIO_SECRET_KEY:-aw-recording-secret-key}"
MINIO_BUCKET="${MINIO_BUCKET:-aw-recording}"
MC_IMAGE="${MC_IMAGE:-quay.io/minio/mc:latest}"
GW="${GW:-http://localhost:8056}"
RECORDING_DIR="${RECORDING_DIR:-$(cd "$(dirname "$0")/../.." && pwd)/recordings}"

usage() { sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 1; }

# api METHOD PATH [JSON] — curl runs inside the app container with the minted key; stdout = body.
api() {
  docker exec -i "$APP" sh -s -- "$GW" "$1" "$2" "${3:-}" <<'EOF'
. /run/vexa/key.env
gw="$1"; m="$2"; p="$3"; b="$4"
if [ -n "$b" ]; then
  curl -s -X "$m" "$gw$p" -H "X-API-Key: $VEXA_API_KEY" -H "Content-Type: application/json" -d "$b"
else
  curl -s -X "$m" "$gw$p" -H "X-API-Key: $VEXA_API_KEY"
fi
EOF
}

cmd="${1:-}"; nid="${2:-}"
[ -n "$cmd" ] && [ -n "$nid" ] || usage

case "$cmd" in
  send)
    bot_name="${3:-AW Recording}"; platform="${4:-google_meet}"
    body=$(printf '{"platform":"%s","native_meeting_id":"%s","bot_name":"%s","transcribe_enabled":true,"recording_enabled":true}' \
      "$platform" "$nid" "$bot_name")
    api POST /bots "$body" | python3 -c '
import json,sys
d=json.load(sys.stdin)
if "detail" in d and "id" not in d: print("REFUSED:", d["detail"]); sys.exit(1)
print("bot requested → meeting id", d.get("id"), "status", d.get("status"), "platform", d.get("platform"), "native", d.get("native_meeting_id"))'
    ;;
  status)
    platform="${3:-google_meet}"
    api GET "/transcripts/$platform/$nid" | python3 -c '
import json,sys
d=json.load(sys.stdin)
if "segments" not in d: print(d.get("detail", d)); sys.exit(0)
segs=d["segments"]; speakers=sorted({(s.get("speaker") or "<unnamed>") for s in segs})
print("meeting", d.get("id"), "status", d.get("status"))
print("segments:", len(segs), "| distinct speakers:", len(speakers))
for sp in speakers: print("  -", sp, "(", sum(1 for s in segs if (s.get("speaker") or "<unnamed>")==sp), "segments )")'
    ;;
  export)
    platform="${3:-google_meet}"
    # Folder carries the numeric meeting id too: the same Meet code can be used for several meetings.
    tmp_t=$(mktemp); api GET "/transcripts/$platform/$nid" > "$tmp_t"
    tid=$(python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d.get("id") or "")' "$tmp_t")
    [ -n "$tid" ] || { echo "no meeting found for $platform/$nid:"; cat "$tmp_t"; rm -f "$tmp_t"; exit 1; }
    # start time in the name too: a fresh database restarts ids at 1, and the S3 prefix keeps history.
    tstart=$(python3 -c 'import json,sys,re; d=json.load(open(sys.argv[1])); t=d.get("start_time") or ""; print(re.sub(r"[^0-9T]","",t[:16]) or "nostart")' "$tmp_t")
    exp="${platform}_${nid}_m${tid}_${tstart}"
    out="$RECORDING_DIR/exports/$exp"; mkdir -p "$out"
    mv "$tmp_t" "$out/transcript.json"
    api GET "/meetings/$platform/$nid/participants"  > "$out/participants.json"
    api GET "/meetings"                              > "$out/_all_meetings.json"
    mid=$(python3 - "$out" "$platform" "$nid" <<'PY'
import json,sys,pathlib
out,platform,nid=sys.argv[1:4]
raw=json.load(open(f"{out}/_all_meetings.json"))
items=raw.get("meetings", raw) if isinstance(raw,dict) else raw
m=next((x for x in items if x.get("platform")==platform and x.get("native_meeting_id")==nid), None)
if m is None:
    t=json.load(open(f"{out}/transcript.json")); m={"id": t.get("id")}
json.dump(m, open(f"{out}/meeting.json","w"), indent=2)
pathlib.Path(f"{out}/_all_meetings.json").unlink()
print(m.get("id") or "")
PY
)
    if [ -n "$mid" ]; then
      api GET "/recordings?meeting_id=$mid" > "$out/recordings.json"
    else
      api GET "/recordings" > "$out/recordings.json"
    fi
    # finalize-on-read: ask meeting-api for each audio master → it assembles + uploads → mirrors pick it up
    for rid in $(python3 -c '
import json,sys
raw=json.load(open(sys.argv[1])); items=raw.get("recordings", raw) if isinstance(raw,dict) else raw
print(" ".join(str(r["id"]) for r in items if "id" in r))' "$out/recordings.json"); do
      api GET "/recordings/$rid/master?type=audio" > "$out/master_${rid}.json"
      echo "master requested for recording $rid → $(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("storage_path") or json.load(open(sys.argv[1])))' "$out/master_${rid}.json")"
    done
    echo "exported → $out"; ls -1 "$out"
    # push the JSON into the bucket so the S3 mirror (and the local one) carry it alongside the audio
    docker run --rm --network "$NETWORK" -v "$out":/exp:ro --entrypoint sh "$MC_IMAGE" -c \
      "mc alias set store http://$MINIO_CONTAINER:9000 $MINIO_ACCESS_KEY $MINIO_SECRET_KEY >/dev/null && mc cp --recursive /exp/ store/$MINIO_BUCKET/exports/$exp/" >/dev/null \
      && echo "copied JSON into bucket: exports/$exp/ (mirrors → RECORDING_DIR + S3)"
    ;;
  *) usage ;;
esac
