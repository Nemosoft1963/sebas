"""C-3 人が起動する夜間実行 (サーバ側の状態と判定)。

一般機構。特定PJ・件数・IDに依存しない。人が起動したときだけ動き、
常駐しない (定期スレッド・スケジューラ・タイマーを作らない。通知は画面内のみ)。
すでに人が自走を有効にしたPJだけを、時間枠・予算・回路遮断の内側で、
既存の autopilot_tick (1ティック=許可された1作業) を順番に回し、朝に実行報告を残す。
夜間実行が自走を有効にすることはない。

安全境界 (既存の自走の境界は変えない):
- 許可リスト・既定無効・1ティック1作業・最大10ティック・空転ガードは既存のまま。
- 外部増分0・A-1のゲート・F1のfail-closedは既存のまま。
- 承認・外部送信・Jenkins送信・適用・RAG登録・OCR投入・完全削除は夜間実行でも自動化しない。
- 秘密や本文を入れない (IDと件数と状態のみ)。

サイドカーSQLite (*.nightrun.sqlite3、既存の流儀。追記専用・排他):
- night_runs: 実行ごとの記録。
- night_run_steps: ティック結果の要約 (status・stop_class・action・数値のみ)。
- kill_switch: 全体停止スイッチ (DB側。ファイル側と両持ち)。
"""
from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

FORMAT = "c3-nightrun/1"

RUNNING = "running"
FINISHED = "finished"
ABORTED = "aborted"
KILLED = "killed"
EXPIRED = "expired"
RUN_STATES = (RUNNING, FINISHED, ABORTED, KILLED, EXPIRED)

KILL_ACTIVE = "active"
KILL_INACTIVE = "inactive"
KILL_UNKNOWN = "unknown"

HEARTBEAT_GRACE_SEC = 300.0

DEFAULT_BUDGET = {
    "max_ticks": 60,
    "per_project": 5,
    "max_seconds": 6 * 3600,
    "max_consec_failures": 3,
    "max_consec_preflight": 3,
}
BUDGET_HARD_CAP = {
    "max_ticks": 1000,
    "per_project": 10,
    "max_seconds": 12 * 3600,
    "max_consec_failures": 10,
    "max_consec_preflight": 10,
}

PREFLIGHT_STATES = ("preflight_unknown", "stale", "failed")
NO_PROGRESS_STATES = ("stopped_guard", "disabled", "busy", "kill_switch")

# 報告の「人の次の操作」(決定的マップ。おすすめは付けない)。
NEXT_ACTION_JA = {
    "advanced": "進んだ一手があります。内容を確認してください",
    "waiting_human": "人の判断待ちです。判断内容を確認してください",
    "failed": "失敗しました。原因を確認してください",
    "stopped_guard": "同じ停止の繰り返しのため止まりました。計画を見直してください",
    "preflight_unknown": "事前照合を確認できません。状態を確認してください",
    "stale": "分類が古い署名のため止まりました。再判定してください",
    "disabled": "自走が無効です。有効化は人が判断してください",
    "busy": "実行中のため回せませんでした。状態を確認してください",
    "kill_switch": "全体停止スイッチのため止まりました。解除は人が判断してください",
    "excluded": "この夜の対象から外しました。理由を確認してください",
    "unreadable": "結果を確認できません。状態を確認してください",
}


def _type_name(exc: BaseException) -> str:
    try:
        return str(type(exc).__name__ or "Error")
    except Exception:
        return "Error"


def _safe_text(value: object, limit: int = 200) -> str:
    blob = str(value if value is not None else "")
    return " ".join(blob.split())[: max(1, int(limit))]


def _utcnow() -> float:
    return time.time()


def _resolve_mem_path(manager_or_path) -> Path:
    if hasattr(manager_or_path, "memory"):
        return Path(manager_or_path.memory.path)
    return Path(manager_or_path)


def nightrun_db_path(manager_or_path) -> Path:
    mem = _resolve_mem_path(manager_or_path)
    return mem.with_name(mem.name + ".nightrun.sqlite3")


def kill_file_path(manager_or_path) -> Path:
    """全体停止スイッチのファイル側。DATA_DIR 規則を踏襲する。"""
    mem = _resolve_mem_path(manager_or_path)
    parent = mem.parent
    data_dir = parent.parent if parent.name == "memory" else parent
    return data_dir / "AUTOPILOT_KILL"


_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(key: str) -> threading.Lock:
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _locks[key] = lock
        return lock


