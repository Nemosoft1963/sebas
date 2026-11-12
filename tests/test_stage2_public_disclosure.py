"""段階2の受入試験。疑似PJ・実関数・外部通信なし。特定案件の語彙なし。"""
import json

from app.goal_review import public_structure
from app.structured_planning import compile_plan, compile_task, contract_of


def _plan():
    task = compile_task(1, "架空文書を作成する", {
        "title": "架空文書", "scope": "架空文書の内容を整理する。",
        "headings": ["目的", "実施内容"]}, ["src-1"])
    return compile_plan(["架空文書を作成する"], [task])


def _expected_counts(snapshot):
    contracts = [contract_of(t) or {} for t in snapshot["tasks"]]
    prep_n = sum(len((c.get("exit_checks") or [])) for c in contracts
                 if c.get("role") == "preparation")
    total_n = sum(len((c.get("exit_checks") or [])) for c in contracts)
    total_n += sum(len([r for r in (c.get("requirement_matrix") or [])
                        if isinstance(r, dict) and r.get("method") in (
                            "count", "artifact_count", "section_char_count",
                            "approval_stage_matrix", "headings", "phrase",
                            "llm_semantic")]) for c in contracts)
    return prep_n, total_n


def _all_keys(obj):
    keys = set()
    if isinstance(obj, dict):
        for key, value in obj.items():
            keys.add(key)
            keys |= _all_keys(value)
    elif isinstance(obj, list):
        for value in obj:
            keys |= _all_keys(value)
    return keys


def test_split_counts_match_internal_contract():
    snapshot = {"tasks": _plan()["tasks"]}
    prep_n, total_n = _expected_counts(snapshot)
    packet = public_structure(snapshot)
    disclosure = packet["preparation_gate_disclosure"]
    assert disclosure["preparation_check_count"] == prep_n
    assert disclosure["all_task_check_count"] == total_n
    assert disclosure["preparation_check_count"] != disclosure["all_task_check_count"]
    assert disclosure["preparation_check_count_scope"]
    assert disclosure["all_task_check_count_scope"]
    assert "check_count" not in disclosure
    assert "check_count" not in _all_keys(packet)


def test_missing_contract_values_are_unknown_not_completed():
    tasks = [{
        "id": "t0", "task_key": "SC00", "title": "x", "description": "y",
        "acceptance_criteria": json.dumps({
            "schema": "local-cowork-plan/v1", "criterion_ids": [],
            "role": "preparation",
            "internal_preparation_gate_ids": ["PREP00"]}, ensure_ascii=False),
        "depends_on": [],
    }]
    disclosure = public_structure({"tasks": tasks})["preparation_gate_disclosure"]
    assert disclosure["preparation_failure_action"] == "unknown"
    assert disclosure["final_aggregation_rule"] == "unknown"
    assert disclosure["disclosure_incomplete"] is True
    assert disclosure["incomplete_reasons"]
    blob = json.dumps(disclosure, ensure_ascii=False)
    assert "確認できない" in blob or "ありません" in blob


def test_present_values_are_used_as_is():
    snapshot = {"tasks": _plan()["tasks"]}
    disclosure = public_structure(snapshot)["preparation_gate_disclosure"]
    assert disclosure["disclosure_incomplete"] is False
    assert disclosure["preparation_failure_action"] == "reopen_producing_task_on_item_fail"
    assert disclosure["final_aggregation_rule"] == "all_required_items_must_pass_for_task_pass"


def test_non_public_counts_without_contents():
    snapshot = {"tasks": _plan()["tasks"]}
    packet = public_structure(snapshot)
    disclosure = packet["preparation_gate_disclosure"]
    assert disclosure["non_public_check_count"] >= 0
    if disclosure["non_public_check_count"] > 0:
        assert disclosure["unevaluable_scope"]
    blob = json.dumps(packet, ensure_ascii=False)
    assert "src-1" not in blob
    assert "result/sc01.md" not in blob


def test_secrets_paths_and_body_are_rejected():
    import app.plan_review_loop as loop
    plan = _plan()
    snapshot = {"tasks": plan["tasks"]}
    packet = public_structure(snapshot)
    # 既存の公開前検査: 公開パケット全体に秘密混入の疑いがあれば種別を返す。
    assert loop.contains_secret("AKIAIOSFODNN7EXAMPLE") == "AWSキー"
    assert loop.contains_secret("山田太郎の連絡先です") == ""
    # 公開パケット自体に原本本文・個人情報・秘密・内部パス・内部ファイル名が入らない。
    blob = json.dumps(packet, ensure_ascii=False)
    assert "架空文書を作成する" not in blob
    assert "src-1" not in blob
    assert "result/sc01.md" not in blob
