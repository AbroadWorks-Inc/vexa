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
 Calendar module (calendar-dispatcher)   Portal (users sign in with Google)
   │  PUT /v2/entries, POST /v2/entries/remove, reads, instant join, stop  (own key each)
   ▼
 AW Bots (this repo, deployed with Helm on EKS)
   ├─ gateway ─ admin-api ─ meeting-api ─ runtime ──► one bot pod per meeting (Karpenter nodes)
   │                                                   joins before start, records audio,
   │                                                   writes speaker-activity.jsonl
   │  stores recordings + speaker activity in  s3://aw-bots/
   │  a bot that fails mid-meeting is replaced on the same meeting ("bot.retry")
   │  every status change: signed webhook to each /v2/webhooks subscriber (the portal, the exporter)
   │  the exporter acts on  "meeting.completed", or "bot.failed" when a recording exists
   ▼
 exporter (our addition, integrations/out/aw-notetaker/)
   │  reads the meeting's recordings through the gateway (its own key)
   │  builds  s3://aw-chatworks-transcribe/recordings/<platform>_<meetingId>_<startUTC>/
   │          master.webm · audio.wav · speaker_timeline.json · participants.json · meeting.json …
   │  then calls  POST /process  (meeting UUID)  and reports the result to AW Bots
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

### The `/v2` API

Apps send meetings to AW Bots through the gateway. **Client developers start with the
[`/v2` API reference](core/meetings/services/meeting-api/V2-API.md)**: every
route, field, reply, error and webhook, with `curl` examples and signature-check code. The rules
behind it are Part 2 of the
[intake design](integrations/out/aw-notetaker/docs/2026-09-25-meeting-intake-and-webhooks-design.md);
they are sealed as `core/meetings/contracts/intake.v1/` and `webhook.v1/`.

| Call | Scope | Does |
|---|---|---|
| `PUT /v2/entries` | `bot` | Create or update one **entry**: one person's invite, or one click (`join_now: true` sends a bot now) |
| `POST /v2/entries/remove` | `bot` | Remove one entry, with an optional reason |
| `GET /v2/entries?user=` | `bot` | The sender's active entries for one user, with a hash of each, so a sender can send only the differences |
| `GET /v2/meetings?user=`, `GET /v2/meetings/{id}?user=` | `tx` | The meetings a user owns or is invited to |
| `POST /v2/meetings/{id}/stop` | `bot` | The live bot, in the call or still joining, leaves now (no live bot at all → `no_live_bot`) |
| `DELETE /v2/meetings/{id}` | `erase` | Erase a finished meeting's AW Bots data (recording copies in `aw-bots`, transcript rows, entries, webhook rows). Nothing in `aw-chatworks-transcribe` is deleted |
| `POST /v2/meetings/{id}/export` | `export` | The exporter reports its result |

- **One meeting per link and time.** Entries with the same meeting link at overlapping times are one
  meeting, within one account: three colleagues with the same invite are three entries, one
  meeting, one bot. Each meeting has a UUID, used in every reply, webhook, exported file and
  `/process` call.
- **One live bot per link.** A meeting due while another bot is still on its link waits, and its bot
  goes as soon as the link is free. A pasted link joins the meeting already there instead of
  sending a second bot.
- **Every request gets a definite answer**: a `result` or an error `code` in the shape
  `{"error": {"code", "message"}}`. Every meeting that ends without a bot carries a typed reason
  and the exact message.
- **Limits per account:** 600 entry writes a minute (the gateway, `429 rate_limited`) and 100 000
  active entries (`429 quota_exceeded`).

### Webhooks

- **Subscriptions** are managed through the gateway with the `webhooks` scope:
  `POST/GET /v2/webhooks`, `PATCH/DELETE /v2/webhooks/{id}`, `POST /v2/webhooks/{id}/rotate-secret`,
  `POST /v2/webhooks/{id}/test` and `GET /v2/webhooks/{id}/deliveries`. The receiver normally
  supplies its own secret. Secrets are stored encrypted and never shown again.