def _ensure_tables(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS night_runs(
        id TEXT PRIMARY KEY,
        actor TEXT NOT NULL DEFAULT '',
        started_at REAL NOT NULL,
        finished_at REAL NOT NULL DEFAULT 0,
        state TEXT NOT NULL DEFAULT 'running',
        budget_json TEXT NOT NULL DEFAULT '{}',
        end_reason TEXT NOT NULL DEFAULT '',
        heartbeat_at REAL NOT NULL,
        token_sha256 TEXT NOT NULL DEFAULT '',
        excluded_json TEXT NOT NULL DEFAULT '[]',
        report_json TEXT NOT NULL DEFAULT '',
        report_status TEXT NOT NULL DEFAULT '')""")
    db.execute("""CREATE TABLE IF NOT EXISTS night_run_steps(
        id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL,
        seq INTEGER NOT NULL,
        project_id TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT '',
        stop_class TEXT NOT NULL DEFAULT '',
        action TEXT NOT NULL DEFAULT '',
        external_delta INTEGER NOT NULL DEFAULT 0,
        external_rounds_delta INTEGER NOT NULL DEFAULT 0,
        budget_kept INTEGER NOT NULL DEFAULT 1,
        created_at REAL NOT NULL)""")
    db.execute("""CREATE INDEX IF NOT EXISTS idx_night_run_steps_run
        ON night_run_steps(run_id, seq)""")
    db.execute("""CREATE TABLE IF NOT EXISTS kill_switch(
        id TEXT PRIMARY KEY,
        state TEXT NOT NULL DEFAULT 'inactive',
        updated_at REAL NOT NULL,
        actor TEXT NOT NULL DEFAULT '')""")


@contextmanager
def _connect(manager_or_path):
    path = nightrun_db_path(manager_or_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=30)
    db.row_factory = sqlite3.Row
    try:
        with db:
            _ensure_tables(db)
        yield db
    finally:
        db.close()


def _read_connection(manager_or_path):
    path = nightrun_db_path(manager_or_path)
    if not path.exists():
        return None
    db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True,
                         timeout=15)
    db.row_factory = sqlite3.Row
    return db


def _decode_run(row: sqlite3.Row) -> dict:
    item = dict(row)
    try:
        budget = json.loads(item.get("budget_json") or "{}")
    except (ValueError, TypeError):
        budget = {}
    try:
        excluded = json.loads(item.get("excluded_json") or "[]")
    except (ValueError, TypeError):
        excluded = []
    try:
        report = json.loads(item.get("report_json") or "null")
    except (ValueError, TypeError):
        report = None
    if not isinstance(budget, dict):
        budget = {}
    if not isinstance(excluded, list):
        excluded = []
    return {
        "run_id": str(item.get("id") or ""),
        "actor": str(item.get("actor") or ""),
        "started_at": float(item.get("started_at") or 0),
        "finished_at": float(item.get("finished_at") or 0),
        "state": str(item.get("state") or ""),
        "budget": budget,
        "end_reason": str(item.get("end_reason") or ""),
        "heartbeat_at": float(item.get("heartbeat_at") or 0),
        "excluded": excluded,
        "report": report,
        "report_status": str(item.get("report_status") or ""),
    }


def _decode_step(row: sqlite3.Row) -> dict:
    item = dict(row)
    try:
        seq = int(item.get("seq") or 0)
    except (TypeError, ValueError):
        seq = 0
    try:
        external_delta = int(item.get("external_delta") or 0)
    except (TypeError, ValueError):
        external_delta = 0
    try:
        rounds_delta = int(item.get("external_rounds_delta") or 0)
    except (TypeError, ValueError):
        rounds_delta = 0
    try:
        created = float(item.get("created_at") or 0)
    except (TypeError, ValueError):
        created = 0
    return {
        "id": str(item.get("id") or ""),
        "run_id": str(item.get("run_id") or ""),
        "seq": seq,
        "project_id": str(item.get("project_id") or ""),
        "status": str(item.get("status") or ""),
        "stop_class": str(item.get("stop_class") or ""),
        "action": str(item.get("action") or ""),
        "external_delta": external_delta,
        "external_rounds_delta": rounds_delta,
        "budget_kept": bool(item.get("budget_kept")),
        "created_at": created,
    }


class NightRunBusy(RuntimeError):
    """すでに夜間実行が走っている (API層で409に変換する)。"""


def normalize_budget(raw: dict | None) -> dict:
    """起動時の予算を正規化する。欠落は既定値。不正・上限超過は例外。

    per_project は10を超えられない (既存の最大10ティックの境界)。
    """
    merged = dict(DEFAULT_BUDGET)
    if raw is not None:
        if not isinstance(raw, dict):
            raise ValueError("budget の形式を確認できません")
        for key in ("max_ticks", "per_project", "max_seconds",
                    "max_consec_failures", "max_consec_preflight"):
            if key in raw and raw[key] is not None:
                try:
                    merged[key] = int(raw[key])
                except (TypeError, ValueError):
                    raise ValueError(f"budget の {key} を確認できません")
    if "until_epoch" in (raw or {}):
        if raw is None:
            raise ValueError("budget の形式を確認できません")
        try:
            merged["until_epoch"] = float(raw.get("until_epoch") or 0)
        except (TypeError, ValueError):
            raise ValueError("budget の until_epoch を確認できません")
        if not merged["until_epoch"] > 0:
            raise ValueError("budget の until_epoch を確認できません")
    for key in ("max_ticks", "per_project", "max_seconds",
                "max_consec_failures", "max_consec_preflight"):
        if not merged[key] >= 1:
            raise ValueError(f"budget の {key} は1以上が必要です")
        if not merged[key] <= BUDGET_HARD_CAP[key]:
            raise ValueError(f"budget の {key} が上限を超えています")
    if int(merged["per_project"]) > 10:
        raise ValueError("budget の per_project は10を超えられません")
    return merged


def _fresh_running_row(db: sqlite3.Connection) -> dict | None:
    try:
        rows = db.execute("SELECT * FROM night_runs WHERE state='running' "
                          "ORDER BY started_at DESC").fetchall()
    except sqlite3.Error as exc:
        raise ValueError("夜間実行の記録を確認できません:"
                         + _type_name(exc)) from exc
    for row in rows:
        decoded = _decode_run(row)
        if decoded.get("run_id"):
            return decoded
    return None


def _expire_if_stale(db: sqlite3.Connection, run: dict) -> bool:
    """heartbeat途絶の実行を expired にする。猶予内なら False。"""
    try:
        age = _utcnow() - float(run.get("heartbeat_at") or 0)
    except (TypeError, ValueError):
        age = HEARTBEAT_GRACE_SEC + 1
    if age <= HEARTBEAT_GRACE_SEC:
        return False
    try:
        db.execute("UPDATE night_runs SET state='expired', "
                   "end_reason='heartbeat_lost', finished_at=? WHERE id=?",
                   (_utcnow(), str(run.get("run_id") or "")))
    except sqlite3.Error as exc:
        raise ValueError("夜間実行の記録を更新できません:"
                         + _type_name(exc)) from exc
    return True


def start_run(manager, actor: str, budget: dict | None = None) -> dict:
    """夜間実行の受付。実際のティックは回さない。IDとトークンを返す。

    同時に走る夜間実行は1つだけ。heartbeat猶予内の running があれば
    NightRunBusy (409)。heartbeat途絶は expired 化して新規を許す。
    判断不能 (DB読取不能) のときは新規を許さない (fail-closed)。
    """
    actor_name = _safe_text(actor, 100).strip()
    if not actor_name:
        raise ValueError("actor is required")
    plan = normalize_budget(budget)
    lock = _lock_for("nightrun-global")
    with lock:
        with _connect(manager) as db:
            db.execute("BEGIN IMMEDIATE")
            existing = _fresh_running_row(db)
            if existing is not None:
                if not _expire_if_stale(db, existing):
                    db.commit()
                    raise NightRunBusy("夜間実行が既に走っています")
            run_id = "nr_" + uuid.uuid4().hex[:16]
            token = secrets.token_urlsafe(32)
            digest = __import__("hashlib").sha256(token.encode()).hexdigest()
            now = _utcnow()
            try:
                db.execute(
                    """INSERT INTO night_runs(id, actor, started_at,
                           finished_at, state, budget_json, end_reason,
                           heartbeat_at, token_sha256, excluded_json,
                           report_json, report_status)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (run_id, actor_name, now, 0, RUNNING,
                     json.dumps(plan, ensure_ascii=False, sort_keys=True),
                     "", now, digest, "[]", "", ""))
            except sqlite3.Error as exc:
                raise ValueError("夜間実行の記録に失敗しました:"
                                 + _type_name(exc)) from exc
            db.commit()
    return {"run_id": run_id, "token": token, "state": RUNNING,
            "actor": actor_name, "started_at": now, "budget": plan}


