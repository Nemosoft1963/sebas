"""P0-2: 原本読取の計画結線 (偽資料・偽登録のみ。外部AI・実通信なし)。"""
import json

import pytest

from app import capability_gap as gap
from app.goal_review import execution_gate
from app.memory.short_term import ShortTermMemory
from app.project_manager import ProjectOrchestrator
from app.structured_planning import compile_task, contract_of
from app.workspace_files import WorkspaceSandbox

SOURCE_STATEMENT = "架空原本01の原本抽出と引用の検証"
NON_SOURCE_STATEMENT = "架空文書01の作成と検証"


def _make_manager(tmp_path, tag=""):
    name = f"p0-2-疑似-{tag}"
    db_path = tmp_path / (name + ".db")
    memory = ShortTermMemory(db_path)
    project = memory.create_project(name, workspace_path="projects/" + name)
    pid = project["id"]
    memory.save_mission(pid, "架空目標-" + tag, "1. " + SOURCE_STATEMENT, "", False, ["chatgpt"])
    manager = ProjectOrchestrator(
        memory, None, lambda p: ("", []), None,
        lambda: [{"id": "chatgpt", "configured": True}],
        workspace=WorkspaceSandbox(tmp_path / ("ws-" + name)),
    )
    return manager, pid


def _register_source(manager, pid):
    manager.memory.add_context_file(pid, "架空原本.md", "架空本文", 8,
                                    b"dummy", "text/markdown", "markdown", "",
                                    "0" * 64, "upload")
    return "ctx-1"


def _plan_with_contract(manager, pid, statement, source_ids):
    task = compile_task(1, statement, {
        "title": "架空工程1", "scope": "架空作業の内容を説明する。", "headings": ["目的", "実施内容"],
    }, source_ids)
    manager.memory.replace_plan(pid, "架空計画", [task])
    return task


def test_compile_task_adds_source_read_only_with_source_and_vocab(tmp_path):
    base = compile_task(1, NON_SOURCE_STATEMENT, {
        "title": "架空工程1", "scope": "架空作業の内容を説明する。", "headings": ["目的", "実施内容"],
    }, ["ctx-1"])
    base_contract = contract_of(base)
    assert "required_capabilities" not in base_contract

    one = compile_task(1, SOURCE_STATEMENT, {
        "title": "架空工程1", "scope": "架空作業の内容を説明する。", "headings": ["目的", "実施内容"],
    }, ["ctx-1"])
    one_contract = contract_of(one)
    assert one_contract.get("required_capabilities") == ["source.read_excerpt"]

    empty_sources = compile_task(1, SOURCE_STATEMENT, {
        "title": "架空工程1", "scope": "架空作業の内容を説明する。", "headings": ["目的", "実施内容"],
    }, [])
    assert "required_capabilities" not in contract_of(empty_sources)

    non_vocab = compile_task(1, NON_SOURCE_STATEMENT, {
        "title": "架空工程1", "scope": "架空作業の内容を説明する。", "headings": ["目的", "実施内容"],
    }, ["ctx-1"])
    assert "required_capabilities" not in contract_of(non_vocab)


def test_compile_task_preserves_existing_required_capabilities(tmp_path):
    base = compile_task(1, NON_SOURCE_STATEMENT, {
        "title": "架空工程1", "scope": "架空作業の内容を説明する。", "headings": ["目的", "実施内容"],
    }, ["ctx-1"])
    before = dict(json.loads(base["acceptance_criteria"]))
    one = compile_task(1, SOURCE_STATEMENT, {
        "title": "架空工程1", "scope": "架空作業の内容を説明する。", "headings": ["目的", "実施内容"],
    }, ["ctx-1"])
    after = json.loads(one["acceptance_criteria"])
    assert after.get("required_capabilities") == ["source.read_excerpt"]
    for key in ("criterion_ids", "criterion", "outputs", "source_refs",
                "exit_checks", "artifact_category", "public_purpose_code"):
        if key == "criterion":
            continue
        assert after.get(key) == before.get(key) or key not in before
    task = compile_task(1, SOURCE_STATEMENT, {
        "title": "架空工程1", "scope": "架空作業の内容を説明する。", "headings": ["目的", "実施内容"],
        "outputs": [{"kind": "document", "path": "result/sc01.md",
                     "required_headings": ["目的", "実施内容"]}],
    }, ["ctx-1"])
    contract = contract_of(task)
    # compile_task は提案側の宣言を上書きしない。既存契約の他項目も不変。
    assert contract["outputs"][0]["path"] == "result/sc01.md"
    assert contract["criterion_ids"] == ["SC01"]
    assert contract["source_refs"] == ["context:ctx-1"]


