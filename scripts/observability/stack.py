#!/usr/bin/env python3
"""Repo-owned DGX Spark observability lifecycle.

All mutating Docker operations are sent over SSH to the configured DGX hosts.
The command manages only resources carrying ``com.dgx-spark.observability``.
``render`` and ``deploy --dry-run`` are safe to run on a laptop.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
from typing import Any, Mapping, NoReturn, Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PROFILE = REPO_ROOT / "config" / "profiles" / "dgx-spark-observability.json"
DEFAULT_LABEL = "com.dgx-spark.observability"
STACK_NAME = "dgx-spark-observability"
ARCHITECTURE = "linux/arm64"

# Public multi-architecture tags. Tags are for resolve-images only; deploy
# requires an external ready lock containing repository@sha256 references.
DEFAULT_IMAGE_TAGS: dict[str, str] = {
    "prometheus": "docker.io/prom/prometheus:v2.54.1",
    "grafana": "docker.io/grafana/grafana:11.2.0",
    "alertmanager": "docker.io/prom/alertmanager:v0.27.0",
    "loki": "docker.io/grafana/loki:3.1.1",
    "promtail": "docker.io/grafana/promtail:3.1.1",
    "node_exporter": "docker.io/prom/node-exporter:v1.8.2",
    # A tiny repo-owned exporter runs in the public Python image and reads
    # only mounted sysfs; it exposes GID/ndev/RoCE-v2/link-flap metrics.
    "fabric_exporter": "docker.io/library/python:3.12-alpine",
}
LOCK_KEYS = tuple(DEFAULT_IMAGE_TAGS)

COMPONENTS = (
    "prometheus", "grafana", "alertmanager", "loki", "promtail_head",
    "promtail_worker", "node_exporter_head", "node_exporter_worker",
    "fabric_exporter_head", "fabric_exporter_worker",
)
HEAD_COMPONENTS = (
    "prometheus", "grafana", "alertmanager", "loki", "promtail_head",
    "node_exporter_head", "fabric_exporter_head",
)
WORKER_COMPONENTS = ("promtail_worker", "node_exporter_worker", "fabric_exporter_worker")

# Existing production env files can be passed to this command. Keep the
# allow-list broad enough for those files while rejecting shell syntax.
ALLOWED_ENV_KEYS = {
    "HEAD_HOST", "WORKER_HOST", "HEAD_SSH_HOST", "WORKER_SSH_HOST", "SSH_USER",
    "HEAD_SSH_USER", "WORKER_SSH_USER", "SSH_PORT", "SSH_KNOWN_HOSTS_FILE",
    "SSH_IDENTITY_FILE", "OBS_HEAD_HOST", "OBS_WORKER_HOST", "OBS_HEAD_SSH_HOST",
    "OBS_WORKER_SSH_HOST", "OBS_SSH_USER", "OBS_HEAD_SSH_USER", "OBS_WORKER_SSH_USER",
    "OBS_SSH_PORT", "OBS_SSH_KNOWN_HOSTS_FILE", "OBS_SSH_IDENTITY_FILE", "OBS_REMOTE_ROOT",
    "OBS_IMAGE_LOCK_FILE", "OBS_PROFILE_FILE", "OBS_GRAFANA_ADMIN_USER",
    "OBS_GRAFANA_ADMIN_PASSWORD", "OBS_BIND_ADDR", "OBS_VLLM_PORT",
    "OBS_VLLM_METRICS_PATH", "OBS_VLLM_SCHEME", "OBS_HEAD_NODE_NAME", "OBS_WORKER_NODE_NAME",
    "OBS_VLLM_CONTAINER_HEAD", "OBS_VLLM_CONTAINER_WORKER",
    "OBS_VLLM_LOG_PATH_HEAD", "OBS_VLLM_LOG_PATH_WORKER",
    "OBS_FABRIC_HCA", "OBS_FABRIC_NDEV", "OBS_FABRIC_GID_INDEX",
    "OBS_DOCKER_PROXY",
    # production keys are accepted and ignored, allowing one env file for both
    "DEPLOYMENT_MODE", "IMAGE_LOCK_FILE", "HEAD_IMAGE_REF", "WORKER_IMAGE_REF",
    "FABRIC_PROFILE", "HEAD_FABRIC_CIDR", "WORKER_FABRIC_CIDR", "HEAD_FABRIC_PEER",
    "WORKER_FABRIC_PEER", "HEAD_FABRIC_CONNECTION", "WORKER_FABRIC_CONNECTION",
    "HEAD_ROCE_GID_INDEX", "WORKER_ROCE_GID_INDEX", "MODEL_ROOT", "MODEL_CONTAINER_PATH",
    "MODEL_MANIFEST_SHA256", "STATE_ROOT", "CACHE_ROOT", "LOG_ROOT", "RESULT_ROOT", "REGISTRY",
    "IMAGE_REF", "MASTER_ADDR", "MASTER_PORT", "API_PORT", "HEAD_NODE_ADDR", "WORKER_NODE_ADDR",
    "HEAD_NET_IFACE", "WORKER_NET_IFACE", "HEAD_HCA", "WORKER_HCA", "HEAD_CUDA_VISIBLE_DEVICES",
    "WORKER_CUDA_VISIBLE_DEVICES", "ROCE_GID_INDEX", "ROCE_MTU", "API_BIND_ADDR",
    "ALLOW_PUBLIC_API", "FORWARD_LOCAL_PORT", "MAX_NUM_SEQS", "MAX_NUM_BATCHED_TOKENS",
    "ALLOW_PRODUCTION_IMAGES_IN_CANDIDATE", "PRODUCTION_CHANGE_ALLOWED", "BASE_IMAGE_REF",
    "BUILD_CONTEXT", "CONTAINERFILE", "IMAGE_REPOSITORY", "IMAGE_TAG", "IMAGE_ARCHIVE",
    "IMAGE_ARCHIVE_SHA256", "REMOTE_IMAGE_ARCHIVE", "PERF_MIN_TOKENS_PER_SECOND",
    "PERF_MAX_TTFT_SECONDS", "PERF_MAX_P95_ITL_MS",
}
KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
USER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
ABS_PATH_RE = re.compile(r"^/(?!$)[^\x00-\x1f\x7f]+$")
DIGEST_REF_RE = re.compile(
    r"^(?:[A-Za-z0-9][A-Za-z0-9._/-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)*)@sha256:[0-9a-f]{64}$"
)
SHA_RE = re.compile(r"^[0-9a-f]{64}$")


class ObservabilityError(Exception):
    """Expected operator/configuration failure."""


def _fail(message: str) -> NoReturn:
    raise ObservabilityError(message)


def parse_env(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE records without evaluating shell syntax."""
    if not path.is_file():
        _fail(f"environment file does not exist: {path}")
    result: dict[str, str] = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            _fail(f"{path}:{number}: expected KEY=VALUE")
        key, value = line.split("=", 1)
        key = key.strip()
        if not KEY_RE.fullmatch(key):
            _fail(f"{path}:{number}: invalid key {key!r}")
        if key not in ALLOWED_ENV_KEYS:
            _fail(f"{path}:{number}: unknown key {key!r}")
        if key in result:
            _fail(f"{path}:{number}: duplicate key {key!r}")
        if any(ord(char) < 0x20 or ord(char) == 0x7f for char in value):
            _fail(f"{path}:{number}: control character in {key}")
        if "$(" in value or "`" in value:
            _fail(f"{path}:{number}: shell syntax is not accepted")
        result[key] = value.strip()
    return result


def _first(env: Mapping[str, str], *keys: str, default: str = "") -> str:
    for key in keys:
        value = env.get(key, "").strip()
        if value:
            return value
    return default


def _int_value(value: str, key: str, minimum: int = 1, maximum: int = 65535) -> int:
    try:
        parsed = int(value)
    except ValueError:
        _fail(f"{key} must be an integer")
    if not minimum <= parsed <= maximum:
        _fail(f"{key} must be between {minimum} and {maximum}")
    return parsed


def _validate_host(value: str, key: str) -> str:
    if not value or not HOST_RE.fullmatch(value):
        _fail(f"{key} must be a host/IP without shell syntax")
    return value


def _validate_user(value: str, key: str) -> str:
    if not value or not USER_RE.fullmatch(value):
        _fail(f"{key} must be a valid SSH username")
    return value


def _validate_abs_path(value: str, key: str, *, external: bool = True) -> str:
    if not value or not ABS_PATH_RE.fullmatch(value):
        _fail(f"{key} must be an absolute path")
    path = Path(value)
    if any(part in (".", "..") for part in path.parts):
        _fail(f"{key} must not contain dot path components")
    if external and (path == REPO_ROOT or REPO_ROOT in path.parents):
        _fail(f"{key} must point outside the repository")
    return value.rstrip("/") or "/"


