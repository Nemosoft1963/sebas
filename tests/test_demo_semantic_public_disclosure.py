import json

from app.goal_review import public_structure
from app.goal_review_queue import public_draft
from app.requirement_verification import verify_requirement_items
from app.structured_planning import compile_plan, compile_task, contract_of


def test_multi_output_semantic_items_fail_closed_without_local_judge(tmp_path):
    proposal = {
        "title": "架空イベント",
        "scope": "案内文と当日チェックリストを作成する。",
        "headings": ["案内", "準備"],
        "outputs": [
            {"kind": "document", "required_headings": ["案内", "連絡先"]},
            {"kind": "checklist", "required_headings": ["準備", "終了後"]},
        ],
        "semantic_review_required": True,
    }
    task = compile_task(1, "架空イベントの案内文とチェックリストを作成する", proposal, [])
    contract = contract_of(task)
    paths = {output["path"] for output in contract["outputs"]}
    semantic = [row for row in contract["requirement_matrix"]
                if row["method"] == "llm_semantic"
                and row["expected"].get("criterion_semantic") is True]
    assert {row["artifact_path"] for row in semantic} == paths
    body = "\n".join(f"## {heading}\n架空の説明です。" for heading in ["案内", "連絡先", "準備", "終了後"])
    for relative in paths:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    results = verify_requirement_items(task, lambda relative: tmp_path / relative, semantic_judge=None)
    assert all(row["result"] == "UNTESTABLE" for row in results
               if row["item_id"] in {item["item_id"] for item in semantic})


def test_public_packet_distinguishes_deliverables_and_final_aggregation():
    task = compile_task(1, "架空の案内文を作成する", {
        "title": "架空案内", "scope": "案内文の内容を整理する。",
        "headings": ["日時", "場所"], "semantic_review_required": True,
    }, [])
    plan = compile_plan(["架空の案内文を作成する"], [task])
    snapshot = {"tasks": plan["tasks"]}
    packet = public_structure(snapshot)
    assert packet["output_classes"]["deliverable_count"] == 1
    assert packet["output_classes"]["internal_record_count"] >= 1
    final = next(row for row in packet["tasks"] if row["role"] == "final_verification")
    assert final["aggregates_earlier_item_count"] == len(contract_of(task)["requirement_matrix"])
    assert final["aggregates_criterion_ids"] == ["SC01"]
    draft = public_draft(snapshot)
    assert "利用者向け完成成果物は1件" in draft


def test_artifact_count_checks_both_files_and_section_length():
    from app.requirement_matrix import extract_requirement_items
    from app.requirement_verification import check_item_deterministic

    outputs = [
        {"path": "result/sc01.md", "required_headings": ["商品A", "商品B"]},
        {"path": "result/sc01_procedure.md", "required_headings": ["準備手順", "提供手順"]},
    ]
    criterion = (
        "架空の2品を設定し、各品の魅力を80字程度で説明する。"
        "商品説明文とスタッフ向け手順書のMarkdownを各1点、合計2点作る。"
        "価格や原材料は仮定と明記する。"
    )
    items = extract_requirement_items("SC01", criterion, outputs, [], require_semantic=True)
    length_item = next(item for item in items if item["method"] == "section_char_count")
    good = "# 商品A\n" + "あ" * 80 + "\n# 商品B\n" + "い" * 80
    short = "# 商品A\n短い\n# 商品B\n" + "い" * 80
    assert check_item_deterministic(length_item, good)["result"] == "PASS"
    assert check_item_deterministic(length_item, short)["result"] == "FAIL"
    assert {item["artifact_path"] for item in items
            if item["method"] == "phrase" and item["expected"].get("keywords") == ["仮"]} == {
                "result/sc01.md", "result/sc01_procedure.md"
            }


