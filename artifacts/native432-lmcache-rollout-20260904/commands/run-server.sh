#!/usr/bin/env bash
# Launch the per-node LMCache MP server. Run once per node with that node's
# fabric IP. Requires the role-specific image built by ../image/build.sh.
#
# Boot order is load-bearing: BOTH servers must be up and verified here BEFORE
# the engine starts (see docs/deployment.md). This script fails loudly
# rather than leaving a half-up server behind a green exit code.
set -euo pipefail
SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
FABRIC_IP="${1:?usage: run-server.sh <this-node-fabric-ip> [image]}"
case "$FABRIC_IP" in
  192.168.100.10) DEFAULT_IMAGE=sha256:af5c963faa0e6d086011cf3870f2b8e7e9f00a0b622b7d3d40d1047704b598c1 ;;
  192.168.100.11) DEFAULT_IMAGE=sha256:b1b0360b0b32c89a97c6eae44bee57ff9f37ff0c75beb016c2bee7c2713da43d ;;
  *) DEFAULT_IMAGE= ;;
esac
IMAGE="${2:-$DEFAULT_IMAGE}"
[[ -n "$IMAGE" ]] || {
  echo "error: pass an explicit image for unknown fabric IP $FABRIC_IP" >&2
  exit 2
}
DISK="${LMCACHE_DISK_DIR:-$HOME/.cache/lmcache/native432-bf16-rope-432-identity-v1}"
PORT="${LMCACHE_PORT:-6667}"
LIFECYCLE_SCRIPT="${LMCACHE_LIFECYCLE_SCRIPT:-$SCRIPT_DIR/../../../dgx_deploy/lmcache_lifecycle.py}"
SERVER_SCRIPT="${LMCACHE_SERVER_SCRIPT:-$SCRIPT_DIR/../../../dgx_deploy/lmcache_server.py}"
L2_TTL_SECONDS="${LMCACHE_L2_TTL_SECONDS:-7200}"
L2_MAX_GB="${LMCACHE_L2_MAX_GB:-100}"
L1_GB="${LMCACHE_L1_GB:-1}"
L2_CAPACITY_TRIM_RATIO="${LMCACHE_L2_CAPACITY_TRIM_RATIO:-0.8}"
L2_SCAN_INTERVAL_SECONDS="${LMCACHE_L2_SCAN_INTERVAL_SECONDS:-60}"
LIFECYCLE_METRICS_PORT="${LMCACHE_LIFECYCLE_METRICS_PORT:-19120}"
SERVER_METRICS_PORT="${LMCACHE_SERVER_METRICS_PORT:-19121}"
# 0 = kernel default (Docker's default too), so this is a no-op unless set.
# A negative value biases the OOM killer away from the cache server on 128 GB
# unified-memory boards; see docs/deployment.md. It makes the engine
# the likelier victim instead, which is the recoverable failure of the two.
OOM_SCORE_ADJ="${LMCACHE_OOM_SCORE_ADJ:-0}"

die() { echo "error: $*" >&2; exit 1; }

# --- preflight (fail before we touch a running cache) -----------------------
command -v docker >/dev/null 2>&1 || die "docker not found on PATH"
docker image inspect "$IMAGE" >/dev/null 2>&1 \
  || die "image '$IMAGE' not present locally; run ../image/build.sh for this role"
[ -r "$LIFECYCLE_SCRIPT" ] \
  || die "L2 lifecycle script is not readable: $LIFECYCLE_SCRIPT"
[ -r "$SERVER_SCRIPT" ] \
  || die "LMCache server wrapper is not readable: $SERVER_SCRIPT"

# The server must bind THIS node's fabric IP; a typo binds nothing and the
# engine's lookups then go to a socket that never answers.
if command -v ip >/dev/null 2>&1; then
  ip -o -4 addr show 2>/dev/null | grep -qw "$FABRIC_IP" \
    || die "$FABRIC_IP is not bound on this host; pass THIS node's fabric IP"
fi

docker run --rm --entrypoint python3 "$IMAGE" -c \
  'import cupy, lmcache
