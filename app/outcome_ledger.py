"""E-1 成果台帳と指標 + E-2 方針の改善提案(人が採否を決める)。

一般機構。特定PJ・件数・IDに依存しない。読み取りだけで算出し、
記録は人が押したときだけ行う。アプリ内に定期スレッド・スケジューラを
作らない。通知は画面内だけ。外部呼出0。

- compute_metrics: 読み取り専用(DB・ファイル不変、外部呼出0)。
- record_snapshot / list_snapshots: サイドカーSQLite(*.ledger.sqlite3)。
  追記専用・排他。同じ内容の連続記録は重複させない(冪等キー)。
- suggest: 読み取り専用・決定的。auto_change は常に偽。
  安全の関門・予算・許可リスト・キルスイッチ等の設定を自動で変える
  提案は作らない(人への依頼の形だけ)。
- record_decision: 記録するだけで何も実行・変更しない。

fail-closed:
- 読めない指標を0や「問題なし」にしない(null+status=unknown)。
- 「不明」を値に混ぜず別フラグで持つ。
- 分母0のとき率を0%や100%にしない(null)。
- safety_ok は確認できない指標が1つでもあれば偽。
- 「確認に失敗した場合」と「基準を満たさない場合」を同じ分岐で扱わない。
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

LOGGER = logging.getLogger(__name__)

FORMAT = "e1-ledger/1"

# テスト用の失敗注入フック。製品コードでは常に None。
FAIL_INJECT: dict | None = None

ADVANCED_STATUSES = ("advanced", "ok")
UNREADABLE_STATUSES = ("deleted", "purged", "unknown")

DECISIONS = ("adopted", "rejected", "deferred")

DECISION_JA = {
    "adopted": "採用",
    "rejected": "却下",
    "deferred": "保留",
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


def _short_hash(value: object) -> str:
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        raw = str(value)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _resolve_mem_path(manager_or_path) -> Path:
    if hasattr(manager_or_path, "memory"):
        return Path(manager_or_path.memory.path)
    return Path(manager_or_path)


def ledger_db_path(manager_or_path) -> Path:
    mem = _resolve_mem_path(manager_or_path)
    return mem.with_name(mem.name + ".ledger.sqlite3")


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
    db.execute("""CREATE TABLE IF NOT EXISTS ledger_snapshots(
        id TEXT PRIMARY KEY,
        created_at REAL NOT NULL,
        actor TEXT NOT NULL DEFAULT '',
        metrics_json TEXT NOT NULL DEFAULT '{}',
        safety_ok INTEGER NOT NULL DEFAULT 0,
        idempotency_key TEXT NOT NULL DEFAULT '')""")
    db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_ledger_snapshots_idem
        ON ledger_snapshots(idempotency_key)""")
    db.execute("""CREATE INDEX IF NOT EXISTS idx_ledger_snapshots_created
        ON ledger_snapshots(created_at DESC)""")
    db.execute("""CREATE TABLE IF NOT EXISTS ledger_suggestion_decisions(
        id TEXT PRIMARY KEY,
        suggestion_id TEXT NOT NULL DEFAULT '',
        decision TEXT NOT NULL DEFAULT '',
        actor TEXT NOT NULL DEFAULT '',
        reason TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL)""")
    db.execute("""CREATE INDEX IF NOT EXISTS idx_ledger_decisions_suggestion
        ON ledger_suggestion_decisions(suggestion_id, created_at)""")


@contextmanager
def _connect(manager_or_path):
    path = ledger_db_path(manager_or_path)
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
    path = ledger_db_path(manager_or_path)
    if not path.exists():
        return None
    db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True,
                         timeout=15)
    db.row_factory = sqlite3.Row
    return db


def _decode_snapshot(row: sqlite3.Row) -> dict:
    item = dict(row)
    try:
        created = float(item.get("created_at") or 0)
    except (TypeError, ValueError):
        created = 0.0
    try:
        metrics = json.loads(str(item.get("metrics_json") or "{}"))
    except (ValueError, TypeError):
        metrics = {}
    if not isinstance(metrics, dict):
        metrics = {}
    return {
        "id": str(item.get("id") or ""),
        "created_at": created,
        "actor": str(item.get("actor") or ""),
        "metrics": metrics,
        "safety_ok": bool(item.get("safety_ok")),
        "idempotency_key": str(item.get("idempotency_key") or ""),
    }


def _decode_decision(row: sqlite3.Row) -> dict:
    item = dict(row)
    try:
        created = float(item.get("created_at") or 0)
    except (TypeError, ValueError):
        created = 0.0
    return {
        "id": str(item.get("id") or ""),
        "suggestion_id": str(item.get("suggestion_id") or ""),
        "decision": str(item.get("decision") or ""),
        "actor": str(item.get("actor") or ""),
        "reason": str(item.get("reason") or ""),
        "created_at": created,
    }


def _list_projects(manager) -> list[dict]:
    memory = getattr(manager, "memory", None)
    if memory is None:
        raise ValueError("manager.memory がありません")
    rows = memory.list_projects()
    if not isinstance(rows, list):
        raise ValueError("PJ一覧を確認できません")
    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("PJ一覧の形式を確認できません")
        pid = row.get("id")
        if pid is None or str(pid) == "":
            raise ValueError("PJ一覧のIDを確認できません")
        out.append({"id": str(pid), "name": str(row.get("name") or "")})
    out.sort(key=lambda item: item["id"])
    return out


def _delete_status_of(manager, project_id: str) -> str:
    try:
        from app.project_delete import delete_state_status

        mem_path = getattr(getattr(manager, "memory", None), "path", None)
        if mem_path is None:
            return "unknown"
        return str(delete_state_status(mem_path, str(project_id or ""))
                   or "unknown")
    except (OSError, ValueError, TypeError, AttributeError, KeyError,
            sqlite3.Error, RuntimeError) as exc:
        LOGGER.warning("ledger delete-state unreadable: %s",
                       _type_name(exc))
        return "unknown"


def _is_excluded(manager, project_id: str) -> bool:
    try:
        from app.project_delete import is_deleted_or_unknown

        mem_path = getattr(getattr(manager, "memory", None), "path", None)
        if mem_path is None:
            return True
        return bool(is_deleted_or_unknown(mem_path, str(project_id or "")))
    except (OSError, ValueError, TypeError, AttributeError, KeyError,
            sqlite3.Error, RuntimeError) as exc:
        LOGGER.warning("ledger delete-check unreadable: %s",
                       _type_name(exc))
        return True


def _indicator_ok(value, detail: dict,
                  unreadable: list[str]) -> dict:
    return {"value": value, "status": "ok", "detail": detail,
            "unreadable_projects": sorted(unreadable)}


def _indicator_unknown(detail: dict, unreadable: list[str]) -> dict:
    return {"value": None, "status": "unknown", "detail": detail,
            "unreadable_projects": sorted(unreadable)}


def _tick_preflight(tick: dict) -> str:
    if not isinstance(tick, dict):
        return ""
    raw = tick.get("preflight_status")
    if isinstance(raw, str) and raw != "":
        return raw
    refs = tick.get("evidence_refs")
    if isinstance(refs, dict):
        inner = refs.get("preflight_status")
        if isinstance(inner, str) and inner != "":
            return str(inner)
        inner2 = refs.get("preflight")
        if isinstance(inner2, str) and inner2 != "":
            return str(inner2)
    # 欠落は空文字(未知)として返す。呼出側が advanced との組合せで判定する。
    return ""


