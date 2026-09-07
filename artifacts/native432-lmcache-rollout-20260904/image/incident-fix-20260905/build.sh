#!/usr/bin/env bash
# Build the 2026-09-05 incident-fix derived engine images.
#
# Overlay contents (all verified by targeted pytest runs against the
# production derived images before packaging):
# - vllm/v1/core/sched/scheduler.py: multi-group (hybrid) KV invalid-block
#   recovery (production crash fix), external KV load transient retry, and
#   session-aware prefix-eviction protection.
# - vllm/v1/request.py, vllm/envs.py,
#   vllm/distributed/kv_transfer/kv_connector/v1/base.py: retry state and
#   connector reset_load_state() hook.
# - vllm/v1/core/block_pool.py, vllm/v1/core/kv_cache_utils.py: session
#   block protection victim ordering.
# - lmcache/integration/vllm/vllm_multi_process_adapter.py: heartbeat PING
#   timeout decoupled from the ping interval (slow-restore misjudgment fix)
#   plus heartbeat failure-reason logging.
# - lmcache/integration/vllm/lmcache_mp_connector.py: reset_load_state()
#   enabling the scheduler retry path.
set -euo pipefail

ROLE="${1:?usage: build.sh <head|worker>}"
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

case "$ROLE" in
  head)
    BASE_TAG=native432-lmcache:head-ttl-v1
    BASE_ID=sha256:efce8b7fec691d8d2dc60e2ff69123219dca325f2c2158a210ce44074b72920d
    OUTPUT_TAG=native432-lmcache:head-incident-v1
    ;;
  worker)
    BASE_TAG=native432-lmcache:worker-ttl-v1
    BASE_ID=sha256:c0fd40d8b59d5fc2b4de9ae35aff9eb3cac2f1656bda1d6b953f1ebcb9362e7a
    OUTPUT_TAG=native432-lmcache:worker-incident-v1
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
