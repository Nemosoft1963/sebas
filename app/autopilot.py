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
    "missing_evidence",
})

# B-3: missing_capability は下書きまで自走させる (人の関門より前で止まる)。
# HUMAN_STOPS には入れない。ACTION_FOR_STOP で capability_preflight を選ぶ。

# B-3: 提案の状態から次の人の操作を導くための順序 (決定的・ソート用)。
# review/export/取込み/apply は自動化しない。文言だけ記録する。
_B3_STATUS_ORDER = (
    "draft",
    "needs_evidence",
    "reviewed",
    "export_ready",
    "submitted",
    "delivered",
    "verified",
    "rejected",
    "stale",
)


def _b3_status_rank(status: str) -> int:
    try:
        return list(_B3_STATUS_ORDER).index(str(status or ""))
    except ValueError:
        return 999


def _summarize_b3_proposals(manager, project_id: str,
                            plan_signature: str) -> dict:
    """同じ計画署名の不足提案の要約 (読み取り専用)。

    戻り値: {"state": str, "ids": list, "by_status": dict, "total": int}。
    IDと件数のみ。秘密・達成条件本文・原本本文を含めない。
    読取不能・形式不正は例外で返す (呼出側が fail-closed で止まる)。
    """
    from app import capability_gap as gap

    pid = str(project_id or "")
    sig = str(plan_signature or "")
    if not sig:
        raise ValueError("plan_signature is required")
    rows = gap.list_proposals(manager, pid, plan_signature=sig)
    if not isinstance(rows, list):
        raise ValueError("提案の一覧を確認できません")
    missing_rows: list[dict] = []
    for item in rows:
        if not isinstance(item, dict):
            raise ValueError("提案の形式を確認できません")
        if str(item.get("gap_code") or "") != "missing_capability":
            continue
        if str(item.get("plan_signature") or "") != sig:
            continue
        proposal_id = item.get("proposal_id")
        status = item.get("status")
        if not proposal_id or not status:
            raise ValueError("提案のIDと状態を確認できません")
        missing_rows.append(item)
    by_status: dict[str, int] = {}
    for item in missing_rows:
        status = str(item.get("status") or "")
        if status not in by_status:
            by_status[status] = 0
        by_status[status] = int(by_status[status]) + 1
    ordered = sorted(by_status.keys())
    if ordered:
        inner = ",".join(f"{key}:{int(by_status[key])}" for key in ordered)
        state = f"{len(missing_rows)}件({inner})"
    else:
        state = "0件"
    ids = sorted(str(item.get("proposal_id") or "") for item in missing_rows)
    ids = [pid_text for pid_text in ids if pid_text][:20]
    return {"state": state, "ids": ids, "by_status": dict(by_status),
            "total": int(len(missing_rows))}

_B3_IDLE_CODES = frozenset({
    "phase:idle",
    "unknown:empty",
    "unknown:unclassifiable",
})


def _b3_blocking_also(record: dict) -> tuple[bool, str]:
    """併存停止に安全境界・実際の人の判断待ちが無いか (読み取り専用)。

    True は下書きを作らない (fail-closed)。idle系の分類不能は実際の判断待ち
    とみなさない (不足を隠さない)。形式不正・読取不能は通過扱いにしない。
    """
    if not isinstance(record, dict):
        return True, "分類記録を確認できないため進めません:empty_record"
    if "also" not in record:
        return True, "併存停止を確認できないため進めません:empty_record"
    raw_also = record.get("also")
    if not isinstance(raw_also, list):
        return True, "併存停止を確認できないため進めません:TypeError"
    for item in raw_also:
        if not isinstance(item, dict):
            return True, "併存停止を確認できないため進めません:TypeError"
        cls_val = item.get("stop_class")
        code_val = item.get("stop_code")
        if not cls_val or not code_val:
            return True, "併存停止を確認できないため進めません:ValueError"
        cls_text = str(cls_val)
        code_text = str(code_val)
        if cls_text == "policy_block":
            return True, _safe_text(
                f"安全境界の停止があるため下書きを作りません:{code_text}", 200)
        if cls_text == "missing_decision" and code_text not in _B3_IDLE_CODES:
            return True, _safe_text(
                f"人の判断待ちがあるため下書きを作りません:{code_text}", 200)
    return False, ""


def _b3_should_auto_draft(manager, project_id: str, record: dict,
                          plan_signature: str) -> tuple[bool, str]:
    """B-3 の自動選択の判定 (実行とdry_runで同じ関数を使う)。

    (b) 主たる停止が missing_capability で併存に安全境界・実際の判断待ちが無い
    (c) 同じ計画署名の提案が読める (existingのみ・提案ゼロのいずれも可。
    冪等で重複を作らないため)。(a)(d) は呼出側のゲート・有効化・予算・空転で見る。
    読取不能・形式不正は False (作らない)。
    """
    if not isinstance(record, dict):
        return False, "分類記録を確認できないため進めません:empty_record"
    if str(record.get("stop_class") or "") != "missing_capability":
        return False, "主たる停止が機能不足ではないため下書きを作りません:not_missing"
    blocked, block_reason = _b3_blocking_also(record)
    if blocked:
        return False, block_reason
    sig = str(plan_signature or "")
    if not sig:
        return False, "計画署名を確認できないため進めません:empty_signature"
    try:
        summary = _summarize_b3_proposals(manager, str(project_id or ""), sig)
    except Exception as exc:
        return False, f"提案を確認できないため進めません:{type(exc).__name__}"
    if not isinstance(summary, dict):
        return False, "提案を確認できないため進めません:TypeError"
    return True, ""


def _b3_predict_outcome(manager, project_id: str,
                        plan_signature: str) -> dict:
    """dry_run用の outcome 予測 (読み取り専用・何も書かない)。

    戻り値 {"outcome", "proposal_state", "proposal_ids", "new_count",
    "by_status"}。outcome は created/existing/skipped/error のいずれか。
    state/ids/by_status は実行後の予測 (created の場合も実行後の件数・ID・
    状態別件数まで決定的に求まる。冪等キーが決定的なため)。予告と実行の
    食い違いを避けるための予測であり、読取不能・形式不正・署名のずれは
    error + 現在読める要約 (読めなければ「確認できず」)。通過扱いにしない。
    """
    pid = str(project_id or "")
    sig = str(plan_signature or "")
    if not pid or not sig:
        return {"outcome": "error", "proposal_state": "確認できず",
                "proposal_ids": [], "new_count": 0, "by_status": {}}
    try:
        cur = _summarize_b3_proposals(manager, pid, sig)
        cur_state = str(cur.get("state") or "0件")
        cur_ids = list(cur.get("ids") or []) if isinstance(
            cur.get("ids"), list) else []
        cur_by = dict(cur.get("by_status") or {}) if isinstance(
            cur.get("by_status"), dict) else {}
        cur_total = int(cur.get("total") or 0)
    except Exception:
        return {"outcome": "error", "proposal_state": "確認できず",
                "proposal_ids": [], "new_count": 0, "by_status": {}}

    def _cur_result(outcome: str) -> dict:
        return {"outcome": outcome, "proposal_state": cur_state,
                "proposal_ids": cur_ids[:20], "new_count": 0,
                "by_status": dict(cur_by)}

    try:
        from app import capability_gap as gap

        preflight = gap.capability_preflight(manager, pid)
    except Exception:
        return _cur_result("error")
    if not isinstance(preflight, dict):
        return _cur_result("error")
    raw_gaps = preflight.get("gaps")
    raw_missing = preflight.get("missing_capability")
    pre_sig = str(preflight.get("plan_signature") or "")
    if not pre_sig or pre_sig != sig or not isinstance(raw_gaps, list) \
            or not isinstance(raw_missing, list):
        return _cur_result("error")
    missing_gaps = [g for g in raw_gaps if isinstance(g, dict)
                    and str(g.get("gap_code") or "") == "missing_capability"]
    if not missing_gaps and not raw_missing:
        return _cur_result("skipped")
    if not missing_gaps and raw_missing:
        return _cur_result("error")
    try:
        from app.capability_gap import _stable_id as _gap_stable

        rows = gap.list_proposals(manager, pid, plan_signature=pre_sig)
    except Exception:
        return _cur_result("error")
    if not isinstance(rows, list):
        return _cur_result("error")
    expected: list[str] = []
    for gap_item in missing_gaps:
        cid = gap_item.get("criterion_id")
        cap = gap_item.get("required_capability_id")
        code = gap_item.get("gap_code")
        if not cid or not cap or not code:
            return _cur_result("error")
        try:
            eid = _gap_stable(pid, pre_sig, str(cid), str(cap), str(code))
        except Exception:
            return _cur_result("error")
        if not eid:
            return _cur_result("error")
        expected.append(str(eid))
    existing_ids: list[str] = []
    for row_item in rows:
        if not isinstance(row_item, dict):
            return _cur_result("error")
        if str(row_item.get("gap_code") or "") != "missing_capability":
            continue
        if str(row_item.get("plan_signature") or "") != pre_sig:
            continue
        proposal_val = row_item.get("proposal_id")
        status_val = row_item.get("status")
        if not proposal_val or not status_val:
            return _cur_result("error")
        existing_ids.append(str(proposal_val))
    existing_set = set(existing_ids)
    new_ids = [eid for eid in expected if eid not in existing_set]
    if not new_ids:
        return _cur_result("existing")
    # created: 実行後は期待IDが全て揃い、新規分は draft で増える。
    # 既存の同署名提案の状態は自動で変わらない (巻き戻しなし)。
    post_ids = sorted(set(cur_ids) | set(expected))[:20]
    post_total = int(cur_total) + len(new_ids)
    post_by = dict(cur_by)
    post_by["draft"] = int(post_by.get("draft") or 0) + len(new_ids)
    ordered = sorted(post_by.keys())
    inner = ",".join(f"{key}:{int(post_by[key])}" for key in ordered)
    post_state = f"{int(post_total)}件({inner})"
    return {"outcome": "created", "proposal_state": post_state,
            "proposal_ids": post_ids, "new_count": len(new_ids),
            "by_status": post_by}


