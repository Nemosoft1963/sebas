"""P3 ギャップ3: 解決コーディネータ(P2)との読み取り接続。

resolution_coordinator が execution_failure / artifact_mismatch 等を
診断したとき、RAG適格性の結果(app/rag_eligibility.py)と候補順位
(app/triz_candidate_ranking.py)を、解決記録の rag_references /
triz_candidates に読み取りで取り込めるようにする。
コーディネータは実行も承認もしない。参照を持つだけ。
既存の引き渡し(recovery_record への handoff)は維持する。

副作用なし: 経験DB・索引・TRIZ保存JSONの読み取りのみ。
RAG登録・承認・実行・自動再開の有効化は行わない。
外部AIへ原本・個人情報・非公開事例・RAG事例本文を送らない。
"""
from __future__ import annotations

import hashlib
import json

_ELIGIBLE_CAUSES = frozenset({
    "execution_failure",
    "artifact_mismatch",
    "goal_evidence_missing",
    "source_unreadable",
    "unknown",
})


def _current_hashes(manager, project_id: str) -> dict:
    try:
        mission = manager.memory.get_mission(project_id)
    except Exception:
        return {"input_version": "", "source_hash": ""}
    try:
        from app.plan_case_reference import current_input_version
        input_version = current_input_version(manager, project_id, mission)
    except Exception:
        input_version = ""
    source_hash = ""
    try:
        from app.goal_contract import get_active
        from app.completion_gate import _current_hashes as _gate_hashes
        contract = get_active(manager, project_id) or {}
        source_hash = str((_gate_hashes(manager, project_id, contract, mission) or {}).get("source_hash") or "")
    except Exception:
        source_hash = ""
    return {"input_version": str(input_version or ""), "source_hash": source_hash}


def _registered_source_hashes(manager, project_id: str) -> dict | None:
    """{source_ref候補: 現行ハッシュ}。検証不能なら None(降格側に倒す)。"""
    try:
        files = manager.memory.list_context_files(str(project_id or ""), include_content=False)
    except Exception:
        return None
    hashes: dict[str, str] = {}
    for item in files or []:
        if not isinstance(item, dict) or item.get("source") == "memo":
            continue
        digest = str(item.get("sha256") or "").strip()
        if not digest:
            continue
        for key in (item.get("id"), item.get("filename")):
            if key:
                hashes[str(key)] = digest
    return hashes or None


def _rag_candidates(exp_memory, project: str, query: str) -> list[dict]:
    """索引の候補IDに対応する正本行だけを読む(内容の送信なし)。"""
    pid = str(project or "").strip()
    from app.plan_case_reference import SEARCH_CANDIDATE_LIMIT
    try:
        search_ids = exp_memory.build_index().search(pid, str(query or "")[:6000], SEARCH_CANDIDATE_LIMIT)
    except Exception:
        return []
    rows: list[dict] = []
    for rid in list(dict.fromkeys([str(v) for v in (search_ids or [])]))[:SEARCH_CANDIDATE_LIMIT]:
        try:
            row = exp_memory.store.get(pid, rid)
        except Exception:
            continue
        if row is None:
            rows.append({"id": rid, "project": "__other__", "status": "unknown"})
        else:
            rows.append(row)
    return rows


