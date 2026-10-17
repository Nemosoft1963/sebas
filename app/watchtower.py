"""C-1 見張り(画面内通知)と C-2 判断待ち横断一覧(読み取り専用)。

一般機構。特定PJ・件数・IDに依存しない。PJのデータには書込まず、
見張り自身の状態(スナップショットと通知)だけを保存する。画面内のみ
(Windows通知・メール・外部送信なし)。外部呼出0。

- collect_snapshot: 論理削除中を除く全PJの build_goal_state 要約を集める。
  1PJの失敗で他を止めない。失敗PJは unknown(理由は例外の型名のみ)で残す。
- diff_snapshots: 変化を決定的に列挙する。変化がなければ作らない。冪等キー付き。
- refresh: 追記のみ・排他。同時実行で二重に作らない。
- collect_decisions: 人の判断待ちを種類別・古い順に並べる。おすすめは付けない。
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

WATCH_FORMAT = "c1-watch/1"
SNAPSHOT_KEEP_REFRESHS = 20

EVENT_KINDS = (
    "stop_appeared",
    "stop_changed",
    "stop_cleared",
    "criteria_progress",
    "autopilot_failed",
    "read_failed",
    "read_recovered",
    "new_project",
    "project_removed",
    "capability_verified",
    "capability_still_remaining",
    "capability_stale",
)

SEVERITY_BY_KIND = {
    "stop_appeared": "attention",
    "stop_changed": "attention",
    "stop_cleared": "info",
    "criteria_progress": "info",
    "autopilot_failed": "alert",
    "read_failed": "alert",
    "read_recovered": "info",
    "new_project": "info",
    "project_removed": "info",
    "capability_verified": "info",
    "capability_still_remaining": "attention",
    "capability_stale": "attention",
}

SEVERITIES = ("info", "attention", "alert")

DECISION_KINDS = (
    "goal_plan_create",
    "plan_approval",
    "issue_binding",
    "fact_confirm",
    "proposal_confirm",
    "instruction_output",
    "evidence_confirm",
    "unknown_confirm",
)

DECISION_JA = {
    "goal_plan_create": "目標と計画の作成",
    "plan_approval": "計画承認",
    "issue_binding": "指摘の採否",
    "fact_confirm": "業務事実の確認",
    "proposal_confirm": "不足機能の提案の確認",
    "instruction_output": "指示の出力",
    "evidence_confirm": "証拠の確認",
    "unknown_confirm": "確認できず",
}

DECISION_SCREEN = {
    "goal_plan_create": "目標と計画",
    "plan_approval": "計画内容",
    "issue_binding": "計画内容",
    "fact_confirm": "計画内容",
    "proposal_confirm": "不足機能",
    "instruction_output": "不足機能",
    "evidence_confirm": "成果物",
    "unknown_confirm": "",
}

DECISION_WHAT = {
    "goal_plan_create": "目標と達成条件・計画が未作成です。作成してください",
    "plan_approval": "計画の承認を判断してください",
    "issue_binding": "指摘の採否・対応付けの確定を判断してください",
    "fact_confirm": "業務事実の確認を判断してください",
    "proposal_confirm": "不足機能の提案の内容を確認してください",
    "instruction_output": "不足機能の提案の指示を出力した記録を確認してください",
    "evidence_confirm": "証拠・成果物を確認してください",
    "unknown_confirm": "状態を確認できないため判断内容を確定できません",
}


def _type_name(exc: BaseException) -> str:
    try:
        return str(type(exc).__name__ or "Error")
    except Exception:
        return "Error"


def _safe_text(value: object, limit: int = 200) -> str:
    blob = str(value if value is not None else "")
    return " ".join(blob.split())[: max(1, int(limit))]


def _resolve_mem_path(manager_or_path) -> Path:
    if hasattr(manager_or_path, "memory"):
        return Path(manager_or_path.memory.path)
    return Path(manager_or_path)


def watch_db_path(manager_or_path) -> Path:
    mem = _resolve_mem_path(manager_or_path)
    return mem.with_name(mem.name + ".watch.sqlite3")


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
    db.execute("""CREATE TABLE IF NOT EXISTS watch_snapshots(
        refresh_id TEXT NOT NULL,
        project_id TEXT NOT NULL,
        snapshot_json TEXT NOT NULL DEFAULT '{}',
        created_at REAL NOT NULL,
        PRIMARY KEY (refresh_id, project_id))""")
    db.execute("""CREATE TABLE IF NOT EXISTS watch_events(
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL DEFAULT '',
        kind TEXT NOT NULL DEFAULT '',
        severity TEXT NOT NULL DEFAULT 'info',
        summary_ja TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL,
        idempotency_key TEXT NOT NULL DEFAULT '',
        ack_by TEXT NOT NULL DEFAULT '',
        ack_at REAL NOT NULL DEFAULT 0,
        ack_note TEXT NOT NULL DEFAULT '')""")
    db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_watch_events_idem
        ON watch_events(project_id, kind, idempotency_key)""")
    db.execute("""CREATE INDEX IF NOT EXISTS idx_watch_events_created
        ON watch_events(created_at DESC)""")
    db.execute("""CREATE INDEX IF NOT EXISTS idx_watch_snapshots_created
        ON watch_snapshots(created_at DESC)""")


@contextmanager
def _connect(manager_or_path):
    path = watch_db_path(manager_or_path)
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
    path = watch_db_path(manager_or_path)
    if not path.exists():
        return None
    db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=15)
    db.row_factory = sqlite3.Row
    return db


def _delete_state_of(manager, project_id: str) -> str:
    try:
        from app.project_delete import delete_state_status

        mem_path = getattr(getattr(manager, "memory", None), "path", None)
        if mem_path is None:
            return "unknown"
        return str(delete_state_status(mem_path, str(project_id or "")) or "unknown")
    except Exception as exc:
        LOGGER.warning("watch delete-state unreadable: %s", _type_name(exc))
        return "unknown"


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


