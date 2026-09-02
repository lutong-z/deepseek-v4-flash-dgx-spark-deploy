#!/usr/bin/env bash
set -euo pipefail

root="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
output="$(python3 "$root/scripts/model/fetch.py" --dry-run)"
case "$output" in
  *"deepseek-ai/DeepSeek-V4-Flash-0731"*"7872f01b1d1fe23eabc4c98b48bffcef5a386062"*"48 weight shards"*"no network requested"*) ;;
  *)
    printf '%s\n' 'model dry-run output did not describe the locked no-network plan' >&2
    exit 1
    ;;
esac
printf '%s\n' 'model dry-run passed without downloading weights'
