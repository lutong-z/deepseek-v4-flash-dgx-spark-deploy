"""Render deterministic, executable role contracts without executing them."""

from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath
from typing import Any, Mapping

from .config import DEFAULT_MODEL_CONTAINER_PATH, canonical_json, config_sha256
from .manifest import deployment_id
from .redact import redact_mapping


class RenderError(ValueError):
    """The fixed profile cannot be rendered safely."""


_ROLES = ("worker", "head")
_REQUIRED_LABELS = (
    "org.opencontainers.image.revision",
    "com.dgx-spark.architecture",
    "com.dgx-spark.profile_sha256",
    "com.dgx-spark.service_contract_sha256",
    "com.dgx-spark.image_lock_sha256",
    "com.dgx-spark.vllm.commit",
    "com.dgx-spark.b12x.commit",
)
_RUNTIME_ENV = {
    "NCCL_CROSS_NIC": "1",
    "NCCL_NET": "IB",
    "NCCL_IB_ADDR_FAMILY": "AF_INET",
    "NCCL_IB_DISABLE": "0",
    "NCCL_IB_ROCE_VERSION_NUM": "2",
    "VLLM_RPC_TIMEOUT": "600000",
    "VLLM_ENGINE_READY_TIMEOUT_S": "3600",
    "VLLM_ALLOW_LONG_MAX_MODEL_LEN": "1",
    "VLLM_MEMORY_PROFILE_INCLUDE_ATTN": "1",
    "VLLM_USE_B12X_MOE": "1",
    "VLLM_USE_B12X_SPARSE_INDEXER": "1",
    "VLLM_USE_B12X_FP8_GEMM": "1",
    "VLLM_USE_B12X_MHC": "1",
    "VLLM_USE_B12X_WO_PROJECTION": "1",
    "VLLM_USE_V2_MODEL_RUNNER": "1",
    "VLLM_USE_MEGA_AOT_ARTIFACT": "0",
    "VLLM_USE_AOT_COMPILE": "0",
    "VLLM_FORCE_AOT_LOAD": "0",
    "VLLM_NVFP4_MLA_DYNAMIC_SCALE": "0",
    "VLLM_MOE_SKIP_PADDING": "0",
    "VLLM_MOE_FORCE_A8": "1",
    "B12X_MOE_FORCE_A8": "1",
    "VLLM_MLA_SM120_UNIFIED": "1",
    "B12X_MLA_SM120_UNIFIED": "1",
    "VLLM_USE_FLASHINFER_SAMPLER": "1",
    "FLASHINFER_DISABLE_VERSION_CHECK": "1",
    "NCCL_CUMEM_ENABLE": "0",
    "NCCL_NVLS_ENABLE": "0",
    "NCCL_IGNORE_CPU_AFFINITY": "1",
    "VLLM_DSPARK_DYNAMIC_DRAFT_DEPTH": "0",
    "VLLM_DSPARK_SPS_DEBUG": "0",
    "VLLM_DSPARK_CAPTURE_SHARDED_MARKOV": "0",
    "KV_FP8_ROPE": "0",
    "CUDA_MODULE_LOADING": "LAZY",
    "TORCH_CUDA_ARCH_LIST": "12.1a",
    "CUTE_DSL_ARCH": "sm_121a",
    "DG_JIT_USE_NVRTC": "0",
    "USE_CUDNN": "1",
    "HF_HUB_OFFLINE": "1",
    "TRANSFORMERS_OFFLINE": "1",
    "PYTHONUNBUFFERED": "1",
}


def _deployment(config: Mapping[str, Any]) -> Mapping[str, Any]:
    value = config.get("deployment")
    if not isinstance(value, Mapping):
        raise RenderError("deployment section is missing")
    return value


def _profile(config: Mapping[str, Any]) -> Mapping[str, Any]:
    value = config.get("profile")
    if not isinstance(value, Mapping):
        raise RenderError("profile section is missing")
    return value


def _role_value(deployment: Mapping[str, Any], role: str, suffix: str) -> str:
    if role not in {"head", "worker"}:
        raise RenderError(f"unsupported role: {role}")
    key = f"{role}_{suffix}"
    if key not in deployment:
        raise RenderError(f"deployment is missing {key}")
    return str(deployment[key])


