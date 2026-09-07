#!/usr/bin/env bash
# Build the 2026-09-05 streaming-restore LMCache server images.
#
# Overlay contents (server-side only; the engine client is unchanged):
# - lmcache/v1/distributed/config.py: --l2-prefetch-l1-reserve-batch-keys
#   (default 32) and --l2-retrieve-window-chunks (default 16).
# - lmcache/v1/distributed/storage_controllers/prefetch_controller.py: L1
#   write reservations run in bounded prefix-ordered windows; out-of-memory
#   degrades to the longest reservable prefix instead of the old
#   all-or-nothing abort, while contention still aborts the whole load.
# - lmcache/v1/multiprocess/modules/lmcache_driven_transfer.py: retrieve
#   streams H2D per chunk window, releasing each window's L1 read locks in
#   stream order, so a large restore never holds the whole prefix in L1.
# - lmcache/v1/distributed/storage_manager.py,
#   lmcache/v1/multiprocess/engine_context.py: wiring for the two knobs.
set -euo pipefail

ROLE="${1:?usage: build.sh <head|worker>}"
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

case "$ROLE" in
  head)
    BASE_TAG=native432-lmcache:head-ttl-v1
    BASE_ID=sha256:efce8b7fec691d8d2dc60e2ff69123219dca325f2c2158a210ce44074b72920d
    OUTPUT_TAG=native432-lmcache:head-server-streaming-v1
    ;;
  worker)
    BASE_TAG=native432-lmcache:worker-ttl-v1
    BASE_ID=sha256:c0fd40d8b59d5fc2b4de9ae35aff9eb3cac2f1656bda1d6b953f1ebcb9362e7a
    OUTPUT_TAG=native432-lmcache:worker-server-streaming-v1
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

docker build --pull=false --provenance=false \
  --file "$SCRIPT_DIR/Containerfile.$ROLE" \
  --tag "$OUTPUT_TAG" \
  "$SCRIPT_DIR"

image_id="$(docker image inspect --format '{{.Id}}' "$OUTPUT_TAG")"
printf '%s image=%s base=%s\n' "$ROLE" "$image_id" "$BASE_ID"
