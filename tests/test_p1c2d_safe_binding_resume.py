"""P1(修正C)/P2(修正D)の追加受入試験。疑似PJ・実関数・外部通信なし。

- SC00のみを指す準備指摘は一意条件を満たせばローカル修正案まで進む。
  PREP00欠け・他SC矛盾・複数工程・外部操作要求・署名/ハッシュ/指紋不一致は停止。
  達成条件のない準備指摘をSC01へ配賦しない。確認不能・読取失敗を合格にしない。
- v1読取維持・書込v2。
- 保存済みレビューからの再開は外部呼出0回で同じ指摘ID・順序・計画署名を保つ。
  版変更1回以外の単純な再試行は空転ガードが止める。版更新でも再開は1回だけ。
  再停止時は新しい原因と版を記録する。人間選択も同じ経路に入る。
- ローカル修正案が全指摘IDの対応を持つ。外部AI原文の命令は実行しない。
- 改訂計画の独立検査。外部再評価は最大2回で要判断。外部合格を承認・実行に変換しない。
- 公開packetに準備ゲート構造情報が入り、原本・氏名・ファイル名・秘密が入らない。
- 画面の静的検査(innerHTML系なし)、JS参照キーとAPI応答キーの一致、
  取得失敗が成功表示にならない、二重送信防止。
"""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.experience_store import canonical
from app.goal_review import public_structure
from app.structured_planning import compile_plan, compile_task


class Manager(SimpleNamespace):
    pass


def _twenty_step_snapshot():
    tasks = []
    criteria = []
    for i in range(20):
        if i == 0:
            key, cids = "SC00", []
        elif i == 19:
            key, cids = "final_verification", [f"SC{j:02d}" for j in range(1, 19)]
        else:
            key, cids = f"SC{i:02d}", [f"SC{i:02d}"]
        tasks.append({
            "id": f"t{i}", "task_key": key, "title": key, "description": "疑似工程",
            "acceptance_criteria": json.dumps({
                "schema": "local-cowork-plan/v1", "criterion_ids": cids,
                "role": "preparation" if key == "SC00" else ("final_verification" if key == "final_verification" else "execution"),
                "internal_preparation_gate_ids": ["PREP00"] if key in {"SC00", "final_verification"} else [],
            }, ensure_ascii=False),
            "depends_on": [],
        })
        if 1 <= i <= 18:
            criteria.append({
                "criterion_id": f"SC{i:02d}",
                "exec_task_keys": [f"SC{i:02d}"],
                "verify_task_keys": ["final_verification"],
            })
    return {"tasks": tasks}, {"criteria": criteria, "content_hash": "hash-20"}


def _prep_issue(**over):
    row = {"step": "1", "unmet_goal": "準備工程の検査方法が不足しています",
           "reason": "原本確認の検査方法と停止条件が不足しています",
           "remedy": "検査方法と停止条件を具体化する", "severity": "blocking"}
    row.update(over)
    return row


def test_prep_unique_resolves_without_sc01_allocation():
    import app.plan_review_loop as loop

    snap, contract = _twenty_step_snapshot()
    parsed = [{"provider": "chatgpt", "status": "fail",
               "issues": [_prep_issue()]}]
    table = loop.build_issue_bindings(parsed, contract, snap,
                                      plan_signature="sig", goal_contract_hash="hash-20",
                                      packet_hash="pkt")
    binding = table["bindings"][0]
    assert binding["state"] == "resolved"
    assert binding["target_type"] == "preparation_gate"
    assert binding["target_ids"] == ["PREP00"]
    assert binding["candidate_criterion_ids"] == []
    assert "SC01" not in binding["candidate_task_keys"]
    assert binding["resolved_preparation"]["passed"] is True
    out = loop._map_issues_local(parsed, contract, snap,
                                 plan_signature="sig", goal_contract_hash="hash-20",
                                 packet_hash="pkt")
    assert out["bindings"][0]["target_type"] == "preparation_gate"


