"""P3 ギャップ2: TRIZ候補(隔離試験の対象)の決定的な順位付け。

観点: (a)対象の達成条件との関連 (b)必要な原本の有無
(c)隔離試験の結果 (d)業務リスク。既知P0欠陥はTRIZ対象から除外する
(従来どおり。除外理由を記録する)。
順位とその理由(各観点の点数と根拠)を保存・表示できる形で返す。
TRIZ原理の選択をユーザーに丸投げしない(順位の理由を日本語で提示する)。
順位が高いことは採用・成功を意味しない(記録に明記し、順位1位でも
artifact_trial_passed ≠ business_recovered を維持する)。
同点は決定的な順序(候補ID)で解決する。

副作用なし。登録済み triz_adapters の capability / automatic_triz の
登録アダプター以外は参照しない。候補の実行・採用・承認は行わない。
"""
from __future__ import annotations

from app.automatic_triz import ALLOWED_ADAPTER_FUNCTIONS, KNOWN_P0_ERROR_CODES

RISK_NOTE = (
    "順位は隔離試験の対象順であり、採用・成功を意味しない。"
    "順位1位でも artifact_trial_passed は業務合格(business_recovered)ではない。"
)

# 各観点の重み(決定的)。外部操作を含む・業務事実を要する場合は大きく降格する。
W_RELEVANCE = 40
W_SOURCES = 30
W_TRIAL = 40
W_RISK = -60

# trial 状態の点数。unknown/未試験=0、不合格は降格、合格は加点。
TRIAL_SCORES = {
    "passed": 40,
    "pass": 40,
    "artifact_trial_passed": 40,
    "failed": -40,
    "trial_failed": -40,
    "running": 0,
    "trial_running": 0,
    "untested": 0,
    "unknown": 0,
    "": 0,
}


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _candidate_text(candidate: dict) -> str:
    parts = []
    for key in ("id", "title", "goal", "criterion_id", "task_key",
                "criterion", "target", "principle", "description"):
        value = candidate.get(key)
        if value:
            parts.append(str(value))
    for step in _as_list(candidate.get("steps")):
        if isinstance(step, dict):
            for key in ("capability", "instruction", "title"):
                if step.get(key):
                    parts.append(str(step[key]))
    return "\n".join(parts)


def _mentions_external_operation(candidate: dict) -> bool:
    blob = _candidate_text(candidate)
    tokens = ("外部操作", "外部AI", "送信", "投稿", "メール送信", "決済",
              "external", "send", "publish", "post", "email")
    lowered = blob.lower()
    return any(t.lower() in lowered for t in tokens)


def _requires_business_fact(candidate: dict) -> bool:
    blob = _candidate_text(candidate)
    tokens = ("業務事実", "人間の判断", "人間確認", "承認が必要", "事実確認",
              "business_fact", "human_input", "human approval")
    lowered = blob.lower()
    return any(t.lower() in lowered for t in tokens)


def _candidate_capabilities(candidate: dict) -> list[str]:
    caps: list[str] = []
    for step in _as_list(candidate.get("steps")):
        if isinstance(step, dict) and step.get("capability"):
            caps.append(str(step["capability"]))
    return caps


def _is_registered_capability(capability: str) -> bool:
    """登録済み triz_adapters / automatic_triz の関数IDだけを認める。"""
    cap = str(capability or "")
    if cap in ALLOWED_ADAPTER_FUNCTIONS:
        return True
    try:
        from app.triz_adapters import CAPABILITIES
        entry = CAPABILITIES.get(cap) or {}
        return bool(entry.get("available"))
    except Exception:
        return False


def _trial_status(candidate: dict, trials: dict) -> str:
    """候補IDに結び付いた隔離試験の状態。無ければ候補内の申告を見る。"""
    cid = str(candidate.get("id") or "")
    if isinstance(trials, dict) and cid in trials:
        entry = trials[cid]
        if isinstance(entry, dict):
            raw = str(entry.get("status") or entry.get("result") or "")
            if raw in TRIAL_SCORES:
                return raw
            if entry.get("passed") is True:
                return "passed"
            if entry.get("passed") is False:
                return "failed"
            return "unknown"
    raw = str(candidate.get("trial_status") or candidate.get("trial") or "")
    if raw in TRIAL_SCORES:
        return raw
    if candidate.get("trial_passed") is True:
        return "passed"
    if candidate.get("trial_passed") is False:
        return "failed"
    return "untested"


def _sources_score(candidate: dict, source_hashes: dict | None) -> tuple[int, str]:
    """必要な原本(source_refs)が登録済みで現行ハッシュと一致するか。

    source_hashes: {source_ref: 現行ハッシュ}。None のときは検証不能として降格
    (加点しない)し、欠ける場合は大きく降格する。
    """
    needed = [str(x) for x in _as_list(candidate.get("source_refs")) if str(x)]
    if not needed:
        # 必要原本の指定が無い候補は、原本照合の加点対象にしない。
        return (0, "必要原本の指定が無いため原本加点なし")
    if not isinstance(source_hashes, dict):
        return (0, "原本登録を確認できないため原本加点なし")
    missing = [ref for ref in needed if ref not in source_hashes]
    if missing:
        return (-30, "必要な原本が未登録のため降格: " + ", ".join(missing[:5]))
    mismatched = [ref for ref in needed
                  if isinstance(candidate.get("source_hashes"), dict)
                  and str((candidate.get("source_hashes") or {}).get(ref) or "")
                  and str((candidate.get("source_hashes") or {})[ref]) != str(source_hashes[ref])]
    if mismatched:
        return (-30, "必要な原本のハッシュが現行と不一致のため降格: " + ", ".join(mismatched[:5]))
    return (W_SOURCES, "必要な原本が登録済みで現行と一致")


