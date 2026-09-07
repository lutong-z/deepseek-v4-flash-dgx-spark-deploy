# Observability stack (Prometheus + Grafana + Loki)

The repository ships a complete, production-tested observability stack for the
two-node DGX Spark service: Prometheus (with alert rules and a Grafana
dashboard), Loki + Promtail for logs, Alertmanager, node exporters, and a
read-only RoCE/Infiniband fabric exporter. The same stack that has been
running against the production service since
[`docs/production-evidence.md`](production-evidence.md) is what this document
deploys.

## What you get

| Component | Purpose |
| --- | --- |
| Prometheus | Scrapes vLLM `/metrics`, node exporters, fabric exporter, lmcache-server, lmcache-lifecycle; alerts |
| Grafana | Dashboard: requests, tokens, latency, cache hits, KV usage, preemptions, fabric |
| Loki + Promtail | Centralized vLLM / system logs, with `NCCL|RoCE|RDMA|timeout|preempt` grep panel |
| Alertmanager | Routes the alert rules; no mail/webhook by default (edit `alertmanager.yml`) |
| node-exporter | Host CPU/mem/disk/network per node |
| fabric-exporter | GID validity, RoCE v2, link up, MTU, carrier flaps |

All images are **public** multi-arch containers (prometheus, grafana, loki,
alertmanager, promtail, node-exporter, python:alpine), so the stack does not
require the proprietary vLLM image, the model weights, or any private registry.

## Constraint: where it runs, and optional skip of the vLLM gate

The monitoring stack lives on the **same two DGX hosts** as the service
(sidecar containers with `com.dgx-spark.observability` labels). It needs SSH
access to both nodes. It also does a minimal vLLM `/health` + `/v1/models` +
`/metrics` check before and after deployment, so that it never reports
"monitoring up" against a dead service.

To stand the stack up **before** the model service exists (or while it is
briefly down), set `OBS_SKIP_VLLM_CHECK=1` in the environment file. The
Prometheus rule `up{job="vllm"} == 0` will honestly show the gap until the
service appears; no other behavior changes. This is the flag used by the
10-minute quickstart below.

## Prerequisites

- Python ≥ 3.11.
- SSH access from this machine to both DGX hosts (BatchMode, key auth).
- Docker with network manager on both hosts (the deploy command only uses
  `docker`, `docker network`, and shell builtins over SSH; it does not need
  the Docker socket on your laptop).

## 10-minute quickstart

The whole flow is: install → write env → resolve public image digests →
deploy. Steps 1-3 take about two minutes; step 4 pulls the public images.

### 1. Install

```bash
git clone <DEPLOY_REPOSITORY_URL> <DEPLOY_CHECKOUT>
cd <DEPLOY_CHECKOUT>
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
scripts/observability/deploy.sh --help
```

### 2. Write the environment file (outside the checkout)

```bash
mkdir -p ~/dgx-spark-observability && cat > ~/dgx-spark-observability/obs.env <<'EOF'
DEPLOYMENT_MODE=production
OBS_HEAD_HOST=192.168.100.10          # head data-plane IP (also the SSH host if no alias)
OBS_WORKER_HOST=192.168.100.11
OBS_HEAD_SSH_HOST=dgx-head-alias      # SSH alias / hostname for head
OBS_WORKER_SSH_HOST=dgx-worker-alias
OBS_SSH_USER=<YOUR_SSH_USER>
OBS_SKIP_VLLM_CHECK=1                 # stand up monitoring before the model service
OBS_HEAD_NODE_NAME=dgx-head           # display labels in Prometheus/Grafana
OBS_WORKER_NODE_NAME=dgx-worker
OBS_VLLM_CONTAINER_HEAD=dsv4-native432-lmcache-head    # container to follow for logs
OBS_VLLM_CONTAINER_WORKER=dsv4-native432-lmcache-worker
EOF
```

`OBS_VLLM_CONTAINER_*` must be valid container names; they select which
container's log file Promtail tails. Defaults are the reviewed
`dsv4-native432-dspark5-327k-seq5-{head,worker}` names.

### 3. Resolve immutable public image digests

This runs on the head node, pulls the public tags, and writes an
ARM64 digest-pinned lock (never a mutable tag):

