"""Stage3: 境界付き自走ランナー (一般機構)。

可逆なローカル作業だけを許可リスト方式で回し、同じ停止理由で空転しない。
既定は無効。有効化は人が決める。実際の自走を勝手に始めない
(定期実行のスレッド・スケジューラは作らない。人がAPIを呼んだときだけ動く)。

- ALLOWED_ACTIONS (コード内の1か所。これ以外は実行しない)
- autopilot_tick / run_until_stop (1ティック最大1アクション、最大10ティック)
- 空転ガードは段階1の is_repeating / guard_auto_tick を使い、外せない
- 外部送信の増分が0でないティックは失敗として記録し、以降の自走を止める
- safe_auto_resume の設定とは分離 (互いに有効化しない)
- 確認者名(actor)は作業記録用の表示名であり、本人認証ではない
  (認証は未実装のため、actor の一致で権限を付与しない)
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

HUMAN_STOPS = frozenset({
    "policy_block",
    "missing_decision",
    "missing_capability",
    "missing_evidence",
})

AUTO_STOPS = frozenset({
    "missing_binding",
    "missing_coverage",
})

MAX_TICKS = 10
TICK_HISTORY_LIMIT = 50

# 決定的な対応 (停止クラス -> 許可アクションを1つ)。1ティック1アクション。
ACTION_FOR_STOP = {
    "missing_binding": "remap_stored_review",
    # 4-B: missing_coverage は保存済み指摘からのローカル候補生成が決定的な一手。
    "missing_coverage": "generate_candidate",
}


def _utcnow() -> float:
    return time.time()


def _safe_text(value: object, limit: int = 500) -> str:
    """履歴・応答・ログ用の安全な短文。秘密混入の疑いは種別だけ残し値を落とす。"""
    blob = str(value or "")
    try:
        from app.plan_review_loop import contains_secret

        label = contains_secret(blob)
    except Exception:
        label = ""
    if label:
        return f"[秘密混入の疑いのため保持しません:{label}]"
    return " ".join(blob.split())[: max(1, int(limit))]


def _short_hash(value: object) -> str:
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        raw = str(value)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


# ----------------------------------------------------------------------------
# 許可アクションの実体 (すべて local_reversible、外部送信なし)
# ----------------------------------------------------------------------------

def _action_remap_stored_review(manager, project_id: str) -> dict:
    """保存済みレビューの再対応付け。外部再送0。読み取り中心の再照合。"""
    from app.goal_review import ReviewStore, plan_snapshot
    from app.plan_feedback import issues_for
    from app.plan_review_loop import build_issue_bindings, list_runs, remap_preview

    pid = str(project_id)
    _snapshot, signature = plan_snapshot(manager, pid)
    issues = issues_for(manager, pid, signature)
    # 保存済みランが human_required で止まっていればプレビューで再照合する。
    preview_info: dict = {}
    try:
        for run in list_runs(manager, pid):
            if not isinstance(run, dict):
                continue
            if (str(run.get("state") or "") == "stopped"
                    and str(run.get("stop_kind") or "") == "human_required"):
                try:
                    preview_info = remap_preview(manager, pid, str(run.get("id") or ""))
                    break
                except Exception:
                    continue
    except Exception:
        preview_info = {}
    bindings_built = 0
    try:
        store = ReviewStore(manager.memory.path)
        review = store.get(pid, "plan", signature) or {}
        parsed = []
        for response in review.get("reviews") or []:
            if not isinstance(response, dict):
                continue
            parsed.append({
                "provider": response.get("provider") or response.get("id") or "",
                "status": response.get("status") or "",
                "issues": response.get("issues") if isinstance(
                    response.get("issues"), list) else [],
            })
        if parsed:
            from app.goal_contract import preview as preview_contract

            contract = preview_contract(manager, pid) or {}
            table = build_issue_bindings(
                parsed, contract, _snapshot, plan_signature=signature,
                goal_contract_hash=str(contract.get("content_hash") or ""))
            bindings_built = len(table.get("bindings") or [])
    except Exception:
        bindings_built = 0
    return {
        "action": "remap_stored_review",
        "plan_signature": _safe_text(signature, 128),
        "issues": len(issues),
        "bindings_built": int(bindings_built),
        "remap_preview_bindings": len((preview_info or {}).get("bindings") or []),
        "external_calls": 0,
    }


class _NeedsHumanDecision(ValueError):
    """進めない条件(指摘なし・生成中・試行上限)。ティックは waiting_human。"""


class _CandidateNotAvailable(ValueError):
    """前提欠落・生成失敗。ティックは failed として理由つきで記録する。"""


def _run_propose_sync(propose_func, manager, project_id: str, signature: str) -> dict:
    """`plan_feedback.propose` (async) をティックから安全に呼ぶ。

    API の tick/run は `asyncio.to_thread` 経由の同期呼出しのため実行中の
    イベントループは無い。ただし他の同期呼出し元が既に動いているループの
    中にいる場合に備え、`asyncio.run` の二重起動はしない:
    実行中ループが無ければ `asyncio.run`、あれば別スレッドで実行して待つ。
    """
    pid = str(project_id)
    coro = propose_func(manager, pid, signature)
    if not hasattr(coro, "__await__"):
        return coro
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    outcome: dict = {}

    def _runner() -> None:
        try:
            outcome["result"] = asyncio.run(coro)
        except BaseException as exc:  # noqa: BLE001 - 呼び出し側で理由つき記録する
            outcome["error"] = exc

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_runner)
        future.result(timeout=210)
    if "error" in outcome:
        raise outcome["error"]
    if "result" not in outcome:
        raise _CandidateNotAvailable("修正案の生成結果を確認できませんでした")
    return outcome["result"]


def _action_generate_candidate(manager, project_id: str) -> dict:
    """保存済み指摘からのローカル修正候補の生成。

    4-B: `plan_feedback.propose` をそのまま呼ぶ実体化。冪等キー・試行上限3回・
    指摘一致の検査・被覆行列・`validate_candidate` は propose 側の既存機構に任せ、
    独自の生成経路は作らない。`apply` は絶対に呼ばない(適用は人の確定操作のみ)。
    記録には候補IDと被覆の要約だけを残し、本文全文・指摘全文は入れない。
    失敗(未起動・タイムアウト・不正JSON・検査不合格)は例外で返し、
    呼び出し側がティックを失敗として理由つきで記録する。
    """
    from app.core import Ollama
    from app.goal_review import plan_snapshot
    from app.plan_feedback import issues_for, propose

    pid = str(project_id)
    llm = getattr(manager, "llm", None)
    if not isinstance(llm, Ollama):
        raise _CandidateNotAvailable(
            "ローカルAI(Ollama)が利用できないため候補を生成できません")
    _snapshot, signature = plan_snapshot(manager, pid)
    try:
        issues = issues_for(manager, pid, signature)
    except Exception as exc:
        raise _CandidateNotAvailable(
            f"保存済みの指摘を確認できません: {type(exc).__name__}") from exc
    if not issues:
        # 保存済みの指摘なし: 生成を試みたふりをせず人の判断に回す。
        raise _NeedsHumanDecision(
            "保存済みの指摘がありません。人の判断が必要です")
    if pid in getattr(manager, "planning_projects", set()):
        # propose 自体は「計画を生成中です」で拒否する。ここでは先に進めず記録する。
        raise _NeedsHumanDecision("計画を生成中のため進めません")
    try:
        from app.goal_review import ReviewStore

        previous = ReviewStore(manager.memory.path).get(pid, "revision", signature) or {}
        attempts = int(previous.get("attempts") or 0)
        prev_status = str(previous.get("status") or "")
    except Exception:
        attempts = 0
        prev_status = ""
    # 試行上限(3回): propose 側の検査と同じく3回で止める。error 状態からの
    # 正規の再生成だけは propose に判断を任せる(4回目の LLM 呼出はしない)。
    if attempts >= 3 and prev_status != "error":
        raise _NeedsHumanDecision(
            "試行が上限(3回)に達したため進めません。人の判断が必要です")
    try:
        row = _run_propose_sync(propose, manager, pid, signature)
    except (_NeedsHumanDecision, _CandidateNotAvailable):
        raise
    except ValueError as exc:
        if "3回" in str(exc):
            raise _NeedsHumanDecision(
                "試行が上限(3回)に達したため進めません。人の判断が必要です") from exc
        raise _CandidateNotAvailable(
            f"修正案を生成できません: {type(exc).__name__}:{str(exc)[:300]}") from exc
    except Exception as exc:
        raise _CandidateNotAvailable(
            f"修正案を生成できません: {type(exc).__name__}:{str(exc)[:300]}") from exc
    if not isinstance(row, dict) or not row.get("candidate_id"):
        raise _CandidateNotAvailable("修正案の候補IDを確認できませんでした")
    matrix = row.get("coverage_matrix") if isinstance(row.get("coverage_matrix"), dict) else {}
    rows = matrix.get("rows") if isinstance(matrix, dict) else []
    rows = rows if isinstance(rows, list) else []
    covered = sum(1 for r in rows if isinstance(r, dict)
                  and r.get("coverage") == "covered_candidate")
    uncovered_reasons: list[str] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        if item.get("coverage") == "covered_candidate":
            continue
        label = _safe_text(
            f"{item.get('issue_id') or '?'}({item.get('coverage') or '?'}: "
            f"{item.get('reason') or ''})", 120)
        if label and label not in uncovered_reasons:
            uncovered_reasons.append(label)
        if len(uncovered_reasons) >= 3:
            break
    blockers = row.get("blockers") if isinstance(row.get("blockers"), list) else []
    return {
        "action": "generate_candidate",
        "plan_signature": _safe_text(signature, 128),
        "candidate_id": _safe_text(str(row.get("candidate_id") or ""), 64),
        "coverage_rows": len(rows),
        "covered_candidate": int(covered),
        "uncovered_reasons": uncovered_reasons,
        "blockers": len(blockers),
        "status": _safe_text(str(row.get("status") or ""), 32),
        "replayed": bool(row.get("replay_count")),
        "attempts": int(row.get("attempts") or 0),
        "external_calls": 0,
        "note": "候補は生成のみ。適用は人の確定操作の中だけ",
    }


def _action_refresh_coverage(manager, project_id: str) -> dict:
    """被覆行列の更新・照会。読み取り中心。外部呼出なし。"""
    from app.goal_review import ReviewStore, plan_snapshot
    from app.plan_coverage_matrix import build_coverage_matrix
    from app.plan_feedback import issues_for

    pid = str(project_id)
    _snapshot, signature = plan_snapshot(manager, pid)
    issues = issues_for(manager, pid, signature)
    store = ReviewStore(manager.memory.path)
    revision = store.get(pid, "revision", signature) or {}
    actions = revision.get("actions") or []
    changes = revision.get("changes") or []
    try:
        order = [str(t.get("task_key") or "") for t in (_snapshot.get("tasks") or [])
                 if isinstance(t, dict)]
    except Exception:
        order = []
    try:
        matrix = build_coverage_matrix(issues=issues, actions=actions,
                                       changes=changes, task_order=order)
    except Exception as exc:
        matrix = {"version": "coverage-matrix-v1", "rows": [],
                  "error": type(exc).__name__}
    rows = matrix.get("rows") if isinstance(matrix, dict) else []
    uncovered = [r for r in rows if isinstance(r, dict)
                 and r.get("coverage") != "covered_candidate"]
    return {
        "action": "refresh_coverage",
        "plan_signature": _safe_text(signature, 128),
        "rows": len(rows),
        "uncovered": len(uncovered),
        "external_calls": 0,
    }


def _action_capability_preflight(manager, project_id: str) -> dict:
    """事前照合の再実行と提案の生成。冪等。外部呼出なし。"""
    from app import capability_gap as gap

    pid = str(project_id)
    preflight = gap.capability_preflight(manager, pid)
    generated: dict = {}
    try:
        generated = gap.generate_proposals(manager, pid, preflight, actor="autopilot")
    except Exception as exc:
        generated = {"proposals": [], "error": type(exc).__name__}
    proposals = generated.get("proposals") if isinstance(generated, dict) else []
    return {
        "action": "capability_preflight",
        "plan_signature": _safe_text(str(preflight.get("plan_signature") or ""), 128),
        "gap_codes": list(preflight.get("gap_codes") or []),
        "proposals": len(proposals) if isinstance(proposals, list) else 0,
        "external_calls": 0,
    }


def _action_completion_replay_read(manager, project_id: str) -> dict:
    """完了判定の読み取り再生。読み取り専用。副作用なし。"""
    from app.completion_replay import preview

    pid = str(project_id)
    view = preview(manager, pid)
    gate = view.get("completion_gate") if isinstance(view, dict) else {}
    return {
        "action": "completion_replay_read",
        "plan_signature": _safe_text(str(view.get("plan_signature") or ""), 128),
        "achieved": bool((gate or {}).get("achieved")),
        "reason_code": _safe_text(str((gate or {}).get("reason_code") or ""), 120),
        "read_only": True,
        "external_calls": 0,
    }


def _action_record_stop_class(manager, project_id: str,
                              classification: dict | None = None) -> dict:
    """停止クラス・次操作・証拠参照の記録。何も進めない。外部呼出なし。"""
    classification = classification if isinstance(classification, dict) else {}
    refs = classification.get("evidence_refs") if isinstance(
        classification.get("evidence_refs"), dict) else {}
    return {
        "action": "record_stop_class",
        "stop_class": str(classification.get("stop_class") or ""),
        "stop_code": str(classification.get("stop_code") or ""),
        "next_action": str(classification.get("next_action") or "none"),
        "reason_ja": _safe_text(str(classification.get("reason_ja") or ""), 500),
        "evidence_refs": {k: _safe_text(v, 128) for k, v in refs.items()},
        "external_calls": 0,
    }


# 許可リスト (コード内の1か所。これ以外は実行しない)。
# 各許可アクションは permission_class=local_reversible を宣言する。
ALLOWED_ACTIONS: dict[str, dict] = {
    "remap_stored_review": {
        "permission_class": "local_reversible",
        "description": "保存済みレビューの再対応付け。外部再送0",
        "func": _action_remap_stored_review,
    },
    "generate_candidate": {
        "permission_class": "local_reversible",
        "description": "ローカルの修正候補の生成。冪等",
        "func": _action_generate_candidate,
    },
    "refresh_coverage": {
        "permission_class": "local_reversible",
        "description": "被覆行列の更新・照会",
        "func": _action_refresh_coverage,
    },
    "capability_preflight": {
        "permission_class": "local_reversible",
        "description": "事前照合の再実行と提案の生成。冪等",
        "func": _action_capability_preflight,
    },
    "completion_replay_read": {
        "permission_class": "local_reversible",
        "description": "完了判定の読み取り再生",
        "func": _action_completion_replay_read,
    },
    "record_stop_class": {
        "permission_class": "local_reversible",
        "description": "停止クラス・次操作・証拠参照の記録",
        "func": _action_record_stop_class,
    },
}

# 不許可の代表例 (テストで全て拒否されること)。コードでは許可リスト外は一律拒否。
DENIED_ACTION_NAMES = (
    "approve_plan",
    "apply",
    "apply_candidate",
    "send_packet",
    "resend_external",
    "publish_site",
    "post_sns",
    "contact_customer",
    "contact_lead",
    "register_rag",
    "apply_ocr",
    "purge_project",
    "export_package",
    "implement_proposal",
)


def _resolve_mem_path(manager_or_path) -> Path:
    if hasattr(manager_or_path, "memory"):
        return Path(manager_or_path.memory.path)
    return Path(manager_or_path)


def autopilot_db_path(manager_or_path) -> Path:
    mem = _resolve_mem_path(manager_or_path)
    return mem.with_name(mem.name + ".autopilot.sqlite3")


_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(project_id: str) -> threading.Lock:
    key = str(project_id or "")
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _locks[key] = lock
        return lock


def _ensure_tables(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS autopilot_config(
        project_id TEXT PRIMARY KEY,
        enabled INTEGER NOT NULL DEFAULT 0,
        updated REAL NOT NULL,
        actor TEXT NOT NULL DEFAULT '',
        reason TEXT NOT NULL DEFAULT '')""")
    db.execute("""CREATE TABLE IF NOT EXISTS autopilot_ticks(
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        tick_index INTEGER NOT NULL,
        plan_signature TEXT NOT NULL DEFAULT '',
        stop_class TEXT NOT NULL DEFAULT '',
        stop_code TEXT NOT NULL DEFAULT '',
        action_name TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT '',
        next_action TEXT NOT NULL DEFAULT '',
        reason_ja TEXT NOT NULL DEFAULT '',
        evidence_refs TEXT NOT NULL DEFAULT '{}',
        external_delta INTEGER NOT NULL DEFAULT 0,
        external_rounds_delta INTEGER NOT NULL DEFAULT 0,
        budget_kept INTEGER NOT NULL DEFAULT 1,
        dry_run INTEGER NOT NULL DEFAULT 0,
        actor TEXT NOT NULL DEFAULT '',
        created REAL NOT NULL)""")
    db.execute("""CREATE INDEX IF NOT EXISTS idx_autopilot_ticks_project
        ON autopilot_ticks(project_id, tick_index)""")


