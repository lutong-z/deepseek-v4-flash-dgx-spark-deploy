#!/usr/bin/env python3
"""Performance validation boundary; remote benchmarks are not enabled in the scaffold."""

from __future__ import annotations

import argparse


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="describe the disabled boundary")
    parser.parse_args()
    print("performance validation requires an isolated service and external result root")
    return 78


if __name__ == "__main__":
    raise SystemExit(main())
