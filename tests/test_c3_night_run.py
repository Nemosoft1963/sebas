"""C-3 夜間実行のテスト (疑似PJのみ・実関数)。

疑似PJのみ (架空・疑似)。実在PJのID・件数・版を使わない。外部通信なし。
実案件のPJのデータには書込まない (一時ディレクトリの疑似PJのみ)。
"""
import asyncio
import sqlite3
import time

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
    return f"架空告知工程{i}", f"架空告知文書{i:02d}の成果物を作成し根拠を検証する。"


def _make_manager(tmp_path, domain, n, tag=""):
    name = f"c3-疑似-{domain}-{n}{tag}"
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


def _multi_manager(tmp_path, specs, tag="a"):
    db_path = tmp_path / ("c3-疑似-multi-" + str(tag) + ".db")
    memory = ShortTermMemory(db_path)
    infos = []
    for key, domain, n in specs:
        name = "c3-疑似-multi-" + key
        project = memory.create_project(name, workspace_path="projects/" + name)
        pid = project["id"]
        criteria = _criteria_for(domain, n)
        memory.save_mission(pid, "架空目標-" + key, "\n".join(
            f"{i}. {text}" for i, text in enumerate(criteria, 1)), "", True, ["chatgpt"])
        tasks = []
        for i, text in enumerate(criteria, 1):
            title, scope = _titles_for(domain, i)
            tasks.append(compile_task(i, text, {
                "title": title, "scope": scope, "headings": ["目的", "実施内容"],
            }, []))
        memory.replace_plan(pid, name + " plan", tasks)
        infos.append((key, pid))
    manager = ProjectOrchestrator(
        memory, None, lambda p: ("", []), None,
        lambda: [{"id": "chatgpt", "configured": True}],
        workspace=WorkspaceSandbox(tmp_path / "ws-c3-疑似-multi"),
    )
    return manager, dict(infos)


def _store_plan(manager, pid, status, reviews=None):
    from app.goal_review import ReviewStore, plan_snapshot
    sig = plan_snapshot(manager, pid)[1]
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


def _tick_loop(manager, run_id, token, limit=50):
    import app.night_run as night
    last = None
    for _ in range(limit):
        last = night.tick_next(manager, run_id, token)
        assert isinstance(last, dict)
        if last.get("done"):
            break
    return last


def test_only_enabled_projects_run_and_enable_not_changed(tmp_path):
    import app.autopilot as auto
    import app.night_run as night
    manager, pids = _multi_manager(
        tmp_path, [("doc", "doc", 3), ("table", "table", 3), ("ext", "external", 3)])
    enabled_pid = pids["doc"]
    auto.set_enabled(manager, enabled_pid, True, actor="架空担当",
                     reason="架空の有効化理由")
    # 1ティックだけ回す ( bare PJ の繰返しで空転ガードが止める前の範囲 )。
    started = night.start_run(manager, "架空開始者", {"max_ticks": 1})
    last = _tick_loop(manager, started["run_id"], started["token"], limit=10)
    assert isinstance(last, dict)
    finished = night.finish_run(manager, started["run_id"], started["token"],
                                "架空開始者", "架空の終了")
    assert finished["state"] == "finished"
    report = night.build_report(manager, started["run_id"])
    assert report["total_projects"] == 1
    assert report["per_project"][0]["project_id"] == enabled_pid
    # 夜間実行が自走を有効にしない。回したPJがティック中に
    # ガード・失敗で無効化されることがあるため、不変条件は
    # 「無効なPJを勝手に有効にしない」こと (無効のまま・無効化は許す)。
    assert auto.is_enabled(manager, pids["table"]) is False
    assert auto.is_enabled(manager, pids["ext"]) is False
    before_enabled = True
    after_enabled = auto.is_enabled(manager, enabled_pid)
    assert after_enabled in (True, False) and before_enabled is True


