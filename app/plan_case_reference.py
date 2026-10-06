"""P1-B: RAGを計画工程に明示接続する統一インターフェース。

計画生成前に達成条件ごとに候補事例を検索し、現在の原本・業種・処理対象・
入力版・適用条件を照合する。上限件数と文字数を固定し、関連度だけで自動採用しない。
監査記録(plan_version/plan_signature/criterion_id/case_id/case_hash/
applicability_verdict/used_or_rejected/reason)を永続化する。
事例本文は必要最小限で、計画の達成証拠としては扱わない。
RAGによる品質改善は主張しない(「参考」であることを明記)。

検索は既存の経験RAG検索経路を使い、正本DBで再照合したものだけを対象にする。
needs_review・candidate・旧入力版は絶対に渡さない。
検索失敗時も計画生成を止めず unavailable を記録する。
外部AIへ事例本文や私的原本は送らない(ローカルLLMプロンプトへの参考追記のみ)。
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

# 上限を定数で固定する(条件あたり3件・事例あたり400字)。
MAX_CASES_PER_CRITERION = 3
MAX_CASE_CHARS = 400
SEARCH_CANDIDATE_LIMIT = 12

REFERENCE_DISCLAIMER = (
    "事例は工程候補の参考であり、達成証拠ではない。RAGによる品質改善は主張しない。"
    "必須工程・検証条件を事例で上書きしない。"
)
STATUS_REFERENCED = "referenced"
STATUS_REJECTED = "rejected"
STATUS_NO_CASE = "no_case"
STATUS_UNAVAILABLE = "unavailable"


def _fingerprint(value) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def case_hash_for(content: str) -> str:
    return hashlib.sha256(str(content or "").encode()).hexdigest()


def truncate_excerpt(text: str, limit: int = MAX_CASE_CHARS) -> str:
    compact = " ".join(str(text or "").split())
    if len(compact) <= limit:
        return compact
    return compact[: max(0, limit - 1)].rstrip() + "…"


def normalize_criteria(criteria) -> list[dict]:
    """GoalContractのcriteriaまたは文字列リストを {criterion_id, statement} に正規化する。"""
    normalized: list[dict] = []
    if not isinstance(criteria, list):
        return normalized
    auto = 0
    for item in criteria:
        if isinstance(item, dict):
            cid = str(item.get("criterion_id") or "").strip()
            statement = str(item.get("statement") or item.get("criterion") or "").strip()
            if not cid:
                auto += 1
                cid = "SC%02d" % auto
            if not statement:
                continue
            normalized.append({"criterion_id": cid, "statement": statement})
        else:
            text = str(item or "").strip()
            if not text:
                continue
            auto += 1
            normalized.append({"criterion_id": "SC%02d" % auto, "statement": text})
    # 文字列入力で SC01.. が既に採番済みの場合の二重採番を避けるため、連番を振り直さない。
    # dict入力のIDは尊重する。
    return normalized


def plan_signature_for(project: str, plan_version: int, input_version: str, criteria: list[dict]) -> str:
    return _fingerprint({
        "project": project,
        "plan_version": int(plan_version or 0),
        "input_version": str(input_version or ""),
        "criteria": [{"id": c["criterion_id"], "statement": c["statement"]} for c in criteria],
    })[:24]


def ensure_table(store) -> None:
    with store.connect() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS plan_case_references(
          project TEXT NOT NULL,
          plan_version INTEGER NOT NULL,
          plan_signature TEXT NOT NULL,
          criterion_id TEXT NOT NULL,
          case_id TEXT NOT NULL,
          case_hash TEXT NOT NULL DEFAULT '',
          applicability_verdict TEXT NOT NULL DEFAULT '',
          used_or_rejected TEXT NOT NULL DEFAULT '',
          reason TEXT NOT NULL DEFAULT '',
          excerpt TEXT NOT NULL DEFAULT '',
          created REAL NOT NULL,
          PRIMARY KEY(project, plan_version, plan_signature, criterion_id, case_id))""")


def _decode_json(value, default):
    try:
        parsed = json.loads(value) if isinstance(value, str) else dict(value or {})
    except (ValueError, TypeError):
        return default
    return parsed if isinstance(parsed, dict) else default