def _tick_counts_known(tick: dict) -> bool:
    if not isinstance(tick, dict):
        return False
    if "external_delta" not in tick:
        return False
    if "external_rounds_delta" not in tick:
        return False
    if "budget_kept" not in tick:
        return False
    ext = tick.get("external_delta")
    rnd = tick.get("external_rounds_delta")
    kept = tick.get("budget_kept")
    if type(ext) is not int:
        return False
    if type(rnd) is not int:
        return False
    if type(kept) is not bool:
        return False
    return True


def _read_night_steps(manager_or_path) -> list[dict] | None:
    """夜間実行のステップを読み取る。DBが無ければ空。読取不能は None。

    戻り値 None は「確認できず」。空リストは「0件」と区別する。
    """
    try:
        from app import night_run as _night

        path = _night.nightrun_db_path(manager_or_path)
    except (ValueError, TypeError, AttributeError, OSError, RuntimeError) as exc:
        LOGGER.warning("ledger nightrun path unreadable: %s",
                       _type_name(exc))
        return None
    try:
        exists = path.exists()
    except OSError:
        return None
    if not exists:
        return []
    try:
        db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True,
                             timeout=15)
        db.row_factory = sqlite3.Row
    except (OSError, sqlite3.Error) as exc:
        LOGGER.warning("ledger nightrun connect failed: %s",
                       _type_name(exc))
        return None
    try:
        try:
            names = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        except sqlite3.Error as exc:
            LOGGER.warning("ledger nightrun tables unreadable: %s",
                           _type_name(exc))
            return None
        if "night_run_steps" not in names:
            return []
        try:
            rows = db.execute(
                "SELECT project_id, status, external_delta, "
                "external_rounds_delta, budget_kept FROM night_run_steps"
            ).fetchall()
        except sqlite3.Error as exc:
            LOGGER.warning("ledger nightrun steps unreadable: %s",
                           _type_name(exc))
            return None
        out: list[dict] = []
        for row in rows:
            try:
                item = dict(row)
            except (TypeError, ValueError):
                return None
            if "project_id" not in item:
                return None
            if "external_delta" not in item:
                return None
            if "external_rounds_delta" not in item:
                return None
            if "budget_kept" not in item:
                return None
            out.append(item)
        return out
    finally:
        try:
            db.close()
        except sqlite3.Error:
            pass


def _read_shadow_count(manager_or_path) -> int | None:
    """影モードの記録数。DBが無ければ0。読取不能は None。"""
    try:
        from app import shadow_mode as _shadow

        path = _shadow.shadow_db_path(manager_or_path)
    except (ValueError, TypeError, AttributeError, OSError, RuntimeError) as exc:
        LOGGER.warning("ledger shadow path unreadable: %s",
                       _type_name(exc))
        return None
    try:
        exists = path.exists()
    except OSError:
        return None
    if not exists:
        return 0
    try:
        db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True,
                             timeout=15)
    except (OSError, sqlite3.Error) as exc:
        LOGGER.warning("ledger shadow connect failed: %s",
                       _type_name(exc))
        return None
    try:
        try:
            names = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        except sqlite3.Error as exc:
            LOGGER.warning("ledger shadow tables unreadable: %s",
                           _type_name(exc))
            return None
        if "shadow_records" not in names:
            return 0
        try:
            row = db.execute("SELECT COUNT(*) FROM shadow_records").fetchone()
        except sqlite3.Error as exc:
            LOGGER.warning("ledger shadow count unreadable: %s",
                           _type_name(exc))
            return None
        if row is None:
            return None
        try:
            return int(row[0])
        except (TypeError, ValueError):
            return None
    finally:
        try:
            db.close()
        except sqlite3.Error:
            pass


def _read_proposal_keys(manager) -> dict[str, list[str]] | None:
    """proposal_keys 表(冪等キー)の割当を読む。読取不能は None。

    戻り値 {proposal_key: [candidate_id, ...]}。DBが無ければ空。
    """
    try:
        from app.goal_review import ReviewStore
    except (ValueError, TypeError, AttributeError, ImportError,
            RuntimeError) as exc:
        LOGGER.warning("ledger proposal-keys import unreadable: %s",
                       _type_name(exc))
        return None
    try:
        mem_path = getattr(getattr(manager, "memory", None), "path", None)
        if mem_path is None:
            return None
        store = ReviewStore(mem_path)
    except (ValueError, TypeError, AttributeError, OSError,
            sqlite3.Error, RuntimeError) as exc:
        LOGGER.warning("ledger proposal-keys store unreadable: %s",
                       _type_name(exc))
        return None
    try:
        path = Path(store.path)
    except (ValueError, TypeError, AttributeError, OSError,
            RuntimeError) as exc:
        LOGGER.warning("ledger proposal-keys path unreadable: %s",
                       _type_name(exc))
        return None
    try:
        exists = path.exists()
    except OSError:
        return None
    if not exists:
        return {}
    try:
        db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True,
                             timeout=15)
    except (OSError, sqlite3.Error) as exc:
        LOGGER.warning("ledger proposal-keys connect failed: %s",
                       _type_name(exc))
        return None
    try:
        try:
            names = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        except sqlite3.Error as exc:
            LOGGER.warning("ledger proposal-keys tables unreadable: %s",
                           _type_name(exc))
            return None
        if "proposal_keys" not in names:
            return {}
        try:
            cols = {row[1] for row in db.execute(
                "PRAGMA table_info(proposal_keys)").fetchall()}
        except sqlite3.Error as exc:
            LOGGER.warning("ledger proposal-keys cols unreadable: %s",
                           _type_name(exc))
            return None
        if "proposal_key" not in cols:
            return None
        id_col = "candidate_id" if "candidate_id" in cols else None
        proj_col = "project" if "project" in cols else None
        if id_col is None:
            return None
        try:
            if proj_col is not None:
                rows = db.execute(
                    "SELECT project, proposal_key, candidate_id FROM "
                    "proposal_keys").fetchall()
            else:
                rows = db.execute(
                    "SELECT proposal_key, candidate_id FROM "
                    "proposal_keys").fetchall()
        except sqlite3.Error as exc:
            LOGGER.warning("ledger proposal-keys rows unreadable: %s",
                           _type_name(exc))
            return None
        out: dict[str, list[str]] = {}
        for row in rows:
            try:
                item = tuple(row)
            except TypeError:
                return None
            if proj_col is not None:
                if len(item) != 3:
                    return None
                _proj, key, cid = item
            else:
                if len(item) != 2:
                    return None
                key, cid = item
            if not isinstance(key, str) or key == "":
                return None
            if not isinstance(cid, str):
                return None
            bucket = out.get(key)
            if bucket is None:
                out[key] = [cid]
            else:
                if cid not in bucket:
                    bucket.append(cid)
        return out
    finally:
        try:
            db.close()
        except sqlite3.Error:
            pass


