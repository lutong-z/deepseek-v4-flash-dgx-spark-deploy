#!/usr/bin/env bash
# One-command production image build from the three fork source trees.
#
# Builds the exact image content running in production (verified 2026-09-07
# byte-for-byte against the live containers) from source, with zero patches
# applied at image-build time: every change is committed in the forks and
# pinned by image.fork.lock.json.
#
# Run on an ARM64 DGX Spark node (or any linux/arm64 host with Docker and
# enough disk/CPU for a multi-hour CUDA build):
#
#   scripts/image/build-fork.sh --work-dir /data/fork-build
#
# Stages: fetch -> prepare -> vllm -> lmcache -> final -> summary.
# --dry-run prints every step without cloning, patching, or building.
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)"
LOCK_FILE="$DEPLOY_ROOT/image.fork.lock.json"

WORK_DIR=""
DRY_RUN=false
PROXY=""
PYPI_MIRROR=""
BUILD_JOBS=""
IMAGE_TAG="vllm-node-b12x:production-20260907"
FINAL_TAG="production/dsv4-native432-fork:production-20260907"
PROFILE_SHA256=""
SERVICE_CONTRACT_SHA256=""
IMAGE_LOCK_SHA256=""

usage() {
  cat <<'EOF'
usage: build-fork.sh [--work-dir <path>] [--dry-run] [--proxy <url>]
                     [--image-tag <tag>] [--final-tag <tag>]
                     [--profile-sha256 <sha>] [--service-contract-sha256 <sha>]
                     [--image-lock-sha256 <sha>]

Stages:
  fetch    clone vllm / lmcache / b12x forks + eugr build system, verify
           every checkout's HEAD equals image.fork.lock.json
  prepare  point the eugr build preset at the forks and no-op the three
           build-time patches already folded into the vllm branch

Network: when a proxy is set (explicitly or auto-detected), the vllm build
runs with --network host and BuildKit forwards the proxy variables into the
build containers, so in-container git/pip traffic uses the loopback proxy.
  vllm     build the vllm+b12x base image (multi-hour CUDA build)
  lmcache  build the lmcache fork wheel inside the base image
  final    install the wheel and stamp the OCI/provenance labels
  summary  print image ID, labels, and a deployment-lock template

The three deploy-side label digests are optional build inputs; when empty
the corresponding labels are stamped empty and must be filled by the
metadata-only child flow in docs/image.md before the image can enter a
ready deployment lock.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --pypi-mirror) PYPI_MIRROR="$2"; shift 2 ;;
    --work-dir) WORK_DIR="$2"; shift 2 ;;
    --build-jobs) BUILD_JOBS="$2"; shift 2 ;;
    --dry-run) DRY_RUN=true; shift ;;
    --proxy) PROXY="$2"; shift 2 ;;
    --image-tag) IMAGE_TAG="$2"; shift 2 ;;
    --final-tag) FINAL_TAG="$2"; shift 2 ;;
    --profile-sha256) PROFILE_SHA256="$2"; shift 2 ;;
    --service-contract-sha256) SERVICE_CONTRACT_SHA256="$2"; shift 2 ;;
    --image-lock-sha256) IMAGE_LOCK_SHA256="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "build-fork: unknown argument $1" >&2; usage >&2; exit 64 ;;
  esac
done

[[ -n "$WORK_DIR" ]] || { echo "build-fork: --work-dir is required (keeps gigabytes of clones/build cache outside any repository)" >&2; exit 64; }

# --- proxy: explicit --proxy, else auto-detect a local loopback proxy -------
# DGX nodes in restricted networks run a local proxy (e.g. mihomo/clash on
# 127.0.0.1:7890); github.com is unreachable directly but reachable via it.
# BuildKit forwards these env vars into RUN steps as predefined build-args;
# combined with --network host the in-container git/pip/uv traffic uses the
# same loopback proxy.
if [[ -z "$PROXY" ]]; then
  for candidate in http://127.0.0.1:7890 http://127.0.0.1:7891 http://127.0.0.1:8118; do
    if timeout 5 curl -s -o /dev/null --proxy "$candidate" https://github.com 2>/dev/null; then
      PROXY="$candidate"
      break
    fi
  done
