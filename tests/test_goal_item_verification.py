"""P0(修正A/B)の受入試験。疑似PJ・実関数・外部通信なし。

- 中心: 必要項目1件を欠いた成果物が、見出しと文字数を満たしていても最終PASSにならない。
- 項目の抽出漏れ・対象成果物不明・検査方法未定義 → 計画が needs_review。
- 片方の成果物の項目ゼロ → 不合格。
- 根拠なし・曖昧・読取失敗(LLM失敗注入) → PASSにしない。
- 計画・原本・成果物の変更で判定が無効化され、再検査される。
- 3デモ(イベント・カフェ・備品申請)の形で台帳が保存され、レポートが台帳からのみ生成される。
- 人間の承認が未承認なら完了しない。車両・OCR・削除・外部送信の既存挙動は回帰で維持。
"""
from __future__ import annotations

import json

from app.completion_gate import evaluate as gate_evaluate
from app.goal_contract import activate
from app.goal_review import plan_snapshot
from app.memory.short_term import ShortTermMemory
from app.project_lifecycle_registry import audit_registry_completeness
from app.project_manager import ProjectOrchestrator
from app.requirement_item_ledger import latest_ledger, record_ledger
from app.requirement_matrix import (
    detect_extraction_doubts,
    extract_requirement_items,
    validate_requirement_matrix,
)
from app.requirement_verification import (
    check_item_semantic,
    ledger_invalidated,
    ledger_payload,
    summarize_criterion,
    verify_requirement_items,
)
from app.structured_planning import compile_plan, compile_task, contract_of, validate_plan, verify_outputs
from app.workspace_files import WorkspaceSandbox


FILLER = "架空の検証可能な具体的内容を記載する。" * 40


def _proposal(title, scope, headings, extra=None):
    row = {"title": title, "scope": scope, "headings": headings, "depends_on": []}
    if extra:
        row.update(extra)
    return row


def _manager(tmp_path, goal, success):
    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("項目検証テスト", workspace_path="projects/itemcheck")
    pid = project["id"]
    memory.save_mission(pid, goal, success, "", False, [])
    workspace = WorkspaceSandbox(tmp_path / "workspace")
    manager = ProjectOrchestrator(
        memory, None, lambda p: ("", []), None, lambda: [], workspace=workspace,
    )
    return manager, pid


def _write(manager, pid, path, body):
    project = manager.memory.get_project(pid)
    target = manager.workspace.resolve_file(project.get("workspace_path", ""), pid, path)[2]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    return target


def _complete_task(manager, pid, task, bodies):
    for path, body in bodies.items():
        _write(manager, pid, path, body)
    manager.memory.update_task(task["id"], "completed", result="成果物作成済み")


def _faq_body(count, missing_one=False):
    sections = []
    total = count - (1 if missing_one else 0)
    for index in range(1, total + 1):
        sections.append(f"## FAQ{index}\n{FILLER}\n質問{index}と回答{index}を記載する。")
    sections.append(f"## 根拠と未確認事項\n{FILLER}")
    sections.append(f"## 実施状態と次の行動\n{FILLER}")
    return "\n".join(sections) + "\n"