def compute_metrics(manager) -> dict:
    """成果の指標を読み取りだけで算出する。DB・ファイルを変えない。外部呼出0。

    各指標は {value, status, detail, unreadable_projects}。
    0であるべきものは0と「確認できず」を区別する。
    """
    try:
        projects = _list_projects(manager)
    except (OSError, ValueError, TypeError, AttributeError, KeyError,
            sqlite3.Error, RuntimeError) as exc:
        reason = _type_name(exc)
        empty_detail = {"reason": "PJ一覧を確認できません:" + reason}
        unknown = _indicator_unknown(empty_detail, [])
        return {
            "format": FORMAT,
            "unknown_passed": unknown,
            "duplicate_proposals": unknown,
            "unauthorized_external": unknown,
            "false_achieved": unknown,
            "stop_to_action_rate": unknown,
            "criteria_fulfilment": unknown,
            "activity": {"ticks": None, "night_run_steps": None,
                         "shadow_records": None,
                         "unreadable_projects": []},
            "projects": {"total": 0, "counted": 0, "excluded": 0,
                         "excluded_ids": [],
                         "reason": "PJ一覧を確認できません:" + reason},
        }
    excluded_ids: list[str] = []
    counted: list[dict] = []
    for item in projects:
        pid = str(item["id"])
        if _is_excluded(manager, pid):
            excluded_ids.append(pid)
            continue
        status_text = _delete_status_of(manager, pid)
        if status_text in UNREADABLE_STATUSES:
            excluded_ids.append(pid)
            continue
        if status_text not in ("active", "restored"):
            excluded_ids.append(pid)
            continue
        counted.append(item)
    excluded_ids.sort()
    names = {str(item["id"]): str(item.get("name") or "") for item in counted}

    # 読取キャッシュ(PJごと1回ずつ。書込なし)。
    ticks_by: dict[str, list | None] = {}
    ticks_unreadable: list[str] = []
    proposals_by: dict[str, list | None] = {}
    proposals_unreadable: list[str] = []
    states_by: dict[str, dict | None] = {}
    states_unreadable: list[str] = []

    from app import autopilot as _auto
    from app import capability_gap as _gap
    from app.goal_state_model import build_goal_state as _build_state

    for item in counted:
        pid = str(item["id"])
        try:
            ticks = _auto.list_ticks(manager, pid)
        except (OSError, ValueError, TypeError, AttributeError, KeyError,
                sqlite3.Error, RuntimeError) as exc:
            LOGGER.warning("ledger ticks unreadable %s: %s", pid,
                           _type_name(exc))
            ticks_by[pid] = None
            if pid not in ticks_unreadable:
                ticks_unreadable.append(pid)
            continue
        if not isinstance(ticks, list):
            ticks_by[pid] = None
            if pid not in ticks_unreadable:
                ticks_unreadable.append(pid)
            continue
        ticks_by[pid] = ticks

    for item in counted:
        pid = str(item["id"])
        try:
            rows = _gap.list_proposals(manager, pid)
        except (OSError, ValueError, TypeError, AttributeError, KeyError,
                sqlite3.Error, RuntimeError) as exc:
            LOGGER.warning("ledger proposals unreadable %s: %s", pid,
                           _type_name(exc))
            proposals_by[pid] = None
            if pid not in proposals_unreadable:
                proposals_unreadable.append(pid)
            continue
        if not isinstance(rows, list):
            proposals_by[pid] = None
            if pid not in proposals_unreadable:
                proposals_unreadable.append(pid)
            continue
        proposals_by[pid] = rows

    for item in counted:
        pid = str(item["id"])
        try:
            state = _build_state(manager, pid)
        except (OSError, ValueError, TypeError, AttributeError, KeyError,
                sqlite3.Error, RuntimeError) as exc:
            LOGGER.warning("ledger goal-state unreadable %s: %s", pid,
                           _type_name(exc))
            states_by[pid] = None
            if pid not in states_unreadable:
                states_unreadable.append(pid)
            continue
        if not isinstance(state, dict):
            states_by[pid] = None
            if pid not in states_unreadable:
                states_unreadable.append(pid)
            continue
        states_by[pid] = state

    # (a) 判定不能を通過扱いにした件数。
    unknown_passed_count = 0
    unknown_passed_ids: list[str] = []
    unread_a = sorted(ticks_unreadable)
    for item in counted:
        pid = str(item["id"])
        if pid in ticks_unreadable:
            continue
        ticks = ticks_by.get(pid)
        if ticks is None:
            if pid not in unread_a:
                unread_a.append(pid)
            continue
        for tick in ticks:
            if not isinstance(tick, dict):
                if pid not in unread_a:
                    unread_a.append(pid)
                break
            if "status" not in tick:
                if pid not in unread_a:
                    unread_a.append(pid)
                break
            status = str(tick.get("status") or "")
            if status not in ADVANCED_STATUSES:
                continue
            preflight = _tick_preflight(tick)
            if preflight != "ok":
                unknown_passed_count = int(unknown_passed_count) + 1
                if pid not in unknown_passed_ids:
                    unknown_passed_ids.append(pid)
    unknown_passed_ids.sort()
    unread_a.sort()
    if unread_a:
        metric_a = _indicator_unknown(
            {"count": int(unknown_passed_count),
             "reason": "ティックを確認できないPJがあります",
             "tick_project_ids": sorted(unknown_passed_ids)},
            unread_a)
    else:
        metric_a = _indicator_ok(
            int(unknown_passed_count),
            {"count": int(unknown_passed_count),
             "tick_project_ids": sorted(unknown_passed_ids)},
            [])

    # (b) 重複提案。同じ冪等キーに複数の提案が紐づいている件数。
    # 参照先は capability_gap の function_proposals の一意キー
    # (project_id+plan_signature+criterion_id+capability_id+gap_code) と、
    # plan_feedback の proposal_keys 表 (project+proposal_key) の両方。
    # 通常は一意制約で0件。制約を迂回した異常行だけを数える。
    # 形式不正・読取不能は unknown 側に倒す (0にしない)。
    dup_groups = 0
    dup_group_ids: list[str] = []
    unread_b = sorted(proposals_unreadable)
    for item in counted:
        pid = str(item["id"])
        if pid in proposals_unreadable:
            continue
        rows = proposals_by.get(pid)
        if rows is None:
            if pid not in unread_b:
                unread_b.append(pid)
            continue
        seen: dict[tuple, list[str]] = {}
        broken = False
        for row in rows:
            if not isinstance(row, dict):
                broken = True
                break
            for key in ("proposal_id", "plan_signature", "criterion_id",
                        "required_capability_id", "gap_code"):
                if key not in row:
                    broken = True
                    break
            if broken:
                break
            proposal_id = str(row.get("proposal_id") or "")
            sig = str(row.get("plan_signature") or "")
            cid = str(row.get("criterion_id") or "")
            cap = str(row.get("required_capability_id") or "")
            code = str(row.get("gap_code") or "")
            if proposal_id == "" or sig == "" or cid == "" \
                    or cap == "" or code == "":
                broken = True
                break
            key = (pid, sig, cid, cap, code)
            bucket = seen.get(key)
            if bucket is None:
                seen[key] = [proposal_id]
            else:
                if proposal_id not in bucket:
                    bucket.append(proposal_id)
        if broken:
            if pid not in unread_b:
                unread_b.append(pid)
            continue
        for key in sorted(seen.keys()):
            ids = sorted(seen[key])
            if len(ids) > 1:
                dup_groups = int(dup_groups) + 1
                dup_group_ids.extend(ids)
    keys_map = _read_proposal_keys(manager)
    if keys_map is None:
        for item in counted:
            pid = str(item["id"])
            if pid not in unread_b:
                unread_b.append(pid)
    else:
        for key in sorted(keys_map.keys()):
            ids = sorted({cid for cid in keys_map[key] if cid != ""})
            if len(ids) > 1:
                dup_groups = int(dup_groups) + 1
                dup_group_ids.extend(ids)
    dup_group_ids = sorted(set(dup_group_ids))[:40]
    unread_b.sort()
    if unread_b:
        metric_b = _indicator_unknown(
            {"groups": int(dup_groups),
             "reason": "提案を確認できないPJがあります",
             "proposal_ids": list(dup_group_ids)},
            unread_b)
    else:
        metric_b = _indicator_ok(
            int(dup_groups),
            {"groups": int(dup_groups),
             "proposal_ids": list(dup_group_ids)},
            [])

    # (c) 無許可の外部作用。
    unauth_count = 0
    unauth_ids: list[str] = []
    unread_c = sorted(ticks_unreadable)
    for item in counted:
        pid = str(item["id"])
        if pid in ticks_unreadable:
            continue
        ticks = ticks_by.get(pid)
        if ticks is None:
            if pid not in unread_c:
                unread_c.append(pid)
            continue
        for tick in ticks:
            if not isinstance(tick, dict):
                if pid not in unread_c:
                    unread_c.append(pid)
                break
            if not _tick_counts_known(tick):
                if pid not in unread_c:
                    unread_c.append(pid)
                break
            ext = int(tick.get("external_delta") or 0)
            rnd = int(tick.get("external_rounds_delta") or 0)
            kept = bool(tick.get("budget_kept"))
            if ext != 0 or rnd != 0 or not kept:
                unauth_count = int(unauth_count) + 1
                tick_id = str(tick.get("id") or "")
                if tick_id != "" and tick_id not in unauth_ids:
                    unauth_ids.append(tick_id)
                if len(unauth_ids) >= 40:
                    pass
    steps = _read_night_steps(manager)
    steps_unreadable = steps is None
    if steps_unreadable:
        for item in counted:
            pid = str(item["id"])
            if pid not in unread_c:
                unread_c.append(pid)
    else:
        for row in steps:
            if not isinstance(row, dict):
                for item in counted:
                    pid = str(item["id"])
                    if pid not in unread_c:
                        unread_c.append(pid)
                break
            if "project_id" not in row or "external_delta" not in row \
                    or "external_rounds_delta" not in row \
                    or "budget_kept" not in row:
                for item in counted:
                    pid = str(item["id"])
                    if pid not in unread_c:
                        unread_c.append(pid)
                break
            pid = str(row.get("project_id") or "")
            if pid == "" or pid not in names:
                continue
            if pid in excluded_ids:
                continue
            raw_ext = row.get("external_delta")
            raw_rnd = row.get("external_rounds_delta")
            raw_kept = row.get("budget_kept")
            if type(raw_ext) is not int or type(raw_rnd) is not int:
                if pid not in unread_c:
                    unread_c.append(pid)
                continue
            if type(raw_kept) is not int or raw_kept not in (0, 1):
                if pid not in unread_c:
                    unread_c.append(pid)
                continue
            if int(raw_ext) != 0 or int(raw_rnd) != 0 \
                    or not bool(raw_kept):
                unauth_count = int(unauth_count) + 1
    unauth_ids.sort()
    unread_c.sort()
    if unread_c:
        metric_c = _indicator_unknown(
            {"count": int(unauth_count),
             "reason": "外部増分を確認できない記録があります",
             "tick_ids": list(unauth_ids[:40])},
            unread_c)
    else:
        metric_c = _indicator_ok(
            int(unauth_count),
            {"count": int(unauth_count),
             "tick_ids": list(unauth_ids[:40])},
            [])

    # (d) 目標の誤達成。(e)(f) と同じ進捗モデルを読むが、判定は別分岐。
    from app.completion_gate import evaluate as _evaluate_gate
    from app.execution_funnel import project_funnel as _funnel_one

    false_count = 0
    false_ids: list[str] = []
    unread_d = sorted(states_unreadable)
    for item in counted:
        pid = str(item["id"])
        if pid in states_unreadable:
            continue
        state = states_by.get(pid)
        if state is None:
            if pid not in unread_d:
                unread_d.append(pid)
            continue
        criteria = state.get("criteria")
        if "criteria" not in state or not isinstance(criteria, list):
            if pid not in unread_d:
                unread_d.append(pid)
            continue
        achieved_ids: list[str] = []
        for entry in criteria:
            if not isinstance(entry, dict):
                if pid not in unread_d:
                    unread_d.append(pid)
                achieved_ids = []
                break
            if "criterion_id" not in entry or "state" not in entry:
                if pid not in unread_d:
                    unread_d.append(pid)
                achieved_ids = []
                break
            if str(entry.get("state") or "") == "achieved":
                achieved_ids.append(str(entry.get("criterion_id") or ""))
        if pid in unread_d:
            continue
        if not achieved_ids:
            continue
        try:
            gate = _evaluate_gate(manager, pid, persist=False)
        except (OSError, ValueError, TypeError, AttributeError, KeyError,
                sqlite3.Error, RuntimeError) as exc:
            LOGGER.warning("ledger gate unreadable %s: %s", pid,
                           _type_name(exc))
            if pid not in unread_d:
                unread_d.append(pid)
            continue
        if not isinstance(gate, dict):
            if pid not in unread_d:
                unread_d.append(pid)
            continue
        if "achieved" not in gate or "criteria" not in gate:
            if pid not in unread_d:
                unread_d.append(pid)
            continue
        if not isinstance(gate.get("criteria"), list):
            if pid not in unread_d:
                unread_d.append(pid)
            continue
        gate_overall = gate.get("achieved")
        if type(gate_overall) is not bool:
            if pid not in unread_d:
                unread_d.append(pid)
            continue
        gate_rows: dict[str, dict] = {}
        gate_broken = False
        for row in gate.get("criteria"):
            if not isinstance(row, dict):
                gate_broken = True
                break
            if "criterion_id" not in row or "status" not in row:
                gate_broken = True
                break
            gate_rows[str(row.get("criterion_id") or "")] = row
        if gate_broken:
            if pid not in unread_d:
                unread_d.append(pid)
            continue
        try:
            funnel = _funnel_one(manager, pid)
        except (OSError, ValueError, TypeError, AttributeError, KeyError,
                sqlite3.Error, RuntimeError) as exc:
            LOGGER.warning("ledger funnel unreadable %s: %s", pid,
                           _type_name(exc))
            if pid not in unread_d:
                unread_d.append(pid)
            continue
        if not isinstance(funnel, dict):
            if pid not in unread_d:
                unread_d.append(pid)
            continue
        if "per_criterion" not in funnel \
                or not isinstance(funnel.get("per_criterion"), list):
            if pid not in unread_d:
                unread_d.append(pid)
            continue
        funnel_rows: dict[str, dict] = {}
        funnel_broken = False
        for row in funnel.get("per_criterion"):
            if not isinstance(row, dict):
                funnel_broken = True
                break
            if "criterion_id" not in row or "last_stage" not in row:
                funnel_broken = True
                break
            funnel_rows[str(row.get("criterion_id") or "")] = row
        if funnel_broken:
            if pid not in unread_d:
                unread_d.append(pid)
            continue
        for cid in sorted(achieved_ids):
            gate_row = funnel_rows.get(cid)
            # 基準を満たさない場合(誤達成)と確認失敗は別分岐。
            # ここでは両方の材料が読めていることが確定している。
            grow = gate_rows.get(cid)
            funnel_ok = isinstance(gate_row, dict) and str(
                gate_row.get("last_stage") or "") == "criterion_achieved"
            gate_ok = isinstance(grow, dict) and str(
                grow.get("status") or "") == "PASS" and bool(gate_overall)
            if not gate_ok or not funnel_ok:
                false_count = int(false_count) + 1
                flag = pid + ":" + cid
                if flag not in false_ids:
                    false_ids.append(flag)
    false_ids.sort()
    unread_d.sort()
    if unread_d:
        metric_d = _indicator_unknown(
            {"count": int(false_count),
             "reason": "達成の裏付けを確認できないPJがあります",
             "criterion_refs": list(false_ids[:40])},
            unread_d)
    else:
        metric_d = _indicator_ok(
            int(false_count),
            {"count": int(false_count),
             "criterion_refs": list(false_ids[:40])},
            [])

    # (e) 停止理由から人の次操作へ到達できる割合。
    stopped_total = 0
    stopped_ok = 0
    unread_e: list[str] = []
    for item in counted:
        pid = str(item["id"])
        if pid in states_unreadable:
            if pid not in unread_e:
                unread_e.append(pid)
            continue
        state = states_by.get(pid)
        if state is None:
            if pid not in unread_e:
                unread_e.append(pid)
            continue
        if "stop" not in state or "next_step" not in state:
            if pid not in unread_e:
                unread_e.append(pid)
            continue
        stop = state.get("stop")
        nxt = state.get("next_step")
        if not isinstance(stop, dict) or not isinstance(nxt, dict):
            if pid not in unread_e:
                unread_e.append(pid)
            continue
        if "stop_class" not in stop or "stop_code" not in stop:
            if pid not in unread_e:
                unread_e.append(pid)
            continue
        stop_class = str(stop.get("stop_class") or "")
        stop_code = str(stop.get("stop_code") or "")
        if stop_class in ("", "unknown", "none"):
            continue
        if stop_code in ("", "unknown", "unknown:empty"):
            continue
        stopped_total = int(stopped_total) + 1
        if "action" not in nxt or "screen" not in nxt:
            if pid not in unread_e:
                unread_e.append(pid)
            stopped_total = int(stopped_total) - 1
            continue
        action = str(nxt.get("action") or "")
        screen = str(nxt.get("screen") or "")
        if action not in ("", "none") and screen != "":
            stopped_ok = int(stopped_ok) + 1
    unread_e.sort()
    if unread_e:
        metric_e = _indicator_unknown(
            {"numerator": int(stopped_ok),
             "denominator": int(stopped_total),
             "reason": "次操作を確認できないPJがあります"},
            unread_e)
    else:
        if int(stopped_total) == 0:
            metric_e = {"value": None, "status": "ok",
                        "detail": {"numerator": 0, "denominator": 0,
                                   "reason": "停止中のPJがありません"},
                        "unreadable_projects": []}
        else:
            metric_e = {"value": float(stopped_ok) / float(stopped_total),
                        "status": "ok",
                        "detail": {"numerator": int(stopped_ok),
                                   "denominator": int(stopped_total)},
                        "unreadable_projects": []}

    # (f) 達成条件の充足率。
    total_criteria = 0
    achieved_criteria = 0
    per_project: list[dict] = []
    unread_f = sorted(states_unreadable)
    for item in counted:
        pid = str(item["id"])
        if pid in states_unreadable:
            continue
        state = states_by.get(pid)
        if state is None:
            if pid not in unread_f:
                unread_f.append(pid)
            continue
        if "criteria" not in state:
            if pid not in unread_f:
                unread_f.append(pid)
            continue
        criteria = state.get("criteria")
        if not isinstance(criteria, list):
            if pid not in unread_f:
                unread_f.append(pid)
            continue
        local_total = 0
        local_achieved = 0
        broken = False
        for entry in criteria:
            if not isinstance(entry, dict):
                broken = True
                break
            if "criterion_id" not in entry or "state" not in entry:
                broken = True
                break
            local_total = int(local_total) + 1
            if str(entry.get("state") or "") == "achieved":
                local_achieved = int(local_achieved) + 1
        if broken:
            if pid not in unread_f:
                unread_f.append(pid)
            continue
        total_criteria = int(total_criteria) + int(local_total)
        achieved_criteria = int(achieved_criteria) + int(local_achieved)
        per_project.append({"project_id": pid,
                            "project_name": str(names.get(pid) or ""),
                            "total": int(local_total),
                            "achieved": int(local_achieved)})
    per_project.sort(key=lambda row: str(row.get("project_id") or ""))
    unread_f.sort()
    if unread_f:
        metric_f = _indicator_unknown(
            {"total": int(total_criteria),
             "achieved": int(achieved_criteria),
             "reason": "達成条件を確認できないPJがあります",
             "per_project": per_project},
            unread_f)
    else:
        if int(total_criteria) == 0:
            metric_f = {"value": None, "status": "ok",
                        "detail": {"total": 0, "achieved": 0,
                                   "per_project": per_project,
                                   "reason": "達成条件がありません"},
                        "unreadable_projects": []}
        else:
            metric_f = {"value": float(achieved_criteria)
                        / float(total_criteria),
                        "status": "ok",
                        "detail": {"total": int(total_criteria),
                                   "achieved": int(achieved_criteria),
                                   "per_project": per_project},
                        "unreadable_projects": []}

    # 参考(成果ではない)。成果の指標に混ぜない。
    activity_ticks: int | None = None
    if not ticks_unreadable:
        total_ticks = 0
        for item in counted:
            ticks = ticks_by.get(str(item["id"]))
            if isinstance(ticks, list):
                total_ticks = int(total_ticks) + int(len(ticks))
        activity_ticks = int(total_ticks)
    activity_steps: int | None = None
    if steps is not None:
        count_steps = 0
        for row in steps:
            if isinstance(row, dict) and str(
                    row.get("project_id") or "") in names:
                count_steps = int(count_steps) + 1
        activity_steps = int(count_steps)
    activity_shadow = _read_shadow_count(manager)
    activity_unread: list[str] = sorted(
        set(ticks_unreadable) | set(unread_c if steps is None else []))

    return {
        "format": FORMAT,
        "unknown_passed": metric_a,
        "duplicate_proposals": metric_b,
        "unauthorized_external": metric_c,
        "false_achieved": metric_d,
        "stop_to_action_rate": metric_e,
        "criteria_fulfilment": metric_f,
        "activity": {"ticks": activity_ticks,
                     "night_run_steps": activity_steps,
                     "shadow_records": activity_shadow,
                     "unreadable_projects": activity_unread},
        "projects": {"total": int(len(projects)),
                     "counted": int(len(counted)),
                     "excluded": int(len(excluded_ids)),
                     "excluded_ids": sorted(excluded_ids)},
    }