fi
if [[ -n "$PROXY" ]]; then
  export https_proxy="$PROXY" http_proxy="$PROXY" HTTPS_PROXY="$PROXY" HTTP_PROXY="$PROXY"
  echo "build-fork: using proxy $PROXY"
else
  echo "build-fork: no proxy configured; direct network access assumed"
fi
[[ -f "$LOCK_FILE" ]] || { echo "build-fork: lock file not found: $LOCK_FILE" >&2; exit 66; }

# --- read coordinates from the lock (python3 stdlib only) -------------------
read_lock() {
  python3 - "$LOCK_FILE" <<'PY'
import json, sys
lock = json.load(open(sys.argv[1]))
bs = lock["build_system"]
out = {
    "VLLM_REPO": lock["vllm"]["repository"],
    "VLLM_REF": lock["vllm"]["ref"],
    "VLLM_COMMIT": lock["vllm"]["commit"],
    "LMCACHE_REPO": lock["lmcache"]["repository"],
    "LMCACHE_REF": lock["lmcache"]["ref"],
    "LMCACHE_COMMIT": lock["lmcache"]["commit"],
    "B12X_REPO": lock["b12x"]["repository"],
    "B12X_REF": lock["b12x"]["ref"],
    "B12X_COMMIT": lock["b12x"]["commit"],
    "BUILD_SYS_REPO": bs["repository"],
    "BUILD_SYS_COMMIT": bs["commit"],
    "BUILD_COMMAND": bs["build_command"],
    "OVERRIDE_VLLM_REPO": bs["vllm_repo_override"],
    "OVERRIDE_VLLM_REF": bs["vllm_ref_override"],
    "OVERRIDE_B12X_REPO": bs["b12x_repo_override"],
    "OVERRIDE_B12X_REF": bs["b12x_ref_override"],
    "FOLDED_PATCHES": " ".join(p["file"] for p in lock["folded_in_patches"]),
}
for k, v in out.items():
    print(f'{k}=\"{v}\"')
PY
}
eval "$(read_lock)"

step() { printf '\n== [%s] %s ==\n' "$1" "$2"; }
run() {
  if $DRY_RUN; then
    printf 'DRY-RUN %s\n' "$*"
  else
    "$@"
  fi
}
clone_at() { # <repo> <ref> <expected-commit> <dest> [need_tags]
  local repo="$1" ref="$2" want="$3" dest="$4" need_tags="${5:-}"
  if $DRY_RUN; then
    if [[ "$ref" == "$want" || "$need_tags" == "need_tags" ]]; then
      printf 'DRY-RUN git clone --filter=blob:none %s %s && checkout %s\n' "$repo" "$dest" "$want"
    else
      printf 'DRY-RUN git clone --depth 1 --branch %s %s %s && verify HEAD == %s\n' "$ref" "$repo" "$dest" "$want"
    fi
    return
  fi
  if [[ ! -d "$dest/.git" ]]; then
    if [[ "$ref" == "$want" || "$need_tags" == "need_tags" ]]; then
      # Raw commit ref, or a branch whose setuptools_scm version needs the
      # full tag history (lmcache): partial clone keeps all refs/tags, blobs
      # fetched on demand.
      git clone --filter=blob:none "$repo" "$dest" >/dev/null
    else
      # Branch used only for lock verification: shallow clone suffices.
      git clone --depth 1 --branch "$ref" "$repo" "$dest" >/dev/null
    fi
  fi
  git -C "$dest" checkout --quiet "$want"
  local actual
  actual="$(git -C "$dest" rev-parse HEAD)"
  [[ "$actual" == "$want" ]] || {
    echo "build-fork: $dest HEAD is $actual, expected $want" >&2; exit 1; }
}
# --- stage 1: fetch ---------------------------------------------------------
step fetch "clone forks and build system, verify commits"
clone_at "$VLLM_REPO" "$VLLM_REF" "$VLLM_COMMIT" "$WORK_DIR/vllm"
clone_at "$LMCACHE_REPO" "$LMCACHE_REF" "$LMCACHE_COMMIT" "$WORK_DIR/lmcache" need_tags
clone_at "$B12X_REPO" "$B12X_REF" "$B12X_COMMIT" "$WORK_DIR/b12x"
clone_at "$BUILD_SYS_REPO" "$BUILD_SYS_COMMIT" "$BUILD_SYS_COMMIT" "$WORK_DIR/spark-vllm-docker"

