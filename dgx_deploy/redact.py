"""Redact operator-sensitive values before displaying a plan or result."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

SENSITIVE_KEYS = frozenset(
    {
        "ssh_identity_file",
        "ssh_known_hosts_file",
        "ssh_user",
        "head_ssh_user",
        "worker_ssh_user",
        "head_host",
        "worker_host",
        "head_node_addr",
        "worker_node_addr",
        "model_root",
        "model_container_path",
        "remote_root",
        "state_root",
        "cache_root",
        "log_root",
        "result_root",
        "registry",
        "image_ref",
        "head_image_ref",
        "worker_image_ref",
        "image_lock_file",
        "model_manifest_sha256",
    }
)
_SECRET_TEXT = re.compile(
    r"(?i)(?:bearer\s+|(?:api[_-]?key|access[_-]?token|password)\s*[=:]\s*)[^\s,]+"
)
_IP_TEXT = re.compile(
    r"(?<![A-Za-z0-9])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![A-Za-z0-9])"
    r"|(?<![A-Za-z0-9])(?:\[[0-9A-Fa-f:.]+\]|(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4})(?![A-Za-z0-9])"
)
_PATH_TEXT = re.compile(r"(?<![A-Za-z0-9:/])/(?:[^,\s\"']+)")


def _redact_string(value: str) -> str:
    value = _SECRET_TEXT.sub("<redacted>", value)
    value = _IP_TEXT.sub("<redacted>", value)
    return _PATH_TEXT.sub("<redacted>", value)


def redact_value(key: str, value: Any) -> Any:
    if key in SENSITIVE_KEYS:
        return "<redacted>" if value not in (None, "") else value
    if isinstance(value, Mapping):
        return redact_mapping(value)
    if isinstance(value, list):
        return [redact_value(key, item) for item in value]
    if isinstance(value, str):
        return _redact_string(value)
    return value


def redact_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): redact_value(str(key), item) for key, item in value.items()}


def redact_text(value: str) -> str:
    return _redact_string(value)
