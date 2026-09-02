"""Canonical service-contract generation and validation helpers."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from .config import canonical_json
from .render import render_contract, render_contract_json


class ContractError(ValueError):
    """A rendered service contract is incomplete or has drifted."""


def service_contract_sha256(contract: Mapping[str, Any]) -> str:
    """Hash the contract excluding its self-referential digest field."""

    value = dict(contract)
    value.pop("service_contract_sha256", None)
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def validate_contract(contract: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the executable contract shape and its exact command lock."""

    required = {
        "schema_version",
        "deployment_id",
        "mode",
        "profile_id",
        "role",
        "container",
        "host",
        "node_addr",
        "master_addr",
        "master_port",
        "model_path",
        "model_root",
        "model_manifest_sha256",
        "image",
        "environment",
        "service_argv",
        "container_argv",
        "contract_path",
        "service_contract_sha256",
    }
    missing = sorted(required - set(contract))
    if missing:
        raise ContractError(f"service contract is missing keys: {', '.join(missing)}")
    if contract.get("schema_version") != 1:
        raise ContractError("service contract schema_version must be 1")
    if contract.get("role") not in {"head", "worker"}:
        raise ContractError("service contract role must be head or worker")
    if not isinstance(contract.get("service_argv"), list) or not contract["service_argv"]:
        raise ContractError("service contract service_argv must be non-empty")
    if not isinstance(contract.get("container_argv"), list) or not contract["container_argv"]:
        raise ContractError("service contract container_argv must be non-empty")
    digest = contract.get("service_contract_sha256")
    if not isinstance(digest, str) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ContractError("service_contract_sha256 must be lowercase sha256")
    # A lock supplied by the image build may intentionally own the digest.  In
    # that case render_contract already made it the source of truth; the
    # structural check remains here for contracts read back from disk.
    value = dict(contract)
    value.pop("service_contract_sha256", None)
    computed = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
    if digest != computed:
        raise ContractError("service contract hash does not match canonical content")
    return dict(contract)


def contract_json(config: Mapping[str, Any], role: str, lock: Mapping[str, Any] | None = None) -> str:
    """Render and validate one complete service contract."""

    value = render_contract(config, role, lock)
    # The renderer's digest includes the complete object except its digest.
    digest = service_contract_sha256(value)
    value["service_contract_sha256"] = digest
    validate_contract(value)
    return json.dumps(value, sort_keys=True, indent=2) + "\n"


__all__ = ["ContractError", "contract_json", "render_contract", "render_contract_json", "service_contract_sha256", "validate_contract"]
