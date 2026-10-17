"""Stage 4 (4-C): Domain Execution Pack — DomainSpec の宣言・登録簿・適合検査・選択の記録。

範囲は 4-C のみ (分野実行パックの型、登録簿と静的検証、適合検査、適用候補、
人の確認による選択の記録、API のための関数境界)。実行の振り分けは変えない。
選択は記録のみであり、ProjectOrchestrator._execute_task の経路、計画、工程、
DB の他の部分に影響しない。

読み取り専用の原則: list_specs()/get_spec()/validate_spec()/
conformance_report()/applicable_packs()/get_selection() は承認・実行・
外部送信・RAG登録・計画適用を一切行わない。DB・ファイルを変更するのは
select_pack()/clear_pack()/reset_for_generation()/purge_project_packs() の
明示的な記録関数だけであり、対象は PJごとのサイドカーSQLite
(*.pack.sqlite3) に限る。

失敗閉鎖 (fail-closed): エラー・不明・読取不能を「適合」「通過」「適用可」
扱いにしない。判定材料が読めない工程は unknown (理由つき) にし、適合に
数えない。全体 conforms=true は全工程 conforms のときだけ。

実案件の分離: 実在の案件に直接関係する実行経路 (専用パイプラインや外部公開
のID) をパックとして宣言しない・触らない・参照もしない。executor_ids に
使えるのは capability_registry.DEFINITIONS のID、または人の実行を表す
human:approved_action / human:review だけであり、実案件の実行経路を示す
ID は validate_spec() で拒否する。
"""
from __future__ import annotations

import copy
import logging
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from app.capability_gap import (
    OPERATION_INDEX,
    OPERATION_VOCABULARY,
    _contract_operations,
)
from app.capability_registry import DEFINITIONS

LOGGER = logging.getLogger(__name__)

SCHEMA = "sebas.domain-spec/v1"

#: 権限境界の固定キー。外部送信・公開・顧客接触・RAG登録・計画承認の個別許可。
#: 既定はすべて不許可であり、宣言に無いキーは不許可として扱う。
PERMISSION_KEYS = (
    "external_send",
    "external_publish",
    "customer_contact",
    "rag_register",
    "plan_approve",
)

#: 人の実行を表すID。LLMが任意コードや外部操作を指定できないように、
#: この2つ以外の human: 形式は受け付けない。
HUMAN_EXECUTORS = (
    "human:approved_action",
    "human:review",
)

#: 実案件の実行経路を示すIDの断片。汎用パックの executor_ids に現れたら拒否する。
#: (汎用枠組みであり、特定案件の実行経路を宣言しないための静的検査)
_REALCASE_EXECUTOR_TOKENS = (
    "vehicle",
    "sales",
    "campaign",
    "google",
)

_DOMAIN_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_VERSION_RE = re.compile(r"^v[0-9]+$")
_SPEC_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

#: 適用候補の選択状態。
APPLICABILITY_STATUSES = ("decided", "ambiguous", "none", "unknown")

#: 適合検査の工程状態。
TASK_VERDICTS = ("conforms", "violates", "unknown")

_EXTERNAL_TASK_OPERATIONS = frozenset({"external_publish", "external_send"})

_EXECUTOR_TO_OPERATION: dict[str, str] = {}
for _entry in OPERATION_VOCABULARY:
    _executor = _entry.get("executor")
    if _executor:
        _EXECUTOR_TO_OPERATION.setdefault(str(_executor), str(_entry["operation_id"]))


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _short(value: str, limit: int = 200) -> str:
    return " ".join(str(value or "").split())[: max(1, int(limit))]


# ----------------------------------------------------------------------------
# 登録簿 (コードの1か所)
# ----------------------------------------------------------------------------

def _permission_all_denied() -> dict:
    return {key: False for key in PERMISSION_KEYS}


def _base_recovery(max_retries: int) -> dict:
    return {
        "retryable_failures": ["一時的な読取失敗"],
        "human_required_failures": ["原本の不足", "内容の確定"],
        "max_retries": max_retries,
        "deadline": "人の確認があるまで停止する",
        "idempotency_key": "同一(案件,計画署名,工程)への再適用は冪等にする",
        "rollback": "生成物を隔離して変更前へ戻す",
    }


_DOCUMENT_KEYWORDS = ["文書", "報告書", "議事録", "提案書", "作成", "生成", "報告"]
_SOURCE_KEYWORDS = ["原本抽出", "抜粋", "引用", "原本確認", "原本読取"]

#: 分野の識別語彙 (他分野と重ならない固有語)。適合度の加点に使う共通語
#: (「作成」「検証」等) とは分け、分野固有の適合だけを識別加点する。
#: 共通語だけの計画は同点→ambiguous になり、勝手に選ばない。
DISTINCTIVE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "document_evidence": ("原本引用", "引用付き文書", "議事録", "提案書"),
    "table_processing": ("管理表", "CSV", "集計", "検算", "クロス集計"),
    "public_research": ("公開調査", "Web調査", "ウェブ調査", "公開情報"),
}

_SPECS: list[dict] = []


def _register(spec: dict) -> dict:
    """登録簿への1件登録。検証を通らない・重複する宣言は登録しない。"""
    checked = validate_spec(spec)
    if not checked.get("ok"):
        raise RuntimeError(
            "invalid domain pack: "
            + "; ".join(str(e) for e in (checked.get("errors") or [])))
    key = (str(spec.get("domain_id") or ""), str(spec.get("version") or ""))
    for existing in _SPECS:
        if (str(existing.get("domain_id") or ""),
                str(existing.get("version") or "")) == key:
            raise RuntimeError(f"duplicate domain pack: {key[0]} {key[1]}")
    _SPECS.append(copy.deepcopy(spec))
    return spec


def list_specs() -> list[dict]:
    """登録済みパックの一覧 (読取り専用。呼出側の変更から登録簿を守るため複写)。"""
    return copy.deepcopy(_SPECS)