def _evaluate_row(row: dict, evidence: dict, applies: dict, input_version: str,
                  extra_context: dict, eligible_ids: set[str], index_state: dict | None,
                  current_identity: str) -> tuple[str, str, str]:
    """(verdict, used_or_rejected, reason) を返す。関連度スコアだけでは採用しない。"""
    rid = str(row.get("id") or "")
    if row.get("status") != "verified":
        return ("not_verified", "rejected", "正本でverifiedではないため不採用(status=%s)" % row.get("status"))
    try:
        expires = float(row.get("expires") or 0)
    except (TypeError, ValueError):
        expires = 0
    if not (expires > time.time()):
        return ("expired", "rejected", "有効期限切れのため不採用")
    if row.get("kind") == "external":
        return ("external_cache", "rejected", "外部回答キャッシュは計画の参考にしない")
    if rid not in (eligible_ids or set()):
        return ("not_verified_in_canonical_store", "rejected",
                "正本の再照合で不採用(取消・期限切れ・原本改変・needs_review・証拠無効のいずれか)")
    stored_content_hash = str(evidence.get("content_sha256") or "").strip()
    if stored_content_hash and case_hash_for(row.get("content")) != stored_content_hash:
        return ("content_hash_mismatch", "rejected", "原本改変の疑いのため不採用(内容ハッシュ不一致)")
    # 入力版の照合。旧入力版・欠落は渡さない。
    want_version = str(input_version or "").strip()
    got_version = str(applies.get("input_version") or "").strip()
    if not got_version:
        return ("missing_input_version", "rejected", "適用条件の入力版が無いため不採用")
    # 現行スコープの版が確定している場合は完全一致させる。取込時の v1 は
    # 既存形式の「現行版」指定として扱うが、old 等の明示的な旧版は拒否する。
    if got_version != want_version:
        return ("input_version_mismatch", "rejected",
                "旧入力版のため不採用(事例=%s, 現行=%s)" % (got_version[:24], want_version[:24]))
    # 索引状態の照合。索引は再生成可能なキャッシュであり、未索引・失敗・旧identityは渡さない。
    if not index_state or index_state.get("status") != "indexed":
        status = (index_state or {}).get("status") or "pending"
        return ("not_indexed", "rejected", "索引が未反映のため不採用(index_status=%s)" % status)
    if (index_state.get("index_identity") or "") != current_identity:
        return ("index_identity_mismatch", "rejected", "旧索引のため不採用(埋め込み版の不一致)")
    # 適用条件の照合。input_version以外に条件があれば現在の業種・処理対象等と突合する。
    # 現在値が不明な条件は検証不能として不採用にし、関連度だけで採用しない。
    for key, value in applies.items():
        if key == "input_version":
            continue
        if extra_context is not None and key in extra_context:
            if str(extra_context.get(key) or "") != str(value or ""):
                return ("not_applicable", "rejected",
                        "適用条件の不一致のため不採用(%s)" % key)
        else:
            return ("unverified_condition", "rejected",
                    "適用条件を確認できないため不採用(%s)" % key)
    return ("applicable", "used", "適用条件に一致したため参考として採用")