def _triz_stored_candidates(manager, project_id: str, task_key: str) -> tuple[list[dict], dict, str]:
    """保存済みTRIZ結果の候補・試験・error_codeだけを読む。無ければ空。

    戻り値: (candidates, trials{candidate_id: {status}}, error_code)。
    呼び出し側の申告は信用せず、保存JSONだけを見る。
    """
    from pathlib import Path
    pid = str(project_id or "").strip()
    try:
        mission = manager.memory.get_mission(pid)
    except Exception:
        return ([], {}, "")
    task = next((x for x in (mission.get("tasks") or [])
                 if str(x.get("task_key") or x.get("id") or "") == str(task_key or "")), None)
    if not task:
        return ([], {}, "")
    error_code = ""
    try:
        from app.automatic_triz import classify_error_code
        from app.vehicle_workflow import load_input
        try:
            envelope = load_input(manager, pid)
        except Exception:
            envelope = None
        error_code = classify_error_code(str(task.get("error") or task.get("result") or ""), envelope)
    except Exception:
        error_code = ""
    candidates: list[dict] = []
    trials: dict = {}
    try:
        from app.vehicle_workflow import resolve
        root = resolve(manager, pid, "result/triz/%s" % task.get("id"))
    except Exception:
        return (candidates, trials, error_code)
    best = None
    try:
        paths = [p for p in Path(root).glob("*.json") if p.is_file()] if Path(root).is_dir() else []
    except OSError:
        paths = []
    for path in paths:
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if item.get("task_id") != task.get("id"):
            continue
        if best is None or path.stat().st_mtime >= best[0]:
            best = (path.stat().st_mtime, item)
    if best is None:
        return (candidates, trials, error_code)
    stored = best[1]
    for item in stored.get("candidates") or []:
        if isinstance(item, dict) and item.get("id"):
            candidates.append(item)
    for trial in stored.get("experiments") or []:
        if not isinstance(trial, dict):
            continue
        cid = str(trial.get("candidate") or trial.get("candidate_id") or "")
        if not cid:
            continue
        cases = trial.get("cases") or []
        if cases and all(isinstance(c, dict) and ((c.get("checks") or {}).get("passed") is True)
                         for c in cases):
            trials[cid] = {"status": "passed"}
        elif cases and any(isinstance(c, dict) and ((c.get("checks") or {}).get("passed") is False)
                           for c in cases):
            trials[cid] = {"status": "failed"}
        elif stored.get("status") == "artifact_trial_passed":
            trials[cid] = {"status": "passed"}
        else:
            trials[cid] = {"status": "unknown"}
    return (candidates, trials, error_code)


