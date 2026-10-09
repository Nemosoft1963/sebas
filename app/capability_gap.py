"""Stage 2-A: capability preflight + FunctionProposal (general mechanism).

範囲は 2-A のみ (事前照合・不足分類・提案の生成/保存/冪等/状態遷移/監査)。
2-B (Jenkins指示パッケージのエクスポート)、2-C (API/UI)、2-D (開発結果の
取込みと再判定) は別タスクであり、このモジュールには作らない。ただし
2-B〜2-D が使える関数境界を意識する:
- 2-B は list_proposals()/get_proposal() の読み取りだけを使う (出力は別途)。
- 2-C は set_proposal_status()/reject_proposal()/correct_proposal() を呼ぶ。
- 2-D の再判定だけが verified を付けられる。2-A には verified への遷移関数を
  持たせない (要求されたら拒否する)。

一般機構であり、特定の案件・件数・ID に依存しない。達成条件の文面と工程契約
(execution_kind / outputs / 宣言された required_capabilities /
public_web_research / 外部作用の action_requirements) から、コード所有の
制御語彙 (OPERATION_VOCABULARY) で決定的に対応付ける。ローカルAIの候補化は
スタブ可能な任意の補助 (ai_assist 引数) に留め、AI出力は語彙表との決定的な
照合を通ったものだけ採用し、曖昧・語彙外は unknown (人間確認) にする。

読み取り専用の原則: capability_preflight() は承認・実行・外部送信・RAG登録・
計画適用を一切行わない。提案の生成・保存は generate_proposals() で明示的に
呼ぶ (別関数)。外部AI呼び出しは行わない (外部AIとローカルLLMはテスト側の
スタブで計測する)。

5状態の検査 (implemented / configured / input_compatible / validated /
permitted) は個別に真偽と根拠を返す。workspace.write_text (文書の生成) を、
文書以外の成果 (システムの構築・稼働・公開・送信など) の代用にしない。
登録 (DEFINITIONS にある) だけで実行可能と判定しない。validated は既存の
レジストリの慣行に従う (OCR 系は ocr_runtime_validated() が False のため
検証済みにしない。実行検証の根拠が無いものを真にしない)。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_PROPOSAL = "sebas.function-proposal/v1"
SCHEMA_PREFLIGHT = "sebas.capability-preflight/v1"

GAP_CODES = (
    "missing_capability",
    "missing_input",
    "missing_evidence",
    "missing_binding",
    "policy_block",
    "unknown",
)

STATUSES = (
    "draft",
    "reviewed",
    "export_ready",
    "submitted",
    "delivered",
    "verified",
    "rejected",
    "stale",
    "needs_evidence",
)

#: 2-D (再判定) 以外で到達できる終了前状態。verified は含めない。
OPEN_STATUSES = (
    "draft",
    "reviewed",
    "export_ready",
    "submitted",
    "delivered",
    "needs_evidence",
)

#: 許される遷移。順序を飛ばせない。verified への遷移は未実装として拒否する。
ALLOWED_TRANSITIONS: dict[str, frozenset] = {
    "draft": frozenset({"reviewed", "rejected", "stale", "needs_evidence"}),
    "reviewed": frozenset({"export_ready", "rejected", "stale", "needs_evidence"}),
    "export_ready": frozenset({"submitted", "rejected", "stale", "needs_evidence"}),
    "submitted": frozenset({"delivered", "rejected", "stale", "needs_evidence"}),
    "delivered": frozenset({"stale"}),
    "needs_evidence": frozenset({"draft", "rejected", "stale"}),
    "rejected": frozenset({"stale"}),
    "stale": frozenset(),
    "verified": frozenset(),
}

#: 保存する引用の最大長。原本本文全文・外部AI回答全文は保存しない。
SHORT_QUOTE_CHARS = 140

#: 観測された失敗の制御語彙 (自由文の乱立を避ける)。
FAILURE_CODES = (
    "execution_contract_document_only",
    "no_registered_executor",
    "executor_not_configured",
    "input_missing",
    "evidence_missing",
    "binding_missing",
    "policy_blocked",
    "unknown_operation",
)

# ----------------------------------------------------------------------------
# 制御語彙 (コード内の1か所)。達成条件・工程契約から操作カテゴリへの対応付けは
# すべてこの表で決定的に行う。特定案件の語・ID・件数を含めないこと。
# any: いずれか1語の出現で対応。pairs: 全語の出現で対応 (単独では広すぎる語用)。
# executor: CapabilityRegistry の能力ID。None は登録実行器なし (不足候補)。
# hypothetical: 提案上の仮ID (CapabilityRegistry の available として登録しない)。
# ----------------------------------------------------------------------------

OPERATION_VOCABULARY: tuple[dict, ...] = (
    {
        "operation_id": "document_generation",
        "label_ja": "文書生成",
        "any": ("文書", "報告書", "議事録", "提案書", "ドキュメント", "作成", "生成", "報告"),
        "pairs": (),
        "executor": "workspace.write_text",
        "hypothetical": "proposal.document_generate",
        "permission": "local_reversible",
    },
    {
        "operation_id": "table_processing",
        "label_ja": "表データ加工",
        "any": ("管理表", "CSV", "集計", "加工", "検算", "テーブル", "クロス集計"),
        "pairs": (),
        "executor": "table.transform",
        "hypothetical": "proposal.table_process",
        "permission": "local_reversible",
    },
    {
        "operation_id": "public_research",
        "label_ja": "公開調査",
        "any": ("公開調査", "Web調査", "ウェブ調査", "情報収集", "公開情報"),
        "pairs": (("Web", "調査"), ("Web", "検索"), ("ウェブ", "検索"),
                  ("インターネット", "調査"), ("公開", "検索")),
        "executor": "web.collect_public",
        "hypothetical": "proposal.public_research",
        "permission": "local_reversible",
    },
    {
        "operation_id": "source_extraction",
        "label_ja": "原本抽出",
        "any": ("原本抽出", "抜粋", "引用", "原本確認", "原本読取"),
        "pairs": (("原本", "抽出"), ("原本", "確認"), ("原本", "読取")),
        "executor": "source.read_excerpt",
        "hypothetical": "proposal.source_extract",
        "permission": "local_reversible",
    },
    {
        "operation_id": "source_ocr",
        "label_ja": "原本の画像読取",
        "any": ("OCR", "スキャン", "画像読取"),
        "pairs": (("画像", "読取"),),
        "executor": "source.ocr_pdf_fallback",
        "hypothetical": "proposal.source_ocr_extract",
        "permission": "local_reversible",
    },
    {
        "operation_id": "external_publish",
        "label_ja": "外部作用(公開・投稿)",
        "any": ("SNS投稿", "サイト公開", "LP公開", "公開URL"),
        "pairs": (("公開", "投稿"), ("サイト", "公開"), ("告知", "公開"),
                  ("Google", "公開"), ("フォーム", "公開")),
        "executor": None,
        "hypothetical": "proposal.external_publish",
        "permission": "external_side_effect",
    },
    {
        "operation_id": "external_send",
        "label_ja": "外部作用(送信・連絡)",
        "any": ("送信", "連絡", "通知", "送付", "商談実施", "契約締結"),
        "pairs": (("顧客", "送信"), ("顧客", "連絡")),
        "executor": None,
        "hypothetical": "proposal.external_send",
        "permission": "external_side_effect",
    },
    {
        "operation_id": "system_build",
        "label_ja": "システム構築",
        "any": ("システム構築", "デプロイ"),
        "pairs": (("システム", "構築"), ("システム", "開発"), ("システム", "実装")),
        "executor": None,
        "hypothetical": "proposal.system_build_verify",
        "permission": "local_irreversible",
    },
    {
        "operation_id": "system_run",
        "label_ja": "システム稼働",
        "any": ("デプロイ",),
        "pairs": (("システム", "稼働"), ("システム", "運用"), ("稼働", "運用")),
        "executor": None,
        "hypothetical": "proposal.system_run_operate",
        "permission": "local_irreversible",
    },
    {
        "operation_id": "system_verify",
        "label_ja": "システム検証",
        "any": ("動作確認", "受入試験", "システム試験"),
        "pairs": (("システム", "検証"), ("動作", "確認"), ("稼働", "検証")),
        "executor": None,
        "hypothetical": "proposal.system_verify_evidence",
        "permission": "local_irreversible",
    },
    {
        "operation_id": "domain_pipeline",
        "label_ja": "領域固有パイプライン実行",
        "any": (),
        "pairs": (),
        "executor": None,
        "hypothetical": "proposal.domain_pipeline_verify",
        "permission": "local_reversible",
    },
)

OPERATION_INDEX = {entry["operation_id"]: entry for entry in OPERATION_VOCABULARY}

#: 達成条件の文面に含まれていれば policy_block とする制御語彙。
POLICY_SIGNALS = (
    "承認待ち",
    "予算上限",
    "予算切れ",
    "上限到達",
    "検証上限",
    "権限",
    "禁止",
)

#: 工程契約の execution_kind から操作への対応付け (部分一致・順序付き)。
_EXEC_KIND_OPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("campaign_execution_sequence", ("external_publish", "external_send")),
    ("google_site_publication", ("external_publish",)),
    ("social_post", ("external_publish",)),
    ("google_form", ("external_publish",)),
    ("lead_capture", ("external_send",)),
    ("approved_outbound", ("external_send",)),
    ("approved_contract", ("external_send",)),
    ("approved_customer_engagement", ("external_send",)),
    ("approved_publication", ("external_publish",)),
    ("approved_external", ("external_send",)),
    ("vehicle_", ("domain_pipeline",)),
    ("final_verification", ()),
)

#: 外部作用の action_requirements の kind から操作への対応付け (部分一致)。
_ACTION_KIND_OPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("google_site_publication", ("external_publish",)),
    ("social_post", ("external_publish",)),
    ("google_form", ("external_publish",)),
    ("approved_publication", ("external_publish",)),
    ("lead_capture", ("external_send",)),
    ("approved_outbound", ("external_send",)),
    ("approved_contract", ("external_send",)),
    ("approved_customer_engagement", ("external_send",)),
    ("approved_external_action", ("external_send",)),
    ("manual_social_post", ("external_publish",)),
    ("social_copy_approval", ("external_publish",)),
    ("post_url_registration", ("external_publish",)),
)

_EXTERNAL_SENDS_COUNTER = {"calls": 0}


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stable_id(*parts: str) -> str:
    raw = "|".join(str(p or "") for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _short_hash(value: object) -> str:
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        raw = str(value)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _safe_short(text: object, limit: int = SHORT_QUOTE_CHARS) -> str:
    """保存用の短い引用。秘密混入の疑いは種別ラベルだけ残し値を落とす。全文は残さない。"""
    blob = str(text or "")
    try:
        from app.plan_review_loop import contains_secret

        label = contains_secret(blob)
    except Exception:
        label = ""
    if label:
        return f"[秘密混入の疑いのため保持しません:{label}]"
    return " ".join(blob.split())[: max(1, int(limit))]


def _match_vocab(text: str) -> list[str]:
    """文面から操作IDを決定的に抽出する。順序は OPERATION_VOCABULARY の定義順。"""
    blob = str(text or "")
    found: list[str] = []
    for entry in OPERATION_VOCABULARY:
        op = entry["operation_id"]
        if op in ("domain_pipeline",):
            continue
        hit = any(token and token in blob for token in entry.get("any") or ())
        if not hit:
            for pair in entry.get("pairs") or ():
                if all(token and token in blob for token in pair):
                    hit = True
                    break
        if hit and op not in found:
            found.append(op)
    return found


def _has_policy_signal(text: str) -> bool:
    blob = str(text or "")
    return any(sig in blob for sig in POLICY_SIGNALS)


# ----------------------------------------------------------------------------
# 工程契約の読取り (判定は再実装しない。対応付けの材料として読むだけ)
# ----------------------------------------------------------------------------

def _contract_of(task: dict) -> dict:
    try:
        from app.structured_planning import contract_of

        return contract_of(task) or {}
    except Exception:
        return {}


def _contract_operations(contract: dict) -> list[str]:
    """工程契約の宣言から操作IDを抽出する (文面の推測ではなく宣言を読む)。"""
    ops: list[str] = []
    kind = str(contract.get("execution_kind") or "")
    for needle, mapped in _EXEC_KIND_OPS:
        if needle and needle in kind:
            for op in mapped:
                if op not in ops:
                    ops.append(op)
    for output in contract.get("outputs") or []:
        path = str((output or {}).get("path") or "") if isinstance(output, dict) else ""
        lowered = path.lower()
        if lowered.endswith(".md") and "document_generation" not in ops:
            ops.append("document_generation")
        elif lowered.endswith((".csv", ".xlsx")) and "table_processing" not in ops:
            ops.append("table_processing")
    web = contract.get("public_web_research") or {}
    if isinstance(web, dict) and web.get("required") and "public_research" not in ops:
        ops.append("public_research")
    for req in contract.get("action_requirements") or []:
        if not isinstance(req, dict):
            continue
        req_kind = str(req.get("kind") or "")
        for needle, mapped in _ACTION_KIND_OPS:
            if needle and needle in req_kind:
                for op in mapped:
                    if op not in ops:
                        ops.append(op)
    for key in ("required_capabilities", "capabilities", "required_capability_ids"):
        declared = contract.get(key) or []
        if not isinstance(declared, list):
            continue
        for cap in declared:
            name = str(cap or "")
            if not name:
                continue
            for entry in OPERATION_VOCABULARY:
                if entry.get("executor") == name and entry["operation_id"] not in ops:
                    ops.append(entry["operation_id"])
    return ops


def _extract_required_operations(statement: str, contracts: list[dict],
                                 ai_candidates: list[str] | None = None) -> list[str]:
    """必要な操作の抽出。制御語彙で決定的に行う。AI候補は語彙照合を通ったものだけ採用する。"""
    ops = _match_vocab(statement)
    for contract in contracts:
        for op in _contract_operations(contract):
            if op not in ops:
                ops.append(op)
    if ai_candidates:
        for candidate in ai_candidates:
            name = str(candidate or "").strip()
            if name in OPERATION_INDEX and name not in ops:
                ops.append(name)
    if not ops:
        return ["unknown"]
    if "unknown" in ops and len(ops) > 1:
        ops = [op for op in ops if op != "unknown"]
    return ops


# ----------------------------------------------------------------------------
# 5状態の検査
# ----------------------------------------------------------------------------

def _registry_of(manager):
    from app.capability_registry import CapabilityRegistry

    workspace = getattr(manager, "workspace", None)
    researcher = None
    for attr in ("researcher", "public_researcher", "web_researcher",
                 "public_web_researcher"):
        researcher = getattr(manager, attr, None)
        if researcher is not None:
            break
    table_executor = None
    for attr in ("table_executor", "tabular_executor", "table_operator"):
        table_executor = getattr(manager, attr, None)
        if table_executor is not None:
            break
    return CapabilityRegistry(workspace=workspace, researcher=researcher,
                              table_executor=table_executor)


def _context_files_of(manager, project_id: str) -> list:
    try:
        rows = manager.memory.list_context_files(str(project_id))
        return list(rows or [])
    except Exception:
        return []


def _inspect_operation(operation_id: str, *, registry, context_files: list,
                       web_allowed: bool, has_csv_output: bool,
                       has_source_refs: bool) -> dict:
    """5状態を個別に検査する。implemented/configured/input_compatible/validated/permitted。"""
    reasons: dict[str, str] = {}
    entry = OPERATION_INDEX.get(operation_id)
    executor = (entry or {}).get("executor")

    if operation_id == "unknown":
        return {
            "operation_id": "unknown",
            "executor_capability": "",
            "implemented": False,
            "configured": False,
            "input_compatible": False,
            "validated": False,
            "permitted": False,
            "reasons": {"unknown": "語彙外・曖昧のため人間確認が必要"},
        }
    if operation_id == "domain_pipeline":
        # 領域固有パイプラインはコード所有の実行器として実装済み扱いだが、
        # 実行検証の根拠が無ければ validated を真にしない。
        return {
            "operation_id": operation_id,
            "executor_capability": "",
            "implemented": True,
            "configured": True,
            "input_compatible": True,
            "validated": False,
            "permitted": True,
            "reasons": {"validated": "実行検証の根拠が無いため未検証",
                        "note": "領域固有パイプラインの実行証拠は別途検査する"},
        }

    implemented = False
    if executor:
        try:
            from app.capability_registry import DEFINITIONS

            implemented = str(executor) in DEFINITIONS
            reasons["implemented"] = ("能力定義あり" if implemented
                                      else "能力定義なし (未実装とは断定しない)")
        except Exception:
            implemented = False
            reasons["implemented"] = "能力定義を確認できない"
    else:
        reasons["implemented"] = "登録された実行器が無い"

    configured = False
    if executor and implemented:
        try:
            checked = registry.check([executor], context_files, web_allowed=bool(web_allowed))
            state = str((checked[0] or {}).get("state") or "") if checked else ""
            configured = state not in ("unavailable", "unknown", "")
            reasons["configured"] = f"registry state={state or 'empty'}"
        except Exception as exc:
            configured = False
            reasons["configured"] = f"設定確認に失敗: {type(exc).__name__}"
    else:
        reasons["configured"] = "実行器が無いため設定確認の対象外"

    if operation_id == "document_generation":
        input_compatible = True
        reasons["input_compatible"] = "文書生成は計画内容から自足できる"
    elif operation_id == "table_processing":
        # 表出力の宣言は成果であり入力ではない。入力適合は入力資料の有無で見る。
        input_compatible = bool(has_source_refs or context_files)
        reasons["input_compatible"] = ("表加工の入力資料あり" if input_compatible
                                       else "表加工の入力資料・参照宣言が無い")
    elif operation_id in ("source_extraction", "source_ocr"):
        input_compatible = bool(context_files)
        reasons["input_compatible"] = ("原本の登録あり" if input_compatible
                                       else "原本の登録が無い")
    else:
        input_compatible = True
        reasons["input_compatible"] = "この操作に入力適合の追加条件は無い"

    validated = False
    if executor and implemented and configured:
        if str(executor).startswith("source.ocr") or "ocr" in str(executor):
            try:
                from app.capability_registry import ocr_runtime_validated

                validated = bool(ocr_runtime_validated())
            except Exception:
                validated = False
            reasons["validated"] = ("実行検証済み" if validated
                                    else "実行検証の根拠が無いため未検証")
        elif operation_id in ("document_generation", "table_processing",
                              "public_research", "source_extraction"):
            # 定義確認済みのローカル実行器は、別途の実行検証を要求しない。
            # OCR 系のように実行検証を要求する能力だけを未検証にする。
            validated = True
            reasons["validated"] = "定義確認済みのローカル実行器 (実行検証の別要件なし)"
        else:
            reasons["validated"] = "実行検証の根拠が無いため未検証"
    else:
        reasons["validated"] = "実行器が無いため検証の対象外"

    if operation_id in ("external_publish", "external_send"):
        permitted = False
        reasons["permitted"] = "外部作用は人間の承認が必要 (事前照合では許可しない)"
    elif operation_id in ("system_build", "system_run", "system_verify"):
        permitted = False
        reasons["permitted"] = "実体系への作用は人間の承認が必要 (事前照合では許可しない)"
    elif operation_id == "public_research" and not web_allowed:
        permitted = False
        reasons["permitted"] = "公開調査の許可宣言が工程契約に無い"
    else:
        permitted = True
        reasons["permitted"] = "この操作に承認境界の不足は無い"

    return {
        "operation_id": operation_id,
        "executor_capability": str(executor or ""),
        "implemented": bool(implemented),
        "configured": bool(configured),
        "input_compatible": bool(input_compatible),
        "validated": bool(validated),
        "permitted": bool(permitted),
        "reasons": reasons,
    }


def _classify_gap(*, has_owner: bool, policy_hit: bool, inspection: dict) -> str | None:
    """不足原因の分類。policy_block・入力不足等から開発依頼を生成しない。不明は不明のまま。"""
    if policy_hit:
        return "policy_block"
    if not has_owner:
        return "missing_binding"
    if inspection.get("operation_id") == "unknown":
        return "unknown"
    if not inspection.get("input_compatible"):
        return "missing_input"
    if not (inspection.get("implemented") and inspection.get("configured")):
        return "missing_capability"
    if not inspection.get("validated"):
        return "missing_evidence"
    if not inspection.get("permitted"):
        return "policy_block"
    return None


# ----------------------------------------------------------------------------
# 事前照合 (読み取り専用)
# ----------------------------------------------------------------------------

def _active_contract(manager, project_id: str) -> dict:
    try:
        from app.goal_contract import preview as preview_contract

        return preview_contract(manager, str(project_id)) or {}
    except Exception:
        return {}


def _plan_snapshot(manager, project_id: str):
    from app.goal_review import plan_snapshot

    return plan_snapshot(manager, str(project_id))


def capability_preflight(manager, project_id: str, plan_signature: str | None = None,
                         ai_assist=None) -> dict:
    """達成条件×能力の事前照合。読み取り専用。

    承認・実行・外部送信・RAG登録・計画適用をしない。DB・ファイルを変更しない。
    ai_assist は任意の補助 callable(statement, hints) -> 操作ID候補の列。
    語彙表との決定的な照合を通ったものだけ採用する。
    """
    pid = str(project_id or "")
    contract = _active_contract(manager, pid)
    snapshot, current_signature = _plan_snapshot(manager, pid)
    signature = str(plan_signature or current_signature or "")
    tasks = [t for t in (snapshot or {}).get("tasks") or [] if isinstance(t, dict)]
    criteria = [c for c in (contract or {}).get("criteria") or [] if isinstance(c, dict)]
    if not criteria:
        try:
            from app.structured_planning import extract_criteria

            mission = manager.memory.get_mission(pid) or {}
            statements = extract_criteria(str(mission.get("goal") or ""),
                                          str(mission.get("success_criteria") or ""))
            criteria = [{"criterion_id": f"SC{i:02d}", "statement": text,
                         "exec_task_keys": [f"SC{i:02d}"], "verify_task_keys": []}
                        for i, text in enumerate(statements, 1)]
        except Exception:
            criteria = []
    context_files = _context_files_of(manager, pid)
    registry = _registry_of(manager)
    try:
        mission = manager.memory.get_mission(pid) or {}
    except Exception:
        mission = {}

    assist_used = False
    rows: list[dict] = []
    gaps: list[dict] = []
    for item in criteria:
        cid = str(item.get("criterion_id") or "")
        if not cid:
            continue
        statement = str(item.get("statement") or "")
        owners: list[dict] = []
        wanted_keys = {str(k) for k in (item.get("exec_task_keys") or []) if str(k)}
        for task in tasks:
            tkey = str(task.get("task_key") or "")
            contract_body = _contract_of(task)
            if not contract_body or contract_body.get("final_verification"):
                continue
            owned = set(str(x) for x in (contract_body.get("criterion_ids") or []))
            if cid in owned or (tkey and tkey in wanted_keys):
                owners.append(task)
        owner_contracts = [_contract_of(t) for t in owners]
        owner_keys = [str(t.get("task_key") or "") for t in owners if str(t.get("task_key") or "")]
        web_allowed = any(isinstance(c.get("public_web_research"), dict)
                          and c.get("public_web_research", {}).get("required")
                          for c in owner_contracts)
        has_csv = any(str((o or {}).get("path") or "").lower().endswith((".csv", ".xlsx"))
                      for c in owner_contracts for o in (c.get("outputs") or [])
                      if isinstance(o, dict))
        has_refs = any((c.get("source_refs") or []) for c in owner_contracts)

        ai_candidates: list[str] = []
        if ai_assist is not None:
            try:
                raw = ai_assist(statement, {"criterion_id": cid, "task_keys": owner_keys})
                ai_candidates = [str(x) for x in (raw or []) if str(x)]
                assist_used = True
            except Exception:
                ai_candidates = []
        # 必要な操作は達成条件の文面 (+AI補助の語彙照合) から決める。
        # 工程契約は「提供できる操作」として別に読み、文書生成を他操作の代用にしない。
        required_ops = _match_vocab(statement)
        if ai_candidates:
            for candidate in ai_candidates:
                name = str(candidate or "").strip()
                if name in OPERATION_INDEX and name not in required_ops:
                    required_ops.append(name)
        if not required_ops:
            required_ops = ["unknown"]
        provided_ops: list[str] = []
        for contract in owner_contracts:
            for op in _contract_operations(contract):
                if op not in provided_ops:
                    provided_ops.append(op)
        operations = required_ops
        policy_hit = _has_policy_signal(statement)
        op_rows: list[dict] = []
        gap_code: str | None = None
        for op in operations:
            inspection = _inspect_operation(op, registry=registry,
                                            context_files=context_files,
                                            web_allowed=web_allowed,
                                            has_csv_output=has_csv,
                                            has_source_refs=has_refs)
            # 文書しか作れない計画は、文書以外の操作を要する条件に不足を返す。
            # 提供できる操作に要求が無ければ、工程の対応付け自体が不足。
            provided = op in provided_ops if provided_ops else True
            if op == "unknown":
                code = _classify_gap(has_owner=bool(owners), policy_hit=policy_hit,
                                     inspection=inspection)
            elif not provided:
                code = ("policy_block" if policy_hit else "missing_capability")
                inspection = dict(inspection)
                reasons = dict(inspection.get("reasons") or {})
                reasons["provided"] = ("要求される操作を提供できる工程が無い "
                                       "(文書生成は代用にしない)")
                inspection["reasons"] = reasons
            else:
                code = _classify_gap(has_owner=bool(owners), policy_hit=policy_hit,
                                     inspection=inspection)
            entry = OPERATION_INDEX.get(op) or {}
            op_rows.append({
                "operation_id": op,
                "label_ja": str(entry.get("label_ja") or ("不明な操作" if op == "unknown" else op)),
                "required_capability_id": str(entry.get("hypothetical") or ""),
                "executor_capability": str(inspection.get("executor_capability") or ""),
                "implemented": bool(inspection.get("implemented")),
                "configured": bool(inspection.get("configured")),
                "input_compatible": bool(inspection.get("input_compatible")),
                "validated": bool(inspection.get("validated")),
                "permitted": bool(inspection.get("permitted")),
                "reasons": dict(inspection.get("reasons") or {}),
                "gap_code": code,
            })
            if code is not None and gap_code is None:
                gap_code = code
        row = {
            "criterion_id": cid,
            "statement_ref": _safe_short(statement),
            "task_keys": owner_keys,
            "operations": op_rows,
            "gap_code": gap_code,
        }
        rows.append(row)
        for op_row in op_rows:
            if op_row.get("gap_code") is None:
                continue
            op_entry = OPERATION_INDEX.get(op_row["operation_id"]) or {}
            gaps.append({
                "criterion_id": cid,
                "task_keys": owner_keys,
                "operation_id": op_row["operation_id"],
                "gap_code": str(op_row["gap_code"]),
                "required_capability_id": str(op_entry.get("hypothetical") or ""),
                "executor_capability": str(op_row.get("executor_capability") or ""),
                "reasons": dict(op_row.get("reasons") or {}),
                "evidence_refs": {
                    "plan_signature": signature,
                    "criterion_id": cid,
                    "operation_id": op_row["operation_id"],
                    "inspection": _short_hash(op_row),
                    "quote": _safe_short(statement),
                },
            })
    missing = [g for g in gaps if g.get("gap_code") == "missing_capability"]
    _ = mission
    return {
        "schema": SCHEMA_PREFLIGHT,
        "project_id": pid,
        "plan_signature": signature,
        "criteria": rows,
        "gaps": gaps,
        "missing_capability": missing,
        "has_missing_capability": bool(missing),
        "gap_codes": sorted({str(g.get("gap_code") or "") for g in gaps if g.get("gap_code")}),
        "ai_assist_used": bool(assist_used),
        "external_sends": 0,
        "external_calls": 0,
        "read_only": True,
    }


def preflight_to_classifier_inputs(preflight: dict) -> dict:
    """停止分類器への入力口 (追加のみ)。missing_capability が残れば development 相当で渡す。

    next_action は既存の IMPLEMENTED_ACTIONS の名前 (resolve_development) か
    none になる (stop_classifier 側の対応表に従うため、ここでは名前を作らない)。
    """
    preflight = preflight if isinstance(preflight, dict) else {}
    gaps = [g for g in (preflight.get("gaps") or []) if isinstance(g, dict)]
    missing = [g for g in gaps if g.get("gap_code") == "missing_capability"]
    signature = str(preflight.get("plan_signature") or "")
    base: dict = {
        "plan_signature": signature,
        "capability_preflight": {
            "missing_capability": bool(missing),
            "gap_codes": sorted({str(g.get("gap_code") or "") for g in gaps
                                 if g.get("gap_code")}),
            "criterion_ids": sorted({str(g.get("criterion_id") or "") for g in missing
                                     if g.get("criterion_id")}),
        },
    }
    if missing:
        base["disposition"] = "development"
        base["development_required"] = True
    return base


def blocks_execution_approval(manager, project_id: str,
                              plan_signature: str | None = None,
                              preflight: dict | None = None) -> dict:
    """missing_capability が残る計画は実行承認不可とするゲートの入口 (読み取り専用)。

    保存済みの FunctionProposal (当該計画署名・未解決の missing_capability) が
    残っていれば承認不可。既存のゲートを弱めない (不可にしかしない)。
    """
    pid = str(project_id or "")
    try:
        if preflight is None:
            _, current_signature = _plan_snapshot(manager, pid)
        else:
            current_signature = str(preflight.get("plan_signature") or "")
    except Exception:
        return {"blocked": False, "reason": "", "proposal_ids": [],
                "plan_signature": str(plan_signature or ""), "read_only": True}
    signature = str(plan_signature or current_signature or "")
    try:
        rows = list_proposals(manager, pid, plan_signature=signature)
    except Exception:
        return {"blocked": False, "reason": "", "proposal_ids": [],
                "plan_signature": signature, "read_only": True}
    open_missing = [r for r in rows
                    if r.get("gap_code") == "missing_capability"
                    and str(r.get("status") or "") in OPEN_STATUSES]
    if not open_missing:
        return {"blocked": False, "reason": "", "proposal_ids": [],
                "plan_signature": signature, "read_only": True}
    ids = sorted({str(r.get("proposal_id") or "") for r in open_missing if r.get("proposal_id")})
    return {
        "blocked": True,
        "reason": (f"不足機能の提案が{len(ids)}件残っているため実行承認できません。"
                   "不足機能の提案を確認してください"),
        "proposal_ids": ids,
        "plan_signature": signature,
        "read_only": True,
    }


# ----------------------------------------------------------------------------
# 永続化 (PJごとのサイドカーSQLite。棚卸しレジストリに登録する)
# ----------------------------------------------------------------------------

def _resolve_mem_path(manager_or_path) -> Path:
    if hasattr(manager_or_path, "memory"):
        return Path(manager_or_path.memory.path)
    return Path(manager_or_path)


def gap_db_path(manager_or_path) -> Path:
    mem = _resolve_mem_path(manager_or_path)
    return mem.with_name(mem.name + ".gap.sqlite3")


_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(db_path: Path) -> threading.Lock:
    key = str(db_path)
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _locks[key] = lock
        return lock


def _ensure_tables(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS function_proposals(
        proposal_id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        plan_signature TEXT NOT NULL,
        criterion_id TEXT NOT NULL,
        criterion_ids TEXT NOT NULL DEFAULT '[]',
        task_keys TEXT NOT NULL DEFAULT '[]',
        gap_code TEXT NOT NULL,
        required_capability_id TEXT NOT NULL,
        required_operation TEXT NOT NULL DEFAULT '',
        observed_failure TEXT NOT NULL DEFAULT '{}',
        existing_alternatives TEXT NOT NULL DEFAULT '[]',
        interface_contract TEXT NOT NULL DEFAULT '{}',
        permission_class TEXT NOT NULL DEFAULT 'local_reversible',
        acceptance_tests TEXT NOT NULL DEFAULT '[]',
        status TEXT NOT NULL DEFAULT 'draft',
        content_hash TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL)""")
    db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_gap_proposals_idem
        ON function_proposals(project_id, plan_signature, criterion_id,
                              required_capability_id, gap_code)""")
    db.execute("""CREATE INDEX IF NOT EXISTS idx_gap_proposals_project
        ON function_proposals(project_id, plan_signature)""")
    db.execute("""CREATE TABLE IF NOT EXISTS proposal_events(
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        proposal_id TEXT NOT NULL,
        actor TEXT NOT NULL DEFAULT '',
        from_status TEXT NOT NULL DEFAULT '',
        to_status TEXT NOT NULL DEFAULT '',
        reason TEXT NOT NULL DEFAULT '',
        content_hash TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL)""")
    db.execute("""CREATE INDEX IF NOT EXISTS idx_gap_events_proposal
        ON proposal_events(proposal_id, created_at)""")


@contextmanager
def _connect(manager_or_path):
    path = gap_db_path(manager_or_path)
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
    path = gap_db_path(manager_or_path)
    if not path.exists():
        return None
    db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=15)
    db.row_factory = sqlite3.Row
    return db


def _decode_proposal(row: sqlite3.Row) -> dict:
    item = dict(row)
    for key in ("criterion_ids", "task_keys", "existing_alternatives", "acceptance_tests"):
        try:
            item[key] = json.loads(item.get(key) or "[]")
        except (ValueError, TypeError):
            item[key] = []
    for key in ("observed_failure", "interface_contract"):
        try:
            item[key] = json.loads(item.get(key) or "{}")
        except (ValueError, TypeError):
            item[key] = {}
    item["schema"] = SCHEMA_PROPOSAL
    return item


def _content_hash(body: dict) -> str:
    material = {
        "project_id": body.get("project_id"),
        "plan_signature": body.get("plan_signature"),
        "criterion_ids": body.get("criterion_ids"),
        "task_keys": body.get("task_keys"),
        "gap_code": body.get("gap_code"),
        "required_capability_id": body.get("required_capability_id"),
        "required_operation": body.get("required_operation"),
        "observed_failure": body.get("observed_failure"),
        "existing_alternatives": body.get("existing_alternatives"),
        "interface_contract": body.get("interface_contract"),
        "permission_class": body.get("permission_class"),
        "acceptance_tests": body.get("acceptance_tests"),
    }
    return _short_hash(material) + _short_hash(str(body.get("proposal_id") or ""))


def _build_proposal_body(*, project_id: str, plan_signature: str, gap: dict) -> dict:
    op_id = str(gap.get("operation_id") or "unknown")
    entry = OPERATION_INDEX.get(op_id) or {}
    cap_id = str(gap.get("required_capability_id") or entry.get("hypothetical") or "")
    cid = str(gap.get("criterion_id") or "")
    task_keys = [str(x) for x in (gap.get("task_keys") or []) if str(x)]
    proposal_id = _stable_id(project_id, plan_signature, cid, cap_id,
                             str(gap.get("gap_code") or ""))
    executor = str(gap.get("executor_capability") or entry.get("executor") or "")
    if executor == "workspace.write_text":
        failure_code = "execution_contract_document_only"
    elif not executor:
        failure_code = "no_registered_executor"
    else:
        failure_code = "executor_not_configured"
    if failure_code not in FAILURE_CODES:
        failure_code = "no_registered_executor"
    label = str(entry.get("label_ja") or "不明な操作")
    observed = {
        "code": failure_code,
        "evidence_refs": dict(gap.get("evidence_refs") or {}),
    }
    alternatives = []
    if executor and executor != "workspace.write_text":
        alternatives.append({
            "capability_id": executor,
            "why_insufficient": "設定・検証・承認のいずれかが不足し実行できない",
        })
    alternatives.append({
        "capability_id": "workspace.write_text",
        "why_insufficient": "文書のみを生成し、要求される操作の実行と検証の証拠は出せない",
    })
    permission = str(entry.get("permission") or "local_reversible")
    if permission not in ("local_reversible", "local_irreversible", "external_side_effect"):
        permission = "local_reversible"
    body = {
        "schema": SCHEMA_PROPOSAL,
        "proposal_id": proposal_id,
        "project_id": str(project_id),
        "plan_signature": str(plan_signature),
        "criterion_ids": [cid] if cid else [],
        "task_keys": task_keys,
        "gap_code": "missing_capability",
        "required_capability_id": cap_id,
        "required_operation": _safe_short(f"{label}の実行と検証 (criterion {cid})"),
        "observed_failure": {
            "code": observed["code"],
            "evidence_refs": {k: _safe_short(v) for k, v in
                              (observed.get("evidence_refs") or {}).items()},
        },
        "existing_alternatives": [
            {"capability_id": _safe_short(a.get("capability_id"), 80),
             "why_insufficient": _safe_short(a.get("why_insufficient"))}
            for a in alternatives
        ],
        "interface_contract": {
            "inputs": ["criterion_statement", "plan_contract"],
            "outputs": ["validated_artifact"],
            "failure_states": ["validation_failed", "permission_denied",
                               "evidence_missing"],
        },
        "permission_class": permission,
        "acceptance_tests": [f"accept:{op_id}:executes",
                             f"accept:{op_id}:evidence",
                             f"accept:{op_id}:no_bypass"],
        "status": "draft",
        "created_at": _utcnow(),
    }
    body["content_hash"] = _content_hash(body)
    return body


def _audit_event(manager_or_path, project_id: str, proposal_id: str, *,
                 actor: str, from_status: str, to_status: str, reason: str,
                 content_hash: str) -> None:
    try:
        with _connect(manager_or_path) as db:
            with db:
                db.execute(
                    """INSERT INTO proposal_events(id, project_id, proposal_id, actor,
                           from_status, to_status, reason, content_hash, created_at)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (_stable_id(proposal_id, str(time.time_ns()))[:32],
                     str(project_id), str(proposal_id), str(actor or "")[:100],
                     str(from_status or "")[:32], str(to_status or "")[:32],
                     _safe_short(reason, 500), str(content_hash or "")[:64],
                     _utcnow()))
    except sqlite3.Error:
        pass
    manager = manager_or_path if hasattr(manager_or_path, "memory") else None
    if manager is not None:
        try:
            manager.memory.add_event(
                str(project_id), "capability_gap_proposal",
                _safe_short(f"機能提案 {to_status or from_status}: {proposal_id}", 200),
                detail=json.dumps({
                    "proposal_id": proposal_id,
                    "from": from_status,
                    "to": to_status,
                    "actor": str(actor or "")[:100],
                    "reason": _safe_short(reason, 200),
                }, ensure_ascii=False))
        except Exception:
            pass


