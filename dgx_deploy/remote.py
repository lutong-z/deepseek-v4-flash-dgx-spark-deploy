"""Safe argument construction and explicit mutation boundary."""

from __future__ import annotations

import re
from collections.abc import Sequence


class MutationDisabled(RuntimeError):
    """The scaffold deliberately cannot mutate hosts or containers."""


_SAFE_HOST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
_SAFE_USER = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")


def ssh_argv(host: str, user: str, port: int, known_hosts: str, command: Sequence[str]) -> list[str]:
    """Build an SSH argv without shell interpolation or execution."""

    if not _SAFE_HOST.fullmatch(host) or "@" in host or "/" in host:
        raise ValueError("SSH host must be an explicit address")
    if not _SAFE_USER.fullmatch(user):
        raise ValueError("SSH user contains unsafe characters")
    if not 1 <= port <= 65535:
        raise ValueError("SSH port is outside the valid range")
    if not known_hosts.startswith("/") or ".." in known_hosts.split("/"):
        raise ValueError("known-hosts path must be absolute and traversal-free")
    if not command or any(not isinstance(token, str) or not token for token in command):
        raise ValueError("SSH command must contain non-empty argument tokens")
    return [
        "ssh",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={known_hosts}",
        "-p",
        str(port),
        f"{user}@{host}",
        *command,
    ]


def reject_mutation(*, apply: bool = False, confirm: str | None = None) -> None:
    """Reject all mutation attempts until a separately reviewed implementation."""

    if apply or confirm is not None:
        raise MutationDisabled("host, network, image, and container mutation is disabled in this scaffold")