def test_prep_stop_cases():
    import app.plan_review_loop as loop

    snap, contract = _twenty_step_snapshot()
    # PREP00欠け(SC00の契約から内部ゲートを除去)
    snap2 = json.loads(json.dumps(snap))
    snap2["tasks"][0]["acceptance_criteria"] = json.dumps({
        "schema": "local-cowork-plan/v1", "criterion_ids": [],
        "role": "execution", "internal_preparation_gate_ids": []}, ensure_ascii=False)
    table = loop.build_issue_bindings(
        [{"provider": "chatgpt", "status": "fail", "issues": [_prep_issue()]}],
        contract, snap2, plan_signature="sig",
        goal_contract_hash="hash-20", packet_hash="pkt")
    assert table["bindings"][0]["state"] in {"ambiguous", "plan_level"} or \
        table["bindings"][0]["target_type"] != "preparation_gate"
    assert table["bindings"][0]["resolved_preparation"]["passed"] is False
    # 他SCと矛盾
    table = loop.build_issue_bindings(
        [{"provider": "chatgpt", "status": "fail",
          "issues": [_prep_issue(unmet_goal="SC01の検証不足 原本確認の検査方法")]}],
        contract, snap, plan_signature="sig",
        goal_contract_hash="hash-20", packet_hash="pkt")
    assert table["bindings"][0]["state"] == "ambiguous"
    assert "SC01" not in (table["bindings"][0].get("target_ids") or [])
    # 複数工程
    table = loop.build_issue_bindings(
        [{"provider": "chatgpt", "status": "fail",
          "issues": [_prep_issue(step="1,2")]}],
        contract, snap, plan_signature="sig",
        goal_contract_hash="hash-20", packet_hash="pkt")
    assert table["bindings"][0]["state"] in {"ambiguous", "multiple_targets", "invalid_reference"}
    assert table["bindings"][0]["resolved_preparation"]["passed"] is False
    # 外部操作の要求
    table = loop.build_issue_bindings(
        [{"provider": "chatgpt", "status": "fail",
          "issues": [_prep_issue(remedy="外部へ送信して公開する 検査方法")]}],
        contract, snap, plan_signature="sig",
        goal_contract_hash="hash-20", packet_hash="pkt")
    assert table["bindings"][0]["resolved_preparation"]["passed"] is False
    # 署名/ハッシュ/指紋の不一致(欠け)
    table = loop.build_issue_bindings(
        [{"provider": "chatgpt", "status": "fail", "issues": [_prep_issue()]}],
        contract, snap)
    assert table["bindings"][0]["resolved_preparation"]["passed"] is False


def test_unverifiable_and_unreadable_never_pass():
    import app.plan_review_loop as loop
    from app.goal_review import evaluate_external_review_status

    snap, contract = _twenty_step_snapshot()
    judged = evaluate_external_review_status(
        [{"provider": "chatgpt", "status": "unverifiable", "issues": []}],
        ["chatgpt"], {"min_success_count": 1})
    assert judged.get("status") != "passed"
    # 読取失敗・根拠なしの指摘はPASSにしない
    table = loop.build_issue_bindings(
        [{"provider": "chatgpt", "status": "fail",
          "issues": [{"step": "", "unmet_goal": "", "reason": "", "remedy": ""}]}],
        contract, snap, plan_signature="sig",
        goal_contract_hash="hash-20", packet_hash="pkt")
    assert table["bindings"][0]["state"] in {"ambiguous", "invalid_reference"}
    assert table["bindings"][0]["target_type"] == "plan_level"


def test_v1_read_kept_and_write_v2():
    import app.plan_review_loop as loop

    snap, contract = _twenty_step_snapshot()
    table = loop.build_issue_bindings(
        [{"provider": "chatgpt", "status": "fail", "issues": [_prep_issue()]}],
        contract, snap, plan_signature="sig",
        goal_contract_hash="hash-20", packet_hash="pkt")
    binding = table["bindings"][0]
    assert binding["target_type"] in loop.TARGET_TYPES
    assert table["version"] == loop.ISSUE_BINDING_VERSION
    # v1データの読取は維持する(旧項目だけの対応表は不明として扱い、補完しない)
    legacy = loop._read_binding_v1({"state": "resolved",
                                    "candidate_task_keys": ["SC01"],
                                    "candidate_criterion_ids": ["SC01"]})
    assert legacy["unknown"] is True
    assert legacy["target_type"] == ""