def _summarize_goal_state(pid: str, state: dict) -> dict:
    if not isinstance(state, dict):
        raise ValueError("進捗モデルの形式を確認できません")
    stop = state.get("stop")
    if not isinstance(stop, dict):
        raise ValueError("停止分類を確認できません")
    summary = state.get("summary")
    if not isinstance(summary, dict):
        raise ValueError("集計を確認できません")
    next_step = state.get("next_step")
    if not isinstance(next_step, dict):
        raise ValueError("次の一手を確認できません")
    proposals = state.get("proposals")
    if not isinstance(proposals, dict):
        raise ValueError("提案を確認できません")
    autopilot = state.get("autopilot")
    if not isinstance(autopilot, dict):
        raise ValueError("自走を確認できません")
    for key in ("achieved", "capability_gap", "evidence_gap",
                "human_decision", "unknown", "total"):
        if key not in summary:
            raise ValueError("集計の項目を確認できません:" + key)
        if type(summary[key]) is not int:
            raise ValueError("集計の形式を確認できません:" + key)
    if "goal_achieved" not in summary or "consistent" not in summary:
        raise ValueError("集計の達成を確認できません")
    if type(summary["goal_achieved"]) is not bool:
        raise ValueError("達成の形式を確認できません")
    stop_class = str(stop.get("stop_class") or "")
    stop_code = str(stop.get("stop_code") or "")
    if stop_class == "" or stop_code == "":
        raise ValueError("停止の形式を確認できません")
    next_kind = str(next_step.get("kind") or "")
    if next_kind == "":
        raise ValueError("次の一手の形式を確認できません")
    preflight_status = state.get("preflight_status")
    if preflight_status is None:
        raise ValueError("事前照合の状態を確認できません")
    preflight_text = str(preflight_status)
    auto_enabled_raw = autopilot.get("enabled")
    auto_readable_raw = autopilot.get("readable")
    if not isinstance(auto_enabled_raw, bool) or not isinstance(
            auto_readable_raw, bool):
        raise ValueError("自走の形式を確認できません")
    latest = autopilot.get("latest")
    if latest is not None and not isinstance(latest, dict):
        raise ValueError("ティックの形式を確認できません")
    latest_status = str((latest or {}).get("status") or "")
    tick_count_raw = autopilot.get("tick_count")
    if tick_count_raw is not None and type(tick_count_raw) is not int:
        raise ValueError("ティック件数の形式を確認できません")
    prop_readable_raw = proposals.get("readable")
    if not isinstance(prop_readable_raw, bool):
        raise ValueError("提案の形式を確認できません")
    by_status_raw = proposals.get("by_status")
    if by_status_raw is not None and not isinstance(by_status_raw, dict):
        raise ValueError("提案の状態別件数を確認できません")
    by_status: dict[str, int] = {}
    if isinstance(by_status_raw, dict):
        for key in sorted(by_status_raw.keys()):
            value = by_status_raw[key]
            if type(value) is not int:
                raise ValueError("提案の件数を確認できません:" + str(key))
            by_status[str(key)] = int(value)
    prop_total_raw = proposals.get("total")
    if prop_total_raw is not None and type(prop_total_raw) is not int:
        raise ValueError("提案の合計を確認できません")
    return {
        "project_id": str(pid),
        "readable": True,
        "reason": "",
        "stop_class": stop_class[:64],
        "stop_code": stop_code[:128],
        "criteria": {
            "achieved": int(summary["achieved"]),
            "capability_gap": int(summary["capability_gap"]),
            "evidence_gap": int(summary["evidence_gap"]),
            "human_decision": int(summary["human_decision"]),
            "unknown": int(summary["unknown"]),
            "total": int(summary["total"]),
            "consistent": bool(summary["consistent"]),
            "goal_achieved": bool(summary["goal_achieved"]),
        },
        "next_kind": next_kind,
        "preflight_status": preflight_text,
        "autopilot": {
            "enabled": bool(auto_enabled_raw),
            "readable": bool(auto_readable_raw),
            "latest_status": latest_status[:64],
            "tick_count": tick_count_raw,
        },
        "proposals": {
            "readable": bool(prop_readable_raw),
            "by_status": by_status,
            "total": prop_total_raw,
        },
    }


def collect_snapshot(manager) -> dict:
    """全PJの読み取り要約を集める。PJデータには書込まない。外部呼出0。

    論理削除中(deleted/purged)は除く。削除状態が不明(unknown)のPJは
    unknownとして残す(消さない・正常扱いにしない)。1PJの失敗で他を止めない。
    """
    from app.goal_state_model import build_goal_state

    projects = _list_projects(manager)
    collected: dict[str, dict] = {}
    for item in projects:
        pid = str(item["id"])
        state_text = _delete_state_of(manager, pid)
        if state_text in ("deleted", "purged"):
            continue
        if state_text not in ("active", "restored", "unknown"):
            collected[pid] = {
                "project_id": pid, "readable": False,
                "reason": "ValueError",
            }
            continue
        if state_text == "unknown":
            collected[pid] = {
                "project_id": pid, "readable": False,
                "reason": "UnknownDeleteState",
            }
            continue
        try:
            raw = build_goal_state(manager, pid)
            collected[pid] = _summarize_goal_state(pid, raw)
        except Exception as exc:
            collected[pid] = {
                "project_id": pid, "readable": False,
                "reason": _type_name(exc),
            }
    return {"format": WATCH_FORMAT, "taken_at": time.time(),
            "projects": collected}


def _is_empty_stop(stop_class: str, stop_code: str) -> bool:
    cls = str(stop_class or "")
    code = str(stop_code or "")
    if cls in ("", "none", "unknown"):
        return True
    if code in ("", "unknown:empty", "unknown"):
        return True
    return False


def _autopilot_failed(auto: dict) -> bool:
    if not isinstance(auto, dict):
        return False
    if not bool(auto.get("readable")):
        return False
    latest = str(auto.get("latest_status") or "")
    return latest in ("failed", "preflight_unknown", "stale")


