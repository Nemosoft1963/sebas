"""Stage1: 停止分類器 (部品C)。

あらゆる停止を6クラスへ正規化し、次の一手を機械可読で出す。
読み取り専用が原則: 分類は承認・実行・外部送信・RAG登録・計画適用を一切行わない。

- STOP_CLASSES: 固定6クラス
- CLASS_TABLE: phase/reason/disposition -> クラスの対応表 (コード内1か所)
- classify_stop(inputs): 純粋関数 (DB・ファイル不変、外部呼出なし)
- is_repeating(history, record): 空転ガード判定 (純粋)
- stop_history sidecar: PJごと (署名,クラス,コード,時刻) 追記のみ。GETでは書かない。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

STOP_CLASSES = (
    "missing_binding",
    "missing_coverage",
    "missing_decision",
    "missing_evidence",
    "missing_capability",
    "policy_block",
)

# 優先順位 (決定的): policy_block > missing_decision > missing_capability
# > missing_evidence > missing_coverage > missing_binding
PRIORITY = (
    "policy_block",
    "missing_decision",
    "missing_capability",
    "missing_evidence",
    "missing_coverage",
    "missing_binding",
)
_PRIORITY_INDEX = {c: i for i, c in enumerate(PRIORITY)}

# 対応表 (コード内1か所)。stop_code -> stop_class。
# stop_code は既存の phase / reason コード等から導く安定識別子。
# 未知の値は missing_decision に倒す (get の default)。
CLASS_TABLE: dict[str, str] = {
    # --- workflow_readiness.PHASE_LABELS の全キー (phase:<key>) ---
    "phase:running": "missing_decision",
    "phase:accuracy_blocked": "missing_evidence",
    "phase:fact_confirm": "missing_decision",
    "phase:dev_blocked": "missing_capability",
    "phase:plan_conflict": "missing_binding",
    "phase:plan_fact_confirm": "missing_decision",
    "phase:proposal_ready": "missing_decision",
    "phase:issues_open": "missing_coverage",
    "phase:waiting_budget": "policy_block",
    "phase:unverified": "missing_evidence",
    "phase:connection_failed": "missing_evidence",
    "phase:provisional": "missing_decision",
    "phase:complete": "missing_decision",
    "phase:idle": "missing_decision",
    # --- completion_gate.REASON_* (gate:<code>) ---
    "gate:NO_CONTRACT": "missing_evidence",
    "gate:RETENTION_FAILED": "missing_coverage",
    "gate:COVERAGE_INCOMPLETE": "missing_coverage",
    "gate:HUMAN_ACCEPTANCE_MISSING": "missing_decision",
    "gate:HUMAN_ACCEPTANCE_HASH_DRIFT": "missing_evidence",
    # generic/vehicle 検査の reason_code (既存の検査が出す値)
    "gate:ARTIFACT_MISSING": "missing_evidence",
    "gate:ARTIFACT_FORMAT": "missing_evidence",
    "gate:ARTIFACT_HASH_DRIFT": "missing_evidence",
    "gate:VERIFICATION_MISSING": "missing_evidence",
    "gate:NO_EXEC_TASK": "missing_evidence",
    "gate:UNTESTABLE": "missing_evidence",
    "gate:CHECK_UNIMPLEMENTED": "missing_capability",
    "gate:TASK_INCOMPLETE": "missing_evidence",
    "gate:EXTERNAL_ACTION_MISSING": "missing_evidence",
    "gate:TRIAL_EVIDENCE_MISSING": "missing_evidence",
    # --- resolution_coordinator.CAUSES (cause:<code>) ---
    "cause:source_unreadable": "missing_evidence",
    "cause:business_fact_missing": "missing_decision",
    "cause:plan_conflict": "missing_binding",
    "cause:execution_failure": "missing_evidence",
    "cause:artifact_mismatch": "missing_evidence",
    "cause:external_dependency": "missing_evidence",
    "cause:approval_wait": "missing_decision",
    "cause:goal_evidence_missing": "missing_evidence",
    "cause:unknown": "missing_decision",
    # --- plan_review_loop の状態・停止種別 (run:<code>) ---
    "run:permitted": "missing_decision",
    "run:drafting": "missing_binding",
    "run:organized": "missing_binding",
    "run:structure_checked": "missing_binding",
    "run:verifying": "missing_evidence",
    "run:intake": "missing_binding",
    "run:proposing": "missing_coverage",
    "run:applied": "missing_decision",
    "run:awaiting_human": "missing_decision",
    "run:stopped": "missing_decision",
    "run:cancelled": "missing_decision",
    "run:human_required": "missing_decision",
    "run:connection_settings": "policy_block",
    "run:legacy": "missing_decision",
    "run:verification_limit": "policy_block",
    # --- plan_feedback の disposition (disposition:<code>) ---
    "disposition:development": "missing_capability",
    "disposition:business_fact": "missing_decision",
    "disposition:unresolved": "missing_coverage",
    # --- 被覆行列 (coverage:<code>) ---
    "coverage:incomplete": "missing_coverage",
    "coverage:partial": "missing_coverage",
    # --- 指摘束縛 (binding:<code>) ---
    "binding:ambiguous": "missing_decision",
    "binding:invalid_reference": "missing_binding",
    "binding:multiple_targets": "missing_binding",
    # --- 安全境界 (policy:<code>)。迂回策なし、next_action は常に none ---
    "policy:external_resend_prohibited": "policy_block",
    "policy:unapproved_ocr": "policy_block",
    "policy:implicit_public": "policy_block",
    "policy:limit_reached": "policy_block",
    "policy:budget_exhausted": "policy_block",
    # --- 分類不能 ---
    "unknown:empty": "missing_decision",
    "unknown:unclassifiable": "missing_decision",
}

# stop_code -> next_action。IMPLEMENTED_ACTIONS にある名前か none のみ。
# policy_block は常に none。
NEXT_ACTION_TABLE: dict[str, str] = {
    "phase:running": "wait",
    "phase:accuracy_blocked": "review_source_difference",
    "phase:fact_confirm": "confirm_allocation",
    "phase:dev_blocked": "resolve_development",
    "phase:plan_conflict": "review_feedback",
    "phase:plan_fact_confirm": "review_feedback",
    "phase:proposal_ready": "review_feedback",
    "phase:issues_open": "propose_feedback",
    "phase:waiting_budget": "none",
    "phase:unverified": "external_review",
    "phase:connection_failed": "external_review",
    "phase:provisional": "review_artifacts",
    "phase:complete": "approve_result",
    "phase:idle": "idle",
    "gate:NO_CONTRACT": "external_review",
    "gate:RETENTION_FAILED": "propose_feedback",
    "gate:COVERAGE_INCOMPLETE": "propose_feedback",
    "gate:HUMAN_ACCEPTANCE_MISSING": "approve_result",
    "gate:HUMAN_ACCEPTANCE_HASH_DRIFT": "review_artifacts",
    "gate:ARTIFACT_MISSING": "review_artifacts",
    "gate:ARTIFACT_FORMAT": "review_artifacts",
    "gate:ARTIFACT_HASH_DRIFT": "review_artifacts",
    "gate:VERIFICATION_MISSING": "external_review",
    "gate:NO_EXEC_TASK": "propose_feedback",
    "gate:UNTESTABLE": "external_review",
    "gate:CHECK_UNIMPLEMENTED": "resolve_development",
    "gate:TASK_INCOMPLETE": "review_feedback",
    "gate:EXTERNAL_ACTION_MISSING": "review_artifacts",
    "gate:TRIAL_EVIDENCE_MISSING": "review_artifacts",
    "cause:source_unreadable": "review_source_difference",
    "cause:business_fact_missing": "confirm_allocation",
    "cause:plan_conflict": "review_feedback",
    "cause:execution_failure": "review_artifacts",
    "cause:artifact_mismatch": "review_artifacts",
    "cause:external_dependency": "review_artifacts",
    "cause:approval_wait": "approve_result",
    "cause:goal_evidence_missing": "external_review",
    "cause:unknown": "idle",
    "run:permitted": "wait",
    "run:drafting": "propose_feedback",
    "run:organized": "propose_feedback",
    "run:structure_checked": "propose_feedback",
    "run:verifying": "external_review",
    "run:intake": "review_feedback",
    "run:proposing": "propose_feedback",
    "run:applied": "approve_plan",
    "run:awaiting_human": "approve_plan",
    "run:stopped": "idle",
    "run:cancelled": "idle",
    "run:human_required": "review_feedback",
    "run:connection_settings": "none",
    "run:legacy": "idle",
    "run:verification_limit": "none",
    "disposition:development": "resolve_development",
    "disposition:business_fact": "confirm_allocation",
    "disposition:unresolved": "review_feedback",
    "coverage:incomplete": "propose_feedback",
    "coverage:partial": "propose_feedback",
    "binding:ambiguous": "review_feedback",
    "binding:invalid_reference": "propose_feedback",
    "binding:multiple_targets": "review_feedback",
    "policy:external_resend_prohibited": "none",
    "policy:unapproved_ocr": "none",
    "policy:implicit_public": "none",
    "policy:limit_reached": "none",
    "policy:budget_exhausted": "none",
    "unknown:empty": "none",
    "unknown:unclassifiable": "none",
}

REASON_JA_TABLE: dict[str, str] = {
    "phase:running": "処理を実行中です。完了まで待ってください。",
    "phase:accuracy_blocked": "原本の読取・照合の証拠が不足しているため確定できません。",
    "phase:fact_confirm": "業務事実の確認が残っています。人が回答してください。",
    "phase:dev_blocked": "追加開発が必要な指摘があります。実行器がありません。",
    "phase:plan_conflict": "指摘・条件・工程の対応付けが不完全です。保存済み材料で再対応付けできます。",
    "phase:plan_fact_confirm": "計画に必要な業務事実を人が確認してください。",
    "phase:proposal_ready": "保存済みの修正案を人が確認してください。",
    "phase:issues_open": "草案・候補が指摘または達成条件を覆っていません。",
    "phase:waiting_budget": "安全境界により停止しています。上限到達のため自動では進めません。",
    "phase:unverified": "現行版の外部検証・最終検証の証拠がありません。",
    "phase:connection_failed": "外部検証の証拠がありません。接続設定を確認してください。",
    "phase:provisional": "暫定成果があります。人の確認が必要です。",
    "phase:complete": "確定条件を満たしています。人の最終確認が必要です。",
    "phase:idle": "待機中です。分類不能のため人の判断が必要です。",
    "gate:NO_CONTRACT": "達成条件の契約がありません。証拠がありません。",
    "gate:RETENTION_FAILED": "要求保持検査に不合格のため被覆がありません。",
    "gate:COVERAGE_INCOMPLETE": "計画被覆が無いため未評価です。",
    "gate:HUMAN_ACCEPTANCE_MISSING": "人間による結果確認が無いため承認待ちです。",
    "gate:HUMAN_ACCEPTANCE_HASH_DRIFT": "承認後に版・原本・成果物が変わりました。証拠を確認してください。",
    "gate:ARTIFACT_MISSING": "必須成果物がありません。",
    "gate:ARTIFACT_FORMAT": "成果物の形式が条件を満たしません。",
    "gate:ARTIFACT_HASH_DRIFT": "成果物のハッシュが変わりました。原本を確認してください。",
    "gate:VERIFICATION_MISSING": "最終検証の評価結果がありません。",
    "gate:NO_EXEC_TASK": "実行工程がありません。",
    "gate:UNTESTABLE": "評価不能のため達成証拠が不足しています。",
    "gate:CHECK_UNIMPLEMENTED": "必要な検査を実行する手段がありません。追加開発が必要です。",
    "gate:TASK_INCOMPLETE": "工程が未完了のため証拠がありません。",
    "gate:EXTERNAL_ACTION_MISSING": "承認済み外部操作の実行証拠がありません。",
    "gate:TRIAL_EVIDENCE_MISSING": "試験の証拠がありません。",
    "cause:source_unreadable": "原本を読み取れないため証拠がありません。",
    "cause:business_fact_missing": "業務事実が不足しているため人の回答が必要です。",
    "cause:plan_conflict": "計画に矛盾があるため再対応付けが必要です。",
    "cause:execution_failure": "実行に失敗しているため証拠がありません。",
    "cause:artifact_mismatch": "成果物が一致しないため証拠がありません。",
    "cause:external_dependency": "外部操作の実行証拠が無いため確認が必要です。",
    "cause:approval_wait": "人間の承認待ちです。",
    "cause:goal_evidence_missing": "達成証拠が不足しています。",
    "cause:unknown": "未診断のため人の判断が必要です。分類不能として扱います。",
    "run:permitted": "人の許可済み・開始待ちのため人の判断が必要です。",
    "run:drafting": "草案の対応付けが不完全です。",
    "run:organized": "整理済み草案の対応付けを確認してください。",
    "run:structure_checked": "構造検査済み内容の対応を確認してください。",
    "run:verifying": "許可済みAIで検証中であり証拠がありません。",
    "run:intake": "指摘の取込・対応付けが不完全です。",
    "run:proposing": "修正案が指摘を覆っていません。",
    "run:applied": "未承認草案への反映済み。人の承認が必要です。",
    "run:awaiting_human": "人の計画承認待ちです。",
    "run:stopped": "停止中のため人の判断が必要です。",
    "run:cancelled": "取消済みのため人の判断が必要です。",
    "run:human_required": "曖昧な指摘・採否・業務事実の確定に人の選択が必要です。",
    "run:connection_settings": "安全境界により停止しています。接続設定の問題であり別手段で進めません。",
    "run:legacy": "旧方式の履歴のため人の判断が必要です。分類不能として扱います。",
    "run:verification_limit": "安全境界により停止しています。検証上限に達したため自動では進めません。",
    "disposition:development": "必要な動詞を実行する手段がありません。追加開発が必要です。",
    "disposition:business_fact": "業務事実の確認に人の回答が必要です。",
    "disposition:unresolved": "未解決の指摘があるため被覆がありません。",
    "coverage:incomplete": "草案・候補が指摘または達成条件を覆っていません。",
    "coverage:partial": "一部の指摘・条件だけ覆っており被覆が不完全です。",
    "binding:ambiguous": "曖昧な指摘の確定に人の選択が必要です。",
    "binding:invalid_reference": "指摘・条件・工程の対応が不完全です。保存済み材料で再対応付けできます。",
    "binding:multiple_targets": "複数対象への対応付けが未確定です。縮約せず再対応付けしてください。",
    "policy:external_resend_prohibited": "安全境界により拒否されました。外部再送は行いません。",
    "policy:unapproved_ocr": "安全境界により拒否されました。未承認OCRを下流へ渡しません。",
    "policy:implicit_public": "安全境界により拒否されました。暗黙の公開は行いません。",
    "policy:limit_reached": "安全境界により拒否されました。上限到達のため自動では進めません。",
    "policy:budget_exhausted": "安全境界により拒否されました。外部検証の利用枠がありません。",
    "unknown:empty": "分類不能のため人の判断が必要です。",
    "unknown:unclassifiable": "分類不能のため人の判断が必要です。",
}

# 外部送信カウンタ (分類中に外部呼出が無いことの実測用)。
# classify_stop はこの値を読み取るだけで、加算しない。
_EXTERNAL_SENDS_COUNTER = {"calls": 0}


def _note_external_send() -> None:
    """外部送信を行った場合に呼ぶ計数器。分類経路からは呼ばない。"""
    _EXTERNAL_SENDS_COUNTER["calls"] = int(_EXTERNAL_SENDS_COUNTER.get("calls") or 0) + 1


def _short_hash(value: object) -> str:
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        raw = str(value)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _truncate_id(value: object, length: int = 64) -> str:
    return str(value or "")[: max(1, int(length))]


def _class_for(code: str) -> str:
    return CLASS_TABLE.get(str(code), "missing_decision")


def _next_for(code: str) -> str:
    cls = _class_for(code)
    if cls == "policy_block":
        return "none"
    nxt = NEXT_ACTION_TABLE.get(str(code), "none")
    # 防衛: IMPLEMENTED_ACTIONS に無い名前は出さない (遅延importで検証)。
    try:
        from app.workflow_readiness import IMPLEMENTED_ACTIONS as _IMPL

        if nxt != "none" and nxt not in set(_IMPL):
            return "none"
    except Exception:
        pass
    return nxt


def _reason_for(code: str) -> str:
    reason = REASON_JA_TABLE.get(str(code))
    if reason:
        return reason
    return "分類不能のため人の判断が必要です。"


def _collect_candidates(inputs: dict) -> list[dict]:
    """既存APIの結果だけから候補を集める。外部呼出・DB書込なし。"""
    if not isinstance(inputs, dict):
        return []
    out: list[dict] = []

    def _push(code: str, origin: str) -> None:
        code = str(code or "").strip()
        if not code:
            return
        cls = _class_for(code)
        # 未知コードは分類不能として理由に残す
        known = code in CLASS_TABLE
        reason = _reason_for(code)
        if not known:
            reason = "分類不能のため人の判断が必要です。(元=" + origin + ")"
            cls = "missing_decision"
        out.append({
            "stop_class": cls,
            "stop_code": code if known else "unknown:unclassifiable",
            "next_action": "none" if cls == "policy_block" else _next_for(code),
            "reason_ja": reason,
            "origin": origin,
        })

    # readiness phase
    readiness = inputs.get("readiness")
    if isinstance(readiness, dict) and readiness.get("phase"):
        _push("phase:" + str(readiness.get("phase")), "readiness.phase")
    phase = inputs.get("readiness_phase") or inputs.get("phase")
    if phase and not isinstance(readiness, dict):
        _push("phase:" + str(phase), "readiness.phase")

    # completion gate / replay reason
    for key in ("gate", "replay", "completion_gate"):
        gate = inputs.get(key)
        if isinstance(gate, dict):
            rc = str(gate.get("reason_code") or "").strip()
            if rc:
                # "HUMAN_ACCEPTANCE_HASH_DRIFT:contract_hash,..." の suffix を落とす
                base = rc.split(":")[0]
                _push("gate:" + base, key + ".reason_code")
            for row in gate.get("criteria") or []:
                if isinstance(row, dict) and str(row.get("status") or "") != "PASS":
                    rcode = str(row.get("reason_code") or "").strip().split(":")[0]
                    if rcode:
                        _push("gate:" + rcode, key + ".criteria")
                        break
            failed = list(gate.get("failed_criteria") or [])
            if failed and not rc:
                _push("gate:COVERAGE_INCOMPLETE", key + ".failed_criteria")
    gate_reason = inputs.get("gate_reason") or inputs.get("gate_reason_code")
    if gate_reason:
        _push("gate:" + str(gate_reason).split(":")[0], "gate_reason")

    # run 状態・停止種別
    run = inputs.get("run")
    if isinstance(run, dict):
        state = str(run.get("state") or "").strip()
        if state:
            _push("run:" + state, "run.state")
        stop_kind = str(run.get("stop_kind") or "").strip()
        if stop_kind and stop_kind not in {"", state}:
            _push("run:" + stop_kind, "run.stop_kind")
        if state == "stopped" and not stop_kind:
            pass
        # 検証上限の明示
        try:
            rounds = int(run.get("verification_rounds") or 0)
            limit = int(run.get("verification_limit") or 2)
        except (TypeError, ValueError):
            rounds, limit = 0, 2
        if rounds >= limit and limit > 0:
            _push("run:verification_limit", "run.verification_limit")
    run_state = inputs.get("run_state")
    if run_state:
        _push("run:" + str(run_state), "run_state")
    run_stop = inputs.get("run_stop_kind") or inputs.get("stop_kind")
    if run_stop:
        _push("run:" + str(run_stop), "stop_kind")

    # 被覆行列 (0-2 の被覆行列: 未被覆・部分)
    coverage = inputs.get("coverage") or inputs.get("coverage_matrix")
    if isinstance(coverage, dict):
        rows = coverage.get("rows") or []
        if isinstance(rows, list) and rows:
            uncovered = 0
            partial = 0
            for row in rows:
                if not isinstance(row, dict):
                    continue
                cov = str(row.get("coverage") or row.get("status") or "")
                if cov in {"uncovered", "UNCOVERED"}:
                    uncovered += 1
                elif cov in {"partial", "partially_covered", "covered_partial"}:
                    partial += 1
            if uncovered:
                _push("coverage:incomplete", "coverage.rows")
            elif partial:
                _push("coverage:partial", "coverage.rows")
        elif coverage.get("passed") is False:
            _push("coverage:incomplete", "coverage.passed")
    if inputs.get("coverage_incomplete") is True:
        _push("coverage:incomplete", "coverage_incomplete")
    if inputs.get("coverage_partial") is True:
        _push("coverage:partial", "coverage_partial")

    # 指摘束縛
    bindings = inputs.get("bindings") or inputs.get("issue_bindings")
    if isinstance(bindings, dict) and isinstance(bindings.get("bindings"), list):
        bindings = bindings.get("bindings")
    if isinstance(bindings, list):
        for binding in bindings:
            if not isinstance(binding, dict):
                continue
            state = str(binding.get("state") or binding.get("binding_state") or "")
            if state in {"ambiguous"}:
                _push("binding:ambiguous", "bindings.state")
                break
            if state in {"invalid_reference"}:
                _push("binding:invalid_reference", "bindings.state")
                break
            if state in {"multiple_targets"}:
                _push("binding:multiple_targets", "bindings.state")
                break
    binding_state = inputs.get("binding_state")
    if binding_state:
        name = str(binding_state)
        if name in {"resolved"}:
            pass
        elif name in {"ambiguous", "invalid_reference", "multiple_targets"}:
            _push("binding:" + name, "binding_state")
        else:
            _push("unknown:unclassifiable", "binding_state")

    # disposition (development / business_fact / unresolved)
    for key in ("disposition", "blocker_disposition", "failure_kind"):
        disp = inputs.get(key)
        if disp:
            name = str(disp)
            if name in {"development", "business_fact", "unresolved"}:
                _push("disposition:" + name, key)
            else:
                _push("unknown:unclassifiable", key)
    blockers = inputs.get("blockers")
    if isinstance(blockers, list):
        for blocker in blockers:
            if isinstance(blocker, dict):
                disp = str(blocker.get("disposition") or "")
                if disp in {"development", "business_fact", "unresolved"}:
                    _push("disposition:" + disp, "blockers")
                    break

    # resolution_coordinator の原因9分類
    for key in ("cause", "resolution_cause", "coordinator_cause"):
        cause = inputs.get(key)
        if cause:
            _push("cause:" + str(cause), key)
    diagnoses = inputs.get("diagnoses")
    if isinstance(diagnoses, list):
        for diag in diagnoses:
            if isinstance(diag, dict) and diag.get("cause"):
                _push("cause:" + str(diag.get("cause")), "diagnoses")
                break

    # ledger の状態 (承認待ち等は missing_decision へ写像する材料)
    ledger = inputs.get("ledger")
    if isinstance(ledger, dict):
        summary = ledger.get("summary") if isinstance(ledger.get("summary"), dict) else ledger
        if isinstance(summary, dict):
            if summary.get("plan_status") == "unapproved":
                _push("run:awaiting_human", "ledger.plan_status")

    # TRIZ view (framed/candidates/tried は回復成功ではない -> 対応付け・被覆の問題)
    triz = inputs.get("triz")
    if isinstance(triz, dict):
        status = str(triz.get("status") or "")
        if status in {"framed", "candidate_generated", "artifact_trial_passed",
                      "artifact_trials_complete", "encoding_repaired", "preparing"}:
            _push("binding:multiple_targets", "triz.status")

    # 安全境界の明示信号 (迂回策なし)
    for key, code in (
        ("external_resend_prohibited", "policy:external_resend_prohibited"),
        ("unapproved_ocr", "policy:unapproved_ocr"),
        ("implicit_public", "policy:implicit_public"),
        ("limit_reached", "policy:limit_reached"),
        ("budget_exhausted", "policy:budget_exhausted"),
        ("policy_block", "policy:limit_reached"),
    ):
        if inputs.get(key) is True:
            _push(code, key)
    policy_code = inputs.get("policy_code")
    if policy_code:
        _push("policy:" + str(policy_code), "policy_code")

    # development 判定・UNIMPLEMENTED_ACTIONS の明示
    if inputs.get("development_required") is True:
        _push("disposition:development", "development_required")
    # Stage 2-A (追加のみ): capability preflight の結果を入力として受け付ける。
    # missing_capability が残れば development 相当で分類する。
    preflight = inputs.get("capability_preflight")
    if isinstance(preflight, dict) and preflight.get("missing_capability") is True:
        _push("disposition:development", "capability_preflight.missing_capability")
    unimplemented = inputs.get("unimplemented_action")
    if unimplemented:
        _push("disposition:development", "unimplemented_action")

    # 未知の phase/reason 文字列の明示 (分類不能の受入用)
    for key in ("unknown_phase", "unknown_reason", "unknown_code"):
        if inputs.get(key):
            _push("unknown:unclassifiable", key)

    return out


def _evidence_refs(inputs: dict) -> dict:
    """原本ID・計画署名・指摘ID・API応答ハッシュのみ。秘密・本文全文を入れない。"""
    if not isinstance(inputs, dict):
        return {}
    refs: dict = {}
    sig = inputs.get("plan_signature")
    if not sig and isinstance(inputs.get("readiness"), dict):
        sig = inputs["readiness"].get("plan_signature")
    if not sig and isinstance(inputs.get("run"), dict):
        sig = inputs["run"].get("plan_signature")
    if sig:
        refs["plan_signature"] = _truncate_id(sig, 128)
    for key in ("contract_hash", "goal_contract_hash", "packet_hash",
                "artifact_hash", "source_hash", "input_hash"):
        val = inputs.get(key)
        if val:
            refs[key] = _truncate_id(val, 64)
    gate = inputs.get("gate")
    if isinstance(gate, dict):
        for key in ("contract_hash", "plan_signature", "artifact_hash", "source_hash"):
            val = gate.get(key)
            if val and key not in refs:
                refs[key] = _truncate_id(val, 64)
    issue_ids: list[str] = []
    for key in ("issue_ids", "issue_id"):
        val = inputs.get(key)
        if isinstance(val, list):
            issue_ids.extend(_truncate_id(x, 64) for x in val[:10] if str(x))
        elif val:
            issue_ids.append(_truncate_id(val, 64))
    bindings = inputs.get("bindings") or inputs.get("issue_bindings")
    if isinstance(bindings, dict) and isinstance(bindings.get("bindings"), list):
        bindings = bindings.get("bindings")
    if isinstance(bindings, list):
        for binding in bindings[:10]:
            if isinstance(binding, dict) and binding.get("issue_id"):
                issue_ids.append(_truncate_id(binding.get("issue_id"), 64))
    if issue_ids:
        refs["issue_ids"] = sorted(set(issue_ids))[:10]
    # API応答のハッシュ (本文は入れない)
    for key in ("readiness", "gate", "replay", "run", "coverage", "ledger"):
        val = inputs.get(key)
        if isinstance(val, dict):
            try:
                refs[key + "_hash"] = _short_hash(val)
            except Exception:
                continue
    return refs


def classify_stop(inputs: dict) -> dict:
    """停止を6クラスへ正規化する純粋関数。読み取り専用。

    入力は既存APIの結果 (readiness、completion replay/gate の結果、
    ランの保存済み状態、被覆行列、指摘束縛) に限る。
    出力は1レコード: stop_class / stop_code / evidence_refs /
    next_action / reason_ja / external_sends (+ also)。
    分類できない/複数にまたがる停止は missing_decision に倒す。
    """
    start_calls = int(_EXTERNAL_SENDS_COUNTER.get("calls") or 0)
    if isinstance(inputs, dict) and bool(inputs.get("ambiguous_stop") or inputs.get("spans_multiple")):
        return {
            "stop_class": "missing_decision",
            "stop_code": "unknown:unclassifiable",
            "evidence_refs": _evidence_refs(inputs),
            "next_action": "none",
            "reason_ja": "分類不能のため人の判断が必要です。(複数にまたがる停止)",
            "external_sends": 0,
            "also": [],
        }
    candidates = _collect_candidates(inputs if isinstance(inputs, dict) else {})
    if not candidates:
        primary = {
            "stop_class": "missing_decision",
            "stop_code": "unknown:empty",
            "next_action": "none",
            "reason_ja": "分類不能のため人の判断が必要です。",
            "origin": "empty",
        }
        also: list[dict] = []
    else:
        def _rank(item: dict) -> tuple:
            return (_PRIORITY_INDEX.get(item["stop_class"], 99), item["stop_code"])

        ordered = sorted(candidates, key=_rank)
        primary = dict(ordered[0])
        also = []
        seen = {(primary["stop_class"], primary["stop_code"])}
        for item in ordered[1:]:
            key = (item["stop_class"], item["stop_code"])
            if key in seen:
                continue
            seen.add(key)
            also.append({
                "stop_class": item["stop_class"],
                "stop_code": item["stop_code"],
                "next_action": item["next_action"],
                "reason_ja": item["reason_ja"],
            })
        # 複数停止の併発時は優先順位で主たる1件を選ぶ (決定的)。
        # 単一の停止が複数クラスにまたがり分類不能な場合は、呼び出し側が
        # unknown コードで渡すことで missing_decision になる。
    # external_sends: 分類で増えた外部送信数 (実測差分。分類では常に0)
    end_calls = int(_EXTERNAL_SENDS_COUNTER.get("calls") or 0)
    external_sends = max(0, end_calls - start_calls)
    if isinstance(inputs, dict) and (
        "external_calls_before" in inputs or "external_calls_after" in inputs
    ):
        try:
            before = int(inputs.get("external_calls_before") or 0)
            after = int(inputs.get("external_calls_after") or start_calls)
            # 呼び出し側カウンタの差分を実測として採用
            external_sends = max(0, int(after) - int(before))
        except (TypeError, ValueError):
            external_sends = max(0, end_calls - start_calls)
    refs = _evidence_refs(inputs if isinstance(inputs, dict) else {})
    # policy_block は常に next_action none (迂回策を出さない)
    next_action = primary["next_action"]
    if primary["stop_class"] == "policy_block":
        next_action = "none"
    return {
        "stop_class": primary["stop_class"],
        "stop_code": primary["stop_code"],
        "evidence_refs": refs,
        "next_action": next_action,
        "reason_ja": primary["reason_ja"],
        "external_sends": int(external_sends),
        "also": also,
    }


def is_repeating(history: list[dict] | list, record: dict) -> bool:
    """同じ (plan_signature, stop_class, stop_code) が2回続いたら True。

    history は過去レコードの列、record は今回の分類レコード。
    新しい署名・新しい停止コードなら False (解除)。
    """
    if not isinstance(record, dict):
        return False
    sig = str(record.get("plan_signature") or record.get("evidence_refs", {}).get("plan_signature") or "")
    # record が evidence_refs 持ちの classify 出力の場合の吸収
    if not sig and isinstance(record.get("evidence_refs"), dict):
        sig = str(record["evidence_refs"].get("plan_signature") or "")
    cls = str(record.get("stop_class") or "")
    code = str(record.get("stop_code") or "")
    if not sig or not cls or not code:
        return False
    if not history:
        return False
    last = history[-1] if isinstance(history, list) else None
    if not isinstance(last, dict):
        return False
    last_sig = str(last.get("plan_signature") or (last.get("evidence_refs") or {}).get("plan_signature") or "")
    if not last_sig and isinstance(last.get("evidence_refs"), dict):
        last_sig = str(last["evidence_refs"].get("plan_signature") or "")
    return (
        last_sig == sig
        and str(last.get("stop_class") or "") == cls
        and str(last.get("stop_code") or "") == code
    )


# ----------------------------------------------------------------------------
# 空転ガード履歴 (sidecar *.stop.sqlite3 / stop_history)。追記のみ。
# GET系 (読み取り) では書かない。履歴を書くのはランのティックだけ。
# ----------------------------------------------------------------------------

def _db_path(manager) -> Path:
    source = Path(manager.memory.path)
    return source.with_name(source.name + ".stop.sqlite3")


@contextmanager
def _connect(manager):
    db = sqlite3.connect(_db_path(manager), timeout=15)
    db.row_factory = sqlite3.Row
    db.execute(
        """CREATE TABLE IF NOT EXISTS stop_history(
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            plan_signature TEXT NOT NULL,
            stop_class TEXT NOT NULL,
            stop_code TEXT NOT NULL,
            created REAL NOT NULL,
            actor TEXT NOT NULL DEFAULT '',
            idempotency_key TEXT NOT NULL DEFAULT '')"""
    )
    db.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_stop_history_idem
           ON stop_history(project_id, idempotency_key)"""
    )
    db.execute(
        """CREATE INDEX IF NOT EXISTS idx_stop_history_project
           ON stop_history(project_id, created)"""
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


def _decode_row(row: sqlite3.Row) -> dict:
    return {
        "id": str(row["id"]),
        "project_id": str(row["project_id"]),
        "plan_signature": str(row["plan_signature"]),
        "stop_class": str(row["stop_class"]),
        "stop_code": str(row["stop_code"]),
        "created": float(row["created"]),
        "actor": str(row["actor"] or ""),
        "idempotency_key": str(row["idempotency_key"] or ""),
    }


def read_history(manager, project_id: str, limit: int = 20) -> list[dict]:
    """停止履歴の読み取り (副作用なし)。"""
    pid = str(project_id or "")
    db = _read_connection(manager)
    if db is None:
        return []
    try:
        with db:
            try:
                rows = db.execute(
                    "SELECT * FROM stop_history WHERE project_id=? ORDER BY created, rowid LIMIT ?",
                    (pid, max(1, int(limit or 20))),
                ).fetchall()
            except sqlite3.OperationalError:
                return []
    finally:
        db.close()
    return [_decode_row(r) for r in rows]


def append_history(manager, project_id: str, record: dict,
                   actor: str = "auto-tick", idempotency_key: str = "") -> dict:
    """ティックでのみ追記する。同時ティックでも二重にしない (冪等キー)。

    record は classify_stop の出力または {plan_signature, stop_class, stop_code}。
    """
    pid = str(project_id or "")
    if not isinstance(record, dict):
        raise ValueError("record must be an object")
    sig = str(record.get("plan_signature") or "")
    if not sig and isinstance(record.get("evidence_refs"), dict):
        sig = str(record["evidence_refs"].get("plan_signature") or "")
    cls = str(record.get("stop_class") or "")
    code = str(record.get("stop_code") or "")
    if not sig or cls not in STOP_CLASSES or not code:
        raise ValueError("record must have plan_signature/stop_class/stop_code")
    key = str(idempotency_key or "").strip()
    rid = uuid.uuid4().hex
    if not key:
        key = rid
    now = time.time()
    with _connect(manager) as db:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute(
            "SELECT * FROM stop_history WHERE project_id=? AND idempotency_key=?",
            (pid, key),
        ).fetchone()
        if existing is not None:
            return _decode_row(existing)
        try:
            db.execute(
                """INSERT INTO stop_history(
                    id, project_id, plan_signature, stop_class, stop_code,
                    created, actor, idempotency_key)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (rid, pid, sig, cls, code, now, str(actor or "")[:100], key),
            )
        except sqlite3.IntegrityError:
            existing = db.execute(
                "SELECT * FROM stop_history WHERE project_id=? AND idempotency_key=?",
                (pid, key),
            ).fetchone()
            if existing is None:
                raise
            return _decode_row(existing)
    # 書き込み後の読み直し (追記のみ・更新なし)
    db = _read_connection(manager)
    if db is None:
        return {"id": rid, "project_id": pid, "plan_signature": sig,
                "stop_class": cls, "stop_code": code, "created": now,
                "actor": str(actor or "")[:100], "idempotency_key": key}
    try:
        with db:
            row = db.execute(
                "SELECT * FROM stop_history WHERE project_id=? AND id=?", (pid, rid)
            ).fetchone()
    finally:
        db.close()
    if row is None:
        return {"id": rid, "project_id": pid, "plan_signature": sig,
                "stop_class": cls, "stop_code": code, "created": now,
                "actor": str(actor or "")[:100], "idempotency_key": key}
    return _decode_row(row)


def flatten_for_history(record: dict) -> dict:
    """classify 出力を履歴用の平坦形にする。"""
    refs = record.get("evidence_refs") if isinstance(record, dict) else {}
    sig = ""
    if isinstance(record, dict):
        sig = str(record.get("plan_signature") or "")
        if not sig and isinstance(refs, dict):
            sig = str(refs.get("plan_signature") or "")
    return {
        "plan_signature": sig,
        "stop_class": str((record or {}).get("stop_class") or ""),
        "stop_code": str((record or {}).get("stop_code") or ""),
    }


def guard_auto_tick(manager, project_id: str, run: dict,
                    record: dict | None = None) -> dict:
    """自動ランのティック入口用ガード。履歴の追記と繰返し判定だけを行う。

    戻り値: {"repeating": bool, "record": flat, "history": [...]}。
    人の選択 (manual_bindings / remap_history の追加) があれば解除 (repeating=False)。
    DB書込はこの関数 (ティック) でのみ行い、GETでは呼ばない。
    """
    pid = str(project_id or "")
    run = run if isinstance(run, dict) else {}
    if record is None:
        sig = str(run.get("plan_signature") or "")
        record = classify_stop({"run": run, "plan_signature": sig})
    flat = flatten_for_history(record)
    if not flat.get("plan_signature"):
        flat["plan_signature"] = str(run.get("plan_signature") or "")
    history = read_history(manager, pid, limit=20)
    # 人の選択があれば解除: run に manual_bindings/remap があれば繰返しにしない
    human_touched = bool(run.get("manual_bindings")) or bool(run.get("remap_history"))
    if human_touched:
        return {"repeating": False, "record": flat, "history": history,
                "reason": "人の選択があるため空転ガードを解除"}
    repeating = is_repeating(history, flat)
    return {"repeating": bool(repeating), "record": flat, "history": history,
            "reason": "同じ停止の繰り返し" if repeating else ""}
