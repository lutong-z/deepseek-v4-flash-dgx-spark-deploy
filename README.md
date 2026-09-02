# Locked two-node DGX Spark deployment

This repository is an executable deployment path for the reviewed DeepSeek V4
two-node contract. Rendering and planning are local-only and redacted by
default. Host/container mutations require a complete external deployment lock
and an explicit confirmation of the rendered deployment ID.

The committed `image.lock.json` remains `pending-artifacts`; it is intentionally
not deployable. A site-owned lock must be marked `ready` and contain immutable
per-role image references, image IDs, and all required OCI labels before
`apply`, `start`, `stop`, `update`, `rollback`, or `verify` can operate.

## Safe workflow

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
python -m dgx_deploy.cli --help
python -m dgx_deploy.cli config validate --env-file /path/to/operator.env
python -m dgx_deploy.cli render --env-file /path/to/operator.env --lock-file /path/to/ready-lock.json --output-dir /path/to/rendered
python -m dgx_deploy.cli plan --env-file /path/to/operator.env --lock-file /path/to/ready-lock.json
python -m dgx_deploy.cli apply --env-file /path/to/operator.env --lock-file /path/to/ready-lock.json --dry-run
python -m dgx_deploy.cli apply --env-file /path/to/operator.env --lock-file /path/to/ready-lock.json --confirm deployment-<id>
```

`apply --dry-run` and `plan` never connect to a node. `apply`, `start`, `stop`,
`update`, and `rollback` reject missing or incorrect `--confirm`; no live
production command is run by tests. `DEPLOYMENT_MODE=production` fixes
`192.168.100.10:8101` and master `29619`; `DEPLOYMENT_MODE=candidate` fixes
isolated API `18101` and master `29621`. Both modes use the same explicit
head/worker addresses but different locked images and container namespaces.
Production access matches the validated service bind (`0.0.0.0:8101`) only
when the operator explicitly sets `ALLOW_PUBLIC_API=1`; candidate mode and
all other configurations remain loopback-only.

The environment file is never sourced as shell. It accepts only comments,
blank lines, and unique `KEY=VALUE` records. Keep it and all roots, SSH keys,
known-hosts files, lock files, model data, and state outside this checkout.
Unknown keys, shell syntax, mutable image tags, path traversal, checkout-local
roots, unresolved values, and mode port collisions fail closed.

## Pinned model lock

`model.lock.json` pins the public `deepseek-ai/DeepSeek-V4-Flash-0731`
repository to commit
`7872f01b1d1fe23eabc4c98b48bffcef5a386062` under the MIT license. The lock
contains the public Hugging Face tree metadata for the 48 safetensors shards,
their sizes and LFS SHA-256 values, plus Git blob SHA-1 values for metadata
files whose public tree records do not expose SHA-256 values. It also fixes
the official `generation_config.json` defaults (`do_sample: true`,
`temperature: 1.0`, `top_p: 1.0`) and records that no Jinja chat template is
present.

The model is approximately 166.9 GB and is intentionally **not** included in
this repository. No fetch is performed by tests or by importing the scripts.
See [`docs/model.md`](docs/model.md) for the lock, fetch, and verification
contract.

## Fixed service profile

`config/profiles/dsv4-native432-b12x-tp2.json` is an immutable contract for:

- two ARM64 DGX Spark GB10 nodes, tensor parallel size 2, worker rank 1 first;
- B12X MLA target attention with native DSV4 NVFP4 432-byte records;
- FP8 DSpark K5 draft cache (`draft_sample_method=probabilistic`) and SHA-256 prefix hashing;
- maximum model length 327,680, maximum sequences 5, batch budget 1,024, and
  `block_size=256`;
- asynchronous scheduling disabled, chunked prefill enabled, and
  `long_prefill_token_threshold=0`;
- `mp` distributed execution, GPU utilization `0.85`, `instanttensor` load
  format, b12x linear/MoE backends, reasoning defaults, and CUDA Graph capture
  size `64` with `custom_ops=["all"]`;
- model path `/models/DeepSeek-V4-Flash-0731` on a read-only model mount;
- external, identity-namespaced cache/state roots and strict RDMA/GPU preflights.

The profile and service-contract template are reviewed inputs. A complete
site-owned deployment lock supplies exact per-role image references, image IDs,
and canonical OCI labels. The pending committed image lock cannot pass the
promotion gate and is never implicitly used for mutation.

## Repository map

- `bin/`: stable operator entry point.
- `config/`: deployment and immutable service/image schemas and profile.
- `container/`: build contract inputs; no image layer or credential is stored.
- `dgx_deploy/`: parsing, validation, hashes, redaction, and argv rendering.
- `scripts/`: lock-gated model/image/network preflight and lifecycle entry
  points; no script falls back to arbitrary shell commands.
- `validation/`: synthetic redacted gate definitions only.
- `tests/`: deterministic local contract tests and a dry-run smoke script.
- `docs/`: deployment, image, networking, model, and rollback contracts.

## Public release boundary

Only generic source, schemas, deterministic tests, and reviewed documentation
belong in a public repository. Exclude environment files, SSH keys and host
records, model/tokenizer/weight data, image archives and IDs, logs, prompts,
responses, metrics, profiler captures, local state, and Mac launch-control or
OMP automation. Review upstream licenses and patch attribution before adding
runtime source or build inputs.
