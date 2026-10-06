"""P2: 薄い解決コーディネータ(原因と案内)。

completion_gate の未達条件から、被覆工程・実行履歴・原本・成果物・外部操作証拠へ
逆引きして残件ごとに原因を診断し、次に行える操作を案内する。
既存の状態機械は二重実装しない。計画矛盾は plan_repair_loop(P1-C)へ、
実行失敗は recovery_record(P3)へ引き渡す(案内であって実行ではない)。
このモジュールが持つ状態は、残件追跡用の参照と診断根拠だけである。

状態: detected → diagnosed → candidate_ready → trial_passed → awaiting_approval
      → executing → verifying → resolved、分岐 blocked / rejected / stale / failed。
順序を飛ばせない。遷移はサーバー側で検証し、自己申告の合格値は信用しない。
unknown は成功扱いにしない。plan_conflict を TRIZ の一般候補生成へ回さない。

detect / 診断は読み取り・追跡のみで、承認・実行・外部操作・RAG登録を一切行わない。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

CAUSES = (
    "source_unreadable",
    "business_fact_missing",
    "plan_conflict",
    "execution_failure",
    "artifact_mismatch",
    "external_dependency",
    "approval_wait",
    "goal_evidence_missing",
    "unknown",
)

CAUSE_LABEL_JA = {
    "source_unreadable": "原本が読み取れない",
    "business_fact_missing": "業務事実が不足している",
    "plan_conflict": "計画に矛盾がある",
    "execution_failure": "実行に失敗している",
    "artifact_mismatch": "成果物が一致しない",
    "external_dependency": "外部操作の証拠待ち",
    "approval_wait": "人間の承認待ち",
    "goal_evidence_missing": "達成証拠が不足している",
    "unknown": "未診断",
}

ORDER = (
    "detected",
    "diagnosed",
    "candidate_ready",
    "trial_passed",
    "awaiting_approval",
    "executing",
    "verifying",
    "resolved",
)

TERMINAL = frozenset({"blocked", "rejected", "stale", "failed"})

STATE_LABEL_JA = {
    "detected": "検出済み",
    "diagnosed": "診断済み",
    "candidate_ready": "対応案内済み",
    "trial_passed": "技術試験合格",
    "awaiting_approval": "人間承認待ち",
    "executing": "対応実行中",
    "verifying": "再評価中",
    "resolved": "解決済み",
    "blocked": "停止中",
    "rejected": "却下",
    "stale": "旧版のため失効",
    "failed": "失敗",
}

MAX_ATTEMPTS = 10


def _db_path(manager) -> Path:
    source = Path(manager.memory.path)
    return source.with_name(source.name + ".resolution.sqlite3")


@contextmanager
def _connect(manager):
    db = sqlite3.connect(_db_path(manager), timeout=15)
    db.row_factory = sqlite3.Row
    db.execute(
        """CREATE TABLE IF NOT EXISTS resolutions(
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            criterion_id TEXT NOT NULL,
            task_key TEXT NOT NULL,
            failure_id TEXT NOT NULL,
            contract_hash TEXT NOT NULL,
            plan_signature TEXT NOT NULL,
            input_version TEXT NOT NULL,
            source_hash TEXT NOT NULL,
            artifact_hash TEXT NOT NULL,
            cause TEXT NOT NULL,
            evidence TEXT NOT NULL DEFAULT '{}',
            missing_evidence TEXT NOT NULL DEFAULT '[]',
            next_action TEXT NOT NULL DEFAULT '{}',
            rag_refs TEXT NOT NULL DEFAULT '[]',
            rag_rejection TEXT NOT NULL DEFAULT '',
            triz_info TEXT NOT NULL DEFAULT '{}',
            repair_run_id TEXT NOT NULL DEFAULT '',
            p2_run_id TEXT NOT NULL DEFAULT '',
            recovery_id TEXT NOT NULL DEFAULT '',
            approval TEXT NOT NULL DEFAULT '{}',
            reevaluation TEXT NOT NULL DEFAULT '{}',
            state TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            history TEXT NOT NULL DEFAULT '[]',
            created REAL NOT NULL,
            updated REAL NOT NULL,
            UNIQUE(project_id, failure_id, plan_signature))"""
    )
    db.execute(
        """CREATE INDEX IF NOT EXISTS idx_resolutions_project
           ON resolutions(project_id, plan_signature)"""
    )
    try:
        with db:
            yield db
    finally:
        db.close()


def _read_connection(manager):
    path = _db_path(manager)
    if not path.exists():
        return None
    db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=15)
    db.row_factory = sqlite3.Row
    return db


def _decode(row: sqlite3.Row) -> dict:
    out = dict(row)
    for key in ("evidence", "next_action", "triz_info", "approval", "reevaluation"):
        try:
            out[key] = json.loads(out[key] or "{}")
        except (TypeError, ValueError):
            out[key] = {}
    for key in ("missing_evidence", "rag_refs", "history"):
        try:
            out[key] = json.loads(out[key] or "[]")
        except (TypeError, ValueError):
            out[key] = []
    out["attempts"] = int(out.get("attempts") or 0)
    return out


def _short(value: str, length: int = 16) -> str:
    return str(value or "")[:length]


def public_view(row: dict) -> dict:
    """案件画面・API向けの公開形。原文の切り詰め済み要約だけを返す。"""
    return {
        "id": row.get("id"),
        "project_id": row.get("project_id"),
        "criterion_id": row.get("criterion_id"),
        "task_key": row.get("task_key"),
        "failure_id": row.get("failure_id"),
        "contract_hash": row.get("contract_hash"),
        "plan_signature": row.get("plan_signature"),
        "input_version": row.get("input_version"),
        "source_hash": row.get("source_hash"),
        "artifact_hash": row.get("artifact_hash"),
        "cause": row.get("cause"),
        "cause_ja": CAUSE_LABEL_JA.get(str(row.get("cause") or ""), "未診断"),
        "evidence": dict(row.get("evidence") or {}),
        "missing_evidence": list(row.get("missing_evidence") or []),
        "next_action": dict(row.get("next_action") or {}),
        "rag_refs": list(row.get("rag_refs") or []),
        "rag_rejection": str(row.get("rag_rejection") or ""),
        "triz_info": dict(row.get("triz_info") or {}),
        "repair_run_id": str(row.get("repair_run_id") or ""),
        "p2_run_id": str(row.get("p2_run_id") or ""),
        "recovery_id": str(row.get("recovery_id") or ""),
        "approval": dict(row.get("approval") or {}),
        "reevaluation": dict(row.get("reevaluation") or {}),
        "state": row.get("state"),
        "state_ja": STATE_LABEL_JA.get(str(row.get("state") or ""), str(row.get("state") or "")),
        "attempts": int(row.get("attempts") or 0),
        "max_attempts": MAX_ATTEMPTS,
        "history": list(row.get("history") or []),
        "created": row.get("created"),
        "updated": row.get("updated"),
    }


def _current_context(manager, project_id: str) -> tuple[dict, str, dict, dict]:
    """現行の mission・計画署名・GoalContract・ハッシュを返す(実在関数のみ)。"""
    from app.completion_gate import _current_hashes
    from app.goal_contract import get_active, preview
    from app.goal_review import plan_snapshot

    mission = manager.memory.get_mission(project_id)
    _snapshot, signature = plan_snapshot(manager, project_id)
    contract = get_active(manager, project_id) or preview(manager, project_id) or {}
    hashes = _current_hashes(manager, project_id, contract, mission)
    return mission, str(signature or ""), contract, {k: str(v or "") for k, v in (hashes or {}).items()}


def _safe_ledger(manager, project_id: str) -> dict:
    try:
        from app.pending_ledger import build as ledger_build

        return ledger_build(manager, project_id) or {}
    except Exception:
        return {"items": [], "summary": {}}


def _safe_external_evidence(manager, project_id: str) -> dict:
    try:
        from app.generic_goal_checks import collect_external_evidence

        return collect_external_evidence(manager, project_id) or {}
    except Exception:
        return {"actions": [], "executed": [], "pending": [], "operations": []}


def _safe_sources_readable(manager, project_id: str, mission: dict) -> bool:
    try:
        from app.vehicle_workflow import digest, sources

        digest(sources(manager, project_id))
        return True
    except Exception:
        return False


def _artifact_presence(manager, project_id: str, outputs: list[dict]) -> tuple[list[str], list[str]]:
    present: list[str] = []
    missing: list[str] = []
    workspace = getattr(manager, "workspace", None)
    project = {}
    try:
        project = manager.memory.get_project(project_id) or {}
    except Exception:
        project = {}
    for output in outputs or []:
        path = str((output or {}).get("path") or "")
        if not path:
            continue
        try:
            if workspace is None:
                raise ValueError("workspace unavailable")
            try:
                _a, _b, target = workspace.resolve_file(
                    project.get("workspace_path", ""), project_id, path, must_exist=True
                )
            except TypeError:
                _a, _b, target = workspace.resolve_file(
                    project.get("workspace_path", ""), project_id, path
                )
            if target.is_file() and target.stat().st_size > 0:
                present.append(path)
            else:
                missing.append(path)
        except Exception:
            missing.append(path)
    return present, missing


def _evidence_for_criterion(
    manager,
    project_id: str,
    mission: dict,
    gate_row: dict,
    coverage_by_cid: dict,
    ledger: dict,
    auto_runs: list[dict],
    actions: list[dict],
    external: dict,
    hashes: dict,
    sources_readable: bool,
) -> dict:
    """1つの未達条件について、確認した証拠だけを集める(読み取り専用)。"""
    cid = str(gate_row.get("criterion_id") or "")
    coverage = dict(coverage_by_cid.get(cid) or {})
    task_key = str(coverage.get("exec_task_key") or "")
    task = {}
    for item in mission.get("tasks") or []:
        if str(item.get("task_key") or "") == task_key:
            task = item
            break
    from app.structured_planning import contract_of

    outputs = list((contract_of(task) or {}).get("outputs") or []) if task else []
    present, missing = _artifact_presence(manager, project_id, outputs)
    failed_runs = [
        x for x in (auto_runs or [])
        if str(x.get("task_key") or "") == task_key and str(x.get("status") or "") == "failed"
    ]
    completed_runs = [
        {"run_id": str(x.get("run_id") or ""), "artifact_hash": str(x.get("artifact_hash") or "")}
        for x in (auto_runs or [])
        if str(x.get("task_key") or "") == task_key and str(x.get("status") or "") == "completed"
    ]
    ledger_summary = dict((ledger or {}).get("summary") or {})
    related_items = []
    for item in (ledger or {}).get("items") or []:
        basis = str((item or {}).get("basis") or "")
        target = str((item or {}).get("target") or "")
        if cid and (cid in basis or cid in target):
            related_items.append({
                "kind": str(item.get("kind") or ""),
                "target": target,
                "state": str(item.get("state") or ""),
            })
    pending_actions = [x for x in (actions or []) if str(x.get("status") or "") == "pending_approval"]
    approved_waiting = [x for x in (actions or []) if str(x.get("status") or "") == "approved"]
    criterion_issues: list[dict] = []
    try:
        from app.plan_feedback import issues_for

        for issue in issues_for(manager, project_id, hashes.get("plan_signature") or "") or []:
            if not isinstance(issue, dict):
                continue
            text = str(issue.get("text") or issue.get("reason") or "")
            if str(issue.get("criterion") or "") == cid or (cid and cid in text):
                criterion_issues.append({
                    "id": str(issue.get("id") or issue.get("issue_id") or ""),
                    "provider": str(issue.get("provider") or ""),
                    "excerpt": text[:200],
                })
    except Exception:
        criterion_issues = []
    store = None
    plan_review = ""
    result_state = ""
    revision_blockers: list[dict] = []
    try:
        from app.goal_review import ReviewStore

        store = ReviewStore(manager.memory.path)
        plan_row = store.get(project_id, "plan", hashes.get("plan_signature") or "") or {}
        plan_review = str(plan_row.get("status") or "")
        revision = store.get(project_id, "revision", hashes.get("plan_signature") or "") or {}
        for blocker in revision.get("blockers") or []:
            if isinstance(blocker, dict):
                revision_blockers.append({
                    "disposition": str(blocker.get("disposition") or ""),
                    "reason": str(blocker.get("reason") or "")[:300],
                })
    except Exception:
        store = None
    return {
        "gate": {
            "status": str(gate_row.get("status") or ""),
            "reason_code": str(gate_row.get("reason_code") or ""),
            "message": str(gate_row.get("message") or "")[:300],
            "evidence_path": str(gate_row.get("evidence_path") or ""),
        },
        "coverage": {
            "status": str(coverage.get("status") or ""),
            "exec_task_key": task_key,
            "verify_task_key": str(coverage.get("verify_task_key") or ""),
        },
        "task": {"task_key": task_key, "status": str((task or {}).get("status") or "")},
        "auto_resume": {"failed": bool(failed_runs), "failed_count": len(failed_runs),
                        "completed_runs": completed_runs[:5]},
        "ledger": {
            "issue_count": ledger_summary.get("issue_count"),
            "unresolved_count": ledger_summary.get("unresolved_count"),
            "business_fact_count": ledger_summary.get("business_fact_count"),
            "plan_status": str(ledger_summary.get("plan_status") or ""),
            "related": related_items[:5],
        },
        "revision_blockers": revision_blockers[:8],
        "criterion_issues": criterion_issues[:5],
        "source": {"source_hash": _short(hashes.get("source_hash")),
                   "readable": bool(sources_readable)},
        "artifact": {"expected": [str(o.get("path") or "") for o in outputs][:8],
                     "present": present[:8], "missing": missing[:8],
                     "artifact_hash": _short(hashes.get("artifact_hash"))},
        "external": {
            "pending_approval": len(pending_actions),
            "approved_waiting": len(approved_waiting),
            "executed": len(external.get("executed") or []),
            "operations": len(external.get("operations") or []),
        },
        "approval": {"plan_review": plan_review, "result_state": result_state},
    }


def _classify_cause(evidence: dict) -> tuple[str, list[str], str]:
    """証拠からのみ原因を分類する。戻り値: (cause, 不足証拠[日本語], 診断根拠[日本語])。"""
    gate = evidence.get("gate") or {}
    reason = str(gate.get("reason_code") or "")
    status = str(gate.get("status") or "")
    coverage = evidence.get("coverage") or {}
    task = evidence.get("task") or {}
    auto = evidence.get("auto_resume") or {}
    ledger = evidence.get("ledger") or {}
    blockers = list(evidence.get("revision_blockers") or [])
    source = evidence.get("source") or {}
    artifact = evidence.get("artifact") or {}
    external = evidence.get("external") or {}

    if str(coverage.get("status") or "") == "uncovered":
        return ("plan_conflict", ["被覆工程(実行・検証タスクの対応付け)"],
                "計画被覆が無いため、P1-Cで現行条件への再結合が必要です")

    business = [b for b in blockers if b.get("disposition") == "business_fact"]
    try:
        ledger_business = int(ledger.get("business_fact_count") or 0)
    except (TypeError, ValueError):
        ledger_business = 0
    if business or ledger_business > 0:
        reason_text = str((business[0] if business else {}).get("reason") or "") or "業務事実の確認"
        return ("business_fact_missing", ["業務事実の回答: " + reason_text[:200]],
                "業務事実の確認が残っているため、人間の回答が必要です")

    development = [b for b in blockers
                   if b.get("disposition") in {"development", "unresolved"}]
    try:
        ledger_unresolved = int(ledger.get("unresolved_count") or 0)
    except (TypeError, ValueError):
        ledger_unresolved = 0
    ledger_issues = ledger.get("issue_count")
    try:
        ledger_issues = int(ledger_issues) if ledger_issues is not None else 0
    except (TypeError, ValueError):
        ledger_issues = 0
    if development or ledger_unresolved > 0 or ledger_issues > 0 or ledger.get("related"):
        criterion_issues = list(evidence.get("criterion_issues") or [])
        if development or ledger_unresolved > 0 or ledger.get("related") or criterion_issues:
            return ("plan_conflict", ["未解決指摘の解消(P1-C修復ラン)"],
                    "未解決の指摘・修正案があるため、P1-Cで局所修復が必要です")

    if str(task.get("status") or "") in {"failed", "needs_review"} or bool(auto.get("failed")):
        return ("execution_failure", ["失敗した実行の再試行証拠(P3回復記録)"],
                "実行履歴に失敗があるため、P3で回復手順へ引き渡します")

    if not source.get("source_hash") or source.get("readable") is False:
        return ("source_unreadable", ["原本の再登録またはOCR確認結果"],
                "原本を読み取れないため、原本・OCRの確認が必要です")

    if reason in {"ARTIFACT_MISSING", "ARTIFACT_FORMAT", "ARTIFACT_HASH_DRIFT"} or (
            artifact.get("missing") and status in {"FAIL", "BLOCKED"}):
        missing = list(artifact.get("missing") or [])[:3]
        return ("artifact_mismatch", ["成果物: " + ", ".join(missing) if missing else "成果物の再作成"],
                "必須成果物が不足・不一致のため、成果物の確認が必要です")

    if reason in {"VERIFICATION_MISSING", "NO_EXEC_TASK", "UNTESTABLE", "CHECK_UNIMPLEMENTED"} or status in {
            "UNTESTABLE", "BLOCKED"} and reason in {"VERIFICATION_MISSING", "TASK_INCOMPLETE"}:
        return ("goal_evidence_missing", ["最終検証の評価結果"],
                "最終検証が未完了のため、達成証拠が不足しています")

    if reason in {"EXTERNAL_ACTION_MISSING", "TRIAL_EVIDENCE_MISSING"} or status in {"FAIL", "BLOCKED"}:
        try:
            pending = int(external.get("pending_approval") or 0)
        except (TypeError, ValueError):
            pending = 0
        if pending > 0:
            return ("approval_wait", [f"外部操作の人間承認({pending}件)"],
                    "外部操作が承認待ちのため、人間の確認が必要です")
        return ("external_dependency", ["承認済み外部操作の実行証拠"],
                "外部操作の実行証拠が無いため、人間の実施・確認が必要です")

    if reason in {"HUMAN_ACCEPTANCE_MISSING", "HUMAN_ACCEPTANCE_HASH_DRIFT"}:
        return ("approval_wait", ["人間による結果確認"],
                "結果の人間確認が無いため、承認待ちとして表示します")

    return ("unknown", ["診断に足りる証拠"],
            "未診断: 既知の分類に当てはまりません。成功扱いにしません")


def _route_for(project_id: str, cause: str, evidence: dict,
               missing: list[str], basis: str) -> dict:
    """振り分け(案内であって実行ではない)。既知の計画矛盾をTRIZへ回さない。"""
    pid = str(project_id or "")
    if cause == "plan_conflict":
        return {
            "route": "plan_repair",
            "label_ja": "P1-C計画修復ランへ引き渡し",
            "detail_ja": "計画矛盾のため、P1-C修復ランで局所修復へ進めます。"
                         "TRIZの一般候補生成へは回しません。",
            "human_ja": "人間が修復ランの開始と承認を行います",
            "question_ja": "",
            "triz_skipped": True,
            "triz_skip_reason": "既知の計画矛盾はTRIZで隠さない",
            "references": {"repair_runs": f"/api/projects/{pid}/plan/repair-runs/start"},
            "missing_evidence": list(missing),
            "basis_ja": basis,
        }
    if cause == "execution_failure":
        return {
            "route": "recovery",
            "label_ja": "P3回復記録へ引き渡し",
            "detail_ja": "実行失敗のため、P3回復記録で回復手順へ進めます。",
            "human_ja": "人間が回復手順の確認と承認を行います",
            "question_ja": "",
            "triz_skipped": False,
            "triz_skip_reason": "",
            "references": {"recovery": f"/api/projects/{pid}/recovery/start"},
            "missing_evidence": list(missing),
            "basis_ja": basis,
        }
    if cause == "source_unreadable":
        return {
            "route": "inspect_source",
            "label_ja": "原本・OCR確認へ案内",
            "detail_ja": "原本が読み取れないため、原本の再登録またはOCR確認結果を用意します。",
            "human_ja": "人間が原本の用意と確認を行います",
            "question_ja": "対象の原本を用意し、読み取れる状態にしてください。",
            "triz_skipped": False,
            "triz_skip_reason": "",
            "references": {},
            "missing_evidence": list(missing),
            "basis_ja": basis,
        }
    if cause == "business_fact_missing":
        question = str((missing[0] if missing else "") or "業務事実の回答")
        return {
            "route": "ask_human",
            "label_ja": "人間への質問として表示",
            "detail_ja": "人間しか答えられない業務事実が不足しています。推測で埋めません。",
            "human_ja": "人間が適用条件と判断根拠を回答します",
            "question_ja": question + "について、適用条件と判断根拠を回答してください。",
            "triz_skipped": False,
            "triz_skip_reason": "",
            "references": {},
            "missing_evidence": list(missing),
            "basis_ja": basis,
        }
    if cause in {"approval_wait", "external_dependency"}:
        return {
            "route": "wait_human",
            "label_ja": "人間の確認待ちとして表示",
            "detail_ja": "外部操作・承認はセバスが推測で代行しません。",
            "human_ja": "人間が承認または実施結果の確認を行います",
            "question_ja": "",
            "triz_skipped": False,
            "triz_skip_reason": "",
            "references": {},
            "missing_evidence": list(missing),
            "basis_ja": basis,
        }
    if cause in {"artifact_mismatch", "goal_evidence_missing"}:
        return {
            "route": "collect_evidence",
            "label_ja": "不足証拠の収集へ案内",
            "detail_ja": "成果物・最終検証の証拠が不足しています。",
            "human_ja": "人間が成果物と検証結果の確認を行います",
            "question_ja": "",
            "triz_skipped": False,
            "triz_skip_reason": "",
            "references": {},
            "missing_evidence": list(missing),
            "basis_ja": basis,
        }
    return {
        "route": "undiagnosed",
        "label_ja": "未診断として表示",
        "detail_ja": "未診断のため成功扱いにしません。証拠がそろい次第、再診断します。",
        "human_ja": "人間が状況の切り分けを行います",
        "question_ja": "",
        "triz_skipped": False,
        "triz_skip_reason": "",
        "references": {},
        "missing_evidence": list(missing),
        "basis_ja": basis,
    }


def _failure_id(gate_row: dict) -> str:
    return "|".join([
        str(gate_row.get("criterion_id") or ""),
        str(gate_row.get("status") or ""),
        str(gate_row.get("reason_code") or ""),
    ])


def _get_row(manager, project_id: str, rid: str) -> dict:
    db = _read_connection(manager)
    if db is None:
        raise ValueError("resolution not found")
    try:
        rows = db.execute(
            "SELECT * FROM resolutions WHERE project_id=? AND id=?",
            (project_id, rid),
        ).fetchone()
    except sqlite3.OperationalError as exc:
        raise ValueError("resolution not found") from exc
    finally:
        db.close()
    if rows is None:
        raise ValueError("resolution not found")
    return _decode(rows)


def get(manager, project_id: str, rid: str) -> dict:
    """残件を1件取得する(読み取り専用)。"""
    return public_view(_get_row(manager, str(project_id or ""), str(rid or "")))


def list_resolutions(manager, project_id: str) -> list[dict]:
    """残件一覧(読み取り専用。DBが無ければ空)。"""
    pid = str(project_id or "")
    db = _read_connection(manager)
    if db is None:
        return []
    try:
        with db:
            rows = db.execute(
                "SELECT * FROM resolutions WHERE project_id=? ORDER BY created",
                (pid,),
            ).fetchall()
    finally:
        db.close()
    return [public_view(_decode(x)) for x in rows]


def _mark_stale(manager, project_id: str, row: dict, actor: str, reason: str) -> dict:
    history = list(row.get("history") or []) + [{
        "from": row.get("state"), "to": "stale", "actor": actor,
        "reason": reason, "at": time.time(),
    }]
    with _connect(manager) as db:
        changed = db.execute(
            "UPDATE resolutions SET state='stale', history=?, updated=? "
            "WHERE project_id=? AND id=? AND state=?",
            (json.dumps(history, ensure_ascii=False), time.time(),
             project_id, row["id"], row.get("state")),
        ).rowcount
    if not changed:
        return _get_row(manager, project_id, row["id"])
    return _get_row(manager, project_id, row["id"])


def _drift_check(manager, project_id: str, row: dict, signature: str,
                 source_hash: str, actor: str) -> dict:
    """版(計画署名)または原本ハッシュが変わったら旧候補を stale にする。"""
    if str(row.get("plan_signature") or "") != str(signature or "") or (
            str(row.get("source_hash") or "") and source_hash
            and str(row.get("source_hash")) != str(source_hash)):
        updated = _mark_stale(
            manager, project_id, row, actor,
            "計画署名または原本ハッシュが変わりました。旧候補は失効(stale)にしました",
        )
        raise ValueError("計画署名または原本が変わりました。旧残件はstaleにしました")
    return row


def _refresh_stale(manager, project_id: str, signature: str,
                   source_hash: str, actor: str) -> list[str]:
    """現行と異なる署名・原本の非終端残件を stale にする。戻り値はstale化したID。"""
    staled: list[str] = []
    for view in list_resolutions(manager, project_id):
        if str(view.get("state") or "") in TERMINAL or str(view.get("state") or "") == "resolved":
            continue
        if str(view.get("plan_signature") or "") != str(signature or "") or (
                str(view.get("source_hash") or "") and source_hash
                and str(view.get("source_hash")) != str(source_hash)):
            row = _get_row(manager, project_id, str(view.get("id")))
            _mark_stale(
                manager, project_id, row, actor,
                "計画署名または原本ハッシュが変わりました。旧候補は失効(stale)にしました",
            )
            staled.append(str(view.get("id")))
    return staled


def diagnose_only(manager, project_id: str) -> dict:
    """現在の未達条件を診断する(読み取り専用。副作用なし・記録しない)。"""
    from app.completion_gate import evaluate
    from app.plan_coverage import build as build_coverage

    pid = str(project_id or "")
    mission, signature, contract, hashes = _current_context(manager, pid)
    gate = evaluate(manager, pid, persist=False)
    coverage = build_coverage(mission, contract or {})
    coverage_by_cid = {str(x.get("criterion_id") or ""): x for x in coverage.get("rows") or []}
    ledger = _safe_ledger(manager, pid)
    try:
        from app.safe_auto_resume import records as auto_records

        auto_runs = auto_records(manager, pid)
    except Exception:
        auto_runs = []
    try:
        actions = manager.memory.list_actions(pid)
    except Exception:
        actions = []
    external = _safe_external_evidence(manager, pid)
    sources_readable = _safe_sources_readable(manager, pid, mission)
    diagnoses = []
    for gate_row in gate.get("criteria") or []:
        if str(gate_row.get("status") or "") == "PASS":
            continue
        evidence = _evidence_for_criterion(
            manager, pid, mission, gate_row, coverage_by_cid, ledger,
            auto_runs, actions or [], external, hashes, sources_readable,
        )
        cause, missing, basis = _classify_cause(evidence)
        next_action = _route_for(pid, cause, evidence, missing, basis)
        diagnoses.append({
            "criterion_id": str(gate_row.get("criterion_id") or ""),
            "task_key": str((evidence.get("task") or {}).get("task_key") or ""),
            "failure_id": _failure_id(gate_row),
            "cause": cause,
            "cause_ja": CAUSE_LABEL_JA.get(cause, "未診断"),
            "evidence": evidence,
            "missing_evidence": missing,
            "basis_ja": basis,
            "next_action": next_action,
        })
    return {
        "project_id": pid,
        "plan_signature": signature,
        "contract_hash": str((contract or {}).get("content_hash") or ""),
        "input_version": str(mission.get("plan_version") or ""),
        "source_hash": str(hashes.get("source_hash") or ""),
        "artifact_hash": str(hashes.get("artifact_hash") or ""),
        "achieved": bool(gate.get("achieved")),
        "diagnoses": diagnoses,
    }


def detect(manager, project_id: str, actor: str) -> dict:
    """現在の未達条件から残件を検出・記録する(冪等)。承認・実行・外部操作・RAG登録は行わない。"""
    if not str(actor or "").strip():
        raise ValueError("actor is required")
    actor_name = str(actor).strip()
    pid = str(project_id or "")
    preview = diagnose_only(manager, pid)
    staled = _refresh_stale(
        manager, pid, preview["plan_signature"], preview["source_hash"], actor_name,
    )
    created: list[str] = []
    views: list[dict] = []
    now = time.time()
    for item in preview["diagnoses"]:
        rid = uuid.uuid4().hex
        history = [
            {"from": None, "to": "detected", "actor": actor_name,
             "reason": "未達条件を検出", "at": now},
            {"from": "detected", "to": "diagnosed", "actor": actor_name,
             "reason": item["basis_ja"], "evidence": {"cause": item["cause"]}, "at": now},
        ]
        with _connect(manager) as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT id FROM resolutions WHERE project_id=? AND failure_id=? AND plan_signature=?",
                (pid, item["failure_id"], preview["plan_signature"]),
            ).fetchone()
            if existing:
                rid = str(existing["id"])
            else:
                try:
                    db.execute(
                        """INSERT INTO resolutions(
                            id, project_id, criterion_id, task_key, failure_id,
                            contract_hash, plan_signature, input_version, source_hash,
                            artifact_hash, cause, evidence, missing_evidence, next_action,
                            state, attempts, history, created, updated)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (rid, pid, item["criterion_id"], item["task_key"], item["failure_id"],
                         preview["contract_hash"], preview["plan_signature"],
                         preview["input_version"], preview["source_hash"],
                         preview["artifact_hash"], item["cause"],
                         json.dumps(item["evidence"], ensure_ascii=False),
                         json.dumps(item["missing_evidence"], ensure_ascii=False),
                         json.dumps(item["next_action"], ensure_ascii=False),
                         "diagnosed", 0, json.dumps(history, ensure_ascii=False), now, now),
                    )
                    created.append(rid)
                except sqlite3.IntegrityError:
                    # 同時要求で先に作られた場合は既存を返す(二重作成しない)。
                    row = db.execute(
                        "SELECT id FROM resolutions WHERE project_id=? AND failure_id=? AND plan_signature=?",
                        (pid, item["failure_id"], preview["plan_signature"]),
                    ).fetchone()
                    if row is None:
                        raise
                    rid = str(row["id"])
        views.append(get(manager, pid, rid))
    views.sort(key=lambda x: (str(x.get("criterion_id") or ""), str(x.get("id") or "")))
    return {
        "project_id": pid,
        "plan_signature": preview["plan_signature"],
        "actor": actor_name,
        "resolutions": views,
        "created": created,
        "staled": staled,
    }