# --- stage 2: prepare -------------------------------------------------------
step prepare "point eugr preset at the forks; no-op folded-in patches"
BUILD_SH="$WORK_DIR/spark-vllm-docker/build-and-copy.sh"
retarget() { # <file> <old> <new> — exact one-line replacement, verified; idempotent
  local file="$1" old="$2" new="$3"
  if $DRY_RUN; then printf 'DRY-RUN retarget %s: %s -> %s\n' "$file" "$old" "$new"; return; fi
  if ! grep -qF "$old" "$file"; then
    if grep -qF "$new" "$file"; then
      echo "build-fork: $file already retargeted; skipping"
      return
    fi
    echo "build-fork: expected line not found in $file: $old" >&2; exit 1
  fi
  python3 - "$file" "$old" "$new" <<'PY'
import sys
path, old, new = sys.argv[1], sys.argv[2], sys.argv[3]
text = open(path).read()
assert text.count(old) == 1, f"{path}: expected exactly one occurrence of {old!r}"
open(path, "w").write(text.replace(old, new, 1))
PY
  grep -qF "$new" "$file" || { echo "build-fork: replacement failed in $file" >&2; exit 1; }
}

retarget "$BUILD_SH" 'EXP_B12X_VLLM_REPO="https://github.com/local-inference-lab/vllm"' "EXP_B12X_VLLM_REPO=\"$OVERRIDE_VLLM_REPO\""
retarget "$BUILD_SH" 'EXP_B12X_VLLM_REF="dev/infernal-invocation"' "EXP_B12X_VLLM_REF=\"$OVERRIDE_VLLM_REF\""
retarget "$BUILD_SH" 'B12X_PACKAGE_REPO="https://github.com/lukealonso/b12x.git"' "B12X_PACKAGE_REPO=\"$OVERRIDE_B12X_REPO\""
retarget "$BUILD_SH" 'B12X_PACKAGE_REF="master"' "B12X_PACKAGE_REF=\"$OVERRIDE_B12X_REF\""

for patch in $FOLDED_PATCHES; do
  target="$WORK_DIR/spark-vllm-docker/$patch"
  if $DRY_RUN; then printf 'DRY-RUN no-op %s (folded into fork branch)\n' "$target"; continue; fi
  [[ -f "$target" ]] || { echo "build-fork: folded patch missing from build system checkout: $target" >&2; exit 1; }
  cat > "$target" <<'EOF'
#!/usr/bin/env python3
"""No-op stub: this build-time patch is folded into the vllm fork branch
(image.fork.lock.json, folded_in_patches). Applying it again would fail on
anchor mismatch; the source already carries the exact production bytes."""
print("SKIP: patch folded into fork source; nothing to apply")
EOF
  grep -q "SKIP: patch folded into fork source" "$target" || { echo "build-fork: stub write failed for $target" >&2; exit 1; }
done

# ARG declarations must live INSIDE each build stage: Docker only expands an
# ARG in a stage's RUN environment when it is declared after that stage's
# FROM. A declaration before the first FROM is global-only and invisible to
# RUN (observed: mirror/proxy ARGs before the first FROM had no effect, pip
# kept hitting the default index). Inject after every FROM, and pass the
# proxy values explicitly via --build-arg because this docker's BuildKit does
# not auto-forward host proxy variables (observed: RUN env shows no proxy).
# Idempotent: injected lines are stripped first, then re-added.
DOCKERFILE="$WORK_DIR/spark-vllm-docker/Dockerfile"
inject_args() {
  python3 - "$DOCKERFILE" "$PYPI_MIRROR" <<'PY'
import sys
path, mirror = sys.argv[1], sys.argv[2]
managed = ("ARG HTTP_PROXY", "ARG HTTPS_PROXY", "ARG http_proxy",
           "ARG https_proxy", "ARG PIP_INDEX_URL", "ARG UV_INDEX_URL")
lines = [l for l in open(path).read().splitlines(keepends=True)
         if not l.startswith(managed)]
block = "ARG HTTP_PROXY\nARG HTTPS_PROXY\nARG http_proxy\nARG https_proxy\n"
if mirror:
    block += f"ARG PIP_INDEX_URL={mirror}\nARG UV_INDEX_URL={mirror}\n"
out, injections = [], 0
for line in lines:
    out.append(line)
    if line.startswith("FROM "):
        out.append(block)
        injections += 1
if injections == 0:
    raise SystemExit(f"{path}: no FROM anchor found for ARG injection")
open(path, "w").write("".join(out))
print(f"injected ARGs after {injections} FROM lines")
PY
}
if $DRY_RUN; then
  printf 'DRY-RUN inject per-stage ARG declarations (proxy%s) into %s\n' \
    "${PYPI_MIRROR:+, mirror=$PYPI_MIRROR}" "$DOCKERFILE"
