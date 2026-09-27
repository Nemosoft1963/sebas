#!/usr/bin/env python3
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
python = root / ".venv" / "bin" / "python"
result = subprocess.run(
    [str(python), "-m", "pytest", "tests/test_experience_memory.py", "-q"],
    cwd=root,
)
sys.exit(result.returncode)
