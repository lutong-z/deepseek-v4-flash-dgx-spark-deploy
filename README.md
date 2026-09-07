# DGX Spark DeepSeek V4 deployment

[![CI](https://github.com/lutong-z/deepseek-v4-flash-dgx-spark-deploy/actions/workflows/test.yml/badge.svg)](https://github.com/lutong-z/deepseek-v4-flash-dgx-spark-deploy/actions/workflows/test.yml)
[![Production-tested](https://img.shields.io/badge/production-tested-2ea44f)](docs/production-evidence.md)
[![ARM64](https://img.shields.io/badge/arch-ARM64%20GB10-blue)]()
[![tests](https://img.shields.io/badge/tests-94%20passing-2ea44f)]()

**Production-tested.** This repository has carried real traffic in a
two-node ARM64 DGX Spark production deployment for over 21 consecutive hours
with **zero failed requests**, **zero preemptions**, **~98 % prompt-token
cache hit ratio**, and a single planned 3.5-minute switch during rollout. Full
measured numbers, metric names, and reproducibility steps are in
[`docs/production-evidence.md`](docs/production-evidence.md).

### Why this repository is trustworthy

- **Measured, not claimed.** Every number in
  [`docs/production-evidence.md`](docs/production-evidence.md) was read from
  the live Prometheus instance this repository deploys — metric names,
  PromQL, and the dashboard are versioned here, so the evidence is
  reproducible against the same revision.
- **No opaque infrastructure.** The deploy tool renders immutable
  head/worker contracts locally, plans changes before touching a node, and
  performs read-only preflights. 94 unit/integration tests run in CI on
  every push and pull request.
- **Fail-closed by default.** The committed image lock is intentionally not
  deployable; production mutation requires an external operator-owned
  environment, a ready lock, and an explicit `--confirm` of the printed
  deployment ID. Nothing here can mutate your nodes by accident.
- **10 minutes to first value.** The
  [observability stack](docs/observability.md) runs on public images only —
  no model weights, no private registry — and the whole flow is
  copy-pasteable.

| Status | Value |
| --- | --- |
| Runtime proven (continuous) | 21+ h, no restart |
| Requests served (24 h) | ≈ 3,990, **0 failed** |
| Cache hit ratio (prompt tokens) | ≈ 98 % |
| Preemptions | 0 |
| Peak concurrency | 8 running requests |
| Stack | Prometheus / Grafana / Loki / Alertmanager, auto-provisioned |

This repository is the public, executable deployment path for the reviewed
DeepSeek V4 two-node service. It renders immutable head/worker contracts,
plans changes locally, performs read-only preflights, and applies lifecycle
changes through strict SSH. The default path is redacted and dry-run only.

## Quick paths

- **[10-minute observability stack](docs/observability.md)** — stand up
  Prometheus/Grafana/Loki on your DGX pair with only public images and a
  handful of commands. No model weights, no private registry.
- **[Production evidence](docs/production-evidence.md)** — measured
  availability, latency, caching, and fabric health from the running service.
- **[Model service, minimal path](docs/quickstart-vllm.md)** — the compressed
  zero-to-serving command sequence with honest time budgets and a
  minutes-long reprovision path.
- **[Zero-to-production](README.md#zero-to-production-quickstart)** — the full
  reviewed deployment path (model fetch/verify, immutable image lock,
  lifecycle commands).

> The Zero-to-production quickstart below is the complete, reviewed path for
> deploying the **model service**. It requires the 166.9 GB locked model tree
> and built images (hours, not minutes). If your goal is monitoring first,
> start with the [10-minute observability stack](docs/observability.md).

### 10-minute tryout — no DGX, no Docker required

You can exercise the production deploy tool end-to-end on any machine with
Python ≥ 3.11, against **synthetic** hosts. `render`, `plan`, and
`deploy --dry-run` never SSH and never touch Docker; they print the exact
commands a real deploy would run. This is a safe first contact with the
repository and doubles as the CI smoke test.

```bash
git clone <DEPLOY_REPOSITORY_URL> demo && cd demo
python3 -m venv .venv && . .venv/bin/activate
python -m pip install -e .
tests/dry_run.sh                      # env validation + redacted plan + mutation rejected
python tests/model_dry_run.sh         # model fetch plan prints without network
bin/dgx-deploy --help
```

That is the same sequence CI runs on every push
([`.github/workflows/test.yml`](.github/workflows/test.yml)). The 10-minute
observability deploy on real hardware is [`docs/observability.md`](docs/observability.md).

Production mutation requires all of the following:

- an operator-owned environment file outside this checkout;
- a complete external deployment lock with `status: "ready"`;
- immutable per-role image references and exact image IDs;
- all seven required OCI provenance labels on both images;
- a model tree verified against `model.lock.json` and its marker;
- a successful repository dry-run and preflight; and
- `--confirm` equal to the deployment ID printed by `plan`.

The committed `image.lock.json` is intentionally `pending-artifacts`. It is a
public provenance record, not a deployable image lock. Never change it to
`ready-for-review` without the independent image/build evidence required by
[`docs/image.md`](docs/image.md).

## Zero-to-production quickstart

The following commands are copy-pasteable after replacing every angle-bracket
placeholder. Keep every operator-owned file and root outside the repository.
The aliases below are examples for a local SSH config; use the aliases that
resolve to the two DGX nodes, not host records embedded in this repository.

### 1. Install the deploy tool

```bash
git clone <DEPLOY_REPOSITORY_URL> <DEPLOY_CHECKOUT>
cd <DEPLOY_CHECKOUT>
python3 -m venv <PATH>/venv
. <PATH>/venv/bin/activate
python -m pip install -e .
bin/dgx-deploy --help
```

Python 3.11 or newer is required. The CLI never sources an environment file as
shell. It accepts only unique `KEY=VALUE` records and rejects shell syntax,
unknown keys, placeholders, unsafe paths, mutable tags, and checkout-local
roots.

### 2. Create the operator environment

```bash
mkdir -p <PATH>/dgx-spark-production
cp .env.example <PATH>/dgx-spark-production/production.env
$EDITOR <PATH>/dgx-spark-production/production.env
```

Set the following production values. Do not paste these values into a public
commit:

```text
DEPLOYMENT_MODE=production
HEAD_HOST=192.168.100.10
WORKER_HOST=192.168.100.11
HEAD_SSH_HOST=DGX-SPARK-0
WORKER_SSH_HOST=DGX-SPARK-1
SSH_USER=<REMOTE_SSH_USER>
SSH_PORT=22
SSH_KNOWN_HOSTS_FILE=<PATH>/known_hosts
SSH_IDENTITY_FILE=<PATH>/ssh-key
REMOTE_ROOT=<REMOTE_PATH>/dgx-spark/deploy
MODEL_ROOT=<REMOTE_PATH>/models/DeepSeek-V4-Flash-0731
MODEL_CONTAINER_PATH=/models/DeepSeek-V4-Flash-0731
MODEL_MANIFEST_SHA256=<SHA256_OF_EXACT_MODEL_LOCK_BYTES>
STATE_ROOT=<REMOTE_PATH>/dgx-spark/state
CACHE_ROOT=<REMOTE_PATH>/dgx-spark/cache
LOG_ROOT=<REMOTE_PATH>/dgx-spark/logs
RESULT_ROOT=<REMOTE_PATH>/dgx-spark/results
IMAGE_REF=<PRODUCTION_HEAD_IMAGE_DIGEST>
HEAD_IMAGE_REF=<PRODUCTION_HEAD_IMAGE_DIGEST>
WORKER_IMAGE_REF=<PRODUCTION_WORKER_IMAGE_DIGEST>
IMAGE_LOCK_FILE=<PATH>/production.lock.json
MASTER_ADDR=192.168.100.10
MASTER_PORT=29619
API_PORT=8101
HEAD_NODE_ADDR=192.168.100.10
WORKER_NODE_ADDR=192.168.100.11
HEAD_NET_IFACE=<HEAD_DATA_INTERFACE>
WORKER_NET_IFACE=<WORKER_DATA_INTERFACE>
HEAD_HCA=<HEAD_ROCE_HCA_LIST>
WORKER_HCA=<WORKER_ROCE_HCA_LIST>
HEAD_CUDA_VISIBLE_DEVICES=<HEAD_GPU_SET>
WORKER_CUDA_VISIBLE_DEVICES=<WORKER_GPU_SET>
ROCE_GID_INDEX=<REVIEWED_GID_INDEX>
ROCE_MTU=<REVIEWED_MTU>
API_BIND_ADDR=0.0.0.0
ALLOW_PUBLIC_API=1
FORWARD_LOCAL_PORT=<LOOPBACK_FORWARD_PORT>
```

Production fixes API port `8101` and rendezvous port `29619`. The public bind
must be explicitly opted into with `ALLOW_PUBLIC_API=1`; otherwise the parser
requires a loopback bind. Candidate mode always remains loopback-only.

Validate before reading or changing any node:

```bash
bin/dgx-deploy config validate --env-file <PATH>/dgx-spark-production/production.env
```

### 3. Prepare and verify the model

`model.lock.json` pins the public
`deepseek-ai/DeepSeek-V4-Flash-0731` tree, including its 48 weight shards and
metadata. It is not model data and must remain in the checkout. Compute the
value to copy into `MODEL_MANIFEST_SHA256` from the exact lock bytes:

```bash
sha256sum model.lock.json
python3 scripts/model/fetch.py <REMOTE_PATH>/models/DeepSeek-V4-Flash-0731 --dry-run
python3 scripts/model/fetch.py <REMOTE_PATH>/models/DeepSeek-V4-Flash-0731
python3 scripts/model/verify.py <REMOTE_PATH>/models/DeepSeek-V4-Flash-0731
```

The verifier writes or checks `<MODEL_ROOT>/.model-lock.sha256` containing the
SHA-256 of the exact lock file. The deployment engine also stages this exact
marker to both nodes before creating containers. It never guesses a model hash
or silently accepts a missing marker.

### 4. Prepare immutable production images

If you do not have a validated parent image yet, build one from the fork
sources with a single command — `scripts/image/build-fork.sh` clones the three
forks at the pinned commits of [`image.fork.lock.json`](image.fork.lock.json)
(production-verified byte-for-byte against the running service) and builds
the ~25 GB image with zero build-time patches. See
[`docs/image.md`](docs/image.md#build-from-fork-sources-one-command).

Use only validated production inputs. A failed candidate image or a candidate
port is never promoted by retagging. If a validated parent image lacks the
seven required labels, create a metadata-only child in a staging image store.
The child recipe must contain only `FROM` and `LABEL` instructions—no `COPY`,
`RUN`, package installation, or source changes:

```bash
mkdir -p <PATH>/image-labels
cat > <PATH>/image-labels/head.Containerfile <<'EOF'
FROM <HEAD_PARENT_IMAGE_REF>
LABEL org.opencontainers.image.revision="<FULL_SOURCE_COMMIT>"
LABEL com.dgx-spark.architecture="linux/arm64"
LABEL com.dgx-spark.profile_sha256="<PROFILE_SHA256>"
LABEL com.dgx-spark.service_contract_sha256="<HEAD_SERVICE_CONTRACT_SHA256>"
LABEL com.dgx-spark.image_lock_sha256="<IMAGE_LOCK_SHA256>"
LABEL com.dgx-spark.vllm.commit="<FULL_VLLM_COMMIT>"
LABEL com.dgx-spark.b12x.commit="<FULL_B12X_COMMIT>"
EOF

docker build --pull=false --platform linux/arm64 \
  --file <PATH>/image-labels/head.Containerfile \
  --tag production/dsv4-native432-dspark5-327k-seq5-head:labelled <PATH>/image-labels

docker image inspect <HEAD_PARENT_IMAGE_REF>
docker image inspect production/dsv4-native432-dspark5-327k-seq5-head:labelled
```

Repeat for the worker with its own service-contract hash and production
namespace:

```bash
cat > <PATH>/image-labels/worker.Containerfile <<'EOF'
FROM <WORKER_PARENT_IMAGE_REF>
LABEL org.opencontainers.image.revision="<FULL_SOURCE_COMMIT>"
LABEL com.dgx-spark.architecture="linux/arm64"
LABEL com.dgx-spark.profile_sha256="<PROFILE_SHA256>"
LABEL com.dgx-spark.service_contract_sha256="<WORKER_SERVICE_CONTRACT_SHA256>"
LABEL com.dgx-spark.image_lock_sha256="<IMAGE_LOCK_SHA256>"
LABEL com.dgx-spark.vllm.commit="<FULL_VLLM_COMMIT>"
LABEL com.dgx-spark.b12x.commit="<FULL_B12X_COMMIT>"
EOF

docker build --pull=false --platform linux/arm64 \
  --file <PATH>/image-labels/worker.Containerfile \
  --tag production/dsv4-native432-dspark5-327k-seq5-worker:labelled <PATH>/image-labels

docker image inspect <WORKER_PARENT_IMAGE_REF>
docker image inspect production/dsv4-native432-dspark5-327k-seq5-worker:labelled
```

Compare `RootFS.Layers` between each parent and child. A metadata-only child
must have identical rootfs layers. Record the child `Id` and immutable digest
reference in the external lock. A local tag is not an immutable lock value;
use `repository@sha256:<digest>` when a registry provides a RepoDigest, or the
exact `sha256:<image-id>` only when the local runtime has no repository digest.
Do not invent a digest for an unpushed tag.

The required label keys are exactly:

```text
org.opencontainers.image.revision
com.dgx-spark.architecture
com.dgx-spark.profile_sha256
com.dgx-spark.service_contract_sha256
com.dgx-spark.image_lock_sha256
com.dgx-spark.vllm.commit
com.dgx-spark.b12x.commit
```

The deploy verifier rejects aliases such as hyphenated profile/service keys,
missing labels, mutable tags, candidate-namespaced production references, and
architecture other than `linux/arm64`.

### 5. Create the external deployment lock

Write `<PATH>/dgx-spark-production/production.lock.json` using the exact child
IDs and labels just read back. The schema is
[`config/deployment-lock.schema.json`](config/deployment-lock.schema.json):

```json
{
  "schema_version": 1,
  "status": "ready",
  "mode": "production",
  "profile_id": "dsv4-native432-b12x-tp2",
  "model_manifest_sha256": "<SHA256_OF_EXACT_MODEL_LOCK_BYTES>",
  "lock_sha256": "<SHA256_OF_CANONICAL_LOCK_CONTENT_WITHOUT_LOCK_SHA256>",
  "images": {
    "head": {
      "reference": "<PRODUCTION_HEAD_REPOSITORY>@sha256:<HEAD_CHILD_DIGEST>",
      "image_id": "sha256:<HEAD_CHILD_ID>",
      "labels": {
        "org.opencontainers.image.revision": "<FULL_SOURCE_COMMIT>",
        "com.dgx-spark.architecture": "linux/arm64",
        "com.dgx-spark.profile_sha256": "<PROFILE_SHA256>",
        "com.dgx-spark.service_contract_sha256": "<HEAD_SERVICE_CONTRACT_SHA256>",
        "com.dgx-spark.image_lock_sha256": "<IMAGE_LOCK_SHA256>",
        "com.dgx-spark.vllm.commit": "<FULL_VLLM_COMMIT>",
        "com.dgx-spark.b12x.commit": "<FULL_B12X_COMMIT>"
      }
    },
    "worker": {
      "reference": "<PRODUCTION_WORKER_REPOSITORY>@sha256:<WORKER_CHILD_DIGEST>",
      "image_id": "sha256:<WORKER_CHILD_ID>",
      "labels": {
        "org.opencontainers.image.revision": "<FULL_SOURCE_COMMIT>",
        "com.dgx-spark.architecture": "linux/arm64",
        "com.dgx-spark.profile_sha256": "<PROFILE_SHA256>",
        "com.dgx-spark.service_contract_sha256": "<WORKER_SERVICE_CONTRACT_SHA256>",
        "com.dgx-spark.image_lock_sha256": "<IMAGE_LOCK_SHA256>",
        "com.dgx-spark.vllm.commit": "<FULL_VLLM_COMMIT>",
        "com.dgx-spark.b12x.commit": "<FULL_B12X_COMMIT>"
      }
    }
  }
}
```

Validate the lock without contacting a node:

```bash
python3 -c 'import json; from pathlib import Path; from jsonschema import Draft202012Validator; s=json.loads(Path("config/deployment-lock.schema.json").read_text()); l=json.loads(Path("<PATH>/dgx-spark-production/production.lock.json").read_text()); e=list(Draft202012Validator(s).iter_errors(l)); assert not e, e; print("lock valid")'
python3 -c 'import hashlib, json; from pathlib import Path; v=json.loads(Path("<PATH>/dgx-spark-production/production.lock.json").read_text()); v.pop("lock_sha256", None); print(hashlib.sha256(json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest())'
```

### 6. Render, plan, and dry-run

Rendering writes complete role contracts only to an explicitly requested
external directory. Planning prints a redacted plan. Neither operation
connects to a node or calls Docker:

```bash
bin/dgx-deploy render \
  --env-file <PATH>/dgx-spark-production/production.env \
  --lock-file <PATH>/dgx-spark-production/production.lock.json \
  --output-dir <PATH>/dgx-spark-production/contracts

bin/dgx-deploy plan \
  --env-file <PATH>/dgx-spark-production/production.env \
  --lock-file <PATH>/dgx-spark-production/production.lock.json

bin/dgx-deploy apply \
  --env-file <PATH>/dgx-spark-production/production.env \
  --lock-file <PATH>/dgx-spark-production/production.lock.json \
  --state-file <PATH>/dgx-spark-production/rollback.json \
  --dry-run
```

The rendered production plan must show API `8101`, master `29619`, model path
`/models/DeepSeek-V4-Flash-0731`, container names
`dsv4-native432-dspark5-327k-seq5-head` and `...-worker`, and worker-before-head
ordering. Inspect the generated `head.contract.json`, `worker.contract.json`,
`head.env`, `worker.env`, and redacted `plan.json` before continuing.

### 7. Apply through the repository

Use `update` when the exact production pair already exists; use `apply` only
for an empty target. Both paths capture rollback state before mutation. First
get the deployment ID from the redacted plan, then confirm that exact value:

```bash
bin/dgx-deploy plan \
  --env-file <PATH>/dgx-spark-production/production.env \
  --lock-file <PATH>/dgx-spark-production/production.lock.json

bin/dgx-deploy update \
  --env-file <PATH>/dgx-spark-production/production.env \
  --lock-file <PATH>/dgx-spark-production/production.lock.json \
  --state-file <PATH>/dgx-spark-production/rollback.json \
  --confirm <DEPLOYMENT_ID>
```

The command sequence is read-only preflight, rollback capture, contract/env and
model-marker staging, worker/head stop and removal only after ownership checks,
worker/head creation, worker start, head start, exact verification, and API
readiness. Any failed gate stops without reporting success; an update failure
attempts exact rollback from the captured state.

## External artifacts

A recommended operator directory is:

```text
<PATH>/dgx-spark-production/
  production.env              # site values; never commit
  production.lock.json        # status=ready; exact child IDs/labels
  contracts/
    head.contract.json         # generated executable contract
    worker.contract.json
    head.env
    worker.env
    plan.json                  # redacted plan
  rollback.json                # exact previous image/command/host state
  production.manifest.json    # optional redacted success manifest
  logs/                        # operator-owned command/API evidence
```

Only generic examples, schemas, and tests belong in the public checkout. Keep
SSH keys, known-hosts files, model data, image archives/IDs, remote host paths,
logs, and state in the operator directory or on the DGX nodes.

## Candidate workflow

Candidate validation is isolated and never promotes an image automatically.
Use a separate environment and ready lock:

```text
DEPLOYMENT_MODE=candidate
HEAD_HOST=192.168.100.10
WORKER_HOST=192.168.100.11
MASTER_ADDR=192.168.100.10
MASTER_PORT=29621
API_PORT=18101
API_BIND_ADDR=127.0.0.1
ALLOW_PUBLIC_API=0
HEAD_IMAGE_REF=<CANDIDATE_HEAD_DIGEST>
WORKER_IMAGE_REF=<CANDIDATE_WORKER_DIGEST>
IMAGE_LOCK_FILE=<PATH>/dgx-spark-candidate/candidate.lock.json
```

Candidate image references must be candidate-namespaced and candidate
containers must not overlap production names. Run the same
`config validate`, `render`, `plan`, and `apply --dry-run` sequence against the
candidate files. Candidate readiness and behavior gates must finish before a
new production lock is prepared. A failed candidate lock is never used for a
production command, and production `8101`/`29619` must not appear in candidate
validation scripts or plans.

```bash
bin/dgx-deploy apply \
  --env-file <PATH>/dgx-spark-candidate/candidate.env \
  --lock-file <PATH>/dgx-spark-candidate/candidate.lock.json \
  --state-file <PATH>/dgx-spark-candidate/rollback.json \
  --dry-run
```

## Verification and minimal smoke

For manual diagnostics, use the same strict options as the repository engine.
The aliases must already be configured in the operator's SSH config:

```bash
ssh_dgx() {
  ssh -T -o BatchMode=yes -o StrictHostKeyChecking=yes \
    -o UserKnownHostsFile=<PATH>/known_hosts -o ConnectTimeout=10 \
    -i <PATH>/ssh-key "$@"
}
scp_dgx() {
  scp -q -o BatchMode=yes -o StrictHostKeyChecking=yes \
    -o UserKnownHostsFile=<PATH>/known_hosts -o ConnectTimeout=10 \
    -i <PATH>/ssh-key "$@"
}
```

Repository verification checks exact image IDs, all required labels, ownership,
command vectors, environment, exact model mount, model files/marker, GPU/RDMA
and interface preflights, then `/health` and `/v1/models` readiness:

```bash
bin/dgx-deploy verify \
  --env-file <PATH>/dgx-spark-production/production.env \
  --lock-file <PATH>/dgx-spark-production/production.lock.json

ssh_dgx DGX-SPARK-0 "curl --fail --silent --show-error http://127.0.0.1:8101/health"
ssh_dgx DGX-SPARK-0 "curl --fail --silent --show-error http://127.0.0.1:8101/v1/models"
```

For a minimal inference smoke, use a bounded request and an approved model ID:

```bash
cat > <PATH>/smoke.json <<'EOF'
{"model":"deepseek-v4-flash-0731-native432","messages":[{"role":"user","content":"Reply with OK."}],"max_tokens":1,"temperature":0}
EOF
scp_dgx <PATH>/smoke.json DGX-SPARK-0:<REMOTE_PATH>/smoke.json
ssh_dgx DGX-SPARK-0 "curl --fail --silent --show-error --max-time 180 -H 'Content-Type: application/json' --data-binary @<REMOTE_PATH>/smoke.json http://127.0.0.1:8101/v1/chat/completions"
ssh_dgx DGX-SPARK-0 "rm -f <REMOTE_PATH>/smoke.json"
```

Record response status, served model ID, prompt/completion token counts, and
finish reason. HTTP success does not replace exact contract verification.

## Rollback

`apply` and `update` write rollback state before stopping a container. The state
contains the previous image ID, command, labels, environment, mounts, host
network/GPU/security settings, and running state for both roles. It must stay
outside the checkout:

```bash
bin/dgx-deploy rollback \
  --env-file <PATH>/dgx-spark-production/production.env \
  --lock-file <PATH>/dgx-spark-production/production.lock.json \
  --state-file <PATH>/dgx-spark-production/rollback.json \
  --confirm <DEPLOYMENT_ID>
```

Rollback proves the previous images exist, stops only the exact current pair,
recreates the captured image/command/labels/host settings, starts worker before
head, and reruns exact verification and readiness. A missing or tampered state
file fails closed. If an operation fails after partial mutation, inspect the
state file and run the same rollback command after the engine reports the
failure; never remove a container by PID or an unowned name.

## Troubleshooting

| Symptom | Safe diagnosis and correction |
| --- | --- |
| `deployment lock status must be ready` | Complete image metadata and labels in the external lock. Do not edit the committed pending lock. |
| `reference must be immutable` | Replace a tag with a registry RepoDigest or exact image ID. Never use `latest` or a candidate tag for production. |
| Required label mismatch | Reinspect the image. If five provenance labels are absent, build a metadata-only child, verify identical `RootFS.Layers`, then update the lock with the child ID. |
| `cannot read deployment lock` | Check `--lock-file` points to an operator-owned JSON file outside the checkout and validate it against `config/deployment-lock.schema.json`. |
| `model manifest marker does not match` | Compute `sha256sum model.lock.json`, write that exact lowercase digest to `<MODEL_ROOT>/.model-lock.sha256`, and rerun model verification. |
| SSH host cannot resolve | Configure the local aliases named by `HEAD_SSH_HOST` and `WORKER_SSH_HOST`; keep `HEAD_HOST`/`WORKER_HOST` as the reviewed node IPs. |
| SSH host-key or identity failure | Use an explicit known-hosts file and identity path. Do not disable host-key checking or put keys in this checkout. |
| `port does not match mode isolation` | Production is `8101`/`29619`; candidate is `18101`/`29621`. Fix the external env, not the profile. |
| `refusing to mutate unowned container` | Stop and inspect labels/name/image. The engine will not touch an unrelated same-name container. |
| Worker exits during readiness | Use `verify` and inspect logs on the node. Check GPU exposure, `ibdev2netdev -v`, RoCE interface/HCA, cache mounts, model marker, and the exact rendered command. |
| Head API is unreachable | Check the explicit bind policy, API port, SSH forwarding, and `/v1/models`. Candidate must stay loopback; production public binding requires `ALLOW_PUBLIC_API=1`. |
| Rollback state is incomplete | Do not guess an image or command. Restore from the last complete external state or stop and obtain a fresh reviewed lock. |

## Fixed reviewed contract

The profile fixes two ARM64 GB10 nodes with TP2, worker rank 1 first, B12X MLA
attention, native DSV4 NVFP4 432-byte records, DSpark K5 with five speculative
tokens and probabilistic draft sampling, SHA-256 prefix hashing, `block_size`
256, max model length 327680, max sequences 5, batch budget 1024, async off,
chunked prefill on, threshold 0, `mp`, GPU utilization 0.85, instanttensor,
b12x linear/MoE, reasoning defaults, and CUDA Graph capture 64 with
`custom_ops=["all"]`. The model path is
`/models/DeepSeek-V4-Flash-0731` on a read-only mount.

The generated contracts also carry explicit RDMA/GPU environment, model/cache/
contract mounts, exact OCI label expectations, and worker-before-head lifecycle
ordering. `plan` is redacted; generated contract files are operator artifacts.
Review them before mutation.