def _check_token(run_id: str, token: str, db: sqlite3.Connection) -> dict:
    try:
        row = db.execute("SELECT * FROM night_runs WHERE id=?",
                         (str(run_id or ""),)).fetchone()
    except sqlite3.Error as exc:
        raise ValueError("夜間実行の記録を確認できません:"
                         + _type_name(exc)) from exc
    if row is None:
        raise ValueError("夜間実行が見つかりません")
    run = _decode_run(row)
    if not token or not str(token).strip():
        raise ValueError("token is required")
    import hashlib as _hl

    digest = _hl.sha256(str(token).encode()).hexdigest()
    try:
        stored = db.execute("SELECT token_sha256 FROM night_runs WHERE id=?",
                            (str(run_id or ""),)).fetchone()
    except sqlite3.Error as exc:
        raise ValueError("夜間実行の記録を確認できません:"
                         + _type_name(exc)) from exc
    if stored is None or str(stored["token_sha256"] or "") != digest:
        raise ValueError("token が一致しません")
    return run


def get_run(manager_or_path, run_id: str) -> dict:
    db = _read_connection(manager_or_path)
    if db is None:
        raise ValueError("夜間実行が見つかりません")
    try:
        try:
            row = db.execute("SELECT * FROM night_runs WHERE id=?",
                             (str(run_id or ""),)).fetchone()
        except sqlite3.Error as exc:
            raise ValueError("夜間実行の記録を確認できません:"
                             + _type_name(exc)) from exc
        if row is None:
            raise ValueError("夜間実行が見つかりません")
        run = _decode_run(row)
        try:
            steps = db.execute("SELECT * FROM night_run_steps WHERE run_id=? "
                               "ORDER BY seq ASC",
                               (str(run_id or ""),)).fetchall()
        except sqlite3.Error as exc:
            raise ValueError("夜間実行の記録を確認できません:"
                             + _type_name(exc)) from exc
        run["steps"] = [_decode_step(item) for item in steps]
        return run
    finally:
        db.close()


def get_latest(manager_or_path) -> dict:
    """最新の実行 (報告つき。読み取り専用)。実行が無ければ理由を返す。"""
    db = _read_connection(manager_or_path)
    if db is None:
        return {"has_run": False, "reason": "実行なし"}
    try:
        try:
            row = db.execute("SELECT * FROM night_runs ORDER BY started_at DESC "
                             "LIMIT 1").fetchone()
        except sqlite3.Error as exc:
            raise ValueError("夜間実行の記録を確認できません:"
                             + _type_name(exc)) from exc
        if row is None:
            return {"has_run": False, "reason": "実行なし"}
        run = _decode_run(row)
        try:
            steps = db.execute("SELECT * FROM night_run_steps WHERE run_id=? "
                               "ORDER BY seq ASC",
                               (str(run.get("run_id") or ""),)).fetchall()
        except sqlite3.Error as exc:
            raise ValueError("夜間実行の記録を確認できません:"
                             + _type_name(exc)) from exc
        run["steps"] = [_decode_step(item) for item in steps]
        run["has_run"] = True
        return run
    finally:
        db.close()


def _excluded_map(run: dict) -> dict:
    out: dict[str, str] = {}
    raw = run.get("excluded")
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and item.get("project_id"):
                out[str(item["project_id"])] = str(item.get("reason") or "")
    return out


def _consec_counts(steps: list[dict]) -> tuple[int, int]:
    failures = 0
    preflight = 0
    for item in reversed(steps):
        status = str(item.get("status") or "") if isinstance(item, dict) else ""
        if status == "failed":
            failures += 1
        else:
            break
    for item in reversed(steps):
        status = str(item.get("status") or "") if isinstance(item, dict) else ""
        if status in PREFLIGHT_STATES:
            preflight += 1
        else:
            break
    return failures, preflight


def _candidate_projects(manager, run: dict, steps: list[dict]) -> tuple[list[str], dict[str, str]]:
    """次に回せるPJを決定的に選ぶ。夜間実行が自走を有効にしない。

    戻り値 (候補ID一覧, 除外理由)。自走が有効で、削除でなく、除外されておらず、
    予算内 (per-PJ) のものだけ。読取不能・不明は候補にしない。
    """
    from app import autopilot as _auto
    from app.project_delete import is_deleted_or_unknown

    try:
        rows = manager.memory.list_projects()
    except Exception as exc:
        raise ValueError("PJ一覧を確認できません:" + _type_name(exc)) from exc
    if not isinstance(rows, list):
        raise ValueError("PJ一覧の形式を確認できません")
    try:
        per_limit = int((run.get("budget") or {}).get("per_project")
                        or DEFAULT_BUDGET["per_project"])
    except (TypeError, ValueError, AttributeError):
        raise ValueError("予算を確認できません:ValueError")
    per_count: dict[str, int] = {}
    for item in steps:
        if not isinstance(item, dict):
            continue
        pid = str(item.get("project_id") or "")
        if pid:
            per_count[pid] = int(per_count.get(pid) or 0) + 1
    excluded = _excluded_map(run)
    ids: list[str] = []
    skipped: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        pid = row.get("id")
        if pid is None or str(pid) == "":
            continue
        pid_text = str(pid)
        if pid_text in excluded:
            skipped[pid_text] = str(excluded.get(pid_text) or "excluded")
            continue
        if int(per_count.get(pid_text) or 0) >= per_limit:
            skipped[pid_text] = "per_project_budget"
            continue
        try:
            blocked = is_deleted_or_unknown(manager.memory.path, pid_text)
        except Exception:
            skipped[pid_text] = "確認できず(delete_state)"
            continue
        if blocked:
            skipped[pid_text] = "deleted_or_unknown"
            continue
        try:
            enabled = _auto.is_enabled(manager, pid_text)
        except Exception:
            skipped[pid_text] = "確認できず(enabled)"
            continue
        if not enabled:
            skipped[pid_text] = "disabled"
            continue
        ids.append(pid_text)
    ids.sort()
    return ids, skipped


