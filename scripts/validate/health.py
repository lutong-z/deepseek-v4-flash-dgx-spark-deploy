#!/usr/bin/env python3
"""Health validation boundary; remote probes are not enabled in the scaffold."""

from __future__ import annotations

import argparse


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="describe the disabled boundary")
    parser.parse_args()
    print("health validation requires a reviewed external deployment manifest")
    return 78


if __name__ == "__main__":
    raise SystemExit(main())
