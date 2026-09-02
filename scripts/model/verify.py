#!/usr/bin/env python3
"""Verify an installed model against the repository's immutable model lock.

This command is deliberately read-only and never contacts Hugging Face.  It
checks the lock, the lock-file digest marker, every allowlisted file, and the
cross-file model metadata/index invariants before a model is mounted by a
runtime.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LOCK = ROOT / "model.lock.json"
DEFAULT_SCHEMA = ROOT / "config" / "model-lock.schema.json"
DEFAULT_MARKER = ".model-lock.sha256"
_CHUNK_SIZE = 8 * 1024 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
_PATH_RE = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")
_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")


class VerificationError(ValueError):
    """An installed model or its lock violates a required invariant."""


def _reject_duplicate_object_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise VerificationError("JSON contains duplicate object keys")
        result[key] = value
    return result


def _read_json(path: Path, label: str) -> Any:
    try:
        with path.open("rb") as stream:
            return json.load(stream, object_pairs_hook=_reject_duplicate_object_keys)
    except FileNotFoundError as exc:
        raise VerificationError(f"{label} is missing: {path}") from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VerificationError(f"{label} is not valid JSON: {path}") from exc


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise VerificationError(f"{label} must be an object")
    return value


def _require_exact_keys(value: Mapping[str, Any], required: Iterable[str], label: str) -> None:
    required_set = set(required)
    actual = set(value)
    missing = sorted(required_set - actual)
    extra = sorted(actual - required_set)
    if missing:
        raise VerificationError(f"{label} is missing required field(s): {', '.join(missing)}")
    if extra:
        raise VerificationError(f"{label} has unexpected field(s): {', '.join(extra)}")


def _require_string(value: Any, label: str, pattern: re.Pattern[str] | None = None) -> str:
    if not isinstance(value, str) or not value:
        raise VerificationError(f"{label} must be a non-empty string")
    if pattern is not None and pattern.fullmatch(value) is None:
        raise VerificationError(f"{label} has an invalid value")
    return value


def _require_int(value: Any, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise VerificationError(f"{label} must be an integer >= {minimum}")
    return value


def _require_bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise VerificationError(f"{label} must be a boolean")
    return value


def _require_number(value: Any, label: str, *, minimum: float | None = None, maximum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise VerificationError(f"{label} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise VerificationError(f"{label} must be finite")
    if minimum is not None and number <= minimum:
        raise VerificationError(f"{label} must be greater than {minimum}")
    if maximum is not None and number > maximum:
        raise VerificationError(f"{label} must be <= {maximum}")
    return number


def validate_relative_path(value: Any, label: str = "path") -> str:
    """Return a lock path after rejecting absolute paths and traversal."""

    path = _require_string(value, label)
    if not _PATH_RE.fullmatch(path) or path.startswith("/") or "\\" in path:
        raise VerificationError(f"{label} is not a safe relative path")
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise VerificationError(f"{label} is not a safe relative path")
    return path


def _validate_schema_document(schema_path: Path) -> None:
    """Fail closed if the checked-in lock schema is missing or malformed."""

    schema = _read_json(schema_path, "model-lock schema")
    schema_obj = _require_mapping(schema, "model-lock schema")
    if schema_obj.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
        raise VerificationError("model-lock schema has an unexpected draft")
    if schema_obj.get("type") != "object" or schema_obj.get("additionalProperties") is not False:
        raise VerificationError("model-lock schema must be a closed object schema")


def _validate_file_entry(value: Any, index: int) -> dict[str, Any]:
    entry = _require_mapping(value, f"lock files[{index}]")
    required = ("path", "size", "role")
    for key in required:
        if key not in entry:
            raise VerificationError(f"lock files[{index}] is missing required field: {key}")
    if set(entry) - {"path", "size", "role", "sha256", "git_blob_sha1"}:
        raise VerificationError(f"lock files[{index}] has an unexpected field")
    path = validate_relative_path(entry["path"], f"lock files[{index}].path")
    size = _require_int(entry["size"], f"lock files[{index}].size")
    role = _require_string(entry["role"], f"lock files[{index}].role")
    if role not in {"config", "generation_config", "index", "license", "tokenizer", "tokenizer_config", "weight"}:
        raise VerificationError(f"lock files[{index}].role is unsupported")
    has_sha256 = "sha256" in entry
    has_git_sha1 = "git_blob_sha1" in entry
    if has_sha256 == has_git_sha1:
        raise VerificationError(f"lock files[{index}] must contain exactly one file hash")
    if has_sha256:
        _require_string(entry["sha256"], f"lock files[{index}].sha256", _SHA256_RE)
    if has_git_sha1:
        _require_string(entry["git_blob_sha1"], f"lock files[{index}].git_blob_sha1", _SHA1_RE)
    result = {"path": path, "size": size, "role": role}
    if has_sha256:
        result["sha256"] = entry["sha256"]
    else:
        result["git_blob_sha1"] = entry["git_blob_sha1"]
    return result


def validate_lock(lock: Any, *, schema_path: Path | str | None = None) -> dict[str, Any]:
    """Validate and normalize the closed model-lock contract.

    This intentionally uses only the standard library so verification cannot
    become dependent on a package manager or an online schema resolver.
    """

    if schema_path is not None:
        _validate_schema_document(Path(schema_path))
    lock_obj = _require_mapping(lock, "model lock")
    required = (
        "schema_version",
        "model",
        "provenance",
        "license",
        "generation_config",
        "chat_template",
        "tokenizer",
        "weights",
        "install",
        "totals",
        "files",
    )
    _require_exact_keys(lock_obj, required, "model lock")
    if lock_obj["schema_version"] != 1:
        raise VerificationError("model lock schema_version must be 1")

    model = _require_mapping(lock_obj["model"], "lock model")
    _require_exact_keys(model, ("repo_id", "revision", "library_name", "model_type", "architecture", "config_path"), "lock model")
    repo_id = _require_string(model["repo_id"], "lock model.repo_id", re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$"))
    revision = _require_string(model["revision"], "lock model.revision", _REVISION_RE)
    library_name = _require_string(model["library_name"], "lock model.library_name")
    model_type = _require_string(model["model_type"], "lock model.model_type")
    architecture = _require_string(model["architecture"], "lock model.architecture")
    config_path = validate_relative_path(model["config_path"], "lock model.config_path")

    provenance = _require_mapping(lock_obj["provenance"], "lock provenance")
    _require_exact_keys(provenance, ("model_api", "tree_api", "metadata_observed_on"), "lock provenance")
    model_api = _require_string(provenance["model_api"], "lock provenance.model_api")
    tree_api = _require_string(provenance["tree_api"], "lock provenance.tree_api")
    observed_on = _require_string(provenance["metadata_observed_on"], "lock provenance.metadata_observed_on", re.compile(r"^\d{4}-\d{2}-\d{2}$"))
    if not model_api.startswith("https://huggingface.co/api/models/") or not tree_api.startswith("https://huggingface.co/api/models/"):
        raise VerificationError("lock provenance must cite public Hugging Face model APIs")
    if repo_id not in model_api or repo_id not in tree_api or revision not in model_api or revision not in tree_api:
        raise VerificationError("lock provenance does not identify the pinned model revision")

    license_obj = _require_mapping(lock_obj["license"], "lock license")
    _require_exact_keys(license_obj, ("spdx", "file_path"), "lock license")
    spdx = _require_string(license_obj["spdx"], "lock license.spdx")
    license_path = validate_relative_path(license_obj["file_path"], "lock license.file_path")
    if spdx.upper() != "MIT":
        raise VerificationError("lock license must be MIT")

    generation = _require_mapping(lock_obj["generation_config"], "lock generation_config")
    _require_exact_keys(generation, ("path", "do_sample", "temperature", "top_p"), "lock generation_config")
    generation_path = validate_relative_path(generation["path"], "lock generation_config.path")
    do_sample = _require_bool(generation["do_sample"], "lock generation_config.do_sample")
    temperature = _require_number(generation["temperature"], "lock generation_config.temperature", minimum=0)
    top_p = _require_number(generation["top_p"], "lock generation_config.top_p", minimum=0, maximum=1)

    chat_template = _require_mapping(lock_obj["chat_template"], "lock chat_template")
    _require_exact_keys(chat_template, ("policy", "jinja"), "lock chat_template")
    if chat_template["policy"] != "absent" or chat_template["jinja"] is not False:
        raise VerificationError("lock chat_template must require an absent Jinja template")

    tokenizer = _require_mapping(lock_obj["tokenizer"], "lock tokenizer")
    _require_exact_keys(tokenizer, ("tokenizer_path", "config_path", "tokenizer_class"), "lock tokenizer")
    tokenizer_path = validate_relative_path(tokenizer["tokenizer_path"], "lock tokenizer.tokenizer_path")
    tokenizer_config_path = validate_relative_path(tokenizer["config_path"], "lock tokenizer.config_path")
    tokenizer_class = _require_string(tokenizer["tokenizer_class"], "lock tokenizer.tokenizer_class")

    weights = _require_mapping(lock_obj["weights"], "lock weights")
    _require_exact_keys(weights, ("format", "index_path", "shard_count", "shards"), "lock weights")
    if weights["format"] != "safetensors":
        raise VerificationError("lock weights.format must be safetensors")
    index_path = validate_relative_path(weights["index_path"], "lock weights.index_path")
    shard_count = _require_int(weights["shard_count"], "lock weights.shard_count", minimum=1)
    shards_raw = weights["shards"]
    if not isinstance(shards_raw, list) or not shards_raw:
        raise VerificationError("lock weights.shards must be a non-empty array")
    shards = [validate_relative_path(item, f"lock weights.shards[{index}]") for index, item in enumerate(shards_raw)]
    if len(set(shards)) != len(shards) or len(shards) != shard_count:
        raise VerificationError("lock weights shard_count does not match its unique shard list")
    if any(not item.endswith(".safetensors") for item in shards):
        raise VerificationError("lock weights.shards must contain only safetensors files")

    install = _require_mapping(lock_obj["install"], "lock install")
    _require_exact_keys(install, ("manifest_marker", "unexpected_files"), "lock install")
    marker_name = validate_relative_path(install["manifest_marker"], "lock install.manifest_marker")
    if marker_name != DEFAULT_MARKER or install["unexpected_files"] != "reject":
        raise VerificationError("lock install must reject unexpected files with the standard marker")

    totals = _require_mapping(lock_obj["totals"], "lock totals")
    _require_exact_keys(totals, ("file_count", "selected_size_bytes", "weight_size_bytes"), "lock totals")
    file_count = _require_int(totals["file_count"], "lock totals.file_count", minimum=1)
    selected_size = _require_int(totals["selected_size_bytes"], "lock totals.selected_size_bytes", minimum=1)
    weight_size = _require_int(totals["weight_size_bytes"], "lock totals.weight_size_bytes", minimum=1)

    files_raw = lock_obj["files"]
    if not isinstance(files_raw, list) or not files_raw:
        raise VerificationError("lock files must be a non-empty array")
    files = [_validate_file_entry(item, index) for index, item in enumerate(files_raw)]
    paths = [item["path"] for item in files]
    if len(set(paths)) != len(paths):
        raise VerificationError("lock files contains duplicate paths")
    expected_paths = {config_path, license_path, generation_path, tokenizer_path, tokenizer_config_path, index_path, *shards}
    if set(paths) != expected_paths:
        missing = sorted(expected_paths - set(paths))
        extra = sorted(set(paths) - expected_paths)
        detail = []
        if missing:
            detail.append("missing " + ", ".join(missing))
        if extra:
            detail.append("unexpected " + ", ".join(extra))
        raise VerificationError("lock files does not match required model files (" + "; ".join(detail) + ")")
    if file_count != len(files) or selected_size != sum(item["size"] for item in files):
        raise VerificationError("lock totals do not match the file inventory")
    if weight_size != sum(item["size"] for item in files if item["role"] == "weight"):
        raise VerificationError("lock totals.weight_size_bytes does not match weight files")

    role_by_path = {item["path"]: item["role"] for item in files}
    expected_roles = {
        config_path: "config",
        license_path: "license",
        generation_path: "generation_config",
        tokenizer_path: "tokenizer",
        tokenizer_config_path: "tokenizer_config",
        index_path: "index",
        **{item: "weight" for item in shards},
    }
    if role_by_path != expected_roles:
        raise VerificationError("lock file roles do not match model metadata")

    normalized = dict(lock_obj)
    normalized["model"] = dict(model)
    normalized["provenance"] = dict(provenance)
    normalized["license"] = {"spdx": spdx, "file_path": license_path}
    normalized["generation_config"] = {
        "path": generation_path,
        "do_sample": do_sample,
        "temperature": temperature,
        "top_p": top_p,
    }
    normalized["tokenizer"] = {
        "tokenizer_path": tokenizer_path,
        "config_path": tokenizer_config_path,
        "tokenizer_class": tokenizer_class,
    }
    normalized["weights"] = {
        "format": "safetensors",
        "index_path": index_path,
        "shard_count": shard_count,
        "shards": shards,
    }
    normalized["install"] = {"manifest_marker": marker_name, "unexpected_files": "reject"}
    normalized["totals"] = {
        "file_count": file_count,
        "selected_size_bytes": selected_size,
        "weight_size_bytes": weight_size,
    }
    normalized["files"] = files
    return normalized


def load_lock(lock_path: Path | str = DEFAULT_LOCK, *, schema_path: Path | str | None = DEFAULT_SCHEMA) -> dict[str, Any]:
    """Read and validate a model lock without contacting any remote service."""

    path = Path(lock_path)
    lock = _read_json(path, "model lock")
    return validate_lock(lock, schema_path=schema_path)


def lock_sha256(lock_path: Path | str = DEFAULT_LOCK) -> str:
    """Hash the exact lock bytes used for an installation marker."""

    digest = hashlib.sha256()
    try:
        with Path(lock_path).open("rb") as stream:
            for block in iter(lambda: stream.read(_CHUNK_SIZE), b""):
                digest.update(block)
    except FileNotFoundError as exc:
        raise VerificationError(f"model lock is missing: {lock_path}") from exc
    except OSError as exc:
        raise VerificationError(f"cannot read model lock: {lock_path}") from exc
    return digest.hexdigest()


def _lstat(path: Path, label: str) -> os.stat_result:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise VerificationError(f"{label} is missing: {path}") from exc
    except OSError as exc:
        raise VerificationError(f"cannot inspect {label}: {path}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise VerificationError(f"{label} must not be a symlink: {path}")
    return info


def _safe_join(root: Path, relative_path: str, label: str) -> Path:
    validate_relative_path(relative_path, label)
    current = root
    for component in relative_path.split("/"):
        current = current / component
        _lstat(current, label)
    return current




def _file_digests(path: Path, expected_size: int) -> tuple[int, str, str]:
    sha256 = hashlib.sha256()
    git_sha1 = hashlib.sha1()
    git_sha1.update(f"blob {expected_size}\0".encode("ascii"))
    actual_size = 0
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(_CHUNK_SIZE), b""):
                actual_size += len(block)
                sha256.update(block)
                git_sha1.update(block)
    except OSError as exc:
        raise VerificationError(f"cannot read locked file: {path}") from exc
    return actual_size, sha256.hexdigest(), git_sha1.hexdigest()


def _verify_file(root: Path, entry: Mapping[str, Any]) -> None:
    relative_path = str(entry["path"])
    path = _safe_join(root, relative_path, f"locked file {relative_path}")
    info = _lstat(path, f"locked file {relative_path}")
    if not stat.S_ISREG(info.st_mode):
        raise VerificationError(f"locked file must be a regular file: {relative_path}")
    actual_size, sha256, git_sha1 = _file_digests(path, int(entry["size"]))
    if actual_size != entry["size"]:
        raise VerificationError(f"size mismatch for {relative_path}: expected {entry['size']}, got {actual_size}")
    if "sha256" in entry and sha256 != entry["sha256"]:
        raise VerificationError(f"SHA-256 mismatch for {relative_path}")
    if "git_blob_sha1" in entry and git_sha1 != entry["git_blob_sha1"]:
        raise VerificationError(f"Git blob SHA-1 mismatch for {relative_path}")


def _iter_install_files(root: Path) -> set[str]:
    files: set[str] = set()
    try:
        entries = list(os.scandir(root))
    except OSError as exc:
        raise VerificationError(f"cannot inspect installed model root: {root}") from exc
    for entry in entries:
        entry_path = Path(entry.path)
        try:
            entry_info = entry.stat(follow_symlinks=False)
        except OSError as exc:
            raise VerificationError(f"cannot inspect installed model entry: {entry_path}") from exc
        if stat.S_ISLNK(entry_info.st_mode):
            raise VerificationError(f"installed model must not contain symlinks: {entry_path}")
        relative = entry_path.relative_to(root).as_posix()
        if stat.S_ISDIR(entry_info.st_mode):
            nested_files = _iter_install_files(entry_path)
            if not nested_files:
                raise VerificationError(f"installed model contains an empty directory: {entry_path}")
            for nested in nested_files:
                files.add(relative + "/" + nested)
        elif stat.S_ISREG(entry_info.st_mode):
            files.add(relative)
        else:
            raise VerificationError(f"installed model contains a non-regular entry: {entry_path}")
    return files


def _verify_marker(root: Path, lock_path: Path, marker_name: str) -> None:
    marker_path = _safe_join(root, marker_name, "manifest marker")
    info = _lstat(marker_path, "manifest marker")
    if not stat.S_ISREG(info.st_mode):
        raise VerificationError("manifest marker must be a regular file")
    try:
        marker = marker_path.read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError) as exc:
        raise VerificationError("manifest marker is not readable ASCII") from exc
    if not re.fullmatch(r"[0-9a-f]{64}\n?", marker):
        raise VerificationError("manifest marker must contain one lowercase SHA-256 digest")
    if marker.rstrip("\n") != lock_sha256(lock_path):
        raise VerificationError("manifest marker does not match model.lock.json")


def _verify_cross_file_metadata(root: Path, lock: Mapping[str, Any]) -> None:
    config_path = _safe_join(root, lock["model"]["config_path"], "config file")
    config = _read_json(config_path, "config.json")
    config_obj = _require_mapping(config, "config.json")
    if config_obj.get("model_type") != lock["model"]["model_type"]:
        raise VerificationError("config.json model_type does not match model lock")
    architectures = config_obj.get("architectures")
    if not isinstance(architectures, list) or lock["model"]["architecture"] not in architectures:
        raise VerificationError("config.json architectures do not match model lock")

    generation_path = _safe_join(root, lock["generation_config"]["path"], "generation_config.json")
    generation = _require_mapping(_read_json(generation_path, "generation_config.json"), "generation_config.json")
    if generation.get("do_sample") is not lock["generation_config"]["do_sample"]:
        raise VerificationError("generation_config.json do_sample does not match model lock")
    if generation.get("temperature") != lock["generation_config"]["temperature"]:
        raise VerificationError("generation_config.json temperature does not match model lock")
    if generation.get("top_p") != lock["generation_config"]["top_p"]:
        raise VerificationError("generation_config.json top_p does not match model lock")

    tokenizer_config_path = _safe_join(root, lock["tokenizer"]["config_path"], "tokenizer_config.json")
    tokenizer_config = _require_mapping(_read_json(tokenizer_config_path, "tokenizer_config.json"), "tokenizer_config.json")
    if tokenizer_config.get("tokenizer_class") != lock["tokenizer"]["tokenizer_class"]:
        raise VerificationError("tokenizer_config.json tokenizer_class does not match model lock")
    if "chat_template" in tokenizer_config:
        raise VerificationError("tokenizer_config.json contains a Jinja chat_template")
    tokenizer_path = _safe_join(root, lock["tokenizer"]["tokenizer_path"], "tokenizer.json")
    _require_mapping(_read_json(tokenizer_path, "tokenizer.json"), "tokenizer.json")

    index_path = _safe_join(root, lock["weights"]["index_path"], "model.safetensors.index.json")
    index = _require_mapping(_read_json(index_path, "model.safetensors.index.json"), "model.safetensors.index.json")
    weight_map = index.get("weight_map")
    if not isinstance(weight_map, dict) or not weight_map:
        raise VerificationError("model.safetensors.index.json has no weight_map")
    locked_shards = set(lock["weights"]["shards"])
    referenced_shards = set()
    for parameter, shard in weight_map.items():
        if not isinstance(parameter, str) or not isinstance(shard, str):
            raise VerificationError("model.safetensors.index.json weight_map must map names to paths")
        validate_relative_path(shard, "model.safetensors.index.json shard path")
        referenced_shards.add(shard)
    if referenced_shards != locked_shards:
        missing = sorted(locked_shards - referenced_shards)
        extra = sorted(referenced_shards - locked_shards)
        detail = []
        if missing:
            detail.append("missing " + ", ".join(missing))
        if extra:
            detail.append("unexpected " + ", ".join(extra))
        raise VerificationError("index shard set does not match model lock (" + "; ".join(detail) + ")")


def verify_model_root(model_root: Path | str, lock_path: Path | str = DEFAULT_LOCK, *, schema_path: Path | str | None = DEFAULT_SCHEMA) -> dict[str, Any]:
    """Verify *model_root* and return a small non-sensitive success summary."""

    root = Path(model_root)
    root_info = _lstat(root, "installed model root")
    if not stat.S_ISDIR(root_info.st_mode):
        raise VerificationError(f"installed model root is not a directory: {root}")
    lock_file = Path(lock_path)
    lock = load_lock(lock_file, schema_path=schema_path)
    _verify_marker(root, lock_file, lock["install"]["manifest_marker"])
    expected_files = {item["path"] for item in lock["files"]} | {lock["install"]["manifest_marker"]}
    actual_files = _iter_install_files(root)
    if actual_files != expected_files:
        missing = sorted(expected_files - actual_files)
        extra = sorted(actual_files - expected_files)
        detail = []
        if missing:
            detail.append("missing " + ", ".join(missing))
        if extra:
            detail.append("unexpected " + ", ".join(extra))
        raise VerificationError("installed model file set does not match lock (" + "; ".join(detail) + ")")
    for entry in lock["files"]:
        _verify_file(root, entry)
    _verify_cross_file_metadata(root, lock)
    return {
        "repo_id": lock["model"]["repo_id"],
        "revision": lock["model"]["revision"],
        "file_count": len(lock["files"]),
        "shard_count": lock["weights"]["shard_count"],
    }


# Short aliases are useful to callers embedding the verifier in a deployment gate.
verify = verify_model_root


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model_root", nargs="?", type=Path, help="installed model directory to verify")
    parser.add_argument("--model-root", dest="model_root_option", type=Path, help="installed model directory to verify")
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK, help="model lock JSON (default: repository model.lock.json)")
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA, help="model lock schema JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    model_root = args.model_root_option or args.model_root
    if model_root is None:
        _parser().error("a model root is required (use MODEL_ROOT or --model-root)")
    if args.model_root_option is not None and args.model_root is not None and args.model_root_option != args.model_root:
        _parser().error("model root specified more than once")
    try:
        summary = verify_model_root(model_root, args.lock, schema_path=args.schema)
    except (VerificationError, OSError) as exc:
        print(f"model verification failed: {exc}", file=sys.stderr)
        return 1
    print(
        f"verified {summary['repo_id']} revision {summary['revision']} "
        f"({summary['file_count']} files, {summary['shard_count']} weight shards)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
