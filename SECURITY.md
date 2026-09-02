# Security boundary

This repository is designed for review and dry-run planning. It has no public
remote in the current checkpoint and contains no operator credentials, host
inventories, model files, image layers, runtime state, or raw validation
output.

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

Keep operator environment files, state, caches, logs, and result bundles
outside the Git checkout. The `.gitignore` policy is a guardrail; review every
file before staging it.

## Safe operation

The Python package constructs argument arrays and validates values before any
future subprocess or SSH integration. The current CLI supports local config
rendering, validation, and redacted dry-run planning only. `--apply` and
`--confirm` are rejected until a separate implementation review completes.
The shell entry points fail closed and never invoke arbitrary command text.

When mutation is eventually implemented, require both an explicit apply flag
and a deployment confirmation identifier after configuration, image, model,
network, and host-key preflights. Match immutable image/profile/model hashes,
role labels, and externally held state before each mutation. Never default to
privileged containers, mutable image tags, guessed process IDs, broad network
binds, or hidden background services.

## Reporting

Report only a redacted summary containing an experiment identifier, source and
image identities, error class, and checksums of private evidence held outside
Git. Do not paste prompts, responses, environment dumps, inspect output, or
operator paths into issues or pull requests. Rotate any credential that may
have entered a working directory.
