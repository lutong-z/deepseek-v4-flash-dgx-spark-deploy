#!/usr/bin/env bash
set -euo pipefail

root="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
env_file="$(mktemp "${TMPDIR:-/tmp}/dgx-deploy-env.XXXXXX")"
trap 'rm -f "$env_file"' EXIT

cat >"$env_file" <<'EOF'
HEAD_HOST=192.0.2.10
WORKER_HOST=192.0.2.11
SSH_USER=runner
SSH_PORT=22
SSH_KNOWN_HOSTS_FILE=/etc/ssh/known_hosts
REMOTE_ROOT=/srv/dgx-spark/deploy
MODEL_ROOT=/srv/models
MODEL_MANIFEST_SHA256=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
STATE_ROOT=/var/lib/dgx-spark/state
CACHE_ROOT=/var/cache/dgx-spark
RESULT_ROOT=/var/lib/dgx-spark/results
IMAGE_REF=registry.example.invalid/dsv4@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
MASTER_ADDR=192.0.2.10
MASTER_PORT=29519
API_PORT=8000
HEAD_NODE_ADDR=192.0.2.10
WORKER_NODE_ADDR=192.0.2.11
HEAD_NET_IFACE=rdma0
WORKER_NET_IFACE=rdma0
HEAD_HCA=mlx5_0
WORKER_HCA=mlx5_0
HEAD_CUDA_VISIBLE_DEVICES=0
WORKER_CUDA_VISIBLE_DEVICES=0
ROCE_MTU=9000
API_BIND_ADDR=127.0.0.1
FORWARD_LOCAL_PORT=18080
EOF

export PYTHONPATH="$root${PYTHONPATH:+:$PYTHONPATH}"
python3 -m dgx_deploy.cli config validate --env-file "$env_file" >/dev/null
python3 -m dgx_deploy.cli plan --env-file "$env_file" >/dev/null
if python3 -m dgx_deploy.cli plan --env-file "$env_file" --apply >/dev/null 2>&1; then
  printf '%s\n' 'mutation unexpectedly accepted' >&2
  exit 1
fi
printf '%s\n' 'dry-run scaffold passed'
