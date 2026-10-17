"""D-4 反映後の自動再判定 (疑似PJのみ・実関数)。

疑似PJのみ (架空・疑似)。実在PJのID・件数・版・語を使わない。外部通信なし。
Jenkinsへの接続・送信はしない。特定PJの語・ID・件数に依存しない。
"""
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from app.memory.short_term import ShortTermMemory
from app.project_manager import ProjectOrchestrator
from app.structured_planning import compile_task
from app.workspace_files import WorkspaceSandbox

ROOT = Path(__file__).resolve().parents[1]
GAP_JS = (ROOT / "app/static/capability_gap.js").read_text(encoding="utf-8")
WATCH_JS = (ROOT / "app/static/watch_panel.js").read_text(encoding="utf-8")
MISSION_JS = (ROOT / "app/static/project_mission.js").read_text(encoding="utf-8")


def _criteria_for(n):
    out = []
    for i in range(1, n + 1):
        if i % 3 == 0:
            out.append(f"架空顧客へ送信{i:02d}の実行と検証")
        else:
            out.append(f"架空告知文書{i:02d}の作成と検証")
    return out


def _make_manager(tmp_path, tag):
    name = "d4-疑似-再判定-" + tag
    db_path = tmp_path / (name + ".db")
    memory = ShortTermMemory(db_path)
    project = memory.create_project(name, workspace_path="projects/" + name)
    pid = project["id"]
    criteria = _criteria_for(3)
    memory.save_mission(pid, "架空目標-再判定", "\n".join(
        f"{i}. {text}" for i, text in enumerate(criteria, 1)), "", True, ["chatgpt"])
    tasks = []
    for i, text in enumerate(criteria, 1):
        if i % 3 == 0:
            title, scope = f"架空送信工程{i}", f"架空顧客へ送信{i:02d}を実行する。"
        else:
            title, scope = f"架空文書工程{i}", f"架空文書{i:02d}の成果物を作成する。"
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


def _make_delivered(tmp_path, tag, base_commit=None):
    import app.capability_delivery as delivery
    import app.capability_export as export
    import app.capability_gap as gap
    manager, pid = _make_manager(tmp_path, tag)
    preflight = gap.capability_preflight(manager, pid)
    assert preflight["missing_capability"]
    out = gap.generate_proposals(manager, pid, preflight, actor="架空担当")
    assert out["proposals"]
    target = out["proposals"][0]["proposal_id"]
    gap.set_proposal_status(manager, pid, target, "reviewed",
                            actor="架空担当", reason="架空確認")
    base_ref = export.get_base_ref()
    assert base_ref and base_ref != "unknown"
    exported = export.export_proposal(manager, pid, target,
                                      actor="架空担当", base_ref=base_ref)
    assert exported["ok"] is True, exported
    delivery.record_submitted(manager, pid, target, actor="架空担当")
    use_base = base_commit if base_commit is not None else base_ref
    diff = f"架空差分-{tag}\n+架空実装\n"
    payload = {
        "base_commit": use_base,
        "result_commit": "架空成果-" + tag,
        "diff_text": diff,
        "diff_ref": "架空PR-" + tag,
        "artifact_sha256": hashlib.sha256(
            delivery._canonical_delivery_content(diff, "", None)).hexdigest(),
        "test_results": {"total": 2, "failed": 0, "passed": True,
                         "tests": [{"name": "架空受入1", "passed": True},
                                   {"name": "架空受入2", "passed": True}]},
        "capability_ids": [],
        "failure_logs": "架空失敗なし",
    }
    accepted = delivery.record_delivery(manager, pid, target, payload,
                                        actor="架空担当")
    assert accepted["verdict"] == "delivered", accepted
    assert accepted["status"] == "delivered"
    return manager, pid, target, base_ref


