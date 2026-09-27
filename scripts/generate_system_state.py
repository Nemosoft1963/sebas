#!/usr/bin/env python3
"""Generate current system state and, optionally, a release manifest."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.system_state import (
    build_release_manifest, build_system_state, write_release_manifest, write_system_state,
)


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", type=Path, default=ROOT / "docs" / "generated")
    parser.add_argument("--memory-path", type=Path)
    parser.add_argument("--tests-result")
    parser.add_argument("--verified-in-production", action="store_true")
    parser.add_argument("--manifest", action="store_true")
    parser.add_argument("--deployed", action="store_true")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    state = build_system_state(
        ROOT, memory_path=args.memory_path, tests_result=args.tests_result,
        verified_in_production=args.verified_in_production,
    )
    write_system_state(args.out_dir, state)
    if args.manifest:
        write_release_manifest(args.out_dir, build_release_manifest(ROOT, deployed=args.deployed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
