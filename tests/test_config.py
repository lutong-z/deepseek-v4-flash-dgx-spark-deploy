from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from dgx_deploy.config import (
    CANDIDATE_SEQ10_PROFILE,
    CANDIDATE_SEQ10_PROFILE_ID,
    ConfigError,
    DEFAULT_PROFILE,
    _validate_profile,
    load_config,
    parse_env_file,
)


def valid_env() -> dict[str, str]:
    return {
        "HEAD_HOST": "192.0.2.10",
        "WORKER_HOST": "192.0.2.11",
        "SSH_USER": "runner",
        "SSH_PORT": "22",
        "SSH_KNOWN_HOSTS_FILE": "/etc/ssh/known_hosts",
        "REMOTE_ROOT": "/srv/dgx-spark/deploy",
        "MODEL_ROOT": "/srv/models/DeepSeek-V4-Flash-0731",
        "MODEL_MANIFEST_SHA256": "a" * 64,
        "STATE_ROOT": "/var/lib/dgx-spark/state",
        "CACHE_ROOT": "/var/cache/dgx-spark",
        "RESULT_ROOT": "/var/lib/dgx-spark/results",
        "IMAGE_REF": "registry.example.invalid/dsv4@sha256:" + "b" * 64,
        "MASTER_ADDR": "192.0.2.10",
        "MASTER_PORT": "29519",
        "API_PORT": "8000",
        "HEAD_NODE_ADDR": "192.0.2.10",
        "WORKER_NODE_ADDR": "192.0.2.11",
        "HEAD_NET_IFACE": "rdma0",
        "WORKER_NET_IFACE": "rdma0",
        "HEAD_HCA": "mlx5_0",
        "WORKER_HCA": "mlx5_0",
        "HEAD_CUDA_VISIBLE_DEVICES": "0",
        "WORKER_CUDA_VISIBLE_DEVICES": "0",
        "ROCE_MTU": "9000",
        "API_BIND_ADDR": "127.0.0.1",
        "FORWARD_LOCAL_PORT": "18080",
    }


def write_env(values: dict[str, str]) -> Path:
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False)
    with handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")
    return Path(handle.name)

def write_raw(lines: list[str]) -> Path:
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False)
    with handle:
        handle.write("\n".join(lines) + "\n")
    return Path(handle.name)


