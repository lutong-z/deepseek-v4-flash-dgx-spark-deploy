from __future__ import annotations

import json
import unittest
from pathlib import Path

from dgx_deploy.config import DEFAULT_PROFILE, ConfigError, load_config
from dgx_deploy.lifecycle import DeploymentEngine
from dgx_deploy.remote import CommandResult
from dgx_deploy.fabric import (
    FabricError,
    apply_commands,
    fabric_spec,
    gid_attribute_commands,
    parse_address_output,
    parse_gid_attributes,
    parse_gid_discovery_output,
    parse_gid_output,
    parse_link_output,
)
from dgx_deploy.render import render_environment, render_plan
from test_config import valid_env, write_env


class FabricTests(unittest.TestCase):
    def _f1_config(self) -> dict[str, object]:
        values = valid_env()
        values.update(
            {
                "DEPLOYMENT_MODE": "production",
                "LOG_ROOT": "/var/log/dgx-spark/production",
                "HEAD_IMAGE_REF": "registry.example.invalid/head@sha256:" + "c" * 64,
                "WORKER_IMAGE_REF": "registry.example.invalid/worker@sha256:" + "d" * 64,
                "MASTER_ADDR": "192.168.100.10",
                "MASTER_PORT": "29619",
                "API_PORT": "8101",
                "HEAD_NODE_ADDR": "192.168.100.10",
                "WORKER_NODE_ADDR": "192.168.100.11",
                "FABRIC_PROFILE": "f1",
                "HEAD_NET_IFACE": "enp1s0f1np1",
                "WORKER_NET_IFACE": "enp1s0f1np1",
                "HEAD_HCA": "rocep1s0f1",
                "WORKER_HCA": "rocep1s0f1",
                "HEAD_FABRIC_CIDR": "192.168.100.10/24",
                "WORKER_FABRIC_CIDR": "192.168.100.11/24",
                "HEAD_FABRIC_PEER": "192.168.100.11",
                "WORKER_FABRIC_PEER": "192.168.100.10",
                "HEAD_FABRIC_CONNECTION": "f1-head",
                "WORKER_FABRIC_CONNECTION": "f1-worker",
            }
        )
        path = write_env(values)
        try:
            return load_config(path, DEFAULT_PROFILE)
        finally:
            path.unlink()

    def test_f1_spec_and_nm_commands_are_primary_only(self) -> None:
        config = self._f1_config()
        spec = fabric_spec(config, "head")
        self.assertEqual(spec["interface"], "enp1s0f1np1")
        self.assertEqual(spec["hca"], "rocep1s0f1")
        commands = apply_commands(config, "head", "registry.example.invalid/head@sha256:" + "c" * 64)
        flattened = [token for command in commands for token in command]
        self.assertIn("enp1s0f1np1", flattened)
        self.assertIn("nmcli", " ".join(flattened))
        self.assertIn("modify", flattened)
        self.assertIn("up", flattened)
        self.assertIn("9000", flattened)
        self.assertNotIn("enP2p1s0f1", flattened)
        self.assertNotIn("roceP2p1s0f1", flattened)

    def test_role_gid_zero_is_not_replaced_by_global_gid(self) -> None:
        config = self._f1_config()
        deployment = dict(config["deployment"])
        deployment["head_roce_gid_index"] = 0
        deployment["roce_gid_index"] = 7
        config = dict(config)
        config["deployment"] = deployment
        self.assertEqual(fabric_spec(config, "head")["gid_index"], 0)
        self.assertEqual(render_environment(config, "head")["NCCL_IB_GID_INDEX"], "0")

    def test_f1_nm_apply_persists_address_mtu_and_autoconnect(self) -> None:
        config = self._f1_config()
        commands = apply_commands(config, "head", "registry.example.invalid/head@sha256:" + "c" * 64)
        nm_modify = next(command for command in commands if "modify" in command and "f1-head" in command)
        self.assertEqual(nm_modify[0:9], ["docker", "run", "--rm", "--privileged", "--network", "host", "--entrypoint", "/usr/sbin/chroot", "--volume"])
        self.assertIn("/:/host:rw", nm_modify)
        self.assertIn("ipv4.addresses", nm_modify)
        self.assertIn("192.168.100.10/24", nm_modify)
        self.assertIn("connection.autoconnect", nm_modify)
        self.assertIn("yes", nm_modify)
        self.assertIn("802-3-ethernet.mtu", nm_modify)
        self.assertIn("9000", nm_modify)
        ensure = commands[0]
        self.assertIn("connection show", " ".join(ensure))
        self.assertIn("connection add", " ".join(ensure))

    def test_f1_gid_attributes_bind_index_to_interface_and_rocev2(self) -> None:
        config = self._f1_config()
        commands = gid_attribute_commands(
            config,
            "worker",
            "registry.example.invalid/worker@sha256:" + "d" * 64,
            7,
        )
        self.assertEqual(len(commands), 2)
        self.assertTrue(any("gid_attrs/ndevs/7" in token for token in commands[0]))
        self.assertTrue(any("gid_attrs/types/7" in token for token in commands[1]))
        parse_gid_attributes("enp1s0f1np1\n", "RoCE v2\n", fabric_spec(config, "worker"))
        with self.assertRaises(FabricError):
            parse_gid_attributes("enp1s0f0np0\n", "RoCE v2\n", fabric_spec(config, "worker"))

    def test_f1_plan_exposes_only_f1_fabric_commands(self) -> None:
        config = self._f1_config()
        plan = render_plan(config)
        self.assertEqual(plan["fabric_profile"], "f1")
        self.assertEqual(set(plan["fabric_commands"]), {"head", "worker"})
        for role in ("head", "worker"):
            commands = plan["fabric_commands"][role]
            self.assertTrue(commands["discovery"])
            self.assertTrue(commands["apply"])
            self.assertTrue(all("f0" not in token for group in commands.values() for command in group for token in command if isinstance(token, str)))

    def test_f1_apply_networks_both_roles_before_peer_readback(self) -> None:
        config = self._f1_config()
        labels = {
            "org.opencontainers.image.revision": "a" * 40,
            "com.dgx-spark.architecture": "linux/arm64",
            "com.dgx-spark.profile_sha256": "b" * 64,
            "com.dgx-spark.service_contract_sha256": "c" * 64,
            "com.dgx-spark.image_lock_sha256": "d" * 64,
            "com.dgx-spark.vllm.commit": "e" * 40,
            "com.dgx-spark.b12x.commit": "f" * 40,
        }
        lock = {
            "images": {
                "worker": {
                    "reference": config["deployment"]["worker_image_ref"],
                    "image_id": "sha256:" + "1" * 64,
                    "labels": labels,
                },
                "head": {
                    "reference": config["deployment"]["head_image_ref"],
                    "image_id": "sha256:" + "2" * 64,
                    "labels": labels,
                },
            }
        }
        events: list[tuple[str, str]] = []
        engine = DeploymentEngine(config, lock, runner=lambda role, command: CommandResult(tuple(command), 0, "", ""))
        engine._remote = lambda role, command: events.append(("apply", role)) or CommandResult(tuple(command), 0, "", "")
        engine._fabric_preflight = lambda role, **kwargs: events.append(("preflight", role))
        engine._refresh_contracts = lambda: None
        engine._apply_fabric()
        apply_events = [event for event in events if event[0] == "apply"]
        preflight_events = [event for event in events if event[0] == "preflight"]
        self.assertEqual(apply_events[:6], [("apply", "worker")] * 6)
        self.assertEqual(apply_events[6:12], [("apply", "head")] * 6)
        self.assertEqual(preflight_events, [("preflight", "worker"), ("preflight", "head")])
        self.assertLess(events.index(("apply", "head")), events.index(("preflight", "worker")))

    def test_f1_readback_checks_address_link_and_gid(self) -> None:
        config = self._f1_config()
        spec = fabric_spec(config, "head")
        parse_address_output(
            json.dumps([{"ifname": "enp1s0f1np1", "addr_info": [{"local": "192.168.100.10", "prefixlen": 24}]}]),
            spec,
        )
        parse_link_output(
            json.dumps([{"ifname": "enp1s0f1np1", "mtu": 9000, "operstate": "UP", "flags": ["LOWER_UP"]}]),
            spec,
        )
        parse_gid_output("fe80:0000:0000:0000:0000:0000:0000:0001\n", {"role": "head"})
        self.assertEqual(parse_gid_discovery_output("4=fe80:0000:0000:0000:0000:0000:0000:0001\n"), (4, "fe80:0000:0000:0000:0000:0000:0000:0001"))
        with self.assertRaises(FabricError):
            parse_link_output(
                json.dumps([{"ifname": "enp1s0f1np1", "mtu": 1500, "operstate": "UP", "flags": ["LOWER_UP"]}]),
                spec,
            )
        with self.assertRaises(FabricError):
            parse_gid_output("0000:0000:0000:0000:0000:0000:0000:0000\n", {"role": "head"})

    def test_f1_rejects_wrong_interface_or_peer(self) -> None:
        values = valid_env()
        values.update(
            {
                "DEPLOYMENT_MODE": "production",
                "LOG_ROOT": "/var/log/dgx-spark/production",
                "HEAD_IMAGE_REF": "registry.example.invalid/head@sha256:" + "c" * 64,
                "WORKER_IMAGE_REF": "registry.example.invalid/worker@sha256:" + "d" * 64,
                "MASTER_ADDR": "192.168.100.10",
                "MASTER_PORT": "29619",
                "API_PORT": "8101",
                "HEAD_NODE_ADDR": "192.168.100.10",
                "WORKER_NODE_ADDR": "192.168.100.11",
                "FABRIC_PROFILE": "f1",
                "HEAD_NET_IFACE": "enp1s0f0np0",
                "WORKER_NET_IFACE": "enp1s0f1np1",
                "HEAD_HCA": "rocep1s0f1",
                "WORKER_HCA": "rocep1s0f1",
                "HEAD_FABRIC_CIDR": "192.168.100.10/24",
                "WORKER_FABRIC_CIDR": "192.168.100.11/24",
                "HEAD_FABRIC_PEER": "192.168.100.11",
                "WORKER_FABRIC_PEER": "192.168.100.10",
                "HEAD_FABRIC_CONNECTION": "f1-head",
                "WORKER_FABRIC_CONNECTION": "f1-worker",
            }
        )
        path = write_env(values)
        try:
            with self.assertRaises(ConfigError):
                load_config(path, DEFAULT_PROFILE)
        finally:
            path.unlink()


if __name__ == "__main__":
    unittest.main()
