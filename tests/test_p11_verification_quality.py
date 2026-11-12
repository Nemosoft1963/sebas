"""P1-1 検証品質の受入試験。疑似PJ・実関数・フェイクLLM。外部通信なし。

- 実走の形(案内文とチェックリストの2成果物、日時・問い合わせ先・終了後確認)で
  項目の生成→検証→台帳→最終検証書まで保存結果を検査する。
- 日時の各項目は役割別に PASS。開始が欠ければその項目だけ FAIL。
  別の成果物の時刻で通らない。『いずれか』は ANY、『全て』は ALL。
- 『問い合わせ先を含め』『終了後確認を含める』は分解され、登録メールと
  『終了後確認』見出しがあれば PASS、欠ければ FAIL。原本に無ければ入力不足。
- P0-C: 『午後3時』と原本の 15:00 は同一、『一部の方は…』『二人で…』は
  人数確約にしない。一方、原本に無い『保証します』『20歳以上』『返却します』
  『無料で提供します』は要確認(UNTESTABLE)のまま。
- 文意判定の例外(タイムアウト、形式不正、実在しない引用)は UNTESTABLE で、
  種別・項目ID・成果物パスが記録され、秘密・原本本文が含まれない。
- 特定案件の語彙は試験入力にだけ置き、製品コードに入れない。
"""
from __future__ import annotations

import pytest

from app.memory.short_term import ShortTermMemory
from app.project_manager import ProjectOrchestrator
from app.requirement_item_ledger import latest_ledger, record_ledger
from app.requirement_verification import (
    check_item_semantic,
    check_p0c_artifact_grounding,
    ledger_payload,
    verify_requirement_items,
)
from app.structured_planning import compile_task, contract_of
from app.workspace_files import WorkspaceSandbox


REGISTERED = (
    "開催日: 2026年11月14日 開始時刻10:00 終了時刻15:00 準備開始時刻9:30 "
    "会場: 架空ホール 連絡先: test-p11@example.com"
)
CRITERION = (
    "案内文1ファイルとチェックリスト1ファイル、合計2ファイルを作る。"
    "案内文には開催日・開始時刻・終了時刻を含める。チェックリストには準備開始時刻を含める。"
    "案内文には問い合わせ先を含め、チェックリストには終了後確認を含める。"
)


def _proposal():
    return {"title": "疑似案内", "scope": "疑似の案内と手順を整理する。",
            "headings": ["概要", "詳細"], "depends_on": [],
            "outputs": [
                {"kind": "document",
                 "required_headings": ["案内文：イベント概要と参加方法",
                                       "案内文：持ち物と問い合わせ先"]},
                {"kind": "checklist",
                 "required_headings": ["チェックリスト：準備・受付・終了後の手順",
                                       "根拠と未確認事項"]},
            ]}


def _manager(tmp_path, registered=REGISTERED):
    memory = ShortTermMemory(tmp_path / "memory.db")
    project = memory.create_project("疑似P1-1")
    memory.save_mission(project["id"], "## 達成条件\n1. " + CRITERION, "", "", False, [])
    data = registered.encode("utf-8")
    memory.add_context_file(project["id"], "registered.md", registered, len(data),
                            data, "text/markdown", "markdown", "疑似原本",
                            "", source="registered")
    workspace = WorkspaceSandbox(tmp_path / "workspace")

    def _static(_project):
        return (registered, [{"id": "ctx1", "filename": "registered.md",
                              "content": registered, "file_kind": "markdown",
                              "size_bytes": len(registered.encode()),
                              "source": "registered",
                              "char_count": len(registered)}])

    manager = ProjectOrchestrator(
        memory, None, _static, None, lambda: [], workspace=workspace)
    return manager, project["id"]


def _bind(manager, pid, registered=REGISTERED):
    from app.structured_planning import compile_plan

    task = compile_task(1, CRITERION, _proposal(), [], registered_text=registered)
    plan = compile_plan([CRITERION], [task])
    manager.memory.replace_plan(pid, plan["summary"], plan["tasks"])
    return next(t for t in manager.memory.get_mission(pid)["tasks"]
                if t["task_key"] == "SC01")


def _write(manager, pid, path, body):
    project = manager.memory.get_project(pid)
    target = manager.workspace.resolve_file(
        project.get("workspace_path", ""), pid, path)[2]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    return target


def _resolve(manager, pid):
    project = manager.memory.get_project(pid)

    def _inner(relative):
        return manager.workspace.resolve_file(
            project.get("workspace_path", ""), pid, relative, must_exist=True)[2]

    return _inner


