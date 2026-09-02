from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from dgx_deploy.cli import main
from dgx_deploy.config import ConfigError, DEFAULT_PROFILE, load_config
from dgx_deploy.lifecycle import DeploymentEngine, verify_container_inspect
from dgx_deploy.locks import load_deployment_lock
from dgx_deploy.remote import CommandResult, ssh_argv
from dgx_deploy.render import render_contract, render_service_argv
from test_config import valid_env, write_env


class LifecycleContractTests(unittest.TestCase):
    def _candidate_values(self) -> dict[str, str]:
        values = valid_env()
        values.update(
            {
                "DEPLOYMENT_MODE": "candidate",
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

    def _lock(self, path: Path, config: dict[str, object]) -> dict[str, object]:
        labels = {
            "org.opencontainers.image.revision": "a" * 40,
            "com.dgx-spark.architecture": "linux/arm64",
            "com.dgx-spark.profile_sha256": "b" * 64,
            "com.dgx-spark.service_contract_sha256": "c" * 64,
            "com.dgx-spark.image_lock_sha256": "d" * 64,
            "com.dgx-spark.vllm.commit": "e" * 40,
            "com.dgx-spark.b12x.commit": "f" * 40,
        }
        value = {
            "schema_version": 1,
            "status": "ready",
            "mode": "candidate",
            "profile_id": config["profile"]["profile_id"],
            "model_manifest_sha256": config["deployment"]["model_manifest_sha256"],
            "images": {
                "head": {
                    "reference": "candidate/dsv4-head@sha256:" + "c" * 64,
                    "image_id": "sha256:" + "1" * 64,
                    "labels": labels,
                },
                "worker": {
                    "reference": "candidate/dsv4-worker@sha256:" + "d" * 64,
                    "image_id": "sha256:" + "2" * 64,
                    "labels": labels,
                },
            },
        }
        path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
        return load_deployment_lock(path, config)

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
                "Mounts": [{"Source": config["deployment"]["model_root"], "Destination": "/models"}],
            }
            verify_container_inspect(inspect, contract)
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
            self.assertEqual(sum(command[:1] == ("ibdev2netdev",) for command in seen), 2)
            self.assertTrue(all(command[1:] == ("-v",) for command in seen if command[0] == "ibdev2netdev"))
        finally:
            env_path.unlink()
            lock_path.unlink()

    def test_ssh_identity_and_timeout_are_explicit(self) -> None:
        argv = ssh_argv("192.0.2.10", "runner", 22, "/etc/ssh/known_hosts", ["docker", "version"], identity_file="/etc/ssh/id_ed25519", connect_timeout=17)
        self.assertIn("-i", argv)
        self.assertIn("/etc/ssh/id_ed25519", argv)
        self.assertIn("ConnectTimeout=17", argv)


if __name__ == "__main__":
    unittest.main()