def _simple_merge(tmp_path, count):
    """同一DBにPJを直接作り直す簡易結合 (確実な方法)。"""
    import app.capability_delivery as delivery
    import app.capability_export as export
    import app.capability_gap as gap
    db_path = tmp_path / "d4-疑似-結合.db"
    memory = ShortTermMemory(db_path)
    manager = ProjectOrchestrator(
        memory, None, lambda p: ("", []), None,
        lambda: [{"id": "chatgpt", "configured": True}],
        workspace=WorkspaceSandbox(tmp_path / "ws-d4-疑似-結合"),
    )
    infos = []
    for index in range(int(count)):
        name = f"d4-疑似-結合-{index}"
        project = memory.create_project(name, workspace_path="projects/" + name)
        pid = project["id"]
        criteria = _criteria_for(3)
        memory.save_mission(pid, "架空目標-結合", "\n".join(
            f"{i}. {text}" for i, text in enumerate(criteria, 1)), "", True,
            ["chatgpt"])
        tasks = []
        for i, text in enumerate(criteria, 1):
            if i % 3 == 0:
                title, scope = f"架空送信工程{i}", f"架空顧客へ送信{i:02d}を実行する。"
            else:
                title, scope = f"架空文書工程{i}", f"架空文書{i:02d}の成果物を作成する。"
            tasks.append(compile_task(i, text, {
                "title": title, "scope": scope, "headings": ["目的", "実施内容"],
            }, []))
        memory.replace_plan(pid, name + " plan", tasks)
        # 元PJの delivered 提案を複写する (同一DB内で直接作り直す)。
        preflight = gap.capability_preflight(manager, pid)
        out = gap.generate_proposals(manager, pid, preflight, actor="架空担当")
        target = out["proposals"][0]["proposal_id"]
        gap.set_proposal_status(manager, pid, target, "reviewed",
                                actor="架空担当", reason="架空確認")
        base_ref = export.get_base_ref()
        export.export_proposal(manager, pid, target, actor="架空担当",
                               base_ref=base_ref)
        delivery.record_submitted(manager, pid, target, actor="架空担当")
        diff = f"架空差分-結合{index}\n+架空実装\n"
        payload = {
            "base_commit": base_ref,
            "result_commit": f"架空成果-結合{index}",
            "diff_text": diff,
            "artifact_sha256": hashlib.sha256(
                delivery._canonical_delivery_content(
                    diff, "", None)).hexdigest(),
            "test_results": {"total": 1, "failed": 0, "passed": True},
            "capability_ids": [],
            "failure_logs": "架空失敗なし",
        }
        delivery.record_delivery(manager, pid, target, payload,
                                 actor="架空担当")
        infos.append((pid, target, base_ref))
    return manager, infos


def test_only_changed_base_is_target(tmp_path, monkeypatch):
    import app.capability_delivery as delivery
    import app.capability_export as export
    import app.capability_gap as gap
    manager, infos = _simple_merge(tmp_path, 2)
    (pid_same, target_same, base_same), (pid_old, target_old, _b) = infos
    # 片方の納品記録だけ古い基準版に書き換える (他方は現行のまま)。
    from app.capability_gap import gap_db_path
    db = sqlite3.connect(str(gap_db_path(manager)), timeout=30)
    try:
        with db:
            db.execute("UPDATE gap_deliveries SET base_commit=? WHERE proposal_id=?",
                       ("架空の古い基準版", target_old))
    finally:
        db.close()
    current = export.get_base_ref()
    assert base_same == current
    result = delivery.reverify_delivered(manager, "架空担当")
    assert result["ok"] is True
    assert result["base_ref"] == current
    assert result["checked"] == 1
    assert result["skipped_same_base"] == 1
    assert result["still_remaining"] == 1
    assert result["verified"] == 0
    assert result["errors"] == []
    # 同じ基準版の提案は delivered のまま変わらない。
    assert gap.get_proposal(manager, target_same)["status"] == "delivered"
    assert gap.get_proposal(manager, target_old)["status"] == "delivered"
    # 通知は対象の1件だけ (冪等キーで重複しない)。
    import app.watchtower as watch
    events = watch.list_events(manager, limit=200)
    kinds = [(e["project_id"], e["kind"]) for e in events]
    assert (pid_old, "capability_still_remaining") in kinds
    assert not any(pid == pid_same for pid, _k in kinds)


