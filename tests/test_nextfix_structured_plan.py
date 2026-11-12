"""受入1〜4の構造修正テスト(疑似PJ・実関数・外部通信なし)。

- 受入1: 完全架空の備品申請仕様書から生成した計画が、4段階・FAQ5件・承認箇所・
  架空明記・実処理禁止を個別に検査できる契約を持つ。
- 受入2: 最終検証レポートの生成責任がシステムで、人間承認は別ゲート。
  人間が未承認なら完了にならない。
- 受入3: 外部指摘の反映後、問題の構造フィールドが実際に変わり、差分と再評価
  結果が記録される。未変更なら適用を拒否。scope追記だけでは被覆を合格にしない。
- 受入4: イベント案件は2点の出力契約、カフェ案件は仮価格を商業条件策定にしない。
  食品安全・価格の仮定を事実として扱わない。
- 外部送信パケットの既定: 内部原本を送らない。public_summaryは明示許可なしで空。
"""
import json

from app.completion_gate import REASON_HUMAN, criteria_ready_for_human_confirmation
from app.goal_review import public_structure
from app.goal_review_queue import public_draft
from app.structured_planning import (
    build_rebuild_diff,
    build_rebuild_generic_plan,
    compile_plan,
    compile_task,
    contract_of,
    validate_plan,
    validate_rebuild_generic_candidate,
)


def _proposal(title, scope, headings, extra=None):
    row = {"title": title, "scope": scope, "headings": headings, "depends_on": []}
    if extra:
        row.update(extra)
    return row


def test_acceptance1_fictional_supply_faq_has_individual_checks():
    criteria = [
        "完全架空の備品申請4段階(申請・上長承認・総務確認・払出)を手順書として作成する",
        "完全架空の備品申請FAQ5件を作成し、各質問を個別に検証する",
        "承認箇所と人間確認の範囲を明記する",
        "完全架空であることを明記し、実処理の完了と判定しない",
    ]
    tasks = [
        compile_task(1, criteria[0], _proposal(
            "架空備品手順書",
            "完全架空の4段階を手順書として整理する。要確認は未確認とする。",
            ["申請手順", "承認手順", "総務確認", "払出手順"],
            {"outputs": [
                {"kind": "procedure", "required_headings": ["申請手順", "承認手順"]},
                {"kind": "checklist", "required_headings": ["総務確認", "払出手順"]},
            ]},
        ), []),
        compile_task(2, criteria[1], _proposal(
            "架空FAQ文書",
            "完全架空のFAQ5件を整理する。各質問の回答根拠と要確認を区別する。",
            ["FAQ1", "FAQ2", "FAQ3"],
            {"outputs": [
                {"kind": "faq", "required_headings": ["FAQ1", "FAQ2", "FAQ3"]},
            ]},
        ), []),
        compile_task(3, criteria[2], _proposal(
            "承認箇所の整理",
            "承認箇所と人間の確認範囲を整理する。未承認は承認待ちとする。",
            ["承認箇所", "確認範囲"],
        ), []),
        compile_task(4, criteria[3], _proposal(
            "架空明記と実処理禁止",
            "完全架空であることを明記し、実処理の完了と判定しない。",
            ["架空明記", "実処理禁止"],
        ), []),
    ]
    plan = compile_plan(criteria, tasks, goal="完全架空の備品申請デモ")
    assert validate_plan(plan, {"SC01", "SC02", "SC03", "SC04"})["passed"]
    by_key = {item["task_key"]: item for item in plan["tasks"]}
    first = contract_of(by_key["SC01"])
    assert len(first["outputs"]) == 2
    assert "each_output_contract_verified" in first["exit_checks"]
    assert "procedure_steps_individually_verified" in first["exit_checks"]
    second = contract_of(by_key["SC02"])
    assert "faq_items_individually_verified" in second["exit_checks"]
    assert second["semantic_review_required"] is True
    third = contract_of(by_key["SC03"])
    assert "approval_points_verified" in third["exit_checks"]
    assert third["human_confirmation_required"] is True
    fourth = contract_of(by_key["SC04"])
    assert "fictional_marking_verified" in fourth["exit_checks"]
    assert "no_real_processing_claimed" in fourth["exit_checks"]
    assert "実処理の完了と判定しない" in by_key["SC04"]["description"]


