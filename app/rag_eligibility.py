"""P3 ギャップ1: RAG適格性判定と不採用理由の記録(TRIZ側)。

失敗1件ごとに、RAG候補事例について同一案件・現行入力版・原本ハッシュ・
verified・有効期限・索引識別子を毎回検証し、採用(eligible)と不採用(rejected)を
理由付きで返す。判定ロジックは plan_case_reference._evaluate_row を再利用し、
同じ基準を二重実装しない。追加で見るのは _evaluate_row が扱わない
同一案件(source project)と原本ハッシュ(source_hash)だけである。

事例は「手順仮説」であり、現在案件の事実・達成証拠として扱わない。
適格0件のときは「適格事例なし」と記録し、RAGによる改善を主張しない。

副作用なし: 検索・正本DB読み取りのみ。監査登録・RAG登録・承認・実行は行わない。
外部AIへ原本・個人情報・非公開事例・RAG事例本文を送らない。
"""
from __future__ import annotations

from app.plan_case_reference import _decode_json, _evaluate_row, case_hash_for, truncate_excerpt

HYPOTHESIS_NOTE = (
    "事例は手順仮説であり、現在案件の事実・達成証拠として扱わない。"
)
NO_ELIGIBLE_NOTE = (
    "適格事例なし。RAGによる改善は主張しない。"
)
SUMMARY_NOTE = (
    "RAG適格性は同一案件・現行入力版・原本ハッシュ・verified・有効期限・"
    "索引識別子を毎回検証した結果である。" + HYPOTHESIS_NOTE
)


def _expected_source_hash(row: dict) -> str:
    evidence = _decode_json(row.get("evidence"), {})
    applies = _decode_json(row.get("applicability"), {})
    return str(applies.get("source_hash") or evidence.get("source_hash") or "").strip()


def _refine_verdict(row: dict, verdict: str, reason: str) -> tuple[str, str]:
    """_evaluate_row の結果を、既存の理由区分で読み替えるだけ(再判定しない)。

    needs_review は _evaluate_row では not_verified になるため、理由の明確化
    として needs_review に読み替える。candidate/revoked は not_verified のまま
    とし、理由に status を含める(既存の期待値を変えない)。
    """
    status = str(row.get("status") or "")
    if verdict == "not_verified" and status == "needs_review":
        return ("needs_review", reason + "(status=needs_review)")
    return (verdict, reason)


