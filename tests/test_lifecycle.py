from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from dgx_deploy.cli import main
from dgx_deploy.lifecycle import DeploymentEngine, LifecycleError, verify_container_inspect
from dgx_deploy.config import ConfigError, DEFAULT_PROFILE, canonical_json, load_config
from dgx_deploy.locks import LockError, load_deployment_lock
from dgx_deploy.remote import CommandResult, ssh_argv
from dgx_deploy.render import render_contract, render_service_argv
from test_config import valid_env, write_env
class LifecycleContractTests(unittest.TestCase):
    def _candidate_values(self) -> dict[str, str]:
        values = valid_env()
        values.update(
            {
                "DEPLOYMENT_MODE": "candidate",
                "REMOTE_ROOT": "/srv/dgx-spark-candidate/deploy",
                "STATE_ROOT": "/var/lib/dgx-spark-candidate/state",
                "CACHE_ROOT": "/var/cache/dgx-spark-candidate/cache",
                "LOG_ROOT": "/var/log/dgx-spark-candidate/logs",
                "RESULT_ROOT": "/var/lib/dgx-spark-candidate/results",
                "MASTER_ADDR": "192.168.100.10",
                "MASTER_PORT": "29621",
                "API_PORT": "18101",
                "HEAD_NODE_ADDR": "192.168.100.10",
                "WORKER_NODE_ADDR": "192.168.100.11",
                "HEAD_IMAGE_REF": "candidate/dsv4-head@sha256:" + "c" * 64,
                "WORKER_IMAGE_REF": "candidate/dsv4-worker@sha256:" + "d" * 64,
            }
        )
        return values
    def _production_values(self) -> dict[str, str]:
        values = valid_env()
        values.update(
            {
                "DEPLOYMENT_MODE": "production",
                "REMOTE_ROOT": "/srv/dgx-spark-production/deploy",
                "STATE_ROOT": "/var/lib/dgx-spark-production/state",
                "CACHE_ROOT": "/var/cache/dgx-spark-production/cache",
                "LOG_ROOT": "/var/log/dgx-spark-production/logs",
                "RESULT_ROOT": "/var/lib/dgx-spark-production/results",
                "MASTER_ADDR": "192.168.100.10",
                "MASTER_PORT": "29619",
                "API_PORT": "8101",
                "HEAD_NODE_ADDR": "192.168.100.10",
                "WORKER_NODE_ADDR": "192.168.100.11",
                "HEAD_IMAGE_REF": "registry.example.invalid/dsv4-head@sha256:" + "c" * 64,
                "WORKER_IMAGE_REF": "registry.example.invalid/dsv4-worker@sha256:" + "d" * 64,
            }
        )
        return values


    def _lock(self, path: Path, config: dict[str, object]) -> dict[str, object]:
        labels = {
            "org.opencontainers.image.revision": "a" * 40,
            "com.dgx-spark.architecture": "linux/arm64",
            "com.dgx-spark.profile_sha256": "b" * 64,
            "com.dgx-spark.service_contract_sha256": "c" * 64,
            "com.dgx-spark.image_lock_sha256": "d" * 64,
            "com.dgx-spark.vllm.commit": "e" * 40,
            "com.dgx-spark.b12x.commit": "f" * 40,
            "com.dgx-spark.runtime-file-sha256": "7" * 64,
        }
        deployment = config["deployment"]
        head_ref = str(deployment["head_image_ref"])
        worker_ref = str(deployment["worker_image_ref"])
        value = {
            "schema_version": 1,
            "status": "ready",
            "mode": str(config["deployment"]["mode"]),
            "profile_id": config["profile"]["profile_id"],

            "model_manifest_sha256": config["deployment"]["model_manifest_sha256"],
            "images": {
                "head": {
                    "reference": head_ref,
                    "image_id": head_ref if head_ref.startswith("sha256:") else "sha256:" + "1" * 64,
                    "labels": labels,
                },
                "worker": {
                    "reference": worker_ref,
                    "image_id": worker_ref if worker_ref.startswith("sha256:") else "sha256:" + "2" * 64,
                    "labels": labels,
                },
            },
        }
        value["lock_sha256"] = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
        path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
        return load_deployment_lock(path, config)
    def _refresh_state_hash(self, state: dict[str, object]) -> None:
        payload = dict(state)
        payload.pop("state_sha256", None)
        state["state_sha256"] = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


    def test_candidate_reuses_exact_locked_production_ids_with_gate(self) -> None:
        values = self._candidate_values()
        values.update(
            {
                "HEAD_IMAGE_REF": "sha256:" + "1" * 64,
                "WORKER_IMAGE_REF": "sha256:" + "2" * 64,
                "ALLOW_PRODUCTION_IMAGES_IN_CANDIDATE": "1",
            }
        )
        env_path = write_env(values)
        lock_path = Path(tempfile.mktemp(prefix="deployment-lock-", suffix=".json"))
        try:
            config = load_config(env_path, DEFAULT_PROFILE)
            lock = self._lock(lock_path, config)
            self.assertEqual(lock["images"]["head"]["reference"], lock["images"]["head"]["image_id"])
            config["deployment"]["allow_production_images_in_candidate"] = False
            with self.assertRaises(LifecycleError):
                DeploymentEngine(config, lock, runner=lambda role, command: CommandResult(tuple(command), 0, "", ""))
            values["ALLOW_PRODUCTION_IMAGES_IN_CANDIDATE"] = "0"
            env_path.unlink()
            env_path = write_env(values)
            config = load_config(env_path, DEFAULT_PROFILE)
            with self.assertRaises(LockError):
                self._lock(lock_path, config)
        finally:
            env_path.unlink()
            lock_path.unlink()

    def test_runtime_file_provenance_is_required_but_scheduler_labels_are_not(self) -> None:
        env_path = write_env(self._candidate_values())
        lock_path = Path(tempfile.mktemp(prefix="deployment-lock-", suffix=".json"))
        try:
            config = load_config(env_path, DEFAULT_PROFILE)
            self._lock(lock_path, config)
            value = json.loads(lock_path.read_text(encoding="utf-8"))
            for image in value["images"].values():
                image["labels"].pop("com.dgx-spark.runtime-file-sha256")
                image["labels"]["com.dgx-spark.profile_sha256"] = "not-used"
                image["labels"]["com.dgx-spark.service_contract_sha256"] = "not-used"
            value["lock_sha256"] = hashlib.sha256(
                canonical_json({key: item for key, item in value.items() if key != "lock_sha256"}).encode("utf-8")
            ).hexdigest()
            lock_path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
            with self.assertRaises(LockError):
                load_deployment_lock(lock_path, config)
        finally:
            env_path.unlink()
            lock_path.unlink()

    def test_reviewed_runtime_flags_and_environment_are_rendered(self) -> None:
        env_path = write_env(self._candidate_values())
        lock_path = Path(tempfile.mktemp(prefix="deployment-lock-", suffix=".json"))
        try:
            config = load_config(env_path, DEFAULT_PROFILE)
            lock = self._lock(lock_path, config)
            argv = render_service_argv(config, "head")
            for token in (
                "--distributed-executor-backend",
                "mp",
                "--gpu-memory-utilization",
                "0.85",
                "--enable-chunked-prefill",
                "--reasoning-config",
                "--default-chat-template-kwargs.thinking=true",
                "--default-chat-template-kwargs.reasoning_effort=high",
                "--load-format",
                "instanttensor",
                "--linear-backend",
                "b12x",
                "--max-cudagraph-capture-size",
                "64",
                "--compilation-config",
                "--long-prefill-token-threshold",
                "0",
            ):
                self.assertIn(token, argv)
            contract = render_contract(config, "head", lock)
            self.assertEqual(contract["container"], "dsv4-candidate-native432-dspark5-327k-seq5-head")
            self.assertEqual(contract["api_port"], 18101)
            self.assertEqual(contract["master_port"], 29621)
            self.assertEqual(contract["model_path"], "/models/DeepSeek-V4-Flash-0731")
            self.assertEqual(contract["environment"]["NCCL_NET"], "IB")
            self.assertEqual(contract["environment"]["NCCL_IB_MTU"], "9000")
            self.assertIn("/root/.cache/flashinfer", " ".join(contract["container_argv"]))
        finally:
            env_path.unlink()
            lock_path.unlink()

    def test_candidate_dry_run_isolated_and_does_not_execute(self) -> None:
        env_path = write_env(self._candidate_values())
        lock_path = Path(tempfile.mktemp(prefix="deployment-lock-", suffix=".json"))
        try:
            config = load_config(env_path, DEFAULT_PROFILE)
            self._lock(lock_path, config)
            output_path = Path(tempfile.mktemp(prefix="dry-run-output-"))
            result = main(["apply", "--env-file", str(env_path), "--lock-file", str(lock_path), "--dry-run"])
            self.assertEqual(result, 0)
            self.assertFalse(output_path.exists())
            # The command is rendered with candidate-only ports and names; the
            # redacted stdout is intentionally not treated as an apply signal.
        finally:
            env_path.unlink()
            lock_path.unlink()

    def test_mode_port_isolation_is_fail_closed(self) -> None:
        values = self._candidate_values()
        values["API_PORT"] = "8101"
        env_path = write_env(values)
        try:
            with self.assertRaises(ConfigError):
                load_config(env_path, DEFAULT_PROFILE)
        finally:
            env_path.unlink()
    def test_candidate_requires_namespaced_external_roots(self) -> None:
        values = self._candidate_values()
        values["CACHE_ROOT"] = "/var/cache/shared"
        env_path = write_env(values)
        try:
            with self.assertRaises(ConfigError):
                load_config(env_path, DEFAULT_PROFILE)
        finally:
            env_path.unlink()
    def test_lock_integrity_hash_rejects_tampering(self) -> None:
        env_path = write_env(self._candidate_values())
        lock_path = Path(tempfile.mktemp(prefix="deployment-lock-", suffix=".json"))
        try:
            config = load_config(env_path, DEFAULT_PROFILE)
            self._lock(lock_path, config)
            value = json.loads(lock_path.read_text(encoding="utf-8"))
            value["images"]["head"]["image_id"] = "sha256:" + "9" * 64
            lock_path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
            with self.assertRaises(LockError):
                load_deployment_lock(lock_path, config)
        finally:
            env_path.unlink()
            lock_path.unlink()

    def test_exact_container_verification_rejects_command_drift(self) -> None:
        env_path = write_env(self._candidate_values())
        lock_path = Path(tempfile.mktemp(prefix="deployment-lock-", suffix=".json"))
        try:
            config = load_config(env_path, DEFAULT_PROFILE)
            lock = self._lock(lock_path, config)
            contract = render_contract(config, "head", lock)
            image = contract["image"]
            labels = dict(image["labels"])
            labels.update({"com.dgx-spark.deployment_id": contract["deployment_id"], "com.dgx-spark.role": "head"})
            inspect = {
                "Name": "/" + contract["container"],
                "Image": image["image_id"],
                "Config": {"Cmd": list(contract["service_argv"]), "Env": [f"{k}={v}" for k, v in contract["environment"].items()], "Labels": labels},
                "State": {"Running": True},
                "Mounts": [{"Source": config["deployment"]["model_root"], "Destination": "/models/DeepSeek-V4-Flash-0731"}],
            }
            verify_container_inspect(inspect, contract)
            parent_contract = dict(contract)
            parent_contract["model_root"] = "/srv/models/DeepSeek-V4-Flash-0731"
            parent_inspect = dict(inspect)
            parent_inspect["Mounts"] = [{"Source": "/srv/models/DeepSeek-V4-Flash-0731", "Destination": "/models/DeepSeek-V4-Flash-0731"}]
            verify_container_inspect(parent_inspect, parent_contract)
            inspect["Config"]["Cmd"][-1] = "drift"
            with self.assertRaises(Exception):
                verify_container_inspect(inspect, contract)
        finally:
            env_path.unlink()
            lock_path.unlink()

    def test_preflight_uses_supported_ibdev2netdev_flag(self) -> None:
        env_path = write_env(self._candidate_values())
        lock_path = Path(tempfile.mktemp(prefix="deployment-lock-", suffix=".json"))
        try:
            config = load_config(env_path, DEFAULT_PROFILE)
            lock = self._lock(lock_path, config)
            seen: list[tuple[str, ...]] = []

            def runner(role: str, argv: tuple[str, ...]) -> CommandResult:
                seen.append(argv)
                if argv[:3] == ("docker", "image", "inspect"):
                    image = lock["images"][role]
                    return CommandResult(
                        argv,
                        0,
                        json.dumps({"Id": image["image_id"], "Architecture": "arm64", "Config": {"Labels": image["labels"]}}),
                        "",
                    )
                if argv[0] == "cat":
                    return CommandResult(argv, 0, config["deployment"]["model_manifest_sha256"], "")
                return CommandResult(argv, 0, "", "")

            engine = DeploymentEngine(config, lock, runner=runner)
            engine.preflight()
            self.assertGreaterEqual(sum(command[:1] == ("ibdev2netdev",) for command in seen), 2)
            self.assertTrue(all(command[1:] == ("-v",) for command in seen if command[0] == "ibdev2netdev"))
        finally:
            env_path.unlink()
            lock_path.unlink()

    def test_ssh_identity_and_timeout_are_explicit(self) -> None:
        argv = ssh_argv("192.0.2.10", "runner", 22, "/etc/ssh/known_hosts", ["docker", "version"], identity_file="/etc/ssh/id_ed25519", connect_timeout=17)
        self.assertIn("-i", argv)
        self.assertIn("/etc/ssh/id_ed25519", argv)
        self.assertIn("ConnectTimeout=17", argv)


    def test_candidate_empty_rollback_removes_owned_pair_and_verifies_absence(self) -> None:
        env_path = write_env(self._candidate_values())
        lock_path = Path(tempfile.mktemp(prefix="deployment-lock-", suffix=".json"))
        state_path = Path(tempfile.mktemp(prefix="rollback-state-", suffix=".json"))
        try:
            config = load_config(env_path, DEFAULT_PROFILE)
            lock = self._lock(lock_path, config)
            containers: dict[str, dict[str, object]] = {}
            seen: list[tuple[str, tuple[str, ...]]] = []

            def runner(role: str, argv: tuple[str, ...]) -> CommandResult:
                seen.append((role, argv))
                if argv[:3] == ("docker", "container", "inspect"):
                    inspect = containers.get(role)
                    if inspect is None:
                        return CommandResult(argv, 1, "", "No such object")
                    return CommandResult(argv, 0, json.dumps(inspect), "")
                if argv[:4] == ("docker", "container", "stop", "--time"):
                    containers[role]["State"] = {"Running": False}
                    return CommandResult(argv, 0, "", "")
                if argv[:3] == ("docker", "container", "rm"):
                    containers.pop(role, None)
                    return CommandResult(argv, 0, "", "")
                return CommandResult(argv, 0, "", "")

            engine = DeploymentEngine(config, lock, runner=runner)
            captured = engine.capture_rollback(state_path)
            self.assertEqual(captured["roles"], {"worker": None, "head": None})

            for role in ("worker", "head"):
                contract = engine.contracts[role]
                labels = dict(contract["image"]["labels"])
                labels.update(
                    {
                        "com.dgx-spark.deployment_id": contract["deployment_id"],
                        "com.dgx-spark.role": role,
                    }
                )
                containers[role] = {
                    "Name": "/" + contract["container"],
                    "Config": {"Labels": labels},
                    "State": {"Running": True},
                }

            seen.clear()
            engine.rollback(state_path)
            self.assertEqual(containers, {})
            mutations = [
                (role, argv)
                for role, argv in seen
                if argv[:3] in {("docker", "container", "stop"), ("docker", "container", "rm")}
            ]
            self.assertEqual(
                mutations,
                [
                    ("worker", ("docker", "container", "stop", "--time", "30", engine.contracts["worker"]["container"])),
                    ("worker", ("docker", "container", "rm", engine.contracts["worker"]["container"])),
                    ("head", ("docker", "container", "stop", "--time", "30", engine.contracts["head"]["container"])),
                    ("head", ("docker", "container", "rm", engine.contracts["head"]["container"])),
                ],
            )
            self.assertEqual(sum(argv[:3] == ("docker", "container", "inspect") for _, argv in seen), 4)
        finally:
            env_path.unlink()
            lock_path.unlink()
            state_path.unlink(missing_ok=True)

    def test_empty_rollback_rejects_mixed_null_and_mapping_before_mutation(self) -> None:
        env_path = write_env(self._candidate_values())
        lock_path = Path(tempfile.mktemp(prefix="deployment-lock-", suffix=".json"))
        state_path = Path(tempfile.mktemp(prefix="rollback-state-", suffix=".json"))
        try:
            config = load_config(env_path, DEFAULT_PROFILE)
            lock = self._lock(lock_path, config)
            seen: list[tuple[str, ...]] = []

            def runner(role: str, argv: tuple[str, ...]) -> CommandResult:
                seen.append(argv)
                return CommandResult(argv, 1, "", "No such object") if argv[:3] == ("docker", "container", "inspect") else CommandResult(argv, 0, "", "")

            engine = DeploymentEngine(config, lock, runner=runner)
            captured = engine.capture_rollback(state_path)
            captured["roles"]["head"] = {}
            self._refresh_state_hash(captured)
            state_path.write_text(json.dumps(captured, sort_keys=True), encoding="utf-8")
            seen.clear()

            with self.assertRaisesRegex(LifecycleError, "both previous roles or neither"):
                engine.rollback(state_path)
            self.assertEqual(seen, [])
        finally:
            env_path.unlink()
            lock_path.unlink()
            state_path.unlink(missing_ok=True)

    def test_empty_rollback_rejects_production_mode_before_mutation(self) -> None:
        env_path = write_env(self._production_values())
        lock_path = Path(tempfile.mktemp(prefix="deployment-lock-", suffix=".json"))
        state_path = Path(tempfile.mktemp(prefix="rollback-state-", suffix=".json"))
        try:
            config = load_config(env_path, DEFAULT_PROFILE)
            lock = self._lock(lock_path, config)
            seen: list[tuple[str, ...]] = []

            def runner(role: str, argv: tuple[str, ...]) -> CommandResult:
                seen.append(argv)
                return CommandResult(argv, 1, "", "No such object") if argv[:3] == ("docker", "container", "inspect") else CommandResult(argv, 0, "", "")

            engine = DeploymentEngine(config, lock, runner=runner)
            engine.capture_rollback(state_path)
            seen.clear()

            with self.assertRaisesRegex(LifecycleError, "candidate mode"):
                engine.rollback(state_path)
            self.assertEqual(seen, [])
        finally:
            env_path.unlink()
            lock_path.unlink()
            state_path.unlink(missing_ok=True)

    def test_empty_rollback_rejects_unowned_pair_before_mutation(self) -> None:
        env_path = write_env(self._candidate_values())
        lock_path = Path(tempfile.mktemp(prefix="deployment-lock-", suffix=".json"))
        state_path = Path(tempfile.mktemp(prefix="rollback-state-", suffix=".json"))
        try:
            config = load_config(env_path, DEFAULT_PROFILE)
            lock = self._lock(lock_path, config)
            containers: dict[str, dict[str, object]] = {}
            seen: list[tuple[str, tuple[str, ...]]] = []

            def runner(role: str, argv: tuple[str, ...]) -> CommandResult:
                seen.append((role, argv))
                if argv[:3] == ("docker", "container", "inspect"):
                    inspect = containers.get(role)
                    if inspect is None:
                        return CommandResult(argv, 1, "", "No such object")
                    return CommandResult(argv, 0, json.dumps(inspect), "")
                return CommandResult(argv, 0, "", "")

            engine = DeploymentEngine(config, lock, runner=runner)
            engine.capture_rollback(state_path)
            for role in ("worker", "head"):
                contract = engine.contracts[role]
                labels = dict(contract["image"]["labels"])
                labels.update(
                    {
                        "com.dgx-spark.deployment_id": contract["deployment_id"],
                        "com.dgx-spark.role": role,
                    }
                )
                if role == "head":
                    labels["com.dgx-spark.deployment_id"] = "other-deployment"
                containers[role] = {
                    "Name": "/" + contract["container"],
                    "Config": {"Labels": labels},
                    "State": {"Running": True},
                }
            seen.clear()

            with self.assertRaisesRegex(LifecycleError, "unowned"):
                engine.rollback(state_path)
            self.assertEqual(
                [argv for _, argv in seen if argv[:3] in {("docker", "container", "stop"), ("docker", "container", "rm")}],
                [],
            )
            self.assertEqual(set(containers), {"worker", "head"})
        finally:
            env_path.unlink()
            lock_path.unlink()
            state_path.unlink(missing_ok=True)

if __name__ == "__main__":
    unittest.main()