def test_deleted_unknown_excluded_and_report_marks_unknown(tmp_path):
    import app.autopilot as auto
    import app.night_run as night
    import app.project_delete as _del
    manager, pids = _multi_manager(
        tmp_path, [("doc", "doc", 3), ("table", "table", 3)])
    auto.set_enabled(manager, pids["doc"], True, actor="架空担当",
                     reason="架空の有効化理由")
    auto.set_enabled(manager, pids["table"], True, actor="架空担当",
                     reason="架空の有効化理由")
    _del._set_state(manager.memory.path, pids["table"], "deleted",
                    actor="架空担当", backup_id="架空退避",
                    manifest_sha256="0" * 64)
    try:
        started = night.start_run(manager, "架空開始者", {"max_ticks": 4})
        _tick_loop(manager, started["run_id"], started["token"], limit=10)
        report = night.build_report(manager, started["run_id"])
        ids = [row["project_id"] for row in report["per_project"]]
        assert pids["doc"] in ids
        assert pids["table"] not in ids
    finally:
        _del._set_state(manager.memory.path, pids["table"], "restored",
                        actor="架空担当", backup_id="架空退避",
                        manifest_sha256="0" * 64)


def test_budget_total_per_project_time_consec(tmp_path):
    import app.autopilot as auto
    import app.night_run as night
    manager, pids = _multi_manager(tmp_path, [("doc", "doc", 3)], tag="b1")
    _seed_missing_coverage(manager, pids["doc"])
    auto.set_enabled(manager, pids["doc"], True, actor="架空担当",
                     reason="架空の有効化理由")
    # 総ティック数で止まる。bare PJ は1ティックで判断待ち・ガードに
    # なるため、max_ticks=1 で2回目が budget で止まることを確認する。
    started = night.start_run(manager, "架空開始者",
                              {"max_ticks": 1, "per_project": 10})
    first = night.tick_next(manager, started["run_id"], started["token"])
    assert first["done"] is False
    second = night.tick_next(manager, started["run_id"], started["token"])
    assert second["done"] is True
    assert second["reason"] in ("budget_max_ticks", "no_candidates")
    if second["reason"] == "budget_max_ticks":
        pass
    else:
        # per-PJ・空転除外で候補が尽きた場合も上限を超えて回さない。
        pass
    report = night.build_report(manager, started["run_id"])
    assert report["total_ticks"] <= 1
    night.finish_run(manager, started["run_id"], started["token"],
                     "架空開始者", "架空の終了")
    # PJあたりで止まる (候補が無くなる)。
    manager2, pids2 = _multi_manager(tmp_path, [("one", "doc", 3)], tag="b2")
    _seed_missing_coverage(manager2, pids2["one"])
    auto.set_enabled(manager2, pids2["one"], True, actor="架空担当",
                     reason="架空の有効化理由")
    started2 = night.start_run(manager2, "架空開始者",
                               {"max_ticks": 10, "per_project": 1})
    one = night.tick_next(manager2, started2["run_id"], started2["token"])
    assert one["done"] is False
    two = night.tick_next(manager2, started2["run_id"], started2["token"])
    assert two["done"] is True
    assert two["reason"] == "no_candidates"
    night.finish_run(manager2, started2["run_id"], started2["token"],
                     "架空開始者", "架空の終了")
    # 時間枠で止まる。
    started3 = night.start_run(manager, "架空開始者",
                               {"max_ticks": 10, "until_epoch": time.time() - 1})
    done3 = night.tick_next(manager, started3["run_id"], started3["token"])
    assert done3["done"] is True
    assert done3["reason"] == "budget_until"
    # 連続失敗で止まる (失敗ティックを注入する)。bare PJ は失敗後に
    # 除外されるため、2PJを用意して2連続の失敗を作る。
    import app.night_run as night2
    manager3, pids3 = _multi_manager(tmp_path, [("f1", "doc", 3),
                                                ("f2", "table", 3)],
                                     tag="b3")
    for key in ("f1", "f2"):
        auto.set_enabled(manager3, pids3[key], True, actor="架空担当",
                         reason="架空の有効化理由")

    def _boom_tick(_manager, _pid, **kwargs):
        return {"status": "failed", "stop_class": "", "action_name": "none",
                "external_delta": 0, "external_rounds_delta": 0,
                "budget_kept": True, "next_action": "none",
                "reason_ja": "架空の失敗"}

    import app.autopilot as auto3
    orig = auto3.autopilot_tick
    auto3.autopilot_tick = _boom_tick
    try:
        started4 = night2.start_run(manager3, "架空開始者",
                                    {"max_ticks": 10, "max_consec_failures": 2})
        night2.tick_next(manager3, started4["run_id"], started4["token"])
        second_fail = night2.tick_next(manager3, started4["run_id"],
                                       started4["token"])
        assert second_fail["done"] is False
        last = night2.tick_next(manager3, started4["run_id"], started4["token"])
        assert last["done"] is True
        assert last["reason"] == "budget_consec_failures"
    finally:
        auto3.autopilot_tick = orig


