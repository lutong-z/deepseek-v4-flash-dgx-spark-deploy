"""Stable local-only command line interface."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from .config import ConfigError, DEFAULT_PROFILE, config_sha256, load_config
from .manifest import manifest_json
from .remote import MutationDisabled, reject_mutation
from .redact import redact_mapping
from .render import render_plan

EXIT_USAGE = 64
EXIT_DISABLED = 78


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dgx-deploy",
        description="Render and validate a generic two-node DGX Spark deployment plan.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    config = sub.add_parser("config", help="validate or render operator configuration")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    render = config_sub.add_parser("render", help="render redacted canonical configuration")
    _add_config_args(render)
    validate_config = config_sub.add_parser("validate", help="validate operator configuration")
    _add_config_args(validate_config)
    plan = sub.add_parser("plan", help="render a redacted dry-run plan")
    _add_config_args(plan)
    manifest = sub.add_parser("manifest", help="render a redacted external manifest")
    _add_config_args(manifest)
    return parser


def _add_config_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--apply", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--confirm", metavar="DEPLOYMENT_ID", help=argparse.SUPPRESS)


def _load(args: argparse.Namespace) -> dict[str, object]:
    reject_mutation(apply=args.apply, confirm=args.confirm)
    return load_config(args.env_file, args.profile)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        config = _load(args)
    except MutationDisabled as exc:
        print(f"dgx-deploy: {exc}", file=sys.stderr)
        return EXIT_DISABLED
    except ConfigError as exc:
        print(f"dgx-deploy: {exc}", file=sys.stderr)
        return EXIT_USAGE
    if args.command == "config" and args.config_command == "render":
        print(json.dumps(redact_mapping(config), sort_keys=True, indent=2))
    elif args.command == "config" and args.config_command == "validate":
        print(json.dumps({"profile_id": config["profile"]["profile_id"], "config_sha256": config_sha256(config)}))
    elif args.command == "plan":
        print(json.dumps(render_plan(config), sort_keys=True, indent=2))
    elif args.command == "manifest":
        print(manifest_json(config), end="")
    else:
        parser.error("unsupported command")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
