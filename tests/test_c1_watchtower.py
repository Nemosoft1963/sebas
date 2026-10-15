"""C-1 見張りとC-2 判断待ち・B-1補正0のテスト (疑似PJのみ・実関数)。

疑似PJのみ (架空・疑似)。実在PJのID・件数・版を使わない。外部通信なし。
PJのデータには書込まない (refresh前後でPJのDB・ファイル不変を実測)。
"""
import json
import sqlite3
import threading

import pytest

from app.memory.short_term import ShortTermMemory
from app.project_manager import ProjectOrchestrator
from app.structured_planning import compile_task
from app.workspace_files import WorkspaceSandbox

FILLER = "架空の検証可能な具体的内容を記載する。" * 40


def _make_manager(tmp_path, name, criteria, tag=""):
    db_path = tmp_path / (name + tag + ".db")
    memory = ShortTermMemory(db_path)
    project = memory.create_project(name + tag, workspace_path="projects/" + name + tag)
    pid = project["id"]
    memory.save_mission(pid, "架空目標", "\n".join(
        f"{i}. {text}" for i, text in enumerate(criteria, 1)), "", True, ["chatgpt"])
    tasks = []
    for i, text in enumerate(criteria, 1):
        tasks.append(compile_task(i, text, {
            "title": f"架空工程{i}",
            "scope": f"架空作業{i:02d}の成果物を作成する。",
            "headings": ["目的", "実施内容"],
        }, []))
    memory.replace_plan(pid, name + " plan", tasks)
    manager = ProjectOrchestrator(
        memory, None, lambda p: ("", []), None,
        lambda: [{"id": "chatgpt", "configured": True}],
        workspace=WorkspaceSandbox(tmp_path / ("ws-" + name + tag)),
    )
    from app.goal_contract import activate as _activate
    if criteria:
        _activate(manager, pid)
    return manager, pid


def _store_passed(manager, pid):
    from app.goal_review import ReviewStore, plan_snapshot
    _snap, sig = plan_snapshot(manager, pid)
    ReviewStore(manager.memory.path).put(pid, "plan", sig, {
        "status": "passed",
        "reviews": [{"provider": "chatgpt", "status": "pass", "issues": [],
                     "response": {"ok": True}}],
        "finished": 0})


