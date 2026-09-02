"""Render deterministic argv vectors without executing them."""

from __future__ import annotations

from typing import Any, Mapping

from .config import config_sha256
from .manifest import deployment_id
from .redact import redact_mapping


class RenderError(ValueError):
    """The fixed profile cannot be rendered safely."""


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
    return str(deployment[f"{role}_{suffix}"])


def render_environment(config: Mapping[str, Any], role: str) -> dict[str, str]:
    """Derive transport environment from role-specific config fields."""

    deployment = _deployment(config)
    return {
        "VLLM_HOST_IP": _role_value(deployment, role, "node_addr"),
        "NCCL_SOCKET_IFNAME": _role_value(deployment, role, "net_iface"),
        "GLOO_SOCKET_IFNAME": _role_value(deployment, role, "net_iface"),
        "TP_SOCKET_IFNAME": _role_value(deployment, role, "net_iface"),
        "NCCL_IB_HCA": _role_value(deployment, role, "hca"),
        "NCCL_IB_DISABLE": "0",
        "NCCL_IB_ROCE_VERSION_NUM": "2",
        "CUDA_VISIBLE_DEVICES": _role_value(deployment, role, "cuda_visible_devices"),
    }


def render_service_argv(config: Mapping[str, Any], role: str) -> list[str]:
    """Render the fixed service profile into one role's token vector."""

    deployment = _deployment(config)
    profile = _profile(config)
    topology = profile["topology"]
    model = profile["model"]
    service = profile["service"]
    limits = profile["limits"]
    if role not in {"head", "worker"}:
        raise RenderError(f"unsupported role: {role}")
    rank = int(topology["head_rank"] if role == "head" else topology["worker_rank"])
    argv = [
        "vllm",
        "serve",
        str(model["container_path"]),
        "--served-model-name",
        str(model["served_model_name"]),
        "--trust-remote-code",
        "--tensor-parallel-size",
        str(topology["tensor_parallel_size"]),
        "--pipeline-parallel-size",
        str(topology["pipeline_parallel_size"]),
        "--nnodes",
        str(topology["nnodes"]),
        "--node-rank",
        str(rank),
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
        "--no-async-scheduling",
        "--enable-prefix-caching",
        "--prefix-caching-hash-algo",
        "sha256",
        "--tokenizer-mode",
        str(profile["parsing"]["tokenizer_mode"]),
        "--tool-call-parser",
        str(profile["parsing"]["tool_call_parser"]),
        "--reasoning-parser",
        str(profile["parsing"]["reasoning_parser"]),
        "--enable-auto-tool-choice",
        "--moe-backend",
        str(service["moe_backend"]),
        "--attention-backend",
        str(service["attention_backend"]),
        "--speculative-config",
        str(service["draft_config_json"]),
    ]
    if role == "head":
        argv.extend(["--host", str(deployment["api_bind_addr"]), "--port", str(deployment["api_port"])])
    else:
        argv.append("--headless")
    return argv


def render_container_argv(config: Mapping[str, Any], role: str) -> list[str]:
    """Render a safe container-create argv; never execute it."""

    deployment = _deployment(config)
    profile = _profile(config)
    container = profile["container"]
    name = f"{container['name_prefix']}{role}"
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
        "--read-only",
        "--security-opt",
        "no-new-privileges:true",
        "--label",
        f"com.dgx-spark.deployment_id={deployment_id(config)}",
        "--label",
        f"com.dgx-spark.profile_sha256={config_sha256({'profile': profile})}",
        "--mount",
        f"type=bind,src={deployment['model_root']},dst={profile['model']['container_path']},readonly",
    ]
    for key, value in sorted(render_environment(config, role).items()):
        argv.extend(["--env", f"{key}={value}"])
    argv.extend([deployment["image_ref"], *render_service_argv(config, role)])
    return argv


def render_plan(config: Mapping[str, Any]) -> dict[str, Any]:
    plan = {
        "schema_version": 1,
        "deployment_id": deployment_id(config),
        "config_sha256": config_sha256(config),
        "profile_id": config["profile"]["profile_id"],
        "mode": "dry-run",
        "roles": {
            role: {
                "environment": render_environment(config, role),
                "service_argv": render_service_argv(config, role),
                "container_argv": render_container_argv(config, role),
            }
            for role in ("worker", "head")
        },
    }
    return redact_mapping(plan)