def generate_proposals(manager, project_id: str, preflight: dict | None = None,
                       actor: str = "") -> dict:
    """missing_capability だけ FunctionProposal 化する (明示的な生成関数)。

    missing_input / missing_evidence / missing_binding / policy_block /
    unknown からは生成しない。不明は不明のまま。同一
    project_id + plan_signature + criterion_id + capability_id + gap_code は
    同一提案へ集約する (DBの一意制約で物理的に防ぐ)。
    """
    pid = str(project_id or "")
    if preflight is None:
        preflight = capability_preflight(manager, pid)
    signature = str(preflight.get("plan_signature") or "")
    mark_stale(manager, pid, signature, actor=actor or "auto")
    gaps = [g for g in (preflight.get("gaps") or [])
            if isinstance(g, dict) and g.get("gap_code") == "missing_capability"]
    created = 0
    with _lock_for(gap_db_path(manager)):
        with _connect(manager) as db:
            db.execute("BEGIN IMMEDIATE")
            for gap in gaps:
                body = _build_proposal_body(project_id=pid, plan_signature=signature,
                                            gap=gap)
                try:
                    db.execute(
                        """INSERT OR IGNORE INTO function_proposals(
                               proposal_id, project_id, plan_signature, criterion_id,
                               criterion_ids, task_keys, gap_code, required_capability_id,
                               required_operation, observed_failure, existing_alternatives,
                               interface_contract, permission_class, acceptance_tests,
                               status, content_hash, created_at, updated_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (body["proposal_id"], pid, signature,
                         str(gap.get("criterion_id") or ""),
                         json.dumps(body["criterion_ids"], ensure_ascii=False),
                         json.dumps(body["task_keys"], ensure_ascii=False),
                         "missing_capability", body["required_capability_id"],
                         body["required_operation"],
                         json.dumps(body["observed_failure"], ensure_ascii=False),
                         json.dumps(body["existing_alternatives"], ensure_ascii=False),
                         json.dumps(body["interface_contract"], ensure_ascii=False),
                         body["permission_class"],
                         json.dumps(body["acceptance_tests"], ensure_ascii=False),
                         "draft", body["content_hash"], body["created_at"],
                         body["created_at"]))
                    if db.total_changes and getattr(db, "total_changes", 0) is not None:
                        pass
                except sqlite3.IntegrityError:
                    continue
            db.commit()
    # 作成分だけ監査する (既存の冪等ヒットは監査を増やさない)。
    current = list_proposals(manager, pid, plan_signature=signature)
    for row in current:
        if row.get("gap_code") != "missing_capability":
            continue
        events = list_proposal_events(manager, row.get("proposal_id") or "")
        if not events:
            created += 1
            _audit_event(manager, pid, str(row.get("proposal_id") or ""),
                         actor=actor or "auto", from_status="", to_status="draft",
                         reason="事前照合の不足から機能提案を生成",
                         content_hash=str(row.get("content_hash") or ""))
    return {
        "project_id": pid,
        "plan_signature": signature,
        "proposals": [r for r in current if r.get("gap_code") == "missing_capability"],
        "created": created,
        "skipped_gap_codes": sorted({str(g.get("gap_code") or "")
                                     for g in (preflight.get("gaps") or [])
                                     if isinstance(g, dict)
                                     and g.get("gap_code") != "missing_capability"}),
    }


def get_proposal(manager_or_path, proposal_id: str) -> dict | None:
    db = _read_connection(manager_or_path)
    if db is None:
        return None
    try:
        with db:
            try:
                row = db.execute("SELECT * FROM function_proposals WHERE proposal_id=?",
                                 (str(proposal_id),)).fetchone()
            except sqlite3.OperationalError:
                return None
    finally:
        db.close()
    return _decode_proposal(row) if row is not None else None


def list_proposals(manager_or_path, project_id: str,
                   plan_signature: str | None = None,
                   status: str | None = None,
                   gap_code: str | None = None) -> list[dict]:
    pid = str(project_id or "")
    db = _read_connection(manager_or_path)
    if db is None:
        return []
    try:
        with db:
            try:
                query = "SELECT * FROM function_proposals WHERE project_id=?"
                args: list = [pid]
                if plan_signature is not None:
                    query += " AND plan_signature=?"
                    args.append(str(plan_signature))
                if status is not None:
                    query += " AND status=?"
                    args.append(str(status))
                if gap_code is not None:
                    query += " AND gap_code=?"
                    args.append(str(gap_code))
                query += " ORDER BY created_at, proposal_id"
                rows = db.execute(query, args).fetchall()
            except sqlite3.OperationalError:
                return []
    finally:
        db.close()
    return [_decode_proposal(r) for r in rows]


def list_proposal_events(manager_or_path, proposal_id: str) -> list[dict]:
    db = _read_connection(manager_or_path)
    if db is None:
        return []
    try:
        with db:
            try:
                rows = db.execute(
                    "SELECT * FROM proposal_events WHERE proposal_id=? ORDER BY created_at",
                    (str(proposal_id),)).fetchall()
            except sqlite3.OperationalError:
                return []
    finally:
        db.close()
    return [dict(r) for r in rows]


def mark_stale(manager_or_path, project_id: str, current_signature: str,
               actor: str = "auto") -> int:
    """計画署名が変われば古い提案は stale (削除しない)。再実行で増殖しない。"""
    pid = str(project_id or "")
    sig = str(current_signature or "")
    if not sig:
        return 0
    with _lock_for(gap_db_path(manager_or_path)):
        with _connect(manager_or_path) as db:
            with db:
                cur = db.execute(
                    """UPDATE function_proposals SET status='stale', updated_at=?
                        WHERE project_id=? AND plan_signature<>?
                        AND status IN ('draft','reviewed','export_ready','submitted',
                                       'delivered','needs_evidence')""",
                    (_utcnow(), pid, sig))
                changed = cur.rowcount or 0
    if changed:
        _audit_event(manager_or_path, pid, f"stale:{sig}", actor=actor,
                     from_status="", to_status="stale",
                     reason=f"計画署名の変更により旧提案{changed}件をstale化",
                     content_hash="")
    return int(changed)


def set_proposal_status(manager, project_id: str, proposal_id: str,
                        new_status: str, actor: str = "", reason: str = "") -> dict:
    """状態遷移。正しい順序のみ可。不正な遷移は拒否する。

    verified への遷移は未実装として拒否する (2-D の再判定でしか付けられない)。
    """
    pid = str(project_id or "")
    target = str(new_status or "")
    if target == "verified":
        raise ValueError("verified への遷移は未実装です。2-D の再判定でのみ付与します")
    if target not in STATUSES:
        raise ValueError(f"未知の状態です: {target}")
    actor_name = str(actor or "").strip()
    if not actor_name:
        raise ValueError("actor is required")
    with _lock_for(gap_db_path(manager)):
        with _connect(manager) as db:
            with db:
                row = db.execute("SELECT * FROM function_proposals WHERE proposal_id=?",
                                 (str(proposal_id),)).fetchone()
                if row is None:
                    raise ValueError("提案が見つかりません")
                current = _decode_proposal(row)
                if str(current.get("project_id") or "") != pid:
                    raise ValueError("別案件の提案は操作できません")
                cur_status = str(current.get("status") or "")
                if target == cur_status:
                    return current
                allowed = ALLOWED_TRANSITIONS.get(cur_status, frozenset())
                if target not in allowed:
                    raise ValueError(f"不正な遷移です: {cur_status} -> {target}")
                db.execute("UPDATE function_proposals SET status=?, updated_at=? "
                           "WHERE proposal_id=?",
                           (target, _utcnow(), str(proposal_id)))
    _audit_event(manager, pid, str(proposal_id), actor=actor_name,
                 from_status=cur_status, to_status=target,
                 reason=reason or "状態遷移",
                 content_hash=str(current.get("content_hash") or ""))
    updated = get_proposal(manager, str(proposal_id))
    if updated is None:
        raise ValueError("提案の再読込に失敗しました")
    return updated


def reject_proposal(manager, project_id: str, proposal_id: str,
                    actor: str = "", reason: str = "") -> dict:
    """人が提案を却下する。却下だけでは元の不足を解消しない (事前照合は不足を返し続ける)。"""
    if not str(reason or "").strip():
        raise ValueError("却下の根拠が必要です")
    return set_proposal_status(manager, project_id, proposal_id, "rejected",
                               actor=actor, reason=reason)


def correct_proposal(manager, project_id: str, proposal_id: str, patch: dict,
                     actor: str = "", reason: str = "") -> dict:
    """人が提案を訂正する。内容ハッシュを更新し監査に残す。状態は変えない。"""
    pid = str(project_id or "")
    if not isinstance(patch, dict) or not patch:
        raise ValueError("訂正内容が必要です")
    allowed_keys = {"required_operation", "interface_contract",
                    "acceptance_tests", "permission_class",
                    "existing_alternatives"}
    unknown_keys = set(patch) - allowed_keys
    if unknown_keys:
        raise ValueError(f"訂正できない項目です: {sorted(unknown_keys)}")
    permission = patch.get("permission_class")
    if permission is not None and permission not in ("local_reversible",
                                                     "local_irreversible",
                                                     "external_side_effect"):
        raise ValueError("permission_class が不正です")
    actor_name = str(actor or "").strip()
    if not actor_name:
        raise ValueError("actor is required")
    with _lock_for(gap_db_path(manager)):
        with _connect(manager) as db:
            with db:
                row = db.execute("SELECT * FROM function_proposals WHERE proposal_id=?",
                                 (str(proposal_id),)).fetchone()
                if row is None:
                    raise ValueError("提案が見つかりません")
                current = _decode_proposal(row)
                if str(current.get("project_id") or "") != pid:
                    raise ValueError("別案件の提案は操作できません")
                if str(current.get("status") or "") in ("stale", "verified"):
                    raise ValueError("stale/verified の提案は訂正できません")
                body = dict(current)
                for key, value in patch.items():
                    if key == "required_operation":
                        body[key] = _safe_short(value)
                    elif key == "existing_alternatives":
                        if not isinstance(value, list):
                            raise ValueError("existing_alternatives は配列です")
                        body[key] = [
                            {"capability_id": _safe_short(a.get("capability_id"), 80),
                             "why_insufficient": _safe_short(a.get("why_insufficient"))}
                            for a in value if isinstance(a, dict)
                        ]
                    elif key == "acceptance_tests":
                        if not isinstance(value, list) or not value:
                            raise ValueError("acceptance_tests は空でない配列です")
                        body[key] = [_safe_short(v, 80) for v in value]
                    elif key == "interface_contract":
                        if not isinstance(value, dict):
                            raise ValueError("interface_contract はオブジェクトです")
                        body[key] = {
                            "inputs": [_safe_short(v, 80) for v in
                                       (value.get("inputs") or [])][:8],
                            "outputs": [_safe_short(v, 80) for v in
                                        (value.get("outputs") or [])][:8],
                            "failure_states": [_safe_short(v, 80) for v in
                                                (value.get("failure_states") or [])][:8],
                        }
                    else:
                        body[key] = value
                body["content_hash"] = _content_hash(body)
                db.execute(
                    """UPDATE function_proposals SET required_operation=?,
                           existing_alternatives=?, interface_contract=?,
                           permission_class=?, acceptance_tests=?, content_hash=?,
                           updated_at=? WHERE proposal_id=?""",
                    (body["required_operation"],
                     json.dumps(body["existing_alternatives"], ensure_ascii=False),
                     json.dumps(body["interface_contract"], ensure_ascii=False),
                     body["permission_class"],
                     json.dumps(body["acceptance_tests"], ensure_ascii=False),
                     body["content_hash"], _utcnow(), str(proposal_id)))
    _audit_event(manager, pid, str(proposal_id), actor=actor_name,
                 from_status=str(current.get("status") or ""),
                 to_status=str(current.get("status") or ""),
                 reason=f"訂正: {reason}" if reason else "訂正",
                 content_hash=body["content_hash"])
    updated = get_proposal(manager, str(proposal_id))
    if updated is None:
        raise ValueError("提案の再読込に失敗しました")
    return updated


def purge_project_proposals(memory_path: str | Path, project_id: str) -> dict[str, int]:
    """完全削除時の後始末。当該PJの行だけ消し、他PJに影響しない。"""
    pid = str(project_id or "")
    path = gap_db_path(memory_path)
    if not path.exists():
        return {"function_proposals": 0, "proposal_events": 0}
    removed: dict[str, int] = {"function_proposals": 0, "proposal_events": 0}
    with _lock_for(path):
        db = sqlite3.connect(str(path), timeout=30)
        try:
            with db:
                try:
                    cur = db.execute("DELETE FROM function_proposals WHERE project_id=?",
                                     (pid,))
                    removed["function_proposals"] = int(cur.rowcount or 0)
                except sqlite3.OperationalError:
                    pass
                try:
                    cur = db.execute("DELETE FROM proposal_events WHERE project_id=?",
                                     (pid,))
                    removed["proposal_events"] = int(cur.rowcount or 0)
                except sqlite3.OperationalError:
                    pass
        finally:
            db.close()
    return removed