def _event_id(project_id: str, kind: str, idem: str) -> str:
    raw = "|".join((str(project_id or ""), str(kind or ""), str(idem or "")))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _criteria_text(cur: dict, prev: dict) -> str:
    pid = str(cur.get("project_id") or "")
    cur_c = cur.get("criteria") if isinstance(cur, dict) else None
    prev_c = prev.get("criteria") if isinstance(prev, dict) else None
    if not isinstance(cur_c, dict) or not isinstance(prev_c, dict):
        return "PJ " + pid + ": 達成条件の状態を確認できません"
    same_total = int(cur_c.get("total", -1)) == int(prev_c.get("total", -2))
    total_part = "全" + str(int(cur_c.get("total", 0))) + "件"
    if not same_total:
        total_part = ("全" + str(int(prev_c.get("total", 0))) + "件→"
                      + str(int(cur_c.get("total", 0))) + "件")
    cur_a = int(cur_c.get("achieved", 0))
    prev_a = int(prev_c.get("achieved", 0))
    if cur_a > prev_a:
        return ("PJ " + pid + ": 達成 " + str(prev_a) + "件→"
                + str(cur_a) + "件、" + total_part)
    return ("PJ " + pid + ": 達成条件の状態が変化("
            + total_part + ")")


def diff_snapshots(prev: dict, cur: dict) -> list[dict]:
    """前回と今回の差を決定的に列挙する。変化がなければ空。重複を作らない。"""
    prev_projects = {}
    cur_projects = {}
    if isinstance(prev, dict) and isinstance(prev.get("projects"), dict):
        prev_projects = prev["projects"]
    if isinstance(cur, dict) and isinstance(cur.get("projects"), dict):
        cur_projects = cur["projects"]
    prev_ids = sorted([str(k) for k in prev_projects.keys()])
    cur_ids = sorted([str(k) for k in cur_projects.keys()])
    events: list[dict] = []
    for pid in cur_ids:
        if pid not in prev_projects:
            cur_one = cur_projects.get(pid)
            if not isinstance(cur_one, dict):
                continue
            idem = "new:" + pid
            events.append({
                "id": _event_id(pid, "new_project", idem),
                "project_id": pid, "kind": "new_project",
                "severity": SEVERITY_BY_KIND["new_project"],
                "summary_ja": "PJ " + pid + " を初めて確認しました",
                "idempotency_key": idem,
            })
    for pid in prev_ids:
        if pid not in cur_projects:
            idem = "removed:" + pid
            events.append({
                "id": _event_id(pid, "project_removed", idem),
                "project_id": pid, "kind": "project_removed",
                "severity": SEVERITY_BY_KIND["project_removed"],
                "summary_ja": "PJ " + pid + " を確認できなくなりました",
                "idempotency_key": idem,
            })
    for pid in sorted(set(prev_ids) & set(cur_ids)):
        prev_one = prev_projects.get(pid)
        cur_one = cur_projects.get(pid)
        if not isinstance(prev_one, dict) or not isinstance(cur_one, dict):
            continue
        prev_ok = bool(prev_one.get("readable"))
        cur_ok = bool(cur_one.get("readable"))
        if not prev_ok and not cur_ok:
            prev_reason = str(prev_one.get("reason") or "Error")
            cur_reason = str(cur_one.get("reason") or "Error")
            if cur_reason != prev_reason:
                idem = "read:" + pid + ":" + cur_reason
                events.append({
                    "id": _event_id(pid, "read_failed", idem),
                    "project_id": pid, "kind": "read_failed",
                    "severity": SEVERITY_BY_KIND["read_failed"],
                    "summary_ja": ("PJ " + pid + " を読み取れません:"
                                   + cur_reason),
                    "idempotency_key": idem,
                })
            continue
        if prev_ok and not cur_ok:
            cur_reason = str(cur_one.get("reason") or "Error")
            idem = "read:" + pid + ":" + cur_reason
            events.append({
                "id": _event_id(pid, "read_failed", idem),
                "project_id": pid, "kind": "read_failed",
                "severity": SEVERITY_BY_KIND["read_failed"],
                "summary_ja": "PJ " + pid + " を読み取れません:" + cur_reason,
                "idempotency_key": idem,
            })
            continue
        if not prev_ok and cur_ok:
            idem = "recovered:" + pid
            events.append({
                "id": _event_id(pid, "read_recovered", idem),
                "project_id": pid, "kind": "read_recovered",
                "severity": SEVERITY_BY_KIND["read_recovered"],
                "summary_ja": "PJ " + pid + " を読み取れるようになりました",
                "idempotency_key": idem,
            })
            continue
        prev_stop = (str(prev_one.get("stop_class") or ""),
                     str(prev_one.get("stop_code") or ""))
        cur_stop = (str(cur_one.get("stop_class") or ""),
                    str(cur_one.get("stop_code") or ""))
        if prev_stop != cur_stop:
            prev_empty = _is_empty_stop(prev_stop[0], prev_stop[1])
            cur_empty = _is_empty_stop(cur_stop[0], cur_stop[1])
            if prev_empty and not cur_empty:
                kind = "stop_appeared"
                summary = ("PJ " + pid + " に停止を確認:"
                           + cur_stop[0] + "/" + cur_stop[1])
            elif not prev_empty and cur_empty:
                kind = "stop_cleared"
                summary = ("PJ " + pid + " の停止が解消:"
                           + prev_stop[0] + "/" + prev_stop[1])
            else:
                kind = "stop_changed"
                summary = ("PJ " + pid + " の停止が変化:"
                           + prev_stop[0] + "/" + prev_stop[1]
                           + "→" + cur_stop[0] + "/" + cur_stop[1])
            idem = ("stop:" + pid + ":" + prev_stop[0] + "/" + prev_stop[1]
                    + "->" + cur_stop[0] + "/" + cur_stop[1])
            events.append({
                "id": _event_id(pid, kind, idem),
                "project_id": pid, "kind": kind,
                "severity": SEVERITY_BY_KIND[kind],
                "summary_ja": summary[:200],
                "idempotency_key": idem,
            })
        prev_c = prev_one.get("criteria")
        cur_c = cur_one.get("criteria")
        if isinstance(prev_c, dict) and isinstance(cur_c, dict):
            keys = ("achieved", "capability_gap", "evidence_gap",
                    "human_decision", "unknown", "total")
            changed = False
            for key in keys:
                try:
                    if int(prev_c.get(key, -1)) != int(cur_c.get(key, -2)):
                        changed = True
                        break
                except (TypeError, ValueError):
                    changed = True
                    break
            try:
                prev_goal = bool(prev_c.get("goal_achieved"))
            except (TypeError, ValueError):
                prev_goal = False
            try:
                cur_goal = bool(cur_c.get("goal_achieved"))
            except (TypeError, ValueError):
                cur_goal = False
            if prev_goal != cur_goal:
                changed = True
            if changed:
                try:
                    idem = ("criteria:" + pid + ":"
                            + ",".join(str(int(prev_c.get(k, 0))) for k in keys)
                            + "->"
                            + ",".join(str(int(cur_c.get(k, 0))) for k in keys))
                except (TypeError, ValueError):
                    idem = "criteria:" + pid + ":changed"
                events.append({
                    "id": _event_id(pid, "criteria_progress", idem),
                    "project_id": pid, "kind": "criteria_progress",
                    "severity": SEVERITY_BY_KIND["criteria_progress"],
                    "summary_ja": _criteria_text(cur_one, prev_one)[:200],
                    "idempotency_key": idem,
                })
        prev_auto = prev_one.get("autopilot")
        cur_auto = cur_one.get("autopilot")
        if isinstance(prev_auto, dict) and isinstance(cur_auto, dict):
            prev_fail = _autopilot_failed(prev_auto)
            cur_fail = _autopilot_failed(cur_auto)
            if cur_fail and not prev_fail:
                latest = str(cur_auto.get("latest_status") or "")
                tick_raw = cur_auto.get("tick_count")
                tick_text = ""
                if type(tick_raw) is int:
                    tick_text = " tick" + str(int(tick_raw))
                idem = "auto:" + pid + ":" + latest + tick_text
                events.append({
                    "id": _event_id(pid, "autopilot_failed", idem),
                    "project_id": pid, "kind": "autopilot_failed",
                    "severity": SEVERITY_BY_KIND["autopilot_failed"],
                    "summary_ja": ("PJ " + pid + " の自走が失敗:"
                                   + latest)[:200],
                    "idempotency_key": idem,
                })
            elif cur_fail and prev_fail:
                prev_latest = str(prev_auto.get("latest_status") or "")
                cur_latest = str(cur_auto.get("latest_status") or "")
                prev_tick = prev_auto.get("tick_count")
                cur_tick = cur_auto.get("tick_count")
                if prev_latest != cur_latest or prev_tick != cur_tick:
                    idem = ("auto:" + pid + ":" + prev_latest + "->"
                            + cur_latest)
                    events.append({
                        "id": _event_id(pid, "autopilot_failed", idem),
                        "project_id": pid, "kind": "autopilot_failed",
                        "severity": SEVERITY_BY_KIND["autopilot_failed"],
                        "summary_ja": ("PJ " + pid + " の自走が失敗:"
                                       + cur_latest)[:200],
                        "idempotency_key": idem,
                    })
        prev_pre = str(prev_one.get("preflight_status") or "")
        cur_pre = str(cur_one.get("preflight_status") or "")
        if prev_pre != cur_pre and cur_pre != "ok" and prev_pre == "ok":
            idem = "auto:" + pid + ":preflight:" + cur_pre
            dup = False
            for item in events:
                if item.get("project_id") == pid and item.get("kind") == "autopilot_failed":
                    dup = True
                    break
            if not dup:
                events.append({
                    "id": _event_id(pid, "autopilot_failed", idem),
                    "project_id": pid, "kind": "autopilot_failed",
                    "severity": SEVERITY_BY_KIND["autopilot_failed"],
                    "summary_ja": ("PJ " + pid + " の事前照合を確認できず:"
                                   + cur_pre)[:200],
                    "idempotency_key": idem,
                })
    events.sort(key=lambda item: (str(item.get("project_id") or ""),
                                  str(item.get("kind") or ""),
                                  str(item.get("idempotency_key") or "")))
    return events


