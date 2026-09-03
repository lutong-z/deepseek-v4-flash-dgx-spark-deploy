from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from jsonschema import Draft202012Validator

from scripts.observability import fabric_exporter, stack


ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "config" / "profiles" / "dgx-spark-observability.json"
PROFILE_SCHEMA = ROOT / "config" / "observability.schema.json"
LOCK_SCHEMA = ROOT / "config" / "observability-image-lock.schema.json"
DIGEST = "a" * 64


def lock_document() -> dict[str, object]:
    return {
        "schema_version": 1,
        "status": "ready",
        "architecture": "linux/arm64",
        "images": {
            key: {
                "reference": f"docker.io/example/{key}@sha256:{DIGEST}",
                "digest": DIGEST,
                "image_id": f"sha256:{DIGEST}",
            }
            for key in stack.DEFAULT_IMAGE_TAGS
        },
    }


def env_file(path: Path, *, password: str = "safe-test-password") -> None:
    path.write_text(
        "\n".join(
            [
                "OBS_HEAD_HOST=192.168.100.10",
                "OBS_WORKER_HOST=192.168.100.11",
                "OBS_HEAD_SSH_HOST=dgx-head",
                "OBS_WORKER_SSH_HOST=dgx-worker",
                "OBS_SSH_USER=operator",
                "OBS_REMOTE_ROOT=/var/lib/dgx-spark/observability",
                f"OBS_GRAFANA_ADMIN_PASSWORD={password}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


class ObservabilityStackTests(unittest.TestCase):
    def test_profile_and_lock_schemas_validate_contract(self) -> None:
        profile = json.loads(PROFILE.read_text(encoding="utf-8"))
        profile_schema = json.loads(PROFILE_SCHEMA.read_text(encoding="utf-8"))
        validator = Draft202012Validator(profile_schema)
        validator.check_schema(profile_schema)
        self.assertEqual(list(validator.iter_errors(profile)), [])

        lock_schema = json.loads(LOCK_SCHEMA.read_text(encoding="utf-8"))
        lock_validator = Draft202012Validator(lock_schema)
        lock_validator.check_schema(lock_schema)
        self.assertEqual(list(lock_validator.iter_errors(lock_document())), [])

    def test_renderer_scrapes_production_metrics_and_declares_ports(self) -> None:
        profile = stack.load_profile(PROFILE)
        config = stack._config({}, profile, {key: f"docker.io/example/{key}@sha256:{DIGEST}" for key in stack.DEFAULT_IMAGE_TAGS}, require_remote=False)
        prometheus = stack.render_prometheus(config)
        self.assertIn('targets: ["192.168.100.10:8101"]', prometheus)
        self.assertIn('metrics_path: "/metrics"', prometheus)
        self.assertIn('"192.168.100.10:19100"', prometheus)
        self.assertIn('"192.168.100.11:19110"', prometheus)
        rules = stack.render_rules()
        for alert in ("VLLMMetricsDown", "NodeExporterDown", "FabricExporterDown"):
            self.assertIn(f"alert: {alert}", rules)
        dashboard = json.loads(stack.render_grafana_dashboard())
        self.assertEqual(dashboard["uid"], "dgx-spark-overview")
        self.assertTrue(any(panel["title"] == "vLLM requests running" for panel in dashboard["panels"]))

    def test_image_loader_rejects_mutable_or_wrong_architecture(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lock.json"
            lock = lock_document()
            lock["architecture"] = "linux/amd64"
            path.write_text(json.dumps(lock), encoding="utf-8")
            with self.assertRaises(stack.ObservabilityError):
                stack.load_lock(path)
            lock = lock_document()
            lock["images"]["grafana"]["reference"] = "docker.io/grafana/grafana:11.2.0"
            path.write_text(json.dumps(lock), encoding="utf-8")
            with self.assertRaises(stack.ObservabilityError):
                stack.load_lock(path)

    def test_deploy_dry_run_does_not_spawn_ssh_or_leak_password(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = root / "obs.env"
            lock = root / "obs.lock.json"
            env_file(env, password="dry-" + "run-" + "secret")
            lock.write_text(json.dumps(lock_document()), encoding="utf-8")
            with patch.object(stack.subprocess, "run", side_effect=AssertionError("dry-run spawned process")):
                result = stack.main(["deploy", "--env-file", str(env), "--image-lock", str(lock), "--confirm", "DGX-OBSERVABILITY", "--dry-run"])
            self.assertEqual(result, 0)

    def test_down_script_is_label_scoped_and_keeps_volumes_by_default(self) -> None:
        profile = stack.load_profile(PROFILE)
        config = stack._config(
            {
                "OBS_SSH_USER": "operator",
                "OBS_REMOTE_ROOT": "/var/lib/dgx-spark/observability",
            },
            profile,
            None,
        )
        script = stack._remote_script(config, "head", "down", purge=False)
        self.assertIn("com.dgx-spark.observability", script)
        self.assertNotIn("docker volume rm", script)
        self.assertNotIn("seq10", script.lower())
        self.assertNotIn("vllm", script.lower())


    def test_up_script_scopes_vllm_logs_and_runs_custom_fabric_exporter(self) -> None:
        profile = stack.load_profile(PROFILE)
        config = stack._config(
            {
                "OBS_SSH_USER": "operator",
                "OBS_REMOTE_ROOT": "/var/lib/dgx-spark/observability",
            },
            profile,
            {key: f"docker.io/example/{key}@sha256:{DIGEST}" for key in stack.DEFAULT_IMAGE_TAGS},
        )
        script = stack._remote_script(config, "head", "up")
        self.assertIn("docker inspect -f", script)
        self.assertIn("VLLM_LOG_PATH", script)
        self.assertIn("dst=/var/log/vllm.log", script)
        self.assertNotIn("/var/run/docker.sock", script)
        self.assertNotIn("/var/lib/docker/containers,dst=/var/lib/docker/containers", script)
        self.assertIn("python3 /opt/fabric_exporter.py", script)
        self.assertIn("FABRIC_GID_INDEX", script)

    def test_fabric_exporter_reports_selected_gid_and_link_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = root / "class"
            gid_root = base / "infiniband" / "rocep1s0f1" / "ports" / "1"
            net_root = base / "net" / "enp1s0f1np1"
            (gid_root / "gids").mkdir(parents=True)
            (gid_root / "gid_attrs" / "types").mkdir(parents=True)
            (gid_root / "gid_attrs" / "ndevs").mkdir(parents=True)
            net_root.mkdir(parents=True)
            (gid_root / "gids" / "4").write_text("0000:0000:0000:0000:0000:ffff:c0a8:640a\n", encoding="utf-8")
            (gid_root / "gid_attrs" / "types" / "4").write_text("RoCE v2\n", encoding="utf-8")
            (gid_root / "gid_attrs" / "ndevs" / "4").write_text("enp1s0f1np1\n", encoding="utf-8")
            (gid_root / "state").write_text("ACTIVE\n", encoding="utf-8")
            (net_root / "carrier").write_text("1\n", encoding="utf-8")
            (net_root / "carrier_changes").write_text("2\n", encoding="utf-8")
            (net_root / "mtu").write_text("9000\n", encoding="utf-8")
            old_root = fabric_exporter.SYS_ROOT
            old_values = (
                fabric_exporter.FABRIC_HCA,
                fabric_exporter.FABRIC_PORT,
                fabric_exporter.FABRIC_NDEV,
                fabric_exporter.FABRIC_GID_INDEX,
            )
            try:
                fabric_exporter.SYS_ROOT = base.parent
                fabric_exporter.FABRIC_HCA = "rocep1s0f1"
                fabric_exporter.FABRIC_PORT = "1"
                fabric_exporter.FABRIC_NDEV = "enp1s0f1np1"
                fabric_exporter.FABRIC_GID_INDEX = "4"
                output = fabric_exporter.exposition()
            finally:
                fabric_exporter.SYS_ROOT = old_root
                (
                    fabric_exporter.FABRIC_HCA,
                    fabric_exporter.FABRIC_PORT,
                    fabric_exporter.FABRIC_NDEV,
                    fabric_exporter.FABRIC_GID_INDEX,
                ) = old_values
            self.assertIn("dgx_fabric_gid_valid{device=\"rocep1s0f1\",index=\"4\",port=\"1\"} 1", output)
            self.assertIn("dgx_fabric_gid_ipv4_mapped{device=\"rocep1s0f1\",index=\"4\",port=\"1\"} 1", output)
            self.assertIn("dgx_fabric_rocev2_gid{device=\"rocep1s0f1\",index=\"4\",port=\"1\"} 1", output)
            self.assertIn("dgx_fabric_mtu{ndev=\"enp1s0f1np1\"} 9000", output)
            self.assertIn("dgx_fabric_rdma_link_up{device=\"rocep1s0f1\",port=\"1\"} 1", output)

    def test_environment_parser_rejects_shell_and_unknown_keys(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "obs.env"
            path.write_text("OBS_SSH_USER=$(id)\n", encoding="utf-8")
            with self.assertRaises(stack.ObservabilityError):
                stack.parse_env(path)
            path.write_text("NOT_ALLOWED=secret\n", encoding="utf-8")
            with self.assertRaises(stack.ObservabilityError):
                stack.parse_env(path)


if __name__ == "__main__":
    unittest.main()