def _b3_is_repeat_guarded(history: list, flat: dict) -> bool:
    """B-3用の空転ガード。同じ停止が3回目になる場合だけ True。

    1回目の下書き(created)、2回目の確認(existing)を許し、3回目で止める。
    履歴・記録の形式不正・読取不能は False (通過扱いにせず、呼出側の
    既存ガードと冪等で重複は作らない)。決定的・ソート不要 (順序比較のみ)。
    """
    if not isinstance(history, list) or not isinstance(flat, dict):
        return False
    sig = str(flat.get("plan_signature") or "")
    cls = str(flat.get("stop_class") or "")
    code = str(flat.get("stop_code") or "")
    if not sig or not cls or not code:
        return False
    if len(history) < 2:
        return False
    last_two = history[-2:]
    for item in last_two:
        if not isinstance(item, dict):
            return False
        item_sig = str(item.get("plan_signature") or "")
        if not item_sig and isinstance(item.get("evidence_refs"), dict):
            try:
                item_sig = str(item["evidence_refs"].get("plan_signature") or "")
            except Exception:
                item_sig = ""
        if item_sig != sig:
            return False
        if str(item.get("stop_class") or "") != cls:
            return False
        if str(item.get("stop_code") or "") != code:
            return False
    return True


def _b3_reason_for_existing(summary: dict) -> str:
    by = summary.get("by_status") if isinstance(summary, dict) else None
    state = str(summary.get("state") or "0件") if isinstance(summary, dict) else "0件"
    if not isinstance(by, dict) or not by:
        return _safe_text(
            f"不足機能の提案があります。内容を確認してください({state})", 1000)
    present = [str(k) for k in by.keys() if str(k)]
    best = ""
    best_rank = 9999
    for key in present:
        rank = _b3_status_rank(key)
        if rank < best_rank:
            best_rank = rank
            best = key
    if best == "draft":
        return _safe_text(
            f"不足機能の提案の下書きがあります。内容を確認してください({state})", 1000)
    if best == "needs_evidence":
        return _safe_text(
            f"不足機能の提案に証拠が必要です。証拠を確認してください({state})", 1000)
    if best == "reviewed":
        return _safe_text(
            f"不足機能の提案は確認済みです。指示を出力してください({state})", 1000)
    if best == "export_ready":
        return _safe_text(
            f"不足機能の提案は指示出力済みです。Jenkinsへ渡した記録をしてください({state})", 1000)
    if best == "submitted":
        return _safe_text(
            f"不足機能の提案はJenkinsへ渡済みです。納品を取り込んでください({state})", 1000)
    if best == "delivered":
        return _safe_text(
            f"不足機能の提案は納品済みです。再判定してください({state})", 1000)
    if best == "verified":
        return _safe_text(
            f"不足機能の提案は検証済みです({state})", 1000)
    if best == "rejected":
        return _safe_text(
            f"不足機能の提案は却下されています。内容を確認してください({state})", 1000)
    if best == "stale":
        return _safe_text(
            f"不足機能の提案は古い計画署名のため無効です。新しい署名で再判定してください({state})", 1000)
    return _safe_text(
        f"不足機能の提案があります。内容を確認してください({state})", 1000)

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
    # B-3: missing_capability は不足機能の提案の下書きまで自走させる一手。
    # review/export/取込み/apply は自動化しない (人の関門)。
    "missing_capability": "capability_preflight",
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
    # F1: 失敗を握りつぶして次候補へ進めない。全件失敗は成功にしない。
    preview_info: dict = {}
    _preview_errors: list[str] = []
    _preview_tried = False
    try:
        runs = list_runs(manager, pid)
    except (ValueError, TypeError, sqlite3.Error, OSError, RuntimeError) as exc:
        raise _CandidateNotAvailable(
            f"保存済みランの確認に失敗しました:{type(exc).__name__}") from exc
    for run in runs:
        if not isinstance(run, dict):
            continue
        if (str(run.get("state") or "") == "stopped"
                and str(run.get("stop_kind") or "") == "human_required"):
            _preview_tried = True
            try:
                preview_info = remap_preview(manager, pid, str(run.get("id") or ""))
                break
            except (ValueError, TypeError, sqlite3.Error, OSError,
                    RuntimeError) as exc:
                _preview_errors.append(type(exc).__name__)
                continue
    if _preview_tried and not isinstance(preview_info, dict):
        raise _CandidateNotAvailable("再対応付けの確認に失敗しました:TypeError")
    if _preview_tried and not (preview_info or {}).get("bindings"):
        if _preview_errors:
            raise _CandidateNotAvailable(
                f"再対応付けの確認に失敗しました:{_preview_errors[0]}")
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
    """事前照合の再実行と提案の生成。冪等。外部呼出なし。

    戻り値は四値 (outcome): created(新規)/existing(既存あり・重複なし)/
    skipped(条件を満たさない・理由つき)/error(生成に失敗・型名のみ)。
    既存キーは維持する。error は「0件の成功」にしない。
    reviewed/rejected/stale を自動で巻き戻さない。
    """
    from app import capability_gap as gap

    pid = str(project_id)
    try:
        preflight = gap.capability_preflight(manager, pid)
    except Exception as exc:
        etype = type(exc).__name__
        return {
            "action": "capability_preflight",
            "plan_signature": "",
            "gap_codes": [],
            "proposals": 0,
            "external_calls": 0,
            "outcome": "error",
            "error_type": etype,
            "reason": f"事前照合に失敗したため提案を生成できません:{etype}",
        }
    if not isinstance(preflight, dict):
        return {
            "action": "capability_preflight",
            "plan_signature": "",
            "gap_codes": [],
            "proposals": 0,
            "external_calls": 0,
            "outcome": "error",
            "error_type": "TypeError",
            "reason": "事前照合の形式不正のため提案を生成できません:TypeError",
        }
    raw_sig = preflight.get("plan_signature")
    sig = _safe_text(str(raw_sig or ""), 128)
    raw_codes = preflight.get("gap_codes")
    if isinstance(raw_codes, list):
        gap_codes = [str(x) for x in raw_codes if str(x)]
    else:
        return {
            "action": "capability_preflight",
            "plan_signature": sig,
            "gap_codes": [],
            "proposals": 0,
            "external_calls": 0,
            "outcome": "error",
            "error_type": "TypeError",
            "reason": "事前照合の形式不正のため提案を生成できません:TypeError",
        }
    try:
        generated = gap.generate_proposals(manager, pid, preflight, actor="autopilot")
    except Exception as exc:
        etype = type(exc).__name__
        return {
            "action": "capability_preflight",
            "plan_signature": sig,
            "gap_codes": gap_codes,
            "proposals": 0,
            "external_calls": 0,
            "outcome": "error",
            "error_type": etype,
            "reason": f"提案の生成に失敗しました:{etype}",
        }
    if not isinstance(generated, dict):
        return {
            "action": "capability_preflight",
            "plan_signature": sig,
            "gap_codes": gap_codes,
            "proposals": 0,
            "external_calls": 0,
            "outcome": "error",
            "error_type": "TypeError",
            "reason": "提案の生成結果の形式不正のため確認できません:TypeError",
        }
    if "proposals" not in generated:
        return {
            "action": "capability_preflight",
            "plan_signature": sig,
            "gap_codes": gap_codes,
            "proposals": 0,
            "external_calls": 0,
            "outcome": "error",
            "error_type": "KeyError",
            "reason": "提案の生成結果を確認できません:KeyError",
        }
    raw_props = generated.get("proposals")
    if not isinstance(raw_props, list):
        return {
            "action": "capability_preflight",
            "plan_signature": sig,
            "gap_codes": gap_codes,
            "proposals": 0,
            "external_calls": 0,
            "outcome": "error",
            "error_type": "TypeError",
            "reason": "提案の生成結果を確認できません:TypeError",
        }
    if len(raw_props) > 0:
        if "created" not in generated:
            return {
                "action": "capability_preflight",
                "plan_signature": sig,
                "gap_codes": gap_codes,
                "proposals": len(raw_props),
                "external_calls": 0,
                "outcome": "error",
                "error_type": "KeyError",
                "reason": "提案の生成結果を確認できません:KeyError",
            }
        raw_created = generated.get("created")
        if isinstance(raw_created, bool) or not isinstance(raw_created, int):
            return {
                "action": "capability_preflight",
                "plan_signature": sig,
                "gap_codes": gap_codes,
                "proposals": len(raw_props),
                "external_calls": 0,
                "outcome": "error",
                "error_type": "TypeError",
                "reason": "提案の生成結果を確認できません:TypeError",
            }
        if raw_created > 0:
            return {
                "action": "capability_preflight",
                "plan_signature": sig,
                "gap_codes": gap_codes,
                "proposals": len(raw_props),
                "external_calls": 0,
                "outcome": "created",
                "created": int(raw_created),
            }
        if raw_created == 0:
            return {
                "action": "capability_preflight",
                "plan_signature": sig,
                "gap_codes": gap_codes,
                "proposals": len(raw_props),
                "external_calls": 0,
                "outcome": "existing",
                "created": 0,
            }
        return {
            "action": "capability_preflight",
            "plan_signature": sig,
            "gap_codes": gap_codes,
            "proposals": len(raw_props),
            "external_calls": 0,
            "outcome": "error",
            "error_type": "ValueError",
            "reason": "提案の生成結果を確認できません:ValueError",
        }
    # 提案0件: 不足があるのに0件は成功にしない (error)。不足なしは skipped。
    raw_gaps = preflight.get("gaps")
    if isinstance(raw_gaps, list):
        for item in raw_gaps:
            if isinstance(item, dict) and item.get("gap_code") == "missing_capability":
                return {
                    "action": "capability_preflight",
                    "plan_signature": sig,
                    "gap_codes": gap_codes,
                    "proposals": 0,
                    "external_calls": 0,
                    "outcome": "error",
                    "error_type": "ValueError",
                    "reason": "不足があるのに提案が0件のため確認できません:ValueError",
                }
    raw_missing = preflight.get("missing_capability")
    if isinstance(raw_missing, list) and len(raw_missing) > 0:
        return {
            "action": "capability_preflight",
            "plan_signature": sig,
            "gap_codes": gap_codes,
            "proposals": 0,
            "external_calls": 0,
            "outcome": "error",
            "error_type": "ValueError",
            "reason": "不足があるのに提案が0件のため確認できません:ValueError",
        }
    if "skipped_gap_codes" in generated:
        raw_skipped = generated.get("skipped_gap_codes")
        if not isinstance(raw_skipped, list):
            return {
                "action": "capability_preflight",
                "plan_signature": sig,
                "gap_codes": gap_codes,
                "proposals": 0,
                "external_calls": 0,
                "outcome": "error",
                "error_type": "TypeError",
                "reason": "提案の生成結果を確認できません:TypeError",
            }
        skipped_codes = [str(x) for x in raw_skipped if str(x)]
    else:
        skipped_codes = list(gap_codes)
    reason_text = "作る条件を満たさないため生成しません"
    if skipped_codes:
        reason_text = f"作る条件を満たさないため生成しません:{','.join(sorted(set(skipped_codes)))}"
    return {
        "action": "capability_preflight",
        "plan_signature": sig,
        "gap_codes": gap_codes,
        "proposals": 0,
        "external_calls": 0,
        "outcome": "skipped",
        "skipped_gap_codes": skipped_codes,
        "reason": _safe_text(reason_text, 200),
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
    # fail-closed: 監査の書込失敗は握りつぶさない。呼出側が失敗として扱う。
    safe = {k: _safe_text(v, 200) for k, v in dict(detail or {}).items()}
    manager.memory.add_event(str(project_id), kind,
                             _safe_text(f"autopilot:{kind}", 200),
                             detail=json.dumps(safe, ensure_ascii=False))


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
        if not isinstance(record, dict):
            record = {}
        record["preflight_status"] = "unknown"
        record["preflight_reason"] = type(exc).__name__
        return {}, record
    try:
        record = build_stop_class(manager, pid, built)
    except Exception as exc:
        record = classify_stop({})
        if not isinstance(record, dict):
            record = {}
        record["preflight_status"] = "unknown"
        record["preflight_reason"] = type(exc).__name__
        return built if isinstance(built, dict) else {}, record
    if not isinstance(record, dict) or not record.get("stop_class"):
        record = classify_stop({})
        if not isinstance(record, dict):
            record = {}
        record["preflight_status"] = "unknown"
        record["preflight_reason"] = "empty_record"
        return built if isinstance(built, dict) else {}, record
    return built if isinstance(built, dict) else {}, record


def _preflight_gate(manager, project_id: str, record: dict) -> dict | None:
    """作業選択の前の事前照合ゲート。通れなければ停止理由を返す。

    戻り値 None は通過 (従来どおり進める)。dict は停止
    (gate_status=preflight_unknown/stale、reason_ja は型名まで)。
    読み取り専用。候補生成・提案生成・ローカルAI・apply を呼ばない。
    """
    pid = str(project_id or "")
    refs = record.get("evidence_refs") if isinstance(record, dict) else None
    refs = refs if isinstance(refs, dict) else {}
    classified_sig = ""
    if isinstance(record, dict):
        raw_sig = record.get("plan_signature") or refs.get("plan_signature")
        classified_sig = str(raw_sig or "")[:128]
    preflight_status = str(record.get("preflight_status") or "") if isinstance(record, dict) else ""
    preflight_reason = str(record.get("preflight_reason") or "") if isinstance(record, dict) else ""
    # 現署名の取得 (取れなければ読取不能)。
    try:
        from app.goal_review import plan_snapshot

        _snapshot, current_sig_raw = plan_snapshot(manager, pid)
        current_sig = str(current_sig_raw or "")[:128]
    except Exception as exc:
        etype = type(exc).__name__
        return {
            "gate_status": "preflight_unknown",
            "preflight_status": preflight_status or "unknown",
            "preflight_reason": etype,
            "plan_signature": classified_sig,
            "reason_ja": f"事前照合または計画署名を確認できないため進めません:{etype}",
        }
    if not current_sig:
        return {
            "gate_status": "preflight_unknown",
            "preflight_status": preflight_status or "unknown",
            "preflight_reason": preflight_reason or "empty_signature",
            "plan_signature": classified_sig,
            "reason_ja": "計画署名を確認できないため進めません:empty_signature",
        }
    # preflight_status が "ok" の記録だけ通す。欠落・unknown・失敗は
    # いずれも unknown として止める (テスト用の迂回は置かない)。
    if preflight_status != "ok":
        reason = preflight_reason or "unknown"
        return {
            "gate_status": "preflight_unknown",
            "preflight_status": preflight_status or "unknown",
            "preflight_reason": reason,
            "plan_signature": current_sig,
            "reason_ja": f"事前照合を確認できないため進めません:{reason}",
        }
    if not classified_sig:
        reason = preflight_reason or "empty_signature"
        return {
            "gate_status": "preflight_unknown",
            "preflight_status": preflight_status,
            "preflight_reason": reason,
            "plan_signature": current_sig,
            "reason_ja": "停止分類の署名を確認できないため進めません:empty_signature",
        }
    if classified_sig != current_sig:
        return {
            "gate_status": "stale",
            "preflight_status": preflight_status,
            "preflight_reason": "stale_signature",
            "plan_signature": current_sig,
            "reason_ja": "停止分類が古い署名のため進めません:stale_signature",
        }
    return None


def preview_planned_action(manager, project_id: str, record: dict,
                           plan_signature: str) -> dict:
    """許可アクションの予告だけ返す純粋関数 (読み取り専用)。

    書込0・外部呼出0。dry_run ティックと進捗モデルの次の一手が同じ判定を使う。
    ティックの実行経路は変えない。読取不能は planned_action=none。
    """
    pid = str(project_id or "")
    rec = record if isinstance(record, dict) else {}
    stop_class = str(rec.get("stop_class") or "")
    stop_code = str(rec.get("stop_code") or "")
    plan_sig = str(plan_signature or "")[:128]
    next_action = str(rec.get("next_action") or "none")
    reason_ja = _safe_text(str(rec.get("reason_ja") or ""), 1000)
    preflight_status = str(rec.get("preflight_status") or "")
    preflight_reason = str(rec.get("preflight_reason") or "")
    empty = {
        "planned_action": "none",
        "gate_status": "preflight_unknown",
        "preflight_status": preflight_status or "unknown",
        "preflight_reason": preflight_reason or "unknown",
        "reason_ja": "",
        "next_action": next_action,
        "proposal_state": "",
        "proposal_ids": [],
        "capability_outcome": "skipped",
        "plan_signature": plan_sig,
    }
    gate = None
    gate_error = ""
    try:
        gate = _preflight_gate(manager, pid, rec)
    except Exception as exc:
        gate_error = type(exc).__name__
    if gate_error != "":
        out = dict(empty)
        out["reason_ja"] = _safe_text(
            "事前照合または計画署名を確認できないため進めません:" + gate_error,
            1000)
        out["preflight_reason"] = gate_error
        return out
    if gate is not None:
        if not isinstance(gate, dict):
            out = dict(empty)
            out["reason_ja"] = "事前照合を確認できないため進めません:TypeError"
            out["preflight_reason"] = "TypeError"
            return out
        gate_sig = str(gate.get("plan_signature") or "")[:128]
        return {
            "planned_action": "none",
            "gate_status": str(gate.get("gate_status") or "preflight_unknown"),
            "preflight_status": str(gate.get("preflight_status") or "unknown"),
            "preflight_reason": str(gate.get("preflight_reason") or "unknown"),
            "reason_ja": _safe_text(str(gate.get("reason_ja") or ""), 1000),
            "next_action": next_action,
            "proposal_state": "",
            "proposal_ids": [],
            "capability_outcome": "skipped",
            "plan_signature": gate_sig if gate_sig != "" else plan_sig,
        }
    if stop_class == "missing_capability":
        should = False
        should_reason = ""
        should_error = ""
        try:
            should, should_reason = _b3_should_auto_draft(
                manager, pid, rec, plan_sig)
        except Exception as exc:
            should_error = type(exc).__name__
        if should_error != "":
            out = dict(empty)
            out["gate_status"] = "ok"
            out["preflight_status"] = preflight_status
            out["preflight_reason"] = preflight_reason
            out["reason_ja"] = _safe_text(
                "下書きの条件を確認できないため進めません:" + should_error, 1000)
            return out
        if not bool(should):
            return {
                "planned_action": "record_stop_class",
                "gate_status": "ok",
                "preflight_status": preflight_status,
                "preflight_reason": preflight_reason,
                "reason_ja": _safe_text(should_reason, 1000),
                "next_action": next_action,
                "proposal_state": "",
                "proposal_ids": [],
                "capability_outcome": "skipped",
                "plan_signature": plan_sig,
            }
        forecast = None
        forecast_error = ""
        try:
            forecast = _b3_predict_outcome(manager, pid, plan_sig)
        except Exception as exc:
            forecast_error = type(exc).__name__
        if forecast_error != "" or not isinstance(forecast, dict):
            forecast = {"outcome": "error", "proposal_state": "確認できず",
                        "proposal_ids": [], "new_count": 0, "by_status": {}}
        outcome = str(forecast.get("outcome") or "error")
        state = str(forecast.get("proposal_state") or "確認できず")
        ids_raw = forecast.get("proposal_ids")
        ids = list(ids_raw) if isinstance(ids_raw, list) else []
        if outcome == "created":
            dry_reason = _safe_text(
                "不足機能の提案の下書きを用意します。"
                "内容を確認してください"
                f"({state})", 1000)
        elif outcome == "existing":
            dry_summary = None
            summary_error = ""
            try:
                dry_summary = _summarize_b3_proposals(manager, pid, plan_sig)
            except Exception as exc:
                summary_error = type(exc).__name__
            if summary_error != "" or not isinstance(dry_summary, dict):
                dry_summary = {"state": state, "ids": ids,
                               "by_status": {}, "total": len(ids)}
            dry_reason = _b3_reason_for_existing(dry_summary)
        elif outcome == "skipped":
            dry_reason = _safe_text(
                "作る条件を満たさないため生成しません"
                f"({state})", 1000)
        else:
            dry_reason = _safe_text(
                "提案を確認できないため進めません"
                f"({state})", 1000)
        return {
            "planned_action": "capability_preflight",
            "gate_status": "ok",
            "preflight_status": preflight_status,
            "preflight_reason": preflight_reason,
            "reason_ja": dry_reason,
            "next_action": next_action,
            "proposal_state": state,
            "proposal_ids": ids[:20],
            "capability_outcome": outcome,
            "plan_signature": plan_sig,
        }
    if stop_class in HUMAN_STOPS:
        return {
            "planned_action": "record_stop_class",
            "gate_status": "ok",
            "preflight_status": preflight_status,
            "preflight_reason": preflight_reason,
            "reason_ja": reason_ja,
            "next_action": next_action,
            "proposal_state": "",
            "proposal_ids": [],
            "capability_outcome": "skipped",
            "plan_signature": plan_sig,
        }
    planned = choose_action(stop_class, stop_code)
    return {
        "planned_action": planned,
        "gate_status": "ok",
        "preflight_status": preflight_status,
        "preflight_reason": preflight_reason,
        "reason_ja": reason_ja,
        "next_action": next_action,
        "proposal_state": "",
        "proposal_ids": [],
        "capability_outcome": "skipped",
        "plan_signature": plan_sig,
    }


def _tick_write_failed(project_id: str, actor_name: str, dry_run: bool,
                       stop_class: str, stop_code: str,
                       error_type: str) -> dict:
    """ティック記録の書込失敗は進行成功にしない (fail-closed)。"""
    etype = str(error_type or "Error")
    return {
        "project_id": str(project_id or ""),
        "status": "failed",
        "stop_class": str(stop_class or ""),
        "stop_code": str(stop_code or ""),
        "action_name": "",
        "next_action": "none",
        "reason_ja": f"ティック記録に失敗したため進めません:{etype}",
        "preflight_status": "unknown",
        "preflight_reason": etype,
        "external_delta": 0,
        "external_rounds_delta": 0,
        "budget_kept": False,
        "actor": str(actor_name or ""),
        "dry_run": bool(dry_run),
        "enabled": False,
    }


def _count_review_rounds(manager, project_id: str) -> int | None:
    """保存済みの外部評価レビュー総数 (適用・再送で増えていないことの実測用)。

    F1: 読取不能は None (不明) を保ち、呼び出し側が必ず停止する。
    0 と同一視しない。広い except をやめ、想定できる失敗だけ受ける。
    """
    try:
        from app.goal_review import ReviewStore

        store = ReviewStore(manager.memory.path)
        total = 0
        for _sig, payload in store.list(str(project_id), "plan"):
            if isinstance(payload, dict):
                total += len(payload.get("reviews") or [])
        return int(total)
    except (OSError, ValueError, TypeError, AttributeError, KeyError,
            RuntimeError) as exc:
        _rounds_note = type(exc).__name__
        return None


def _budget_snapshot(manager, project_id: str) -> dict | None:
    """予算スナップショット。F1: 読取不能は空辞書で通さず不明として停止する。

    戻り値 None は不明。型不正も不明。呼び出し側は必ず停止する。
    広い except をやめ、想定できる失敗だけ受ける。
    """
    try:
        from app.goal_review import review_budget

        snap = review_budget(manager, str(project_id))
        if not isinstance(snap, dict):
            return None
        return dict(snap)
    except (OSError, ValueError, TypeError, AttributeError, KeyError,
            RuntimeError) as exc:
        _budget_note = type(exc).__name__
        return None


def _budget_unchanged(before: dict | None, after: dict | None) -> bool:
    """F1: 不明 (None/unknown 印) は維持扱いにせず False (停止側) にする。

    比較不能も False。広い except をやめ、想定できる失敗だけ受ける。
    """
    try:
        if before is None or after is None:
            return False
        if not isinstance(before, dict) or not isinstance(after, dict):
            return before == after
        if bool(before.get("_budget_unknown")) or bool(
                after.get("_budget_unknown")):
            return False
        if bool(before.get("managed")) != bool(after.get("managed")):
            return False
        if not before.get("managed"):
            return True
        if str(before.get("period_utc") or "") != str(after.get("period_utc") or ""):
            return True
        return (int(before.get("used_calls") or 0) == int(after.get("used_calls") or 0)
                and int(before.get("remaining_calls") or 0)
                == int(after.get("remaining_calls") or 0))
    except (TypeError, ValueError, AttributeError):
        return False


@contextmanager
def _count_external_calls(manager):
    """実行前後で外部呼出が増えていないことを実測するための計数器。

    manager.plan_review_runner / 外部AI / ローカルLLM の呼出を数える。
    実行前後で復元するため、呼出自体は委譲する (止めない)。増分は呼び出し側で判定する。

    F1: 復元の失敗は握りつぶさず counts["_restore_failed"] に記録する。
    呼び出し側は印があれば計数不能=ティック失敗にする (増分0と見なさない)。
    広い except をやめ、想定できる失敗だけ受ける。
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
    except (ValueError, TypeError, sqlite3.Error, OSError, RuntimeError,
            ImportError) as exc:
        provider_module = None
        _provider_note = type(exc).__name__
    # ローカルAI(Ollama)は外部ではない。`_local_complete` の呼出は外部増分に
    # 数えず、監査用に別枠で記録する(4-B: ローカル生成の実測のため)。
    try:
        yield counts
    finally:
        try:
            counts["local_external_excluded"] = int(counts.get("local") or 0)
        except (TypeError, ValueError, AttributeError) as exc:
            counts["local_external_excluded"] = 0
            counts["_restore_failed"] = (
                f"local_bookkeeping:{type(exc).__name__}")
        try:
            counts["local"] = 0
        except (TypeError, ValueError, AttributeError) as exc:
            counts["_restore_failed"] = (
                f"local_reset:{type(exc).__name__}")
        try:
            if callable(originals.get(("runner", "plan_review_runner"))):
                manager.plan_review_runner = originals[("runner",
                                                         "plan_review_runner")]
        except (AttributeError, TypeError, ValueError, RuntimeError) as exc:
            counts["_restore_failed"] = f"runner:{type(exc).__name__}"
        try:
            if callable(originals.get(("local", "_local_complete"))):
                manager._local_complete = originals[("local", "_local_complete")]
        except (AttributeError, TypeError, ValueError, RuntimeError) as exc:
            counts["_restore_failed"] = f"local:{type(exc).__name__}"
        try:
            if provider_module is not None and callable(provider_original):
                provider_module.call_provider_with_metadata = provider_original
        except (AttributeError, TypeError, ValueError, RuntimeError) as exc:
            counts["_restore_failed"] = f"provider:{type(exc).__name__}"


def _external_unknown(before: dict, after: dict, counts: dict) -> str:
    """F1: 外部増分の計数不能を判定する。空文字は計数可 (増分判定へ進める)。

    復元失敗・輪数不明・予算不明があれば理由を返す (呼び出し側がティック失敗)。
    増分0とは見なさない。広い except を使わない。
    """
    if not isinstance(before, dict) or not isinstance(after, dict):
        return "external_snapshot_unknown"
    if not isinstance(counts, dict):
        return "external_counts_unknown"
    if counts.get("_restore_failed"):
        return f"external_restore_failed:{counts.get('_restore_failed')}"
    if before.get("rounds") is None or after.get("rounds") is None:
        return "external_rounds_unknown"
    if before.get("budget") is None or after.get("budget") is None:
        return "external_budget_unknown"
    if isinstance(before.get("budget"), dict) and bool(
            before["budget"].get("_budget_unknown")):
        return "external_budget_unknown"
    if isinstance(after.get("budget"), dict) and bool(
            after["budget"].get("_budget_unknown")):
        return "external_budget_unknown"
    return ""


def _snapshot_external(manager, project_id: str) -> dict:
    """F1: sends の読取不能は 0 と見なさず不明として停止する。

    rounds/budget の不明 (None) はそのまま保ち、呼び出し側が
    _external_unknown で必ずティック失敗にする。
    """
    try:
        from app.stop_classifier import _EXTERNAL_SENDS_COUNTER as _counter

        sends = int(_counter.get("calls") or 0)
    except (ValueError, TypeError, AttributeError, KeyError,
            RuntimeError) as exc:
        sends = None
        _sends_note = type(exc).__name__
    return {
        "sends": sends,
        "rounds": _count_review_rounds(manager, project_id),
        "budget": _budget_snapshot(manager, project_id),
    }


def _sends_delta_unknown(before: dict, after: dict) -> str:
    """F1: sends 差分の計数不能を判定する。空文字は計数可。"""
    if not isinstance(before, dict) or not isinstance(after, dict):
        return "external_sends_unknown"
    if before.get("sends") is None or after.get("sends") is None:
        return "external_sends_unknown"
    return ""


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


def _b3_tick_missing_capability(manager, pid: str, record: dict,
                                  flat: dict, plan_sig: str, next_action: str,
                                  refs: dict, actor_name: str, before: dict,
                                  detail_holder: dict) -> dict | None:
    """B-3 の下書きまでの一手。実行しない条件では None を返す。

    戻り値 dict は確定応答 (waiting_human/failed)。None のときだけ呼出側が
    従来どおり record_stop_class で記録する。review/export/取込み/apply・
    承認・外部送信・RAG登録・OCR投入は呼ばない。予算・空転の範囲内で1回だけ。
    同じ署名・同じ状態の再ティックで提案・監査の重複を作らない。
    """
    should, should_reason = _b3_should_auto_draft(manager, pid, record, plan_sig)
    if not should:
        try:
            tick = _insert_tick(manager, pid, {
                "plan_signature": plan_sig, "stop_class": "missing_capability",
                "stop_code": str(record.get("stop_code") or "unknown:empty"),
                "action_name": "record_stop_class",
                "status": "waiting_human", "next_action": next_action,
                "reason_ja": _safe_text(should_reason or
                                        "下書きの条件を満たさないため進めません", 1000),
                "evidence_refs": {k: _safe_text(v, 128) for k, v in refs.items()},
                "external_delta": 0, "external_rounds_delta": 0,
                "budget_kept": True, "dry_run": False, "actor": actor_name})
        except Exception as exc:
            return _tick_write_failed(pid, actor_name, False, "missing_capability",
                                      str(record.get("stop_code") or "unknown:empty"),
                                      type(exc).__name__)
        try:
            from app.stop_classifier import append_history as _append

            _append(manager, pid, flat, actor="auto-tick",
                    idempotency_key=f"autopilot:{tick.get('id')}")
        except Exception as exc:
            _history_note = type(exc).__name__
        try:
            run_named_action(manager, pid, "record_stop_class",
                             actor=actor_name, classification=record)
        except (ValueError, sqlite3.Error, OSError, RuntimeError) as exc:
            _record_note = type(exc).__name__
        tick = dict(tick)
        tick["enabled"] = True
        tick["local_calls"] = 0
        tick["detail"] = {"action": "record_stop_class", "external_calls": 0,
                          "b3_not_selected": _safe_text(should_reason, 200)}
        tick["proposal_state"] = ""
        tick["proposal_ids"] = []
        tick["capability_outcome"] = "skipped"
        return tick
    # B-3専用の空転ガード: 同じ停止が3回目になる場合だけ止める
    # (1回目created・2回目existingを許す)。既存の repeating 判定は使わない。
    try:
        from app.stop_classifier import read_history as _read_history

        b3_history = _read_history(manager, pid, limit=20)
    except Exception:
        b3_history = []
    if _b3_is_repeat_guarded(b3_history, flat):
        try:
            tick = _insert_tick(manager, pid, {
                "plan_signature": flat.get("plan_signature") or "",
                "stop_class": "missing_capability",
                "stop_code": str(record.get("stop_code") or "unknown:empty"),
                "action_name": "record_stop_class", "status": "stopped_guard",
                "next_action": next_action,
                "reason_ja": "同じ停止の繰り返しのため自動ランを停止しました",
                "evidence_refs": {k: _safe_text(v, 128) for k, v in refs.items()},
                "external_delta": 0, "external_rounds_delta": 0,
                "budget_kept": True, "dry_run": False, "actor": actor_name})
        except Exception as exc:
            return _tick_write_failed(pid, actor_name, False, "missing_capability",
                                      str(record.get("stop_code") or "unknown:empty"),
                                      type(exc).__name__)
        try:
            from app.stop_classifier import append_history as _append2

            _append2(manager, pid, flat, actor="auto-tick",
                     idempotency_key=f"autopilot:{tick.get('id')}")
        except (sqlite3.Error, ValueError, OSError, RuntimeError) as exc:
            _history_note_2 = type(exc).__name__
        try:
            _disable_internal(manager, pid,
                              reason="同じ停止の繰り返しのため自走を止めました")
        except Exception as exc:
            return _tick_write_failed(pid, actor_name, False, "missing_capability",
                                      str(record.get("stop_code") or "unknown:empty"),
                                      type(exc).__name__)
        try:
            _audit(manager, pid, "autopilot_guard_stopped",
                   {"stop_class": "missing_capability",
                    "stop_code": str(record.get("stop_code") or "unknown:empty")})
        except Exception as exc:
            return _tick_write_failed(pid, actor_name, False, "missing_capability",
                                      str(record.get("stop_code") or "unknown:empty"),
                                      type(exc).__name__)
        tick = dict(tick)
        tick["enabled"] = False
        return tick
    # 実行: _action_capability_preflight の四値を使う (1回だけ)。
    # 生成の前後で外部増分・予算を実測し、増分があれば失敗にする。
    detail: dict = {}
    action_error = ""
    counts_holder: dict = {}
    with _count_external_calls(manager) as counts:
        try:
            detail = run_named_action(manager, pid, "capability_preflight",
                                      actor=actor_name, classification=record)
            if hasattr(detail, "__await__"):
                raise ValueError("async action is not supported in tick")
            if isinstance(detail, dict) and detail.get("outcome") == "error":
                err_type = str(detail.get("error_type") or "Error")
                err_reason = str(detail.get("reason") or "")[:300]
                raise _CandidateNotAvailable(
                    f"提案の生成に失敗しました:{err_type}:{err_reason}")
        except _CandidateNotAvailable as exc:
            action_error = str(exc)[:500] or "提案の生成に失敗しました"
            detail = {}
        except Exception as exc:
            action_error = f"{type(exc).__name__}:{str(exc)[:300]}"
            detail = {}
        try:
            counts_holder["local"] = int(counts.get("local_external_excluded") or 0)
            counts_holder["runner"] = int(counts.get("runner") or 0)
            counts_holder["provider"] = int(counts.get("provider") or 0)
        except (TypeError, ValueError, AttributeError):
            counts_holder = {"local": 0, "runner": 0, "provider": 0}
        # F1: 復元失敗の印を引き継ぐ (増分0と見なさないため)。
        try:
            _restore_flag = counts.get("_restore_failed")
        except (AttributeError, TypeError):
            _restore_flag = None
        if _restore_flag:
            counts_holder["_restore_failed"] = _restore_flag
    after = _snapshot_external(manager, pid)
    # F1: 計数不能 (復元失敗・輪数不明・予算不明・sends不明) は増分0と
    # 見なさずティック失敗にする。
    _unknown_reason = _external_unknown(before, after, counts_holder)
    if not _unknown_reason:
        _unknown_reason = _sends_delta_unknown(before, after)
    if _unknown_reason:
        action_error = (
            f"外部増分を確認できないため進めません:{_unknown_reason}")
        detail = {}
    try:
        local_calls_made = int(counts_holder.get("local") or 0)
    except (TypeError, ValueError, AttributeError):
        local_calls_made = 0
    try:
        calls_delta = int(counts_holder.get("runner") or 0) + int(
            counts_holder.get("provider") or 0)
    except (TypeError, ValueError, AttributeError):
        calls_delta = 0
    try:
        if before.get("sends") is None or after.get("sends") is None:
            sends_delta = 0
        else:
            sends_delta = int(after.get("sends") or 0) - int(
                before.get("sends") or 0)
    except (TypeError, ValueError):
        sends_delta = 0
    external_delta = int(calls_delta) + max(0, int(sends_delta))
    rounds_delta = 0
    try:
        if after.get("rounds") is not None and before.get("rounds") is not None:
            rounds_delta = int(after["rounds"]) - int(before["rounds"])
    except (TypeError, ValueError):
        rounds_delta = 0
    budget_kept = _budget_unchanged(before.get("budget"), after.get("budget"))
    if action_error or external_delta != 0 or rounds_delta != 0 or not budget_kept:
        status = "failed"
        fail_reason = action_error or (
            f"外部増分を検出: calls={external_delta} "
            f"rounds={rounds_delta} budget_kept={budget_kept}")
        try:
            tick = _insert_tick(manager, pid, {
                "plan_signature": plan_sig, "stop_class": "missing_capability",
                "stop_code": str(record.get("stop_code") or "unknown:empty"),
                "action_name": "capability_preflight",
                "status": status, "next_action": next_action,
                "reason_ja": _safe_text(fail_reason, 1000),
                "evidence_refs": {k: _safe_text(v, 128) for k, v in refs.items()},
                "external_delta": external_delta,
                "external_rounds_delta": rounds_delta,
                "budget_kept": bool(budget_kept), "dry_run": False,
                "actor": actor_name})
        except Exception as exc:
            return _tick_write_failed(pid, actor_name, False, "missing_capability",
                                      str(record.get("stop_code") or "unknown:empty"),
                                      type(exc).__name__)
        try:
            from app.stop_classifier import append_history as _append3

            _append3(manager, pid, flat, actor="auto-tick",
                     idempotency_key=f"autopilot:{tick.get('id')}")
        except (sqlite3.Error, ValueError, OSError, RuntimeError) as exc:
            _history_note_3 = type(exc).__name__
        try:
            _disable_internal(manager, pid,
                              reason=f"ティック失敗のため自走を止めました: {fail_reason}")
        except Exception as exc:
            return _tick_write_failed(pid, actor_name, False, "missing_capability",
                                      str(record.get("stop_code") or "unknown:empty"),
                                      type(exc).__name__)
        try:
            _audit(manager, pid, "autopilot_tick_failed",
                   {"action": "capability_preflight", "reason": fail_reason})
        except Exception as exc:
            return _tick_write_failed(pid, actor_name, False, "missing_capability",
                                      str(record.get("stop_code") or "unknown:empty"),
                                      type(exc).__name__)
        tick = dict(tick)
        tick["enabled"] = False
        tick["local_calls"] = int(local_calls_made)
        tick["detail"] = detail if isinstance(detail, dict) else {}
        try:
            summary = _summarize_b3_proposals(manager, pid, plan_sig)
            tick["proposal_state"] = str(summary.get("state") or "")
            tick["proposal_ids"] = list(summary.get("ids") or [])
        except (ValueError, TypeError, sqlite3.Error, OSError, RuntimeError, KeyError):
            tick["proposal_state"] = "確認できず"
            tick["proposal_ids"] = []
        tick["capability_outcome"] = "error"
        detail_holder["counts"] = dict(counts_holder)
        return tick
    # 成功系: created/existing/skipped に応じて確認待ちで止まる。
    # 同じ署名・同じ状態の再ティックで提案・監査の重複を作らない
    # (generate_proposals 側の冪等 + 作成分だけ監査のため)。
    detail_map = detail if isinstance(detail, dict) else {}
    outcome = str(detail_map.get("outcome") or "")
    try:
        summary = _summarize_b3_proposals(manager, pid, plan_sig)
    except Exception as exc:
        fail_reason = f"提案を確認できないため進めません:{type(exc).__name__}"
        try:
            tick = _insert_tick(manager, pid, {
                "plan_signature": plan_sig, "stop_class": "missing_capability",
                "stop_code": str(record.get("stop_code") or "unknown:empty"),
                "action_name": "capability_preflight",
                "status": "failed", "next_action": next_action,
                "reason_ja": _safe_text(fail_reason, 1000),
                "evidence_refs": {k: _safe_text(v, 128) for k, v in refs.items()},
                "external_delta": external_delta,
                "external_rounds_delta": rounds_delta,
                "budget_kept": bool(budget_kept), "dry_run": False,
                "actor": actor_name})
        except Exception as exc2:
            return _tick_write_failed(pid, actor_name, False, "missing_capability",
                                      str(record.get("stop_code") or "unknown:empty"),
                                      type(exc2).__name__)
        try:
            from app.stop_classifier import append_history as _append4

            _append4(manager, pid, flat, actor="auto-tick",
                     idempotency_key=f"autopilot:{tick.get('id')}")
        except (sqlite3.Error, ValueError, OSError, RuntimeError) as exc2:
            _history_note_4 = type(exc2).__name__
        try:
            _disable_internal(manager, pid,
                              reason=f"ティック失敗のため自走を止めました: {fail_reason}")
        except Exception as exc2:
            return _tick_write_failed(pid, actor_name, False, "missing_capability",
                                      str(record.get("stop_code") or "unknown:empty"),
                                      type(exc2).__name__)
        try:
            _audit(manager, pid, "autopilot_tick_failed",
                   {"action": "capability_preflight", "reason": fail_reason})
        except Exception as exc2:
            return _tick_write_failed(pid, actor_name, False, "missing_capability",
                                      str(record.get("stop_code") or "unknown:empty"),
                                      type(exc2).__name__)
        tick = dict(tick)
        tick["enabled"] = False
        tick["local_calls"] = int(local_calls_made)
        tick["detail"] = detail_map
        tick["proposal_state"] = "確認できず"
        tick["proposal_ids"] = []
        tick["capability_outcome"] = "error"
        detail_holder["counts"] = dict(counts_holder)
        return tick
    if outcome == "created":
        total = int(summary.get("total") or 0)
        try:
            total = int(detail_map.get("proposals") or total)
        except Exception:
            total = int(summary.get("total") or 0)
        reason_text = _safe_text(
            f"不足機能の提案の下書きを用意しました。内容を確認してください({int(total)}件)",
            1000)
        status = "waiting_human"
        tick_next = next_action
    elif outcome == "existing":
        reason_text = _b3_reason_for_existing(summary)
        status = "waiting_human"
        tick_next = next_action
    elif outcome == "skipped":
        raw_skipped = detail_map.get("skipped_gap_codes")
        if isinstance(raw_skipped, list):
            skipped_text = ",".join(sorted({str(x) for x in raw_skipped
                                            if str(x)}))
        else:
            skipped_text = ""
        reason_text = _safe_text("作る条件を満たさないため生成しません"
                                 + (f":{skipped_text}" if skipped_text else ""),
                                 1000)
        status = "waiting_human"
        tick_next = next_action
    else:
        fail_reason = "提案の生成結果を確認できません:ValueError"
        try:
            tick = _insert_tick(manager, pid, {
                "plan_signature": plan_sig, "stop_class": "missing_capability",
                "stop_code": str(record.get("stop_code") or "unknown:empty"),
                "action_name": "capability_preflight",
                "status": "failed", "next_action": next_action,
                "reason_ja": _safe_text(fail_reason, 1000),
                "evidence_refs": {k: _safe_text(v, 128) for k, v in refs.items()},
                "external_delta": external_delta,
                "external_rounds_delta": rounds_delta,
                "budget_kept": bool(budget_kept), "dry_run": False,
                "actor": actor_name})
        except Exception as exc:
            return _tick_write_failed(pid, actor_name, False, "missing_capability",
                                      str(record.get("stop_code") or "unknown:empty"),
                                      type(exc).__name__)
        try:
            from app.stop_classifier import append_history as _append5

            _append5(manager, pid, flat, actor="auto-tick",
                     idempotency_key=f"autopilot:{tick.get('id')}")
        except (sqlite3.Error, ValueError, OSError, RuntimeError) as exc:
            _history_note_5 = type(exc).__name__
        try:
            _disable_internal(manager, pid,
                              reason=f"ティック失敗のため自走を止めました: {fail_reason}")
        except Exception as exc:
            return _tick_write_failed(pid, actor_name, False, "missing_capability",
                                      str(record.get("stop_code") or "unknown:empty"),
                                      type(exc).__name__)
        try:
            _audit(manager, pid, "autopilot_tick_failed",
                   {"action": "capability_preflight", "reason": fail_reason})
        except Exception as exc:
            return _tick_write_failed(pid, actor_name, False, "missing_capability",
                                      str(record.get("stop_code") or "unknown:empty"),
                                      type(exc).__name__)
        tick = dict(tick)
        tick["enabled"] = False
        tick["local_calls"] = int(local_calls_made)
        tick["detail"] = detail_map
        tick["proposal_state"] = str(summary.get("state") or "")
        tick["proposal_ids"] = list(summary.get("ids") or [])
        tick["capability_outcome"] = "error"
        detail_holder["counts"] = dict(counts_holder)
        return tick
    try:
        tick = _insert_tick(manager, pid, {
            "plan_signature": plan_sig, "stop_class": "missing_capability",
            "stop_code": str(record.get("stop_code") or "unknown:empty"),
            "action_name": "capability_preflight",
            "status": status, "next_action": tick_next,
            "reason_ja": reason_text,
            "evidence_refs": {k: _safe_text(v, 128) for k, v in refs.items()},
            "external_delta": external_delta,
            "external_rounds_delta": rounds_delta,
            "budget_kept": bool(budget_kept), "dry_run": False,
            "actor": actor_name})
    except Exception as exc:
        return _tick_write_failed(pid, actor_name, False, "missing_capability",
                                  str(record.get("stop_code") or "unknown:empty"),
                                  type(exc).__name__)
    try:
        from app.stop_classifier import append_history as _append6

        _append6(manager, pid, flat, actor="auto-tick",
                 idempotency_key=f"autopilot:{tick.get('id')}")
    except (sqlite3.Error, ValueError, OSError, RuntimeError) as exc:
        _history_note_6 = type(exc).__name__
    tick = dict(tick)
    tick["enabled"] = True
    tick["local_calls"] = int(local_calls_made)
    tick["detail"] = detail_map
    tick["proposal_state"] = str(summary.get("state") or "")
    tick["proposal_ids"] = list(summary.get("ids") or [])
    tick["capability_outcome"] = outcome
    detail_holder["counts"] = dict(counts_holder)
    return tick


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

    # C-3: 全体停止スイッチの判定 (A-1のゲートより前)。active または unknown
    # (読めない/矛盾=止める側) なら作業を選ばず kill_switch として停止する。
    # pid/actor は後続の既存定義より前で必要になるため先に確定する
    # (既存の分岐の順序・戻り値の形は変えない)。
    _c3_pid = str(project_id or "")
    _c3_actor = _safe_text(actor, 100).strip()
    if not _c3_actor:
        raise ValueError("actor is required")
    try:
        from app.night_run import kill_switch_state as _c3_kill_state

        _c3_kill = _c3_kill_state(manager)
    except Exception:
        _c3_kill = "unknown"
    if _c3_kill in ("active", "unknown"):
        if bool(dry_run):
            return {"project_id": _c3_pid, "status": "dry_run",
                    "stop_class": "", "stop_code": "kill_switch",
                    "plan_signature": "",
                    "next_action": "none",
                    "reason_ja": "全体停止スイッチのため実行しません:" + str(_c3_kill),
                    "evidence_refs": {},
                    "planned_action": "none",
                    "gate_status": "kill_switch",
                    "preflight_status": "unknown",
                    "preflight_reason": "kill_switch:" + str(_c3_kill),
                    "proposal_state": "",
                    "proposal_ids": [],
                    "capability_outcome": "skipped",
                    "external_delta": 0, "external_rounds_delta": 0,
                    "budget_kept": True, "actor": _c3_actor,
                    "dry_run": True,
                    "kill_switch": str(_c3_kill)}
        return {"project_id": _c3_pid, "status": "kill_switch",
                "stop_class": "", "stop_code": "kill_switch",
                "action_name": "none",
                "next_action": "none",
                "reason_ja": "全体停止スイッチのため進めません:" + str(_c3_kill),
                "preflight_status": "unknown",
                "preflight_reason": "kill_switch:" + str(_c3_kill),
                "external_delta": 0,
                "external_rounds_delta": 0,
                "budget_kept": True,
                "actor": _c3_actor,
                "dry_run": False,
                "enabled": is_enabled(manager, _c3_pid),
                "kill_switch": str(_c3_kill)}
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
        # A-1: 作業の選択より前に事前照合ゲートを通す。通れなければ
        # 候補生成・提案生成・ローカルAI・apply を一切行わない。
        gate = _preflight_gate(manager, pid, record)
        gate_reason = ""
        gate_plan_sig = ""
        gate_preflight_status = ""
        gate_preflight_reason = ""
        if gate is not None:
            gate_reason = str(gate.get("reason_ja") or "")
            gate_plan_sig = str(gate.get("plan_signature") or "")
            gate_preflight_status = str(gate.get("preflight_status") or "unknown")
            gate_preflight_reason = str(gate.get("preflight_reason") or "unknown")

        if bool(dry_run):
            # 予告は preview_planned_action と同じ判定。何も書かない。
            preview = preview_planned_action(manager, pid, record, plan_sig)
            if not isinstance(preview, dict):
                preview = {
                    "planned_action": "none",
                    "gate_status": "preflight_unknown",
                    "preflight_status": "unknown",
                    "preflight_reason": "TypeError",
                    "reason_ja": "自走の一手を確認できません:TypeError",
                    "next_action": next_action,
                    "proposal_state": "",
                    "proposal_ids": [],
                    "capability_outcome": "skipped",
                    "plan_signature": plan_sig,
                }
            ids_raw = preview.get("proposal_ids")
            ids = list(ids_raw) if isinstance(ids_raw, list) else []
            return {"project_id": pid, "status": "dry_run",
                    "stop_class": stop_class, "stop_code": stop_code,
                    "plan_signature": str(preview.get("plan_signature")
                                          or gate_plan_sig or plan_sig),
                    "next_action": str(preview.get("next_action")
                                       or next_action),
                    "reason_ja": _safe_text(
                        str(preview.get("reason_ja") or gate_reason
                            or reason_ja), 1000),
                    "evidence_refs": {k: _safe_text(v, 128)
                                      for k, v in refs.items()},
                    "planned_action": str(preview.get("planned_action")
                                          or "none"),
                    "gate_status": str(preview.get("gate_status") or ""),
                    "preflight_status": str(
                        preview.get("preflight_status")
                        or gate_preflight_status
                        or record.get("preflight_status") or ""),
                    "preflight_reason": str(
                        preview.get("preflight_reason")
                        or gate_preflight_reason
                        or record.get("preflight_reason") or ""),
                    "proposal_state": str(preview.get("proposal_state") or ""),
                    "proposal_ids": ids[:20],
                    "capability_outcome": str(
                        preview.get("capability_outcome") or "skipped"),
                    "external_delta": 0, "external_rounds_delta": 0,
                    "budget_kept": True, "actor": actor_name,
                    "dry_run": True}

        if gate is not None:
            # 判定不能・古い分類では作業を選ばず停止として記録する。
            # 自走は無効化しない (ok に戻れば従来どおり進める)。
            # 空転ガードの履歴には追記しない (判定不能の停止を通常の
            # 停止繰返しと混ぜない。ティック表への記録のみ)。
            gate_status = str(gate.get("gate_status") or "preflight_unknown")
            try:
                tick = _insert_tick(manager, pid, {
                    "plan_signature": gate_plan_sig or plan_sig,
                    "stop_class": stop_class, "stop_code": stop_code,
                    "action_name": "none", "status": gate_status,
                    "next_action": next_action,
                    "reason_ja": _safe_text(gate_reason, 1000),
                    "evidence_refs": {k: _safe_text(v, 128)
                                      for k, v in refs.items()},
                    "external_delta": 0, "external_rounds_delta": 0,
                    "budget_kept": True, "dry_run": False, "actor": actor_name})
            except Exception as exc:
                return _tick_write_failed(pid, actor_name, False, stop_class,
                                          stop_code, type(exc).__name__)
            try:
                _audit(manager, pid, "autopilot_preflight_stopped",
                       {"stop_class": stop_class, "stop_code": stop_code,
                        "gate_status": gate_status,
                        "preflight_status": gate_preflight_status,
                        "preflight_reason": gate_preflight_reason})
            except Exception as exc:
                return _tick_write_failed(pid, actor_name, False, stop_class,
                                          stop_code, type(exc).__name__)
            tick = dict(tick)
            tick["enabled"] = True
            tick["gate_status"] = gate_status
            tick["preflight_status"] = gate_preflight_status
            tick["preflight_reason"] = gate_preflight_reason
            tick["local_calls"] = 0
            tick["detail"] = {}
            return tick

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
        # F1: 評価失敗は握りつぶさず停止側に倒す (繰り返し不明=繰り返し扱い)。
        _guard_unknown = ""
        try:
            _guard = guard_auto_tick(manager, pid,
                                     {"plan_signature": flat.get("plan_signature"),
                                      "manual_bindings": ([{"manual": True}]
                                                          if human_touched else [])},
                                     record)
            if not human_touched:
                repeating = repeating or bool(_guard.get("repeating"))
        except (ValueError, TypeError, AttributeError, KeyError,
                sqlite3.Error, OSError, RuntimeError) as exc:
            _guard_unknown = type(exc).__name__
            repeating = True

        # B-3: missing_capability は 1回目(created)と 2回目(existing)を許す。
        # 空転ガード自体は外さない (判定は必ず行い、3回目は B-3 側で止める)。
        if repeating and stop_class == "missing_capability":
            repeating = False

        if repeating:
            try:
                tick = _insert_tick(manager, pid, {
                    "plan_signature": flat.get("plan_signature") or "",
                    "stop_class": stop_class, "stop_code": stop_code,
                    "action_name": "record_stop_class", "status": "stopped_guard",
                    "next_action": next_action, "reason_ja": "同じ停止の繰り返しのため自動ランを停止しました",
                    "evidence_refs": {k: _safe_text(v, 128) for k, v in refs.items()},
                    "external_delta": 0, "external_rounds_delta": 0,
                    "budget_kept": True, "dry_run": False, "actor": actor_name})
            except Exception as exc:
                return _tick_write_failed(pid, actor_name, False, stop_class,
                                          stop_code, type(exc).__name__)
            try:
                append_history(manager, pid, flat, actor="auto-tick",
                               idempotency_key=f"autopilot:{tick.get('id')}")
            except Exception as exc:
                # F1: 履歴の書込失敗は握りつぶさず、止めたうえで失敗を返す。
                # 通知 (無効化・監査) が再度失敗しても成功は返さない。
                _append_error = type(exc).__name__
                try:
                    _disable_internal(
                        manager, pid,
                        reason="履歴の記録に失敗したため自走を止めました")
                except Exception as exc2:
                    return _tick_write_failed(pid, actor_name, False, stop_class,
                                              stop_code, type(exc2).__name__)
                try:
                    _audit(manager, pid, "autopilot_tick_failed",
                           {"action": "history_append",
                            "reason": "履歴の記録に失敗しました",
                            "error_type": _append_error})
                except Exception as exc3:
                    return _tick_write_failed(pid, actor_name, False, stop_class,
                                              stop_code, type(exc3).__name__)
                return _tick_write_failed(pid, actor_name, False, stop_class,
                                          stop_code, _append_error)
            try:
                _disable_internal(manager, pid,
                                  reason="同じ停止の繰り返しのため自走を止めました")
            except Exception as exc:
                return _tick_write_failed(pid, actor_name, False, stop_class,
                                          stop_code, type(exc).__name__)
            try:
                _audit(manager, pid, "autopilot_guard_stopped",
                       {"stop_class": stop_class, "stop_code": stop_code})
            except Exception as exc:
                return _tick_write_failed(pid, actor_name, False, stop_class,
                                          stop_code, type(exc).__name__)
            tick = dict(tick)
            tick["enabled"] = False
            return tick

        # B-3: 主たる停止が missing_capability のときは下書きまで自走させる。
        # 条件を満たさないときは従来どおり進めず記録して止まる。
        # review/export/取込み/apply は自動化しない (人の関門)。
        if stop_class == "missing_capability":
            before_b3 = _snapshot_external(manager, pid)
            holder: dict = {}
            b3_tick = _b3_tick_missing_capability(
                manager, pid, record, flat, plan_sig, next_action, refs,
                actor_name, before_b3, holder)
            if isinstance(b3_tick, dict):
                try:
                    counts = holder.get("counts")
                    if isinstance(counts, dict):
                        b3_tick["local_calls"] = int(
                            counts.get("local") or 0)
                    else:
                        b3_tick["local_calls"] = 0
                except (TypeError, ValueError):
                    b3_tick["local_calls"] = 0
                return b3_tick
            # 到達不能の保険: fail-closed で記録して止まる。
            try:
                tick = _insert_tick(manager, pid, {
                    "plan_signature": plan_sig, "stop_class": stop_class,
                    "stop_code": stop_code, "action_name": "record_stop_class",
                    "status": "waiting_human", "next_action": next_action,
                    "reason_ja": _safe_text(
                        "下書きの判定を確認できないため進めません:ValueError",
                        1000),
                    "evidence_refs": {k: _safe_text(v, 128)
                                      for k, v in refs.items()},
                    "external_delta": 0, "external_rounds_delta": 0,
                    "budget_kept": True, "dry_run": False,
                    "actor": actor_name})
            except Exception as exc:
                return _tick_write_failed(pid, actor_name, False, stop_class,
                                          stop_code, type(exc).__name__)
            try:
                append_history(manager, pid, flat, actor="auto-tick",
                               idempotency_key=f"autopilot:{tick.get('id')}")
            except (sqlite3.Error, ValueError, OSError, RuntimeError) as exc:
                _history_note_b3 = type(exc).__name__
            tick = dict(tick)
            tick["enabled"] = True
            tick["local_calls"] = 0
            tick["detail"] = {"action": "record_stop_class",
                              "external_calls": 0}
            tick["proposal_state"] = ""
            tick["proposal_ids"] = []
            tick["capability_outcome"] = "skipped"
            return tick

        if stop_class in HUMAN_STOPS:
            # 人の判断が要る地点。進めず記録だけする。
            try:
                tick = _insert_tick(manager, pid, {
                    "plan_signature": plan_sig, "stop_class": stop_class,
                    "stop_code": stop_code, "action_name": "record_stop_class",
                    "status": "waiting_human", "next_action": next_action,
                    "reason_ja": reason_ja,
                    "evidence_refs": {k: _safe_text(v, 128) for k, v in refs.items()},
                    "external_delta": 0, "external_rounds_delta": 0,
                    "budget_kept": True, "dry_run": False, "actor": actor_name})
            except Exception as exc:
                return _tick_write_failed(pid, actor_name, False, stop_class,
                                          stop_code, type(exc).__name__)
            try:
                append_history(manager, pid, flat, actor="auto-tick",
                               idempotency_key=f"autopilot:{tick.get('id')}")
            except Exception as exc:
                # F1: 履歴の書込失敗は握りつぶさず、止めたうえで失敗を返す。
                # 通知 (無効化・監査) が再度失敗しても成功は返さない。
                _append_error = type(exc).__name__
                try:
                    _disable_internal(
                        manager, pid,
                        reason="履歴の記録に失敗したため自走を止めました")
                except Exception as exc2:
                    return _tick_write_failed(pid, actor_name, False, stop_class,
                                              stop_code, type(exc2).__name__)
                try:
                    _audit(manager, pid, "autopilot_tick_failed",
                           {"action": "history_append",
                            "reason": "履歴の記録に失敗しました",
                            "error_type": _append_error})
                except Exception as exc3:
                    return _tick_write_failed(pid, actor_name, False, stop_class,
                                              stop_code, type(exc3).__name__)
                return _tick_write_failed(pid, actor_name, False, stop_class,
                                          stop_code, _append_error)
            # 記録だけの動作を許可アクションとしても残す (外部呼出0)。
            # F1: 記録失敗は握りつぶさずティック失敗にする (成功を返さない)。
            try:
                run_named_action(manager, pid, "record_stop_class",
                                 actor=actor_name, classification=record)
            except (ValueError, TypeError, sqlite3.Error, OSError,
                    RuntimeError) as exc:
                _record_error = type(exc).__name__
                try:
                    _disable_internal(
                        manager, pid,
                        reason="記録動作に失敗したため自走を止めました")
                except Exception as exc2:
                    return _tick_write_failed(pid, actor_name, False, stop_class,
                                              stop_code, type(exc2).__name__)
                try:
                    _audit(manager, pid, "autopilot_tick_failed",
                           {"action": "record_stop_class",
                            "reason": "記録動作に失敗しました",
                            "error_type": _record_error})
                except Exception as exc3:
                    return _tick_write_failed(pid, actor_name, False, stop_class,
                                              stop_code, type(exc3).__name__)
                return _tick_write_failed(pid, actor_name, False, stop_class,
                                          stop_code, _record_error)
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
                # A-2: 四値の error はティックの失敗にする (「0件の成功」にしない)。
                if (isinstance(result_detail, dict)
                        and result_detail.get("outcome") == "error"):
                    err_type = str(result_detail.get("error_type") or "Error")
                    err_reason = str(result_detail.get("reason") or "")[:300]
                    raise _CandidateNotAvailable(
                        f"提案の生成に失敗しました:{err_type}:{err_reason}")
            except _NeedsHumanDecision as exc:
                # 進めない条件(指摘なし・生成中・試行上限): 失敗ではなく人の判断待ち。
                needs_human_reason = str(exc)[:500] or "人の判断が必要です"
                result_detail = {}
            except Exception as exc:
                action_error = f"{type(exc).__name__}:{str(exc)[:300]}"
                result_detail = {}
        after = _snapshot_external(manager, pid)
        # F1: 計数不能 (復元失敗・輪数不明・予算不明・sends不明) は増分0と
        # 見なさずティック失敗にする。
        _unknown_reason = _external_unknown(before, after, counts)
        if not _unknown_reason:
            _unknown_reason = _sends_delta_unknown(before, after)
        if _unknown_reason:
            action_error = (
                f"外部増分を確認できないため進めません:{_unknown_reason}")
            result_detail = {}
        try:
            local_calls_made = int(counts.get("local_external_excluded") or 0)
        except (TypeError, ValueError, AttributeError):
            local_calls_made = 0
        try:
            # ローカルAI(Ollama)は外部ではない。外部増分には数えない。
            calls_delta = int(counts.get("runner") or 0) + int(
                counts.get("provider") or 0)
        except (TypeError, ValueError, AttributeError):
            calls_delta = 0
        try:
            if before.get("sends") is None or after.get("sends") is None:
                sends_delta = 0
            else:
                sends_delta = int(after.get("sends") or 0) - int(
                    before.get("sends") or 0)
        except (TypeError, ValueError):
            sends_delta = 0
        external_delta = int(calls_delta) + max(0, int(sends_delta))
        rounds_delta = 0
        try:
            if after.get("rounds") is not None and before.get("rounds") is not None:
                rounds_delta = int(after["rounds"]) - int(before["rounds"])
        except (TypeError, ValueError):
            rounds_delta = 0
        budget_kept = _budget_unchanged(before.get("budget"),
                                        after.get("budget"))
        if (needs_human_reason and not action_error and external_delta == 0
                and rounds_delta == 0 and budget_kept):
            # 進めない条件: 生成を試みたことにせず、人の判断待ちで記録する。
            # 自走は無効化しない (enabled を保ち、無限再生成は冪等＋空転ガードで止める)。
            try:
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
            except Exception as exc:
                return _tick_write_failed(pid, actor_name, False, stop_class,
                                          stop_code, type(exc).__name__)
            try:
                append_history(manager, pid, flat, actor="auto-tick",
                               idempotency_key=f"autopilot:{tick.get('id')}")
            except Exception as exc:
                # F1: 履歴の書込失敗は握りつぶさず、止めたうえで失敗を返す。
                # 通知 (無効化・監査) が再度失敗しても成功は返さない。
                _append_error = type(exc).__name__
                try:
                    _disable_internal(
                        manager, pid,
                        reason="履歴の記録に失敗したため自走を止めました")
                except Exception as exc2:
                    return _tick_write_failed(pid, actor_name, False, stop_class,
                                              stop_code, type(exc2).__name__)
                try:
                    _audit(manager, pid, "autopilot_tick_failed",
                           {"action": "history_append",
                            "reason": "履歴の記録に失敗しました",
                            "error_type": _append_error})
                except Exception as exc3:
                    return _tick_write_failed(pid, actor_name, False, stop_class,
                                              stop_code, type(exc3).__name__)
                return _tick_write_failed(pid, actor_name, False, stop_class,
                                          stop_code, _append_error)
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
            try:
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
            except Exception as exc:
                return _tick_write_failed(pid, actor_name, False, stop_class,
                                          stop_code, type(exc).__name__)
            try:
                append_history(manager, pid, flat, actor="auto-tick",
                               idempotency_key=f"autopilot:{tick.get('id')}")
            except Exception as exc:
                # F1: 履歴の書込失敗は握りつぶさず、止めたうえで失敗を返す。
                # 通知 (無効化・監査) が再度失敗しても成功は返さない。
                _append_error = type(exc).__name__
                try:
                    _disable_internal(
                        manager, pid,
                        reason="履歴の記録に失敗したため自走を止めました")
                except Exception as exc2:
                    return _tick_write_failed(pid, actor_name, False, stop_class,
                                              stop_code, type(exc2).__name__)
                try:
                    _audit(manager, pid, "autopilot_tick_failed",
                           {"action": "history_append",
                            "reason": "履歴の記録に失敗しました",
                            "error_type": _append_error})
                except Exception as exc3:
                    return _tick_write_failed(pid, actor_name, False, stop_class,
                                              stop_code, type(exc3).__name__)
                return _tick_write_failed(pid, actor_name, False, stop_class,
                                          stop_code, _append_error)
            try:
                _disable_internal(manager, pid,
                                  reason=f"ティック失敗のため自走を止めました: {fail_reason}")
            except Exception as exc:
                return _tick_write_failed(pid, actor_name, False, stop_class,
                                          stop_code, type(exc).__name__)
            try:
                _audit(manager, pid, "autopilot_tick_failed",
                       {"action": action_name, "reason": fail_reason})
            except Exception as exc:
                return _tick_write_failed(pid, actor_name, False, stop_class,
                                          stop_code, type(exc).__name__)
            tick = dict(tick)
            tick["enabled"] = False
            tick["local_calls"] = int(local_calls_made)
            tick["detail"] = result_detail if isinstance(result_detail, dict) else {}
            return tick

        detail_map = result_detail if isinstance(result_detail, dict) else {}
        candidate_note = ""
        effective_next = next_action
        outcome_note = ""
        if isinstance(detail_map, dict) and detail_map.get("action") == "capability_preflight":
            detail_outcome = str(detail_map.get("outcome") or "")
            if detail_outcome == "error":
                # 四値の error に到達したら失敗に倒す (到達不能な保険)。
                err_type = str(detail_map.get("error_type") or "Error")
                fail_reason = f"提案の生成に失敗しました:{err_type}"
                try:
                    tick = _insert_tick(manager, pid, {
                        "plan_signature": plan_sig, "stop_class": stop_class,
                        "stop_code": stop_code, "action_name": action_name,
                        "status": "failed", "next_action": next_action,
                        "reason_ja": _safe_text(fail_reason, 1000),
                        "evidence_refs": {k: _safe_text(v, 128) for k, v in refs.items()},
                        "external_delta": external_delta,
                        "external_rounds_delta": rounds_delta,
                        "budget_kept": bool(budget_kept), "dry_run": False,
                        "actor": actor_name})
                except Exception as exc:
                    return _tick_write_failed(pid, actor_name, False, stop_class,
                                              stop_code, type(exc).__name__)
                try:
                    append_history(manager, pid, flat, actor="auto-tick",
                                   idempotency_key=f"autopilot:{tick.get('id')}")
                except Exception as exc:
                    # F1: 履歴の書込失敗は握りつぶさず、止めたうえで失敗を返す。
                    _append_error = type(exc).__name__
                    try:
                        _disable_internal(
                            manager, pid,
                            reason="履歴の記録に失敗したため自走を止めました")
                    except Exception as exc2:
                        return _tick_write_failed(pid, actor_name, False, stop_class,
                                                  stop_code, type(exc2).__name__)
                    try:
                        _audit(manager, pid, "autopilot_tick_failed",
                               {"action": "history_append",
                                "reason": "履歴の記録に失敗しました",
                                "error_type": _append_error})
                    except Exception as exc3:
                        return _tick_write_failed(pid, actor_name, False, stop_class,
                                                  stop_code, type(exc3).__name__)
                    return _tick_write_failed(pid, actor_name, False, stop_class,
                                              stop_code, _append_error)
                try:
                    _disable_internal(
                        manager, pid,
                        reason=f"ティック失敗のため自走を止めました: {fail_reason}")
                except Exception as exc:
                    return _tick_write_failed(pid, actor_name, False, stop_class,
                                              stop_code, type(exc).__name__)
                try:
                    _audit(manager, pid, "autopilot_tick_failed",
                           {"action": action_name, "reason": fail_reason})
                except Exception as exc:
                    return _tick_write_failed(pid, actor_name, False, stop_class,
                                              stop_code, type(exc).__name__)
                tick = dict(tick)
                tick["enabled"] = False
                tick["local_calls"] = int(local_calls_made)
                tick["detail"] = detail_map
                return tick
            if detail_outcome in ("created", "existing", "skipped"):
                effective_next = next_action
                if detail_outcome == "created":
                    outcome_note = (
                        f"不足機能の提案を生成しました({detail_map.get('proposals')}件)。"
                        "人の確認が必要です")
                elif detail_outcome == "existing":
                    outcome_note = (
                        f"不足機能の提案は既にあります({detail_map.get('proposals')}件)。"
                        "重複は作りません。人の確認が必要です")
                else:
                    skipped = detail_map.get("skipped_gap_codes")
                    skipped_text = ",".join(sorted({str(x) for x in skipped})) if isinstance(
                        skipped, list) else ""
                    outcome_note = "作る条件を満たさないため生成しません"
                    if skipped_text:
                        outcome_note = f"{outcome_note}:{skipped_text}"
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
        try:
            tick = _insert_tick(manager, pid, {
                "plan_signature": plan_sig, "stop_class": stop_class,
                "stop_code": stop_code, "action_name": action_name,
                "status": "advanced", "next_action": effective_next,
                "reason_ja": _safe_text(candidate_note or outcome_note or reason_ja, 1000),
                "evidence_refs": {k: _safe_text(v, 128) for k, v in refs.items()},
                "external_delta": 0, "external_rounds_delta": 0,
                "budget_kept": True, "dry_run": False, "actor": actor_name})
        except Exception as exc:
            return _tick_write_failed(pid, actor_name, False, stop_class,
                                      stop_code, type(exc).__name__)
        try:
            append_history(manager, pid, flat, actor="auto-tick",
                           idempotency_key=f"autopilot:{tick.get('id')}")
        except Exception as exc:
            # F1: 成功経路でも履歴の書込失敗は成功にしない。二重失敗でも同様。
            _append_error = type(exc).__name__
            try:
                _disable_internal(
                    manager, pid,
                    reason="履歴の記録に失敗したため自走を止めました")
            except Exception as exc2:
                return _tick_write_failed(pid, actor_name, False, stop_class,
                                          stop_code, type(exc2).__name__)
            try:
                _audit(manager, pid, "autopilot_tick_failed",
                       {"action": "history_append",
                        "reason": "履歴の記録に失敗しました",
                        "error_type": _append_error})
            except Exception as exc3:
                return _tick_write_failed(pid, actor_name, False, stop_class,
                                          stop_code, type(exc3).__name__)
            return _tick_write_failed(pid, actor_name, False, stop_class,
                                      stop_code, _append_error)
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
    # C-3: 全体停止スイッチの判定 (A-1のゲートより前)。autopilot_tick 側でも
    # 判定するが、入口でも作業0・外部0で止める (二重の安全)。
    try:
        from app.night_run import kill_switch_state as _c3_kill_state_run

        _c3_kill_run = _c3_kill_state_run(manager)
    except Exception:
        _c3_kill_run = "unknown"
    if _c3_kill_run in ("active", "unknown"):
        _actor_early = _safe_text(actor, 100).strip()
        if not _actor_early:
            raise ValueError("actor is required")
        return {"project_id": str(project_id or ""), "status": "kill_switch",
                "ticks": [], "count": 0,
                "max_ticks": MAX_TICKS, "actor": _actor_early,
                "kill_switch": str(_c3_kill_run),
                "reason": "全体停止スイッチのため進めません:" + str(_c3_kill_run)}
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
        if status in ("failed", "stopped_guard", "waiting_human",
                        "preflight_unknown", "stale"):
            final_status = status
            break
        if status != "advanced":
            final_status = status
            break
    else:
        final_status = "max_ticks"
    return {"project_id": pid, "status": final_status, "ticks": ticks,
            "count": len(ticks), "max_ticks": limit, "actor": actor_name}
