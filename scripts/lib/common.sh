#!/usr/bin/env bash
set -euo pipefail

DGX_DEPLOY_ROOT="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"

dgx_deploy() {
  PYTHONPATH="$DGX_DEPLOY_ROOT${PYTHONPATH:+:$PYTHONPATH}" python3 -m dgx_deploy.cli "$@"
}
locked_refusal() {
  local action="$1"
  printf 'dgx-deploy: %s requires a reviewed lock and is intentionally disabled\n' "$action" >&2
  return 78
}

require_external_root() {
  local name="$1"
  local value="${!name:-}"
  [[ -n "$value" && "$value" != "$DGX_DEPLOY_ROOT" && "$value" != "$DGX_DEPLOY_ROOT"/* ]] || {
    printf 'dgx-deploy: %s must point outside the repository\n' "$name" >&2
    return 64
  }
}
