"""Strict parsing and validation for operator-owned deployment settings."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROFILE = ROOT / "config" / "profiles" / "dsv4-native432-b12x-tp2.json"

# Keep this list explicit: an env file must not become an arbitrary override
# surface for service flags or shell commands.
ALLOWED_KEYS = frozenset(
    {
        "DEPLOYMENT_MODE",
        "IMAGE_LOCK_FILE",
        "HEAD_IMAGE_REF",
        "WORKER_IMAGE_REF",
        "MODEL_CONTAINER_PATH",
        "HEAD_HOST",
        "WORKER_HOST",
        "SSH_USER",
        "HEAD_SSH_USER",
        "WORKER_SSH_USER",
        "SSH_PORT",
        "SSH_KNOWN_HOSTS_FILE",
        "SSH_IDENTITY_FILE",
        "REMOTE_ROOT",
        "MODEL_ROOT",
        "MODEL_MANIFEST_SHA256",
        "STATE_ROOT",
        "CACHE_ROOT",
        "LOG_ROOT",
        "RESULT_ROOT",
        "REGISTRY",
        "IMAGE_REF",
        "MASTER_ADDR",
        "MASTER_PORT",
        "API_PORT",
        "HEAD_NODE_ADDR",
        "WORKER_NODE_ADDR",
        "HEAD_NET_IFACE",
        "WORKER_NET_IFACE",
        "HEAD_HCA",
        "WORKER_HCA",
        "HEAD_CUDA_VISIBLE_DEVICES",
        "WORKER_CUDA_VISIBLE_DEVICES",
        "ROCE_GID_INDEX",
        "ROCE_MTU",
        "API_BIND_ADDR",
        "FORWARD_LOCAL_PORT",
        "BASE_IMAGE_REF",
        "BUILD_CONTEXT",
        "CONTAINERFILE",
        "IMAGE_REPOSITORY",
        "IMAGE_TAG",
        "IMAGE_ARCHIVE",
        "IMAGE_ARCHIVE_SHA256",
        "REMOTE_IMAGE_ARCHIVE",
        "PERF_MIN_TOKENS_PER_SECOND",
        "PERF_MAX_TTFT_SECONDS",
        "PERF_MAX_P95_ITL_MS",
    }
)
REQUIRED_KEYS = frozenset(
    {
        "HEAD_HOST",
        "WORKER_HOST",
        "SSH_PORT",
        "SSH_KNOWN_HOSTS_FILE",
        "REMOTE_ROOT",
        "MODEL_ROOT",
        "MODEL_MANIFEST_SHA256",
        "STATE_ROOT",
        "CACHE_ROOT",
        "RESULT_ROOT",
        "IMAGE_REF",
        "MASTER_ADDR",
        "MASTER_PORT",
        "API_PORT",
        "HEAD_NODE_ADDR",
        "WORKER_NODE_ADDR",
        "HEAD_NET_IFACE",
        "WORKER_NET_IFACE",
        "HEAD_HCA",
        "WORKER_HCA",
        "HEAD_CUDA_VISIBLE_DEVICES",
        "WORKER_CUDA_VISIBLE_DEVICES",
        "ROCE_MTU",
        "API_BIND_ADDR",
        "FORWARD_LOCAL_PORT",
    }
)
OPTIONAL_EMPTY_KEYS = frozenset(ALLOWED_KEYS - REQUIRED_KEYS)
_SAFE_VALUE = re.compile(r"^[^\x00-\x1f\x7f]+$")
_SAFE_HOST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
_SAFE_ID = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
_SAFE_LIST = re.compile(r"^[A-Za-z0-9_.-]+(?:,[A-Za-z0-9_.-]+)*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_DIGEST = re.compile(r"^(?:[A-Za-z0-9._/-]+@)?sha256:[0-9a-f]{64}$")
_PLACEHOLDER = re.compile(
    r"(?:<[^>]+>|\b(?:REPLACE(?:_WITH)?|CHANGEME|EXAMPLE_VALUE|YOUR_VALUE)\b)",
    re.IGNORECASE,
)
_SHELL_SYNTAX = re.compile(r"(?:\$\(|\$\{|`|;|\|\||&&|\n|\r)")


class ConfigError(ValueError):
    """A configuration file is invalid or unsafe to render."""


def _reject(condition: bool, message: str) -> None:
    if condition:
        raise ConfigError(message)


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse only comments, blank lines, and unique KEY=VALUE records."""

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f"cannot read env file: {path}") from exc
    values: dict[str, str] = {}
    for line_number, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        _reject("=" not in line, f"line {line_number}: expected KEY=VALUE")
        key, value = line.split("=", 1)
        _reject(not re.fullmatch(r"[A-Z][A-Z0-9_]*", key), f"line {line_number}: invalid key")
        _reject(key not in ALLOWED_KEYS, f"line {line_number}: unknown key {key}")
        _reject(key in values, f"line {line_number}: duplicate key {key}")
        _reject(value != value.strip(), f"line {line_number}: surrounding value whitespace is not allowed")
        _reject(not _SAFE_VALUE.fullmatch(value), f"line {line_number}: control character is not allowed")
        _reject(_SHELL_SYNTAX.search(value) is not None, f"line {line_number}: shell syntax is not allowed")
        _reject(_PLACEHOLDER.search(value) is not None, f"line {line_number}: unresolved placeholder")
        values[key] = value
    return values