class ConfigTests(unittest.TestCase):
    def test_valid_config_loads_fixed_profile(self) -> None:
        path = write_env(valid_env())
        try:
            config = load_config(path, DEFAULT_PROFILE)
        finally:
            path.unlink()
        self.assertEqual(config["profile"]["profile_id"], "dsv4-native432-b12x-tp2")
        self.assertEqual(config["deployment"]["master_addr"], "192.0.2.10")
    def test_auto_roce_gid_index_leaves_role_discovery_unpinned(self) -> None:
        values = valid_env()
        values["ROCE_GID_INDEX"] = "auto"
        path = write_env(values)
        try:
            config = load_config(path, DEFAULT_PROFILE)
        finally:
            path.unlink()
        self.assertIsNone(config["deployment"]["roce_gid_index"])
        self.assertIsNone(config["deployment"]["head_roce_gid_index"])
        self.assertIsNone(config["deployment"]["worker_roce_gid_index"])


    def test_duplicate_and_shell_records_are_rejected(self) -> None:
        path = write_raw(["HEAD_HOST=192.0.2.10", "HEAD_HOST=192.0.2.11"])
        try:
            with self.assertRaises(ConfigError):
                parse_env_file(path)
        finally:
            path.unlink()
        path = write_env({"HEAD_HOST": "$(uname)"})
        try:
            with self.assertRaises(ConfigError):
                parse_env_file(path)
        finally:
            path.unlink()

    def test_private_bind_and_mutable_image_are_rejected(self) -> None:
        values = valid_env()
        values["API_BIND_ADDR"] = "0.0.0.0"
        path = write_env(values)
        try:
            with self.assertRaises(ConfigError):
                load_config(path, DEFAULT_PROFILE)
        finally:
            path.unlink()
        values = valid_env()
        values["IMAGE_REF"] = "registry.example.invalid/dsv4:latest"
        path = write_env(values)
        try:
            with self.assertRaises(ConfigError):
                load_config(path, DEFAULT_PROFILE)
        finally:
            path.unlink()
    def test_public_production_bind_requires_explicit_gate(self) -> None:
        values = valid_env()
        values.update(
            {
                "DEPLOYMENT_MODE": "production",
                "REMOTE_ROOT": "/srv/dgx-spark/deploy-production",
                "STATE_ROOT": "/var/lib/dgx-spark/state-production",
                "CACHE_ROOT": "/var/cache/dgx-spark/cache-production",
                "LOG_ROOT": "/var/log/dgx-spark/logs-production",
                "RESULT_ROOT": "/var/lib/dgx-spark/results-production",
                "MASTER_ADDR": "192.168.100.10",
                "MASTER_PORT": "29619",
                "API_PORT": "8101",
                "HEAD_NODE_ADDR": "192.168.100.10",
                "WORKER_NODE_ADDR": "192.168.100.11",
                "HEAD_IMAGE_REF": "registry.example.invalid/head@sha256:" + "c" * 64,
                "WORKER_IMAGE_REF": "registry.example.invalid/worker@sha256:" + "d" * 64,
                "API_BIND_ADDR": "0.0.0.0",
            }
        )
        path = write_env(values)
        try:
            with self.assertRaises(ConfigError):
                load_config(path, DEFAULT_PROFILE)
            values["ALLOW_PUBLIC_API"] = "1"
            path.unlink()
            path = write_env(values)
            config = load_config(path, DEFAULT_PROFILE)
            self.assertEqual(config["deployment"]["api_bind_addr"], "0.0.0.0")
            self.assertTrue(config["deployment"]["allow_public_api"])
        finally:
            path.unlink()
    def test_reviewed_seq10_profile_loads_only_for_candidate(self) -> None:
        values = valid_env()
        values.update(
            {
                "DEPLOYMENT_MODE": "candidate",
                "REMOTE_ROOT": "/srv/dgx-spark/deploy-candidate",
                "STATE_ROOT": "/var/lib/dgx-spark/state-candidate",
                "CACHE_ROOT": "/var/cache/dgx-spark/cache-candidate",
                "LOG_ROOT": "/var/log/dgx-spark/logs-candidate",
                "RESULT_ROOT": "/var/lib/dgx-spark/results-candidate",
                "MASTER_ADDR": "192.168.100.10",
                "MASTER_PORT": "29621",
                "API_PORT": "18101",
                "HEAD_NODE_ADDR": "192.168.100.10",
                "WORKER_NODE_ADDR": "192.168.100.11",
                "HEAD_IMAGE_REF": "candidate/head@sha256:" + "c" * 64,
                "WORKER_IMAGE_REF": "candidate/worker@sha256:" + "d" * 64,
            }
        )
        path = write_env(values)
        try:
            config = load_config(path, CANDIDATE_SEQ10_PROFILE)
            self.assertEqual(config["profile"]["profile_id"], CANDIDATE_SEQ10_PROFILE_ID)
            self.assertEqual(config["profile"]["limits"]["max_num_seqs"], 10)
            self.assertEqual(config["profile"]["limits"]["max_num_batched_tokens"], 8192)
            values["DEPLOYMENT_MODE"] = "production"
            path.unlink()
            path = write_env(values)
            with self.assertRaises(ConfigError):
                load_config(path, CANDIDATE_SEQ10_PROFILE)
        finally:
            path.unlink()



    def test_checkout_local_roots_are_rejected(self) -> None:
        values = valid_env()
        values["REMOTE_ROOT"] = str(DEFAULT_PROFILE.parents[2] / "local-root")
        path = write_env(values)
        try:
            with self.assertRaises(ConfigError):
                load_config(path, DEFAULT_PROFILE)
        finally:
            path.unlink()


    def test_profile_path_and_invariants_are_pinned(self) -> None:
        path = write_env(valid_env())
        profile_handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False)
        profile_path = Path(profile_handle.name)
        with profile_handle:
            profile_handle.write(DEFAULT_PROFILE.read_text(encoding="utf-8"))
        try:
            with self.assertRaises(ConfigError):
                load_config(path, profile_path)
        finally:
            path.unlink()
            profile_path.unlink()
        profile = json.loads(DEFAULT_PROFILE.read_text(encoding="utf-8"))
        profile["limits"]["max_num_seqs"] = 6
        with self.assertRaises(ConfigError):
            _validate_profile(profile)

if __name__ == "__main__":
    unittest.main()
