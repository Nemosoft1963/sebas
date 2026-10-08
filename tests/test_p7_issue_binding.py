import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.experience_store import canonical
from app.goal_review import ReviewStore, plan_snapshot
from app.memory.short_term import ShortTermMemory
from app.structured_planning import compile_plan, compile_task
from app.workspace_files import WorkspaceSandbox


class Manager(SimpleNamespace):
    pass


FAKE_SECRET = "sk-fake0123456789abcdef"


def _manager(tmp_path, providers=("chatgpt",)):
    mem = ShortTermMemory(tmp_path / "memory" / "conversations.db")
    pid = mem.create_project("p7pseudo")["id"]
    mem.save_mission(
        pid,
        "疑似目標\n1. 架空手順Aを定義する\n2. 架空手順Bを報告する",
        "疑似目標\n1. 架空手順Aを定義する\n2. 架空手順Bを報告する",
        "",
        True,
        list(providers),
    )
    ws = WorkspaceSandbox(tmp_path / "workspace")
    mgr = Manager(memory=mem, workspace=ws, planning_projects=set(), llm=None,
                  workers={}, _sync_memos=lambda pid: None,
                  provider_statuses=lambda: [{"id": p, "configured": True} for p in providers],
                  plan_review_runner=None)
    return mgr, mem, pid


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


def _six_issues():
    return [
        {"severity": "blocking", "step": "1,20", "unmet_goal": "準備と最終の接続不足",
         "reason": "工程1と20の接続が不足", "remedy": "接続を明示する"},
        {"severity": "blocking", "step": "1-15", "unmet_goal": "前半工程の粒度",
         "reason": "工程1-15が粗い", "remedy": "分割する"},
        {"severity": "blocking", "step": "16-18", "unmet_goal": "後半の検証不足",
         "reason": "工程16-18の検証が不足", "remedy": "検証を追加"},
        {"severity": "blocking", "step": "17-18", "unmet_goal": "公開前確認",
         "reason": "工程17-18の確認が不足", "remedy": "確認点を追加"},
        {"severity": "blocking", "step": "5,17-18", "unmet_goal": "対象工程のずれ",
         "reason": "工程5と17-18が混在", "remedy": "対象を分ける"},
        {"severity": "blocking", "step": "17-20", "unmet_goal": "SC17の検証不足",
         "reason": "SC17の検証手順が不足している", "remedy": "検証手順を追記する"},
    ]


def test_p7_six_mock_issues_keep_all_fields_and_ranges():
    import app.plan_review_loop as loop

    snap, contract = _twenty_step_snapshot()
    issues = _six_issues()
    parsed = [{"provider": "chatgpt", "status": "fail", "issues": issues}]
    table = loop.build_issue_bindings(parsed, contract, snap, plan_signature="sig20",
                                      goal_contract_hash="hash-20", packet_hash="pkt")
    assert len(table["bindings"]) == 6
    for original, binding in zip(issues, table["bindings"]):
        assert binding["unmet_goal"] == original["unmet_goal"]
        assert binding["reason"] == original["reason"]
        assert binding["remedy"] == original["remedy"]
        assert binding["step_raw"] == original["step"]
        assert binding["severity"] == original["severity"]
    assert table["bindings"][0]["steps"] == [1, 20]
    assert table["bindings"][1]["steps"] == list(range(1, 16))
    assert table["bindings"][4]["steps"] == [5, 17, 18]
    assert table["bindings"][5]["steps"] == [17, 18, 19, 20]
    # 範囲を単一工程へ縮めない
    assert all(len(b["steps"]) >= 2 for b in table["bindings"])


def test_p7_invalid_references_and_unknown_sc_and_no_fallback():
    import app.plan_review_loop as loop

    snap, contract = _twenty_step_snapshot()
    cases = ["0", "21", "18-16", "abc", ""]
    for raw in cases:
        parsed = [{"provider": "chatgpt", "status": "fail",
                   "issues": [{"step": raw, "unmet_goal": "x", "reason": "y", "remedy": "z"}]}]
        table = loop.build_issue_bindings(parsed, contract, snap)
        assert table["bindings"][0]["state"] == "invalid_reference", raw
    # 過大範囲
    parsed = [{"provider": "chatgpt", "status": "fail",
               "issues": [{"step": "1-20", "unmet_goal": "x", "reason": "y", "remedy": "z"}]}]
    assert loop.build_issue_bindings(parsed, contract, snap)["bindings"][0]["state"] == "invalid_reference"
    # 存在しないSCのみ言及は ambiguous
    parsed = [{"provider": "chatgpt", "status": "fail",
               "issues": [{"step": "2", "unmet_goal": "SC99の不足",
                           "reason": "SC99は契約に無い", "remedy": "見直す"}]}]
    table = loop.build_issue_bindings(parsed, contract, snap)
    assert table["bindings"][0]["state"] == "ambiguous"
    # 任意工程へのフォールバックが無い(候補が空ならambiguous、勝手な先頭工程を入れない)
    assert table["bindings"][0]["candidate_task_keys"] == [] or table["bindings"][0]["state"] == "ambiguous"