def test_artifact_count_fails_when_declared_second_file_is_missing(tmp_path):
    proposal = {
        "title": "架空の説明と手順", "scope": "商品説明と手順書を作成する。",
        "headings": ["商品説明", "準備手順"],
        "outputs": [
            {"kind": "document", "required_headings": ["商品説明", "価格の仮定"]},
            {"kind": "procedure", "required_headings": ["準備手順", "提供手順"]},
        ],
    }
    task = compile_task(1, "商品説明文と手順書のMarkdownを合計2点作る", proposal, [])
    first = tmp_path / "result/sc01.md"
    first.parent.mkdir(parents=True, exist_ok=True)
    first.write_text("# 商品説明\n架空の説明", encoding="utf-8")
    rows = verify_requirement_items(task, lambda relative: tmp_path / relative)
    assert any(row["method"] == "artifact_count" and row["result"] == "FAIL" for row in rows)
    (tmp_path / "result/sc01_procedure.md").write_text("# 準備手順\n架空の手順", encoding="utf-8")
    rows = verify_requirement_items(task, lambda relative: tmp_path / relative)
    assert any(row["method"] == "artifact_count" and row["result"] == "PASS" for row in rows)


def test_semantic_conditions_follow_the_responsible_output():
    from app.requirement_matrix import extract_requirement_items

    criterion = (
        "参加者向け案内文に日時、場所、対象、持ち物、問い合わせ先を含める。"
        "当日運営チェックリストに準備、受付、終了後の確認を含める。"
        "成果物は日本語Markdown 2点とする。"
    )
    outputs = [
        {"path": "result/sc01.md", "artifact_category": "document",
         "required_headings": ["イベント概要", "持ち物", "問い合わせ先"]},
        {"path": "result/sc01_checklist.md", "artifact_category": "checklist",
         "required_headings": ["準備", "受付", "終了後の確認"]},
    ]
    rows = extract_requirement_items("SC01", criterion, outputs, [], require_semantic=True)
    by_path = {row["artifact_path"]: row["requirement"] for row in rows
               if row["method"] == "llm_semantic" and row["expected"].get("criterion_semantic")}
    assert "日時、場所、対象" in by_path["result/sc01.md"]
    assert "当日運営チェックリスト" not in by_path["result/sc01.md"]
    assert "当日運営チェックリスト" in by_path["result/sc01_checklist.md"]
    assert "参加者向け案内文" not in by_path["result/sc01_checklist.md"]


def test_approval_stage_matrix_requires_each_stage_decision():
    from app.requirement_matrix import extract_requirement_items
    from app.requirement_verification import check_item_deterministic

    criterion = (
        "申請、上長確認、購入、受け取りまでの4段階を説明する。"
        "各段階のうち人間の承認が必要な箇所を個別に示す。"
    )
    items = extract_requirement_items("SC01", criterion,
        [{"path": "result/sc01.md", "required_headings": ["申請", "上長確認"]}], [])
    item = next(row for row in items if row["method"] == "approval_stage_matrix")
    complete = "\n".join([
        "## 申請", "申請時に承認不要。", "## 上長確認", "上長の承認が必要。",
        "## 購入", "購入時は別の承認不要。", "## 受け取り", "受け取り時の承認不要。",
    ])
    incomplete = complete.replace("受け取り時の承認不要。", "品物を受け取る。")
    assert check_item_deterministic(item, complete)["result"] == "PASS"
    assert check_item_deterministic(item, incomplete)["result"] == "FAIL"


def test_semantic_transport_timeout_is_unverifiable_not_a_task_crash():
    import httpx
    from app.requirement_verification import build_local_semantic_judge, check_item_semantic

    def unavailable(_system, _prompt):
        raise httpx.ReadTimeout("local model timeout")

    item = {
        "item_id": "SC01:S1", "method": "llm_semantic", "required": True,
        "requirement": "架空の案内文が読みやすい", "expected": {},
    }
    row = check_item_semantic(item, "架空の案内文", semantic_judge=build_local_semantic_judge(unavailable))
    assert row["result"] == "UNTESTABLE"
    assert "ローカルLLMの失敗" in row["reason"]