def _trial_evidence_server_side(manager, project_id: str, row: dict) -> dict | None:
    """隔離試験・限定再実行の既存記録だけを見る。呼び出し側の申告は信用しない。"""
    task_key = str(row.get("task_key") or "")
    if not task_key:
        return None
    try:
        from app.safe_auto_resume import records as auto_records

        for run in auto_records(manager, project_id) or []:
            if str(run.get("task_key") or "") != task_key:
                continue
            if str(run.get("status") or "") != "completed":
                continue
            if not run.get("artifact_hash") or not run.get("evidence"):
                continue
            try:
                from app.recovery_record import _verify_run_artifact

                _verify_run_artifact(manager, project_id, run)
            except Exception:
                continue
            return {"kind": "p2_run", "run_id": str(run.get("run_id") or ""),
                    "artifact_hash": str(run.get("artifact_hash") or "")}
    except Exception:
        pass
    try:
        from app.recovery_record import list_records

        for record in list_records(manager, project_id) or []:
            if str(record.get("task_key") or "") != task_key:
                continue
            if bool(record.get("business_passed")) or bool(record.get("rerun_passed")):
                return {"kind": "recovery", "recovery_id": str(record.get("id") or ""),
                        "state": str(record.get("state") or "")}
    except Exception:
        pass
    return None