def _preflight_for(manager, pid, statement, source_ids):
    manager.memory.save_mission(pid, "架空目標", "1. " + statement, "", False, ["chatgpt"])
    _plan_with_contract(manager, pid, statement, source_ids)
    return gap.capability_preflight(manager, pid)


def test_preflight_declared_has_no_gap(tmp_path):
    manager, pid = _make_manager(tmp_path, tag="declared")
    _register_source(manager, pid)
    preflight = _preflight_for(manager, pid, SOURCE_STATEMENT, ["ctx-1"])
    assert preflight["gap_codes"] == []
    row = preflight["criteria"][0]
    assert row["gap_code"] is None
    op = next(item for item in row["operations"] if item["operation_id"] == "source_extraction")
    assert "next_action" not in op


def test_preflight_undeclared_is_plan_wiring_gap(tmp_path):
    manager, pid = _make_manager(tmp_path, tag="wiring")
    _register_source(manager, pid)
    # compile_task の宣言を外した計画 (原本読取が必要だが工程が宣言していない)。
    task = compile_task(1, NON_SOURCE_STATEMENT, {
        "title": "架空工程1", "scope": "架空作業の内容を説明する。", "headings": ["目的", "実施内容"],
    }, ["ctx-1"])
    manager.memory.save_mission(pid, "架空目標", "1. " + SOURCE_STATEMENT, "", False, ["chatgpt"])
    manager.memory.replace_plan(pid, "架空計画", [task])
    preflight = gap.capability_preflight(manager, pid)
    assert preflight["gap_codes"] == ["plan_wiring_gap"]
    assert preflight["has_missing_capability"] is False
    row = preflight["criteria"][0]
    assert row["gap_code"] == "plan_wiring_gap"
    op = next(item for item in row["operations"] if item["operation_id"] == "source_extraction")
    assert op["gap_code"] == "plan_wiring_gap"
    assert op["next_action"] == "revise_plan"
    assert op["implemented"] and op["configured"] and op["input_compatible"]
    assert op["validated"] and op["permitted"]
    assert "既存の実行器で提供可能。計画修正で解消する" in op["reasons"]["provided"]
    wiring = [item for item in preflight["gaps"] if item["gap_code"] == "plan_wiring_gap"]
    assert len(wiring) == 1
    assert wiring[0]["next_action"] == "revise_plan"
    # 開発依頼は作らない。実行承認は止める。
    out = gap.generate_proposals(manager, pid, preflight, actor="架空担当")
    assert out["proposals"] == []
    assert out["skipped_gap_codes"] == ["plan_wiring_gap"]
    gate = gap.blocks_execution_approval(manager, pid, preflight=preflight)
    assert gate["blocked"] is True
    assert gate["next_action"] == "revise_plan"
    classifier = gap.preflight_to_classifier_inputs(preflight)
    assert "disposition" not in classifier
    assert classifier["capability_preflight"]["missing_capability"] is False


def test_preflight_missing_input_stays_missing_input(tmp_path):
    manager, pid = _make_manager(tmp_path, tag="noinput")
    preflight = _preflight_for(manager, pid, SOURCE_STATEMENT, ["ctx-1"])
    assert preflight["gap_codes"] == ["missing_input"]