def get_spec(domain_id: str, version: str) -> dict | None:
    """登録済みパックの取得。形式不正・未登録は None (例外にしない読取り)。"""
    did = str(domain_id or "")
    ver = str(version or "")
    if not _SPEC_ID_RE.fullmatch(did) or not _VERSION_RE.fullmatch(ver):
        return None
    for spec in _SPECS:
        if str(spec.get("domain_id") or "") == did and str(spec.get("version") or "") == ver:
            return copy.deepcopy(spec)
    return None


# ----------------------------------------------------------------------------
# 静的検証 validate_spec()
# ----------------------------------------------------------------------------

def _is_bool(value: object) -> bool:
    return isinstance(value, bool)


def validate_spec(spec: dict) -> dict:
    """宣言の静的検証。すべて拒否 (fail-closed)。例外を投げない。

    拒否するもの: 必須項目の欠落、未知の executor_id、不正な権限クラス、
    許可があるのに空の証拠契約、最大回数の欠落・負値、版の形式違反。
    重複登録は _register() が拒否する (ここでは純粋な1件検査のみ)。
    """
    errors: list[str] = []
    if not isinstance(spec, dict):
        return {"ok": False, "errors": ["specはオブジェクトであること"]}
    if spec.get("schema") != SCHEMA:
        errors.append(f"schema は {SCHEMA} であること")
    required_top = ("domain_id", "version", "label_ja", "applicability",
                    "input_contract", "plan_template", "executor_ids",
                    "permission_boundary", "evidence_contract", "recovery_policy")
    for field in required_top:
        if spec.get(field) in (None, "", [], {}):
            errors.append(f"必須項目の欠落: {field}")
    if not _DOMAIN_ID_RE.fullmatch(str(spec.get("domain_id") or "")):
        errors.append("domain_id の形式が不正 (先頭英小文字の英数_・2〜64文字)")
    if not _VERSION_RE.fullmatch(str(spec.get("version") or "")):
        errors.append("version の形式が不正 (v1, v2, ... の形)")
    label = spec.get("label_ja")
    if not isinstance(label, str) or not label.strip() or len(label) > 200:
        errors.append("label_ja は1〜200文字の日本語名であること")

    applicability = spec.get("applicability")
    if isinstance(applicability, dict):
        required_ops = applicability.get("required_operations")
        if not isinstance(required_ops, list) or not required_ops:
            errors.append("applicability.required_operations は空でない配列であること")
        else:
            for op in required_ops:
                if str(op) not in OPERATION_INDEX:
                    errors.append(f"applicability.required_operations に未知の操作: {op}")
        forbidden_ops = applicability.get("forbidden_operations")
        if not isinstance(forbidden_ops, list):
            errors.append("applicability.forbidden_operations は配列であること")
        else:
            for op in forbidden_ops:
                if str(op) not in OPERATION_INDEX:
                    errors.append(f"applicability.forbidden_operations に未知の操作: {op}")
        triggers = applicability.get("trigger_keywords")
        if not isinstance(triggers, dict) or not triggers:
            errors.append("applicability.trigger_keywords は空でない対応表であること")
        else:
            known_required = {str(o) for o in (required_ops or []) if isinstance(o, str)}
            for op, keywords in triggers.items():
                if str(op) not in known_required:
                    errors.append(f"applicability.trigger_keywords の操作は"
                                  f"required_operations に含めること: {op}")
                if (not isinstance(keywords, list) or not keywords
                        or any(not isinstance(k, str) or not k.strip() or len(k) > 80
                               for k in keywords)):
                    errors.append(f"applicability.trigger_keywords[{op}] は"
                                  f"空でない文字列配列であること")
        min_score = applicability.get("min_score")
        if not isinstance(min_score, int) or _is_bool(min_score) or min_score < 1:
            errors.append("applicability.min_score は1以上の整数であること")
        if (not isinstance(applicability.get("description"), str)
                or not str(applicability.get("description") or "").strip()):
            errors.append("applicability.description は空でない文字列であること")

    input_contract = spec.get("input_contract")
    if isinstance(input_contract, dict):
        if not isinstance(input_contract.get("required_original_kinds"), list):
            errors.append("input_contract.required_original_kinds は配列であること")
        unreadable = input_contract.get("unreadable_states")
        if (not isinstance(unreadable, list) or not unreadable
                or any(not isinstance(s, str) or not s.strip() for s in unreadable)):
            errors.append("input_contract.unreadable_states は空でない文字列配列であること"
                          "(欠測・0件・読取不能を別状態で扱う旨)")
        if (not isinstance(input_contract.get("rule"), str)
                or not str(input_contract.get("rule") or "").strip()):
            errors.append("input_contract.rule は空でない文字列であること")

    plan_template = spec.get("plan_template")
    if isinstance(plan_template, dict):
        roles = plan_template.get("required_roles")
        if (not isinstance(roles, list) or not roles
                or any(not isinstance(r, str) or not r.strip() for r in roles)):
            errors.append("plan_template.required_roles は空でない文字列配列であること")
        for field in ("dependency_rule", "criterion_mapping"):
            if (not isinstance(plan_template.get(field), str)
                    or not str(plan_template.get(field) or "").strip()):
                errors.append(f"plan_template.{field} は空でない文字列であること")
        stops = plan_template.get("stop_conditions")
        if (not isinstance(stops, list) or not stops
                or any(not isinstance(s, str) or not s.strip() for s in stops)):
            errors.append("plan_template.stop_conditions は空でない文字列配列であること")

    executor_ids = spec.get("executor_ids")
    if isinstance(executor_ids, list):
        if not executor_ids:
            errors.append("executor_ids は空でない配列であること")
        for executor in executor_ids:
            name = str(executor or "")
            if name in HUMAN_EXECUTORS:
                continue
            if name not in DEFINITIONS:
                errors.append(f"未知の executor_id: {executor or '(空)'}"
                              f"(登録簿に無い/human: 以外)")
                continue
            lowered = name.lower()
            if any(token in lowered for token in _REALCASE_EXECUTOR_TOKENS):
                errors.append(f"実案件の実行経路を示すIDは汎用パックに使えない: {name}")

    boundary = spec.get("permission_boundary")
    if isinstance(boundary, dict):
        for key in boundary:
            if key not in PERMISSION_KEYS:
                errors.append(f"不正な権限クラス: {key}")
        for key in PERMISSION_KEYS:
            if key not in boundary:
                errors.append(f"permission_boundary に個別許可が無い: {key}"
                              f"(既定不許可を明示すること)")
            elif not _is_bool(boundary.get(key)):
                errors.append(f"permission_boundary[{key}] は真偽値であること")

    evidence = spec.get("evidence_contract")
    if isinstance(evidence, dict):
        allows = [k for k in PERMISSION_KEYS
                  if isinstance(boundary, dict) and boundary.get(k) is True]
        if allows and not evidence:
            errors.append("permission_boundary に許可があるのに "
                          "evidence_contract が空であること")
        required_outputs = evidence.get("required_outputs")
        if (not isinstance(required_outputs, list) or not required_outputs
                or any(not isinstance(s, str) or not s.startswith(".")
                       for s in required_outputs)):
            errors.append("evidence_contract.required_outputs は"
                          "'.md' のような接尾辞の空でない配列であること")
        for field in ("require_source_refs", "require_web_declaration"):
            if field in evidence and not _is_bool(evidence.get(field)):
                errors.append(f"evidence_contract.{field} は真偽値であること")
        for field in ("operation_audit", "independent_check"):
            if (not isinstance(evidence.get(field), str)
                    or not str(evidence.get(field) or "").strip()):
                errors.append(f"evidence_contract.{field} は空でない文字列であること")

    recovery = spec.get("recovery_policy")
    if isinstance(recovery, dict):
        if not isinstance(recovery.get("retryable_failures"), list):
            errors.append("recovery_policy.retryable_failures は配列であること")
        human_failures = recovery.get("human_required_failures")
        if (not isinstance(human_failures, list) or not human_failures
                or any(not isinstance(s, str) or not s.strip() for s in human_failures)):
            errors.append("recovery_policy.human_required_failures は"
                          "空でない文字列配列であること")
        max_retries = recovery.get("max_retries")
        if not isinstance(max_retries, int) or _is_bool(max_retries) or max_retries < 0:
            errors.append("recovery_policy.max_retries は0以上の整数であること"
                          "(欠落・負値を拒否)")
        for field in ("deadline", "idempotency_key", "rollback"):
            if (not isinstance(recovery.get(field), str)
                    or not str(recovery.get(field) or "").strip()):
                errors.append(f"recovery_policy.{field} は空でない文字列であること")

    return {"ok": not errors, "errors": errors}