- **Every status change is an event**, written in the same transaction as the change. Each event
  carries the full meeting, an `event_id` and a `sequence` that rises by 1 per meeting.
- **Signing:** `X-Webhook-Timestamp: <unix seconds>` and
  `X-Webhook-Signature: sha256=<HMAC-SHA256(secret, "<timestamp>." + body)>`. For 24 hours after a
  secret is rotated, `X-Webhook-Signature-Previous` carries the same signature under the old
  secret; a receiver accepts a match on either header and rejects a timestamp more than 300 s off.
- **Delivery** is at least once and not in order. Retries after a 5xx, 429, timeout or connection
  error come after the waits in `WEBHOOK_RETRY_SCHEDULE_S` (default 1 min, 5 min, 30 min and 2 h),
  then the delivery is `dead`; any other answer that
  isn't 2xx (including a redirect) is `failed`. **Receivers dedupe on `event_id` and ignore an event
  whose `sequence` is older than one they already applied.**
- The exporter is a subscriber like the portal (its own subscription and secret). Upstream's
  system webhook (`VEXA_SYSTEM_WEBHOOK_URL` + `VEXA_SYSTEM_WEBHOOK_SECRET`, both or neither) and the
  per-user `webhook_url` carry upstream's meeting block and retry through Redis; AW sets neither.

### Keys and signed identity

- **Every client goes through the gateway.** The gateway checks the key's scope, sets `x-user-id`
  and signs it with the active key of the `GATEWAY_IDENTITY_KEYS` ring, naming it (`kid`), over the
  user and every other `x-user-*` header it forwards (email, scopes, limits, workspaces, webhook
  URL, secret and events), the method, path, query and body. meeting-api and admin-api refuse a
  client request whose signature is missing, wrong, under a kid not in their ring or more than
  `GATEWAY_IDENTITY_MAX_SKEW_S` (60 s) off their clock, or that carries an identity header twice,
  so a direct call with `x-user-id` gets 401.
  Only the bots, the runtime and service-to-service calls reach meeting-api directly: bot status
  callbacks carry the internal secret, each bot's runtime callback URL carries its own token, and
  the `/internal/*` routes between admin-api and meeting-api check the internal secret.
- **One key per consumer**, all under the service user, with only the scopes it needs:

  | Key name | Scopes | Where it lives |
  |---|---|---|
  | `calendar-dispatcher` | `bot` | Secret `aw-bots-key-calendar-dispatcher` (namespace `notetaker`) |
  | `portal` | `bot`, `tx` | Secret `aw-bots-key-portal` (namespace `notetaker`) |
  | `exporter` | `tx`, `export` | Secret `aw-bots-key-exporter` (namespace `aw-bots`) |
  | `operator` | `webhooks`, `erase` | the operator vault only |

  The `/user/*` routes accept only the user scopes `bot`, `tx` and `browser`.
- **Rotation:** mint a new key with the same name (`expires_in` 365 days), update its Secret, roll
  the Deployment, then revoke the old key by id. admin-api's metrics show the time left on each
  named key.

---

## What we changed

Everything else is upstream Vexa, unchanged.

