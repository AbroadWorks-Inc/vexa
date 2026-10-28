# Architecture

Who runs in this fork, and which calls are trusted. The picture of the whole path, from calendar
and portal through to the transcript, is in the [README](../../README.md#how-it-fits-together).
Image names and the settings table are there too. Route fields are in the
[`/v2` API reference](../../core/meetings/services/meeting-api/V2-API.md).

## What AW Bots is responsible for

AW Bots joins a Google Meet, Microsoft Teams, Zoom, or Jitsi call, records the mixed audio, and writes
who was speaking when. On Google Meet and Jitsi, when the deployment lists the platform, the bot
also records each remote channel beside that mix. When that meeting has a recording, the exporter
builds the folder the existing AW notetaker reads and calls `POST /process`. The transcript and the
summary are produced by `notetaker-worker` in the deployment repo.

`meet.abroadworks.com` is a Jitsi host on `VEXA_JITSI_HOSTS`. Intake does not refuse it.

Live transcription inside the bot is off. `TRANSCRIBE_ENABLED` is set false in the Helm values.
The recording is what gets transcribed. See the configuration table in the README.

## Services

One Helm release, `aw-bots`, in namespace `aw-bots`, chart `deploy/helm/charts/vexa`. The exporter
is a separate Deployment next to that release (`deployment/base/aw-exporter/` in aw-notetaker).

| Service | Code | Role |
|---|---|---|
| gateway | `core/gateway/services/gateway` | The only door for client keys. Checks the key's scope, sets `x-user-id`, and signs the identity it forwards. |
| admin-api | `core/identity/services/admin-api` | Users, API tokens, and webhook subscription rows. Owns the database schema. |
| meeting-api | `core/meetings/services/meeting-api` | Meetings, the `/v2` intake, recordings, the status writer, and webhook delivery. |
| runtime | `core/runtime` | Starts and stops bot pods. On EKS the backend is Kubernetes (`RUNTIME_BACKEND=k8s`). |
| bot | `core/meetings/services/bot` | One pod per meeting. Joins, records, writes `speaker-activity.jsonl`. |
| exporter | `integrations/out/aw-notetaker` | Subscribes to webhooks, builds the notetaker folder, calls `/process`. |
| Postgres and Valkey | chart defaults | Vexa's own database and bus, in this release. Separate from notetaker-postgres. |
| terminal | upstream `vexaai/v012-terminal` | Web console. Meetings do not pass through it. |

Agent API, dashboard, and flows stay off. We do not use Vexa's agents.

Six images are built from this repo by `.github/workflows/aw-images.yml`: meeting-api, gateway,
admin-api, runtime, bot, and exporter. Tags are the full commit SHA. The cluster runs the tag
pinned in aw-notetaker (`deployment/base/aw-bots/values.yaml` for the chart images and
`runtime.browserImage`, `deployment/base/aw-exporter/deployment.yaml` for the exporter). This
page does not copy those tags. A push to `development` publishes images. It does not roll the
cluster.

The runtime image is ours (`aw-bots-runtime`, context `core/runtime`). It deletes a bot pod after
the exit code is stored, so an exited pod does not hold the meeting node. See
[recording and speakers](recording-and-speakers.md#the-runtime-pod).

## Trust boundary

Clients (calendar-dispatcher, portal, exporter, an operator) call the gateway with `X-API-Key`.
The gateway is `aw-bots-vexa-gateway.aw-bots.svc.cluster.local:8000` from inside the cluster.

meeting-api and admin-api accept a client identity only when `x-gateway-signature` verifies under
`GATEWAY_IDENTITY_KEYS`. A direct call that sets `x-user-id` and carries no valid signature is
401. The rules are in [security](security.md).

Three other callers reach meeting-api without that client signature:

- The bot's status callback, with the internal secret.
- The runtime callback URL for that bot, with that bot's token.
- `/internal/*` between admin-api and meeting-api, with the internal secret.

`GET /metrics` on meeting-api and admin-api is not on the gateway. Prometheus scrapes the pod.
Admin routes that use `X-Admin-API-Key` are served by admin-api on port 8001.

## Request paths

**A meeting is handed in.** Calendar-dispatcher or the portal calls `PUT /v2/entries` (scope
`bot`). meeting-api stores an entry and, when the rules say so, one meeting. Reads of meetings
use scope `tx`. The route map is in the README. What an entry becomes is
[meeting lifecycle](meeting-lifecycle.md).

**A bot is started.** The auto-join sweep in meeting-api asks the runtime for a pod. The runtime
does not get the meeting from the client. The pod is scheduled on the meetings node pool
(`runtime.nodeSelector` in the Helm values). Chart services stay on the services pool.

**During the call.** The bot writes audio chunks and `speaker-activity.jsonl` to bucket `aw-bots`.
It reports status to meeting-api. Every status change goes through one writer, which stores the
webhook event in the same transaction. See [export and webhooks](export-and-webhooks.md).

**After the call.** Subscribers receive the event. The exporter, on `meeting.completed` or
`bot.failed` when there is audio, writes
`s3://aw-chatworks-transcribe/recordings/<folder>/` and calls `POST /process` on the notetaker
Service. It then reports that result to `POST /v2/meetings/{id}/export`.

## What lives outside this repo

| Piece | Where |
|---|---|
| Helm values, node pools, alert rules, the runbook | aw-notetaker `deployment/base/aw-bots/` |
| Exporter Deployment and its Secret templates | aw-notetaker `deployment/base/aw-exporter/` |
| notetaker-worker and the transcriber | the deployment repo, namespace `notetaker` and `transcriber` |
| Calendar sync and the portal UI | aw-notetaker, other owners. They are clients of `/v2`. |