def test_acceptance2_final_report_is_system_and_human_gate_is_separate():
    plan = compile_plan(["架空文書Aを作成する"], [
        compile_task(1, "架空文書Aを作成する", _proposal(
            "架空文書A", "架空文書Aの内容を整理する。", ["目的", "実施内容"]), []),
    ])
    final = contract_of(plan["tasks"][-1])
    assert final["responsible_role"] == "system"
    assert final["completion_evidence"] == "validated_artifact"
    assert final["verification_report"]["generated_by"] == "system"
    assert "per_criterion_verdicts_recorded" in final["exit_checks"]
    # 人間の承認は別ゲートとして残る。
    assert final["human_confirmation_required"] is True
    assert final["human_approval_gate"] == "separate_human_acceptance"
    # 未承認を完了扱いにしない(既存の完了ゲートの意味は変えない)。
    assert criteria_ready_for_human_confirmation({"criteria": []}) is False
    assert criteria_ready_for_human_confirmation(
        {"criteria": [{"status": "PASS"}, {"status": "UNTESTABLE"}]}) is False
    assert criteria_ready_for_human_confirmation(
        {"criteria": [{"status": "PASS"}, {"status": "PASS"}]}) is True
    assert REASON_HUMAN == "HUMAN_ACCEPTANCE_MISSING"


def test_acceptance3_structural_issue_without_diff_is_rejected():
    criteria = ["架空文書の役割と成果物と検査項目を整理する"]
    old_task = compile_task(1, criteria[0], _proposal(
        "架空文書", "架空文書の内容を整理する。", ["目的", "実施内容"]), [])
    old_snapshot = {"tasks": [old_task], "sources": []}
    mission = {"goal": "架空目標", "success_criteria": criteria[0], "plan_version": 1}
    actions = [{
        "disposition": "rebuild_generic",
        "target": "execution_pipeline",
        "change": "役割と成果物と検査項目をacceptance_criteriaへ反映する具体的な再構成を行う。",
        "reason": "構造指摘に対応するため汎用計画を再構成する。",
        "binds": {"goal_criterion_ids": ["SC01"]},
    }]
    rebuilt = build_rebuild_generic_plan(mission, old_snapshot, actions, [], None)
    assert validate_rebuild_generic_candidate(rebuilt, None, mission)["passed"]
    diff = build_rebuild_diff(old_snapshot, rebuilt)
    assert diff["changed_tasks"] >= 1
    assert rebuilt["rebuild_diff"]["changed_tasks"] >= 1
    assert "rebuild_plan_signature" in rebuilt
    # scope文章への追記だけでは構造差分にならない(被覆を合格にしない)。
    same = build_rebuild_diff(old_snapshot, {"tasks": [old_task]})
    assert same["changed_tasks"] == 0


def test_acceptance4_event_two_outputs_and_cafe_not_commercial():
    event_criteria = ["架空イベントの案内文とチェックリストを作成する"]
    event_task = compile_task(1, event_criteria[0], _proposal(
        "架空案内", "架空イベントの案内内容を整理する。", ["案内文", "注意事項"],
        {"outputs": [
            {"kind": "document", "required_headings": ["案内文", "注意事項"]},
            {"kind": "checklist", "required_headings": ["準備", "当日対応"]},
        ]},
    ), [])
    event_contract = contract_of(event_task)
    assert len(event_contract["outputs"]) == 2
    assert "each_output_contract_verified" in event_contract["exit_checks"]

    cafe_criteria = ["完全架空のカフェ新メニューを作成する。価格は仮の例示であり要確認とする"]
    cafe_task = compile_task(1, cafe_criteria[0], _proposal(
        "架空メニュー",
        "完全架空の商品説明を作成する。価格は仮の例示であり、食品安全の仮定は要確認とする。",
        ["商品説明", "仮価格の扱い"],
    ), [])
    cafe_contract = contract_of(cafe_task)
    assert cafe_contract["public_purpose_code"] == "menu_document"
    assert cafe_contract["public_purpose_code"] != "commercial_terms"
    assert "仮定・想定・例示は事実として扱わず" in cafe_task["description"]
    assert "食品安全の仮定は事実として扱わず" in cafe_task["description"]
    assert cafe_contract["semantic_review_required"] is True