def _resolution_reevaluation(manager, project_id: str, row: dict) -> dict:
    """該当条件の再評価だけをサーバー側で行う。自己申告は信用しない。"""
    from app.completion_gate import evaluate

    gate = evaluate(manager, project_id, persist=False)
    cid = str(row.get("criterion_id") or "")
    status = ""
    for item in gate.get("criteria") or []:
        if str(item.get("criterion_id") or "") == cid:
            status = str(item.get("status") or "")
            break
    human_ok = bool(gate.get("human_accepted"))
    try:
        from app.goal_contract import get_active

        contract = get_active(manager, project_id) or {}
        require_human = bool((contract.get("completion_policy") or {}).get(
            "require_human_acceptance", True))
    except Exception:
        require_human = True
    passed = status == "PASS" and (human_ok or not require_human)
    return {
        "criterion_id": cid,
        "status": status or "UNKNOWN",
        "passed": bool(passed),
        "human_accepted": human_ok,
        "achieved": bool(gate.get("achieved")),
        "at": time.time(),
    }


def record_reevaluation(manager, project_id: str, rid: str, record: dict) -> dict:
    """P4: safe_auto_resume 実行直後の独立再評価を残件へ記録する(記録のみ)。

    - 読み取り(評価)と記録のみ。承認・外部操作・追加の実行・RAG登録・
      自動再開の有効化は一切行わない。
    - verifying -> resolved への遷移提案は「独立評価で当該条件PASS」の場合だけ
      可能になる(resolved にはしない。advance("resolved") が別途 gate を見る)。
      自己申告・工程完了だけでは resolved 可能にしない。
    - 未達・例外時は残件を resolved 可能にせず、理由を保存する(unknown 扱い)。
    - コーディネータ未初期化でも呼び出し側の完了記録を壊さないよう、
      残件が無ければ何もせず返す(呼び出し側で完了記録は維持される)。
    """
    if not isinstance(record, dict):
        raise ValueError("record must be an object")
    pid = str(project_id or "")
    row = _get_row(manager, pid, str(rid or ""))
    current = str(row.get("state") or "")
    if current in TERMINAL or current == "resolved":
        return get(manager, pid, str(rid or ""))
    # 版が変わっていたら旧残件として扱い、再評価で解決可能にしない。
    try:
        _mission, signature, _contract, hashes = _current_context(manager, pid)
        if str(row.get("plan_signature") or "") != str(signature or ""):
            raise ValueError("plan_signature changed")
    except ValueError:
        raise
    except Exception as exc:
        stored = {
            "criterion_id": str(row.get("criterion_id") or ""),
            "status": "UNKNOWN",
            "passed": False,
            "human_accepted": False,
            "achieved": False,
            "at": time.time(),
            "p4_reevaluation": {
                "task_key": str(record.get("task_key") or ""),
                "run_id": str(record.get("run_id") or ""),
                "artifact_verified": False,
                "gate_status": "UNKNOWN",
                "gate_passed": False,
                "error": f"context_unavailable:{type(exc).__name__}",
                "note": "再評価の前提(現行計画・原本)の確認に失敗したため resolved にしない",
            },
        }
        history = list(row.get("history") or []) + [{
            "from": current, "to": current, "actor": "p4-auto-reevaluation",
            "reason": stored["p4_reevaluation"]["note"], "at": time.time(),
        }]
        with _connect(manager) as db:
            db.execute(
                "UPDATE resolutions SET reevaluation=?, history=?, updated=? "
                "WHERE project_id=? AND id=? AND state=?",
                (json.dumps(stored, ensure_ascii=False),
                 json.dumps(history, ensure_ascii=False), time.time(),
                 pid, row["id"], current),
            )
        return get(manager, pid, str(rid or ""))

    gate_passed = bool(record.get("gate_passed")) and bool(record.get("artifact_verified")) \
        and not str(record.get("error") or "")
    stored = {
        "criterion_id": str(row.get("criterion_id") or record.get("criterion_id") or ""),
        "status": str(record.get("gate_status") or "UNKNOWN"),
        "passed": False,
        "human_accepted": False,
        "achieved": bool(record.get("gate_achieved")),
        "at": float(record.get("at") or time.time()),
        "p4_reevaluation": {
            "task_key": str(record.get("task_key") or ""),
            "run_id": str(record.get("run_id") or ""),
            "artifact_hash": str(record.get("artifact_hash") or "")[:64],
            "artifact_verified": bool(record.get("artifact_verified")),
            "gate_status": str(record.get("gate_status") or "UNKNOWN"),
            "gate_passed": bool(record.get("gate_passed")),
            "gate_achieved": bool(record.get("gate_achieved")),
            "error": str(record.get("error") or ""),
            "note": str(record.get("note") or "")[:1000],
        },
    }
    # resolved 可能条件は advance("resolved") と同一の独立 gate 判定に従う。
    # ここでは記録だけし、状態遷移は行わない(提案可能状態の可視化のみ)。
    # verifying への自動遷移も行わない: 呼び出し側の完了記録と独立させる。
    history = list(row.get("history") or []) + [{
        "from": current, "to": current, "actor": "p4-auto-reevaluation",
        "reason": str(record.get("note") or "P4独立再評価を記録")[:1000],
        "evidence": {"gate_passed": gate_passed,
                     "gate_status": stored["status"]},
        "at": time.time(),
    }]
    with _connect(manager) as db:
        db.execute(
            "UPDATE resolutions SET reevaluation=?, p2_run_id=?, history=?, updated=? "
            "WHERE project_id=? AND id=? AND state=?",
            (json.dumps(stored, ensure_ascii=False),
             str(record.get("run_id") or row.get("p2_run_id") or ""),
             json.dumps(history, ensure_ascii=False), time.time(),
             pid, row["id"], current),
        )
    return get(manager, pid, str(rid or ""))


