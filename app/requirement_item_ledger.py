"""項目別検証台帳(サイドカーSQLite)。既存の台帳・永続化の流儀に合わせる。

- 追記のみ。最終検証レポートはこの台帳からのみ生成する。
- fail-closed: 読取不能・不明は別扱い。分母0で率を作らない。
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

LOGGER = logging.getLogger(__name__)


class LedgerReadError(RuntimeError):
    """台帳の読取失敗。『台帳が空』とは区別する(旧式追補へ迂回しない)。

    kind: 'connect_failed' | 'table_missing' | 'table_unreadable' | 'row_unreadable'
    原本本文は含めない。原因の種別・パスの種別・SQLiteのエラー種別だけ記録する。
    """

    def __init__(self, kind: str, *, path_kind: str = "",
                 sqlite_error: str = "", detail: str = "") -> None:
        super().__init__(f"ledger read failed: {kind}")
        self.kind = str(kind or "")
        self.path_kind = str(path_kind or "")
        self.sqlite_error = str(sqlite_error or "")
        self.detail = str(detail or "")[:500]


def classify_ledger_path_kind(raw: str | Path) -> str:
    """UNC/相対/ドライブ文字/絶対POSIXの種別。診断用のみ(本文は含めない)。"""
    text = str(raw or "")
    if re.match(r"^[\\/]{2}[^\\/]+[\\/][^\\/]+", text):
        return "unc"
    if re.match(r"^[A-Za-z]:[\\/]", text):
        return "drive"
    if text.startswith("/") or text.startswith("\\\\"):
        return "absolute_posix"
    return "relative"


def _quote_path_segment(text: str) -> str:
    # SQLite URIのパス部だけエンコードする。'?'・'#'・'%'・空白を正しく扱う。
    # '/' と ':'(ドライブ文字)は区切りとして残す。
    return quote(text, safe="/:")


def ledger_readonly_uri(db_path: str | Path) -> str:
    """台帳の読取専用URI。相対・ドライブ文字・UNCのいずれでも成立する。

    - 相対パス: 絶対化して file:///…?mode=ro にする(従来の相対解決を維持)。
    - Windows UNC (\\\\server\\share\\…): file:////server/share/…?mode=ro に変換する。
      authority として解釈される file://server 形を避け、SQLite が Windows 上で
      UNC として開ける4スラッシュ形にする。
    - ドライブ文字 (C:\\… / C:/…): file:///C:/…?mode=ro に変換する。
    """
    text = str(db_path or "")
    if text.startswith("file:"):
        return text
    normalized = text.replace("\\", "/")
    if re.match(r"^//[^/]+/[^/]+", normalized):
        # UNC: //server/share/… → file:////server/share/… (authorityを使わない)。
        return "file://" + _quote_path_segment(normalized) + "?mode=ro"
    if re.match(r"^[A-Za-z]:/", normalized):
        return "file:///" + _quote_path_segment(normalized) + "?mode=ro"
    resolved = Path(text).resolve(strict=False).as_posix()
    return "file:" + _quote_path_segment(resolved) + "?mode=ro"


@contextmanager
def _connect_readonly(db_path: Path):
    """読取専用接続。失敗は LedgerReadError にする(『空』に倒さない)。"""
    kind = classify_ledger_path_kind(db_path)
    try:
        db = sqlite3.connect(ledger_readonly_uri(db_path), uri=True, timeout=15)
        db.row_factory = sqlite3.Row
    except (OSError, sqlite3.Error, ValueError) as exc:
        raise LedgerReadError(
            "connect_failed", path_kind=kind,
            sqlite_error=_type_name(exc)) from exc
    try:
        yield db
    finally:
        try:
            db.close()
        except sqlite3.Error:
            pass


def _require_ledger_tables(db: sqlite3.Connection) -> None:
    try:
        names = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    except sqlite3.Error as exc:
        raise LedgerReadError(
            "table_unreadable", sqlite_error=_type_name(exc)) from exc
    if "requirement_item_ledger" not in names or "requirement_item_rows" not in names:
        raise LedgerReadError(
            "table_missing",
            detail="requirement_item_ledger/requirement_item_rows"
        ) from None


def open_ledger_readonly(memory_path: str | Path, project_id: str) -> dict | None:
    """監視・試験用の読み取り専用経路。PJの状態を書き換えない。

    ShortTermMemory を新規生成しないため、実行中PJの一時停止は起きない。
    戻り値は latest_ledger と同じ。台帳が無ければNone、読取失敗は例外。
    """
    return latest_ledger(memory_path, project_id)


def _type_name(exc: BaseException) -> str:
    try:
        return str(type(exc).__name__ or "Error")
    except Exception:
        return "Error"


def _ledger_failure_signature(kind: str, path_kind: str, sqlite_error: str) -> str:
    import hashlib

    raw = "|".join((str(kind or ""), str(path_kind or ""), str(sqlite_error or "")))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _stop_ledger_read_failed(
    memory, project_id: str, error: "LedgerReadError",
) -> dict:
    """台帳読取失敗は旧式追補へ迂回せず診断可能な異常停止にする。

    同じ失敗署名で重複タスクを作らない。復旧後は再開できる。
    戻り値は常に stopped=True とし、記録処理の成否も返す(追補ループへ戻らない)。
    """
    pid = str(project_id or "")
    recording_errors: list[dict[str, str]] = []

    def _recording_error(operation: str, exc: BaseException) -> None:
        recording_errors.append({"operation": operation, "error_type": _type_name(exc)})
        LOGGER.error("ledger read failure recording failed: %s: %s", operation, _type_name(exc))
    kind = str(getattr(error, "kind", "") or "unknown")
    path_kind = str(getattr(error, "path_kind", "") or "unknown")
    sqlite_error = str(getattr(error, "sqlite_error", "") or "unknown")
    signature = _ledger_failure_signature(kind, path_kind, sqlite_error)
    try:
        mission = memory.get_mission(pid, event_limit=500)
    except (OSError, ValueError, RuntimeError, AttributeError, KeyError, TypeError):
        mission = {"tasks": [], "events": []}
    for task in mission.get("tasks") or []:
        if not isinstance(task, dict):
            continue
        if str(task.get("task_key") or "") != f"ledger_read_failed_{signature}":
            continue
        if str(task.get("status") or "") not in {"failed", "blocked"}:
            return {"stopped": True, "recorded": True, "recording_errors": []}
    try:
        memory.add_event(
            pid, "ledger_read_failed",
            "項目別台帳の読取に失敗したため旧式追補へ迂回せず異常停止しました",
            None,
            detail=json.dumps({
                "failure_kind": kind,
                "path_kind": path_kind,
                "sqlite_error": sqlite_error,
                "signature": signature,
            }, ensure_ascii=False)[:2000],
        )
    except (OSError, ValueError, RuntimeError, AttributeError, KeyError, TypeError) as exc:
        _recording_error("add_event", exc)
    try:
        memory.append_plan_tasks(
            pid,
            [{
                "task_key": f"ledger_read_failed_{signature}",
                "depends_on": [],
                "title": "項目別台帳の読取失敗の診断",
                "description": (
                    "項目別台帳の読取に失敗しました。旧式追補へ迂回しません。"
                    "台帳ファイルとSQLiteの状態を確認し、復旧後に再開してください。"
                ),
                "acceptance_criteria": (
                    "項目別台帳が読み取れること。旧式追補タスクを作らないこと。"
                ),
                "mode": "local",
            }],
            "P0-3 ledger read failed: " + signature,
        )
    except (OSError, ValueError, RuntimeError, AttributeError, KeyError, TypeError) as exc:
        _recording_error("append_plan_tasks", exc)
    try:
        for task in memory.get_mission(pid, event_limit=50).get("tasks") or []:
            if isinstance(task, dict) and str(task.get("task_key") or "") == f"ledger_read_failed_{signature}":
                memory.update_task(
                    task["id"], "failed",
                    error=(
                        f"ledger read failed: kind={kind} "
                        f"path_kind={path_kind} sqlite_error={sqlite_error}"
                    )[:2000],
                )
                break
        memory.set_final_report(
            pid,
            "# 項目別台帳の読取失敗\n\n"
            "項目別台帳の読取に失敗したため、旧式追補へ迂回せず異常停止しました。\n"
            f"- 原因の種別: {kind}\n"
            f"- パスの種別: {path_kind}\n"
            f"- SQLiteのエラー種別: {sqlite_error}\n"
            f"- 失敗署名: {signature}\n",
            status="failed",
            message="項目別台帳の読取失敗のため異常停止しました",
        )
    except (OSError, ValueError, RuntimeError, AttributeError, KeyError, TypeError) as exc:
        _recording_error("update_task_or_set_final_report", exc)
    return {
        "stopped": True,
        "recorded": not recording_errors,
        "recording_errors": recording_errors,
    }


def item_ledger_db_path(memory_path: str | Path) -> Path:
    mem = Path(memory_path)
    return mem.with_name(mem.name + ".item_ledger.sqlite3")


_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(db_path: Path) -> threading.Lock:
    key = str(db_path)
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _locks[key] = lock
        return lock


def _ensure_tables(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS requirement_item_ledger(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id TEXT NOT NULL,
        plan_signature TEXT NOT NULL DEFAULT '',
        source_hash TEXT NOT NULL DEFAULT '',
        artifact_hashes_json TEXT NOT NULL DEFAULT '{}',
        criterion_verdicts_json TEXT NOT NULL DEFAULT '{}',
        ledger_digest TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS requirement_item_rows(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ledger_id INTEGER NOT NULL,
        project_id TEXT NOT NULL,
        item_id TEXT NOT NULL DEFAULT '',
        criterion_id TEXT NOT NULL DEFAULT '',
        artifact_path TEXT NOT NULL DEFAULT '',
        requirement TEXT NOT NULL DEFAULT '',
        method TEXT NOT NULL DEFAULT '',
        is_required INTEGER NOT NULL DEFAULT 1,
        result TEXT NOT NULL DEFAULT '',
        line INTEGER NOT NULL DEFAULT -1,
        quote TEXT NOT NULL DEFAULT '',
        reason TEXT NOT NULL DEFAULT '',
        expected_json TEXT NOT NULL DEFAULT '{}')""")
    columns = {row[1] for row in db.execute("PRAGMA table_info(requirement_item_rows)").fetchall()}
    if "expected_json" not in columns:
        db.execute("ALTER TABLE requirement_item_rows ADD COLUMN expected_json TEXT NOT NULL DEFAULT '{}'")
    db.execute("""CREATE TABLE IF NOT EXISTS requirement_item_confirmations(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ledger_id INTEGER NOT NULL,
        project_id TEXT NOT NULL,
        item_id TEXT NOT NULL DEFAULT '',
        criterion_id TEXT NOT NULL DEFAULT '',
        artifact_path TEXT NOT NULL DEFAULT '',
        artifact_hash TEXT NOT NULL DEFAULT '',
        plan_signature TEXT NOT NULL DEFAULT '',
        actor TEXT NOT NULL DEFAULT '',
        reason TEXT NOT NULL DEFAULT '',
        quote TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS requirement_repair_runs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id TEXT NOT NULL,
        item_id TEXT NOT NULL DEFAULT '',
        failure_kind TEXT NOT NULL DEFAULT '',
        failure_signature TEXT NOT NULL DEFAULT '',
        plan_signature TEXT NOT NULL DEFAULT '',
        source_hash TEXT NOT NULL DEFAULT '',
        artifact_path TEXT NOT NULL DEFAULT '',
        artifact_hash_before TEXT NOT NULL DEFAULT '',
        artifact_hash_after TEXT NOT NULL DEFAULT '',
        item_result_before TEXT NOT NULL DEFAULT '',
        item_result_after TEXT NOT NULL DEFAULT '',
        decision TEXT NOT NULL DEFAULT '',
        operator_action TEXT NOT NULL DEFAULT '',
        reason TEXT NOT NULL DEFAULT '',
        attempt INTEGER NOT NULL DEFAULT 1,
        created_at REAL NOT NULL)""")
    db.execute("""CREATE INDEX IF NOT EXISTS idx_item_ledger_project
        ON requirement_item_ledger(project_id, id DESC)""")
    db.execute("""CREATE INDEX IF NOT EXISTS idx_item_rows_ledger
        ON requirement_item_rows(ledger_id, item_id)""")
    db.execute("""CREATE INDEX IF NOT EXISTS idx_item_confirm_ledger
        ON requirement_item_confirmations(ledger_id, item_id)""")
    db.execute("""CREATE INDEX IF NOT EXISTS idx_repair_signature
        ON requirement_repair_runs(project_id, failure_signature)""")