def test_preflight_undefined_executor_stays_missing_capability(tmp_path, monkeypatch):
    manager, pid = _make_manager(tmp_path, tag="noexec")
    _register_source(manager, pid)
    manager.memory.save_mission(pid, "架空目標", "1. " + SOURCE_STATEMENT, "", False, ["chatgpt"])
    task = compile_task(1, NON_SOURCE_STATEMENT, {
        "title": "架空工程1", "scope": "架空作業の内容を説明する。", "headings": ["目的", "実施内容"],
    }, ["ctx-1"])
    manager.memory.replace_plan(pid, "架空計画", [task])
    import app.capability_registry as registry

    saved = dict(registry.DEFINITIONS)
    monkeypatch.delitem(registry.DEFINITIONS, "source.read_excerpt")
    try:
        preflight = gap.capability_preflight(manager, pid)
    finally:
        registry.DEFINITIONS.update(saved)
    assert preflight["gap_codes"] == ["missing_capability"]


def test_preflight_config_check_failure_is_not_wiring(tmp_path, monkeypatch):
    manager, pid = _make_manager(tmp_path, tag="checkfail")
    _register_source(manager, pid)
    manager.memory.save_mission(pid, "架空目標", "1. " + SOURCE_STATEMENT, "", False, ["chatgpt"])
    task = compile_task(1, NON_SOURCE_STATEMENT, {
        "title": "架空工程1", "scope": "架空作業の内容を説明する。", "headings": ["目的", "実施内容"],
    }, ["ctx-1"])
    manager.memory.replace_plan(pid, "架空計画", [task])

    def _boom(self, capabilities, context_files, web_allowed=False):
        raise RuntimeError("架空の確認失敗")

    monkeypatch.setattr("app.capability_registry.CapabilityRegistry.check", _boom)
    preflight = gap.capability_preflight(manager, pid)
    assert preflight["gap_codes"] == ["missing_capability"]


def test_preflight_unvalidated_stays_missing_evidence(tmp_path, monkeypatch):
    manager, pid = _make_manager(tmp_path, tag="evidence")
    _register_source(manager, pid)
    # 計画に宣言あり・原本あり・実行器ありでも、validated が偽なら
    # plan_wiring_gap にならず missing_evidence のまま。
    # domain_pipeline は validated を真にしない既存の経路で確認する
    # (document_generation 等の validated=True 経路は使わない)。
    inspection = gap._inspect_operation(
        "domain_pipeline", registry=gap._registry_of(manager),
        context_files=[{"id": "ctx-1"}], web_allowed=False,
        has_csv_output=False, has_source_refs=True)
    assert inspection["implemented"] and inspection["configured"]
    assert inspection["input_compatible"] and inspection["permitted"]
    assert inspection["validated"] is False
    assert gap._is_plan_wiring_candidate(inspection) is False
    code = gap._classify_gap(has_owner=True, policy_hit=False, inspection=inspection)
    assert code == "missing_evidence"


def _wiring_setup(tmp_path, tag):
    """受入1・2用: 登録原本あり・達成条件が原本抽出・計画に宣言なしの状態を作る。"""
    manager, pid = _make_manager(tmp_path, tag=tag)
    _register_source(manager, pid)
    task = compile_task(1, NON_SOURCE_STATEMENT, {
        "title": "架空工程1", "scope": "架空作業の内容を説明する。", "headings": ["目的", "実施内容"],
    }, ["ctx-1"])
    manager.memory.save_mission(pid, "架空目標", "1. " + SOURCE_STATEMENT, "", False, ["chatgpt"])
    manager.memory.replace_plan(pid, "架空計画", [task])
    return manager, pid


def test_actual_call_shape_blocks_wiring_without_preflight(tmp_path):
    """受入1: 実際の呼出形(preflightなし)で止まる。execution_gateも止まる。"""
    manager, pid = _wiring_setup(tmp_path, tag="actual-block")
    gate = gap.blocks_execution_approval(manager, pid)
    assert gate["blocked"] is True
    assert gate.get("plan_wiring_gap") is True
    assert gate.get("next_action") == "revise_plan"
    eg = execution_gate(manager, pid)
    assert eg["blocked"] is True
    assert "結線不足" in str(eg.get("reason") or "")