def advance(manager, project_id: str, rid: str, next_state: str,
            actor: str, reason: str = "", evidence: dict | None = None) -> dict:
    """残件の状態遷移(検証付き)。承認・実行・外部操作は行わない。"""
    if not str(actor or "").strip():
        raise ValueError("actor is required")
    actor_name = str(actor).strip()
    pid = str(project_id or "")
    target = str(next_state or "").strip()
    if not target:
        raise ValueError("next_state is required")
    if not isinstance(evidence, dict):
        raise ValueError("evidence must be an object")
    row = _get_row(manager, pid, str(rid or ""))
    current = str(row.get("state") or "")
    if current in TERMINAL or current == "resolved":
        raise ValueError(f"resolution is terminal: {current}")
    mission, signature, _contract, hashes = _current_context(manager, pid)
    _drift_check(manager, pid, row, signature,
                 str(hashes.get("source_hash") or ""), actor_name)
    row = _get_row(manager, pid, str(rid or ""))
    current = str(row.get("state") or "")

    if target in TERMINAL:
        if not str(reason or "").strip():
            raise ValueError("blocked/rejected/stale/failed には reason が必須です")
    elif target in ORDER:
        try:
            if ORDER.index(target) != ORDER.index(current) + 1:
                raise ValueError(f"invalid resolution transition: {current} -> {target}")
        except ValueError as exc:
            if "invalid resolution transition" not in str(exc):
                raise ValueError(f"invalid resolution transition: {current} -> {target}") from exc
            raise
        if str(row.get("cause") or "") == "unknown" and ORDER.index(target) >= ORDER.index("candidate_ready"):
            raise ValueError("未診断(unknown)の残件は対応へ進めません。証拠をそろえて再診断してください")
    else:
        raise ValueError(f"unknown resolution state: {target}")

    if int(row.get("attempts") or 0) >= MAX_ATTEMPTS and target not in TERMINAL:
        with _connect(manager) as db:
            history = list(row.get("history") or []) + [{
                "from": current, "to": "failed", "actor": actor_name,
                "reason": f"試行回数が上限({MAX_ATTEMPTS})に達しました",
                "at": time.time(),
            }]
            db.execute(
                "UPDATE resolutions SET state='failed', attempts=?, history=?, updated=? "
                "WHERE project_id=? AND id=? AND state=?",
                (int(row.get("attempts") or 0) + 1, json.dumps(history, ensure_ascii=False),
                 time.time(), pid, row["id"], current),
            )
        raise ValueError(f"試行回数が上限({MAX_ATTEMPTS})に達したため failed にしました")

    values: dict = {}
    note = str(reason or "").strip()
    if target == "candidate_ready":
        # 案内であって実行ではない。修復/回復の開始は行わない。
        # 既存の修復ラン・回復記録への参照だけを追跡用に保存する。
        repair_run_id = str(evidence.get("repair_run_id") or "")
        recovery_id = str(evidence.get("recovery_id") or "")
        if repair_run_id:
            from app.plan_repair_loop import get_run as get_repair_run

            stored = get_repair_run(manager, pid, repair_run_id)
            if str(stored.get("project_id") or "") != pid:
                raise ValueError("repair run is not for this project")
            values["repair_run_id"] = repair_run_id
        if recovery_id:
            from app.recovery_record import get as get_recovery

            stored = get_recovery(manager, pid, recovery_id)
            if str(stored.get("task_key") or "") != str(row.get("task_key") or ""):
                raise ValueError("recovery record is not for this task")
            values["recovery_id"] = recovery_id
        if not note:
            note = str((row.get("next_action") or {}).get("label_ja") or "対応案内を確定")
    elif target == "trial_passed":
        # 呼び出し側の合格申告は信用せず、既存の隔離試験・限定再実行記録だけを見る。
        trial = _trial_evidence_server_side(manager, pid, row)
        if not trial:
            raise ValueError("隔離試験または限定再実行の既存記録が無いため trial_passed へ進めません")
        triz = dict(row.get("triz_info") or {})
        triz.update({"trial": trial, "business_recovered": False,
                     "note": "技術試験の合格であり業務達成ではない"})
        values["triz_info"] = triz
        if trial.get("kind") == "p2_run" and trial.get("run_id"):
            values["p2_run_id"] = str(trial["run_id"])
        if not note:
            note = "既存の隔離試験・限定再実行記録を確認"
    elif target == "awaiting_approval":
        if not note:
            note = "人間の承認待ち"
    elif target == "executing":
        approval = dict(row.get("approval") or {})
        claimed = evidence.get("approval") if isinstance(evidence.get("approval"), dict) else {}
        by = str(claimed.get("by") or approval.get("by") or "").strip()
        if not by:
            raise ValueError("executing には人間の承認記録(approval.by)が必要です")
        approval.update({"by": by, "note": str(claimed.get("note") or approval.get("note") or "")[:1000],
                         "at": time.time()})
        values["approval"] = approval
        if evidence.get("p2_run_id"):
            values["p2_run_id"] = str(evidence.get("p2_run_id") or "")
        if not note:
            note = "引渡し先での対応実行を追跡"
    elif target == "verifying":
        if not note:
            note = "再評価を開始"
    elif target == "resolved":
        checked = _resolution_reevaluation(manager, pid, row)
        values["reevaluation"] = checked
        if not checked.get("passed"):
            raise ValueError(
                "再評価で該当条件がPASSしていないため resolved にできません "
                f"(status={checked.get('status')}, human_accepted={checked.get('human_accepted')})"
            )
        if not note:
            note = "再評価で該当条件PASSを確認"
    elif target in TERMINAL:
        pass

    history = list(row.get("history") or []) + [{
        "from": current, "to": target, "actor": actor_name,
        "reason": note[:1000], "evidence": {"keys": sorted(evidence.keys())[:10]},
        "at": time.time(),
    }]
    values.update(state=target, attempts=int(row.get("attempts") or 0) + 1,
                  history=history)
    serial = {}
    for key, value in values.items():
        if key in {"triz_info", "approval", "reevaluation", "history"}:
            serial[key] = json.dumps(value, ensure_ascii=False)
        else:
            serial[key] = value
    serial["updated"] = time.time()
    with _connect(manager) as db:
        changed = db.execute(
            "UPDATE resolutions SET " + ",".join(f"{k}=?" for k in serial) +
            " WHERE project_id=? AND id=? AND state=?",
            (*serial.values(), pid, row["id"], current),
        ).rowcount
    if changed != 1:
        raise ValueError("resolution state changed or record not found")
    return get(manager, pid, str(row["id"]))


