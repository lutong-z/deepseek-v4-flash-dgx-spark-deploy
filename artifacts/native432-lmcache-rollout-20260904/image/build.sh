#!/usr/bin/env bash
set -euo pipefail

ROLE="${1:?usage: build.sh <head|worker>}"
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PATCH="$SCRIPT_DIR/prefix-cache-idle-ttl.patch"
PATCH_SHA256=1c8cf55a6ec686973405708f46948512b35338d5d2e9e7dd143ca8ffb5a4e909

case "$ROLE" in
  head)
    BASE_TAG=native432-lmcache:head-fork
    BASE_ID=sha256:1e508adb13d2112167e910be7d6b899b413dffea3fc07ed22edc4ae10992ab8b
    OUTPUT_TAG=native432-lmcache:head-ttl-v1
    ;;
  worker)
    BASE_TAG=native432-lmcache:worker-fork
    BASE_ID=sha256:18b9dfbd1ebcede78f1a563e2bc9f47a9263a2a222d03f21cf67fd2d017df49d
    OUTPUT_TAG=native432-lmcache:worker-ttl-v1
    ;;
  *)
    echo "error: role must be head or worker" >&2
    exit 2
    ;;
esac

actual_base="$(docker image inspect --format '{{.Id}}' "$BASE_TAG")"
[[ "$actual_base" == "$BASE_ID" ]] || {
  echo "error: $BASE_TAG resolved to $actual_base, expected $BASE_ID" >&2
  exit 1
}
actual_patch="$(sha256sum "$PATCH" | cut -d ' ' -f 1)"
[[ "$actual_patch" == "$PATCH_SHA256" ]] || {
  echo "error: patch digest is $actual_patch, expected $PATCH_SHA256" >&2
  exit 1
}

docker build --pull=false --provenance=false \
  --file "$SCRIPT_DIR/Containerfile.$ROLE" \
  --tag "$OUTPUT_TAG" \
  "$SCRIPT_DIR"

image_id="$(docker image inspect --format '{{.Id}}' "$OUTPUT_TAG")"
label="$(docker image inspect --format '{{index .Config.Labels "com.dgx-spark.vllm.runtime-patch-sha256"}}' "$OUTPUT_TAG")"
[[ "$label" == "$PATCH_SHA256" ]] || {
  echo "error: derived image patch label is $label" >&2
  exit 1
}
printf '%s image=%s base=%s patch=%s\n' "$ROLE" "$image_id" "$BASE_ID" "$PATCH_SHA256"