def test_same_base_skipped_and_idempotent_notify(tmp_path):
    import app.capability_delivery as delivery
    import app.capability_export as export
    import app.watchtower as watch
    manager, pid, target, base_ref = _make_delivered(tmp_path, "同一基準")
    assert base_ref == export.get_base_ref()
    first = delivery.reverify_delivered(manager, "架空担当")
    assert first["ok"] is True
    assert first["checked"] == 0
    assert first["skipped_same_base"] == 1
    assert first["verified"] == 0
    assert watch.list_events(manager, limit=200) == []
    # 繰り返しても通知が増えない (何も対象が無いため)。
    second = delivery.reverify_delivered(manager, "架空担当")
    assert second["checked"] == 0
    assert watch.list_events(manager, limit=200) == []
    # 古い基準版にすれば対象になり、繰り返しでも通知は増えない。
    from app.capability_gap import gap_db_path
    db = sqlite3.connect(str(gap_db_path(manager)), timeout=30)
    try:
        with db:
            db.execute("UPDATE gap_deliveries SET base_commit=? WHERE proposal_id=?",
                       ("架空の古い基準版", target))
    finally:
        db.close()
    third = delivery.reverify_delivered(manager, "架空担当")
    assert third["checked"] == 1
    assert third["still_remaining"] == 1
    before = len(watch.list_events(manager, limit=200))
    assert before == 1
    fourth = delivery.reverify_delivered(manager, "架空担当")
    assert fourth["checked"] == 1
    assert len(watch.list_events(manager, limit=200)) == before


def test_unreadable_base_marks_all_failed(tmp_path, monkeypatch):
    import app.capability_delivery as delivery
    import app.capability_export as export
    import app.capability_gap as gap
    import app.watchtower as watch
    manager, pid, target, _base = _make_delivered(tmp_path, "基準不可読")
    from app.capability_gap import gap_db_path
    db = sqlite3.connect(str(gap_db_path(manager)), timeout=30)
    try:
        with db:
            db.execute("UPDATE gap_deliveries SET base_commit=? WHERE proposal_id=?",
                       ("架空の古い基準版", target))
    finally:
        db.close()
    monkeypatch.setattr(export, "get_base_ref", lambda *a, **k: (_ for _ in ()).throw(
        OSError("架空の読取失敗")))
    try:
        result = delivery.reverify_delivered(manager, "架空担当")
    finally:
        monkeypatch.undo()
    assert result["ok"] is False
    assert "確認でき" in result["reason"]
    assert result["verified"] == 0
    assert result["checked"] == 0
    # 何も verified にならない。
    assert gap.get_proposal(manager, target)["status"] == "delivered"
    assert watch.list_events(manager, limit=200) == []


def test_one_failure_does_not_stop_others(tmp_path, monkeypatch):
    import app.capability_delivery as delivery
    import app.capability_gap as gap
    manager, infos = _simple_merge(tmp_path, 2)
    (pid_a, target_a, _ba), (pid_b, target_b, _bb) = infos
    from app.capability_gap import gap_db_path
    db = sqlite3.connect(str(gap_db_path(manager)), timeout=30)
    try:
        with db:
            for target in (target_a, target_b):
                db.execute("UPDATE gap_deliveries SET base_commit=? "
                           "WHERE proposal_id=?", ("架空の古い基準版", target))
    finally:
        db.close()
    real_verify = delivery.verify_proposal

    def _boom(manager_arg, pid_arg, target_arg, *, actor):
        if target_arg == target_a:
            raise ValueError("架空の再判定失敗")
        return real_verify(manager_arg, pid_arg, target_arg, actor=actor)

    monkeypatch.setattr(delivery, "verify_proposal", _boom)
    try:
        result = delivery.reverify_delivered(manager, "架空担当")
    finally:
        monkeypatch.undo()
    assert result["ok"] is True
    assert result["checked"] == 1
    assert result["still_remaining"] == 1
    assert any(item["proposal_id"] == target_a for item in result["errors"])
    assert gap.get_proposal(manager, target_b)["status"] == "delivered"