def _mode(config: Mapping[str, Any]) -> str:
    mode = str(_deployment(config).get("mode", "generic"))
    if mode not in {"generic", "production", "candidate"}:
        raise RenderError(f"unsupported deployment mode: {mode}")
    return mode


def _container_name(config: Mapping[str, Any], role: str) -> str:
    mode = _mode(config)
    if mode == "production":
        return f"dsv4-native432-dspark5-327k-seq5-{role}"
    if mode == "candidate":
        return f"dsv4-candidate-native432-dspark5-327k-seq5-{role}"
    profile = _profile(config)
    return f"{profile['container']['name_prefix']}{role}"


def _model_path(config: Mapping[str, Any]) -> str:
    deployment = _deployment(config)
    profile_model = _profile(config)["model"]
    expected = str(profile_model.get("model_path", DEFAULT_MODEL_CONTAINER_PATH))
    configured = str(deployment.get("model_container_path", expected))
    if configured != expected:
        raise RenderError("model container path differs from reviewed profile")
    return expected


def _remote_contract_path(config: Mapping[str, Any], role: str) -> str:
    deployment = _deployment(config)
    mode = _mode(config)
    root = PurePosixPath(str(deployment["remote_root"]))
    return str(root / "contracts" / f"{mode}-{role}.json")


def _remote_env_path(config: Mapping[str, Any], role: str) -> str:
    deployment = _deployment(config)
    mode = _mode(config)
    root = PurePosixPath(str(deployment["remote_root"]))
    return str(root / "contracts" / f"{mode}-{role}.env")


def _cache_root(config: Mapping[str, Any], role: str) -> str:
    deployment = _deployment(config)
    rank = 0 if role == "head" else 1
    return str(PurePosixPath(str(deployment["cache_root"])) / f"dgx{rank}")


def render_environment(config: Mapping[str, Any], role: str) -> dict[str, str]:
    """Render the reviewed RDMA/GPU/runtime environment for one role."""

    deployment = _deployment(config)
    result = dict(_RUNTIME_ENV)
    result.update(
        {
            "VLLM_HOST_IP": _role_value(deployment, role, "node_addr"),
            "NCCL_SOCKET_IFNAME": _role_value(deployment, role, "net_iface"),
            "GLOO_SOCKET_IFNAME": _role_value(deployment, role, "net_iface"),
            "TP_SOCKET_IFNAME": _role_value(deployment, role, "net_iface"),
            "NCCL_IB_HCA": _role_value(deployment, role, "hca"),
            "CUDA_VISIBLE_DEVICES": _role_value(deployment, role, "cuda_visible_devices"),
        }
    )
    gid = deployment.get("roce_gid_index")
    if gid is not None:
        result["NCCL_IB_GID_INDEX"] = str(gid)
    result["NCCL_IB_MTU"] = str(deployment["roce_mtu"])
    return dict(sorted(result.items()))


