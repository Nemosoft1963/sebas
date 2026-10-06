"""L3: PJ削除の再設計 (プレビュー→退避→論理削除→復元→完全削除)。

L1 の棚卸し・退避・停止条件・残存検出と、L2 の lease・冪等キー・preview token の
流儀を再利用し、新しい並行機構は作らない (PJごとの単一ロック)。
本番DB・実案件には触れない設計。テストは一時ディレクトリの疑似PJのみ。
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

DELETE_FORMAT = "l3-delete/1"
PREVIEW_TOKEN_TTL_SEC = 10 * 60
PURGE_TOKEN_TTL_SEC = 10 * 60
DEFAULT_PROJECT_ID = "default"
DEFAULT_RETENTION_DAYS = 7

# テスト用の失敗注入フック。製品コードでは常に None。
# {"stage": "backup" | "delete" | "purge" | "record"}
FAIL_INJECT: dict[str, Any] | None = None

_tokens: dict[str, dict[str, Any]] = {}
_purge_tokens: dict[str, dict[str, Any]] = {}
_token_lock = threading.Lock()
_lease_locks: dict[str, threading.Lock] = {}
_lease_guard = threading.Lock()


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_retention_days(override: int | None = None) -> int:
    if override is not None:
        try:
            v = int(override)
            return max(0, v)
        except (TypeError, ValueError):
            pass
    try:
        v = int(str(os.getenv("PROJECT_DELETE_RETENTION_DAYS", "") or DEFAULT_RETENTION_DAYS))
        return max(0, v)
    except (TypeError, ValueError):
        return DEFAULT_RETENTION_DAYS


def delete_db_path(memory_path: str | Path) -> Path:
    mem = Path(memory_path)
    return mem.with_name(mem.name + ".delete.sqlite3")


def _connect(memory_path: str | Path) -> sqlite3.Connection:
    path = delete_db_path(memory_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=30)
    db.row_factory = sqlite3.Row
    with db:
        db.execute("""CREATE TABLE IF NOT EXISTS project_delete_states(
          project_id TEXT PRIMARY KEY, state TEXT NOT NULL DEFAULT '',
          actor TEXT NOT NULL DEFAULT '', backup_id TEXT NOT NULL DEFAULT '',
          manifest_sha256 TEXT NOT NULL DEFAULT '',
          deleted_at TEXT NOT NULL DEFAULT '', purgeable_at TEXT NOT NULL DEFAULT '',
          retention_days INTEGER NOT NULL DEFAULT 7,
          fail_reason TEXT NOT NULL DEFAULT '',
          updated_at TEXT NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS project_delete_ops(
          id TEXT PRIMARY KEY, project_id TEXT NOT NULL, op TEXT NOT NULL,
          idempotency_key TEXT NOT NULL DEFAULT '', state TEXT NOT NULL DEFAULT '',
          reason TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
        db.execute("CREATE INDEX IF NOT EXISTS idx_delete_ops_key "
                   "ON project_delete_ops(project_id, op, idempotency_key)")
    return db


def _table_exists(db: sqlite3.Connection, table: str) -> bool:
    try:
        return db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,)).fetchone() is not None
    except sqlite3.Error:
        return False


def _lease_for(pid: str) -> threading.Lock:
    with _lease_guard:
        lock = _lease_locks.get(pid)
        if lock is None:
            lock = threading.Lock()
            _lease_locks[pid] = lock
        return lock


def get_delete_state(memory_path: str | Path, project_id: str) -> dict[str, Any] | None:
    pid = str(project_id)
    path = delete_db_path(memory_path)
    if not path.exists():
        return None
    try:
        db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
    except sqlite3.Error:
        return None
    try:
        if not _table_exists(db, "project_delete_states"):
            return None
        row = db.execute("SELECT * FROM project_delete_states WHERE project_id=?",
                         (pid,)).fetchone()
        return dict(row) if row else None
    except sqlite3.Error:
        return None
    finally:
        db.close()


def is_deleted(memory_path: str | Path, project_id: str) -> bool:
    st = get_delete_state(memory_path, project_id)
    return bool(st and st.get("state") == "deleted")


def is_purged(memory_path: str | Path, project_id: str) -> bool:
    st = get_delete_state(memory_path, project_id)
    return bool(st and st.get("state") == "purged")


def list_deleted(memory_path: str | Path) -> list[dict[str, Any]]:
    path = delete_db_path(memory_path)
    if not path.exists():
        return []
    try:
        db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
    except sqlite3.Error:
        return []
    try:
        if not _table_exists(db, "project_delete_states"):
            return []
        rows = db.execute("SELECT * FROM project_delete_states ORDER BY updated_at").fetchall()
        return [dict(r) for r in rows]
    except sqlite3.Error:
        return []
    finally:
        db.close()