def load_profile(path: Path) -> dict[str, Any]:
    if not path.is_file():
        _fail(f"observability profile does not exist: {path}")
    try:
        profile = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        _fail(f"invalid profile JSON: {exc}")
    if not isinstance(profile, dict):
        _fail("observability profile must be an object")
    if profile.get("architecture") != ARCHITECTURE:
        _fail(f"observability profile architecture must be {ARCHITECTURE}")
    network = profile.get("network")
    if not isinstance(network, dict):
        _fail("observability profile network is required")
    expected_ports = {
        "grafana": 13000, "loki": 13100, "prometheus": 19090,
        "alertmanager": 19093, "node_exporter": 19100, "fabric_exporter": 19110,
    }
    if network.get("ports") != expected_ports:
        _fail(f"profile ports must exactly be {expected_ports}")
    service = profile.get("service")
    if not isinstance(service, dict) or service.get("api_port") != 8101:
        _fail("observability profile must scrape vLLM API port 8101")
    components = profile.get("components")
    volumes = profile.get("volumes")
    if not isinstance(components, dict) or not isinstance(volumes, dict):
        _fail("observability profile components and volumes are required")
    for component in COMPONENTS:
        entry = components.get(component)
        if not isinstance(entry, dict):
            _fail(f"observability profile missing component {component}")
        if entry.get("role") not in ("head", "worker"):
            _fail(f"component {component} has invalid role")
        name = entry.get("container")
        if not isinstance(name, str) or not name.startswith("dgx-observe-"):
            _fail(f"component {component} has invalid owned container name")
    for key in ("prometheus", "grafana", "alertmanager", "loki", "promtail_head", "promtail_worker"):
        value = volumes.get(key)
        if not isinstance(value, str) or not value.startswith("dgx-observe-"):
            _fail(f"profile has invalid volume name for {key}")
    return profile


def load_lock(path: Path) -> dict[str, str]:
    """Load an external ready lock and return immutable image refs."""
    if not path.is_file():
        _fail(f"image lock does not exist: {path}")
    try:
        lock = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        _fail(f"invalid image lock JSON: {exc}")
    if not isinstance(lock, dict) or lock.get("schema_version") != 1:
        _fail("observability image lock schema_version must be 1")
    if lock.get("status") != "ready":
        _fail("observability image lock status must be ready")
    if lock.get("architecture") != ARCHITECTURE:
        _fail(f"observability image lock architecture must be {ARCHITECTURE}")
    images = lock.get("images")
    if not isinstance(images, dict):
        _fail("observability image lock images is required")
    refs: dict[str, str] = {}
    for key in DEFAULT_IMAGE_TAGS:
        entry = images.get(key)
        if not isinstance(entry, dict):
            _fail(f"observability image lock missing {key}")
        reference = entry.get("reference")
        if not isinstance(reference, str) or not DIGEST_REF_RE.fullmatch(reference):
            _fail(f"observability image {key} must use repository@sha256 digest")
        digest = entry.get("digest")
        if digest is not None and (not isinstance(digest, str) or not SHA_RE.fullmatch(digest)):
            _fail(f"observability image {key} digest must be lowercase hex")
        if digest is not None and not reference.endswith("@sha256:" + digest):
            _fail(f"observability image {key} digest does not match reference")
        refs[key] = reference
    return refs


def _lock_document(images: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    return {"schema_version": 1, "status": "ready", "architecture": ARCHITECTURE,
            "images": {key: dict(images[key]) for key in sorted(images)}}


def _quote(value: str) -> str:
    return shlex.quote(value)


def _safe_component_name(name: str) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name):
        _fail(f"invalid component name {name!r}")
    return name


def _config(
    env: Mapping[str, str],
    profile: Mapping[str, Any],
    lock: Mapping[str, str] | None,
    *,
    require_remote: bool = True,
) -> dict[str, Any]:
    network = profile["network"]
    ports = network["ports"]
    head_host = _validate_host(_first(env, "OBS_HEAD_HOST", "HEAD_HOST", default=str(network["head_addr"])), "OBS_HEAD_HOST")
    worker_host = _validate_host(_first(env, "OBS_WORKER_HOST", "WORKER_HOST", default=str(network["worker_addr"])), "OBS_WORKER_HOST")
    head_ssh = _validate_host(_first(env, "OBS_HEAD_SSH_HOST", "HEAD_SSH_HOST", default=head_host), "OBS_HEAD_SSH_HOST")
    worker_ssh = _validate_host(_first(env, "OBS_WORKER_SSH_HOST", "WORKER_SSH_HOST", default=worker_host), "OBS_WORKER_SSH_HOST")
    user = _first(env, "OBS_SSH_USER", "SSH_USER")
    head_user = _first(env, "OBS_HEAD_SSH_USER", "HEAD_SSH_USER", default=user)
    worker_user = _first(env, "OBS_WORKER_SSH_USER", "WORKER_SSH_USER", default=user)
    if not head_user:
        if require_remote:
            _fail("OBS_HEAD_SSH_USER (or SSH_USER) is required")
        head_user = "operator"
    if not worker_user:
        if require_remote:
            _fail("OBS_WORKER_SSH_USER (or SSH_USER) is required")
        worker_user = "operator"
    _validate_user(head_user, "OBS_HEAD_SSH_USER")
    _validate_user(worker_user, "OBS_WORKER_SSH_USER")
    ssh_port = _int_value(_first(env, "OBS_SSH_PORT", "SSH_PORT", default="22"), "OBS_SSH_PORT")
    remote_root_value = _first(env, "OBS_REMOTE_ROOT", default="/tmp/dgx-spark-observability-render")
    remote_root = _validate_abs_path(remote_root_value, "OBS_REMOTE_ROOT", external=require_remote)
    # Bind every observability endpoint on all interfaces so the stack is
    # reachable over Tailscale (or any routed network) without a tunnel.
    bind_addr = _first(env, "OBS_BIND_ADDR", default="0.0.0.0")
    if bind_addr not in ("0.0.0.0", "127.0.0.1", "::1"):
        _fail("OBS_BIND_ADDR must be 0.0.0.0, 127.0.0.1, or ::1")
    vllm_port = _int_value(_first(env, "OBS_VLLM_PORT", "API_PORT", default="8101"), "OBS_VLLM_PORT")
    if vllm_port != 8101:
        _fail("OBS_VLLM_PORT must remain 8101; production vLLM is not changed")
    metrics_path = _first(env, "OBS_VLLM_METRICS_PATH", default=str(profile["service"]["metrics_path"]))
    if not metrics_path.startswith("/") or any(char in metrics_path for char in " \t\r\n'\""):
        _fail("OBS_VLLM_METRICS_PATH must be a simple absolute URL path")
    scheme = _first(env, "OBS_VLLM_SCHEME", default="http")
    if scheme not in ("http", "https"):
        _fail("OBS_VLLM_SCHEME must be http or https")
    admin_user = _first(env, "OBS_GRAFANA_ADMIN_USER", default="admin")
    if not USER_RE.fullmatch(admin_user):
        _fail("OBS_GRAFANA_ADMIN_USER must be a simple username")
    # Site default password; override via OBS_GRAFANA_ADMIN_PASSWORD when the
    # stack is reachable beyond a trusted single-operator network.
    admin_password = env.get("OBS_GRAFANA_ADMIN_PASSWORD", "123456")
    if any(ord(char) < 0x20 or ord(char) == 0x7f for char in admin_password):
        _fail("OBS_GRAFANA_ADMIN_PASSWORD contains a control character")
    containers = {key: _safe_component_name(str(value["container"])) for key, value in profile["components"].items()}
    expected_vllm_containers = {
        "head": "dsv4-native432-dspark5-327k-seq5-head",
        "worker": "dsv4-native432-dspark5-327k-seq5-worker",
    }
    container_names = {
        "head": _first(env, "OBS_VLLM_CONTAINER_HEAD", default=expected_vllm_containers["head"]),
        "worker": _first(env, "OBS_VLLM_CONTAINER_WORKER", default=expected_vllm_containers["worker"]),
    }
    log_paths = {
        "head": _first(env, "OBS_VLLM_LOG_PATH_HEAD"),
        "worker": _first(env, "OBS_VLLM_LOG_PATH_WORKER"),
    }
    for role, name in container_names.items():
        if name != expected_vllm_containers[role]:
            _fail(f"OBS_VLLM_CONTAINER_{role.upper()} must identify the production vLLM container")
    for role, path in log_paths.items():
        if path and (not path.startswith("/") or any(char in path for char in "*?[\\r\\n'\"")):
            _fail(f"OBS_VLLM_LOG_PATH_{role.upper()} must be an exact absolute file path")
    fabric_hca = _first(env, "OBS_FABRIC_HCA", default=str(network.get("hca", "")))
    fabric_ndev = _first(env, "OBS_FABRIC_NDEV", default=str(network.get("interface", "")))
    fabric_gid_index = _first(env, "OBS_FABRIC_GID_INDEX", default="")
    for key, value in (("OBS_FABRIC_HCA", fabric_hca), ("OBS_FABRIC_NDEV", fabric_ndev), ("OBS_FABRIC_GID_INDEX", fabric_gid_index)):
        if value and not re.fullmatch(r"[A-Za-z0-9_.-]+", value):
            _fail(f"{key} contains invalid characters")
    docker_proxy = _first(env, "OBS_DOCKER_PROXY")
    if docker_proxy and docker_proxy != "http://127.0.0.1:7890":
        _fail("OBS_DOCKER_PROXY must be http://127.0.0.1:7890")
    return {
        "head_host": head_host, "worker_host": worker_host, "head_ssh": head_ssh,
        "worker_ssh": worker_ssh, "head_user": head_user, "worker_user": worker_user,
        "ssh_port": ssh_port, "ssh_known_hosts": _first(env, "OBS_SSH_KNOWN_HOSTS_FILE", "SSH_KNOWN_HOSTS_FILE"),
        "ssh_identity": _first(env, "OBS_SSH_IDENTITY_FILE", "SSH_IDENTITY_FILE"),
        "remote_root": remote_root, "bind_addr": bind_addr, "vllm_port": vllm_port,
        "metrics_path": metrics_path, "scheme": scheme, "grafana_admin_user": admin_user,
        "grafana_admin_password": admin_password, "ports": ports, "containers": containers,
        "volumes": dict(profile["volumes"]), "head_node_name": _first(env, "OBS_HEAD_NODE_NAME", default="dgx-head"),
        "worker_node_name": _first(env, "OBS_WORKER_NODE_NAME", default="dgx-worker"),
        "vllm_container_names": container_names, "vllm_log_paths": log_paths,
        "fabric_hca": fabric_hca, "fabric_ndev": fabric_ndev, "fabric_gid_index": fabric_gid_index,
        "profile_id": str(profile["profile_id"]), "retention": dict(profile.get("retention", {})),
        "intervals": dict(profile.get("intervals", {})), "images": dict(lock or {}),
        "docker_proxy": docker_proxy,
    }