def _safety_of(metrics: dict) -> bool:
    """safety_ok は(a)(b)(c)(d)がすべて ok かつ 0 のときだけ真。

    「確認できず」が1つでもあれば偽。基準未達と確認失敗は別分岐で扱う。
    """
    if not isinstance(metrics, dict):
        return False
    for key in ("unknown_passed", "duplicate_proposals",
                "unauthorized_external", "false_achieved"):
        if key not in metrics:
            return False
        item = metrics.get(key)
        if not isinstance(item, dict):
            return False
        # 確認失敗は偽(通過扱いにしない)。
        if str(item.get("status") or "") != "ok":
            return False
        # 値の欠落・非数値も偽。
        value = item.get("value")
        if type(value) is not int:
            return False
        # 基準未達は偽。
        if int(value) != 0:
            return False
    return True


def _canonical_metrics(metrics: dict) -> str:
    return json.dumps(metrics, ensure_ascii=False, sort_keys=True,
                      default=str)


def record_snapshot(manager, actor: str) -> dict:
    """今の指標を台帳に追記する。人が押したときだけ呼ぶ。"""
    actor_name = _safe_text(actor, 100).strip()
    if actor_name == "":
        return {"ok": False, "reason": "actor is required",
                "error_type": "ValueError"}
    if FAIL_INJECT is not None and isinstance(FAIL_INJECT, dict):
        stage = str(FAIL_INJECT.get("stage") or "")
        if stage == "snapshot_write_fail":
            return {"ok": False, "reason": "台帳の記録に失敗しました:InjectedError",
                    "error_type": "InjectedError"}
    try:
        metrics = compute_metrics(manager)
    except (OSError, ValueError, TypeError, AttributeError, KeyError,
            sqlite3.Error, RuntimeError) as exc:
        return {"ok": False,
                "reason": "指標を確認できないため記録しません:"
                + _type_name(exc),
                "error_type": _type_name(exc)}
    if not isinstance(metrics, dict):
        return {"ok": False, "reason": "指標を確認できません:TypeError",
                "error_type": "TypeError"}
    safety_ok = _safety_of(metrics)
    canonical = _canonical_metrics(metrics)
    idem = _short_hash(["ledger-snapshot", canonical])
    path = ledger_db_path(manager)
    lock = _lock_for(path)
    with lock:
        try:
            with _connect(manager) as db:
                db.execute("BEGIN IMMEDIATE")
                try:
                    latest = db.execute(
                        "SELECT * FROM ledger_snapshots ORDER BY created_at DESC "
                        "LIMIT 1").fetchone()
                except sqlite3.Error as exc:
                    raise ValueError("台帳を確認できません:"
                                     + _type_name(exc)) from exc
                if latest is not None:
                    try:
                        latest_key = str(dict(latest).get("idempotency_key") or "")
                    except (TypeError, ValueError, AttributeError):
                        raise ValueError(
                            "台帳を確認できません:ValueError") from None
                    if latest_key == idem:
                        db.commit()
                        found = _decode_snapshot(latest)
                        found["ok"] = True
                        found["idempotent"] = True
                        found["safety_ok"] = bool(found.get("safety_ok"))
                        return {"ok": True, "idempotent": True,
                                "snapshot": found,
                                "safety_ok": bool(found.get("safety_ok"))}
                rid = "ls_" + uuid.uuid4().hex[:16]
                now = _utcnow()
                try:
                    db.execute(
                        """INSERT INTO ledger_snapshots(id, created_at, actor,
                               metrics_json, safety_ok, idempotency_key)
                           VALUES(?,?,?,?,?,?)""",
                        (rid, now, actor_name, canonical,
                         int(1 if safety_ok else 0), idem))
                except sqlite3.Error as exc:
                    raise ValueError("台帳の記録に失敗しました:"
                                     + _type_name(exc)) from exc
                db.commit()
                try:
                    row = db.execute(
                        "SELECT * FROM ledger_snapshots WHERE id=?",
                        (rid,)).fetchone()
                except sqlite3.Error as exc:
                    raise ValueError("台帳を確認できません:"
                                     + _type_name(exc)) from exc
                if row is None:
                    raise ValueError("台帳を確認できません:ValueError")
                saved = _decode_snapshot(row)
        except (ValueError, OSError, sqlite3.Error, RuntimeError) as exc:
            return {"ok": False,
                    "reason": "台帳の記録に失敗しました:" + _type_name(exc),
                    "error_type": _type_name(exc)}
    saved["ok"] = True
    saved["idempotent"] = False
    return {"ok": True, "idempotent": False, "snapshot": saved,
            "safety_ok": bool(safety_ok)}


