"""Fail-closed f0/f1/auto fabric commands and read-back validation."""

from __future__ import annotations

import ipaddress
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any


class FabricError(ValueError):
    """A fabric profile or host read-back is unsafe or inconsistent."""


_F1_IFACE = "enp1s0f1np1"
_F1_HCA = "rocep1s0f1"
_F1_MTU = 9000
_F1_GID = re.compile(r"^(?:[0-9a-f]{4}:){7}[0-9a-f]{4}$", re.IGNORECASE)


def _fail(condition: bool, message: str) -> None:
    if condition:
        raise FabricError(message)


def _role_value(deployment: Mapping[str, Any], role: str, suffix: str) -> str:
    if role not in {"head", "worker"}:
        raise FabricError(f"unsupported role: {role}")
    return str(deployment[f"{role}_{suffix}"])


def profile(config: Mapping[str, Any]) -> str:
    deployment = config.get("deployment")
    _fail(not isinstance(deployment, Mapping), "deployment section is missing")
    value = str(deployment.get("fabric_profile", "auto"))
    _fail(value not in {"f0", "f1", "auto"}, "fabric profile must be f0, f1, or auto")
    return value


def fabric_spec(config: Mapping[str, Any], role: str) -> dict[str, Any]:
    deployment = config.get("deployment")
    _fail(not isinstance(deployment, Mapping), "deployment section is missing")
    role_gid = deployment.get(f"{role}_roce_gid_index")
    global_gid = deployment.get("roce_gid_index")
    value: dict[str, Any] = {
        "profile": profile(config),
        "role": role,
        "interface": _role_value(deployment, role, "net_iface"),
        "hca": _role_value(deployment, role, "hca"),
        "address": _role_value(deployment, role, "node_addr"),
        "peer": deployment.get(f"{role}_fabric_peer"),
        "cidr": deployment.get(f"{role}_fabric_cidr"),
        "connection": deployment.get(f"{role}_fabric_connection"),
        "gid_index": role_gid if role_gid is not None else global_gid,
        "mtu": int(deployment["roce_mtu"]),
    }
    if value["profile"] == "f1":
        _fail(value["interface"] != _F1_IFACE, f"{role} f1 interface must be {_F1_IFACE}")
        _fail(value["hca"] != _F1_HCA, f"{role} f1 HCA must be {_F1_HCA}")
        _fail(value["cidr"] is None or value["peer"] is None, f"{role} f1 address and peer are required")
        _fail(value["connection"] is None, f"{role} f1 NetworkManager connection is required")
        _fail(value["mtu"] != _F1_MTU, f"f1 MTU must be {_F1_MTU}")
        try:
            interface = ipaddress.ip_interface(str(value["cidr"]))
            peer = ipaddress.ip_address(str(value["peer"]))
        except ValueError as exc:
            raise FabricError(f"{role} f1 address or peer is malformed") from exc
        _fail(interface.network.prefixlen != 24, f"{role} f1 address must use /24")
        _fail(peer not in interface.network, f"{role} f1 peer is outside the /24")
        if value["gid_index"] is not None:
            _fail(not isinstance(value["gid_index"], int) or not 0 <= value["gid_index"] <= 255, f"{role} f1 GID index is outside 0..255")
    return value


def helper_argv(image_ref: str, command: Sequence[str], *, writable: bool = False) -> list[str]:
    """Run one fixed host command through the reviewed privileged image helper."""

    _fail(not image_ref or any("\x00" in str(token) for token in command), "fabric helper command is malformed")
    return [
        "docker",
        "run",
        "--rm",
        "--privileged",
        "--network",
        "host",
        "--entrypoint",
        "/usr/sbin/chroot",
        "--volume",
        f"/:/host:{'rw' if writable else 'ro'}",
        image_ref,
        "/host",
        *[str(token) for token in command],
    ]


def discovery_commands(config: Mapping[str, Any], role: str, image_ref: str) -> list[list[str]]:
    """Read-only f1/f0 discovery and peer checks."""

    spec = fabric_spec(config, role)
    interface = spec["interface"]
    hca = spec["hca"]
    peer = spec["peer"]
    commands: list[list[str]] = [
        ["ip", "-json", "address", "show", "dev", interface],
        ["ip", "-json", "link", "show", "dev", interface],
        ["rdma", "-j", "link"],
        ["ibdev2netdev", "-v"],
    ]
    if peer:
        commands.append(["ping", "-I", interface, "-c", "3", "-W", "1", str(peer)])
    if spec["gid_index"] is not None:
        commands.append(helper_argv(image_ref, ["/bin/cat", f"/sys/class/infiniband/{hca}/ports/1/gids/{spec['gid_index']}"]))
    else:
        commands.append(gid_discovery_command(config, role, image_ref))
    return commands


def gid_discovery_command(config: Mapping[str, Any], role: str, image_ref: str) -> list[str]:
    spec = fabric_spec(config, role)
    hca = str(spec["hca"])
    script = (
        "for i in $(seq 0 31); do "
        f"p=/sys/class/infiniband/{hca}/ports/1/gids/$i; "
        "if test -r \"$p\"; then g=$(cat \"$p\"); "
        "case \"$g\" in 0000:0000:0000:0000:0000:0000:0000:0000) ;; "
        "*) printf '%s=%s\\n' \"$i\" \"$g\"; exit 0 ;; esac; fi; "
        "done; exit 1"
    )
    return helper_argv(image_ref, ["/bin/sh", "-c", script])