from lmcache.lmcache_fs import LMCacheFSClient
from lmcache.v1.distributed.l2_adapters.fs_l2_adapter import _object_key_to_filename
from lmcache.v1.distributed.l2_adapters.native_connector_l2_adapter import NativeConnectorL2Adapter
assert hasattr(NativeConnectorL2Adapter, "submit_lookup_and_lock_task")' \
  >/dev/null 2>&1 \
  || die "image '$IMAGE' lacks the pinned native FS L2 integration"

mkdir -p "$DISK" || die "cannot create L2 dir $DISK"
[ -w "$DISK" ] || die "L2 dir $DISK is not writable"

# Replacing a cache server under a LIVE engine is the documented wedge
# condition, so two guards stand between here and the docker rm below:
#
# Guard 1 — the model container. The real-world failure mode is a DEAD server
# under a LIVE (wedged) engine (docs/deployment.md); proceeding
# there would rm + recreate an EMPTY server against that engine — the wedge
# itself. So this refusal has NO override: LMCACHE_FORCE_REPLACE is documented
# to mean "the pair is already down", and a live model container falsifies
# that claim. Stop the pair first; the recovery is a full-pair restart.
# Fail CLOSED: a docker-ps failure here must not be read as "nothing is
# running" — that would take the guard down exactly when the host is sick.
if ! model_ps_a="$(docker ps -q -f 'name=dsv4-native432' 2>&1)"; then
  die "docker ps (native432 name filter) failed — refusing to recreate the cache server without trustworthy state: $model_ps_a"
fi
if ! model_ps_b="$(docker ps -q -f 'label=com.dgx-spark.deployment_id' 2>&1)"; then
  die "docker ps (DGX deployment label filter) failed — refusing to recreate the cache server without trustworthy state: $model_ps_b"
fi
# Catch both the preserved production names and LMCache candidate names, plus
# deploy-managed containers with an explicit deployment identity. The role
# label is inherited by the cache image itself and therefore is not selective.
if [ -n "$model_ps_a" ] || [ -n "$model_ps_b" ]; then
  die "a model (vllm-dspark) container is RUNNING. Recreating the lmcache server
       under a live engine is the documented wedge (docs/deployment.md):
       the fresh server has no GPU contexts and the engine parks every lookup
       hit forever. Stop the complete TP pair, then re-run this script.
       LMCACHE_FORCE_REPLACE does NOT bypass this guard."
fi

# Guard 2 — a still-running (possibly poisoned) server. Overridable only for
# the legitimate pair-is-down case: stale/empty server, no engine running.
if ! server_ps="$(docker ps -q -f 'name=^lmcache-server$' 2>&1)"; then
  die "docker ps (server filter) failed — refusing to recreate the cache server without trustworthy state: $server_ps"
fi
if [ -n "$server_ps" ]; then
  [ "${LMCACHE_FORCE_REPLACE:-0}" = "1" ] \
    || die "lmcache-server is already RUNNING and no model container is up.
       Only re-create it with LMCACHE_FORCE_REPLACE=1 if the pair is already
       down (e.g. stale server from an aborted boot)."
fi
docker rm -f lmcache-server >/dev/null 2>&1 || true

# L1 is a bounded transfer tier: stores are deleted after reaching L2 and
# restores are temporary. It cannot be removed because every L2 store and
# restore stages native432 objects through L1. The 1 GiB default plus vLLM's
# 0.84 fraction remains below the proven 0.85 service GPU budget.
docker run -d --name lmcache-server --network host --ipc host --gpus all \
  --restart no \
  --oom-score-adj "$OOM_SCORE_ADJ" \
  -e PYTHONHASHSEED=0 \
  -e LMCACHE_L2_ROOT=/lmcache-disk \
  -v "$DISK:/lmcache-disk" \
  --mount "type=bind,src=$LIFECYCLE_SCRIPT,dst=/opt/dgx-spark/lmcache_lifecycle.py,readonly" \
  --mount "type=bind,src=$SERVER_SCRIPT,dst=/opt/dgx-spark/lmcache_server.py,readonly" \
  --entrypoint /bin/bash "$IMAGE" -lc '