def _yaml_scalar(value: str) -> str:
    return json.dumps(value)


def render_prometheus(config: Mapping[str, Any]) -> str:
    head, worker, ports = config["head_host"], config["worker_host"], config["ports"]
    return f"""global:
  scrape_interval: {config['intervals'].get('scrape', '15s')}
  evaluation_interval: {config['intervals'].get('evaluation', '15s')}
  external_labels:
    cluster: dgx-spark
    profile: {_yaml_scalar(config['profile_id'])}
rule_files:
  - /etc/prometheus/rules/observability.rules.yml
alerting:
  alertmanagers:
    - static_configs:
        - targets: [\"alertmanager:9093\"]
scrape_configs:
  - job_name: vllm
    metrics_path: {_yaml_scalar(config['metrics_path'])}
    scheme: {_yaml_scalar(config['scheme'])}
    static_configs:
      - targets: [{_yaml_scalar(f'{head}:{config["vllm_port"]}')}]
        labels:
          service: vllm
          node: {_yaml_scalar(config['head_node_name'])}
  - job_name: node
    static_configs:
      - targets:
          - {_yaml_scalar(f'{head}:{ports["node_exporter"]}')}
          - {_yaml_scalar(f'{worker}:{ports["node_exporter"]}')}
        labels:
          service: node-exporter
  - job_name: fabric
    static_configs:
      - targets:
          - {_yaml_scalar(f'{head}:{ports["fabric_exporter"]}')}
          - {_yaml_scalar(f'{worker}:{ports["fabric_exporter"]}')}
        labels:
          service: fabric-exporter
  - job_name: prometheus
    static_configs:
      - targets: [\"prometheus:9090\"]
  - job_name: alertmanager
    static_configs:
      - targets: [\"alertmanager:9093\"]
  - job_name: loki
    metrics_path: /metrics
    static_configs:
      - targets: [\"loki:3100\"]
"""


def render_rules() -> str:
    return """groups:
  - name: dgx-spark-observability
    interval: 15s
    rules:
      - alert: VLLMMetricsDown
        expr: up{job="vllm"} == 0
        for: 2m
        labels:
          severity: critical
          service: vllm
        annotations:
          summary: vLLM metrics endpoint is unavailable
          description: Prometheus cannot scrape the production vLLM metrics endpoint on port 8101.
      - alert: NodeExporterDown
        expr: up{job="node"} == 0
        for: 2m
        labels:
          severity: warning
          service: node-exporter
        annotations:
          summary: DGX node exporter is unavailable
          description: A DGX node exporter has been unreachable for more than two minutes.
      - alert: FabricExporterDown
        expr: up{job="fabric"} == 0
        for: 2m
        labels:
          severity: warning
          service: fabric-exporter
        annotations:
          summary: DGX fabric exporter is unavailable
          description: A fabric exporter has been unreachable for more than two minutes.
      - alert: VLLMKVCacheHigh
        expr: max(vllm:gpu_cache_usage_perc) > 90
        for: 5m
        labels:
          severity: warning
          service: vllm
        annotations:
          summary: vLLM KV cache usage is high
          description: KV cache usage has exceeded 90 percent for five minutes.
      - alert: VLLMRequestsWaiting
        expr: sum(vllm:num_requests_waiting) > 0
        for: 5m
        labels:
          severity: warning
          service: vllm
        annotations:
          summary: vLLM requests are waiting
          description: Requests have remained queued for five minutes.
      - alert: VLLMPreemptions
        expr: increase(vllm:num_preemptions_total[5m]) > 0
        for: 1m
        labels:
          severity: warning
          service: vllm
        annotations:
          summary: vLLM request preemptions increased
          description: vLLM reported one or more request preemptions.
      - alert: FabricGIDInvalid
        expr: dgx_fabric_gid_valid == 0 or dgx_fabric_gid_ipv4_mapped == 0 or dgx_fabric_ndev_match == 0
        for: 1m
        labels:
          severity: critical
          service: fabric
        annotations:
          summary: F1 GID mapping is invalid
          description: The selected F1 GID is empty, not IPv4-mapped, or bound to the wrong interface.
      - alert: FabricNotRoCEv2
        expr: dgx_fabric_rocev2_gid == 0
        for: 1m
        labels:
          severity: critical
          service: fabric
        annotations:
          summary: F1 GID is not RoCE v2
          description: The selected F1 GID is not a valid RoCE v2 mapping.
      - alert: FabricLinkDown
        expr: dgx_fabric_link_up == 0
        for: 1m
        labels:
          severity: critical
          service: fabric
        annotations:
          summary: F1 network carrier is down
          description: The reviewed enp1s0f1np1 carrier is down.
      - alert: VLLMRequestErrors
        expr: increase(vllm:request_success_total{finished_reason="abort"}[5m]) > 0
        for: 5m
        labels:
          severity: warning
          service: vllm
        annotations:
          summary: vLLM reported aborted requests
          description: vLLM has reported aborted requests during the last five minutes.
"""


def render_alertmanager() -> str:
    return """global:
  resolve_timeout: 5m
route:
  receiver: default-null
  group_by: [alertname, service]
  group_wait: 30s
  group_interval: 5m
  repeat_interval: 4h
receivers:
  - name: default-null
"""


def render_loki() -> str:
    return """auth_enabled: false
server:
  http_listen_port: 3100
  grpc_listen_port: 9096
common:
  path_prefix: /loki
  replication_factor: 1
  ring:
    kvstore:
      store: inmemory
  storage:
    filesystem:
      chunks_directory: /loki/chunks
      rules_directory: /loki/rules
schema_config:
  configs:
    - from: 2024-01-01
      store: tsdb
      object_store: filesystem
      schema: v13
      index:
        prefix: index_
        period: 24h
limits_config:
  retention_period: 168h
compactor:
  working_directory: /loki/compactor
  retention_enabled: true
  delete_request_store: filesystem
"""


def render_promtail(config: Mapping[str, Any], role: str) -> str:
    if role == "head":
        target, node = "http://loki:3100/loki/api/v1/push", config["head_node_name"]
    else:
        target, node = f"http://{config['head_host']}:{config['ports']['loki']}/loki/api/v1/push", config["worker_node_name"]
    # The host path is bound to this fixed in-container path by the remote
    # run command. It is deliberately not a wildcard.
    log_path = "/var/log/vllm.log"
    return f"""server:
  http_listen_port: 9080
  grpc_listen_port: 0
positions:
  filename: /var/lib/promtail/positions.yaml
clients:
  - url: {_yaml_scalar(target)}
scrape_configs:
  - job_name: vllm
    static_configs:
      - targets: [localhost]
        labels:
          job: vllm
          node: {_yaml_scalar(node)}
          __path__: {_yaml_scalar(log_path)}
"""