def _nonempty(values: Mapping[str, str], key: str) -> str:
    value = values.get(key, "")
    _reject(not value, f"missing required value: {key}")
    return value


def _port(values: Mapping[str, str], key: str) -> int:
    raw = _nonempty(values, key)
    _reject(not raw.isdecimal(), f"{key} must be an integer port")
    value = int(raw)
    _reject(not 1 <= value <= 65535, f"{key} must be between 1 and 65535")
    return value


def _address(values: Mapping[str, str], key: str) -> str:
    value = _nonempty(values, key)
    _reject(not _SAFE_HOST.fullmatch(value), f"{key} contains unsafe host characters")
    _reject("@" in value or "/" in value, f"{key} must not contain a user or path")
    try:
        ipaddress.ip_address(value)
    except ValueError as exc:
        raise ConfigError(f"{key} must be an explicit IPv4 or IPv6 address") from exc
    return value


def _absolute_root(values: Mapping[str, str], key: str) -> str:
    value = _nonempty(values, key)
    path = Path(value)
    _reject(not path.is_absolute(), f"{key} must be absolute")
    _reject(path == Path("/"), f"{key} may not be the filesystem root")
    _reject(".." in path.parts, f"{key} may not contain traversal")
    _reject(not _SAFE_VALUE.fullmatch(value), f"{key} contains control characters")
    return value.rstrip("/") or "/"

DEFAULT_MODEL_CONTAINER_PATH = "/models/DeepSeek-V4-Flash-0731"


def _lock_file(values: Mapping[str, str]) -> str:
    raw = values.get("IMAGE_LOCK_FILE", "")
    if not raw:
        return str(ROOT / "image.lock.json")
    path = Path(raw)
    _reject(not path.is_absolute(), "IMAGE_LOCK_FILE must be absolute")
    _reject(path == Path("/") or ".." in path.parts, "IMAGE_LOCK_FILE must be an absolute traversal-free file")
    _reject(not _SAFE_VALUE.fullmatch(raw), "IMAGE_LOCK_FILE contains control characters")
    return str(path)

def _external_file(values: Mapping[str, str], key: str) -> str:
    value = _absolute_root(values, key)
    path = Path(value)
    repo = ROOT.resolve()
    _reject(path == repo or repo in path.parents, f"{key} must be outside the repository")
    return value