def test_actual_call_shape_clears_after_plan_fix(tmp_path):
    """受入2: 宣言を足すと実際の呼出形でも外れる(他の理由で止まらない構成)。"""
    manager, pid = _wiring_setup(tmp_path, tag="actual-fix")
    before = gap.blocks_execution_approval(manager, pid)
    assert before["blocked"] is True
    fixed = compile_task(1, SOURCE_STATEMENT, {
        "title": "架空工程1", "scope": "架空作業の内容を説明する。", "headings": ["目的", "実施内容"],
    }, ["ctx-1"])
    manager.memory.replace_plan(pid, "架空計画", [fixed])
    gate = gap.blocks_execution_approval(manager, pid)
    assert gate["blocked"] is False
    eg = execution_gate(manager, pid)
    assert eg["blocked"] is False


def test_preflight_compute_failure_propagates_and_gate_blocks(tmp_path, monkeypatch):
    """受入3: 事前照合の計算が例外なら握りつぶさず伝播し、execution_gateは止める側に倒れる。"""
    manager, pid = _wiring_setup(tmp_path, tag="actual-boom")

    def _boom(*args, **kwargs):
        raise RuntimeError("架空の照合失敗")

    monkeypatch.setattr(gap, "capability_preflight", _boom)
    with pytest.raises(RuntimeError):
        gap.blocks_execution_approval(manager, pid)
    eg = execution_gate(manager, pid)
    assert eg["blocked"] is True
    assert "不足機能の確認に失敗" in str(eg.get("reason") or "")


def test_malformed_preflight_blocks_fail_closed(tmp_path):
    """受入4: gapsがlistでない/要素がdictでない不正な事前照合は止める側に倒れる。"""
    manager, pid = _wiring_setup(tmp_path, tag="actual-malformed")
    good = gap.capability_preflight(manager, pid)
    sig = str(good.get("plan_signature") or "")
    bad_list = {"plan_signature": sig, "gaps": "not-a-list"}
    bad_elem = {"plan_signature": sig, "gaps": ["not-a-dict"]}
    bad_none = {"plan_signature": sig, "gaps": None}
    for bad in (bad_list, bad_elem, bad_none):
        gate = gap.blocks_execution_approval(manager, pid, preflight=bad)
        assert gate["blocked"] is True
        assert gate.get("plan_wiring_gap") is True
    # 署名の食い違いも通さない。
    mismatched = gap.blocks_execution_approval(
        manager, pid, plan_signature="架空の別署名", preflight=good)
    assert mismatched["blocked"] is True

    fixed = compile_task(1, SOURCE_STATEMENT, {
        "title": "架空工程1", "scope": "架空作業の内容を説明する。", "headings": ["目的", "実施内容"],
    }, ["ctx-1"])
    manager.memory.replace_plan(pid, "架空計画", [fixed])
    after = gap.capability_preflight(manager, pid)
    assert after["gap_codes"] == []
    gate = gap.blocks_execution_approval(manager, pid, preflight=after)
    assert gate["blocked"] is False


def test_plan_fix_clears_wiring_without_changing_goal(tmp_path):
    manager, pid = _make_manager(tmp_path, tag="fix")
    source_id = _register_source(manager, pid)
    statement = SOURCE_STATEMENT
    manager.memory.save_mission(pid, "架空目標", "1. " + statement, "", False, ["chatgpt"])
    broken = compile_task(1, NON_SOURCE_STATEMENT, {
        "title": "架空工程1", "scope": "架空作業の内容を説明する。", "headings": ["目的", "実施内容"],
    }, [source_id])
    manager.memory.replace_plan(pid, "架空計画", [broken])
    before = gap.capability_preflight(manager, pid)
    assert before["gap_codes"] == ["plan_wiring_gap"]
    assert gap.blocks_execution_approval(manager, pid, preflight=before)["blocked"] is True
    # 目標の達成条件は削らず、宣言だけ足す (compile_task の正規の宣言を使う)。
    fixed = compile_task(1, statement, {
        "title": "架空工程1", "scope": "架空作業の内容を説明する。", "headings": ["目的", "実施内容"],
    }, [source_id])
    fixed_contract = json.loads(fixed["acceptance_criteria"])
    assert fixed_contract["criterion"] == statement
    manager.memory.replace_plan(pid, "架空計画", [fixed])
    after = gap.capability_preflight(manager, pid)
    assert after["gap_codes"] == []
    gate = gap.blocks_execution_approval(manager, pid, preflight=after)
    assert gate["blocked"] is False


