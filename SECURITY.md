# Security boundary

This public repository contains reviewed schemas, immutable profile data,
deterministic renderers, lock validation, and the repository-managed lifecycle
engine. It contains no operator credentials, model weights, image archives,
private host inventory, or live deployment state.

## Never commit

- SSH private keys, host-key databases, passwords, bearer tokens, cookies,
  registry credentials, or authentication/TLS material.
- Personal workstation paths, user names, private hostnames, private address
  assignments, interface/HCA inventories, GPU captures, or desktop automation.
- Model weights, tokenizer files, prompts, responses, completions, activation
  dumps, caches, profiler traces, Docker inspection output, container IDs, or
  process logs.
- Image archives, mutable deployment tags, copied upstream source trees, or
  generated binaries.

Keep operator environment files, ready locks, generated contracts, rollback
state, caches, logs, and result bundles outside the Git checkout. The
`.gitignore` policy is a guardrail; review every file before staging it.

## Safe operation

The Python package parses environment files without sourcing shell, constructs
argv vectors, redacts operator values in plans, and validates the immutable
profile before any remote operation. `render`, `plan`, and `--dry-run` are
local-only. They do not call SSH, SCP, Docker, model clients, or host mutation.

The mutating commands are `apply`, `update`, `start`, `stop`, and `rollback`.
Each requires a complete external `status: "ready"` deployment lock with a
valid canonical `lock_sha256` and `--confirm` equal to the exact deployment ID
emitted by `plan`. `verify` is read-only but still requires the ready lock for
production/candidate modes. Legacy hidden `--apply` options on config/plan are
rejected; there is no implicit mutation switch.

Before mutation the engine checks mode port isolation, immutable per-role image
IDs/references, exact OCI labels, ARM64 architecture, model lock marker and
required model files, Docker/GPU/RDMA/interface state, and owned container
names/labels. It captures exact rollback state before an update and protects it
with a canonical integrity hash. It starts worker before head. A failed update
attempts exact rollback; an empty-target partial apply removes only containers
carrying the current ownership labels. Full restored-state verification covers
image, command, labels, environment, mounts, host settings, and running state.

Production `0.0.0.0` API binding is not a general default: it requires the
explicit `ALLOW_PUBLIC_API=1` configuration gate. Candidate mode is loopback
only and uses API `18101`/master `29621`; production uses API `8101`/master
`29619`. Reviewed production/candidate container security and GPU settings are
fixed by the renderer and are not operator shell overrides.

Never disable host-key checking, substitute mutable tags, guess image/model
hashes, bypass label or ownership checks, kill a process by PID, or use an
unreviewed privileged container. Do not run live lifecycle commands against
production until the redacted plan, dry-run, lock, and maintenance approval
have been independently reviewed.

## Reporting

Report only a redacted summary containing an experiment/deployment identifier,
source and image identities, error class, and checksums of private evidence
held outside Git. Do not paste prompts, responses, environment dumps, inspect
output, credentials, or operator paths into issues or pull requests. Rotate any
credential that may have entered a working directory.