def _safe_list(values: Mapping[str, str], key: str) -> str:
    value = _nonempty(values, key)
    _reject(not _SAFE_LIST.fullmatch(value), f"{key} contains unsafe characters")
    return value


def validate(values: Mapping[str, str], profile: Mapping[str, Any]) -> dict[str, Any]:
    """Validate deployment values and return a canonical structured object."""

    unknown = set(values) - ALLOWED_KEYS
    _reject(bool(unknown), f"unknown configuration keys: {sorted(unknown)}")
    missing = sorted(key for key in REQUIRED_KEYS if not values.get(key))
    _reject(bool(missing), f"missing required values: {', '.join(missing)}")
    mode = values.get("DEPLOYMENT_MODE", "").strip().lower() or "generic"
    _reject(mode not in {"generic", "production", "candidate"}, "DEPLOYMENT_MODE must be generic, production, or candidate")
    if mode in {"production", "candidate"}:
        expected_ports = (29619, 8101) if mode == "production" else (29621, 18101)
        _reject(values.get("MASTER_ADDR") != "192.168.100.10", f"{mode} MASTER_ADDR must be 192.168.100.10")
        _reject(values.get("HEAD_NODE_ADDR") != "192.168.100.10", f"{mode} HEAD_NODE_ADDR must be 192.168.100.10")
        _reject(values.get("WORKER_NODE_ADDR") != "192.168.100.11", f"{mode} WORKER_NODE_ADDR must be 192.168.100.11")
        _reject(values.get("MASTER_PORT") != str(expected_ports[0]), f"{mode} MASTER_PORT must be {expected_ports[0]}")
        _reject(values.get("API_PORT") != str(expected_ports[1]), f"{mode} API_PORT must be {expected_ports[1]}")
        _reject(not values.get("HEAD_IMAGE_REF") or not values.get("WORKER_IMAGE_REF"), f"{mode} requires distinct HEAD_IMAGE_REF and WORKER_IMAGE_REF")
    head_host = _address(values, "HEAD_HOST")
    worker_host = _address(values, "WORKER_HOST")
    _reject(head_host == worker_host, "HEAD_HOST and WORKER_HOST must differ")
    ssh_user = values.get("SSH_USER", "")
    head_user = values.get("HEAD_SSH_USER", "") or ssh_user
    worker_user = values.get("WORKER_SSH_USER", "") or ssh_user
    _reject("SSH_KNOWN_HOSTS_FILE" not in values or not values["SSH_KNOWN_HOSTS_FILE"], "SSH_KNOWN_HOSTS_FILE is required")
    known_hosts = _external_file(values, "SSH_KNOWN_HOSTS_FILE")
    identity = values.get("SSH_IDENTITY_FILE", "")
    if identity:
        identity = _external_file(values, "SSH_IDENTITY_FILE")
    remote_root = _external_file(values, "REMOTE_ROOT")
    model_root = _external_file(values, "MODEL_ROOT")
    state_root = _external_file(values, "STATE_ROOT")
    cache_root = _external_file(values, "CACHE_ROOT")
    result_root = _external_file(values, "RESULT_ROOT")
    log_root = values.get("LOG_ROOT", "")
    if log_root:
        log_root = _external_file(values, "LOG_ROOT")
    model_manifest = _nonempty(values, "MODEL_MANIFEST_SHA256")
    _reject(not _SHA256.fullmatch(model_manifest), "MODEL_MANIFEST_SHA256 must be 64 lowercase hex characters")
    image_ref = _nonempty(values, "IMAGE_REF")
    _reject(not _IMAGE_DIGEST.fullmatch(image_ref), "IMAGE_REF must be an immutable sha256 digest")
    head_image_ref = values.get("HEAD_IMAGE_REF", "") or image_ref
    worker_image_ref = values.get("WORKER_IMAGE_REF", "") or image_ref
    _reject(not _IMAGE_DIGEST.fullmatch(head_image_ref), "HEAD_IMAGE_REF must be an immutable sha256 digest")
    _reject(not _IMAGE_DIGEST.fullmatch(worker_image_ref), "WORKER_IMAGE_REF must be an immutable sha256 digest")
    if mode in {"production", "candidate"}:
        _reject(head_image_ref == worker_image_ref, f"{mode} HEAD_IMAGE_REF and WORKER_IMAGE_REF must differ")
    model_container_path = values.get("MODEL_CONTAINER_PATH", "") or DEFAULT_MODEL_CONTAINER_PATH
    _reject(model_container_path != DEFAULT_MODEL_CONTAINER_PATH, "MODEL_CONTAINER_PATH is fixed by the reviewed model contract")
    master_addr = _address(values, "MASTER_ADDR")
    head_node_addr = _address(values, "HEAD_NODE_ADDR")
    worker_node_addr = _address(values, "WORKER_NODE_ADDR")
    _reject(master_addr != head_node_addr, "MASTER_ADDR must equal HEAD_NODE_ADDR")
    _reject(head_node_addr == worker_node_addr, "HEAD_NODE_ADDR and WORKER_NODE_ADDR must differ")
    master_port = _port(values, "MASTER_PORT")
    api_port = _port(values, "API_PORT")
    forward_port = _port(values, "FORWARD_LOCAL_PORT")
    _reject(master_port == api_port, "MASTER_PORT and API_PORT must differ")
    _reject(forward_port == api_port, "FORWARD_LOCAL_PORT and API_PORT must differ")
    if mode in {"production", "candidate"}:
        _reject(forward_port in {master_port, api_port}, f"{mode} FORWARD_LOCAL_PORT must not overlap service ports")
    api_bind = _nonempty(values, "API_BIND_ADDR")
    _reject(api_bind not in {"127.0.0.1", "::1"}, "API_BIND_ADDR must remain loopback")
    head_iface = _safe_list(values, "HEAD_NET_IFACE")
    worker_iface = _safe_list(values, "WORKER_NET_IFACE")
    head_hca = _safe_list(values, "HEAD_HCA")
    worker_hca = _safe_list(values, "WORKER_HCA")
    head_devices = _safe_list(values, "HEAD_CUDA_VISIBLE_DEVICES")
    worker_devices = _safe_list(values, "WORKER_CUDA_VISIBLE_DEVICES")
    gid = values.get("ROCE_GID_INDEX", "")
    if gid:
        _reject(not gid.isdecimal() or not 0 <= int(gid) <= 255, "ROCE_GID_INDEX must be between 0 and 255")
    mtu = _nonempty(values, "ROCE_MTU")
    _reject(not mtu.isdecimal() or not 576 <= int(mtu) <= 65535, "ROCE_MTU is outside the valid range")

    profile_id = profile.get("profile_id")
    _reject(not isinstance(profile_id, str) or not profile_id, "profile profile_id is required")
    return {
        "profile": dict(profile),
        "deployment": {
            "mode": mode,
            "head_host": head_host,
            "worker_host": worker_host,
            "ssh_user": ssh_user or None,
            "head_ssh_user": head_user,
            "worker_ssh_user": worker_user,
            "ssh_port": _port(values, "SSH_PORT"),
            "ssh_known_hosts_file": known_hosts,
            "ssh_identity_file": identity or None,
            "remote_root": remote_root,
            "model_root": model_root,
            "model_container_path": model_container_path,
            "model_manifest_sha256": model_manifest,
            "state_root": state_root,
            "cache_root": cache_root,
            "log_root": log_root or None,
            "result_root": result_root,
            "registry": values.get("REGISTRY") or None,
            "image_ref": image_ref,
            "head_image_ref": head_image_ref,
            "worker_image_ref": worker_image_ref,
            "image_lock_file": _lock_file(values),
            "master_addr": master_addr,
            "master_port": master_port,
            "api_port": api_port,
            "head_node_addr": head_node_addr,
            "worker_node_addr": worker_node_addr,
            "head_net_iface": head_iface,
            "worker_net_iface": worker_iface,
            "head_hca": head_hca,
            "worker_hca": worker_hca,
            "head_cuda_visible_devices": head_devices,
            "worker_cuda_visible_devices": worker_devices,
            "roce_gid_index": int(gid) if gid else None,
            "roce_mtu": int(mtu),
            "api_bind_addr": api_bind,
            "forward_local_port": forward_port,
        },
    }