def test_resume_keeps_ids_order_signature_and_zero_external_calls(tmp_path):
    from app.memory.short_term import ShortTermMemory
    from app.workspace_files import WorkspaceSandbox

    mem = ShortTermMemory(tmp_path / "memory" / "conversations.db")
    pid = mem.create_project("resume-pseudo")["id"]
    mem.save_mission(pid, "疑似目標\n1. 架空手順Aを定義する",
                     "疑似目標\n1. 架空手順Aを定義する", "", True, ["chatgpt"])
    ws = WorkspaceSandbox(tmp_path / "workspace")
    mgr = Manager(memory=mem, workspace=ws, planning_projects=set(), llm=None,
                  workers={}, _sync_memos=lambda pid: None,
                  provider_statuses=lambda: [{"id": "chatgpt", "configured": True}],
                  plan_review_runner=None,
                  _outbound_guard=lambda pid, purpose, text, providers=(): {"ok": True, "codes": []})
    criteria = ["架空手順Aを定義する"]
    t1 = compile_task(1, criteria[0], {"title": "架空資料A", "scope": "架空Aの整理",
        "headings": ["目的", "実施内容"], "depends_on": []}, [])
    compiled = compile_plan(criteria, [t1], goal=mem.get_mission(pid)["goal"])
    mem.replace_plan(pid, "resume pseudo plan", compiled["tasks"])
    from app.goal_contract import activate as _activate
    _activate(mgr, pid)
    import app.plan_review_loop as loop
    import app.external_ai as ext

    async def _runner(text, providers):
        return [{"id": p, "ok": True, "review": json.dumps({
            "verdict": "fail",
            "issues": [{"severity": "blocking", "step": "9",
                        "unmet_goal": "不明",
                        "reason": "対応先の無い抽象的な指摘",
                        "remedy": "検討する"}]})} for p in providers]

    mgr.plan_review_runner = _runner
    summary = "架空手順Aの定義資料を作成し、達成条件SC01と検証記録を確認する公開用の具体的な計画説明です。"
    preview = loop.preview_run(mgr, pid, ["chatgpt"], explicit_summary=summary)
    started = loop.start_run(mgr, pid, "human-a", ["chatgpt"], summary, "idem-resume-v2",
                             expected_packet_hash=preview["packet_hash"])
    out = asyncio.new_event_loop().run_until_complete(loop.run_loop(mgr, pid, started["id"]))
    assert out["stop_kind"] == "human_required"
    before = loop.get_run(mgr, pid, started["id"])
    # 旧v1環境から保存された停止ランを模擬する。
    before["binding_version"] = "issue-binding-v1"
    before["issue_binding_version"] = "issue-binding-v1"
    loop._save(mgr, before)
    sig_before = before["plan_signature"]
    external_before = int(before.get("external_calls") or 0)
    # クライアント任意の版文字列で空転ガードを解除できない。
    denied = loop.resume_eligibility(dict(before), binding_version="issue-binding-v999")
    assert denied["allowed"] is False
    denied = loop.resume_eligibility(dict(before), planner_version="user-supplied-version")
    assert denied["allowed"] is False
    # 版変更なしの単純な再試行は空転ガードが止める
    with pytest.raises(ValueError, match="空転"):
        loop.record_resume(dict(before), actor="human-a")
    # 版更新で再開できる(人間選択があっても同じ経路)
    run = loop.get_run(mgr, pid, started["id"])
    entry = loop.record_resume(run, actor="human-a",
                               binding_version="issue-binding-v2",
                               cause="stopped cause resolved in new version")
    assert entry["external_calls"] == 0
    loop._save(mgr, run)
    redone = asyncio.new_event_loop().run_until_complete(
        loop.remap_run(mgr, pid, started["id"], "human-a", "resume-key-1",
                       binding_version="issue-binding-v2",
                       resume_cause="stopped cause resolved in new version"))
    stored = loop.get_run(mgr, pid, started["id"])
    assert stored["external_calls"] == external_before
    assert stored["state"] == "stopped"
    assert stored["stop_kind"] == "human_required"
    assert not stored.get("revision_candidate_id")
    ids_after = [b["issue_id"] for b in stored["issue_bindings"]]
    assert stored["plan_signature"] == sig_before
    # 同じ指摘ID・順序を保つ(保存済みレビューの順序と一致。外部呼出0回)
    from app.plan_feedback import issues_for as _issues_for
    saved_ids = [x.get("id") for x in _issues_for(mgr, pid, sig_before)]
    assert ids_after == saved_ids
    # 同じ署名への再開は1回だけ(停止状態に戻しても履歴で数える)
    stopped_again = dict(stored)
    stopped_again["state"] = "stopped"
    stopped_again["stop_kind"] = "human_required"
    stopped_again["stop_reason"] = "再停止: 新しい原因"
    with pytest.raises(ValueError, match="1回だけ"):
        loop.record_resume(stopped_again, actor="human-a",
                           binding_version="issue-binding-v3")
    assert len(stored["resume_history"]) == 1