def _doc_statement_without_declaration_setup(tmp_path, tag):
    """受入1用: document_generationを要する達成条件・実行器あり・5状態真・計画に宣言なし。"""
    manager, pid = _make_manager(tmp_path, tag=tag)
    _register_source(manager, pid)
    statement = NON_SOURCE_STATEMENT
    manager.memory.save_mission(pid, "架空目標", "1. " + statement, "", False, ["chatgpt"])
    task = compile_task(1, statement, {
        "title": "架空工程1", "scope": "架空作業の内容を説明する。", "headings": ["目的", "実施内容"],
    }, ["ctx-1"])
    contract = json.loads(task["acceptance_criteria"])
    # 文書生成を提供しない計画にする(.md出力・宣言・外部作用を外し、表加工だけ提供)。
    contract["outputs"] = [{"path": "result/sc01.csv", "required_columns": ["架空列"],
                            "minimum_rows": 1}]
    for key in ("required_capabilities", "public_web_research",
                "action_requirements", "execution_kind"):
        contract.pop(key, None)
    task["acceptance_criteria"] = json.dumps(contract, ensure_ascii=False)
    manager.memory.replace_plan(pid, "架空計画", [task])
    return manager, pid, statement


def test_document_generation_without_declaration_stays_missing_capability(tmp_path):
    """受入1: document_generationはplan_wiring_gapにしない(従来のmissing_capability)。"""
    manager, pid, _ = _doc_statement_without_declaration_setup(tmp_path, tag="doc-nonwiring")
    preflight = gap.capability_preflight(manager, pid)
    assert "plan_wiring_gap" not in preflight["gap_codes"]
    assert preflight["gap_codes"] == ["missing_capability"]
    row = preflight["criteria"][0]
    assert row["gap_code"] == "missing_capability"
    op = next(item for item in row["operations"] if item["operation_id"] == "document_generation")
    assert op["gap_code"] == "missing_capability"
    assert "next_action" not in op
    # 5状態は真・実行器ありでも操作IDの条件で結線不足にしない。
    assert op["implemented"] and op["configured"] and op["input_compatible"]
    assert op["validated"] and op["permitted"]
    assert gap._is_plan_wiring_candidate({**op, "operation_id": "document_generation"}) is False
    gate = gap.blocks_execution_approval(manager, pid, preflight=preflight)
    assert gate["blocked"] is False
    eg = execution_gate(manager, pid)
    assert eg["blocked"] is False


def test_source_extraction_without_declaration_is_wiring_and_blocks(tmp_path):
    """受入2: source_extractionは同じ条件でplan_wiring_gapになり実行ゲートが止まる。"""
    manager, pid = _wiring_setup(tmp_path, tag="src-wiring-pair")
    preflight = gap.capability_preflight(manager, pid)
    assert preflight["gap_codes"] == ["plan_wiring_gap"]
    op = next(item for item in preflight["criteria"][0]["operations"]
              if item["operation_id"] == "source_extraction")
    assert op["gap_code"] == "plan_wiring_gap"
    assert op["next_action"] == "revise_plan"
    gate = gap.blocks_execution_approval(manager, pid, preflight=preflight)
    assert gate["blocked"] is True
    eg = execution_gate(manager, pid)
    assert eg["blocked"] is True
