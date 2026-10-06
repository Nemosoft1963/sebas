"""P1-C: 計画の意味検証と修復ループ。

現行の達成条件と外部指摘を、同一の GoalContract・同一の計画署名に固定し、
未解決指摘を各条件・工程・具体的な修正差分へ対応づける修復ランを永続化する。
順序: proposed → patched → structure_checked → coverage_checked →
      external_reviewed → rediff_evaluated → passed / failed / blocked。
自己申告の合格値は信用せず、各段階で既存の実在関数をサーバー側で再実行する。
業務判断を要する基準値・価格・対外行為はシステムが勝手に確定しない。
passed になっても計画の承認自体は人間が既存ゲートで行う(自動承認しない)。
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid

ORDER = (
    "proposed",
    "patched",
    "structure_checked",
    "coverage_checked",
    "external_reviewed",
    "rediff_evaluated",
    "passed",
)
TERMINAL = frozenset({"passed", "failed", "blocked"})

NEXT = {
    "proposed": ("patched",),
    "patched": ("structure_checked",),
    "structure_checked": ("coverage_checked",),
    "coverage_checked": ("external_reviewed",),
    "external_reviewed": ("rediff_evaluated",),
    "rediff_evaluated": ("passed", "failed"),
}

# 上限(生成回数・外部費用・変更範囲)。件数ベース。
MAX_EXTERNAL_CALLS = 3
MAX_ADVANCES = 12
MAX_PATCHED_TASKS = 8


def _store(manager):
    from app.goal_review import ReviewStore

    return ReviewStore(manager.memory.path)


def _now() -> float:
    return time.time()


def _current_context(manager, project_id: str) -> tuple[dict, str, dict]:
    """現行の mission・計画署名・GoalContract を返す(実在関数のみ)。"""
    from app.goal_contract import preview as contract_preview
    from app.goal_review import plan_snapshot

    mission = manager.memory.get_mission(project_id)
    _snapshot, signature = plan_snapshot(manager, project_id)
    contract = contract_preview(manager, project_id) or {}
    return mission, str(signature or ""), contract


def _expected_signature(run: dict) -> str:
    return str(run.get("patched_signature") or run.get("plan_signature") or "")


def _check_pinned(manager, project_id: str, run: dict) -> tuple[dict, str, dict]:
    """同一署名・同一 GoalContract に固定されていることを検証する。"""
    mission, signature, contract = _current_context(manager, project_id)
    expected = _expected_signature(run)
    if signature != expected:
        raise ValueError(
            "計画署名が変わりました。修復ランは同一署名に固定されます "
            f"(run={expected}, current={signature})。現行版で新規開始してください"
        )
    if str(contract.get("content_hash") or "") != str(run.get("goal_contract_hash") or ""):
        raise ValueError(
            "GoalContract が変わりました。修復ランは同一 GoalContract に固定されます。"
            "現行版で新規開始してください"
        )
    return mission, signature, contract


def _save(manager, run: dict) -> dict:
    run["updated_at"] = _now()
    _store(manager).put(run["project_id"], "repair_run", run["id"], run)
    return run


def get_run(manager, project_id: str, run_id: str) -> dict:
    row = _store(manager).get(project_id, "repair_run", str(run_id))
    if not row:
        raise ValueError("repair run not found")
    return row


def list_runs(manager, project_id: str) -> list[dict]:
    rows = [payload for _sig, payload in _store(manager).list(project_id, "repair_run")]
    rows.sort(key=lambda x: float(x.get("created_at") or 0))
    return rows


def _candidate_view(candidate: dict) -> dict:
    return {
        "issue_id": candidate.get("issue_id"),
        "issue_excerpt": candidate.get("issue_excerpt") or "",
        "criterion_id": candidate.get("criterion_id"),
        "task_key": candidate.get("task_key"),
        "classification": candidate.get("classification"),
        "bound": bool(candidate.get("bound")),
        "basis": candidate.get("basis") or "",
        "question": candidate.get("question") or "",
        "has_patch": bool(candidate.get("patch")),
        "plan_signature": candidate.get("plan_signature") or "",
        "goal_contract_hash": candidate.get("goal_contract_hash") or "",
        "contract_hash": candidate.get("contract_hash") or "",
        "artifact_diff": candidate.get("artifact_diff") or {},
    }


def _contract_hash_of(mission: dict, task_key: str) -> str:
    """現行工程の成果物契約ハッシュ。古いSC番号への付け替えは行わない。"""
    from app.structured_planning import contract_of

    for task in mission.get("tasks") or []:
        if str(task.get("task_key") or "") != str(task_key or ""):
            continue
        contract = contract_of(task) or {}
        raw = json.dumps(contract, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode()).hexdigest()
    return ""


def _artifact_diff_of(patch: dict | None) -> dict:
    """before/after の成果物契約・検証条件の差分。無い場合は空辞書。"""
    if not isinstance(patch, dict):
        return {}
    before = patch.get("before") if isinstance(patch.get("before"), dict) else {}
    after = patch.get("after") if isinstance(patch.get("after"), dict) else {}
    return {"before": dict(before), "after": dict(after)}


def _enrich_candidates(manager, project_id: str, mission: dict, raw_candidates: list[dict]) -> list[dict]:
    """P0: 指摘→現行条件→現行工程→成果物契約/検証条件差分を証拠付きで保存する。"""
    enriched = []
    for cand in raw_candidates or []:
        patch = cand.get("patch") if isinstance(cand.get("patch"), dict) else None
        task_key = str(cand.get("task_key") or "")
        enriched.append({
            "issue_id": str(cand.get("issue_id") or ""),
            "issue_excerpt": str(cand.get("basis") or "")[:600],
            "criterion_id": str(cand.get("criterion_id") or ""),
            "task_key": task_key,
            "classification": str(cand.get("classification") or ""),
            "bound": bool(cand.get("bound")),
            "basis": str(cand.get("basis") or "")[:2000],
            "question": str(cand.get("question") or ""),
            "patch": patch,
            "artifact_diff": _artifact_diff_of(patch),
            "plan_signature": str(cand.get("plan_signature") or ""),
            "goal_contract_hash": str(cand.get("goal_contract_hash") or ""),
            "contract_hash": _contract_hash_of(mission, task_key) if task_key else "",
        })
    return enriched


def public_view(run: dict) -> dict:
    return {
        "id": run.get("id"),
        "project_id": run.get("project_id"),
        "plan_signature": run.get("plan_signature"),
        "patched_signature": run.get("patched_signature"),
        "goal_id": run.get("goal_id"),
        "goal_contract_hash": run.get("goal_contract_hash"),
        "issue_ids": list(run.get("issue_ids") or []),
        "state": run.get("state"),
        "candidates": [_candidate_view(c) for c in (run.get("candidates") or [])],
        "fact_answers": dict(run.get("fact_answers") or {}),
        "applied": list(run.get("applied") or []),
        "evidence": dict(run.get("evidence") or {}),
        "external_calls": int(run.get("external_calls") or 0),
        "advances": int(run.get("advances") or 0),
        "unresolved_count": run.get("unresolved_count"),
        "created_at": run.get("created_at"),
        "updated_at": run.get("updated_at"),
    }


def start(manager, project_id: str, actor: str, issues: list[dict] | None = None) -> dict:
    """修復ランを開始し、案件・計画署名・GoalContract・指摘ID一覧を固定する。"""
    if not str(actor or "").strip():
        raise ValueError("actor is required")
    mission, signature, contract = _current_context(manager, project_id)
    if not signature:
        raise ValueError("plan signature is unavailable")
    if not contract.get("content_hash"):
        raise ValueError("GoalContract is unavailable")
    from app.local_patch_preview import preview as patch_preview
    from app.plan_feedback import issues_for

    base_issues = list(issues) if issues is not None else issues_for(manager, project_id, signature)
    preview = patch_preview(manager, project_id, issues=base_issues)
    if str(preview.get("plan_signature") or "") != signature:
        raise ValueError("計画署名の取得が安定しません。再読込してください")
    if str(preview.get("goal_contract_hash") or "") != str(contract.get("content_hash") or ""):
        raise ValueError("GoalContract の取得が安定しません。再読込してください")
    candidates = []
    issue_ids = []
    for cand in preview.get("candidates") or []:
        issue_ids.append(str(cand.get("issue_id") or ""))
    candidates = _enrich_candidates(manager, project_id, mission, preview.get("candidates") or [])
    try:
        from app.pending_ledger import build as ledger_build

        pending_summary = (ledger_build(manager, project_id) or {}).get("summary") or {}
    except Exception:
        pending_summary = {}
    run = {
        "id": uuid.uuid4().hex,
        "project_id": project_id,
        "plan_signature": signature,
        "patched_signature": "",
        "goal_id": str(contract.get("goal_id") or ""),
        "goal_contract_hash": str(contract.get("content_hash") or ""),
        "issue_ids": issue_ids,
        "state": "proposed",
        "candidates": candidates,
        "fact_answers": {},
        "applied": [],
        "evidence": {
            "proposed": {
                "actor": str(actor).strip(),
                "criterion_count": len(contract.get("criteria") or []),
                "invalidated_approvals": preview.get("invalidated_approvals") or [],
                "touches_completed_task": bool(preview.get("touches_completed_task")),
                "pending_summary": pending_summary,
            }
        },
        "external_calls": 0,
        "advances": 0,
        "unresolved_count": None,
        "created_at": _now(),
        "updated_at": _now(),
    }
    _store(manager).put(project_id, "repair_run", run["id"], run)
    return public_view(run)


def _unanswered_questions(run: dict) -> list[str]:
    answers = run.get("fact_answers") or {}
    missing = []
    for cand in run.get("candidates") or []:
        if cand.get("classification") == "business_fact" and cand.get("question"):
            if not str(answers.get(cand.get("issue_id")) or "").strip():
                missing.append(str(cand.get("issue_id")))
    return missing


def _lingering_originals(run: dict) -> list[str]:
    """局所パッチでは解決できない原本(未結合・要実装)を数える。"""
    out = []
    for cand in run.get("candidates") or []:
        if not cand.get("bound") or cand.get("classification") == "development_required":
            out.append(str(cand.get("issue_id")))
    return out


def _current_unresolved(manager, project_id: str, run: dict, signature: str) -> dict:
    from app.local_patch_preview import preview as patch_preview
    from app.plan_feedback import issues_for

    current_issues = issues_for(manager, project_id, signature)
    current_ids = [str(x.get("id") or x.get("issue_id") or "") for x in current_issues]
    original_ids = set(run.get("issue_ids") or [])
    remaining = [iid for iid in current_ids if iid in original_ids]
    new = [iid for iid in current_ids if iid not in original_ids]
    resolved = [iid for iid in original_ids if iid not in current_ids]
    lingering = [iid for iid in _lingering_originals(run) if iid not in resolved]
    unanswered = [iid for iid in _unanswered_questions(run)]
    preview = patch_preview(manager, project_id, issues=current_issues)
    unresolved = len(remaining) + len(new) + len(lingering)
    return {
        "current_issue_ids": current_ids,
        "remaining": remaining,
        "new": new,
        "resolved": resolved,
        "lingering_originals": lingering,
        "unanswered_business_facts": unanswered,
        "unresolved_count": unresolved,
        "current_preview": {
            "classifications": {
                str(c.get("issue_id")): str(c.get("classification")) for c in preview.get("candidates") or []
            },
            "unbound": [str(c.get("issue_id")) for c in preview.get("candidates") or [] if not c.get("bound")],
        },
    }


def _run_structure_check(mission: dict, contract: dict) -> dict:
    from app.structured_planning import inspect_plan_structure

    expected = {str(x.get("criterion_id") or "") for x in contract.get("criteria") or []}
    expected.discard("")
    result = inspect_plan_structure({"tasks": mission.get("tasks") or []}, expected)
    return result


def _run_coverage_check(mission: dict, contract: dict) -> dict:
    from app.plan_coverage import build as build_coverage
    from app.requirement_retention import inspect as inspect_retention

    coverage = build_coverage(mission, contract)
    retention = inspect_retention(mission, contract)
    return {"coverage": coverage, "retention": retention}


def _do_patched(manager, project_id: str, run: dict, payload: dict, actor: str) -> dict:
    mission, signature, _contract = _current_context(manager, project_id)
    if signature != str(run.get("plan_signature") or ""):
        raise ValueError("開始後に計画が変わりました。現行版で新規開始してください")
    proposed = (run.get("evidence") or {}).get("proposed") or {}
    if len(proposed.get("invalidated_approvals") or []) >= 2:
        raise ValueError("失効する承認が2件以上のため自動適用できません。人間が承認を整理してください")
    if proposed.get("touches_completed_task"):
        raise ValueError("完了工程を変更する候補があるため自動適用できません。人間が個別に判断してください")
    by_issue = {c.get("issue_id"): c for c in run.get("candidates") or []}
    approvals = payload.get("approvals") or []
    if not isinstance(approvals, list):
        raise ValueError("approvals must be an array")
    approved_map: dict[str, dict] = {}
    for item in approvals:
        if not isinstance(item, dict) or item.get("approved") is not True:
            continue
        iid = str(item.get("issue_id") or "")
        cand = by_issue.get(iid)
        if not cand:
            raise ValueError(f"unknown issue_id: {iid}")
        if cand.get("classification") != "safe_plan_patch" or not cand.get("bound") or not cand.get("patch"):
            raise ValueError(f"承認済み safe_plan_patch だけを適用できます: {iid}")
        reviewer = str(item.get("reviewer") or "").strip()
        reason = str(item.get("reason") or "").strip()
        if not reviewer or not reason:
            raise ValueError(f"人間承認には reviewer と reason が必須です: {iid}")
        approved_map[iid] = {"reviewer": reviewer, "reason": reason}
    answers = payload.get("answers") or {}
    if not isinstance(answers, dict):
        raise ValueError("answers must be an object")
    fact_answers = dict(run.get("fact_answers") or {})
    for iid, text in answers.items():
        if not str(text or "").strip():
            continue
        cand = by_issue.get(str(iid))
        if not cand or cand.get("classification") != "business_fact":
            raise ValueError("business_fact の指摘にだけ回答できます。推測では埋めません")
        fact_answers[str(iid)] = str(text).strip()[:2000]
    touched_keys = {by_issue[iid]["task_key"] for iid in approved_map if by_issue[iid].get("task_key")}
    if len(touched_keys) > MAX_PATCHED_TASKS:
        raise ValueError(f"変更範囲が上限({MAX_PATCHED_TASKS}工程)を超えています")
    new_signature = signature
    applied: list[dict] = []
    if approved_map:
        patch_by_key: dict[str, dict] = {}
        for iid in approved_map:
            cand = by_issue[iid]
            patch_by_key.setdefault(cand["task_key"], []).append((iid, cand["patch"]))
        tasks = []
        for task in mission.get("tasks") or []:
            row = {
                "task_key": task.get("task_key"),
                "depends_on": list(task.get("depends_on") or []),
                "title": task.get("title"),
                "description": task.get("description"),
                "acceptance_criteria": task.get("acceptance_criteria"),
                "mode": task.get("mode") or "local",
            }
            if row["task_key"] in patch_by_key:
                after = patch_by_key[row["task_key"]][0][1].get("after") or {}
                row["description"] = after.get("description") or row["description"]
                applied.append({
                    "task_key": row["task_key"],
                    "issue_ids": [iid for iid, _p in patch_by_key[row["task_key"]]],
                    **approved_map[[iid for iid, _p in patch_by_key[row["task_key"]]][0]],
                })
            tasks.append(row)
        summary = str(mission.get("plan_summary") or "") + "\n修復ループの承認済み局所パッチを適用。内容は再検証待ち。"
        manager.memory.replace_plan(
            project_id, summary, tasks, expected_version=mission.get("plan_version")
        )
        from app.goal_review import plan_snapshot as _snapshot

        new_signature = _snapshot(manager, project_id)[1]
    run["patched_signature"] = str(new_signature)
    run["fact_answers"] = fact_answers
    run["applied"] = applied
    evidence = dict(run.get("evidence") or {})
    evidence["patched"] = {
        "actor": actor,
        "applied": applied,
        "approved_count": len(approved_map),
        "answered_count": len(fact_answers),
        "new_signature": str(new_signature),
        "preserve_existing_artifacts": True,
        "recreate_published_assets": False,
    }
    run["evidence"] = evidence
    return run


def _do_structure_checked(manager, project_id: str, run: dict, actor: str) -> dict:
    mission, signature, contract = _check_pinned(manager, project_id, run)
    result = _run_structure_check(mission, contract)
    if not result.get("passed"):
        raise ValueError("STRUCTURE_FAILED: " + "; ".join(result.get("issues") or ["構造検査不合格"])[:2000])
    evidence = dict(run.get("evidence") or {})
    evidence["structure_checked"] = {
        "actor": actor, "signature": signature,
        "criterion_ids": result.get("criterion_ids") or [],
        "passed": True,
    }
    run["evidence"] = evidence
    return run


def _do_coverage_checked(manager, project_id: str, run: dict, actor: str) -> dict:
    mission, signature, contract = _check_pinned(manager, project_id, run)
    result = _run_coverage_check(mission, contract)
    coverage = result["coverage"]
    retention = result["retention"]
    problems = []
    if not coverage.get("passed"):
        problems.append("COVERAGE_INCOMPLETE: " + "; ".join(coverage.get("issues") or ["被覆不合格"]))
    if not retention.get("passed"):
        problems.append("RETENTION_FAILED: " + ",".join(retention.get("missing_topics") or ["要求保持不合格"]))
    if problems:
        raise ValueError(" / ".join(problems)[:2000])
    evidence = dict(run.get("evidence") or {})
    evidence["coverage_checked"] = {
        "actor": actor, "signature": signature,
        "contract_hash": coverage.get("contract_hash") or "",
        "retention": {"passed": retention.get("passed"), "missing_topics": retention.get("missing_topics") or []},
        "passed": True,
    }
    run["evidence"] = evidence
    return run


async def _do_external_reviewed(manager, project_id: str, run: dict, payload: dict, actor: str) -> dict:
    _mission, signature, _contract = _check_pinned(manager, project_id, run)
    if payload.get("review_unavailable") is True:
        reason = str(payload.get("reason") or "").strip()
        if not reason:
            raise ValueError("review_unavailable には reason が必須です")
        evidence = dict(run.get("evidence") or {})
        evidence["external_reviewed"] = {
            "actor": actor, "signature": signature,
            "status": "review_unavailable",
            "outcome": "review_unavailable",
            "stop_reason": reason[:1000],
            "public_summary_chars": 0,
            "sent_private_originals": False,
            "connection_error": False,
            "note": "外部AI内容レビュー不可のため承認を通さない",
        }
        run["evidence"] = evidence
        return run
    summary = str(payload.get("public_summary") or "").strip()
    safe_to_send = payload.get("safe_to_send") is True
    # P0: 明示の外部AI調査指示と安全な送信内容がある場合だけ実行する。
    if not safe_to_send or not 20 <= len(summary) <= 12000:
        raise ValueError("公開用要約(20〜12000文字)と明示の送信承認が必要です。私的原本・RAG事例本文は送りません")
    if int(run.get("external_calls") or 0) >= MAX_EXTERNAL_CALLS:
        run["state"] = "blocked"
        run["evidence"] = {**(run.get("evidence") or {}), "blocked": {
            "actor": actor, "reason": f"外部呼び出し回数が上限({MAX_EXTERNAL_CALLS})に達しました"}}
        _save(manager, run)
        raise ValueError(f"外部呼び出し回数が上限({MAX_EXTERNAL_CALLS})に達したため blocked にしました")
    from app.goal_review import review_budget, review_plan

    try:
        budget = review_budget(manager, project_id)
    except Exception:
        budget = {"managed": False}
    if budget.get("managed") and int(budget.get("remaining_calls") or 0) <= 0:
        run["state"] = "blocked"
        run["evidence"] = {**(run.get("evidence") or {}), "blocked": {
            "actor": actor, "reason": "外部検証の利用枠(費用上限)に達したため blocked にしました"}}
        _save(manager, run)
        raise ValueError("外部検証の利用枠(費用上限)に達したため blocked にしました")
    # 既存の外部レビュー経路を使い、送るのは公開用要約のみ(原本・RAG本文は送らない)。
    result = await review_plan(manager, project_id, signature, summary, True)
    status = str(result.get("status") or "")
    if status == "passed":
        outcome = "content_pass"
    elif status == "not_passed":
        outcome = "content_fail"
    elif status in {"connection_failed", "awaiting_external"}:
        outcome = "connection"
    else:
        outcome = "unavailable"
    run["external_calls"] = int(run.get("external_calls") or 0) + 1
    evidence = dict(run.get("evidence") or {})
    evidence["external_reviewed"] = {
        "actor": actor, "signature": signature,
        "status": status,
        "outcome": outcome,
        "success_count": result.get("success_count"),
        "required_count": result.get("required_count"),
        "stop_reason": str(result.get("stop_reason") or "")[:1000],
        "public_summary_chars": len(summary),
        "sent_private_originals": False,
        "connection_error": outcome in {"connection", "unavailable"},
    }
    run["evidence"] = evidence
    return run


def _do_rediff_evaluated(manager, project_id: str, run: dict, actor: str) -> dict:
    _mission, signature, _contract = _check_pinned(manager, project_id, run)
    rediff = _current_unresolved(manager, project_id, run, signature)
    run["unresolved_count"] = int(rediff["unresolved_count"])
    evidence = dict(run.get("evidence") or {})
    evidence["rediff_evaluated"] = {"actor": actor, "signature": signature, **{
        k: v for k, v in rediff.items() if k != "current_preview"},
        "current_classifications": rediff["current_preview"]["classifications"],
        "current_unbound": rediff["current_preview"]["unbound"],
    }
    run["evidence"] = evidence
    return run


def answer_business_fact(manager, project_id: str, run_id: str, issue_id: str,
                           answer: str, actor: str) -> dict:
    """P0: business_fact への人間回答を記録する。推測での埋め込みは行わない。"""
    if not str(actor or "").strip():
        raise ValueError("actor is required")
    text = str(answer or "").strip()
    if not text:
        raise ValueError("回答内容が必要です")
    if len(text) > 2000:
        raise ValueError("回答は2000文字以内です")
    run = get_run(manager, project_id, run_id)
    if str(run.get("state") or "") in TERMINAL:
        raise ValueError(f"repair run is terminal: {run.get('state')}")
    target = None
    for cand in run.get("candidates") or []:
        if str(cand.get("issue_id") or "") == str(issue_id or ""):
            target = cand
            break
    if target is None:
        raise ValueError(f"unknown issue_id: {issue_id}")
    if target.get("classification") != "business_fact":
        raise ValueError("business_fact の指摘にだけ回答できます")
    answers = dict(run.get("fact_answers") or {})
    answers[str(issue_id)] = text
    run["fact_answers"] = answers
    evidence = dict(run.get("evidence") or {})
    log = list(evidence.get("fact_answer_log") or [])
    log.append({"actor": str(actor).strip(), "issue_id": str(issue_id),
                "at": _now(), "chars": len(text)})
    evidence["fact_answer_log"] = log[-50:]
    run["evidence"] = evidence
    _save(manager, run)
    return public_view(run)


def _ja_state_label(state: str) -> str:
    return {
        "proposed": "対応づけ済み・未着手",
        "patched": "修復案を適用・再検証待ち",
        "structure_checked": "構造検査済み",
        "coverage_checked": "被覆検査済み",
        "external_reviewed": "内容レビュー済み",
        "rediff_evaluated": "再差分を評価済み",
        "passed": "合格（承認は人間）",
        "failed": "不合格",
        "blocked": "停止中",
    }.get(str(state or ""), str(state or ""))


def repair_cards(manager, project_id: str) -> dict:
    """P0: 案件画面向けの日本語カード。読み取り専用。JSONだけを見せない方針の材料。"""
    try:
        mission, signature, contract = _current_context(manager, project_id)
    except Exception:
        return {"runs": [], "cards": []}
    criteria = {str(x.get("criterion_id") or ""): str(x.get("statement") or "")
                for x in (contract.get("criteria") or []) if x.get("criterion_id")}
    tasks = {str(t.get("task_key") or ""): str(t.get("title") or "")
             for t in (mission.get("tasks") or []) if t.get("task_key")}
    rediff_by_run: dict[str, dict] = {}
    for run in list_runs(manager, project_id):
        rediff_by_run[str(run.get("id"))] = dict(
            ((run.get("evidence") or {}).get("rediff_evaluated") or {}))
    cards = []
    runs = []
    for run in list_runs(manager, project_id):
        if str(run.get("goal_contract_hash") or "") != str(contract.get("content_hash") or ""):
            continue
        if str(run.get("plan_signature") or "") != signature and str(
                run.get("patched_signature") or "") != signature:
            continue
        runs.append(public_view(run))
        answers = run.get("fact_answers") or {}
        rediff = rediff_by_run.get(str(run.get("id")), {})
        remaining = set(rediff.get("remaining") or []) | set(rediff.get("new") or [])
        lingering = set(rediff.get("lingering_originals") or [])
        unanswered = set(rediff.get("unanswered_business_facts") or [])
        external = (run.get("evidence") or {}).get("external_reviewed") or {}
        for cand in run.get("candidates") or []:
            iid = str(cand.get("issue_id") or "")
            cid = str(cand.get("criterion_id") or "")
            tkey = str(cand.get("task_key") or "")
            kind = str(cand.get("classification") or "")
            bound = bool(cand.get("bound"))
            if kind == "business_fact" and not str(answers.get(iid) or "").strip():
                result = "回答待ち（未解決）"
            elif not bound or kind == "development_required":
                result = "未結合・要実装のため未解決"
            elif iid in remaining or iid in lingering or iid in unanswered:
                result = "再評価で未解決"
            elif external.get("outcome") == "review_unavailable":
                result = "レビュー不可のため承認不可"
            elif external.get("outcome") != "content_pass":
                result = "内容レビュー待ち"
            else:
                result = "対応づけ済み・再検証待ち"
            if kind == "business_fact":
                nxt = "人間が適用条件と判断根拠を回答する"
                need = str(cand.get("question") or "") or "適用条件と判断根拠の回答が必要"
            elif not bound:
                nxt = "セバスが現行条件・工程への再結合を試みる"
                need = "現行GoalContractに結び付く根拠の整理が必要"
            elif kind == "development_required":
                nxt = "セバスが実装可否と試験範囲を整理する"
                need = "実装・受入テスト方針の人間判断が必要"
            else:
                nxt = "セバスが同一署名で再検証へ進める"
                need = "人間承認（reviewer・reason）と再検証結果の確認が必要"
            before = (cand.get("artifact_diff") or {}).get("before") or {}
            after = (cand.get("artifact_diff") or {}).get("after") or {}
            tried = "差分あり" if before != after else "差分なし（要確認）"
            cards.append({
                "run_id": str(run.get("id")),
                "run_state": _ja_state_label(str(run.get("state") or "")),
                "issue_id": iid,
                "issue_excerpt": str(cand.get("issue_excerpt") or cand.get("basis") or "")[:600],
                "対象条件": (cid + " " + criteria.get(cid, "")).strip() or "未結合",
                "停止理由": ("未結合のため承認・開始不可" if not bound
                             else "業務事実の回答待ち" if kind == "business_fact" and result.startswith("回答待ち")
                             else "内容レビュー・再差分の確認待ち"),
                "確認した証拠": "指摘ID=" + iid + " / 条件ID=" + (cid or "なし") + " / 工程=" + (tkey or "なし") +
                    " / 署名=" + str(cand.get("plan_signature") or run.get("plan_signature") or "")[:16] +
                    " / 契約=" + str(cand.get("contract_hash") or "")[:16],
                "試した処置": tried,
                "セバスが次に行えること": nxt,
                "人間に必要な判断": need,
                "再評価結果": result,
                "技術試験と業務達成の区別": "技術試験（構造・被覆・内容再レビュー）と業務達成（未解決0・回答済み・人間承認）は別に判定する",
            })
    return {"runs": runs, "cards": cards}


def _evaluate_pass(manager, project_id: str, run: dict) -> list[str]:
    """合格条件を全てサーバー側で再検査する。空リスト=合格。"""
    reasons: list[str] = []
    try:
        mission, signature, contract = _check_pinned(manager, project_id, run)
    except ValueError as exc:
        return [str(exc)]
    structure = _run_structure_check(mission, contract)
    if not structure.get("passed"):
        reasons.append("STRUCTURE_FAILED: " + "; ".join(structure.get("issues") or ["構造検査不合格"]))
    checked = _run_coverage_check(mission, contract)
    if not checked["coverage"].get("passed"):
        reasons.append("COVERAGE_INCOMPLETE: " + "; ".join(checked["coverage"].get("issues") or ["被覆不合格"]))
    if not checked["retention"].get("passed"):
        reasons.append("RETENTION_FAILED: " + ",".join(checked["retention"].get("missing_topics") or ["要求保持不合格"]))
    from app.goal_review import ReviewStore

    row = ReviewStore(manager.memory.path).get(project_id, "plan", signature) or {}
    external = ((run.get("evidence") or {}).get("external_reviewed") or {})
    if not (row.get("status") == "passed" and external.get("outcome") == "content_pass"
            and external.get("signature") == signature):
        reasons.append(
            "EXTERNAL_NOT_PASSED: 同一署名の内容レビュー合格が必要です "
            f"(review={row.get('status') or 'missing'}, outcome={external.get('outcome') or 'missing'})"
        )
    rediff = _current_unresolved(manager, project_id, run, signature)
    if rediff["remaining"] or rediff["new"]:
        reasons.append("UNRESOLVED_ISSUES: 未解決指摘が残っています: " + ",".join(
            (rediff["remaining"] + rediff["new"])[:8]))
    if rediff["lingering_originals"]:
        reasons.append("UNRESOLVED_ORIGINALS: 要実装・未結合の指摘が残っています: " + ",".join(
            rediff["lingering_originals"][:8]))
    if rediff["unanswered_business_facts"]:
        reasons.append("BUSINESS_FACT_UNANSWERED: 業務事実の限定質問に未回答があります: " + ",".join(
            rediff["unanswered_business_facts"][:8]))
    try:
        from app.completion_replay import preview as replay_preview

        replay = replay_preview(manager, project_id)
        if replay.get("crossed_approval_boundary"):
            reasons.append("REPLAY_CROSSED_BOUNDARY: 完走プレビューで承認境界を越えています")
    except Exception as exc:
        reasons.append("REPLAY_UNAVAILABLE: 完走プレビューを確認できません: " + type(exc).__name__)
    if int(run.get("external_calls") or 0) > MAX_EXTERNAL_CALLS:
        reasons.append(f"EXTERNAL_BUDGET_EXCEEDED: 上限({MAX_EXTERNAL_CALLS})超過")
    return reasons


async def advance(
    manager,
    project_id: str,
    run_id: str,
    next_state: str,
    actor: str,
    payload: dict | None = None,
) -> dict:
    """状態遷移(検証付き)。自己申告の合格値は信用せずサーバー側で再検査する。"""
    if not str(actor or "").strip():
        raise ValueError("actor is required")
    payload = dict(payload or {})
    run = get_run(manager, project_id, run_id)
    current = str(run.get("state") or "")
    target = str(next_state or "").strip()
    if current in TERMINAL:
        raise ValueError(f"repair run is terminal: {current}")
    allowed = list(NEXT.get(current, ()))
    if target in ("blocked", "failed") and current not in TERMINAL:
        allowed = allowed + [t for t in ("blocked", "failed") if t not in allowed]
    if target not in allowed:
        raise ValueError(f"invalid repair transition: {current} -> {target or '(empty)'}")
    if int(run.get("advances") or 0) >= MAX_ADVANCES and target not in ("blocked", "failed"):
        run["state"] = "blocked"
        run["evidence"] = {**(run.get("evidence") or {}), "blocked": {
            "actor": str(actor).strip(), "reason": f"生成回数が上限({MAX_ADVANCES})に達しました"}}
        _save(manager, run)
        raise ValueError(f"生成回数が上限({MAX_ADVANCES})に達したため blocked にしました")
    actor_name = str(actor).strip()
    if target == "patched":
        run = _do_patched(manager, project_id, run, payload, actor_name)
    elif target == "structure_checked":
        run = _do_structure_checked(manager, project_id, run, actor_name)
    elif target == "coverage_checked":
        run = _do_coverage_checked(manager, project_id, run, actor_name)
    elif target == "external_reviewed":
        run = await _do_external_reviewed(manager, project_id, run, payload, actor_name)
    elif target == "rediff_evaluated":
        run = _do_rediff_evaluated(manager, project_id, run, actor_name)
    elif target == "passed":
        reasons = _evaluate_pass(manager, project_id, run)
        if reasons:
            raise ValueError("REPAIR_NOT_PASSED: " + " / ".join(reasons)[:2000])
        evidence = dict(run.get("evidence") or {})
        evidence["passed"] = {
            "actor": actor_name, "signature": _expected_signature(run),
            "note": "修復ループ合格。計画の承認自体は人間が既存ゲートで行う(自動承認しない)",
        }
        run["evidence"] = evidence
        run["unresolved_count"] = 0
    elif target in ("blocked", "failed"):
        reason = str(payload.get("reason") or "").strip()
        if not reason:
            raise ValueError("blocked/failed には reason が必須です")
        evidence = dict(run.get("evidence") or {})
        evidence[target] = {"actor": actor_name, "reason": reason[:2000]}
        run["evidence"] = evidence
    else:
        raise ValueError(f"unknown repair state: {target}")
    run["state"] = target
    run["advances"] = int(run.get("advances") or 0) + 1
    _save(manager, run)
    return public_view(run)


def repair_gate_summary(manager, project_id: str) -> dict:
    """既存ゲートから参照する修復ループの要約(読み取り専用)。"""
    try:
        _mission, signature, contract = _current_context(manager, project_id)
    except Exception:
        return {"blocking": False, "reason": ""}
    open_runs = []
    for run in list_runs(manager, project_id):
        if str(run.get("state") or "") in TERMINAL:
            continue
        if str(run.get("plan_signature") or "") != signature and str(
                run.get("patched_signature") or "") != signature:
            continue
        if str(run.get("goal_contract_hash") or "") != str(contract.get("content_hash") or ""):
            continue
        open_runs.append(run.get("id"))
    if not open_runs:
        return {"blocking": False, "reason": ""}
    return {
        "blocking": True,
        "open_runs": open_runs,
        "reason": "修復ループ未完了のため計画承認・実行開始できません。"
                  "未解決指摘0・同一署名の内容レビュー合格・人間承認がそろうまでお待ちください",
    }


def approval_gate(manager, project_id: str) -> None:
    """既存の計画承認/実行開始の判定へ接続する。弱めない方向(追加ブロック)のみ。"""
    try:
        summary = repair_gate_summary(manager, project_id)
    except Exception:
        return
    if summary.get("blocking"):
        raise ValueError(str(summary.get("reason") or "修復ループ未完了のため実行できません"))