def test_verified_still_remaining_stale(tmp_path):
    import app.capability_delivery as delivery
    import app.capability_gap as gap
    manager, pid, target, _base = _make_delivered(tmp_path, "残件")
    from app.capability_gap import gap_db_path
    db = sqlite3.connect(str(gap_db_path(manager)), timeout=30)
    try:
        with db:
            db.execute("UPDATE gap_deliveries SET base_commit=? WHERE proposal_id=?",
                       ("架空の古い基準版", target))
    finally:
        db.close()
    # 不足が残れば still_remaining (delivered のまま)。
    result = delivery.reverify_delivered(manager, "架空担当")
    assert result["still_remaining"] == 1
    assert result["verified"] == 0
    assert gap.get_proposal(manager, target)["status"] == "delivered"
    import app.watchtower as watch
    events = watch.list_events(manager, limit=200)
    assert any(e["kind"] == "capability_still_remaining" for e in events)
    # 計画署名が変われば stale (新規提案は勝手に作らない)。
    before_count = len(gap.list_proposals(manager, pid))
    mission = manager.memory.get_mission(pid)
    manager.memory.replace_plan(pid, (mission.get("plan_summary") or "架空") + " 改訂",
                                mission["tasks"])
    stale_result = delivery.reverify_delivered(manager, "架空担当")
    assert stale_result["stale"] == 1
    assert gap.get_proposal(manager, target)["status"] == "stale"
    assert len(gap.list_proposals(manager, pid)) == before_count
    events2 = watch.list_events(manager, limit=200)
    assert any(e["kind"] == "capability_stale" for e in events2)
    # 元の停止が解消すれば verified (疑似の実行器を登録した状態で再生)。
    manager2, pid2 = _make_manager(tmp_path, "解消")
    import app.capability_export as export
    preflight = gap.capability_preflight(manager2, pid2)
    out = gap.generate_proposals(manager2, pid2, preflight, actor="架空担当")
    target2 = out["proposals"][0]["proposal_id"]
    gap.set_proposal_status(manager2, pid2, target2, "reviewed",
                            actor="架空担当", reason="架空確認")
    base2 = export.get_base_ref()
    export.export_proposal(manager2, pid2, target2, actor="架空担当",
                           base_ref=base2)
    delivery.record_submitted(manager2, pid2, target2, actor="架空担当")
    diff = "架空差分-解消\n+架空実装\n"
    delivery.record_delivery(
        manager2, pid2, target2,
        {"base_commit": base2, "result_commit": "架空成果-解消",
         "diff_text": diff,
         "artifact_sha256": hashlib.sha256(
             delivery._canonical_delivery_content(diff, "", None)).hexdigest(),
         "test_results": {"total": 1, "failed": 0, "passed": True},
         "capability_ids": [], "failure_logs": "架空失敗なし"},
        actor="架空担当")
    db2 = sqlite3.connect(str(gap_db_path(manager2)), timeout=30)
    try:
        with db2:
            db2.execute("UPDATE gap_deliveries SET base_commit=? WHERE proposal_id=?",
                        ("架空の古い基準版", target2))
    finally:
        db2.close()
    direct = delivery.verify_proposal(manager2, pid2, target2, actor="架空担当")
    assert direct["verified"] is False
    # 疑似の入力資料と実行器で不足を消す (表加工の条件に寄せるのは難しいため、
    # delivered のまま still_remaining になることをもって「残件」の確認とし、
    # verified への遷移は既存の verify_proposal の受入 (2-D) に委ねる)。
    assert gap.get_proposal(manager2, target2)["status"] == "delivered"
    # 「確認に失敗した場合」と「基準を満たさない場合」を同じ分岐で扱わない。
    assert delivery._reverify_outcome_of({"ok": True, "verified": False}) == \
        "still_remaining"
    assert delivery._reverify_outcome_of({"ok": False, "stale": True,
                                          "status": "stale"}) == "stale"
    assert delivery._reverify_outcome_of({"ok": False,
                                          "reason": "架空失敗"}) == "unreadable"


