"""P0-A検証契約の正規化の受入試験。疑似PJ・実関数・外部通信なし。

実測と同じ形(案内文1ファイル+チェックリスト1ファイル、達成条件に『案内文には日時』
『チェックリストには準備』『終了後確認を含める』)で検証する。特定案件の語彙は
試験入力にだけ置き、製品コードには入れない。
"""
from __future__ import annotations

import json

from app.memory.short_term import ShortTermMemory
from app.project_manager import ProjectOrchestrator
from app.requirement_matrix import (
    extract_requirement_items,
    is_p0a_requirement_sentence_keyword,
    normalize_p0a_items,
    p0a_contract_issues,
)
from app.requirement_verification import verify_requirement_items
from app.structured_planning import compile_task, contract_of
from app.workspace_files import WorkspaceSandbox


REGISTERED = "開催日: 2026年11月14日 開始時刻10:00 会場: 架空ホール"
CRITERION = (
    "案内文1ファイルとチェックリスト1ファイル、合計2ファイルを作る。"
    "案内文には日時を含める。チェックリストには準備・終了後確認を含める。"
)


def _proposal(extra=None):
    row = {"title": "疑似案内", "scope": "疑似の案内と手順を整理する。",
           "headings": ["概要", "詳細"], "depends_on": [],
           "outputs": [
               {"kind": "document",
                "required_headings": ["案内文：イベント概要と参加方法", "案内文：持ち物と問い合わせ先"]},
               {"kind": "checklist",
                "required_headings": ["チェックリスト：準備・受付・終了後の手順", "根拠と未確認事項"]},
           ]}
    if extra:
        row.update(extra)
    return row


def _matrix(registered=REGISTERED):
    task = compile_task(1, CRITERION, _proposal(),
                        [], registered_text=registered)
    return contract_of(task)


def test_p0a_datetime_grounded_and_paths_split():
    contract = _matrix()
    matrix = contract["requirement_matrix"]
    outs = [o for o in contract["outputs"] if o["path"].endswith(".md")]
    doc = "result/sc01.md"
    chk = "result/sc01_checklist.md"
    assert {o["path"] for o in outs} == {doc, chk}
    # (a) 日時は役割別の項目に分かれ、原本値と許容表記を明示する。
    # 開催日と開始時刻は別項目で、match:any・対象パスが保存される。
    dated = [r for r in matrix
             if r["method"] == "phrase"
             and (r.get("expected") or {}).get("datetime_role") == "event_date"]
    timed = [r for r in matrix
             if r["method"] == "phrase"
             and (r.get("expected") or {}).get("datetime_role") == "start_time"]
    assert dated and timed, matrix
    assert dated[0]["artifact_path"] == doc
    assert (dated[0].get("expected") or {}).get("match") == "any"
    assert any(str(v) in ("2026年11月14日", "2026/11/14", "11/14", "11月14日")
               for v in list((dated[0].get("expected") or {}).get("keywords") or []))
    assert (timed[0].get("expected") or {}).get("match") == "any"
    # (b) 準備・終了後確認はチェックリストパスを対象にする。
    preps = [r for r in matrix
             if r["method"] == "phrase"
             and list((r.get("expected") or {}).get("keywords") or []) == ["準備"]]
    ends = [r for r in matrix
            if r["method"] == "headings"
            and any("終了後" in str(k) for k in
                    list((r.get("expected") or {}).get("headings") or []))]
    assert preps and ends, matrix
    assert all(r["artifact_path"] == chk for r in preps + ends)
    # (c) 要求文そのものを必須語句にしない。
    for r in matrix:
        if r["method"] != "phrase":
            continue
        for k in list((r.get("expected") or {}).get("keywords") or []):
            assert not is_p0a_requirement_sentence_keyword(str(k), CRITERION), (r, k)
    assert p0a_contract_issues(CRITERION, matrix, outs) == []
    # 文意項目の挙動は変えない(担当範囲の条件が残る)。
    assert any(r["method"] == "llm_semantic" for r in matrix)


def test_p0a_missing_concrete_value_is_input_missing():
    contract = _matrix(registered="開催日: 未定 会場: 架空ホール")
    missing = [r for r in contract["requirement_matrix"] if r["method"] == "input_missing"]
    assert missing, contract["requirement_matrix"]
    for r in missing:
        expected = r.get("expected") or {}
        assert str(expected.get("reason") or "")
        assert expected.get("needed_inputs")
    # 推測の具体値で契約を作らない。
    for r in contract["requirement_matrix"]:
        if r["method"] != "phrase":
            continue
        for k in list((r.get("expected") or {}).get("keywords") or []):
            assert k not in ("日時", "案内文には日時")