def render_service_argv(config: Mapping[str, Any], role: str) -> list[str]:
    """Render the reviewed DeepSeek V4 service command as tokenized argv."""

    deployment = _deployment(config)
    profile = _profile(config)
    topology = profile["topology"]
    model = profile["model"]
    service = profile["service"]
    limits = profile["limits"]
    parsing = profile["parsing"]
    if role not in {"head", "worker"}:
        raise RenderError(f"unsupported role: {role}")
    rank = int(topology["head_rank"] if role == "head" else topology["worker_rank"])
    reasoning_config = json.dumps(
        json.loads(str(service["reasoning_config_json"])),
        sort_keys=True,
        separators=(",", ":"),
    )
    draft = json.loads(str(service["draft_config_json"]))
    draft_json = json.dumps(draft, sort_keys=True, separators=(",", ":"))
    compilation = json.dumps(
        json.loads(str(service["compilation_config_json"])),
        sort_keys=True,
        separators=(",", ":"),
    )
    argv = [
        "vllm",
        "serve",
        _model_path(config),
        "--served-model-name",
        str(model["served_model_name"]),
        "--trust-remote-code",
        "--tensor-parallel-size",
        str(topology["tensor_parallel_size"]),
        "--pipeline-parallel-size",
        str(topology["pipeline_parallel_size"]),
        "--distributed-executor-backend",
        str(service["distributed_executor_backend"]),
        "--nnodes",
        str(topology["nnodes"]),
        "--node-rank",
        str(rank),
    ]
    if role == "head":
        argv.extend(["--host", str(deployment["api_bind_addr"]), "--port", str(deployment["api_port"])])
    else:
        argv.append("--headless")
    argv.extend(
        [
            "--master-addr",
            str(deployment["master_addr"]),
            "--master-port",
            str(deployment["master_port"]),
            "--kv-cache-dtype",
            str(service["target_kv_cache_dtype"]),
            "--block-size",
            str(service["block_size"]),
            "--max-model-len",
            str(limits["max_model_len"]),
            "--max-num-seqs",
            str(limits["max_num_seqs"]),
            "--max-num-batched-tokens",
            str(limits["max_num_batched_tokens"]),
            "--gpu-memory-utilization",
            str(service["gpu_memory_utilization"]),
            "--enable-prefix-caching",
            "--prefix-caching-hash-algo",
            str(service["prefix_caching"]["hash_algorithm"]),
            "--no-async-scheduling",
            "--enable-chunked-prefill",
            "--long-prefill-token-threshold",
            str(service["long_prefill_token_threshold"]),
            "--tokenizer-mode",
            str(parsing["tokenizer_mode"]),
            "--tool-call-parser",
            str(parsing["tool_call_parser"]),
            "--enable-auto-tool-choice",
            "--reasoning-parser",
            str(parsing["reasoning_parser"]),
            "--reasoning-config",
            reasoning_config,
            "--default-chat-template-kwargs.thinking=true",
            "--default-chat-template-kwargs.reasoning_effort=high",
            "--load-format",
            str(service["load_format"]),
            "--moe-backend",
            str(service["moe_backend"]),
            "--linear-backend",
            str(service["linear_backend"]),
            "--attention-backend",
            str(service["attention_backend"]),
            "--speculative-config",
            draft_json,
            "--max-cudagraph-capture-size",
            str(service["max_cudagraph_capture_size"]),
            "--compilation-config",
            compilation,
        ]
    )
    return argv


def _lock_image(lock: Mapping[str, Any] | None, role: str, config: Mapping[str, Any]) -> tuple[str, str | None, dict[str, str]]:
    deployment = _deployment(config)
    fallback = str(deployment[f"{role}_image_ref"])
    if lock is None:
        labels = {
            "org.opencontainers.image.revision": "unlocked",
            "com.dgx-spark.architecture": "linux/arm64",
            "com.dgx-spark.profile_sha256": config_sha256({"profile": _profile(config)}),
            "com.dgx-spark.service_contract_sha256": "unlocked",
            "com.dgx-spark.image_lock_sha256": "unlocked",
            "com.dgx-spark.vllm.commit": "unlocked",
            "com.dgx-spark.b12x.commit": "unlocked",
        }
        return fallback, None, labels
    images = lock.get("images")
    if not isinstance(images, Mapping) or not isinstance(images.get(role), Mapping):
        raise RenderError(f"deployment lock has no {role} image")
    image = images[role]
    return str(image["reference"]), str(image["image_id"]), {str(k): str(v) for k, v in image["labels"].items()}