def _project_name(memory_path: str | Path, pid: str) -> str:
    mem = Path(memory_path)
    if not mem.exists():
        return ""
    try:
        db = sqlite3.connect(f"file:{mem.as_posix()}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        try:
            try:
                r = db.execute("SELECT name FROM projects WHERE id=?", (pid,)).fetchone()
                return str(r["name"]) if r else ""
            except sqlite3.Error:
                return ""
        finally:
            db.close()
    except sqlite3.Error:
        return ""


def _project_exists(memory_path: str | Path, pid: str) -> bool:
    mem = Path(memory_path)
    if not mem.exists():
        return False
    try:
        db = sqlite3.connect(f"file:{mem.as_posix()}?mode=ro", uri=True)
        try:
            try:
                return db.execute("SELECT 1 FROM projects WHERE id=?", (pid,)).fetchone() is not None
            except sqlite3.Error:
                return False
        finally:
            db.close()
    except sqlite3.Error:
        return False


def _state_fingerprint(memory_path: str | Path, pid: str,
                       workspace_root: str | Path | None = None) -> str:
    from app.project_lifecycle_registry import count_all
    mem = Path(memory_path)
    plan_version = 0
    if mem.exists():
        try:
            db = sqlite3.connect(f"file:{mem.as_posix()}?mode=ro", uri=True)
            try:
                try:
                    r = db.execute("SELECT plan_version FROM project_missions WHERE project_id=?",
                                   (pid,)).fetchone()
                    if r:
                        plan_version = int(r[0] or 0)
                except sqlite3.Error:
                    plan_version = 0
            finally:
                db.close()
        except sqlite3.Error:
            plan_version = 0
    try:
        counts = count_all(memory_path, pid, workspace_root)
    except Exception:
        counts = {}
    total = sum(int(v) for v in counts.values())
    raw = json.dumps({"pid": pid, "plan_version": plan_version, "total": total,
                      "counts": sorted(counts.items())},
                     ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def check_stop_conditions(memory_path: str | Path, project_id: str,
                          executions: dict | None = None) -> dict[str, Any]:
    """L2 の停止条件を再利用 (並行機構を作らない)。"""
    from app.project_generation import check_stop_conditions as l2_checks
    return l2_checks(memory_path, project_id, executions)


def build_delete_request_preview(memory_path: str | Path, project_id: str,
                                 workspace_root: str | Path | None = None,
                                 executions: dict | None = None,
                                 retention_days: int | None = None) -> dict[str, Any]:
    """削除プレビュー。読み取り専用・副作用なし (トークン発行のみ)。"""
    from app.project_lifecycle_registry import build_delete_preview
    pid = str(project_id)
    base = build_delete_preview(memory_path, pid, workspace_root, executions)
    name = str((base.get("project") or {}).get("name") or "")
    fingerprint = _state_fingerprint(memory_path, pid, workspace_root)
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with _token_lock:
        _tokens[token_hash] = {"project_id": pid, "fingerprint": fingerprint,
                               "project_name": name,
                               "expires": time.time() + PREVIEW_TOKEN_TTL_SEC,
                               "created_at": _utcnow()}
    ret = get_retention_days(retention_days)
    blocked = list(base.get("blocked_reasons") or [])
    stops = check_stop_conditions(memory_path, pid, executions)
    for r in stops.get("reasons") or []:
        if r not in blocked:
            blocked.append(r)
    if not _project_exists(memory_path, pid):
        blocked.append("PJが存在しません")
    st = get_delete_state(memory_path, pid)
    if st and st.get("state") == "deleted":
        blocked.append("既に論理削除されています")
    if st and st.get("state") == "purged":
        blocked.append("既に完全削除されています")
    out = dict(base)
    out.update({
        "format": DELETE_FORMAT,
        "preview_token": token,
        "token_expires_in_sec": PREVIEW_TOKEN_TTL_SEC,
        "retention_days": ret,
        "restorable_note": f"論理削除後は{ret}日間復元できます(保持期間)。外部に公開済みの成果物は消せません",
        "external_warning_full": ("フォーム/Google Sites/SNS投稿/送信済みメール等の外部公開物は"
                                  "ローカル削除では撤回・取消できません" if base.get("external_items") else ""),
        "stop_conditions": stops,
        "blocked_reasons": blocked,
        "can_delete": not blocked,
        "delete_state": st,
    })
    return out


def _verify_preview_token(pid: str, token: str, project_name: str,
                          memory_path: str | Path,
                          workspace_root: str | Path | None = None) -> dict[str, Any]:
    if not token:
        return {"ok": False, "reason": "preview token is required"}
    token_hash = hashlib.sha256(str(token).encode()).hexdigest()
    with _token_lock:
        row = _tokens.get(token_hash)
    if row is None:
        return {"ok": False, "reason": "preview token が無効です"}
    if row.get("project_id") != pid:
        return {"ok": False, "reason": "preview token が対象と一致しません"}
    if float(row.get("expires") or 0) < time.time():
        return {"ok": False, "reason": "preview token の期限が切れています"}
    expected_name = str(row.get("project_name") or "")
    if str(project_name or "") != expected_name or not expected_name:
        return {"ok": False, "reason": "PJ名が一致しません"}
    current = _state_fingerprint(memory_path, pid, workspace_root)
    if current != row.get("fingerprint"):
        return {"ok": False, "reason": "プレビュー後に状態が変わりました。再取得してください"}
    return {"ok": True}


def _find_op(memory_path: str | Path, pid: str, op: str, key: str) -> dict | None:
    if not key:
        return None
    try:
        db = _connect(memory_path)
    except sqlite3.Error:
        return None
    try:
        rows = db.execute(
            "SELECT * FROM project_delete_ops WHERE project_id=? AND op=? AND idempotency_key=? "
            "ORDER BY created_at DESC", (pid, op, key)).fetchall()
        return dict(rows[0]) if rows else None
    except sqlite3.Error:
        return None
    finally:
        db.close()


def _record_op(memory_path: str | Path, pid: str, op: str, key: str,
               state: str, reason: str = "") -> dict:
    oid = f"op_{pid}_{op}_{uuid.uuid4().hex[:8]}"
    now = _utcnow()
    db = _connect(memory_path)
    try:
        with db:
            db.execute("INSERT INTO project_delete_ops(id, project_id, op, idempotency_key,"
                       " state, reason, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
                       (oid, pid, op, key, state, str(reason or "")[:2000], now, now))
    finally:
        db.close()
    return {"id": oid, "state": state}


def _set_state(memory_path: str | Path, pid: str, state: str, *, actor: str = "",
               backup_id: str = "", manifest_sha256: str = "",
               retention_days: int = DEFAULT_RETENTION_DAYS,
               fail_reason: str = "") -> None:
    now = _utcnow()
    deleted_at = ""
    purgeable_at = ""
    cur = get_delete_state(memory_path, pid)
    if state == "deleted":
        deleted_at = now
        try:
            base = datetime.now(timezone.utc) + timedelta(days=int(retention_days))
            purgeable_at = base.isoformat()
        except (TypeError, ValueError):
            purgeable_at = now
    elif cur:
        deleted_at = str(cur.get("deleted_at") or "")
        purgeable_at = str(cur.get("purgeable_at") or "")
        if state == "restored":
            # 復元後も履歴として退避IDは残すが、削除状態は解除する。
            pass
    db = _connect(memory_path)
    try:
        with db:
            db.execute("INSERT OR REPLACE INTO project_delete_states(project_id, state, actor,"
                       " backup_id, manifest_sha256, deleted_at, purgeable_at,"
                       " retention_days, fail_reason, updated_at)"
                       " VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (pid, state, str(actor or "")[:100],
                        str(backup_id or "")[:200], str(manifest_sha256 or "")[:128],
                        deleted_at, purgeable_at, int(retention_days),
                        str(fail_reason or "")[:2000], now))
    finally:
        db.close()


def request_delete(memory_path: str | Path, project_id: str, *,
                   preview_token: str, project_name: str,
                   actor: str = "", reason: str = "",
                   idempotency_key: str = "",
                   workspace_root: str | Path | None = None,
                   backup_root: str | Path | None = None,
                   executions: dict | None = None,
                   retention_days: int | None = None) -> dict[str, Any]:
    """退避→論理削除。失敗時は failed として残し、半端な状態を成功と表示しない。"""
    from app.project_lifecycle_backup import create_backup, verify_backup
    pid = str(project_id)
    actor_name = str(actor or "").strip()
    if pid == DEFAULT_PROJECT_ID:
        return {"ok": False, "reason": "既定PJは削除できません"}
    if not actor_name:
        return {"ok": False, "reason": "actor is required"}
    tok = _verify_preview_token(pid, preview_token, project_name, memory_path, workspace_root)
    if not tok.get("ok"):
        return {"ok": False, "reason": tok.get("reason")}
    stops = check_stop_conditions(memory_path, pid, executions)
    if not stops.get("ok"):
        return {"ok": False, "reason": "; ".join(stops["reasons"]), "stops": stops}
    if not _project_exists(memory_path, pid):
        return {"ok": False, "reason": "PJが存在しません"}
    cur = get_delete_state(memory_path, pid)
    if cur and cur.get("state") == "deleted":
        return {"ok": False, "reason": "既に論理削除されています"}
    if cur and cur.get("state") == "purged":
        return {"ok": False, "reason": "既に完全削除されています"}
    key = str(idempotency_key or "").strip()
    if key:
        existing = _find_op(memory_path, pid, "delete", key)
        if existing is not None and existing.get("state") in ("ok", "deleted"):
            st = get_delete_state(memory_path, pid)
            return {"ok": True, "idempotent": True, "delete_state": st,
                    "note": "同じ冪等キーによる再送のため再実行しません"}
        if existing is not None and existing.get("state") == "failed":
            pass
    lock = _lease_for(pid)
    if not lock.acquire(blocking=False):
        return {"ok": False, "reason": "同じPJの削除を実行中です", "code": "LEASE_BUSY"}
    try:
        if key:
            existing = _find_op(memory_path, pid, "delete", key)
            if existing is not None and existing.get("state") in ("ok", "deleted"):
                st = get_delete_state(memory_path, pid)
                return {"ok": True, "idempotent": True, "delete_state": st,
                        "note": "同じ冪等キーによる再送のため再実行しません"}
        if FAIL_INJECT and FAIL_INJECT.get("stage") == "backup":
            backup = {"ok": False, "reason": "injected backup failure"}
        else:
            if backup_root is None:
                backup = {"ok": False, "reason": "backup_root is required"}
            else:
                backup = create_backup(memory_path, pid, backup_root,
                                       workspace_root=workspace_root, actor=actor_name)
        if not backup.get("ok"):
            _record_op(memory_path, pid, "delete", key, "failed",
                       str(backup.get("reason") or "backup failed"))
            _set_state(memory_path, pid, "failed", actor=actor_name,
                       fail_reason=f"退避に失敗したため何も変更しません: {backup.get('reason')}")
            return {"ok": False, "reason": f"退避に失敗したため何も変更しません: {backup.get('reason')}",
                    "backup": backup, "code": "BACKUP_FAILED",
                    "restore_hint": "何も変更していません。原因を解消して再実行してください"}
        check = verify_backup(backup["manifest_path"])
        if not check.get("ok"):
            _record_op(memory_path, pid, "delete", key, "failed", "backup verify failed")
            _set_state(memory_path, pid, "failed", actor=actor_name,
                       fail_reason="退避の検証に失敗したため何も変更しません")
            return {"ok": False, "reason": "退避の検証に失敗したため何も変更しません",
                    "verify": check, "backup": backup, "code": "VERIFY_FAILED"}
        if FAIL_INJECT and FAIL_INJECT.get("stage") in ("delete", "record"):
            _record_op(memory_path, pid, "delete", key, "failed", "injected delete failure")
            _set_state(memory_path, pid, "failed", actor=actor_name,
                       backup_id=str(backup.get("backup_id") or ""),
                       manifest_sha256=str(backup.get("manifest_sha256") or ""),
                       fail_reason="削除処理に失敗しました(注入)。成功と表示しません")
            return {"ok": False, "reason": "削除処理に失敗しました",
                    "backup_id": backup.get("backup_id"),
                    "restore_hint": f"退避 {backup.get('backup_id')} は検証済みです。原因解消後に再実行してください",
                    "code": "DELETE_FAILED"}
        ret = get_retention_days(retention_days)
        _set_state(memory_path, pid, "deleted", actor=actor_name,
                   backup_id=str(backup.get("backup_id") or ""),
                   manifest_sha256=str(backup.get("manifest_sha256") or ""),
                   retention_days=ret)
        _record_op(memory_path, pid, "delete", key, "ok", str(reason or "")[:500])
        with _token_lock:
            _tokens.pop(hashlib.sha256(str(preview_token).encode()).hexdigest(), None)
        try:
            from app.memory.short_term import ShortTermMemory
            mem = ShortTermMemory(Path(memory_path))
            mem.add_event(pid, "project_delete_requested",
                          f"PJを退避して論理削除: backup={backup.get('backup_id')}",
                          detail=json.dumps({"backup_id": backup.get("backup_id"),
                                             "actor": actor_name,
                                             "reason": str(reason or "")[:500]},
                                            ensure_ascii=False))
        except Exception:
            pass
        st = get_delete_state(memory_path, pid)
        from app.project_lifecycle_registry import collect_external_items
        externals = collect_external_items(memory_path, pid)
        return {"ok": True, "delete_state": st, "backup_id": backup.get("backup_id"),
                "manifest_sha256": backup.get("manifest_sha256"),
                "purgeable_at": (st or {}).get("purgeable_at", ""),
                "external_note": ("外部に公開済みの成果物(Google公開、メール等)は消せません・"
                                  "取り消されません" if externals else "外部公開物は検出されませんでした"),
                "external_items_count": len(externals),
                "note": f"論理削除しました。{ret}日間は復元できます。データは残しています"}
    finally:
        try:
            lock.release()
        except RuntimeError:
            pass


def restore_deleted(memory_path: str | Path, project_id: str, *,
                    actor: str = "", idempotency_key: str = "",
                    workspace_root: str | Path | None = None,
                    backup_root: str | Path | None = None) -> dict[str, Any]:
    """論理削除されたPJを復元。冪等。復元後に件数とハッシュが退避と一致。"""
    from app.project_lifecycle_backup import verify_backup
    pid = str(project_id)
    actor_name = str(actor or "").strip()
    if not actor_name:
        return {"ok": False, "reason": "actor is required"}
    if pid == DEFAULT_PROJECT_ID:
        return {"ok": False, "reason": "既定PJは復元できません"}
    st = get_delete_state(memory_path, pid)
    if st is None or st.get("state") != "deleted":
        if st and st.get("state") == "restored":
            key0 = str(idempotency_key or "").strip()
            if key0:
                existing = _find_op(memory_path, pid, "restore", key0)
                if existing is not None and existing.get("state") == "ok":
                    return {"ok": True, "idempotent": True, "delete_state": st,
                            "note": "同じ冪等キーによる再送のため再実行しません"}
            return {"ok": False, "reason": "既に復元されています"}
        return {"ok": False, "reason": "論理削除されていません"}
    key = str(idempotency_key or "").strip()
    if key:
        existing = _find_op(memory_path, pid, "restore", key)
        if existing is not None and existing.get("state") == "ok":
            return {"ok": True, "idempotent": True, "delete_state": get_delete_state(memory_path, pid),
                    "note": "同じ冪等キーによる再送のため再実行しません"}
    lock = _lease_for(pid)
    if not lock.acquire(blocking=False):
        return {"ok": False, "reason": "同じPJの削除・復元を実行中です", "code": "LEASE_BUSY"}
    try:
        if key:
            existing = _find_op(memory_path, pid, "restore", key)
            if existing is not None and existing.get("state") == "ok":
                return {"ok": True, "idempotent": True,
                        "delete_state": get_delete_state(memory_path, pid),
                        "note": "同じ冪等キーによる再送のため再実行しません"}
        backup_id = str(st.get("backup_id") or "")
        if backup_root is None or not backup_id:
            return {"ok": False, "reason": "backup_root または backup_id が不足しています"}
        manifest_path = Path(backup_root) / backup_id / "manifest.json"
        if not manifest_path.is_file():
            _record_op(memory_path, pid, "restore", key, "failed", "backup not found")
            return {"ok": False, "reason": "退避が見つかりません",
                    "restore_hint": "退避が無いため復元できません。管理者に連絡してください"}
        check = verify_backup(manifest_path)
        if not check.get("ok"):
            _record_op(memory_path, pid, "restore", key, "failed", "backup verify failed")
            return {"ok": False, "reason": "退避の検証に失敗したため復元しません",
                    "failures": check.get("failures")}
        # 論理削除中はデータが残っているはず。件数とハッシュが退避と一致するか確認。
        # project_events は追加型の監査履歴であり、delete-request 自体が1件足すため
        # 件数完全一致の対象外とする (L2 と同じ流儀。消さないことが要件)。
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return {"ok": False, "reason": f"manifest unreadable: {exc}"}
        from app.project_lifecycle_registry import count_all
        try:
            counts = count_all(memory_path, pid, workspace_root)
        except Exception as exc:
            _record_op(memory_path, pid, "restore", key, "failed", f"count failed: {exc}")
            return {"ok": False, "reason": f"件数確認に失敗しました: {exc}"}
        mismatches: list[str] = []
        for e in manifest.get("entries", []):
            name = e.get("name")
            if name == "main:project_events":
                continue
            if counts.get(name, 0) != int(e.get("count", 0)):
                mismatches.append(f"{name}: manifest={e.get('count')} current={counts.get(name, 0)}")
        if mismatches:
            # データが欠けている場合は L1 restore_backup が必要。論理削除のはずが
            # 欠損しているため、成功と表示せず手順を示す。
            _record_op(memory_path, pid, "restore", key, "failed", "count mismatch")
            _set_state(memory_path, pid, "failed", actor=actor_name,
                       backup_id=backup_id,
                       manifest_sha256=str(st.get("manifest_sha256") or ""),
                       fail_reason="復元前の件数が退避と一致しません")
            return {"ok": False, "reason": "復元前の件数が退避と一致しません",
                    "mismatches": mismatches,
                    "restore_hint": f"L1 restore_backup で退避 {backup_id} から別ターゲットへ復元し、"
                                    f"内容を確認してください。半端な状態を成功と表示しません",
                    "code": "RESTORE_MISMATCH"}
        _set_state(memory_path, pid, "restored", actor=actor_name,
                   backup_id=backup_id,
                   manifest_sha256=str(st.get("manifest_sha256") or ""),
                   retention_days=int(st.get("retention_days") or DEFAULT_RETENTION_DAYS))
        _record_op(memory_path, pid, "restore", key, "ok", "")
        try:
            from app.memory.short_term import ShortTermMemory
            mem = ShortTermMemory(Path(memory_path))
            mem.add_event(pid, "project_delete_restored",
                          f"論理削除から復元: backup={backup_id}",
                          detail=json.dumps({"backup_id": backup_id, "actor": actor_name},
                                            ensure_ascii=False))
        except Exception:
            pass
        out_state = get_delete_state(memory_path, pid)
        return {"ok": True, "delete_state": out_state, "restored_rows": manifest.get("total_rows"),
                "verified": True,
                "note": "復元しました。件数とハッシュが退避と一致しました"}
    finally:
        try:
            lock.release()
        except RuntimeError:
            pass


def build_purge_preview(memory_path: str | Path, project_id: str,
                        workspace_root: str | Path | None = None,
                        retention_days: int | None = None) -> dict[str, Any]:
    """purge 専用プレビュー。読み取り専用。専用 purge 確認トークンを発行。"""
    from app.project_lifecycle_registry import collect_external_items, count_all
    pid = str(project_id)
    st = get_delete_state(memory_path, pid)
    name = _project_name(memory_path, pid)
    try:
        counts = count_all(memory_path, pid, workspace_root)
    except Exception:
        counts = {}
    externals = collect_external_items(memory_path, pid)
    fingerprint = _state_fingerprint(memory_path, pid, workspace_root)
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with _token_lock:
        _purge_tokens[token_hash] = {"project_id": pid, "fingerprint": fingerprint,
                                     "project_name": name,
                                     "expires": time.time() + PURGE_TOKEN_TTL_SEC,
                                     "created_at": _utcnow()}
    blocked: list[str] = []
    if pid == DEFAULT_PROJECT_ID:
        blocked.append("既定PJは削除できません")
    if st is None or st.get("state") != "deleted":
        blocked.append("論理削除されていません(先に退避して削除してください)")
    else:
        try:
            purgeable = datetime.fromisoformat(str(st.get("purgeable_at") or ""))
            now = datetime.now(timezone.utc)
            if purgeable.tzinfo is None:
                purgeable = purgeable.replace(tzinfo=timezone.utc)
            if now < purgeable:
                blocked.append(f"保持期間内です。{st.get('purgeable_at')} までは完全削除できません")
        except (ValueError, TypeError):
            blocked.append("削除状態の日時が不正です")
    if not _project_exists(memory_path, pid) and (st is None or st.get("state") != "deleted"):
        blocked.append("PJが存在しません")
    ret = int((st or {}).get("retention_days") or get_retention_days(retention_days))
    return {
        "format": DELETE_FORMAT,
        "project_id": pid, "project_name": name,
        "delete_state": st,
        "retention_days": ret,
        "purge_token": token,
        "token_expires_in_sec": PURGE_TOKEN_TTL_SEC,
        "counts": counts,
        "total_rows": sum(int(v) for v in counts.values()),
        "external_items": externals,
        "external_warning": ("外部に公開済みの成果物(Google公開、メール等)は消せません・"
                             "取り消されません" if externals else "外部公開物は検出されませんでした"),
        "blocked_reasons": blocked,
        "can_purge": not blocked,
        "restore_hint": (f"完全削除前に退避 {str((st or {}).get('backup_id') or '')} が検証済みである必要があります"
                         if st else ""),
        "read_only": True,
    }


def _verify_purge_token(pid: str, token: str, project_name: str,
                        memory_path: str | Path,
                        workspace_root: str | Path | None = None) -> dict[str, Any]:
    if not token:
        return {"ok": False, "reason": "purge 確認トークンが必要です"}
    token_hash = hashlib.sha256(str(token).encode()).hexdigest()
    with _token_lock:
        row = _purge_tokens.get(token_hash)
    if row is None:
        return {"ok": False, "reason": "purge 確認トークンが無効です"}
    if row.get("project_id") != pid:
        return {"ok": False, "reason": "purge 確認トークンが対象と一致しません"}
    if float(row.get("expires") or 0) < time.time():
        return {"ok": False, "reason": "purge 確認トークンの期限が切れています"}
    expected_name = str(row.get("project_name") or "")
    if str(project_name or "") != expected_name or not expected_name:
        return {"ok": False, "reason": "PJ名が一致しません"}
    current = _state_fingerprint(memory_path, pid, workspace_root)
    if current != row.get("fingerprint"):
        return {"ok": False, "reason": "プレビュー後に状態が変わりました。再取得してください"}
    return {"ok": True}


def _delete_entry_rows(memory_path: str | Path, entry_name: str, pid: str) -> int:
    from app.project_lifecycle_registry import REGISTRY, resolve_db_path
    entry = next((x for x in REGISTRY if x.name == entry_name), None)
    if entry is None or entry.kind in ("directory", "other"):
        return 0
    if entry.db_label == "generation":
        return 0
    db_path = resolve_db_path(memory_path, entry.db_label)
    if db_path is None or not db_path.exists():
        return 0
    try:
        db = sqlite3.connect(str(db_path), timeout=30)
    except sqlite3.Error:
        return 0
    try:
        try:
            names = {r[0] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        except sqlite3.Error:
            return 0
        if not entry.table or entry.table not in names:
            return 0
        try:
            cols = {r[1] for r in db.execute(
                f"PRAGMA table_info({entry.table})").fetchall()}
        except sqlite3.Error:
            return 0
        removed = 0
        try:
            with db:
                if entry.project_column and entry.project_column in cols:
                    cur = db.execute(
                        f"DELETE FROM {entry.table} WHERE {entry.project_column}=?", (pid,))
                    removed += cur.rowcount or 0
                elif entry.table in ("ocr_pages", "ocr_blocks", "ocr_fields",
                                     "ocr_validations", "ocr_reviews"):
                    if "ocr_runs" not in names or "run_id" not in cols:
                        return 0
                    cur = db.execute(
                        f"DELETE FROM {entry.table} WHERE run_id IN "
                        f"(SELECT run_id FROM ocr_runs WHERE project_id=?)", (pid,))
                    removed += cur.rowcount or 0
                elif entry.table in ("detailed_steps", "detailed_reviews"):
                    cur = db.execute(
                        f"DELETE FROM {entry.table} WHERE plan_id IN "
                        f"(SELECT id FROM detailed_plans WHERE project_id=?)", (pid,))
                    removed += cur.rowcount or 0
        except sqlite3.Error:
            try:
                db.rollback()
            except sqlite3.Error:
                pass
            return 0
        return removed
    finally:
        db.close()


def _purge_all_data(memory_path: str | Path, pid: str,
                    workspace_root: str | Path | None = None,
                    manifest_ws_rel: str = "") -> dict[str, Any]:
    from app.project_lifecycle_registry import REGISTRY, resolve_workspace_dir
    removed: dict[str, int] = {}
    for entry in REGISTRY:
        if entry.db_label == "generation":
            continue
        if entry.kind in ("directory", "other"):
            continue
        # goal_reviews:reviews は全 kind を外す。
        removed[entry.name] = _delete_entry_rows(memory_path, entry.name, pid)
    # projects 行自体を最後に消す (存在確認の共通入口で除外されるため、物理削除)。
    mem = Path(memory_path)
    if mem.exists():
        try:
            db = sqlite3.connect(str(mem), timeout=30)
            try:
                with db:
                    try:
                        cur = db.execute("DELETE FROM project_missions WHERE project_id=?", (pid,))
                        removed["main:project_missions(final)"] = cur.rowcount or 0
                    except sqlite3.Error:
                        pass
                    try:
                        cur = db.execute("DELETE FROM projects WHERE id=?", (pid,))
                        removed["main:projects"] = cur.rowcount or 0
                    except sqlite3.Error:
                        pass
            finally:
                db.close()
        except sqlite3.Error:
            pass
    # Workspace 削除。
    ws_removed = 0
    if workspace_root is not None:
        configured = ""
        if mem.exists():
            try:
                db = sqlite3.connect(f"file:{mem.as_posix()}?mode=ro", uri=True)
                try:
                    try:
                        # projects 行は既に消えている場合があるため失敗しても続行。
                        r = db.execute("SELECT workspace_path FROM projects WHERE id=?",
                                       (pid,)).fetchone()
                        if r:
                            configured = str(r[0] or "")
                    except sqlite3.Error:
                        pass
                finally:
                    db.close()
            except sqlite3.Error:
                pass
        d = resolve_workspace_dir(memory_path, pid, workspace_root, configured)
        # 設定が読めない場合・projects行が既に消えている場合に備え、退避manifestの
        # 配置 (workspace.rel) と既定配置も試す。
        candidates = [d] if d is not None else []
        for rel in (manifest_ws_rel, f"projects/{pid}"):
            if not rel:
                continue
            fb = resolve_workspace_dir(memory_path, pid, workspace_root, rel)
            if fb is not None and fb not in candidates:
                candidates.append(fb)
        for target in candidates:
            if target is not None and target.exists():
                try:
                    shutil.rmtree(target)
                    ws_removed += 1
                except OSError:
                    continue
    removed["workspace:project_dir(removed)"] = ws_removed
    return removed


def purge_deleted(memory_path: str | Path, project_id: str, *,
                  purge_token: str, project_name: str,
                  actor: str = "", idempotency_key: str = "",
                  workspace_root: str | Path | None = None,
                  backup_root: str | Path | None = None) -> dict[str, Any]:
    """論理削除後の完全削除。保持期間・専用トークン・退避検証が必須。"""
    from app.project_lifecycle_backup import verify_backup
    from app.project_lifecycle_registry import collect_external_items, list_orphans_after_legacy_delete
    pid = str(project_id)
    actor_name = str(actor or "").strip()
    if pid == DEFAULT_PROJECT_ID:
        return {"ok": False, "reason": "既定PJは削除できません"}
    if not actor_name:
        return {"ok": False, "reason": "actor is required"}
    tok = _verify_purge_token(pid, purge_token, project_name, memory_path, workspace_root)
    if not tok.get("ok"):
        return {"ok": False, "reason": tok.get("reason")}
    st = get_delete_state(memory_path, pid)
    if st is None or st.get("state") != "deleted":
        return {"ok": False, "reason": "論理削除されていません(先に退避して削除してください)"}
    try:
        purgeable = datetime.fromisoformat(str(st.get("purgeable_at") or ""))
        now = datetime.now(timezone.utc)
        if purgeable.tzinfo is None:
            purgeable = purgeable.replace(tzinfo=timezone.utc)
        if now < purgeable:
            return {"ok": False, "reason": f"保持期間内です。{st.get('purgeable_at')} までは完全削除できません"}
    except (ValueError, TypeError):
        return {"ok": False, "reason": "削除状態の日時が不正です"}
    key = str(idempotency_key or "").strip()
    if key:
        existing = _find_op(memory_path, pid, "purge", key)
        if existing is not None and existing.get("state") == "ok":
            return {"ok": True, "idempotent": True, "delete_state": get_delete_state(memory_path, pid),
                    "note": "同じ冪等キーによる再送のため再実行しません"}
    lock = _lease_for(pid)
    if not lock.acquire(blocking=False):
        return {"ok": False, "reason": "同じPJの削除を実行中です", "code": "LEASE_BUSY"}
    try:
        if key:
            existing = _find_op(memory_path, pid, "purge", key)
            if existing is not None and existing.get("state") == "ok":
                return {"ok": True, "idempotent": True,
                        "delete_state": get_delete_state(memory_path, pid),
                        "note": "同じ冪等キーによる再送のため再実行しません"}
        backup_id = str(st.get("backup_id") or "")
        if backup_root is None or not backup_id:
            return {"ok": False, "reason": "退避が無いため完全削除できません"}
        manifest_path = Path(backup_root) / backup_id / "manifest.json"
        if not manifest_path.is_file():
            _record_op(memory_path, pid, "purge", key, "failed", "backup not found")
            return {"ok": False, "reason": "退避が無いため完全削除できません",
                    "restore_hint": "退避が無いため完全削除できません"}
        check = verify_backup(manifest_path)
        if not check.get("ok"):
            _record_op(memory_path, pid, "purge", key, "failed", "backup verify failed")
            return {"ok": False, "reason": "完全削除前に退避が検証済みである必要があります",
                    "failures": check.get("failures")}
        if FAIL_INJECT and FAIL_INJECT.get("stage") == "purge":
            _record_op(memory_path, pid, "purge", key, "failed", "injected purge failure")
            _set_state(memory_path, pid, "failed", actor=actor_name, backup_id=backup_id,
                       manifest_sha256=str(st.get("manifest_sha256") or ""),
                       fail_reason="完全削除に失敗しました(注入)。成功と表示しません")
            return {"ok": False, "reason": "完全削除に失敗しました",
                    "restore_hint": f"退避 {backup_id} は検証済みです。原因解消後に再実行してください",
                    "code": "PURGE_FAILED"}
        externals = collect_external_items(memory_path, pid)
        try:
            _manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            _ws_rel = str((_manifest.get("workspace") or {}).get("rel") or "")
        except (OSError, ValueError):
            _ws_rel = ""
        removed = _purge_all_data(memory_path, pid, workspace_root, _ws_rel)
        orphans = list_orphans_after_legacy_delete(memory_path, pid, workspace_root)
        if orphans.get("orphan_count"):
            _record_op(memory_path, pid, "purge", key, "failed", "orphans remain")
            _set_state(memory_path, pid, "failed", actor=actor_name, backup_id=backup_id,
                       manifest_sha256=str(st.get("manifest_sha256") or ""),
                       fail_reason="完全削除後に残存があります")
            return {"ok": False, "reason": "完全削除後に孤児データが残っています",
                    "orphans": orphans, "removed": removed, "code": "ORPHANS_REMAIN"}
        _set_state(memory_path, pid, "purged", actor=actor_name, backup_id=backup_id,
                   manifest_sha256=str(st.get("manifest_sha256") or ""),
                   retention_days=int(st.get("retention_days") or DEFAULT_RETENTION_DAYS))
        _record_op(memory_path, pid, "purge", key, "ok", "")
        with _token_lock:
            _purge_tokens.pop(hashlib.sha256(str(purge_token).encode()).hexdigest(), None)
        return {"ok": True, "delete_state": get_delete_state(memory_path, pid),
                "removed": removed, "orphans": orphans,
                "backup_id": backup_id,
                "external_note": ("外部に公開済みの成果物(Google公開、メール等)は消せません・"
                                  "取り消されません" if externals else "外部公開物は検出されませんでした"),
                "restore_procedure": (f"退避 {backup_id} は残っています。必要時は L1 restore_backup で"
                                      f"別ターゲットへ復元してください。外部公開物は復元・取消できません"),
                "note": "完全削除しました。退避は残しています"}
    finally:
        try:
            lock.release()
        except RuntimeError:
            pass


@contextmanager
def failure_injection(stage: str | None):
    global FAIL_INJECT
    prev = FAIL_INJECT
    FAIL_INJECT = {"stage": stage} if stage else None
    try:
        yield
    finally:
        FAIL_INJECT = prev