_PROFILE_KEYS: dict[tuple[str, ...], frozenset[str]] = {
    (): frozenset(
        {
            "schema_version",
            "profile_id",
            "release_gated_manifest_fields",
            "platform",
            "topology",
            "model",
            "service",
            "limits",
            "parsing",
            "container",
            "image",
            "security",
        }
    ),
    ("image",): frozenset({"lock_path", "candidate_id", "source_refs", "required_labels"}),
    ("image", "source_refs"): frozenset({"vllm", "b12x"}),
    ("platform",): frozenset({"architecture", "accelerator", "nodes"}),
    ("topology",): frozenset(
        {
            "tensor_parallel_size",
            "pipeline_parallel_size",
            "nnodes",
            "head_rank",
            "worker_rank",
            "worker_first",
        }
    ),
    ("model",): frozenset({"family", "served_model_name", "container_path", "model_path"}),
    ("service",): frozenset(
        {
            "attention_backend",
            "moe_backend",
            "linear_backend",
            "distributed_executor_backend",
            "target_kv_cache_dtype",
            "block_size",
            "gpu_memory_utilization",
            "enable_chunked_prefill",
            "long_prefill_token_threshold",
            "compact_layout",
            "draft_config_json",
            "reasoning_config_json",
            "load_format",
            "prefix_caching",
            "async_scheduling",
            "cuda_graph",
            "max_cudagraph_capture_size",
            "compilation_config_json",
        }
    ),
    ("service", "compact_layout"): frozenset({"abi", "record_bytes"}),
    ("service", "prefix_caching"): frozenset({"enabled", "hash_algorithm"}),
    ("service", "cuda_graph"): frozenset({"enabled", "mode"}),
    ("limits",): frozenset({"max_model_len", "max_num_seqs", "max_num_batched_tokens"}),
    ("parsing",): frozenset({"tokenizer_mode", "tool_call_parser", "reasoning_parser"}),
    ("container",): frozenset({"name_prefix", "read_only_root", "no_new_privileges"}),
    ("security",): frozenset({"production_change_allowed", "api_bind_default", "model_mount_read_only"}),
}
_PROFILE_VALUES: dict[tuple[str, ...], Any] = {
    ("schema_version",): 1,
    ("profile_id",): "dsv4-native432-b12x-tp2",
    ("image", "lock_path"): "image.lock.json",
    ("image", "candidate_id"): "dsv4-0731-native432-b12x",
    ("image", "source_refs", "vllm"): "release/dsv4-0731-native432-b12x",
    ("image", "source_refs", "b12x"): "release/dsv4-0731-native432",
    (
        "image",
        "required_labels",
    ): [
        "org.opencontainers.image.revision",
        "com.dgx-spark.architecture",
        "com.dgx-spark.profile_sha256",
        "com.dgx-spark.service_contract_sha256",
        "com.dgx-spark.image_lock_sha256",
        "com.dgx-spark.vllm.commit",
        "com.dgx-spark.b12x.commit",
    ],
    (
        "release_gated_manifest_fields",
    ): [
        "chat_template",
        "reasoning_config",
        "load_format",
        "linear_backend",
        "cuda_graph_capture_sizes",
        "compact_block_stride",
    ],
    ("model", "family"): "DeepSeek-V4-Flash",
    ("model", "served_model_name"): "deepseek-v4-flash-0731-native432",
    ("model", "container_path"): "/models",
    ("model", "model_path"): "/models/DeepSeek-V4-Flash-0731",
    ("service", "attention_backend"): "B12X_MLA_SPARSE",
    ("service", "moe_backend"): "b12x",
    ("service", "linear_backend"): "b12x",
    ("service", "distributed_executor_backend"): "mp",
    ("service", "target_kv_cache_dtype"): "nvfp4_ds_mla",
    ("service", "block_size"): 256,
    ("service", "gpu_memory_utilization"): 0.85,
    ("service", "enable_chunked_prefill"): True,
    ("service", "long_prefill_token_threshold"): 0,
    ("service", "compact_layout", "abi"): "dsv4-native432-compact-v1",
    ("service", "compact_layout", "record_bytes"): 432,
    (
        "service",
        "draft_config_json",
    ): '{"method":"dspark","num_speculative_tokens":5,"draft_sample_method":"probabilistic","attention_backend":"B12X_MLA_SPARSE","kv_cache_dtype":"fp8"}',
    ("service", "reasoning_config_json"): '{"reasoning_parser":"deepseek_v4","reasoning_start_str":"","reasoning_end_str":""}',
    ("service", "load_format"): "instanttensor",
    ("service", "prefix_caching", "enabled"): True,
    ("service", "prefix_caching", "hash_algorithm"): "sha256",
    ("service", "async_scheduling"): False,
    ("service", "cuda_graph", "enabled"): True,
    ("service", "cuda_graph", "mode"): "FULL_AND_PIECEWISE",
    ("service", "max_cudagraph_capture_size"): 64,
    ("service", "compilation_config_json"): '{"cudagraph_mode":"FULL_AND_PIECEWISE","custom_ops":["all"]}',
    ("limits", "max_model_len"): 327680,
    ("limits", "max_num_seqs"): 5,
    ("limits", "max_num_batched_tokens"): 1024,
    ("parsing", "tokenizer_mode"): "deepseek_v4",
    ("parsing", "tool_call_parser"): "deepseek_v4",
    ("parsing", "reasoning_parser"): "deepseek_v4",
    ("container", "name_prefix"): "dsv4-candidate-",
    ("container", "read_only_root"): True,
    ("container", "no_new_privileges"): True,
    ("security", "production_change_allowed"): False,
    ("security", "api_bind_default"): "127.0.0.1",
    ("security", "model_mount_read_only"): True,
}


