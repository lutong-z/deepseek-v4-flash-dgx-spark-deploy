# Deployment contract

The repository is the executable path for a two-node ARM64 DGX Spark service.
The worker (rank 1) is started before the head (rank 0). Rendering and planning
are local-only; lifecycle commands use strict tokenized SSH and never source an
operator shell file.

## Configuration flow

1. Copy `.env.example` to an operator-owned location outside this checkout.
2. Fill explicit SSH/RDMA values, model manifest hash, external roots, and
   immutable per-role image references.
3. Select `DEPLOYMENT_MODE=production` for API `8101`/master `29619`, or
   `DEPLOYMENT_MODE=candidate` for isolated API `18101`/master `29621`.
4. Mark a complete site-owned deployment lock `status=ready`; the committed
   `image.lock.json` remains `pending-artifacts` and cannot be used for
   lifecycle operations.
5. Run `bin/dgx-deploy config validate --env-file PATH`.
6. Run `bin/dgx-deploy render --env-file PATH --lock-file LOCK --output-dir DIR`
   and inspect `bin/dgx-deploy plan --env-file PATH --lock-file LOCK`.
7. Run `bin/dgx-deploy apply ... --dry-run`; only after review run
   `bin/dgx-deploy apply ... --confirm deployment-<id>`.

`render`, `plan`, and every `--dry-run` path perform no SSH, Docker, SCP, or
filesystem mutation beyond an explicitly requested render output directory.
`start`, `stop`, `update`, `rollback`, and `apply` require the exact rendered
deployment ID. `verify` checks image IDs, labels, command vectors, environment,
mounts, model marker, GPU/RDMA/network preflights, and `/health` plus
`/v1/models` readiness.

The parser accepts only comments, blank lines, and unique `KEY=VALUE` records.
It rejects unknown keys, shell syntax, control characters, unresolved values,
mutable image tags, path traversal, checkout-local roots, equal hosts, unsafe
SSH values, candidate/production port collisions, and public API binding unless
production explicitly sets `ALLOW_PUBLIC_API=1`. Candidate mode always uses a
loopback API bind.

## Reviewed service profile

The profile fixes TP2 over two GB10 nodes; B12X MLA attention; native DSV4
NVFP4 432-byte records; DSpark K5 with five speculative tokens and
`draft_sample_method=probabilistic`; SHA-256 prefix hashing; `block_size=256`;
maximum model length `327680`, sequences `5`, batch `1024`; asynchronous
scheduling off; chunked prefill on; `long_prefill_token_threshold=0`; `mp`;
GPU utilization `0.85`; `instanttensor`; b12x linear/MoE; reasoning defaults;
and CUDA Graph capture `64` with `custom_ops=["all"]`. The reviewed model path
is `/models/DeepSeek-V4-Flash-0731` on a read-only mount.


## Layered prefix-cache lifecycle

The production cache design uses independent, pressure-safe tiers:

- local GPU prefix-cache blocks have a 300-second idle TTL configured with
  `--prefix-cache-idle-timeout-seconds 300`; this is an upper bound, not a pin,
  because allocator pressure may evict free cached blocks sooner;
- LMCache L1 is a 1 GiB transient transfer tier. Completed stores are removed
  after reaching L2, restores are released after the waiting request consumes
  them, and LRU pressure may reclaim them earlier; and
- each TP rank writes to its own persistent filesystem L2 root, bounded to
  100 GiB with a 7,200-second idle TTL and an 83 percent high-water trim target.

Run `python3 -m dgx_deploy.lmcache_lifecycle` beside each LMCache server to
enforce L2 TTL and size limits. Files are published by atomic rename, `.tmp`
is excluded from live scans, and a root ownership marker prevents cleanup
outside the dedicated cache directory. Run the LMCache server through
`python3 -m dgx_deploy.lmcache_server` so successful lookups refresh object
access time and the lifecycle process implements LRU rather than FIFO.

Prometheus scrapes lifecycle metrics on port `19120`, LMCache metrics on
`19121`, and vLLM reports local expiry through
`vllm:prefix_cache_expired_blocks_total`. Stop the complete TP service before
creating or replacing either cache server. A server restarted under a live
engine has a new GPU context while the connector retains stale state and can
wait indefinitely.

`session_id` is request metadata; it does not reserve or pin prefix-cache
blocks. Cache identity remains token-hash based, so concurrent sessions reuse
an identical prefix without a session-specific cache key.

Exact image IDs, immutable references, source labels, model hash, and service
contract hash come from the complete external lock. No value is inferred from
logs or local state.

## Lifecycle and recovery

`apply` captures the currently-owned head/worker image, command, labels,
environment, and running state before replacement. `update` stops/removes only
containers carrying the matching ownership labels, stages both role contracts,
creates both containers, and starts worker before head. Any failed gate stops
without reporting success. `rollback` requires a complete state file, proves
the old image is available, and restores the exact captured command/image/labels
in worker-before-head order. Unowned containers are never touched.