def render_grafana_dashboard() -> str:
    dashboard = {
        "id": None, "uid": "dgx-spark-overview", "title": "DGX Spark overview",
        "schemaVersion": 39, "version": 1, "refresh": "15s",
        "time": {"from": "now-1h", "to": "now"},
        "panels": [
            {"id": 1, "type": "stat", "title": "vLLM metrics", "gridPos": {"h": 5, "w": 6, "x": 0, "y": 0},
             "targets": [{"expr": "up{job=\"vllm\"}", "refId": "A"}]},
            {"id": 2, "type": "timeseries", "title": "vLLM requests running", "gridPos": {"h": 8, "w": 12, "x": 6, "y": 0},
             "targets": [{"expr": "vllm:num_requests_running", "refId": "A"}]},
            {"id": 3, "type": "timeseries", "title": "vLLM request throughput", "gridPos": {"h": 8, "w": 12, "x": 0, "y": 8},
             "targets": [{"expr": "rate(vllm:request_success_total[5m])", "refId": "A"}]},
            {"id": 4, "type": "timeseries", "title": "Node CPU utilisation", "gridPos": {"h": 8, "w": 12, "x": 12, "y": 8},
             "targets": [{"expr": "100 - (avg by(instance) (rate(node_cpu_seconds_total{mode=\"idle\"}[5m])) * 100)", "refId": "A"}]},
            {"id": 5, "type": "timeseries", "title": "Fabric exporter health", "gridPos": {"h": 8, "w": 12, "x": 0, "y": 16},
             "targets": [{"expr": "up{job=\"fabric\"}", "refId": "A"}]},
            {"id": 6, "type": "timeseries", "title": "vLLM KV cache usage", "gridPos": {"h": 8, "w": 12, "x": 12, "y": 16},
             "targets": [{"expr": "vllm:kv_cache_usage_perc", "refId": "A"}]},
            {"id": 7, "type": "timeseries", "title": "vLLM waiting requests", "gridPos": {"h": 8, "w": 12, "x": 0, "y": 24},
             "targets": [{"expr": "vllm:num_requests_waiting", "refId": "A"}]},
            {"id": 8, "type": "timeseries", "title": "vLLM preemptions", "gridPos": {"h": 8, "w": 12, "x": 12, "y": 24},
             "targets": [{"expr": "rate(vllm:num_preemptions_total[5m])", "refId": "A"}]},
            {"id": 9, "type": "timeseries", "title": "F1 GID validity", "gridPos": {"h": 8, "w": 12, "x": 0, "y": 32},
             "targets": [{"expr": "dgx_fabric_gid_valid", "refId": "A"}, {"expr": "dgx_fabric_rocev2_gid", "refId": "B"}]},
            {"id": 10, "type": "timeseries", "title": "F1 link and MTU", "gridPos": {"h": 8, "w": 12, "x": 12, "y": 32},
             "targets": [{"expr": "dgx_fabric_link_up", "refId": "A"}]},
            {"id": 11, "type": "logs", "title": "NCCL/RoCE errors", "gridPos": {"h": 8, "w": 24, "x": 0, "y": 40},
             "targets": [{"expr": "{job=\"vllm\"} |~ \"(?i)NCCL|RoCE|RDMA|timeout|preempt\"", "refId": "A", "datasource": {"type": "loki", "uid": "dgx-loki"}}]},
            {"id": 12, "type": "timeseries", "title": "External KV cache hit rate (LMCache)", "gridPos": {"h": 8, "w": 12, "x": 0, "y": 48},
             "fieldConfig": {"defaults": {"unit": "percentunit", "max": 1, "min": 0}, "overrides": []},
             "targets": [{"expr": "sum(rate(vllm:external_prefix_cache_hits_total[5m])) / sum(rate(vllm:external_prefix_cache_queries_total[5m]))", "refId": "A", "legendFormat": "external hit rate"}]},
            {"id": 13, "type": "timeseries", "title": "Local prefix cache hit rate", "gridPos": {"h": 8, "w": 12, "x": 12, "y": 48},
             "fieldConfig": {"defaults": {"unit": "percentunit", "max": 1, "min": 0}, "overrides": []},
             "targets": [{"expr": "sum(rate(vllm:prefix_cache_hits_total[5m])) / sum(rate(vllm:prefix_cache_queries_total[5m]))", "refId": "A", "legendFormat": "local hit rate"}]},
            {"id": 14, "type": "timeseries", "title": "Token throughput (input/output)", "gridPos": {"h": 8, "w": 12, "x": 0, "y": 56},
             "fieldConfig": {"defaults": {"unit": "tps"}, "overrides": []},
             "targets": [{"expr": "sum(rate(vllm:prompt_tokens_total[5m]))", "refId": "A", "legendFormat": "input tokens/s"},
                          {"expr": "sum(rate(vllm:generation_tokens_total[5m]))", "refId": "B", "legendFormat": "output tokens/s"}]},
            {"id": 15, "type": "timeseries", "title": "Prompt tokens by source (cache-hit input)", "gridPos": {"h": 8, "w": 12, "x": 12, "y": 56},
             "fieldConfig": {"defaults": {"unit": "tps"}, "overrides": []},
             "targets": [{"expr": "sum by (source) (rate(vllm:prompt_tokens_by_source_total[5m]))", "refId": "A", "legendFormat": "{{source}}"},
                          {"expr": "sum(rate(vllm:external_prefix_cache_hits_total[5m]))", "refId": "B", "legendFormat": "external hit input tokens/s"}]},
        ],
        "templating": {"list": []}, "annotations": {"list": []},
    }
    return json.dumps(dashboard, indent=2) + "\n"


def render_grafana_provisioning() -> dict[str, str]:
    return {
        "grafana/provisioning/datasources/datasources.yaml": """apiVersion: 1
datasources:
  - name: Prometheus
    uid: dgx-prometheus
    type: prometheus
    access: proxy
    url: http://prometheus:9090
    isDefault: true
  - name: Loki
    uid: dgx-loki
    type: loki
    access: proxy
    url: http://loki:3100
""",
        "grafana/provisioning/dashboards/dashboards.yaml": """apiVersion: 1
providers:
  - name: dgx-spark
    orgId: 1
    folder: DGX Spark
    type: file
    disableDeletion: true
    updateIntervalSeconds: 30
    options:
      path: /var/lib/grafana/dashboards
""",
    }


def render_files(config: Mapping[str, Any]) -> dict[str, bytes]:
    fabric_source = (Path(__file__).with_name("fabric_exporter.py")).read_bytes()
    files: dict[str, bytes] = {
        "prometheus/prometheus.yml": render_prometheus(config).encode(),
        "prometheus/rules/observability.rules.yml": render_rules().encode(),
        "alertmanager/alertmanager.yml": render_alertmanager().encode(),
        "loki/loki.yml": render_loki().encode(),
        "grafana/dashboards/dgx-spark-overview.json": render_grafana_dashboard().encode(),
        "fabric/fabric_exporter.py": fabric_source,
    }
    files.update({key: value.encode() for key, value in render_grafana_provisioning().items()})
    files["promtail/head.yml"] = render_promtail(config, "head").encode()
    files["promtail/worker.yml"] = render_promtail(config, "worker").encode()
    return files


def _ssh_args(config: Mapping[str, Any], role: str) -> list[str]:
    user = config["head_user"] if role == "head" else config["worker_user"]
    host = config["head_ssh"] if role == "head" else config["worker_ssh"]
    args = ["ssh", "-T", "-p", str(config["ssh_port"])]
    known_hosts = config.get("ssh_known_hosts", "")
    if known_hosts:
        args += ["-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={known_hosts}"]
    else:
        args += ["-o", "StrictHostKeyChecking=accept-new"]
    identity = config.get("ssh_identity", "")
    if identity:
        args += ["-i", identity]
    args.append(f"{user}@{host}")
    return args


def _scp_args(config: Mapping[str, Any], role: str) -> list[str]:
    user = config["head_user"] if role == "head" else config["worker_user"]
    host = config["head_ssh"] if role == "head" else config["worker_ssh"]
    args = ["scp", "-q", "-P", str(config["ssh_port"])]
    known_hosts = config.get("ssh_known_hosts", "")
    if known_hosts:
        args += ["-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={known_hosts}"]
    else:
        args += ["-o", "StrictHostKeyChecking=accept-new"]
    identity = config.get("ssh_identity", "")
    if identity:
        args += ["-i", identity]
    args.append(f"{user}@{host}")
    return args