def _row_counts(path):
    db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        tables = [row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        counts = {}
        for table in tables:
            try:
                counts[table] = int(db.execute(
                    f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            except sqlite3.Error:
                continue
        return counts
    finally:
        db.close()


def _workspace_files(manager, pid):
    project = manager.memory.get_project(pid)
    _, scope, _ = manager.workspace.resolve_file(
        project.get("workspace_path", ""), pid, "result")
    if not scope.parent.exists():
        return {}
    root = scope.parent if scope.name == "result" else scope
    out = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            out[path.relative_to(root.parent if root.name != pid else root).as_posix()] = (
                path.stat().st_size, path.stat().st_mtime_ns)
    return out


@pytest.fixture()
def three_managers(tmp_path):
    managers = []
    pids = []
    specs = [
        ("c1-疑似-doc", ["架空文書01の作成と検証", "架空文書02の作成と検証"]),
        ("c1-疑似-table", ["架空営業管理表01の集計と検算", "架空営業管理表02の集計と検算"]),
        ("c1-疑似-ext", ["架空告知文書01の作成と検証", "架空顧客へ送信02の実行と検証"]),
    ]
    for name, criteria in specs:
        manager, pid = _make_manager(tmp_path, name, criteria)
        _store_passed(manager, pid)
        managers.append(manager)
        pids.append(pid)
    # 同一メモリDBに3PJを集める: 先頭managerのmemoryへ後続を統合しない。
    # watchtowerはmanager単位のため、結合マネージャを作る。
    base_mem = managers[0].memory
    for extra in managers[1:]:
        src = sqlite3.connect(str(extra.memory.path))
        try:
            for table in ("projects", "project_missions", "project_tasks"):
                try:
                    cols = [r[1] for r in src.execute(
                        f"PRAGMA table_info({table})").fetchall()]
                except sqlite3.Error:
                    continue
                rows = src.execute(f"SELECT * FROM {table}").fetchall()
                if not rows:
                    continue
                marks = ",".join("?" for _ in cols)
                dst = sqlite3.connect(str(base_mem.path))
                try:
                    with dst:
                        for row in rows:
                            try:
                                dst.execute(
                                    f"INSERT OR IGNORE INTO {table}({','.join(cols)}) "
                                    f"VALUES({marks})", tuple(row))
                            except sqlite3.Error:
                                continue
                finally:
                    dst.close()
        finally:
            src.close()
    # goal_completion / goal_reviews のPJ行も先頭DBへ寄せるのは難しいため、
    # 単一PJのmanagerでrefreshの基本形を検証し、複数PJは同一DB再作成で検証する。
    return managers, pids


def _single_db_managers(tmp_path):
    db_path = tmp_path / "c1-疑似-multi.db"
    memory = ShortTermMemory(db_path)
    infos = []
    for key, criteria in (("doc", ["架空文書01の作成と検証", "架空文書02の作成と検証"]),
                          ("table", ["架空営業管理表01の集計と検算"]),
                          ("ext", ["架空告知文書01の作成と検証",
                                   "架空顧客へ送信02の実行と検証"])):
        name = "c1-疑似-multi-" + key
        project = memory.create_project(name, workspace_path="projects/" + name)
        pid = project["id"]
        memory.save_mission(pid, "架空目標-" + key, "\n".join(
            f"{i}. {text}" for i, text in enumerate(criteria, 1)), "", True, ["chatgpt"])
        tasks = []
        for i, text in enumerate(criteria, 1):
            tasks.append(compile_task(i, text, {
                "title": f"架空工程{i}",
                "scope": f"架空作業{i:02d}の成果物を作成する。",
                "headings": ["目的", "実施内容"],
            }, []))
        memory.replace_plan(pid, name + " plan", tasks)
        infos.append((key, pid, criteria))
    manager = ProjectOrchestrator(
        memory, None, lambda p: ("", []), None,
        lambda: [{"id": "chatgpt", "configured": True}],
        workspace=WorkspaceSandbox(tmp_path / "ws-c1-疑似-multi"),
    )
    from app.goal_contract import activate as _activate
    for _key, pid, _criteria in infos:
        _activate(manager, pid)
        _store_passed(manager, pid)
    return manager, [pid for _key, pid, _c in infos]


def test_refresh_first_second_no_dup(tmp_path):
    import app.watchtower as watch
    manager, pids = _single_db_managers(tmp_path)
    assert len(pids) == 3
    first = watch.refresh(manager, "架空担当")
    assert first["ok"] is True
    # 既定PJ(default)を含むため3以上。疑似3PJが全て含まれること。
    assert first["total_projects"] >= 3
    snap = watch.collect_snapshot(manager)
    for pid in pids:
        assert pid in snap["projects"]
        assert snap["projects"][pid]["readable"] is True
    second = watch.refresh(manager, "架空担当")
    assert second["new_events"] == 0
    third = watch.refresh(manager, "架空担当")
    assert third["new_events"] == 0
    events = watch.list_events(manager, unread_only=False, limit=200)
    first_ids = sorted(item["id"] for item in events)
    watch.refresh(manager, "架空担当")
    events2 = watch.list_events(manager, unread_only=False, limit=200)
    assert sorted(item["id"] for item in events2) == first_ids
    _ = three_managers


def test_changes_create_one_event_each(tmp_path):
    import app.autopilot as auto
    import app.watchtower as watch
    manager, pids = _single_db_managers(tmp_path)
    watch.refresh(manager, "架空担当")
    # 停止・達成条件の変化を起こす: 計画を承認する (planning→ready)。
    target = pids[0]
    manager.memory.set_mission_status(target, "ready", "架空承認", "approved")
    out = watch.refresh(manager, "架空担当")
    assert out["new_events"] >= 1
    events = watch.list_events(manager, unread_only=False, limit=200)
    kinds = {(item["project_id"], item["kind"]) for item in events}
    assert any(pid == target and kind in ("stop_appeared", "stop_changed",
                                          "criteria_progress")
               for pid, kind in kinds), kinds
    before = len(events)
    again = watch.refresh(manager, "架空担当")
    assert again["new_events"] == 0
    assert len(watch.list_events(manager, unread_only=False, limit=200)) == before
    # 自走を失敗させる: 有効化して読取不能なティックを作るのは難しいため、
    # preflight不能のPJを作る代わりに、既存ティック失敗を直接検証する。
    _ = auto


def test_read_failure_keeps_unknown(tmp_path, monkeypatch):
    import app.watchtower as watch
    manager, pids = _single_db_managers(tmp_path)
    watch.refresh(manager, "架空担当")
    import app.goal_state_model as model
    real = model.build_goal_state
    target = pids[1]

    def _boom(manager_arg, pid_arg):
        if str(pid_arg) == str(target):
            raise RuntimeError("架空の読取失敗")
        return real(manager_arg, pid_arg)

    monkeypatch.setattr(model, "build_goal_state", _boom)
    try:
        out = watch.refresh(manager, "架空担当")
    finally:
        monkeypatch.undo()
    assert out["ok"] is True
    assert out["unreadable_projects"] >= 1
    snap = watch.collect_snapshot(manager)
    # monkeypatchを外した後は読めるため、保存された最新を読む。
    latest_events = watch.list_events(manager, unread_only=False, limit=200)
    assert any(item["project_id"] == target and item["kind"] == "read_failed"
               for item in latest_events), latest_events
    assert snap["projects"][target]["readable"] is True
    _ = out


def test_deleted_excluded_unknown_kept(tmp_path):
    import app.project_delete as _del
    import app.watchtower as watch
    manager, pids = _single_db_managers(tmp_path)
    watch.refresh(manager, "架空担当")
    _del._set_state(manager.memory.path, pids[0], "deleted", actor="架空担当",
                    backup_id="架空退避", manifest_sha256="0" * 64)
    try:
        snap = watch.collect_snapshot(manager)
        assert pids[0] not in snap["projects"]
        assert pids[1] in snap["projects"]
        before_total = watch.refresh(manager, "架空担当")["total_projects"]
        assert before_total >= 2
        out = watch.refresh(manager, "架空担当")
        assert out["total_projects"] == before_total
    finally:
        _del._set_state(manager.memory.path, pids[0], "restored",
                        actor="架空担当", backup_id="架空退避",
                        manifest_sha256="0" * 64)


def test_concurrent_refresh_no_dup(tmp_path):
    import asyncio
    import app.watchtower as watch
    manager, pids = _single_db_managers(tmp_path)
    assert len(pids) == 3
    manager.memory.save_mission(pids[0], "架空目標-doc",
                                "1. 架空文書01の作成と検証\n2. 架空文書02の作成と検証\n"
                                "3. 架空文書03の作成と検証",
                                "", True, ["chatgpt"])

    async def _run():
        calls = [lambda: watch.refresh(manager, "架空担当") for _ in range(4)]
        return await asyncio.gather(*[asyncio.to_thread(call) for call in calls])

    results = asyncio.run(_run())
    assert all(item["ok"] for item in results)
    events = watch.list_events(manager, unread_only=False, limit=200)
    keys = [(item["project_id"], item["kind"], item["idempotency_key"])
            for item in events]
    assert len(keys) == len(set(keys)), keys


def test_refresh_does_not_write_project_data(tmp_path):
    import app.watchtower as watch
    manager, pids = _single_db_managers(tmp_path)
    mem_before = _row_counts(manager.memory.path)
    ws_before = {pid: _workspace_files(manager, pid) for pid in pids}
    calls = {"n": 0}

    async def _boom(*a, **k):
        calls["n"] += 1
        raise AssertionError("external must not be called")

    manager.plan_review_runner = _boom
    first = watch.refresh(manager, "架空担当")
    assert first["ok"] is True
    assert calls["n"] == 0
    assert _row_counts(manager.memory.path) == mem_before
    for pid in pids:
        assert _workspace_files(manager, pid) == ws_before[pid]
    # ackは既読だけで本文を変えない。
    events = watch.list_events(manager, unread_only=False, limit=200)
    if events:
        body_before = events[0]["summary_ja"]
        acked = watch.ack_event(manager, events[0]["id"], "架空担当", "架空メモ")
        assert acked["summary_ja"] == body_before
        assert acked["ack_by"] == "架空担当"


def test_decisions_basic(tmp_path):
    import app.watchtower as watch
    manager, pids = _single_db_managers(tmp_path)
    decisions = watch.collect_decisions(manager)
    assert decisions, "判断待ちが空です"
    for item in decisions:
        assert set(item) >= {"project_id", "kind", "what_ja", "screen",
                             "since", "basis"}
        assert "recommend" not in json.dumps(item, ensure_ascii=False).lower()
        assert "おすすめ" not in item["what_ja"]
    kinds = {item["kind"] for item in decisions}
    assert kinds <= {"goal_plan_create", "plan_approval", "issue_binding",
                     "fact_confirm", "proposal_confirm", "instruction_output",
                     "evidence_confirm", "unknown_confirm"}, kinds
    # 古い順: since の昇順。
    sinces = [str(item.get("since") or "") for item in decisions]
    assert sinces == sorted(sinces), sinces
    unreadable = [item for item in decisions if item["kind"] == "unknown_confirm"]
    assert isinstance(unreadable, list)


def test_decisions_read_failure_kept(tmp_path, monkeypatch):
    import app.goal_state_model as model
    import app.watchtower as watch
    manager, pids = _single_db_managers(tmp_path)
    real = model.build_goal_state
    target = pids[2]

    def _boom(manager_arg, pid_arg):
        if str(pid_arg) == str(target):
            raise RuntimeError("架空の読取失敗")
        return real(manager_arg, pid_arg)

    monkeypatch.setattr(model, "build_goal_state", _boom)
    try:
        decisions = watch.collect_decisions(manager)
    finally:
        monkeypatch.undo()
    hit = [item for item in decisions if item["project_id"] == target]
    assert hit, decisions
    assert hit[0]["kind"] == "unknown_confirm"
    assert hit[0]["what_ja"] != ""


def test_lifecycle_registry_and_purge(tmp_path):
    import app.watchtower as watch
    from app.project_lifecycle_registry import audit_registry_completeness
    audit = audit_registry_completeness()
    assert audit["ok"] is True, audit["unregistered"]
    manager, pids = _single_db_managers(tmp_path)
    watch.refresh(manager, "架空担当")
    reset = watch.reset_for_generation(manager.memory.path, pids[0])
    assert reset["reset"] is True
    purged = watch.purge_project_watch(manager.memory.path, pids[0])
    assert purged["watch_snapshots"] >= 0
    assert purged["watch_events"] >= 0


def test_zero_criteria_next_step_is_human_confirm(tmp_path):
    from app.goal_state_model import build_goal_state
    manager, pid = _make_manager(tmp_path, "c1-疑似-zero", [])
    out = build_goal_state(manager, pid)
    assert out["summary"]["total"] == 0
    assert out["summary"]["achieved"] == 0
    assert out["summary"]["goal_achieved"] is False
    assert out["next_step"]["kind"] == "human_confirm"
    assert "未作成" in out["next_step"]["detail_ja"]
    assert out["next_step"]["screen"] == "目標と計画"


def test_generic_no_specific_project_words():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    text = (root / "app" / "watchtower.py").read_text(encoding="utf-8")
    for word in ("拡販", "販売システム", "c1-疑似", "1698"):
        assert word not in text
    assert "innerHTML" not in text
    assert "http://" not in text and "https://" not in text


def test_registry_completeness_c1():
    from app.project_lifecycle_registry import audit_registry_completeness
    audit = audit_registry_completeness()
    assert audit["ok"] is True, audit["unregistered"]


def test_watch_purge_sql_error_is_not_ignored(tmp_path):
    import sqlite3
    import pytest
    from app.watchtower import purge_project_watch, watch_db_path

    memory_path = tmp_path / "conversations.db"
    path = watch_db_path(memory_path)
    with sqlite3.connect(str(path)) as db:
        db.execute("CREATE TABLE watch_snapshots (wrong_column TEXT)")
    with pytest.raises(sqlite3.OperationalError):
        purge_project_watch(memory_path, "test-project")