@contextmanager
def _connect(memory_path: str | Path):
    path = item_ledger_db_path(memory_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=30)
    db.row_factory = sqlite3.Row
    try:
        with db:
            _ensure_tables(db)
        yield db
    finally:
        db.close()


def record_ledger(memory_path: str | Path, payload: dict) -> dict:
    """台帳を追記する。rowsは別テーブルへ1件ずつ。"""
    if not isinstance(payload, dict):
        raise ValueError("payload is required")
    project_id = str(payload.get("project_id") or "")
    if not project_id:
        raise ValueError("project_id is required")
    rows = payload.get("rows")
    if not isinstance(rows, list) or not rows:
        raise ValueError("rows are required")
    # C0: 台帳投入口でも item_id の重複を検出したら握りつぶさず失敗側に倒す。
    # 同一検証結果(payload)の二重投入だけを拒否する。異なる計画由来の
    # 行を束ねる既存試験は壊さない(同一IDでも内容が違えば別物として通す)。
    seen: dict[str, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        text = str(row.get("item_id") or "").strip()
        if not text:
            continue
        cid = str(row.get("criterion_id") or "").strip()
        key = f"{cid}\u0000{text}"
        signature = (
            str(row.get("criterion_id") or ""),
            str(row.get("artifact_path") or ""),
            str(row.get("requirement") or ""),
            str(row.get("method") or ""),
        )
        if key in seen and seen[key] == signature:
            raise ValueError(f"duplicate item_id in requirement_item_ledger: {text}")
        seen[key] = signature
    path = item_ledger_db_path(memory_path)
    lock = _lock_for(path)
    with lock:
        with _connect(memory_path) as db:
            cursor = db.execute(
                """INSERT INTO requirement_item_ledger(
                    project_id, plan_signature, source_hash, artifact_hashes_json,
                    criterion_verdicts_json, ledger_digest, created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (project_id, str(payload.get("plan_signature") or ""),
                 str(payload.get("source_hash") or ""),
                 json.dumps(payload.get("artifact_hashes") or {}, ensure_ascii=False, sort_keys=True, default=str),
                 json.dumps(payload.get("criterion_verdicts") or {}, ensure_ascii=False, sort_keys=True, default=str),
                 str(payload.get("ledger_digest") or ""), time.time()),
            )
            ledger_id = int(cursor.lastrowid)
            for row in rows:
                if not isinstance(row, dict):
                    continue
                try:
                    line_number = int(row.get("line", -1))
                except (TypeError, ValueError):
                    line_number = -1
                db.execute(
                    """INSERT INTO requirement_item_rows(
                        ledger_id, project_id, item_id, criterion_id, artifact_path,
                        requirement, method, is_required, result, line, quote, reason,
                        expected_json)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (ledger_id, project_id, str(row.get("item_id") or ""),
                     str(row.get("criterion_id") or ""), str(row.get("artifact_path") or ""),
                     str(row.get("requirement") or "")[:2000], str(row.get("method") or ""),
                     1 if row.get("required", True) else 0, str(row.get("result") or ""),
                     line_number, str(row.get("quote") or "")[:1000],
                     str(row.get("reason") or "")[:2000],
                     json.dumps(row.get("expected") or {}, ensure_ascii=False,
                                sort_keys=True, default=str)[:4000]),
                )
            db.commit()
            saved = db.execute(
                "SELECT * FROM requirement_item_ledger WHERE id=?", (ledger_id,)).fetchone()
    return {"id": ledger_id, "project_id": project_id,
            "ledger_digest": str(payload.get("ledger_digest") or "")}


def latest_ledger(memory_path: str | Path, project_id: str) -> dict | None:
    """最新の台帳と行を返す。台帳が無いときだけNone。読取失敗は例外。

    読取失敗(接続不可・テーブル確認失敗)は LedgerReadError とし、
    『台帳が空/行が無い』と区別する。呼出側は迂回せず異常停止する。
    """
    path = item_ledger_db_path(memory_path)
    if not path.exists():
        return None
    with _connect_readonly(path) as db:
        try:
            _require_ledger_tables(db)
        except LedgerReadError:
            raise
        try:
            row = db.execute(
                "SELECT * FROM requirement_item_ledger WHERE project_id=? ORDER BY id DESC LIMIT 1",
                (str(project_id or ""),)).fetchone()
        except sqlite3.Error as exc:
            raise LedgerReadError(
                "row_unreadable",
                path_kind=classify_ledger_path_kind(path),
                sqlite_error=_type_name(exc)) from exc
        if row is None:
            return None
        saved = dict(row)
        try:
            rows = db.execute(
                "SELECT * FROM requirement_item_rows WHERE ledger_id=? ORDER BY item_id",
                (int(saved.get("id") or 0),)).fetchall()
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise LedgerReadError(
                "row_unreadable",
                path_kind=classify_ledger_path_kind(path),
                sqlite_error=_type_name(exc)) from exc
        saved["rows"] = [dict(r) for r in rows]
        for item in saved["rows"]:
            try:
                expected = json.loads(str(item.get("expected_json") or "{}"))
            except (TypeError, ValueError):
                expected = {}
            item["expected"] = expected if isinstance(expected, dict) else {}
        try:
            names2 = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        except sqlite3.Error as exc:
            raise LedgerReadError(
                "table_unreadable",
                path_kind=classify_ledger_path_kind(path),
                sqlite_error=_type_name(exc)) from exc
        if "requirement_item_confirmations" in names2:
            try:
                confirmations = db.execute(
                    "SELECT * FROM requirement_item_confirmations WHERE ledger_id=? ORDER BY id",
                    (int(saved.get("id") or 0),)).fetchall()
                saved["confirmations"] = [dict(r) for r in confirmations]
            except (sqlite3.Error, TypeError, ValueError):
                saved["confirmations"] = []
        else:
            saved["confirmations"] = []
        # 人確認の適用: 文意の項目(llm_semantic)だけ HUMAN_CONFIRMED として扱う。
        # 機械項目(count/headings/phrase)は人が上書きできない(拒否相当として無視)。
        applied = 0
        try:
            hashes = json.loads(str(saved.get("artifact_hashes_json") or "{}"))
        except (TypeError, ValueError):
            hashes = {}
        if not isinstance(hashes, dict):
            hashes = {}
        plan_signature = str(saved.get("plan_signature") or "")
        for confirmation in saved.get("confirmations") or []:
            if not isinstance(confirmation, dict):
                continue
            item_id = str(confirmation.get("item_id") or "")
            if not item_id:
                continue
            # 成果物・計画が変わったら無効になる。
            path = str(confirmation.get("artifact_path") or "")
            if path and str(hashes.get(path) or "") != str(confirmation.get("artifact_hash") or ""):
                continue
            if str(confirmation.get("plan_signature") or "") != plan_signature:
                continue
            for row in saved["rows"]:
                if not isinstance(row, dict):
                    continue
                if str(row.get("item_id") or "") != item_id:
                    continue
                if str(row.get("method") or "") != "llm_semantic":
                    continue
                if str(row.get("result") or "") != "UNTESTABLE":
                    continue
                row["result"] = "HUMAN_CONFIRMED"
                row["confirmed_by"] = str(confirmation.get("actor") or "")
                row["confirm_reason"] = str(confirmation.get("reason") or "")
                row["confirm_quote"] = str(confirmation.get("quote") or "")
                applied += 1
                break
        saved["human_confirmed_count"] = applied
        return saved


def record_human_confirmation(memory_path: str | Path, project_id: str, item_id: str,
                               actor: str, reason: str, quote: str,
                               artifact_hashes: dict[str, str] | None = None) -> dict:
    """UNTESTABLE の文意項目(llm_semantic)について人の確認を追記する。

    担当者名 actor・理由 reason・引用 quote は必須。文意の項目にだけ適用でき、
    数・見出し・語句の項目は拒否する。成果物のハッシュ・計画署名に結び付け、
    変更されたら無効になる。機械のPASSとは別の結果(HUMAN_CONFIRMED)として扱う。
    """
    pid = str(project_id or "")
    target_item = str(item_id or "")
    actor_name = str(actor or "").strip()
    reason_text = str(reason or "").strip()
    quote_text = str(quote or "").strip()
    if not pid or not target_item:
        raise ValueError("project_id and item_id are required")
    if not actor_name or not reason_text or not quote_text:
        raise ValueError("actor, reason and quote are required")
    path = item_ledger_db_path(memory_path)
    lock = _lock_for(path)
    with lock:
        with _connect(memory_path) as db:
            row = db.execute(
                "SELECT * FROM requirement_item_ledger WHERE project_id=? ORDER BY id DESC LIMIT 1",
                (pid,)).fetchone()
            if row is None:
                raise ValueError("項目別検証台帳が無い: 再計画が必要")
            saved = dict(row)
            ledger_id = int(saved.get("id") or 0)
            rows = db.execute(
                "SELECT * FROM requirement_item_rows WHERE ledger_id=? ORDER BY item_id",
                (ledger_id,)).fetchall()
            target = None
            for candidate in rows:
                item = dict(candidate)
                if str(item.get("item_id") or "") == target_item:
                    target = item
                    break
            if target is None:
                raise ValueError("対象の項目が台帳にありません")
            if str(target.get("method") or "") != "llm_semantic":
                raise ValueError("人の確認は文意の項目(llm_semantic)にだけ適用できます")
            if str(target.get("result") or "") != "UNTESTABLE":
                raise ValueError("人の確認は未確認(UNTESTABLE)の項目にだけ適用できます")
            plan_signature = str(saved.get("plan_signature") or "")
            hashes = dict(artifact_hashes or {})
            artifact_path = str(target.get("artifact_path") or "")
            artifact_hash = str(hashes.get(artifact_path) or "")
            if not artifact_hash:
                try:
                    stored = json.loads(str(saved.get("artifact_hashes_json") or "{}"))
                except (TypeError, ValueError):
                    stored = {}
                if isinstance(stored, dict):
                    artifact_hash = str(stored.get(artifact_path) or "")
            if not artifact_hash:
                raise ValueError("成果物のハッシュを確認できません")
            db.execute(
                """INSERT INTO requirement_item_confirmations(
                    ledger_id, project_id, item_id, criterion_id, artifact_path,
                    artifact_hash, plan_signature, actor, reason, quote, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (ledger_id, pid, target_item, str(target.get("criterion_id") or ""),
                 artifact_path, artifact_hash, plan_signature,
                 actor_name, reason_text[:2000], quote_text[:1000], time.time()),
            )
            db.commit()
    return {"project_id": pid, "item_id": target_item, "result": "HUMAN_CONFIRMED"}


def reset_for_generation(memory_path: str | Path, project_id: str) -> dict:
    """PJ初期化では台帳の記録は変えない。証拠として残す。"""
    return {"project_id": str(project_id or ""), "reset": False,
            "note": "項目別検証台帳の記録は世代切替で変えません"}


def count_repair_attempts(
    memory_path: str | Path, project_id: str, failure_signature: str,
) -> int:
    """同じ失敗署名での自動修復の試行回数。読み取れなければ上限扱い(無限に増やさない)。"""
    pid = str(project_id or "")
    signature = str(failure_signature or "")
    if not pid or not signature:
        return 0
    path = item_ledger_db_path(memory_path)
    if not path.exists():
        return 0
    with _connect_readonly(path) as db:
        try:
            names = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        except sqlite3.Error:
            return 2_147_483_647
        if "requirement_repair_runs" not in names:
            return 0
        try:
            count = db.execute(
                "SELECT COUNT(1) FROM requirement_repair_runs "
                "WHERE project_id=? AND failure_signature=?",
                (pid, signature)).fetchone()
        except sqlite3.Error:
            return 2_147_483_647
        try:
            return int(count[0] or 0) if count else 0
        except (TypeError, ValueError):
            return 2_147_483_647


def list_repair_runs(
    memory_path: str | Path, project_id: str, limit: int = 50,
) -> list[dict]:
    """保存された修復・判断待ちの記録。修正前後の項目結果と成果物ハッシュを含む。"""
    pid = str(project_id or "")
    try:
        capped = max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        capped = 50
    path = item_ledger_db_path(memory_path)
    if not pid or not path.exists():
        return []
    with _connect_readonly(path) as db:
        try:
            names = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        except sqlite3.Error:
            return []
        if "requirement_repair_runs" not in names:
            return []
        # 旧DBに列が無い場合は空扱い(作り直しはしない)。
        try:
            available = {row[1] for row in db.execute(
                "PRAGMA table_info(requirement_repair_runs)").fetchall()}
        except sqlite3.Error:
            return []
        wanted = (
            "id", "item_id", "failure_kind", "failure_signature",
            "plan_signature", "source_hash", "artifact_path",
            "artifact_hash_before", "artifact_hash_after",
            "item_result_before", "item_result_after",
            "decision", "operator_action", "reason", "attempt", "created_at",
        )
        if any(column not in available for column in wanted):
            return []
        try:
            result = db.execute(
                "SELECT * FROM requirement_repair_runs WHERE project_id=? "
                "ORDER BY id DESC LIMIT ?", (pid, capped)).fetchall()
        except sqlite3.Error:
            return []
        return [dict(row) for row in result]


def record_repair_run(
    memory_path: str | Path,
    *,
    project_id: str,
    item_id: str = "",
    failure_kind: str = "",
    failure_signature: str = "",
    plan_signature: str = "",
    source_hash: str = "",
    artifact_path: str = "",
    artifact_hash_before: str = "",
    artifact_hash_after: str = "",
    item_result_before: str = "",
    item_result_after: str = "",
    decision: str = "",
    operator_action: str = "",
    reason: str = "",
) -> dict:
    """修復の試行・再検証・判断待ちを追記する。戻り値に試行回数を含める。"""
    pid = str(project_id or "")
    signature = str(failure_signature or "")
    kind = str(failure_kind or "")
    if not pid or not signature or not kind:
        raise ValueError("project_id, failure_kind and failure_signature are required")
    path = item_ledger_db_path(memory_path)
    lock = _lock_for(path)
    with lock:
        with _connect(memory_path) as db:
            try:
                cursor = db.execute(
                    """INSERT INTO requirement_repair_runs(
                        project_id, item_id, failure_kind, failure_signature,
                        plan_signature, source_hash, artifact_path,
                        artifact_hash_before, artifact_hash_after,
                        item_result_before, item_result_after,
                        decision, operator_action, reason, attempt, created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (pid, str(item_id or ""), kind, signature,
                     str(plan_signature or ""), str(source_hash or ""),
                     str(artifact_path or ""),
                     str(artifact_hash_before or ""), str(artifact_hash_after or ""),
                     str(item_result_before or ""), str(item_result_after or ""),
                     str(decision or ""), str(operator_action or "")[:2000],
                     str(reason or "")[:2000],
                     int(count_repair_attempts_unlocked(db, pid, signature)) + 1,
                     time.time()),
                )
            except sqlite3.Error as exc:
                raise ValueError(
                    f"repair log unavailable: {type(exc).__name__}") from exc
            db.commit()
            saved_id = int(cursor.lastrowid)
    return {
        "id": saved_id, "project_id": pid, "item_id": str(item_id or ""),
        "failure_kind": kind, "failure_signature": signature,
        "attempt": count_repair_attempts(memory_path, pid, signature),
    }


def count_repair_attempts_unlocked(
    db: sqlite3.Connection, project_id: str, failure_signature: str,
) -> int:
    """同一ロック内の試行回数読み取り。読み取れなければ上限扱いにする。"""
    try:
        count = db.execute(
            "SELECT COUNT(1) FROM requirement_repair_runs "
            "WHERE project_id=? AND failure_signature=?",
            (str(project_id or ""), str(failure_signature or ""))).fetchone()
    except sqlite3.Error:
        return 2_147_483_647
    try:
        return int(count[0] or 0)
    except (TypeError, ValueError):
        return 2_147_483_647


def purge_project_items(memory_path: str | Path, project_id: str) -> dict:
    """完全削除時は当該PJの台帳行だけ消す。他PJに影響しない。"""
    pid = str(project_id or "")
    path = item_ledger_db_path(memory_path)
    if not path.exists() or not pid:
        return {"requirement_item_ledger": 0, "requirement_item_rows": 0,
                "requirement_item_confirmations": 0,
                "requirement_repair_runs": 0}
    removed = {"requirement_item_ledger": 0, "requirement_item_rows": 0,
               "requirement_item_confirmations": 0,
               "requirement_repair_runs": 0}
    try:
        db = sqlite3.connect(str(path), timeout=30)
    except (OSError, sqlite3.Error) as exc:
        LOGGER.warning("item ledger purge connect failed: %s", _type_name(exc))
        raise RuntimeError(
            f"item ledger purge connect failed: {_type_name(exc)}") from exc
    try:
        with db:
            try:
                names = {row[0] for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            except sqlite3.Error as exc:
                raise RuntimeError(
                    f"item ledger purge table inspection failed: {_type_name(exc)}") from exc
            if "requirement_item_ledger" not in names or "requirement_item_rows" not in names:
                raise RuntimeError("item ledger purge required tables are missing")
            try:
                ids = [row[0] for row in db.execute(
                    "SELECT id FROM requirement_item_ledger WHERE project_id=?", (pid,)).fetchall()]
            except sqlite3.Error as exc:
                raise RuntimeError(
                    f"item ledger purge row enumeration failed: {_type_name(exc)}") from exc
            for ledger_id in ids:
                try:
                    first = db.execute(
                        "DELETE FROM requirement_item_rows WHERE ledger_id=?", (ledger_id,))
                    removed["requirement_item_rows"] = int(removed["requirement_item_rows"]) + int(first.rowcount or 0)
                    try:
                        names_now = {row[0] for row in db.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
                    except sqlite3.Error:
                        names_now = set()
                    if "requirement_item_confirmations" in names_now:
                        third = db.execute(
                            "DELETE FROM requirement_item_confirmations WHERE ledger_id=?", (ledger_id,))
                        removed["requirement_item_confirmations"] = int(
                            removed["requirement_item_confirmations"]) + int(third.rowcount or 0)
                    second = db.execute(
                        "DELETE FROM requirement_item_ledger WHERE id=?", (ledger_id,))
                    removed["requirement_item_ledger"] = int(removed["requirement_item_ledger"]) + int(second.rowcount or 0)
                except sqlite3.Error as exc:
                    try:
                        db.rollback()
                    except sqlite3.Error as rollback_exc:
                        raise RuntimeError(
                            f"item ledger purge rollback failed: {_type_name(rollback_exc)}") from exc
                    raise RuntimeError(
                        f"item ledger purge delete failed: {_type_name(exc)}") from exc
            try:
                repair_names = {row[0] for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            except sqlite3.Error as exc:
                raise RuntimeError(
                    f"item ledger purge repair table inspection failed: {_type_name(exc)}") from exc
            if "requirement_repair_runs" in repair_names:
                try:
                    fourth = db.execute(
                        "DELETE FROM requirement_repair_runs WHERE project_id=?", (pid,))
                    removed["requirement_repair_runs"] = int(
                        removed["requirement_repair_runs"]) + int(fourth.rowcount or 0)
                except sqlite3.Error as exc:
                    raise RuntimeError(
                        f"item ledger repair purge failed: {_type_name(exc)}") from exc
    finally:
        try:
            db.close()
        except sqlite3.Error:
            pass
    return removed