# ----------------------------------------------------------------------------
# 汎用パックの宣言 (3件のみ。実案件と無関係)
# ----------------------------------------------------------------------------

_register({
    "schema": SCHEMA,
    "domain_id": "document_evidence",
    "version": "v1",
    "label_ja": "原本引用つき文書生成",
    "applicability": {
        "required_operations": ["document_generation", "source_extraction"],
        "trigger_keywords": {
            "document_generation": list(_DOCUMENT_KEYWORDS),
            "source_extraction": list(_SOURCE_KEYWORDS),
        },
        "forbidden_operations": ["external_publish", "external_send",
                                 "system_build", "system_run",
                                 "system_verify", "domain_pipeline"],
        "min_score": 1,
        "description": "原本の引用を伴う文書の作成・検証だけを対象にする。"
                       "表の加工・公開調査・外部作用を含む計画には適合しない。",
    },
    "input_contract": {
        "required_original_kinds": ["登録済み原本(抽出本文あり)"],
        "unreadable_states": ["欠測", "0件", "読取不能"],
        "rule": "必須原本の欠測・0件・読取不能を別状態で扱い、"
                "いずれも適合・達成に数えない。",
    },
    "plan_template": {
        "required_roles": ["preparation", "execution", "final_verification"],
        "dependency_rule": "依存は先行工程だけを指定する",
        "stop_conditions": ["原本不足", "承認待ち", "検証不合格"],
        "criterion_mapping": "各達成条件に実行工程を対応付け、"
                             "最終検証で条件別に判定する",
    },
    "executor_ids": ["source.read_excerpt", "workspace.write_text"],
    "permission_boundary": _permission_all_denied(),
    "evidence_contract": {
        "required_outputs": [".md"],
        "require_source_refs": False,
        "require_web_declaration": False,
        "operation_audit": "workspace_files の操作監査記録を参照する",
        "independent_check": "適合検査は宣言の有無だけを見る。成果物の実在・"
                             "形式は実行時の検証器が確認する。",
    },
    "recovery_policy": _base_recovery(2),
})

_register({
    "schema": SCHEMA,
    "domain_id": "table_processing",
    "version": "v1",
    "label_ja": "登録表データの確認と宣言型加工",
    "applicability": {
        "required_operations": ["table_processing", "document_generation"],
        "trigger_keywords": {
            "table_processing": ["管理表", "CSV", "集計", "加工", "検算",
                                 "テーブル", "クロス集計"],
            "document_generation": list(_DOCUMENT_KEYWORDS),
        },
        "forbidden_operations": ["external_publish", "external_send",
                                 "system_build", "system_run",
                                 "system_verify", "domain_pipeline"],
        "min_score": 1,
        "description": "登録済み表データの構造確認と宣言型の集計・加工、"
                       "結果文書の作成を対象にする。公開調査・外部作用を"
                       "含む計画には適合しない。",
    },
    "input_contract": {
        "required_original_kinds": ["登録済み表データ(CSV/表)"],
        "unreadable_states": ["欠測", "0件", "読取不能"],
        "rule": "入力表の欠測・0件・読取不能を別状態で扱い、0件を成功に"
                "読み替えない。いずれも適合・達成に数えない。",
    },
    "plan_template": {
        "required_roles": ["preparation", "execution", "final_verification"],
        "dependency_rule": "依存は先行工程だけを指定する",
        "stop_conditions": ["入力表の不足", "承認待ち", "検証不合格"],
        "criterion_mapping": "各達成条件に実行工程を対応付け、"
                             "最終検証で条件別に判定する",
    },
    "executor_ids": ["table.profile", "table.transform", "workspace.write_text"],
    "permission_boundary": _permission_all_denied(),
    "evidence_contract": {
        "required_outputs": [".md", ".csv"],
        "require_source_refs": False,
        "require_web_declaration": False,
        "operation_audit": "table_operations と workspace_files の"
                           "操作監査記録を参照する",
        "independent_check": "適合検査は宣言の有無だけを見る。成果物の実在・"
                             "形式・行列数は実行時の検証器が確認する。",
    },
    "recovery_policy": _base_recovery(2),
})

