#!/usr/bin/env bash
set -euo pipefail
source "$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../lib" && pwd)/common.sh"
scaffold_refusal 'network restore'