def list_snapshots(manager, limit: int = 10) -> dict:
    """直近の記録と前回との差を返す。読み取り専用。"""
    try:
        count = max(1, min(int(limit), 100))
    except (TypeError, ValueError):
        raise ValueError("limit が不正です")
    db = _read_connection(manager)
    if db is None:
        return {"snapshots": [], "diff": {}, "has_snapshot": False}
    try:
        try:
            rows = db.execute(
                "SELECT * FROM ledger_snapshots ORDER BY created_at DESC "
                "LIMIT ?", (count,)).fetchall()
        except sqlite3.Error as exc:
            raise ValueError("台帳を確認できません:"
                             + _type_name(exc)) from exc
        snapshots = [_decode_snapshot(item) for item in rows]
        diff: dict = {}
        if len(snapshots) >= 2:
            cur = snapshots[0].get("metrics")
            prev = snapshots[1].get("metrics")
            if isinstance(cur, dict) and isinstance(prev, dict):
                for key in ("unknown_passed", "duplicate_proposals",
                            "unauthorized_external", "false_achieved",
                            "stop_to_action_rate", "criteria_fulfilment"):
                    cur_item = cur.get(key)
                    prev_item = prev.get(key)
                    if not isinstance(cur_item, dict) \
                            or not isinstance(prev_item, dict):
                        diff[key] = {"delta": None,
                                     "reason": "確認できず"}
                        continue
                    if "value" not in cur_item or "value" not in prev_item:
                        diff[key] = {"delta": None,
                                     "reason": "確認できず"}
                        continue
                    cur_v = cur_item.get("value")
                    prev_v = prev_item.get("value")
                    if cur_v is None or prev_v is None:
                        diff[key] = {"delta": None,
                                     "reason": "測定できない",
                                     "current": cur_v, "previous": prev_v}
                        continue
                    if type(cur_v) not in (int, float):
                        diff[key] = {"delta": None,
                                     "reason": "確認できず"}
                        continue
                    if type(prev_v) not in (int, float):
                        diff[key] = {"delta": None,
                                     "reason": "確認できず"}
                        continue
                    try:
                        delta = float(cur_v) - float(prev_v)
                    except (TypeError, ValueError):
                        diff[key] = {"delta": None,
                                     "reason": "確認できず"}
                        continue
                    diff[key] = {"delta": float(delta),
                                 "current": cur_v, "previous": prev_v}
        return {"snapshots": snapshots, "diff": diff,
                "has_snapshot": bool(snapshots)}
    finally:
        db.close()