_register({
    "schema": SCHEMA,
    "domain_id": "public_research",
    "version": "v1",
    "label_ja": "許可された公開調査",
    "applicability": {
        "required_operations": ["public_research", "document_generation"],
        "trigger_keywords": {
            "public_research": ["公開調査", "Web調査", "ウェブ調査",
                                "情報収集", "公開情報"],
            "document_generation": list(_DOCUMENT_KEYWORDS),
        },
        "forbidden_operations": ["external_publish", "external_send",
                                 "system_build", "system_run",
                                 "system_verify", "domain_pipeline"],
        "min_score": 1,
        "description": "許可宣言のある公開情報の取得と、取得原本に基づく"
                       "報告書の作成を対象にする。許可宣言の無い取得・"
                       "外部作用を含む計画には適合しない。",
    },
    "input_contract": {
        "required_original_kinds": ["取得済み公開Web原本(URL・取得日時つき)"],
        "unreadable_states": ["欠測", "0件", "読取不能"],
        "rule": "取得0件・取得失敗を別状態で扱い、成功に読み替えない。"
                "いずれも適合・達成に数えない。",
    },
    "plan_template": {
        "required_roles": ["preparation", "execution", "final_verification"],
        "dependency_rule": "依存は先行工程だけを指定する",
        "stop_conditions": ["取得原本の不足", "承認待ち", "検証不合格"],
        "criterion_mapping": "各達成条件に実行工程を対応付け、"
                             "最終検証で条件別に判定する",
    },
    "executor_ids": ["web.collect_public", "workspace.write_text"],
    "permission_boundary": _permission_all_denied(),
    "evidence_contract": {
        "required_outputs": [".md"],
        "require_source_refs": False,
        "require_web_declaration": True,
        "operation_audit": "公開取得の記録と workspace_files の"
                           "操作監査記録を参照する",
        "independent_check": "適合検査は宣言の有無だけを見る。取得の成否・"
                             "成果物の実在は実行時の検証器が確認する。",
    },
    "recovery_policy": _base_recovery(2),
})


# ----------------------------------------------------------------------------
# 計画の読取り (判定は再実装しない。対応付けの材料として読むだけ)
# ----------------------------------------------------------------------------

def _safe_mission(manager, project_id: str):
    try:
        mission = manager.memory.get_mission(str(project_id))
    except Exception as exc:
        return None, f"mission読取失敗:{type(exc).__name__}"
    if not isinstance(mission, dict):
        return None, "mission形式不正"
    return mission, ""


def _safe_snapshot(manager, project_id: str):
    try:
        from app.goal_review import plan_snapshot

        snapshot, signature = plan_snapshot(manager, str(project_id))
    except Exception as exc:
        return None, "", f"plan_snapshot失敗:{type(exc).__name__}"
    if not isinstance(snapshot, dict):
        return None, "", "plan_snapshot形式不正"
    return snapshot, str(signature or ""), ""


def _safe_contract_preview(manager, project_id: str):
    try:
        from app.goal_contract import preview as preview_contract

        contract = preview_contract(manager, str(project_id))
    except Exception as exc:
        return None, f"contract読取失敗:{type(exc).__name__}"
    if not isinstance(contract, dict):
        return None, "contract形式不正"
    return contract, ""


def _task_contract(task: dict):
    try:
        from app.structured_planning import contract_of

        return contract_of(task)
    except Exception as exc:
        # F3a: 失敗をログに残す。Noneは「判定不能」(呼び出し側がunknown側に倒す)。
        # 契約なし(contract_ofがNone)を読取不能と混同しない。
        LOGGER.warning("domain_spec _task_contract failed: %s", type(exc).__name__)
        return None


def _is_evaluable(task: dict, contract: dict | None) -> bool:
    """適合検査の対象工程か。準備・最終検証は汎用判定であり対象外。

    契約が読めない (None) 工程は対象として残し、_check_task() の
    unknown 分岐で判定不能にする (適合に数えない。スキップして
    conforms にしない)。
    """
    if not isinstance(contract, dict):
        return True
    if contract.get("final_verification"):
        return False
    if str(task.get("task_key") or "") == "final_verification":
        return False
    if str(task.get("task_key") or "") == "SC00":
        return False
    if not (contract.get("criterion_ids") or []):
        # criterion_ids を持たない正規契約の準備工程は対象外。
        return False
    return True


def _pack_covered_operations(spec: dict) -> set[str]:
    """パックの executor_ids が担える操作カテゴリ。"""
    covered: set[str] = set()
    for executor in (spec.get("executor_ids") or []):
        operation = _EXECUTOR_TO_OPERATION.get(str(executor) or "")
        if operation:
            covered.add(operation)
    return covered


def _task_external_operations(contract: dict) -> list[str]:
    """工程契約が要求する外部作用の操作。読めないものは含めない。"""
    found: list[str] = []
    try:
        operations = _contract_operations(contract) or []
    except Exception as exc:
        # F3a: 失敗をログに残し伝播する(空リストで「外部作用なし」にしない)。
        # _check_taskは広いexceptでunknownに倒すため、ここで握りつぶさない。
        LOGGER.warning("domain_spec _task_external_operations failed: %s", type(exc).__name__)
        raise
    for operation in operations:
        if operation in _EXTERNAL_TASK_OPERATIONS and operation not in found:
            found.append(operation)
    return found