def _load_latest_snapshot(manager_or_path) -> dict:
    db = _read_connection(manager_or_path)
    if db is None:
        return {"format": WATCH_FORMAT, "taken_at": 0, "projects": {}}
    try:
        try:
            rows = db.execute(
                "SELECT refresh_id, MAX(created_at) AS created_at "
                "FROM watch_snapshots GROUP BY refresh_id "
                "ORDER BY created_at DESC LIMIT 1").fetchall()
        except sqlite3.OperationalError as exc:
            raise ValueError("見張りの記録を確認できません:"
                             + _type_name(exc))
        if not rows:
            return {"format": WATCH_FORMAT, "taken_at": 0, "projects": {}}
        latest_id = str(rows[0]["refresh_id"] or "")
        if latest_id == "":
            return {"format": WATCH_FORMAT, "taken_at": 0, "projects": {}}
        try:
            data = db.execute(
                "SELECT project_id, snapshot_json, created_at "
                "FROM watch_snapshots WHERE refresh_id=?",
                (latest_id,)).fetchall()
        except sqlite3.OperationalError as exc:
            raise ValueError("見張りの記録を確認できません:"
                             + _type_name(exc))
        projects: dict[str, dict] = {}
        taken = 0.0
        for row in data:
            pid = str(row["project_id"] or "")
            if pid == "":
                continue
            try:
                one = json.loads(str(row["snapshot_json"] or "{}"))
            except (ValueError, TypeError):
                raise ValueError("見張りの記録を確認できません:ValueError")
            if not isinstance(one, dict):
                raise ValueError("見張りの記録を確認できません:TypeError")
            projects[pid] = one
            try:
                taken = max(taken, float(row["created_at"] or 0))
            except (TypeError, ValueError):
                taken = max(taken, 0.0)
        return {"format": WATCH_FORMAT, "taken_at": taken,
                "projects": projects, "refresh_id": latest_id}
    finally:
        db.close()