@contextmanager
def _connect(manager_or_path):
    path = autopilot_db_path(manager_or_path)
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
    path = autopilot_db_path(manager_or_path)
    if not path.exists():
        return None
    db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=15)
    db.row_factory = sqlite3.Row
    return db


def _decode_config(row: sqlite3.Row) -> dict:
    return {
        "project_id": str(row["project_id"]),
        "enabled": bool(row["enabled"]),
        "updated": float(row["updated"] or 0),
        "actor": str(row["actor"] or ""),
        "reason": str(row["reason"] or ""),
    }


def _decode_tick(row: sqlite3.Row) -> dict:
    item = dict(row)
    try:
        item["evidence_refs"] = json.loads(item.get("evidence_refs") or "{}")
    except (ValueError, TypeError):
        item["evidence_refs"] = {}
    item["enabled_snapshot"] = None
    return {
        "id": str(item.get("id") or ""),
        "project_id": str(item.get("project_id") or ""),
        "tick_index": int(item.get("tick_index") or 0),
        "plan_signature": str(item.get("plan_signature") or ""),
        "stop_class": str(item.get("stop_class") or ""),
        "stop_code": str(item.get("stop_code") or ""),
        "action_name": str(item.get("action_name") or ""),
        "status": str(item.get("status") or ""),
        "next_action": str(item.get("next_action") or ""),
        "reason_ja": str(item.get("reason_ja") or ""),
        "evidence_refs": item.get("evidence_refs") if isinstance(
            item.get("evidence_refs"), dict) else {},
        "external_delta": int(item.get("external_delta") or 0),
        "external_rounds_delta": int(item.get("external_rounds_delta") or 0),
        "budget_kept": bool(item.get("budget_kept")),
        "dry_run": bool(item.get("dry_run")),
        "actor": str(item.get("actor") or ""),
        "created": float(item.get("created") or 0),
    }