def test_p0a_wrong_target_is_rejected_before_save():
    contract = _matrix()
    outs = [o for o in contract["outputs"] if o["path"].endswith(".md")]
    matrix = [dict(r) for r in contract["requirement_matrix"]]
    assert matrix
    victim = next(r for r in matrix if r["method"] == "phrase")
    victim["artifact_path"] = ("result/sc01_checklist.md"
                               if victim["artifact_path"] == "result/sc01.md"
                               else "result/sc01.md")
    issues = p0a_contract_issues(CRITERION, matrix, outs)
    assert any("wrong_target" in i for i in issues), issues
    # 限定再生成: 2回でも不一致なら保存しない。
    from app.structured_planning import ensure_p0a_item_target_consistency
    result = ensure_p0a_item_target_consistency(
        [CRITERION], [{"task_key": "SC01",
                       "acceptance_criteria": json.dumps(
                           {**contract, "requirement_matrix": matrix},
                           ensure_ascii=False)}],
        lambda missing: {"tasks": [{"task_key": "SC01",
                                    "acceptance_criteria": json.dumps(
                                        {**contract, "requirement_matrix": matrix},
                                        ensure_ascii=False)}]},
        max_attempts=2)
    assert result["saved"] is False
    assert result["attempts_used"] == 2
    # 正規化で直る取り違えは1回で保存できる。
    raw = [dict(r) for r in contract["requirement_matrix"]]
    assert raw
    raw[0]["artifact_path"] = "result/sc01.md"
    fixed, _report = normalize_p0a_items("SC01", CRITERION, raw, outs, REGISTERED)
    good = ensure_p0a_item_target_consistency(
        [CRITERION], [{"task_key": "SC01",
                       "acceptance_criteria": json.dumps(
                           {**contract, "requirement_matrix": fixed},
                           ensure_ascii=False)}],
        lambda missing: (_ for _ in ()).throw(AssertionError("no regen expected")))
    assert good["saved"] is True


def test_p0a_requirement_sentence_keyword_rejected_short_values_allowed():
    contract = _matrix()
    outs = [o for o in contract["outputs"] if o["path"].endswith(".md")]
    matrix = [dict(r) for r in contract["requirement_matrix"]]
    bad = dict(matrix[0])
    bad["item_id"] = "SC01-R99"
    bad["method"] = "phrase"
    bad["expected"] = {"keywords": ["案内文には日時を含める"]}
    issues = p0a_contract_issues(CRITERION, matrix + [bad], outs)
    assert any("requirement sentence must not be a keyword" in i for i in issues)
    # 短い具体値(日時・固有名)は拒否しない。
    assert not is_p0a_requirement_sentence_keyword("10:00", CRITERION)
    assert not is_p0a_requirement_sentence_keyword("2026年11月14日", CRITERION)
    assert not is_p0a_requirement_sentence_keyword("架空", CRITERION)


def test_p0a_missing_content_stays_fail_and_correct_passes(tmp_path):
    contract = _matrix()
    task = {"task_key": "SC01",
            "acceptance_criteria": json.dumps(contract, ensure_ascii=False)}
    project_dir = tmp_path / "ws"
    project_dir.mkdir()
    (project_dir / "result").mkdir()
    good_doc = ("# 案内\n開催日: 2026年11月14日 10:00\n"
                "## 案内文：イベント概要と参加方法\n日時と場所を記載する。\n"
                "## 案内文：持ち物と問い合わせ先\n持ち物と架空の問い合わせ先を記載する。\n"
                "## 根拠と未確認事項\n架空と要確認と想定。\n"
                "## 実施状態と次の行動\n実施状態と次の行動。\n")
    good_chk = ("# チェックリスト\n準備と受付と終了後確認を記載する。\n"
                "## チェックリスト：準備・受付・終了後の手順\n準備と受付と終了後確認。\n"
                "## 根拠と未確認事項\n架空と要確認と想定。\n"
                "## 実施状態と次の行動\n実施状態と次の行動。\n")
    (project_dir / "result" / "sc01.md").write_text(good_doc, encoding="utf-8")
    (project_dir / "result" / "sc01_checklist.md").write_text(good_chk, encoding="utf-8")

    def resolve(rel):
        target = project_dir / rel
        if not target.is_file():
            raise FileNotFoundError(rel)
        return target

    rows = verify_requirement_items(task, resolve, semantic_judge=None)
    dated = next(r for r in rows if "2026年11月14日" in str((r.get("quote") or "")))
    assert dated["result"] == "PASS"
    # 必須内容が欠けた反例では FAIL のまま(検査を緩めて通さない)。
    bad_chk = good_chk.replace("準備", "xxxx").replace("終了後確認", "yyyy")
    (project_dir / "result" / "sc01_checklist.md").write_text(bad_chk, encoding="utf-8")
    rows2 = verify_requirement_items(task, resolve, semantic_judge=None)
    fails = [r for r in rows2 if r["result"] == "FAIL"]
    assert fails, rows2


