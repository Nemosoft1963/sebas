"""Read-only, deterministic system-state and release-manifest generation."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

GENERATOR_VERSION = "1.0.0"
GOAL_MODULES = (
    "completion_gate", "evidence_graph", "goal_contract", "goal_decompose",
    "goal_state_machine", "next_action_controller", "question_view",
    "restart_convergence", "triz_exclusion",
)
GOAL_ROUTE_MARKERS = (
    "goal-contract", "completion-gate", "next-action", "goal-state",
    "goal-metrics", "evidence", "questions", "facts", "plan/coverage",
    "workflow-readiness",
)
KNOWN_UNVERIFIED = (
    "実ブラウザでの質問UI画面確認",
    "実Ollamaでの汎用案件分解品質",
    "本番案件(plan v25)での12条件の実データ受入",
    "本番反映・flag ON",
)
SAFETY_INVARIANTS = (
    "読取不能・不明・未承認を0円・成功・承認済みにしない",
    "前月の請求・支払を当月に配賦しない",
    "独立した原本合計が無ければ確定しない",
    "OCR未承認の結果を下流・RAGに流さない",
    "AIが人間の承認を書かない",
    "PASSは検査IDと証拠を伴うときだけ",
    "docker system prune -a / docker volume prune / docker compose down -v を使わない",
)
EXCLUDED_PARTS = {"__pycache__", "data", "logs", "inputs", "backups", ".git"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_git(root: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args], cwd=root, capture_output=True, text=True,
            check=True, timeout=10,
        )
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def _git_state(root: Path) -> dict[str, Any]:
    head = _run_git(root, "rev-parse", "HEAD")
    status = _run_git(root, "status", "--porcelain")
    return {"head": head, "dirty": None if status is None else bool(status)}


def _module_status(root: Path) -> dict[str, bool]:
    result = {}
    for name in GOAL_MODULES:
        path = root / "app" / f"{name}.py"
        try:
            result[name] = path.is_file() and importlib.util.spec_from_file_location(
                f"_system_state_{name}", path
            ) is not None
        except (ImportError, OSError, ValueError):
            result[name] = False
    return dict(sorted(result.items()))


def _api_state(app=None) -> dict[str, Any]:
    if app is None:
        from app.web import app as web_app
        app = web_app
    all_routes = []
    goal_routes = []
    for route in app.routes:
        path = getattr(route, "path", None)
        if not path:
            continue
        methods = sorted(getattr(route, "methods", None) or [])
        for method in methods:
            item = {"method": method, "path": path}
            all_routes.append(item)
            if any(marker in path for marker in GOAL_ROUTE_MARKERS):
                goal_routes.append(item)
    key = lambda item: (item["path"], item["method"])
    return {"route_count": len(all_routes), "goal_completion_routes": sorted(goal_routes, key=key)}


def _feature_flags(memory_path: Path | None) -> dict[str, Any] | None:
    if memory_path is None:
        return None
    from app.goal_completion_flag import config_path, enabled
    path = config_path(memory_path)
    projects = []
    try:
        if path.exists():
            cfg = json.loads(path.read_text(encoding="utf-8-sig"))
            projects = sorted(str(key) for key in (cfg.get("projects") or {}))
    except (OSError, ValueError, TypeError):
        projects = []
    return {
        "default": enabled(memory_path),
        "projects": {project: enabled(memory_path, project) for project in projects},
    }


def _goal_db(memory_path: Path | None) -> dict[str, Any] | None:
    if memory_path is None:
        return None
    path = memory_path.parent / "goal_completion.sqlite3"
    if not path.is_file():
        return None
    try:
        db = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
        try:
            tables = sorted(row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ))
            counts = {}
            for table in ("goal_facts", "human_acceptances"):
                counts[table] = db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] if table in tables else None
            return {"tables": tables, "counts": dict(sorted(counts.items()))}
        finally:
            db.close()
    except (OSError, sqlite3.Error):
        return None


def _docs(root: Path) -> list[dict[str, Any]]:
    result = []
    for path in sorted((root / "docs").glob("CHANGELOG_*.md"), key=lambda p: p.name):
        try:
            modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
        except OSError:
            modified = None
        result.append({"name": path.name, "modified_at": modified})
    return result


def build_system_state(
    root: Path, memory_path: Path | None = None, tests_result: str | None = None,
    verified_in_production: bool = False, app=None, generated_at: str | None = None,
) -> dict[str, Any]:
    root = Path(root).resolve()
    try:
        from app.vehicle_auto import REVISION
        vehicle_revision = REVISION
    except (ImportError, AttributeError):
        vehicle_revision = None
    return {
        "generated_at": generated_at or _now(),
        "generator_version": GENERATOR_VERSION,
        "revisions": {"goal_completion_modules": _module_status(root), "vehicle_extraction": vehicle_revision},
        "git": _git_state(root),
        "api": _api_state(app),
        "feature_flags": _feature_flags(Path(memory_path) if memory_path is not None else None),
        "goal_completion_db": _goal_db(Path(memory_path) if memory_path is not None else None),
        "tests": tests_result if tests_result is not None else "unknown",
        "verification": {
            "known_unverified": list(KNOWN_UNVERIFIED),
            "verified_in_production": bool(verified_in_production),
        },
        "safety_invariants": list(SAFETY_INVARIANTS),
        "docs": _docs(root),
        "known_stale": ["MANIFEST.json は 2026-08-29 のまま更新されていない"],
    }


def render_state_markdown(state: dict[str, Any]) -> str:
    lines = [
        "# CURRENT SYSTEM STATE", "",
        "> この文書は CURRENT_SYSTEM_STATE.json から機械生成されています。承認・検証・本番反映の根拠ではありません。", "",
        f"- generated_at: `{state['generated_at']}`",
        f"- generator_version: `{state['generator_version']}`",
        f"- tests: `{state['tests']}`",
        f"- verified_in_production: `{str(state['verification']['verified_in_production']).lower()}`", "",
        "## JSON", "", "```json",
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
        "```", "",
    ]
    return "\n".join(lines)


def write_system_state(out_dir: Path, state: dict[str, Any]) -> tuple[Path, Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "CURRENT_SYSTEM_STATE.json"
    md_path = out_dir / "CURRENT_SYSTEM_STATE.md"
    json_path.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md_path.write_text(render_state_markdown(state), encoding="utf-8")
    return json_path, md_path


def _excluded(relative: Path) -> bool:
    return any(part in EXCLUDED_PARTS or part.endswith(".egg-info") for part in relative.parts) or relative.suffix == ".pyc"


def _manifest_files(root: Path) -> list[dict[str, Any]]:
    files = []
    for directory in ("app", "scripts", "tests", "docs"):
        base = root / directory
        if not base.exists():
            continue
        for path in base.rglob("*"):
            relative = path.relative_to(root)
            if not path.is_file() or _excluded(relative) or relative.parts[:2] == ("docs", "generated"):
                continue
            data = path.read_bytes()
            files.append({"path": relative.as_posix(), "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    return sorted(files, key=lambda item: item["path"])


def _aggregate(files: list[dict[str, Any]]) -> str:
    payload = "".join(f"{item['path']}\0{item['sha256']}\n" for item in files).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def build_release_manifest(root: Path, deployed: bool = False, generated_at: str | None = None) -> dict[str, Any]:
    root = Path(root).resolve()
    files = _manifest_files(root)
    return {
        "generated_at": generated_at or _now(),
        "git": {"head": _run_git(root, "rev-parse", "HEAD")},
        "files": files,
        "aggregate_sha256": _aggregate(files),
        "counts": {"files": len(files)},
        "deployed": bool(deployed),
    }


def write_release_manifest(out_dir: Path, manifest: dict[str, Any]) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "RELEASE_MANIFEST.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def verify_release_manifest(manifest_path: Path, root: Path) -> dict[str, Any]:
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    expected = {item["path"]: item for item in manifest.get("files", [])}
    current = {item["path"]: item for item in _manifest_files(Path(root).resolve())}
    mismatched = sorted(path for path in expected.keys() & current.keys()
                        if expected[path]["sha256"] != current[path]["sha256"] or expected[path]["size"] != current[path]["size"])
    missing = sorted(expected.keys() - current.keys())
    extra = sorted(current.keys() - expected.keys())
    return {"ok": not (mismatched or missing or extra), "mismatched": mismatched, "missing": missing, "extra": extra}
