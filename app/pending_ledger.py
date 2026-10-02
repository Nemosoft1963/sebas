"""P0: 承認・停止状態の単一台帳 (Pending Ledger)。

workflow-readiness / next-action / 概要カードの判定を同一台帳から生成する。
読み取り専用: このモジュールは DB への書き込みを行わない。
"""
from __future__ import annotations

import time


KIND_PLAN_ISSUE = "plan_issue"
KIND_PLAN_APPROVAL = "plan_approval"
KIND_EXTERNAL_ACTION = "external_action"
KIND_RESULT_APPROVAL = "result_approval"
KIND_BUSINESS_FACT = "business_fact"

KINDS = (
    KIND_PLAN_ISSUE,
    KIND_PLAN_APPROVAL,
    KIND_EXTERNAL_ACTION,
    KIND_RESULT_APPROVAL,
    KIND_BUSINESS_FACT,
)

UNIMPLEMENTED_IDS = frozenset({"triz_adopt", "triz_rerun", "recipe_apply"})
UNIMPLEMENTED_REASONS = {
    "triz_adopt": "P2完了までTRIZ採用は blocked: framed/candidates/tried は回復成功ではありません",
    "triz_rerun": "TRIZ採用再実行は業務検査と限定再実行の完了後です",
    "recipe_apply": "承認済みレシピは抽出時に適用判定します。条件不一致は明示拒否し、専用ボタンでは再実行しません",
}
IDLE_WAIT = frozenset({"idle", "wait"})


def decide_action(
    action_id: str,
    action_class: str = "",
    auto: bool = False,
    allowed_actions: dict | None = None,
    stop_reason: str = "",
    unimplemented_ids=None,
) -> dict:
    """単一の判定式。readiness と NAC の両方から使う。

    - navigable: 詳細を開けるか (idle 以外は True。停止中でも開ける)
    - executable: 処理を実行できるか (local_safe かつ allowed のみ True)
    - manual_executable: 人が操作すれば実行できるか
    - auto_executable: システムが自動実行できるか
    """
    if unimplemented_ids is None:
        unimplemented = set(UNIMPLEMENTED_IDS)
    else:
        unimplemented = set(unimplemented_ids)
    if allowed_actions is None:
        allowed = False
        rule_reason = "action permissions unavailable"
    else:
        row = (allowed_actions or {}).get(str(action_id or ""))
        if isinstance(row, dict):
            allowed = bool(row.get("allowed"))
            rule_reason = str(row.get("reason") or "")
        elif isinstance(row, bool):
            allowed = bool(row)
            rule_reason = ""
        else:
            allowed = False
            rule_reason = "action is not allowed"
    action_id = str(action_id or "")
    if action_id in unimplemented:
        allowed = False
        if not rule_reason or rule_reason == "action is not allowed":
            rule_reason = str(UNIMPLEMENTED_REASONS.get(action_id) or "automatic execution is not permitted")
    action_class = str(action_class or "")
    executable = (
        bool(allowed)
        and action_id not in IDLE_WAIT
        and action_class == "local_safe"
        and action_id not in unimplemented
    )
    manual_executable = (
        bool(allowed) and action_id not in IDLE_WAIT and action_id not in unimplemented
    )
    auto_executable = bool(auto) and action_class == "local_safe" and executable
    blocked = not executable
    reason = str(rule_reason or "")
    if not reason and blocked:
        reason = str(stop_reason or "automatic execution is not permitted")
    navigable = action_id not in {"idle"}
    return {
        "allowed": bool(allowed),
        "reason": reason,
        "executable": bool(executable),
        "manual_executable": bool(manual_executable),
        "auto_executable": bool(auto_executable),
        "blocked": bool(blocked),
        "navigable": bool(navigable),
    }


def _safe_mission(manager, pid: str) -> dict:
    try:
        mission = manager.memory.get_mission(pid)
        return dict(mission) if isinstance(mission, dict) else {}
    except Exception:
        return {}


