"""P0-C 成果物の根拠確認の受入試験。疑似PJ・実関数・外部通信なし。

- 案内文に原本外の「持ち物は終了後に返却します」があると unsupported_assertions に
  入り、台帳に必須の UNTESTABLE(要確認)が記録され、完了ゲートが通らない。
  修正候補が提示される(自動適用されない)。
- 登録事実に同じ確約が明記されていれば検出されない。原本追記で再検証すると通る。
- 日付・金額・連絡先・人数・固有名が出典に無ければ検出され、出典にあれば通る
  (表記ゆれは同一)。数値0・不明・読取不能・未承認を成功/実績に読み替えない。
  照合できない・読めないときは「検査できなかった」で UNTESTABLE。
- 要確認は自動修復されず、P0-Bの修復ループで無限に試行されない(unsupported_fact
  として理由と人の操作が記録される)。
- 特定案件の語彙は試験入力にだけ置き、製品コードに入れない。
"""
from __future__ import annotations

import pytest

from app.completion_gate import evaluate as gate_evaluate
from app.goal_contract import activate
from app.memory.short_term import ShortTermMemory
from app.project_manager import ProjectOrchestrator
from app.requirement_item_ledger import (
    latest_ledger,
    list_repair_runs,
)
from app.requirement_verification import (
    P0C_METHOD,
    build_p0c_grounding_rows,
    check_p0c_artifact_grounding,
    classify_item_failure,
)
from app.structured_planning import compile_plan, compile_task, contract_of
from app.workspace_files import WorkspaceSandbox


REGISTERED = "開催日: 2026年11月14日 10:00 会場: 架空ホール 持ち物: 飲み物"
CRITERION = (
    "案内文1ファイルとチェックリスト1ファイル、合計2ファイルを作る。"
    "案内文には日時を含める。チェックリストには準備・終了後確認を含める。"
)

GUIDE_HEAD = (
    "## 案内文：イベント概要と参加方法\n日時と場所。\n"
    "## 案内文：持ち物と問い合わせ先\n"
)
GUIDE_TAIL = (
    "## 根拠と未確認事項\n架空と要確認と想定。\n"
    "## 実施状態と次の行動\n実施状態と次の行動。\n"
)
CHECK_HEAD = (
    "# チェックリスト\n準備と受付と終了後確認を記載する。\n"
    "## チェックリスト：準備・受付・終了後の手順\n準備と受付と終了後確認。\n"
)
CHECK_TAIL = GUIDE_TAIL


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
    project = memory.create_project("疑似P0-C")
    memory.save_mission(project["id"], "## 達成条件\n1. " + CRITERION, "", "", False, [])
    data = registered.encode("utf-8")
    memory.add_context_file(project["id"], "registered.md", registered, len(data),
                            data, "text/markdown", "markdown", "疑似原本",
                            "", source="registered")
    workspace = WorkspaceSandbox(tmp_path / "workspace")

    def _static(_project):
        return (registered, [{"id": "ctx1", "filename": "registered.md",
                              "content": registered, "file_kind": "markdown",
                              "size_bytes": len(registered.encode()), "source": "registered",
                              "char_count": len(registered)}])

    manager = ProjectOrchestrator(
        memory, None, _static, None, lambda: [], workspace=workspace)
    return manager, project["id"]


def _bind(manager, pid):
    task = compile_task(1, CRITERION, _proposal(), [], registered_text=REGISTERED)
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


def _write_fixtures(manager, pid, guide_mid, check_mid=None):
    _write(manager, pid, "result/sc01.md",
           "# 案内\n開催日: 2026年11月14日 10:00 会場: 架空ホール\n"
           + GUIDE_HEAD + guide_mid + "\n" + GUIDE_TAIL)
    _write(manager, pid, "result/sc01_checklist.md",
           CHECK_HEAD + (check_mid or "") + ("\n" if check_mid else "") + CHECK_TAIL)


