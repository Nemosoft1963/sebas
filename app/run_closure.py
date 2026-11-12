"""P0-2 実行ループの終了規則(判定・分類・上限の純粋部品)。

_run() の終了処理へ接続するための規則だけを置く。LLM出力は読まない。
不明・未検証・未承認・読取不能を PASS・達成・承認済み・0 に変換しない。
finished だけを条件に公開・RAG登録しない(既存ゲートを維持)。
"""
from __future__ import annotations

import uuid

P02_VERSION = "p02-run-closure-v1"
# 項目単位の上限(初期値2回)。P0B_MAX_REPAIR_ATTEMPTS と一致させる。
P02_MAX_AUTO_REPAIR_PER_ITEM = 2


def new_run_id(project_id: str, plan_version: int | str = 0) -> str:
    pid = str(project_id or "p").strip() or "p"
    try:
        ver = int(plan_version)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        ver = 0
    return f"{pid}-v{ver}-{uuid.uuid4().hex[:8]}"


def map_open_kind(failure_kind: str) -> str:
    """P0-B の失敗分類を残件台帳の kind(OPEN_ITEM_KINDS)へ写す。推測しない。"""
    kind = str(failure_kind or "")
    if kind == "artifact_missing":
        return "artifact_missing"
    if kind == "phrase_missing":
        return "phrase_missing"
    if kind == "unsupported_fact":
        return "unsupported_fact"
    if kind == "human_decision":
        return "human_decision"
    if kind in ("external_evidence", "external_result"):
        # 実施しなければ決まらない事項は実施結果待ち(external_result)で閉じる。
        return "external_result"
    if kind == "system_error":
        return "system_error"
    if kind == "wrong_target":
        # 対象パスの取り違えは成果物側の不足として残す(契約は書き換えない)。
        return "artifact_missing"
    if kind == "semantic_quote_mismatch":
        # 引用不一致は人の確認フローで記録する(機械のPASSにしない)。
        return "human_decision"
    return "unclassified"


def owner_for_open_kind(open_kind: str) -> str:
    if open_kind in ("human_decision", "unclassified"):
        return "human"
    if open_kind in ("external_result", "external_evidence"):
        return "external"
    return "system"


def pending_kind_for_open_kinds(open_kinds: list[str]) -> str:
    kinds = [str(k or "") for k in (open_kinds or [])]
    if any(k in ("external_result", "external_evidence") for k in kinds):
        return "external_result"
    if "human_decision" in kinds:
        return "human_decision"
    if any(k in ("artifact_missing", "phrase_missing") for k in kinds):
        return "artifact"
    if "unsupported_fact" in kinds:
        return "source_fact"
    if "system_error" in kinds:
        return "verification"
    if kinds:
        return "verification"
    return "none"


def decide_verdict(unmet_rows: list[dict]) -> tuple[str, str]:
    """残件行から goal_verdict と pending_kind の素案を決める。規則のみ。

    FAIL が1件でもあれば unmet(成果物不足・検証不合格)。
    UNTESTABLE だけなら undetermined(検証不能・実施後でなければ決まらない)。
    """
    rows = [r for r in (unmet_rows or []) if isinstance(r, dict)]
    if not rows:
        return "undetermined", "none"
    kinds = [map_open_kind(str(r.get("_failure_kind") or r.get("kind") or "")) for r in rows]
    # _failure_kind が無ければ P0-B 分類を呼び出し側で付与する。ここでは kind 優先。
    pending = pending_kind_for_open_kinds(kinds)
    results = [str(r.get("result") or "") for r in rows]
    if any(r == "FAIL" for r in results):
        return "unmet", pending
    return "undetermined", pending


def build_open_item_params(
    *,
    run_id: str,
    row: dict,
    failure_kind: str,
    signature: str = "",
) -> dict:
    """残件1件分の add_open_item 引数を作る。項目単位で保存する。"""
    from app.requirement_verification import failure_operator_hint

    item_id = str((row or {}).get("item_id") or "")
    criterion_id = str((row or {}).get("criterion_id") or "")
    artifact_path = str((row or {}).get("artifact_path") or "")
    method = str((row or {}).get("method") or "")
    result = str((row or {}).get("result") or "")
    reason = str((row or {}).get("reason") or "")[:500]
    open_kind = map_open_kind(failure_kind)
    owner = owner_for_open_kind(open_kind)
    hint = failure_operator_hint(failure_kind if failure_kind else "unclassified")
    artifacts = [artifact_path] if artifact_path else []
    if open_kind == "external_result":
        status_text = (
            f"{item_id or '(項目不明)'}: 実施結果待ち({result})。{reason or '実施後の数値が必要'}"
        )[:2000]
        evidence_needed = (
            "必要な観測値: 実施後に得られる実測値(数値・日時・対象)。"
            "証拠形式: 承認記録と実在する結果(原本・公開URL・回答同期など)。"
            f"登録先: 当該PJへ証拠登録し、新しいrun_idで再判定する。対象={artifact_path or '未定'}"
        )[:2000]
        next_action = (
            f"人間が外部実施を行い証拠を登録する。{hint} "
            "証拠登録後は同じ実行を改ざんせず新しいrun_idで再開する。"
        )[:2000]
    elif open_kind == "unclassified":
        status_text = (
            f"{item_id or '(項目不明)'}: 分類不能({result})。{reason or '失敗分類を確定できない'}"
        )[:2000]
        evidence_needed = (
            "必要な証拠: 人が対象成果物・検証方法・失敗理由を確認し、"
            "適切な失敗分類と根拠を記録する。"
        )[:2000]
        next_action = (
            "分類できないため人が確認し、分類と必要な修正または証拠登録手順を決定する。"
        )[:2000]
    elif open_kind == "human_decision":
        status_text = (
            f"{item_id or '(項目不明)'}: 人判断待ち({result})。{reason or '人の確認・承認が必要'}"
        )[:2000]
        evidence_needed = (
            "必要な証拠: 人の確認・承認の記録(担当者・理由・引用)。"
            "承認の代行・自動付与はしない。未承認のまま終了する。"
        )[:2000]
        next_action = (
            f"{hint} 承認記録を受けた後に次のサイクル(新しいrun_id)を起動する。"
        )[:2000]
    else:
        status_text = (
            f"{item_id or '(項目不明)'}({criterion_id or '-'}): {result} "
            f"[{failure_kind or 'unclassified'}/{method or '-'}] {reason or '未達'}"
        )[:2000]
        evidence_needed = (
            f"必要な証拠: 対象成果物の修正と項目別検証の再検証。対象={artifact_path or '未定'}。"
            f"分類={failure_kind or 'unclassified'}。推測値・仮の成功で埋めない。"
        )[:2000]
        next_action = (
            f"{hint} "
            f"上限({P02_MAX_AUTO_REPAIR_PER_ITEM}回)を超えたら追補せず残件として終了する。"
        )[:2000]
    key_source = f"{run_id}:{item_id}:{signature or failure_kind or 'unclassified'}"
    return {
        "item_id": item_id,
        "kind": open_kind,
        "status_text": status_text,
        "evidence_needed": evidence_needed,
        "owner": owner,
        "next_action": next_action,
        "artifacts": artifacts,
        "idempotency_key": key_source[:200],
    }