| Change | Where | Why |
|---|---|---|
| **Speaker activity file.** The bot always writes `speaker-activity.jsonl`: who spoke when, with no audio, about 1–40 MB for a 3-hour meeting. meeting-api accepts it as a new signal file. | `core/meetings/services/bot/src/speaker-activity.ts` (+ small wiring in `capture-bridge.ts`, `index.ts`, `signal-upload.ts`); `core/meetings/services/meeting-api/src/meeting_api/recordings/jsonb.py` | Vexa kept this data only inside its debug tape, which also stores everyone's audio and stops at 250 MB (about 50 minutes). Long meetings lost their speaker names. |
| **Exporter.** A new small service. It learns that a meeting finished from its own `/v2/webhooks` subscription (the §2.4 meeting carries `upstream_id`, `started_at` and `ended_at` for it), reads through the gateway with its own key and names everything by the meeting's UUID. | `integrations/out/aw-notetaker/` | Turns each finished meeting that has a recording (completed, or failed after its bot recorded part of the call) into the folder the AW notetaker pipeline reads, and hands it over. Its trigger is stored in Postgres and retried on a bounded schedule, like every other subscriber's. |
| **Helm chart: meeting-api service account.** Optional `meetingApi.serviceAccount` (default off; the default render is unchanged). | `deploy/helm/charts/vexa` (`values.yaml`, `templates/serviceaccount-meeting-api.yaml`, `deployment-meeting-api.yaml`) | Lets meeting-api get its own IAM role (IRSA) for the `aw-bots` bucket, like our other services' service accounts. |
| **Helm chart: pre-created Postgres credentials.** Optional `postgres.existingCredentialsSecret` (default off; the default render is unchanged). | `deploy/helm/charts/vexa` (`values.yaml`, `templates/secret.yaml`), tests in `deploy/helm/tests/test_template.sh` | Keeps the in-cluster Postgres but reads its password from a Secret we create, so a `helm upgrade` never rewrites it. |
| **Meeting intake (`/v2`).** Entries, meetings, stop, erase and the export report; one meeting per link and time, one live bot per link; the scheduler sends the bot for the exact meeting that is due. The meeting (§2.4) carries `upstream_id`, `started_at` and `ended_at` too. | `core/meetings/services/meeting-api/src/meeting_api/intake/` (+ changes in `bot_spawn/`, `lifecycle/`, `collector/`); contract `core/meetings/contracts/intake.v1/` | Any app (the calendar module, the portal) can hand AW Bots its meetings and get a definite answer, without two bots in one call. |
| **One status writer and the webhook outbox.** Every status change goes through one function, which records the event in the same transaction. Leased senders deliver it, with their state in Postgres. | `meeting_api/intake/status.py`, `meeting_api/webhooks/`; new `webhook.v1` events | No change is lost or sent twice from our side, whichever meeting-api replica handles it. |
| **One finish.** Every way a meeting ends, whether the bot reports it or aw-bots ends the meeting itself, runs the same finish steps: the transcript finalized, the service provenance, the typed event and its edges (flows, the per-user `webhook_url`, the system hook when one is set), the copilot reap. | `meeting_api/app.py` (`_finish`) | A meeting's end is reported the same way whoever ended it. |
| **Bounded bot sends.** A meeting entries manage gets at most `BOT_SEND_MAX_ATTEMPTS` sends, `BOT_SEND_RETRY_BACKOFF_S` apart; the last failure ends it `not_sent` with the typed code. | `meeting_api/intake/service.py`, `bot_spawn/auto_join.py`; `meeting_aw_state.send_attempts` | A bot that can't be sent is tried again a few times, never forever, and the reason reaches the webhook and the metrics. |
| **A new bot on the same meeting.** A bot that fails while its meeting is on (a failed start, joining, a lobby rejection or timeout, a crash in the call, a lost bot) is replaced on the same meeting, from the same send count, once the failed pod is proven gone; a deadline, a pending-teardown list, a spawn that names its workload before it creates it, and a reconcile backstop bound every case. Not replaced: a user's stop, the host removing the bot, nobody joining, a normal end. | `meeting_api/intake/retry.py`, `bot_spawn/`, `lifecycle/reconcile.py`; webhook `bot.retry` | Never two bots in one call, and a meeting whose bot failed early still gets recorded. The exporter joins every session's audio and speakers into one folder. |
| **Bounded sweeps.** Every intake sweep, and upstream's reconcile loops (`stale-stopping`, `stale-nonterminal`), reads in pages of `SWEEP_BATCH_SIZE` and gives an item up after `SWEEP_MAX_ITEM_FAILURES`, logged and counted. A runtime that doesn't answer is never an item's failure: a teardown waits for it, counted `runtime_unreachable`, until `UNPROVEN_TEARDOWN_MAX_AGE_S`. Give-up records are kept while their item is listed and pruned after `SWEEP_ITEM_FAILURES_RETENTION_S`; admin-api's webhook retention deletes in bounded batches. An entry write that loses a database race is run again `INTAKE_CONFLICT_RETRIES` times, then answered 500. | `meeting_api/sweeps/item_failures.py` (`run_pages`, `run_item`), `lifecycle/reconcile.py` (`runtime_bound`), table `sweep_item_failures`; `intake/service.py`; `admin_api/app/retention.py` | Nothing retries forever, and one bad item never stops the rest. |
| **Webhook subscriptions, scopes and keys.** `/v2/webhooks`, encrypted receiver secrets, new scopes `webhooks`, `erase`, `export`. | `core/identity/services/admin-api`; `core/identity/contracts/identity.v1/` | Each app subscribes on its own; each consumer's key can do only its own job. |
| **Signed gateway identity; callback checks.** The gateway signs the user it forwards; meeting-api and admin-api check it. Bot callbacks must carry the internal secret; runtime callbacks carry a per-bot token. The gateway limits entry writes per account and answers `/v2` errors in the `/v2` shape. | `core/gateway/services/gateway`, meeting-api, admin-api | A pod inside the cluster can't act as any user by setting a header. |
| **Metrics.** `/metrics` on meeting-api and admin-api (not routed through the gateway). | `meeting_api/metrics.py`, `admin_api/app/metrics.py`; Helm `meetingApi.podAnnotations`, `adminApi.podAnnotations` | Prometheus scrapes them; the alerts live in aw-notetaker. |
| **Image workflow.** Builds and pushes our five images (meeting-api, bot, exporter, gateway, admin-api) to GHCR. | `.github/workflows/aw-images.yml` | Images come from CI on every push to `development`, or on a manual run with a release tag. |
| **Lite helper for local tests.** | `deploy/lite/Makefile`, `deploy/lite/aw-recording.sh` | Run one bot on a laptop against a real meeting and get the files out. |