def refresh(manager, actor: str) -> dict:
    """スナップショットを集め、前回と比較し、新規イベントを保存する。

    追記のみ・排他。同時実行で二重に作らない。PJデータには書込まない。
    """
    actor_name = str(actor or "").strip()
    if actor_name == "":
        raise ValueError("actor is required")
    cur = collect_snapshot(manager)
    try:
        total_projects = len(cur.get("projects") or {})
    except (TypeError, AttributeError):
        raise ValueError("スナップショットを確認できません")
    unreadable = 0
    for pid in sorted((cur.get("projects") or {}).keys()):
        one = (cur.get("projects") or {}).get(pid)
        if isinstance(one, dict) and not bool(one.get("readable")):
            unreadable += 1
    path = watch_db_path(manager)
    lock = _lock_for(path)
    with lock:
        with _connect(manager) as db:
            db.execute("BEGIN IMMEDIATE")
            prev = _load_latest_snapshot(manager)
            events = diff_snapshots(prev, cur)
            now = time.time()
            refresh_id = "wr_" + uuid.uuid4().hex[:16]
            inserted = 0
            for item in events:
                if not isinstance(item, dict):
                    continue
                kind = str(item.get("kind") or "")
                if kind not in EVENT_KINDS:
                    continue
                severity = str(item.get("severity") or "info")
                if severity not in SEVERITIES:
                    severity = "info"
                try:
                    cursor = db.execute(
                        """INSERT OR IGNORE INTO watch_events(
                               id, project_id, kind, severity, summary_ja,
                               created_at, idempotency_key, ack_by, ack_at,
                               ack_note)
                           VALUES(?,?,?,?,?,?,?,?,?,?)""",
                        (str(item.get("id") or _event_id(
                            str(item.get("project_id") or ""), kind,
                            str(item.get("idempotency_key") or ""))),
                         str(item.get("project_id") or "")[:128],
                         kind, severity,
                         _safe_text(item.get("summary_ja") or "", 200),
                         now, str(item.get("idempotency_key") or "")[:200],
                         "", 0.0, ""))
                    if cursor.rowcount:
                        inserted += int(cursor.rowcount)
                except sqlite3.Error as exc:
                    raise ValueError("見張りの記録に失敗しました:"
                                     + _type_name(exc))
            try:
                for pid in sorted((cur.get("projects") or {}).keys()):
                    one = (cur.get("projects") or {}).get(pid)
                    if not isinstance(one, dict):
                        continue
                    db.execute(
                        """INSERT OR REPLACE INTO watch_snapshots(
                               refresh_id, project_id, snapshot_json,
                               created_at)
                           VALUES(?,?,?,?)""",
                        (refresh_id, str(pid)[:128],
                         json.dumps(one, ensure_ascii=False, sort_keys=True),
                         now))
            except (sqlite3.Error, TypeError, ValueError) as exc:
                raise ValueError("見張りの記録に失敗しました:"
                                 + _type_name(exc))
            try:
                ids = [row[0] for row in db.execute(
                    "SELECT DISTINCT refresh_id FROM watch_snapshots "
                    "ORDER BY created_at DESC").fetchall()]
            except sqlite3.Error as exc:
                raise ValueError("見張りの記録を確認できません:"
                                 + _type_name(exc))
            if len(ids) > SNAPSHOT_KEEP_REFRESHS:
                for old_id in ids[SNAPSHOT_KEEP_REFRESHS:]:
                    try:
                        db.execute("DELETE FROM watch_snapshots WHERE refresh_id=?",
                                   (str(old_id),))
                    except sqlite3.Error as exc:
                        raise ValueError("見張りの整理に失敗しました:"
                                         + _type_name(exc))
            db.commit()
    return {"ok": True, "refresh_id": refresh_id,
            "new_events": int(inserted),
            "total_projects": int(total_projects),
            "unreadable_projects": int(unreadable),
            "actor": actor_name[:100], "created_at": now}


def list_events(manager_or_path, unread_only: bool = False,
                limit: int = 50) -> list[dict]:
    try:
        count = max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        raise ValueError("limit が不正です")
    db = _read_connection(manager_or_path)
    if db is None:
        return []
    try:
        try:
            if unread_only:
                rows = db.execute(
                    "SELECT * FROM watch_events WHERE ack_by='' "
                    "ORDER BY created_at DESC LIMIT ?",
                    (count,)).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM watch_events ORDER BY created_at DESC "
                    "LIMIT ?", (count,)).fetchall()
        except sqlite3.OperationalError as exc:
            raise ValueError("見張りの記録を確認できません:" + _type_name(exc))
        out: list[dict] = []
        for row in rows:
            item = dict(row)
            try:
                created = float(item.get("created_at") or 0)
            except (TypeError, ValueError):
                raise ValueError("見張りの記録を確認できません:ValueError")
            try:
                ack_at = float(item.get("ack_at") or 0)
            except (TypeError, ValueError):
                raise ValueError("見張りの記録を確認できません:ValueError")
            out.append({
                "id": str(item.get("id") or ""),
                "project_id": str(item.get("project_id") or ""),
                "kind": str(item.get("kind") or ""),
                "severity": str(item.get("severity") or "info"),
                "summary_ja": str(item.get("summary_ja") or ""),
                "created_at": created,
                "idempotency_key": str(item.get("idempotency_key") or ""),
                "ack_by": str(item.get("ack_by") or ""),
                "ack_at": ack_at,
                "ack_note": str(item.get("ack_note") or ""),
                "unread": str(item.get("ack_by") or "") == "",
            })
        return out
    finally:
        db.close()