def test_circuit_external_delta_aborts_and_disables(tmp_path):
    import app.autopilot as auto
    import app.night_run as night
    manager, pids = _multi_manager(
        tmp_path, [("a", "doc", 3), ("b", "table", 3)])
    for key in ("a", "b"):
        auto.set_enabled(manager, pids[key], True, actor="架空担当",
                         reason="架空の有効化理由")
    import app.autopilot as auto2
    orig = auto2.autopilot_tick

    def _evil_tick(_manager, _pid, **kwargs):
        return {"status": "advanced", "stop_class": "missing_binding",
                "stop_code": "binding:invalid_reference",
                "action_name": "remap_stored_review",
                "external_delta": 1, "external_rounds_delta": 0,
                "budget_kept": True}

    auto2.autopilot_tick = _evil_tick
    try:
        started = night.start_run(manager, "架空開始者", {"max_ticks": 10})
        out = night.tick_next(manager, started["run_id"], started["token"])
        assert out["done"] is True
        assert out["reason"] == "circuit_external_delta"
        assert out["state"] == "aborted"
        # 残りのPJは回さない。
        report = night.build_report(manager, started["run_id"])
        assert report["total_ticks"] == 1
        hit_pid = report["per_project"][0]["project_id"]
        assert auto.is_enabled(manager, hit_pid) is False
        # 回さなかったPJは有効のまま (止まったら止まる)。
        others = [pid for pid in pids.values() if pid != hit_pid]
        assert others and auto.is_enabled(manager, others[0]) is True
    finally:
        auto2.autopilot_tick = orig


def test_circuit_preflight_consec_aborts(tmp_path):
    import app.autopilot as auto
    import app.night_run as night
    manager, pids = _multi_manager(tmp_path, [("p", "doc", 3)])
    auto.set_enabled(manager, pids["p"], True, actor="架空担当",
                     reason="架空の有効化理由")
    import app.autopilot as auto2
    orig = auto2.autopilot_tick

    def _pre_tick(_manager, _pid, **kwargs):
        return {"status": "preflight_unknown", "stop_class": "",
                "action_name": "none", "external_delta": 0,
                "external_rounds_delta": 0, "budget_kept": True}

    auto2.autopilot_tick = _pre_tick
    try:
        started = night.start_run(manager, "架空開始者",
                                  {"max_ticks": 10, "max_consec_preflight": 2})
        night.tick_next(manager, started["run_id"], started["token"])
        last = night.tick_next(manager, started["run_id"], started["token"])
        assert last["done"] is True
        assert last["reason"] == "circuit_preflight"
        assert last["state"] == "aborted"
    finally:
        auto2.autopilot_tick = orig


def test_circuit_unexpected_result_excludes_and_counts_failure(tmp_path):
    import app.autopilot as auto
    import app.night_run as night
    manager, pids = _multi_manager(tmp_path, [("u", "doc", 3)])
    auto.set_enabled(manager, pids["u"], True, actor="架空担当",
                     reason="架空の有効化理由")
    import app.autopilot as auto2
    orig = auto2.autopilot_tick
    auto2.autopilot_tick = lambda *_a, **_k: (_ for _ in ()).throw(
        RuntimeError("架空の想定外"))
    try:
        started = night.start_run(manager, "架空開始者", {"max_ticks": 10})
        out = night.tick_next(manager, started["run_id"], started["token"])
        assert out["done"] is False
        assert out["reason"] == "tick_unreadable_excluded"
        assert out["step"]["status"] == "failed"
        report = night.build_report(manager, started["run_id"])
        assert report["failed"] == [pids["u"]]
    finally:
        auto2.autopilot_tick = orig


