#!/usr/bin/env python3
"""Read-only readiness check for one Local Supporter project."""
from __future__ import annotations

import argparse
import json
import urllib.request


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("project_id")
    parser.add_argument("--base-url", default="http://127.0.0.1:8099")
    args = parser.parse_args()
    url = f"{args.base_url.rstrip('/')}/api/projects/{args.project_id}/premarketing"
    with urllib.request.urlopen(url, timeout=10) as response:
        payload = json.load(response)
    print(json.dumps(payload.get("google", {}), ensure_ascii=False, indent=2))
    return 0 if payload.get("google", {}).get("configured") else 2


if __name__ == "__main__":
    raise SystemExit(main())