else
  inject_args
  grep -c '^ARG PIP_INDEX_URL' "$DOCKERFILE" || true
fi

# Pass proxy values explicitly: BuildKit on this host does not auto-forward.
if $DRY_RUN; then
  printf 'DRY-RUN append explicit proxy --build-arg flags to COMMON_BUILD_FLAGS\n'
else
  if ! grep -q 'build-arg" "\$PROXY_BUILD_ARG' "$BUILD_SH"; then
    python3 - "$BUILD_SH" <<'PY'
import sys
path = sys.argv[1]
text = open(path).read()
anchor = 'if [ -n "$NETWORK_ARG" ]; then\n    COMMON_BUILD_FLAGS+=("--network" "$NETWORK_ARG")\nfi'
assert anchor in text, f"{path}: COMMON_BUILD_FLAGS network anchor not found"
addition = anchor + '''
for PROXY_BUILD_ARG in HTTP_PROXY HTTPS_PROXY http_proxy https_proxy; do
    if [ -n "${!PROXY_BUILD_ARG:-}" ]; then
        COMMON_BUILD_FLAGS+=("--build-arg" "$PROXY_BUILD_ARG=${!PROXY_BUILD_ARG}")
    fi
done'''
open(path, "w").write(text.replace(anchor, addition, 1))
PY
    grep -q 'PROXY_BUILD_ARG' "$BUILD_SH" || { echo "build-fork: build-arg injection failed" >&2; exit 1; }
  fi
fi

# --- stage 3: vllm + b12x base image (multi-hour) ---------------------------
step vllm "build vllm+b12x base image $IMAGE_TAG (multi-hour)"
# --rebuild-vllm is mandatory, not optional: without it the eugr preset pulls
# its prebuilt runner image (someone else's vllm+b12x) and never compiles the
# fork sources — the exact failure this script exists to prevent.
BUILD_ARGS=(--rebuild-vllm)
if [[ -n "$PROXY" ]]; then BUILD_ARGS+=(--network host); fi
if [[ -n "$BUILD_JOBS" ]]; then BUILD_ARGS+=(--build-jobs "$BUILD_JOBS"); fi
if $DRY_RUN; then
  printf 'DRY-RUN (cd %s && %s -t %s %s)\n' "$WORK_DIR/spark-vllm-docker" "$BUILD_COMMAND" "$IMAGE_TAG" "${BUILD_ARGS[*]:-}"
else
  (cd "$WORK_DIR/spark-vllm-docker" && $BUILD_COMMAND -t "$IMAGE_TAG" "${BUILD_ARGS[@]}")
fi
# --- stage 4: lmcache fork wheel --------------------------------------------
step lmcache "build lmcache fork wheel inside the base image"
if $DRY_RUN; then
  printf 'DRY-RUN docker run --rm -v <lmcache>:/src %s pip wheel lmcache fork\n' "$IMAGE_TAG"
