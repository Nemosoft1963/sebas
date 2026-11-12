import asyncio
import json
import threading

from app.project_manager import ProjectOrchestrator
from app.structured_planning import compile_plan, compile_task, contract_of
from tests.test_generic_goal_checks import _manager, _write_markdown
from app.requirement_item_ledger import latest_ledger


def test_final_verification_runs_in_worker_thread_for_async_llm():
    main_thread = threading.get_ident()
    task = {"id":"final-id","task_key":"final_verification",
            "acceptance_criteria":json.dumps({"schema":"local-cowork-plan/v1","final_verification":True})}
    class Memory:
        def get_mission(self, _pid):
            return {"tasks":[task]}
        def get_project(self, _pid):
            return {"workspace_path":""}
    class Fake:
        memory = Memory()
        def _execute_structured_final_verification(self, _project, _pid, _task):
            return threading.get_ident()
    actual = asyncio.run(ProjectOrchestrator._execute_legacy_task(Fake(), "demo", "final-id"))
    assert actual != main_thread


def test_first_new_plan_creates_ledger_without_legacy_missing_note(tmp_path):
    manager, pid = _manager(tmp_path, "架空の説明文を作る", "概要を説明する")
    item = compile_task(1, "概要を説明する", {
        "title":"概要文書", "scope":"概要の内容を整理する",
        "headings":["概要","詳細"], "depends_on":[]
    }, [])
    plan = compile_plan(["概要を説明する"], [item])
    manager.memory.replace_plan(pid, plan["summary"], plan["tasks"])
    mission = manager.memory.get_mission(pid)
    project = manager.memory.get_project(pid)
    for task in mission["tasks"][:-1]:
        for output in contract_of(task)["outputs"]:
            _write_markdown(manager, pid, output["path"], output["required_headings"])
        manager.memory.update_task(task["id"], "completed", result="架空文書作成")
    final = manager.memory.get_mission(pid)["tasks"][-1]
    try:
        report = manager._execute_structured_final_verification(project, pid, final)
    except Exception as exc:
        # 項目の意味内容不足などは別の失敗として扱うが、初回台帳不足ではない。
        assert "項目別検証台帳が無い" not in str(exc)
    else:
        assert "項目別検証台帳が無い" not in report
    assert latest_ledger(manager.memory.path, pid) is not None

def test_missing_item_verifier_stays_untestable(tmp_path, monkeypatch):
    import sys
    from app.completion_gate import evaluate
    from tests.test_generic_goal_checks import setup_two_artifact_criteria, complete_all_artifacts
    manager, pid = setup_two_artifact_criteria(tmp_path)
    complete_all_artifacts(manager, pid)
    before = evaluate(manager, pid)
    assert any(row["status"] == "PASS" for row in before["criteria"])
    monkeypatch.setitem(sys.modules, "app.requirement_verification", None)
    after = evaluate(manager, pid)
    assert all(row["status"] != "PASS" for row in after["criteria"])