def _check_deleted(manager, project_id: str) -> None:
    from app.project_delete import is_deleted_or_unknown

    try:
        blocked = is_deleted_or_unknown(manager.memory.path, str(project_id))
    except Exception:
        raise ValueError("削除状態を確認できないPJには動作しません(状態を確定できません)")
    if blocked:
        raise ValueError("論理削除中または削除状態を確認できないPJには動作しません(状態を確定できません)")


def get_config(manager, project_id: str) -> dict:
    pid = str(project_id or "")
    db = _read_connection(manager)
    if db is None:
        return {"project_id": pid, "enabled": False, "updated": 0,
                "actor": "", "reason": ""}
    try:
        with db:
            try:
                row = db.execute("SELECT * FROM autopilot_config WHERE project_id=?",
                                 (pid,)).fetchone()
            except sqlite3.OperationalError:
                return {"project_id": pid, "enabled": False, "updated": 0,
                        "actor": "", "reason": ""}
    finally:
        db.close()
    if row is None:
        return {"project_id": pid, "enabled": False, "updated": 0,
                "actor": "", "reason": ""}
    return _decode_config(row)


def is_enabled(manager, project_id: str) -> bool:
    return bool(get_config(manager, project_id).get("enabled"))


def _audit(manager, project_id: str, kind: str, detail: dict) -> None:
    try:
        safe = {k: _safe_text(v, 200) for k, v in dict(detail or {}).items()}
        manager.memory.add_event(str(project_id), kind,
                                 _safe_text(f"autopilot:{kind}", 200),
                                 detail=json.dumps(safe, ensure_ascii=False))
    except Exception:
        pass