def _append_step(db: sqlite3.Connection, run_id: str, seq: int,
                 project_id: str, summary: dict) -> dict:
    if not isinstance(summary, dict):
        summary = {}
    status = str(summary.get("status") or "unreadable")
    stop_class = str(summary.get("stop_class") or "")
    action = str(summary.get("action") or summary.get("action_name") or "")
    try:
        external_delta = int(summary.get("external_delta") or 0)
    except (TypeError, ValueError):
        external_delta = 0
    try:
        rounds_delta = int(summary.get("external_rounds_delta") or
                           summary.get("rounds_delta") or 0)
    except (TypeError, ValueError):
        rounds_delta = 0
    budget_kept = summary.get("budget_kept")
    if budget_kept is None:
        budget_flag = 0
    else:
        budget_flag = 1 if bool(budget_kept) else 0
    step_id = "ns_" + uuid.uuid4().hex[:16]
    try:
        current = db.execute("SELECT MAX(seq) FROM night_run_steps "
                             "WHERE run_id=?", (str(run_id or ""),)).fetchone()
    except sqlite3.Error as exc:
        raise ValueError("夜間実行の記録を確認できません:"
                         + _type_name(exc)) from exc
    try:
        next_seq = int((current[0] if current else 0) or 0) + 1
    except (TypeError, ValueError):
        next_seq = int(seq)
    use_seq = max(int(seq), int(next_seq))
    try:
        db.execute(
            """INSERT INTO night_run_steps(id, run_id, seq, project_id, status,
                   stop_class, action, external_delta, external_rounds_delta,
                   budget_kept, created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (step_id, str(run_id or ""), int(use_seq), str(project_id or "")[:128],
             status[:64], stop_class[:64], action[:64], external_delta,
             rounds_delta, budget_flag, _utcnow()))
    except sqlite3.Error as exc:
        raise ValueError("夜間実行の記録に失敗しました:"
                         + _type_name(exc)) from exc
    return {"id": step_id, "run_id": str(run_id or ""), "seq": int(use_seq),
            "project_id": str(project_id or ""), "status": status,
            "stop_class": stop_class, "action": action,
            "external_delta": external_delta,
            "external_rounds_delta": rounds_delta,
            "budget_kept": bool(budget_flag)}


def _set_excluded(db: sqlite3.Connection, run_id: str, project_id: str,
                  reason: str) -> None:
    try:
        row = db.execute("SELECT excluded_json FROM night_runs WHERE id=?",
                         (str(run_id or ""),)).fetchone()
    except sqlite3.Error as exc:
        raise ValueError("夜間実行の記録を確認できません:"
                         + _type_name(exc)) from exc
    if row is None:
        raise ValueError("夜間実行が見つかりません")
    try:
        current = json.loads(str(row["excluded_json"] or "[]"))
    except (ValueError, TypeError):
        current = []
    if not isinstance(current, list):
        current = []
    known = {str(item.get("project_id")) for item in current
             if isinstance(item, dict) and item.get("project_id")}
    if str(project_id or "") not in known:
        current.append({"project_id": str(project_id or ""),
                        "reason": _safe_text(reason, 200)})
    try:
        db.execute("UPDATE night_runs SET excluded_json=? WHERE id=?",
                   (json.dumps(current, ensure_ascii=False), str(run_id or "")))
    except sqlite3.Error as exc:
        raise ValueError("夜間実行の記録を更新できません:"
                         + _type_name(exc)) from exc


def _abort_run(db: sqlite3.Connection, run_id: str, reason: str) -> None:
    try:
        db.execute("UPDATE night_runs SET state='aborted', end_reason=?, "
                   "finished_at=? WHERE id=?",
                   (_safe_text(reason, 500), _utcnow(), str(run_id or "")))
    except sqlite3.Error as exc:
        raise ValueError("夜間実行の記録を更新できません:"
                         + _type_name(exc)) from exc


def tick_next(manager, run_id: str, token: str) -> dict:
    """次に回すPJをサーバ側で選び、1ティックだけ実行して記録する。

    キルスイッチ・予算・回路遮断の判定は必ずサーバ側で行う。
    戻り値 {done, reason, step}。done=True はもう回さないこと。
    """
    from app import autopilot as _auto

    lock = _lock_for("nightrun-global")
    with lock:
        with _connect(manager) as db:
            db.execute("BEGIN IMMEDIATE")
            run = _check_token(str(run_id or ""), token, db)
            rid = str(run.get("run_id") or "")
            if str(run.get("state") or "") != RUNNING:
                db.commit()
                return {"done": True,
                        "reason": "実行は既に確定しています:"
                        + str(run.get("state") or ""),
                        "step": None, "run_id": rid,
                        "state": str(run.get("state") or "")}
            # キルスイッチ (止める側が優先)。
            kill = kill_switch_state(manager)
            if kill in (KILL_ACTIVE, KILL_UNKNOWN):
                try:
                    db.execute("UPDATE night_runs SET state='killed', "
                               "end_reason=?, finished_at=? WHERE id=?",
                               ("kill_switch:" + kill, _utcnow(), rid))
                except sqlite3.Error as exc:
                    raise ValueError("夜間実行の記録を更新できません:"
                                     + _type_name(exc)) from exc
                db.commit()
                return {"done": True, "reason": "kill_switch:" + kill,
                        "step": None, "run_id": rid, "state": KILLED}
            try:
                steps = db.execute("SELECT * FROM night_run_steps WHERE run_id=? "
                                   "ORDER BY seq ASC", (rid,)).fetchall()
            except sqlite3.Error as exc:
                raise ValueError("夜間実行の記録を確認できません:"
                                 + _type_name(exc)) from exc
            decoded_steps = [_decode_step(item) for item in steps]
            budget = run.get("budget") if isinstance(run.get("budget"), dict) else {}
            # 予算の判定。
            try:
                max_ticks = int(budget.get("max_ticks")
                                or DEFAULT_BUDGET["max_ticks"])
                max_seconds = int(budget.get("max_seconds")
                                  or DEFAULT_BUDGET["max_seconds"])
                max_fail = int(budget.get("max_consec_failures")
                               or DEFAULT_BUDGET["max_consec_failures"])
                max_pre = int(budget.get("max_consec_preflight")
                              or DEFAULT_BUDGET["max_consec_preflight"])
            except (TypeError, ValueError, AttributeError):
                _abort_run(db, rid, "予算を確認できないため中止します")
                db.commit()
                return {"done": True, "reason": "budget_unreadable",
                        "step": None, "run_id": rid, "state": ABORTED}
            if len(decoded_steps) >= max_ticks:
                try:
                    db.execute("UPDATE night_runs SET heartbeat_at=? WHERE id=?",
                               (_utcnow(), rid))
                except sqlite3.Error as exc:
                    raise ValueError("夜間実行の記録を更新できません:"
                                     + _type_name(exc)) from exc
                db.commit()
                return {"done": True, "reason": "budget_max_ticks",
                        "step": None, "run_id": rid, "state": RUNNING}
            try:
                elapsed = _utcnow() - float(run.get("started_at") or 0)
            except (TypeError, ValueError):
                _abort_run(db, rid, "開始時刻を確認できないため中止します")
                db.commit()
                return {"done": True, "reason": "started_at_unreadable",
                        "step": None, "run_id": rid, "state": ABORTED}
            until_epoch = None
            if isinstance(budget, dict) and budget.get("until_epoch") is not None:
                try:
                    until_epoch = float(budget.get("until_epoch") or 0)
                except (TypeError, ValueError):
                    until_epoch = None
            if until_epoch is not None and _utcnow() >= until_epoch:
                try:
                    db.execute("UPDATE night_runs SET heartbeat_at=? WHERE id=?",
                               (_utcnow(), rid))
                except sqlite3.Error as exc:
                    raise ValueError("夜間実行の記録を更新できません:"
                                     + _type_name(exc)) from exc
                db.commit()
                return {"done": True, "reason": "budget_until",
                        "step": None, "run_id": rid, "state": RUNNING}
            if elapsed >= max_seconds:
                try:
                    db.execute("UPDATE night_runs SET heartbeat_at=? WHERE id=?",
                               (_utcnow(), rid))
                except sqlite3.Error as exc:
                    raise ValueError("夜間実行の記録を更新できません:"
                                     + _type_name(exc)) from exc
                db.commit()
                return {"done": True, "reason": "budget_max_seconds",
                        "step": None, "run_id": rid, "state": RUNNING}
            failures, preflight = _consec_counts(decoded_steps)
            if failures >= max_fail:
                try:
                    db.execute("UPDATE night_runs SET heartbeat_at=? WHERE id=?",
                               (_utcnow(), rid))
                except sqlite3.Error as exc:
                    raise ValueError("夜間実行の記録を更新できません:"
                                     + _type_name(exc)) from exc
                db.commit()
                return {"done": True, "reason": "budget_consec_failures",
                        "step": None, "run_id": rid, "state": RUNNING,
                        "consec_failures": failures}
            if preflight >= max_pre:
                _abort_run(db, rid, "事前照合の失敗が連続したため中止します")
                db.commit()
                return {"done": True, "reason": "circuit_preflight",
                        "step": None, "run_id": rid, "state": ABORTED}
            # 次のPJ (サーバ側で選ぶ)。
            try:
                candidates, _skipped = _candidate_projects(manager, run,
                                                           decoded_steps)
            except ValueError as exc:
                _abort_run(db, rid, "候補を確認できないため中止します:"
                           + _type_name(exc))
                db.commit()
                return {"done": True, "reason": "candidates_unreadable",
                        "step": None, "run_id": rid, "state": ABORTED}
            if not candidates:
                try:
                    db.execute("UPDATE night_runs SET heartbeat_at=? WHERE id=?",
                               (_utcnow(), rid))
                except sqlite3.Error as exc:
                    raise ValueError("夜間実行の記録を更新できません:"
                                     + _type_name(exc)) from exc
                db.commit()
                return {"done": True, "reason": "no_candidates",
                        "step": None, "run_id": rid, "state": RUNNING}
            target = str(candidates[0])
            seq = len(decoded_steps) + 1
            try:
                db.execute("UPDATE night_runs SET heartbeat_at=? WHERE id=?",
                           (_utcnow(), rid))
            except sqlite3.Error as exc:
                raise ValueError("夜間実行の記録を更新できません:"
                                 + _type_name(exc)) from exc
            db.commit()
            # 1ティックだけ既存の autopilot_tick を実行する。
            tick: dict = {}
            tick_error = ""
            try:
                tick = _auto.autopilot_tick(manager, target,
                                            actor=str(run.get("actor") or ""),
                                            dry_run=False)
            except Exception as exc:
                tick_error = _type_name(exc)
                tick = {}
            if tick_error or not isinstance(tick, dict):
                # (c) 想定外・例外は成功扱いにしない。そのPJを除外し失敗に数える。
                with _connect(manager) as db2:
                    db2.execute("BEGIN IMMEDIATE")
                    summary = {"status": "failed", "stop_class": "",
                               "action": "",
                               "reason": "想定外の結果のため除外します:"
                               + (tick_error or "TypeError")}
                    step = _append_step(db2, rid, seq, target, summary)
                    _set_excluded(db2, rid, target,
                                  "unreadable_result:" + (tick_error or "TypeError"))
                    try:
                        db2.execute("UPDATE night_runs SET heartbeat_at=? "
                                    "WHERE id=?", (_utcnow(), rid))
                    except sqlite3.Error as exc:
                        raise ValueError("夜間実行の記録を更新できません:"
                                         + _type_name(exc)) from exc
                    db2.commit()
                step["reason"] = "想定外の結果のため除外します:" + (tick_error or "TypeError")
                return {"done": False, "reason": "tick_unreadable_excluded",
                        "step": step, "run_id": rid, "state": RUNNING}
            status = str(tick.get("status") or "")
            if status not in ("advanced", "waiting_human", "failed",
                              "stopped_guard", "preflight_unknown", "stale",
                              "disabled", "busy", "kill_switch", "dry_run"):
                with _connect(manager) as db2:
                    db2.execute("BEGIN IMMEDIATE")
                    summary = {"status": "failed", "stop_class": "",
                               "action": "",
                               "reason": "想定外の状態のため除外します:" + status}
                    step = _append_step(db2, rid, seq, target, summary)
                    _set_excluded(db2, rid, target,
                                  "unexpected_status:" + (status or "empty"))
                    try:
                        db2.execute("UPDATE night_runs SET heartbeat_at=? "
                                    "WHERE id=?", (_utcnow(), rid))
                    except sqlite3.Error as exc:
                        raise ValueError("夜間実行の記録を更新できません:"
                                         + _type_name(exc)) from exc
                    db2.commit()
                step["reason"] = "想定外の状態のため除外します:" + status
                return {"done": False, "reason": "tick_unexpected_excluded",
                        "step": step, "run_id": rid, "state": RUNNING}
            try:
                external_delta = int(tick.get("external_delta") or 0)
            except (TypeError, ValueError):
                external_delta = 0
                tick["external_delta"] = "確認できず"
            try:
                rounds_delta = int(tick.get("external_rounds_delta") or 0)
            except (TypeError, ValueError):
                rounds_delta = 0
                tick["external_rounds_delta"] = "確認できず"
            budget_kept = tick.get("budget_kept")
            if not isinstance(budget_kept, bool):
                budget_kept = False
            summary = {
                "status": status,
                "stop_class": str(tick.get("stop_class") or ""),
                "action": str(tick.get("action_name") or ""),
                "external_delta": external_delta,
                "external_rounds_delta": rounds_delta,
                "budget_kept": bool(budget_kept),
            }
            # (a) 外部増分が1でもあれば全体を aborted にする。
            if external_delta != 0 or rounds_delta != 0 or not budget_kept:
                with _connect(manager) as db2:
                    db2.execute("BEGIN IMMEDIATE")
                    step = _append_step(db2, rid, seq, target, summary)
                    _abort_run(db2, rid,
                               "外部増分を検出したため中止します:"
                               f"calls={external_delta} "
                               f"rounds={rounds_delta} "
                               f"budget_kept={bool(budget_kept)}")
                    try:
                        db2.execute("UPDATE night_runs SET heartbeat_at=? "
                                    "WHERE id=?", (_utcnow(), rid))
                    except sqlite3.Error as exc:
                        raise ValueError("夜間実行の記録を更新できません:"
                                         + _type_name(exc)) from exc
                    db2.commit()
                # 該当PJの自走は既存の仕組みで無効化される。夜間実行側も確認し、
                # 無効になっていなければ無効化する。
                disable_note = ""
                try:
                    still = _auto.is_enabled(manager, target)
                except Exception as exc:
                    still = True
                    disable_note = "確認できず:" + _type_name(exc)
                if still:
                    try:
                        _auto._disable_internal(
                            manager, target,
                            reason="夜間実行の回路遮断のため自走を止めました")
                        disable_note = "無効化しました"
                    except Exception as exc:
                        disable_note = "無効化できず:" + _type_name(exc)
                else:
                    disable_note = "既に無効でした"
                step["circuit"] = "external_delta"
                step["disable_note"] = disable_note
                return {"done": True, "reason": "circuit_external_delta",
                        "step": step, "run_id": rid, "state": ABORTED}
            with _connect(manager) as db2:
                db2.execute("BEGIN IMMEDIATE")
                step = _append_step(db2, rid, seq, target, summary)
                # (d) 進まないことが確定したら以後その夜は回さない。
                if status in NO_PROGRESS_STATES:
                    _set_excluded(db2, rid, target, "no_progress:" + status)
                # (b) 連続の再判定 (今回分を含む)。
                try:
                    rows = db2.execute("SELECT * FROM night_run_steps WHERE run_id=? "
                                       "ORDER BY seq ASC", (rid,)).fetchall()
                except sqlite3.Error as exc:
                    raise ValueError("夜間実行の記録を確認できません:"
                                     + _type_name(exc)) from exc
                tails = [_decode_step(item) for item in rows]
                _fails, _pres = _consec_counts(tails)
                aborted_now = ""
                if _pres >= max_pre:
                    _abort_run(db2, rid, "事前照合の失敗が連続したため中止します")
                    aborted_now = ABORTED
                elif _fails >= max_fail:
                    aborted_now = ""
                try:
                    db2.execute("UPDATE night_runs SET heartbeat_at=? WHERE id=?",
                                (_utcnow(), rid))
                except sqlite3.Error as exc:
                    raise ValueError("夜間実行の記録を更新できません:"
                                     + _type_name(exc)) from exc
                db2.commit()
            if aborted_now == ABORTED:
                return {"done": True, "reason": "circuit_preflight",
                        "step": step, "run_id": rid, "state": ABORTED}
            if status in NO_PROGRESS_STATES:
                return {"done": False, "reason": "tick_no_progress_excluded",
                        "step": step, "run_id": rid, "state": RUNNING}
            return {"done": False, "reason": "tick_recorded",
                    "step": step, "run_id": rid, "state": RUNNING}


def _notify_watchtower(manager, run_id: str, state: str,
                       summary_ja: str) -> dict:
    """見張りの通知を1件作る (project_id空の全体イベント。冪等キー=run_id)。

    既存の watchtower の関数を呼ぶ追加のみ (watchtower.pyは変更しない)。
    通知の失敗は握りつぶさない (呼出側が報告に残す)。
    """
    import hashlib as _hl

    from app import watchtower as _watch

    kind = "night_run_finished" if state == FINISHED else "night_run_aborted"
    if state == KILLED:
        kind = "night_run_aborted"
    if state not in (FINISHED, ABORTED, KILLED, EXPIRED):
        kind = "night_run_finished"
    severity = "info" if kind == "night_run_finished" else "attention"
    event_id = _hl.sha256(
        ("|" + kind + "|" + str(run_id or "")).encode("utf-8")).hexdigest()[:32]
    with _watch._connect(manager) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            cursor = db.execute(
                """INSERT OR IGNORE INTO watch_events(
                       id, project_id, kind, severity, summary_ja,
                       created_at, idempotency_key, ack_by, ack_at, ack_note)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (event_id, "", kind, severity,
                 _safe_text(summary_ja, 200), _utcnow(),
                 str(run_id or "")[:200], "", 0.0, ""))
        except sqlite3.Error as exc:
            raise ValueError("見張りの通知に失敗しました:"
                             + _type_name(exc)) from exc
        db.commit()
        inserted = int(cursor.rowcount or 0)
    return {"ok": True, "event_id": event_id, "kind": kind,
            "inserted": inserted}