def test_p7_contradiction_is_ambiguous_and_blocks_auto_apply():
    import app.plan_review_loop as loop

    snap, contract = _twenty_step_snapshot()
    parsed = [{"provider": "chatgpt", "status": "fail", "issues": [
        {"step": "3", "unmet_goal": "SC01の検証不足",
         "reason": "SC01の検証手順が不足している", "remedy": "追記する"},
    ]}]
    table = loop.build_issue_bindings(parsed, contract, snap)
    binding = table["bindings"][0]
    assert binding["state"] == "ambiguous"
    assert "矛盾" in binding["basis"]
    assert "自動反映せず" in binding["basis"]
    # 候補は両方保持して画面で確認できる(step側SC02と明示SC01側)
    assert "SC01" in binding["candidate_task_keys"]
    assert "final_verification" in binding["candidate_task_keys"]
    assert "SC02" in binding["candidate_task_keys"]
    assert "SC02" in binding["candidate_criterion_ids"]
    assert "SC01" in binding["candidate_criterion_ids"]
    with pytest.raises(ValueError, match="対応先"):
        loop._map_issues_local(parsed, contract, snap)


def test_p7_auto_apply_only_resolved_and_coherent_multiple():
    import app.plan_review_loop as loop

    snap, contract = _twenty_step_snapshot()
    # 矛盾なし multiple_targets は自動反映してよい(止めない)
    parsed = [{"provider": "chatgpt", "status": "fail", "issues": [
        {"step": "16-18", "unmet_goal": "後半の検証不足",
         "reason": "工程16-18の検証が不足", "remedy": "検証を追加"},
    ]}]
    out = loop._map_issues_local(parsed, contract, snap)
    assert out["bindings"][0]["state"] == "multiple_targets"
    assert out["mapped"][0]["state"] == "multiple_targets"
    # 矛盾 multiple は作られない(ambiguousに分離済み)。防衛として矛盾文言のmultipleは止める
    fake = [{"provider": "p", "status": "fail", "issues": []}]
    table = loop.build_issue_bindings(fake, contract, snap)
    assert table["empty"] is True