def set_enabled(manager, project_id: str, value: bool, *,
                actor: str, reason: str) -> dict:
    """有効化/無効化。担当者(actor)と理由を必須とし監査に残す。

    safe_auto_resume の設定とは分離する (互いに有効化しない)。
    論理削除中または削除状態を確認できないPJでは動作しない。
    """
    _check_deleted(manager, project_id)
    pid = str(project_id or "")
    # 確認者名は本人認証ではない (認証は未実装)。作業記録用の表示名である。
    actor_name = _safe_text(actor, 100).strip()
    reason_text = _safe_text(reason, 500).strip()
    if not actor_name:
        raise ValueError("actor is required")
    if not reason_text:
        raise ValueError("reason is required")
    now = _utcnow()
    with _connect(manager) as db:
        with db:
            db.execute(
                """INSERT INTO autopilot_config(project_id, enabled, updated,
                       actor, reason) VALUES(?,?,?,?,?)
                   ON CONFLICT(project_id) DO UPDATE SET enabled=excluded.enabled,
                   updated=excluded.updated, actor=excluded.actor,
                   reason=excluded.reason""",
                (pid, int(bool(value)), now, actor_name, reason_text))
    _audit(manager, pid,
           "autopilot_enabled" if value else "autopilot_disabled",
           {"actor": actor_name, "reason": reason_text,
            "enabled": bool(value)})
    return {"project_id": pid, "enabled": bool(value), "actor": actor_name,
            "reason": reason_text, "updated": now}


def _disable_internal(manager, project_id: str, *, reason: str) -> dict:
    """ガード・失敗による自動停止 (人の有効化とは別経路で無効に戻す)。"""
    pid = str(project_id or "")
    now = _utcnow()
    text = _safe_text(reason, 500)
    with _connect(manager) as db:
        with db:
            db.execute(
                """INSERT INTO autopilot_config(project_id, enabled, updated,
                       actor, reason) VALUES(?,?,?,?,?)
                   ON CONFLICT(project_id) DO UPDATE SET enabled=0,
                   updated=excluded.updated, actor=excluded.actor,
                   reason=excluded.reason""",
                (pid, 0, now, "auto-guard", text))
    _audit(manager, pid, "autopilot_auto_stopped", {"reason": text})
    return {"project_id": pid, "enabled": False, "actor": "auto-guard",
            "reason": text, "updated": now}


def reset_for_generation(memory_path: str | Path, project_id: str) -> dict:
    """PJ初期化(世代切替)で自走は無効に戻る。行は残し enabled=0 にする。"""
    pid = str(project_id or "")
    path = autopilot_db_path(memory_path)
    if not path.exists():
        return {"project_id": pid, "enabled": False, "reset": False}
    now = _utcnow()
    db = sqlite3.connect(str(path), timeout=30)
    try:
        with db:
            try:
                db.execute(
                    """CREATE TABLE IF NOT EXISTS autopilot_config(
                        project_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0,
                        updated REAL NOT NULL, actor TEXT NOT NULL DEFAULT '',
                        reason TEXT NOT NULL DEFAULT '')""")
                db.execute(
                    """INSERT INTO autopilot_config(project_id, enabled, updated,
                           actor, reason) VALUES(?,?,?,?,?)
                       ON CONFLICT(project_id) DO UPDATE SET enabled=0,
                       updated=excluded.updated, actor=excluded.actor,
                       reason=excluded.reason""",
                    (pid, 0, now, "auto-guard", "PJ初期化のため無効に戻しました"))
            except sqlite3.Error:
                raise
    finally:
        db.close()
    return {"project_id": pid, "enabled": False, "reset": True}