def get_case_references(exp_memory, project: str, criteria, input_version: str,
                        plan_version: int, plan_signature: str,
                        extra_context: dict | None = None) -> dict:
    """統一インターフェース: 両方の計画経路から同じ関数で参照結果を取得する。

    exp_memory は実在の ExperienceMemory、criteria は GoalContract の criteria
    (または文字列リスト)、input_version は現行の入力版。検索は既存の索引経路を
    使うが、返された事例は正本DBで再照合したものだけを対象にする。
    例外時は計画生成を止めず unavailable を記録する。
    """
    items = normalize_criteria(criteria)
    project = str(project or "").strip()
    input_version = str(input_version or "").strip()
    plan_version = int(plan_version or 0)
    plan_signature = str(plan_signature or "").strip()
    extra = dict(extra_context or {})
    # L3: 論理削除中のPJは計画・RAG参照から除外する。
    try:
        from app.project_delete import is_deleted as _l3_is_deleted
        _mem = None
        try:
            _store = getattr(exp_memory, "store", None)
            _root = getattr(getattr(_store, "path", None), "parent", None)
            # experience.sqlite3 の親(experience_memory)の親が DATA_DIR 相当。
            # memory.path は DATA_DIR/memory/conversations.db の規則。
            if _root is not None:
                from pathlib import Path as _Path
                _mem = _Path(str(_root)).parent / "memory" / "conversations.db"
        except Exception:
            _mem = None
        if _mem is not None:
            try:
                if _l3_is_deleted(_mem, project):
                    result_none: dict = {
                        "project": project,
                        "plan_version": plan_version,
                        "plan_signature": plan_signature,
                        "input_version": input_version,
                        "note": "参考（達成証拠ではない）。" + REFERENCE_DISCLAIMER,
                        "criteria": [],
                    }
                    for entry in items:
                        result_none["criteria"].append({
                            "criterion_id": entry["criterion_id"], "statement": entry["statement"],
                            "status": STATUS_UNAVAILABLE, "references": [],
                            "reason": "論理削除中のため参照不可",
                        })
                    return result_none
            except Exception:
                pass
    except Exception:
        pass
    result: dict = {
        "project": project,
        "plan_version": plan_version,
        "plan_signature": plan_signature,
        "input_version": input_version,
        "note": "参考（達成証拠ではない）。" + REFERENCE_DISCLAIMER,
        "criteria": [],
    }
    if not project or not items or not plan_signature:
        for entry in items:
            result["criteria"].append({
                "criterion_id": entry["criterion_id"], "statement": entry["statement"],
                "status": STATUS_UNAVAILABLE, "references": [],
                "reason": "参照条件が不足しているため利用不可",
            })
        return result
    try:
        ensure_table(exp_memory.store)
    except Exception:
        for entry in items:
            result["criteria"].append({
                "criterion_id": entry["criterion_id"], "statement": entry["statement"],
                "status": STATUS_UNAVAILABLE, "references": [],
                "reason": "監査記録の準備に失敗したため利用不可",
            })
        return result
    try:
        from app.experience_memory import current_index_identity
        current_identity = current_index_identity(exp_memory.config)
    except Exception:
        current_identity = ""
    try:
        eligible_ids = {r["id"] for r in exp_memory.verified_rows_for_index(project)}
    except Exception as exc:
        for entry in items:
            _persist_marker(exp_memory, project, plan_version, plan_signature,
                            entry["criterion_id"], STATUS_UNAVAILABLE,
                            "正本の再照合に失敗したため利用不可(%s)" % type(exc).__name__)
            result["criteria"].append({
                "criterion_id": entry["criterion_id"], "statement": entry["statement"],
                "status": STATUS_UNAVAILABLE, "references": [],
                "reason": "正本の再照合に失敗したため利用不可",
            })
        return result
    for entry in items:
        cid = entry["criterion_id"]
        query = entry["statement"]
        try:
            search_ids = exp_memory.build_index().search(project, query[:6000], SEARCH_CANDIDATE_LIMIT)
        except Exception as exc:
            _persist_marker(exp_memory, project, plan_version, plan_signature, cid,
                            STATUS_UNAVAILABLE,
                            "検索に失敗したため利用不可(%s)。計画生成は継続する。" % type(exc).__name__)
            result["criteria"].append({
                "criterion_id": cid, "statement": query,
                "status": STATUS_UNAVAILABLE, "references": [],
                "reason": "検索に失敗したため利用不可。計画生成は継続する。",
            })
            try:
                exp_memory.store.audit_retrieval(project, query, [], "plan_case_reference_unavailable")
            except Exception:
                pass
            continue
        if not search_ids:
            _persist_marker(exp_memory, project, plan_version, plan_signature, cid,
                            STATUS_NO_CASE, "該当する事例が無いため参考なし")
            result["criteria"].append({
                "criterion_id": cid, "statement": query,
                "status": STATUS_NO_CASE, "references": [],
                "reason": "該当する事例が無いため参考なし",
            })
            try:
                exp_memory.store.audit_retrieval(project, query, [], "plan_case_reference_no_case")
            except Exception:
                pass
            continue
        evaluated: list[dict] = []
        used_count = 0
        for rid in list(dict.fromkeys([str(v) for v in search_ids]))[:SEARCH_CANDIDATE_LIMIT]:
            row = exp_memory.store.get(project, rid)
            if row is None:
                evaluated.append({
                    "criterion_id": cid, "case_id": rid, "case_hash": "",
                    "applicability_verdict": "cross_project_or_removed",
                    "used_or_rejected": "rejected",
                    "reason": "他案件または削除済みのため不採用",
                    "excerpt": "",
                })
                continue
            evidence = _decode_json(row.get("evidence"), {})
            applies = _decode_json(row.get("applicability"), {})
            try:
                state = exp_memory.store.get_index_state(project, rid)
            except Exception:
                state = None
            verdict, used_or_rejected, reason = _evaluate_row(
                row, evidence, applies, input_version, extra, eligible_ids, state, current_identity)
            if used_or_rejected == "used":
                if used_count >= MAX_CASES_PER_CRITERION:
                    verdict, used_or_rejected, reason = (
                        "limit_exceeded", "rejected",
                        "条件あたりの上限(%d件)のため不採用" % MAX_CASES_PER_CRITERION)
                else:
                    used_count += 1
            evaluated.append({
                "criterion_id": cid, "case_id": rid,
                "case_hash": case_hash_for(row.get("content")),
                "applicability_verdict": verdict,
                "used_or_rejected": used_or_rejected,
                "reason": reason,
                "excerpt": truncate_excerpt(row.get("content")) if used_or_rejected == "used" else "",
            })
        # 監査記録へ冪等保存する(同一の計画版・条件・事例は上書きで1行)。
        _persist_rows(exp_memory, project, plan_version, plan_signature, evaluated)
        try:
            exp_memory.store.audit_retrieval(
                project, query, [e["case_id"] for e in evaluated if e["used_or_rejected"] == "used"],
                "plan_case_reference")
        except Exception:
            pass
        used = [e for e in evaluated if e["used_or_rejected"] == "used"]
        status = STATUS_REFERENCED if used else STATUS_REJECTED
        reason = "" if used else "候補はあったが適用条件の不一致等のため不採用"
        result["criteria"].append({
            "criterion_id": cid, "statement": query,
            "status": status, "references": evaluated, "reason": reason,
        })
    return result


