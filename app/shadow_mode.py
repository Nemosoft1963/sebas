"""C-4 影モード (実行せず予告だけ記録し、人が正誤を付ける)。

一般機構。特定PJ・件数・IDに依存しない。影モードは何も実行しない
(書込は影の記録の表だけ。PJのデータ・外部・承認・Jenkins・RAG・OCR・
削除には触れない)。IDと状態のみ。秘密・原本本文を入れない。

- observe: 既存の preview_planned_action を呼ぶだけで予告を得て記録する
  (autopilot_tick も run_named_action も呼ばない=実行ゼロ)。
  冪等キー=project_id+plan_signature+planned_action+stop_class+stop_code
  の短縮ハッシュ。同じキーがある限り重複して作らない。
- label: 人が正しい/誤り/判断できないを付ける。履歴は追記のみ。
- metrics/gate: 本番有効化の関門。読取不能は passed:false。
- 通知は画面内だけ。定期スレッド・スケジューラは作らない
  (記録は人が押したときだけ)。

サイドカーSQLite (*.shadow.sqlite3、既存の流儀。追記専用・排他):
- shadow_records: 影の記録 (最新ラベル付き)。
- shadow_label_history: ラベル変更の履歴 (追記のみ)。
- shadow_overrides: 関門の無視 (担当者名と理由必須) の記録。
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

LOGGER = logging.getLogger(__name__)

FORMAT = "c4-shadow/1"

# 関門の既定の基準 (後から変更しやすい形で1か所に)。
THRESHOLDS = {
    "min_judged": 20,
    "min_signatures": 3,
    "min_agreement": 0.95,
    "max_dangerous": 0,
    "max_age_days": 30,
}

LABELS = ("correct", "wrong", "unsure")

LABEL_JA = {
    "correct": "正しい",
    "wrong": "誤り",
    "unsure": "判断できない",
    "": "未判定",
}

# テスト用の失敗注入フック。製品コードでは常に None。
FAIL_INJECT: dict | None = None


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


def shadow_db_path(manager_or_path) -> Path:
    mem = _resolve_mem_path(manager_or_path)
    return mem.with_name(mem.name + ".shadow.sqlite3")


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
    db.execute("""CREATE TABLE IF NOT EXISTS shadow_records(
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL DEFAULT '',
        plan_signature TEXT NOT NULL DEFAULT '',
        observed_at REAL NOT NULL,
        actor TEXT NOT NULL DEFAULT '',
        planned_action TEXT NOT NULL DEFAULT '',
        stop_class TEXT NOT NULL DEFAULT '',
        stop_code TEXT NOT NULL DEFAULT '',
        gate_status TEXT NOT NULL DEFAULT '',
        preflight_status TEXT NOT NULL DEFAULT '',
        next_step_kind TEXT NOT NULL DEFAULT '',
        reason_ja TEXT NOT NULL DEFAULT '',
        label TEXT NOT NULL DEFAULT '',
        label_by TEXT NOT NULL DEFAULT '',
        label_at REAL NOT NULL DEFAULT 0,
        label_note TEXT NOT NULL DEFAULT '',
        danger INTEGER NOT NULL DEFAULT 0,
        idempotency_key TEXT NOT NULL DEFAULT '')""")
    db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_shadow_records_idem
        ON shadow_records(idempotency_key)""")
    db.execute("""CREATE INDEX IF NOT EXISTS idx_shadow_records_project
        ON shadow_records(project_id, observed_at DESC)""")
    db.execute("""CREATE TABLE IF NOT EXISTS shadow_label_history(
        id TEXT PRIMARY KEY,
        record_id TEXT NOT NULL DEFAULT '',
        project_id TEXT NOT NULL DEFAULT '',
        label TEXT NOT NULL DEFAULT '',
        actor TEXT NOT NULL DEFAULT '',
        note TEXT NOT NULL DEFAULT '',
        danger INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL)""")
    db.execute("""CREATE INDEX IF NOT EXISTS idx_shadow_label_history_record
        ON shadow_label_history(record_id, created_at)""")
    db.execute("""CREATE TABLE IF NOT EXISTS shadow_overrides(
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL DEFAULT '',
        actor TEXT NOT NULL DEFAULT '',
        reason TEXT NOT NULL DEFAULT '',
        gate_summary TEXT NOT NULL DEFAULT '{}',
        created_at REAL NOT NULL)""")
    db.execute("""CREATE INDEX IF NOT EXISTS idx_shadow_overrides_project
        ON shadow_overrides(project_id, created_at DESC)""")


@contextmanager
def _connect(manager_or_path):
    path = shadow_db_path(manager_or_path)
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
    path = shadow_db_path(manager_or_path)
    if not path.exists():
        return None
    db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=15)
    db.row_factory = sqlite3.Row
    return db