# ----------------------------------------------------------------------------
# E-2 方針の改善提案(人が採否を決める。自動変更0)
# ----------------------------------------------------------------------------

def _suggestion_id(kind: str, key: str) -> str:
    return _short_hash(["ledger-suggestion", str(kind), str(key)])


def suggest(manager) -> list[dict]:
    """提案を決定的に挙げる。読み取り専用。auto_change は常に偽。"""
    try:
        projects = _list_projects(manager)
    except (OSError, ValueError, TypeError, AttributeError, KeyError,
            sqlite3.Error, RuntimeError) as exc:
        raise ValueError("PJ一覧を確認できません:"
                         + _type_name(exc)) from exc
    counted: list[str] = []
    for item in projects:
        pid = str(item.get("id") or "")
        if pid == "":
            continue
        if _is_excluded(manager, pid):
            continue
        if _delete_status_of(manager, pid) not in ("active", "restored"):
            continue
        counted.append(pid)
    counted.sort()

    stop_counts: dict[tuple[str, str], int] = {}
    stop_projects: dict[tuple[str, str], list[str]] = {}
    stop_next: dict[tuple[str, str], str] = {}
    guard_counts: dict[tuple[str, str], int] = {}
    guard_projects: dict[tuple[str, str], list[str]] = {}
    unreadable_ids: list[str] = []
    shadow_wrong: dict[str, int] = {}
    shadow_wrong_projects: dict[str, list[str]] = {}

    from app import autopilot as _auto
    from app.goal_state_model import build_goal_state as _build_state

    for pid in counted:
        try:
            state = _build_state(manager, pid)
        except (OSError, ValueError, TypeError, AttributeError, KeyError,
                sqlite3.Error, RuntimeError) as exc:
            LOGGER.warning("ledger suggest state unreadable %s: %s", pid,
                           _type_name(exc))
            if pid not in unreadable_ids:
                unreadable_ids.append(pid)
            continue
        if not isinstance(state, dict):
            if pid not in unreadable_ids:
                unreadable_ids.append(pid)
            continue
        stop = state.get("stop")
        nxt = state.get("next_step")
        if not isinstance(stop, dict) or not isinstance(nxt, dict):
            if pid not in unreadable_ids:
                unreadable_ids.append(pid)
            continue
        if "stop_class" not in stop or "stop_code" not in stop:
            if pid not in unreadable_ids:
                unreadable_ids.append(pid)
            continue
        stop_class = str(stop.get("stop_class") or "")
        stop_code = str(stop.get("stop_code") or "")
        if stop_class in ("", "unknown", "none"):
            continue
        if stop_code in ("", "unknown", "unknown:empty"):
            continue
        key = (stop_class, stop_code)
        stop_counts[key] = int(stop_counts.get(key) or 0) + 1
        bucket = stop_projects.get(key)
        if bucket is None:
            stop_projects[key] = [pid]
        else:
            if pid not in bucket:
                bucket.append(pid)
        if key not in stop_next and "next_action" in stop:
            stop_next[key] = str(stop.get("next_action") or "none")
        try:
            ticks = _auto.list_ticks(manager, pid)
        except (OSError, ValueError, TypeError, AttributeError, KeyError,
                sqlite3.Error, RuntimeError) as exc:
            LOGGER.warning("ledger suggest ticks unreadable %s: %s", pid,
                           _type_name(exc))
            if pid not in unreadable_ids:
                unreadable_ids.append(pid)
            continue
        if not isinstance(ticks, list):
            if pid not in unreadable_ids:
                unreadable_ids.append(pid)
            continue
        for tick in ticks:
            if not isinstance(tick, dict):
                if pid not in unreadable_ids:
                    unreadable_ids.append(pid)
                break
            if str(tick.get("status") or "") != "stopped_guard":
                continue
            if "stop_class" not in tick or "stop_code" not in tick:
                if pid not in unreadable_ids:
                    unreadable_ids.append(pid)
                break
            gkey = (str(tick.get("stop_class") or ""),
                    str(tick.get("stop_code") or ""))
            if gkey[0] == "" or gkey[1] == "":
                continue
            guard_counts[gkey] = int(guard_counts.get(gkey) or 0) + 1
            gbucket = guard_projects.get(gkey)
            if gbucket is None:
                guard_projects[gkey] = [pid]
            else:
                if pid not in gbucket:
                    gbucket.append(pid)

    try:
        from app import shadow_mode as _shadow

        for pid in counted:
            try:
                records = _shadow.list_records(manager, pid, 200)
            except (OSError, ValueError, TypeError, AttributeError,
                    KeyError, sqlite3.Error, RuntimeError) as exc:
                LOGGER.warning("ledger suggest shadow unreadable %s: %s",
                               pid, _type_name(exc))
                if pid not in unreadable_ids:
                    unreadable_ids.append(pid)
                continue
            if not isinstance(records, list):
                if pid not in unreadable_ids:
                    unreadable_ids.append(pid)
                continue
            for rec in records:
                if not isinstance(rec, dict):
                    continue
                if str(rec.get("label") or "") != "wrong":
                    continue
                planned = str(rec.get("planned_action") or "")
                if planned == "":
                    continue
                shadow_wrong[planned] = int(shadow_wrong.get(planned) or 0) + 1
                bucket = shadow_wrong_projects.get(planned)
                if bucket is None:
                    shadow_wrong_projects[planned] = [pid]
                else:
                    if pid not in bucket:
                        bucket.append(pid)
    except (ValueError, TypeError, AttributeError, ImportError, RuntimeError) as exc:
        LOGGER.warning("ledger suggest shadow failed: %s", _type_name(exc))

    suggestions: list[dict] = []

    for key in sorted(stop_counts.keys()):
        stop_class, stop_code = key
        count = int(stop_counts[key])
        pids = sorted(stop_projects.get(key) or [])[:20]
        next_action = str(stop_next.get(key) or "none")
        sid = _suggestion_id("stop", stop_class + "|" + stop_code)
        suggestions.append({
            "id": sid,
            "kind": "stop_frequent",
            "title_ja": "停止「" + stop_class + "/" + stop_code + "」が"
            + str(count) + "件あります",
            "evidence": {"kind": "stop_frequent",
                         "stop_class": stop_class, "stop_code": stop_code,
                         "count": int(count), "project_ids": list(pids)},
            "suggested_human_action": "判断待ち一覧で「" + stop_class + "」の"
            "対応(次操作:" + next_action + ")を確認してください",
            "auto_change": False,
        })

    for key in sorted(guard_counts.keys()):
        stop_class, stop_code = key
        count = int(guard_counts[key])
        pids = sorted(guard_projects.get(key) or [])[:20]
        sid = _suggestion_id("guard", stop_class + "|" + stop_code)
        suggestions.append({
            "id": sid,
            "kind": "guard_repeat",
            "title_ja": "空転ガードで「" + stop_class + "/" + stop_code
            + "」が" + str(count) + "件止まっています",
            "evidence": {"kind": "guard_repeat",
                         "stop_class": stop_class, "stop_code": stop_code,
                         "count": int(count), "project_ids": list(pids)},
            "suggested_human_action": "計画を見直し、同じ停止の原因を人が解消してください",
            "auto_change": False,
        })

    for planned in sorted(shadow_wrong.keys()):
        count = int(shadow_wrong[planned])
        pids = sorted(shadow_wrong_projects.get(planned) or [])[:20]
        sid = _suggestion_id("shadow", planned)
        suggestions.append({
            "id": sid,
            "kind": "shadow_wrong",
            "title_ja": "関門(影モード)で予告「" + planned + "」の誤りが"
            + str(count) + "件あります",
            "evidence": {"kind": "shadow_wrong",
                         "planned_action": planned,
                         "count": int(count), "project_ids": list(pids)},
            "suggested_human_action": "影モードの記録を確認し、予告の正誤を人が付けてください",
            "auto_change": False,
        })

    unreadable_ids = sorted(set(unreadable_ids))
    if unreadable_ids:
        sid = _suggestion_id("unreadable", ",".join(unreadable_ids))
        suggestions.append({
            "id": sid,
            "kind": "unreadable",
            "title_ja": "「確認できず」が" + str(len(unreadable_ids))
            + "件のPJであります",
            "evidence": {"kind": "unreadable",
                         "count": int(len(unreadable_ids)),
                         "project_ids": list(unreadable_ids[:20])},
            "suggested_human_action": "読み取れないPJの状態を人が確認してください",
            "auto_change": False,
        })

    suggestions.sort(key=lambda row: str(row.get("id") or ""))
    return suggestions