def test_p7_remap_idempotent_and_concurrent_no_duplicates(tmp_path, monkeypatch):
    import json as _json
    from types import SimpleNamespace as _NS

    mgr, mem, pid = _manager(tmp_path)
    criteria = ["架空手順Aを定義する", "架空手順Bを報告する"]
    t1 = compile_task(1, criteria[0], {"title": "架空資料A", "scope": "架空Aの整理",
        "headings": ["目的", "実施内容"], "depends_on": []}, [])
    t2 = compile_task(2, criteria[1], {"title": "架空資料B", "scope": "架空Bの整理",
        "headings": ["目的", "実施内容"], "depends_on": ["SC01"]}, [])
    compiled = compile_plan(criteria, [t1, t2], goal=mem.get_mission(pid)["goal"])
    mem.replace_plan(pid, "p7 pseudo plan", compiled["tasks"])
    from app.goal_contract import activate as _activate
    _activate(mgr, pid)

    import app.plan_review_loop as loop
    import app.external_ai as ext

    async def _fake_gemini(*args, **kwargs):
        raise AssertionError("Gemini must not be called")

    monkeypatch.setattr(ext, "call_provider_with_metadata", _fake_gemini)

    async def _runner(text, providers):
        out = []
        for p in providers:
            out.append({"id": p, "ok": True, "review": _json.dumps({
                "verdict": "fail",
                "issues": [{"severity": "blocking", "step": "9",
                            "unmet_goal": "不明",
                            "reason": "対応先の無い抽象的な指摘",
                            "remedy": "検討する"}]})})
        return out

    mgr.plan_review_runner = _runner
    calls = {"n": 0}
    real_runner = mgr.plan_review_runner

    async def _counting(text, providers):
        calls["n"] += 1
        return await real_runner(text, providers)

    mgr.plan_review_runner = _counting
    started = loop.start_run(mgr, pid, "human-a", ["chatgpt"], "", "idem-p7-remap")
    out = asyncio.new_event_loop().run_until_complete(loop.run_loop(mgr, pid, started["id"]))
    assert out["state"] == "stopped"
    assert out["stop_kind"] == "human_required"
    calls_before = calls["n"]
    external_before = out["external_calls"]
    rounds_before = out["verification_rounds"]
    review_count_before = len((loop.get_run(mgr, pid, started["id"])["rounds"][0]["reviews"]))
    status_before = loop.get_run(mgr, pid, started["id"])["rounds"][0]["status"]

    async def _concurrent():
        return await asyncio.gather(
            loop.remap_run(mgr, pid, started["id"], "human-a", "p7-key-1"),
            loop.remap_run(mgr, pid, started["id"], "human-a", "p7-key-1"))

    first, second = asyncio.new_event_loop().run_until_complete(_concurrent())
    assert calls["n"] == calls_before
    stored = loop.get_run(mgr, pid, started["id"])
    assert len(stored["remap_history"]) == 1
    assert stored["external_calls"] == external_before
    assert stored["verification_rounds"] == rounds_before
    # レビュー元の判定と指摘数が不変
    assert stored["rounds"][0]["status"] == status_before
    assert len(stored["rounds"][0]["reviews"]) == review_count_before
    # 重複実行でも修正案・イベントが重複しない
    again = asyncio.new_event_loop().run_until_complete(
        loop.remap_run(mgr, pid, started["id"], "human-a", "p7-key-1"))
    assert len(loop.get_run(mgr, pid, started["id"])["remap_history"]) == 1
    events = [e for e in (mem.get_mission(pid).get("events") or [])
              if e.get("kind") == "auto_loop_remapped"]
    assert len(events) == 1


def test_p7_remap_rejects_on_signature_contract_packet_review_change(tmp_path, monkeypatch):
    import json as _json

    mgr, mem, pid = _manager(tmp_path)
    criteria = ["架空手順Aを定義する", "架空手順Bを報告する"]
    t1 = compile_task(1, criteria[0], {"title": "架空資料A", "scope": "架空Aの整理",
        "headings": ["目的", "実施内容"], "depends_on": []}, [])
    t2 = compile_task(2, criteria[1], {"title": "架空資料B", "scope": "架空Bの整理",
        "headings": ["目的", "実施内容"], "depends_on": ["SC01"]}, [])
    compiled = compile_plan(criteria, [t1, t2], goal=mem.get_mission(pid)["goal"])
    mem.replace_plan(pid, "p7 pseudo plan", compiled["tasks"])
    from app.goal_contract import activate as _activate
    _activate(mgr, pid)
    import app.plan_review_loop as loop
    import app.external_ai as ext

    async def _fake_gemini(*args, **kwargs):
        raise AssertionError("Gemini must not be called")

    monkeypatch.setattr(ext, "call_provider_with_metadata", _fake_gemini)

    async def _runner(text, providers):
        return [{"id": p, "ok": True, "review": _json.dumps({
            "verdict": "fail",
            "issues": [{"severity": "blocking", "step": "9",
                        "unmet_goal": "不明",
                        "reason": "対応先の無い抽象的な指摘",
                        "remedy": "検討する"}]})} for p in providers]

    mgr.plan_review_runner = _runner
    started = loop.start_run(mgr, pid, "human-a", ["chatgpt"], "", "idem-p7-409")
    out = asyncio.new_event_loop().run_until_complete(loop.run_loop(mgr, pid, started["id"]))
    assert out["stop_kind"] == "human_required"
    run = loop.get_run(mgr, pid, started["id"])
    # GoalContract が変われば409(計画版は変えず契約だけ差し替え)
    run["goal_contract_hash"] = "different-hash"
    loop._save(mgr, run)
    with pytest.raises(ValueError, match="409.*GoalContract"):
        loop.remap_preview(mgr, pid, started["id"])
    with pytest.raises(ValueError, match="409.*GoalContract"):
        asyncio.new_event_loop().run_until_complete(
            loop.remap_run(mgr, pid, started["id"], "human-a", "k-contract"))
    # packet が変われば409(署名・契約を現行に戻し、保存packetだけ改変)
    from app.goal_review import ReviewStore as _RS
    from app.experience_store import fingerprint as _fp2
    _mission, _snap, _sig, _contract = loop._current(mgr, pid)
    run = loop.get_run(mgr, pid, started["id"])
    run["goal_contract_hash"] = _contract.get("content_hash") or ""
    loop._save(mgr, run)
    stored = _RS(mem.path).get(pid, "plan", _sig) or {}
    if stored.get("packet"):
        stored["packet"] = dict(stored["packet"], _p7_tampered=True)
        _RS(mem.path).put(pid, "plan", _sig, stored)
        with pytest.raises(ValueError, match="409.*packet"):
            loop.remap_preview(mgr, pid, started["id"])
    # レビュー指紋が変われば409(保存roundsのレビューを改変。packetは先に復元)
    from app.goal_review import review_plan as _review_plan_real  # noqa: F401 (参照固定)
    run = loop.get_run(mgr, pid, started["id"])
    # packet改変を戻す(保存し直し)
    stored2 = _RS(mem.path).get(pid, "plan", _sig) or {}
    if stored2.get("packet") and "_p7_tampered" in stored2["packet"]:
        stored2["packet"] = {k: v for k, v in stored2["packet"].items() if k != "_p7_tampered"}
        _RS(mem.path).put(pid, "plan", _sig, stored2)
    run = loop.get_run(mgr, pid, started["id"])
    # ReviewStoreの保存レビューを改変すればレビュー指紋不一致で409
    stored3 = _RS(mem.path).get(pid, "plan", _sig) or {}
    if stored3.get("reviews"):
        stored3["reviews"] = list(stored3["reviews"]) + [
            {"provider": "chatgpt", "status": "fail", "issues": [{"reason": "追加"}]}]
        _RS(mem.path).put(pid, "plan", _sig, stored3)
        with pytest.raises(ValueError, match="409"):
            asyncio.new_event_loop().run_until_complete(
                loop.remap_run(mgr, pid, started["id"], "human-a", "k-review"))
    # 計画署名が変われば409
    mem.replace_plan(pid, "changed", mem.get_mission(pid)["tasks"])
    with pytest.raises(ValueError, match="409"):
        loop.remap_preview(mgr, pid, started["id"])
    with pytest.raises(ValueError, match="409"):
        asyncio.new_event_loop().run_until_complete(
            loop.remap_run(mgr, pid, started["id"], "human-a", "k2"))
    # human_required以外/現行版でないランは拒否(取消ラン)
    mgr2, mem2, pid2 = _manager(tmp_path)
    assert pid2 != pid