def build_report(manager_or_path, run_id: str) -> dict:
    """朝の実行報告。件数・結果・人の次の操作・外部0の実測を集計する。

    迷ったら「確認できず」(0件・成功にしない)。壊れた/欠けたステップは
    「確認できず」として数える。
    """
    run = get_run(manager_or_path, str(run_id or ""))
    steps = run.get("steps") if isinstance(run.get("steps"), list) else []
    per_project: dict[str, dict] = {}
    unreadable = 0
    external_total = 0
    rounds_total = 0
    budget_broken = 0
    for item in steps:
        if not isinstance(item, dict) or not item.get("project_id"):
            unreadable += 1
            continue
        pid = str(item.get("project_id") or "")
        entry = per_project.get(pid)
        if entry is None:
            entry = {"project_id": pid, "ticks": 0, "statuses": [],
                     "last_status": "確認できず", "outcome": "確認できず"}
            per_project[pid] = entry
        entry["ticks"] = int(entry["ticks"]) + 1
        status = str(item.get("status") or "")
        if status not in NEXT_ACTION_JA:
            unreadable += 1
            entry["statuses"].append("確認できず")
            entry["last_status"] = "確認できず"
            continue
        entry["statuses"].append(status)
        entry["last_status"] = status
        try:
            external_total += int(item.get("external_delta") or 0)
        except (TypeError, ValueError):
            unreadable += 1
        try:
            rounds_total += int(item.get("external_rounds_delta") or 0)
        except (TypeError, ValueError):
            unreadable += 1
        if not bool(item.get("budget_kept")):
            budget_broken += 1
    progressed: list[str] = []
    stopped: list[str] = []
    failed: list[str] = []
    unknown: list[str] = []
    rows: list[dict] = []
    for pid in sorted(per_project.keys()):
        entry = per_project[pid]
        statuses = [str(s) for s in entry["statuses"]]
        if any(s == "advanced" for s in statuses):
            entry["outcome"] = "進んだ"
            progressed.append(pid)
        elif any(s == "failed" for s in statuses):
            entry["outcome"] = "失敗"
            failed.append(pid)
        elif any(s in NEXT_ACTION_JA for s in statuses):
            entry["outcome"] = "止まった"
            stopped.append(pid)
        else:
            entry["outcome"] = "確認できず"
            unknown.append(pid)
        last = str(entry.get("last_status") or "確認できず")
        rows.append({
            "project_id": pid,
            "ticks": int(entry["ticks"]),
            "outcome": str(entry["outcome"]),
            "last_status": last,
            "next_action": str(NEXT_ACTION_JA.get(last) or "確認できず"),
        })
    total_ticks = 0
    for item in steps:
        if isinstance(item, dict) and item.get("project_id"):
            total_ticks += 1
        else:
            total_ticks += 0
    state = str(run.get("state") or "確認できず")
    if state == RUNNING:
        state_text = "実行中"
    elif state == FINISHED:
        state_text = "終了"
    elif state == ABORTED:
        state_text = "中止"
    elif state == KILLED:
        state_text = "全体停止で終了"
    elif state == EXPIRED:
        state_text = "heartbeat途絶で終了"
    else:
        state_text = "確認できず"
    end_reason = str(run.get("end_reason") or "")
    if not end_reason:
        end_reason = "確認できず" if state not in (RUNNING,) else ""
    next_actions: list[str] = []
    if state == ABORTED:
        next_actions.append("中止の原因を確認してください:"
                            + (end_reason or "確認できず"))
    if failed:
        next_actions.append("失敗したPJの原因を確認してください")
    if stopped:
        next_actions.append("止まったPJの判断内容を確認してください")
    if unknown or unreadable:
        next_actions.append("確認できなかった記録があります。状態を確認してください")
    if not next_actions:
        next_actions.append("特になし。結果を確認してください")
    try:
        external_ok = bool(external_total == 0 and rounds_total == 0
                           and budget_broken == 0)
    except (TypeError, ValueError):
        external_ok = False
    return {
        "run_id": str(run.get("run_id") or ""),
        "state": state,
        "state_ja": state_text,
        "actor": str(run.get("actor") or "確認できず"),
        "started_at": run.get("started_at"),
        "finished_at": run.get("finished_at"),
        "end_reason": end_reason,
        "total_projects": len(per_project),
        "total_ticks": total_ticks,
        "progressed": progressed,
        "stopped": stopped,
        "failed": failed,
        "unknown": unknown,
        "unreadable_steps": unreadable,
        "per_project": rows,
        "external_calls_total": external_total,
        "external_rounds_total": rounds_total,
        "budget_broken_total": budget_broken,
        "external_zero_measured": external_ok,
        "approvals_total": 0,
        "approvals_note": "夜間実行は承認を行いません(実測: 承認操作0件)",
        "next_actions": next_actions,
    }