def _delete_status_of(manager, project_id: str) -> str:
    """削除状態を返す。判定不能は "unknown" (対象外側)。"""
    from app.project_delete import delete_state_status

    try:
        mem_path = getattr(getattr(manager, "memory", None), "path", None)
        if mem_path is None:
            return "unknown"
        return str(delete_state_status(mem_path, str(project_id or ""))
                   or "unknown")
    except (OSError, ValueError, TypeError, AttributeError, KeyError,
            sqlite3.Error, RuntimeError) as exc:
        LOGGER.warning("shadow delete-state unreadable: %s",
                       _type_name(exc))
        return "unknown"


def _check_deleted(manager, project_id: str) -> bool:
    """論理削除中・状態不明なら True (対象外)。判定不能も対象外側。

    restored/active のみ対象。deleted/purged/unknown は対象外。
    """
    return _delete_status_of(manager, project_id) in (
        "deleted", "purged", "unknown")


def _classify_for_observe(manager, project_id: str) -> tuple[dict, dict, str]:
    """読み取り専用の分類。失敗は例外で返す (呼出側が ok:false にする)。"""
    from app import autopilot as _auto

    pid = str(project_id or "")
    _built, record = _auto._classify_current(manager, pid)
    if not isinstance(record, dict):
        raise ValueError("分類を確認できません")
    refs = record.get("evidence_refs")
    refs = refs if isinstance(refs, dict) else {}
    raw_sig = record.get("plan_signature") or refs.get("plan_signature") or ""
    plan_sig = str(raw_sig or "")[:128]
    if not plan_sig:
        # 現行署名で補う (取れなければ失敗)。
        try:
            from app.goal_review import plan_snapshot

            _snapshot, current = plan_snapshot(manager, pid)
            plan_sig = str(current or "")[:128]
        except (ValueError, TypeError, sqlite3.Error, OSError,
                RuntimeError) as exc:
            raise ValueError("計画署名を確認できません:"
                             + _type_name(exc)) from exc
    if not plan_sig:
        raise ValueError("計画署名を確認できません")
    stop_class = str(record.get("stop_class") or "")
    stop_code = str(record.get("stop_code") or "")
    if not stop_class or not stop_code:
        raise ValueError("停止分類を確認できません")
    return record, refs, plan_sig


def _next_step_kind_of(manager, project_id: str) -> str:
    """次の一手の種類 (読み取り専用)。読めなければ空文字 (不明は空で返す)。

    空文字は「確認できず」として画面で区別する。関門の判定には使わない。
    """
    try:
        from app.goal_state_model import build_goal_state

        state = build_goal_state(manager, str(project_id or ""))
    except (ValueError, TypeError, sqlite3.Error, OSError, KeyError,
            RuntimeError) as exc:
        LOGGER.warning("shadow next-step unreadable: %s", _type_name(exc))
        return ""
    if not isinstance(state, dict):
        return ""
    next_step = state.get("next_step")
    if not isinstance(next_step, dict):
        return ""
    kind = str(next_step.get("kind") or "")
    return kind[:64]


def _decode_record(row: sqlite3.Row) -> dict:
    item = dict(row)
    try:
        observed = float(item.get("observed_at") or 0)
    except (TypeError, ValueError):
        observed = 0.0
    try:
        label_at = float(item.get("label_at") or 0)
    except (TypeError, ValueError):
        label_at = 0.0
    return {
        "id": str(item.get("id") or ""),
        "project_id": str(item.get("project_id") or ""),
        "plan_signature": str(item.get("plan_signature") or ""),
        "observed_at": observed,
        "actor": str(item.get("actor") or ""),
        "planned_action": str(item.get("planned_action") or ""),
        "stop_class": str(item.get("stop_class") or ""),
        "stop_code": str(item.get("stop_code") or ""),
        "gate_status": str(item.get("gate_status") or ""),
        "preflight_status": str(item.get("preflight_status") or ""),
        "next_step_kind": str(item.get("next_step_kind") or ""),
        "reason_ja": str(item.get("reason_ja") or ""),
        "label": str(item.get("label") or ""),
        "label_by": str(item.get("label_by") or ""),
        "label_at": label_at,
        "label_note": str(item.get("label_note") or ""),
        "danger": int(item.get("danger") or 0),
        "idempotency_key": str(item.get("idempotency_key") or ""),
    }