```bash
scripts/observability/deploy.sh resolve-images \
  --env-file ~/dgx-spark-observability/obs.env \
  --output ~/dgx-spark-observability/observability-lock.json
```

The result is a complete ready lock with exact `repository@sha256` references,
digests, and image IDs for all seven images. Commit it next to your env file;
it is your reproducibility record.

### 4. Deploy (dry-run first, then confirmed)

```bash
scripts/observability/deploy.sh deploy \
  --env-file ~/dgx-spark-observability/obs.env \
  --image-lock ~/dgx-spark-observability/observability-lock.json \
  --confirm DGX-OBSERVABILITY --dry-run
```

Review the generated commands. Then repeat without `--dry-run`:

```bash
scripts/observability/deploy.sh deploy \
  --env-file ~/dgx-spark-observability/obs.env \
  --image-lock ~/dgx-spark-observability/observability-lock.json \
  --confirm DGX-OBSERVABILITY
```

`deploy` orders worker components before head components, publishes the ports
from the profile, and re-checks vLLM health afterwards unless
`OBS_SKIP_VLLM_CHECK=1`.

### 5. Access

| Service | URL |
| --- | --- |
| Grafana | `http://<head-ip>:13000` (default admin / `123456`; set `OBS_GRAFANA_ADMIN_PASSWORD`) |
| Prometheus | `http://<head-ip>:19090` |
| Alertmanager | `http://<head-ip>:19093` |
| Loki | `http://<head-ip>:13100` |

The dashboard is auto-provisioned; open Grafana → *Dashboards* →
*DGX Spark Overview*.

## Management commands

```bash
# status of the labelled containers on both hosts
scripts/observability/deploy.sh status --env-file ~/dgx-spark-observability/obs.env

# show the rendered Prometheus config etc. (local only; no SSH/Docker)
scripts/observability/deploy.sh render \
  --env-file ~/dgx-spark-observability/obs.env \
  --image-lock ~/dgx-spark-observability/observability-lock.json \
  --allow-mutable-images --output ~/dgx-spark-observability/render

# tear down the stack (keeps data volumes unless --purge-data)
scripts/observability/deploy.sh down --env-file ~/dgx-spark-observability/obs.env
scripts/observability/deploy.sh down --env-file ~/dgx-spark-observability/obs.env --purge-data
```

`down` removes only containers/volumes carrying the
`com.dgx-spark.observability` label; it never touches the vLLM service
containers or their data.

## What is scraped

- `job="vllm"` — `http://<head>:8101/metrics` (vLLM native metrics)
- `job="node"` — node-exporter on both hosts (19100)
- `job="fabric"` — fabric-exporter on both hosts (19110)
- `job="lmcache-server"` — lmcache server metrics on both hosts (19121)
- `job="lmcache-lifecycle"` — lmcache lifecycle metrics on both hosts (19120)
- prometheus, alertmanager, loki self-monitoring

The scrape interval is 15 s; retention is 7 d / 10 GB (Prometheus) and 168 h
(Loki); all configurable in
[`config/profiles/dgx-spark-observability.json`](../config/profiles/dgx-spark-observability.json).

## Alert rules (provisioned)

- `VllmDown` — `up{job="vllm"} == 0` for 2 min
- `NodeExporterDown`, `FabricExporterDown`
- `GpuCacheHigh` — KV-cache usage > 90 %
- `RequestsWaiting` — any request waiting
- `PreemptionsIncrease`
- `AbortedRequests`

Rules live in `prometheus/rules/observability.rules.yml` inside the stack
render (`render` output). Edit and redeploy to change them.

## Security notes

- Grafana auth defaults to `admin`/`123456`; set
  `OBS_GRAFANA_ADMIN_PASSWORD` to a strong value before exposing the ports
  beyond a trusted network.
- The stack publishes all probe ports on `0.0.0.0` (`OBS_BIND_ADDR`, default
  `0.0.0.0`). Set `OBS_BIND_ADDR=127.0.0.1` for loopback-only probing and
  SSH-tunnel access.
- Environment files, digests, and locks are operator-owned; keep them outside
  the checkout (they are gitignored).