def test_numbered_stages_and_faq_are_counted_in_their_own_sections():
    from app.requirement_verification import check_item_deterministic

    content = """# 架空手順書
## 1. 申請
このステップで入力します。
## 2. 上長確認
人間が確認します。
## 3. 購入
発注担当が確認します。
## 4. 受け取り
内容を照合します。
## FAQ（新人向け）
1. **質問A？** 回答A
2. **質問B？** 回答B
3. **質問C？** 回答C
4. **質問D？** 回答D
5. **質問E？** 回答E
## 未確認事項
各項目を確認します。
"""
    stages = {"item_id": "S", "method": "count", "expected":
              {"count": 4, "unit": "段階", "markers": ["段階", "ステップ"]}}
    faq = {"item_id": "F", "method": "count", "expected":
           {"count": 5, "unit": "件", "markers": ["問", "件", "Q", "?"]}}
    assert check_item_deterministic(stages, content)["result"] == "PASS"
    assert check_item_deterministic(faq, content)["result"] == "PASS"
    assert check_item_deterministic(faq, content.replace("5. **質問E？** 回答E\n", ""))["result"] == "FAIL"


def test_single_artifact_repair_rechecks_and_repairs_newly_exposed_second_file():
    import asyncio
    import json
    from app.project_manager import ProjectOrchestrator

    class Memory:
        def __init__(self):
            self.events = []
        def add_event(self, _project_id, kind, _message, _task_id, detail=""):
            self.events.append(kind)

    class Fake:
        def __init__(self):
            self.memory = Memory()
            self.responses = iter(("initial", "first", "second"))
            self.verifications = iter((
                ["result/first.md: minimum_characters未達"],
                ["result/first.md: minimum_characters未達"],
                ["result/second.md: minimum_characters未達"],
                [],
            ))
        def _expected_artifact_paths(self, _contract):
            return {"result/first.md", "result/second.md"}
        def _verify_execution_evidence(self, *_args):
            failures = next(self.verifications)
            return failures, {"requires_table_operation": False, "requires_artifact": True,
                              "requires_source_reference": False, "missing_contract_paths": [],
                              "artifacts": [{"path": "result/first.md"}]}
        async def _local_complete(self, _system, _prompt, _schema):
            return next(self.responses)
        def _apply_execution_response(self, _project, _project_id, _task_id, response,
                                      _allowed_source_ids, _presented_source_ids):
            return response, [], {"workspace_operations": [{"path": response}]}
        def _merge_execution_evidence(self, _old, new):
            return new
        def _reject_unverified_artifacts(self, *_args):
            raise AssertionError("passing repairs must not be rejected")

    task = {"id": "task", "title": "2文書", "description": "2文書を作る",
            "acceptance_criteria": json.dumps({"outputs": [
                {"path": "result/first.md", "required_headings": ["A"], "minimum_characters": 600},
                {"path": "result/second.md", "required_headings": ["B"], "minimum_characters": 600},
            ]})}
    fake = Fake()
    result = asyncio.run(ProjectOrchestrator._verify_or_correct_execution(
        fake, {}, "project", task, "base", "first report", {}, [], set()))
    assert "自動補正後に合格" in result
    assert fake.memory.events.count("task_single_artifact_repair_started") == 2


def test_product_count_and_each_description_length_use_child_headings():
    from app.requirement_verification import check_item_deterministic

    content = ("# 新メニュー概要\n架空の2品です。\n# 魅力の説明\n"
               "## りんごトースト\n" + "あ" * 80 + "\n"
               "## かぼちゃラテ\n" + "い" * 80 + "\n"
               "# 根拠と未確認事項\n価格は仮定。")
    count = {"item_id": "C", "method": "count", "expected":
             {"count": 2, "unit": "品", "markers": ["品", "点"]}}
    length = {"item_id": "L", "method": "section_char_count", "expected":
              {"headings": ["魅力の説明"], "minimum": 60, "maximum": 100}}
    assert check_item_deterministic(count, content)["result"] == "PASS"
    assert check_item_deterministic(length, content)["result"] == "PASS"
    assert check_item_deterministic(length, content.replace("い" * 80, "短い"))["result"] == "FAIL"