def _scores_for(candidate: dict, *, criterion_id: str, task_key: str,
                source_hashes: dict | None, trials: dict) -> dict:
    cid = str(criterion_id or "")
    tkey = str(task_key or "")
    reasons: list[str] = []

    targets = {str(x) for x in (
        [candidate.get("criterion_id"), candidate.get("task_key")]
        + _as_list(candidate.get("criterion_ids")) + _as_list(candidate.get("task_keys"))) if x}
    if cid and cid in targets:
        relevance = W_RELEVANCE
        reasons.append("対象の達成条件(%s)と一致" % cid)
    elif tkey and tkey in targets:
        relevance = W_RELEVANCE
        reasons.append("対象工程(%s)と一致" % tkey)
    else:
        relevance = 0
        reasons.append("対象の達成条件・工程との明示的な一致なし")

    sources_score, sources_reason = _sources_score(candidate, source_hashes)
    reasons.append(sources_reason)

    status = _trial_status(candidate, trials)
    trial_score = TRIAL_SCORES.get(status, 0)
    if status in ("passed", "pass", "artifact_trial_passed"):
        reasons.append("隔離試験の記録あり(合格)。業務達成ではない")
    elif status in ("failed", "trial_failed"):
        reasons.append("隔離試験の記録あり(不合格)のため降格")
    elif status in ("running", "trial_running"):
        reasons.append("隔離試験中のため加点なし")
    else:
        reasons.append("隔離試験の記録なし(未試験)のため加点なし")

    risk_score = 0
    risk_notes: list[str] = []
    if _mentions_external_operation(candidate):
        risk_score += W_RISK
        risk_notes.append("外部操作を含むため大きく降格")
    if _requires_business_fact(candidate):
        risk_score += W_RISK
        risk_notes.append("業務事実の判断を要するため大きく降格")
    unknown_caps = [c for c in _candidate_capabilities(candidate)
                    if not _is_registered_capability(c)]
    if unknown_caps:
        # 未登録関数は試験対象にできない(動的exec禁止)。大きく降格する。
        risk_score += W_RISK
        risk_notes.append("未登録の能力を含むため大きく降格: " + ", ".join(unknown_caps[:5]))
    if not risk_notes:
        risk_notes.append("既知の業務リスクの記載なし")
    reasons.extend(risk_notes)

    total = int(relevance) + int(sources_score) + int(trial_score) + int(risk_score)
    return {
        "relevance": int(relevance),
        "sources": int(sources_score),
        "trial": int(trial_score),
        "trial_status": status,
        "risk": int(risk_score),
        "total": total,
        "reasons": reasons,
    }


def rank_candidates(candidates: list[dict], *, criterion_id: str = "",
                    task_key: str = "", source_hashes: dict | None = None,
                    trials: dict | None = None,
                    error_code: str = "") -> dict:
    """候補を決定的に順位付けする。戻り値は保存・表示できる形。

    既知P0欠陥(error_code が KNOWN_P0_ERROR_CODES)はTRIZ対象から除外する
    (従来どおり)。除外した候補は ranked に含めず excluded に理由付きで入れる。
    """
    trials = dict(trials or {})
    ranked: list[dict] = []
    excluded: list[dict] = []
    if str(error_code or "") in KNOWN_P0_ERROR_CODES:
        for candidate in candidates or []:
            if isinstance(candidate, dict):
                excluded.append({
                    "candidate_id": str(candidate.get("id") or ""),
                    "verdict": "excluded_known_p0",
                    "reason_ja": "既知P0欠陥(%s)はTRIZ対象から除外する(従来どおり)" % error_code,
                })
        return {
            "criterion_id": str(criterion_id or ""),
            "task_key": str(task_key or ""),
            "ranked": [],
            "excluded": sorted(excluded, key=lambda x: x["candidate_id"]),
            "note_ja": ("既知P0欠陥のためTRIZ候補は対象外。全%d件を除外した。"
                        % len(excluded)) + RISK_NOTE,
            "risk_note_ja": RISK_NOTE,
        }
    scored: list[tuple[int, str, dict, dict]] = []
    for candidate in candidates or []:
        if not isinstance(candidate, dict):
            continue
        cid = str(candidate.get("id") or "")
        if not cid:
            continue
        detail = _scores_for(candidate, criterion_id=criterion_id, task_key=task_key,
                             source_hashes=source_hashes, trials=trials)
        scored.append((detail["total"], cid, candidate, detail))
    # 決定的: 合計点の降順、同点は候補IDの昇順。
    scored.sort(key=lambda item: (-item[0], item[1]))
    for rank, (total, cid, candidate, detail) in enumerate(scored, 1):
        summary = ("第%d位(合計%d点: 関連%d/原本%d/試験%d(%s)/リスク%d)。%s" % (
            rank, total, detail["relevance"], detail["sources"],
            detail["trial"], detail["trial_status"], detail["risk"],
            " / ".join(detail["reasons"])))
        ranked.append({
            "rank": rank,
            "candidate_id": cid,
            "score": total,
            "scores": {
                "relevance": detail["relevance"],
                "sources": detail["sources"],
                "trial": detail["trial"],
                "trial_status": detail["trial_status"],
                "risk": detail["risk"],
            },
            "reasons": list(detail["reasons"]),
            "reason_ja": summary,
        })
    if ranked:
        note = "候補%d件を順位付けした(同点は候補ID順)。%s" % (len(ranked), RISK_NOTE)
    else:
        note = "順位付けできる候補が無い。" + RISK_NOTE
    return {
        "criterion_id": str(criterion_id or ""),
        "task_key": str(task_key or ""),
        "ranked": ranked,
        "excluded": [],
        "note_ja": note,
        "risk_note_ja": RISK_NOTE,
    }