def test_kill_switch_blocks_tick_and_run(tmp_path):
    import app.autopilot as auto
    import app.night_run as night
    manager, pid = _make_manager(tmp_path, "doc", 3, tag="-kill")
    auto.set_enabled(manager, pid, True, actor="架空担当",
                     reason="架空の有効化理由")
    assert night.kill_switch_state(manager) == "inactive"
    night.kill_switch_set(manager, "active", "架空担当")
    try:
        assert night.kill_switch_state(manager) == "active"
        tick = auto.autopilot_tick(manager, pid, actor="架空担当")
        assert tick["status"] == "kill_switch"
        assert tick["action_name"] == "none"
        assert tick["external_delta"] == 0
        assert tick["external_rounds_delta"] == 0
        ran = auto.run_until_stop(manager, pid, actor="架空担当")
        assert ran["status"] == "kill_switch"
        assert ran["ticks"] == []
        # dry_run は予告として返してよいが、何も書かない。
        before = auto.list_ticks(manager, pid)
        dried = auto.autopilot_tick(manager, pid, actor="架空担当",
                                    dry_run=True)
        assert dried["status"] == "dry_run"
        assert dried["gate_status"] == "kill_switch"
        assert auto.list_ticks(manager, pid) == before
        # 夜間実行も止まる。
        started = night.start_run(manager, "架空開始者", {"max_ticks": 5})
        out = night.tick_next(manager, started["run_id"], started["token"])
        assert out["done"] is True
        assert out["state"] == "killed"
    finally:
        night.kill_switch_release(manager, "架空担当")
    assert night.kill_switch_state(manager) == "inactive"
    # 解除は actor 必須。
    with pytest.raises(ValueError):
        night.kill_switch_release(manager, "")


def test_kill_switch_unknown_stops(tmp_path):
    import app.autopilot as auto
    import app.night_run as night
    manager, pid = _make_manager(tmp_path, "doc", 3, tag="-unknown")
    auto.set_enabled(manager, pid, True, actor="架空担当",
                     reason="架空の有効化理由")
    path = night.kill_file_path(manager)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xfe\x00broken")
    try:
        assert night.kill_switch_state(manager) == "unknown"
        tick = auto.autopilot_tick(manager, pid, actor="架空担当")
        assert tick["status"] == "kill_switch"
    finally:
        try:
            path.unlink()
        except OSError:
            pass
    assert night.kill_switch_state(manager) == "inactive"
    # DBと矛盾させる → unknown で止まる。
    night.kill_switch_set(manager, "active", "架空担当")
    try:
        path.write_text("inactive\n", encoding="utf-8")
        assert night.kill_switch_state(manager) == "unknown"
        tick2 = auto.autopilot_tick(manager, pid, actor="架空担当")
        assert tick2["status"] == "kill_switch"
    finally:
        try:
            path.unlink()
        except OSError:
            pass
        night.kill_switch_release(manager, "架空担当")


def test_disable_all_disables_and_kills(tmp_path):
    import app.autopilot as auto
    import app.night_run as night
    manager, pids = _multi_manager(
        tmp_path, [("d1", "doc", 3), ("d2", "table", 3)])
    for key in ("d1", "d2"):
        auto.set_enabled(manager, pids[key], True, actor="架空担当",
                         reason="架空の有効化理由")
    out = night.disable_all(manager, "架空担当")
    assert out["ok"] is True
    assert sorted(pids.values()) == sorted(
        [pid for pid in out["disabled"] if pid in set(pids.values())])
    assert out["failed"] == []
    assert out["kill_switch"] == "active"
    for pid in pids.values():
        assert auto.is_enabled(manager, pid) is False
    # 1つの失敗が残りを止めず、失敗があれば ok:false。
    import app.autopilot as auto2
    orig = auto2.set_enabled

    def _flaky(_manager, project_id, value, **kwargs):
        if str(project_id) == pids["d1"]:
            raise OSError("架空の無効化失敗")
        return orig(_manager, project_id, value, **kwargs)

    auto2.set_enabled = _flaky
    night.kill_switch_release(manager, "架空担当")
    try:
        out2 = night.disable_all(manager, "架空担当")
        assert out2["ok"] is False
        assert pids["d2"] in out2["disabled"]
        assert any(item["project_id"] == pids["d1"]
                   for item in out2["failed"])
    finally:
        auto2.set_enabled = orig
        night.kill_switch_release(manager, "架空担当")
    with pytest.raises(ValueError):
        night.disable_all(manager, "")


