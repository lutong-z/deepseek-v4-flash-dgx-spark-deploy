from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from dgx_deploy.config import (
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
        "MODEL_ROOT": "/srv/models",
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