def summary(manager, project_id: str) -> dict:
    """上位サマリ用の件数と原因別内訳(読み取り専用)。"""
    pid = str(project_id or "")
    by_cause: dict[str, int] = {}
    by_state: dict[str, int] = {}
    total = 0
    open_count = 0
    for view in list_resolutions(manager, pid):
        total += 1
        cause = str(view.get("cause") or "unknown")
        state = str(view.get("state") or "")
        by_cause[cause] = by_cause.get(cause, 0) + 1
        by_state[state] = by_state.get(state, 0) + 1
        if state in {"detected", "diagnosed", "candidate_ready", "trial_passed",
                     "awaiting_approval", "executing", "verifying"}:
            open_count += 1
    try:
        from app.goal_review import plan_snapshot

        signature = str(plan_snapshot(manager, pid)[1] or "")
    except Exception:
        signature = ""
    return {
        "project_id": pid,
        "plan_signature": signature,
        "total": total,
        "open_count": open_count,
        "by_cause": by_cause,
        "by_state": by_state,
    }


def _statements(manager, project_id: str) -> dict[str, str]:
    try:
        from app.goal_contract import preview as contract_preview

        contract = contract_preview(manager, project_id) or {}
        return {str(x.get("criterion_id") or ""): str(x.get("statement") or "")
                for x in contract.get("criteria") or [] if x.get("criterion_id")}
    except Exception:
        return {}