def _check_task(task: dict, contract: dict | None, spec: dict,
                covered: set[str]) -> dict:
    """1工程の適合検査。conforms/violates/unknown のいずれか (理由つき)。"""
    task_key = str(task.get("task_key") or "")
    if not isinstance(contract, dict):
        return {"task_key": task_key, "verdict": "unknown",
                "reasons": ["工程契約が読めないため判定不能(適合に数えない)"]}
    evidence = spec.get("evidence_contract") or {}
    boundary = spec.get("permission_boundary") or {}

    try:
        required = _contract_operations(contract) or []
    except Exception as exc:
        return {"task_key": task_key, "verdict": "unknown",
                "reasons": [f"工程契約の対応付けに失敗:{type(exc).__name__}"
                            f"(適合に数えない)"]}
    required = [str(op) for op in required if str(op)]

    external = [op for op in required if op in _EXTERNAL_TASK_OPERATIONS]
    if external:
        denied = [op for op in external if not boundary.get(
            "external_publish" if op == "external_publish" else "external_send",
            False)]
        if denied:
            return {"task_key": task_key, "verdict": "violates",
                    "reasons": [f"権限境界で許可されていない外部作用:"
                                f"{','.join(sorted(denied))}"]}

    missing_executors = [op for op in required if op not in covered]
    # unknown 操作 (語彙外) は適合に数えない。
    unknown_ops = [op for op in missing_executors if op not in OPERATION_INDEX]
    if unknown_ops:
        return {"task_key": task_key, "verdict": "unknown",
                "reasons": [f"未知の操作カテゴリ:{','.join(sorted(unknown_ops))}"
                            f"(適合に数えない)"]}
    if missing_executors:
        return {"task_key": task_key, "verdict": "violates",
                "reasons": [f"パックの実行器に無い能力を要する:"
                            f"{','.join(sorted(missing_executors))}"]}

    outputs = contract.get("outputs") or []
    declared = [str(o.get("path") or "").lower() for o in outputs
                if isinstance(o, dict) and o.get("path")]
    missing_outputs = [suffix for suffix in (evidence.get("required_outputs") or [])
                       if not any(path.endswith(str(suffix).lower()) for path in declared)]
    if missing_outputs:
        return {"task_key": task_key, "verdict": "violates",
                "reasons": [f"証拠契約を満たす出力の宣言が無い:"
                            f"{','.join(missing_outputs)}"]}
    if evidence.get("require_source_refs") and not (contract.get("source_refs") or []):
        return {"task_key": task_key, "verdict": "violates",
                "reasons": ["証拠契約が求める原本参照の宣言が無い"]}
    if evidence.get("require_web_declaration"):
        web = contract.get("public_web_research") or {}
        if not (isinstance(web, dict) and web.get("required")):
            return {"task_key": task_key, "verdict": "violates",
                    "reasons": ["証拠契約が求める公開取得の許可宣言が無い"]}

    return {"task_key": task_key, "verdict": "conforms",
            "reasons": [f"要求操作({','.join(sorted(required)) or 'なし'})を"
                        f"パックの実行器で担え、証拠契約の宣言がある"]}


# ----------------------------------------------------------------------------
# 適合検査 conformance_report() (読み取り専用)
# ----------------------------------------------------------------------------

def conformance_report(manager, project_id: str,
                       domain_id: str, version: str) -> dict:
    """PJの現行計画がパックの宣言に適合しているか (読み取り専用)。

    承認・実行・外部送信・RAG登録・計画適用をしない。DB・ファイルを変更しない。
    工程ごとに conforms/violates/unknown (理由つき) を返し、全工程が
    conforms のときだけ全体 conforms=true。判定材料が読めない工程は
    unknown にし、適合に数えない。
    """
    pid = str(project_id or "")
    spec = get_spec(domain_id, version)
    if spec is None:
        raise ValueError("unknown pack")
    covered = _pack_covered_operations(spec)

    mission, mission_error = _safe_mission(manager, pid)
    if mission is None:
        return {"schema": SCHEMA, "project_id": pid,
                "domain_id": str(spec.get("domain_id") or ""),
                "version": str(spec.get("version") or ""),
                "conforms": False, "verdict": "unknown",
                "tasks": [], "counts": {"conforms": 0, "violates": 0, "unknown": 0},
                "skipped_task_keys": [], "reason": mission_error,
                "read_only": True, "external_sends": 0}
    snapshot, _signature, snapshot_error = _safe_snapshot(manager, pid)
    if snapshot is None:
        return {"schema": SCHEMA, "project_id": pid,
                "domain_id": str(spec.get("domain_id") or ""),
                "version": str(spec.get("version") or ""),
                "conforms": False, "verdict": "unknown",
                "tasks": [], "counts": {"conforms": 0, "violates": 0, "unknown": 0},
                "skipped_task_keys": [], "reason": snapshot_error,
                "read_only": True, "external_sends": 0}

    rows: list[dict] = []
    skipped: list[str] = []
    for task in (snapshot.get("tasks") or []):
        if not isinstance(task, dict):
            rows.append({"task_key": "", "verdict": "unknown",
                         "reasons": ["工程の形式が不正のため判定不能(適合に数えない)"]})
            continue
        contract = _task_contract(task)
        if not _is_evaluable(task, contract):
            # 契約が読めない (None) 工程は _check_task() の unknown 分岐へ。
            if contract is not None:
                skipped.append(str(task.get("task_key") or ""))
                continue
        rows.append(_check_task(task, contract, spec, covered))

    counts = {"conforms": 0, "violates": 0, "unknown": 0}
    for row in rows:
        verdict = str(row.get("verdict") or "")
        if verdict in counts:
            counts[verdict] += 1
        else:
            # 想定外の値は unknown に倒す (通過扱いにしない)。
            row["verdict"] = "unknown"
            counts["unknown"] += 1
    if not rows:
        verdict = "unknown"
        conforms = False
        reason = "評価できる工程が無いため適合と判定しない"
    elif counts["unknown"]:
        verdict = "unknown"
        conforms = False
        reason = "判定不能の工程があるため適合と判定しない"
    elif counts["violates"]:
        verdict = "violates"
        conforms = False
        reason = "宣言に適合しない工程がある"
    else:
        verdict = "conforms"
        conforms = True
        reason = "全工程がパックの宣言に適合している"
    return {"schema": SCHEMA, "project_id": pid,
            "domain_id": str(spec.get("domain_id") or ""),
            "version": str(spec.get("version") or ""),
            "conforms": bool(conforms), "verdict": verdict,
            "tasks": rows, "counts": counts,
            "skipped_task_keys": sorted(skipped), "reason": reason,
            "read_only": True, "external_sends": 0}