def purge_project_autopilot(memory_path: str | Path, project_id: str) -> dict:
    """完全削除時の後始末。当該PJの行だけ消し、他PJに影響しない。"""
    pid = str(project_id or "")
    path = autopilot_db_path(memory_path)
    if not path.exists():
        return {"autopilot_config": 0, "autopilot_ticks": 0}
    removed = {"autopilot_config": 0, "autopilot_ticks": 0}
    db = sqlite3.connect(str(path), timeout=30)
    try:
        with db:
            for table in ("autopilot_config", "autopilot_ticks"):
                try:
                    cur = db.execute(f"DELETE FROM {table} WHERE project_id=?",
                                     (pid,))
                    removed[table] = int(cur.rowcount or 0)
                except sqlite3.OperationalError:
                    pass
    finally:
        db.close()
    return removed


def list_ticks(manager, project_id: str, limit: int = 20) -> list[dict]:
    pid = str(project_id or "")
    db = _read_connection(manager)
    if db is None:
        return []
    try:
        with db:
            try:
                rows = db.execute(
                    "SELECT * FROM autopilot_ticks WHERE project_id=? "
                    "ORDER BY tick_index DESC LIMIT ?",
                    (pid, max(1, int(limit or 20)))).fetchall()
            except sqlite3.OperationalError:
                return []
    finally:
        db.close()
    out = [_decode_tick(r) for r in rows]
    out.reverse()
    return out


def _classify_current(manager, project_id: str) -> tuple[dict, dict]:
    """現行の停止分類 (読み取り専用)。既存の build_readiness 経由で材料を作る。"""
    from app.stop_classifier import classify_stop
    from app.workflow_readiness import build_readiness, build_stop_class

    pid = str(project_id or "")
    try:
        built = build_readiness(manager, pid)
    except Exception as exc:
        record = classify_stop({})
        return {}, record
    try:
        record = build_stop_class(manager, pid, built)
    except Exception:
        record = classify_stop({})
    if not isinstance(record, dict) or not record.get("stop_class"):
        record = classify_stop({})
    return built if isinstance(built, dict) else {}, record


def _count_review_rounds(manager, project_id: str) -> int | None:
    """保存済みの外部評価レビュー総数 (適用・再送で増えていないことの実測用)。"""
    try:
        from app.goal_review import ReviewStore

        store = ReviewStore(manager.memory.path)
        total = 0
        for _sig, payload in store.list(str(project_id), "plan"):
            if isinstance(payload, dict):
                total += len(payload.get("reviews") or [])
        return int(total)
    except Exception:
        return None


def _budget_snapshot(manager, project_id: str) -> dict:
    try:
        from app.goal_review import review_budget

        snap = review_budget(manager, str(project_id))
        return dict(snap) if isinstance(snap, dict) else {}
    except Exception:
        return {}


def _budget_unchanged(before: dict, after: dict) -> bool:
    try:
        if not isinstance(before, dict) or not isinstance(after, dict):
            return before == after
        if bool(before.get("managed")) != bool(after.get("managed")):
            return False
        if not before.get("managed"):
            return True
        if str(before.get("period_utc") or "") != str(after.get("period_utc") or ""):
            return True
        return (int(before.get("used_calls") or 0) == int(after.get("used_calls") or 0)
                and int(before.get("remaining_calls") or 0)
                == int(after.get("remaining_calls") or 0))
    except Exception:
        return False


@contextmanager
def _count_external_calls(manager):
    """実行前後で外部呼出が増えていないことを実測するための計数器。

    manager.plan_review_runner / 外部AI / ローカルLLM の呼出を数える。
    実行前後で復元するため、呼出自体は委譲する (止めない)。増分は呼び出し側で判定する。
    """
    counts = {"runner": 0, "provider": 0, "local": 0}
    originals: dict = {}

    def _wrap(name: str, target: object, attr: str) -> None:
        originals[(name, attr)] = target
        if not callable(target):
            return

        def _sync_wrapper(*args, **kwargs):
            counts[name] += 1
            return target(*args, **kwargs)

        async def _async_wrapper(*args, **kwargs):
            counts[name] += 1
            result = target(*args, **kwargs)
            if hasattr(result, "__await__"):
                return await result
            return result

        import asyncio as _asyncio_mod

        if _asyncio_mod.iscoroutinefunction(target):
            holder[name] = _async_wrapper
        else:
            holder[name] = _sync_wrapper

    holder: dict = {}
    manager_runner = getattr(manager, "plan_review_runner", None)
    if callable(manager_runner):
        _wrap("runner", manager_runner, "plan_review_runner")
        if "runner" in holder:
            manager.plan_review_runner = holder["runner"]
    manager_local = getattr(manager, "_local_complete", None)
    if callable(manager_local):
        _wrap("local", manager_local, "_local_complete")
        if "local" in holder:
            manager._local_complete = holder["local"]
    provider_module = None
    provider_original = None
    try:
        from app import external_ai as _ext

        provider_module = _ext
        provider_original = _ext.call_provider_with_metadata
        if callable(provider_original):
            _wrap("provider", provider_original, "call_provider_with_metadata")
            if "provider" in holder:
                _ext.call_provider_with_metadata = holder["provider"]
    except Exception:
        provider_module = None
    # ローカルAI(Ollama)は外部ではない。`_local_complete` の呼出は外部増分に
    # 数えず、監査用に別枠で記録する(4-B: ローカル生成の実測のため)。
    try:
        yield counts
    finally:
        try:
            counts["local_external_excluded"] = int(counts.get("local") or 0)
        except Exception:
            pass
        try:
            counts["local"] = 0
        except Exception:
            pass
        try:
            if callable(originals.get(("runner", "plan_review_runner"))):
                manager.plan_review_runner = originals[("runner",
                                                         "plan_review_runner")]
        except Exception:
            pass
        try:
            if callable(originals.get(("local", "_local_complete"))):
                manager._local_complete = originals[("local", "_local_complete")]
        except Exception:
            pass
        try:
            if provider_module is not None and callable(provider_original):
                provider_module.call_provider_with_metadata = provider_original
        except Exception:
            pass