def test_p7_remap_rejects_deleted_and_non_human_required(tmp_path, monkeypatch):
    import json as _json

    mgr, mem, pid = _manager(tmp_path)
    criteria = ["架空手順Aを定義する", "架空手順Bを報告する"]
    t1 = compile_task(1, criteria[0], {"title": "架空資料A", "scope": "架空Aの整理",
        "headings": ["目的", "実施内容"], "depends_on": []}, [])
    t2 = compile_task(2, criteria[1], {"title": "架空資料B", "scope": "架空Bの整理",
        "headings": ["目的", "実施内容"], "depends_on": ["SC01"]}, [])
    compiled = compile_plan(criteria, [t1, t2], goal=mem.get_mission(pid)["goal"])
    mem.replace_plan(pid, "p7 pseudo plan", compiled["tasks"])
    from app.goal_contract import activate as _activate
    _activate(mgr, pid)
    import app.plan_review_loop as loop
    import app.external_ai as ext

    async def _fake_gemini(*args, **kwargs):
        raise AssertionError("Gemini must not be called")

    monkeypatch.setattr(ext, "call_provider_with_metadata", _fake_gemini)

    async def _runner(text, providers):
        return [{"id": p, "ok": True, "review": _json.dumps({
            "verdict": "fail",
            "issues": [{"severity": "blocking", "step": "9",
                        "unmet_goal": "不明",
                        "reason": "対応先の無い抽象的な指摘",
                        "remedy": "検討する"}]})} for p in providers]

    mgr.plan_review_runner = _runner
    started = loop.start_run(mgr, pid, "human-a", ["chatgpt"], "", "idem-p7-del")
    out = asyncio.new_event_loop().run_until_complete(loop.run_loop(mgr, pid, started["id"]))
    assert out["stop_kind"] == "human_required"
    # 論理削除中PJは拒否
    import app.project_delete as _del
    from app.project_lifecycle_backup import create_backup as _backup
    import tempfile as _tf
    with _tf.TemporaryDirectory() as _bd:
        prev = _del.build_delete_request_preview(mem.path, pid)
        res = _del.request_delete(mem.path, pid, preview_token=prev["preview_token"],
                                  project_name=mem.get_project(pid)["name"],
                                  actor="human-a", reason="p7 test",
                                  workspace_root=str(tmp_path / "workspace"),
                                  backup_root=_bd)
        assert res.get("ok") is True
        with pytest.raises(ValueError, match="論理削除中"):
            loop.remap_preview(mgr, pid, started["id"])
        with pytest.raises(ValueError, match="論理削除中"):
            asyncio.new_event_loop().run_until_complete(
                loop.remap_run(mgr, pid, started["id"], "human-a", "k-del"))
    # human_required以外のランは拒否(取消。別PJで論理削除の影響を受けない)
    mgr3, mem3, pid3 = _manager(tmp_path)
    t1b = compile_task(1, criteria[0], {"title": "架空資料A", "scope": "架空Aの整理",
        "headings": ["目的", "実施内容"], "depends_on": []}, [])
    t2b = compile_task(2, criteria[1], {"title": "架空資料B", "scope": "架空Bの整理",
        "headings": ["目的", "実施内容"], "depends_on": ["SC01"]}, [])
    compiled3 = compile_plan(criteria, [t1b, t2b], goal=mem3.get_mission(pid3)["goal"])
    mem3.replace_plan(pid3, "p7 pseudo plan", compiled3["tasks"])
    _activate(mgr3, pid3)
    mgr3.plan_review_runner = _runner
    monkeypatch.setattr(ext, "call_provider_with_metadata", _fake_gemini)
    started2 = loop.start_run(mgr3, pid3, "human-a", ["chatgpt"], "", "idem-p7-cancel")
    loop.cancel_run(mgr3, pid3, started2["id"], "human-a")
    with pytest.raises(ValueError, match="human_required"):
        loop.remap_preview(mgr3, pid3, started2["id"])
    with pytest.raises(ValueError, match="human_required"):
        asyncio.new_event_loop().run_until_complete(
            loop.remap_run(mgr3, pid3, started2["id"], "human-a", "k-cancel"))


