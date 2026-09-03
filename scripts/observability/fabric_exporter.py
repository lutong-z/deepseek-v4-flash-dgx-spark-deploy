#!/usr/bin/env python3
"""Small read-only Prometheus exporter for DGX RoCE/Infiniband state.

The process reads only the host sysfs tree mounted at ``/host/sys``.  It does
not require the Docker socket, privileges, or a mutable configuration file.
Metrics intentionally describe the facts needed to catch bad fabric setup:
GID validity/type, ndev mapping, RoCE v2 presence, carrier state, and link flap
counts.
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import html
import os
from pathlib import Path
import socket
import time
from typing import Iterable

SYS_ROOT = Path(os.environ.get("FABRIC_SYS_ROOT", "/host/sys"))
FABRIC_HCA = os.environ.get("FABRIC_HCA", "rocep1s0f1")
FABRIC_PORT = os.environ.get("FABRIC_PORT", "1")
FABRIC_NDEV = os.environ.get("FABRIC_NDEV", "enp1s0f1np1")
FABRIC_GID_INDEX = os.environ.get("FABRIC_GID_INDEX", "4")


def _label(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace("\"", '\\"').replace("\n", "") + '"'


def _metric(name: str, value: str | int | float, labels: dict[str, str] | None = None) -> str:
    suffix = ""
    if labels:
        suffix = "{" + ",".join(f"{key}={_label(labels[key])}" for key in sorted(labels)) + "}"
    return f"{name}{suffix} {value}\n"


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except (FileNotFoundError, PermissionError, OSError):
        return ""


def _entries(path: Path) -> Iterable[Path]:
    try:
        return tuple(path.iterdir())
    except (FileNotFoundError, PermissionError, OSError):
        return ()

def exposition() -> str:
    labels = {"device": FABRIC_HCA, "port": FABRIC_PORT, "index": FABRIC_GID_INDEX}
    gid_path = SYS_ROOT / "class" / "infiniband" / FABRIC_HCA / "ports" / FABRIC_PORT / "gids" / FABRIC_GID_INDEX
    type_path = SYS_ROOT / "class" / "infiniband" / FABRIC_HCA / "ports" / FABRIC_PORT / "gid_attrs" / "types" / FABRIC_GID_INDEX
    ndev_path = SYS_ROOT / "class" / "infiniband" / FABRIC_HCA / "ports" / FABRIC_PORT / "gid_attrs" / "ndevs" / FABRIC_GID_INDEX
    gid = _read(gid_path).lower()
    gid_type = _read(type_path)
    ndev = _read(ndev_path)
    nonzero = bool(gid and gid != "0000:0000:0000:0000:0000:0000:0000:0000")
    ipv4_mapped = gid.startswith("0000:0000:0000:0000:0000:ffff:")
    ndev_match = ndev == FABRIC_NDEV
    rocev2 = gid_type.lower() == "roce v2"
    carrier = _read(SYS_ROOT / "class" / "net" / FABRIC_NDEV / "carrier")
    flaps = _read(SYS_ROOT / "class" / "net" / FABRIC_NDEV / "carrier_changes")
    mtu = _read(SYS_ROOT / "class" / "net" / FABRIC_NDEV / "mtu")
    rdma_state = _read(
        SYS_ROOT / "class" / "infiniband" / FABRIC_HCA / "ports" / FABRIC_PORT / "state"
    ).lower()
    lines = [
        "# HELP dgx_fabric_gid_valid Whether the selected GID is non-zero.",
        "# TYPE dgx_fabric_gid_valid gauge",
        "# HELP dgx_fabric_gid_ipv4_mapped Whether the selected GID is IPv4-mapped.",
        "# TYPE dgx_fabric_gid_ipv4_mapped gauge",
        "# HELP dgx_fabric_gid_type_info Selected GID type.",
        "# TYPE dgx_fabric_gid_type_info gauge",
        "# HELP dgx_fabric_link_up Whether the reviewed netdev carrier is up.",
        "# TYPE dgx_fabric_link_up gauge",
        "# HELP dgx_fabric_mtu Reviewed netdev MTU.",
        "# TYPE dgx_fabric_mtu gauge",
        "# HELP dgx_fabric_rdma_link_up Whether the reviewed RDMA port is active.",
        "# TYPE dgx_fabric_rdma_link_up gauge",
        "# HELP dgx_fabric_rocev2_gid Whether the selected GID is RoCE v2.",
        "# TYPE dgx_fabric_rocev2_gid gauge",
        "# HELP dgx_fabric_link_flaps Number of carrier changes reported by sysfs.",
        "# TYPE dgx_fabric_link_flaps counter",
        "# HELP dgx_fabric_exporter_build_info Exporter build and host identity.",
        "# TYPE dgx_fabric_exporter_build_info gauge",
    ]
    lines.append(_metric("dgx_fabric_exporter_build_info", 1, {"hostname": socket.gethostname(), "interface": FABRIC_NDEV}))
    lines.append(_metric("dgx_fabric_gid_valid", int(nonzero), labels))
    lines.append(_metric("dgx_fabric_gid_ipv4_mapped", int(ipv4_mapped), labels))
    lines.append(_metric("dgx_fabric_gid_type_info", 1, {**labels, "gid_type": gid_type or "unknown"}))
    lines.append(_metric("dgx_fabric_ndev_match", int(ndev_match), {**labels, "ndev": ndev or "unknown"}))
    lines.append(_metric("dgx_fabric_rocev2_gid", int(rocev2 and nonzero and ndev_match), labels))
    lines.append(_metric("dgx_fabric_link_up", int(carrier == "1"), {"ndev": FABRIC_NDEV}))
    lines.append(_metric("dgx_fabric_mtu", int(mtu) if mtu.isdigit() else 0, {"ndev": FABRIC_NDEV}))
    lines.append(_metric("dgx_fabric_rdma_link_up", int(rdma_state in {"active", "port_active"} or "active" in rdma_state), {"device": FABRIC_HCA, "port": FABRIC_PORT}))
    if flaps.isdigit():
        lines.append(_metric("dgx_fabric_link_flaps", flaps, {"ndev": FABRIC_NDEV}))
    return "".join(lines)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib API
        if self.path.split("?", 1)[0] not in ("/metrics", "/health"):
            self.send_error(404)
            return
        body = exposition().encode("utf-8") if self.path.startswith("/metrics") else b"ok\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        return


def main() -> None:
    host = os.environ.get("FABRIC_LISTEN_HOST", "0.0.0.0")
    port = int(os.environ.get("FABRIC_LISTEN_PORT", "9100"))
    server = ThreadingHTTPServer((host, port), Handler)
    server.serve_forever(poll_interval=1.0)


if __name__ == "__main__":
    main()
