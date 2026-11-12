"""P0-1 実行サイクル記録と残件台帳(記録する器のみ)。

- 項目別台帳と同じSQLite(item_ledger_db_path)に追記する追加型。既存データは変えない。
- execution_state と goal_verdict は別軸。finished+unmet / finished+undetermined
  (+external_result) は正常。achieved は完了ゲートの結果が必須で、
  ゲートが通っていなければ拒否する(単独で書けない)。
- 同じ run_id の終了は一度だけ(2回目は冪等に同じ結果)。
- 旧PJ(記録なし)は『実行履歴未判定』を読む側で返す。移行処理は作らない。
- _run() の終了規則には接続しない(次の単位)。mission.status は変更しない。
- B4: 終了時に当時の最小結果を project_run_results へ1行だけ追記する
  (サイクルにつき1行・冪等・上書きしない)。保存の失敗は終了自体を
  失敗させず、basis_status=unavailable として失敗を残す。
- B4: 残件の責任区分(responsibility)を追記型で持つ。既存行は
  unclassified として読む(推測で割り当てない)。
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from app.requirement_item_ledger import (
    _ensure_tables as _ensure_item_tables,
    classify_ledger_path_kind,
    item_ledger_db_path,
    ledger_readonly_uri,
)

LOGGER = logging.getLogger(__name__)

EXECUTION_STATES = frozenset({
    "running", "finished", "stopped_error", "stopped_time_limit", "cancelled",
})
GOAL_VERDICTS = frozenset({"achieved", "unmet", "undetermined"})
PENDING_KINDS = frozenset({
    "none", "artifact", "human_decision", "external_result",
    "source_fact", "verification",
})
# P0-B の失敗分類 + 実施結果待ち(external_result)。
OPEN_ITEM_KINDS = frozenset({
    "artifact_missing", "phrase_missing", "unsupported_fact",
    "human_decision", "external_evidence", "system_error",
    "external_result", "unclassified",
})
OPEN_ITEM_OWNERS = frozenset({"human", "system", "external"})
# B4: 残件の責任区分。unclassified は「分類できていない」(黙って割り当てない)。
RUN_RESPONSIBILITIES = frozenset({
    "fix_artifact", "register_source", "human_decision",
    "await_external", "unclassified",
})
# B4: 最小結果の要求項目の集計キー。欠落時は 0件として保存しない。
RESULT_COUNT_KEYS = ("pass", "fail", "untestable", "human_pending", "input_missing")


class RunCycleReadError(RuntimeError):
    """実行サイクル台帳の読取失敗。『記録なし』とは区別する。

    kind: 'connect_failed' | 'table_missing' | 'table_unreadable' | 'row_unreadable'
    """

    def __init__(self, kind: str, *, path_kind: str = "",
                 sqlite_error: str = "", detail: str = "") -> None:
        super().__init__(f"run cycle read failed: {kind}")
        self.kind = str(kind or "")
        self.path_kind = str(path_kind or "")
        self.sqlite_error = str(sqlite_error or "")
        self.detail = str(detail or "")[:500]


def _type_name(exc: BaseException) -> str:
    try:
        return str(type(exc).__name__ or "Error")
    except Exception:
        return "Error"


RUN_CYCLE_TABLE_DDL = """CREATE TABLE IF NOT EXISTS project_run_cycles(
    run_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    plan_signature TEXT NOT NULL DEFAULT '',
    source_hash TEXT NOT NULL DEFAULT '',
    started_at REAL NOT NULL,
    ended_at REAL,
    execution_state TEXT NOT NULL DEFAULT 'running',
    goal_verdict TEXT NOT NULL DEFAULT 'undetermined',
    pending_kind TEXT NOT NULL DEFAULT 'none',
    stop_reason TEXT NOT NULL DEFAULT '',
    verification_ref TEXT NOT NULL DEFAULT '',
    verification_hash TEXT NOT NULL DEFAULT '',
    completion_achieved INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL)"""

RUN_OPEN_ITEM_TABLE_DDL = """CREATE TABLE IF NOT EXISTS project_run_open_items(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    item_id TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT '',
    status_text TEXT NOT NULL DEFAULT '',
    evidence_needed TEXT NOT NULL DEFAULT '',
    owner TEXT NOT NULL DEFAULT '',
    next_action TEXT NOT NULL DEFAULT '',
    artifacts_json TEXT NOT NULL DEFAULT '[]',
    idempotency_key TEXT NOT NULL DEFAULT '',
    supersedes_id INTEGER,
    version INTEGER NOT NULL DEFAULT 1,
    responsibility TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL)"""

RUN_RESULT_TABLE_DDL = """CREATE TABLE IF NOT EXISTS project_run_results(
    run_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    goal_verdict TEXT NOT NULL DEFAULT '',
    counts_json TEXT NOT NULL DEFAULT '',
    open_items_json TEXT NOT NULL DEFAULT '[]',
    verification_ref TEXT NOT NULL DEFAULT '',
    verification_hash TEXT NOT NULL DEFAULT '',
    basis_status TEXT NOT NULL DEFAULT 'unavailable',
    basis_reason TEXT NOT NULL DEFAULT '',
    basis_sha256 TEXT NOT NULL DEFAULT '',
    recorded_at REAL NOT NULL)"""


def classify_responsibility(kind: str = "", owner: str = "",
                            status_text: str = "", evidence_needed: str = "",
                            next_action: str = "") -> str:
    """既存の列から責任区分を決める。決められない行は unclassified。

    規則(推測で黙って割り当てない):
    - kind=human_decision → 人が判断
    - kind=external_result/external_evidence か owner=external → 外部実施結果待ち
    - kind=unsupported_fact → 出典を登録
    - 残りは本文の手がかり(出典/原本/登録→出典を登録、確認/判断/承認→人が判断、
      実施/公開/投稿/顧客/外部/観測→外部実施結果待ち)を見て、
      artifact_missing/phrase_missing/system_error → 成果物を修正。
    - どれにも当てはまらなければ unclassified。
    """
    k = str(kind or "")
    o = str(owner or "")
    if k == "human_decision":
        return "human_decision"
    if k in ("external_result", "external_evidence") or o == "external":
        return "await_external"
    if k == "unsupported_fact":
        return "register_source"
    blob = "\n".join((str(status_text or ""), str(evidence_needed or ""),
                      str(next_action or "")))
    for sig in ("出典", "原本", "登録", "input_missing", "source_ref", "context:"):
        if sig and sig in blob:
            return "register_source"
    for sig in ("確認", "判断", "承認", "文意", "human", "pending"):
        if sig and sig in blob:
            return "human_decision"
    for sig in ("実施", "公開", "投稿", "顧客", "外部", "観測", "実測", "external"):
        if sig and sig in blob:
            return "await_external"
    if k in ("artifact_missing", "phrase_missing", "system_error"):
        return "fix_artifact"
    return "unclassified"


def build_counts_from_ledger_rows(rows: list[dict] | None) -> dict | None:
    """既存の検証結果の集計だけを作る(新しい判定は作らない)。

    rows が読めない・欠落(None・空でないリスト以外)のときは None を返す。
    呼び出し側は None を 0件として保存せず unavailable にする。
    """
    if not isinstance(rows, list) or not rows:
        return None
    counts = {key: 0 for key in RESULT_COUNT_KEYS}
    for row in rows:
        if not isinstance(row, dict):
            continue
        result = str(row.get("result") or "")
        if result == "PASS":
            counts["pass"] += 1
        elif result == "FAIL":
            counts["fail"] += 1
        elif result == "HUMAN_CONFIRMED":
            counts["human_pending"] += 1
        elif result == "UNTESTABLE":
            blob = " ".join((str(row.get("method") or ""),
                             str(row.get("reason") or ""),
                             str(row.get("requirement") or "")))
            if "input_missing" in blob.lower() or "入力" in blob:
                counts["input_missing"] += 1
            else:
                counts["untestable"] += 1
        else:
            counts["untestable"] += 1
    return counts


def _normalize_counts(raw: object) -> dict | None:
    if not isinstance(raw, dict):
        return None
    out: dict[str, int] = {}
    for key in RESULT_COUNT_KEYS:
        try:
            value = int(raw.get(key))  # type: ignore[union-attr]
        except (TypeError, ValueError):
            return None
        if value < 0:
            return None
        out[key] = value
    return out


def _basis_payload(run_id: str, project_id: str, goal_verdict: str,
                   counts: dict, open_items: list[dict],
                   verification_ref: str, verification_hash: str) -> dict:
    return {
        "run_id": str(run_id or ""),
        "project_id": str(project_id or ""),
        "goal_verdict": str(goal_verdict or ""),
        "counts": {key: int(counts.get(key, 0)) for key in RESULT_COUNT_KEYS},
        "open_items": [
            {"id": int(item.get("id") or 0),
             "item_id": str(item.get("item_id") or ""),
             "kind": str(item.get("kind") or ""),
             "responsibility": str(item.get("responsibility") or "unclassified")}
            for item in (open_items or []) if isinstance(item, dict)
        ],
        "verification_ref": str(verification_ref or ""),
        "verification_hash": str(verification_hash or ""),
    }


def _basis_sha256(payload: dict) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _ensure_responsibility_column(db: sqlite3.Connection) -> None:
    try:
        cols = {row[1] for row in db.execute(
            "PRAGMA table_info(project_run_open_items)").fetchall()}
    except sqlite3.Error:
        return
    if "responsibility" not in cols:
        try:
            db.execute("ALTER TABLE project_run_open_items "
                       "ADD COLUMN responsibility TEXT NOT NULL DEFAULT ''")
        except sqlite3.Error:
            # 既存データを変えない。読む側は空を unclassified として扱う。
            LOGGER.warning("run open items responsibility column add failed")


def ensure_run_tables(db: sqlite3.Connection) -> None:
    """実行サイクル3表と索引を作成する。失敗は呼出側へ送出する(握りつぶさない)。

    項目別台帳の表も同じDBに揃える(追加型)。既存データは変えない。
    responsibility 列は有無を調べてから追加する。
    """
    _ensure_item_tables(db)
    db.execute(RUN_CYCLE_TABLE_DDL)
    db.execute(RUN_OPEN_ITEM_TABLE_DDL)
    db.execute(RUN_RESULT_TABLE_DDL)
    _ensure_responsibility_column(db)
    db.execute("""CREATE INDEX IF NOT EXISTS idx_run_cycles_project
        ON project_run_cycles(project_id, started_at DESC)""")
    db.execute("""CREATE INDEX IF NOT EXISTS idx_run_open_items_run
        ON project_run_open_items(run_id, id)""")
    db.execute("""CREATE INDEX IF NOT EXISTS idx_run_open_items_project
        ON project_run_open_items(project_id, run_id)""")
    db.execute("""CREATE INDEX IF NOT EXISTS idx_run_results_project
        ON project_run_results(project_id, recorded_at DESC)""")


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


@contextmanager
def _connect_rw(memory_path: str | Path):
    path = item_ledger_db_path(memory_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=30)
    db.row_factory = sqlite3.Row
    try:
        with db:
            ensure_run_tables(db)
        yield db
    finally:
        db.close()


@contextmanager
def _connect_ro(memory_path: str | Path):
    path = item_ledger_db_path(memory_path)
    kind = classify_ledger_path_kind(path)
    try:
        db = sqlite3.connect(ledger_readonly_uri(path), uri=True, timeout=15)
        db.row_factory = sqlite3.Row
    except (OSError, sqlite3.Error, ValueError) as exc:
        raise RunCycleReadError(
            "connect_failed", path_kind=kind,
            sqlite_error=_type_name(exc)) from exc
    try:
        yield db
    finally:
        try:
            db.close()
        except sqlite3.Error:
            pass


def _require_run_tables(db: sqlite3.Connection) -> None:
    try:
        names = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    except sqlite3.Error as exc:
        raise RunCycleReadError(
            "table_unreadable", sqlite_error=_type_name(exc)) from exc
    if "project_run_cycles" not in names or "project_run_open_items" not in names:
        raise RunCycleReadError(
            "table_missing",
            detail="project_run_cycles/project_run_open_items") from None


def _result_table_exists(db: sqlite3.Connection) -> bool:
    try:
        names = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    except sqlite3.Error:
        return False
    return "project_run_results" in names


def start_run(memory_path: str | Path, project_id: str, run_id: str, *,
              plan_signature: str = "", source_hash: str = "") -> dict:
    """実行サイクルを開始する(running)。二重開始は拒否する。"""
    pid = str(project_id or "")
    rid = str(run_id or "")
    if not pid or not rid:
        raise ValueError("project_id and run_id are required")
    path = item_ledger_db_path(memory_path)
    lock = _lock_for(path)
    now = time.time()
    with lock:
        with _connect_rw(memory_path) as db:
            existing = db.execute(
                "SELECT run_id FROM project_run_cycles WHERE run_id=?",
                (rid,)).fetchone()
            if existing is not None:
                raise ValueError(f"run_id already exists: {rid}")
            db.execute(
                """INSERT INTO project_run_cycles(
                    run_id, project_id, plan_signature, source_hash,
                    started_at, ended_at, execution_state, goal_verdict,
                    pending_kind, stop_reason, verification_ref,
                    verification_hash, completion_achieved, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (rid, pid, str(plan_signature or "")[:500],
                 str(source_hash or "")[:256], now, None, "running",
                 "undetermined", "none", "", "", "", 0, now),
            )
            db.commit()
    return {"run_id": rid, "project_id": pid, "execution_state": "running",
            "goal_verdict": "undetermined"}


