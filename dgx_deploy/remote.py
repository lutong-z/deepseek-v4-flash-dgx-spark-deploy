"""Safe tokenized SSH/SCP execution boundary."""

from __future__ import annotations

import re
import shlex
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Callable


class MutationDisabled(RuntimeError):
    """A mutation was requested without the explicit deployment gate."""


class RemoteError(RuntimeError):
    """A remote command failed or returned malformed output."""


_SAFE_HOST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
_SAFE_USER = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")


def _validate_known_hosts(known_hosts: str) -> None:
    if not known_hosts.startswith("/") or ".." in known_hosts.split("/"):
        raise ValueError("known-hosts path must be absolute and traversal-free")


def ssh_argv(
    host: str,
    user: str,
    port: int,
    known_hosts: str,
    command: Sequence[str],
    *,
    identity_file: str | None = None,
    connect_timeout: int = 10,
) -> list[str]:
    """Build an SSH argv without shell interpolation or execution."""

    if not _SAFE_HOST.fullmatch(host) or "@" in host or "/" in host:
        raise ValueError("SSH host must be an explicit address")
    if not _SAFE_USER.fullmatch(user):
        raise ValueError("SSH user contains unsafe characters")
    if not 1 <= port <= 65535:
        raise ValueError("SSH port is outside the valid range")
    _validate_known_hosts(known_hosts)
    if identity_file is not None:
        if not identity_file.startswith("/") or ".." in identity_file.split("/"):
            raise ValueError("SSH identity path must be absolute and traversal-free")
    if not 1 <= connect_timeout <= 300:
        raise ValueError("SSH connect timeout is outside the valid range")
    if not command or any(not isinstance(token, str) or not token for token in command):
        raise ValueError("SSH command must contain non-empty argument tokens")
    argv = [
        "ssh",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={known_hosts}",
        "-o",
        f"ConnectTimeout={connect_timeout}",
    ]
    if identity_file is not None:
        argv.extend(["-i", identity_file])
    argv.extend(["-p", str(port), f"{user}@{host}", *command])
    return argv


def scp_argv(
    source: str,
    host: str,
    user: str,
    port: int,
    known_hosts: str,
    destination: str,
    *,
    identity_file: str | None = None,
    connect_timeout: int = 10,
) -> list[str]:
    """Build a strict SCP argv for staging a rendered contract."""

    if not source or not destination or any("\x00" in item for item in (source, destination)):
        raise ValueError("SCP source and destination must be non-empty")
    ssh = ssh_argv(
        host,
        user,
        port,
        known_hosts,
        ["true"],
        identity_file=identity_file,
        connect_timeout=connect_timeout,
    )
    options = ssh[1 : ssh.index("-p")]
    return ["scp", *options, "-P", str(port), source, f"{user}@{host}:{destination}"]


def reject_mutation(*, apply: bool = False, confirm: str | None = None) -> None:
    """Require an explicit deployment ID for every mutating operation."""

    if apply and not confirm:
        raise MutationDisabled("mutation is disabled without --confirm DEPLOYMENT_ID")
    if confirm is not None and not confirm.startswith("deployment-"):
        raise MutationDisabled("mutation is disabled: confirmation must be the rendered deployment ID")

@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""


Runner = Callable[[Sequence[str]], CommandResult]


def run_local(argv: Sequence[str]) -> CommandResult:
    """Run a command with argv semantics and captured output."""

    completed = subprocess.run(list(argv), check=False, capture_output=True, text=True)
    result = CommandResult(tuple(argv), completed.returncode, completed.stdout, completed.stderr)
    if result.returncode:
        detail = result.stderr.strip()
        suffix = f": {detail}" if detail else ""
        raise RemoteError(f"command failed ({result.returncode}): {' '.join(argv)}{suffix}")
    return result


class SSHRunner:
    """Tokenized SSH runner used by lifecycle operations and easy to fake in tests."""

    def __init__(self, deployment: dict[str, object], runner: Runner = run_local) -> None:
        self.deployment = deployment
        self.runner = runner

    def __call__(self, role: str, command: Sequence[str]) -> CommandResult:
        if role not in {"head", "worker"}:
            raise ValueError(f"unsupported role: {role}")
        host = str(self.deployment[f"{role}_host"])
        user = str(self.deployment[f"{role}_ssh_user"])
        argv = ssh_argv(
            host,
            user,
            int(self.deployment["ssh_port"]),
            str(self.deployment["ssh_known_hosts_file"]),
            ["sh", "-c", shlex.join(command)],
            identity_file=self.deployment.get("ssh_identity_file") or None,
        )
        return self.runner(argv)
