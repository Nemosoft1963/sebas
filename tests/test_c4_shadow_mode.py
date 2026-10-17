"""C-4 影モードのテスト (疑似PJのみ・実関数)。

疑似PJのみ (架空・疑似)。実在PJのID・件数・版を使わない。外部通信なし。
実案件のPJのデータには書込まない (一時ディレクトリの疑似PJのみ)。
"""
import asyncio
import json
import sqlite3
import threading

import pytest

from app.memory.short_term import ShortTermMemory
from app.project_manager import ProjectOrchestrator
from app.structured_planning import compile_task
from app.workspace_files import WorkspaceSandbox


def _criteria_for(domain, n):
    out = []
    for i in range(1, n + 1):
        if domain == "doc":
            out.append(f"架空文書{i:02d}の作成と検証")
        elif domain == "table":
            out.append(f"架空営業管理表{i:02d}の集計と検算")
        else:
            if i % 3 == 0:
                out.append(f"架空顧客へ送信{i:02d}の実行と検証")
            else:
                out.append(f"架空告知文書{i:02d}の作成と検証")
    return out


def _titles_for(domain, i):
    if domain == "doc":
        return f"架空文書工程{i}", f"架空文書{i:02d}の成果物を作成し根拠を検証する。"
    if domain == "table":
        return f"架空集計工程{i}", f"架空営業管理表{i:02d}の表を作成し検算する。"
    if i % 3 == 0:
        return f"架空送信工程{i}", f"架空顧客へ送信{i:02d}を実行する。実際の外部送信は行わない。"
    return f"架空告知工程{i}", f"架空告知文書{i:02d}の成果物を作成し検算する。"


def _make_manager(tmp_path, domain, n, tag=""):
    name = f"c4-疑似-{domain}-{n}{tag}"
    db_path = tmp_path / (name + ".db")
    memory = ShortTermMemory(db_path)
    project = memory.create_project(name, workspace_path="projects/" + name)
    pid = project["id"]
    criteria = _criteria_for(domain, n)
    memory.save_mission(pid, "架空目標-" + domain, "\n".join(
        f"{i}. {text}" for i, text in enumerate(criteria, 1)), "", True, ["chatgpt"])
    tasks = []
    for i, text in enumerate(criteria, 1):
        title, scope = _titles_for(domain, i)
        tasks.append(compile_task(i, text, {
            "title": title, "scope": scope, "headings": ["目的", "実施内容"],
        }, []))
    memory.replace_plan(pid, name + " plan", tasks)
    manager = ProjectOrchestrator(
        memory, None, lambda p: ("", []), None,
        lambda: [{"id": "chatgpt", "configured": True}],
        workspace=WorkspaceSandbox(tmp_path / ("ws-" + name)),
    )
    return manager, pid


def _sig_of(manager, pid):
    from app.goal_review import plan_snapshot
    return plan_snapshot(manager, pid)[1]


def _store_plan(manager, pid, status, reviews=None):
    from app.goal_review import ReviewStore
    sig = _sig_of(manager, pid)
    store = ReviewStore(manager.memory.path)
    body = {"status": status, "reviews": list(reviews or []),
            "finished": 0, "packet": None}
    store.put(pid, "plan", sig, body)
    return sig


def _fail_issue():
    return {"provider": "chatgpt", "status": "fail",
            "issues": [{"text": "架空指摘文の対応が必要",
                        "criterion": "SC01", "step": "1"}]}


def _seed_missing_coverage(manager, pid):
    _store_plan(manager, pid, "not_passed", [_fail_issue()])


