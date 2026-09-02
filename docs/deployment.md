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
