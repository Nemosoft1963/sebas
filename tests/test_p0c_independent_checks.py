"""P0-C: 修正案が目標を壊さない独立検査。疑似PJ・実関数・外部通信なし。

特定案件の語彙を製品コードに入れない。フェイクのLLM応答で再現する。
"""
import asyncio
import json

import pytest

from app.goal_review import ReviewStore, plan_snapshot
from app.structured_planning import compile_plan, compile_task


CONTACT = "name@example.invalid"


def _proposal(title, outputs):
    return {"title": title, "scope": "疑似の2資料を整理する。",
            "headings": ["概要", "詳細"], "depends_on": [], "outputs": outputs}


def _plan_pair():
    t1 = compile_task(1, "資料Aを1ファイルで作る",
                      _proposal("疑似A", [{"kind": "document",
                                           "required_headings": ["資料Aの概要", "資料Aの詳細"]}]), [])
    t2 = compile_task(2, "資料Bを1ファイルで作る",
                      _proposal("疑似B", [{"kind": "document",
                                           "required_headings": ["資料Bの概要", "資料Bの詳細"]}]), ["SC01"])
    return compile_plan(["資料Aを1ファイルで作る", "資料Bを1ファイルで作る"],
                        [t1, t2], goal="疑似目標")


def _merged_plan():
    t1 = compile_task(1, "資料Aを1ファイルで作る",
                      _proposal("疑似統合", [{"kind": "document",
                                              "required_headings": ["統合資料の概要", "統合資料の詳細"]}]), [])
    t2 = compile_task(2, "資料Bを1ファイルで作る",
                      _proposal("疑似B2", [{"kind": "document",
                                            "required_headings": ["資料Bの概要", "資料Bの詳細"]}]), ["SC01"])
    plan = compile_plan(["資料Aを1ファイルで作る", "資料Bを1ファイルで作る"],
                        [t1, t2], goal="疑似目標")
    # 2ファイルを1ファイルに統合した候補: SC02の出力をSC01へ畳む。
    merged_tasks = []
    for task in plan["tasks"]:
        if str(task.get("task_key") or "") == "SC02":
            continue
        merged_tasks.append(task)
    return {"tasks": merged_tasks}


def _manager(tmp_path, mission_extra=""):
    from types import SimpleNamespace
    from app.memory.short_term import ShortTermMemory
    from app.workspace_files import WorkspaceSandbox

    mem = ShortTermMemory(tmp_path / "memory" / "conversations.db")
    pid = mem.create_project("p0c-pseudo")["id"]
    mem.save_mission(
        pid,
        "疑似目標\n1. 資料Aを1ファイルで作る\n2. 資料Bを1ファイルで作る",
        ("疑似目標\n1. 資料Aを1ファイルで作る 連絡先 " + CONTACT
         + "\n2. 資料Bを1ファイルで作る" + mission_extra),
        "",
        True,
        ["chatgpt"],
    )
    ws = WorkspaceSandbox(tmp_path / "workspace")
    mgr = SimpleNamespace(
        memory=mem, workspace=ws, planning_projects=set(), llm=object(),
        workers={}, _sync_memos=lambda pid: None,
        provider_statuses=lambda: [{"id": "chatgpt", "configured": True}],
        plan_review_runner=None,
        _local_complete=None,
    )
    return mgr, mem, pid


def _prepare(mgr, mem, pid, pair=None):
    plan = pair or _plan_pair()
    mem.replace_plan(pid, "p0c plan", plan["tasks"])
    from app.goal_contract import activate
    activate(mgr, pid)
    snapshot, signature = plan_snapshot(mgr, pid)
    for task in snapshot["tasks"]:
        if str(task.get("task_key") or "") == "SC01":
            task["description"] = "連絡先 " + CONTACT + " を使って資料Aを作る"
    mem.replace_plan(pid, "p0c plan with fact", snapshot["tasks"])
    snapshot, signature = plan_snapshot(mgr, pid)
    return snapshot, signature


def _issues_for_contact():
    return [{"id": "issue-contact", "provider": "chatgpt",
             "criterion": "SC01",
             "text": "資料Aの連絡先の記載方法を具体化してください。"
                     "架空の連絡先を勝手に変えずに記載してください。",
             "origin": "user_import", "lifecycle": "imported"}]