def _snapshot_external(manager, project_id: str) -> dict:
    try:
        from app.stop_classifier import _EXTERNAL_SENDS_COUNTER as _counter

        sends = int(_counter.get("calls") or 0)
    except Exception:
        sends = 0
    return {
        "sends": sends,
        "rounds": _count_review_rounds(manager, project_id),
        "budget": _budget_snapshot(manager, project_id),
    }


def choose_action(stop_class: str, stop_code: str = "") -> str:
    """停止クラスから許可アクションを決定的に1つ選ぶ。"""
    name = ACTION_FOR_STOP.get(str(stop_class or ""), "")
    if name and name in ALLOWED_ACTIONS:
        return name
    # 可逆作業の既定は被覆の再照会 (決定的)。
    if str(stop_class or "") in AUTO_STOPS:
        return "refresh_coverage"
    return "record_stop_class"


def run_named_action(manager, project_id: str, action_name: str, *,
                     actor: str = "", classification: dict | None = None) -> dict:
    """許可リストの名前だけ実行する。リスト外は拒否し監査に残す。"""
    _check_deleted(manager, project_id)
    pid = str(project_id or "")
    name = str(action_name or "").strip()
    if name not in ALLOWED_ACTIONS:
        _audit(manager, pid, "autopilot_action_rejected",
               {"action": _safe_text(name, 120), "actor": _safe_text(actor, 100)})
        raise ValueError(f"許可されていないアクションのため拒否しました: {name}")
    entry = ALLOWED_ACTIONS[name]
    if entry.get("permission_class") != "local_reversible":
        raise ValueError("local_reversible 以外のアクションは実行できません")
    func = entry.get("func")
    if not callable(func):
        raise ValueError("アクションの実体がありません")
    if name == "record_stop_class":
        return func(manager, pid, classification)
    return func(manager, pid)


