"""Strict deployment lock parsing for executable lifecycle operations."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping

from .config import ConfigError

REQUIRED_IMAGE_LABELS = (
    "org.opencontainers.image.revision",
    "com.dgx-spark.architecture",
    "com.dgx-spark.profile_sha256",
    "com.dgx-spark.service_contract_sha256",
    "com.dgx-spark.image_lock_sha256",
    "com.dgx-spark.vllm.commit",
    "com.dgx-spark.b12x.commit",
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_IMAGE_REF = re.compile(r"^(?:[A-Za-z0-9._:/-]+@)?sha256:[0-9a-f]{64}$")
_GIT_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_CANDIDATE_REF = re.compile(r"(?:^|[/:._-])candidate(?:[/:._-]|$)", re.IGNORECASE)
_PLACEHOLDER = re.compile(r"(?:<[^>]+>|\b(?:REPLACE|CHANGEME|EXAMPLE|YOUR_VALUE)\b)", re.IGNORECASE)


class LockError(ConfigError):
    """A deployment lock is incomplete, mutable, or inconsistent."""


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise LockError(message)


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    _expect(isinstance(value, Mapping), f"{name} must be an object")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], name: str) -> None:
    actual = set(value)
    missing = sorted(expected - actual)
    unknown = sorted(actual - expected)
    _expect(not missing, f"{name} is missing keys: {', '.join(missing)}")
    _expect(not unknown, f"{name} has unknown keys: {', '.join(unknown)}")


def _read_lock(path: Path) -> tuple[dict[str, Any], str]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LockError(f"cannot read deployment lock: {path}") from exc
    _expect(isinstance(value, dict), "deployment lock must be a JSON object")
    return value, hashlib.sha256(raw).hexdigest()


def _validate_labels(labels: Mapping[str, Any], role: str) -> dict[str, str]:
    _exact_keys(labels, set(REQUIRED_IMAGE_LABELS), f"images.{role}.labels")
    normalized: dict[str, str] = {}
    for key in REQUIRED_IMAGE_LABELS:
        value = labels.get(key)
        _expect(isinstance(value, str) and bool(value), f"images.{role}.labels.{key} must be non-empty")
        _expect(_PLACEHOLDER.search(value) is None, f"images.{role}.labels.{key} contains a placeholder")
        normalized[key] = value
    _expect(normalized["com.dgx-spark.architecture"] == "linux/arm64", f"images.{role} architecture label must be linux/arm64")
    for key in (
        "com.dgx-spark.profile_sha256",
        "com.dgx-spark.service_contract_sha256",
        "com.dgx-spark.image_lock_sha256",
    ):
        _expect(_SHA256.fullmatch(normalized[key]) is not None, f"images.{role}.labels.{key} must be a sha256")
    for key in (
        "org.opencontainers.image.revision",
        "com.dgx-spark.vllm.commit",
        "com.dgx-spark.b12x.commit",
    ):
        _expect(_GIT_COMMIT.fullmatch(normalized[key]) is not None, f"images.{role}.labels.{key} must be a full git commit")
    return normalized


def load_deployment_lock(path: Path, config: Mapping[str, Any]) -> dict[str, Any]:
    """Load a complete per-role image lock and bind it to canonical config."""

    value, lock_sha256 = _read_lock(path)
    _exact_keys(
        value,
        {"schema_version", "status", "mode", "profile_id", "model_manifest_sha256", "images"},
        "deployment lock",
    )
    _expect(value["schema_version"] == 1, "deployment lock schema_version must be 1")
    _expect(value["status"] == "ready", "deployment lock status must be ready")

    deployment = _mapping(config.get("deployment"), "deployment")
    profile = _mapping(config.get("profile"), "profile")
    mode = deployment.get("mode")
    _expect(value["mode"] == mode, "deployment lock mode does not match operator configuration")
    _expect(value["profile_id"] == profile.get("profile_id"), "deployment lock profile_id does not match profile")
    model_sha256 = value.get("model_manifest_sha256")
    _expect(isinstance(model_sha256, str) and _SHA256.fullmatch(model_sha256) is not None, "deployment lock model_manifest_sha256 must be a sha256")
    _expect(model_sha256 == deployment.get("model_manifest_sha256"), "deployment lock model manifest does not match operator configuration")

    images = _mapping(value.get("images"), "images")
    _exact_keys(images, {"head", "worker"}, "images")
    normalized_images: dict[str, Any] = {}
    for role in ("head", "worker"):
        image = _mapping(images.get(role), f"images.{role}")
        _exact_keys(image, {"reference", "image_id", "labels"}, f"images.{role}")
        reference = image.get("reference")
        image_id = image.get("image_id")
        _expect(isinstance(reference, str) and _IMAGE_REF.fullmatch(reference) is not None, f"images.{role}.reference must be immutable")
        _expect(isinstance(image_id, str) and _IMAGE_ID.fullmatch(image_id) is not None, f"images.{role}.image_id must be an exact image ID")
        is_candidate = _CANDIDATE_REF.search(reference) is not None
        if mode == "candidate":
            _expect(is_candidate, f"candidate images.{role}.reference must be candidate-namespaced")
        else:
            _expect(not is_candidate, f"production images.{role}.reference must not be candidate-namespaced")
        normalized_images[role] = {
            "reference": reference,
            "image_id": image_id,
            "labels": _validate_labels(_mapping(image.get("labels"), f"images.{role}.labels"), role),
        }

    normalized = dict(value)
    normalized["images"] = normalized_images
    normalized["lock_sha256"] = lock_sha256
    return normalized
