"""L2: PJ初期化 (世代切替) 。

PJのIDと名前を保ったまま、選択した作業状態を新しい世代へ切り替える。
物理削除ではなく「退避→現行参照を空にする」方式。
L1 の棚卸し・退避・停止条件を再利用し、新しい並行機構は作らない
(プロセス内の lease は plan_review_loop と同じく PJ ごとの単一ロック)。

方式の選択理由と限界は最終報告に記載する。
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.project_lifecycle_registry import (
    REGISTRY,
    count_all,
    resolve_db_path,
)

GENERATION_FORMAT = "l2-generation/1"
PREVIEW_TOKEN_TTL_SEC = 10 * 60
DEFAULT_PROJECT_ID = "default"
MODES = ("replan", "rerun", "fresh")

# テスト用の失敗注入フック。製品コードでは常に None。
# {"stage": "backup" | "switch" | "record", "at_entry": str|None}
FAIL_INJECT: dict[str, Any] | None = None

_tokens: dict[str, dict[str, Any]] = {}
_token_lock = threading.Lock()
_lease_locks: dict[str, threading.Lock] = {}
_lease_guard = threading.Lock()


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def generation_db_path(memory_path: str | Path) -> Path:
    mem = Path(memory_path)
    return mem.with_name(mem.name + ".generation.sqlite3")


def _connect(memory_path: str | Path) -> sqlite3.Connection:
    path = generation_db_path(memory_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=30)
    db.row_factory = sqlite3.Row
    with db:
        db.execute("""CREATE TABLE IF NOT EXISTS project_generations(
          id TEXT PRIMARY KEY, project_id TEXT NOT NULL,
          generation INTEGER NOT NULL, mode TEXT NOT NULL,
          actor TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '',
          backup_id TEXT NOT NULL DEFAULT '', manifest_sha256 TEXT NOT NULL DEFAULT '',
          input_version TEXT NOT NULL DEFAULT '',
          state TEXT NOT NULL DEFAULT 'active',
          idempotency_key TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
        db.execute("CREATE INDEX IF NOT EXISTS idx_generations_project "
                   "ON project_generations(project_id, generation DESC)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_generations_idem "
                   "ON project_generations(project_id, idempotency_key)")
    return db


def _lease_for(pid: str) -> threading.Lock:
    with _lease_guard:
        lock = _lease_locks.get(pid)
        if lock is None:
            lock = threading.Lock()
            _lease_locks[pid] = lock
        return lock


def _table_exists(db: sqlite3.Connection, table: str) -> bool:
    try:
        return db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,)).fetchone() is not None
    except sqlite3.Error:
        return False


def list_generations(memory_path: str | Path, project_id: str) -> list[dict[str, Any]]:
    pid = str(project_id)
    path = generation_db_path(memory_path)
    if not path.exists():
        return []
    try:
        db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
    except sqlite3.Error:
        return []
    try:
        if not _table_exists(db, "project_generations"):
            return []
        rows = db.execute(
            "SELECT * FROM project_generations WHERE project_id=? ORDER BY generation",
            (pid,)).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.Error:
        return []
    finally:
        db.close()


def current_generation(memory_path: str | Path, project_id: str) -> dict[str, Any] | None:
    rows = list_generations(memory_path, project_id)
    active = [r for r in rows if r.get("state") == "active"]
    if active:
        return max(active, key=lambda r: int(r.get("generation") or 0))
    if rows:
        return max(rows, key=lambda r: int(r.get("generation") or 0))
    return None


def current_generation_number(memory_path: str | Path, project_id: str) -> int:
    cur = current_generation(memory_path, project_id)
    return int(cur.get("generation") or 0) if cur else 0


def generation_input_version(base_version: str, generation: int) -> str:
    base = str(base_version or "").strip()
    if base:
        return f"{base}#gen{int(generation)}"
    return f"gen{int(generation)}"


_MODE_LABELS = {
    "replan": "計画を作り直す",
    "rerun": "実行をやり直す",
    "fresh": "内容から再出発",
    "restore": "旧世代へ復元",
}
_START_STATE_TEXT = {
    "replan": "新世代を開始しました。計画は生成待ちです。目標・原本は残しています。",
    "rerun": "新世代を開始しました。工程は未実行です。計画草案は残しています。",
    "fresh": "新世代を開始しました。原本・目標・計画の現行参照は空です。",
    "restore": "旧世代を復元しました。復元前の現行は別退避に残しています。",
}


def generation_overview(memory_path: str | Path, project_id: str) -> dict[str, Any]:
    """概要カード用の世代表示。読み取り専用。失敗しても空の安全な値を返す。"""
    pid = str(project_id)
    empty = {
        "generation": 0, "mode": "", "mode_label": "", "state": "",
        "backup_id": "", "input_version": "", "started": False,
        "start_state": "世代切替はまだありません",
        "previous_backup_id": "", "restore_hint": "",
    }
    try:
        cur = current_generation(memory_path, pid)
        rows = list_generations(memory_path, pid)
    except Exception:
        return dict(empty)
    if cur is None:
        return empty
    mode = str(cur.get("mode") or "")
    state = str(cur.get("state") or "")
    backup_id = str(cur.get("backup_id") or "")
    archived = [r for r in rows if r.get("state") == "archived"]
    prev = None
    if archived:
        prev = max(archived, key=lambda r: int(r.get("generation") or 0))
    prev_backup = str((prev or {}).get("backup_id") or "")
    restore_id = backup_id or prev_backup
    hint = ""
    if restore_id:
        hint = f"旧世代は退避 {restore_id} から復元できます"
    if state == "failed":
        start_state = "世代切替に失敗しました。成功と表示しません。" + (hint and (" " + hint) or "")
        started = False
    elif state == "initializing":
        start_state = "世代切替の途中です。完了するまで成功と表示しません。"
        started = False
    else:
        start_state = _START_STATE_TEXT.get(mode, "新世代を開始しました。")
        started = True
    return {
        "generation": int(cur.get("generation") or 0),
        "mode": mode,
        "mode_label": _MODE_LABELS.get(mode, mode),
        "state": state,
        "backup_id": backup_id,
        "input_version": str(cur.get("input_version") or ""),
        "started": started,
        "start_state": start_state,
        "previous_backup_id": prev_backup,
        "restore_hint": hint,
    }


