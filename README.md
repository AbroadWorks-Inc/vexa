# AW Bots

AbroadWorks's meeting bots. A bot joins a **Google Meet, Microsoft Teams or Zoom** call before it
starts, records the audio and notes **who was speaking when**. After the call, the meeting goes to the
AW notetaker pipeline, which produces a **named transcript and a summary**.

AW Bots is a fork of the open-source project [Vexa](https://github.com/Vexa-ai/vexa) (v0.12,
Apache-2.0). We run Vexa as-is wherever we can and keep our own changes small and clearly separated
(see [What we changed](#what-we-changed)). Upstream's original README is kept in
[`README.upstream.md`](README.upstream.md).

**Jitsi** (meet.abroadworks.com) does not use AW Bots. It has its own pipeline (Jibri records,
Prosody supplies who-spoke-when), which feeds the **same** `notetaker-worker`, so all four platforms
end up with the same kind of transcript.

---

## How it fits together

```
 Portal (users sign in with Google)
   │  pushes each upcoming meeting:  POST /meetings {title, scheduled_at, meeting_url}
   ▼
 AW Bots (this repo, deployed with Helm on EKS)
   ├─ gateway ─ admin-api ─ meeting-api ─ runtime ──► one bot pod per meeting (Karpenter nodes)
   │                                                   joins before start, records audio,
   │                                                   writes speaker-activity.jsonl
   │  stores recordings + speaker activity in  s3://aw-bots/
   │  when the meeting ends: signed webhook  "meeting.completed"
   ▼
 exporter (our addition, integrations/out/aw-notetaker/)
   │  builds  s3://aw-chatworks-transcribe/recordings/<platform>_<meetingId>_<startUTC>/
   │          master.webm · audio.wav · speaker_timeline.json · participants.json · meeting.json …
   │  then calls  POST /process
   ▼
 notetaker-worker ──► transcriber (Whisper large-v3, GPU) ──► notes.json · transcript.txt · summary
   (existing AW pipeline, shared with Jitsi; lives in the deployment repo, not here)
```

**How names get onto the transcript.** Whisper turns `audio.wav` into text with timestamps but
doesn't know who is speaking. The bot's `speaker-activity.jsonl` says who was talking at each moment.
`notetaker-worker` matches the two by time and writes the names. This is the same method Jitsi uses
with Prosody's speaker timeline.

**Live transcription is off.** Vexa can transcribe live during the call, but we use the recording and
the shared Whisper large-v3 transcriber instead. Live transcription is a setting
(`TRANSCRIBE_ENABLED`), default off for us.

---

## What we changed

Everything else is upstream Vexa, unchanged.

| Change | Where | Why |
|---|---|---|
| **Speaker activity file.** The bot always writes `speaker-activity.jsonl`: who spoke when, with no audio, about 1–40 MB for a 3-hour meeting. meeting-api accepts it as a new signal file. | `core/meetings/services/bot/src/speaker-activity.ts` (+ small wiring in `capture-bridge.ts`, `index.ts`, `signal-upload.ts`); `core/meetings/services/meeting-api/src/meeting_api/recordings/jsonb.py` | Vexa kept this data only inside its debug tape, which also stores everyone's audio and stops at 250 MB (about 50 minutes). Long meetings lost their speaker names. |
| **Exporter.** A new small service. | `integrations/out/aw-notetaker/` | Turns each finished meeting into the folder the AW notetaker pipeline reads, and hands it over. |
| **Helm chart: meeting-api service account.** Optional `meetingApi.serviceAccount` (default off; the default render is unchanged). | `deploy/helm/charts/vexa` (`values.yaml`, `templates/serviceaccount-meeting-api.yaml`, `deployment-meeting-api.yaml`) | Lets meeting-api get its own IAM role (IRSA) for the `aw-bots` bucket, like our other services' service accounts. |
| **Helm chart: pre-created Postgres credentials.** Optional `postgres.existingCredentialsSecret` (default off; the default render is unchanged). | `deploy/helm/charts/vexa` (`values.yaml`, `templates/secret.yaml`), tests in `deploy/helm/tests/test_template.sh` | Keeps the in-cluster Postgres but reads its password from a Secret we create, so a `helm upgrade` never rewrites it. |
| **Image workflow.** Builds and pushes our three images to GHCR. | `.github/workflows/aw-images.yml` | Images come from CI on every push to `development`, or on a manual run with a release tag. |
| **Lite helper for local tests.** | `deploy/lite/Makefile`, `deploy/lite/aw-recording.sh` | Run one bot on a laptop against a real meeting and get the files out. |

The design and the reasoning behind each decision live in
[`integrations/out/aw-notetaker/docs/`](integrations/out/aw-notetaker/docs/README.md).

---

## Repository map

Folders we work in:

| Folder | What it is |
|---|---|
| `core/meetings/services/bot` | The meeting bot (TypeScript, Playwright + Chromium). One container per meeting. |
| `core/meetings/services/meeting-api` | Meetings, recordings and signal files; auto-join; webhooks (Python). |
| `integrations/out/aw-notetaker` | **Our** exporter service (Python), with its docs and tests. |
| `deploy/helm/charts/vexa` | Upstream's Helm chart. We deploy it with our own values file (see [Deploy](#deploy-on-eks)). |
| `deploy/lite`, `deploy/compose` | Local ways to run the stack (single container / full Docker Compose). |

The rest is upstream Vexa. Build-relevant: `core/*` (all other services), `clients/terminal` (the web
console), `packages`, `licenses`, `behavior`, `scripts` (repo checks). Upstream governance and
publishing only: `docs`, `calm`, `carve`, `release`, `releases`, `security`, `assets`, `sdks`, `tools`.
We leave those folders in place so upstream changes keep merging cleanly.

---

## Branches

| Branch | Role |
|---|---|
| `main` | Mirror of upstream Vexa. We never commit to it. |
| `development` | Our working line. Feature branches merge here. |
| `feat/<topic>`, `fix/<topic>` | Work in progress, cut from `development` (current: `feat/aw-rearchitecture`). |
| `aw/main` and `aw/*` | The old cloud bot (Vexa v0.10.4). Kept for reference only. |

**Taking upstream changes:** update `main` from upstream, then merge `main` into `development` on a
branch and run the checks. Renovate currently opens dependency-bump branches against `aw/main`, the
old line, so those branches don't bring upstream changes into `development`.

---

## Services and images

One Helm install runs all of these. Each service is its own Docker image.

| Service | Image | Ours or upstream |
|---|---|---|
| gateway (API front door) | `vexaai/v012-gateway` | upstream |
| admin-api (users, API keys, settings) | `vexaai/v012-admin-api` | upstream |
| **meeting-api** | built from this repo | **ours** (accepts `speaker-activity`) |
| runtime (starts bot pods) | `vexaai/v012-runtime` | upstream |
| **bot** | built from this repo | **ours** (writes `speaker-activity.jsonl`) |
| terminal (web console) | `vexaai/v012-terminal` | upstream |
| agent-api, dashboard, flows | `vexaai/*` | upstream; all three **off** for us (we don't use Vexa's AI agents) |
| postgres, redis (Valkey) | chart defaults, inside the same install | upstream; Vexa's own database and message bus, separate from the notetaker's |
| **exporter** | built from `integrations/out/aw-notetaker` | **ours**, deployed next to the chart |

Our images go to GHCR, under the names `ghcr.io/voyantt-consultancy-services-llp/aw-bots-meeting-api`,
`…/aw-bots-bot` and `…/aw-bots-exporter`.

### Build our images

**Normally CI builds them**: `.github/workflows/aw-images.yml` ("AW images — build & push").

| Trigger | Tags pushed |
|---|---|
| Push to `development` | `:<commit sha>` for all three images |
| Manual run (Actions → Run workflow: branch, tag e.g. `v0.1.1`, `all` or one image) | `:<commit sha>` and `:<tag>` |

There is no `:latest`: the deployment pins exact tags, so a new image reaches the cluster only when
the tag is changed in aw-notetaker and applied (its runbook, "Upgrading our images"). The workflow
needs the secret `GHCR_PAT` in this repo (`AbroadWorks-Inc/vexa`): a classic personal access token with `write:packages`, owned by a member of `voyantt-consultancy-services-llp`. The talke repo's secret of the same name lives in the Voyantt org and is not visible here.
The exporter's tests and checks run before its image is built.

**By hand**, from the repo root. **Every image is built for `linux/amd64`**: both AW Bots machine pools are
amd64 (Intel/AMD), because upstream publishes and tests the bot on amd64 only. An Apple Silicon Mac
is arm64, so a build without `--platform linux/amd64` produces an image the cluster can't run
(`exec format error`).

```bash
TAG=v0.1.0
REG=ghcr.io/voyantt-consultancy-services-llp

# bot — two steps: the join environment base, then the bot.
docker build --platform linux/amd64 -f core/meetings/modules/join/Dockerfile.env -t vexa/meet-join-env:dev core/meetings/modules/join
docker build --platform linux/amd64 -f core/meetings/services/bot/Dockerfile \
  --build-arg VEXA_IMAGE_VERSION=$TAG -t $REG/aw-bots-bot:$TAG .
docker push $REG/aw-bots-bot:$TAG

# meeting-api (context: the repo root) and the exporter.
docker build --platform linux/amd64 -f core/meetings/services/meeting-api/Dockerfile \
  -t $REG/aw-bots-meeting-api:$TAG .
docker build --platform linux/amd64 -t $REG/aw-bots-exporter:$TAG integrations/out/aw-notetaker
docker push $REG/aw-bots-meeting-api:$TAG
docker push $REG/aw-bots-exporter:$TAG
```

Upstream's bot image is about 3.6–4.6 GB, mostly Chromium. Our change adds one small source file and
no dependencies. The exporter image is about 640 MB, mostly ffmpeg.

---

## Configuration

Every setting lives in configuration, not code:

| Setting | Where | Value for us |
|---|---|---|
| Live transcription | Helm values → meeting-api env `TRANSCRIBE_ENABLED` | `false` |
| Recording | `RECORDING_ENABLED` | `true` |
| Storage | `MINIO_BUCKET` + `S3_ENDPOINT` (IAM role on EKS, no static keys) | bucket `aw-bots` |
| meeting-api's IAM role (IRSA) | Helm `meetingApi.serviceAccount` (`create`, `name`, `annotations` with `eks.amazonaws.com/role-arn`) | its own service account, e.g. `aw-bots-meeting-api` |
| "Meeting finished" webhook | `VEXA_SYSTEM_WEBHOOK_URL`, `VEXA_SYSTEM_WEBHOOK_SECRET` (+ `…_ALLOW_PRIVATE_HTTP=true`) | the exporter's in-cluster URL |
| How early the bot joins | `AUTO_JOIN_LEAD_S` | measured from cold starts |
| Services on Karpenter | Helm `global.nodeSelector` / `global.tolerations` | the `aw-bots-services` NodePool |
| Bot pods on Karpenter | Helm `runtime.nodeSelector` / `runtime.tolerations` | the `aw-bots-meetings` NodePool |
| Postgres / Redis disks | Helm `postgres.persistence.storageClassName`, `redis.persistence.storageClassName` | `ebs-sc-gp3` (any zone, expandable) |
| Vexa's AI agents | Helm `agentApi.enabled` | `false` |
| Postgres password | Helm `postgres.existingCredentialsSecret` → pre-created Secret `postgres-credentials` | `true` (the chart then creates no Secret at all) |
| Bot size | Helm `runtime.workloadResources.meetingBot` | 1 CPU / 2560 MiB |
| Our images | Helm `meetingApi.image.*`, `runtime.browserImage` | our GHCR tags |
| Debug tape off | admin-api platform setting `capture_signal=false` | off (turn on only to debug a meeting) |
| Where the exporter sends meetings | exporter env `NOTETAKER_URL` | `http://notetaker-api.notetaker.svc.cluster.local:8080` |
| Exporter buckets | `VEXA_BUCKET`, `EXPORT_BUCKET`, `EXPORT_PREFIX` | `aw-bots`, `aw-chatworks-transcribe`, `recordings/` |
| How long exported files are kept | the exporter tags each object `retention-class`; the bucket's lifecycle rules act on the tag | `master.webm` = `recording-mp4` (30 days), `audio.wav` = `audio` (7 days), JSON = `metadata` (365 days) |
| Exporter ↔ Vexa | `MEETING_API_URL`, `VEXA_WEBHOOK_SECRET` (same value as `VEXA_SYSTEM_WEBHOOK_SECRET`) | in-cluster |

The full list of exporter settings is in
[`integrations/out/aw-notetaker/README.md`](integrations/out/aw-notetaker/README.md). Secret values
live only in Kubernetes Secrets, never in this repo.

---

## Deploy on EKS

The deployment files live in the **aw-notetaker** repo, next to the rest of AW's infrastructure, not in
this fork. The step-by-step runbook is `deployment/base/aw-bots/README.md` there. The files:

| aw-notetaker path | What it is |
|---|---|
| `deployment/base/aw-bots/values.yaml` | Our Helm values for the upstream chart |
| `deployment/base/aw-bots/*.yaml.template` | Secret templates (key names and placeholders only) |
| `deployment/base/aw-bots/nodepools.yaml` | AW Bots' two Karpenter pools: `aw-bots-services` and `aw-bots-meetings`, both amd64 and on-demand |
| `deployment/base/aw-exporter/` | The exporter's Deployment, Service, ServiceAccount and kustomization |
| `deployment/aws/iam/abroadworks-aw-bots-meeting-api-role/`, `…/abroadworks-aw-exporter-role/` | IAM roles (IRSA) |
| `deployment/aws/s3-lifecycle/aw-bots-lifecycle.json` | 14-day expiry for the `aw-bots` bucket |

In outline:

1. **Buckets and IAM.** Bucket `aw-bots` gets a 14-day expiry on `recordings/` and `signal/`. It holds
   Vexa's own files; the clean per-meeting folders are in `aw-chatworks-transcribe`, whose existing
   lifecycle rules expire objects by their `retention-class` tag. Create two IAM roles (IRSA):
   - meeting-api reads and writes `aw-bots`. The role attaches through `meetingApi.serviceAccount`.
   - The exporter reads `aw-bots` and writes `aw-chatworks-transcribe`, including
     `s3:PutObjectTagging`.
2. **Two Karpenter NodePools of AW Bots' own**, so it shares no machines with anything else in the
   cluster: `aw-bots-services` (one node for the chart's services, Postgres, Redis and the
   exporter) and `aw-bots-meetings` (bot pods). Both amd64 and **on-demand only** (never spot: a
   reclaimed node kills the meeting). Nodes are removed only when empty, so a live bot is never
   evicted.
3. **Install AW Bots.** `helm upgrade --install aw-bots deploy/helm/charts/vexa -n aw-bots -f <our values file>`.
   The values file lives in aw-notetaker; it sets the table above and points at our images.
4. **Deploy the exporter** from its manifests: one replica, `strategy: Recreate`.
5. **Set the debug tape off** (`capture_signal=false`).
6. **Create one AW Bots service account** (admin-api) and give its API key to the portal. Every meeting
   is created under this one account, which is what stops two bots joining the same shared meeting.

When upgrading the three images of ours separately, roll out meeting-api first, then the bot, then the
exporter. A new bot needs a meeting-api that accepts its speaker file, and the exporter needs bots that
write one. A single release of all three at once has no ordering issue.

---

## Run locally

- **One bot against a real meeting (Lite).** Needs Docker.
  ```bash
  make -C deploy/lite up RECORDING_DIR=$PWD/recordings LOCAL_STT=1
  deploy/lite/aw-recording.sh send <meet-code>       # the bot joins
  deploy/lite/aw-recording.sh status <meet-code>
  deploy/lite/aw-recording.sh export <meet-code>     # after the call
  make -C deploy/lite down RECORDING_DIR=$PWD/recordings
  ```
  Optional live copy to S3: `S3_MIRROR=1 S3_BUCKET=<bucket> S3_PREFIX=<prefix>`, with S3 credentials
  in `.env.local` (gitignored, read only by the mirror).
- **The full stack (Docker Compose).** `make dev` builds everything from this checkout; `make down`
  stops it. First run `bash deploy/compose/mint-dev-env.sh`, which creates the gitignored
  `deploy/compose/.env` with random local-only secrets.

---

## Tests and checks

| What | Command |
|---|---|
| Bot | `cd core/meetings/services/bot && npm test` (and `npx tsc --noEmit -p .`) |
| meeting-api | `cd core/meetings/services/meeting-api && uv run pytest` |
| Exporter | `cd integrations/out/aw-notetaker && pytest -q && black --check . && ruff check . && mypy exporter` |
| Exporter end-to-end (Docker, MinIO) | `pytest -m integration -q tests/integration` |
| Repo checks (the full suite) | `pnpm install && node scripts/gates.mjs all` |

**Before you push.** `git push` runs a pre-push hook (`.githooks/pre-push`) with 13 fast static
checks: READMEs, architecture, contracts and so on. It needs Node 22, `pnpm install` and
[`uv`](https://docs.astral.sh/uv/) (`brew install uv`). Every folder needs a non-empty `README.md`. A
local, gitignored folder can opt out with an empty `.gateignore` file.

The full suite additionally starts the whole stack in Docker, which needs the minted
`deploy/compose/.env`. Docker Hub refuses anonymous pulls of `minio/*`, so if that happens pull
`quay.io/minio/minio` and `quay.io/minio/mc` and tag them as `minio/minio:latest` and `minio/mc:latest`.

---

## Docs

- [Design](integrations/out/aw-notetaker/docs/2026-09-23-aw-rearchitecture-design.md): the architecture, decisions, deployment settings and risks.
- [Speaker activity design](integrations/out/aw-notetaker/docs/2026-09-23-speaker-activity-design.md): the who-spoke-when file.
- [Completion report](integrations/out/aw-notetaker/docs/2026-09-23-aw-exporter-completion-report.md): what was built, how it was checked, what is pending.
- Upstream Vexa docs: [`docs/docs`](docs/docs) and [docs.vexa.ai](https://docs.vexa.ai).

## License

Apache-2.0, as upstream Vexa. See [`LICENSE`](LICENSE).