def finish_run(manager, run_id: str, token: str, actor: str,
               end_reason: str = "") -> dict:
    """残りの状態を確定し、報告を作る。冪等 (二重呼出は再実行しない)。

    報告の生成が失敗しても、実行の状態は「報告なし」と残す (消さない)。
    見張りの通知の失敗は握りつぶさない (報告に残す)。
    """
    actor_name = _safe_text(actor, 100).strip()
    if not actor_name:
        raise ValueError("actor is required")
    lock = _lock_for("nightrun-global")
    with lock:
        with _connect(manager) as db:
            db.execute("BEGIN IMMEDIATE")
            run = _check_token(str(run_id or ""), token, db)
            rid = str(run.get("run_id") or "")
            state = str(run.get("state") or "")
            if state not in (RUNNING, EXPIRED) and str(run.get("report_status") or "") == "ok":
                db.commit()
                return {"run_id": rid, "state": state, "idempotent": True,
                        "report": run.get("report"),
                        "report_status": str(run.get("report_status") or "")}
            if state == RUNNING:
                new_state = FINISHED
            elif state == EXPIRED:
                new_state = EXPIRED
            else:
                new_state = state or FINISHED
            reason = _safe_text(end_reason, 500) or str(run.get("end_reason") or "")
            try:
                db.execute("UPDATE night_runs SET state=?, end_reason=?, "
                           "finished_at=? WHERE id=?",
                           (new_state, reason, _utcnow(), rid))
            except sqlite3.Error as exc:
                raise ValueError("夜間実行の記録を更新できません:"
                                 + _type_name(exc)) from exc
            db.commit()
    report: dict | None = None
    report_status = "missing"
    report_error = ""
    try:
        report = build_report(manager, rid)
        report_status = "ok"
    except Exception as exc:
        report = None
        report_status = "missing"
        report_error = _type_name(exc)
    watch_note = ""
    try:
        if report_status == "ok" and isinstance(report, dict):
            summary = ("夜間実行 " + rid + ": " + str(report.get("state_ja") or "")
                       + "、PJ" + str(report.get("total_projects")) + "件、"
                       + "ティック" + str(report.get("total_ticks")) + "件")
        else:
            summary = "夜間実行 " + rid + ": 報告なし"
        notify = _notify_watchtower(manager, rid, new_state, summary)
        if int(notify.get("inserted") or 0) == 0:
            watch_note = "通知は既にありました"
        else:
            watch_note = "通知を1件作りました"
    except Exception as exc:
        watch_note = "通知に失敗しました:" + _type_name(exc)
    with lock:
        with _connect(manager) as db:
            db.execute("BEGIN IMMEDIATE")
            payload = ""
            if report is not None:
                try:
                    payload = json.dumps(report, ensure_ascii=False,
                                         sort_keys=True)
                except (TypeError, ValueError):
                    payload = ""
                    report_status = "missing"
                    if not report_error:
                        report_error = "ValueError"
            if report_status != "ok" and not report_error:
                report_error = "確認できず"
            stored_note = watch_note
            if report_error:
                stored_note = (watch_note + " / 報告なし:" + report_error).strip()
            if payload:
                try:
                    final = json.loads(payload)
                except (ValueError, TypeError):
                    final = None
                if isinstance(final, dict):
                    final["watch_note"] = stored_note
                    try:
                        payload = json.dumps(final, ensure_ascii=False,
                                             sort_keys=True)
                    except (TypeError, ValueError):
                        stored_note = stored_note + " / 報告の保存に失敗"
                        payload = ""
                        report_status = "missing"
            try:
                db.execute("UPDATE night_runs SET report_json=?, "
                           "report_status=? WHERE id=?",
                           (payload, report_status, rid))
            except sqlite3.Error as exc:
                raise ValueError("夜間実行の記録を更新できません:"
                                 + _type_name(exc)) from exc
            db.commit()
    out_report = None
    if payload:
        try:
            out_report = json.loads(payload)
        except (ValueError, TypeError):
            out_report = None
    return {"run_id": rid, "state": new_state, "idempotent": False,
            "report": out_report, "report_status": report_status,
            "report_error": report_error, "watch_note": watch_note}


