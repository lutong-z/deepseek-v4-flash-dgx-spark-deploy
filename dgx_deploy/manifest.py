"""Deterministic external manifest identity helpers."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from .config import canonical_json, config_sha256
from .redact import redact_mapping


def manifest_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def deployment_id(config: Mapping[str, Any]) -> str:
    return "deployment-" + config_sha256(config)[:16]


def redacted_manifest(config: Mapping[str, Any]) -> dict[str, Any]:
    deployment = redact_mapping(dict(config["deployment"]))
    return {
        "schema_version": 1,
        "deployment_id": deployment_id(config),
        "config_sha256": config_sha256(config),
        "profile_id": config["profile"]["profile_id"],
        "deployment": deployment,
    }


def manifest_json(config: Mapping[str, Any]) -> str:
    return json.dumps(redacted_manifest(config), sort_keys=True, indent=2) + "\n"