def ack_event(manager_or_path, event_id: str, actor: str,
              note: str = "") -> dict:
    eid = str(event_id or "").strip()
    if eid == "":
        raise ValueError("event id is required")
    actor_name = str(actor or "").strip()
    if actor_name == "":
        raise ValueError("actor is required")
    note_text = _safe_text(note or "", 500)
    path = watch_db_path(manager_or_path)
    lock = _lock_for(path)
    with lock:
        with _connect(manager_or_path) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute("SELECT * FROM watch_events WHERE id=?",
                                 (eid,)).fetchone()
            except sqlite3.Error as exc:
                raise ValueError("見張りの記録を確認できません:"
                                 + _type_name(exc))
            if row is None:
                raise ValueError("通知が見つかりません")
            before = dict(row)
            now = time.time()
            try:
                db.execute("UPDATE watch_events SET ack_by=?, ack_at=?, "
                           "ack_note=? WHERE id=?",
                           (actor_name[:100], now, note_text, eid))
            except sqlite3.Error as exc:
                raise ValueError("既読の記録に失敗しました:" + _type_name(exc))
            db.commit()
            try:
                after = db.execute("SELECT * FROM watch_events WHERE id=?",
                                   (eid,)).fetchone()
            except sqlite3.Error as exc:
                raise ValueError("既読の記録を確認できません:"
                                 + _type_name(exc))
            if after is None:
                raise ValueError("既読の記録を確認できません:ValueError")
            result = dict(after)
    for key in ("summary_ja", "kind", "severity", "project_id"):
        if str(before.get(key) or "") != str(result.get(key) or ""):
            raise ValueError("通知の本文が変わりました:ValueError")
    try:
        created = float(result.get("created_at") or 0)
    except (TypeError, ValueError):
        raise ValueError("見張りの記録を確認できません:ValueError")
    return {"ok": True, "id": str(result.get("id") or ""),
            "project_id": str(result.get("project_id") or ""),
            "kind": str(result.get("kind") or ""),
            "severity": str(result.get("severity") or "info"),
            "summary_ja": str(result.get("summary_ja") or ""),
            "created_at": created,
            "ack_by": str(result.get("ack_by") or ""),
            "ack_at": now, "ack_note": note_text}


def watch_status(manager_or_path) -> dict:
    db = _read_connection(manager_or_path)
    if db is None:
        return {"last_refresh_at": 0.0, "unread_count": 0,
                "unreadable_projects": 0, "total_projects": 0,
                "has_snapshot": False}
    try:
        try:
            row = db.execute(
                "SELECT MAX(created_at) AS last_at FROM watch_snapshots"
            ).fetchone()
            try:
                last_at = float((row["last_at"] if row else None) or 0)
            except (TypeError, ValueError):
                raise ValueError("見張りの記録を確認できません:ValueError")
            count_row = db.execute(
                "SELECT COUNT(*) AS unread FROM watch_events WHERE ack_by=''"
            ).fetchone()
            try:
                unread = int((count_row["unread"] if count_row else 0) or 0)
            except (TypeError, ValueError):
                raise ValueError("見張りの記録を確認できません:ValueError")
        except sqlite3.OperationalError as exc:
            raise ValueError("見張りの記録を確認できません:" + _type_name(exc))
        latest = _load_latest_snapshot(manager_or_path)
        projects = latest.get("projects") if isinstance(latest, dict) else None
        if not isinstance(projects, dict):
            raise ValueError("見張りの記録を確認できません:TypeError")
        unreadable = 0
        for pid in sorted(projects.keys()):
            one = projects.get(pid)
            if isinstance(one, dict) and not bool(one.get("readable")):
                unreadable += 1
        return {"last_refresh_at": last_at, "unread_count": unread,
                "unreadable_projects": int(unreadable),
                "total_projects": int(len(projects)),
                "has_snapshot": bool(projects)}
    finally:
        db.close()


def reset_for_generation(memory_path: str | Path, project_id: str) -> dict:
    """PJ初期化(世代切替)で見張りのスナップショットを捨てる。通知(監査)は残す。"""
    pid = str(project_id or "")
    path = watch_db_path(memory_path)
    if not path.exists():
        return {"project_id": pid, "snapshots_removed": 0, "reset": False}
    removed = 0
    db = sqlite3.connect(str(path), timeout=30)
    try:
        with db:
            try:
                db.execute("""CREATE TABLE IF NOT EXISTS watch_snapshots(
                    refresh_id TEXT NOT NULL, project_id TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL,
                    PRIMARY KEY (refresh_id, project_id))""")
                cur = db.execute("DELETE FROM watch_snapshots WHERE project_id=?",
                                 (pid,))
                removed = int(cur.rowcount or 0)
            except sqlite3.Error as exc:
                raise ValueError("見張りの初期化に失敗しました:" + _type_name(exc))
    finally:
        db.close()
    return {"project_id": pid, "snapshots_removed": int(removed),
            "reset": True}


def purge_project_watch(memory_path: str | Path, project_id: str) -> dict:
    """完全削除時の後始末。当該PJの行だけ消し、他PJに影響しない。"""
    pid = str(project_id or "")
    path = watch_db_path(memory_path)
    if not path.exists():
        return {"watch_snapshots": 0, "watch_events": 0}
    removed = {"watch_snapshots": 0, "watch_events": 0}
    db = sqlite3.connect(str(path), timeout=30)
    try:
        with db:
            for table in ("watch_snapshots", "watch_events"):
                exists = db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (table,)).fetchone()
                if exists is None:
                    continue
                cur = db.execute(f"DELETE FROM {table} WHERE project_id=?",
                                 (pid,))
                removed[table] = int(cur.rowcount or 0)
    finally:
        db.close()
    return removed


def _decision_since_for_proposals(manager, project_id: str) -> str:
    try:
        from app import capability_gap as gap

        mem_path = getattr(getattr(manager, "memory", None), "path", None)
        rows = gap.list_proposals(manager, str(project_id or ""))
        _ = mem_path
    except Exception as exc:
        LOGGER.warning("watch proposals unreadable: %s", _type_name(exc))
        return ""
    if not isinstance(rows, list) or not rows:
        return ""
    stamps: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("gap_code") or "") != "missing_capability":
            continue
        for key in ("updated_at", "created_at"):
            value = row.get(key)
            if value is None:
                continue
            text = str(value)
            if text != "":
                stamps.append(text)
                break
    stamps.sort()
    return stamps[0] if stamps else ""