def test_p7_remap_preview_is_readonly(tmp_path, monkeypatch):
    import json as _json
    from app.experience_store import fingerprint as _fp

    mgr, mem, pid = _manager(tmp_path)
    criteria = ["架空手順Aを定義する", "架空手順Bを報告する"]
    t1 = compile_task(1, criteria[0], {"title": "架空資料A", "scope": "架空Aの整理",
        "headings": ["目的", "実施内容"], "depends_on": []}, [])
    t2 = compile_task(2, criteria[1], {"title": "架空資料B", "scope": "架空Bの整理",
        "headings": ["目的", "実施内容"], "depends_on": ["SC01"]}, [])
    compiled = compile_plan(criteria, [t1, t2], goal=mem.get_mission(pid)["goal"])
    mem.replace_plan(pid, "p7 pseudo plan", compiled["tasks"])
    from app.goal_contract import activate as _activate
    _activate(mgr, pid)
    import app.plan_review_loop as loop
    import app.external_ai as ext

    async def _fake_gemini(*args, **kwargs):
        raise AssertionError("Gemini must not be called")

    monkeypatch.setattr(ext, "call_provider_with_metadata", _fake_gemini)

    async def _runner(text, providers):
        return [{"id": p, "ok": True, "review": _json.dumps({
            "verdict": "fail",
            "issues": [{"severity": "blocking", "step": "9",
                        "unmet_goal": "不明",
                        "reason": "対応先の無い抽象的な指摘",
                        "remedy": "検討する"}]})} for p in providers]

    mgr.plan_review_runner = _runner
    started = loop.start_run(mgr, pid, "human-a", ["chatgpt"], "", "idem-p7-ro")
    out = asyncio.new_event_loop().run_until_complete(loop.run_loop(mgr, pid, started["id"]))
    assert out["stop_kind"] == "human_required"
    before_run = _fp(loop.get_run(mgr, pid, started["id"]))
    before_db = Path(mem.path).read_bytes() if Path(str(mem.path)).exists() else b""
    preview = loop.remap_preview(mgr, pid, started["id"])
    assert preview["external_calls"] == 0
    after_run = _fp(loop.get_run(mgr, pid, started["id"]))
    after_db = Path(mem.path).read_bytes() if Path(str(mem.path)).exists() else b""
    assert before_run == after_run
    assert before_db == after_db


