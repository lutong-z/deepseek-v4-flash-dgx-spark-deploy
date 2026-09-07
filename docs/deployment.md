# Deployment operations

This repository manages a two-node ARM64 GB10 DGX Spark service. The worker is
rank 1 and starts before the head at rank 0. `render` and `plan` are local-only;
`apply`, `update`, `start`, `stop`, `rollback`, and `verify` use strict SSH
argvs and never source the operator environment as shell.

## Contract and modes

The immutable profile fixes:

- model directory `<MODEL_ROOT>/DeepSeek-V4-Flash-0731` and container path
  `/models/DeepSeek-V4-Flash-0731`, with the same model path in vLLM;
- served name `deepseek-v4-flash-0731-native432`;
- TP2/PP1 over two nodes with distributed executor `mp`;
- `block_size=256`, max model length `327680`, max sequences `5`, and batch
  budget `1024`;
- DSpark K5 (`num_speculative_tokens=5`, probabilistic draft sampling),
  B12X MLA sparse attention, b12x MoE/linear backends, and NVFP4 DS MLA KV;
- prefix caching enabled with SHA-256, async scheduling disabled, chunked
  prefill enabled, and `long_prefill_token_threshold=0`; and
- CUDA Graph `FULL_AND_PIECEWISE`, capture size `64`, and
  `custom_ops=["all"]`.

Production mode is fixed to head/node `192.168.100.10`, worker/node
`192.168.100.11`, API `8101`, and rendezvous `29619`. Candidate mode is fixed
to API `18101` and rendezvous `29621`; it must use candidate-namespaced images
and isolated remote/cache/state/result/log roots. Candidate mode cannot overlap
the production pair. A production public bind requires explicit
`ALLOW_PUBLIC_API=1`; candidate mode always requires loopback.

## External artifact set

Keep this entire set outside the checkout:

```text
<PATH>/dgx-spark-production/
  production.env
  production.lock.json
  contracts/
    head.contract.json
    worker.contract.json
    head.env
    worker.env
    plan.json
  rollback.json
  production.manifest.json
  logs/
```

`production.env` supplies site addresses, SSH aliases/users, external roots,
model digest, mode ports, and role image refs. `production.lock.json` supplies
`status=ready`, a canonical-content `lock_sha256`, exact per-role image
IDs/references, and seven OCI labels.
Generated contracts contain executable argv vectors, runtime environment,
exact model/cache/contract mounts, labels, and verification identity.
`rollback.json` is written before an apply/update and records exact prior image,
command, labels, environment, mounts, host settings, and running state. A
success manifest is optional but should contain only redacted identities and
gate results.

## Configuration and plan separation

```bash
bin/dgx-deploy config validate --env-file <PATH>/dgx-spark-production/production.env
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

`config validate` checks immutable profile/config invariants. `render` writes
complete role files only under the requested external directory. `plan` prints
redacted actions and command vectors. `apply --dry-run` validates the lock and
prints the same plan without SSH, SCP, Docker, or model mutation. No command
should be promoted to a mutating run until the redacted plan is reviewed.

## Lifecycle order

Use `apply` for an empty target and `update` for an existing owned pair:

```bash
bin/dgx-deploy update \
  --env-file <PATH>/dgx-spark-production/production.env \
  --lock-file <PATH>/dgx-spark-production/production.lock.json \
  --state-file <PATH>/dgx-spark-production/rollback.json \
  --confirm <DEPLOYMENT_ID>