# ----------------------------------------------------------------------------
# 全体停止スイッチ (キルスイッチ)
# ----------------------------------------------------------------------------

def _read_kill_db(manager_or_path) -> str:
    """DB側の状態。DBが存在しない (未使用) なら inactive。

    存在するのに読めない・形式不正なら unknown (止める側)。
    """
    path = nightrun_db_path(manager_or_path)
    try:
        exists = path.exists()
    except OSError:
        return KILL_UNKNOWN
    if not exists:
        return KILL_INACTIVE
    db = _read_connection(manager_or_path)
    try:
        try:
            row = db.execute("SELECT state FROM kill_switch WHERE id='global' "
                             "LIMIT 1").fetchone()
        except sqlite3.Error:
            return KILL_UNKNOWN
        if row is None:
            return KILL_INACTIVE
        state = str(row["state"] or "")
        if state in (KILL_ACTIVE, KILL_INACTIVE):
            return state
        return KILL_UNKNOWN
    finally:
        try:
            db.close()
        except sqlite3.Error:
            pass


def _read_kill_file(manager_or_path) -> str:
    """ファイル側の状態。存在すれば active、無ければ inactive。

    読めないときは unknown (止める側)。
    """
    try:
        path = kill_file_path(manager_or_path)
    except Exception:
        return KILL_UNKNOWN
    try:
        exists = path.exists()
    except OSError:
        return KILL_UNKNOWN
    if not exists:
        return KILL_INACTIVE
    try:
        text = path.read_text(encoding="utf-8", errors="strict")
    except (OSError, ValueError, UnicodeError):
        return KILL_UNKNOWN
    first = text.strip().splitlines()[0] if text.strip() else ""
    if first in (KILL_ACTIVE, ""):
        return KILL_ACTIVE
    if first == KILL_INACTIVE:
        return KILL_INACTIVE
    return KILL_UNKNOWN