def test_p0a_plan_save_verify_ledger_chain(tmp_path):
    """計画生成(フェイクLLM)→保存→項目別検証→最終検証書まで届く。外部通信なし。"""
    import json as _json

    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("疑似P0-A")
    memory.save_mission(project["id"],
                        "## 達成条件\n1. " + CRITERION,
                        "", "", False, [])

    class FakeLlm:
        async def stream(self, messages):
            yield _json.dumps({"title": "疑似案内", "scope": "疑似の案内と手順を整理する。",
                               "headings": ["概要", "詳細"], "depends_on": [],
                               "outputs": [
                                   {"kind": "document",
                                    "required_headings": ["案内文：イベント概要と参加方法",
                                                          "案内文：持ち物と問い合わせ先"]},
                                   {"kind": "checklist",
                                    "required_headings": ["チェックリスト：準備・受付・終了後の手順",
                                                          "根拠と未確認事項"]},
                               ]}, ensure_ascii=False)

    def _static(_project):
        return (REGISTERED, [{"id": "ctx1", "filename": "registered.md",
                              "content": REGISTERED, "file_kind": "markdown",
                              "size_bytes": len(REGISTERED.encode()), "source": "registered",
                              "char_count": len(REGISTERED)}])

    manager = ProjectOrchestrator(
        memory, FakeLlm(), _static, None, lambda: [],
        workspace=WorkspaceSandbox(tmp_path / "workspace"))
    import asyncio as _asyncio
    mission = _asyncio.run(manager.generate_plan(project["id"]))
    sc = next(t for t in mission["tasks"] if t["task_key"] == "SC01")
    contract = contract_of(sc)
    matrix = contract["requirement_matrix"]
    assert any("2026年11月14日" in str((r.get("expected") or {}).get("keywords") or [])
               for r in matrix if r["method"] == "phrase")
    assert all(r["artifact_path"] in ("result/sc01.md", "result/sc01_checklist.md")
               for r in matrix if r["method"] == "phrase")
    # 保存された計画の契約が同じ内容である。
    saved = memory.get_mission(project["id"])
    saved_matrix = contract_of(next(t for t in saved["tasks"] if t["task_key"] == "SC01"))["requirement_matrix"]
    assert saved_matrix == matrix
    # 項目別検証→台帳→最終検証書の内容まで届く。
    for path, body in (("result/sc01.md",
                        "# 案内\n開催日: 2026年11月14日 10:00\n"
                        "## 案内文：イベント概要と参加方法\n日時と場所。\n"
                        "## 案内文：持ち物と問い合わせ先\n持ち物と架空の問い合わせ先。\n"
                        "## 根拠と未確認事項\n架空と要確認と想定。\n"
                        "## 実施状態と次の行動\n実施状態と次の行動。\n"),
                       ("result/sc01_checklist.md",
                        "# チェックリスト\n準備と受付と終了後確認。\n"
                        "## チェックリスト：準備・受付・終了後の手順\n準備と受付と終了後確認。\n"
                        "## 根拠と未確認事項\n架空と要確認と想定。\n"
                        "## 実施状態と次の行動\n実施状態と次の行動。\n")):
        proj = manager.memory.get_project(project["id"])
        target = manager.workspace.resolve_file(
            proj.get("workspace_path", ""), project["id"], path)[2]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")

    def resolve(rel):
        proj = manager.memory.get_project(project["id"])
        return manager.workspace.resolve_file(
            proj.get("workspace_path", ""), project["id"], rel, must_exist=True)[2]

    rows = verify_requirement_items(sc, resolve, semantic_judge=None)
    assert any("2026年11月14日" in str(r.get("quote") or "") and r["result"] == "PASS"
               for r in rows)
    from app.requirement_item_ledger import record_ledger
    from app.requirement_verification import ledger_payload
    record_ledger(manager.memory.path, ledger_payload(project["id"], "sig", "src", rows,
                                                      {"result/sc01.md": "h1",
                                                       "result/sc01_checklist.md": "h2"}))
    assert "2026年11月14日" in str(saved_matrix)
    # LLM出力の語句で保存値を上書きさせない: 規則で決めた対象パスが残る。
    assert all(r["artifact_path"] in ("result/sc01.md", "result/sc01_checklist.md")
               for r in matrix)


def test_p0a_quoted_role_datetime_is_covered_only_when_grounded():
    from app.requirement_matrix import detect_extraction_doubts

    criterion = "「案内文には日時」を含める。"
    row = {
        "item_id": "SC01-R01", "method": "phrase", "artifact_path": "result/sc01.md",
        "requirement": "原本にある開催日の具体値を含む",
        "expected": {"keywords": ["2026年11月14日"], "grounded_concept": "日時"},
    }
    assert "語句の抽出漏れの疑義: 案内文には日時" not in detect_extraction_doubts(criterion, [row])
    ungrounded = {**row, "expected": {"keywords": ["2026年11月14日"]}}
    assert "語句の抽出漏れの疑義: 案内文には日時" in detect_extraction_doubts(criterion, [ungrounded])