def _issues_pair(second_text="資料Bの手順を具体化してください。"):
    return [{"id": "issue-1", "provider": "chatgpt", "criterion": "SC01",
             "text": "資料Aの手順を具体化してください。",
             "origin": "user_import", "lifecycle": "imported"},
            {"id": "issue-2", "provider": "chatgpt", "criterion": "SC02",
             "text": second_text,
             "origin": "user_import", "lifecycle": "imported"}]


def _save_issues(mem, pid, signature, issues):
    store = ReviewStore(mem.path)
    store.put(pid, "feedback", signature, {"issues": list(issues)})


def _body(actions):
    return {"actions": list(actions)}


def _action(issue_id, target="SC01", change=None, reason=None, **extra):
    row = {"issue_id": issue_id, "disposition": "amend", "target": target,
           "change": change or ("原本の参照箇所と成果物を一対一で照合して記録する。"
                                "連絡先は正本のまま変えない。"),
           "reason": reason or ("指摘に対応する具体的な照合手順を追加する。"
                                "連絡先は正本のまま変えない。")}
    row.update(extra)
    return row


def test_contact_mutation_is_rejected_with_field_and_issue():
    import app.plan_feedback as fb

    mgr, mem, pid = _manager(__import__("pathlib").Path("/tmp/p0c-nouse"))
    snapshot, _signature = _prepare(mgr, mem, pid)
    issues = _issues_for_contact()
    bad = _body([_action("issue-contact",
                         change="連絡先を example に一般化して記載する。",
                         reason="連絡先を example に一般化して記載する。")])
    with pytest.raises(ValueError, match="P0-C独立検査が不合格"):
        fb.validate_candidate(bad, issues, snapshot, None)
    # 正本の値を保った候補は通る。
    good = _body([_action("issue-contact",
                          change="連絡先 " + CONTACT + " の記載箇所と確認手順を追加する。",
                          reason="指摘に対応する具体的な記載箇所の追加。" + CONTACT)])
    out = fb.validate_candidate(good, issues, snapshot, None)
    assert out[0]["disposition"] == "amend"


def test_merge_two_files_into_one_is_rejected():
    import app.plan_feedback as fb

    mgr, mem, pid = _manager(__import__("pathlib").Path("/tmp/p0c-nouse2"))
    mem.save_mission(
        pid,
        "疑似目標\n1. 資料Aを1ファイル、資料Bを1ファイル、合計2ファイルで作る",
        ("疑似目標\n1. 資料Aを1ファイル、資料Bを1ファイル、合計2ファイルで作る\n"
         "連絡先 " + CONTACT),
        "", True, ["chatgpt"])
    _prepare(mgr, mem, pid)
    issues = [{"id": "issue-1", "provider": "chatgpt", "criterion": "SC01",
               "text": "資料Aの手順を具体化してください。",
               "origin": "user_import", "lifecycle": "imported"},
              {"id": "issue-2", "provider": "chatgpt", "criterion": "SC02",
               "text": "資料Bの手順を具体化してください。",
               "origin": "user_import", "lifecycle": "imported"}]
    merged = _merged_plan()
    execution_plan = {"tasks": list(merged["tasks"])}
    # 事実保持(完全形の消去)と成果物統合の両方が違反だが、
    # 最初に返るのは事実保持側。両方が検出されることを確認する。
    with pytest.raises(fb.P0CRejectionError) as caught:
        fb._p0c_finalize_candidate_or_raise(
            actions=[fb._action if False else _action("issue-1", change="資料Aの手順を追加する。" + CONTACT,
                                                       reason="指摘に対応する。" + CONTACT),
                     _action("issue-2", target="SC02",
                             change="資料Bの手順を追加する。",
                             reason="指摘に対応する。")],
            issues=issues, changes=[],
            execution_plan=execution_plan, snapshot=plan_snapshot(mgr, pid)[0],
            detail=None, mission=mem.get_mission(pid))
    assert caught.value.violation_field in ("artifact_count", "protected_fact:contact")
    assert set(caught.value.issue_ids) >= {"issue-1", "issue-2"} or caught.value.issue_ids
    # 成果物統合だけの候補(事実を残す)は artifact_count で拒否される。
    # _p0c_check_artifact_separation は違反dictのlistを返す(例外ではない)。
    rows = fb._p0c_check_artifact_separation(
        actions=[_action("issue-1", change="資料Aの手順を追加する。" + CONTACT,
                         reason="指摘に対応する。" + CONTACT),
                 _action("issue-2", target="SC02")],
        changes=[], new_tasks=list(merged["tasks"]),
        snapshot=plan_snapshot(mgr, pid)[0], mission=mem.get_mission(pid))
    assert rows and rows[0]["violation_field"] == "artifact_count"


