# Generic two-node DGX Spark deployment

This is a clean, local-only scaffold for a generic two-node ARM64 DGX Spark
service. It contains a validated service profile, strict configuration
parsing, deterministic command rendering, redaction helpers, and fail-closed
operation boundaries. It does not contain model data, image layers, host
inventories, SSH configuration, evidence, or operator automation.

No public remote is configured by this checkpoint. No command in the scaffold
connects to a host, changes networking, pulls or pushes an image, starts or
stops a container, or changes production state. Mutating CLI flags are rejected
until the reviewed source/build/service contract is supplied and the lifecycle
implementation passes its separate gates.

## Safe start

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
python -m dgx_deploy.cli --help
python -m dgx_deploy.cli config render --env-file /path/to/operator.env
python -m dgx_deploy.cli plan --env-file /path/to/operator.env
```

The environment file is never sourced as shell. It accepts only comments,
blank lines, and unique `KEY=VALUE` records. Keep the file outside this
checkout. The renderer rejects unknown keys, duplicate keys, shell syntax,
control characters, unresolved values, mutable image tags, path traversal,
port collisions, and public API binding by default.

Use `.env.example` as a key reference only. It contains no site hosts,
credentials, image IDs, model files, or private addresses. Fill an operator
copy with explicit values for the control plane, RoCE data plane, model
manifest, immutable image, and external state roots.

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

- two ARM64 DGX Spark GB10 nodes, tensor parallel size 2, rank 1 first;
- B12X MLA target attention with native DSV4 NVFP4 432-byte records;
- FP8 DSpark K5 draft cache and SHA-256 prefix hashing;
- maximum model length 327,680, maximum sequences 5, and batch budget 1,024;
- asynchronous scheduling disabled and CUDA Graph mode fixed by the profile;
- read-only model mount and external, identity-namespaced cache/state roots;
- DeepSeek V4 tokenizer, reasoning parser, and tool parser family names;
  chat-template wiring remains build-gated.

The profile does not prove that a runtime image implements the contract. The
provisional [`image.lock.json`](image.lock.json) records the public vLLM and
B12X release coordinates and the native432 service fields, but deliberately
leaves all build and image artifact hashes as `null` until the isolated
runtime-image build report is reviewed. It contains no private paths, logs,
archives, host records, or local evidence.

The first scaffold intentionally leaves image-contract details release-gated:
chat-template options, load format, linear backend, CUDA Graph capture sizing,
compact block stride, source and dependency revisions, and image-specific
tuning must come from the reviewed build lock. They are not inferred from
local logs or silently supplied as operator overrides. A lock with
`pending-artifacts` status is not a deployable image and must not be loaded or
published.

## Repository map

- `bin/`: stable operator entry point.
- `config/`: deployment and immutable service/image schemas and profile.
- `container/`: build contract inputs; no image layer or credential is stored.
- `dgx_deploy/`: parsing, validation, hashes, redaction, and argv rendering.
- `scripts/`: lock-gated model fetch/verification plus explicitly disabled
  deployment lifecycle entry points; no script falls back to arbitrary shell
  commands.
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