def _run_local(args: Sequence[str], *, input_text: str | None = None, dry_run: bool = False) -> str:
    if dry_run:
        print("DRY-RUN", " ".join(_quote(str(item)) for item in args))
        if input_text:
            safe = re.sub(r"GF_SECURITY_ADMIN_PASSWORD=.*", "GF_SECURITY_ADMIN_PASSWORD=[secret omitted]", input_text)
            print(safe.rstrip())
        return ""
    try:
        completed = subprocess.run(list(args), input=input_text, text=True, check=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError as exc:
        _fail(f"required executable not found: {exc.filename}")
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        _fail(f"command failed ({exc.returncode}): {' '.join(args[:3])}; {detail}")
    return completed.stdout


def _remote_script(config: Mapping[str, Any], role: str, action: str, *, purge: bool = False) -> str:
    root = config["remote_root"]
    components = HEAD_COMPONENTS if role == "head" else WORKER_COMPONENTS
    containers, volumes, images = config["containers"], config["volumes"], config["images"]
    network_name = f"{STACK_NAME}-net"
    docker_proxy = str(config.get("docker_proxy", ""))
    if action == "up":
        vllm_container = config["vllm_container_names"][role]
        configured_log_path = config["vllm_log_paths"].get(role, "")
    lines = [
        "#!/usr/bin/env bash", "set -euo pipefail", f"ROOT={_quote(root)}",
        f"NETWORK={_quote(network_name)}", "LABEL_KEY=com.dgx-spark.observability",
        "LABEL=dgx-spark-observability",
        "owned_container() {",
        "  local name=$1 stack_label component_label",
        "  if ! docker inspect \"$name\" >/dev/null 2>&1; then return 1; fi",
        "  stack_label=$(docker inspect -f '{{ index .Config.Labels \"com.dgx-spark.observability\" }}' \"$name\" 2>/dev/null || true)",
        "  component_label=$(docker inspect -f '{{ index .Config.Labels \"com.dgx-spark.observability.component\" }}' \"$name\" 2>/dev/null || true)",
        "  [[ \"$stack_label\" == \"$LABEL\" || ( ( \"$stack_label\" == \"\" || \"$stack_label\" == \"<no value>\" ) && \"$component_label\" == \"$name\" ) ]] || { echo \"refusing unowned container: $name\" >&2; return 2; }",
        "}",
        "owned_volume() {",
        "  local name=$1 labels",
        "  if ! docker volume inspect \"$name\" >/dev/null 2>&1; then docker volume create --label \"$LABEL_KEY=$LABEL\" \"$name\" >/dev/null; return; fi",
        "  labels=$(docker volume inspect -f '{{json .Labels}}' \"$name\" 2>/dev/null || true)",
        "  [[ \"$labels\" == *'\"com.dgx-spark.observability\"'* ]] || { echo \"refusing unowned volume: $name\" >&2; return 2; }",
        "}",
        "owned_network() {",
        "  local labels",
        "  if ! docker network inspect \"$NETWORK\" >/dev/null 2>&1; then docker network create --label \"$LABEL_KEY=$LABEL\" \"$NETWORK\" >/dev/null; return; fi",
        "  labels=$(docker network inspect -f '{{json .Labels}}' \"$NETWORK\" 2>/dev/null || true)",
        "  [[ \"$labels\" == *'\"com.dgx-spark.observability\"'* ]] || { echo \"refusing unowned network: $NETWORK\" >&2; return 2; }",
        "}",
    ]
    if docker_proxy:
        lines.append(f"DOCKER_PROXY={_quote(docker_proxy)}")
    if action == "down":
        for component in components:
            name = containers[component]
            lines.append(f"if owned_container {_quote(name)}; then docker rm -f {_quote(name)} >/dev/null; fi")
        lines += [
            "if docker network inspect \"$NETWORK\" >/dev/null 2>&1; then",
            "  owned_network",
            "  docker network rm \"$NETWORK\" >/dev/null || true", "fi",
        ]
        if purge:
            purge_keys = (
                ("prometheus", "grafana", "alertmanager", "loki", "promtail_head")
                if role == "head" else ("promtail_worker",)
            )
            for key in purge_keys:
                lines.append(f"if docker volume inspect {_quote(volumes[key])} >/dev/null 2>&1; then owned_volume {_quote(volumes[key])}; docker volume rm {_quote(volumes[key])} >/dev/null; fi")
        return "\n".join(lines) + "\n"
    if action == "status":
        lines.append("docker ps -a --filter label=\"$LABEL_KEY=$LABEL\" --format '{{.Names}}\\t{{.Status}}\\t{{.Image}}' || true")
        return "\n".join(lines) + "\n"
    if action == "verify":
        for component in components:
            name = containers[component]
            lines += [
                f"owned_container {_quote(name)}",
                f"state=$(docker inspect -f '{{{{.State.Status}}}}' {_quote(name)})",
                f"if [[ \"$state\" != running ]]; then echo {_quote('observability container not running: ' + name)}\" state=$state\" >&2; exit 1; fi",
            ]
        return "\n".join(lines) + "\n"

    if action == "up":
        lines += [
            f"VLLM_CONTAINER={_quote(vllm_container)}",
            "VLLM_LOG_PATH=$(docker inspect -f '{{.LogPath}}' \"$VLLM_CONTAINER\" 2>/dev/null || true)",
            "if [[ -z \"$VLLM_LOG_PATH\" || \"$VLLM_LOG_PATH\" != /* ]]; then echo \"Docker LogPath for exact vLLM container is empty or not absolute: $VLLM_CONTAINER\" >&2; exit 1; fi",
        ]
        if configured_log_path:
            lines.append(f"test \"$VLLM_LOG_PATH\" = {_quote(configured_log_path)}")
    lines += ["owned_network"]
    for component in components:
        name = containers[component]
        lines.append(f"if docker inspect {_quote(name)} >/dev/null 2>&1; then owned_container {_quote(name)}; docker rm -f {_quote(name)} >/dev/null; fi")
    volume_keys = (
        ("prometheus", "grafana", "alertmanager", "loki", "promtail_head")
        if role == "head" else ("promtail_worker",)
    )
    for key in volume_keys:
        lines.append(f"owned_volume {_quote(volumes[key])}")

    def image(key: str) -> str:
        ref = images.get(key)
        if not isinstance(ref, str) or not DIGEST_REF_RE.fullmatch(ref):
            _fail(f"missing immutable image reference for {key}")
        return ref

    def run(name: str, key: str, args: str = "", mounts: str = "", portspec: str = "", envspec: str = "") -> None:
        ref = image(key)
        image_var = f"DGX_OBS_IMAGE_{key.upper()}"
        cache_tag = f"dgx-observe-cache:{key}"
        lines.append(f"{image_var}={_quote(ref)}")
        lines.append(f"if ! docker image inspect \"${image_var}\" >/dev/null 2>&1 && docker image inspect {_quote(cache_tag)} >/dev/null 2>&1; then {image_var}={_quote(cache_tag)}; fi")
        docker_run = "docker run -d --restart unless-stopped"
        if docker_proxy:
            docker_run = 'HTTPS_PROXY="$DOCKER_PROXY" HTTP_PROXY="$DOCKER_PROXY" ' + docker_run
        parts = [docker_run, f"--name {_quote(name)}",
                 "--label \"$LABEL_KEY=$LABEL\"", f"--label {_quote('com.dgx-spark.observability.component=' + name)}",
                 "--network \"$NETWORK\""]
        network_alias = {"loki": "loki", "alertmanager": "alertmanager", "prometheus": "prometheus"}.get(key)
        if network_alias:
            parts.append(f"--network-alias {_quote(network_alias)}")
        for item in (portspec, envspec, mounts):
            if item:
                parts.append(item)
        parts.append("--log-opt max-size=20m --log-opt max-file=3")
        parts.append(f"\"${image_var}\"")
        if args:
            parts.append(args)
        lines.append(" ".join(parts) + " >/dev/null")

    rootq = '"$ROOT"'
    log_mount = '--mount type=bind,src="$VLLM_LOG_PATH",dst=/var/log/vllm.log,readonly'
    if role == "head":
        run(containers["loki"], "loki", "-config.file=/etc/loki/loki.yml",
            mounts=f"--mount type=bind,src={rootq}/config/loki/loki.yml,dst=/etc/loki/loki.yml,readonly --mount type=volume,src={_quote(volumes['loki'])},dst=/loki",
            portspec=f"--publish {_quote(str(config['bind_addr']) + ':' + str(config['ports']['loki']) + ':3100')}")
        run(containers["alertmanager"], "alertmanager", "--config.file=/etc/alertmanager/alertmanager.yml --storage.path=/alertmanager",
            f"--mount type=bind,src={rootq}/config/alertmanager/alertmanager.yml,dst=/etc/alertmanager/alertmanager.yml,readonly --mount type=volume,src={_quote(volumes['alertmanager'])},dst=/alertmanager",
            f"--publish {_quote(str(config['bind_addr']) + ':' + str(config['ports']['alertmanager']) + ':9093')}")
        run(containers["prometheus"], "prometheus",
            f"--config.file=/etc/prometheus/prometheus.yml --storage.tsdb.path=/prometheus --storage.tsdb.retention.time={_quote(str(config['retention'].get('prometheus_time', '7d')))} --storage.tsdb.retention.size={_quote(str(config['retention'].get('prometheus_size', '10GB')))}",
            f"--mount type=bind,src={rootq}/config/prometheus/prometheus.yml,dst=/etc/prometheus/prometheus.yml,readonly --mount type=bind,src={rootq}/config/prometheus/rules/observability.rules.yml,dst=/etc/prometheus/rules/observability.rules.yml,readonly --mount type=volume,src={_quote(volumes['prometheus'])},dst=/prometheus",
            f"--publish {_quote(str(config['bind_addr']) + ':' + str(config['ports']['prometheus']) + ':9090')}")
        run(containers["grafana"], "grafana",
            mounts=f"--mount type=volume,src={_quote(volumes['grafana'])},dst=/var/lib/grafana --mount type=bind,src={rootq}/config/grafana/provisioning,dst=/etc/grafana/provisioning,readonly --mount type=bind,src={rootq}/config/grafana/dashboards,dst=/var/lib/grafana/dashboards,readonly",
            portspec=f"--publish {_quote(str(config['bind_addr']) + ':' + str(config['ports']['grafana']) + ':3000')}",
            envspec=f"--env-file {rootq}/grafana.env")
    promtail_key = "promtail_head" if role == "head" else "promtail_worker"
    promtail_role = "head" if role == "head" else "worker"
    run(containers[promtail_key], "promtail", "-config.file=/etc/promtail/config.yml",
        f"--mount type=bind,src={rootq}/config/promtail/{promtail_role}.yml,dst=/etc/promtail/config.yml,readonly --mount type=volume,src={_quote(volumes[promtail_key])},dst=/var/lib/promtail {log_mount}")
    node_key = "node_exporter_head" if role == "head" else "node_exporter_worker"
    run(containers[node_key], "node_exporter",
        " ".join(_quote(argument) for argument in (
            "--path.rootfs=/host",
            "--collector.filesystem.mount-points-exclude=^/(sys|proc|dev|host|etc)($|/)",
        )),
        mounts="--mount type=bind,src=/,dst=/host,readonly,bind-propagation=rslave",
        portspec=f"--publish {_quote(str(config['bind_addr']) + ':' + str(config['ports']['node_exporter']) + ':9100')}")
    fabric_key = "fabric_exporter_head" if role == "head" else "fabric_exporter_worker"
    run(containers[fabric_key], "fabric_exporter", "python3 /opt/fabric_exporter.py",
        mounts=f"--mount type=bind,src={rootq}/config/fabric/fabric_exporter.py,dst=/opt/fabric_exporter.py,readonly --mount type=bind,src=/sys,dst=/host/sys,readonly",
        portspec=f"--publish {_quote(str(config['bind_addr']) + ':' + str(config['ports']['fabric_exporter']) + ':9100')}",
        envspec=" ".join(
            _quote(f"--env={key}={value}")
            for key, value in (
                ("FABRIC_HCA", config["fabric_hca"]),
                ("FABRIC_NDEV", config["fabric_ndev"]),
                ("FABRIC_GID_INDEX", config["fabric_gid_index"] or "4"),
                ("FABRIC_LISTEN_PORT", "9100"),
            )
        ))
    return "\n".join(lines) + "\n"


def _write_files(directory: Path, files: Mapping[str, bytes]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for relative, content in files.items():
        destination = directory / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        # Configs are non-secret and must be readable by the upstream image
        # users (Prometheus/Loki/Grafana commonly run without root).
        destination.chmod(0o644)


def _grafana_env(config: Mapping[str, Any], *, require_password: bool) -> bytes:
    password = str(config.get("grafana_admin_password", ""))
    if require_password and not password:
        _fail("OBS_GRAFANA_ADMIN_PASSWORD is required for deploy")
    escaped = password.replace("\\", "\\\\").replace("\n", "")
    return f"GF_SECURITY_ADMIN_USER={config['grafana_admin_user']}\nGF_SECURITY_ADMIN_PASSWORD={escaped}\nGF_AUTH_ANONYMOUS_ENABLED=false\n".encode()


def _remote_prepare(config: Mapping[str, Any], role: str, files: Mapping[str, bytes], *, dry_run: bool) -> None:
    root = config["remote_root"]
    ssh = _ssh_args(config, role)
    mkdir = f"set -eu\nmkdir -p {_quote(root + '/config')}\nchmod 755 {_quote(root)}\n"
    _run_local(ssh + ["bash", "-s"], input_text=mkdir, dry_run=dry_run)
    role_files = dict(files)
    if role == "worker":
        for key in list(role_files):
            if key.startswith(("prometheus/", "alertmanager/", "loki/", "grafana/")):
                role_files.pop(key, None)
    dirs = sorted({str(Path(relative).parent) for relative in role_files if str(Path(relative).parent) != "."})
    if not dry_run:
        if dirs:
            mkdir = "set -eu\n" + "\n".join(f"mkdir -p {_quote(root + '/config/' + directory)}" for directory in dirs) + "\n"
            _run_local(ssh + ["bash", "-s"], input_text=mkdir)
    for relative, content in role_files.items():
        if dry_run:
            print(f"DRY-RUN upload {role} {relative} ({len(content)} bytes)")
            continue
        with tempfile.NamedTemporaryFile(prefix="dgx-observe-", suffix=".tmp", delete=False) as temporary:
            temporary.write(content)
            temporary_path = Path(temporary.name)
        try:
            destination = f"{_host_target(config, role)}:{root}/config/{relative}"
            _run_local(_scp_args(config, role)[:-1] + [str(temporary_path), destination])
        finally:
            temporary_path.unlink(missing_ok=True)
    if not dry_run:
        permissions = ["set -eu", f"chmod 755 {_quote(root + '/config')}"]
        permissions.extend(f"chmod 755 {_quote(root + '/config/' + directory)}" for directory in dirs)
        permissions.extend(f"chmod 644 {_quote(root + '/config/' + relative)}" for relative in role_files)
        _run_local(ssh + ["bash", "-s"], input_text="\n".join(permissions) + "\n")
    if role == "head":
        if dry_run:
            print("DRY-RUN upload head grafana.env ([secret omitted])")
        else:
            with tempfile.NamedTemporaryFile(prefix="dgx-grafana-", suffix=".env", delete=False) as temporary:
                temporary.write(_grafana_env(config, require_password=True))
                temporary_path = Path(temporary.name)
            try:
                destination = f"{_host_target(config, role)}:{root}/grafana.env"
                _run_local(_scp_args(config, role)[:-1] + [str(temporary_path), destination])
                _run_local(ssh + ["bash", "-s"], input_text=f"set -eu\nchmod 600 {_quote(root + '/grafana.env')}\n")
            finally:
                temporary_path.unlink(missing_ok=True)
def _remote_proxy_prepare(config: Mapping[str, Any], role: str, *, dry_run: bool) -> None:
    proxy = str(config.get("docker_proxy", ""))
    if not proxy:
        return
    proxy_literal = _quote(proxy)
    info_check = (
        "set -eu\n"
        f"PROXY={proxy_literal}\n"
        "INFO=$(docker info 2>/dev/null || true)\n"
        "if [[ \"$INFO\" == *\"HTTP Proxy: $PROXY\"* && \"$INFO\" == *\"HTTPS Proxy: $PROXY\"* ]]; then exit 0; fi\n"
    )
    if role == "head":
        info_check += "echo 'DGX head Docker daemon is missing the configured registry proxy' >&2\nexit 1\n"
    else:
        document = json.dumps({
            "proxies": {
                "http-proxy": proxy,
                "https-proxy": proxy,
                "no-proxy": "localhost,127.0.0.1,192.168.100.0/24",
            }
        })
        info_check += (
            "if [ -e /etc/docker/daemon.json ]; then\n"
            "  echo 'refusing to overwrite existing /etc/docker/daemon.json' >&2\n"
            "  exit 1\n"
            "fi\n"
            "tmp=$(mktemp)\n"
            "trap 'rm -f \"$tmp\"' EXIT\n"
            "sudo -n mkdir -p /etc/docker\n"
            f"printf '%s\\n' {_quote(document)} >\"$tmp\"\n"
            "sudo -n install -o root -g root -m 0644 \"$tmp\" /etc/docker/daemon.json\n"
            "sudo -n systemctl daemon-reload\n"
            "sudo -n systemctl reload docker\n"
            "INFO=$(docker info 2>/dev/null || true)\n"
            "if [[ \"$INFO\" == *\"HTTP Proxy: $PROXY\"* && \"$INFO\" == *\"HTTPS Proxy: $PROXY\"* ]]; then exit 0; fi\n"
            "echo 'DGX worker Docker daemon did not report the configured registry proxy after reload' >&2\n"
            "exit 1\n"
        )
    _run_local(_ssh_args(config, role) + ["bash", "-s"], input_text=info_check, dry_run=dry_run)




def _host_target(config: Mapping[str, Any], role: str) -> str:
    user = config["head_user"] if role == "head" else config["worker_user"]
    host = config["head_ssh"] if role == "head" else config["worker_ssh"]
    return f"{user}@{host}"
def _stage_worker_images(config: Mapping[str, Any], *, dry_run: bool) -> None:
    refs: list[tuple[str, str]] = []
    for key in ("promtail", "node_exporter", "fabric_exporter"):
        ref = config["images"].get(key)
        if not isinstance(ref, str) or not DIGEST_REF_RE.fullmatch(ref):
            _fail(f"missing immutable image reference for {key}")
        refs.append((key, ref))
    archive = f"/tmp/{STACK_NAME}-worker-images.tar"
    if dry_run:
        print("DRY-RUN stage locked worker images from DGX head to worker")
        return
    proxy = str(config.get("docker_proxy", ""))
    proxy_prefix = ""
    if proxy:
        proxy_prefix = f"HTTPS_PROXY={_quote(proxy)} HTTP_PROXY={_quote(proxy)} "
    pull_save = ["set -eu", f"ARCHIVE={_quote(archive)}"]
    pull_save.extend(
        f"if ! docker image inspect {_quote(ref)} >/dev/null 2>&1; then "
        f"{proxy_prefix}docker pull --platform {_quote(ARCHITECTURE)} {_quote(ref)} >/dev/null; fi\n"
        f"docker tag {_quote(ref)} {_quote('dgx-observe-cache:' + key)}"
        for key, ref in refs
    )
    pull_save.append(f"docker save --output \"$ARCHIVE\" {' '.join(_quote('dgx-observe-cache:' + key) for key, _ in refs)}")
    _run_local(
        _ssh_args(config, "head") + ["bash", "-s"],
        input_text="\n".join(pull_save) + "\n",
    )
    with tempfile.NamedTemporaryFile(prefix="dgx-observability-images-", suffix=".tar", delete=False) as temporary:
        local_path = Path(temporary.name)
    try:
        _run_local(
            _scp_args(config, "head")[:-1]
            + [f"{_host_target(config, 'head')}:{archive}", str(local_path)]
        )
        _run_local(
            _scp_args(config, "worker")[:-1]
            + [str(local_path), f"{_host_target(config, 'worker')}:{archive}"]
        )
        _run_local(
            _ssh_args(config, "worker") + ["bash", "-s"],
            input_text=f"set -eu\ndocker load --input {_quote(archive)} >/dev/null\nrm -f {_quote(archive)}\n",
        )
    finally:
        local_path.unlink(missing_ok=True)
        for role in ("head", "worker"):
            try:
                _run_local(
                    _ssh_args(config, role) + ["bash", "-s"],
                    input_text=f"set -eu\nrm -f {_quote(archive)}\n",
                )
            except ObservabilityError:
                pass




def _run_action(config: Mapping[str, Any], profile: Mapping[str, Any], action: str, *, dry_run: bool = False, purge: bool = False) -> None:
    if action == "up":
        files = render_files(config)
        _stage_worker_images(config, dry_run=dry_run)
        for role in ("head", "worker"):
            _remote_proxy_prepare(config, role, dry_run=dry_run)
            _remote_prepare(config, role, files, dry_run=dry_run)
            _run_local(_ssh_args(config, role) + ["bash", "-s"], input_text=_remote_script(config, role, "up"), dry_run=dry_run)
        return
    for role in ("head", "worker"):
        output = _run_local(_ssh_args(config, role) + ["bash", "-s"], input_text=_remote_script(config, role, action, purge=purge), dry_run=dry_run)
        if output:
            print(f"[{role}]\n{output}", end="")


def _load_inputs(args: argparse.Namespace, *, need_lock: bool) -> tuple[dict[str, str], dict[str, Any], dict[str, str] | None]:
    env = parse_env(Path(args.env_file)) if args.env_file else {}
    profile = load_profile(Path(args.profile or env.get("OBS_PROFILE_FILE") or DEFAULT_PROFILE))
    lock_path = getattr(args, "image_lock", None) or env.get("OBS_IMAGE_LOCK_FILE")
    lock = load_lock(Path(lock_path)) if need_lock and lock_path else None
    if need_lock and lock is None:
        _fail("--image-lock or OBS_IMAGE_LOCK_FILE is required for deploy")
    return env, profile, lock


def cmd_render(args: argparse.Namespace) -> int:
    env, profile, lock = _load_inputs(args, need_lock=False)
    if args.allow_mutable_images:
        lock = dict(DEFAULT_IMAGE_TAGS)
    elif args.image_lock:
        lock = load_lock(Path(args.image_lock))
    config = _config(env, profile, lock, require_remote=False)
    output = Path(args.output)
    _validate_abs_path(str(output), "--output", external=False)
    _write_files(output, render_files(config))
    print(f"rendered observability files under {output}")
    return 0


def _check_vllm(config: Mapping[str, Any], *, dry_run: bool = False) -> None:
    base = f"{config['scheme']}://{config['head_host']}:{config['vllm_port']}"
    paths = ("/health", "/v1/models", config["metrics_path"])
    lines = ["set -eu"]
    for path in paths:
        lines.append(f"curl --fail --silent --show-error --max-time 15 {_quote(base + path)} >/dev/null")
    _run_local(_ssh_args(config, "head") + ["bash", "-s"], input_text="\n".join(lines) + "\n", dry_run=dry_run)
def _smoke_vllm(config: Mapping[str, Any], *, dry_run: bool = False) -> None:
    base = f"{config['scheme']}://{config['head_host']}:{config['vllm_port']}"
    lines = [
        "set -eu",
        f"BASE={_quote(base)}",
        "models=$(curl --fail --silent --show-error --max-time 15 \"$BASE/v1/models\")",
        "model=$(printf '%s' \"$models\" | python3 -c 'import json,sys; data=json.load(sys.stdin).get(\"data\", []); print(data[0].get(\"id\", \"\") if data and isinstance(data[0], dict) else \"\")')",
        "test -n \"$model\"",
        "payload=$(MODEL=\"$model\" python3 -c 'import json,os; print(json.dumps({\"model\": os.environ[\"MODEL\"], \"prompt\": \"Hello\", \"max_tokens\": 1, \"temperature\": 0}))')",
        "response=$(curl --fail --silent --show-error --max-time 30 -H 'Content-Type: application/json' --data \"$payload\" \"$BASE/v1/completions\")",
        "RESPONSE=\"$response\" python3 -c 'import json,os; value=json.loads(os.environ[\"RESPONSE\"]); choices=value.get(\"choices\"); usage=value.get(\"usage\", {}); assert isinstance(choices,list) and choices and isinstance(choices[0],dict) and isinstance(choices[0].get(\"text\"),str); assert usage.get(\"completion_tokens\") == 1, usage'",
    ]
    _run_local(_ssh_args(config, "head") + ["bash", "-s"], input_text="\n".join(lines) + "\n", dry_run=dry_run)




def cmd_check_vllm(args: argparse.Namespace) -> int:
    env, profile, _ = _load_inputs(args, need_lock=False)
    config = _config(env, profile, None)
    _check_vllm(config, dry_run=args.dry_run)
    if args.smoke_one_token:
        _smoke_vllm(config, dry_run=args.dry_run)
    if args.dry_run:
        print("dry-run complete; no DGX or production service access performed")
    elif args.smoke_one_token:
        print("production vLLM /health, /v1/models, /metrics, and one-token smoke verified from DGX head")
    else:
        print("production vLLM /health, /v1/models, and /metrics verified read-only from DGX head")
    return 0


def cmd_deploy(args: argparse.Namespace) -> int:
    if args.confirm != "DGX-OBSERVABILITY":
        _fail("--confirm must equal DGX-OBSERVABILITY")
    env, profile, lock = _load_inputs(args, need_lock=True)
    config = _config(env, profile, lock)
    _check_vllm(config, dry_run=args.dry_run)
    _run_action(config, profile, "up", dry_run=args.dry_run)
    if not args.dry_run:
        _check_vllm(config)
    print("dry-run complete; no DGX or Docker mutation performed" if args.dry_run else "observability stack deployed on DGX hosts; production vLLM health remained intact")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    env, profile, _ = _load_inputs(args, need_lock=False)
    _run_action(_config(env, profile, None), profile, "status", dry_run=args.dry_run)
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    env, profile, _ = _load_inputs(args, need_lock=False)
    config = _config(env, profile, None)
    _run_action(config, profile, "verify", dry_run=args.dry_run)
    if not args.dry_run:
        service_hosts = {
            "prometheus": config["bind_addr"],
            "grafana": config["bind_addr"],
            "loki": config["head_host"],
            "alertmanager": config["bind_addr"],
        }
        checks = (
            ("prometheus", config["ports"]["prometheus"], "/-/ready"),
            ("grafana", config["ports"]["grafana"], "/api/health"),
            ("loki", config["ports"]["loki"], "/ready"),
            ("alertmanager", config["ports"]["alertmanager"], "/-/ready"),
        )
        lines = ["set -euo pipefail"]
        for name, port, path in checks:
            host = service_hosts[name]
            url = "http://" + host + ":" + str(port) + path
            lines.append(f"if ! curl --fail --silent --show-error --max-time 10 {_quote(url)} >/dev/null; then echo {_quote('observability endpoint failed: ' + name)} >&2; exit 1; fi")
        for host in (config["head_host"], config["worker_host"]):
            node_url = _quote("http://" + host + ":" + str(config["ports"]["node_exporter"]) + "/metrics")
            fabric_url = _quote("http://" + host + ":" + str(config["ports"]["fabric_exporter"]) + "/metrics")
            lines.append(f"node_metrics=$(curl --fail --silent --show-error --max-time 10 {node_url})")
            lines.append(f"if [[ \"$node_metrics\" != *\"node_exporter_build_info\"* ]]; then echo {_quote('node exporter failed: ' + host)} >&2; exit 1; fi")
            lines.append(f"if ! fabric_metrics=$(curl --fail --silent --show-error --max-time 10 {fabric_url}); then echo {_quote('fabric exporter failed: ' + host)} >&2; exit 1; fi")
            for metric in ("dgx_fabric_gid_valid", "dgx_fabric_gid_ipv4_mapped", "dgx_fabric_gid_type_info", "dgx_fabric_ndev_match", "dgx_fabric_rocev2_gid", "dgx_fabric_mtu", "dgx_fabric_rdma_link_up", "dgx_fabric_link_up"):
                lines.append(f"if [[ \"$fabric_metrics\" != *\"{metric}\"* ]]; then echo {_quote(metric + ' missing: ' + host)} >&2; exit 1; fi")
        vllm_url = config["scheme"] + "://" + config["head_host"] + ":" + str(config["vllm_port"]) + config["metrics_path"]
        lines.append(f"if ! curl --fail --silent --show-error --max-time 10 {_quote(vllm_url)} >/dev/null; then echo 'vLLM metrics endpoint failed' >&2; exit 1; fi")
        _run_local(_ssh_args(config, "head") + ["bash", "-s"], input_text="\n".join(lines) + "\n")
        print("observability endpoints, exporters, and vLLM metrics verified from DGX head")
    return 0


def cmd_down(args: argparse.Namespace) -> int:
    env, profile, _ = _load_inputs(args, need_lock=False)
    _run_action(_config(env, profile, None), profile, "down", dry_run=args.dry_run, purge=args.purge_data)
    if args.dry_run:
        print("dry-run complete; no DGX or Docker mutation performed")
    else:
        print("observability containers removed; persistent volumes " + ("purged" if args.purge_data else "retained"))
    return 0


def cmd_resolve_images(args: argparse.Namespace) -> int:
    env, profile, _ = _load_inputs(args, need_lock=False)
    config = _config(env, profile, None)
    output = Path(args.output)
    _validate_abs_path(str(output), "--output", external=True)
    lines = ["set -eu", "tmp=$(mktemp)", "trap 'rm -f \"$tmp\"' EXIT", "printf '%s\\n' '{' >\"$tmp\""]
    keys = sorted(DEFAULT_IMAGE_TAGS)
    for index, key in enumerate(keys):
        tag = DEFAULT_IMAGE_TAGS[key]
        comma = "," if index < len(keys) - 1 else ""
        lines += [
            f"docker pull --platform {_quote(ARCHITECTURE)} {_quote(tag)} >/dev/null",
            f"ref=$(docker image inspect --format '{{{{index .RepoDigests 0}}}}' {_quote(tag)})",
            "case \"$ref\" in *'@sha256:'*) ;; *) echo 'image has no immutable RepoDigest' >&2; exit 1;; esac",
            f"id=$(docker image inspect --format '{{{{.Id}}}}' {_quote(tag)})",
            f"printf '%s' {_quote('  ' + json.dumps(key) + ': {"reference":"')} >>\"$tmp\"",
            "printf '%s' \"$ref\" >>\"$tmp\"",
            f"printf '%s' {_quote('","digest":"')} >>\"$tmp\"",
            "printf '%s' \"${ref##*@sha256:}\" >>\"$tmp\"",
            f"printf '%s' {_quote('","image_id":"')} >>\"$tmp\"",
            "printf '%s' \"$id\" >>\"$tmp\"",
            f"printf '%s\\n' {_quote('"}' + comma)} >>\"$tmp\"",
        ]
    lines += ["printf '%s\\n' '}' >>\"$tmp\"", "cat \"$tmp\""]
    output_text = _run_local(_ssh_args(config, "head") + ["bash", "-s"], input_text="\n".join(lines) + "\n", dry_run=args.dry_run)
    if args.dry_run:
        print(f"DRY-RUN would write immutable lock to {output}")
        return 0
    try:
        image_entries = json.loads(output_text)
    except json.JSONDecodeError as exc:
        _fail(f"DGX image resolver returned invalid JSON: {exc}")
    if not isinstance(image_entries, dict) or set(image_entries) != set(DEFAULT_IMAGE_TAGS):
        _fail("DGX image resolver returned incomplete image set")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(_lock_document(image_entries), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    output.chmod(0o600)
    print(f"wrote immutable ARM64 observability image lock to {output}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def common(command: argparse.ArgumentParser, *, env_required: bool = False) -> None:
        command.add_argument("--env-file", required=env_required, help="operator-owned KEY=VALUE file outside checkout")
        command.add_argument("--profile", help="observability profile JSON (default: committed DGX profile)")

    render = sub.add_parser("render", help="render configs locally without SSH or Docker")
    common(render)
    render.add_argument("--image-lock", help="external ready image lock")
    render.add_argument("--allow-mutable-images", action="store_true", help="render only; never valid for deploy")
    render.add_argument("--output", required=True)
    render.set_defaults(func=cmd_render)

    deploy = sub.add_parser("deploy", help="deploy labelled observability containers on both DGX hosts")
    common(deploy, env_required=True)
    deploy.add_argument("--image-lock", help="external ready image lock")
    deploy.add_argument("--confirm", required=True, help="type DGX-OBSERVABILITY to authorize remote mutation")
    deploy.add_argument("--dry-run", action="store_true")
    deploy.set_defaults(func=cmd_deploy)
    check = sub.add_parser("check-vllm", help="read-only production vLLM health, models, and metrics check from DGX head")
    common(check, env_required=True)
    check.add_argument("--dry-run", action="store_true")
    check.add_argument("--smoke-one-token", action="store_true", help="run one deterministic completion token after endpoint checks")
    check.set_defaults(func=cmd_check_vllm)

    resolve = sub.add_parser("resolve-images", help="resolve public image tags to ARM64 RepoDigests on DGX")
    common(resolve, env_required=True)
    resolve.add_argument("--output", required=True)
    resolve.add_argument("--dry-run", action="store_true")
    resolve.set_defaults(func=cmd_resolve_images)
    for name, func, help_text in (("status", cmd_status, "show labelled observability containers"), ("verify", cmd_verify, "verify containers and read-only health endpoints"), ("down", cmd_down, "remove labelled observability containers")):
        command = sub.add_parser(name, help=help_text)
        common(command, env_required=True)
        command.add_argument("--dry-run", action="store_true")
        if name == "down":
            command.add_argument("--purge-data", action="store_true")
        command.set_defaults(func=func)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except ObservabilityError as exc:
        print(f"dgx-observability: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("dgx-observability: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