def _project_name_and_mission(memory_path: str | Path, pid: str) -> tuple[str, dict]:
    name = ""
    mission: dict = {}
    mem = Path(memory_path)
    if not mem.exists():
        return name, mission
    try:
        db = sqlite3.connect(f"file:{mem.as_posix()}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        try:
            try:
                r = db.execute("SELECT name FROM projects WHERE id=?", (pid,)).fetchone()
                if r:
                    name = str(r["name"] or "")
            except sqlite3.Error:
                pass
            try:
                m = db.execute("SELECT * FROM project_missions WHERE project_id=?",
                               (pid,)).fetchone()
                if m:
                    mission = dict(m)
            except sqlite3.Error:
                mission = {}
        finally:
            db.close()
    except sqlite3.Error:
        pass
    return name, mission


def _state_fingerprint(memory_path: str | Path, pid: str,
                       workspace_root: str | Path | None = None) -> str:
    """preview token に結び付ける状態指紋。版・世代・対象件数を含む。"""
    _, mission = _project_name_and_mission(memory_path, pid)
    plan_version = int(mission.get("plan_version") or 0) if mission else 0
    gen = current_generation_number(memory_path, pid)
    try:
        counts = count_all(memory_path, pid, workspace_root)
    except Exception:
        counts = {}
    total = sum(int(v) for v in counts.values())
    raw = json.dumps({"pid": pid, "plan_version": plan_version,
                      "generation": gen, "total": total,
                      "counts": sorted(counts.items())},
                     ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ----------------------------------------------------------------------------
# 停止条件 (L1再利用 + 未確定OCR・送信待ちの付加検査)
# ----------------------------------------------------------------------------

def find_unconfirmed_ocr(memory_path: str | Path, project_id: str) -> list[dict[str, Any]]:
    """未確定OCR: 終端外ステータス、needs_review、passed+未承認を未確定とする。"""
    from app.ocr_store import TERMINAL_STATUSES
    pid = str(project_id)
    mem = Path(memory_path)
    if not mem.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        db = sqlite3.connect(f"file:{mem.as_posix()}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        try:
            try:
                names = {r[0] for r in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            except sqlite3.Error:
                return []
            if "ocr_runs" not in names:
                return []
            try:
                rows = db.execute(
                    "SELECT run_id, status, adoption_status FROM ocr_runs "
                    "WHERE project_id=?", (pid,)).fetchall()
            except sqlite3.Error:
                return []
            for r in rows:
                status = str(r["status"] or "")
                adoption = str(r["adoption_status"] or "unapproved")
                if status not in TERMINAL_STATUSES:
                    out.append({"run_id": r["run_id"], "status": status,
                                "reason": "終端外ステータス"})
                elif status == "needs_review":
                    out.append({"run_id": r["run_id"], "status": status,
                                "reason": "人の確認待ち"})
                elif status == "passed" and adoption not in ("approved", "published"):
                    out.append({"run_id": r["run_id"], "status": status,
                                "adoption_status": adoption,
                                "reason": "承認・公開が未確定"})
        finally:
            db.close()
    except sqlite3.Error:
        return []
    return out


def find_pending_external(memory_path: str | Path, project_id: str) -> list[dict[str, Any]]:
    """送信待ち・公開待ちの外部操作。実行済み・公開済みの証拠は対象外。"""
    pid = str(project_id)
    mem = Path(memory_path)
    if not mem.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        db = sqlite3.connect(f"file:{mem.as_posix()}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        try:
            try:
                names = {r[0] for r in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            except sqlite3.Error:
                return []
            if "project_actions" in names:
                try:
                    for r in db.execute(
                            "SELECT id, kind, status FROM project_actions WHERE project_id=? "
                            "AND status IN ('pending_approval','approved')", (pid,)).fetchall():
                        out.append({"store": "project_actions", "ref": r["id"],
                                    "kind": str(r["kind"] or ""),
                                    "status": str(r["status"] or "")})
                except sqlite3.Error:
                    pass
            if "premarketing_campaigns" in names:
                try:
                    cols = {r[1] for r in db.execute(
                        "PRAGMA table_info(premarketing_campaigns)").fetchall()}
                    rows = db.execute(
                        "SELECT * FROM premarketing_campaigns WHERE project_id=?",
                        (pid,)).fetchall()
                    for row in rows:
                        item = dict(row)
                        pub = str(item.get("publication_status") or "")
                        site = str(item.get("site_publication_status") or "")
                        if pub in ("awaiting_approval", "approved", "publishing_form",
                                   "awaiting_site"):
                            out.append({"store": "premarketing_campaigns",
                                        "ref": item.get("id", ""),
                                        "kind": f"publication:{pub}"})
                        if site in ("awaiting_approval", "approved", "generating",
                                    "review_ready"):
                            out.append({"store": "premarketing_campaigns",
                                        "ref": item.get("id", ""),
                                        "kind": f"site:{site}"})
                        _ = cols
                except sqlite3.Error:
                    pass
            if "campaign_social_shares" in names:
                try:
                    for r in db.execute(
                            "SELECT campaign_id, channel, status FROM campaign_social_shares "
                            "WHERE project_id=? AND status IN "
                            "('awaiting_approval','approved')", (pid,)).fetchall():
                        out.append({"store": "campaign_social_shares",
                                    "ref": f"{r['campaign_id']}/{r['channel']}",
                                    "kind": "sns_pending",
                                    "status": str(r["status"] or "")})
                except sqlite3.Error:
                    pass
        finally:
            db.close()
    except sqlite3.Error:
        return []
    return out


def check_stop_conditions(memory_path: str | Path, project_id: str,
                          executions: dict | None = None) -> dict[str, Any]:
    from app.project_lifecycle_registry import collect_external_items, has_running_jobs
    pid = str(project_id)
    jobs = has_running_jobs(memory_path, pid, executions)
    unconfirmed = find_unconfirmed_ocr(memory_path, pid)
    pending = find_pending_external(memory_path, pid)
    externals = collect_external_items(memory_path, pid)
    reasons: list[str] = []
    if jobs.get("has_running"):
        reasons.append("実行中ジョブあり。停止/取消後に再実行してください")
    if unconfirmed:
        reasons.append(f"未確定OCRあり({len(unconfirmed)}件)。確定・承認後に再実行してください")
    if pending:
        reasons.append(f"送信待ち・公開待ちの外部操作あり({len(pending)}件)。整理後に再実行してください")
    return {"ok": not reasons, "reasons": reasons,
            "running_jobs": jobs, "unconfirmed_ocr": unconfirmed,
            "pending_external": pending, "external_items": externals}


# ----------------------------------------------------------------------------
# モードごとの残す/初期化する対象
# ----------------------------------------------------------------------------

MODE_KEEP_TEXT = {
    "replan": ["PJ設定", "登録原本", "固定コンテキスト", "目標・達成条件", "旧版の監査履歴"],
    "rerun": ["PJ設定", "原本", "目標", "計画草案", "旧実行履歴"],
    "fresh": ["PJのID・名前"],
}
MODE_RESET_TEXT = {
    "replan": ["現行の計画", "工程状態", "計画評価キュー", "修正案"],
    "rerun": ["工程の実行状態", "新世代の成果物", "計画・成果物契約・外部検証の再確認"],
    "fresh": ["原本", "固定コンテキスト", "目標", "計画", "実行", "結果の現行参照"],
}
MODE_RESTORE_TEXT = {
    "replan": "退避から旧世代を復元できます（原本・目標・履歴は不変）",
    "rerun": "退避から旧世代を復元できます（外部実行証拠は旧世代の監査に残ります）",
    "fresh": "退避から旧世代を復元できます（ID・名前以外の現行参照を戻します）",
}

# 現行参照を空にする対象 (レジストリ名)。世代テーブル自体は対象外。
_REPLAN_CLEAR = {
    "main:project_tasks",
    "main:detailed_plans", "main:detailed_steps", "main:detailed_reviews",
    "main:detailed_budgets",
    "main:task_attempts", "main:execution_budgets",
    "main:source_versions", "main:source_units", "main:task_contract_extensions",
    "main:capability_checks", "main:failure_records", "main:presentation_manifests",
    "main:validation_runs", "main:recovery_attempts", "main:claims",
    "main:claim_evidence_links",
    "goal_completion:plan_coverage", "goal_completion:completion_evaluations",
    "goal_completion:nac_executions",
    "experience:retrievals", "experience:candidate_events",
    "experience:experience_index_state",
}
_RERUN_CLEAR = {
    "main:detailed_reviews",
    "main:task_attempts", "main:validation_runs", "main:failure_records",
    "main:presentation_manifests", "main:recovery_attempts",
    "main:claims", "main:claim_evidence_links",
    "goal_completion:completion_evaluations", "goal_completion:goal_states",
    "goal_completion:goal_state_events", "goal_completion:human_acceptances",
    "goal_completion:nac_executions",
    "experience:retrievals", "experience:candidate_events",
}
_FRESH_CLEAR_ALL = True  # projects(行は保持)・events・世代以外を全て空にする


def _entry_by_name(name: str):
    for e in REGISTRY:
        if e.name == name:
            return e
    return None


def _open_rw(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(db_path), timeout=30)
    db.row_factory = sqlite3.Row
    return db


def _delete_entry_rows(memory_path: str | Path, entry_name: str, pid: str,
                       kinds: list[str] | None = None) -> int:
    """指定エントリの当該PJ行を現行から外す。削除件数を返す。"""
    from app.project_lifecycle_registry import resolve_db_path
    entry = _entry_by_name(entry_name)
    if entry is None or entry.kind in ("directory", "other"):
        return 0
    if entry.db_label == "generation":
        return 0
    db_path = resolve_db_path(memory_path, entry.db_label)
    if db_path is None or not db_path.exists():
        return 0
    # goal_reviews:reviews は kind 絞り込みに対応する。
    if entry.table == "reviews" and entry.db_label == "goal_reviews" and kinds:
        db = _open_rw(db_path)
        try:
            if not _table_exists(db, "reviews"):
                return 0
            marks = ",".join("?" for _ in kinds)
            cur = db.execute(
                f"DELETE FROM reviews WHERE project=? AND kind IN ({marks})",
                (pid, *kinds))
            db.commit()
            return cur.rowcount or 0
        finally:
            db.close()
    db = _open_rw(db_path)
    try:
        if not entry.table or not _table_exists(db, entry.table):
            return 0
        try:
            cols = {r[1] for r in db.execute(
                f"PRAGMA table_info({entry.table})").fetchall()}
        except sqlite3.Error:
            return 0
        removed = 0
        if entry.project_column and entry.project_column in cols:
            cur = db.execute(
                f"DELETE FROM {entry.table} WHERE {entry.project_column}=?", (pid,))
            removed += cur.rowcount or 0
        elif entry.table in ("ocr_pages", "ocr_blocks", "ocr_fields",
                             "ocr_validations", "ocr_reviews"):
            if "ocr_runs" not in {r[0] for r in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}:
                db.commit()
                return 0
            if "run_id" not in cols:
                db.commit()
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
        db.commit()
        return removed
    finally:
        db.close()


def _reset_tasks_for_rerun(memory_path: str | Path, pid: str) -> int:
    mem = Path(memory_path)
    if not mem.exists():
        return 0
    db = _open_rw(mem)
    try:
        if not _table_exists(db, "project_tasks"):
            return 0
        cur = db.execute(
            "UPDATE project_tasks SET status='pending', result='', error='', "
            "attempts=0, started_at=NULL, completed_at=NULL WHERE project_id=?",
            (pid,))
        db.commit()
        return cur.rowcount or 0
    finally:
        db.close()


def _reset_mission_for_mode(memory_path: str | Path, pid: str, mode: str) -> None:
    mem = Path(memory_path)
    if not mem.exists():
        return
    db = _open_rw(mem)
    try:
        if not _table_exists(db, "project_missions"):
            return
        now = _utcnow()
        if mode == "replan":
            db.execute("UPDATE project_missions SET status='draft', plan_summary='', "
                       "final_report='', updated_at=? WHERE project_id=?", (now, pid))
        elif mode == "rerun":
            db.execute("UPDATE project_missions SET status='paused', final_report='', "
                       "updated_at=? WHERE project_id=?", (now, pid))
        elif mode == "fresh":
            db.execute("DELETE FROM project_missions WHERE project_id=?", (pid,))
        db.commit()
    finally:
        db.close()


def _clear_projects_context(memory_path: str | Path, pid: str) -> None:
    mem = Path(memory_path)
    if not mem.exists():
        return
    db = _open_rw(mem)
    try:
        if _table_exists(db, "projects"):
            db.execute("UPDATE projects SET context_text='', updated_at=? WHERE id=?",
                       (_utcnow(), pid))
            db.commit()
    finally:
        db.close()


def _clear_workspace_dir(memory_path: str | Path, pid: str,
                         workspace_root: str | Path | None) -> int:
    from app.project_lifecycle_registry import resolve_workspace_dir
    import shutil
    if workspace_root is None:
        return 0
    mem = Path(memory_path)
    configured = ""
    if mem.exists():
        try:
            db = sqlite3.connect(f"file:{mem.as_posix()}?mode=ro", uri=True)
            try:
                db.row_factory = sqlite3.Row
                try:
                    r = db.execute("SELECT workspace_path FROM projects WHERE id=?",
                                   (pid,)).fetchone()
                    if r:
                        configured = str(r["workspace_path"] or "")
                except sqlite3.Error:
                    pass
            finally:
                db.close()
        except sqlite3.Error:
            pass
    d = resolve_workspace_dir(memory_path, pid, workspace_root, configured)
    if d is None or not d.exists():
        return 0
    removed = 0
    for child in sorted(d.iterdir()):
        try:
            if child.is_file() or child.is_symlink():
                child.unlink()
                removed += 1
            elif child.is_dir():
                shutil.rmtree(child)
                removed += 1
        except OSError:
            continue
    return removed


def switch_to_new_generation(memory_path: str | Path, pid: str, mode: str,
                             workspace_root: str | Path | None = None) -> dict[str, Any]:
    """モードごとの現行参照の切り替え。退避済みであることが前提。"""
    if FAIL_INJECT and FAIL_INJECT.get("stage") == "switch":
        raise RuntimeError("injected switch failure")
    cleared: dict[str, int] = {}
    if mode == "replan":
        for name in sorted(_REPLAN_CLEAR):
            cleared[name] = _delete_entry_rows(memory_path, name, pid)
        # 評価キュー・修正案だけを外し、旧版の監査(reviews=plan/result等)は残す。
        cleared["goal_reviews:reviews(plan_queue,feedback,revision)"] = _delete_entry_rows(
            memory_path, "goal_reviews:reviews", pid,
            kinds=["plan_queue", "feedback", "revision"])
        _reset_mission_for_mode(memory_path, pid, mode)
    elif mode == "rerun":
        reset_tasks = _reset_tasks_for_rerun(memory_path, pid)
        cleared["main:project_tasks(reset)"] = reset_tasks
        for name in sorted(_RERUN_CLEAR):
            cleared[name] = _delete_entry_rows(memory_path, name, pid)
        # 計画・成果物契約・外部検証は再確認待ちにする: result系の現行参照を外す。
        cleared["goal_reviews:reviews(result)"] = _delete_entry_rows(
            memory_path, "goal_reviews:reviews", pid, kinds=["result"])
        _reset_mission_for_mode(memory_path, pid, mode)
    elif mode == "fresh":
        for entry in REGISTRY:
            if entry.db_label == "generation":
                continue
            if entry.kind in ("directory", "other"):
                continue
            if entry.db_label == "main" and entry.table in ("projects",):
                continue
            if entry.db_label == "main" and entry.table in ("project_events",):
                continue
            cleared[entry.name] = _delete_entry_rows(memory_path, entry.name, pid)
        # goal_reviews は全 kind を外す (監査は退避に残る)。
        cleared["goal_reviews:reviews(all)"] = _delete_entry_rows(
            memory_path, "goal_reviews:reviews", pid)
        _reset_mission_for_mode(memory_path, pid, mode)
        _clear_projects_context(memory_path, pid)
        cleared["workspace:project_dir"] = _clear_workspace_dir(
            memory_path, pid, workspace_root)
    else:
        raise ValueError(f"unknown mode: {mode}")
    return cleared


# ----------------------------------------------------------------------------
# プレビュー (読み取り専用)
# ----------------------------------------------------------------------------

def build_reset_preview(memory_path: str | Path, project_id: str, mode: str,
                        workspace_root: str | Path | None = None,
                        executions: dict | None = None) -> dict[str, Any]:
    pid = str(project_id)
    if mode not in MODES:
        raise ValueError(f"unknown mode: {mode}")
    name, mission = _project_name_and_mission(memory_path, pid)
    exists = bool(name) or bool(mission) or current_generation_number(memory_path, pid) > 0
    if not exists:
        # projects 行の有無で最終判定する。
        mem = Path(memory_path)
        if mem.exists():
            try:
                db = sqlite3.connect(f"file:{mem.as_posix()}?mode=ro", uri=True)
                try:
                    try:
                        r = db.execute("SELECT 1 FROM projects WHERE id=?",
                                       (pid,)).fetchone()
                        exists = r is not None
                    except sqlite3.Error:
                        exists = False
                finally:
                    db.close()
            except sqlite3.Error:
                exists = False
    try:
        counts = count_all(memory_path, pid, workspace_root)
    except Exception:
        counts = {}
    stops = check_stop_conditions(memory_path, pid, executions)
    gen = current_generation_number(memory_path, pid)
    fingerprint = _state_fingerprint(memory_path, pid, workspace_root)
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with _token_lock:
        _tokens[token_hash] = {"project_id": pid, "mode": mode,
                               "fingerprint": fingerprint,
                               "project_name": name,
                               "expires": time.time() + PREVIEW_TOKEN_TTL_SEC,
                               "created_at": _utcnow()}
    is_default = (pid == DEFAULT_PROJECT_ID)
    blocked = list(stops["reasons"])
    if is_default:
        blocked.append("既定PJは初期化できません")
    if not exists:
        blocked.append("PJが存在しません")
    return {
        "format": GENERATION_FORMAT,
        "project_id": pid, "project_name": name, "mode": mode,
        "project_exists": exists,
        "is_default_project": is_default,
        "generation": gen,
        "plan_version": int((mission or {}).get("plan_version") or 0),
        "keep": MODE_KEEP_TEXT[mode], "reset": MODE_RESET_TEXT[mode],
        "restore": MODE_RESTORE_TEXT[mode],
        "counts": counts,
        "total_rows": sum(int(v) for v in counts.values()),
        "stops": stops,
        "blocked_reasons": blocked,
        "can_initialize": not blocked,
        "preview_token": token,
        "token_expires_in_sec": PREVIEW_TOKEN_TTL_SEC,
        "read_only": True,
    }


def _verify_preview_token(pid: str, mode: str, token: str,
                          project_name: str, memory_path: str | Path,
                          workspace_root: str | Path | None = None) -> dict[str, Any]:
    if not token:
        return {"ok": False, "reason": "preview token is required"}
    token_hash = hashlib.sha256(str(token).encode()).hexdigest()
    with _token_lock:
        row = _tokens.get(token_hash)
    if row is None:
        return {"ok": False, "reason": "preview token が無効です"}
    if row.get("project_id") != pid or row.get("mode") != mode:
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


# ----------------------------------------------------------------------------
# 初期化
# ----------------------------------------------------------------------------

def _find_by_idempotency(memory_path: str | Path, pid: str, key: str) -> dict | None:
    if not key:
        return None
    db = _connect(memory_path)
    try:
        rows = db.execute(
            "SELECT * FROM project_generations WHERE project_id=? AND idempotency_key=? "
            "ORDER BY generation DESC", (pid, key)).fetchall()
        return dict(rows[0]) if rows else None
    finally:
        db.close()


def initialize_project(memory_path: str | Path, project_id: str, mode: str, *,
                       preview_token: str, project_name: str,
                       actor: str = "", reason: str = "",
                       idempotency_key: str = "",
                       workspace_root: str | Path | None = None,
                       backup_root: str | Path | None = None,
                       executions: dict | None = None) -> dict[str, Any]:
    from app.project_lifecycle_backup import create_backup, verify_backup
    pid = str(project_id)
    actor_name = str(actor or "").strip()
    if mode not in MODES:
        return {"ok": False, "reason": f"unknown mode: {mode}"}
    if not actor_name:
        return {"ok": False, "reason": "actor is required"}
    if pid == DEFAULT_PROJECT_ID:
        return {"ok": False, "reason": "既定PJは初期化できません"}
    tok = _verify_preview_token(pid, mode, preview_token, project_name,
                                memory_path, workspace_root)
    if not tok.get("ok"):
        return {"ok": False, "reason": tok.get("reason")}
    stops = check_stop_conditions(memory_path, pid, executions)
    if not stops.get("ok"):
        return {"ok": False, "reason": "; ".join(stops["reasons"]),
                "stops": stops}
    key = str(idempotency_key or "").strip()
    if key:
        existing = _find_by_idempotency(memory_path, pid, key)
        if existing is not None:
            return {"ok": True, "idempotent": True,
                    "generation": existing,
                    "note": "同じ冪等キーによる再送のため再実行しません"}
    lock = _lease_for(pid)
    if not lock.acquire(blocking=False):
        return {"ok": False, "reason": "同じPJの初期化を実行中です",
                "code": "LEASE_BUSY"}
    try:
        if key:
            existing = _find_by_idempotency(memory_path, pid, key)
            if existing is not None:
                return {"ok": True, "idempotent": True,
                        "generation": existing,
                        "note": "同じ冪等キーによる再送のため再実行しません"}
        if FAIL_INJECT and FAIL_INJECT.get("stage") == "backup":
            backup = {"ok": False, "reason": "injected backup failure"}
        else:
            if backup_root is None:
                backup = {"ok": False, "reason": "backup_root is required"}
            else:
                backup = create_backup(memory_path, pid, backup_root,
                                       workspace_root=workspace_root,
                                       actor=actor_name)
        if not backup.get("ok"):
            return {"ok": False, "reason": f"退避に失敗したため何も変更しません: {backup.get('reason')}",
                    "backup": backup}
        check = verify_backup(backup["manifest_path"])
        if not check.get("ok"):
            return {"ok": False, "reason": "退避の検証に失敗したため何も変更しません",
                    "verify": check, "backup": backup}
        prev_gen = current_generation_number(memory_path, pid)
        new_gen_no = prev_gen + 1
        # 新世代の input_version は旧世代と異なる値にする。
        try:
            base_fp = _state_fingerprint(memory_path, pid, workspace_root)
        except Exception:
            base_fp = ""
        new_input_version = generation_input_version(base_fp, new_gen_no)
        gid = f"gen_{pid}_{new_gen_no}_{uuid.uuid4().hex[:8]}"
        db = _connect(memory_path)
        try:
            with db:
                db.execute("INSERT INTO project_generations(id, project_id, generation, mode,"
                           " actor, reason, backup_id, manifest_sha256, input_version, state,"
                           " idempotency_key, created_at, updated_at)"
                           " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (gid, pid, new_gen_no, mode, actor_name, str(reason or "")[:2000],
                            str(backup.get("backup_id") or ""),
                            str(backup.get("manifest_sha256") or ""),
                            new_input_version, "initializing", key,
                            _utcnow(), _utcnow()))
                # 旧 active を archived にする (新世代が確定するまで旧世代を失わない)。
                db.execute("UPDATE project_generations SET state='archived', updated_at=? "
                           "WHERE project_id=? AND generation<? AND state='active'",
                           (_utcnow(), pid, new_gen_no))
        finally:
            db.close()
        if FAIL_INJECT and FAIL_INJECT.get("stage") == "record":
            raise RuntimeError("injected record failure")
        try:
            cleared = switch_to_new_generation(memory_path, pid, mode, workspace_root)
        except Exception as exc:
            db = _connect(memory_path)
            try:
                with db:
                    db.execute("UPDATE project_generations SET state='failed', updated_at=? "
                               "WHERE id=?", (_utcnow(), gid))
            finally:
                db.close()
            return {"ok": False, "reason": f"切り替えに失敗しました: {exc}",
                    "generation_id": gid, "generation": new_gen_no,
                    "backup_id": backup.get("backup_id"),
                    "restore_hint": "退避IDから restore-generation で戻せます",
                    "code": "SWITCH_FAILED"}
        # 世代確定。トークンは使い捨てにする。
        token_hash = hashlib.sha256(str(preview_token).encode()).hexdigest()
        with _token_lock:
            _tokens.pop(token_hash, None)
        db = _connect(memory_path)
        try:
            with db:
                db.execute("UPDATE project_generations SET state='active', updated_at=? "
                           "WHERE id=?", (_utcnow(), gid))
        finally:
            db.close()
        try:
            from app.memory.short_term import ShortTermMemory
            mem = ShortTermMemory(Path(memory_path))
            mem.add_event(pid, "generation_initialized",
                          f"PJ初期化を実行: mode={mode} 世代={new_gen_no}",
                          detail=json.dumps({"generation_id": gid, "mode": mode,
                                             "backup_id": backup.get("backup_id"),
                                             "input_version": new_input_version},
                                            ensure_ascii=False))
        except Exception:
            pass
        return {"ok": True, "generation_id": gid, "generation": new_gen_no,
                "mode": mode, "backup_id": backup.get("backup_id"),
                "manifest_sha256": backup.get("manifest_sha256"),
                "input_version": new_input_version,
                "cleared": cleared,
                "note": "外部アクションは自動再実行しません。旧世代の証拠は退避に残ります"}
    finally:
        try:
            lock.release()
        except RuntimeError:
            pass


# ----------------------------------------------------------------------------
# 復元
# ----------------------------------------------------------------------------

def restore_generation(memory_path: str | Path, project_id: str, backup_id: str, *,
                       actor: str = "", idempotency_key: str = "",
                       workspace_root: str | Path | None = None,
                       backup_root: str | Path | None = None) -> dict[str, Any]:
    from app.project_lifecycle_backup import create_backup, verify_backup
    pid = str(project_id)
    actor_name = str(actor or "").strip()
    if not actor_name:
        return {"ok": False, "reason": "actor is required"}
    if pid == DEFAULT_PROJECT_ID:
        return {"ok": False, "reason": "既定PJは復元できません"}
    safe = "".join(ch if (ch.isalnum() or ch in ("-", "_")) else "_" for ch in str(backup_id))[:128]
    if not safe or safe != str(backup_id):
        return {"ok": False, "reason": "invalid backup_id"}
    if backup_root is None:
        return {"ok": False, "reason": "backup_root is required"}
    manifest_path = Path(backup_root) / safe / "manifest.json"
    if not manifest_path.is_file():
        return {"ok": False, "reason": "backup not found"}
    check = verify_backup(manifest_path)
    if not check.get("ok"):
        return {"ok": False, "reason": "backup verification failed",
                "failures": check.get("failures")}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"ok": False, "reason": f"manifest unreadable: {exc}"}
    if str(manifest.get("project_id") or "") != pid:
        return {"ok": False, "reason": "backup belongs to another project"}
    key = str(idempotency_key or "").strip()
    restore_key = f"restore:{safe}:{key}" if key else f"restore:{safe}"
    existing = _find_by_idempotency(memory_path, pid, restore_key)
    if existing is not None and existing.get("state") == "active":
        # 冪等: 既に同じ退避へ戻してあれば再実行しない。
        return {"ok": True, "idempotent": True, "generation": existing,
                "note": "同じ退避への復元済みのため再実行しません"}
    lock = _lease_for(pid)
    if not lock.acquire(blocking=False):
        return {"ok": False, "reason": "同じPJの初期化・復元を実行中です",
                "code": "LEASE_BUSY"}
    try:
        existing = _find_by_idempotency(memory_path, pid, restore_key)
        if existing is not None and existing.get("state") == "active":
            return {"ok": True, "idempotent": True, "generation": existing,
                    "note": "同じ退避への復元済みのため再実行しません"}
        from app.project_lifecycle_registry import has_running_jobs
        jobs = has_running_jobs(memory_path, pid, None)
        if jobs.get("has_running"):
            return {"ok": False, "reason": "実行中ジョブがあるため復元を拒否します"}
        # 戻す前に現行世代も退避する (復元で現行世代を失わない)。
        cur_backup = create_backup(memory_path, pid, backup_root,
                                   workspace_root=workspace_root, actor=actor_name)
        if not cur_backup.get("ok"):
            return {"ok": False, "reason": f"現行世代の退避に失敗したため復元しません: {cur_backup.get('reason')}"}
        cur_check = verify_backup(cur_backup["manifest_path"])
        if not cur_check.get("ok"):
            return {"ok": False, "reason": "現行世代の退避検証に失敗したため復元しません"}
        # 世代履歴は append-only のため退避しておく。
        gen_rows = list_generations(memory_path, pid)
        # 復元前のクリアは行削除に留め、テーブル自体は残す (遅延作成テーブルの
        # schema が退避に無い場合に DROP すると戻せなくなる)。
        _clear_for_restore(memory_path, pid, workspace_root)
        # 世代テーブルを除外して復元する: manifest の世代エントリを一時的に外す。
        # L1 restore は別ターゲット復元の衝突拒否が前提のため、同一PJへ戻す L2 復元
        # では内部の上書き復元経路を使う (L1 と同じ verify・件数照合つき)。
        # 復元後にもとの世代履歴を戻す (世代は退避対象だが履歴は append-only)。
        result = _overwrite_restore(manifest_path, Path(memory_path),
                                    workspace_root=workspace_root)
        # 世代履歴を復旧する。
        db = _connect(memory_path)
        try:
            with db:
                for row in gen_rows:
                    db.execute("INSERT OR REPLACE INTO project_generations(id, project_id,"
                               " generation, mode, actor, reason, backup_id, manifest_sha256,"
                               " input_version, state, idempotency_key, created_at, updated_at)"
                               " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                               (row["id"], row["project_id"], row["generation"], row["mode"],
                                row.get("actor", ""), row.get("reason", ""),
                                row.get("backup_id", ""), row.get("manifest_sha256", ""),
                                row.get("input_version", ""), row.get("state", "archived"),
                                row.get("idempotency_key", ""), row.get("created_at", _utcnow()),
                                row.get("updated_at", _utcnow())))
        finally:
            db.close()
        if not result.get("ok"):
            return {"ok": False, "reason": "復元に失敗しました",
                    "errors": result.get("errors") or result.get("mismatches"),
                    "current_backup_id": cur_backup.get("backup_id"),
                    "note": "成功と表示しません。現行世代の退避から再開できます"}
        prev_gen = current_generation_number(memory_path, pid)
        new_gen_no = prev_gen + 1
        gid = f"gen_{pid}_{new_gen_no}_{uuid.uuid4().hex[:8]}"
        db = _connect(memory_path)
        try:
            with db:
                db.execute("INSERT INTO project_generations(id, project_id, generation, mode,"
                           " actor, reason, backup_id, manifest_sha256, input_version, state,"
                           " idempotency_key, created_at, updated_at)"
                           " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (gid, pid, new_gen_no, "restore", actor_name,
                            f"restore {safe}", safe,
                            hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
                            "", "active", restore_key, _utcnow(), _utcnow()))
                db.execute("UPDATE project_generations SET state='archived', updated_at=? "
                           "WHERE project_id=? AND generation<? AND state='active' AND id<>?",
                           (_utcnow(), pid, new_gen_no, gid))
        finally:
            db.close()
        try:
            from app.memory.short_term import ShortTermMemory
            mem = ShortTermMemory(Path(memory_path))
            mem.add_event(pid, "generation_restored",
                          f"旧世代へ復元: backup={safe} 世代={new_gen_no}",
                          detail=json.dumps({"generation_id": gid, "backup_id": safe,
                                             "current_backup_id": cur_backup.get("backup_id")},
                                            ensure_ascii=False))
        except Exception:
            pass
        return {"ok": True, "generation_id": gid, "generation": new_gen_no,
                "restored_backup_id": safe,
                "current_backup_id": cur_backup.get("backup_id"),
                "restored_rows": result.get("restored_rows"),
                "note": "復元前に現行世代を退避しました。再復元は冪等です"}
    finally:
        try:
            lock.release()
        except RuntimeError:
            pass


def _decode_backup_value(value: Any) -> Any:
    import base64
    if isinstance(value, dict) and set(value.keys()) == {"$bytes"}:
        return base64.b64decode(value["$bytes"])
    return value


def _overwrite_restore(manifest_path: str | Path, memory_path: str | Path,
                       workspace_root: str | Path | None = None) -> dict[str, Any]:
    """L1 restore と同じ検証・照合で、同一個所へ上書き復元する内部経路。

    L1 restore_backup は別ターゲット復元の衝突拒否が前提のため、L2 の世代復元
    (同一PJへ戻す) では使えない。ここでは L1 と同じ verify・件数・ハッシュ照合を
    行い、INSERT OR REPLACE で冪等に上書きする。部分失敗は成功と扱わない。
    """
    from app.project_lifecycle_backup import verify_backup
    check = verify_backup(manifest_path)
    if not check.get("ok"):
        return {"ok": False, "reason": "backup verification failed",
                "failures": check.get("failures")}
    mpath = Path(manifest_path)
    backup_dir = mpath.parent
    manifest = json.loads(mpath.read_text(encoding="utf-8"))
    pid = str(manifest["project_id"])
    target_mem = Path(memory_path)
    errors: list[str] = []
    restored = 0
    db_cache: dict[str, sqlite3.Connection] = {}
    try:
        try:
            target_mem.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return {"ok": False, "reason": f"target unavailable: {exc}"}
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
            schema = payload.get("schema", "")
            if not rows and not str(schema).strip():
                # 退避時に存在しなかった遅延作成テーブル: 戻す行が無いため何もしない。
                continue
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
                    db = sqlite3.connect(str(db_path), timeout=30)
                    db_cache[str(db_path)] = db
                with db:
                    if not _table_exists(db, table):
                        if not str(schema).strip():
                            raise ValueError(f"table missing and no schema: {table}")
                        db.execute(str(schema))
                    cols = {r[1] for r in db.execute(
                        f"PRAGMA table_info({table})").fetchall()}
                    for row in rows:
                        usable = {k: _decode_backup_value(v)
                                  for k, v in dict(row).items() if k in cols}
                        if not usable:
                            continue
                        names = ",".join(usable.keys())
                        marks = ",".join("?" for _ in usable)
                        db.execute(f"INSERT OR REPLACE INTO {table}({names}) "
                                   f"VALUES({marks})", tuple(usable.values()))
                restored += len(rows)
            except (sqlite3.Error, ValueError, TypeError) as exc:
                errors.append(f"{table}: restore failed: {exc}")
                continue
        # Workspace 上書き。
        import shutil
        ws_rel = str(manifest.get("workspace", {}).get("rel") or f"projects/{pid}")
        src_ws = backup_dir / "workspace" / manifest.get("workspace", {}).get(
            "rel", f"projects/{pid}")
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
    # 復元後検証: 件数が manifest と一致すること。
    # 退避時に存在しなかったテーブル (遅延作成テーブル等) は manifest に
    # schema・行が無いため検証対象外 (作らないのが正しい)。
    # project_events は追加型の監査履歴であり、初期化・復元自体が1件足すため
    # 件数完全一致の対象外とする (消さないことが要件。原本・目標・計画の
    # ハッシュ一致はテスト側で検証する)。
    from app.project_lifecycle_registry import count_entry
    manifest_counts = {e.get("name"): int(e.get("count", 0))
                       for e in manifest.get("entries", [])}
    mismatches: list[str] = []
    for e in manifest.get("entries", []):
        entry_def = _entry_by_name(e.get("name") or "")
        if entry_def is None or entry_def.kind in ("directory", "other"):
            continue
        if entry_def.db_label == "main" and entry_def.table == "project_events":
            continue
        try:
            got = count_entry(target_mem, entry_def, pid, workspace_root)
        except Exception as exc:
            mismatches.append(f"{e.get('name')}: count failed: {exc}")
            continue
        if got != int(e.get("count", 0)):
            mismatches.append(f"{e.get('name')}: manifest={e.get('count')} restored={got}")
    _ = manifest_counts
    if mismatches:
        return {"ok": False, "reason": "post-restore verification failed",
                "mismatches": mismatches, "restored_rows": restored}
    return {"ok": True, "project_id": pid, "restored_rows": restored, "verified": True}


def _clear_for_restore(memory_path: str | Path, pid: str,
                      workspace_root: str | Path | None = None) -> None:
    """復元前のクリア。行削除に留め、テーブル自体は残す。

    遅延作成テーブル (execution_budgets 等) は退避に schema が無い場合があり、
    DROP すると復元できなくなる。DELETE FROM で行だけ外す。
    """
    from app.project_lifecycle_registry import resolve_db_path
    for entry in REGISTRY:
        if entry.db_label == "generation":
            continue
        if entry.kind in ("directory", "other"):
            continue
        if entry.db_label == "main" and entry.table in ("projects", "project_events"):
            continue
        db_path = resolve_db_path(memory_path, entry.db_label)
        if entry.table == "reviews" and entry.db_label == "goal_reviews":
            _delete_entry_rows(memory_path, entry.name, pid)
            continue
        if db_path is None or not db_path.exists():
            continue
        db = _open_rw(db_path)
        try:
            if entry.table and _table_exists(db, entry.table):
                try:
                    cols = {r[1] for r in db.execute(
                        f"PRAGMA table_info({entry.table})").fetchall()}
                except sqlite3.Error:
                    continue
                try:
                    if entry.project_column and entry.project_column in cols:
                        db.execute(f"DELETE FROM {entry.table} "
                                   f"WHERE {entry.project_column}=?", (pid,))
                    elif entry.table in ("ocr_pages", "ocr_blocks", "ocr_fields",
                                         "ocr_validations", "ocr_reviews"):
                        if "run_id" in cols:
                            db.execute(
                                f"DELETE FROM {entry.table} WHERE run_id IN "
                                f"(SELECT run_id FROM ocr_runs WHERE project_id=?)",
                                (pid,))
                    elif entry.table in ("detailed_steps", "detailed_reviews"):
                        db.execute(
                            f"DELETE FROM {entry.table} WHERE plan_id IN "
                            f"(SELECT id FROM detailed_plans WHERE project_id=?)",
                            (pid,))
                    db.commit()
                except sqlite3.Error:
                    try:
                        db.rollback()
                    except sqlite3.Error:
                        pass
        finally:
            db.close()
    # vector_index は project 列で行削除したが、identity 主キーのため
    # 同一 identity+id の上書きは INSERT OR REPLACE で冪等に戻る。
    _clear_workspace_dir(memory_path, pid, workspace_root)


@contextmanager
def failure_injection(stage: str | None):
    global FAIL_INJECT
    prev = FAIL_INJECT
    FAIL_INJECT = {"stage": stage} if stage else None
    try:
        yield
    finally:
        FAIL_INJECT = prev