def _main_fingerprint(manager, pid):
    db = sqlite3.connect(f"file:{manager.memory.path.as_posix()}?mode=ro",
                         uri=True)
    try:
        db.row_factory = sqlite3.Row
        tables = [r[0] for r in db.execute(
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


def _autopilot_calls():
    return {"tick": 0, "run": 0}


def _watch_autopilot(monkeypatch, counter):
    import app.autopilot as auto
    orig_tick = auto.autopilot_tick
    orig_run = auto.run_named_action

    def _tick(*args, **kwargs):
        counter["tick"] += 1
        return orig_tick(*args, **kwargs)

    def _run(*args, **kwargs):
        counter["run"] += 1
        return orig_run(*args, **kwargs)

    monkeypatch.setattr(auto, "autopilot_tick", _tick)
    monkeypatch.setattr(auto, "run_named_action", _run)
    return orig_tick, orig_run


def _event_kinds(manager, pid):
    db = sqlite3.connect(f"file:{manager.memory.path.as_posix()}?mode=ro", uri=True)
    try:
        try:
            rows = db.execute(
                "SELECT kind FROM project_events WHERE project_id=?",
                (pid,)).fetchall()
        except sqlite3.Error:
            return []
        return sorted({str(r[0]) for r in rows})
    finally:
        db.close()


@pytest.mark.parametrize("domain", ["doc", "table", "external"])
def test_observe_writes_nothing_but_shadow(tmp_path, domain, monkeypatch):
    import app.autopilot as auto
    import app.shadow_mode as shadow
    manager, pid = _make_manager(tmp_path, domain, 3, tag="-zero")
    _seed_missing_coverage(manager, pid)
    calls = _autopilot_calls()
    _watch_autopilot(monkeypatch, calls)
    before_main = _main_fingerprint(manager, pid)
    auto_before = list(auto.list_ticks(manager, pid))
    out = shadow.observe(manager, pid, "架空担当")
    assert out["ok"] is True, out
    assert calls == {"tick": 0, "run": 0}
    assert _main_fingerprint(manager, pid) == before_main
    assert list(auto.list_ticks(manager, pid)) == auto_before
    # PJのDB・ファイル(影の表以外)に書込がないこと。読み取り専用の
    # 遅延作成DB (goal_completion 等) が空で作られることは許すが、
    # 中身(行数)は増えないこと。
    after_main = _main_fingerprint(manager, pid)
    assert set(after_main) >= set(before_main)
    for table, count in before_main.items():
        assert after_main.get(table) == count, table
    # 影DBだけが新規に作られるか、既存の影DBに追記されること。
    shadow_path = shadow.shadow_db_path(manager)
    assert shadow_path.exists()
    assert out["record"]["planned_action"]
    assert out["record"]["stop_class"]
    assert out["record"]["stop_code"]
    assert out["record"]["plan_signature"]


@pytest.mark.parametrize("domain", ["doc", "table", "external"])
def test_observe_deleted_or_unknown_excluded(tmp_path, domain):
    import app.shadow_mode as shadow
    import app.project_delete as _del
    manager, pid = _make_manager(tmp_path, domain, 2, tag="-del")
    _del._set_state(manager.memory.path, pid, "deleted", actor="架空担当",
                    backup_id="架空退避", manifest_sha256="0" * 64)
    try:
        out = shadow.observe(manager, pid, "架空担当")
        assert out["ok"] is False
        assert shadow.list_records(manager, pid) == []
    finally:
        _del._set_state(manager.memory.path, pid, "restored",
                        actor="架空担当", backup_id="架空退避",
                        manifest_sha256="0" * 64)
    # 状態不明 (delete DB破損) も対象外。
    path = _del.delete_db_path(manager.memory.path)
    path.write_bytes(b"not a sqlite database")
    out2 = shadow.observe(manager, pid, "架空担当")
    assert out2["ok"] is False
    assert shadow.list_records(manager, pid) == []


def test_observe_idempotent_and_new_on_change(tmp_path, monkeypatch):
    import app.autopilot as auto
    import app.shadow_mode as shadow
    manager, pid = _make_manager(tmp_path, "doc", 3, tag="-idem")
    _seed_missing_coverage(manager, pid)
    first = shadow.observe(manager, pid, "架空担当")
    assert first["ok"] is True and first["idempotent"] is False
    second = shadow.observe(manager, pid, "架空担当")
    assert second["ok"] is True and second["idempotent"] is True
    assert second["record_id"] == first["record_id"]
    assert len(shadow.list_records(manager, pid)) == 1
    # 状態が変われば増える (署名を変える)。ラベル付きの既存は残る。
    labeled = shadow.label(manager, first["record_id"], "correct",
                           "架空判定者", "架空メモ")
    assert labeled["ok"] is True
    mission = manager.memory.get_mission(pid)
    manager.memory.replace_plan(pid, (mission.get("plan_summary") or "架空") + " 改訂",
                                mission["tasks"])
    third = shadow.observe(manager, pid, "架空担当")
    assert third["ok"] is True
    rows = shadow.list_records(manager, pid)
    assert len(rows) == 2
    assert {row["id"] for row in rows} == {first["record_id"],
                                           third["record_id"]}


def test_observe_concurrent_no_double(tmp_path):
    import app.shadow_mode as shadow
    manager, pid = _make_manager(tmp_path, "table", 3, tag="-conc")

    async def _gather():
        return await asyncio.gather(
            asyncio.to_thread(shadow.observe, manager, pid, "架空担当"),
            asyncio.to_thread(shadow.observe, manager, pid, "架空担当"))

    results = asyncio.run(_gather())
    assert all(item["ok"] is True for item in results), results
    rows = shadow.list_records(manager, pid)
    assert len(rows) == 1
    assert results[0]["record_id"] == results[1]["record_id"]


def test_observe_preview_failure_makes_no_record(tmp_path, monkeypatch):
    import app.autopilot as auto
    import app.shadow_mode as shadow
    manager, pid = _make_manager(tmp_path, "doc", 2, tag="-fail")

    def _boom(manager_inner, project_id, record, plan_signature):
        raise RuntimeError("架空の予告失敗")

    monkeypatch.setattr(auto, "preview_planned_action", _boom)
    out = shadow.observe(manager, pid, "架空担当")
    assert out["ok"] is False
    assert out["error_type"] == "RuntimeError"
    assert shadow.list_records(manager, pid) == []
    # 注入フックでも同様。
    monkeypatch.undo()
    shadow.FAIL_INJECT = {"stage": "observe_preview_fail"}
    try:
        out2 = shadow.observe(manager, pid, "架空担当")
        assert out2["ok"] is False
        assert shadow.list_records(manager, pid) == []
    finally:
        shadow.FAIL_INJECT = None


def test_observe_all_continues_on_failure(tmp_path, monkeypatch):
    import app.shadow_mode as shadow
    from app.memory.short_term import ShortTermMemory
    from app.project_manager import ProjectOrchestrator
    db_path = tmp_path / "c4-疑似-multi.db"
    memory = ShortTermMemory(db_path)
    pids = []
    for key in ("a", "b"):
        project = memory.create_project("c4-疑似-multi-" + key,
                                        workspace_path="projects/c4-" + key)
        pid = project["id"]
        memory.save_mission(pid, "架空目標-" + key, "1. 架空文書01の作成と検証",
                            "", True, ["chatgpt"])
        memory.replace_plan(pid, "架空plan", [__import__(
            "app.structured_planning", fromlist=["compile_task"]).compile_task(
            1, "架空文書01の作成と検証",
            {"title": "架空文書工程1",
             "scope": "架空文書01の成果物を作成する。",
             "headings": ["目的", "実施内容"]}, [])])
        pids.append(pid)
    manager = ProjectOrchestrator(
        memory, None, lambda p: ("", []), None,
        lambda: [{"id": "chatgpt", "configured": True}],
        workspace=WorkspaceSandbox(tmp_path / "ws-c4-multi"),
    )
    real_observe = shadow.observe

    def _flaky(manager_inner, project_id, actor):
        if str(project_id) == pids[0]:
            return {"ok": False, "reason": "架空の失敗", "error_type": "ValueError"}
        return real_observe(manager_inner, project_id, actor)

    monkeypatch.setattr(shadow, "observe", _flaky)
    out = shadow.observe_all(manager, "架空担当")
    assert out["ok"] is True
    assert out["total_projects"] >= 2
    by_id = {item["project_id"]: item for item in out["results"]}
    assert by_id[pids[0]]["ok"] is False
    assert "確認できず" in by_id[pids[0]]["reason"]
    assert by_id[pids[1]]["ok"] is True
    assert len(shadow.list_records(manager, pids[1])) == 1
    assert shadow.list_records(manager, pids[0]) == []


def test_label_correct_wrong_unsure_and_danger_only_wrong(tmp_path):
    import app.shadow_mode as shadow
    manager, pid = _make_manager(tmp_path, "doc", 2, tag="-label")
    observed = shadow.observe(manager, pid, "架空担当")
    assert observed["ok"] is True
    rid = observed["record_id"]
    ok_correct = shadow.label(manager, rid, "correct", "架空判定者", "架空メモ")
    assert ok_correct["ok"] is True
    assert ok_correct["record"]["label"] == "correct"
    assert ok_correct["record"]["danger"] == 0
    # danger は誤りのときだけ。
    bad_danger = shadow.label(manager, rid, "correct", "架空判定者", "", True)
    assert bad_danger["ok"] is False
    ok_wrong = shadow.label(manager, rid, "wrong", "架空判定者", "架空メモ", True)
    assert ok_wrong["ok"] is True
    assert ok_wrong["record"]["danger"] == 1
    ok_unsure = shadow.label(manager, rid, "unsure", "架空判定者", "")
    assert ok_unsure["ok"] is True
    assert ok_unsure["record"]["label"] == "unsure"
    assert ok_unsure["record"]["danger"] == 0
    history = shadow.label_history(manager, rid)
    assert [item["label"] for item in history] == ["correct", "wrong", "unsure"]
    assert history[-1]["danger"] == 0
    # actor なし・不正ラベル・存在しないIDは失敗。
    assert shadow.label(manager, rid, "correct", "", "")["ok"] is False
    assert shadow.label(manager, rid, "nope", "架空判定者", "")["ok"] is False
    assert shadow.label(manager, "sh_存在しない", "correct",
                        "架空判定者", "")["ok"] is False


def _insert_direct(manager, pid, sig, planned="record_stop_class",
                   stop_class="missing_decision", stop_code="x:y",
                   label="", danger=0, at=None):
    import time as _time
    import uuid as _uuid
    import app.shadow_mode as shadow
    db_path = shadow.shadow_db_path(manager)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(db_path), timeout=30)
    try:
        with db:
            shadow._ensure_tables(db)
            rid = "sh_" + _uuid.uuid4().hex[:16]
            now = float(at) if at is not None else _time.time()
            idem = shadow._short_hash([pid, sig, planned, stop_class,
                                       stop_code, rid])
            db.execute(
                """INSERT INTO shadow_records(id, project_id, plan_signature,
                       observed_at, actor, planned_action, stop_class,
                       stop_code, gate_status, preflight_status,
                       next_step_kind, reason_ja, label, label_by, label_at,
                       label_note, danger, idempotency_key)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (rid, pid, sig, now, "架空担当", planned, stop_class,
                 stop_code, "ok", "ok", "", "架空理由", label,
                 "架空判定者" if label else "", now if label else 0.0,
                 "", int(danger), idem))
            if label:
                db.execute(
                    """INSERT INTO shadow_label_history(id, record_id,
                           project_id, label, actor, note, danger, created_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    ("shl_" + _uuid.uuid4().hex[:16], rid, pid, label,
                     "架空判定者", "", int(danger), now))
            return rid
    finally:
        db.close()


def test_metrics_agreement_and_null_denominator(tmp_path):
    import app.shadow_mode as shadow
    manager, pid = _make_manager(tmp_path, "doc", 2, tag="-metrics")
    empty = shadow.metrics(manager, pid)
    assert empty["count"] == 0
    assert empty["agreement"] is None
    gate_empty = shadow.gate(manager, pid)
    assert gate_empty["passed"] is False
    _insert_direct(manager, pid, "sig-a", label="correct")
    _insert_direct(manager, pid, "sig-a", label="correct")
    _insert_direct(manager, pid, "sig-b", label="wrong", danger=1)
    _insert_direct(manager, pid, "sig-b", label="unsure")
    _insert_direct(manager, pid, "sig-c")
    data = shadow.metrics(manager, pid)
    assert data["count"] == 5
    assert data["correct"] == 2 and data["wrong"] == 1
    assert data["unsure"] == 1 and data["unjudged"] == 1
    assert data["judged_for_gate"] == 3
    assert abs(float(data["agreement"]) - 2.0 / 3.0) < 1e-9
    assert data["dangerous"] == 1
    # 判定済み(正しい/誤り)の署名だけ数える: sig-a, sig-b。sig-c は未判定。
    assert data["signatures"] == 2


def test_gate_each_threshold_fails_alone(tmp_path):
    import time as _time
    import app.shadow_mode as shadow
    manager, pid = _make_manager(tmp_path, "doc", 2, tag="-gate")
    # 満たす土台: 20件 (19正しい+1誤り=95%)・署名3つ・危険0・直近。
    sigs = ["sig-1", "sig-2", "sig-3"]
    for i in range(19):
        _insert_direct(manager, pid, sigs[i % 3], label="correct")
    _insert_direct(manager, pid, "sig-3", label="wrong")
    good = shadow.gate(manager, pid)
    assert good["passed"] is True, good
    # (1) 判定済み19件だけ未達。
    manager2, pid2 = _make_manager(tmp_path, "doc", 2, tag="-gate1")
    for i in range(18):
        _insert_direct(manager2, pid2, sigs[i % 3], label="correct")
    _insert_direct(manager2, pid2, "sig-3", label="wrong")
    bad1 = shadow.gate(manager2, pid2)
    assert bad1["passed"] is False
    assert any("判定済み" in reason for reason in bad1["reasons"])
    # (2) 署名2つだけ未達。
    manager3, pid3 = _make_manager(tmp_path, "doc", 2, tag="-gate2")
    for _ in range(19):
        _insert_direct(manager3, pid3, "sig-1", label="correct")
    _insert_direct(manager3, pid3, "sig-2", label="wrong")
    bad2 = shadow.gate(manager3, pid3)
    assert bad2["passed"] is False
    assert any("計画署名" in reason for reason in bad2["reasons"])
    # (3) 一致率だけ未達 (18正しい+2誤り=90%)。
    manager4, pid4 = _make_manager(tmp_path, "doc", 2, tag="-gate3")
    for i in range(18):
        _insert_direct(manager4, pid4, sigs[i % 3], label="correct")
    _insert_direct(manager4, pid4, "sig-2", label="wrong")
    _insert_direct(manager4, pid4, "sig-3", label="wrong")
    bad3 = shadow.gate(manager4, pid4)
    assert bad3["passed"] is False
    assert any("一致率" in reason for reason in bad3["reasons"])
    # (4) 危険1件だけ未達。
    manager5, pid5 = _make_manager(tmp_path, "doc", 2, tag="-gate4")
    for i in range(19):
        _insert_direct(manager5, pid5, sigs[i % 3], label="correct")
    _insert_direct(manager5, pid5, "sig-3", label="wrong", danger=1)
    bad4 = shadow.gate(manager5, pid5)
    assert bad4["passed"] is False
    assert any("危険" in reason for reason in bad4["reasons"])
    # (5) 直近31日前だけ未達 (最新も古い)。
    manager6, pid6 = _make_manager(tmp_path, "doc", 2, tag="-gate5")
    old = _time.time() - 31 * 86400.0
    for i in range(19):
        _insert_direct(manager6, pid6, sigs[i % 3], label="correct", at=old)
    _insert_direct(manager6, pid6, "sig-3", label="wrong", at=old)
    bad5 = shadow.gate(manager6, pid6)
    assert bad5["passed"] is False
    assert any("直近" in reason for reason in bad5["reasons"])


def test_gate_unreadable_fails_closed(tmp_path, monkeypatch):
    import app.shadow_mode as shadow
    manager, pid = _make_manager(tmp_path, "doc", 2, tag="-gatefail")
    _insert_direct(manager, pid, "sig-1", label="correct")
    shadow.FAIL_INJECT = {"stage": "metrics_unreadable"}
    try:
        bad = shadow.gate(manager, pid)
        assert bad["passed"] is False
        assert any("確認できず" in reason for reason in bad["reasons"])
    finally:
        shadow.FAIL_INJECT = None
    shadow.FAIL_INJECT = {"stage": "gate_unreadable"}
    try:
        bad2 = shadow.gate(manager, pid)
        assert bad2["passed"] is False
    finally:
        shadow.FAIL_INJECT = None


def test_registry_and_purge_and_generic(tmp_path):
    import app.shadow_mode as shadow
    from app.project_lifecycle_registry import audit_registry_completeness
    audit = audit_registry_completeness()
    assert audit["ok"] is True, audit["unregistered"]
    manager, pid = _make_manager(tmp_path, "doc", 2, tag="-reg")
    other, other_pid = _make_manager(tmp_path, "table", 2, tag="-other")
    rid = _insert_direct(manager, pid, "sig-1", label="wrong", danger=1)
    _insert_direct(other, other_pid, "sig-9", label="correct")
    assert shadow.get_record(manager, rid)["project_id"] == pid
    purged = shadow.purge_project_shadow(manager.memory.path, pid)
    assert purged["shadow_records"] >= 1
    assert shadow.list_records(manager, pid) == []
    assert len(shadow.list_records(other, other_pid)) == 1
    # 初期化では変えない。
    rid2 = _insert_direct(manager, pid, "sig-2", label="correct")
    reset = shadow.reset_for_generation(manager.memory.path, pid)
    assert reset["reset"] is False
    assert shadow.get_record(manager, rid2)["project_id"] == pid
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    text = (root / "app" / "shadow_mode.py").read_text(encoding="utf-8")
    for word in ("拡販", "販売システム", "c4-疑似", "2078"):
        assert word not in text
    assert "innerHTML" not in text
    assert "http://" not in text and "https://" not in text
    assert "httpx" not in text and "requests.post" not in text
    assert "urlopen" not in text
    # 実行関数を呼ばないこと (静的)。文言での言及は除く。
    code_lines = [line for line in text.splitlines()
                  if "呼ばない" not in line and "呼ぶのは" not in line
                  and "呼ばない" not in line]
    code_only = "\n".join(code_lines)
    assert "auto.autopilot_tick" not in code_only
    assert "auto.run_named_action" not in code_only
    assert "autopilot_tick(" not in code_only
    assert "run_named_action(" not in code_only
    assert "preview_planned_action" in text
    # A-3 の禁止形を入れない。
    assert "except Exception: pass" not in text
    assert ".catch(()=>({}))" not in text


def test_no_background_threads_statically():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    text = (root / "app" / "shadow_mode.py").read_text(encoding="utf-8")
    assert "threading.Thread" not in text
    assert "asyncio.create_task" not in text
    for word in ("setInterval", "setTimeout", "cron", "APScheduler"):
        assert word not in text
    # 二重送信防止ではなく排他ロックのみ。
    assert "_lock_for" in text


def test_gate_signatures_count_only_judged_records(tmp_path):
    """未判定・判断できないだけの署名は、関門の「異なる計画署名」に数えない。"""
    import app.shadow_mode as shadow
    manager, pid = _make_manager(tmp_path, "doc", 2, tag="-gate-sig")
    for _ in range(19):
        _insert_direct(manager, pid, "sig-1", label="correct")
    _insert_direct(manager, pid, "sig-1", label="wrong")
    # 署名2・3は判定されていない記録だけ (未判定/判断できない)。
    _insert_direct(manager, pid, "sig-2", label="")
    _insert_direct(manager, pid, "sig-3", label="unsure")
    result = shadow.gate(manager, pid)
    assert result["metrics"]["signatures"] == 1
    assert result["passed"] is False
    assert any("計画署名" in reason for reason in result["reasons"])