def test_unsupported_promise_becomes_required_untestable():
    # 約束語を含む原本外文は検出される。修正候補は提示のみ(削除はしない)。
    checked = check_p0c_artifact_grounding(
        "持ち物は終了後に返却します。\n日時: 2026年11月14日\n", REGISTERED,
        artifact_path="result/sc01.md")
    assert checked["checked"] is True
    assert checked["passed"] is False
    kinds = {(r.get("kind"), r.get("value")) for r in checked["unsupported_assertions"]}
    assert any(k == "promise" and "返却" in str(v) for k, v in kinds)
    assert checked["suggestions"]
    assert "確定" not in str(checked["suggestions"][0]["proposal"]).replace("確定文にせず", "")
    # 原本に根拠が無いこと・原文・位置・修正候補が残る。
    assert "原本に根拠がない" in checked["suggestions"][0]["proposal"]
    assert "持ち物は終了後に返却します" in checked["suggestions"][0]["sentence"]
    manager = _manager.__wrapped__ if hasattr(_manager, "__wrapped__") else None
    assert manager is None  # 試験は製品の登録テキスト経由で行う(下の統合試験)。


def test_same_promise_in_registered_source_passes():
    registered = REGISTERED + " 持ち物は終了後に返却します"
    checked = check_p0c_artifact_grounding(
        "持ち物は終了後に返却します。\n", registered,
        artifact_path="result/sc01.md")
    assert checked["checked"] is True
    assert checked["passed"] is True
    assert checked["unsupported_assertions"] == []


def test_value_grounding_with_notation_variants():
    # 表記ゆれは同一として通る。出典に無い値は検出される。
    registered = "日時2026年11月14日 電話03-1234-5678 参加者30人"
    ok = check_p0c_artifact_grounding(
        "日時は2026/11/14、連絡先は０３-１２３４-５６７８、参加者は３０人。\n",
        registered)
    assert ok["unsupported_assertions"] == []
    bad = check_p0c_artifact_grounding(
        "日時は2026年11月15日、連絡先は090-1111-2222、参加者は50人。\n",
        registered)
    kinds = {r["kind"] for r in bad["unsupported_assertions"]}
    assert {"datetime", "contact_phone", "number"} <= kinds


def test_time_minutes_and_derived_relative_minutes():
    registered = "開始時刻10:00 終了時刻15:00 準備開始時刻9:30"
    checked = check_p0c_artifact_grounding(
        "受付は午前10時00分、終了は午後3時00分です。準備は30分前です。",
        registered)
    assert not [r for r in checked["unsupported_assertions"]
                if r["kind"] == "number" and r["value"] in {"00分", "30分"}], checked
    bad = check_p0c_artifact_grounding("準備は45分前です。", registered)
    assert any(r["kind"] == "number" and "45分" in r["value"]
               for r in bad["unsupported_assertions"]), bad


def test_age_and_promises_remain_detected():
    for sentence in ("特に年齢制限はなく参加できます。", "品質を保証します。",
                     "持ち物は返却します。", "参加費は無料です。"):
        checked = check_p0c_artifact_grounding(sentence, "案内を作成する。")
        assert checked["passed"] is False, sentence
        assert checked["unsupported_assertions"], sentence


def test_zero_unknown_unreadable_are_not_success():
    # 数値0・不明・読取不能・未承認の記述を成功/実績に読み替えない。
    # 0人は登録事実に無い値として検出される(成功扱いにしない)。
    checked = check_p0c_artifact_grounding(
        "参加者は0人で、詳細は不明。\n",
        "案内を作成する", artifact_path="result/sc01.md")
    assert checked["passed"] is False
    assert any(r["kind"] == "number" for r in checked["unsupported_assertions"])
    # 成果物が読めない・出典が読めないときは「検査できなかった」で UNTESTABLE。
    missing_artifact = check_p0c_artifact_grounding(
        None, REGISTERED, artifact_path="result/sc01.md",
        read_error="読み取り失敗: OSError")
    assert missing_artifact["checked"] is False
    assert missing_artifact["passed"] is False
    assert "検査できなかった" in missing_artifact["reason"]
    missing_source = check_p0c_artifact_grounding("本文がある。\n", "")
    assert missing_source["checked"] is False
    assert missing_source["passed"] is False
    assert "検査できなかった" in missing_source["reason"] or "unreadable" in missing_source["reason"]