# ----------------------------------------------------------------------------
# 適用候補 applicable_packs() (読み取り専用・決定的)
# ----------------------------------------------------------------------------

def _score_statements(spec: dict, statements: list[str]) -> int:
    triggers = (spec.get("applicability") or {}).get("trigger_keywords") or {}
    keywords: list[str] = []
    for values in triggers.values():
        if isinstance(values, list):
            keywords.extend(str(k) for k in values if str(k))
    hits = 0
    for statement in statements:
        blob = str(statement or "")
        if any(keyword and keyword in blob for keyword in keywords):
            hits += 1
    return hits


def _score_distinctive(spec: dict, statements: list[str]) -> int:
    """分野固有語の加点 (2点/件)。共通語だけの計画は同点のままにする。"""
    domain_id = str(spec.get("domain_id") or "")
    keywords = DISTINCTIVE_KEYWORDS.get(domain_id, ())
    hits = 0
    for statement in statements:
        blob = str(statement or "")
        if any(keyword and keyword in blob for keyword in keywords):
            hits += 2
    return hits


def _score_tasks(spec: dict, task_operations: list[list[str]]) -> int:
    required = set(str(o) for o in
                   ((spec.get("applicability") or {}).get("required_operations") or []))
    covered = _pack_covered_operations(spec)
    hits = 0
    for operations in task_operations:
        needed = {str(o) for o in (operations or []) if str(o)}
        if needed and needed <= covered and needed & required:
            hits += 1
    return hits


def applicable_packs(manager, project_id: str) -> dict:
    """目標・達成条件・工程契約から適合度つき候補パックを返す (読み取り専用)。

    承認・実行・外部送信・RAG登録・計画適用をしない。決定的 (順序は
    スコア降順・domain_id昇順)。複数の候補が同点、または閾値未満、
    読み取り不能のときは候補を選ばず ambiguous/none/unknown を返す。
    """
    pid = str(project_id or "")
    contract, contract_error = _safe_contract_preview(manager, pid)
    if contract is None:
        return {"project_id": pid, "status": "unknown", "selected": None,
                "candidates": [], "threshold": 1,
                "reason": contract_error, "read_only": True, "external_sends": 0}
    snapshot, _signature, snapshot_error = _safe_snapshot(manager, pid)
    if snapshot is None:
        return {"project_id": pid, "status": "unknown", "selected": None,
                "candidates": [], "threshold": 1,
                "reason": snapshot_error, "read_only": True, "external_sends": 0}

    statements = [str(c.get("statement") or "") for c in (contract.get("criteria") or [])
                  if isinstance(c, dict) and str(c.get("statement") or "")]
    if not statements:
        return {"project_id": pid, "status": "none", "selected": None,
                "candidates": [], "threshold": 1,
                "reason": "達成条件が無いため候補を選ばない",
                "read_only": True, "external_sends": 0}

    task_operations: list[list[str]] = []
    for task in (snapshot.get("tasks") or []):
        if not isinstance(task, dict):
            continue
        contract_body = _task_contract(task)
        if not isinstance(contract_body, dict) or not _is_evaluable(task, contract_body):
            continue
        try:
            operations = [str(o) for o in (_contract_operations(contract_body) or [])]
        except Exception:
            # 対応付け不能の工程は unknown 扱いに相当し、加点しない。
            operations = []
        task_operations.append(operations)

    threshold = 1
    scored: list[dict] = []
    for spec in _SPECS:
        score = (_score_statements(spec, statements)
                 + _score_tasks(spec, task_operations)
                 + _score_distinctive(spec, statements))
        scored.append({"domain_id": str(spec.get("domain_id") or ""),
                       "version": str(spec.get("version") or ""),
                       "label_ja": str(spec.get("label_ja") or ""),
                       "score": int(score),
                       "statement_hits": int(_score_statements(spec, statements)),
                       "task_hits": int(_score_tasks(spec, task_operations))})
    scored.sort(key=lambda item: (-int(item["score"]), str(item["domain_id"]),
                                  str(item["version"])))
    top = int(scored[0]["score"]) if scored else 0
    if top < threshold:
        return {"project_id": pid, "status": "none", "selected": None,
                "candidates": scored, "threshold": threshold,
                "reason": "閾値以上の候補が無いため選ばない",
                "read_only": True, "external_sends": 0}
    tied = [item for item in scored if int(item["score"]) == top]
    if len(tied) > 1:
        return {"project_id": pid, "status": "ambiguous", "selected": None,
                "candidates": scored, "threshold": threshold,
                "reason": f"候補が同点({top}点)のため勝手に選ばない",
                "read_only": True, "external_sends": 0}
    selected = dict(tied[0])
    return {"project_id": pid, "status": "decided", "selected": selected,
            "candidates": scored, "threshold": threshold,
            "reason": f"適合度{top}点で1件に決まる",
            "read_only": True, "external_sends": 0}


# ----------------------------------------------------------------------------
# 選択の記録 (人の確認)。記録のみで、実行の振り分けは変えない。
# ----------------------------------------------------------------------------

def _resolve_mem_path(manager_or_path) -> Path:
    if hasattr(manager_or_path, "memory"):
        return Path(manager_or_path.memory.path)
    return Path(manager_or_path)


