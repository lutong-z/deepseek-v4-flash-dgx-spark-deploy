#!/usr/bin/env bash
set -euo pipefail
ACTION="${1:---preflight}"
ROLE=head
EXPECTED_HOST=spark-acd0
CONTAINER=dsv4-native432-lmcache-head
IMAGE_ID=sha256:52d606ea024b7dd3d38960f98ce546c389796383d2a575a82fde13264ba2190e
MODEL_ROOT=/home/lutongzzz/models/DeepSeek-V4-Flash-0731
MODEL_LOCK_SHA256=523d07e082f034ecf2ba7833d115ddd77eddabd3f0c93a2081c6dacc5513e5cb
CONTRACT_PATH=/home/lutongzzz/.cache/dgx-spark-tests/nonflash-327k-rollout-20260902T022533Z/deployment/repo-managed-production/contracts/production-head.json
ENV_PATH=/home/lutongzzz/.cache/dgx-spark-tests/nonflash-327k-rollout-20260902T022533Z/deployment/repo-managed-production/contracts/production-head.env
FABRIC_IF=enp1s0f1np1
FABRIC_HCA=rocep1s0f1
FABRIC_IP=192.168.100.10
CAPTURED_GID=3

fail() { printf 'ERROR[%s]: %s\n' "$ROLE" "$*" >&2; exit 1; }
[[ "$(hostname)" == "$EXPECTED_HOST" ]] || fail "expected host $EXPECTED_HOST, got $(hostname)"
docker image inspect "$IMAGE_ID" >/dev/null 2>&1 || fail "missing rollback image $IMAGE_ID"
[[ -d "$MODEL_ROOT" ]] || fail "missing model root $MODEL_ROOT"
[[ "$(cat "$MODEL_ROOT/.model-lock.sha256")" == "$MODEL_LOCK_SHA256" ]] || fail "model lock marker mismatch"
ip -4 address show dev "$FABRIC_IF" | grep -Fq "$FABRIC_IP/24" || fail "fabric address $FABRIC_IP/24 absent from $FABRIC_IF"
[[ "$(cat "/sys/class/net/$FABRIC_IF/mtu")" == 9000 ]] || fail "fabric MTU is not 9000"
DISCOVERED_GID="$(show_gids | awk -v h="$FABRIC_HCA" -v ip="$FABRIC_IP" '$1 == h && $5 == ip && $6 == "v2" && !found {print $3; found=1}')"
restart_preserved() {
  if [[ "$(docker container inspect --format '{{.State.Running}}' "$CONTAINER")" == true ]]; then
    docker container stop -t 30 "$CONTAINER" >/dev/null
  fi
  docker container start "$CONTAINER" >/dev/null
}
[[ -n "$DISCOVERED_GID" ]] || fail "cannot auto-discover IPv4 RoCE-v2 GID"
printf '%s preflight: image=%s model_lock=%s fabric=%s/%s captured_gid=%s discovered_gid=%s\n' "$ROLE" "$IMAGE_ID" "$MODEL_LOCK_SHA256" "$FABRIC_HCA" "$FABRIC_IF" "$CAPTURED_GID" "$DISCOVERED_GID"
[[ "$ACTION" == "--apply" ]] || exit 0

GID_MODE="${GID_MODE:-auto}"
case "$GID_MODE" in
  auto) RESTORE_GID="$DISCOVERED_GID" ;;
  captured) RESTORE_GID="$CAPTURED_GID" ;;
  *) fail "GID_MODE must be auto or captured" ;;
esac

if docker container inspect "$CONTAINER" >/dev/null 2>&1; then
  EXISTING_IMAGE="$(docker container inspect --format '{{.Image}}' "$CONTAINER")"
  [[ "$EXISTING_IMAGE" == "$IMAGE_ID" ]] || fail "existing $CONTAINER uses $EXISTING_IMAGE, expected $IMAGE_ID"
  if [[ "$GID_MODE" == captured ]]; then
    restart_preserved
    printf '%s restored by starting preserved exact container %s (captured GID %s)\n' "$ROLE" "$CONTAINER" "$CAPTURED_GID"
    exit 0
  fi
  EXISTING_GID="$(docker container inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$CONTAINER" | sed -n 's/^NCCL_IB_GID_INDEX=//p')"
  if [[ "$EXISTING_GID" == "$RESTORE_GID" ]]; then
    restart_preserved
    printf '%s restored by starting preserved container %s (auto GID %s)\n' "$ROLE" "$CONTAINER" "$RESTORE_GID"
    exit 0
  fi
  [[ "${ALLOW_RECREATE_FOR_GID_DRIFT:-0}" == 1 ]] || fail "preserved container GID $EXISTING_GID differs from auto GID $RESTORE_GID; set GID_MODE=captured for exact replay or ALLOW_RECREATE_FOR_GID_DRIFT=1"
  docker container stop -t 30 "$CONTAINER" >/dev/null 2>&1 || true
  SAVED_NAME="${CONTAINER}-captured-$(date -u +%Y%m%dT%H%M%SZ)"
  docker container rename "$CONTAINER" "$SAVED_NAME"
  printf '%s preserved prior container as %s before auto-GID recreation\n' "$ROLE" "$SAVED_NAME"