def list_decisions(manager, suggestion_id: str = "") -> list[dict]:
    target = str(suggestion_id or "")
    db = _read_connection(manager)
    if db is None:
        return []
    try:
        try:
            if target == "":
                rows = db.execute(
                    "SELECT * FROM ledger_suggestion_decisions ORDER BY "
                    "created_at DESC LIMIT 200").fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM ledger_suggestion_decisions WHERE "
                    "suggestion_id=? ORDER BY created_at DESC LIMIT 200",
                    (target,)).fetchall()
        except sqlite3.Error as exc:
            raise ValueError("採否の記録を確認できません:"
                             + _type_name(exc)) from exc
        return [_decode_decision(item) for item in rows]
    finally:
        db.close()


def record_decision(manager, suggestion_id: str, decision: str,
                    actor: str, reason: str) -> dict:
    """採否を記録するだけ。何も実行・変更しない。"""
    sid = str(suggestion_id or "").strip()
    actor_name = _safe_text(actor, 100).strip()
    reason_text = _safe_text(reason, 2000).strip()
    decision_text = str(decision or "").strip()
    if sid == "":
        return {"ok": False, "reason": "suggestion_id is required",
                "error_type": "ValueError"}
    if actor_name == "":
        return {"ok": False, "reason": "actor is required",
                "error_type": "ValueError"}
    if decision_text not in DECISIONS:
        return {"ok": False, "reason": "decision を確認できません",
                "error_type": "ValueError"}
    if reason_text == "":
        return {"ok": False, "reason": "reason is required",
                "error_type": "ValueError"}
    if FAIL_INJECT is not None and isinstance(FAIL_INJECT, dict):
        stage = str(FAIL_INJECT.get("stage") or "")
        if stage == "decision_write_fail":
            return {"ok": False,
                    "reason": "採否の記録に失敗しました:InjectedError",
                    "error_type": "InjectedError"}
    path = ledger_db_path(manager)
    lock = _lock_for(path)
    with lock:
        try:
            with _connect(manager) as db:
                db.execute("BEGIN IMMEDIATE")
                rid = "ld_" + uuid.uuid4().hex[:16]
                now = _utcnow()
                try:
                    db.execute(
                        """INSERT INTO ledger_suggestion_decisions(id,
                               suggestion_id, decision, actor, reason,
                               created_at)
                           VALUES(?,?,?,?,?,?)""",
                        (rid, sid[:200], decision_text,
                         actor_name, reason_text, now))
                except sqlite3.Error as exc:
                    raise ValueError("採否の記録に失敗しました:"
                                     + _type_name(exc)) from exc
                db.commit()
                try:
                    row = db.execute(
                        "SELECT * FROM ledger_suggestion_decisions WHERE id=?",
                        (rid,)).fetchone()
                except sqlite3.Error as exc:
                    raise ValueError("採否の記録を確認できません:"
                                     + _type_name(exc)) from exc
                if row is None:
                    raise ValueError("採否の記録を確認できません:ValueError")
                saved = _decode_decision(row)
        except (ValueError, OSError, sqlite3.Error, RuntimeError) as exc:
            return {"ok": False,
                    "reason": "採否の記録に失敗しました:" + _type_name(exc),
                    "error_type": _type_name(exc)}
    saved["ok"] = True
    return {"ok": True, "decision": saved}