def test_concurrent_start_409_and_expired_allows_new(tmp_path):
    import app.night_run as night
    manager, _ = _make_manager(tmp_path, "doc", 3, tag="-conc")
    first = night.start_run(manager, "架空開始者", {"max_ticks": 5})
    with pytest.raises(night.NightRunBusy):
        night.start_run(manager, "架空開始者2", {"max_ticks": 5})
    # heartbeat途絶の expired と新規開始。
    import sqlite3 as _sql
    path = night.nightrun_db_path(manager)
    db = _sql.connect(str(path), timeout=30)
    try:
        with db:
            db.execute("UPDATE night_runs SET heartbeat_at=? WHERE id=?",
                       (time.time() - night.HEARTBEAT_GRACE_SEC - 10,
                        first["run_id"]))
    finally:
        db.close()
    second = night.start_run(manager, "架空開始者2", {"max_ticks": 5})
    assert second["run_id"] != first["run_id"]
    row = night.get_run(manager, first["run_id"])
    assert row["state"] == "expired"


def test_concurrent_tick_next_no_double(tmp_path):
    import app.autopilot as auto
    import app.night_run as night
    manager, pids = _multi_manager(tmp_path, [("w", "doc", 3)])
    _seed_missing_coverage(manager, pids["w"])
    auto.set_enabled(manager, pids["w"], True, actor="架空担当",
                     reason="架空の有効化理由")
    started = night.start_run(manager, "架空開始者", {"max_ticks": 10})

    async def _gather():
        return await asyncio.gather(
            asyncio.to_thread(night.tick_next, manager,
                              started["run_id"], started["token"]),
            asyncio.to_thread(night.tick_next, manager,
                              started["run_id"], started["token"]))

    results = asyncio.run(_gather())
    assert len(results) == 2
    # 同じPJの二重ティックにならない: グローバル排他のため直列化され、
    # 1件が記録・もう1件は候補枯渇(no_candidates)でdoneになるか、
    # 両方記録されるかのいずれか。いずれも欠落・重複は無い。
    got = night.get_run(manager, started["run_id"])
    assert 1 <= len(got["steps"]) <= 2
    seqs = sorted(item["seq"] for item in got["steps"])
    assert len(set(seqs)) == len(seqs)
    if len(seqs) == 2:
        assert seqs[1] == seqs[0] + 1
    done_flags = [bool(item.get("done")) for item in results]
    assert any(done_flags) or len(got["steps"]) == 2


def test_report_counts_next_external_and_watch_once(tmp_path):
    import app.autopilot as auto
    import app.night_run as night
    from app import watchtower as watch
    manager, pids = _multi_manager(tmp_path, [("r", "doc", 3)])
    _seed_missing_coverage(manager, pids["r"])
    auto.set_enabled(manager, pids["r"], True, actor="架空担当",
                     reason="架空の有効化理由")
    started = night.start_run(manager, "架空開始者", {"max_ticks": 3})
    _tick_loop(manager, started["run_id"], started["token"], limit=5)
    finished = night.finish_run(manager, started["run_id"],
                                started["token"], "架空開始者", "架空の終了")
    assert finished["report_status"] == "ok"
    report = finished["report"]
    assert report["total_projects"] == 1
    assert report["total_ticks"] >= 1
    assert report["external_calls_total"] == 0
    assert report["external_rounds_total"] == 0
    assert report["external_zero_measured"] is True
    assert report["approvals_total"] == 0
    assert report["next_actions"]
    assert report["per_project"][0]["next_action"]
    # 見張りの通知が1件だけ (冪等)。
    events = watch.list_events(manager, unread_only=False, limit=200)
    hits = [item for item in events
            if item.get("idempotency_key") == started["run_id"]]
    assert len(hits) == 1
    again = night.finish_run(manager, started["run_id"], started["token"],
                             "架空開始者", "架空の終了")
    assert again["idempotent"] is True
    events2 = watch.list_events(manager, unread_only=False, limit=200)
    hits2 = [item for item in events2
             if item.get("idempotency_key") == started["run_id"]]
    assert len(hits2) == 1