def _decide_one(pid: str, name: str, state: dict,
                proposal_since: str) -> list[dict]:
    if not isinstance(state, dict):
        return [{
            "project_id": pid, "project_name": name,
            "kind": "unknown_confirm",
            "kind_ja": DECISION_JA["unknown_confirm"],
            "what_ja": DECISION_WHAT["unknown_confirm"],
            "screen": "", "since": "",
            "basis": {"project_id": pid},
        }]
    summary = state.get("summary")
    stop = state.get("stop")
    next_step = state.get("next_step")
    proposals = state.get("proposals")
    if not isinstance(summary, dict) or not isinstance(stop, dict) \
            or not isinstance(next_step, dict) or not isinstance(proposals, dict):
        return [{
            "project_id": pid, "project_name": name,
            "kind": "unknown_confirm",
            "kind_ja": DECISION_JA["unknown_confirm"],
            "what_ja": DECISION_WHAT["unknown_confirm"],
            "screen": "", "since": "",
            "basis": {"project_id": pid},
        }]
    try:
        total = int(summary.get("total", 0))
    except (TypeError, ValueError):
        return [{
            "project_id": pid, "project_name": name,
            "kind": "unknown_confirm",
            "kind_ja": DECISION_JA["unknown_confirm"],
            "what_ja": DECISION_WHAT["unknown_confirm"],
            "screen": "", "since": "",
            "basis": {"project_id": pid},
        }]
    kind = str(next_step.get("kind") or "")
    screen = str(next_step.get("screen") or "")
    detail = str(next_step.get("detail_ja") or "")
    stop_class = str(stop.get("stop_class") or "")
    plan_sig = str(state.get("plan_signature") or "")
    sig_short = plan_sig[:12] if plan_sig != "" else ""
    prop_ids: list[str] = []
    raw_ids = proposals.get("ids")
    if isinstance(raw_ids, list):
        for entry in raw_ids:
            text = str(entry or "")
            if text != "" and text not in prop_ids:
                prop_ids.append(text)
        prop_ids.sort()
        prop_ids = prop_ids[:20]
    by_status = proposals.get("by_status")
    by_keys: list[str] = []
    if isinstance(by_status, dict):
        by_keys = sorted([str(k) for k in by_status.keys() if str(k) != ""])
    basis: dict = {"project_id": pid}
    if sig_short != "":
        basis["plan_signature"] = sig_short
    if prop_ids:
        basis["proposal_ids"] = list(prop_ids)
    if total == 0:
        return [{
            "project_id": pid, "project_name": name,
            "kind": "goal_plan_create",
            "kind_ja": DECISION_JA["goal_plan_create"],
            "what_ja": DECISION_WHAT["goal_plan_create"],
            "screen": DECISION_SCREEN["goal_plan_create"],
            "since": "", "basis": basis,
        }]
    if kind == "autonomous":
        return []
    if kind == "blocked_unknown":
        return []
    if kind == "evidence_confirm":
        return [{
            "project_id": pid, "project_name": name,
            "kind": "evidence_confirm",
            "kind_ja": DECISION_JA["evidence_confirm"],
            "what_ja": DECISION_WHAT["evidence_confirm"],
            "screen": DECISION_SCREEN["evidence_confirm"],
            "since": "", "basis": basis,
        }]
    if kind == "capability_dev":
        has_reviewed = any(key in ("reviewed", "export_ready", "submitted",
                                   "delivered") for key in by_keys)
        needs_instruction = has_reviewed or ("指示を出力" in detail) \
            or ("指示出力" in detail)
        if needs_instruction:
            return [{
                "project_id": pid, "project_name": name,
                "kind": "instruction_output",
                "kind_ja": DECISION_JA["instruction_output"],
                "what_ja": DECISION_WHAT["instruction_output"],
                "screen": DECISION_SCREEN["instruction_output"],
                "since": str(proposal_since or ""), "basis": basis,
            }]
        return [{
            "project_id": pid, "project_name": name,
            "kind": "proposal_confirm",
            "kind_ja": DECISION_JA["proposal_confirm"],
            "what_ja": DECISION_WHAT["proposal_confirm"],
            "screen": DECISION_SCREEN["proposal_confirm"],
            "since": str(proposal_since or ""), "basis": basis,
        }]
    if kind == "human_confirm":
        if screen == "目標と計画":
            return [{
                "project_id": pid, "project_name": name,
                "kind": "goal_plan_create",
                "kind_ja": DECISION_JA["goal_plan_create"],
                "what_ja": DECISION_WHAT["goal_plan_create"],
                "screen": DECISION_SCREEN["goal_plan_create"],
                "since": "", "basis": basis,
            }]
        if screen == "成果物":
            return [{
                "project_id": pid, "project_name": name,
                "kind": "evidence_confirm",
                "kind_ja": DECISION_JA["evidence_confirm"],
                "what_ja": DECISION_WHAT["evidence_confirm"],
                "screen": DECISION_SCREEN["evidence_confirm"],
                "since": "", "basis": basis,
            }]
        if stop_class == "missing_binding" or ("指摘" in detail) \
                or ("対応付け" in detail):
            return [{
                "project_id": pid, "project_name": name,
                "kind": "issue_binding",
                "kind_ja": DECISION_JA["issue_binding"],
                "what_ja": DECISION_WHAT["issue_binding"],
                "screen": DECISION_SCREEN["issue_binding"],
                "since": "", "basis": basis,
            }]
        if ("業務事実" in detail) or ("missing_input" in detail):
            return [{
                "project_id": pid, "project_name": name,
                "kind": "fact_confirm",
                "kind_ja": DECISION_JA["fact_confirm"],
                "what_ja": DECISION_WHAT["fact_confirm"],
                "screen": DECISION_SCREEN["fact_confirm"],
                "since": "", "basis": basis,
            }]
        return [{
            "project_id": pid, "project_name": name,
            "kind": "plan_approval",
            "kind_ja": DECISION_JA["plan_approval"],
            "what_ja": DECISION_WHAT["plan_approval"],
            "screen": DECISION_SCREEN["plan_approval"],
            "since": "", "basis": basis,
        }]
    return []