def test_full_loop_gate_blocks_until_human_confirms(tmp_path):
    # 成果物保存→台帳に要確認→完了ゲートが通らない→人が確認(原本追記)→再検証で通る。
    manager, pid = _manager(tmp_path)
    task = _bind(manager, pid)
    _write_fixtures(manager, pid, "持ち物は終了後に返却します。")
    rows = build_p0c_grounding_rows(task, _resolve(manager, pid), REGISTERED)
    assert rows
    assert all(r["method"] == P0C_METHOD and r["result"] == "UNTESTABLE"
               and r["required"] is True for r in rows)
    assert any("返却" in str(r.get("quote") or "") or "返却" in str(r.get("reason") or "")
               for r in rows)
    # 修正候補が提示され、本文は自動で直っていない。
    assert any("修正候補" in str(r.get("reason") or "") for r in rows)
    target = _resolve(manager, pid)("result/sc01.md")
    assert "返却します" in target.read_text(encoding="utf-8")
    # 台帳に保存される(最終検証前)。分類は unsupported_fact(要確認)。
    from app.requirement_item_ledger import record_ledger
    from app.requirement_verification import ledger_payload, verify_requirement_items
    from app.goal_review import plan_snapshot
    base_rows = verify_requirement_items(task, _resolve(manager, pid), semantic_judge=None)
    all_rows = list(base_rows) + list(rows)
    _, signature = plan_snapshot(manager, pid)
    record_ledger(manager.memory.path, ledger_payload(
        pid, signature, "src-p0c", all_rows,
        {"result/sc01.md": "h1", "result/sc01_checklist.md": "h2"}))
    saved = latest_ledger(manager.memory.path, pid)
    kinds = {classify_item_failure(r) for r in saved["rows"]
             if r["method"] == P0C_METHOD}
    assert kinds == {"unsupported_fact"}
    # 完了ゲートは通らない。
    activate(manager, pid)
    mission = manager.memory.get_mission(pid)
    for item in mission["tasks"]:
        if (contract_of(item) or {}).get("final_verification"):
            continue
        manager.memory.update_task(item["id"], "completed", result="疑似完了")
    final = next(item for item in mission["tasks"]
                 if (contract_of(item) or {}).get("final_verification"))
    manager.memory.update_task(final["id"], "completed", result="疑似完了")
    gate = gate_evaluate(manager, pid)
    assert gate["achieved"] is False
    # 要確認の修正候補どおり、原本に追記して根拠にすれば再検証で通る。
    registered2 = REGISTERED + " 持ち物は終了後に返却します"
    rows2 = build_p0c_grounding_rows(task, _resolve(manager, pid), registered2)
    assert rows2 == [] or all(r["result"] != "UNTESTABLE" for r in rows2)
    assert "返却します" in target.read_text(encoding="utf-8")


