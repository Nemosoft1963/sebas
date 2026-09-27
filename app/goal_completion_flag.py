"""Feature flag for goal-completion v1. Default off so existing tests stay unchanged."""
from __future__ import annotations

import json
from pathlib import Path


CONFIG_NAME = "goal_completion.json"


def config_path(memory_path) -> Path:
    return Path(memory_path).parent / CONFIG_NAME


def enabled(memory_path, project_id: str | None = None) -> bool:
    path = config_path(memory_path)
    if not path.exists():
        return False
    try:
        cfg = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return False
    if project_id:
        per = (cfg.get("projects") or {}).get(project_id)
        if per is False:
            return False
        if per is True:
            return True
    return cfg.get("enabled") is True


def enable(memory_path, project_id: str | None = None) -> None:
    path = config_path(memory_path)
    cfg = {}
    if path.exists():
        try:
            cfg = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            cfg = {}
    if project_id:
        projects = dict(cfg.get("projects") or {})
        projects[project_id] = True
        cfg["projects"] = projects
    else:
        cfg["enabled"] = True
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