def observe(manager, project_id: str, actor: str) -> dict:
    """今の予告を記録する (実行ゼロ)。失敗時は記録を作らず ok:false。

    autopilot_tick も run_named_action も呼ばない。呼ぶのは
    preview_planned_action (dry_run と共通の純粋関数) だけ。
    """
    import uuid as _uuid

    from app import autopilot as _auto

    pid = str(project_id or "")
    actor_name = _safe_text(actor, 100).strip()
    if not pid:
        return {"ok": False, "reason": "project_id is required",
                "error_type": "ValueError"}
    if not actor_name:
        return {"ok": False, "reason": "actor is required",
                "error_type": "ValueError"}
    if FAIL_INJECT is not None and isinstance(FAIL_INJECT, dict):
        stage = str(FAIL_INJECT.get("stage") or "")
        if stage == "observe_preview_fail":
            return {"ok": False, "reason": "予告を確認できません:InjectedError",
                    "error_type": "InjectedError"}
    if _check_deleted(manager, pid):
        return {"ok": False,
                "reason": "論理削除中または削除状態を確認できないPJは対象外です",
                "error_type": "DeletedOrUnknown"}
    try:
        record, _refs, plan_sig = _classify_for_observe(manager, pid)
    except Exception as exc:
        return {"ok": False,
                "reason": "分類を確認できないため記録しません:" + _type_name(exc),
                "error_type": _type_name(exc)}
    try:
        preview = _auto.preview_planned_action(manager, pid, record, plan_sig)
    except Exception as exc:
        return {"ok": False,
                "reason": "予告を確認できないため記録しません:" + _type_name(exc),
                "error_type": _type_name(exc)}
    if not isinstance(preview, dict):
        return {"ok": False,
                "reason": "予告を確認できないため記録しません:TypeError",
                "error_type": "TypeError"}
    # 予告の形を厳密に確認する。不明を成功にしない。
    for key in ("planned_action", "gate_status", "preflight_status",
                "reason_ja", "plan_signature"):
        if key not in preview:
            return {"ok": False,
                    "reason": "予告を確認できないため記録しません:KeyError",
                    "error_type": "KeyError"}
        if not isinstance(preview.get(key), str):
            return {"ok": False,
                    "reason": "予告を確認できないため記録しません:TypeError",
                    "error_type": "TypeError"}
    planned = str(preview.get("planned_action") or "")
    gate_status = str(preview.get("gate_status") or "")
    preflight_status = str(preview.get("preflight_status") or "")
    reason_ja = _safe_text(preview.get("reason_ja") or "", 1000)
    out_sig = str(preview.get("plan_signature") or "")[:128]
    if not planned or not gate_status or not out_sig:
        return {"ok": False,
                "reason": "予告を確認できないため記録しません:ValueError",
                "error_type": "ValueError"}
    stop_class = str(record.get("stop_class") or "")[:64]
    stop_code = str(record.get("stop_code") or "")[:128]
    if not stop_class or not stop_code:
        return {"ok": False,
                "reason": "停止分類を確認できないため記録しません:ValueError",
                "error_type": "ValueError"}
    next_kind = _next_step_kind_of(manager, pid)
    idem = _short_hash([pid, out_sig, planned, stop_class, stop_code])
    path = shadow_db_path(manager)
    lock = _lock_for(path)
    with lock:
        try:
            with _connect(manager) as db:
                db.execute("BEGIN IMMEDIATE")
                try:
                    existing = db.execute(
                        "SELECT * FROM shadow_records WHERE idempotency_key=?",
                        (idem,)).fetchone()
                except sqlite3.Error as exc:
                    raise ValueError("影の記録を確認できません:"
                                     + _type_name(exc)) from exc
                if existing is not None:
                    db.commit()
                    found = _decode_record(existing)
                    found["ok"] = True
                    found["idempotent"] = True
                    found["record"] = {k: found[k] for k in (
                        "id", "project_id", "plan_signature", "planned_action",
                        "stop_class", "stop_code") if k in found}
                    return {"ok": True, "idempotent": True, "record": found,
                            "record_id": str(found.get("id") or "")}
                rid = "sh_" + _uuid.uuid4().hex[:16]
                now = _utcnow()
                try:
                    db.execute(
                        """INSERT INTO shadow_records(id, project_id,
                               plan_signature, observed_at, actor, planned_action,
                               stop_class, stop_code, gate_status,
                               preflight_status, next_step_kind, reason_ja,
                               label, label_by, label_at, label_note, danger,
                               idempotency_key)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (rid, pid, out_sig, now, actor_name, planned[:64],
                         stop_class, stop_code, gate_status[:64],
                         preflight_status[:64], next_kind, reason_ja,
                         "", "", 0.0, "", 0, idem))
                except sqlite3.Error as exc:
                    raise ValueError("影の記録に失敗しました:"
                                     + _type_name(exc)) from exc
                db.commit()
                try:
                    row = db.execute(
                        "SELECT * FROM shadow_records WHERE id=?",
                        (rid,)).fetchone()
                except sqlite3.Error as exc:
                    raise ValueError("影の記録を確認できません:"
                                     + _type_name(exc)) from exc
                if row is None:
                    raise ValueError("影の記録を確認できません:ValueError")
                saved = _decode_record(row)
        except (ValueError, OSError, sqlite3.Error, RuntimeError) as exc:
            return {"ok": False,
                    "reason": "影の記録に失敗したため記録しません:"
                    + _type_name(exc),
                    "error_type": _type_name(exc)}
    saved["ok"] = True
    saved["idempotent"] = False
    return {"ok": True, "idempotent": False, "record": saved,
            "record_id": str(saved.get("id") or "")}


def observe_all(manager, actor: str) -> dict:
    """全PJを対象に1件ずつ observe。1つの失敗で他を止めない。"""
    actor_name = _safe_text(actor, 100).strip()
    if not actor_name:
        raise ValueError("actor is required")
    try:
        rows = manager.memory.list_projects()
    except (ValueError, TypeError, sqlite3.Error, OSError,
            AttributeError, RuntimeError) as exc:
        raise ValueError("PJ一覧を確認できません:"
                         + _type_name(exc)) from exc
    if not isinstance(rows, list):
        raise ValueError("PJ一覧の形式を確認できません")
    ids: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        pid = row.get("id")
        if pid is None or str(pid) == "":
            continue
        ids.append(str(pid))
    ids.sort()
    results: list[dict] = []
    for pid in ids:
        try:
            one = observe(manager, pid, actor_name)
        except (ValueError, TypeError, sqlite3.Error, OSError,
                RuntimeError) as exc:
            one = {"ok": False, "project_id": pid,
                   "reason": "確認できず:" + _type_name(exc),
                   "error_type": _type_name(exc)}
        if not isinstance(one, dict):
            one = {"ok": False, "project_id": pid,
                   "reason": "確認できず:TypeError",
                   "error_type": "TypeError"}
        out = {"project_id": pid, "ok": bool(one.get("ok"))}
        if bool(one.get("ok")):
            out["record_id"] = str(one.get("record_id") or "")
            out["idempotent"] = bool(one.get("idempotent"))
        else:
            # 失敗したPJは「確認できず」として返す (消さない)。
            reason = str(one.get("reason") or "確認できず")
            if "確認できず" not in reason:
                reason = "確認できず:" + reason
            out["reason"] = reason[:500]
            err = str(one.get("error_type") or "Error")
            out["error_type"] = err
        results.append(out)
    observed = sum(1 for item in results if bool(item.get("ok")))
    failed = [item for item in results if not bool(item.get("ok"))]
    return {"ok": True, "actor": actor_name, "results": results,
            "observed": int(observed), "failed": failed,
            "total_projects": int(len(ids))}


def list_records(manager_or_path, project_id: str, limit: int = 50) -> list[dict]:
    pid = str(project_id or "")
    try:
        count = max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        raise ValueError("limit が不正です")
    db = _read_connection(manager_or_path)
    if db is None:
        return []
    try:
        try:
            rows = db.execute(
                "SELECT * FROM shadow_records WHERE project_id=? "
                "ORDER BY observed_at DESC LIMIT ?",
                (pid, count)).fetchall()
        except sqlite3.Error as exc:
            raise ValueError("影の記録を確認できません:"
                             + _type_name(exc)) from exc
        return [_decode_record(item) for item in rows]
    finally:
        db.close()


def get_record(manager_or_path, record_id: str) -> dict:
    rid = str(record_id or "").strip()
    if not rid:
        raise ValueError("record id is required")
    db = _read_connection(manager_or_path)
    if db is None:
        raise ValueError("記録が見つかりません")
    try:
        try:
            row = db.execute("SELECT * FROM shadow_records WHERE id=?",
                             (rid,)).fetchone()
        except sqlite3.Error as exc:
            raise ValueError("影の記録を確認できません:"
                             + _type_name(exc)) from exc
        if row is None:
            raise ValueError("記録が見つかりません")
        return _decode_record(row)
    finally:
        db.close()


def label_history(manager_or_path, record_id: str) -> list[dict]:
    rid = str(record_id or "").strip()
    if not rid:
        raise ValueError("record id is required")
    db = _read_connection(manager_or_path)
    if db is None:
        return []
    try:
        try:
            rows = db.execute(
                "SELECT * FROM shadow_label_history WHERE record_id=? "
                "ORDER BY created_at ASC", (rid,)).fetchall()
        except sqlite3.Error as exc:
            raise ValueError("履歴を確認できません:"
                             + _type_name(exc)) from exc
        out: list[dict] = []
        for row in rows:
            item = dict(row)
            try:
                created = float(item.get("created_at") or 0)
            except (TypeError, ValueError):
                created = 0.0
            out.append({
                "id": str(item.get("id") or ""),
                "record_id": str(item.get("record_id") or ""),
                "project_id": str(item.get("project_id") or ""),
                "label": str(item.get("label") or ""),
                "actor": str(item.get("actor") or ""),
                "note": str(item.get("note") or ""),
                "danger": int(item.get("danger") or 0),
                "created_at": created,
            })
        return out
    finally:
        db.close()


def label(manager, record_id: str, label_value: str, actor: str,
          note: str = "", danger: bool = False) -> dict:
    """人が正誤を付ける。履歴は追記のみ (上書きでなく追記。最新が有効)。"""
    import uuid as _uuid

    rid = str(record_id or "").strip()
    actor_name = _safe_text(actor, 100).strip()
    text = str(label_value or "").strip()
    note_text = _safe_text(note or "", 2000)
    danger_flag = bool(danger)
    if not rid:
        return {"ok": False, "reason": "record id is required",
                "error_type": "ValueError"}
    if not actor_name:
        return {"ok": False, "reason": "actor is required",
                "error_type": "ValueError"}
    if text not in LABELS:
        return {"ok": False, "reason": "label を確認できません",
                "error_type": "ValueError"}
    if danger_flag and text != "wrong":
        return {"ok": False,
                "reason": "danger は誤りのときだけ付けられます",
                "error_type": "ValueError"}
    path = shadow_db_path(manager)
    lock = _lock_for(path)
    with lock:
        with _connect(manager) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute("SELECT * FROM shadow_records WHERE id=?",
                                 (rid,)).fetchone()
            except sqlite3.Error as exc:
                return {"ok": False,
                        "reason": "影の記録を確認できません:" + _type_name(exc),
                        "error_type": _type_name(exc)}
            if row is None:
                return {"ok": False, "reason": "記録が見つかりません",
                        "error_type": "ValueError"}
            current = _decode_record(row)
            now = _utcnow()
            hid = "shl_" + _uuid.uuid4().hex[:16]
            try:
                db.execute(
                    """INSERT INTO shadow_label_history(id, record_id,
                           project_id, label, actor, note, danger, created_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (hid, rid, str(current.get("project_id") or ""), text,
                     actor_name, note_text, int(1 if danger_flag else 0),
                     now))
            except sqlite3.Error as exc:
                return {"ok": False,
                        "reason": "履歴の記録に失敗しました:" + _type_name(exc),
                        "error_type": _type_name(exc)}
            try:
                db.execute("UPDATE shadow_records SET label=?, label_by=?, "
                           "label_at=?, label_note=?, danger=? WHERE id=?",
                           (text, actor_name, now, note_text,
                            int(1 if danger_flag else 0), rid))
            except sqlite3.Error as exc:
                return {"ok": False,
                        "reason": "ラベルの記録に失敗しました:" + _type_name(exc),
                        "error_type": _type_name(exc)}
            db.commit()
            try:
                updated = db.execute(
                    "SELECT * FROM shadow_records WHERE id=?",
                    (rid,)).fetchone()
            except sqlite3.Error as exc:
                return {"ok": False,
                        "reason": "ラベルの確認に失敗しました:" + _type_name(exc),
                        "error_type": _type_name(exc)}
            if updated is None:
                return {"ok": False,
                        "reason": "ラベルの確認に失敗しました:ValueError",
                        "error_type": "ValueError"}
            saved = _decode_record(updated)
    saved["ok"] = True
    return {"ok": True, "record": saved, "record_id": rid}


def metrics(manager_or_path, project_id: str) -> dict:
    """集計。分母0のとき一致率は null (測定できない)。読取不能は例外。"""
    if FAIL_INJECT is not None and isinstance(FAIL_INJECT, dict):
        stage = str(FAIL_INJECT.get("stage") or "")
        if stage == "metrics_unreadable":
            raise ValueError("集計を確認できません:InjectedError")
    pid = str(project_id or "")
    if not pid:
        raise ValueError("project_id is required")
    db = _read_connection(manager_or_path)
    if db is None:
        # 記録が無い (DB自体が無い) 場合は0件の集計 (測定できない)。
        return {"project_id": pid, "count": 0, "judged": 0,
                "correct": 0, "wrong": 0, "unsure": 0, "unjudged": 0,
                "judged_for_gate": 0, "agreement": None,
                "dangerous": 0, "signatures": 0, "latest_at": 0.0}
    try:
        try:
            rows = db.execute(
                "SELECT label, danger, plan_signature, observed_at "
                "FROM shadow_records WHERE project_id=?", (pid,)).fetchall()
        except sqlite3.Error as exc:
            raise ValueError("集計を確認できません:"
                             + _type_name(exc)) from exc
        count = 0
        correct = 0
        wrong = 0
        unsure = 0
        unjudged = 0
        dangerous = 0
        sigs: set[str] = set()
        latest = 0.0
        for row in rows:
            try:
                item = dict(row)
            except (TypeError, ValueError) as exc:
                raise ValueError("集計を確認できません:"
                                 + _type_name(exc)) from exc
            if "label" not in item or "danger" not in item:
                raise ValueError("集計を確認できません:KeyError")
            if "plan_signature" not in item or "observed_at" not in item:
                raise ValueError("集計を確認できません:KeyError")
            lab = item.get("label")
            dgr = item.get("danger")
            sig = item.get("plan_signature")
            at = item.get("observed_at")
            if not isinstance(lab, str) or not isinstance(sig, str):
                raise ValueError("集計を確認できません:TypeError")
            if type(dgr) is not int:
                try:
                    dgr_int = int(dgr)
                except (TypeError, ValueError) as exc:
                    raise ValueError("集計を確認できません:TypeError") from exc
                if type(dgr) is bool:
                    raise ValueError("集計を確認できません:TypeError")
                dgr = dgr_int
            try:
                at_f = float(at or 0)
            except (TypeError, ValueError) as exc:
                raise ValueError("集計を確認できません:ValueError") from exc
            count += 1
            if lab == "correct":
                correct += 1
            elif lab == "wrong":
                wrong += 1
            elif lab == "unsure":
                unsure += 1
            elif lab == "":
                unjudged += 1
            else:
                raise ValueError("集計を確認できません:ValueError")
            if lab == "wrong" and int(dgr) == 1:
                dangerous += 1
            elif int(dgr) not in (0, 1):
                raise ValueError("集計を確認できません:ValueError")
            elif int(dgr) == 1 and lab != "wrong":
                raise ValueError("集計を確認できません:ValueError")
            # 関門の「異なる計画署名」は、人が判定した(正しい/誤り)記録の署名だけを数える。
            # 未判定・判断できないだけの署名で基準を満たさないようにする。
            if sig and lab in ("correct", "wrong"):
                sigs.add(sig)
            if at_f > latest:
                latest = at_f
        judged = int(correct) + int(wrong) + int(unsure)
        judged_gate = int(correct) + int(wrong)
        denom = int(correct) + int(wrong)
        if denom == 0:
            agreement = None
        else:
            agreement = float(correct) / float(denom)
        return {"project_id": pid, "count": int(count),
                "judged": int(judged), "correct": int(correct),
                "wrong": int(wrong), "unsure": int(unsure),
                "unjudged": int(unjudged),
                "judged_for_gate": int(judged_gate),
                "agreement": agreement, "dangerous": int(dangerous),
                "signatures": int(len(sigs)), "latest_at": float(latest)}
    finally:
        db.close()


def gate(manager_or_path, project_id: str) -> dict:
    """関門の判定。読取不能・集計不能は passed:false (理由=確認できず)。"""
    if FAIL_INJECT is not None and isinstance(FAIL_INJECT, dict):
        stage = str(FAIL_INJECT.get("stage") or "")
        if stage == "gate_unreadable":
            return {"passed": False, "reasons": ["確認できず:InjectedError"],
                    "metrics": {}, "thresholds": dict(THRESHOLDS)}
    pid = str(project_id or "")
    thresholds = dict(THRESHOLDS)
    try:
        data = metrics(manager_or_path, pid)
    except (ValueError, TypeError, sqlite3.Error, OSError,
            RuntimeError) as exc:
        return {"passed": False,
                "reasons": ["確認できず:" + _type_name(exc)],
                "metrics": {}, "thresholds": thresholds}
    if not isinstance(data, dict):
        return {"passed": False, "reasons": ["確認できず:TypeError"],
                "metrics": {}, "thresholds": thresholds}
    for key in ("correct", "wrong", "judged_for_gate", "dangerous",
                "signatures", "latest_at"):
        if key not in data:
            return {"passed": False, "reasons": ["確認できず:KeyError"],
                    "metrics": {}, "thresholds": thresholds}
    try:
        judged_gate = int(data.get("judged_for_gate") or 0)
        sig_count = int(data.get("signatures") or 0)
        dangerous = int(data.get("dangerous") or 0)
        latest = float(data.get("latest_at") or 0)
        agreement = data.get("agreement")
        if agreement is not None:
            agreement = float(agreement)
    except (TypeError, ValueError):
        return {"passed": False, "reasons": ["確認できず:ValueError"],
                "metrics": {}, "thresholds": thresholds}
    reasons: list[str] = []
    min_judged = int(thresholds.get("min_judged") or 0)
    if judged_gate < min_judged:
        reasons.append(
            "判定済み(正しい+誤り)が" + str(judged_gate) + "件で、基準"
            + str(min_judged) + "件に足りません")
    min_sig = int(thresholds.get("min_signatures") or 0)
    if sig_count < min_sig:
        reasons.append(
            "異なる計画署名が" + str(sig_count) + "つで、基準"
            + str(min_sig) + "つに足りません")
    min_agree = float(thresholds.get("min_agreement") or 0)
    if agreement is None:
        reasons.append("一致率を測定できません(判定済みがありません)")
    elif not 0.0 <= agreement <= 1.0:
        return {"passed": False, "reasons": ["確認できず:ValueError"],
                "metrics": {}, "thresholds": thresholds}
    elif agreement < min_agree:
        pct = round(agreement * 100.0, 1)
        reasons.append(
            "一致率が" + str(pct) + "%で、基準95%以上に足りません")
    max_danger = int(thresholds.get("max_dangerous") or 0)
    if dangerous > max_danger:
        reasons.append(
            "危険な誤提案が" + str(dangerous) + "件あります(基準0件)")
    max_age = int(thresholds.get("max_age_days") or 0)
    if not latest > 0:
        reasons.append("観測がありません")
    else:
        try:
            age_days = (_utcnow() - float(latest)) / 86400.0
        except (TypeError, ValueError):
            return {"passed": False, "reasons": ["確認できず:ValueError"],
                    "metrics": {}, "thresholds": thresholds}
        if age_days < 0:
            return {"passed": False, "reasons": ["確認できず:ValueError"],
                    "metrics": {}, "thresholds": thresholds}
        if age_days > float(max_age):
            reasons.append(
                "直近の観測が" + str(int(age_days)) + "日前で、基準"
                + str(max_age) + "日以内にありません")
    # passed を既定値で真にしない。理由が空のときだけ真。
    passed = not reasons
    return {"passed": bool(passed), "reasons": reasons,
            "metrics": data, "thresholds": thresholds}


def list_overrides(manager_or_path, project_id: str,
                   limit: int = 20) -> list[dict]:
    pid = str(project_id or "")
    try:
        count = max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        raise ValueError("limit が不正です")
    db = _read_connection(manager_or_path)
    if db is None:
        return []
    try:
        try:
            rows = db.execute(
                "SELECT * FROM shadow_overrides WHERE project_id=? "
                "ORDER BY created_at DESC LIMIT ?",
                (pid, count)).fetchall()
        except sqlite3.Error as exc:
            raise ValueError("記録を確認できません:"
                             + _type_name(exc)) from exc
        out: list[dict] = []
        for row in rows:
            item = dict(row)
            try:
                gate_summary = json.loads(str(item.get("gate_summary") or "{}"))
            except (ValueError, TypeError):
                gate_summary = {}
            if not isinstance(gate_summary, dict):
                gate_summary = {}
            try:
                created = float(item.get("created_at") or 0)
            except (TypeError, ValueError):
                created = 0.0
            out.append({
                "id": str(item.get("id") or ""),
                "project_id": str(item.get("project_id") or ""),
                "actor": str(item.get("actor") or ""),
                "reason": str(item.get("reason") or ""),
                "gate_summary": gate_summary,
                "created_at": created,
            })
        return out
    finally:
        db.close()


def record_override(manager, project_id: str, actor: str, reason: str,
                    gate_result: dict | None = None) -> dict:
    """関門の無視を shadow_overrides に追記し、監査に残す。"""
    import uuid as _uuid

    from app import autopilot as _auto

    pid = str(project_id or "")
    actor_name = _safe_text(actor, 100).strip()
    reason_text = _safe_text(reason, 2000).strip()
    if not pid:
        raise ValueError("project_id is required")
    if not actor_name:
        raise ValueError("actor is required")
    if len(reason_text) < 10:
        raise ValueError("理由は10字以上が必要です")
    summary: dict = {}
    if isinstance(gate_result, dict):
        reasons = gate_result.get("reasons")
        reasons = list(reasons) if isinstance(reasons, list) else []
        summary = {"passed": bool(gate_result.get("passed")),
                   "reasons": [_safe_text(item, 200) for item in reasons][:10]}
        raw_metrics = gate_result.get("metrics")
        if isinstance(raw_metrics, dict):
            for key in ("count", "judged_for_gate", "correct", "wrong",
                        "agreement", "dangerous", "signatures"):
                if key in raw_metrics:
                    summary[key] = raw_metrics.get(key)
    else:
        summary = {"passed": False, "reasons": ["確認できず"]}
    oid = "sho_" + _uuid.uuid4().hex[:16]
    now = _utcnow()
    path = shadow_db_path(manager)
    lock = _lock_for(path)
    with lock:
        with _connect(manager) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute(
                    """INSERT INTO shadow_overrides(id, project_id, actor,
                           reason, gate_summary, created_at)
                       VALUES(?,?,?,?,?,?)""",
                    (oid, pid, actor_name, reason_text,
                     json.dumps(summary, ensure_ascii=False, sort_keys=True),
                     now))
            except sqlite3.Error as exc:
                raise ValueError("無視の記録に失敗しました:"
                                 + _type_name(exc)) from exc
            db.commit()
    # 監査は autopilot の流儀 (_audit)。失敗は握りつぶさない。
    _auto._audit(manager, pid, "autopilot_enabled_without_gate",
                 {"actor": actor_name, "reason": reason_text,
                  "gate_passed": bool(summary.get("passed"))})
    return {"ok": True, "id": oid, "project_id": pid, "actor": actor_name,
            "reason": reason_text, "gate_summary": summary,
            "created_at": now}


def summary(manager) -> dict:
    """全PJの関門の状態を横断で。読取不能PJは「確認できず」。"""
    try:
        rows = manager.memory.list_projects()
    except (ValueError, TypeError, sqlite3.Error, OSError,
            AttributeError, RuntimeError) as exc:
        raise ValueError("PJ一覧を確認できません:"
                         + _type_name(exc)) from exc
    if not isinstance(rows, list):
        raise ValueError("PJ一覧の形式を確認できません")
    ids: list[str] = []
    names: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        pid = row.get("id")
        if pid is None or str(pid) == "":
            continue
        pid_text = str(pid)
        ids.append(pid_text)
        names[pid_text] = str(row.get("name") or "")
    ids.sort()
    items: list[dict] = []
    for pid in ids:
        try:
            if _check_deleted(manager, pid):
                items.append({"project_id": pid,
                              "project_name": names.get(pid, ""),
                              "readable": False, "reason": "対象外(削除状態)",
                              "passed": False,
                              "gate": {"passed": False,
                                       "reasons": ["対象外(削除状態)"]}})
                continue
            result = gate(manager, pid)
        except (ValueError, TypeError, sqlite3.Error, OSError,
                RuntimeError) as exc:
            items.append({"project_id": pid,
                          "project_name": names.get(pid, ""),
                          "readable": False,
                          "reason": "確認できず:" + _type_name(exc),
                          "passed": False,
                          "gate": {"passed": False,
                                   "reasons": ["確認できず:" + _type_name(exc)]}})
            continue
        if not isinstance(result, dict) or "passed" not in result:
            items.append({"project_id": pid,
                          "project_name": names.get(pid, ""),
                          "readable": False, "reason": "確認できず:TypeError",
                          "passed": False,
                          "gate": {"passed": False,
                                   "reasons": ["確認できず:TypeError"]}})
            continue
        items.append({"project_id": pid,
                      "project_name": names.get(pid, ""),
                      "readable": True, "reason": "",
                      "passed": bool(result.get("passed")),
                      "gate": result})
    passed_count = sum(1 for item in items if bool(item.get("passed")))
    unreadable = sum(1 for item in items if not bool(item.get("readable")))
    return {"projects": items, "total": int(len(items)),
            "passed": int(passed_count), "unreadable": int(unreadable)}


def reset_for_generation(memory_path: str | Path, project_id: str) -> dict:
    """PJ初期化 (世代切替) では影の記録は変えない。証拠として残す。"""
    return {"project_id": str(project_id or ""), "reset": False,
            "note": "影の記録は世代切替で変えません"}


def purge_project_shadow(memory_path: str | Path, project_id: str) -> dict:
    """完全削除時の後始末。当該PJの行だけ消し、他PJに影響しない。"""
    pid = str(project_id or "")
    path = shadow_db_path(memory_path)
    if not path.exists():
        return {"shadow_records": 0, "shadow_label_history": 0,
                "shadow_overrides": 0}
    removed = {"shadow_records": 0, "shadow_label_history": 0,
               "shadow_overrides": 0}
    db = sqlite3.connect(str(path), timeout=30)
    try:
        with db:
            try:
                names = {row[0] for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            except sqlite3.Error:
                raise
            mapping = (("shadow_records", "shadow_records"),
                       ("shadow_label_history", "shadow_label_history"),
                       ("shadow_overrides", "shadow_overrides"))
            for table, key in mapping:
                if table not in names:
                    continue
                try:
                    cur = db.execute(f"DELETE FROM {table} WHERE project_id=?",
                                     (pid,))
                    removed[key] = int(cur.rowcount or 0)
                except sqlite3.Error:
                    raise
            # 履歴の record_id が残る孤児は project_id でも消えるため追加不要。
            # ただし旧版で行が無い場合に備え、record_id 経由の掃除はしない
            # (他PJの記録に触れないため)。
    finally:
        db.close()
    return removed