def _persist_rows(exp_memory, project: str, plan_version: int, plan_signature: str,
                  evaluated: list[dict]) -> None:
    now = time.time()
    try:
        with exp_memory.store.connect() as db:
            for item in evaluated:
                db.execute("""INSERT OR IGNORE INTO plan_case_references(
                  project, plan_version, plan_signature, criterion_id, case_id,
                  case_hash, applicability_verdict, used_or_rejected, reason, excerpt, created)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (
                    project, plan_version, plan_signature, item["criterion_id"], item["case_id"],
                    item.get("case_hash", ""), item.get("applicability_verdict", ""),
                    item.get("used_or_rejected", ""), str(item.get("reason", ""))[:2000],
                    str(item.get("excerpt", ""))[:2000], now))
    except Exception:
        pass


def _persist_marker(exp_memory, project: str, plan_version: int, plan_signature: str,
                    criterion_id: str, status: str, reason: str) -> None:
    _persist_rows(exp_memory, project, plan_version, plan_signature, [{
        "criterion_id": criterion_id, "case_id": "-",
        "case_hash": "", "applicability_verdict": status,
        "used_or_rejected": status, "reason": reason, "excerpt": "",
    }])


def format_criterion_hint(criterion_result: dict) -> str:
    """ローカルLLM経路で事例を「工程候補の参考」として最小限に渡す文面。"""
    used = [r for r in (criterion_result.get("references") or []) if r.get("used_or_rejected") == "used"]
    if not used:
        return ""
    lines = ["# 過去事例の参考（工程候補の参考。達成証拠ではない）",
             "対象達成条件: %s" % criterion_result.get("criterion_id", "")]
    for ref in used[:MAX_CASES_PER_CRITERION]:
        lines.append("- 参照ID %s: %s" % (ref.get("case_id", ""), ref.get("excerpt", "")))
    lines.append("注意: " + REFERENCE_DISCLAIMER)
    return "\n".join(lines)[: (MAX_CASE_CHARS * MAX_CASES_PER_CRITERION + 600)]


def format_for_prompt(result: dict) -> dict[str, str]:
    """達成条件ごとの参考文面。事例0件のときは空文字にして既存挙動を変えない。"""
    hints: dict[str, str] = {}
    for item in result.get("criteria") or []:
        hints[item.get("criterion_id", "")] = format_criterion_hint(item)
    return hints


def current_input_version(manager, project_id: str, mission: dict | None = None) -> str:
    """計画生成を包む memory_scope と同じ入力版指紋。自己申告値は使わない。"""
    pid = str(project_id or "").strip()
    try:
        current = mission if mission is not None else manager.memory.get_mission(pid)
        files = manager.memory.list_context_files(pid, include_content=True)
        inputs = [{"id": f["id"], "content": f.get("content", ""), "original": f.get("sha256"),
                    "updated_at": f.get("updated_at")}
                  for f in files if f.get("source") != "memo"]
        from app.experience_store import fingerprint as _fp
        return _fp({
            "project_context": manager.memory.get_project(pid).get("context_text", ""),
            "mission": {k: current.get(k) for k in (
                "plan_version", "goal", "success_criteria", "constraints_text",
                "instruction_messages")},
            "files": sorted(inputs, key=lambda f: f["id"]),
        })
    except Exception:
        return ""


def collect_for_current_plan(manager, project_id: str, criteria_input=None,
                             extra_context: dict | None = None) -> dict:
    """計画生成の2経路から同じ関数で参照結果を取得するための入口。

    manager は実在の ProjectOrchestrator、criteria_input は GoalContract の
    criteria(省略時は現行契約または達成条件の抽出から復元)。検索失敗・RAG無効時も
    例外を出さず unavailable/no_case を返すため、計画生成を止めない。
    """
    pid = str(project_id or "").strip()
    try:
        mission = manager.memory.get_mission(pid)
    except Exception:
        return {"project": pid, "plan_version": 0, "plan_signature": "",
                "note": "参考（達成証拠ではない）。" + REFERENCE_DISCLAIMER, "criteria": []}
    next_version = int(mission.get("plan_version") or 0) + 1
    criteria: list[dict] = []
    try:
        if criteria_input is not None:
            criteria = normalize_criteria(criteria_input)
        else:
            criteria = []
        if not criteria:
            try:
                from app import goal_contract as _gc
                preview = _gc.preview(manager, pid)
                raw = (preview or {}).get("criteria") or []
                criteria = normalize_criteria(raw)
            except Exception:
                criteria = []
        if not criteria:
            try:
                from app.structured_planning import extract_criteria as _extract
                statements = _extract(mission.get("goal") or "", mission.get("success_criteria") or "")
                criteria = normalize_criteria(statements)
            except Exception:
                criteria = []
    except Exception:
        criteria = []
    # 現行の入力版は計画生成を包む memory_scope と一致させる(自己申告値は信用しない)。
    input_version = ""
    exp_memory = None
    try:
        from app.experience_memory import CURRENT_MEMORY, configured_memory
        scope = CURRENT_MEMORY.get()
        if scope and scope.get("project") == pid and scope.get("memory") is not None:
            exp_memory = scope["memory"]
            input_version = str(scope.get("input_version") or "")
        else:
            setting = configured_memory(manager.memory.path, pid)
            if setting is not None:
                exp_memory, _mode = setting
    except Exception:
        exp_memory = None
    if exp_memory is None:
        try:
            from app.experience_memory import configured_memory as _configured
            setting = _configured(manager.memory.path, pid)
            if setting is not None:
                exp_memory, _mode = setting
        except Exception:
            exp_memory = None
    if not input_version:
        # memory_scope外(テスト等の直接呼出し)では mission+原本から決定的に復元する。
        # project_experience と同じ指紋を使い、自己申告値は信用しない。
        input_version = current_input_version(manager, pid, mission)
    if exp_memory is None:
        signature = plan_signature_for(pid, next_version, input_version, criteria)
        out: dict = {"project": pid, "plan_version": next_version, "plan_signature": signature,
                      "input_version": input_version,
                      "note": "参考（達成証拠ではない）。" + REFERENCE_DISCLAIMER, "criteria": []}
        for entry in criteria:
            out["criteria"].append({
                "criterion_id": entry["criterion_id"], "statement": entry["statement"],
                "status": STATUS_UNAVAILABLE, "references": [], "reason": "RAGが無効のため利用不可",
            })
        return out
    # 業種・処理対象などの追加条件は mission から復元できれば照合に使う(無ければ厳しめに不採用)。
    extra = dict(extra_context or {})
    try:
        for key in ("industry", "process_target", "task_kind"):
            value = mission.get(key)
            if value and key not in extra:
                extra[key] = value
    except Exception:
        pass
    signature = plan_signature_for(pid, next_version, input_version, criteria)
    try:
        return get_case_references(exp_memory, pid, criteria, input_version,
                                   next_version, signature, extra)
    except Exception:
        out = {"project": pid, "plan_version": next_version, "plan_signature": signature,
               "input_version": input_version,
               "note": "参考（達成証拠ではない）。" + REFERENCE_DISCLAIMER, "criteria": []}
        for entry in criteria:
            out["criteria"].append({
                "criterion_id": entry["criterion_id"], "statement": entry["statement"],
                "status": STATUS_UNAVAILABLE, "references": [], "reason": "参照処理に失敗したため利用不可",
            })
        return out


def current_view(memory_path, project_id: str, plan_version: int) -> dict:
    """現行計画版の参照結果と不採用理由を返す(API/画面用)。事例本文は最小限のみ。"""
    from app.experience_memory import configured_memory
    pid = str(project_id or "").strip()
    try:
        setting = configured_memory(memory_path, pid)
    except Exception:
        setting = None
    if setting is None:
        return {"project": pid, "plan_version": int(plan_version or 0), "plan_signature": "",
                "status": STATUS_UNAVAILABLE, "criteria": [],
                "note": "参考（達成証拠ではない）。RAGが無効のため利用不可。" + REFERENCE_DISCLAIMER}
    exp_memory, _mode = setting
    try:
        ensure_table(exp_memory.store)
    except Exception:
        pass
    rows: list[dict] = []
    signatures: list[str] = []
    try:
        with exp_memory.store.connect() as db:
            try:
                fetched = db.execute(
                    "SELECT * FROM plan_case_references WHERE project=? AND plan_version=? "
                    "ORDER BY criterion_id, case_id",
                    (pid, int(plan_version or 0))).fetchall()
            except Exception:
                fetched = []
            for row in fetched:
                item = dict(row)
                signatures.append(str(item.get("plan_signature") or ""))
                if str(item.get("case_id") or "") == "-":
                    rows.append({
                        "criterion_id": item.get("criterion_id", ""),
                        "case_id": "-", "case_hash": "",
                        "applicability_verdict": item.get("applicability_verdict", ""),
                        "used_or_rejected": item.get("used_or_rejected", ""),
                        "reason": item.get("reason", ""), "excerpt": "",
                    })
                else:
                    rows.append({
                        "criterion_id": item.get("criterion_id", ""),
                        "case_id": item.get("case_id", ""),
                        "case_hash": item.get("case_hash", ""),
                        "applicability_verdict": item.get("applicability_verdict", ""),
                        "used_or_rejected": item.get("used_or_rejected", ""),
                        "reason": item.get("reason", ""),
                        # 事例本文は最小限で返し、達成証拠としては表示しない。
                        "excerpt": str(item.get("excerpt", ""))[:MAX_CASE_CHARS],
                    })
    except Exception:
        rows = []
    by_criterion: dict[str, list[dict]] = {}
    for item in rows:
        by_criterion.setdefault(str(item.get("criterion_id") or ""), []).append(item)
    criteria_view = []
    for cid in sorted(by_criterion):
        refs = by_criterion[cid]
        used = [r for r in refs if r.get("used_or_rejected") == "used"]
        if any(r.get("used_or_rejected") == STATUS_UNAVAILABLE for r in refs):
            status = STATUS_UNAVAILABLE
        elif any(r.get("used_or_rejected") == STATUS_NO_CASE for r in refs):
            status = STATUS_NO_CASE
        elif used:
            status = STATUS_REFERENCED
        else:
            status = STATUS_REJECTED
        criteria_view.append({"criterion_id": cid, "status": status, "references": refs})
    signature = max(signatures, key=len) if signatures else ""
    return {"project": pid, "plan_version": int(plan_version or 0), "plan_signature": signature,
            "status": "ok" if criteria_view else STATUS_NO_CASE, "criteria": criteria_view,
            "note": ("参考にした事例と採用しなかった理由の一覧。事例本文は最小限であり、"
                     "達成証拠として表示しない。RAGによる品質改善は主張しない。")}
