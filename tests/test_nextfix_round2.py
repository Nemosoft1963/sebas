import pytest
from app.structured_planning import compile_task, compile_plan
from app.goal_review import public_structure

def test_one_markdown_goal_rejects_split_outputs():
    proposal = {"title":"架空備品手順","scope":"手順とFAQを一つの成果物にまとめる",
                "headings":["申請手順","よくある質問"],
                "outputs":[{"kind":"procedure","required_headings":["申請手順","承認手順"]},
                           {"kind":"faq","required_headings":["質問","回答"]}]}
    with pytest.raises(ValueError, match="requires 1 Markdown"):
        compile_task(1,"1つの日本語Markdownに手順とFAQを記載する",proposal,[])
    proposal["outputs"] = [proposal["outputs"][0]]
    assert len(compile_task(1,"1つの日本語Markdownに手順とFAQを記載する",proposal,[])["acceptance_criteria"]) > 0

def test_public_structure_exposes_only_safe_gate_and_check_codes():
    task=compile_task(1,"架空のFAQを作成する",{"title":"架空FAQ","scope":"質問と回答を整理する","headings":["質問","回答"]},[])
    packet=public_structure({"tasks":compile_plan(["架空のFAQを作成する"],[task])["tasks"]})
    assert all(set(row.get("exit_check_codes",[])) <= {
        "source_evidence_linked","criterion_content_verified","unknowns_and_failures_reported",
        "faq_items_individually_verified","procedure_steps_individually_verified",
        "approval_points_verified","fictional_marking_verified","no_real_processing_claimed",
        "each_output_contract_verified","all_criteria_decided","evidence_paths_verified",
        "unmet_and_pending_reported","per_criterion_verdicts_recorded",
        "registered_sources_classified","missing_inputs_listed","validation_method_defined",
    } for row in packet["tasks"])
    assert packet["tasks"][-1]["separate_human_approval_gate"] is True


def test_two_markdown_goal_uses_total_count_over_each_one():
    proposal = {"title":"架空メニュー","scope":"説明と手順を作る","headings":["説明","手順"],
                "outputs":[{"kind":"document","required_headings":["説明","前提"]},
                           {"kind":"procedure","required_headings":["準備","片付け"]}]}
    criterion="商品説明と手順のMarkdownを各1点、合計2点作る。"
    assert compile_task(1,criterion,proposal,[])
    proposal["outputs"].append({"kind":"faq","required_headings":["質問","回答"]})
    with pytest.raises(ValueError,match="requires 2 Markdown"):
        compile_task(1,criterion,proposal,[])
