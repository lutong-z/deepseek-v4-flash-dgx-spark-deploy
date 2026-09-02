"""Safe deployment command line interface."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence

from .config import ConfigError, DEFAULT_PROFILE, ROOT, config_sha256, load_config
from .lifecycle import LifecycleError, load_engine
from .locks import LockError, load_deployment_lock
from .manifest import deployment_id, manifest_json
from .redact import redact_mapping
from .remote import MutationDisabled, reject_mutation
from .render import RenderError, render_contract, render_contract_json, render_plan

EXIT_USAGE = 64
EXIT_DISABLED = 78
EXIT_OPERATION = 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dgx-deploy",
        description="Render, verify, and apply locked two-node DGX Spark contracts.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    config = sub.add_parser("config", help="validate or render operator configuration")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    config_render = config_sub.add_parser("render", help="render redacted canonical configuration")
    _add_config_args(config_render, legacy=True)
    validate_config = config_sub.add_parser("validate", help="validate operator configuration")
    _add_config_args(validate_config, legacy=True)

    render = sub.add_parser("render", help="render executable head/worker contracts")
    _add_config_args(render)
    render.add_argument("--output-dir", type=Path, help="write complete contracts to an external directory")
    plan = sub.add_parser("plan", help="render a redacted deterministic deployment plan")
    _add_config_args(plan, legacy=True)

    for command, help_text in (
        ("apply", "create and start a locked deployment"),
        ("start", "start the owned worker/head pair"),
        ("stop", "stop the owned worker/head pair"),
        ("update", "replace the owned pair with a locked deployment"),
        ("rollback", "restore the exact captured image and command lock"),
    ):
        lifecycle = sub.add_parser(command, help=help_text)
        _add_config_args(lifecycle)
        lifecycle.add_argument("--state-file", type=Path, help="external rollback state path")
        lifecycle.add_argument("--confirm", metavar="DEPLOYMENT_ID", help="required for host/container mutation")
        lifecycle.add_argument("--dry-run", action="store_true", help="validate and print plan without mutation")

    verify = sub.add_parser("verify", help="verify exact image, labels, command, and readiness")
    _add_config_args(verify)
    verify.add_argument("--dry-run", action="store_true", help="validate and print plan without remote verification")

    manifest = sub.add_parser("manifest", help="render a redacted external manifest")
    _add_config_args(manifest)
    return parser


def _add_config_args(parser: argparse.ArgumentParser, *, legacy: bool = False) -> None:
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--lock-file", type=Path, help="external complete deployment lock")
    if legacy:
        parser.add_argument("--apply", action="store_true", help=argparse.SUPPRESS)
        parser.add_argument("--confirm", metavar="DEPLOYMENT_ID", help=argparse.SUPPRESS)


def _load(args: argparse.Namespace) -> dict[str, Any]:
    # The legacy config --apply flag remains fail-closed for compatibility.
    try:
        config = load_config(args.env_file, args.profile)
    except ConfigError:
        raise
    if args.lock_file is not None:
        lock_path = args.lock_file.expanduser()
        if not lock_path.is_absolute():
            lock_path = lock_path.resolve()
        config["deployment"]["image_lock_file"] = str(lock_path)
    return config


def _lock(config: dict[str, Any]) -> dict[str, Any] | None:
    mode = str(config["deployment"].get("mode", "generic"))
    if mode == "generic":
        return None
    return load_deployment_lock(Path(str(config["deployment"]["image_lock_file"])), config)


def _state_file(args: argparse.Namespace, config: dict[str, Any]) -> Path:
    value = getattr(args, "state_file", None)
    if value is not None:
        return value.expanduser()
    return Path(str(config["deployment"]["state_root"])) / f"{deployment_id(config)}.rollback.json"


def _render_to_dir(config: dict[str, Any], lock: dict[str, Any] | None, output_dir: Path) -> dict[str, Any]:
    output_dir = output_dir.expanduser().resolve()
    _repo = ROOT.resolve()
    if output_dir == _repo or _repo in output_dir.parents:
        raise ConfigError("render output directory must be outside the repository")
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for role in ("worker", "head"):
        contract_path = output_dir / f"{role}.contract.json"
        env_path = output_dir / f"{role}.env"
        contract_path.write_text(render_contract_json(config, role, lock), encoding="utf-8")
        env = render_contract(config, role, lock)["environment"]
        env_path.write_text("".join(f"{key}={value}\n" for key, value in env.items()), encoding="utf-8")
        written.extend([str(contract_path), str(env_path)])
    (output_dir / "plan.json").write_text(json.dumps(render_plan(config, lock), sort_keys=True, indent=2) + "\n", encoding="utf-8")
    written.append(str(output_dir / "plan.json"))
    return {"output_dir": str(output_dir), "files": written, "deployment_id": deployment_id(config)}


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        config = _load(args)
        command = args.command
        if command == "config":
            reject_mutation(apply=getattr(args, "apply", False), confirm=getattr(args, "confirm", None))
            if args.config_command == "render":
                print(json.dumps(redact_mapping(config), sort_keys=True, indent=2))
            elif args.config_command == "validate":
                print(json.dumps({"profile_id": config["profile"]["profile_id"], "config_sha256": config_sha256(config)}))
            return 0
        lock = _lock(config) if command in {"render", "plan", "apply", "start", "stop", "update", "rollback", "verify"} else None
        if command == "plan":
            reject_mutation(apply=getattr(args, "apply", False), confirm=getattr(args, "confirm", None))
            print(json.dumps(render_plan(config, lock), sort_keys=True, indent=2))
            return 0
        if command == "render":
            if args.output_dir is not None:
                result = _render_to_dir(config, lock, args.output_dir)
            else:
                result = {
                    "deployment_id": deployment_id(config),
                    "mode": config["deployment"]["mode"],
                    "roles": {role: render_contract(config, role, lock) for role in ("worker", "head")},
                }
            print(json.dumps(redact_mapping(result), sort_keys=True, indent=2))
            return 0
        if command == "manifest":
            print(manifest_json(config), end="")
            return 0
        if command == "verify" and args.dry_run:
            print(json.dumps(render_plan(config, lock), sort_keys=True, indent=2))
            return 0
        if command in {"apply", "start", "stop", "update", "rollback"}:
            reject_mutation(apply=not args.dry_run, confirm=args.confirm)
            if not args.dry_run and args.confirm != deployment_id(config):
                raise MutationDisabled("mutation is disabled: --confirm must equal the rendered deployment ID")
            if args.dry_run:
                print(json.dumps(render_plan(config, lock), sort_keys=True, indent=2))
                return 0
        engine = load_engine(config)
        if command == "verify":
            engine.verify()
        elif command == "apply":
            engine.apply(_state_file(args, config))
        elif command == "start":
            engine.start()
        elif command == "stop":
            engine.stop()
        elif command == "update":
            engine.update(_state_file(args, config))
        elif command == "rollback":
            engine.rollback(_state_file(args, config))
        else:
            parser.error("unsupported command")
        print(json.dumps({"deployment_id": deployment_id(config), "command": command, "status": "ok"}))
        return 0
    except MutationDisabled as exc:
        print(f"dgx-deploy: {exc}", file=sys.stderr)
        return EXIT_DISABLED
    except (ConfigError, LockError, RenderError, LifecycleError, OSError) as exc:
        print(f"dgx-deploy: {exc}", file=sys.stderr)
        return EXIT_USAGE if isinstance(exc, (ConfigError, LockError, RenderError)) else EXIT_OPERATION


if __name__ == "__main__":
    raise SystemExit(main())
