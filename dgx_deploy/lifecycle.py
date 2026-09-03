"""Locked, ordered lifecycle operations for the two-node deployment.

The engine is deliberately boring: render and plan never call this module, and
all mutating entry points require the CLI confirmation gate.  Every remote
operation is an argv vector sent through strict SSH; no operator environment is
sourced and no shell fragment is accepted from configuration.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from .config import ConfigError, canonical_json, config_sha256
from .fabric import (
    FabricError,
    apply_commands,
    discovery_commands,
    fabric_spec,
    gid_attribute_commands,
    gid_discovery_command,
    parse_address_output,
    parse_gid_attributes,
    parse_gid_discovery_output,
    parse_gid_output,
    parse_link_output,
    verify_rdma_output,
)
from .contract import contract_json
from .locks import REQUIRED_IMAGE_LABELS, load_deployment_lock
from .manifest import deployment_id
from .remote import CommandResult, RemoteError, SSHRunner, scp_argv, run_local
from .render import RenderError, render_contract, render_contract_json, render_container_argv, render_environment


class LifecycleError(ConfigError):
    """A lifecycle precondition, operation, or verification gate failed."""

_CANDIDATE_REF = re.compile(r"(?:^|[/:._-])candidate(?:[/:._-]|$)", re.IGNORECASE)


@dataclass(frozen=True)
class RoleState:
    """Minimal exact rollback state captured from Docker inspect."""

    name: str
    image: str
    command: tuple[str, ...]
    labels: dict[str, str]
    running: bool
    image_id: str | None = None


@dataclass(frozen=True)
class OperationRecord:
    role: str
    argv: tuple[str, ...]


def _fail(condition: bool, message: str) -> None:
    if condition:
        raise LifecycleError(message)
def _state_digest(state: Mapping[str, Any]) -> str:
    payload = dict(state)
    payload.pop("state_sha256", None)
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _as_result(value: CommandResult | str | Sequence[str] | None, argv: Sequence[str]) -> CommandResult:
    if isinstance(value, CommandResult):
        return value
    if value is None:
        return CommandResult(tuple(argv), 0, "", "")
    if isinstance(value, str):
        return CommandResult(tuple(argv), 0, value, "")
    return CommandResult(tuple(argv), 0, "\n".join(str(item) for item in value), "")


def _json_stdout(result: CommandResult, context: str) -> Any:
    try:
        return json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise LifecycleError(f"{context} returned non-JSON output") from exc


def _inspect_object(value: Any, context: str) -> Mapping[str, Any]:
    if isinstance(value, list):
        _fail(len(value) != 1, f"{context} must identify exactly one object")
        value = value[0]
    _fail(not isinstance(value, Mapping), f"{context} must be an object")
    return value


def _labels(inspect: Mapping[str, Any], context: str) -> dict[str, str]:
    config = inspect.get("Config")
    labels = config.get("Labels") if isinstance(config, Mapping) else inspect.get("Labels")
    _fail(not isinstance(labels, Mapping), f"{context} is missing Docker labels")
    return {str(key): str(value) for key, value in labels.items()}


def _command(inspect: Mapping[str, Any], context: str) -> tuple[str, ...]:
    config = inspect.get("Config")
    cmd = config.get("Cmd") if isinstance(config, Mapping) else inspect.get("Cmd")
    _fail(not isinstance(cmd, list) or any(not isinstance(item, str) or not item for item in cmd), f"{context} is missing an exact command")
    return tuple(cmd)


def verify_image_inspect(inspect: Mapping[str, Any], expected: Mapping[str, Any], role: str) -> None:
    """Verify an image inspect object against the immutable role lock."""

    image_id = inspect.get("Id") or inspect.get("ID") or inspect.get("Image")
    _fail(image_id != expected.get("image_id"), f"{role} image ID does not match deployment lock")
    architecture = inspect.get("Architecture") or inspect.get("Os")
    if architecture == "arm64":
        pass
    elif architecture is not None:
        _fail(architecture not in {"linux/arm64", "arm64"}, f"{role} image architecture is not linux/arm64")
    actual = _labels(inspect, f"{role} image")
    expected_labels = expected.get("labels")
    _fail(not isinstance(expected_labels, Mapping), f"{role} image lock labels are missing")
    for key in REQUIRED_IMAGE_LABELS:
        _fail(actual.get(key) != expected_labels.get(key), f"{role} image label {key} does not match deployment lock")


def verify_container_inspect(inspect: Mapping[str, Any], contract: Mapping[str, Any]) -> None:
    """Verify name, image, command, environment, labels, mounts, and running state."""

    role = str(contract["role"])
    name = str(contract["container"])
    actual_name = str(inspect.get("Name", "")).lstrip("/")
    _fail(actual_name != name, f"{role} container name is not owned by this contract")
    image = contract["image"]
    _fail(not isinstance(image, Mapping), f"{role} contract image is malformed")
    actual_image = inspect.get("Image")
    _fail(actual_image not in {image.get("image_id"), image.get("reference")}, f"{role} container image does not match deployment lock")
    actual_labels = _labels(inspect, f"{role} container")
    expected_labels = image.get("labels")
    _fail(not isinstance(expected_labels, Mapping), f"{role} contract labels are malformed")
    for key in REQUIRED_IMAGE_LABELS:
        _fail(actual_labels.get(key) != expected_labels.get(key), f"{role} container label {key} does not match lock")
    _fail(actual_labels.get("com.dgx-spark.deployment_id") != contract.get("deployment_id"), f"{role} container deployment ownership label is wrong")
    _fail(actual_labels.get("com.dgx-spark.role") != role, f"{role} container role ownership label is wrong")
    actual_command = _command(inspect, f"{role} container")
    expected_command = tuple(contract["service_argv"])
    _fail(actual_command != expected_command, f"{role} container command differs from rendered command lock")
    config = inspect.get("Config")
    actual_env = config.get("Env") if isinstance(config, Mapping) else inspect.get("Env")
    _fail(not isinstance(actual_env, list), f"{role} container environment is unavailable")
    actual_env_map = {str(item).split("=", 1)[0]: str(item).split("=", 1)[1] for item in actual_env if isinstance(item, str) and "=" in item}
    expected_env = contract.get("environment")
    _fail(not isinstance(expected_env, Mapping), f"{role} contract environment is malformed")
    for key, value in expected_env.items():
        _fail(actual_env_map.get(key) != str(value), f"{role} environment {key} differs from contract")
    state = inspect.get("State")
    _fail(not isinstance(state, Mapping) or not bool(state.get("Running")), f"{role} container is not running")
    mounts = inspect.get("Mounts")
    _fail(not isinstance(mounts, list), f"{role} container mounts are unavailable")
    mount_strings = {
        f"{item.get('Source')}:{item.get('Destination')}"
        for item in mounts
        if isinstance(item, Mapping)
    }
    model_root = str(contract.get("model_root", ""))
    model_path = str(contract.get("model_path", ""))
    if model_root and model_path:
        model_destination = model_path if Path(model_root).name == Path(model_path).name else "/models"
        _fail(f"{model_root}:{model_destination}" not in mount_strings, f"{role} model mount is missing or points at the wrong root")


def _role_order() -> tuple[str, str]:
    return ("worker", "head")


class DeploymentEngine:
    """Execute a validated lock on two explicit SSH targets."""

    def __init__(
        self,
        config: Mapping[str, Any],
        lock: Mapping[str, Any] | None = None,
        *,
        runner: Callable[[str, Sequence[str]], CommandResult | str | Sequence[str] | None] | None = None,
        scp_runner: Callable[[Sequence[str]], CommandResult | str | Sequence[str] | None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.deployment = config.get("deployment")
        if not isinstance(self.deployment, Mapping):
            raise LifecycleError("deployment section is missing")
        self.mode = str(self.deployment.get("mode", "generic"))
        _fail(self.mode not in {"production", "candidate"}, "lifecycle requires production or candidate mode")
        self.lock = dict(lock) if lock is not None else load_deployment_lock(Path(str(self.deployment["image_lock_file"])), config)
        self.runner = runner
        self.scp_runner = scp_runner or run_local
        self.sleep = sleep
        self.records: list[OperationRecord] = []
        self.contracts = {role: render_contract(config, role, self.lock) for role in ("worker", "head")}
        self._validate_contracts()

    def _validate_contracts(self) -> None:
        expected = (29619, 8101) if self.mode == "production" else (29621, 18101)
        _fail(int(self.deployment["master_port"]) != expected[0], "master port does not match mode isolation")
        _fail(int(self.deployment["api_port"]) != expected[1], "API port does not match mode isolation")
        names = [str(self.contracts[role]["container"]) for role in ("worker", "head")]
        _fail(len(set(names)) != 2, "head and worker container names collide")
        _fail(self.contracts["head"]["api_port"] != int(self.deployment["api_port"]), "head contract API port drifted")
        _fail(self.contracts["worker"]["api_port"] is not None, "worker contract must not expose an API port")
        for role in ("worker", "head"):
            image = self.contracts[role]["image"]
            _fail(not isinstance(image, Mapping), f"{role} image lock is malformed")
            reference = str(image.get("reference", ""))
            image_id = str(image.get("image_id", ""))
            is_candidate = _CANDIDATE_REF.search(reference) is not None
            if self.mode == "candidate" and not is_candidate:
                _fail(
                    not bool(self.deployment.get("allow_production_images_in_candidate")),
                    "candidate production image reuse requires explicit authorization",
                )
                _fail(reference != image_id, f"candidate {role} production image reference must equal its locked image ID")
            if self.mode == "production":
                _fail(is_candidate, f"production {role} image reference must not be candidate-namespaced")
            labels = image.get("labels")
            _fail(not isinstance(labels, Mapping), f"{role} image labels are missing")
            for key in REQUIRED_IMAGE_LABELS:
                _fail(not labels.get(key), f"{role} image label {key} is empty")

    def _fabric_preflight(self, role: str, *, require_address: bool = True) -> None:
        if str(self.deployment.get("fabric_profile", "auto")) == "f0":
            return
        try:
            spec = fabric_spec(self.config, role)
            image_ref = str(self.contracts[role]["image"]["reference"])
            commands = discovery_commands(self.config, role, image_ref)
            if not require_address and spec["profile"] == "f1":
                commands = []
            elif not require_address:
                commands = [command for command in commands if command[0] != "ping" and not (command[0] == "docker" and "/bin/" in command)]
            discovered_gid: int | None = None
            for command in commands:
                result = self._remote(role, command)
                if spec["profile"] == "auto":
                    continue
                if command[:4] == ["ip", "-json", "address", "show"]:
                    if require_address:
                        parse_address_output(result.stdout, spec)
                elif command[:4] == ["ip", "-json", "link", "show"]:
                    if require_address:
                        parse_link_output(result.stdout, spec)
                elif command[:3] == ["rdma", "-j", "link"]:
                    if require_address:
                        verify_rdma_output(result.stdout, spec)
                elif command[0] == "docker" and "/bin/cat" in command:
                    if require_address:
                        parse_gid_output(result.stdout, spec)
                elif command[0] == "docker" and "/bin/sh" in command:
                    if require_address:
                        discovered_gid, _ = parse_gid_discovery_output(result.stdout)
                        key = f"{role}_roce_gid_index"
                        configured = self.deployment.get(key)
                        if configured is not None:
                            _fail(int(configured) != discovered_gid, f"{role} discovered GID index differs from lock")
                        self.deployment[key] = discovered_gid
            if require_address and spec["profile"] == "f1":
                gid_index = discovered_gid if discovered_gid is not None else self.deployment.get(f"{role}_roce_gid_index")
                _fail(gid_index is None, f"{role} has no discovered GID index")
                attrs = gid_attribute_commands(self.config, role, image_ref, int(gid_index))
                attr_results = [self._remote(role, command) for command in attrs]
                parse_gid_attributes(attr_results[0].stdout, attr_results[1].stdout, spec)
        except FabricError as exc:
            raise LifecycleError(str(exc)) from exc

    def _apply_fabric(self) -> None:
        if str(self.deployment.get("fabric_profile", "auto")) != "f1":
            return
        for role in _role_order():
            try:
                image_ref = str(self.contracts[role]["image"]["reference"])
                for command in apply_commands(self.config, role, image_ref):
                    self._remote(role, command)
            except FabricError as exc:
                raise LifecycleError(str(exc)) from exc
        for role in _role_order():
            self._fabric_preflight(role, require_address=True)
        self._refresh_contracts()

    def _refresh_contracts(self) -> None:
        self.contracts = {role: render_contract(self.config, role, self.lock) for role in ("worker", "head")}
        self._validate_contracts()

    def _remote(self, role: str, argv: Sequence[str]) -> CommandResult:
        command = tuple(str(item) for item in argv)
        self.records.append(OperationRecord(role, command))
        if self.runner is not None:
            result = _as_result(self.runner(role, command), command)
            if result.returncode:
                detail = result.stderr.strip()
                suffix = f": {detail}" if detail else ""
                raise LifecycleError(f"{role} command failed: {' '.join(command)}{suffix}")
            return result
        try:
            return SSHRunner(dict(self.deployment))(role, command)
        except RemoteError as exc:
            raise LifecycleError(str(exc)) from exc

    def _scp(self, argv: Sequence[str]) -> CommandResult:
        result = _as_result(self.scp_runner(argv), argv)
        if result.returncode:
            raise LifecycleError(f"staging command failed: {' '.join(argv)}")
        return result

    def _inspect(self, role: str, *, allow_missing: bool = False) -> Mapping[str, Any] | None:
        name = str(self.contracts[role]["container"])
        try:
            result = self._remote(role, ["docker", "container", "inspect", "--format", "{{json .}}", name])
        except LifecycleError as exc:
            if allow_missing and ("No such object" in str(exc) or "no such container" in str(exc).lower() or "not found" in str(exc).lower()):
                return None
            raise
        return _inspect_object(_json_stdout(result, f"{role} container inspect"), f"{role} container inspect")
    def _assert_owned(self, role: str, inspect: Mapping[str, Any]) -> None:
        contract = self.contracts[role]
        labels = _labels(inspect, f"{role} container")
        actual_name = str(inspect.get("Name", "")).lstrip("/")
        _fail(actual_name != contract["container"], f"refusing to mutate unexpected {role} container")
        current_owner = labels.get("com.dgx-spark.deployment_id") == contract["deployment_id"] and labels.get("com.dgx-spark.role") == role
        legacy_owner = (
            self.mode == "production"
            and labels.get("com.dgx-spark.owner") == "deepseek-v4-flash-dgx-spark"
            and labels.get("com.dgx-spark.role") == role
            and str(inspect.get("Image", "")) == {
                "head": "sha256:53c78475720e2561cf3245a9244f574e9db0fb0637c57e84a390a2c096122aa3",
                "worker": "sha256:828171dd2c4970777993837b7d5234adf905a9ddc20503cd392a95c74b42ada8",
            }[role]
        )
        _fail(not (current_owner or legacy_owner), f"refusing to mutate unowned {role} container")

    def preflight(self, *, require_model_marker: bool = True, require_fabric: bool = True) -> None:
        """Check Docker/image/model/GPU/RDMA/network prerequisites on both nodes."""

        for role in _role_order():
            contract = self.contracts[role]
            self._remote(role, ["docker", "version", "--format", "{{.Server.Arch}}"])
            self._remote(role, ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"])
            self._remote(role, ["rdma", "link"])
            iface = str(self.deployment[f"{role}_net_iface"]).split(",", 1)[0]
            self._remote(role, ["ip", "link", "show", iface])
            hca = str(self.deployment[f"{role}_hca"]).split(",", 1)[0]
            self._remote(role, ["ibdev2netdev", "-v"])
            self._fabric_preflight(role, require_address=require_fabric)
            image_result = self._remote(role, ["docker", "image", "inspect", "--format", "{{json .}}", str(contract["image"]["reference"])])
            _fail(not image_result.stdout.strip(), f"{role} image inspect returned no data")
            image_object = _inspect_object(_json_stdout(image_result, f"{role} image inspect"), f"{role} image inspect")
            verify_image_inspect(image_object, contract["image"], role)
            model_root = Path(str(self.deployment["model_root"]))
            model_path = Path(str(contract["model_path"]))
            if model_root.name != model_path.name:
                model_root = model_root / model_path.name
            marker = str(model_root / ".model-lock.sha256")
            marker_result: CommandResult | None = None
            try:
                marker_result = self._remote(role, ["cat", marker])
            except LifecycleError:
                if require_model_marker:
                    raise
            if marker_result is not None:
                _fail(marker_result.stdout.strip() != str(self.deployment["model_manifest_sha256"]), f"{role} model manifest marker does not match model lock")
            for model_file in ("config.json", "model.safetensors.index.json", "tokenizer.json", "tokenizer_config.json"):
                self._remote(role, ["test", "-r", str(model_root / model_file)])
            self._remote(role, ["test", "-r", str(model_root)])
            self._remote(role, ["test", "-n", hca])

    def capture_rollback(self, state_file: Path) -> dict[str, Any]:
        """Capture exact owned image/command/labels before any mutation."""

        roles: dict[str, Any] = {}
        for role in _role_order():
            inspect = self._inspect(role, allow_missing=True)
            if inspect is None:
                roles[role] = None
                continue
            self._assert_owned(role, inspect)
            state = inspect.get("State")
            docker_config = inspect.get("Config")
            host_config = inspect.get("HostConfig")
            host_state = host_config if isinstance(host_config, Mapping) else {}
            roles[role] = {
                "name": str(inspect.get("Name", "")).lstrip("/"),
                "image": str(inspect.get("Image", "")),
                "image_id": str(inspect.get("Image", "")),
                "command": list(_command(inspect, f"{role} container")),
                "labels": _labels(inspect, f"{role} container"),
                "environment": list(docker_config.get("Env", [])) if isinstance(docker_config, Mapping) and isinstance(docker_config.get("Env"), list) else [],
                "mounts": list(inspect.get("Mounts", [])) if isinstance(inspect.get("Mounts"), list) else [],
                "working_dir": str(docker_config.get("WorkingDir", "")) if isinstance(docker_config, Mapping) else "",
                "host_config": {
                    "network_mode": str(host_state.get("NetworkMode", "")),
                    "ipc_mode": str(host_state.get("IpcMode", "")),
                    "privileged": bool(host_state.get("Privileged", False)),
                    "shm_size": int(host_state.get("ShmSize", 0) or 0),
                    "security_opt": list(host_state.get("SecurityOpt", [])) if isinstance(host_state.get("SecurityOpt"), list) else [],
                    "device_requests": list(host_state.get("DeviceRequests", [])) if isinstance(host_state.get("DeviceRequests"), list) else [],
                    "ulimits": list(host_state.get("Ulimits", [])) if isinstance(host_state.get("Ulimits"), list) else [],
                    "readonly_rootfs": bool(host_state.get("ReadonlyRootfs", False)),
                },
                "running": bool(state.get("Running")) if isinstance(state, Mapping) else False,
            }
        fabric_state: dict[str, Any] = {}
        if str(self.deployment.get("fabric_profile", "auto")) == "f1":
            for role in _role_order():
                fabric_state[role] = fabric_spec(self.config, role)
        state = {
            "schema_version": 1,
            "deployment_id": deployment_id(self.config),
            "mode": self.mode,
            "config_sha256": config_sha256(self.config),
            "fabric": fabric_state,
            "roles": roles,
        }
        state["state_sha256"] = _state_digest(state)
        state_file = state_file.expanduser()
        state_file.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".rollback.", dir=str(state_file.parent), text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(state, stream, sort_keys=True, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, state_file)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return state

    def _stage(self) -> None:
        remote_root = str(self.deployment["remote_root"])
        model_root = Path(str(self.deployment["model_root"]))
        model_path = Path(str(self.contracts["head"]["model_path"]))
        if model_root.name != model_path.name:
            model_root = model_root / model_path.name
        remote_model_marker = str(model_root / ".model-lock.sha256")
        for role in _role_order():
            contract = self.contracts[role]
            cache_root = Path(str(self.deployment["cache_root"])) / ("dgx0" if role == "head" else "dgx1")
            remote_contract = str(contract["contract_path"])
            remote_env = str(Path(remote_root) / "contracts" / f"{self.mode}-{role}.env")
            self._remote(role, ["mkdir", "-p", str(Path(remote_contract).parent), *(str(cache_root / name) for name in ("vllm", "b12x", "flashinfer", "triton", "tilelang"))])
            with tempfile.TemporaryDirectory(prefix="dgx-deploy-") as directory:
                contract_path = Path(directory) / f"{role}.json"
                env_path = Path(directory) / f"{role}.env"
                marker_path = Path(directory) / "model.lock.sha256"
                marker_path.write_text(str(self.deployment["model_manifest_sha256"]) + "\n", encoding="ascii")
                contract_path.write_text(contract_json(self.config, role, self.lock), encoding="utf-8")
                env_path.write_text("".join(f"{key}={value}\n" for key, value in render_environment(self.config, role).items()), encoding="utf-8")
                ssh = dict(self.deployment)
                for source, destination in (
                    (contract_path, remote_contract),
                    (env_path, remote_env),
                    (marker_path, remote_model_marker),
                ):
                    self._scp(
                        scp_argv(
                            str(source), str(ssh.get(f"{role}_ssh_host") or ssh[f"{role}_host"]), str(ssh[f"{role}_ssh_user"]),
                            int(ssh["ssh_port"]), str(ssh["ssh_known_hosts_file"]), destination,
                            identity_file=ssh.get("ssh_identity_file") or None,
                        )
                    )
    def _stop_owned(self) -> None:
        for role in _role_order():
            inspect = self._inspect(role, allow_missing=True)
            if inspect is not None:
                self._assert_owned(role, inspect)
                state = inspect.get("State")
                if isinstance(state, Mapping) and bool(state.get("Running")):
                    self._remote(role, ["docker", "container", "stop", "--time", "30", str(self.contracts[role]["container"])])

    def _remove_owned(self) -> None:
        for role in _role_order():
            inspect = self._inspect(role, allow_missing=True)
            if inspect is not None:
                self._assert_owned(role, inspect)
                self._remote(role, ["docker", "container", "rm", str(self.contracts[role]["container"])])

    def _create(self) -> None:
        for role in _role_order():
            self._remote(role, render_container_argv(self.config, role, self.lock))

    def _start(self) -> None:
        for role in _role_order():
            inspect = self._inspect(role, allow_missing=True)
            if inspect is not None:
                self._assert_owned(role, inspect)
                state = inspect.get("State")
                if isinstance(state, Mapping) and bool(state.get("Running")):
                    continue
            self._remote(role, ["docker", "container", "start", str(self.contracts[role]["container"])])

    def verify(self, *, readiness_attempts: int = 36, readiness_interval: float = 10.0, preflight: bool = True) -> None:
        """Verify exact image/labels/command and head API readiness."""
        if preflight:
            self.preflight()
        for role in _role_order():
            inspect = self._inspect(role)
            assert inspect is not None
            verify_container_inspect(inspect, self.contracts[role])
        head = self.contracts["head"]
        api_port = int(head["api_port"])
        last_error: Exception | None = None
        for attempt in range(readiness_attempts):
            try:
                self._remote("head", ["curl", "--fail", "--silent", "--show-error", "--max-time", "10", f"http://127.0.0.1:{api_port}/health"])
                self._remote("head", ["curl", "--fail", "--silent", "--show-error", "--max-time", "10", f"http://127.0.0.1:{api_port}/v1/models"])
                return
            except LifecycleError as exc:
                last_error = exc
                if attempt + 1 < readiness_attempts:
                    self.sleep(readiness_interval)
        raise LifecycleError(f"service readiness failed after {readiness_attempts} attempts") from last_error

    def _cleanup_partial(self) -> None:
        """Remove only current-contract containers after an empty-target failure."""
        for role in _role_order():
            inspect = self._inspect(role, allow_missing=True)
            if inspect is None:
                continue
            self._assert_owned(role, inspect)
            state = inspect.get("State")
            if isinstance(state, Mapping) and bool(state.get("Running")):
                self._remote(role, ["docker", "container", "stop", "--time", "30", str(self.contracts[role]["container"])])
            self._remote(role, ["docker", "container", "rm", str(self.contracts[role]["container"])])

    def apply(self, state_file: Path, *, update: bool = False) -> None:
        """Apply a locked contract; worker is always started before head."""

        self.preflight(require_model_marker=False, require_fabric=False)
        captured = self.capture_rollback(state_file)
        try:
            self._apply_fabric()
            self.preflight(require_model_marker=False, require_fabric=True)
            if update:
                self._stop_owned()
                self._remove_owned()
            self._stage()
            self.preflight(require_model_marker=True, require_fabric=True)
            self._create()
            self._start()
            self.verify(preflight=False)
        except LifecycleError:
            previous_roles = captured.get("roles", {})
            if update and isinstance(previous_roles, Mapping) and all(isinstance(previous_roles.get(role), Mapping) for role in _role_order()):
                self.rollback(state_file)
            else:
                self._cleanup_partial()
            raise
    def start(self) -> None:
        self.preflight()
        self._start()
        self.verify(preflight=False)


    def stop(self) -> None:
        self._stop_owned()

    def _verify_rollback(self, roles: Mapping[str, Any]) -> None:
        for role in _role_order():
            inspect = self._inspect(role)
            assert inspect is not None
            old = roles[role]
            actual_name = str(inspect.get("Name", "")).lstrip("/")
            _fail(actual_name != str(old["name"]), f"rollback {role} name differs from captured state")
            _fail(str(inspect.get("Image", "")) != str(old["image"]), f"rollback {role} image differs from captured state")
            _fail(_command(inspect, f"rollback {role}") != tuple(str(item) for item in old["command"]), f"rollback {role} command differs from captured state")
            _fail(_labels(inspect, f"rollback {role}") != {str(k): str(v) for k, v in old["labels"].items()}, f"rollback {role} labels differ from captured state")
            docker_config = inspect.get("Config")
            _fail(not isinstance(docker_config, Mapping), f"rollback {role} Docker config is missing")
            _fail(list(docker_config.get("Env", [])) != list(old["environment"]), f"rollback {role} environment differs from captured state")
            _fail(str(docker_config.get("WorkingDir", "")) != str(old["working_dir"]), f"rollback {role} working directory differs from captured state")
            actual_mounts = inspect.get("Mounts")
            _fail(not isinstance(actual_mounts, list), f"rollback {role} mounts are missing")
            mount_key = lambda mount: (str(mount.get("Type", "")), str(mount.get("Source", "")), str(mount.get("Destination", "")), bool(mount.get("RW", False)))
            _fail(sorted(mount_key(mount) for mount in actual_mounts if isinstance(mount, Mapping)) != sorted(mount_key(mount) for mount in old["mounts"] if isinstance(mount, Mapping)), f"rollback {role} mounts differ from captured state")
            actual_host = inspect.get("HostConfig")
            expected_host = old["host_config"]
            _fail(not isinstance(actual_host, Mapping) or not isinstance(expected_host, Mapping), f"rollback {role} host settings are missing")
            for actual_key, expected_key in (
                ("NetworkMode", "network_mode"),
                ("IpcMode", "ipc_mode"),
                ("Privileged", "privileged"),
                ("ShmSize", "shm_size"),
                ("ReadonlyRootfs", "readonly_rootfs"),
            ):
                _fail(actual_host.get(actual_key) != expected_host.get(expected_key), f"rollback {role} host setting {actual_key} differs from captured state")
            _fail(list(actual_host.get("SecurityOpt") or []) != list(expected_host.get("security_opt") or []), f"rollback {role} security settings differ from captured state")
            _fail(list(actual_host.get("Ulimits") or []) != list(expected_host.get("ulimits") or []), f"rollback {role} ulimits differ from captured state")
            _fail(list(actual_host.get("DeviceRequests") or []) != list(expected_host.get("device_requests") or []), f"rollback {role} GPU requests differ from captured state")
            state = inspect.get("State")
            running = bool(state.get("Running")) if isinstance(state, Mapping) else False
            _fail(running != bool(old["running"]), f"rollback {role} running state differs from captured state")

    def update(self, state_file: Path) -> None:
        self.apply(state_file, update=True)
    def _remove_rollback_targets(self, roles: Mapping[str, Any]) -> None:
        """Remove only an exact current/old pair while recovering a partial update."""
        for role in _role_order():
            inspect = self._inspect(role, allow_missing=True)
            if inspect is None:
                continue
            old = roles[role]
            actual_name = str(inspect.get("Name", "")).lstrip("/")
            actual_image = str(inspect.get("Image", ""))
            current_image = str(self.contracts[role]["image"]["image_id"])
            _fail(actual_name != str(old["name"]), f"rollback found an unexpected {role} name")
            _fail(actual_image not in {str(old["image"]), current_image}, f"rollback found an unexpected {role} image")
            actual_labels = _labels(inspect, f"rollback {role}")
            old_labels = old["labels"]
            _fail(not isinstance(old_labels, Mapping), f"rollback {role} captured labels are malformed")
            current_owned = actual_labels.get("com.dgx-spark.deployment_id") == self.contracts[role]["deployment_id"] and actual_labels.get("com.dgx-spark.role") == role
            captured_owned = actual_labels == {str(key): str(value) for key, value in old_labels.items()}
            _fail(not (current_owned or captured_owned), f"rollback refusing to remove unowned {role} target")
            state = inspect.get("State")
            if isinstance(state, Mapping) and bool(state.get("Running")):
                self._remote(role, ["docker", "container", "stop", "--time", "30", str(old["name"])])
            self._remote(role, ["docker", "container", "rm", str(old["name"])])

    def _remove_empty_rollback_targets(self) -> None:
        """Remove an exact candidate pair when no previous pair was captured."""
        inspected: dict[str, Mapping[str, Any] | None] = {}
        # Validate both targets before mutating either one.  An empty rollback
        # state has no prior names to authorize; ownership must come entirely
        # from the current candidate contracts.
        for role in _role_order():
            inspect = self._inspect(role, allow_missing=True)
            if inspect is not None:
                self._assert_owned(role, inspect)
            inspected[role] = inspect
        for role in _role_order():
            inspect = inspected[role]
            if inspect is None:
                continue
            state = inspect.get("State")
            name = str(self.contracts[role]["container"])
            if isinstance(state, Mapping) and bool(state.get("Running")):
                self._remote(role, ["docker", "container", "stop", "--time", "30", name])
            self._remote(role, ["docker", "container", "rm", name])
        for role in _role_order():
            remaining = self._inspect(role, allow_missing=True)
            _fail(remaining is not None, f"rollback empty {role} container remains")


    def rollback(self, state_file: Path) -> None:
        """Restore exact captured commands/images, refusing incomplete state."""

        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise LifecycleError(f"cannot read rollback state: {state_file}") from exc
        _fail(not isinstance(state.get("state_sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", str(state.get("state_sha256"))), "rollback state hash is missing or malformed")
        _fail(str(state["state_sha256"]) != _state_digest(state), "rollback state integrity hash does not match")
        _fail(state.get("schema_version") != 1, "rollback state schema_version is unsupported")
        _fail(state.get("deployment_id") != deployment_id(self.config), "rollback state deployment ID does not match")
        roles = state.get("roles")
        _fail(not isinstance(roles, Mapping), "rollback state roles are missing")
        _fail(any(role not in roles for role in _role_order()), "rollback state roles are missing a deployment role")
        role_states = {role: roles[role] for role in _role_order()}
        if all(role_states[role] is None for role in _role_order()):
            _fail(self.mode != "candidate", "empty rollback state is allowed only in candidate mode")
            self._remove_empty_rollback_targets()
            return
        _fail(any(role_states[role] is None for role in _role_order()), "rollback state must contain both previous roles or neither")
        for role in _role_order():
            old = role_states[role]
            _fail(not isinstance(old, Mapping), f"rollback state has no previous {role} contract")
            for key in ("name", "image", "command", "labels", "environment", "mounts", "working_dir", "host_config", "running"):
                _fail(key not in old, f"rollback state {role} is missing {key}")
        # Check that the exact previous image is available before touching the
        # currently-owned pair.  The old command vector and labels remain
        # entirely state-file controlled.
        for role in _role_order():
            old = role_states[role]
            self._remote(role, ["docker", "image", "inspect", str(old["image"])])
        self._remove_rollback_targets(role_states)
        for role in _role_order():
            old = role_states[role]
            create = ["docker", "container", "create", "--name", str(old["name"])]
            host_config = old["host_config"]
            _fail(not isinstance(host_config, Mapping), f"rollback {role} host configuration is malformed")
            network_mode = str(host_config.get("network_mode", ""))
            if network_mode:
                create.extend(["--network", network_mode])
            ipc_mode = str(host_config.get("ipc_mode", ""))
            if ipc_mode:
                create.extend(["--ipc", ipc_mode])
            if bool(host_config.get("privileged")):
                create.append("--privileged")
            shm_size = int(host_config.get("shm_size", 0) or 0)
            if shm_size:
                create.extend(["--shm-size", str(shm_size)])
            for option in host_config.get("security_opt", []):
                _fail(not isinstance(option, str) or not option, f"rollback {role} security option is malformed")
                create.extend(["--security-opt", option])
            for request in host_config.get("device_requests", []):
                if isinstance(request, Mapping) and int(request.get("Count", 0) or 0) == -1:
                    create.extend(["--gpus", "all"])
                    break
            for limit in host_config.get("ulimits", []):
                _fail(not isinstance(limit, Mapping), f"rollback {role} ulimit is malformed")
                lname = limit.get("Name")
                hard = limit.get("Hard")
                soft = limit.get("Soft", hard)
                _fail(not isinstance(lname, str) or not lname or hard is None or soft is None, f"rollback {role} ulimit is incomplete")
                create.extend(["--ulimit", f"{lname}={soft}:{hard}"])
            if bool(host_config.get("readonly_rootfs")):
                create.append("--read-only")
            labels = old["labels"]
            _fail(not isinstance(labels, Mapping), f"rollback {role} labels are malformed")
            for key, value in sorted(labels.items()):
                _fail(not isinstance(key, str) or not key or not isinstance(value, str) or not value, f"rollback {role} label is malformed")
                create.extend(["--label", f"{key}={value}"])
            environment = old["environment"]
            _fail(not isinstance(environment, list), f"rollback {role} environment is malformed")
            for value in environment:
                _fail(not isinstance(value, str) or not value or "\x00" in value, f"rollback {role} environment contains an invalid value")
                create.extend(["--env", value])
            mounts = old["mounts"]
            _fail(not isinstance(mounts, list), f"rollback {role} mounts are malformed")
            for mount in mounts:
                _fail(not isinstance(mount, Mapping), f"rollback {role} mount is malformed")
                source = mount.get("Source")
                destination = mount.get("Destination")
                _fail(not isinstance(source, str) or not isinstance(destination, str) or not source or not destination, f"rollback {role} mount is incomplete")
                option = f"type={mount.get('Type', 'bind')},src={source},dst={destination}"
                if mount.get("RW") is False:
                    option += ",readonly"
                create.extend(["--mount", option])
            working_dir = str(old["working_dir"])
            if working_dir:
                create.extend(["--workdir", working_dir])
            create.extend([str(old["image"]), *[str(token) for token in old["command"]]])
            self._remote(role, create)
            if bool(old["running"]):
                self._remote(role, ["docker", "container", "start", str(old["name"])])
        self._verify_rollback(role_states)



def load_engine(config: Mapping[str, Any]) -> DeploymentEngine:
    """Build the locked engine for CLI commands."""

    return DeploymentEngine(config)
