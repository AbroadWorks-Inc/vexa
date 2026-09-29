# deploy/helm — the v0.12 control-plane chart (Kubernetes)

The `helm` target of the lite/compose/helm trio: the full v0.12 stack as a Kubernetes release —
the control plane **gateway · admin-api · meeting-api · runtime · agent-api**, the **terminal** web
UI, and infra (`postgres:17` · `valkey:8` · `minio` + a `minio-init` bucket Job). The **terminal** is
the human front door (Next.js; proxies `/ws` → gateway and REST/login → agent-api/admin-api
server-side); the gateway stays the API front door for programmatic use. The difference from compose
is the **spawn substrate**: on k8s the `runtime` launches the bot and agent-worker as **Pods** (via
`kubectl`, under a chart-provided ServiceAccount/RBAC), selected by `RUNTIME_BACKEND=k8s` — not the
host Docker socket.

## Chart

[`charts/vexa`](charts/vexa/) — the full multi-service deployment. Production-hardened scaffolding
carried from the 0.10.6.3 baseline: zero-downtime `RollingUpdate` (maxSurge 1 / maxUnavailable 0),
PodDisruptionBudgets on stateless services, the Redis durability paired invariant, secret-sourced
DB/admin/provider credentials, optional PgBouncer for managed Postgres.

## Quick start (any cluster)

```bash
# 1. Pin the image tag your build produced (build-once promotion), fill secrets.
helm upgrade --install vexa deploy/helm/charts/vexa -n vexa --create-namespace \
  --set global.imageTag=YYMMDD-HHMM \
  --set secrets.adminApiToken=$ADMIN_TOKEN \
  --set secrets.internalApiSecret=$INTERNAL_API_SECRET \
  --set-file secrets.gatewayIdentityKeys=gateway-identity-ring.json \
  --set secrets.gatewayIdentityActiveKey=$GATEWAY_IDENTITY_ACTIVE_KEY \
  --set secrets.transcriptionServiceToken=$STT_TOKEN \
  --wait --timeout 10m

# 2. Watch it come up, then probe the front door.
kubectl -n vexa rollout status deploy/vexa-vexa-gateway
kubectl -n vexa port-forward svc/vexa-vexa-gateway 8000:8000 &
curl -sf localhost:8000/health
```

## Local k3s smoke (no registry)

```bash
make -C deploy/helm test     # static gate:helm — lint + render assertions, no cluster
make -C deploy/helm smoke    # build 5 images → import into k3s containerd → install → status
make -C deploy/helm down     # uninstall + drop namespace
```

`smoke` needs `sudo` (k3s writes a root-only kubeconfig at `/etc/rancher/k3s/k3s.yaml`) and a local
Docker to build the images. It proves the control plane stands up and `/health` is green.

## Configuration that matters

