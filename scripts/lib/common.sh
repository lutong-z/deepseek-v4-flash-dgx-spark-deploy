#!/usr/bin/env bash
set -euo pipefail

DGX_DEPLOY_ROOT="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"

scaffold_refusal() {
  local action="$1"
  printf 'dgx-deploy: %s is disabled in the clean scaffold; use bin/dgx-deploy plan for local dry-run rendering\n' "$action" >&2
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