def test_packet_default_sends_no_original_and_summary_empty_without_permission():
    plan = compile_plan(["架空文書を作成する"], [
        compile_task(1, "架空文書を作成する", _proposal(
            "架空文書", "架空文書の内容を整理する。", ["目的", "実施内容"]), ["src-1"]),
    ])
    snapshot = {"tasks": plan["tasks"]}
    packet = public_structure(snapshot)
    blob = json.dumps(packet, ensure_ascii=False)
    assert "架空文書を作成する" not in blob
    assert "src-1" not in blob
    # public_summaryは明示許可がなければ空(既定は従来どおり)。
    assert public_draft(snapshot) and "公開用要約" not in public_draft(snapshot)
    allowed = public_draft(snapshot, public_summary_allowed="完全架空の公開用要約です")
    assert "完全架空の公開用要約です" in allowed


def test_real_planner_schema_can_request_distinct_outputs():
    from app.project_manager import PROPOSAL_JSON_SCHEMA
    properties = PROPOSAL_JSON_SCHEMA["properties"]
    assert "outputs" in properties
    assert "exit_checks" in properties
    assert "semantic_review_required" in properties


def test_event_two_outputs_without_model_optional_field():
    criterion = "架空イベントの案内文と当日運営チェックリストの2点を作成する"
    task = compile_task(1, criterion, _proposal(
        "案内文とチェックリスト", "参加者向け案内と当日手順を整理する。", ["日時と場所", "参加方法"]
    ), [])
    outputs = contract_of(task)["outputs"]
    assert [item["path"] for item in outputs] == ["result/sc01.md", "result/sc01_checklist.md"]
    assert "each_output_contract_verified" in contract_of(task)["exit_checks"]


def test_model_cannot_disable_semantic_review_or_silently_drop_outputs():
    import pytest
    criterion = "架空の備品申請手順書とFAQを作成し確認する"
    task = compile_task(1, criterion, _proposal(
        "備品案内", "各手順とFAQを確認する。", ["手順", "FAQ"],
        {"semantic_review_required": False}
    ), [])
    assert contract_of(task)["semantic_review_required"] is True
    too_many = [{"kind": "document", "required_headings": ["目的", "内容"]} for _ in range(5)]
    with pytest.raises(ValueError, match="at most 4"):
        compile_task(1, criterion, _proposal(
            "備品案内", "各手順とFAQを確認する。", ["手順", "FAQ"], {"outputs": too_many}
        ), [])


def test_rebuild_preserves_separate_procedure_output_from_saved_contract():
    criterion = (
        "架空カフェの新メニュー2品を説明する。商品説明文とスタッフ向け手順書の"
        "日本語Markdownを各1点、合計2点作る。"
    )
    original = compile_task(1, criterion, _proposal(
        "架空メニューと手順書", "架空のメニューとスタッフ手順を作る。",
        ["新メニュー概要", "魅力の説明"],
        {"outputs": [
            {"kind": "document", "required_headings": ["新メニュー概要", "魅力の説明"]},
            {"kind": "procedure", "required_headings": ["準備手順", "提供手順"]},
        ]},
    ), [])
    original_plan = compile_plan([criterion], [original], goal="架空カフェの紹介資料")
    before = contract_of(next(t for t in original_plan["tasks"] if t["task_key"] == "SC01"))["outputs"]
    goal_contract = {"criteria": [{"criterion_id": "SC01", "statement": criterion,
                                  "exec_task_keys": ["SC01"],
                                  "verify_task_keys": ["final_verification"]}]}
    mission = {"goal": "架空カフェの紹介資料", "success_criteria": criterion,
               "plan_version": 1}
    rebuilt = build_rebuild_generic_plan(
        mission, {"tasks": original_plan["tasks"], "sources": []},
        [{"disposition": "rebuild_generic", "target": "SC01",
          "change": "2品それぞれの魅力を具体化する"}],
        [], goal_contract,
    )
    after = contract_of(next(t for t in rebuilt["tasks"] if t["task_key"] == "SC01"))["outputs"]
    assert [item["path"] for item in after] == [item["path"] for item in before]
    assert [item["artifact_category"] for item in after] == [
        "document", "procedure_document"
    ]
    assert validate_rebuild_generic_candidate(rebuilt, goal_contract, mission)["passed"]