set -euo pipefail
lifecycle_pid=
server_pid=
cleanup() {
  [ -z "$server_pid" ] || kill "$server_pid" 2>/dev/null || true
  [ -z "$lifecycle_pid" ] || kill "$lifecycle_pid" 2>/dev/null || true
  [ -z "$server_pid" ] || wait "$server_pid" 2>/dev/null || true
  [ -z "$lifecycle_pid" ] || wait "$lifecycle_pid" 2>/dev/null || true
}
trap cleanup EXIT INT TERM
python3 /opt/dgx-spark/lmcache_lifecycle.py \
  --root /lmcache-disk \
  --ttl-seconds "$3" \
  --max-size-gb "$6" \
  --capacity-trim-ratio "$7" \
  --scan-interval-seconds "$4" \
  --listen-host "$1" \
  --metrics-port "$5" \
  --initialize-root &
lifecycle_pid=$!
lifecycle_ready=false
for _ in $(seq 1 30); do
  if exec 3<>"/dev/tcp/$1/$5"; then
    printf "GET /health HTTP/1.0\r\nHost: localhost\r\n\r\n" >&3
    IFS= read -r status <&3 || true
    exec 3<&- 3>&-
    case "$status" in
      *" 200 "*) lifecycle_ready=true; break ;;
    esac
  fi
  sleep 1
done
"$lifecycle_ready" || exit 1
kill -0 "$lifecycle_pid"
python3 /opt/dgx-spark/lmcache_server.py \
  --host "$1" --port "$2" --chunk-size 256 \
  --separate-object-groups \
  --worker-reap-timeout-seconds "$9" \
  --l1-size-gb "$8" --l1-use-lazy \
  --l1-write-ttl-seconds 60 --l1-read-ttl-seconds 60 \
  --eviction-policy noop \
  --eviction-trigger-watermark 0.90 --eviction-ratio 0.50 \
  --l2-store-policy skip_l1 \
  --l2-prefetch-policy default \
  --l2-adapter "{\"type\":\"fs_native\",\"base_path\":\"/lmcache-disk\",\"num_workers\":4,\"relative_tmp_dir\":\".tmp\",\"use_odirect\":false,\"read_ahead_size\":16777216,\"max_capacity_gb\":0}" \
  --prometheus-port "${10}" &
server_pid=$!
wait -n "$lifecycle_pid" "$server_pid"
' -- "$FABRIC_IP" "$PORT" "$L2_TTL_SECONDS" "$L2_SCAN_INTERVAL_SECONDS" \
  "$LIFECYCLE_METRICS_PORT" "$L2_MAX_GB" "$L2_CAPACITY_TRIM_RATIO" \
  "$L1_GB" "${LMCACHE_WORKER_REAP_TIMEOUT:-0}" \
  "$SERVER_METRICS_PORT" >/dev/null

# --- verify the resulting state, not the exit code of docker run -----------
for _ in $(seq 1 30); do
  [ -n "$(docker ps -q -f name="^lmcache-server$")" ] || break
  ports_ready=true
  for ready_port in "$PORT" "$LIFECYCLE_METRICS_PORT" "$SERVER_METRICS_PORT"; do
    if ! (exec 3<>"/dev/tcp/${FABRIC_IP}/${ready_port}") 2>/dev/null; then
      ports_ready=false
      break
    fi
    exec 3<&- 3>&- 2>/dev/null || true
  done
  if "$ports_ready"; then
    echo "lmcache server up on ${FABRIC_IP}:${PORT}"
    echo "  L1: ${L1_GB}G transient transfer tier; persistent fs L2: $DISK"
    echo "  L2: ${L2_MAX_GB}G LRU, TTL ${L2_TTL_SECONDS}s, trim ratio ${L2_CAPACITY_TRIM_RATIO}"
    echo "  metrics: ${LIFECYCLE_METRICS_PORT},${SERVER_METRICS_PORT}"
    echo "NOTE: start the engine only after EVERY node reports this line."
    exit 0
  fi
  sleep 1
done
echo "--- lmcache-server logs ---" >&2
docker logs --tail=50 lmcache-server >&2 2>&1 || true
die "lmcache-server did not come up listening on ${FABRIC_IP}:${PORT}"