```

The engine performs these ordered gates:

1. Validate mode ports, immutable role image locks, labels, profile, and model
   digest.
2. On both nodes, check Docker/ARM64, GPU visibility, RDMA links, interface,
   HCA mapping, image digest/labels, the exact model directory, all required
   model files, and `.model-lock.sha256`.
3. Capture the currently-owned pair before mutation. Existing production A
   containers may be adopted only when their exact names, owner/role labels,
   and supplied A image IDs match.
4. Stop worker then head, and remove only containers carrying the exact
   deployment/role ownership identity. For a partial pair, missing roles are
   treated as absent; unexpected names/images/commands fail closed.
5. Create external remote contract/cache directories and stage both generated
   contracts, role env files, and the exact model marker.
6. Create worker then head with host networking, host IPC, GPU exposure,
   reviewed security settings, read-only model/contract mounts, and direct
   service argv. Start worker then head.
7. Verify exact image IDs, command vectors, environment, labels, mounts,
   running state, model files/marker, `/health`, and `/v1/models` readiness.

A missing, mutable, mismatched, or unowned prerequisite fails closed. An
update failure attempts exact rollback from the integrity-checked captured
state. An empty-target apply failure removes only a partial pair carrying the
current deployment/role labels; if cleanup itself cannot prove ownership, it
stops and reports the unresolved state for an operator to inspect. The command
never kills a process by PID or touches a same-name container without ownership
proof.

## Layered prefix-cache candidate

The isolated rollout under
`artifacts/native432-lmcache-rollout-20260904/` adds two bounded cache tiers:

- local GPU prefix-cache entries have a 300-second idle TTL. This is an upper
  bound, not a pin: capacity pressure may evict them sooner;
- each TP rank writes its native432 shard directly to a local filesystem L2;
  L2 uses access-refreshed mtime LRU, a 100 GiB high watermark, an 80 GiB trim
  target, and a two-hour idle TTL;
- LMCache L1 is a 1 GiB transient transfer tier. Completed stores are removed
  from L1, and L2 restores are released after the waiting request consumes them.

Build the role-specific derived images with `image/build.sh head` on rank 0 and
`image/build.sh worker` on rank 1. The builder verifies the exact base image and
patch digest before writing the derived tag. The create scripts then consume
the derived images by immutable image ID and pass
`--prefix-cache-idle-timeout-seconds 300`.

The engine allocator fraction is `0.85`; do not raise it further without a
measured transfer-stress run. On the production 121.69 GiB unified-memory
nodes, each percentage point consumes about 1.22 GiB. The pre-change
`0.84` worker profile reported 7.63 GiB of KV cache and a safe fixed-cache
ceiling of 9.13 GiB after CUDA Graph capture, so the 1.22 GiB increase remains
inside the measured 1.50 GiB headroom. LMCache retains its required 1 GiB L1
transfer tier; every L2 store and restore stages native432 objects through it.

The engine commands use `max_num_batched_tokens=1024` with
`max_num_seqs=8`. The smaller global prefill step protects resident session
prefixes and decode latency; it intentionally trades away some single-request
cold-prefill throughput.

The cache server and engine form one failure domain. Stop the complete TP pair
before creating or replacing either LMCache server. Start and verify both
LMCache servers before creating worker then head. Never restart an empty cache
server under a live engine: existing connectors retain stale GPU contexts and
can wait indefinitely. The server script refuses this state even when
`LMCACHE_FORCE_REPLACE=1`.

## Incident-fix image layer (2026-09-05)

`image/incident-fix-20260905/` derives `native432-lmcache:{head,worker}-incident-v1`
from the TTL images and is the layer the create scripts consume. Its overlay
was rebased onto the production image files (the scheduler-fork tree is
behind production: it lacks the `zip_longest` partial-layer fix and the
DSpark unaligned-prefix repair; full-file overlays from the tree must not be
shipped without this rebase). The layer contains:

- hybrid (multi-group) KV invalid-block recovery: an external-load failure
  now truncates each request at the earliest invalid position across KV
  groups instead of crashing the engine on the single-group unpack
  (`_update_requests_with_invalid_blocks`);
- transient external KV load retry: with the `recompute` failure policy, a
  failed async load retries with a fresh LOOKUP/RETRIEVE cycle
  (`VLLM_KV_LOAD_MAX_RETRIES=2`, `VLLM_KV_LOAD_RETRY_BACKOFF_SECONDS=1`)
  before falling back to local recompute, when the connector accepts
  `reset_load_state()`;
- session-aware eviction protection: blocks freed by session-tagged
  requests skip eviction victim selection for
  `VLLM_SESSION_BLOCK_PROTECT_SECONDS=600` (allocation still falls back to
  protected blocks when nothing else is free);
- LMCache heartbeat hardening: the PING timeout is decoupled from the ping
  interval (`lmcache.mp.heartbeat_timeout`, default 10s) so a slow restore
  occupying the server control plane no longer trips a 1-second heartbeat
  into degraded mode, and heartbeat failures log their reason.

The 2026-09-05 crash (single-group unpack at
`_update_requests_with_invalid_blocks` after a heartbeat misjudgment during
a 220K-token restore) is covered by the first and last items.

## Streaming-restore server layer (2026-09-05)

`image/streaming-restore-20260905/` derives
`native432-lmcache:{head,worker}-server-streaming-v1` from the TTL images;
`run-server.sh` consumes it. The layer bounds the L1 footprint of large
restores so a 100K+ prefix restore streams through the 1 GiB L1 instead of
failing atomically:

- L1 write reservations run in prefix-ordered windows
  (`--l2-prefetch-l1-reserve-batch-keys`, default 32). Out-of-memory
  degrades to the longest reservable prefix instead of the old
  all-or-nothing abort; contention (a key write-locked by a concurrent
  request) still aborts the whole load by design.
- Retrieve streams H2D per chunk window
  (`--l2-retrieve-window-chunks`, default 16), releasing each window's L1
  read locks in stream order, so retrieve never holds the whole prefix in
  L1 at once.

Verified on production (2026-09-05): a 100,001-token prompt restored
99,840 tokens in 0.72–0.81s on replay, including with four concurrent
20K-token warm-session requests; warm hits stayed at 19,968/20,110 with
zero server or engine errors. The earlier all-or-nothing failure ("Failed
to batched allocate 472 memory blocks") cannot degrade a large restore
into a full recompute anymore.

Filesystem objects are published by atomic rename. The lifecycle process never
scans `.tmp` while the server is live; it removes orphaned temp files before a
new server starts. A root ownership marker prevents cleanup outside the
dedicated cache directory. Prometheus scrapes lifecycle metrics on port 19120,
LMCache metrics on 19121, and vLLM exposes local expiry through
`vllm:prefix_cache_expired_blocks_total`.

## SSH aliases

`HEAD_HOST` and `WORKER_HOST` are the reviewed node IPs used in service/NCCL
configuration. `HEAD_SSH_HOST` and `WORKER_SSH_HOST` may be local SSH aliases,
for example `DGX-SPARK-0` and `DGX-SPARK-1`. The local SSH config must resolve
them and provide the expected user, key, and known-host entry. The CLI adds
strict host-key checking, batch mode, explicit known-hosts, identity, and a
connect timeout. It does not accept `StrictHostKeyChecking=no`.

For manual diagnostics, use the same safe wrapper rather than raw SSH defaults:

```bash
ssh_dgx() {
  ssh -T -o BatchMode=yes -o StrictHostKeyChecking=yes \
    -o UserKnownHostsFile=<PATH>/known_hosts -o ConnectTimeout=10 \
    -i <PATH>/ssh-key "$@"
}
```

## No private paths in public files

The public repository must not contain site hostnames, SSH users/keys,
registry credentials, model data, image IDs, image archives, private source
paths, logs, or local evidence. Use `<PATH>`, `<REMOTE_PATH>`, `<REMOTE_SSH_USER>`,
`<PRODUCTION_*_DIGEST>`, and `<FULL_*_COMMIT>` placeholders in copied examples.
Operator env/lock/contract/state files remain external and are never committed.