def collect_p3_context(manager, project_id: str, row: dict) -> dict:
    """解決記録1件分のRAG適格性と候補順位を読み取りで集める(副作用なし)。

    row: resolution_coordinator の内部行(_decode済み想定。public_viewでも可)。
    戻り値: {"rag_references": {...}, "triz_candidates": {...}}。
    対象外の原因(計画矛盾など)では空の参照を返す(何も主張しない)。
    """
    from app import rag_eligibility as _elig
    from app import triz_candidate_ranking as _ranking
    pid = str(project_id or "")
    cause = str((row or {}).get("cause") or "")
    criterion_id = str((row or {}).get("criterion_id") or "")
    task_key = str((row or {}).get("task_key") or "")
    empty = {
        "rag_references": {"eligible": [], "rejected": [], "summary_ja": "",
                           "note_ja": "対象外の原因のためRAG適格性を主張しない",
                           "cause": cause},
        "triz_candidates": {"ranked": [], "excluded": [], "note_ja": "",
                            "risk_note_ja": _ranking.RISK_NOTE, "cause": cause},
    }
    if cause not in _ELIGIBLE_CAUSES:
        return empty
    hashes = _current_hashes(manager, pid)
    input_version = str(hashes.get("input_version") or "")
    source_hash = str(hashes.get("source_hash") or "")

    # RAG適格性: 経験RAGが無効・未設定なら空参照(何も主張しない)。
    rag_view: dict = {"eligible": [], "rejected": [],
                      "summary_ja": "RAGが無効のため適格性を主張しない",
                      "note_ja": _elig.SUMMARY_NOTE, "cause": cause}
    try:
        from app.experience_memory import configured_memory
        setting = configured_memory(manager.memory.path, pid)
    except Exception:
        setting = None
    if setting is not None:
        exp_memory, _mode = setting
        query_parts = [criterion_id, task_key]
        try:
            evidence = (row.get("evidence") or {})
            gate = (evidence.get("gate") or {}) if isinstance(evidence, dict) else {}
            for key in ("message", "reason_code"):
                if gate.get(key):
                    query_parts.append(str(gate[key]))
        except Exception:
            pass
        query = " ".join(p for p in query_parts if p)[:6000] or criterion_id or task_key
        try:
            rows = _rag_candidates(exp_memory, pid, query)
            states: dict[str, dict | None] = {}
            for item in rows:
                rid = str(item.get("id") or "")
                if not rid:
                    continue
                try:
                    states[rid] = exp_memory.store.get_index_state(pid, rid)
                except Exception:
                    states[rid] = None
            assessed = _elig.assess_rows(exp_memory, pid, input_version, source_hash,
                                         rows, states, None)
        except Exception as exc:
            assessed = {"eligible": [], "rejected": [],
                        "summary_ja": "RAG適格性の評価に失敗したため適格事例なし(%s)" % type(exc).__name__,
                        "notes": [_elig.SUMMARY_NOTE, _elig.HYPOTHESIS_NOTE, _elig.NO_ELIGIBLE_NOTE]}
        rag_view = {
            "eligible": [{"case_id": x["case_id"], "excerpt": x.get("excerpt", ""),
                          "verdict": x.get("verdict", ""),
                          "reason": x.get("reason", "")} for x in assessed.get("eligible", [])],
            "rejected": [{"case_id": x["case_id"], "verdict": x.get("verdict", ""),
                          "reason": x.get("reason", "")} for x in assessed.get("rejected", [])],
            "summary_ja": str(assessed.get("summary_ja") or ""),
            "note_ja": _elig.SUMMARY_NOTE,
            "cause": cause,
        }

    # 候補順位: 保存済みTRIZ候補だけを対象にする。無ければ空(主張しない)。
    triz_view: dict = {"ranked": [], "excluded": [],
                       "note_ja": "保存済みTRIZ候補が無いため順位を主張しない",
                       "risk_note_ja": _ranking.RISK_NOTE, "cause": cause}
    try:
        stored_candidates, trials, error_code = _triz_stored_candidates(manager, pid, task_key)
    except Exception:
        stored_candidates, trials, error_code = ([], {}, "")
    if stored_candidates or str(error_code or ""):
        try:
            ranked_view = _ranking.rank_candidates(
                stored_candidates, criterion_id=criterion_id, task_key=task_key,
                source_hashes=_registered_source_hashes(manager, pid),
                trials=trials, error_code=error_code)
        except Exception as exc:
            ranked_view = {"ranked": [], "excluded": [],
                           "note_ja": "候補順位の評価に失敗したため順位を主張しない(%s)" % type(exc).__name__,
                           "risk_note_ja": _ranking.RISK_NOTE}
        triz_view = {
            "ranked": list(ranked_view.get("ranked") or []),
            "excluded": list(ranked_view.get("excluded") or []),
            "note_ja": str(ranked_view.get("note_ja") or ""),
            "risk_note_ja": str(ranked_view.get("risk_note_ja") or _ranking.RISK_NOTE),
            "cause": cause,
        }
    return {"rag_references": rag_view, "triz_candidates": triz_view}


def attach_p3_context(view: dict, context: dict | None) -> dict:
    """公開形(public_view)のコピーに参照だけを載せる(DB書き込みなし)。

    view 自体は変更しない。context が None なら空参照を載せる。
    """
    from app import triz_candidate_ranking as _ranking
    out = dict(view or {})
    context = dict(context or {})
    rag = dict(context.get("rag_references") or {})
    triz = dict(context.get("triz_candidates") or {})
    out["rag_references"] = {
        "eligible": list(rag.get("eligible") or []),
        "rejected": list(rag.get("rejected") or []),
        "summary_ja": str(rag.get("summary_ja") or ""),
        "note_ja": str(rag.get("note_ja") or ""),
    }
    out["triz_candidates"] = {
        "ranked": list(triz.get("ranked") or []),
        "excluded": list(triz.get("excluded") or []),
        "note_ja": str(triz.get("note_ja") or ""),
        "risk_note_ja": str(triz.get("risk_note_ja") or _ranking.RISK_NOTE),
    }
    # 既存の公開フィールド rag_refs / rag_rejection / triz_info は変えない。
    return out


def _fingerprint_context(context: dict) -> str:
    raw = json.dumps(context or {}, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]