def _safe_signature(manager, pid: str) -> tuple[dict, str]:
    try:
        from app.goal_review import plan_snapshot

        snapshot, signature = plan_snapshot(manager, pid)
        return snapshot, str(signature or "")
    except Exception:
        return {}, ""


def _safe_issues(manager, pid: str, signature: str) -> list:
    try:
        from app.plan_feedback import issues_for

        rows = issues_for(manager, pid, signature)
        return [dict(x) if isinstance(x, dict) else {"text": str(x)} for x in (rows or [])]
    except Exception:
        return None


def _safe_store_row(manager, pid: str, kind: str, signature: str):
    try:
        from app.goal_review import ReviewStore

        store = ReviewStore(manager.memory.path)
        row = store.get(pid, kind, signature)
        return dict(row) if isinstance(row, dict) else None
    except Exception:
        return None


def _safe_actions(manager, pid: str) -> list:
    try:
        rows = manager.memory.list_actions(pid)
        return [dict(x) for x in (rows or []) if isinstance(x, dict)]
    except Exception:
        return None


def _safe_result_state(manager, pid: str, current_signature: str) -> tuple[str, str, str]:
    """戻り値: (state, basis, result_signature)。承認済みに見せない条件を厳密にする。"""
    try:
        from app.goal_review import ReviewStore, execution_snapshot

        _proof, result_signature, failures = execution_snapshot(manager, pid)
        result_signature = str(result_signature or "")
        if not result_signature:
            return "missing", "結果レビューは未作成です", ""
        store = ReviewStore(manager.memory.path)
        row = store.get(pid, "result", result_signature)
        if not row:
            return "missing", "結果レビューは未作成です", result_signature
        if not isinstance(row, dict):
            return "invalid", "結果レビューの形式が不正です", result_signature
        if not current_signature:
            return "unknown", "現行計画の署名を確認できません", result_signature
        if row.get("status") != "approved":
            return "unapproved", "結果レビューは承認されていません", result_signature
        try:
            expires = float(row.get("expires") or 0)
        except (TypeError, ValueError):
            expires = 0
        if not expires or expires <= time.time():
            return "expired", "結果レビューの承認期限が切れています", result_signature
        snapshot = row.get("snapshot") or {}
        snap_plan = str(snapshot.get("plan") or "")
        if not snap_plan or snap_plan != current_signature:
            return "stale", "旧版への承認のため現行版では無効です", result_signature
        failures = list(failures or [])
        if failures:
            return "unapproved", "未達条件が残るため承認できません: " + " / ".join(failures[:3]), result_signature
        return "approved", "現行結果への承認が有効です", result_signature
    except Exception as exc:
        return "unknown", str(exc)[:300] or "結果レビューを確認できません", ""


def _plan_approval_state(plan_row: dict | None, mission: dict) -> tuple[str, str]:
    status = str((plan_row or {}).get("status") or "")
    if not plan_row or not status:
        return "unverified", "現行版の外部検証が未完了または未合格です"
    if status == "passed":
        mstatus = str(mission.get("status") or "")
        if mstatus in {"ready", "paused", "running", "completed"}:
            return "approved", "現行計画は検証合格かつ承認済みです"
        return "unapproved", "現行計画は検証合格ですが人間の承認待ちです"
    if status == "not_passed":
        return "not_passed", "計画内容に指摘があります (not_passed)。承認ではなく修正案が必要です"
    if status == "connection_failed":
        return "connection_failed", str((plan_row or {}).get("stop_reason") or "外部AI接続の問題で停止しています")
    if status in {"awaiting_external", "waiting_budget"}:
        return str(status), str((plan_row or {}).get("stop_reason") or "外部検証の完了待ちです")
    if status in {"running", "generating"}:
        return "running", "外部検証を実行中です"
    if status == "needs_attention":
        return "needs_attention", str((plan_row or {}).get("stop_reason") or "検証の再確認が必要です")
    return str(status), str((plan_row or {}).get("stop_reason") or "現行版の検証が未完了です")