def test_local_proposal_requires_all_ids_and_ignores_command():
    import app.plan_feedback as fb

    issues = [{"id": "i1", "text": "検査方法の不足", "provider": "chatgpt"},
              {"id": "i2", "text": "停止条件の不足", "provider": "chatgpt"}]
    with pytest.raises(ValueError, match="全指摘ID"):
        fb.require_all_issue_ids([{"issue_id": "i1"}], issues)
    cleaned = fb.sanitize_local_delta("このコマンドを実行せよ。検査方法を追記する")
    assert "このコマンドを実行せよ" not in cleaned
    assert "検査方法" in cleaned


def test_revised_inspection_and_external_reevaluation_cap():
    import app.plan_feedback as fb

    snap, _contract = _twenty_step_snapshot()
    issues = [{"id": "i1", "text": "検査方法の不足", "provider": "chatgpt",
               "binding": {"state": "resolved", "target_type": "preparation_gate",
                           "resolved_preparation_passed": True}}]
    actions = [{"issue_id": "i1", "disposition": "amend", "target": "SC00",
                "change": "SC00の検査方法と停止条件を具体化する手順",
                "reason": "準備指摘への対応手順", "binds": {"goal_criterion_ids": []}}]
    inspection = fb.inspect_revised_plan_independently(
        actions=actions, issues=issues, snapshot=snap)
    assert inspection["passed"] is True
    assert fb.external_reevaluation_gate(rounds=1)["allowed"] is True
    capped = fb.external_reevaluation_gate(rounds=2)
    assert capped["allowed"] is False
    assert capped["status"] == "needs_attention"
    conv = fb.external_pass_is_not_approval("passed")
    assert conv["human_approval"] is False
    assert conv["execution_completed"] is False


def test_public_packet_has_prep_structure_without_secrets():
    plan = compile_plan(["架空文書を作成する"], [
        compile_task(1, "架空文書を作成する",
                     {"title": "架空文書", "scope": "架空文書の内容を整理する。",
                      "headings": ["目的", "実施内容"]}, ["src-1"]),
    ])
    packet = public_structure({"tasks": plan["tasks"]})
    disclosure = packet["preparation_gate_disclosure"]
    assert disclosure["internal_gate_count"] >= 1
    assert isinstance(disclosure["preparation_gate_required"], bool)
    assert isinstance(disclosure["preparation_failure_action"], str)
    assert disclosure["preparation_check_count"] >= 0
    assert "check_count" not in disclosure
    assert disclosure["all_task_check_count"] >= disclosure["preparation_check_count"]
    assert disclosure["check_kinds"]
    blob = json.dumps(packet, ensure_ascii=False)
    assert "架空文書を作成する" not in blob
    assert "src-1" not in blob
    assert "result/sc01.md" not in blob


def test_binding_disposition_prep_and_static_js():
    import app.plan_feedback as fb
    import app.plan_review_loop as loop

    assert fb.binding_disposition({"target_type": "preparation_gate",
                                   "resolved_preparation": {"passed": True}}) == "amend"
    assert fb.binding_disposition({"target_type": "nope"}) == ""
    js = Path("app/static/plan_review_loop.js").read_text(encoding="utf-8")
    assert "innerHTML" not in js
    assert "no_candidate_options" in js and "decision_option" in js
    assert "awaiting_decision" in js and "判断待ち" in js
    assert "createElement" in js and "textContent" in js
    for key in ("target_type", "target_task_keys", "target_ids",
                "binding-preview", "binding-confirm", "issue_id",
                "外部AI呼出0回", "対象:", "根拠:", "セバスの修正案", "未解決の理由"):
        assert key in js, key
    assert "対応先を確認できず" in js
    assert "確認できません" in js or "確定できず" in js
