#!/usr/bin/env python3
"""Register converted lesson files via app.experience_cli using the project .venv."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENV_PYTHON = ROOT / ".venv" / "bin" / "python"
LESSONS_DIR = ROOT / "experience_lessons"
MEMORY_ROOT = ROOT / "experience_memory"
CONFIG = ROOT / "experience_memory.json"
PROJECT = "default"
PROOF = "成功事例収集エージェントでの人手レビュー済み"


def run_cli(args: list[str]) -> dict:
    command = [
        str(VENV_PYTHON),
        "-m",
        "app.experience_cli",
        "--root",
        str(MEMORY_ROOT),
        "--project",
        PROJECT,
        *args,
    ]
    completed = subprocess.run(
        command,
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "cli failed (%s): %s\n%s"
            % (completed.returncode, " ".join(command), completed.stderr or completed.stdout)
        )
    return json.loads(completed.stdout)


def main() -> int:
    if not VENV_PYTHON.exists():
        raise SystemExit("missing %s" % VENV_PYTHON)
    files = sorted(LESSONS_DIR.glob("lesson-*.json"))
    if len(files) != 30:
        raise SystemExit("expected 30 lesson files, found %d" % len(files))
    MEMORY_ROOT.mkdir(parents=True, exist_ok=True)
    added = []
    for path in files:
        result = run_cli(["add", str(path)])
        record_id = result["id"]
        run_cli([
            "review",
            record_id,
            "--status",
            "verified",
            "--reviewer",
            "operator",
            "--proof",
            PROOF,
        ])
        added.append({"file": path.name, "id": record_id})
        print("registered %s -> %s" % (path.name, record_id), flush=True)
    indexed = run_cli(["index", "--config", str(CONFIG)])
    stats = run_cli(["stats"])
    print(json.dumps({"added": added, "indexed": indexed, "stats": stats}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
