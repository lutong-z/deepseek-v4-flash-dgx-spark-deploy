# Image lifecycle

The service image must be an immutable ARM64 artifact whose embedded service
contract matches the committed profile and the rendered configuration. Image
build inputs, archives, registry credentials, and final digests remain outside
Git.

## Candidate lock

`image.lock.json` is the sanitized, provisional provenance record for the
`dsv4-native432-b12x-tp2` profile. It records only public source coordinates
and supplied artifact values:

- the vLLM fork at `release/dsv4-0731-native432-b12x`, HEAD
  `5817170bc04b0f203797c4a667f976bff49c12d4`;
- B12X at `release/dsv4-0731-native432`, HEAD
  `d476465883cc7e46c128e0effa89fad1a7200cd7`;
- profile SHA-256, both source-archive SHA-256 values, and the runtime-manifest
  SHA-256;
- role image IDs for head and worker, plus the worker RepoDigest. The head
  image has no RepoDigest in the supplied inspection.

The lock's `pending-artifacts` status remains intentional while the behavioral
four-smoke is pending. Base-image identity, dependency-lock and
service-contract hashes, generic image reference/RepoDigest/configuration and
image IDs, and the offline archive SHA-256 remain `null` because they were not
supplied. The lock MUST NOT be changed to `ready-for-review` until those
values and the behavioral validation are independently recorded and checked.
No private checkout paths, build logs, image archives, host records, or local
evidence are represented here.

The profile's `image.required_labels` list is the label contract for a future
artifact. In particular, source commits, profile and service-contract hashes,
the lock hash, and `linux/arm64` architecture must be read back from the image
and compared with this lock before any load or promotion. A mutable tag is
never a substitute for an immutable digest.

## Build review

A future build command must require:

- a digest-pinned ARM64 base image;
- a reviewed source/build lock and patch license record;
- BuildKit secrets for any private dependency access, never build arguments;
- OCI labels for source revision, profile hash, build-lock hash, architecture,
  and service-contract hash. The scaffold wires these as
  `org.opencontainers.image.revision`,
  `com.dgx-spark.profile_sha256`, `com.dgx-spark.image_lock_sha256`,
  `com.dgx-spark.architecture`, `com.dgx-spark.service_contract_sha256`,
  `com.dgx-spark.vllm.commit`, and `com.dgx-spark.b12x.commit`.

The `Containerfile` ARGs are metadata inputs only; they do not fetch source,
accept credentials, or authorize promotion. The current shell entry point
rejects builds, so no image can be created accidentally from this scaffold.

## Distribution review

Registry and offline archive modes are mutually exclusive. Registry mode must
resolve a repository digest after authenticated staging. Offline mode must
carry one named archive and an external SHA-256, transfer only to explicit
operator hosts, verify before load, and compare resulting image identities on
both nodes. Directory mirroring, mutable tags, and guessed image IDs are not
allowed.

Image verification must prove architecture, digest, labels, and embedded
contract before any future lifecycle command can create a container. Build
logs and archives belong under external roots and are never staged.