The design and the reasoning behind each decision live in
[`integrations/out/aw-notetaker/docs/`](integrations/out/aw-notetaker/docs/README.md).

---

## Repository map

Folders we work in:

| Folder | What it is |
|---|---|
| `core/meetings/services/bot` | The meeting bot (TypeScript, Playwright + Chromium). One container per meeting. |
| `core/meetings/services/meeting-api` | Meetings, recordings and signal files; the `/v2` intake; auto-join; webhooks (Python). |
| `core/identity/services/admin-api` | Users, API keys and scopes; webhook subscriptions; the database schema (Python). |
| `core/gateway/services/gateway` | The API front door: keys, scopes, the signed identity, the per-account write limit (Python). |
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
| `feat/<topic>`, `fix/<topic>` | Work in progress, cut from `development` (current: `feat/meeting-intake`). |
| `aw/main` and `aw/*` | The old cloud bot (Vexa v0.10.4). Kept for reference only. |

**Taking upstream changes:** update `main` from upstream, then merge `main` into `development` on a
branch and run the checks. Renovate currently opens dependency-bump branches against `aw/main`, the
old line, so those branches don't bring upstream changes into `development`.

---

## Services and images

One Helm install runs all of these. Each service is its own Docker image.

| Service | Image | Ours or upstream |
|---|---|---|
| **gateway** (API front door) | built from this repo | **ours** (the `/v2` routes, new scopes, signed identity, write limit) |
| **admin-api** (users, API keys, settings) | built from this repo | **ours** (webhook subscriptions, new scopes, checks the signed identity) |
| **meeting-api** | built from this repo | **ours** (accepts `speaker-activity`; the `/v2` intake and webhooks) |
| **runtime** (starts bot pods) | built from this repo | **ours** (deletes a bot pod once it has exited, so the meeting node can scale to zero) |
| **bot** | built from this repo | **ours** (writes `speaker-activity.jsonl`) |
| terminal (web console) | `vexaai/v012-terminal` | upstream |
| agent-api, dashboard, flows | `vexaai/*` | upstream; all three **off** for us (we don't use Vexa's AI agents) |
| postgres, redis (Valkey) | chart defaults, inside the same install | upstream; Vexa's own database and message bus, separate from the notetaker's |
| **exporter** | built from `integrations/out/aw-notetaker` | **ours**, deployed next to the chart |