fi
[[ -r "$CONTRACT_PATH" ]] || fail "missing captured contract $CONTRACT_PATH"
[[ -r "$ENV_PATH" ]] || fail "missing captured env $ENV_PATH"
CREATE=(
  docker
  container
  create
  --name
  dsv4-native432-lmcache-head
  --hostname
  dsv4-native432-lmcache-head
  --network
  host
  --ipc
  host
  --gpus
  all
  --shm-size
  68719476736
  --workdir
  /workspace/vllm
  --ulimit
  memlock=-1
  --ulimit
  nofile=1048576:1048576
  --label
  com.dgx-spark.deployment_id=deployment-077e240867f1c2d7
  --label
  com.dgx-spark.role=head
  --privileged
  --security-opt
  label=disable
  --label
  org.opencontainers.image.revision=3efb51d631aa96623b45edc23548f4d48bb98d33
  --label
  com.dgx-spark.architecture=linux/arm64
  --label
  com.dgx-spark.vllm.commit=4646721870943ee38f221036797f0995ca45a1a9
  --label
  com.dgx-spark.b12x.commit=d476465883cc7e46c128e0effa89fad1a7200cd7
  --label
  com.dgx-spark.runtime-file-sha256=0e215ba1a214ab46bdb9d293a9fa2716e63882513e599837e634aea610ac66f0
  --env
  B12X_MLA_SM120_UNIFIED=1
  --env
  B12X_MOE_FORCE_A8=1
  --env
  CUDA_MODULE_LOADING=LAZY
  --env
  CUDA_VISIBLE_DEVICES=0
  --env
  CUTE_DSL_ARCH=sm_121a
  --env
  DGX_SPARK_FABRIC_CIDR=192.168.100.10/24
  --env
  DGX_SPARK_FABRIC_PEER=192.168.100.11
  --env
  DGX_SPARK_FABRIC_PROFILE=f1
  --env
  DG_JIT_USE_NVRTC=0
  --env
  FLASHINFER_DISABLE_VERSION_CHECK=1
  --env
  GLOO_SOCKET_IFNAME=enp1s0f1np1
  --env
  HF_HUB_OFFLINE=1
  --env
  KV_FP8_ROPE=0
  --env
  NCCL_CROSS_NIC=1
  --env
  NCCL_CUMEM_ENABLE=0
  --env
  NCCL_IB_ADDR_FAMILY=AF_INET
  --env
  NCCL_IB_DISABLE=0
  --env
  NCCL_IB_GID_INDEX=3
  --env
  NCCL_IB_HCA=rocep1s0f1
  --env
  NCCL_IB_MTU=9000
  --env
  NCCL_IB_ROCE_VERSION_NUM=2
  --env
  NCCL_IGNORE_CPU_AFFINITY=1
  --env
  NCCL_NET=IB
  --env
  NCCL_NVLS_ENABLE=0
  --env
  NCCL_SOCKET_IFNAME=enp1s0f1np1
  --env
  PYTHONUNBUFFERED=1
  --env
  TORCH_CUDA_ARCH_LIST=12.1a
  --env
  TP_SOCKET_IFNAME=enp1s0f1np1
  --env
  TRANSFORMERS_OFFLINE=1
  --env
  USE_CUDNN=1
  --env
  VLLM_ALLOW_LONG_MAX_MODEL_LEN=1
  --env
  VLLM_DSPARK_CAPTURE_SHARDED_MARKOV=0
  --env
  VLLM_DSPARK_DYNAMIC_DRAFT_DEPTH=0
  --env
  VLLM_DSPARK_SPS_DEBUG=0
  --env
  VLLM_ENGINE_READY_TIMEOUT_S=3600
  --env
  VLLM_FORCE_AOT_LOAD=0
  --env
  VLLM_HOST_IP=192.168.100.10
  --env
  VLLM_MEMORY_PROFILE_INCLUDE_ATTN=1
  --env
  VLLM_MLA_SM120_UNIFIED=1
  --env
  VLLM_MOE_FORCE_A8=1
  --env
  VLLM_MOE_SKIP_PADDING=0
  --env
  VLLM_NVFP4_MLA_DYNAMIC_SCALE=0
  --env
  VLLM_RPC_TIMEOUT=600000
  --env
  VLLM_USE_AOT_COMPILE=0
  --env
  VLLM_USE_B12X_FP8_GEMM=1
  --env
  VLLM_USE_B12X_MHC=1
  --env
  VLLM_USE_B12X_MOE=1
  --env
  VLLM_USE_B12X_SPARSE_INDEXER=1
  --env
  VLLM_USE_B12X_WO_PROJECTION=1
  --env
  VLLM_USE_FLASHINFER_SAMPLER=1
  --env
  VLLM_USE_MEGA_AOT_ARTIFACT=0
  --env
  VLLM_USE_V2_MODEL_RUNNER=1
  --env-file
  /home/lutongzzz/.cache/dgx-spark-tests/nonflash-327k-rollout-20260902T022533Z/deployment/repo-managed-production/contracts/production-head.env
  --mount
  type=bind,src=/home/lutongzzz/models/DeepSeek-V4-Flash-0731,dst=/models/DeepSeek-V4-Flash-0731,readonly
  --mount
  type=bind,src=/home/lutongzzz/.cache/dgx-spark-tests/nonflash-327k-rollout-20260902T022533Z/cache/dgx0/vllm,dst=/root/.cache/vllm
  --mount
  type=bind,src=/home/lutongzzz/.cache/dgx-spark-tests/nonflash-327k-rollout-20260902T022533Z/cache/dgx0/b12x,dst=/root/.cache/b12x
  --mount
  type=bind,src=/home/lutongzzz/.cache/dgx-spark-tests/nonflash-327k-rollout-20260902T022533Z/cache/dgx0/flashinfer,dst=/root/.cache/flashinfer
  --mount
  type=bind,src=/home/lutongzzz/.cache/dgx-spark-tests/nonflash-327k-rollout-20260902T022533Z/cache/dgx0/triton,dst=/root/.triton
  --mount
  type=bind,src=/home/lutongzzz/.cache/dgx-spark-tests/nonflash-327k-rollout-20260902T022533Z/cache/dgx0/tilelang,dst=/root/.tilelang
  --mount
  type=bind,src=/home/lutongzzz/.cache/dgx-spark-tests/nonflash-327k-rollout-20260902T022533Z/deployment/repo-managed-production/contracts/production-head.json,dst=/etc/dgx-spark/service-contract.json,readonly
  "$IMAGE_ID"
  vllm
  serve
  /models/DeepSeek-V4-Flash-0731
  --served-model-name
  deepseek-v4-flash-0731
  --trust-remote-code
  --tensor-parallel-size
  2
  --pipeline-parallel-size
  1
  --distributed-executor-backend
  mp
  --nnodes
  2
  --node-rank
  0
  --host
  0.0.0.0
  --port
  8101
  --master-addr
  192.168.100.10
  --master-port
  29619
  --kv-cache-dtype
  nvfp4_ds_mla
  --block-size
  256
  --max-model-len
  327680
  --max-num-seqs
  8
  --max-num-batched-tokens
  1024
  --gpu-memory-utilization
  0.85
  --enable-prefix-caching
  --prefix-caching-hash-algo
  sha256
  --prefix-cache-idle-timeout-seconds
  300
  --no-async-scheduling
  --enable-chunked-prefill
  --long-prefill-token-threshold
  0
  --tokenizer-mode
  deepseek_v4
  --tool-call-parser
  deepseek_v4
  --enable-auto-tool-choice
  --reasoning-parser
  deepseek_v4
  --reasoning-config
  '{"reasoning_end_str":"","reasoning_parser":"deepseek_v4","reasoning_start_str":""}'
  --default-chat-template-kwargs.thinking=true
  --default-chat-template-kwargs.reasoning_effort=high
  --load-format
  instanttensor
  --moe-backend
  b12x
  --linear-backend
  b12x
  --attention-backend
  B12X_MLA_SPARSE
  --speculative-config
  '{"attention_backend":"B12X_MLA_SPARSE","draft_sample_method":"probabilistic","kv_cache_dtype":"fp8","method":"dspark","num_speculative_tokens":5}'
  --max-cudagraph-capture-size
  64
  --compilation-config
  '{"cudagraph_mode":"FULL_AND_PIECEWISE","custom_ops":["all"]}'
  --kv-transfer-config
  '{"kv_connector":"Native432LMCacheMPConnector","kv_connector_module_path":"lmcache.integration.vllm.native432_mp_connector","kv_role":"kv_both","kv_load_failure_policy":"recompute","kv_connector_extra_config":{"lmcache.mp.server_urls":"tcp://192.168.100.10:6667,tcp://192.168.100.11:6667","lmcache.mp.mq_timeout":30,"lmcache.mp.heartbeat_interval":1}}'
)
for i in "${!CREATE[@]}"; do
  if [[ "${CREATE[$i]}" == NCCL_IB_GID_INDEX=* ]]; then CREATE[$i]="NCCL_IB_GID_INDEX=$RESTORE_GID"; fi
done
"${CREATE[@]}" >/dev/null
docker container start "$CONTAINER" >/dev/null
printf '%s recreated and started %s image=%s gid=%s\n' "$ROLE" "$CONTAINER" "$IMAGE_ID" "$RESTORE_GID"