def _get_cycle_locked(db: sqlite3.Connection, run_id: str) -> dict | None:
    row = db.execute(
        "SELECT * FROM project_run_cycles WHERE run_id=?", (run_id,)).fetchone()
    return dict(row) if row else None


def add_open_item(memory_path: str | Path, run_id: str, *,
                  item_id: str = "", kind: str = "",
                  status_text: str = "", evidence_needed: str = "",
                  owner: str = "", next_action: str = "",
                  artifacts: list[str] | None = None,
                  idempotency_key: str = "",
                  responsibility: str | None = None) -> dict:
    """残件を追記する。同じ冪等キーは二重に追加しない(冪等)。

    responsibility を省略したら既存の列から決定規則で付与する。
    決められない行は unclassified(推測で割り当てない)。
    """
    rid = str(run_id or "")
    item_kind = str(kind or "")
    item_owner = str(owner or "")
    if not rid:
        raise ValueError("run_id is required")
    if item_kind not in OPEN_ITEM_KINDS:
        raise ValueError(f"unknown open item kind: {item_kind}")
    if item_owner not in OPEN_ITEM_OWNERS:
        raise ValueError(f"unknown open item owner: {item_owner}")
    if not str(status_text or "").strip():
        raise ValueError("status_text is required")
    if not str(evidence_needed or "").strip():
        raise ValueError("evidence_needed is required")
    if not str(next_action or "").strip():
        raise ValueError("next_action is required")
    key = str(idempotency_key or "")
    artifact_list = [str(a) for a in (artifacts or [])][:50]
    if responsibility is None or str(responsibility or "") == "":
        decided = classify_responsibility(
            item_kind, item_owner, str(status_text or ""),
            str(evidence_needed or ""), str(next_action or ""))
    else:
        decided = str(responsibility or "")
        if decided not in RUN_RESPONSIBILITIES:
            raise ValueError(f"unknown responsibility: {decided}")
    path = item_ledger_db_path(memory_path)
    lock = _lock_for(path)
    with lock:
        with _connect_rw(memory_path) as db:
            cycle = _get_cycle_locked(db, rid)
            if cycle is None:
                raise ValueError(f"run_id not found: {rid}")
            pid = str(cycle.get("project_id") or "")
            if key:
                dup = db.execute(
                    "SELECT * FROM project_run_open_items WHERE run_id=? AND idempotency_key=?",
                    (rid, key)).fetchone()
                if dup is not None:
                    saved = dict(dup)
                    return {"id": int(saved["id"]), "run_id": rid,
                            "project_id": pid, "idempotent": True}
            cursor = db.execute(
                """INSERT INTO project_run_open_items(
                    run_id, project_id, item_id, kind, status_text,
                    evidence_needed, owner, next_action, artifacts_json,
                    idempotency_key, supersedes_id, version, responsibility,
                    created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (rid, pid, str(item_id or "")[:200], item_kind,
                 str(status_text or "")[:2000],
                 str(evidence_needed or "")[:2000], item_owner,
                 str(next_action or "")[:2000],
                 json.dumps(artifact_list, ensure_ascii=False)[:8000],
                 key, None, 1, decided, time.time()),
            )
            db.commit()
            return {"id": int(cursor.lastrowid), "run_id": rid,
                    "project_id": pid, "idempotent": False,
                    "responsibility": decided}


def correct_open_item(memory_path: str | Path, open_item_id: int, *,
                      status_text: str = "", evidence_needed: str = "",
                      next_action: str = "",
                      responsibility: str | None = None) -> dict:
    """残件の訂正は新しい行/版として追加する(上書きで消さない)。

    responsibility を省略したら新しい文面から決定規則で付け直す
    (訂正で区分を変えられる)。明示された区分は検証して使う。
    """
    try:
        target_id = int(open_item_id)
    except (TypeError, ValueError):
        raise ValueError("open_item_id is required") from None
    if not str(status_text or "").strip():
        raise ValueError("status_text is required")
    if not str(evidence_needed or "").strip():
        raise ValueError("evidence_needed is required")
    if not str(next_action or "").strip():
        raise ValueError("next_action is required")
    path = item_ledger_db_path(memory_path)
    lock = _lock_for(path)
    with lock:
        with _connect_rw(memory_path) as db:
            prev = db.execute(
                "SELECT * FROM project_run_open_items WHERE id=?",
                (target_id,)).fetchone()
            if prev is None:
                raise ValueError(f"open item not found: {target_id}")
            old = dict(prev)
            prev_version = old.get("version")
            next_version = int(prev_version) + 1 if prev_version is not None else 2
            old_kind = str(old.get("kind") or "")
            old_owner = str(old.get("owner") or "")
            if responsibility is None or str(responsibility or "") == "":
                decided = classify_responsibility(
                    old_kind, old_owner, str(status_text or ""),
                    str(evidence_needed or ""), str(next_action or ""))
            else:
                decided = str(responsibility or "")
                if decided not in RUN_RESPONSIBILITIES:
                    raise ValueError(f"unknown responsibility: {decided}")
            cursor = db.execute(
                """INSERT INTO project_run_open_items(
                    run_id, project_id, item_id, kind, status_text,
                    evidence_needed, owner, next_action, artifacts_json,
                    idempotency_key, supersedes_id, version, responsibility,
                    created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (str(old.get("run_id") or ""),
                 str(old.get("project_id") or ""),
                 str(old.get("item_id") or ""),
                 old_kind,
                 str(status_text or "")[:2000],
                 str(evidence_needed or "")[:2000],
                 old_owner,
                 str(next_action or "")[:2000],
                 str(old.get("artifacts_json") or "[]")[:8000],
                 "", int(old.get("id") or 0),
                 next_version, decided, time.time()),
            )
            db.commit()
            return {"id": int(cursor.lastrowid),
                    "run_id": str(old.get("run_id") or ""),
                    "supersedes_id": int(old.get("id") or 0),
                    "version": next_version,
                    "responsibility": decided}


def _check_gate_for_achieved(completion_gate_result: dict | None) -> int:
    if not isinstance(completion_gate_result, dict):
        raise ValueError("ACHIEVED_REQUIRES_GATE: completion gate result is required")
    if completion_gate_result.get("achieved") is not True:
        raise ValueError("ACHIEVED_REQUIRES_GATE: gate did not achieve")
    return 1


def _save_run_result_locked(db: sqlite3.Connection, *, run_id: str,
                            project_id: str, goal_verdict: str,
                            verification_ref: str, verification_hash: str,
                            result_basis: dict | None) -> dict:
    """当時の最小結果を1行だけ追記する(冪等・上書きしない)。

    result_basis が読めない・欠落のときは空の集計を 0件として保存せず、
    basis_status=unavailable と理由を保存する。保存自体の失敗は呼出側で
    unavailable に残す(握りつぶさない)。戻り値は保存の成否を含む。
    """
    rid = str(run_id or "")
    pid = str(project_id or "")
    counts: dict | None = None
    reason = ""
    if isinstance(result_basis, dict):
        raw_counts = result_basis.get("counts")
        counts = _normalize_counts(raw_counts)
        if counts is None:
            reason = str(result_basis.get("reason") or
                         result_basis.get("basis_reason") or
                         "result_basis unreadable")[:500]
        else:
            reason = str(result_basis.get("reason") or "")[:500]
    else:
        reason = "result_basis not provided"
    if not _result_table_exists(db):
        return {"result_saved": False, "basis_status": "unavailable",
                "basis_reason": "result table missing: " + (reason or "unavailable"),
                "result_error": "table_missing"}
    try:
        existing = db.execute(
            "SELECT run_id FROM project_run_results WHERE run_id=?",
            (rid,)).fetchone()
    except sqlite3.Error as exc:
        LOGGER.warning("run result read failed: %s", _type_name(exc))
        return {"result_saved": False, "basis_status": "unavailable",
                "basis_reason": f"result read failed: {_type_name(exc)}",
                "result_error": _type_name(exc)}
    if existing is not None:
        try:
            saved = db.execute(
                "SELECT * FROM project_run_results WHERE run_id=?",
                (rid,)).fetchone()
            row = dict(saved) if saved else {}
        except sqlite3.Error:
            row = {}
        return {"result_saved": True, "idempotent": True,
                "basis_status": str(row.get("basis_status") or "unavailable"),
                "basis_sha256": str(row.get("basis_sha256") or ""),
                "result_error": ""}
    try:
        items = _list_open_items_locked(db, rid)
    except sqlite3.Error as exc:
        LOGGER.warning("run result open items read failed: %s", _type_name(exc))
        items = []
    _current, _history = _split_current_and_history(items)
    snapshot = [{
        "id": int(item.get("id") or 0),
        "item_id": str(item.get("item_id") or ""),
        "kind": str(item.get("kind") or ""),
        "responsibility": str(item.get("responsibility") or "") or "unclassified",
    } for item in _current]
    if counts is None:
        status = "unavailable"
        counts_json = ""
        open_json = json.dumps(snapshot, ensure_ascii=False,
                               sort_keys=True)[:20000]
        payload_hash = ""
        recorded_reason = reason or "minimal result unavailable"
    else:
        status = "available"
        counts_json = json.dumps(counts, ensure_ascii=False,
                                 sort_keys=True)
        open_json = json.dumps(snapshot, ensure_ascii=False,
                               sort_keys=True)[:20000]
        payload = _basis_payload(rid, pid, str(goal_verdict or ""), counts,
                                 snapshot, str(verification_ref or ""),
                                 str(verification_hash or ""))
        payload_hash = _basis_sha256(payload)
        recorded_reason = reason
    try:
        db.execute(
            """INSERT OR IGNORE INTO project_run_results(
                run_id, project_id, goal_verdict, counts_json,
                open_items_json, verification_ref, verification_hash,
                basis_status, basis_reason, basis_sha256, recorded_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (rid, pid, str(goal_verdict or ""), counts_json, open_json,
             str(verification_ref or "")[:1000],
             str(verification_hash or "")[:256], status,
             recorded_reason[:500], payload_hash, time.time()),
        )
        db.commit()
    except sqlite3.Error as exc:
        LOGGER.warning("run result save failed: %s", _type_name(exc))
        return {"result_saved": False, "basis_status": "unavailable",
                "basis_reason": f"result save failed: {_type_name(exc)}",
                "result_error": _type_name(exc)}
    return {"result_saved": True, "idempotent": False,
            "basis_status": status, "basis_reason": recorded_reason,
            "basis_sha256": payload_hash, "result_error": ""}


def finish_run(memory_path: str | Path, run_id: str, *,
               execution_state: str, goal_verdict: str,
               pending_kind: str = "none", stop_reason: str = "",
               verification_ref: str = "",
               verification_hash: str = "",
               completion_gate_result: dict | None = None,
               result_basis: dict | None = None) -> dict:
    """実行サイクルを終了する。同じ run_id は一度だけ(2回目は冪等)。

    goal_verdict=achieved は完了ゲートの結果が必須。ゲートが通って
    いなければ拒否する(記録関数が単独で achieved を書けない)。
    result_basis は省略可能。既存の呼び出しは壊さない。渡された集計は
    そのまま保存するだけで新しい判定は作らない。取れないときは
    unavailable として残し、空の集計を 0件として保存しない。
    最小結果の保存の失敗は終了自体を失敗させない(失敗を残す)。
    """
    rid = str(run_id or "")
    state = str(execution_state or "")
    verdict = str(goal_verdict or "")
    pending = str(pending_kind or "none")
    if not rid:
        raise ValueError("run_id is required")
    if state not in EXECUTION_STATES or state == "running":
        raise ValueError(f"execution_state must be a terminal state: {state}")
    if verdict not in GOAL_VERDICTS:
        raise ValueError(f"unknown goal_verdict: {verdict}")
    if pending not in PENDING_KINDS:
        raise ValueError(f"unknown pending_kind: {pending}")
    gate_flag = 0
    if verdict == "achieved":
        gate_flag = _check_gate_for_achieved(completion_gate_result)
    path = item_ledger_db_path(memory_path)
    lock = _lock_for(path)
    with lock:
        with _connect_rw(memory_path) as db:
            cycle = _get_cycle_locked(db, rid)
            if cycle is None:
                raise ValueError(f"run_id not found: {rid}")
            if cycle.get("ended_at") is not None or str(
                    cycle.get("execution_state") or "") != "running":
                saved = _get_cycle_locked(db, rid)
                return {"run_id": rid,
                        "project_id": str((saved or {}).get("project_id") or ""),
                        "execution_state": str((saved or {}).get("execution_state") or ""),
                        "goal_verdict": str((saved or {}).get("goal_verdict") or ""),
                        "idempotent": True}
            now = time.time()
            db.execute(
                """UPDATE project_run_cycles SET
                    ended_at=?, execution_state=?, goal_verdict=?,
                    pending_kind=?, stop_reason=?, verification_ref=?,
                    verification_hash=?, completion_achieved=?
                   WHERE run_id=?""",
                (now, state, verdict, pending,
                 str(stop_reason or "")[:2000],
                 str(verification_ref or "")[:1000],
                 str(verification_hash or "")[:256],
                 gate_flag, rid),
            )
            db.commit()
            saved = _get_cycle_locked(db, rid)
            pid = str((saved or {}).get("project_id") or "")
            # 最小結果の保存。失敗しても終了自体は成功させ、失敗を残す。
            try:
                result_info = _save_run_result_locked(
                    db, run_id=rid, project_id=pid, goal_verdict=verdict,
                    verification_ref=str(verification_ref or ""),
                    verification_hash=str(verification_hash or ""),
                    result_basis=result_basis)
            except (OSError, ValueError, RuntimeError, AttributeError,
                    KeyError, TypeError) as exc:
                LOGGER.warning("run result save raised: %s", _type_name(exc))
                result_info = {"result_saved": False,
                               "basis_status": "unavailable",
                               "basis_reason": f"result save raised: {_type_name(exc)}",
                               "result_error": _type_name(exc)}
            out = {"run_id": rid, "project_id": pid,
                   "execution_state": state, "goal_verdict": verdict,
                   "pending_kind": pending, "idempotent": False}
            out.update({key: result_info.get(key) for key in
                        ("result_saved", "basis_status", "basis_reason",
                         "basis_sha256", "result_error") if key in result_info})
            return out


def _list_open_items_locked(db: sqlite3.Connection, run_id: str) -> list[dict]:
    rows = db.execute(
        "SELECT * FROM project_run_open_items WHERE run_id=? ORDER BY id",
        (run_id,)).fetchall()
    out: list[dict] = []
    for row in rows:
        item = dict(row)
        raw = str(item.get("artifacts_json") or "[]")
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = []
        item["artifacts"] = parsed if isinstance(parsed, list) else []
        if not str(item.get("responsibility") or ""):
            item["responsibility"] = "unclassified"
        elif str(item.get("responsibility") or "") not in RUN_RESPONSIBILITIES:
            item["responsibility"] = "unclassified"
        else:
            item["responsibility"] = str(item.get("responsibility") or "")
        out.append(item)
    return out


def _open_item_id(raw: object) -> int | None:
    if raw is None:
        return None
    try:
        return int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _split_current_and_history(items: list[dict]) -> tuple[list[dict], list[dict]]:
    """訂正で置き換えられた古い版を現在の残件から除く。

    新しい行の supersedes_id が指す行は古い版(履歴)。置き換えられて
    いない行だけが現在。判定は supersedes_id の集合で決定的に行う。
    """
    superseded: set[int] = set()
    for item in items:
        target = _open_item_id(item.get("supersedes_id"))
        if target is not None:
            superseded.add(target)
    current = [item for item in items if _open_item_id(item.get("id")) not in superseded]
    return current, list(items)


def _open_item_view(item: dict) -> dict:
    responsibility = str(item.get("responsibility") or "")
    if responsibility not in RUN_RESPONSIBILITIES:
        responsibility = "unclassified"
    return {
        "id": int(item.get("id") or 0),
        "item_id": str(item.get("item_id") or ""),
        "kind": str(item.get("kind") or ""),
        "status_text": str(item.get("status_text") or ""),
        "evidence_needed": str(item.get("evidence_needed") or ""),
        "owner": str(item.get("owner") or ""),
        "next_action": str(item.get("next_action") or ""),
        "artifacts": item.get("artifacts") or [],
        "responsibility": responsibility,
        "version": _open_item_version(item.get("version")),
        "supersedes_id": item.get("supersedes_id"),
    }


def _open_item_version(raw: object) -> int:
    if raw is None:
        return 1
    try:
        return int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ValueError("open item version is unreadable") from None


def _result_view(row: dict) -> dict:
    counts: dict | None = None
    raw_counts = str(row.get("counts_json") or "")
    if raw_counts:
        try:
            parsed = json.loads(raw_counts)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            normalized = _normalize_counts(parsed)
            counts = normalized
    open_items: list[dict] = []
    raw_open = str(row.get("open_items_json") or "[]")
    try:
        parsed_open = json.loads(raw_open)
    except ValueError:
        parsed_open = []
    if isinstance(parsed_open, list):
        for item in parsed_open:
            if not isinstance(item, dict):
                continue
            responsibility = str(item.get("responsibility") or "")
            if responsibility not in RUN_RESPONSIBILITIES:
                responsibility = "unclassified"
            open_items.append({
                "id": int(item.get("id") or 0),
                "item_id": str(item.get("item_id") or ""),
                "kind": str(item.get("kind") or ""),
                "responsibility": responsibility,
            })
    status = str(row.get("basis_status") or "unavailable")
    if status not in ("available", "unavailable"):
        status = "unavailable"
    if status == "available" and counts is None:
        status = "unavailable"
    return {
        "run_id": str(row.get("run_id") or ""),
        "project_id": str(row.get("project_id") or ""),
        "goal_verdict": str(row.get("goal_verdict") or ""),
        "counts": counts,
        "open_items": open_items,
        "verification_ref": str(row.get("verification_ref") or ""),
        "verification_hash": str(row.get("verification_hash") or ""),
        "basis_status": status,
        "basis_reason": str(row.get("basis_reason") or ""),
        "basis_sha256": str(row.get("basis_sha256") or ""),
        "recorded_at": row.get("recorded_at"),
        "read_only": True,
    }


def get_run_result(memory_path: str | Path, project_id: str,
                   run_id: str) -> dict | None:
    """当時の最小結果を読み取り専用で返す。他PJの run_id は None(404相当)。

    読取失敗は RunCycleReadError(空や成功に倒さない)。結果行が無いが
    同一PJのサイクルがある場合は unavailable として返す(404にしない)。
    """
    pid = str(project_id or "")
    rid = str(run_id or "")
    if not pid:
        raise ValueError("project_id is required")
    if not rid:
        raise ValueError("run_id is required")
    path = item_ledger_db_path(memory_path)
    if not path.exists():
        return None
    with _connect_ro(memory_path) as db:
        try:
            _require_run_tables(db)
        except RunCycleReadError as exc:
            if exc.kind == "table_missing":
                return None
            raise
        try:
            cycle = db.execute(
                "SELECT * FROM project_run_cycles WHERE run_id=?",
                (rid,)).fetchone()
        except sqlite3.Error as exc:
            raise RunCycleReadError(
                "row_unreadable",
                path_kind=classify_ledger_path_kind(path),
                sqlite_error=_type_name(exc)) from exc
        if cycle is None:
            return None
        if str(dict(cycle).get("project_id") or "") != pid:
            return None
        if not _result_table_exists(db):
            saved_cycle = dict(cycle)
            return {
                "run_id": rid, "project_id": pid,
                "goal_verdict": str(saved_cycle.get("goal_verdict") or ""),
                "counts": None, "open_items": [],
                "verification_ref": str(saved_cycle.get("verification_ref") or ""),
                "verification_hash": str(saved_cycle.get("verification_hash") or ""),
                "basis_status": "unavailable",
                "basis_reason": "保存なし（機能追加前の実行）",
                "basis_sha256": "", "recorded_at": None,
                "read_only": True,
            }
        try:
            row = db.execute(
                "SELECT * FROM project_run_results WHERE run_id=?",
                (rid,)).fetchone()
        except sqlite3.Error as exc:
            raise RunCycleReadError(
                "row_unreadable",
                path_kind=classify_ledger_path_kind(path),
                sqlite_error=_type_name(exc)) from exc
        if row is None:
            saved_cycle = dict(cycle)
            return {
                "run_id": rid, "project_id": pid,
                "goal_verdict": str(saved_cycle.get("goal_verdict") or ""),
                "counts": None, "open_items": [],
                "verification_ref": str(saved_cycle.get("verification_ref") or ""),
                "verification_hash": str(saved_cycle.get("verification_hash") or ""),
                "basis_status": "unavailable",
                "basis_reason": "保存なし（機能追加前の実行）",
                "basis_sha256": "", "recorded_at": None,
                "read_only": True,
            }
        return _result_view(dict(row))


def list_project_runs(memory_path: str | Path, project_id: str,
                      limit: int = 50) -> list[dict]:
    """履歴一覧を新しい順で返す。各行に goal_verdict・basis_status を含める。

    読取失敗は RunCycleReadError(空に倒さない)。
    """
    pid = str(project_id or "")
    if not pid:
        raise ValueError("project_id is required")
    try:
        capped = max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        capped = 50
    path = item_ledger_db_path(memory_path)
    if not path.exists():
        return []
    with _connect_ro(memory_path) as db:
        try:
            _require_run_tables(db)
        except RunCycleReadError as exc:
            if exc.kind == "table_missing":
                return []
            raise
        try:
            rows = db.execute(
                "SELECT * FROM project_run_cycles WHERE project_id=? "
                "ORDER BY started_at DESC, rowid DESC LIMIT ?",
                (pid, capped)).fetchall()
        except sqlite3.Error as exc:
            raise RunCycleReadError(
                "row_unreadable",
                path_kind=classify_ledger_path_kind(path),
                sqlite_error=_type_name(exc)) from exc
        has_results = _result_table_exists(db)
        results: dict[str, dict] = {}
        if has_results and rows:
            try:
                for record in db.execute(
                        "SELECT * FROM project_run_results WHERE project_id=?",
                        (pid,)).fetchall():
                    item = dict(record)
                    results[str(item.get("run_id") or "")] = item
            except sqlite3.Error as exc:
                raise RunCycleReadError(
                    "row_unreadable",
                    path_kind=classify_ledger_path_kind(path),
                    sqlite_error=_type_name(exc)) from exc
        out: list[dict] = []
        for row in rows:
            saved = dict(row)
            rid = str(saved.get("run_id") or "")
            record = results.get(rid)
            if record is None:
                out.append({
                    "run_id": rid,
                    "project_id": pid,
                    "goal_verdict": str(saved.get("goal_verdict") or ""),
                    "execution_state": str(saved.get("execution_state") or ""),
                    "basis_status": "unavailable",
                    "basis_reason": "保存なし（機能追加前の実行）",
                    "recorded_at": None,
                })
            else:
                viewed = _result_view(record)
                out.append({
                    "run_id": rid,
                    "project_id": pid,
                    "goal_verdict": str(saved.get("goal_verdict") or ""),
                    "execution_state": str(saved.get("execution_state") or ""),
                    "basis_status": viewed["basis_status"],
                    "basis_reason": viewed["basis_reason"],
                    "recorded_at": viewed["recorded_at"],
                })
        return out


def get_latest_run(memory_path: str | Path, project_id: str) -> dict:
    """最新の実行と残件を読み取り専用で返す。

    記録が無ければ found=False の『実行履歴未判定』。読取失敗は
    RunCycleReadError(空や成功に倒さない)。PJの状態は書き換えない。
    at_run(当時保存した最小結果)と current(現在のサイクルと残件の現在版)を
    別のキーで返す(混ぜない)。過去の判定は書き換わらない。
    """
    pid = str(project_id or "")
    if not pid:
        raise ValueError("project_id is required")
    path = item_ledger_db_path(memory_path)
    if not path.exists():
        return {"found": False, "project_id": pid,
                "status": "no_run_history",
                "note": "実行履歴未判定: 実行サイクル記録がありません"}
    with _connect_ro(memory_path) as db:
        try:
            _require_run_tables(db)
        except RunCycleReadError as exc:
            # 台帳DBは読めるが2表がまだ無い(移行直後の旧PJ)は、
            # 『実行履歴未判定』として返す。読取失敗とは区別する。
            if exc.kind == "table_missing":
                return {"found": False, "project_id": pid,
                        "status": "no_run_history",
                        "note": "実行履歴未判定: 実行サイクル記録がありません"}
            raise
        try:
            row = db.execute(
                "SELECT * FROM project_run_cycles WHERE project_id=? "
                "ORDER BY started_at DESC, rowid DESC LIMIT 1",
                (pid,)).fetchone()
        except sqlite3.Error as exc:
            raise RunCycleReadError(
                "row_unreadable",
                path_kind=classify_ledger_path_kind(path),
                sqlite_error=_type_name(exc)) from exc
        if row is None:
            return {"found": False, "project_id": pid,
                    "status": "no_run_history",
                    "note": "実行履歴未判定: 実行サイクル記録がありません"}
        saved = dict(row)
        try:
            items = _list_open_items_locked(db, str(saved.get("run_id") or ""))
        except sqlite3.Error as exc:
            raise RunCycleReadError(
                "row_unreadable",
                path_kind=classify_ledger_path_kind(path),
                sqlite_error=_type_name(exc)) from exc
        current, _history_all = _split_current_and_history(items)
        # 当時の最小結果(読み取り専用・保存済み)。無ければ unavailable。
        at_run: dict | None = None
        try:
            if _result_table_exists(db):
                record = db.execute(
                    "SELECT * FROM project_run_results WHERE run_id=?",
                    (str(saved.get("run_id") or ""),)).fetchone()
                at_run = _result_view(dict(record)) if record is not None else None
        except sqlite3.Error as exc:
            raise RunCycleReadError(
                "row_unreadable",
                path_kind=classify_ledger_path_kind(path),
                sqlite_error=_type_name(exc)) from exc
        if at_run is None:
            at_run = {
                "run_id": str(saved.get("run_id") or ""),
                "project_id": pid,
                "goal_verdict": str(saved.get("goal_verdict") or ""),
                "counts": None, "open_items": [],
                "verification_ref": str(saved.get("verification_ref") or ""),
                "verification_hash": str(saved.get("verification_hash") or ""),
                "basis_status": "unavailable",
                "basis_reason": "保存なし（機能追加前の実行）",
                "basis_sha256": "", "recorded_at": None,
                "read_only": True,
            }
    open_view = [_open_item_view(item) for item in current]
    history_view = [_open_item_view(item) for item in items]
    current_view = {
        "execution_state": str(saved.get("execution_state") or ""),
        "goal_verdict": str(saved.get("goal_verdict") or ""),
        "pending_kind": str(saved.get("pending_kind") or ""),
        "stop_reason": str(saved.get("stop_reason") or ""),
        "plan_signature": str(saved.get("plan_signature") or ""),
        "source_hash": str(saved.get("source_hash") or ""),
        "verification_ref": str(saved.get("verification_ref") or ""),
        "verification_hash": str(saved.get("verification_hash") or ""),
        "open_items": open_view,
    }
    return {
        "found": True,
        "project_id": pid,
        "run_id": str(saved.get("run_id") or ""),
        "execution_state": str(saved.get("execution_state") or ""),
        "goal_verdict": str(saved.get("goal_verdict") or ""),
        "pending_kind": str(saved.get("pending_kind") or ""),
        "stop_reason": str(saved.get("stop_reason") or ""),
        "plan_signature": str(saved.get("plan_signature") or ""),
        "source_hash": str(saved.get("source_hash") or ""),
        "verification_ref": str(saved.get("verification_ref") or ""),
        "verification_hash": str(saved.get("verification_hash") or ""),
        "open_items": open_view,
        "open_item_history": history_view,
        "at_run": at_run,
        "current": current_view,
        "read_only": True,
    }


def purge_project_run_cycles(memory_path: str | Path, project_id: str) -> dict:
    """完全削除時は当該PJの3表の行だけ消す。他PJに影響しない。"""
    pid = str(project_id or "")
    path = item_ledger_db_path(memory_path)
    if not path.exists() or not pid:
        return {"project_run_cycles": 0, "project_run_open_items": 0,
                "project_run_results": 0}
    try:
        db = sqlite3.connect(str(path), timeout=30)
    except (OSError, sqlite3.Error) as exc:
        LOGGER.warning("run cycle purge connect failed: %s", _type_name(exc))
        raise RuntimeError(
            f"run cycle purge connect failed: {_type_name(exc)}") from exc
    try:
        with db:
            try:
                names = {row[0] for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            except sqlite3.Error as exc:
                raise RuntimeError(
                    f"run cycle purge table inspection failed: {_type_name(exc)}") from exc
            # 実行を一度も記録していないPJは表が無い。削除対象なし(0件)として成功にする。
            # 表がある場合の削除失敗は従来どおり例外(失敗)として返す。
            removed = {"project_run_cycles": 0, "project_run_open_items": 0,
                       "project_run_results": 0}
            try:
                if "project_run_open_items" in names:
                    cur = db.execute(
                        "DELETE FROM project_run_open_items WHERE project_id=?", (pid,))
                    removed["project_run_open_items"] = int(cur.rowcount or 0)
                if "project_run_cycles" in names:
                    cur = db.execute(
                        "DELETE FROM project_run_cycles WHERE project_id=?", (pid,))
                    removed["project_run_cycles"] = int(cur.rowcount or 0)
                if "project_run_results" in names:
                    cur = db.execute(
                        "DELETE FROM project_run_results WHERE project_id=?", (pid,))
                    removed["project_run_results"] = int(cur.rowcount or 0)
            except sqlite3.Error as exc:
                raise RuntimeError(
                    f"run cycle purge delete failed: {_type_name(exc)}") from exc
            return removed
    finally:
        try:
            db.close()
        except sqlite3.Error:
            pass


def reset_for_generation(memory_path: str | Path, project_id: str) -> dict:
    """PJ初期化(世代切替)では記録を変えない。証拠として残す。"""
    return {"project_id": str(project_id or ""), "reset": False,
            "note": "実行サイクル記録は世代切替で変えません"}
