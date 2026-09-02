# Image lifecycle

The service image must be an immutable ARM64 artifact whose embedded service
contract matches the committed profile and the rendered configuration. Image
build inputs, archives, registry credentials, and final digests remain outside
Git.

## Build review

A future build command must require:

- a digest-pinned ARM64 base image;
- a reviewed source/build lock and patch license record;
- BuildKit secrets for any private dependency access, never build arguments;
- OCI labels for source revision, profile hash, build-lock hash, architecture,
  and service-contract hash.

`container/Containerfile` contains only the contract wiring. The current shell
entry point rejects builds, so no image can be created accidentally from this
scaffold.

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
