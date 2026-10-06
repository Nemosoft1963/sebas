"""L1: 退避(バックアップ)・検証・復元.

コピーのみ。元データは変更しない。削除は行わない。
manifest には件数・ハッシュ・パス・時刻・案件ID・形式版のみを記録し、
行内容や秘密値は書かない (内容は退避ファイル側)。
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.project_lifecycle_registry import (
    REGISTRY,
    RegistryEntry,
    collect_external_items,
    count_all,
    count_entry,
    data_dir_of,
    has_running_jobs,
    resolve_db_path,
    resolve_workspace_dir,
)

LOGGER = logging.getLogger(__name__)

BACKUP_FORMAT = "l1-backup/1"
ENCRYPTION_NOTE = "none (access-restriction only: dir 0700, files 0600)"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _encode_value(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"$bytes": base64.b64encode(bytes(value)).decode("ascii")}
    return value


def _decode_value(value: Any) -> Any:
    if isinstance(value, dict) and set(value.keys()) == {"$bytes"}:
        return base64.b64decode(value["$bytes"])
    return value


def _sanitize_id(value: str) -> str:
    return "".join(ch if (ch.isalnum() or ch in ("-", "_")) else "_" for ch in str(value))[:64] or "pj"


def _chmod_restricted(path: Path, is_dir: bool) -> None:
    try:
        os.chmod(path, 0o700 if is_dir else 0o600)
    except OSError:
        pass


def _open_ro(db_path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def _table_exists(db: sqlite3.Connection, table: str) -> bool:
    try:
        return db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None
    except sqlite3.Error:
        return False


def _table_sql(db: sqlite3.Connection, table: str) -> str:
    try:
        row = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        return str(row[0]) if row and row[0] else ""
    except sqlite3.Error:
        return ""


def _extract_rows(memory_path: str | Path, entry: RegistryEntry,
                  project_id: str) -> tuple[bool, str, list[dict[str, Any]]]:
    """該当行を抽出する。(table_present, schema_sql, rows)。副作用なし。"""
    pid = str(project_id)
    db_path = resolve_db_path(memory_path, entry.db_label)
    if not entry.table or db_path is None or not db_path.exists():
        return False, "", []
    try:
        db = _open_ro(db_path)
    except sqlite3.Error:
        return False, "", []
    try:
        if not _table_exists(db, entry.table):
            return False, "", []
        schema = _table_sql(db, entry.table)
        cols = {r[1] for r in db.execute(f"PRAGMA table_info({entry.table})").fetchall()}
        rows: list[dict[str, Any]] = []
        if entry.project_column and entry.project_column in cols:
            cur = db.execute(
                f"SELECT * FROM {entry.table} WHERE {entry.project_column}=?", (pid,))
            rows = [{k: _encode_value(v) for k, v in dict(r).items()} for r in cur.fetchall()]
        elif entry.table in ("ocr_pages", "ocr_blocks", "ocr_fields",
                             "ocr_validations", "ocr_reviews"):
            if _table_exists(db, "ocr_runs"):
                runs = [r[0] for r in db.execute(
                    "SELECT run_id FROM ocr_runs WHERE project_id=?", (pid,)).fetchall()]
                if runs and "run_id" in cols:
                    marks = ",".join("?" for _ in runs)
                    cur = db.execute(
                        f"SELECT * FROM {entry.table} WHERE run_id IN ({marks})", runs)
                    rows = [{k: _encode_value(v) for k, v in dict(r).items()}
                            for r in cur.fetchall()]
        elif entry.table in ("detailed_steps", "detailed_reviews"):
            if _table_exists(db, "detailed_plans"):
                plans = [r[0] for r in db.execute(
                    "SELECT id FROM detailed_plans WHERE project_id=?", (pid,)).fetchall()]
                if plans:
                    marks = ",".join("?" for _ in plans)
                    cur = db.execute(
                        f"SELECT * FROM {entry.table} WHERE plan_id IN ({marks})", plans)
                    rows = [{k: _encode_value(v) for k, v in dict(r).items()}
                            for r in cur.fetchall()]
        return True, schema, rows
    except sqlite3.Error:
        return False, "", []
    finally:
        try:
            db.close()
        except sqlite3.Error:
            pass


def _project_workspace_rel(memory_path: str | Path, project_id: str) -> str:
    pid = str(project_id)
    configured = ""
    mem = Path(memory_path)
    if mem.exists():
        try:
            db = _open_ro(mem)
            try:
                if _table_exists(db, "projects"):
                    r = db.execute("SELECT workspace_path FROM projects WHERE id=?",
                                   (pid,)).fetchone()
                    if r:
                        try:
                            configured = str(r["workspace_path"] or "")
                        except (KeyError, IndexError):
                            configured = ""
            finally:
                db.close()
        except sqlite3.Error:
            pass
    rel = (configured or "").strip().replace("\\", "/").strip("/")
    if not rel:
        rel = f"projects/{pid}"
    return rel


def _project_name(memory_path: str | Path, project_id: str) -> str:
    mem = Path(memory_path)
    if not mem.exists():
        return ""
    try:
        db = _open_ro(mem)
        try:
            if _table_exists(db, "projects"):
                r = db.execute("SELECT name FROM projects WHERE id=?",
                               (str(project_id),)).fetchone()
                return str(r["name"]) if r else ""
        finally:
            db.close()
    except sqlite3.Error:
        return ""
    return ""


def _project_exists(memory_path: str | Path, project_id: str) -> bool:
    mem = Path(memory_path)
    if not mem.exists():
        return False
    try:
        db = _open_ro(mem)
        try:
            if not _table_exists(db, "projects"):
                return False
            return db.execute("SELECT 1 FROM projects WHERE id=?",
                              (str(project_id),)).fetchone() is not None
        finally:
            db.close()
    except sqlite3.Error:
        return False


# ----------------------------------------------------------------------------
# 退避
# ----------------------------------------------------------------------------

def create_backup(memory_path: str | Path, project_id: str, backup_root: str | Path,
                  *, workspace_root: str | Path | None = None,
                  actor: str = "") -> dict[str, Any]:
    """PJ単位のレコードと専用Workspaceを退避する。コピーのみ。"""
    pid = str(project_id)
    actor_name = str(actor or "").strip()
    if not actor_name:
        return {"ok": False, "reason": "actor is required"}
    if not pid:
        return {"ok": False, "reason": "project_id is required"}
    mem = Path(memory_path)
    root = Path(backup_root)
    if not _project_exists(mem, pid):
        return {"ok": False, "reason": f"project not found: {pid}"}
    try:
        root.mkdir(parents=True, exist_ok=True)
        _chmod_restricted(root, True)
    except OSError as exc:
        return {"ok": False, "reason": f"backup root unavailable: {exc}"}
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_id = f"backup_{_sanitize_id(pid)}_{stamp}_{uuid.uuid4().hex[:8]}"
    tmp_dir = root / f"tmp_{uuid.uuid4().hex}"
    dest = root / backup_id
    try:
        tmp_dir.mkdir(parents=True, exist_ok=False)
        _chmod_restricted(tmp_dir, True)
        counts = count_all(mem, pid, workspace_root)
        entries_manifest: list[dict[str, Any]] = []
        files_manifest: list[dict[str, Any]] = []
        total_bytes = 0
        for entry in REGISTRY:
            if entry.kind in ("directory", "other"):
                continue
            table_present, schema, rows = _extract_rows(mem, entry, pid)
            rel = Path("tables") / entry.db_label / f"{entry.table or entry.name}.json"
            target = tmp_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            payload = {"project_id": pid, "table": entry.table,
                       "db_label": entry.db_label, "schema": schema, "rows": rows,
                       "table_present": bool(table_present)}
            data = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
            target.write_bytes(data)
            _chmod_restricted(target, False)
            digest = _sha256_bytes(data)
            total_bytes += len(data)
            entries_manifest.append({
                "name": entry.name, "kind": entry.kind, "db": entry.db_label,
                "table": entry.table, "count": len(rows),
                "table_present": bool(table_present),
                # 存在した場合のスキーマ。存在しなかった場合は空。ハッシュ検証対象。
                "schema": schema if table_present else "",
                "file": rel.as_posix(), "sha256": digest, "bytes": len(data),
            })
            files_manifest.append({"path": rel.as_posix(), "sha256": digest, "bytes": len(data)})
        # Workspace コピー。
        ws_rel = _project_workspace_rel(mem, pid)
        ws_dir = resolve_workspace_dir(mem, pid, workspace_root,
                                       "" if ws_rel == f"projects/{pid}" else ws_rel)
        ws_files = 0
        ws_bytes = 0
        if ws_dir is not None and ws_dir.exists():
            for src in sorted(p for p in ws_dir.rglob("*") if p.is_file()):
                try:
                    rel_in_ws = src.relative_to(ws_dir).as_posix()
                except ValueError:
                    continue
                dst = tmp_dir / "workspace" / ws_rel / rel_in_ws
                dst.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                try:
                    with open(src, "rb") as fh_in, open(dst, "wb") as fh_out:
                        for chunk in iter(lambda: fh_in.read(1024 * 1024), b""):
                            digest.update(chunk)
                            fh_out.write(chunk)
                except OSError as exc:
                    raise OSError(f"workspace copy failed: {rel_in_ws}: {exc}") from exc
                _chmod_restricted(dst, False)
                try:
                    size = dst.stat().st_size
                except OSError:
                    size = 0
                hex_digest = digest.hexdigest()
                ws_files += 1
                ws_bytes += size
                total_bytes += size
                files_manifest.append({"path": f"workspace/{ws_rel}/{rel_in_ws}",
                                       "sha256": hex_digest, "bytes": size})
        jobs = has_running_jobs(mem, pid, None)
        externals = collect_external_items(mem, pid)
        manifest = {
            "format": BACKUP_FORMAT,
            "registry_format": "l1-registry/1",
            "project_id": pid,
            "project_name": _project_name(mem, pid),
            "actor": actor_name,
            "created_at": _utcnow(),
            "encryption": ENCRYPTION_NOTE,
            "entries": entries_manifest,
            "workspace": {"rel": ws_rel, "files": ws_files, "bytes": ws_bytes,
                          "items": [item for item in files_manifest
                                    if item["path"].startswith("workspace/")]},
            "total_rows": sum(e["count"] for e in entries_manifest),
            "total_bytes": total_bytes,
            "has_running_jobs": bool(jobs["has_running"]),
            "running_job_count": len(jobs["running"]),
            "external_items_count": len(externals),
            "note": ("counts and hashes only. row contents live in backup files. "
                     "shared global config (experience_memory.json) is not copied."),
        }
        manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2,
                                     sort_keys=True) + "\n").encode("utf-8")
        (tmp_dir / "manifest.json").write_bytes(manifest_bytes)
        _chmod_restricted(tmp_dir / "manifest.json", False)
        (tmp_dir / "manifest.sha256").write_text(_sha256_bytes(manifest_bytes) + "\n",
                                                 encoding="utf-8")
        _chmod_restricted(tmp_dir / "manifest.sha256", False)
        # 仕上げ前に自己検証し、失敗時は作りかけを残さない。
        check = verify_backup(tmp_dir / "manifest.json")
        if not check.get("ok"):
            raise OSError(f"self verification failed: {check.get('failures')}")
        tmp_dir.rename(dest)
        _chmod_restricted(dest, True)
        return {
            "ok": True, "backup_id": backup_id,
            "backup_dir": str(dest), "manifest_path": str(dest / "manifest.json"),
            "manifest_sha256": _sha256_bytes(manifest_bytes),
            "project_id": pid, "actor": actor_name,
            "created_at": manifest["created_at"],
            "total_rows": manifest["total_rows"], "total_bytes": total_bytes,
            "entry_count": len(entries_manifest), "workspace_files": ws_files,
            "verified": True,
        }
    except Exception as exc:
        LOGGER.warning("backup failed project=%s: %s", pid, exc)
        try:
            if tmp_dir.exists():
                shutil.rmtree(tmp_dir, ignore_errors=False)
        except OSError:
            try:
                (tmp_dir / "INCOMPLETE.marker").write_text(
                    f"incomplete backup: {exc}\n", encoding="utf-8")
            except OSError:
                pass
            return {"ok": False, "reason": str(exc)[:500],
                    "incomplete_left": True, "path": str(tmp_dir)}
        return {"ok": False, "reason": str(exc)[:500]}
    finally:
        try:
            if tmp_dir.exists() and not dest.exists():
                shutil.rmtree(tmp_dir, ignore_errors=True)
        except OSError:
            pass


# ----------------------------------------------------------------------------
# 検証
# ----------------------------------------------------------------------------

def _expected_files(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    for e in manifest.get("entries", []):
        files.append({"path": e["file"], "sha256": e["sha256"], "bytes": e.get("bytes")})
    base = manifest.get("workspace", {}).get("rel", "")
    # workspace ファイルは files 一覧に含まれる (entries には含めない)。
    return files


def verify_backup(manifest_path: str | Path) -> dict[str, Any]:
    """manifest と各ファイルの SHA-256 を再計算照合する。読み取り専用。"""
    mpath = Path(manifest_path)
    backup_dir = mpath.parent
    failures: list[str] = []
    checked = 0
    if mpath.name != "manifest.json" or not mpath.is_file():
        return {"ok": False, "checked": 0, "failures": ["manifest.json not found"]}
    try:
        raw = mpath.read_bytes()
    except OSError as exc:
        return {"ok": False, "checked": 0, "failures": [f"manifest unreadable: {exc}"]}
    hash_path = backup_dir / "manifest.sha256"
    try:
        expected = hash_path.read_text(encoding="utf-8").split()[0]
    except (OSError, IndexError) as exc:
        return {"ok": False, "checked": 0,
                "failures": [f"manifest hash file missing/unreadable: {exc}"]}
    if _sha256_bytes(raw) != expected.strip():
        return {"ok": False, "checked": 0,
                "failures": ["manifest hash mismatch (tampered or corrupted)"]}
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        return {"ok": False, "checked": 0, "failures": [f"manifest invalid: {exc}"]}
    if manifest.get("format") != BACKUP_FORMAT:
        failures.append(f"unsupported format: {manifest.get('format')}")
    listed = {e["file"] for e in manifest.get("entries", [])}
    # Manifest includes each workspace file hash and size.
    actual: set[str] = set()
    try:
        for p in backup_dir.rglob("*"):
            if p.is_file():
                actual.add(p.relative_to(backup_dir).as_posix())
    except OSError as exc:
        return {"ok": False, "checked": 0, "failures": [f"backup dir unreadable: {exc}"]}
    allowed = set(listed) | {"manifest.json", "manifest.sha256"}
    ws_prefix = "workspace/"
    unexpected = sorted(a for a in actual
                        if a not in allowed and not a.startswith(ws_prefix)
                        and a != "INCOMPLETE.marker")
    if unexpected:
        failures.append(f"unexpected files: {unexpected[:5]}")
    for e in manifest.get("entries", []):
        fpath = backup_dir / e["file"]
        if not fpath.is_file():
            failures.append(f"missing file: {e['file']}")
            continue
        try:
            digest = _sha256_file(fpath)
        except OSError as exc:
            failures.append(f"unreadable file: {e['file']}: {exc}")
            continue
        checked += 1
        if digest != e["sha256"]:
            failures.append(f"hash mismatch: {e['file']}")
    # workspace ファイルの照合。
    ws_files = sorted(a for a in actual if a.startswith(ws_prefix))
    ws_node = manifest.get("workspace", {})
    if len(ws_files) != int(ws_node.get("files", 0)):
        failures.append(f"workspace file count mismatch: manifest={ws_node.get('files')} actual={len(ws_files)}")
    ws_items = ws_node.get("items")
    if not isinstance(ws_items, list):
        failures.append("workspace hash list missing")
        ws_items = []
    expected_ws: dict[str, dict[str, Any]] = {}
    for item in ws_items:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            failures.append("invalid workspace hash entry")
            continue
        rel = item["path"]
        if rel in expected_ws or not rel.startswith(ws_prefix):
            failures.append(f"invalid/duplicate workspace path: {rel}")
            continue
        expected_ws[rel] = item
    if set(ws_files) != set(expected_ws):
        failures.append("workspace file list mismatch")
    for rel in ws_files:
        try:
            actual_size = (backup_dir / rel).stat().st_size
            digest = _sha256_file(backup_dir / rel)
            checked += 1
        except OSError as exc:
            failures.append(f"unreadable workspace file: {rel}: {exc}")
            continue
        item = expected_ws.get(rel)
        if item is not None and (digest != item.get("sha256") or
                                 actual_size != item.get("bytes")):
            failures.append(f"workspace hash/size mismatch: {rel}")
    ok = not failures
    return {"ok": ok, "checked": checked, "failures": failures,
            "project_id": manifest.get("project_id"),
            "backup_format": manifest.get("format")}


# ----------------------------------------------------------------------------
# 復元
# ----------------------------------------------------------------------------

def _target_has_project(target_memory: Path, pid: str,
                        workspace_root: str | Path | None) -> bool:
    if _project_exists(target_memory, pid):
        return True
    for entry in REGISTRY:
        if entry.kind == "other":
            continue
        try:
            if count_entry(target_memory, entry, pid, workspace_root) > 0:
                return True
        except Exception:
            continue
    return False


def _ensure_table(db: sqlite3.Connection, table: str, schema: str) -> None:
    if _table_exists(db, table):
        return
    if not schema.strip():
        raise ValueError(f"table missing and no schema recorded: {table}")
    db.execute(schema)


def restore_backup(manifest_path: str | Path, target_memory_path: str | Path,
                   *, workspace_root: str | Path | None = None,
                   new_project_id: str | None = None) -> dict[str, Any]:
    """退避を復元する。衝突時は拒否。冪等で再開可能。部分失敗は成功と扱わない。"""
    check = verify_backup(manifest_path)
    if not check.get("ok"):
        return {"ok": False, "reason": "backup verification failed",
                "failures": check.get("failures")}
    mpath = Path(manifest_path)
    backup_dir = mpath.parent
    manifest = json.loads(mpath.read_text(encoding="utf-8"))
    src_pid = str(manifest["project_id"])
    pid = str(new_project_id or src_pid).strip()
    if not pid:
        return {"ok": False, "reason": "restore project id is empty"}
    target_mem = Path(target_memory_path)
    try:
        target_mem.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"ok": False, "reason": f"target unavailable: {exc}"}
    if _target_has_project(target_mem, pid, workspace_root):
        return {"ok": False, "reason": f"restore refused: project already exists: {pid}",
                "collision": True}
    errors: list[str] = []
    restored = 0
    # テーブル復元 (INSERT OR REPLACE で冪等)。
    # manifest で「退避元に存在しなかった/0行」と記録されたテーブルは
    # 復元対象なしとして成功扱いにし、復元先に作らない。
    # 本物の欠損 (存在し行があるのにスキーマ無し、行数不一致、
    # manifest/ファイル間のスキーマ不一致、ファイル欠損等) は失敗のまま。
    db_cache: dict[str, sqlite3.Connection] = {}
    try:
        for e in manifest.get("entries", []):
            table = e.get("table") or ""
            if not table:
                continue
            fpath = backup_dir / e["file"]
            try:
                payload = json.loads(fpath.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                errors.append(f"{e['file']}: unreadable: {exc}")
                continue
            rows = payload.get("rows", [])
            if not isinstance(rows, list):
                errors.append(f"{table}: restore failed: rows is not a list")
                continue
            schema_payload = str(payload.get("schema", "") or "")
            try:
                expected = int(e.get("count", 0))
            except (TypeError, ValueError):
                errors.append(f"{table}: restore failed: invalid count in manifest")
                continue
            if len(rows) != expected:
                errors.append(f"{table}: restore failed: row count mismatch: "
                              f"manifest={expected} file={len(rows)}")
                continue
            present_flag = e.get("table_present", None)
            manifest_schema = e.get("schema", None)
            if manifest_schema is not None:
                manifest_schema = str(manifest_schema or "")
                if manifest_schema != schema_payload:
                    errors.append(f"{table}: restore failed: schema mismatch "
                                  f"between manifest and file")
                    continue
            if "table_present" in payload and present_flag is not None:
                try:
                    if bool(payload.get("table_present")) is not bool(present_flag):
                        errors.append(
                            f"{table}: restore failed: table_present mismatch "
                            f"between manifest and file")
                        continue
                except (TypeError, ValueError):
                    pass
            schema = schema_payload
            if present_flag is False:
                # 退避元に存在しなかった。新形式では schema は空・0行が正。
                if (manifest_schema is not None
                        and str(manifest_schema or "").strip() != ""):
                    errors.append(
                        f"{table}: restore failed: inconsistent manifest: "
                        f"absent but schema recorded")
                    continue
                if expected != 0 or len(rows) != 0:
                    errors.append(
                        f"{table}: restore failed: inconsistent manifest: "
                        f"absent but rows present")
                    continue
                # 復元対象なしとして成功扱い。復元先に作らない。
                continue
            if present_flag is True:
                if expected == 0 and len(rows) == 0:
                    # 存在したが0行。復元対象なしとして成功扱い。作らなくてよい。
                    continue
                effective = schema_payload
                if manifest_schema is not None and not effective.strip():
                    effective = str(manifest_schema or "")
                if not effective.strip():
                    errors.append(
                        f"{table}: restore failed: table present with rows "
                        f"but no schema recorded: {table}")
                    continue
                schema = effective
            else:
                # 旧形式 (table_present 無し)。既存挙動を維持:
                # 0行・スキーマ無しは成功扱い (作らない)、行あり・スキーマ無しは失敗。
                if expected == 0 and len(rows) == 0:
                    continue
                if (expected > 0 or len(rows) > 0) and not schema_payload.strip():
                    errors.append(
                        f"{table}: restore failed: table missing and no schema "
                        f"recorded: {table}")
                    continue
                schema = schema_payload
            db_path = resolve_db_path(target_mem, e["db"])
            if db_path is None:
                errors.append(f"{e['file']}: unknown db label: {e['db']}")
                continue
            try:
                db_path.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                errors.append(f"{e['file']}: target dir unavailable: {exc}")
                continue
            try:
                db = db_cache.get(str(db_path))
                if db is None:
                    db = sqlite3.connect(db_path, timeout=30)
                    db_cache[str(db_path)] = db
                with db:
                    _ensure_table(db, table, schema)
                    cols = {r[1] for r in db.execute(
                        f"PRAGMA table_info({table})").fetchall()}
                    for row in rows:
                        item = dict(row)
                        # 別ID復元時は案件キー列を書き換える。結合従属は親IDで辿るため不変。
                        entry_def = next((x for x in REGISTRY if x.name == e.get("name")), None)
                        if pid != src_pid and entry_def is not None and entry_def.project_column:
                            if entry_def.project_column in item:
                                item[entry_def.project_column] = pid
                        usable = {k: _decode_value(v) for k, v in item.items() if k in cols}
                        if not usable:
                            continue
                        names = ",".join(usable.keys())
                        marks = ",".join("?" for _ in usable)
                        db.execute(f"INSERT OR REPLACE INTO {table}({names}) VALUES({marks})",
                                   tuple(usable.values()))
                restored += len(rows)
            except (sqlite3.Error, ValueError, TypeError) as exc:
                errors.append(f"{table}: restore failed: {exc}")
                continue
        # Workspace 復元。
        ws_rel = str(manifest.get("workspace", {}).get("rel") or f"projects/{src_pid}")
        if pid != src_pid and ws_rel == f"projects/{src_pid}":
            ws_rel = f"projects/{pid}"
        src_ws = backup_dir / "workspace" / manifest.get("workspace", {}).get("rel",
                                                                              f"projects/{src_pid}")
        if src_ws.exists() and workspace_root is not None:
            dst_ws = Path(workspace_root) / ws_rel
            try:
                dst_ws.mkdir(parents=True, exist_ok=True)
                for src in sorted(p for p in src_ws.rglob("*") if p.is_file()):
                    rel = src.relative_to(src_ws)
                    dst = dst_ws / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        shutil.copyfile(src, dst)
                    except OSError as exc:
                        errors.append(f"workspace/{rel.as_posix()}: copy failed: {exc}")
            except OSError as exc:
                errors.append(f"workspace: {exc}")
        # 別ID復元時は projects 行の workspace_path を辻褄合わせする。
        if pid != src_pid:
            try:
                main = db_cache.get(str(target_mem))
                own = False
                if main is None and target_mem.exists():
                    main = sqlite3.connect(target_mem, timeout=30)
                    own = True
                if main is not None:
                    with main:
                        if _table_exists(main, "projects"):
                            main.execute("UPDATE projects SET workspace_path=? WHERE id=?",
                                         ("" if ws_rel == f"projects/{pid}" else ws_rel, pid))
                if own:
                    main.close()
            except sqlite3.Error as exc:
                errors.append(f"projects workspace_path adjust failed: {exc}")
    finally:
        for db in db_cache.values():
            try:
                db.close()
            except sqlite3.Error:
                pass
    if errors:
        return {"ok": False, "reason": "partial restore failure",
                "errors": errors, "restored_rows": restored,
                "note": "not successful. re-running resumes (idempotent)."}
    # 復元後検証: 件数とハッシュが manifest と一致すること。
    mismatches: list[str] = []
    for e in manifest.get("entries", []):
        entry_def = next((x for x in REGISTRY if x.name == e.get("name")), None)
        if entry_def is None:
            continue
        try:
            got = count_entry(target_mem, entry_def, pid, workspace_root)
        except Exception as exc:
            mismatches.append(f"{e.get('name')}: count failed: {exc}")
            continue
        if got != int(e.get("count", 0)):
            mismatches.append(f"{e.get('name')}: manifest={e.get('count')} restored={got}")
    # 原本・計画版のハッシュ照合 (context files の original_data / detailed payload)。
    if mismatches:
        return {"ok": False, "reason": "post-restore verification failed",
                "mismatches": mismatches, "restored_rows": restored}
    return {"ok": True, "project_id": pid, "restored_rows": restored,
            "verified": True,
            "note": "counts match manifest. re-running is idempotent."}


# ----------------------------------------------------------------------------
# 退避一覧 (API用。秘密値を含まない要約のみ)
# ----------------------------------------------------------------------------

def list_backups(backup_root: str | Path, project_id: str) -> list[dict[str, Any]]:
    """退避一覧の要約。manifest の安全な項目のみ返す。"""
    pid = str(project_id)
    root = Path(backup_root)
    if not root.exists():
        return []
    out: list[dict[str, Any]] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name.startswith("tmp_"):
            continue
        mpath = child / "manifest.json"
        if not mpath.is_file():
            continue
        try:
            manifest = json.loads(mpath.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if str(manifest.get("project_id") or "") != pid:
            continue
        out.append({
            "backup_id": child.name,
            "project_id": pid,
            "created_at": manifest.get("created_at"),
            "format": manifest.get("format"),
            "total_rows": manifest.get("total_rows"),
            "total_bytes": manifest.get("total_bytes"),
            "entry_count": len(manifest.get("entries", [])),
            "workspace_files": manifest.get("workspace", {}).get("files"),
            "encryption": manifest.get("encryption"),
        })
    return out