def resolution_cards(manager, project_id: str) -> dict:
    """案件画面向けの日本語カード(読み取り専用)。JSONだけを見せない方針の材料。"""
    pid = str(project_id or "")
    statements = _statements(manager, pid)
    cards = []
    views = list_resolutions(manager, pid)
    for view in views:
        cid = str(view.get("criterion_id") or "")
        cause = str(view.get("cause") or "unknown")
        nxt = dict(view.get("next_action") or {})
        evidence = dict(view.get("evidence") or {})
        gate = dict(evidence.get("gate") or {})
        approval = dict(view.get("approval") or {})
        reeval = dict(view.get("reevaluation") or {})
        tried = []
        if view.get("repair_run_id"):
            tried.append("P1-C修復ラン参照=" + _short(view.get("repair_run_id"), 8))
        if view.get("recovery_id"):
            tried.append("P3回復記録参照=" + _short(view.get("recovery_id"), 8))
        if view.get("p2_run_id"):
            tried.append("P2実行参照=" + _short(view.get("p2_run_id"), 8))
        triz = dict(view.get("triz_info") or {})
        if triz.get("trial"):
            tried.append("技術試験=" + str((triz.get("trial") or {}).get("kind") or "確認済み"))
        evidence_text = (
            "条件=" + (cid or "なし")
            + " / 状態=" + str(gate.get("status") or "不明")
            + " / 理由=" + str(gate.get("reason_code") or "不明")
            + " / 被覆=" + str((evidence.get("coverage") or {}).get("status") or "不明")
            + " / 工程=" + str((evidence.get("task") or {}).get("task_key") or "なし")
            + "(" + str((evidence.get("task") or {}).get("status") or "不明") + ")"
            + " / 原本=" + _short(str((evidence.get("source") or {}).get("source_hash") or "不明"))
            + " / 成果物=" + _short(str((evidence.get("artifact") or {}).get("artifact_hash") or "なし"))
            + " / 外部承認待ち=" + str((evidence.get("external") or {}).get("pending_approval"))
        )
        if reeval:
            reeval_text = (
                "条件=" + str(reeval.get("status") or "不明")
                + " / 達成=" + ("はい" if reeval.get("passed") else "いいえ")
                + " / 人間確認=" + ("あり" if reeval.get("human_accepted") else "なし")
            )
        else:
            reeval_text = "未再評価"
        cards.append({
            "id": str(view.get("id")),
            "対象条件": ((cid + " " + statements.get(cid, "")).strip() or "未結合"),
            "停止理由": CAUSE_LABEL_JA.get(cause, "未診断") + ": " + str(gate.get("message") or nxt.get("basis_ja") or "")[:300],
            "確認した証拠": evidence_text[:800],
            "試した処置": (" / ".join(tried) if tried else "未実施"),
            "セバスが次に行えること": str(nxt.get("label_ja") or "未診断のため対応案内なし"),
            "人間に必要な判断": str(nxt.get("human_ja") or nxt.get("question_ja") or "状況の切り分けが必要"),
            "再評価結果": reeval_text,
            "状態": str(view.get("state_ja") or ""),
            "技術試験と業務達成の区別": "技術試験(隔離試験・限定再実行)と業務達成(条件PASS・人間確認)は別に判定する",
            "承認": ("承認者=" + str(approval.get("by") or "なし")) if approval.get("by") else "承認なし",
        })
    return {"project_id": pid, "resolutions": views, "cards": cards}