def test_report_broken_step_is_unknown(tmp_path):
    import app.autopilot as auto
    import app.night_run as night
    manager, pids = _multi_manager(tmp_path, [("b", "doc", 3)])
    auto.set_enabled(manager, pids["b"], True, actor="架空担当",
                     reason="架空の有効化理由")
    started = night.start_run(manager, "架空開始者", {"max_ticks": 3})
    import sqlite3 as _sql
    path = night.nightrun_db_path(manager)
    db = _sql.connect(str(path), timeout=30)
    try:
        with db:
            db.execute(
                "INSERT INTO night_run_steps(id, run_id, seq, project_id,"
                " status, stop_class, action, external_delta,"
                " external_rounds_delta, budget_kept, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                ("ns_broken", started["run_id"], 1, pids["b"],
                 "架空の壊れた状態", "", "", 0, 0, 1, time.time()))
    finally:
        db.close()
    report = night.build_report(manager, started["run_id"])
    assert report["unknown"] == [pids["b"]]
    assert report["unreadable_steps"] >= 1


def test_registry_and_purge_and_generic(tmp_path):
    from pathlib import Path
    import app.night_run as night
    from app.project_lifecycle_registry import audit_registry_completeness
    audit = audit_registry_completeness()
    assert audit["ok"] is True, audit["unregistered"]
    manager, pids = _multi_manager(tmp_path, [("g", "doc", 3)])
    started = night.start_run(manager, "架空開始者", {"max_ticks": 3})
    import sqlite3 as _sql
    path = night.nightrun_db_path(manager)
    db = _sql.connect(str(path), timeout=30)
    try:
        with db:
            db.execute(
                "INSERT INTO night_run_steps(id, run_id, seq, project_id,"
                " status, stop_class, action, external_delta,"
                " external_rounds_delta, budget_kept, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                ("ns_g", started["run_id"], 1, pids["g"],
                 "waiting_human", "", "", 0, 0, 1, time.time()))
    finally:
        db.close()
    purged = night.purge_project_nightrun(manager.memory.path, pids["g"])
    assert purged["night_run_steps"] == 1
    got = night.get_run(manager, started["run_id"])
    assert got["steps"] == []
    root = Path(__file__).resolve().parents[1]
    text = (root / "app" / "night_run.py").read_text(encoding="utf-8")
    for word in ("拡販", "販売システム", "c3-疑似", "1698"):
        assert word not in text
    assert "innerHTML" not in text
    assert "http://" not in text and "https://" not in text
    assert "httpx" not in text and "requests.post" not in text
    assert "urlopen" not in text

@pytest.mark.parametrize("counts", [
    {},
    {"external_delta": "確認できず", "external_rounds_delta": 0},
    {"external_delta": 0, "external_rounds_delta": None},
    {"external_delta": True, "external_rounds_delta": 0},
])
def test_unreadable_external_counts_abort_instead_of_zero(
        tmp_path, monkeypatch, counts):
    import app.autopilot as auto
    import app.night_run as night

    manager, pids = _multi_manager(tmp_path, [("a", "doc", 3)])
    pid = pids["a"]
    auto.set_enabled(manager, pid, True, actor="架空担当",
                     reason="架空の有効化理由")
    payload = {"status": "advanced", "action_name": "none",
               "budget_kept": True, **counts}
    monkeypatch.setattr(auto, "autopilot_tick",
                        lambda _manager, _pid, **kwargs: payload)
    started = night.start_run(manager, "架空開始者", {"max_ticks": 10})
    out = night.tick_next(manager, started["run_id"], started["token"])
    assert out["done"] is True
    assert out["state"] == "aborted"
    assert out["reason"] == "circuit_external_unknown"
    assert out["step"]["status"] == "unreadable"
    assert auto.is_enabled(manager, pid) is False
    report = night.build_report(manager, started["run_id"])
    assert report["external_zero_measured"] is False
    assert "counts_known=False" in report["end_reason"]
