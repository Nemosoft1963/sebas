"""P1: version-bound, read-only local repair candidate preview."""
from __future__ import annotations

import re
from copy import deepcopy

from app.goal_contract import preview as contract_preview
from app.goal_review import ReviewStore, plan_snapshot
from app.plan_feedback import issues_for, normalize_issue_text
from app.structured_planning import contract_of

BUSINESS_FACT = "business_fact"
SAFE_PLAN_PATCH = "safe_plan_patch"
DEVELOPMENT_REQUIRED = "development_required"

_BUSINESS = re.compile(r"リード|有効条件|対象顧客|予算|価格|配賦|所属|乗替|業務事実|承認者")
_DEVELOPMENT = re.compile(r"実装|API|機能追加|専用処理|コード|自動化|連携|認証|データベース")
_ORDER = re.compile(r"順序|先に|後に|依存|禁止|してはいけ|公開前|送信前|承認後")
_ID = re.compile(r"\b(?:SC|C)\d{2}\b", re.I)


def _stable_issue_id(issue: dict, index: int) -> str:
    return str(issue.get("id") or issue.get("issue_id") or f"issue-{index:02d}")


def _criterion(issue: dict, criteria: list[dict]) -> dict:
    text = " ".join((str(issue.get("criterion") or ""), str(issue.get("text") or "")))
    wanted = [x.upper() for x in _ID.findall(text)]
    by_id = {str(x.get("criterion_id") or "").upper(): x for x in criteria}
    for ident in wanted:
        if ident in by_id:
            return by_id[ident]
    normalized = normalize_issue_text(text)
    for item in criteria:
        statement = normalize_issue_text(item.get("statement") or "")
        if statement and (statement in normalized or normalized in statement):
            return item
    return {}


def _task_for(criterion: dict, tasks: list[dict]) -> dict:
    keys = list(criterion.get("exec_task_keys") or [])
    cid = str(criterion.get("criterion_id") or "")
    keys.extend([cid] if cid else [])
    by_key = {str(x.get("task_key") or ""): x for x in tasks}
    for key in keys:
        if key in by_key:
            return by_key[key]
    return {}


def _classification(text: str, task: dict) -> str:
    if _BUSINESS.search(text):
        return BUSINESS_FACT
    contract = contract_of(task) or {}
    dedicated = bool(contract.get("execution_kind") or contract.get("public_web_research"))
    if _DEVELOPMENT.search(text) or dedicated and not _ORDER.search(text):
        return DEVELOPMENT_REQUIRED
    return SAFE_PLAN_PATCH


def _patch(text: str, task: dict) -> dict | None:
    if not task:
        return None
    instruction = (
        "承認済み成果物・公開済みサイト/フォームを再作成せず、既存成果を再利用する。"
        "外部送信・公開・破壊的変更は人間承認後に行い、実行後に検証証拠を記録する。"
    )
    if _ORDER.search(text):
        instruction = "操作順序を固定する: 検証、明示承認、外部送信または公開の順とし、承認前の外部操作を禁止する。"
    before = {
        "description": str(task.get("description") or ""),
        "depends_on": deepcopy(task.get("depends_on") or []),
        "contract": deepcopy(contract_of(task) or {}),
    }
    after = deepcopy(before)
    if instruction not in after["description"]:
        after["description"] = (after["description"].rstrip() + "\n\n局所修復候補: " + instruction).strip()
    return {"task_key": str(task.get("task_key") or ""), "before": before, "after": after}


def preview(manager, project_id: str, issues: list[dict] | None = None) -> dict:
    """Return a deterministic preview. This function performs no writes or application."""
    mission = manager.memory.get_mission(project_id)
    snapshot, signature = plan_snapshot(manager, project_id)
    contract = contract_preview(manager, project_id) or {}
    criteria = list(contract.get("criteria") or [])
    tasks = list(snapshot.get("tasks") or mission.get("tasks") or [])
    source = list(issues if issues is not None else issues_for(manager, project_id, signature))
    store = ReviewStore(manager.memory.path)

    invalidations = []
    if store.get(project_id, "plan", signature):
        invalidations.append({"type": "plan_review", "signature": signature})
    if store.get(project_id, "send_approval", signature):
        invalidations.append({"type": "send_approval", "signature": signature})

    candidates = []
    questions = []
    touches_completed = False
    for index, issue in enumerate(source, 1):
        iid = _stable_issue_id(issue, index)
        criterion = _criterion(issue, criteria)
        task = _task_for(criterion, tasks)
        text = normalize_issue_text(issue.get("text") or issue.get("reason") or "")
        bound = bool(criterion and task)
        kind = _classification(text, task) if bound else DEVELOPMENT_REQUIRED
        patch = _patch(text, task) if bound and kind == SAFE_PLAN_PATCH else None
        basis = text[:600] or "現行指摘を現行GoalContractへ再結合"
        if not bound:
            basis = "現行の達成条件・工程に再結合できない: " + basis
        if str(task.get("status") or "") in {"completed", "done", "succeeded"} and patch:
            touches_completed = True
        question = ""
        if kind == BUSINESS_FACT:
            question = f"{text[:160]}について、適用条件と判断根拠を回答してください。"
            questions.append({"issue_id": iid, "question": question})
        candidates.append({
            "issue_id": iid,
            "plan_signature": signature,
            "goal_id": str(contract.get("goal_id") or ""),
            "goal_contract_hash": str(contract.get("content_hash") or ""),
            "criterion_id": str(criterion.get("criterion_id") or ""),
            "task_key": str(task.get("task_key") or ""),
            "basis": basis,
            "classification": kind,
            "bound": bound,
            "question": question,
            "patch": patch,
        })

    automatic_allowed = False  # P1 requirement: preview/classification only; human gate always required.
    reasons = ["P1では自動適用せず、既存の人間承認ゲートを必須とします"]
    if len(invalidations) >= 2:
        reasons.append("失効対象が2件以上です")
    if touches_completed:
        reasons.append("完了工程を変更する候補があります")
    review = store.get(project_id, "plan", signature) or {}
    externally_passed = review.get("status") == "passed"
    return {
        "project_id": project_id,
        "plan_signature": signature,
        "goal_id": str(contract.get("goal_id") or ""),
        "goal_contract_hash": str(contract.get("content_hash") or ""),
        "candidates": candidates,
        "questions": questions,
        "invalidated_approvals": invalidations,
        "touches_completed_task": touches_completed,
        "automatic_apply_allowed": automatic_allowed,
        "automatic_apply_blocked_reasons": reasons,
        "requires_human_approval": True,
        "requires_external_revalidation": True,
        "external_validation_signature": signature,
        "external_validation_passed": externally_passed,
        "execution_start_allowed": False,  # This preview cannot grant execution approval.
        "preserve_existing_artifacts": True,
        "recreate_published_assets": False,
        "saved": False,
    }