def kill_switch_state(manager_or_path) -> str:
    """active/inactive/unknown を返す。読めない/矛盾するときは unknown=止める側。"""
    try:
        db_state = _read_kill_db(manager_or_path)
    except Exception:
        db_state = KILL_UNKNOWN
    try:
        file_state = _read_kill_file(manager_or_path)
    except Exception:
        file_state = KILL_UNKNOWN
    if db_state == file_state and db_state in (KILL_ACTIVE, KILL_INACTIVE):
        return db_state
    return KILL_UNKNOWN


def kill_switch_set(manager, state: str, actor: str) -> dict:
    """キルスイッチの設定。DBとファイルの両方に書く。失敗は握りつぶさない。"""
    actor_name = _safe_text(actor, 100).strip()
    if not actor_name:
        raise ValueError("actor is required")
    if state not in (KILL_ACTIVE, KILL_INACTIVE):
        raise ValueError("state を確認できません")
    now = _utcnow()
    with _connect(manager) as db:
        try:
            with db:
                db.execute("INSERT OR REPLACE INTO kill_switch(id, state, "
                           "updated_at, actor) VALUES('global',?,?,?)",
                           (state, now, actor_name))
        except sqlite3.Error as exc:
            raise ValueError("キルスイッチの記録に失敗しました:"
                             + _type_name(exc)) from exc
    try:
        path = kill_file_path(manager)
        path.parent.mkdir(parents=True, exist_ok=True)
        if state == KILL_ACTIVE:
            path.write_text(KILL_ACTIVE + "\n", encoding="utf-8")
        else:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
    except OSError as exc:
        raise ValueError("キルスイッチの記録に失敗しました:"
                         + _type_name(exc)) from exc
    check = kill_switch_state(manager)
    if check != state:
        raise ValueError("キルスイッチの状態を確認できません:" + check)
    return {"state": state, "actor": actor_name, "updated_at": now}


def kill_switch_release(manager, actor: str) -> dict:
    """解除は人が行う (actor必須)。"""
    return kill_switch_set(manager, KILL_INACTIVE, actor)


def disable_all(manager, actor: str) -> dict:
    """全PJの自走を無効化し (各PJの監査に記録)、同時にキルスイッチを active にする。

    1つ失敗しても残りは続ける。失敗があれば ok:false と失敗PJを返す
    (成功扱いにしない)。
    """
    from app import autopilot as _auto

    actor_name = _safe_text(actor, 100).strip()
    if not actor_name:
        raise ValueError("actor is required")
    try:
        rows = manager.memory.list_projects()
    except Exception as exc:
        raise ValueError("PJ一覧を確認できません:" + _type_name(exc)) from exc
    if not isinstance(rows, list):
        raise ValueError("PJ一覧の形式を確認できません")
    disabled: list[str] = []
    failed: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            failed.append({"project_id": "確認できず", "error": "TypeError"})
            continue
        pid = row.get("id")
        if pid is None or str(pid) == "":
            failed.append({"project_id": "確認できず", "error": "ValueError"})
            continue
        pid_text = str(pid)
        try:
            _auto.set_enabled(manager, pid_text, False, actor=actor_name,
                              reason="夜間実行の全体停止のため無効化しました")
            disabled.append(pid_text)
        except Exception as exc:
            failed.append({"project_id": pid_text,
                           "error": _type_name(exc)})
    disabled.sort()
    kill_error = ""
    try:
        kill_switch_set(manager, KILL_ACTIVE, actor_name)
    except Exception as exc:
        kill_error = _type_name(exc)
    if kill_error:
        failed.append({"project_id": "", "error": "kill_switch:" + kill_error})
    ok = not failed
    return {"ok": ok, "disabled": disabled, "failed": failed,
            "kill_switch": kill_switch_state(manager)}


# ----------------------------------------------------------------------------
# 棚卸し・完全削除時の扱い
# ----------------------------------------------------------------------------

def purge_project_nightrun(memory_path: str | Path, project_id: str) -> dict:
    """完全削除時の後始末。ステップ行の当該project_idを消す (既存watchの流儀)。

    run行自体はPJごとの行を持たない (project_idは記録のみ) ため残す。
    他PJに影響しない。
    """
    pid = str(project_id or "")
    path = nightrun_db_path(memory_path)
    if not path.exists():
        return {"night_run_steps": 0}
    removed = {"night_run_steps": 0}
    db = sqlite3.connect(str(path), timeout=30)
    try:
        with db:
            try:
                names = {row[0] for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            except sqlite3.Error:
                raise
            if "night_run_steps" not in names:
                return removed
            try:
                cur = db.execute("DELETE FROM night_run_steps WHERE project_id=?",
                                 (pid,))
                removed["night_run_steps"] = int(cur.rowcount or 0)
            except sqlite3.Error:
                raise
    finally:
        db.close()
    return removed


def reset_for_generation(memory_path: str | Path, project_id: str) -> dict:
    """PJ初期化 (世代切替) では夜間実行の記録は変えない。実行中の記録も残す。"""
    return {"project_id": str(project_id or ""), "reset": False,
            "note": "夜間実行の記録は世代切替で変えません"}