_GOOD_DOC = (
    "# 案内\n開催日: 2026年11月14日 午前10時開始、午後3時終了\n"
    "## 案内文：イベント概要と参加方法\n開催日と開始と終了を記載する。\n"
    "## 案内文：持ち物と問い合わせ先\n問い合わせはtest-p11@example.comへ。\n"
    "## 根拠と未確認事項\n架空と要確認と想定。\n"
    "## 実施状態と次の行動\n実施状態と次の行動。\n"
)
_GOOD_CHK = (
    "# チェックリスト\n9:30から準備する。\n"
    "## チェックリスト：準備・受付・終了後の手順\n準備開始と受付と終了後確認。\n"
    "## 根拠と未確認事項\n架空と要確認と想定。\n"
    "## 実施状態と次の行動\n実施状態と次の行動。\n"
    "## 終了後確認\n終了後の確認手順。\n"
)


def test_datetime_roles_pass_and_missing_start_only_that_item_fails(tmp_path):
    manager, pid = _manager(tmp_path)
    task = _bind(manager, pid)
    roles = {(r.get("expected") or {}).get("datetime_role")
             for r in contract_of(task)["requirement_matrix"]
             if r["method"] == "phrase"}
    assert {"event_date", "start_time", "end_time", "prep_time"} <= roles
    _write(manager, pid, "result/sc01.md", _GOOD_DOC)
    _write(manager, pid, "result/sc01_checklist.md", _GOOD_CHK)
    rows = verify_requirement_items(task, _resolve(manager, pid), semantic_judge=None)
    dated = {r["requirement"]: r for r in rows if r["method"] == "phrase"
             and (r.get("expected") or {}).get("datetime_role")}
    assert dated and all(r["result"] == "PASS" for r in dated.values()), dated
    # 開始時刻が欠けた反例ではその項目だけ FAIL。
    bad_doc = _GOOD_DOC.replace("午前10時開始", "開始時刻は未定")
    _write(manager, pid, "result/sc01.md", bad_doc)
    rows2 = verify_requirement_items(task, _resolve(manager, pid), semantic_judge=None)
    start_rows = [r for r in rows2 if (r.get("expected") or {}).get("datetime_role") == "start_time"]
    assert start_rows and all(r["result"] == "FAIL" for r in start_rows)
    others = [r for r in rows2 if (r.get("expected") or {}).get("datetime_role") in
              ("event_date", "end_time", "prep_time")]
    assert others and all(r["result"] == "PASS" for r in others), rows2


def test_other_artifact_time_does_not_pass(tmp_path):
    manager, pid = _manager(tmp_path)
    task = _bind(manager, pid)
    # 開始時刻がチェックリスト側にだけあり、案内文側に無い場合は FAIL。
    doc = _GOOD_DOC.replace("午前10時開始", "開始時刻は未定")
    chk = _GOOD_CHK.replace("9:30から準備する。", "9:30から準備し、午前10時に開始する。")
    _write(manager, pid, "result/sc01.md", doc)
    _write(manager, pid, "result/sc01_checklist.md", chk)
    rows = verify_requirement_items(task, _resolve(manager, pid), semantic_judge=None)
    start_rows = [r for r in rows if (r.get("expected") or {}).get("datetime_role") == "start_time"]
    assert start_rows and all(r["result"] == "FAIL" for r in start_rows), rows


def test_any_contract_is_any_and_all_contract_requires_all(tmp_path):
    manager, pid = _manager(tmp_path)
    task_any = compile_task(1, "案内文に日時(10:00、15:00のいずれか)を含める。",
                            {"title": "疑似", "scope": "疑似の整理。",
                             "headings": ["概要", "詳細"], "depends_on": [],
                             "outputs": [{"kind": "document",
                                           "required_headings": ["概要", "詳細"]}]},
                            [], registered_text="開始時刻10:00 終了時刻15:00")
    matrix_any = contract_of(task_any)["requirement_matrix"]
    # 『いずれか』は1項目の ANY 契約になる。
    any_rows = [r for r in matrix_any if (r.get("expected") or {}).get("match") == "any"]
    assert len(any_rows) == 1 and len(any_rows[0]["expected"]["keywords"]) >= 2
    _write(manager, pid, "result/sc01.md",
           "# 案内\n10:00だけ記載する。\n## 概要\n概要。\n## 詳細\n詳細。\n"
           "## 根拠と未確認事項\n架空。\n## 実施状態と次の行動\n行動。\n")
    rows = verify_requirement_items(task_any, _resolve(manager, pid), semantic_judge=None)
    assert any(r["result"] == "PASS" for r in rows), rows
    # 『全て』は全て要求する(従来の all 契約)。
    _write(manager, pid, "result/sc01.md",
           "# 案内\n10:00だけ記載する。\n## 概要\n概要。\n## 詳細\n詳細。\n"
           "## 根拠と未確認事項\n架空。\n## 実施状態と次の行動\n行動。\n")
    row = {"item_id": "SC01-R01", "criterion_id": "SC01", "artifact_path": "result/sc01.md",
           "requirement": "必須語", "method": "phrase", "required": True,
           "expected": {"keywords": ["10:00", "15:00"], "match": "all"}}
    task_all = dict(task_any)
    import json as _json
    contract = contract_of(task_any)
    contract["requirement_matrix"] = [row]
    task_all["acceptance_criteria"] = _json.dumps(contract, ensure_ascii=False)
    rows_all = verify_requirement_items(task_all, _resolve(manager, pid), semantic_judge=None)
    assert rows_all and rows_all[0]["result"] == "FAIL"