def parse_gid_discovery_output(stdout: str) -> tuple[int, str]:
    for line in stdout.splitlines():
        if "=" not in line:
            continue
        index, value = line.split("=", 1)
        if index.isdecimal() and _F1_GID.fullmatch(value.strip()):
            if set(value.replace(":", "").lower()) != {"0"}:
                return int(index), value.strip().lower()
    raise FabricError("no populated RoCE GID was discovered")
def gid_attribute_commands(config: Mapping[str, Any], role: str, image_ref: str, gid_index: int) -> list[list[str]]:
    spec = fabric_spec(config, role)
    base = f"/sys/class/infiniband/{spec['hca']}/ports/1/gid_attrs"
    return [
        helper_argv(image_ref, ["/bin/cat", f"{base}/ndevs/{gid_index}"]),
        helper_argv(image_ref, ["/bin/cat", f"{base}/types/{gid_index}"]),
    ]


def parse_gid_attributes(ndev_stdout: str, type_stdout: str, spec: Mapping[str, Any]) -> None:
    _fail(ndev_stdout.strip() != str(spec["interface"]), f"{spec['role']} GID ndev does not match f1 interface")
    _fail(type_stdout.strip().lower() not in {"rocev2", "roce v2"}, f"{spec['role']} GID type is not RoCEv2")


def apply_commands(config: Mapping[str, Any], role: str, image_ref: str) -> list[list[str]]:
    """Build idempotent f1 address/MTU/up and persistent NetworkManager argv."""

    spec = fabric_spec(config, role)
    _fail(spec["profile"] != "f1", "network apply requires FABRIC_PROFILE=f1")
    iface = str(spec["interface"])
    cidr = str(spec["cidr"])
    connection = str(spec["connection"])
    mtu = str(spec["mtu"])
    ensure_script = (
        "if /usr/bin/nmcli connection show \"$1\" >/dev/null 2>&1; then exit 0; "
        "else /usr/bin/nmcli connection add type ethernet ifname \"$2\" con-name \"$1\"; fi"
    )
    return [
        helper_argv(image_ref, ["/bin/sh", "-c", ensure_script, "dgx-fabric", connection, iface], writable=True),
        helper_argv(image_ref, ["/bin/ip", "link", "set", "dev", iface, "mtu", mtu], writable=True),
        helper_argv(image_ref, ["/bin/ip", "addr", "replace", cidr, "dev", iface], writable=True),
        helper_argv(image_ref, ["/bin/ip", "link", "set", "dev", iface, "up"], writable=True),
        helper_argv(
            image_ref,
            [
                "/usr/bin/nmcli",
                "connection",
                "modify",
                connection,
                "ipv4.method",
                "manual",
                "ipv4.addresses",
                cidr,
                "ipv4.never-default",
                "yes",
                "ipv6.method",
                "disabled",
                "connection.autoconnect",
                "yes",
                "802-3-ethernet.mtu",
                mtu,
            ],
            writable=True,
        ),
        helper_argv(image_ref, ["/usr/bin/nmcli", "connection", "up", connection], writable=True),
    ]


def parse_address_output(stdout: str, spec: Mapping[str, Any]) -> None:
    try:
        records = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise FabricError("ip address read-back was not JSON") from exc
    _fail(not isinstance(records, list) or not records, f"{spec['role']} interface address read-back is empty")
    found = False
    expected_ip = str(spec["address"])
    expected_prefix = int(str(spec["cidr"]).rsplit("/", 1)[1]) if spec.get("cidr") else None
    for record in records:
        if not isinstance(record, Mapping):
            continue
        for addr in record.get("addr_info", []):
            if isinstance(addr, Mapping) and addr.get("local") == expected_ip and addr.get("prefixlen") == expected_prefix:
                found = True
                break
    _fail(not found, f"{spec['role']} f1 address/prefix is missing")


def parse_link_output(stdout: str, spec: Mapping[str, Any]) -> None:
    try:
        records = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise FabricError("ip link read-back was not JSON") from exc
    _fail(not isinstance(records, list) or len(records) != 1, f"{spec['role']} interface link read-back is ambiguous")
    record = records[0]
    _fail(not isinstance(record, Mapping), f"{spec['role']} interface link read-back is malformed")
    _fail(record.get("ifname") != spec["interface"], f"{spec['role']} interface name drifted")
    _fail(int(record.get("mtu", 0)) != int(spec["mtu"]), f"{spec['role']} interface MTU drifted")
    _fail(str(record.get("operstate", "")).upper() != "UP", f"{spec['role']} interface is not up")
    flags = {str(flag).upper() for flag in record.get("flags", [])}
    _fail("LOWER_UP" not in flags and record.get("link_detected") not in {True, 1}, f"{spec['role']} interface carrier is down")


def parse_gid_output(stdout: str, spec: Mapping[str, Any]) -> None:
    value = stdout.strip().lower()
    _fail(not _F1_GID.fullmatch(value), f"{spec['role']} GID read-back is malformed")
    _fail(set(value.replace(":", "")) == {"0"}, f"{spec['role']} configured GID is all-zero")


def verify_rdma_output(stdout: str, spec: Mapping[str, Any]) -> None:
    text = stdout.lower()
    _fail(str(spec["hca"]).lower() not in text, f"{spec['role']} HCA is absent from RDMA mapping")
    _fail(str(spec["interface"]).lower() not in text and "active" not in text, f"{spec['role']} HCA/interface mapping is not active")