def test_p0c_is_not_auto_repaired_and_repair_loop_is_bounded(tmp_path):
    manager, pid = _manager(tmp_path)
    task = _bind(manager, pid)
    _write_fixtures(manager, pid, "持ち物は終了後に返却します。")
    from app.goal_review import plan_snapshot
    from app.requirement_item_ledger import record_ledger
    from app.requirement_verification import (
        ledger_payload,
        verify_requirement_items,
    )
    base_rows = verify_requirement_items(task, _resolve(manager, pid), semantic_judge=None)
    p0c_rows = build_p0c_grounding_rows(task, _resolve(manager, pid), REGISTERED)
    assert p0c_rows
    _, signature = plan_snapshot(manager, pid)
    record_ledger(manager.memory.path, ledger_payload(
        pid, signature, "src-p0c", list(base_rows) + list(p0c_rows),
        {"result/sc01.md": "h1", "result/sc01_checklist.md": "h2"}))
    ok = manager._append_goal_supplement(pid, ["SC01: 要確認あり"], "# report")
    assert ok is True
    runs = list_repair_runs(manager.memory.path, pid)
    by_kind = {r["item_id"]: r["failure_kind"] for r in runs
               if r["decision"] == "awaiting_decision"}
    # 要確認(P0-C行)は unsupported_fact として判断待ちになる(自動修復されない)。
    p0c_ids = {r["item_id"] for r in p0c_rows}
    assert p0c_ids and all(by_kind.get(i) == "unsupported_fact" for i in p0c_ids), by_kind
    # 自動修復の対象ではない(修復の試行として数えない)。追補で無限に増えない。
    mission = manager.memory.get_mission(pid)
    supplements = [t for t in mission["tasks"] if t["task_key"].startswith("goal_supplement_")]
    assert supplements == []
    project = manager.memory.get_project(pid)
    try:
        manager.workspace.resolve_file(
            project.get("workspace_path", ""), pid,
            "result/sc01.v1.md", must_exist=True)
        versioned = True
    except (OSError, ValueError, FileNotFoundError):
        versioned = False
    assert versioned is False
    # 同じ失敗の再試行上限(2回)を超えても判断待ちのまま。修復で増殖しない。
    from app.requirement_item_ledger import record_repair_run
    from app.requirement_verification import failure_signature
    row = next(r for r in p0c_rows if r["method"] == P0C_METHOD)
    for _ in range(2):
        record_repair_run(
            manager.memory.path, project_id=pid, item_id=row["item_id"],
            failure_kind="unsupported_fact",
            failure_signature=failure_signature(row["item_id"], "unsupported_fact",
                                                "h1", "src-p0c"),
            plan_signature="sig", source_hash="src-p0c",
            artifact_path=row["artifact_path"], artifact_hash_before="h1",
            artifact_hash_after="h1", item_result_before="UNTESTABLE",
            item_result_after="UNTESTABLE", decision="awaiting_decision",
            operator_action="人の操作", reason="疑似の判断待ち")
    assert manager._append_goal_supplement(pid, ["SC01: 要確認あり"], "# report") is True
    mission2 = manager.memory.get_mission(pid)
    assert [t for t in mission2["tasks"] if t["task_key"].startswith("goal_supplement_")] == []
    assert any(e["kind"] == "p0b_awaiting_decision" for e in mission2["events"])


def test_p0c_runs_after_existing_refusals_and_semantic_behavior_unchanged(tmp_path):
    # 既存の具体的拒否(対象不明)が先に残り、P0-Cが隠さない。
    from app.requirement_verification import verify_requirement_items
    manager, pid = _manager(tmp_path)
    task = _bind(manager, pid)
    broken = dict(task)
    import json as _json
    contract = _json.loads(task["acceptance_criteria"])
    contract["requirement_matrix"] = [dict(contract["requirement_matrix"][0], artifact_path="")]
    broken["acceptance_criteria"] = _json.dumps(contract, ensure_ascii=False)
    _write_fixtures(manager, pid, "持ち物は終了後に返却します。")
    rows = verify_requirement_items(broken, _resolve(manager, pid), semantic_judge=None,
                                    registered_text=REGISTERED)
    assert any("対象成果物不明" in str(r.get("reason") or "") for r in rows)
    assert any(r.get("method") == P0C_METHOD for r in rows)
    # 文意項目の挙動は変えない(引用不一致は UNTESTABLE のまま)。
    from app.requirement_verification import check_item_semantic
    item = {"item_id": "SC01-R01", "criterion_id": "SC01",
            "artifact_path": "result/sc01.md", "requirement": "文意の適合",
            "method": "llm_semantic", "required": True, "expected": {}}
    assert check_item_semantic(item, "本文", "", None)["result"] == "UNTESTABLE"


def test_relative_minutes_require_registered_start_and_prep_roles():
    # 2時刻の偶然の差だけでは「準備は開始30分前」を裏付けない。
    registered = "受付9:30 開始時刻10:00 終了時刻15:00"
    checked = check_p0c_artifact_grounding("準備は30分前です。", registered)
    assert any(r["kind"] == "number" and "30分" in r["value"]
               for r in checked["unsupported_assertions"]), checked


def test_relative_minutes_after_is_not_derived_from_start_and_prep():
    registered = "開始時刻10:00 準備開始時刻9:30"
    checked = check_p0c_artifact_grounding("終了は30分後です。", registered)
    assert any(r["kind"] == "number" and "30分" in r["value"]
               for r in checked["unsupported_assertions"]), checked