# ----------------------------------------------------------------------------
# 棚卸し・完全削除時の扱い
# ----------------------------------------------------------------------------

def reset_for_generation(memory_path: str | Path, project_id: str) -> dict:
    """PJ初期化(世代切替)では台帳の記録は変えない。証拠として残す。"""
    return {"project_id": str(project_id or ""), "reset": False,
            "note": "成果台帳の記録は世代切替で変えません"}


def purge_project_ledger(memory_path: str | Path, project_id: str) -> dict:
    """完全削除時の後始末。detail に含まれるPJ IDを匿名化する。

    台帳自体はPJごとの行を持たないため行は消さない。他PJに影響しない。
    """
    pid = str(project_id or "")
    path = ledger_db_path(memory_path)
    if not path.exists():
        return {"ledger_snapshots": 0, "ledger_suggestion_decisions": 0}
    scrubbed = {"ledger_snapshots": 0, "ledger_suggestion_decisions": 0}
    if pid == "":
        return scrubbed
    db = sqlite3.connect(str(path), timeout=30)
    try:
        with db:
            try:
                names = {row[0] for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            except sqlite3.Error:
                raise
            if "ledger_snapshots" in names:
                try:
                    rows = db.execute(
                        "SELECT id, metrics_json FROM ledger_snapshots").fetchall()
                except sqlite3.Error:
                    raise
                for row in rows:
                    try:
                        rid = str(row[0])
                        blob = str(row[1] or "")
                    except (TypeError, ValueError):
                        continue
                    if pid not in blob:
                        continue
                    try:
                        payload = json.loads(blob)
                    except (ValueError, TypeError):
                        continue
                    text = json.dumps(payload, ensure_ascii=False,
                                      sort_keys=True, default=str)
                    if pid not in text:
                        continue
                    anonymized = text.replace(pid, "[deleted-project]")
                    try:
                        db.execute(
                            "UPDATE ledger_snapshots SET metrics_json=? WHERE id=?",
                            (anonymized, rid))
                        scrubbed["ledger_snapshots"] = int(
                            scrubbed["ledger_snapshots"]) + 1
                    except sqlite3.Error:
                        raise
            if "ledger_suggestion_decisions" in names:
                try:
                    rows = db.execute(
                        "SELECT id, suggestion_id, reason FROM "
                        "ledger_suggestion_decisions").fetchall()
                except sqlite3.Error:
                    raise
                for row in rows:
                    try:
                        rid = str(row[0])
                        sid = str(row[1] or "")
                        reason = str(row[2] or "")
                    except (TypeError, ValueError):
                        continue
                    if pid not in sid and pid not in reason:
                        continue
                    new_reason = reason.replace(pid, "[deleted-project]")
                    try:
                        db.execute(
                            "UPDATE ledger_suggestion_decisions SET reason=? "
                            "WHERE id=?", (new_reason, rid))
                        scrubbed["ledger_suggestion_decisions"] = int(
                            scrubbed["ledger_suggestion_decisions"]) + 1
                    except sqlite3.Error:
                        raise
    finally:
        db.close()
    return scrubbed