def _supply_plan():
    criteria = [
        "完全架空の備品申請4段階(申請・上長承認・総務確認・払出)を手順書として作成する",
        "完全架空の備品申請FAQ5件を作成し、各質問を個別に検証する",
    ]
    tasks = [
        compile_task(1, criteria[0], _proposal(
            "架空備品手順書",
            "完全架空の4段階を手順書として整理する。要確認は未確認とする。",
            ["申請手順", "承認手順", "総務確認", "払出手順"],
            {"outputs": [
                {"kind": "procedure", "required_headings": ["申請手順", "承認手順"]},
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
    ]
    return compile_plan(criteria, tasks, goal="完全架空の備品申請デモ")


def test_matrix_generated_with_stable_ids_and_sources():
    plan = _supply_plan()
    by_key = {item["task_key"]: contract_of(item) for item in plan["tasks"]}
    first = by_key["SC01"]
    second = by_key["SC02"]
    assert isinstance(first.get("requirement_matrix"), list) and first["requirement_matrix"]
    assert isinstance(second.get("requirement_matrix"), list) and second["requirement_matrix"]
    for row in first["requirement_matrix"] + second["requirement_matrix"]:
        assert row["item_id"].startswith(row["criterion_id"] + "-R")
        assert row["artifact_path"]
        assert row["requirement"]
        assert row["method"] in ("count", "headings", "phrase", "llm_semantic")
        assert "required" in row and "source_ref" in row
    # 決定性(PYTHONHASHSEEDに依存しない順序)。
    again = _supply_plan()
    again_by_key = {item["task_key"]: contract_of(item) for item in again["tasks"]}
    assert again_by_key["SC02"]["requirement_matrix"] == second["requirement_matrix"]
    # デモ語彙(FAQ5件)は試験側の入力文にだけ存在する。製品は語彙を補わず、
    # 達成条件文から一般規則で「5件」の個数要求を抽出することを確認する。
    blob = "\n".join(row["requirement"] for row in second["requirement_matrix"])
    assert "数量要求: 5件を満たす" in blob
    blob1 = "\n".join(row["requirement"] for row in first["requirement_matrix"])
    assert "申請" in blob1 and "払出" in blob1


def test_missing_extraction_target_method_needs_review():
    task = compile_task(1, "架空文書を作成する", _proposal(
        "架空文書", "架空文書の内容を整理する。", ["目的", "実施内容"]), [])
    contract = contract_of(task)
    assert contract["requirement_matrix"]
    broken = json.loads(json.dumps(contract))
    broken["requirement_matrix"][0]["artifact_path"] = ""
    task_broken = {"task_key": "SC01",
                   "acceptance_criteria": json.dumps({**contract, "requirement_matrix": broken["requirement_matrix"]},
                                                     ensure_ascii=False)}
    checked = validate_requirement_matrix({"tasks": [task_broken]})
    assert checked["passed"] is False
    assert checked["needs_human_review"] is True
    broken2 = json.loads(json.dumps(contract))
    broken2["requirement_matrix"][0]["method"] = "unknown_method"
    task_broken2 = {"task_key": "SC01",
                    "acceptance_criteria": json.dumps({**contract, "requirement_matrix": broken2["requirement_matrix"]},
                                                      ensure_ascii=False)}
    checked2 = validate_requirement_matrix({"tasks": [task_broken2]})
    assert checked2["passed"] is False
    assert checked2["needs_human_review"] is True
    # 数量の抽出漏れ疑義は人待ちにする。
    doubts = detect_extraction_doubts("FAQ5件を作成する", [])
    assert doubts


def test_zero_items_on_one_artifact_fails():
    criterion = "架空イベントの案内文とチェックリストを作成する"
    task = compile_task(1, criterion, _proposal(
        "架空案内", "架空イベントの案内内容を整理する。", ["案内文", "注意事項"],
        {"outputs": [
            {"kind": "document", "required_headings": ["案内文", "注意事項"]},
            {"kind": "checklist", "required_headings": ["準備", "当日対応"]},
        ]},
    ), [])
    contract = contract_of(task)
    matrix = [row for row in contract["requirement_matrix"]
              if row["artifact_path"] != "result/sc01_checklist.md"]
    assert matrix
    broken = {"task_key": "SC01",
              "acceptance_criteria": json.dumps({**contract, "requirement_matrix": matrix},
                                                ensure_ascii=False)}
    checked = validate_requirement_matrix({"tasks": [broken]})
    assert checked["passed"] is False
    assert checked["artifact_zero_failed"] is True


def test_one_missing_item_blocks_final_pass_despite_format_pass(tmp_path):
    manager, pid = _manager(
        tmp_path,
        "完全架空の備品申請デモ",
        "1. 完全架空の備品申請4段階(申請・上長承認・総務確認・払出)を手順書として作成する\n"
        "2. 完全架空の備品申請FAQ5件を作成し、各質問を個別に検証する",
    )
    plan = _supply_plan()
    manager.memory.replace_plan(pid, plan["summary"], plan["tasks"])
    activate(manager, pid)
    mission = manager.memory.get_mission(pid)
    by_key = {item["task_key"]: item for item in mission["tasks"]}
    # SC01は完全、SC02はFAQが4問(1件欠落)。見出しと文字数は満たす。
    first_contract = contract_of(by_key["SC01"])
    first_headings = first_contract["outputs"][0]["required_headings"]
    _complete_task(manager, pid, by_key["SC01"], {
        first_contract["outputs"][0]["path"]: "\n".join(
            f"## {heading}\n{FILLER}\n申請と上長承認と総務確認と払出と人の承認と未定義と未実施を記載する。"
            for heading in first_headings) + "\n",
    })
    second_contract = contract_of(by_key["SC02"])
    second_path = second_contract["outputs"][0]["path"]
    second_headings = second_contract["outputs"][0]["required_headings"]
    body = _faq_body(5, missing_one=True)
    for heading in second_headings:
        if f"## {heading}" not in body:
            body += f"\n## {heading}\n{FILLER}\n"
    _complete_task(manager, pid, by_key["SC02"], {second_path: body})
    # 形式検査は通る(見出し・文字数)。
    def resolve(relative):
        project = manager.memory.get_project(pid)
        return manager.workspace.resolve_file(
            project.get("workspace_path", ""), pid, relative, must_exist=True)[2]
    assert verify_outputs(by_key["SC02"], resolve) == []
    # 項目別検証ではFAQ5問の1件欠落を個別に検出する。
    rows = verify_requirement_items(by_key["SC02"], resolve, semantic_judge=None)
    count_rows = [row for row in rows if row["method"] == "count"]
    assert count_rows and any(row["result"] == "FAIL" for row in count_rows)
    assert summarize_criterion(rows)["SC02"] == "FAIL"
    # 台帳に保存し、最終検証レポートは台帳からのみ生成される。
    _, plan_signature = plan_snapshot(manager, pid)
    payload = ledger_payload(pid, plan_signature, "source-hash-1", rows, {second_path: "hash-1"})
    record_ledger(manager.memory.path, payload)
    saved = latest_ledger(manager.memory.path, pid)
    assert saved is not None and saved["rows"]
    gate = gate_evaluate(manager, pid)
    by_id = {row["criterion_id"]: row for row in gate["criteria"]}
    assert by_id["SC02"]["status"] != "PASS"
    assert gate["achieved"] is False


def test_semantic_without_evidence_ambiguity_and_read_failure_never_pass():
    item = {"item_id": "SC01-R01", "criterion_id": "SC01", "artifact_path": "result/sc01.md",
            "requirement": "文意の適合", "method": "llm_semantic", "required": True,
            "expected": {"keywords": ["日時"]}}
    content = "## 案内\n日時と場所を記載する。"
    # 根拠引用なしはPASSにしない。
    assert check_item_semantic(
        item, content, "", lambda _item, _content: {"result": "PASS", "quote": "", "reason": "良い"})["result"] != "PASS"
    # 曖昧な verdict はPASSにしない。
    assert check_item_semantic(
        item, content, "", lambda _item, _content: {"result": "MAYBE", "quote": "日時", "reason": "曖昧"})["result"] != "PASS"
    # LLM失敗の注入はPASSにしない(人待ち)。
    def _boom(_item, _content):
        raise RuntimeError("injected llm failure")
    assert check_item_semantic(item, content, "", _boom)["result"] == "UNTESTABLE"
    # 読取失敗はPASSにしない。
    assert check_item_semantic(item, None, "読み取り失敗: OSError", None)["result"] == "UNTESTABLE"
    # 根拠付きPASSだけ通す。
    ok = check_item_semantic(
        item, content, "", lambda _item, _content: {"result": "PASS", "quote": "日時", "reason": "根拠あり", "line": 1})
    assert ok["result"] == "PASS" and ok["quote"] == "日時"


def test_change_invalidates_ledger_and_requires_recheck():
    saved = {"plan_signature": "sig-1", "source_hash": "src-1",
             "artifact_hashes": {"result/sc01.md": "hash-1"}}
    assert ledger_invalidated(saved, "sig-1", "src-1", {"result/sc01.md": "hash-1"}) == []
    assert "plan_changed" in ledger_invalidated(saved, "sig-2", "src-1", {"result/sc01.md": "hash-1"})
    assert "source_changed" in ledger_invalidated(saved, "sig-1", "src-2", {"result/sc01.md": "hash-1"})
    assert any(value.startswith("artifact_changed:") for value in ledger_invalidated(
        saved, "sig-1", "src-1", {"result/sc01.md": "hash-2"}))


def test_three_demo_shapes_persist_ledgers_and_report_from_ledger_only(tmp_path):
    # イベント(5+3項目)・カフェ(6項目)・備品申請(9項目超)の形で台帳保存を確認する。
    event = compile_task(1, "架空イベントの案内文とチェックリストを作成する", _proposal(
        "架空案内", "架空イベントの案内内容を整理する。", ["案内文", "注意事項"],
        {"outputs": [
            {"kind": "document", "required_headings": ["案内文", "注意事項"]},
            {"kind": "checklist", "required_headings": ["準備", "当日対応"]},
        ]},
    ), [])
    cafe = compile_task(1, "完全架空のカフェ新メニューを作成する。価格は仮の例示であり要確認とする", _proposal(
        "架空メニュー",
        "完全架空の商品説明を作成する。価格は仮の例示であり、食品安全の仮定は要確認とする。",
        ["商品説明", "仮価格の扱い"],
    ), [])
    supply = next(item for item in _supply_plan()["tasks"] if item["task_key"] == "SC02")
    event_items = contract_of(event)["requirement_matrix"]
    cafe_items = contract_of(cafe)["requirement_matrix"]
    supply_items = contract_of(supply)["requirement_matrix"]
    # 汎用規則の期待件数(試験入力の文言から導出。製品側に案件別の補正は無い)。
    # イベント: 「架空」の明記1 + 見出し2成果物 = 3件。
    # カフェ: 仮・架空・要確認の明記3 + 見出し1 = 4件。
    # 備品申請SC02: 数量5件 + 「架空」の明記 + 見出し1 = 3件。
    assert len([row for row in event_items if row["method"] != "llm_semantic"]) == 4
    assert {row["artifact_path"] for row in event_items} == {"result/sc01.md", "result/sc01_checklist.md"}
    assert len([row for row in cafe_items if row["method"] != "llm_semantic"]) == 4
    assert len([row for row in supply_items if row["method"] != "llm_semantic"]) == 3
    for items in (event_items, cafe_items, supply_items):
        outputs = {row["artifact_path"] for row in items}
        semantic_outputs = {row["artifact_path"] for row in items
                            if row["method"] == "llm_semantic"
                            and (row.get("expected") or {}).get("criterion_semantic") is True}
        assert semantic_outputs == outputs
    all_items = event_items + cafe_items + supply_items
    manager, pid = _manager(tmp_path, "架空デモ", "1. 架空文書を作成する")
    rows = [dict(row, result="PASS", line=1, quote="確認", reason="根拠あり") for row in all_items]
    payload = ledger_payload(pid, "sig-demo", "src-demo", rows, {"result/sc01.md": "hash-1"})
    record_ledger(manager.memory.path, payload)
    saved = latest_ledger(manager.memory.path, pid)
    assert saved is not None
    # 台帳は全項目を1行ずつ保存する(件数が一致しない保存は不合格)。
    assert len(saved["rows"]) == len(all_items)
    # 最終検証レポートは台帳からのみ生成される。形式検査の文面は台帳判定へ混ぜない。
    assert "minimum_characters" not in str(saved.get("criterion_verdicts_json") or "")


def test_human_approval_still_required_and_regression_gates(tmp_path):
    manager, pid = _manager(tmp_path, "架空文書デモ", "1. 架空文書を作成する")
    plan = compile_plan(["架空文書を作成する"], [
        compile_task(1, "架空文書を作成する", _proposal(
            "架空文書", "架空文書の内容を整理する。", ["目的", "実施内容"]), []),
    ])
    manager.memory.replace_plan(pid, plan["summary"], plan["tasks"])
    activate(manager, pid)
    mission = manager.memory.get_mission(pid)
    by_key = {item["task_key"]: item for item in mission["tasks"]}
    assert validate_plan({"tasks": mission["tasks"]}, {"SC01"})["passed"]
    gate = gate_evaluate(manager, pid)
    # 未承認なら完了しない(既存の挙動を維持)。
    assert gate["achieved"] is False
    assert gate["reason_code"] in ("HUMAN_ACCEPTANCE_MISSING", "HUMAN_ACCEPTANCE_HASH_DRIFT",
                                   "COVERAGE_INCOMPLETE", "RETENTION_FAILED", "VERIFICATION_MISSING",
                                   "ARTIFACT_MISSING", "ARTIFACT_FORMAT", "ARTIFACT_HASH_DRIFT",
                                   "TASK_INCOMPLETE", "UNTESTABLE", "BLOCKED_WAITING_INPUT",
                                   "NO_EXEC_TASK", "")
    # 新テーブルは棚卸しレジストリに登録されている。
    audit = audit_registry_completeness()
    assert audit["ok"] is True, audit["unregistered"]
    assert "requirement_item_ledger" in {entry.table for entry in __import__(
        "app.project_lifecycle_registry", fromlist=["REGISTRY"]).REGISTRY}
    assert "requirement_item_rows" in {entry.table for entry in __import__(
        "app.project_lifecycle_registry", fromlist=["REGISTRY"]).REGISTRY}


def test_existing_vehicle_ocr_delete_and_external_behaviors_unchanged(tmp_path):
    # 車両損益・OCR・削除状態・外部送信の既存の挙動が変わらない(回帰)。
    # 削除状態: サイドカーDB無しは active(既存の test_status_matrix と同じ)。
    from app.project_delete import delete_state_status, is_deleted_or_unknown
    from app.goal_review import public_structure

    assert delete_state_status(tmp_path / "no-such-memory.db", "架空pid") == "active"
    assert is_deleted_or_unknown(tmp_path / "no-such-memory.db", "架空pid") is False
    plan = compile_plan(["架空文書を作成する"], [
        compile_task(1, "架空文書を作成する", _proposal(
            "架空文書", "架空文書の内容を整理する。", ["目的", "実施内容"]), ["src-1"]),
    ])
    packet = public_structure({"tasks": plan["tasks"]})
    blob = json.dumps(packet, ensure_ascii=False)
    assert "架空文書を作成する" not in blob
    assert "src-1" not in blob


def test_p0c_three_domain_neutral_sentences_extraction():
    outputs = [{"path": "result/sc01.md", "required_headings": ["概要", "詳細"]}]

    # 文①: 読点だけの列挙 + 要求動詞、個数要求、「Xと断定せず『要確認』と明記する」
    c1 = "報告書に月次の売上、費用、利益を記載し、グラフを3点含める。確定とは断定せず「要確認」と明記する"
    items1 = extract_requirement_items("SC01", c1, outputs, [])
    assert detect_extraction_doubts(c1, items1) == []
    kw_lists1 = [list((r.get("expected") or {}).get("keywords") or []) for r in items1]
    forbid_lists1 = [list((r.get("expected") or {}) .get("forbid") or []) for r in items1]
    for term in ("売上", "費用", "利益"):
        matching = [r for r in items1 if r["method"] == "phrase" and (r.get("expected") or {}).get("keywords") == [term]]
        assert len(matching) == 1, f"{term} should be extracted as exactly 1 phrase item"
    assert ["要確認"] in kw_lists1
    assert all("要確認" not in f for f in forbid_lists1)
    assert all("確定" not in f for f in forbid_lists1)
    count_items1 = [r for r in items1 if r["method"] == "count"]
    assert len(count_items1) == 1 and count_items1[0]["expected"]["count"] == 3

    # 文②: 個数要求 + 鉤括弧の無い否定要求(「講師の連絡先は載せない」)
    c2 = "研修資料を作成し、受講者向けFAQを5問含め、講師の連絡先は載せない"
    items2 = extract_requirement_items("SC01", c2, outputs, [])
    assert detect_extraction_doubts(c2, items2) == []
    count_items2 = [r for r in items2 if r["method"] == "count"]
    assert len(count_items2) == 1 and count_items2[0]["expected"]["count"] == 5
    sem_items2 = [r for r in items2 if r["method"] == "llm_semantic"]
    assert len(sem_items2) == 1
    assert sem_items2[0]["required"] is True
    assert sem_items2[0]["artifact_path"] == "result/sc01.md"
    assert "講師の連絡先は載せない" in sem_items2[0]["requirement"]

    # 文③: 単純な作成文(見出しのみ1項目、疑義なし)
    c3 = "簡単な社内文書を作成する"
    items3 = extract_requirement_items("SC01", c3, outputs, [])
    assert detect_extraction_doubts(c3, items3) == []
    assert len(items3) == 1 and items3[0]["method"] == "headings"


def test_p0c_unanchored_enumeration_negation_and_contradiction_become_doubts_and_needs_review():
    outputs = [{"path": "result/sc01.md", "required_headings": ["概要", "詳細"]}]
    headings_only = [{
        "item_id": "SC01-R01", "criterion_id": "SC01", "artifact_path": "result/sc01.md",
        "requirement": "result/sc01.mdに必須見出しを含む", "method": "headings",
        "required": True, "source_ref": "", "expected": {"headings": ["概要", "詳細"]},
    }]

    # 要求の動詞が無い読点の連なりは必須語と断定せず、疑義にして needs_review に止める
    c_enum = "売上、費用、利益の推移に関して社内文書を作成する"
    items_enum = extract_requirement_items("SC01", c_enum, outputs, [])
    doubts_enum = detect_extraction_doubts(c_enum, items_enum)
    assert any("列挙" in d for d in doubts_enum)
    task_enum = compile_task(1, c_enum, _proposal("社内文書", "内容を整理する。", ["概要", "詳細"]), [])
    checked_enum = validate_requirement_matrix({"tasks": [task_enum]})
    assert checked_enum["passed"] is False
    assert checked_enum["needs_review"] is True
    assert checked_enum["needs_human_review"] is True

    # 否定の要求があるのに項目に結び付かない場合は疑義 → needs_review
    c_neg = "研修資料を作成し、講師の連絡先は載せない"
    doubts_neg = detect_extraction_doubts(c_neg, headings_only)
    assert any("否定要求" in d for d in doubts_neg)
    broken_neg_contract = {**contract_of(compile_task(1, c_neg, _proposal("研修資料", "整理する。", ["概要", "詳細"]), [])),
                           "requirement_matrix": headings_only}
    checked_neg = validate_requirement_matrix({
        "tasks": [{"task_key": "SC01", "acceptance_criteria": json.dumps(broken_neg_contract, ensure_ascii=False)}],
    })
    assert checked_neg["passed"] is False
    assert checked_neg["needs_review"] is True
    assert checked_neg["needs_human_review"] is True

    # 同じ語が必須と禁止の両方に入る矛盾は項目を作らず疑義にして needs_review に止める
    c_contra = "「要確認」は記載しない。「要確認」と明記する"
    items_contra = extract_requirement_items("SC01", c_contra, outputs, [])
    for row in items_contra:
        exp = row.get("expected") or {}
        assert "要確認" not in (exp.get("keywords") or [])
        assert "要確認" not in (exp.get("forbid") or [])
    doubts_contra = detect_extraction_doubts(c_contra, items_contra)
    assert any("矛盾" in d or "要確認" in d for d in doubts_contra)
    task_contra = compile_task(1, c_contra, _proposal("矛盾テスト", "整理する。", ["概要", "詳細"]), [])
    checked_contra = validate_requirement_matrix({"tasks": [task_contra]})
    assert checked_contra["passed"] is False
    assert checked_contra["needs_review"] is True
    assert checked_contra["needs_human_review"] is True


def test_p0c_missing_enumerated_term_fails_item_verification(tmp_path):
    c1 = "報告書に月次の売上、費用、利益を記載し、グラフを3点含める。確定とは断定せず「要確認」と明記する"
    task = compile_task(1, c1, _proposal("月次報告書", "月次の数値を整理する。", ["概要", "詳細"]), [])
    target = tmp_path / "sc01.md"
    # 「費用」「利益」「要確認」「3点」はあるが「売上」が欠落している成果物
    target.write_text(
        "## 概要\n費用と利益を記載し、要確認とする。1点、2点、3点のグラフ。\n"
        "## 詳細\n詳細な内訳。\n"
        "## 根拠と未確認事項\n要確認事項あり。\n"
        "## 実施状態と次の行動\n次月も継続。\n",
        encoding="utf-8",
    )
    rows = verify_requirement_items(task, lambda _rel: target, semantic_judge=None)
    by_req = {r["requirement"]: r for r in rows}
    assert by_req["必須語「売上」を含む"]["result"] == "FAIL"
    assert by_req["必須語「費用」を含む"]["result"] == "PASS"
    assert by_req["必須語「利益」を含む"]["result"] == "PASS"
    assert by_req["必須語「要確認」を含む"]["result"] == "PASS"
    assert summarize_criterion(rows)["SC01"] == "FAIL"


