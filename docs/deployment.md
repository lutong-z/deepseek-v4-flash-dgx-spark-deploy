# Deployment contract

This repository describes a two-node ARM64 DGX Spark service with rank 1
(worker) started before rank 0 (head). The profile fixes service invariants;
the operator env file supplies only site values such as explicit addresses,
external roots, and immutable image/model identities.

The current scaffold supports local parsing, validation, hashing, redacted
rendering, and dry-run planning. It does not connect to nodes or mutate a
container, image, network, or production service. Lifecycle scripts fail
closed until the reviewed source/build/service contract is available.

## Configuration flow

1. Copy `.env.example` to an operator-owned location outside this checkout.
2. Fill explicit SSH and RoCE addresses, users, ports, model manifest hash,
   external roots, and an immutable image reference.
3. Run `bin/dgx-deploy config validate --env-file PATH`.
4. Inspect `bin/dgx-deploy plan --env-file PATH`; it emits redacted argv vectors.
5. Keep generated manifests and results under external state/result roots.

The renderer never sources shell. It accepts only comments, blank lines, and
unique `KEY=VALUE` records, rejecting shell syntax, control characters,
unknown keys, duplicates, unresolved values, path traversal, mutable image
tags, port collisions, equal node addresses, and non-loopback API binding.

## Fixed profile

The committed profile fixes TP2 over two GB10 nodes, B12X target attention,
native DSV4 NVFP4 432-byte records, FP8 DSpark K5 draft state, SHA-256 prefix
hashing, asynchronous scheduling off, CUDA Graph mode, parser/tokenizer
choices, and service limits. Operators cannot override these values through
the env file.

Exact runtime source revisions, image labels, dependency hashes, model
manifest, and service-contract hash are separate reviewed build inputs. Do not
reconstruct them from logs or local state.

## Mutation gate

Any future mutating command must render and validate the config, acquire an
external lock, verify strict host keys, prove image/model/network preflights,
and print a redacted plan. Mutation then requires both `--apply` and
`--confirm DEPLOYMENT_ID`. The current CLI rejects both flags because no
mutation implementation is present.