Our images go to GHCR, under the names `ghcr.io/voyantt-consultancy-services-llp/aw-bots-meeting-api`,
`…/aw-bots-bot`, `…/aw-bots-exporter`, `…/aw-bots-gateway` and `…/aw-bots-admin-api`.
**The gateway and admin-api need our images, like meeting-api:** upstream's `vexaai/v012-gateway` and
`vexaai/v012-admin-api` don't have the `/v2` routes, the new scopes or the signed identity, and the
new meeting-api refuses every client request that upstream's gateway forwards unsigned. Our Helm
values must point `gateway.image` and `adminApi.image` at them.

### Build our images

**Normally CI builds them**: `.github/workflows/aw-images.yml` ("AW images — build & push").

| Trigger | Tags pushed |
|---|---|
| Push to `development` | `:<commit sha>` for all five images |
| Manual run (Actions → Run workflow: branch, tag e.g. `v0.1.1`, `all` or one image) | `:<commit sha>` and `:<tag>` |

To build one image with a version tag, for example the gateway or admin-api, use the manual run
and pick that image.

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

# gateway (context: the repo root) and admin-api (context: its own folder): build as CI does.
docker build --platform linux/amd64 -f core/gateway/services/gateway/Dockerfile -t $REG/aw-bots-gateway:$TAG .
docker build --platform linux/amd64 -t $REG/aw-bots-admin-api:$TAG core/identity/services/admin-api
docker push $REG/aw-bots-gateway:$TAG
docker push $REG/aw-bots-admin-api:$TAG
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
| "Meeting finished" webhook | the exporter's `/v2/webhooks` subscription (`events: ["meeting.completed", "bot.failed"]`), secret = exporter env `EXPORTER_WEBHOOK_SECRET`; upstream's `VEXA_SYSTEM_WEBHOOK_URL` | subscription URL `http://aw-exporter.aw-bots.svc.cluster.local:8080/hooks/vexa`; `VEXA_SYSTEM_WEBHOOK_URL` unset |
| How early the bot joins | Helm `meetingApi.autoJoinLeadSeconds` → env `AUTO_JOIN_LEAD_S` (the chart's default is 120) | `300` |
| Services on Karpenter | Helm `global.nodeSelector` / `global.tolerations` | the `aw-bots-services` NodePool |
| Bot pods on Karpenter | Helm `runtime.nodeSelector` / `runtime.tolerations` | the `aw-bots-meetings` NodePool |
| Postgres / Redis disks | Helm `postgres.persistence.storageClassName`, `redis.persistence.storageClassName` | `ebs-sc-gp3` (any zone, expandable) |
| Vexa's AI agents | Helm `agentApi.enabled` | `false` |
| Postgres password | Helm `postgres.existingCredentialsSecret` → pre-created Secret `postgres-credentials` | `true` (the chart then creates no Secret at all) |
| Bot size | Helm `runtime.workloadResources.meetingBot` | 1 CPU / 2560 MiB |
| Our images | Helm `meetingApi.image.*`, `gateway.image.*`, `adminApi.image.*`, `runtime.browserImage` | our GHCR tags |
| Debug tape off | admin-api platform setting `capture_signal=false` | off (turn on only to debug a meeting) |
| Where the exporter sends meetings | exporter env `NOTETAKER_URL` | `http://notetaker-api.notetaker.svc.cluster.local:8080` |
| Exporter buckets | `VEXA_BUCKET`, `EXPORT_BUCKET`, `EXPORT_PREFIX` | `aw-bots`, `aw-chatworks-transcribe`, `recordings/` |
| How long exported files are kept | the exporter tags each object `retention-class`; the bucket's lifecycle rules act on the tag | `master.webm` = `recording-mp4` (30 days), `audio.wav` = `audio` (7 days), JSON = `metadata` (365 days) |
| Exporter ↔ AW Bots | `GATEWAY_URL` + `EXPORTER_API_KEY` (the `exporter` key, scopes `tx` + `export`); `EXPORTER_WEBHOOK_SECRET` (the secret of its subscription) | the gateway's in-cluster URL; key from Secret `aw-bots-key-exporter` |
| Signed identity | `GATEWAY_IDENTITY_KEYS` (a key ring in the webhook ring's format) on the gateway, meeting-api and admin-api (one value); `GATEWAY_IDENTITY_ACTIVE_KEY` (the kid it signs with) on the gateway; `GATEWAY_IDENTITY_MAX_SKEW_S` (the replay window, 60 s) on meeting-api and admin-api, set the same | **required**: without them the gateway refuses to start and meeting-api and admin-api refuse every client request. Rotation: add the new key to the ring on all three and roll; switch the active kid on the gateway and roll; later drop the old key and roll |
| Webhook secret encryption | `WEBHOOK_SECRET_ENC_KEYS` (a key ring) and `WEBHOOK_SECRET_ENC_ACTIVE_KEY`, on meeting-api and admin-api, read only from an existing Secret | required for webhook subscriptions; unset, subscriptions are off and the services log why |
| Intake | `ENTRY_MAX_DAYS_AHEAD`, `JOIN_NOW_ADOPT_AHEAD_S`, `ENTRY_BLOCKED_HOSTS`, `INTAKE_MAX_ACTIVE_ENTRIES` (meeting-api); `INTAKE_RATE_LIMIT_PER_MIN` (gateway) | 30 days, 3600 s, `meet.abroadworks.com` until the Jitsi cutover, 100 000, 600 |
| Webhooks | `WEBHOOK_PRIVATE_HOST_ALLOWLIST` (meeting-api, admin-api); `WEBHOOK_MAX_SUBSCRIPTIONS`, `WEBHOOK_DELIVERY_RETENTION_DAYS` (admin-api) | `portal.notetaker.svc.cluster.local,aw-exporter.aw-bots.svc.cluster.local`, 20, 30 days |
| Webhook retention batches | `WEBHOOK_DELIVERY_RETENTION_BATCH_SIZE`, `WEBHOOK_DELIVERY_RETENTION_MAX_BATCHES` (admin-api): rows per delete batch, and the per-run cap of batches of each delete | 1000, 100 |
| Webhook delivery | `WEBHOOK_RETRY_SCHEDULE_S`, `WEBHOOK_DNS_THREADS`, `WEBHOOK_DNS_TIMEOUT_S`, `WEBHOOK_SEND_TIMEOUT_S`, `WEBHOOK_LEASE_S` (more than the send timeout + 5 s), `WEBHOOK_CLAIM_LIMIT` (meeting-api) | `60,300,1800,7200`, 4, 5 s, 10 s, 60 s, 50 |
| Upstream's system webhook | `VEXA_SYSTEM_WEBHOOK_URL`, `VEXA_SYSTEM_WEBHOOK_SECRET` (meeting-api; both or neither) | unset |
| Bot sends and retries | `BOT_SEND_MAX_ATTEMPTS`, `BOT_SEND_RETRY_BACKOFF_S` (meeting-api), for meetings entries manage; a bot replaced after it failed spends the same count | 3, 60 s |
| Proof that a failed bot is gone | `MEETING_UNTRACKED_GRACE_SEC` (meeting-api): how long a runtime 404 must last, and how long past its due time a waiting meeting or an unfinished spawn gets | 600 s |
| Bounded work | `SWEEP_BATCH_SIZE`, `SWEEP_MAX_ITEM_FAILURES`, `INTAKE_CONFLICT_RETRIES`, `INTAKE_CONFLICT_DELAY_MIN_S`/`_MAX_S` (the pause before each rerun, times the try's number), `INTAKE_STOP_LINK_RETRIES` (reruns of a stop whose meeting moved link) (meeting-api) | 200, 5, 3, 0.01–0.05 s, 1 |
| Teardowns the runtime doesn't answer | `UNPROVEN_TEARDOWN_MAX_AGE_S` (meeting-api): how long a pending teardown or a stale reconcile row is chased while the runtime doesn't answer, before it is given up | 21600 s (6 h) |
| Give-up records | `SWEEP_ITEM_FAILURES_RETENTION_S` (meeting-api): an untouched `sweep_item_failures` record is pruned after this | 604800 s (7 days) |
| Per-user rate limit | `GATEWAY_RATE_LIMIT_BURST`, `GATEWAY_RATE_LIMIT_RPS` (gateway, WS-6): the per-user token bucket at the single REST funnel | 120, 40/s |

Every meeting-api, admin-api and gateway setting is declared in its service's `config.v1.json`;
the chart's `meetingApi.*`, `adminApi.*` and `gateway.*` values set every one above. The full list
of exporter settings (among them `EXPORTER_WEBHOOK_SECRET` and `EXPORT_MAX_RECORDINGS`) is in
[`integrations/out/aw-notetaker/README.md`](integrations/out/aw-notetaker/README.md). Secret values
live only in Kubernetes Secrets, never in this repo. On EKS the four new keys (the two key rings
and their active ids) go into `aw-bots-secrets` **before** the `helm upgrade` that brings the new
services, and `VEXA_SYSTEM_WEBHOOK_SECRET` leaves it right before the upgrade that turns the system
webhook off. Docker Compose, Lite and
a chart-managed Secret (`secrets.gatewayIdentityKeys`, `secrets.gatewayIdentityActiveKey`) supply
`GATEWAY_IDENTITY_KEYS` and `GATEWAY_IDENTITY_ACTIVE_KEY` too; they don't need the webhook key ring.

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
6. **Create one AW Bots service account** (admin-api). Every meeting is created under this one account,
   which is what stops two bots joining the same shared meeting. Mint its four named keys
   (`calendar-dispatcher`, `portal`, `exporter`, `operator`; see [Keys and signed identity](#keys-and-signed-identity))
   into their Secrets. Operators, too, reach admin-api's `/user/*` routes through the gateway: a
   direct call with an API key (for example over a port-forward) gets 401.

When upgrading our images separately, roll out the gateway and admin-api first, then meeting-api, then
the bot, then the exporter. The new meeting-api refuses the unsigned requests an old gateway forwards;
a new bot needs a meeting-api that accepts its speaker file; the exporter needs the export route and
bots that write a speaker file.

**Bringing in the intake and webhooks release** follows the rollout order in Part 5 of the
[intake design](integrations/out/aw-notetaker/docs/2026-09-25-meeting-intake-and-webhooks-design.md),
with no meeting in progress until the exporter's subscription exists. In short: the database
migration (`MIGRATION-0008`) first; the gateway identity ring and the webhook key ring into
`aw-bots-secrets` before any `helm upgrade`; our gateway and admin-api images, with the system
webhook off in the same upgrade; the four keys; drain the exporter queue with the old exporter;
then meeting-api and the bot, and only after them the exporter's `EXPORTER_WEBHOOK_SECRET`, the new
exporter and its subscription (see its [README](integrations/out/aw-notetaker/README.md)). The
commands are aw-notetaker's runbook, steps 9–18.

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
| admin-api, gateway | the same, in `core/identity/services/admin-api` and `core/gateway/services/gateway` |
| Exporter | `cd integrations/out/aw-notetaker && uv run pytest -q && uv run black --check . && uv run ruff check . && uv run mypy exporter` |
| Exporter end-to-end (Docker, MinIO) | `uv run pytest -m integration -q tests/integration` (from `integrations/out/aw-notetaker`) |
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
- [Meeting intake and webhooks design](integrations/out/aw-notetaker/docs/2026-09-25-meeting-intake-and-webhooks-design.md): the `/v2` API, webhooks, keys, signed identity and the rollout order.
- [`/v2` API reference](core/meetings/services/meeting-api/V2-API.md): for client developers — every route, field, reply, error and webhook, with examples.
- [Speaker activity design](integrations/out/aw-notetaker/docs/2026-09-23-speaker-activity-design.md): the who-spoke-when file.
- [Completion report](integrations/out/aw-notetaker/docs/2026-09-23-aw-exporter-completion-report.md): what was built, how it was checked, what is pending.
- Upstream Vexa docs: [`docs/docs`](docs/docs) and [docs.vexa.ai](https://docs.vexa.ai).

## License

Apache-2.0, as upstream Vexa. See [`LICENSE`](LICENSE).