def render_container_argv(
    config: Mapping[str, Any],
    role: str,
    lock: Mapping[str, Any] | None = None,
    *,
    contract_source: str | None = None,
) -> list[str]:
    """Render a safe docker-create argv; never execute it."""

    deployment = _deployment(config)
    profile = _profile(config)
    image_ref, _, labels = _lock_image(lock, role, config)
    name = _container_name(config, role)
    contract_source = contract_source or _remote_contract_path(config, role)
    env_source = _remote_env_path(config, role)
    cache = _cache_root(config, role)
    argv = [
        "docker",
        "container",
        "create",
        "--name",
        name,
        "--hostname",
        name,
        "--network",
        "host",
        "--ipc",
        "host",
        "--gpus",
        "all",
        "--shm-size",
        "68719476736",
        "--workdir",
        "/workspace/vllm",
        "--ulimit",
        "memlock=-1",
        "--ulimit",
        "nofile=1048576:1048576",
        "--label",
        f"com.dgx-spark.deployment_id={deployment_id(config)}",
        "--label",
        f"com.dgx-spark.role={role}",
    ]
    if _mode(config) in {"production", "candidate"}:
        argv.extend(["--privileged", "--security-opt", "label=disable"])
    else:
        argv.extend(["--read-only", "--security-opt", "no-new-privileges:true"])
    for key in _REQUIRED_LABELS:
        argv.extend(["--label", f"{key}={labels[key]}"])
    for key, value in render_environment(config, role).items():
        argv.extend(["--env", f"{key}={value}"])
    argv.extend(
        [
            "--env-file",
            env_source,
            "--mount",
            f"type=bind,src={deployment['model_root']},dst=/models,readonly",
        ]
    )
    for suffix, destination in (
        ("vllm", "/root/.cache/vllm"),
        ("b12x", "/root/.cache/b12x"),
        ("flashinfer", "/root/.cache/flashinfer"),
        ("triton", "/root/.triton"),
        ("tilelang", "/root/.tilelang"),
    ):
        argv.extend(["--mount", f"type=bind,src={cache}/{suffix},dst={destination}"])
    argv.extend(
        [
            "--mount",
            f"type=bind,src={contract_source},dst=/etc/dgx-spark/service-contract.json,readonly",
            image_ref,
            *render_service_argv(config, role),
        ]
    )
    return argv


def render_contract(config: Mapping[str, Any], role: str, lock: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Build one complete role contract, including command and verification locks."""

    image_ref, image_id, labels = _lock_image(lock, role, config)
    base: dict[str, Any] = {
        "schema_version": 1,
        "deployment_id": deployment_id(config),
        "mode": _mode(config),
        "profile_id": _profile(config)["profile_id"],
        "role": role,
        "container": _container_name(config, role),
        "host": _role_value(_deployment(config), role, "host"),
        "node_addr": _role_value(_deployment(config), role, "node_addr"),
        "api_port": int(_deployment(config)["api_port"]) if role == "head" else None,
        "master_addr": str(_deployment(config)["master_addr"]),
        "master_port": int(_deployment(config)["master_port"]),
        "model_path": _model_path(config),
        "model_root": str(_deployment(config)["model_root"]),
        "model_manifest_sha256": str(_deployment(config)["model_manifest_sha256"]),
        "image": {"reference": image_ref, "image_id": image_id, "labels": labels},
        "environment": render_environment(config, role),
        "service_argv": render_service_argv(config, role),
        "container_argv": render_container_argv(config, role, lock),
        "contract_path": _remote_contract_path(config, role),
    }
    digest_input = {
        key: base[key]
        for key in (
            "schema_version",
            "mode",
            "profile_id",
            "role",
            "node_addr",
            "api_port",
            "master_addr",
            "master_port",
            "model_path",
            "model_manifest_sha256",
            "environment",
            "service_argv",
        )
    }
    base["service_contract_sha256"] = hashlib.sha256(canonical_json(digest_input).encode("utf-8")).hexdigest()
    return base


def render_contract_json(config: Mapping[str, Any], role: str, lock: Mapping[str, Any] | None = None) -> str:
    return json.dumps(render_contract(config, role, lock), sort_keys=True, indent=2) + "\n"


def render_plan(config: Mapping[str, Any], lock: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Render a redacted deterministic plan; this function performs no I/O."""

    plan = {
        "schema_version": 1,
        "deployment_id": deployment_id(config),
        "config_sha256": config_sha256(config),
        "profile_id": config["profile"]["profile_id"],
        "mode": _mode(config),
        "ports": {
            "api": int(_deployment(config)["api_port"]),
            "master": int(_deployment(config)["master_port"]),
            "forward": int(_deployment(config)["forward_local_port"]),
        },
        "actions": [
            "preflight",
            "capture-rollback",
            "stage-contracts",
            "create-worker",
            "create-head",
            "start-worker",
            "start-head",
            "verify",
        ],
        "roles": {
            role: render_contract(config, role, lock)
            for role in _ROLES
        },
    }
    return redact_mapping(plan)
