# Release image preparation

The deployment lock is separate from the public `image.lock.json`. The public
lock records reviewed source coordinates and remains `pending-artifacts` until
all build metadata and behavioral evidence are independently complete. A site
must create an external `status: "ready"` deployment lock before any image can
be used by `dgx-deploy` lifecycle commands.

## Build from fork sources (one command)

The reviewed way to produce the ~25 GB service image is
[`scripts/image/build-fork.sh`](../scripts/image/build-fork.sh), driven by the
committed coordinates in [`image.fork.lock.json`](../image.fork.lock.json):

```bash
# on an ARM64 DGX Spark node with Docker (multi-hour CUDA build)
git clone <DEPLOY_REPOSITORY_URL> <DEPLOY_CHECKOUT> && cd <DEPLOY_CHECKOUT>
scripts/image/build-fork.sh --work-dir <EXTERNAL_BUILD_DIR> --dry-run   # review the plan
scripts/image/build-fork.sh --work-dir <EXTERNAL_BUILD_DIR>
```

The script clones the three forks at the pinned commits — **vllm**
`release/production-20260907`, **LMCache** `feat/native432-mp-connector`,
**b12x** `release/dsv4-0731-native432` — plus the same eugr/spark-vllm-docker
build system (commit `e9cf3596`) that produced the running production image,
retargets its B12X preset at the forks, and builds. **No patch is applied at
image-build time**: the three build-time patches the production pipeline used
are already committed in the vllm fork branch (see `folded_in_patches` in the
lock) and are stubbed to no-ops during the build.

Every coordinate was verified on 2026-09-07 by SHA-256 over every installed
Python file against the live production containers: the vllm branch matches
the production engine byte-for-byte (2,226 files) except six parser files
carrying the reviewed DSML fix (upstream PR 52645 port); b12x matches 234/234;
the LMCache connector branch is the union of both production rollout variants.
Details are in the lock's `production_verification` field.

Pass `--profile-sha256`, `--service-contract-sha256`, and
`--image-lock-sha256` to stamp all seven required labels in one build. When
omitted, those three labels are empty and must be filled with the
metadata-only child flow below before the image can enter a ready lock.

## Inputs and immutable identities

A deployable role image must be:

- `linux/arm64` for the GB10 DGX Spark nodes;
- identified by an immutable digest or exact image ID, never a mutable tag;
- built from a reviewed parent/source lock;
- labeled with every required OCI key below; and
- verified to carry the expected embedded service contract.

Production and candidate image namespaces are separate. A production lock MUST
NOT reference `candidate/...`; candidate images and ports are never promoted by
retagging.

```text
production/dsv4-native432-dspark5-327k-seq5-head@sha256:<HEAD_DIGEST>
production/dsv4-native432-dspark5-327k-seq5-worker@sha256:<WORKER_DIGEST>
```

If a local image store has no repository digest for a production tag, use the
exact `sha256:<image-id>` only for that local store. Do not write a guessed
`repository@sha256:` reference. A registry push and read-back is required
before a repository digest can be recorded.

The registry `RepoDigest` read-back is the only accepted repository digest.

## Required labels

The exact label vocabulary is fixed by the profile and deployment-lock schema:

```text
org.opencontainers.image.revision
com.dgx-spark.architecture
com.dgx-spark.profile_sha256
com.dgx-spark.service_contract_sha256
com.dgx-spark.image_lock_sha256
com.dgx-spark.vllm.commit
com.dgx-spark.b12x.commit
```

Read every value from `docker image inspect` and compare it with the external
lock. Do not translate aliases such as `com.dgx-spark.profile-sha256` or
`com.dgx-spark.service-contract-sha256`; the verifier rejects them.

## Metadata-only labels for an existing validated image

Some validated runtime images predate the public label contract. A metadata-only child
can preserve the rootfs while adding labels. Perform this in a staging
image store on the appropriate DGX host or image builder. The recipe MUST have
only `FROM` and `LABEL`; do not add a `RUN`, `COPY`, package, source, or model
layer:

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
```

Repeat with a worker-specific parent and service-contract hash:

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
```

Verify the parent and child before recording the child ID:

```bash
docker image inspect <PARENT_IMAGE_REF>
docker image inspect production/dsv4-native432-dspark5-327k-seq5-head:labelled
docker image inspect <PARENT_IMAGE_REF> --format '{{json .RootFS.Layers}}'
docker image inspect production/dsv4-native432-dspark5-327k-seq5-head:labelled --format '{{json .RootFS.Layers}}'
docker image inspect production/dsv4-native432-dspark5-327k-seq5-head:labelled --format '{{json .Config.Labels}}'
docker image inspect production/dsv4-native432-dspark5-327k-seq5-head:labelled --format '{{.Id}}'
```

`RootFS.Layers` MUST be byte-for-byte identical. The child image ID will be
different because image configuration/labels contribute to its identity. Update
the external lock with that new child ID and digest reference. Never retain the
parent ID in a lock after labeling.

## Embedded service contract