def pack_db_path(manager_or_path) -> Path:
    mem = _resolve_mem_path(manager_or_path)
    return mem.with_name(mem.name + ".pack.sqlite3")


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
    db.execute("""CREATE TABLE IF NOT EXISTS domain_pack_selection(
        project_id TEXT PRIMARY KEY,
        domain_id TEXT NOT NULL,
        version TEXT NOT NULL,
        actor TEXT NOT NULL DEFAULT '',
        reason TEXT NOT NULL DEFAULT '',
        conforms INTEGER NOT NULL DEFAULT 0,
        warnings TEXT NOT NULL DEFAULT '[]',
        selected_at TEXT NOT NULL,
        updated_at TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS domain_pack_events(
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL,
        domain_id TEXT NOT NULL DEFAULT '',
        version TEXT NOT NULL DEFAULT '',
        actor TEXT NOT NULL DEFAULT '',
        kind TEXT NOT NULL DEFAULT '',
        reason TEXT NOT NULL DEFAULT '',
        conforms INTEGER NOT NULL DEFAULT 0,
        warnings TEXT NOT NULL DEFAULT '[]',
        created_at TEXT NOT NULL)""")
    db.execute("""CREATE INDEX IF NOT EXISTS idx_pack_events_project
        ON domain_pack_events(project_id, created_at)""")


@contextmanager
def _connect(manager_or_path):
    path = pack_db_path(manager_or_path)
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
    path = pack_db_path(manager_or_path)
    if not path.exists():
        return None
    db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=15)
    db.row_factory = sqlite3.Row
    return db


def _check_secret(*values: object) -> str:
    """秘密混入の疑いがあれば種別ラベル、なければ空文字。値は返さない。"""
    try:
        from app.plan_review_loop import contains_secret
    except Exception as exc:
        # F3a: import失敗を握りつぶして空文字(検査済み扱い)にしない。呼び出し側は
        # select_pack/clear_packで記録を拒否する(秘密検査不能=安全側)。
        LOGGER.warning("domain_spec _check_secret import failed: %s", type(exc).__name__)
        return "検査不能"
    for value in values:
        try:
            label = contains_secret(str(value or ""))
        except Exception as exc:
            # 検査自体が失敗したら安全側に倒す (記録しない)。
            LOGGER.warning("domain_spec _check_secret check failed: %s", type(exc).__name__)
            return "検査不能"
        if label:
            return str(label)
    return ""


def _audit_event(manager_or_path, project_id: str, domain_id: str, version: str, *,
                 actor: str, kind: str, reason: str, conforms: bool,
                 warnings: list, db: sqlite3.Connection | None = None) -> None:
    """選択の変更と同一トランザクションで監査を記録する。失敗は呼出側へ返す。"""
    import json as _json

    values = (f"evt_{project_id}_{time.time_ns()}",
              str(project_id), str(domain_id), str(version),
              str(actor or "")[:100], str(kind or "")[:32],
              _short(reason, 500), 1 if conforms else 0,
              _json.dumps(list(warnings or []), ensure_ascii=False)[:4000],
              _utcnow())
    statement = """INSERT INTO domain_pack_events(id, project_id, domain_id, version,
                   actor, kind, reason, conforms, warnings, created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)"""
    if db is not None:
        db.execute(statement, values)
    else:
        with _connect(manager_or_path) as connection:
            with connection:
                connection.execute(statement, values)


def get_selection(manager_or_path, project_id: str) -> dict | None:
    """現在の選択の読取り (読取り専用。DB・ファイルを作らない)。"""
    import json as _json

    pid = str(project_id or "")
    db = _read_connection(manager_or_path)
    if db is None:
        return None
    try:
        with db:
            try:
                row = db.execute(
                    "SELECT * FROM domain_pack_selection WHERE project_id=?",
                    (pid,)).fetchone()
            except sqlite3.OperationalError:
                return None
    finally:
        db.close()
    if row is None:
        return None
    item = dict(row)
    item["conforms"] = bool(item.get("conforms"))
    try:
        item["warnings"] = _json.loads(item.get("warnings") or "[]")
    except (ValueError, TypeError):
        item["warnings"] = ["警告の形式不正(内容は信用しない)"]
    return item


def list_selection_events(manager_or_path, project_id: str) -> list[dict]:
    """選択の監査履歴 (追記専用。読取りは副作用なし)。"""
    import json as _json

    pid = str(project_id or "")
    db = _read_connection(manager_or_path)
    if db is None:
        return []
    try:
        with db:
            try:
                rows = db.execute(
                    "SELECT * FROM domain_pack_events WHERE project_id=? "
                    "ORDER BY created_at", (pid,)).fetchall()
            except sqlite3.OperationalError:
                return []
    finally:
        db.close()
    out = []
    for row in rows:
        item = dict(row)
        item["conforms"] = bool(item.get("conforms"))
        try:
            item["warnings"] = _json.loads(item.get("warnings") or "[]")
        except (ValueError, TypeError):
            item["warnings"] = []
        out.append(item)
    return out