def collect_decisions(manager) -> list[dict]:
    """全PJの人の判断待ちを種類別・古い順に並べる。読み取り専用。おすすめは付けない。"""
    from app.goal_state_model import build_goal_state

    projects = _list_projects(manager)
    names = {str(item["id"]): str(item.get("name") or "") for item in projects}
    out: list[dict] = []
    for item in projects:
        pid = str(item["id"])
        state_text = _delete_state_of(manager, pid)
        if state_text in ("deleted", "purged"):
            continue
        if state_text not in ("active", "restored", "unknown"):
            out.append({
                "project_id": pid, "project_name": names.get(pid, ""),
                "kind": "unknown_confirm",
                "kind_ja": DECISION_JA["unknown_confirm"],
                "what_ja": DECISION_WHAT["unknown_confirm"],
                "screen": "", "since": "",
                "basis": {"project_id": pid},
            })
            continue
        if state_text == "unknown":
            out.append({
                "project_id": pid, "project_name": names.get(pid, ""),
                "kind": "unknown_confirm",
                "kind_ja": DECISION_JA["unknown_confirm"],
                "what_ja": DECISION_WHAT["unknown_confirm"],
                "screen": "", "since": "",
                "basis": {"project_id": pid},
            })
            continue
        try:
            raw = build_goal_state(manager, pid)
        except Exception as exc:
            out.append({
                "project_id": pid, "project_name": names.get(pid, ""),
                "kind": "unknown_confirm",
                "kind_ja": DECISION_JA["unknown_confirm"],
                "what_ja": DECISION_WHAT["unknown_confirm"],
                "screen": "", "since": "",
                "basis": {"project_id": pid,
                          "reason": _type_name(exc)},
            })
            continue
        since = ""
        try:
            next_kind = str((raw.get("next_step") or {}).get("kind") or "")
        except (AttributeError, TypeError):
            next_kind = ""
        if next_kind == "capability_dev":
            since = _decision_since_for_proposals(manager, pid)
        try:
            out.extend(_decide_one(pid, names.get(pid, ""), raw, since))
        except Exception as exc:
            out.append({
                "project_id": pid, "project_name": names.get(pid, ""),
                "kind": "unknown_confirm",
                "kind_ja": DECISION_JA["unknown_confirm"],
                "what_ja": DECISION_WHAT["unknown_confirm"],
                "screen": "", "since": "",
                "basis": {"project_id": pid,
                          "reason": _type_name(exc)},
            })
            continue
    out.sort(key=lambda row: (str(row.get("since") or ""),
                              str(row.get("project_id") or ""),
                              str(row.get("kind") or "")))
    return out


#: D-4 再判定の通知先 (追加のみ)。kind と outcome の対応は1か所。
CAPABILITY_NOTIFY_KIND = {
    "verified": "capability_verified",
    "still_remaining": "capability_still_remaining",
    "stale": "capability_stale",
}

CAPABILITY_NOTIFY_JA = {
    "verified": "元の停止が再判定で解消しました",
    "still_remaining": "元の停止が残っています",
    "stale": "計画が更新されたため古い版です",
}


def record_capability_event(manager_or_path, project_id: str, proposal_id: str,
                            base_ref: str, outcome: str) -> dict:
    """D-4 再判定の結果を1件通知する (追加のみ・既存の表を使う)。

    kind は verified/still_remaining/stale の3種だけ。冪等キー =
    proposal_id + 基準版 + 結果。同じキーでは増やさない (INSERT OR IGNORE)。
    PJのデータには書込まない (watch_events への追記のみ)。失敗は握りつぶさず
    ValueError として返す (呼出側が errors に残す)。
    """
    pid = str(project_id or "")
    target = str(proposal_id or "")
    ref = str(base_ref or "")
    result = str(outcome or "")
    if pid == "" or target == "":
        raise ValueError("project_id と proposal_id が必要です")
    if ref == "":
        raise ValueError("基準版を確認できないため通知できません")
    kind = CAPABILITY_NOTIFY_KIND.get(result)
    if kind is None:
        raise ValueError("通知の結果を確認できません:" + result[:32])
    if kind not in EVENT_KINDS:
        raise ValueError("通知の種別を確認できません:" + kind[:64])
    severity = SEVERITY_BY_KIND.get(kind)
    if severity not in SEVERITIES:
        raise ValueError("通知の重要度を確認できません:" + kind[:64])
    summary = ("PJ " + pid + " の提案 " + target[:8] + ": "
               + str(CAPABILITY_NOTIFY_JA.get(result) or "確認できず"))
    idem = "reverify:" + target + ":" + ref + ":" + result
    event_id = _event_id(pid, kind, idem)
    path = watch_db_path(manager_or_path)
    lock = _lock_for(path)
    with lock:
        with _connect(manager_or_path) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                cursor = db.execute(
                    """INSERT OR IGNORE INTO watch_events(
                           id, project_id, kind, severity, summary_ja,
                           created_at, idempotency_key, ack_by, ack_at,
                           ack_note)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (event_id, pid[:128], kind, severity,
                     _safe_text(summary, 200), time.time(), idem[:200],
                     "", 0.0, ""))
            except sqlite3.Error as exc:
                raise ValueError("見張りの記録に失敗しました:"
                                 + _type_name(exc))
            db.commit()
            inserted = bool(cursor.rowcount)
    return {"ok": True, "id": event_id, "project_id": pid, "kind": kind,
            "severity": severity, "inserted": inserted,
            "idempotency_key": idem}