| Knob | Default | Notes |
|---|---|---|
| `global.imageTag` | `""` | Set to a pinned `YYMMDD-HHMM` tag — overrides every service tag (build-once). |
| `runtime.backend` | `k8s` | `k8s` spawns Pods via RBAC (real cloud); `docker` mounts the host socket (single-node only); `process` runs child processes. |
| `secrets.*` | placeholders | `adminApiToken`, `internalApiSecret`, `gatewayIdentityKeys`, `gatewayIdentityActiveKey`, `transcriptionServiceToken`, `dispatchSigningKey`, `nextauthSecret`, `anthropic*`. Or set `secrets.existingSecretName` (must carry `ADMIN_API_TOKEN`, `INTERNAL_API_SECRET`, `GATEWAY_IDENTITY_KEYS`, `GATEWAY_IDENTITY_ACTIVE_KEY`, `TRANSCRIPTION_SERVICE_TOKEN`, `VEXA_DISPATCH_SIGNING_KEY`, `NEXTAUTH_SECRET`). |
| `postgres.enabled` / `redis.enabled` / `minio.enabled` | `true` | Flip to `false` to use managed backing; then set `database.*` / `redisConfig.*` and a pre-existing `postgres.credentialsSecretName`. |
| `postgres.existingCredentialsSecret` | `false` | `true` keeps the in-cluster Postgres but reads its password from a pre-created `postgres.credentialsSecretName` Secret (`POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`); the chart renders none, so an upgrade never rewrites it. |
| `statefulAntiAffinity` | `true` | postgres, redis and minio prefer different nodes. `false` drops that preference when they are meant to share one node (Karpenter otherwise starts an extra node for it). |
| `pgbouncer.enabled` | `false` | Transaction pooler for managed Postgres with a fixed slot budget. |
| `terminal.enabled` | `true` | The web UI. Set `terminal.publicUrl` (NEXTAUTH_URL/TERMINAL_URL) when fronted by ingress; add OAuth via `terminal.extraEnv`. |
| `ingress.enabled` | `false` | Fronts the **terminal** by default; set `host`/`className`/`tls`. Add a second path to `gateway` to also expose the raw API. |
| `minio.service.type` | `ClusterIP` | `NodePort` to reach presigned download URLs browser-side on dev clusters. |
| `GATEWAY_IDENTITY_KEYS`, `GATEWAY_IDENTITY_ACTIVE_KEY`, `WEBHOOK_SECRET_ENC_KEYS`, `WEBHOOK_SECRET_ENC_ACTIVE_KEY` | none | The gateway identity ring (JSON `{"<kid>": "<32 bytes base64>"}`, the webhook ring's format) and its active kid come from the existing Secret or, in the chart-managed one, from the required `secrets.gatewayIdentityKeys` (pass it with `--set-file`) and `secrets.gatewayIdentityActiveKey`; the gateway reads both and refuses to start without them, and meeting-api and admin-api hold the same ring. The webhook key ring is read only from an existing Secret (meeting-api and admin-api); without it the webhook feature is off. |
| `meetingApi.entryMaxDaysAhead` / `joinNowAdoptAheadSeconds` / `entryBlockedHosts` / `intakeMaxActiveEntries` | `30` / `3600` / `""` / `100000` | Meeting intake (`ENTRY_MAX_DAYS_AHEAD`, `JOIN_NOW_ADOPT_AHEAD_S`, `ENTRY_BLOCKED_HOSTS`, `INTAKE_MAX_ACTIVE_ENTRIES`). |
| `meetingApi.intakeConflictRetries` | `3` | `INTAKE_CONFLICT_RETRIES`: retries for an entry write that lost a database-constraint race before it answers `500 internal_error`. |
| `meetingApi.intakeConflictDelayMinSeconds` / `intakeConflictDelayMaxSeconds` | `0.01` / `0.05` | `INTAKE_CONFLICT_DELAY_MIN_S`, `INTAKE_CONFLICT_DELAY_MAX_S` (§6.9 F-D): the random pause before each of those retries is a point between the two, times the try's number. |
| `meetingApi.intakeStopLinkRetries` | `1` | `INTAKE_STOP_LINK_RETRIES` (§1.7): how many more times `POST /v2/meetings/{id}/stop` runs again when the meeting moved to another link between its read and its link lock; after the last it answers `503 unavailable`. |
| `meetingApi.gatewayIdentityMaxSkewSeconds` / `adminApi.gatewayIdentityMaxSkewSeconds` | `60` / `60` | `GATEWAY_IDENTITY_MAX_SKEW_S` (§1.10): a gateway signature whose `t` is further than this from the service's clock is refused with 401 (the replay window). Set both the same. |
| `meetingApi.autoJoinLeadSeconds` / `notSentSweepIntervalSeconds` / `jitsiHosts` | `120` / `30` / `""` | `AUTO_JOIN_LEAD_S`, `NOT_SENT_SWEEP_INTERVAL_S`, `VEXA_JITSI_HOSTS`. |
| `meetingApi.botSendMaxAttempts` / `botSendRetryBackoffSeconds` | `3` / `60` | `BOT_SEND_MAX_ATTEMPTS`, `BOT_SEND_RETRY_BACKOFF_S`: retry budget and backoff for an entry-managed meeting's bot send. |
| `meetingApi.meetingUntrackedGraceSeconds` | `600` | `MEETING_UNTRACKED_GRACE_SEC` (§6.9 F-K2): how long a runtime 404 must last before a workload counts as gone, and how long past `due_at` a waiting meeting or an unfinished spawn gets before it ends `failed`. |
| `meetingApi.sweepBatchSize` / `sweepMaxItemFailures` | `200` / `5` | `SWEEP_BATCH_SIZE`, `SWEEP_MAX_ITEM_FAILURES`: paging size and give-up threshold shared by the auto-join tick, the not-sent sweep and the outbox publisher. |
| `meetingApi.unprovenTeardownMaxAgeSeconds` | `21600` | `UNPROVEN_TEARDOWN_MAX_AGE_S` (§6.9 F-K2, F-I): how long a workload's teardown is chased while the runtime doesn't answer before it is given up: a pending teardown (`data.unproven_teardown`) from its `since`, a `stale-stopping` or `stale-nonterminal` reconcile row from its `updated_at`. A runtime that doesn't answer is never one of the item's failures, so this age is its only bound; a refused delete still counts toward `SWEEP_MAX_ITEM_FAILURES`. |
| `meetingApi.sweepItemFailuresRetentionSeconds` | `604800` | `SWEEP_ITEM_FAILURES_RETENTION_S` (§6.9 F-I): a `sweep_item_failures` record untouched this long is deleted, `SWEEP_BATCH_SIZE` at a time, on each reconcile pass. A given-up item's record is touched whenever its sweep still lists it, so only records of items no longer pending go. |
| `meetingApi.webhookPrivateHostAllowlist` / `adminApi.webhookPrivateHostAllowlist` | `""` | `WEBHOOK_PRIVATE_HOST_ALLOWLIST`: private hosts a webhook may target; empty refuses every private target. Set both the same. |
| `meetingApi.webhookPublishIntervalSeconds` / `webhookSendIntervalSeconds` | `1` / `1` | `WEBHOOK_PUBLISH_INTERVAL_S`, `WEBHOOK_SEND_INTERVAL_S`. |
| `meetingApi.webhookRetryScheduleSeconds` / `webhookDnsThreads` / `webhookDnsTimeoutSeconds` | `60,300,1800,7200` / `4` / `5` | `WEBHOOK_RETRY_SCHEDULE_S`, `WEBHOOK_DNS_THREADS`, `WEBHOOK_DNS_TIMEOUT_S`: the sender's retry waits, its own DNS pool size and per-lookup timeout. |
| `meetingApi.webhookSendTimeoutSeconds` / `webhookLeaseSeconds` / `webhookClaimLimit` | `10` / `60` / `50` | `WEBHOOK_SEND_TIMEOUT_S`, `WEBHOOK_LEASE_S`, `WEBHOOK_CLAIM_LIMIT` (§1.8): the most one subscription post may take in total (longer is a failed attempt, error `timeout`), how long a sender's claim holds a delivery row (it must be more than the send timeout + 5 s, or meeting-api refuses to start), and the most rows one sender tick claims. |
| `adminApi.webhookMaxSubscriptions` / `webhookDeliveryRetentionDays` | `20` / `30` | `WEBHOOK_MAX_SUBSCRIPTIONS`, `WEBHOOK_DELIVERY_RETENTION_DAYS`. |
| `adminApi.webhookDeliveryRetentionBatchSize` / `webhookDeliveryRetentionMaxBatches` | `1000` / `100` | `WEBHOOK_DELIVERY_RETENTION_BATCH_SIZE`, `WEBHOOK_DELIVERY_RETENTION_MAX_BATCHES` (§6.9 F-I): the daily retention sweep deletes in batches of this many rows, each its own transaction, and at most this many batches of each delete per run; a run that reaches the cap logs a warning, counts `aw_sweep_runs_total{sweep="webhook-retention",result="capped"}` and leaves the rest to the next run. |
| `gateway.intakeRateLimitPerMin` | `600` | `INTAKE_RATE_LIMIT_PER_MIN`: entry writes per account per minute. |
| `gateway.rateLimitBurst` / `rateLimitRps` | `120` / `40` | `GATEWAY_RATE_LIMIT_BURST`, `GATEWAY_RATE_LIMIT_RPS` (WS-6): the per-user token bucket at the single REST funnel — burst capacity and refill rate. |
| `meetingApi.podAnnotations` / `adminApi.podAnnotations` | `{}` | Merged over `global.podAnnotations` for that service's pods; a key set here wins (e.g. `prometheus.io/scrape`). |

## Known boundaries (v0.12)

- **Bot spawn** works on k8s (the bot's config arrives as one env var). **Agent-worker** Pods mount
  the workspace store with **per-mount tenant isolation**: one `subPath` + `readOnly` volumeMount per
  granted workspace against the store PVC (`runtime_kernel/mounts.py:k8s_volume_mounts`) — a worker's
  filesystem contains only its dispatch's workspaces. Multi-node clusters need an **RWX** storage
  class for the store PVC (NFS/Longhorn; k3s `local-path` is RWO-only — single node works), with
  `agentApi.workspaces.accessMode: ReadWriteMany`.
- The `runtime` image bundles `kubectl` for the k8s backend; the docker/process backends ignore it.
- **`TRANSCRIPTION_MODEL` is not values-plumbed yet** (#522 ships the env on compose + Lite): to
  point k8s bots at a validating STT backend (Groq/vLLM), add the env to the meeting-api (and
  terminal) deployment via `extraEnv` for now; first-class `transcription.model` values plumbing is
  a declared follow-up.

## Contracts

This is a composition layer — it owns no service code and consumes none of the `*.v1` schemas
directly (each service vendors its own). It mirrors the [`deploy/compose`](../compose/) env contract.

## Smoke probe — "is this install actually working?"

```bash
make probe SURFACE=helm          # from the repo root; port-forwards if GATEWAY_URL is unset
GATEWAY_URL=http://<node>:<nodePort> make probe SURFACE=helm   # drive a NodePort directly
```

The full-journey smoke (spawn → schedule → boot → join → transcribe → live-view → stop) plus a
one-shot `kubectl logs` sweep of every deployment. Mints its API key through the release secret's
`ADMIN_API_TOKEN` unless `VEXA_API_KEY` is given. See `deploy/helm/probe.sh`.