def assess_rows(exp_memory, project: str, input_version: str, source_hash: str,
                rows: list[dict], states: dict[str, dict | None],
                extra_context: dict | None = None) -> dict:
    """行リストを _evaluate_row で判定し、同一案件・原本ハッシュだけ追加検証する。

    rows: 正本DBの行(dict)。他案件の行を含むことができる。
    states: {case_id: index_state or None}。
    戻り値: {"eligible": [...], "rejected": [...], "summary_ja": str, "notes": [...]}
    eligible要素: {case_id, excerpt(要旨のみ400字), verdict, reason}
    rejected要素: {case_id, verdict, reason}
    事例本文は eligible の excerpt(要旨)のみで返し、全文は返さない。
    """
    pid = str(project or "").strip()
    want_version = str(input_version or "").strip()
    current_hash = str(source_hash or "").strip()
    extra = dict(extra_context or {})
    try:
        from app.experience_memory import current_index_identity
        current_identity = current_index_identity(exp_memory.config)
    except Exception:
        current_identity = ""
    try:
        eligible_ids = {r["id"] for r in exp_memory.verified_rows_for_index(pid)}
    except Exception:
        eligible_ids = set()
    eligible: list[dict] = []
    rejected: list[dict] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        rid = str(row.get("id") or "")
        if not rid:
            continue
        # 同一案件の検証(_evaluate_rowの対象外)。他案件は理由付きで不採用。
        row_project = str(row.get("project") or pid)
        if row_project != pid:
            rejected.append({
                "case_id": rid, "verdict": "other_project",
                "reason": "他案件の事例のため不採用(project=%s)" % row_project[:24],
            })
            continue
        evidence = _decode_json(row.get("evidence"), {})
        applies = _decode_json(row.get("applicability"), {})
        state = states.get(rid) if isinstance(states, dict) else None
        # source_hash は _evaluate_row が汎用の適用条件として扱うため、
        # ここでは外して既存基準に任せ、原本ハッシュは直後に専用判定する。
        applies_for_eval = dict(applies)
        applies_for_eval.pop("source_hash", None)
        verdict, _decision, reason = _evaluate_row(
            row, evidence, applies_for_eval, want_version, extra, eligible_ids, state,
            current_identity)
        verdict, reason = _refine_verdict(row, verdict, reason)
        if verdict != "applicable":
            rejected.append({"case_id": rid, "verdict": verdict, "reason": reason})
            continue
        # 原本ハッシュの検証(_evaluate_rowの対象外)。欠ける・変わるなら降格ではなく不採用。
        expected = _expected_source_hash(row)
        if not current_hash or not expected:
            rejected.append({
                "case_id": rid, "verdict": "source_hash_changed",
                "reason": "原本ハッシュを検証できないため不採用(手順仮説として扱えない)",
            })
            continue
        if expected != current_hash:
            rejected.append({
                "case_id": rid, "verdict": "source_hash_changed",
                "reason": "原本ハッシュ変更のため不採用(事例の原本と現行原本が不一致)",
            })
            continue
        eligible.append({
            "case_id": rid,
            "excerpt": truncate_excerpt(row.get("content")),
            "verdict": "applicable",
            "reason": reason,
        })
    eligible.sort(key=lambda x: x["case_id"])
    rejected.sort(key=lambda x: x["case_id"])
    if eligible:
        summary = "適格事例%d件、不採用%d件。%s" % (len(eligible), len(rejected), HYPOTHESIS_NOTE)
    else:
        summary = "適格事例なし(候補%d件は全て不採用)。%s%s" % (
            len(rejected), NO_ELIGIBLE_NOTE, HYPOTHESIS_NOTE)
    return {
        "eligible": eligible,
        "rejected": rejected,
        "summary_ja": summary,
        "notes": [SUMMARY_NOTE, HYPOTHESIS_NOTE,
                  NO_ELIGIBLE_NOTE if not eligible else ""],
    }


def evaluate_for_failure(exp_memory, project: str, query: str, input_version: str,
                         source_hash: str, extra_context: dict | None = None,
                         limit: int = 12) -> dict:
    """失敗1件分のRAG適格性(読み取りのみ)。検索は既存の索引経路を使う。

    検索失敗時は空候補として「適格事例なし」を返し、改善を主張しない。
    監査登録・RAG登録は行わない(副作用なし)。
    """
    from app.plan_case_reference import SEARCH_CANDIDATE_LIMIT
    pid = str(project or "").strip()
    fetch_limit = max(1, min(int(limit or 12), SEARCH_CANDIDATE_LIMIT))
    try:
        search_ids = exp_memory.build_index().search(pid, str(query or "")[:6000], fetch_limit)
    except Exception as exc:
        return {
            "eligible": [], "rejected": [],
            "summary_ja": "検索に失敗したため適格事例なし(%s)。%s%s" % (
                type(exc).__name__, NO_ELIGIBLE_NOTE, HYPOTHESIS_NOTE),
            "notes": [SUMMARY_NOTE, HYPOTHESIS_NOTE, NO_ELIGIBLE_NOTE],
            "search_error": type(exc).__name__,
        }
    rows: list[dict] = []
    states: dict[str, dict | None] = {}
    for rid in list(dict.fromkeys([str(v) for v in (search_ids or [])]))[:fetch_limit]:
        row = exp_memory.store.get(pid, rid)
        if row is None:
            # 他案件または削除済み。内容を読まずIDのみで不採用にする。
            rows.append({"id": rid, "project": "__other__", "status": "unknown"})
            states[rid] = None
            continue
        rows.append(row)
        try:
            states[rid] = exp_memory.store.get_index_state(pid, rid)
        except Exception:
            states[rid] = None
    result = assess_rows(exp_memory, pid, input_version, source_hash, rows, states,
                         extra_context)
    result["query_hint"] = str(query or "")[:200]
    return result