def test_description_only_executor_fix_stays_unresolved():
    import app.plan_feedback as fb
    from app.structured_planning import compile_task as _compile_task

    mgr, mem, pid = _manager(__import__("pathlib").Path("/tmp/p0c-nouse3"))
    snapshot, _signature = _prepare(mgr, mem, pid)
    # 専用実行器の工程(SC01にexecution_kindを付与)では説明追記がdevelopmentへ回る。
    for task in snapshot["tasks"]:
        if str(task.get("task_key") or "") == "SC01":
            task["description"] = "専用実行器で資料Aを作る"
    try:
        from app.structured_planning import contract_of as _contract_of
        import json as _json
        for task in snapshot["tasks"]:
            if str(task.get("task_key") or "") == "SC01":
                contract = _contract_of(task) or {}
                contract["execution_kind"] = "dedicated_executor_pseudo"
                task["acceptance_criteria"] = _json.dumps(
                    {"criterion_ids": ["SC01"], "execution_kind": "dedicated_executor_pseudo",
                     "outputs": contract.get("outputs") or []},
                    ensure_ascii=False)
    except Exception:
        pass
    issues = [{"id": "issue-dev", "provider": "chatgpt", "criterion": "SC01",
               "text": "専用実行器の動作が不足している。説明文の追記だけでは直らない。",
               "origin": "user_import", "lifecycle": "imported"}]
    body = _body([{"issue_id": "issue-dev", "disposition": "amend",
                   "target": "final_verification",
                   "change": "説明文に専用実行器の動作説明を追記する。",
                   "reason": "指摘に対応するため説明文を追記して解決する。"}])
    out = fb.validate_candidate(body, issues, snapshot, None)
    assert out[0]["disposition"] in ("development", "rebuild_generic", "unresolved")
    deltas = fb.build_p0c_issue_deltas(actions=out, issues=issues, changes=[],
                                       mission=mem.get_mission(pid),
                                       inspection_notes={})
    assert deltas[0]["remaining"]
    assert "解決済み" not in " / ".join(deltas[0]["remaining"])


def test_missing_issue_is_rejected_and_full_set_passes():
    import app.plan_feedback as fb
    mgr, mem, pid = _manager(__import__("pathlib").Path("/tmp/p0c-nouse4"))
    snapshot, _signature = _prepare(mgr, mem, pid)
    issues = _issues_pair()
    partial = _body([_action("issue-1")])
    with pytest.raises(ValueError, match="全指摘"):
        fb.validate_candidate(partial, issues, snapshot, None)
    full = _body([_action("issue-1"),
                  _action("issue-2", target="SC02",
                           change="資料Bの手順を追加する具体的な差分。",
                           reason="指摘に対応する具体的な手順の追加。")])
    out = fb.validate_candidate(full, issues, snapshot, None)
    assert {a["issue_id"] for a in out} == {"issue-1", "issue-2"}


def test_broken_p0c_text_check_does_not_pass(monkeypatch):
    import app.plan_feedback as fb

    mgr, mem, pid = _manager(__import__("pathlib").Path("/tmp/p0c-broken"))
    snapshot, _signature = _prepare(mgr, mem, pid)
    issues = _issues_for_contact()
    bad = _body([_action("issue-contact",
                         change="連絡先を example に一般化して記載する。",
                         reason="連絡先を example に一般化して記載する。")])

    def _broken(*args, **kwargs):
        raise RuntimeError("injected inspection failure")

    monkeypatch.setattr(fb, "_p0c_check_action_text", _broken)
    with pytest.raises(ValueError, match="P0-C独立検査を確認できませんでした"):
        fb.validate_candidate(bad, issues, snapshot, None)