def _profile_at(profile: Mapping[str, Any], path: tuple[str, ...]) -> Any:
    current: Any = profile
    for key in path:
        _reject(not isinstance(current, Mapping) or key not in current, f"profile is missing {'.'.join(path)}")
        current = current[key]
    return current


def _validate_profile(profile: Mapping[str, Any]) -> None:
    for path, expected_keys in _PROFILE_KEYS.items():
        current = _profile_at(profile, path)
        _reject(
            not isinstance(current, Mapping) or frozenset(current) != expected_keys,
            f"profile keys changed at {'.'.join(path) or '<root>'}",
        )
    for path, expected in _PROFILE_VALUES.items():
        _reject(_profile_at(profile, path) != expected, f"profile invariant changed: {'.'.join(path)}")


def load_profile(path: Path = DEFAULT_PROFILE) -> dict[str, Any]:
    try:
        resolved = path.resolve()
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot read profile: {path}") from exc
    _reject(resolved != DEFAULT_PROFILE.resolve(), "only the committed default profile may be used")
    _reject(not isinstance(value, dict), "profile must be a JSON object")
    _validate_profile(value)
    return value


def load_config(env_file: Path, profile_file: Path = DEFAULT_PROFILE) -> dict[str, Any]:
    values = parse_env_file(env_file)
    return validate(values, load_profile(profile_file))


def canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def config_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