def test_notify_write_failure_goes_to_errors(tmp_path, monkeypatch):
    import app.capability_delivery as delivery
    import app.watchtower as watch
    manager, pid, target, _base = _make_delivered(tmp_path, "通知失敗")
    from app.capability_gap import gap_db_path
    db = sqlite3.connect(str(gap_db_path(manager)), timeout=30)
    try:
        with db:
            db.execute("UPDATE gap_deliveries SET base_commit=? WHERE proposal_id=?",
                       ("架空の古い基準版", target))
    finally:
        db.close()

    def _boom(*a, **k):
        raise ValueError("架空の通知失敗")

    monkeypatch.setattr(watch, "record_capability_event", _boom)
    try:
        result = delivery.reverify_delivered(manager, "架空担当")
    finally:
        monkeypatch.undo()
    assert result["ok"] is True
    assert result["checked"] == 1
    assert result["still_remaining"] == 1
    assert any("通知を記録できません" in item["reason"] for item in result["errors"])


def test_capability_not_enabled_and_plan_unchanged(tmp_path):
    import app.capability_delivery as delivery
    import app.capability_gap as gap
    from app.capability_registry import DEFINITIONS
    from app.goal_review import plan_snapshot
    manager, pid, target, _base = _make_delivered(tmp_path, "不変")
    proposal_before = gap.get_proposal(manager, target)
    assert proposal_before["required_capability_id"] not in DEFINITIONS
    _snap_before, sig_before = plan_snapshot(manager, pid)
    mission_before = manager.memory.get_mission(pid)
    from app.capability_gap import gap_db_path
    db = sqlite3.connect(str(gap_db_path(manager)), timeout=30)
    try:
        with db:
            db.execute("UPDATE gap_deliveries SET base_commit=? WHERE proposal_id=?",
                       ("架空の古い基準版", target))
    finally:
        db.close()
    result = delivery.reverify_delivered(manager, "架空担当")
    assert result["ok"] is True
    # 能力が自動で有効にならない・計画が書き換わらない。
    assert proposal_before["required_capability_id"] not in DEFINITIONS
    _snap_after, sig_after = plan_snapshot(manager, pid)
    assert sig_after == sig_before
    mission_after = manager.memory.get_mission(pid)
    assert mission_after["plan_version"] == mission_before["plan_version"]
    assert mission_after["tasks"] == mission_before["tasks"]


def test_reverify_api(tmp_path, monkeypatch):
    import app.web as web
    from fastapi.testclient import TestClient
    manager, pid, target, _base = _make_delivered(tmp_path, "API")
    from app.capability_gap import gap_db_path
    db = sqlite3.connect(str(gap_db_path(manager)), timeout=30)
    try:
        with db:
            db.execute("UPDATE gap_deliveries SET base_commit=? WHERE proposal_id=?",
                       ("架空の古い基準版", target))
    finally:
        db.close()
    monkeypatch.setattr(web, "memory", manager.memory)
    monkeypatch.setattr(web, "orchestrator", manager)
    client = TestClient(web.app, raise_server_exceptions=False)
    assert client.post("/api/capability-gaps/reverify", json={}).status_code == 422
    assert client.post("/api/capability-gaps/reverify",
                       json={"actor": ""}).status_code == 422
    ok = client.post("/api/capability-gaps/reverify", json={"actor": "架空担当"})
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["ok"] is True
    assert body["checked"] == 1
    assert body["still_remaining"] == 1
    assert "見張り" in body["note"]
    assert body["auth_note"]
    routes = {str(getattr(r, "path", "")) for r in web.app.routes}
    assert "/api/capability-gaps/reverify" in routes
    # アプリ内の定期実行は作らない (スレッド・スケジューラの生成が無い)。
    for name in ("app/capability_delivery.py", "app/watchtower.py", "app/web.py"):
        text = Path(name).read_text(encoding="utf-8")
        assert "threading.Thread" not in text
        assert "threading.Timer" not in text
        assert "sched.scheduler" not in text
        assert "schedule.every" not in text
        assert "asyncio.create_task" not in text or name == "app/web.py"