The committed [`container/service-contract.json.in`](../container/service-contract.json.in)
contains the reviewed model path, native432 served name, TP2/MP topology, KV
and scheduler limits, reasoning defaults, DSpark draft settings, b12x backends,
CUDA Graph settings, and source/profile/contract hash placeholders. The deploy
renderer writes a role-specific complete contract to an external directory and
mounts it read-only at `/etc/dgx-spark/service-contract.json`.

```bash
bin/dgx-deploy render \
  --env-file <PATH>/dgx-spark-production/production.env \
  --lock-file <PATH>/dgx-spark-production/production.lock.json \
  --output-dir <PATH>/dgx-spark-production/contracts
python3 -c 'import json; from pathlib import Path; from dgx_deploy.contract import validate_contract; [validate_contract(json.loads(Path("<PATH>/dgx-spark-production/contracts/"+r+".contract.json").read_text())) for r in ("head","worker")]; print("contracts valid")'
```

Do not copy a private command script, local cache path, log, or image archive
into this repository. Runtime environment values belong in the generated
external role env files or in the immutable image, not in arbitrary operator
shell hooks.

## External deployment lock

Use [`config/deployment-lock.schema.json`](../config/deployment-lock.schema.json)
as the shape contract. Each role requires a reference, image ID, and all seven
labels. The `model_manifest_sha256` field must equal the SHA-256 of the exact
public `model.lock.json` bytes used to install the model. Validate the finished
lock before `plan`:
The lock also carries `lock_sha256`, computed over canonical JSON after
removing only the `lock_sha256` field. Compute and write that value last, then
rerun schema validation. The engine checks both this semantic content digest
and every required image/profile/model identity; a valid JSON file with a
different byte/content hash is rejected.

```bash
python3 -c 'import json; from pathlib import Path; from jsonschema import Draft202012Validator; s=json.loads(Path("config/deployment-lock.schema.json").read_text()); l=json.loads(Path("<PATH>/dgx-spark-production/production.lock.json").read_text()); e=list(Draft202012Validator(s).iter_errors(l)); assert not e, e; print("lock valid")'
python3 -c 'import hashlib, json; from pathlib import Path; v=json.loads(Path("<PATH>/dgx-spark-production/production.lock.json").read_text()); v.pop("lock_sha256", None); print(hashlib.sha256(json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest())'
```

A ready lock is still subject to node preflight. If an image read-back does not
match any label or ID, `apply`/`update` stops before touching containers.

## Offline archive transfer and read-back

When a registry is unavailable, save each already-labeled production image
separately. Use the strict `ssh_dgx`/`scp_dgx` wrappers from
[`deployment.md`](deployment.md), and keep archives outside Git:

```bash
docker save --output <PATH>/head.tar <HEAD_IMAGE_REF>
docker save --output <PATH>/worker.tar <WORKER_IMAGE_REF>
sha256sum <PATH>/head.tar > <PATH>/head.tar.sha256
sha256sum <PATH>/worker.tar > <PATH>/worker.tar.sha256
scp_dgx <PATH>/head.tar DGX-SPARK-0:<REMOTE_PATH>/head.tar
scp_dgx <PATH>/worker.tar DGX-SPARK-1:<REMOTE_PATH>/worker.tar
ssh_dgx DGX-SPARK-0 "sha256sum -c <REMOTE_PATH>/head.tar.sha256 && docker load --input <REMOTE_PATH>/head.tar"
ssh_dgx DGX-SPARK-1 "sha256sum -c <REMOTE_PATH>/worker.tar.sha256 && docker load --input <REMOTE_PATH>/worker.tar"
ssh_dgx DGX-SPARK-0 "docker image inspect <PRODUCTION_HEAD_IMAGE_REF>"
ssh_dgx DGX-SPARK-1 "docker image inspect <PRODUCTION_WORKER_IMAGE_REF>"
```

Transfer the checksum files alongside the archives or copy their exact
contents through the same controlled channel. After `docker load`, read back
the image ID, architecture, all seven labels, and rootfs layer list on each
node. The read-back ID/reference—not the tag in the archive—is what belongs in
the deployment lock.

Derive values in this order: render/validate the service contract; build or
label the image; inspect labels and rootfs; save and hash the archive if using
offline transfer; load; inspect the loaded image again; then write the
external lock's `image_id`, immutable `reference`, label values, and
`lock_sha256` last. `model_manifest_sha256` remains the SHA-256 of exact
`model.lock.json` bytes. Never derive a lock or label from an unverified tag,
an archive filename, a mutable registry response, or a private log.

## Distribution and promotion rules

Registry mode must push to an operator-approved production repository, then
read back each repository digest and labels. Offline mode must transfer one
named archive with an external SHA-256 and verify the resulting image IDs on
both nodes. The deploy engine does not pull, push, or promote images implicitly.

Candidate qualification must produce a separate candidate lock and evidence.
Only after candidate gates pass may an operator prepare a new production lock.
A candidate failure leaves the production lock unchanged.

## Public release boundary

Do not commit image IDs, registry credentials, archives, hostnames, SSH paths,
model data, private source trees, or local evidence. Keep all final image and
runtime metadata under operator-owned external roots. The public image lock
stays pending until full metadata is available; a deployment lock is the
operator's explicit, reviewable release decision.
