from __future__ import annotations

import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from app.context_files import extract_context_file

MAX_OPERATION_COUNT = 20
MAX_TEXT_WRITE_BYTES = 1024 * 1024
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
MAX_TREE_ENTRIES = 500
MAX_SNAPSHOT_CHARS = 40_000


def normalize_workspace_path(value: str, *, allow_empty: bool = False) -> str:
    raw = value.strip().replace("\\", "/")
    if not raw and allow_empty:
        return ""
    if not raw or raw.startswith("/") or any(ord(char) < 32 for char in raw):
        raise ValueError("Workspace path must be relative to /workspace")
    parts = [part for part in PurePosixPath(raw).parts if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts) or ":" in parts[0]:
        raise ValueError("Workspace path must stay inside /workspace")
    normalized = "/".join(parts)
    if len(normalized) > 500:
        raise ValueError("Workspace path must be 500 characters or fewer")
    return normalized


class WorkspaceSandbox:
    """Scoped operations; user actions are deletion-free and rejected AI writes are recoverable."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()

    def project_path(self, configured: str, project_id: str) -> tuple[str, Path]:
        relative = normalize_workspace_path(configured or f"projects/{project_id}")
        return relative, self._inside(self.root, relative)

    def _inside(self, base: Path, relative: str, *, must_exist: bool = False) -> Path:
        normalized = normalize_workspace_path(relative, allow_empty=True)
        resolved_base = base.resolve(strict=False)
        candidate = (resolved_base / normalized).resolve(strict=False)
        if candidate != resolved_base and resolved_base not in candidate.parents:
            raise ValueError("Workspace path escaped the allowed folder")
        if must_exist and not candidate.exists():
            raise FileNotFoundError(normalized)
        return candidate

    def resolve_file(self, configured: str, project_id: str, relative: str,
                     *, must_exist: bool = False) -> tuple[str, Path, Path]:
        project_relative, scope = self.project_path(configured, project_id)
        relative = self._scope_relative(project_relative, relative)
        return project_relative, scope, self._inside(scope, relative, must_exist=must_exist)

    @staticmethod
    def _scope_relative(project_relative: str, value: str) -> str:
        """Accept /workspace paths only when they name the active project scope."""
        raw = str(value).strip().replace("\\", "/")
        if raw.startswith("workspace:"):
            raw = raw.removeprefix("workspace:").lstrip("/")
        prefix = f"/workspace/{project_relative}/"
        if raw in {"/workspace", "workspace:"}:
            return ""
        if raw == f"/workspace/{project_relative}":
            return ""
        if raw.startswith(prefix):
            return raw[len(prefix):]
        if raw.startswith("/workspace/"):
            return raw.removeprefix("/workspace/")
        if raw.startswith("/"):
            return raw.lstrip("/")
        return raw

    def list_entries(self, configured: str, project_id: str) -> dict[str, Any]:
        project_relative, scope = self.project_path(configured, project_id)
        if not scope.exists():
            return {"workspace_path": project_relative, "exists": False, "entries": []}
        entries: list[dict[str, Any]] = []
        for path in sorted(scope.rglob("*"), key=lambda item: item.as_posix().lower()):
            if ".local_cowork_backups" in path.parts:
                continue
            try:
                resolved = path.resolve(strict=True)
            except (FileNotFoundError, OSError):
                continue
            if resolved != scope and scope not in resolved.parents:
                continue
            stat = path.stat()
            entries.append({
                "path": path.relative_to(scope).as_posix(), "name": path.name,
                "kind": "directory" if path.is_dir() else "file",
                "size_bytes": 0 if path.is_dir() else stat.st_size,
                "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            })
            if len(entries) >= MAX_TREE_ENTRIES:
                break
        return {"workspace_path": project_relative, "exists": True, "entries": entries,
                "truncated": len(entries) >= MAX_TREE_ENTRIES}

    def snapshot(self, configured: str, project_id: str, *,
                 include_contents: bool = True) -> str:
        listing = self.list_entries(configured, project_id)
        lines = [f"Workspace: /workspace/{listing['workspace_path']}",
                 "この範囲だけを操作できます。削除と任意コマンド実行は禁止です。"]
        if not listing["exists"]:
            lines.append("（未作成。mkdirまたはwrite_textで作成できます）")
            return "\n".join(lines)
        scope = self.project_path(configured, project_id)[1]
        files = [item for item in listing["entries"] if item["kind"] == "file"]
        lines.extend(f"- {item['path']} ({item['size_bytes']} bytes)" for item in files)
        if not include_contents:
            return "\n".join(lines)[:MAX_SNAPSHOT_CHARS]
        used = len("\n".join(lines))
        for item in files[:40]:
            if item["size_bytes"] > 5 * 1024 * 1024 or used >= MAX_SNAPSHOT_CHARS:
                continue
            try:
                path = self._inside(scope, item["path"], must_exist=True)
                extracted = extract_context_file(item["path"], path.read_bytes())
            except (OSError, ValueError):
                continue
            if extracted.content:
                excerpt = extracted.content[:MAX_SNAPSHOT_CHARS - used]
                lines.append(f"\n### {item['path']}\n{excerpt}")
                used += len(excerpt)
        return "\n".join(lines)[:MAX_SNAPSHOT_CHARS]

    def _backup(self, scope: Path, target: Path) -> str | None:
        if not target.exists() or not target.is_file():
            return None
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = scope / ".local_cowork_backups" / stamp / target.relative_to(scope)
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, backup)
        return backup.relative_to(scope).as_posix()

    @staticmethod
    def _atomic_write(target: Path, data: bytes) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".local-cowork-", dir=target.parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def apply_operations(self, configured: str, project_id: str,
                         operations: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if len(operations) > MAX_OPERATION_COUNT:
            raise ValueError(f"One task may perform at most {MAX_OPERATION_COUNT} operations")
        _, scope = self.project_path(configured, project_id)
        scope.mkdir(parents=True, exist_ok=True)
        audit: list[dict[str, Any]] = []
        for operation in operations:
            if not isinstance(operation, dict):
                raise ValueError("Each file operation must be an object")
            action = str(operation.get("action", "")).strip().lower()
            project_relative = self.project_path(configured, project_id)[0]
            scoped_path = self._scope_relative(
                project_relative, str(operation.get("path", "")),
            )
            if not scoped_path and action == "mkdir":
                audit.append({"action": action, "path": "", "status": "exists"})
                continue
            relative = normalize_workspace_path(scoped_path)
            target = self._inside(scope, relative)
            if action == "mkdir":
                target.mkdir(parents=True, exist_ok=True)
                audit.append({"action": action, "path": relative, "status": "created"})
            elif action in {"write_text", "append_text"}:
                content = str(operation.get("content", ""))
                data = content.encode("utf-8")
                if len(data) > MAX_TEXT_WRITE_BYTES:
                    raise ValueError("One text write must be 1 MB or smaller")
                backup = self._backup(scope, target)
                if action == "append_text" and target.exists():
                    data = (target.read_text(encoding="utf-8") + content).encode("utf-8")
                self._atomic_write(target, data)
                audit.append({"action": action, "path": relative, "status": "written",
                              "bytes": len(data), "backup": backup})
            elif action in {"copy", "move"}:
                source_relative = normalize_workspace_path(self._scope_relative(
                    project_relative, str(operation.get("source", "")),
                ))
                source = self._inside(scope, source_relative, must_exist=True)
                if target.exists():
                    raise FileExistsError(f"Destination already exists: {relative}")
                target.parent.mkdir(parents=True, exist_ok=True)
                if action == "copy":
                    shutil.copy2(source, target) if source.is_file() else shutil.copytree(source, target)
                else:
                    shutil.move(str(source), str(target))
                audit.append({"action": action, "source": source_relative,
                              "path": relative, "status": "completed"})
            else:
                raise ValueError("Allowed operations: mkdir, write_text, append_text, copy, move")
        return audit

    def reject_operations(
        self, configured: str, project_id: str, audit: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Quarantine unverified AI output and restore the pre-task files."""
        _, scope = self.project_path(configured, project_id)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        results: list[dict[str, Any]] = []
        for index, item in enumerate(reversed(audit), 1):
            if not isinstance(item, dict):
                continue
            action = str(item.get("action", "")).strip().lower()
            if action not in {"write_text", "append_text", "copy", "move"}:
                continue
            relative = normalize_workspace_path(str(item.get("path", "")))
            target = self._inside(scope, relative)
            rejected_relative = normalize_workspace_path(
                f".local_cowork_rejected/{stamp}/{index:03d}/{relative}"
            )
            rejected = self._inside(scope, rejected_relative)
            if target.exists() and target.is_file():
                rejected.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, rejected)
            backup_relative = str(item.get("backup") or "").strip()
            status = "quarantined"
            if backup_relative:
                backup = self._inside(scope, backup_relative, must_exist=True)
                self._atomic_write(target, backup.read_bytes())
                status = "restored"
            elif action == "move":
                source_relative = normalize_workspace_path(
                    str(item.get("source", ""))
                )
                source = self._inside(scope, source_relative)
                if target.exists() and not source.exists():
                    source.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(target), str(source))
                    status = "move_restored"
            elif target.exists() and target.is_file():
                target.unlink()
            results.append({
                "action": action,
                "path": relative,
                "status": status,
                "rejected_path": rejected_relative if rejected.exists() else "",
                "restored_backup": backup_relative,
            })
        return results

    def write_bytes(self, configured: str, project_id: str,
                    relative: str, data: bytes) -> dict[str, Any]:
        """Atomically write a generated binary artifact inside the project scope."""
        if len(data) > MAX_UPLOAD_BYTES:
            raise ValueError("One generated workspace file must be 50 MB or smaller")
        project_relative, scope, target = self.resolve_file(configured, project_id, relative)
        scope.mkdir(parents=True, exist_ok=True)
        backup = self._backup(scope, target)
        self._atomic_write(target, data)
        scoped = self._scope_relative(project_relative, relative)
        return {
            "action": "write_bytes", "path": normalize_workspace_path(scoped),
            "status": "written", "bytes": len(data), "backup": backup,
        }
    def save_upload(self, configured: str, project_id: str,
                    relative: str, data: bytes) -> dict[str, Any]:
        if len(data) > MAX_UPLOAD_BYTES:
            raise ValueError("One workspace upload must be 50 MB or smaller")
        _, scope, target = self.resolve_file(configured, project_id, relative)
        scope.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise FileExistsError("A workspace file with the same path already exists")
        self._atomic_write(target, data)
        return {"path": normalize_workspace_path(relative), "size_bytes": len(data)}

    def store_original(self, configured: str, project_id: str,
                       relative: str, data: bytes) -> dict[str, Any]:
        """Persist a registered source under the project-local originals folder."""
        source_relative = normalize_workspace_path(relative)
        target_relative = f"originals/{source_relative}"
        _, scope, target = self.resolve_file(configured, project_id, target_relative)
        scope.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.is_file() and target.read_bytes() == data:
            return {"path": target_relative, "size_bytes": len(data), "status": "unchanged"}
        backup = self._backup(scope, target)
        self._atomic_write(target, data)
        return {"path": target_relative, "size_bytes": len(data),
                "status": "stored", "backup": backup}