def build(manager, pid: str, allowed_actions: dict | None = None) -> dict:
    """単一台帳を構築する。読み取り専用。別案件・失効版は含めない。"""
    pid = str(pid or "")
    mission = _safe_mission(manager, pid)
    _snapshot, signature = _safe_signature(manager, pid)
    issues = _safe_issues(manager, pid, signature) if signature else None
    issues_unavailable = issues is None
    issues = issues or []
    # 一部の既存データは plan_feedback 名で現行版の指摘を保持する。
    # issues_for が正規化対象にしていない場合も、現行案件・現行署名に限定して取り込む。
    if signature and not issues:
        feedback_row = _safe_store_row(manager, pid, "plan_feedback", signature) or {}
        issues = [dict(x) for x in (feedback_row.get("issues") or []) if isinstance(x, dict)]
    plan_row = _safe_store_row(manager, pid, "plan", signature) if signature else None
    revision = _safe_store_row(manager, pid, "revision", signature) if signature else None
    actions = _safe_actions(manager, pid)
    actions_unavailable = actions is None
    actions = actions or []
    result_state, result_basis, result_signature = _safe_result_state(manager, pid, signature)
    plan_state, plan_basis = _plan_approval_state(plan_row, mission)
    if not signature:
        plan_state, plan_basis = "unknown", "現行計画の署名を確認できません"

    blockers = []
    if isinstance(revision, dict):
        for item in revision.get("blockers") or []:
            if isinstance(item, dict):
                blockers.append(item)
    revision_status = str((revision or {}).get("status") or "")
    has_changes = bool((revision or {}).get("changes"))
    if (blockers or issues or issues_unavailable) and plan_state == "approved":
        plan_state, plan_basis = "unapproved", "指摘または未解決の修正案があるため現行計画を承認済みにできません"

    items: list[dict] = []

    # 計画指摘: 現行署名の issues_for のみ。別案件・旧版は含めない。
    for issue in issues:
        iid = str(issue.get("id") or issue.get("issue_id") or "")
        provider = str(issue.get("provider") or "")
        text = str(issue.get("text") or "")[:600]
        if revision_status == "draft" and blockers:
            next_action = "resolve_development"
        elif revision_status == "draft" and has_changes:
            next_action = "apply"
        else:
            next_action = "propose_feedback"
        items.append(
            {
                "project_id": pid,
                "target": iid,
                "kind": KIND_PLAN_ISSUE,
                "plan_signature": signature,
                "state": "open",
                "basis": (provider + ": " + text).strip(": ")[:800],
                "next_action": next_action,
                "owner": "human",
                "navigable": True,
                "executable": False,
            }
        )

    # 計画承認: not_passed と unapproved を別状態にする。
    if plan_state == "approved":
        plan_next = "start"
        plan_owner = "human"
    elif plan_state == "unapproved":
        plan_next = "approve_plan"
        plan_owner = "human"
    elif plan_state in {"connection_failed", "running", "awaiting_external", "waiting_budget"}:
        plan_next = "external_review"
        plan_owner = "external"
    elif issues:
        plan_next = "propose_feedback"
        plan_owner = "human"
    else:
        plan_next = "external_review"
        plan_owner = "human"
    items.append(
        {
            "project_id": pid,
            "target": "plan:" + str(mission.get("plan_version") or 0),
            "kind": KIND_PLAN_APPROVAL,
            "plan_signature": signature,
            "state": plan_state,
            "basis": plan_basis[:800],
            "next_action": plan_next,
            "owner": plan_owner,
            "navigable": True,
            "executable": False,
        }
    )

    # 外部操作: 承認待ち0件と計画未承認は別集計にする。別案件は含めない。
    pending_external = 0
    approved_external = 0
    for action in actions:
        status = str(action.get("status") or "")
        if status not in {"pending_approval", "approved"}:
            continue
        if status == "pending_approval":
            pending_external += 1
        else:
            approved_external += 1
        aid = str(action.get("id") or "")
        kind = str(action.get("kind") or "")
        target = str(action.get("target") or "")
        if status == "pending_approval":
            state = "pending_approval"
            basis = "外部操作の人間承認待ち: " + kind + " -> " + target
            nxt = "approve_external_action"
        else:
            state = "approved_awaiting_execution"
            basis = "承認済み・実行/証拠待ち: " + kind + " -> " + target
            nxt = "execute_external_action"
        items.append(
            {
                "project_id": pid,
                "target": aid,
                "kind": KIND_EXTERNAL_ACTION,
                "plan_signature": signature,
                "state": state,
                "basis": basis[:800],
                "next_action": nxt,
                "owner": "human",
                "navigable": True,
                "executable": False,
            }
        )

    # 結果承認: 未作成・失効版を承認済みにしない。
    if result_state == "approved":
        result_next = "approve_result"
    else:
        result_next = "approve_result"
    items.append(
        {
            "project_id": pid,
            "target": result_signature or "result:missing",
            "kind": KIND_RESULT_APPROVAL,
            "plan_signature": signature,
            "state": result_state,
            "basis": result_basis[:800],
            "next_action": result_next,
            "owner": "human",
            "navigable": True,
            "executable": False,
        }
    )

    # 業務事実: 現行 revision の business_fact ブロッカーのみ。旧版は含めない。
    business_count = 0
    for index, blocker in enumerate(blockers):
        disposition = str(blocker.get("disposition") or "")
        reason = str(blocker.get("reason") or "")[:600]
        if disposition == "business_fact":
            business_count += 1
            items.append(
                {
                    "project_id": pid,
                    "target": "blocker:" + str(index),
                    "kind": KIND_BUSINESS_FACT,
                    "plan_signature": signature,
                    "state": "open",
                    "basis": reason or "業務事実の確認が必要です",
                    "next_action": "confirm_allocation",
                    "owner": "human",
                    "navigable": True,
                    "executable": False,
                }
            )
    # development / unresolved ブロッカーは計画指摘として可視化 (未解決6件のため)
    for index, blocker in enumerate(blockers):
        disposition = str(blocker.get("disposition") or "")
        if disposition == "business_fact":
            continue
        reason = str(blocker.get("reason") or "")[:600]
        items.append(
            {
                "project_id": pid,
                "target": "blocker:" + str(index),
                "kind": KIND_PLAN_ISSUE,
                "plan_signature": signature,
                "state": "open",
                "basis": ("[" + disposition + "] " + reason).strip()[:800] or "未解決の指摘があります",
                "next_action": "resolve_development",
                "owner": "human",
                "navigable": True,
                "executable": False,
            }
        )

    # 台帳は状態の参照専用。人間の確認・承認を実行可能と表示しない。
    open_items = [x for x in items if str(x.get("state") or "") not in {"approved", "executed", "resolved"}]
    summary = {
        "project_id": pid,
        "plan_signature": signature,
        "plan_status": plan_state,
        "plan_approval_blocked": plan_state != "approved",
        "plan_basis": plan_basis[:800],
        "issue_count": None if issues_unavailable else len(issues),
        "unresolved_count": len(blockers),
        "business_fact_count": business_count,
        "open_count": None if issues_unavailable or actions_unavailable else len(open_items),
        "external_pending_count": None if actions_unavailable else pending_external,
        "external_approved_waiting_count": None if actions_unavailable else approved_external,
        "external_waiting_zero": None if actions_unavailable else pending_external == 0,
        "result_state": result_state,
        "result_approved": result_state == "approved",
    }
    return {"items": items, "summary": summary}


def summarize_for_action(summary: dict, decision: dict, action_id: str) -> dict:
    """上位表示用の要約に今回の判定を結合する。"""
    out = dict(summary or {})
    out.update(
        {
            "next_action_id": str(action_id or ""),
            "allowed": bool(decision.get("allowed")),
            "executable": bool(decision.get("executable")),
            "manual_executable": bool(decision.get("manual_executable")),
            "auto_executable": bool(decision.get("auto_executable")),
            "blocked": bool(decision.get("blocked")),
            "navigable": bool(decision.get("navigable")),
            "reason": str(decision.get("reason") or ""),
        }
    )
    return out