def select_pack(manager, project_id: str, domain_id: str, version: str,
                actor: str = "", reason: str = "") -> dict:
    """パックの採用を記録する (人の確認)。記録のみで実行の振り分けは変えない。

    適合検査が violates/unknown のパックも拒否はしない (人が判断する) が、
    警告 (理由) を返し、「適合していない」と明記して記録する。
    """
    import json as _json

    pid = str(project_id or "")
    actor_name = str(actor or "").strip()
    reason_text = str(reason or "").strip()
    if not actor_name:
        raise ValueError("actor is required")
    if not reason_text:
        raise ValueError("reason is required")
    secret_label = _check_secret(actor_name, reason_text)
    if secret_label:
        raise ValueError(f"秘密混入の疑いのため記録しません:{secret_label}")
    spec = get_spec(domain_id, version)
    if spec is None:
        raise ValueError("unknown pack")
    report = conformance_report(manager, pid, str(spec.get("domain_id") or ""),
                                str(spec.get("version") or ""))
    warnings: list[str] = []
    for row in (report.get("tasks") or []):
        if str(row.get("verdict") or "") != "conforms":
            warnings.append(f"{row.get('task_key') or '(工程不明)'}:"
                            f"{str(row.get('verdict') or '')}:"
                            f"{';'.join(str(r) for r in (row.get('reasons') or []))}")
    if str(report.get("verdict") or "") == "unknown" and not warnings:
        warnings.append(str(report.get("reason") or "判定不能"))
    if not (report.get("tasks") or []) and not warnings:
        warnings.append(str(report.get("reason") or "評価できる工程が無い"))
    conforms = bool(report.get("conforms"))
    now = _utcnow()
    with _lock_for(pack_db_path(manager)):
        with _connect(manager) as db:
            with db:
                db.execute(
                    """INSERT INTO domain_pack_selection(project_id, domain_id, version,
                           actor, reason, conforms, warnings, selected_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(project_id) DO UPDATE SET domain_id=excluded.domain_id,
                       version=excluded.version, actor=excluded.actor,
                       reason=excluded.reason, conforms=excluded.conforms,
                       warnings=excluded.warnings, updated_at=excluded.updated_at""",
                    (pid, str(spec.get("domain_id") or ""),
                     str(spec.get("version") or ""), actor_name,
                     _short(reason_text, 2000), 1 if conforms else 0,
                     _json.dumps(warnings, ensure_ascii=False)[:4000], now, now))
                _audit_event(manager, pid, str(spec.get("domain_id") or ""),
                             str(spec.get("version") or ""), actor=actor_name,
                             kind="selected", reason=reason_text, conforms=conforms,
                             warnings=warnings, db=db)
    note = ("適合しているパックを記録しました(実行の振り分けは変えません)"
            if conforms else
            "適合していないパックを記録しました(実行の振り分けは変えません。"
            "人が判断してください)")
    return {"ok": True, "project_id": pid,
            "domain_id": str(spec.get("domain_id") or ""),
            "version": str(spec.get("version") or ""),
            "conforms": conforms, "warnings": warnings,
            "conformance": report, "note": note,
            # 確認者名は本人確認を保証しない (認証は未実装)。
            "auth_note": "確認者名は本人確認を保証しません(認証は未実装)。",
            "read_only_execution": True, "external_sends": 0}


def clear_pack(manager, project_id: str,
               actor: str = "", reason: str = "") -> dict:
    """選択の解除を記録する (人の確認)。実行の振り分けは変えない。"""
    pid = str(project_id or "")
    actor_name = str(actor or "").strip()
    reason_text = str(reason or "").strip()
    if not actor_name:
        raise ValueError("actor is required")
    if not reason_text:
        raise ValueError("reason is required")
    secret_label = _check_secret(actor_name, reason_text)
    if secret_label:
        raise ValueError(f"秘密混入の疑いのため記録しません:{secret_label}")
    current = get_selection(manager, pid)
    if current is None:
        return {"ok": True, "project_id": pid, "cleared": False,
                "note": "選択が無いため何も解除しない",
                "auth_note": "確認者名は本人確認を保証しません(認証は未実装)。"}
    with _lock_for(pack_db_path(manager)):
        with _connect(manager) as db:
            with db:
                db.execute("DELETE FROM domain_pack_selection WHERE project_id=?",
                           (pid,))
                _audit_event(manager, pid, str(current.get("domain_id") or ""),
                             str(current.get("version") or ""), actor=actor_name,
                             kind="cleared", reason=reason_text,
                             conforms=bool(current.get("conforms")),
                             warnings=list(current.get("warnings") or []), db=db)
    return {"ok": True, "project_id": pid, "cleared": True,
            "domain_id": str(current.get("domain_id") or ""),
            "version": str(current.get("version") or ""),
            "note": "選択を解除しました(実行の振り分けは変えません)",
            "auth_note": "確認者名は本人確認を保証しません(認証は未実装)。",
            "external_sends": 0}


def reset_for_generation(memory_path: str | Path, project_id: str) -> dict:
    """PJ初期化 (世代切替) で選択をリセットする。行は消し、監査は残す。"""
    pid = str(project_id or "")
    path = pack_db_path(memory_path)
    if not path.exists():
        return {"project_id": pid, "reset": False}
    current = get_selection(memory_path, pid)
    removed = 0
    with _lock_for(path):
        db = sqlite3.connect(str(path), timeout=30)
        try:
            with db:
                try:
                    cur = db.execute(
                        "DELETE FROM domain_pack_selection WHERE project_id=?",
                        (pid,))
                    removed = int(cur.rowcount or 0)
                except sqlite3.OperationalError:
                    removed = 0
                if current is not None or removed:
                    _audit_event(memory_path, pid, str((current or {}).get("domain_id") or ""),
                                 str((current or {}).get("version") or ""),
                                 actor="auto-guard", kind="generation_reset",
                                 reason="PJ初期化のため選択をリセットしました",
                                 conforms=False, warnings=[], db=db)
        finally:
            db.close()
    return {"project_id": pid, "reset": bool(current is not None or removed)}


def purge_project_packs(memory_path: str | Path, project_id: str) -> dict[str, int]:
    """完全削除時の後始末。当該PJの行だけ消し、他PJに影響しない。"""
    pid = str(project_id or "")
    path = pack_db_path(memory_path)
    if not path.exists():
        return {"domain_pack_selection": 0, "domain_pack_events": 0}
    removed: dict[str, int] = {"domain_pack_selection": 0, "domain_pack_events": 0}
    with _lock_for(path):
        db = sqlite3.connect(str(path), timeout=30)
        try:
            with db:
                for table in ("domain_pack_selection", "domain_pack_events"):
                    try:
                        cur = db.execute(f"DELETE FROM {table} WHERE project_id=?",
                                         (pid,))
                        removed[table] = int(cur.rowcount or 0)
                    except sqlite3.OperationalError:
                        pass
        finally:
            db.close()
    return removed