def test_contact_and_heading_decomposed(tmp_path):
    manager, pid = _manager(tmp_path)
    task = _bind(manager, pid)
    matrix = contract_of(task)["requirement_matrix"]
    # 要求節そのものは必須フレーズにならない。
    for row in matrix:
        if row["method"] != "phrase":
            continue
        for keyword in list((row.get("expected") or {}).get("keywords") or []):
            assert "問い合わせ先を含め" not in str(keyword)
            assert "終了後確認を含める" not in str(keyword)
    # 登録メールと終了後確認見出しの分解項目がある。
    assert any("test-p11@example.com" in str((r.get("expected") or {}).get("keywords") or [])
               for r in matrix if r["method"] == "phrase")
    assert any(r["method"] == "headings"
               and "終了後確認" in list((r.get("expected") or {}).get("headings") or [])
               for r in matrix)
    _write(manager, pid, "result/sc01.md", _GOOD_DOC)
    _write(manager, pid, "result/sc01_checklist.md", _GOOD_CHK)
    rows = verify_requirement_items(task, _resolve(manager, pid), semantic_judge=None)
    contact = [r for r in rows if "test-p11@example.com" in str((r.get("expected") or {}).get("keywords") or [])]
    assert contact and all(r["result"] == "PASS" for r in contact)
    # 見出しが欠ければ FAIL。
    bad_chk = _GOOD_CHK.replace("## 終了後確認\n終了後の確認手順。\n", "")
    _write(manager, pid, "result/sc01_checklist.md", bad_chk)
    rows2 = verify_requirement_items(task, _resolve(manager, pid), semantic_judge=None)
    heading = [r for r in rows2 if r["method"] == "headings"
               and "終了後確認" in list((r.get("expected") or {}).get("headings") or [])]
    assert heading and all(r["result"] == "FAIL" for r in heading)


def test_contact_missing_source_is_input_missing():
    task = compile_task(1, "案内文には問い合わせ先を含める。",
                        {"title": "疑似", "scope": "疑似の整理。",
                         "headings": ["概要", "詳細"], "depends_on": [],
                         "outputs": [{"kind": "document",
                                       "required_headings": ["概要", "詳細"]}]},
                        [], registered_text="案内を作成する")
    missing = [r for r in contract_of(task)["requirement_matrix"]
               if r["method"] == "input_missing"]
    assert missing and any("問い合わせ先" in str(r.get("requirement") or "") for r in missing)


def test_p0c_time_notation_and_numeral_and_proper_are_not_false_positives():
    # 時刻の別表記は同一とみなす。
    ok = check_p0c_artifact_grounding("午後3時に終了します。\n", "終了時刻15:00\n")
    assert ok["checked"] is True and ok["passed"] is True
    # 数詞の語義: 部分・序数・慣用は人数確約にしない。
    ok2 = check_p0c_artifact_grounding("一部の方は着席してください。二人で確認してください。\n",
                                       "案内を作成する\n")
    assert ok2["checked"] is True
    assert not [r for r in ok2["unsupported_assertions"] if r["kind"] == "number"], ok2
    # 固有名の誤抽出: 普通名詞の一部は proper にしない。
    ok3 = check_p0c_artifact_grounding("全部の資料を配布します。お子様も参加できます。\n",
                                       "案内を作成する\n")
    assert ok3["checked"] is True
    assert not [r for r in ok3["unsupported_assertions"] if r["kind"] == "proper"], ok3
    # 原本に無い確約はこれまで通り要確認(見逃さない)。
    for sentence in ("品質を保証します。\n", "20歳以上が対象です。\n",
                     "持ち物は終了後に返却します。\n", "無料で提供します。\n"):
        bad = check_p0c_artifact_grounding(sentence, "案内を作成する\n")
        assert bad["checked"] is True and bad["passed"] is False, sentence
        assert bad["unsupported_assertions"], sentence