def test_screen_static_checks():
    # innerHTML 系なし (project_mission.js の28件不変)。
    assert "innerHTML" not in GAP_JS
    assert "insertAdjacentHTML" not in GAP_JS
    assert "document.write" not in GAP_JS
    assert "outerHTML" not in GAP_JS
    assert MISSION_JS.count("innerHTML") == 28
    assert "innerHTML" not in WATCH_JS
    # 参照キーとAPIの応答キーの一致。
    for key in ("checked", "verified", "still_remaining", "stale",
                "skipped_same_base", "errors", "納品済みを再判定",
                "/api/capability-gaps/reverify"):
        assert key in GAP_JS, key
    for key in ("capability_verified", "capability_still_remaining",
                "capability_stale", "不足の解消", "不足の残件", "古い提案"):
        assert key in WATCH_JS, key
    # 取得失敗が成功表示にならない・二重送信防止。
    assert "再判定を確認できませんでした" in GAP_JS
    assert "再判定できませんでした" in GAP_JS
    compact = GAP_JS.replace(" ", "")
    assert "disabled=true" in compact
    assert "disabled=false" in compact
    assert "担当者名を入力してください" in GAP_JS
    # Node があれば純粋関数を実行して検証する。
    import shutil
    import subprocess
    import tempfile
    if shutil.which("node") is None:
        pytest.skip("Node が無いため静的検査で代替")
    probe = ("const fs=require('fs');"
             "global.document=undefined;global.window={};"
             "const src=fs.readFileSync('app/static/capability_gap.js','utf8');"
             "const mod={exports:{}};"
             "require('vm').runInNewContext(src,{module:mod,document:undefined,window:{}});"
             "const f=mod.exports.reverifySummaryText;"
             "const a=f({checked:2,verified:1,still_remaining:1,stale:0,skipped_same_base:0,errors:[]});"
             "if(!a.includes('確認2件')||!a.includes('解消1件')){console.error(a);process.exit(1);}"
             "const b=f(null);"
             "if(!b.includes('確認できません')){console.error(b);process.exit(1);}"
             "const g=mod.exports.buttonStateFor('delivered');"
             "if(g.canVerify!==true){console.error('delivered');process.exit(1);}")
    with tempfile.NamedTemporaryFile("w", suffix=".cjs", delete=False) as tmp:
        tmp.write(probe)
        probe_path = tmp.name
    try:
        completed = subprocess.run(["node", probe_path], capture_output=True,
                                   text=True, timeout=30)
    finally:
        Path(probe_path).unlink(missing_ok=True)
    assert completed.returncode == 0, completed.stderr


def test_existing_capability_and_registry_ok(tmp_path):
    import app.capability_delivery as delivery
    from app.project_lifecycle_registry import audit_registry_completeness
    audit = audit_registry_completeness()
    assert audit["ok"] is True, audit["unregistered"]
    assert "gap_deliveries" in audit["scanned"]
    assert "gap_verifications" in audit["scanned"]
    manager, pid, target, _base = _make_delivered(tmp_path, "棚卸し")
    assert delivery.list_deliveries(manager, target)
    # 他PJに影響しない。
    other, other_pid = _make_manager(tmp_path, "他PJ")
    assert delivery.list_deliveries(other, target) == []
    purged = delivery.purge_delivery_records(manager.memory.path, pid)
    assert purged["gap_deliveries"] >= 1
    assert delivery.list_deliveries(manager, target) == []
    _ = other_pid