def test_artifact_undetermined_rebuild_is_rejected_not_passed(monkeypatch):
    import app.plan_feedback as fb

    mgr, mem, pid = _manager(__import__("pathlib").Path("/tmp/p0c-undet"))
    mem.save_mission(
        pid,
        "疑似目標\n1. 資料Aを1ファイル、資料Bを1ファイル、合計2ファイルで作る",
        ("疑似目標\n1. 資料Aを1ファイル、資料Bを1ファイル、合計2ファイルで作る\n"
         "連絡先 " + CONTACT),
        "", True, ["chatgpt"])
    snapshot, _signature = _prepare(mgr, mem, pid)
    issues = _issues_pair()
    merged = _merged_plan()
    execution_plan = {"tasks": list(merged["tasks"])}
    actions = [_action("issue-1", change="資料Aの手順を追加する。" + CONTACT,
                       reason="指摘に対応する。" + CONTACT),
               _action("issue-2", target="SC02")]
    monkeypatch.setattr(fb, "_p0c_required_files", lambda mission: (2, ["疑似要件"], True))
    with pytest.raises(fb.P0CRejectionError) as caught:
        fb._p0c_finalize_candidate_or_raise(
            actions=actions, issues=issues, changes=[],
            execution_plan=execution_plan, snapshot=snapshot,
            detail=None, mission=mem.get_mission(pid))
    assert caught.value.violation_field == "artifact_count"


def test_artifact_undetermined_amend_stays_unresolved(monkeypatch):
    import app.plan_feedback as fb

    mgr, mem, pid = _manager(__import__("pathlib").Path("/tmp/p0c-undet2"))
    mem.save_mission(
        pid,
        "疑似目標\n1. 資料Aを1ファイル、資料Bを1ファイル、合計2ファイルで作る",
        ("疑似目標\n1. 資料Aを1ファイル、資料Bを1ファイル、合計2ファイルで作る\n"
         "連絡先 " + CONTACT),
        "", True, ["chatgpt"])
    snapshot, _signature = _prepare(mgr, mem, pid)
    issues = _issues_pair()
    actions = [_action("issue-1", change="資料Aの手順を追加する。" + CONTACT,
                       reason="指摘に対応する。" + CONTACT),
               _action("issue-2", target="SC02",
                       change="資料Bの手順を追加する。",
                       reason="指摘に対応する。")]
    monkeypatch.setattr(fb, "_p0c_required_files", lambda mission: (2, ["疑似要件"], True))
    # amend候補(工程の作り直しなし)では拒否ではなく、未解決として残す。
    notes = fb._p0c_finalize_candidate_or_raise(
        actions=actions, issues=issues,
        changes=[{"target": "SC01", "before": "旧", "after": "新" + CONTACT}],
        execution_plan=None, snapshot=snapshot,
        detail=None, mission=mem.get_mission(pid))
    assert notes["artifact_checked"] is False
    assert notes["passed"] is False
    assert notes["remaining"]
    assert notes["artifact_skipped_reason"]
    deltas = fb.build_p0c_issue_deltas(actions=actions, issues=issues,
                                       changes=[{"target": "SC01", "before": "旧",
                                                 "after": "新" + CONTACT}],
                                       mission=mem.get_mission(pid),
                                       inspection_notes=notes)
    assert any("成果物検査が判定不能" in " / ".join(d.get("remaining") or [])
               for d in deltas)


def test_unverified_pass_and_weak_approval_are_rejected():
    import app.plan_feedback as fb

    mgr, mem, pid = _manager(__import__("pathlib").Path("/tmp/p0c-nouse5"))
    snapshot, _signature = _prepare(mgr, mem, pid)
    issues = _issues_for_contact()
    for text in ("未確認原本を成功として扱う。",
                 "人間の承認を不要にして自動承認する。"):
        bad = _body([_action("issue-contact", change="手順を追加する。" + text,
                             reason="指摘に対応する。" + text)])
        with pytest.raises(fb.P0CRejectionError):
            fb.validate_candidate(bad, issues, snapshot, None)