else
  mkdir -p "$WORK_DIR/lmcache-wheel"
  WHEEL_NET=()
  if [[ -n "$PROXY" ]]; then
    WHEEL_NET=(--network host
      -e "https_proxy=$PROXY" -e "http_proxy=$PROXY"
      -e "HTTPS_PROXY=$PROXY" -e "HTTP_PROXY=$PROXY")
  fi
  docker run --rm "${WHEEL_NET[@]}" \
    -v "$WORK_DIR/lmcache":/src:ro \
    -v "$WORK_DIR/lmcache-wheel":/out \
    "$IMAGE_TAG" bash -c '
      set -euo pipefail
      cp -r /src /tmp/lmcache-src
      pip wheel --no-deps --wheel-dir /out /tmp/lmcache-src >/dev/null
      ls /out/*.whl
    '
  ls "$WORK_DIR"/lmcache-wheel/*.whl >/dev/null
fi

# --- stage 5: final image with provenance labels -----------------------------
step final "install wheel and stamp labels -> $FINAL_TAG"
FINAL_DIR="$WORK_DIR/final-image"
if $DRY_RUN; then
  printf 'DRY-RUN generate final Containerfile (FROM %s + wheel + 8 labels) and docker build -t %s\n' "$IMAGE_TAG" "$FINAL_TAG"
else
  rm -rf "$FINAL_DIR" && mkdir -p "$FINAL_DIR"
  cp "$WORK_DIR"/lmcache-wheel/*.whl "$FINAL_DIR/"
  cat > "$FINAL_DIR/Containerfile" <<EOF
FROM $IMAGE_TAG
ARG VLLM_COMMIT
ARG B12X_COMMIT
ARG LMCACHE_COMMIT
ARG SOURCE_REVISION
ARG PROFILE_SHA256=""
ARG SERVICE_CONTRACT_SHA256=""
ARG IMAGE_LOCK_SHA256=""
LABEL org.opencontainers.image.title="DGX Spark DeepSeek V4 service"
LABEL org.opencontainers.image.revision="\${SOURCE_REVISION}"
LABEL com.dgx-spark.architecture="linux/arm64"
LABEL com.dgx-spark.profile_sha256="\${PROFILE_SHA256}"
LABEL com.dgx-spark.service_contract_sha256="\${SERVICE_CONTRACT_SHA256}"
LABEL com.dgx-spark.image_lock_sha256="\${IMAGE_LOCK_SHA256}"
LABEL com.dgx-spark.vllm.commit="\${VLLM_COMMIT}"
LABEL com.dgx-spark.b12x.commit="\${B12X_COMMIT}"
LABEL com.dgx-spark.lmcache.commit="\${LMCACHE_COMMIT}"
COPY *.whl /tmp/lmcache-wheel/
RUN pip install --no-deps /tmp/lmcache-wheel/*.whl && rm -rf /tmp/lmcache-wheel \
 && python3 -c "import lmcache, importlib.metadata as m; print('lmcache', m.version('lmcache'))"
EOF
  docker build --pull=false --platform linux/arm64 \
    --file "$FINAL_DIR/Containerfile" \
    --build-arg "VLLM_COMMIT=$VLLM_COMMIT" \
    --build-arg "B12X_COMMIT=$B12X_COMMIT" \
    --build-arg "LMCACHE_COMMIT=$LMCACHE_COMMIT" \
    --build-arg "SOURCE_REVISION=$VLLM_COMMIT" \
    --build-arg "PROFILE_SHA256=$PROFILE_SHA256" \
    --build-arg "SERVICE_CONTRACT_SHA256=$SERVICE_CONTRACT_SHA256" \
    --build-arg "IMAGE_LOCK_SHA256=$IMAGE_LOCK_SHA256" \
    --tag "$FINAL_TAG" "$FINAL_DIR"
fi

# --- stage 6: summary ---------------------------------------------------------
step summary "image identity and lock template"
if $DRY_RUN; then
  printf 'DRY-RUN docker image inspect %s (id, arch, labels) and write deployment-lock template\n' "$FINAL_TAG"
  echo "dry-run complete; no clone, patch, or docker mutation performed"
  exit 0
fi
IMAGE_ID="$(docker image inspect "$FINAL_TAG" --format '{{.Id}}')"
echo "final image: $FINAL_TAG"
echo "image id:    $IMAGE_ID"
docker image inspect "$FINAL_TAG" --format 'labels: {{json .Config.Labels}}'
if [[ -z "$PROFILE_SHA256" || -z "$SERVICE_CONTRACT_SHA256" || -z "$IMAGE_LOCK_SHA256" ]]; then
  cat >&2 <<'EOF'
WARNING: one or more deploy-side label digests were empty. Before this image
can enter a ready deployment lock, stamp the real values with the
metadata-only child flow in docs/image.md (profile_sha256,
service_contract_sha256, image_lock_sha256).
EOF
fi