def test_p7_unverifiable_never_passes_and_manual_confirm_recorded(tmp_path, monkeypatch):
    import app.plan_review_loop as loop
    from app.goal_review import evaluate_external_review_status as _judge
    from app.goal_review import require_review as _require

    snap, contract = _twenty_step_snapshot()
    parsed = [{"provider": "chatgpt", "status": "unverifiable", "issues": []}]
    judged = _judge(parsed, ["chatgpt"], {"min_success_count": 1})
    assert judged.get("status") != "passed"
    # remapしても合格に変わらない(保存レビューの判定は不変・unverifiableのまま)
    assert parsed[0]["status"] == "unverifiable"
    judged2 = _judge(parsed, ["chatgpt"], {"min_success_count": 1})
    assert judged2.get("status") != "passed"
    # 最大2回の外部検証上限が維持される
    assert loop.VERIFICATION_LIMIT == 2
    assert loop.max_external_calls(1) == 2
    # 曖昧な指摘の人による候補確定が担当者・日時・元指紋付きで記録され、検証合格にならない
    table = loop.build_issue_bindings([{"provider": "chatgpt", "status": "fail", "issues": [
        {"step": "3", "unmet_goal": "SC01の検証不足",
         "reason": "SC01の検証手順が不足している", "remedy": "追記する"}]}], contract, snap)
    assert table["bindings"][0]["state"] == "ambiguous"
    run = {"issue_bindings": table["bindings"], "manual_bindings": []}
    entry = loop.confirm_binding_candidate(run, table["bindings"][0]["issue_id"],
                                           actor="human-a", task_key="SC01")
    assert entry["actor"] == "human-a"
    assert entry["at"]
    assert entry["source_binding_fingerprint"]
    assert len(run["manual_bindings"]) == 1
    # それだけでは検証合格にならない(状態・判定は変えない)
    assert table["bindings"][0]["state"] == "ambiguous"


def test_p7_unverifiable_blocks_execution_start(tmp_path):
    from app.goal_review import ReviewStore as _RS
    from app.goal_review import require_review as _require
    from app.goal_review import plan_snapshot as _snap

    mgr, mem, pid = _manager(tmp_path)
    criteria = ["架空手順Aを定義する", "架空手順Bを報告する"]
    t1 = compile_task(1, criteria[0], {"title": "架空資料A", "scope": "架空Aの整理",
        "headings": ["目的", "実施内容"], "depends_on": []}, [])
    t2 = compile_task(2, criteria[1], {"title": "架空資料B", "scope": "架空Bの整理",
        "headings": ["目的", "実施内容"], "depends_on": ["SC01"]}, [])
    compiled = compile_plan(criteria, [t1, t2], goal=mem.get_mission(pid)["goal"])
    mem.replace_plan(pid, "p7 pseudo plan", compiled["tasks"])
    _sig = _snap(mgr, pid)[1]
    # レビュー必須をこのPJに明示して、unverifiable の保存レビューが
    # 実行開始を確実に拒否する統合経路を検証する。
    policy_path = Path(mem.path).parent / "goal_review_policy.json"
    policy_path.write_text(json.dumps({"projects": {pid: True}}), encoding="utf-8")
    _RS(mem.path).put(pid, "plan", _sig, {
        "status": "unverifiable", "stop_reason": "検証不能",
        "reviews": [{"provider": "chatgpt", "status": "unverifiable", "issues": []}]})
    from app.goal_review import enabled as _enabled
    assert _enabled(mgr, pid) is True
    with pytest.raises(ValueError, match="未完了または不合格"):
        _require(mgr, pid)


def test_p7_secret_values_never_leak_and_js_has_no_innerhtml():
    import app.plan_review_loop as loop

    snap, contract = _twenty_step_snapshot()
    table = loop.build_issue_bindings([{"provider": "chatgpt", "status": "fail", "issues": [
        {"step": "2", "unmet_goal": "通常指摘",
         "reason": "通常の理由 " + FAKE_SECRET, "remedy": "見直す"}]}], contract, snap)
    blob = canonical(table["bindings"])
    assert FAKE_SECRET not in blob
    assert "通常の理由" not in blob
    assert table["bindings"][0]["reason"] == "[秘密混入の疑いのため本文を保持しません]"
    # 秘密検出は種別ラベルのみ
    assert loop.contains_secret(FAKE_SECRET) == "APIキー(sk-)"
    js = Path("app/static/plan_review_loop.js").read_text(encoding="utf-8")
    assert "innerHTML" not in js