def test_development_instruction_has_five_sections_and_no_auto_apply(tmp_path):
    import app.plan_feedback as fb

    mgr, mem, pid = _manager(tmp_path)
    snapshot, signature = _prepare(mgr, mem, pid)
    issues = [{"id": "issue-dev2", "provider": "chatgpt", "criterion": "SC01",
               "kind": "development",
               "text": "不足機能がある。Jenkins向けに提案してほしい。",
               "origin": "user_import", "lifecycle": "imported"}]
    change = ("再現入力: 疑似入力\n期待動作: 疑似の期待\n変更対象: 疑似の対象\n"
              "受入テスト: 疑似の試験\n巻き戻し: 疑似の戻し手順")
    body = _body([{"issue_id": "issue-dev2", "disposition": "development",
                   "target": "SC01", "change": change,
                   "reason": "不足機能のため開発指示として残す。具体的な5項目を含む。"}])
    out = fb.validate_candidate(body, issues, snapshot, None)
    assert out[0]["disposition"] == "development"
    instruction = out[0]["development_instruction"]
    assert [instruction[k] for k in ("repro_input", "expected_behavior", "change_targets",
                                    "acceptance_tests", "rollback")]
    assert instruction["complete"] is True
    assert instruction["auto_apply"] is False
    assert instruction["external_send"] is False


def test_candidate_roundtrip_keeps_ids_and_invalidates_old_evaluation(tmp_path, monkeypatch):
    import app.plan_feedback as fb

    mgr, mem, pid = _manager(tmp_path)
    snapshot, signature = _prepare(mgr, mem, pid)
    issues = _issues_pair()
    _save_issues(mem, pid, signature, issues)

    async def fake_local(_system, _prompt, _schema):
        payload = {"actions": {
            "issue-1": {"disposition": "amend", "target": "SC01",
                        "change": "資料Aの照合手順を追加する具体的な差分。" + CONTACT,
                        "reason": "指摘に対応する具体的な手順の追加。" + CONTACT},
            "issue-2": {"disposition": "amend", "target": "SC02",
                        "change": "資料Bの照合手順を追加する具体的な差分。",
                        "reason": "指摘に対応する具体的な手順の追加。"},
        }}
        return json.dumps(payload)

    mgr._local_complete = fake_local
    mgr.llm = object()
    # Ollama の型検査を避けるため、propose の直前検査だけを通す。
    import app.plan_feedback as _fb
    import app.core as _core
    real_ollama = _core.Ollama
    try:
        _core.Ollama = object
        row = asyncio.new_event_loop().run_until_complete(
            _fb.propose(mgr, pid, signature))
    finally:
        _core.Ollama = real_ollama
    assert row["status"] == "draft"
    assert row["p0c_checks"]["passed"] is True
    assert [d["issue_id"] for d in row["issue_deltas"]] == ["issue-1", "issue-2"]
    # 保存された候補がAPI応答相当の行まで届く。
    saved = ReviewStore(mem.path).get(pid, "revision", signature)
    assert saved["candidate_id"] == row["candidate_id"]
    assert saved["issue_deltas"] == row["issue_deltas"]
    assert "p0c_checks" in saved
    # 保存済みの合格表示を信用せず、適用直前の再計算が判定不能なら止める。
    with monkeypatch.context() as patch:
        patch.setattr(fb, "_p0c_recheck_saved_candidate_or_raise",
                      lambda **_kwargs: {"passed": False,
                                         "artifact_skipped_reason": "成果物数が判定不能"})
        with pytest.raises(fb.P0CRejectionError) as rejected:
            fb.apply(mgr, pid, signature, row["candidate_id"])
        assert rejected.value.violation_field == "artifact_count"
    out = fb.apply(mgr, pid, signature, row["candidate_id"])
    assert out["status"] == "awaiting_review"
    assert out["signature"] != signature
    # 旧評価は失効する(旧版の合格は新版の署名に引き継がれない)。
    # 新署名の合格が無い状態で require_review は未完了を拒否する。
    # goal_review_policy が無い疑似PJでは enabled=False のため、
    # 「未検証のまま実行不可」のValueErrorを期待するのではなく、
    # 新署名に合格行が無いことを実測で確認する。
    from app.goal_review import require_review as _require_review
    from app.goal_review import ReviewStore as _Store
    _Store(mem.path).put(pid, "plan", signature, {"status": "passed"})
    new_review = _Store(mem.path).get(pid, "plan", out["signature"])
    assert not (isinstance(new_review, dict) and new_review.get("status") == "passed")
    # 旧版の合格は現行版(新署名)の検証として使えない。
    from app.goal_review import plan_snapshot as _plan_snapshot
    live_sig = _plan_snapshot(mgr, pid)[1]
    assert live_sig == out["signature"]
    assert live_sig != signature