def _insert_tick(manager, project_id: str, row: dict) -> dict:
    pid = str(project_id or "")
    rid = uuid.uuid4().hex
    now = _utcnow()
    with _connect(manager) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            cur = db.execute("SELECT MAX(tick_index) FROM autopilot_ticks "
                             "WHERE project_id=?", (pid,))
            current = cur.fetchone()
            try:
                nxt = int((current[0] if current else 0) or 0) + 1
            except (TypeError, ValueError):
                nxt = 1
        except sqlite3.OperationalError:
            nxt = 1
        db.execute(
            """INSERT INTO autopilot_ticks(id, project_id, tick_index,
                   plan_signature, stop_class, stop_code, action_name, status,
                   next_action, reason_ja, evidence_refs, external_delta,
                   external_rounds_delta, budget_kept, dry_run, actor, created)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (rid, pid, nxt,
             str(row.get("plan_signature") or "")[:128],
             str(row.get("stop_class") or "")[:64],
             str(row.get("stop_code") or "")[:128],
             str(row.get("action_name") or "")[:64],
             str(row.get("status") or "")[:64],
             str(row.get("next_action") or "")[:64],
             _safe_text(row.get("reason_ja") or "", 1000),
             json.dumps(row.get("evidence_refs") if isinstance(
                 row.get("evidence_refs"), dict) else {}, ensure_ascii=False),
             int(row.get("external_delta") or 0),
             int(row.get("external_rounds_delta") or 0),
             int(1 if row.get("budget_kept") else 0),
             int(1 if row.get("dry_run") else 0),
             _safe_text(row.get("actor") or "", 100)[:100],
             now))
        db.commit()
    saved = list_ticks(manager, pid, limit=TICK_HISTORY_LIMIT + 10)
    for item in saved:
        if item.get("id") == rid:
            return item
    return {
        "id": rid, "project_id": pid, "tick_index": nxt,
        "plan_signature": str(row.get("plan_signature") or ""),
        "stop_class": str(row.get("stop_class") or ""),
        "stop_code": str(row.get("stop_code") or ""),
        "action_name": str(row.get("action_name") or ""),
        "status": str(row.get("status") or ""),
        "next_action": str(row.get("next_action") or ""),
        "reason_ja": _safe_text(row.get("reason_ja") or "", 1000),
        "evidence_refs": row.get("evidence_refs") if isinstance(
            row.get("evidence_refs"), dict) else {},
        "external_delta": int(row.get("external_delta") or 0),
        "external_rounds_delta": int(row.get("external_rounds_delta") or 0),
        "budget_kept": bool(row.get("budget_kept")),
        "dry_run": bool(row.get("dry_run")),
        "actor": _safe_text(row.get("actor") or "", 100)[:100],
        "created": now,
    }


def autopilot_tick(manager, project_id: str, *, actor: str,
                   dry_run: bool = False) -> dict:
    """1ティック (最大1アクション)。人がAPIを呼んだときだけ動く。

    dry_run=True は何も書かず、何を実行する予定かだけ返す。
    """
    from app.stop_classifier import (
        append_history,
        flatten_for_history,
        guard_auto_tick,
        is_repeating,
        read_history,
    )

    _check_deleted(manager, project_id)
    pid = str(project_id or "")
    # 確認者名は本人認証ではない (認証は未実装)。作業記録用の表示名である。
    actor_name = _safe_text(actor, 100).strip()
    if not actor_name:
        raise ValueError("actor is required")
    lock = _lock_for(pid)
    if not lock.acquire(blocking=False):
        return {"project_id": pid, "status": "busy",
                "reason": "同じPJのティックを実行中です", "actor": actor_name,
                "dry_run": bool(dry_run)}
    try:
        config = get_config(manager, pid)
        if not config.get("enabled"):
            return {"project_id": pid, "status": "disabled",
                    "reason": "自走は無効です", "actor": actor_name,
                    "enabled": False, "dry_run": bool(dry_run)}
        _built, record = _classify_current(manager, pid)
        if not isinstance(record, dict):
            record = {"stop_class": "missing_decision",
                      "stop_code": "unknown:empty",
                      "evidence_refs": {}, "next_action": "none",
                      "reason_ja": "分類不能のため人の判断が必要です。",
                      "external_sends": 0}
        stop_class = str(record.get("stop_class") or "missing_decision")
        stop_code = str(record.get("stop_code") or "unknown:empty")
        refs = record.get("evidence_refs") if isinstance(
            record.get("evidence_refs"), dict) else {}
        plan_sig = str(record.get("plan_signature") or refs.get("plan_signature")
                        or "")[:128]
        next_action = str(record.get("next_action") or "none")
        reason_ja = _safe_text(str(record.get("reason_ja") or ""), 1000)

        if bool(dry_run):
            if stop_class in HUMAN_STOPS:
                planned = "record_stop_class"
            else:
                planned = choose_action(stop_class, stop_code)
            return {"project_id": pid, "status": "dry_run",
                    "stop_class": stop_class, "stop_code": stop_code,
                    "plan_signature": plan_sig, "next_action": next_action,
                    "reason_ja": reason_ja,
                    "evidence_refs": {k: _safe_text(v, 128)
                                      for k, v in refs.items()},
                    "planned_action": planned,
                    "external_delta": 0, "external_rounds_delta": 0,
                    "budget_kept": True, "actor": actor_name,
                    "dry_run": True}

        # 空転ガード (外せない)。履歴はティックでのみ追記する。
        flat = flatten_for_history(record)
        if not flat.get("plan_signature"):
            flat["plan_signature"] = plan_sig
        history = read_history(manager, pid, limit=20)
        # 人の選択があれば解除する (guard_auto_tick の流儀を踏襲)。
        human_touched = False
        try:
            from app.plan_review_loop import list_runs as _list_runs

            for run in _list_runs(manager, pid):
                if not isinstance(run, dict):
                    continue
                if str(run.get("plan_signature") or "") != flat.get(
                        "plan_signature"):
                    continue
                if run.get("manual_bindings") or run.get("remap_history"):
                    human_touched = True
                    break
        except Exception:
            human_touched = False
        repeating = bool(is_repeating(history, flat)) and not human_touched
        # 監査用の guard 判定も残す (無効化はできない)。
        try:
            _guard = guard_auto_tick(manager, pid,
                                     {"plan_signature": flat.get("plan_signature"),
                                      "manual_bindings": ([{"manual": True}]
                                                          if human_touched else [])},
                                     record)
            if not human_touched:
                repeating = repeating or bool(_guard.get("repeating"))
        except Exception:
            pass

        if repeating:
            tick = _insert_tick(manager, pid, {
                "plan_signature": flat.get("plan_signature") or "",
                "stop_class": stop_class, "stop_code": stop_code,
                "action_name": "record_stop_class", "status": "stopped_guard",
                "next_action": next_action, "reason_ja": "同じ停止の繰り返しのため自動ランを停止しました",
                "evidence_refs": {k: _safe_text(v, 128) for k, v in refs.items()},
                "external_delta": 0, "external_rounds_delta": 0,
                "budget_kept": True, "dry_run": False, "actor": actor_name})
            try:
                append_history(manager, pid, flat, actor="auto-tick",
                               idempotency_key=f"autopilot:{tick.get('id')}")
            except Exception:
                pass
            _disable_internal(manager, pid,
                              reason="同じ停止の繰り返しのため自走を止めました")
            _audit(manager, pid, "autopilot_guard_stopped",
                   {"stop_class": stop_class, "stop_code": stop_code})
            tick = dict(tick)
            tick["enabled"] = False
            return tick

        if stop_class in HUMAN_STOPS:
            # 人の判断が要る地点。進めず記録だけする。
            tick = _insert_tick(manager, pid, {
                "plan_signature": plan_sig, "stop_class": stop_class,
                "stop_code": stop_code, "action_name": "record_stop_class",
                "status": "waiting_human", "next_action": next_action,
                "reason_ja": reason_ja,
                "evidence_refs": {k: _safe_text(v, 128) for k, v in refs.items()},
                "external_delta": 0, "external_rounds_delta": 0,
                "budget_kept": True, "dry_run": False, "actor": actor_name})
            try:
                append_history(manager, pid, flat, actor="auto-tick",
                               idempotency_key=f"autopilot:{tick.get('id')}")
            except Exception:
                pass
            # 記録だけの動作を許可アクションとしても残す (外部呼出0)。
            try:
                run_named_action(manager, pid, "record_stop_class",
                                 actor=actor_name, classification=record)
            except Exception:
                pass
            tick = dict(tick)
            tick["enabled"] = True
            return tick

        # 可逆なローカル作業だけ進める (決定的に1つ)。
        action_name = choose_action(stop_class, stop_code)
        before = _snapshot_external(manager, pid)
        result_detail: dict = {}
        action_error = ""
        needs_human_reason = ""
        with _count_external_calls(manager) as counts:
            try:
                result_detail = run_named_action(manager, pid, action_name,
                                                 actor=actor_name,
                                                 classification=record)
                if hasattr(result_detail, "__await__"):
                    raise ValueError("async action is not supported in tick")
            except _NeedsHumanDecision as exc:
                # 進めない条件(指摘なし・生成中・試行上限): 失敗ではなく人の判断待ち。
                needs_human_reason = str(exc)[:500] or "人の判断が必要です"
                result_detail = {}
            except Exception as exc:
                action_error = f"{type(exc).__name__}:{str(exc)[:300]}"
                result_detail = {}
        after = _snapshot_external(manager, pid)
        try:
            local_calls_made = int(counts.get("local_external_excluded") or 0)
        except Exception:
            local_calls_made = 0
        try:
            # ローカルAI(Ollama)は外部ではない。外部増分には数えない。
            calls_delta = int(counts.get("runner") or 0) + int(
                counts.get("provider") or 0)
        except Exception:
            calls_delta = 0
        try:
            sends_delta = int(after.get("sends") or 0) - int(before.get("sends") or 0)
        except (TypeError, ValueError):
            sends_delta = 0
        external_delta = int(calls_delta) + max(0, int(sends_delta))
        rounds_delta = 0
        try:
            if after.get("rounds") is not None and before.get("rounds") is not None:
                rounds_delta = int(after["rounds"]) - int(before["rounds"])
        except (TypeError, ValueError):
            rounds_delta = 0
        budget_kept = _budget_unchanged(before.get("budget") or {},
                                        after.get("budget") or {})
        if (needs_human_reason and not action_error and external_delta == 0
                and rounds_delta == 0 and budget_kept):
            # 進めない条件: 生成を試みたことにせず、人の判断待ちで記録する。
            # 自走は無効化しない (enabled を保ち、無限再生成は冪等＋空転ガードで止める)。
            tick = _insert_tick(manager, pid, {
                "plan_signature": plan_sig, "stop_class": "missing_decision",
                "stop_code": "autopilot:needs_human",
                "action_name": action_name,
                "status": "waiting_human", "next_action": next_action,
                "reason_ja": _safe_text(needs_human_reason, 1000),
                "evidence_refs": {k: _safe_text(v, 128) for k, v in refs.items()},
                "external_delta": external_delta,
                "external_rounds_delta": rounds_delta,
                "budget_kept": bool(budget_kept), "dry_run": False,
                "actor": actor_name})
            try:
                append_history(manager, pid, flat, actor="auto-tick",
                               idempotency_key=f"autopilot:{tick.get('id')}")
            except Exception:
                pass
            tick = dict(tick)
            tick["enabled"] = True
            tick["local_calls"] = int(local_calls_made)
            tick["detail"] = result_detail if isinstance(result_detail, dict) else {}
            return tick
        # 増分が0でないティックは失敗として記録し、以降の自走を止める。
        if action_error or external_delta != 0 or rounds_delta != 0 or not budget_kept:
            status = "failed"
            fail_reason = action_error or (
                f"外部増分を検出: calls={external_delta} "
                f"rounds={rounds_delta} budget_kept={budget_kept}")
            tick = _insert_tick(manager, pid, {
                "plan_signature": plan_sig, "stop_class": stop_class,
                "stop_code": stop_code, "action_name": action_name,
                "status": status, "next_action": next_action,
                "reason_ja": _safe_text(fail_reason, 1000),
                "evidence_refs": {k: _safe_text(v, 128) for k, v in refs.items()},
                "external_delta": external_delta,
                "external_rounds_delta": rounds_delta,
                "budget_kept": bool(budget_kept), "dry_run": False,
                "actor": actor_name})
            try:
                append_history(manager, pid, flat, actor="auto-tick",
                               idempotency_key=f"autopilot:{tick.get('id')}")
            except Exception:
                pass
            _disable_internal(manager, pid,
                              reason=f"ティック失敗のため自走を止めました: {fail_reason}")
            _audit(manager, pid, "autopilot_tick_failed",
                   {"action": action_name, "reason": fail_reason})
            tick = dict(tick)
            tick["enabled"] = False
            tick["local_calls"] = int(local_calls_made)
            tick["detail"] = result_detail if isinstance(result_detail, dict) else {}
            return tick

        detail_map = result_detail if isinstance(result_detail, dict) else {}
        candidate_note = ""
        effective_next = next_action
        if action_name == "generate_candidate" and detail_map.get("candidate_id"):
            # 候補が作られても「解消」とは記録しない。被覆が揃わない/blockers ありの
            # 場合は人の確認が要る次の一手(review_feedback)を記録する。
            try:
                from app.workflow_readiness import IMPLEMENTED_ACTIONS as _IMPL

                if "review_feedback" in set(_IMPL):
                    effective_next = "review_feedback"
            except Exception:
                effective_next = next_action
            candidate_note = (
                f"修正案を生成しました(候補ID={detail_map.get('candidate_id')})。"
                f"被覆{detail_map.get('covered_candidate')}/"
                f"{detail_map.get('coverage_rows')}行、"
                f"未解決{detail_map.get('blockers')}件。人の確認が必要です"
            )
        tick = _insert_tick(manager, pid, {
            "plan_signature": plan_sig, "stop_class": stop_class,
            "stop_code": stop_code, "action_name": action_name,
            "status": "advanced", "next_action": effective_next,
            "reason_ja": _safe_text(candidate_note or reason_ja, 1000),
            "evidence_refs": {k: _safe_text(v, 128) for k, v in refs.items()},
            "external_delta": 0, "external_rounds_delta": 0,
            "budget_kept": True, "dry_run": False, "actor": actor_name})
        try:
            append_history(manager, pid, flat, actor="auto-tick",
                           idempotency_key=f"autopilot:{tick.get('id')}")
        except Exception:
            pass
        tick = dict(tick)
        tick["enabled"] = True
        tick["local_calls"] = int(local_calls_made)
        tick["detail"] = detail_map
        return tick
    finally:
        try:
            lock.release()
        except RuntimeError:
            pass


def run_until_stop(manager, project_id: str, *, actor: str,
                   max_ticks: int = MAX_TICKS) -> dict:
    """上限つきで autopilot_tick を繰り返す。后台の常駐処理は作らない。"""
    _check_deleted(manager, project_id)
    pid = str(project_id or "")
    actor_name = _safe_text(actor, 100).strip()
    if not actor_name:
        raise ValueError("actor is required")
    try:
        limit = max(1, min(int(max_ticks or MAX_TICKS), MAX_TICKS))
    except (TypeError, ValueError):
        limit = MAX_TICKS
    ticks: list[dict] = []
    final_status = "completed"
    for _ in range(limit):
        tick = autopilot_tick(manager, pid, actor=actor_name, dry_run=False)
        ticks.append(tick)
        status = str(tick.get("status") or "")
        if status in ("disabled", "busy"):
            final_status = status
            break
        if status in ("failed", "stopped_guard", "waiting_human"):
            final_status = status
            break
        if status != "advanced":
            final_status = status
            break
    else:
        final_status = "max_ticks"
    return {"project_id": pid, "status": final_status, "ticks": ticks,
            "count": len(ticks), "max_ticks": limit, "actor": actor_name}