def test_semantic_failure_is_untestable_with_diagnosis_and_no_secrets():
    secret = "sk-secret-p11"
    source = f"原本本文{secret}"
    item = {"item_id": "SC01-R09", "criterion_id": "SC01",
            "artifact_path": "result/sc01.md", "requirement": "文意の適合",
            "method": "llm_semantic", "required": True, "expected": {}}

    def _timeout(_item, _content):
        raise TimeoutError("local model timeout")

    row = check_item_semantic(item, "本文", "", _timeout)
    assert row["result"] == "UNTESTABLE"
    assert "タイムアウト" in row["reason"] and "SC01-R09" in row["reason"]
    assert "result/sc01.md" in row["reason"]
    assert secret not in row["reason"] and source not in row["reason"]

    def _bad_json(_item, _content):
        raise RuntimeError("local semantic judge returned non-JSON: " + source)

    row2 = check_item_semantic(item, "本文", "", _bad_json)
    assert row2["result"] == "UNTESTABLE"
    assert "応答形式" in row2["reason"] or "失敗" in row2["reason"]
    assert source not in row2["reason"]

    # 実在しない引用は UNTESTABLE のまま(PASS に読み替えない)。
    row3 = check_item_semantic(
        item, "本文", "",
        lambda _i, _c: {"result": "PASS", "quote": "存在しない引用", "reason": "x"})
    assert row3["result"] == "UNTESTABLE"
    assert "引用が成果物と一致しない" in row3["reason"]


def test_semantic_judge_and_repair_emit_no_unawaited_coroutine_warning(tmp_path):
    import asyncio
    import gc
    import json
    import warnings

    from app.requirement_verification import build_local_semantic_judge

    async def complete(_system, _prompt):
        return json.dumps({"result": "PASS", "quote": "本文", "reason": "根拠", "line": 1})

    def sync_complete(system, prompt):
        return asyncio.run(complete(system, prompt))

    item = {"item_id": "SC01-R99", "artifact_path": "result/sc01.md",
            "requirement": "文意", "required": True, "expected": {}}
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        judge = build_local_semantic_judge(sync_complete)
        row = check_item_semantic(item, "本文", "", judge)
        assert row["result"] == "PASS"
        gc.collect()
    assert not [w for w in caught if issubclass(w.category, RuntimeWarning)
                and "was never awaited" in str(w.message)]


def test_full_chain_items_to_report(tmp_path):
    manager, pid = _manager(tmp_path)
    task = _bind(manager, pid)
    _write(manager, pid, "result/sc01.md", _GOOD_DOC)
    _write(manager, pid, "result/sc01_checklist.md", _GOOD_CHK)
    rows = verify_requirement_items(task, _resolve(manager, pid), semantic_judge=None)
    assert rows and all(r["result"] == "PASS" for r in rows if r.get("required", True)
                        and r["method"] != "llm_semantic")
    from app.goal_review import plan_snapshot
    _, signature = plan_snapshot(manager, pid)
    record_ledger(manager.memory.path, ledger_payload(
        pid, signature, "src-p11", rows,
        {"result/sc01.md": "h1", "result/sc01_checklist.md": "h2"}))
    saved = latest_ledger(manager.memory.path, pid)
    assert saved is not None and saved["rows"]
    # 契約の新値(match・役割・診断)が台帳と最終検証書まで届く。
    assert any((r.get("expected") or {}).get("datetime_role") for r in saved["rows"])
    assert any((r.get("expected") or {}).get("match") == "any" for r in saved["rows"])
    _write(manager, pid, "result/final_verification.md",
           "# 最終検証\n## 達成条件別判定\n- SC01: PASS\n")
    target = _resolve(manager, pid)("result/final_verification.md")
    assert "SC01" in target.read_text(encoding="utf-8")


def test_heading_in_other_artifact_does_not_suppress_required_phrase():
    from app.requirement_matrix import _p0a_core_items

    matrix = [
        {"item_id": "SC01-RX", "criterion_id": "SC01",
         "artifact_path": "result/sc01.md", "method": "phrase",
         "required": True, "expected": {"keywords": ["終了後確認を含める"]}},
        {"item_id": "SC01-HX", "criterion_id": "SC01",
         "artifact_path": "result/sc01_checklist.md", "method": "headings",
         "required": True, "expected": {"headings": ["終了後確認"]}},
    ]
    outputs = [{"path": "result/sc01.md", "artifact_category": "document"},
               {"path": "result/sc01_checklist.md", "artifact_category": "checklist"}]
    rows, _ = _p0a_core_items("SC01", "案内文には終了後確認を含める",
                              matrix, outputs, "")
    required = next(row for row in rows if row["item_id"] == "SC01-RX")
    assert required["method"] == "phrase"
    assert required["artifact_path"] == "result/sc01.md"
    assert required["expected"]["keywords"] == ["終了後確認"]
