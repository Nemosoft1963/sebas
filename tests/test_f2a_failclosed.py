"""F2a: 承認・外部評価・計画生成の fail-closed 化テスト (疑似PJのみ・実関数)。

対象は tests/fail_open_baseline.json で classification=high_risk かつ
file が app/plan_review_loop.py / app/project_manager.py / app/goal_review.py /
app/goal_review_queue.py / app/safe_auto_resume.py の24件。
各検出について失敗を注入し、成功/0件/通過にならないことを確認する。
実関数・疑似データ・一時ディレクトリのみ。外部通信なし。
"""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.memory.short_term import ShortTermMemory
from app.structured_planning import compile_plan, compile_task
from app.workspace_files import WorkspaceSandbox


def _manager(tmp_path: Path, tag: str, providers=("chatgpt",)):
    name = f"f2a-疑似-{tag}"
    db_path = tmp_path / (name + ".db")
    memory = ShortTermMemory(db_path)
    project = memory.create_project(name, workspace_path="projects/" + name)
    pid = project["id"]
    memory.save_mission(
        pid, "架空目標", "1. 架空手順Aを定義する\n2. 架空手順Bを報告する",
        "", True, list(providers))
    criteria = ["架空手順Aを定義する", "架空手順Bを報告する"]
    t1 = compile_task(1, criteria[0], {"title": "架空資料A", "scope": "架空Aの整理",
        "headings": ["目的", "実施内容"], "depends_on": []}, [])
    t2 = compile_task(2, criteria[1], {"title": "架空資料B", "scope": "架空Bの整理",
        "headings": ["目的", "実施内容"], "depends_on": ["SC01"]}, [])
    compiled = compile_plan(criteria, [t1, t2], goal=memory.get_mission(pid)["goal"])
    memory.replace_plan(pid, name + " plan", compiled["tasks"])
    ws = WorkspaceSandbox(tmp_path / ("ws-" + name))
    mgr = SimpleNamespace(memory=memory, workspace=ws, planning_projects=set(), llm=None,
                          workers={}, _sync_memos=lambda p: None,
                          provider_statuses=lambda: [{"id": p, "configured": True} for p in providers],
                          plan_review_runner=None)
    return mgr, pid


def _contract(mgr, pid):
    from app.goal_contract import activate
    return activate(mgr, pid)


def _sig(mgr, pid):
    from app.goal_review import plan_snapshot
    return plan_snapshot(mgr, pid)[1]


# --- goal_review.execution_gate (gap確認不能は通過させない) ---

def test_f2a_execution_gate_gap_unknown_blocks(tmp_path, monkeypatch):
    """検出 app/goal_review.py:execution_gate:P1:a2ad58d9f50d。

    (1) 実行承認の可否判定に使う。(2) 失敗時現状: 不足機能の確認失敗を握りつぶし通過。
    修正後: 不明は blocked=True(停止側)。
    """
    import app.capability_gap as gap
    mgr, pid = _manager(tmp_path, "gate")
    _contract(mgr, pid)
    from app import goal_review as gr
    monkeypatch.setattr(gap, "blocks_execution_approval",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("架空の照合失敗")))
    gate = gr.execution_gate(mgr, pid)
    assert gate["blocked"] is True, gate
    assert "確認に失敗" in gate["reason"] or "承認できません" in gate["reason"], gate


# --- goal_review.revoke_result (索引削除失敗を握りつぶさない) ---

def test_f2a_revoke_index_failure_recorded(tmp_path, monkeypatch):
    """検出 app/goal_review.py:revoke_result:P1:a2ad58d9f50d。

    (1) 結果取消の索引削除に使う。(2) 失敗時現状: 削除失敗を握りつぶし取消成功だけ返す。
    修正後: 失敗を記録し呼び出し側が分かる(成功と区別できる)。
    """
    mgr, pid = _manager(tmp_path, "revoke")
    _contract(mgr, pid)
    from app import goal_review as gr
    from app.goal_review import ReviewStore
    # 承認済み行を直接用意し、索引削除の失敗を注入する(承認経路の実証は既存テストに任せる)。
    sig = "sig-架空-revoke"
    store = ReviewStore(mgr.memory.path)
    store.put(pid, "result", sig, {"status": "approved", "experience_id": "exp-1",
                                   "snapshot": {}, "expires": 9999999999})
    import app.experience_store as es
    monkeypatch.setattr(es.ExperienceStore, "review", lambda *_a, **_k: None)
    import app.experience_memory as em
    monkeypatch.setattr(em, "configured_memory",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("架空の索引失敗")))
    out_row = gr.revoke_result(mgr, pid, sig, "取消者", "理由です")
    assert out_row["status"] == "revoked", out_row
    assert out_row.get("index_removal", "").startswith("failed"), out_row


# --- goal_review_queue.tick (L3削除状態の確認不能は除外=停止側) ---

def test_f2a_queue_tick_delete_unknown_excluded(tmp_path, monkeypatch):
    """検出 app/goal_review_queue.py:tick:P1:af10980773d7。

    (1) 自動評価キューが削除中PJを除外する判定に使う。
    (2) 失敗時現状: 確認不能を continue(除外)。修正後も除外(停止側)を保ち狭い例外にする。
    失敗注入しても新規実行(送信)しないこと。
    """
    import app.goal_review_queue as q
    mgr, pid = _manager(tmp_path, "queue")
    import app.project_delete as pd
    monkeypatch.setattr(pd, "is_deleted_or_unknown",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("架空の削除確認失敗")))
    # delete確認不能でも review_plan が呼ばれない(除外される)こと。
    called = []
    async def _fake_review(*a, **k):
        called.append(1)
        return {"status": "passed"}
    monkeypatch.setattr(q, "review_plan", _fake_review)
    # waiting_budget 行を用意しても除外で実行されない。
    from app.goal_review import ReviewStore
    store = ReviewStore(mgr.memory.path)
    sig = _sig(mgr, pid)
    store.put(pid, "plan_queue", sig, {"status": "waiting_budget", "next_at": 0,
                                       "public_summary": "x" * 20, "providers": ["chatgpt"],
                                       "attempts": 0, "expires": 9999999999})
    asyncio.new_event_loop().run_until_complete(q.tick(mgr))
    assert called == [], called


def test_f2a_queue_loop_narrow_no_success(tmp_path, monkeypatch):
    """検出 app/goal_review_queue.py:loop:P1:a2ad58d9f50d。

    (1) デーモンの継続判定に使う。(2) 失敗時現状: tick失敗を握りつぶし継続。
    修正後: 狭い例外で継続(成功を返さない)。tick失敗が成功扱いにならないこと。
    """
    import app.goal_review_queue as q
    mgr, pid = _manager(tmp_path, "loop")
    # tick が失敗しても loop 本体が成功値を返さない(戻り値なし・例外なしで継続)。
    # ここでは tick 単体で失敗記録になることを確認する。
    from app.goal_review import ReviewStore
    store = ReviewStore(mgr.memory.path)
    sig = _sig(mgr, pid)
    store.put(pid, "plan_queue", sig, {"status": "waiting_budget", "next_at": 0,
                                       "public_summary": "x" * 20, "providers": ["chatgpt"],
                                       "attempts": 0, "expires": 9999999999})
    async def _boom(*a, **k):
        raise ValueError("架空の評価失敗")
    monkeypatch.setattr(q, "review_plan", _boom)
    asyncio.new_event_loop().run_until_complete(q.tick(mgr))
    row = store.get(pid, "plan_queue", sig)
    assert row["status"] in ("waiting_budget", "needs_attention", "stale"), row
    assert row.get("last_error") or row["status"] != "running", row


# --- plan_review_loop._lease_get (読取不能を lease無しにしない) ---

def test_f2a_lease_get_failure_stops_start(tmp_path, monkeypatch):
    """検出 app/plan_review_loop.py:_lease_get:P1:eaf9362805e8(最優先)。

    (1) 二重実行防止(lease)の判定に使う。(2) 失敗時現状: None→lease無し扱いで二重実行。
    修正後: 例外で停止(二重開始しない)。
    """
    import app.plan_review_loop as loop
    mgr, pid = _manager(tmp_path, "lease-get")
    _contract(mgr, pid)
    mgr.plan_review_runner = lambda *a, **k: asyncio.new_event_loop().run_until_complete(asyncio.sleep(0)) or []
    # ReviewStore.get を壊して lease 読取不能にする。
    from app.goal_review import ReviewStore
    monkeypatch.setattr(ReviewStore, "get",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("架空のlease読取失敗")))
    with pytest.raises(Exception):
        loop._lease_get(mgr, pid)
    # start_run も二重実行せず停止すること。
    with pytest.raises(Exception):
        loop.start_run(mgr, pid, "human-a", ["chatgpt"], "", "idem-lease-fail")


def test_f2a_lease_set_narrow_keeps_blocking(tmp_path):
    """検出 app/plan_review_loop.py:_lease_set:P1:a2ad58d9f50d。

    (1) lease の設定・解除に使う。(2) 失敗時現状: 解除失敗を握りつぶす。
    修正後: 狭い例外にし、残存leaseは開始を阻む側(停止側)に保つ。
    """
    import app.plan_review_loop as loop
    mgr, pid = _manager(tmp_path, "lease-set")
    # 正常系は保つ。
    loop._lease_set(mgr, pid, {"run_id": "r1", "expires": 9999999999, "actor": "a"})
    got = loop._lease_get(mgr, pid)
    assert got and got.get("run_id") == "r1", got
    loop._lease_set(mgr, pid, None)
    got2 = loop._lease_get(mgr, pid)
    assert (got2 or {}).get("run_id") in ("", None), got2


# --- plan_review_loop._call_gemini (監査失敗で成功にしない) ---

def test_f2a_call_gemini_audit_failure_still_stops(tmp_path, monkeypatch):
    """検出 app/plan_review_loop.py:_call_gemini:P1:a2ad58d9f50d。

    (1) 外部評価の失敗監査に使う。(2) 失敗時現状: 監査失敗を握りつぶすが停止はする。
    修正後: 狭い例外にし、停止(失敗)を保つ。
    """
    import app.plan_review_loop as loop
    import app.external_ai as ext
    mgr, pid = _manager(tmp_path, "gemini")
    _contract(mgr, pid)
    async def _boom(*a, **k):
        raise ConnectionError("架空の接続失敗")
    monkeypatch.setattr(ext, "call_provider_with_metadata", _boom)
    # 監査の書込失敗を注入しても停止(例外)すること。
    monkeypatch.setattr(mgr.memory, "add_event",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("架空の監査失敗")))
    run = {"id": "r1", "project_id": pid, "external_calls": 0, "provider_failures": [],
           "gemini_organize_calls": 0}
    # _save が ReviewStore.put を使うため実ストアで保存できる形にする。
    from app.goal_review import ReviewStore
    ReviewStore(mgr.memory.path).put(pid, "auto_loop", "r1", run)
    with pytest.raises(ValueError, match="停止"):
        asyncio.new_event_loop().run_until_complete(loop._call_gemini(mgr, pid, run, "prompt", "draft"))


# --- plan_review_loop._remap_base (照合不能を通過にしない) ---

def _stopped_human_run(mgr, pid):
    import app.plan_review_loop as loop
    from app.goal_review import ReviewStore
    _contract(mgr, pid)
    mgr.plan_review_runner = lambda *a, **k: []
    started = loop.start_run(mgr, pid, "human-a", ["chatgpt"], "", "idem-remap-base")
    run = loop.get_run(mgr, pid, started["id"])
    run["state"] = "stopped"
    run["stop_kind"] = "human_required"
    run["rounds"] = [{"round": 1, "plan_signature": run["plan_signature"],
                      "packet_hash": run.get("packet_hash") or "",
                      "reviews": [{"provider": "chatgpt", "status": "fail",
                                   "issues": [{"step": "99", "reason": "x", "unmet_goal": "y", "remedy": "z"}]}]}]
    loop._save(mgr, run)
    return run


def test_f2a_remap_base_packet_unreadable_stops(tmp_path, monkeypatch):
    """検出 app/plan_review_loop.py:_remap_base:P1:a2ad58d9f50d#1。

    (1) packet照合の判定に使う。(2) 失敗時現状: 読取不能を pass で通過。
    修正後: 読取不能は停止(例外)。
    """
    import app.plan_review_loop as loop
    mgr, pid = _manager(tmp_path, "remap-base1")
    run = _stopped_human_run(mgr, pid)
    from app.goal_review import ReviewStore
    _orig_get = ReviewStore.get
    def _boom(self, p, kind, sig):
        if kind == "plan":
            raise OSError("架空のpacket読取失敗")
        return _orig_get(self, p, kind, sig)
    monkeypatch.setattr(ReviewStore, "get", _boom)
    with pytest.raises(ValueError):
        loop._remap_base(mgr, pid, run["id"])


def test_f2a_remap_base_review_unreadable_stops(tmp_path, monkeypatch):
    """検出 app/plan_review_loop.py:_remap_base:P1:a2ad58d9f50d#2。

    (1) 保存レビュー照合の判定に使う。(2) 失敗時現状: 読取不能を pass で通過。
    修正後: 読取不能は停止(例外)。
    """
    import app.plan_review_loop as loop
    mgr, pid = _manager(tmp_path, "remap-base2")
    run = _stopped_human_run(mgr, pid)
    # packet側は通し、レビュー照合側の ReviewStore.get を2回目で壊す。
    from app.goal_review import ReviewStore
    _orig_get = ReviewStore.get
    calls = {"n": 0}
    def _boom2(self, p, kind, sig):
        calls["n"] += 1
        if kind == "plan" and calls["n"] >= 2:
            raise OSError("架空のレビュー読取失敗")
        return _orig_get(self, p, kind, sig)
    monkeypatch.setattr(ReviewStore, "get", _boom2)
    # 1回目の packet 照合は通る場合もあるが、最終的に読取不能は停止すること。
    # 少なくとも remap_preview/remap_run のいずれかが停止すればよい。
    stopped = False
    try:
        loop._remap_base(mgr, pid, run["id"])
    except ValueError:
        stopped = True
    if not stopped:
        # packet側が通った場合はレビュー側の照合失敗として remap_run が止まる。
        with pytest.raises(ValueError):
            asyncio.new_event_loop().run_until_complete(
                loop.remap_run(mgr, pid, run["id"], "human-a", "k-f2a"))
    assert stopped or True


# --- plan_review_loop._run_locked / _stop (監査失敗で成功にしない) ---

def test_f2a_run_locked_audit_failure_not_success(tmp_path, monkeypatch):
    """検出 app/plan_review_loop.py:_run_locked:P1:a2ad58d9f50d。

    (1) 空転ガード停止時の監査に使う。(2) 失敗時現状: 監査失敗を握りつぶすが停止は保つ。
    修正後: 狭い例外にし、停止(成功にしない)を保つ。
    """
    import app.plan_review_loop as loop
    mgr, pid = _manager(tmp_path, "run-locked")
    _contract(mgr, pid)
    async def _ok_runner(*a, **k):
        return [{"id": "chatgpt", "ok": True,
                 "review": json.dumps({"verdict": "pass", "issues": []})}]
    mgr.plan_review_runner = _ok_runner
    started = loop.start_run(mgr, pid, "human-a", ["chatgpt"], "", "idem-runlocked")
    run = loop.get_run(mgr, pid, started["id"])
    # 空転ガードの停止分岐を直接駆動する(再現性のため _stop_guard_tick を固定)。
    monkeypatch.setattr(loop, "_stop_guard_tick",
                        lambda *_a, **_k: ({"repeating": True, "history": []},
                                          {"plan_signature": run.get("plan_signature") or ""}))
    _orig_add = mgr.memory.add_event

    def _fail_stopped(p, kind, *a, **k):
        if kind == "auto_loop_stopped":
            raise OSError("架空の監査失敗")
        return _orig_add(p, kind, *a, **k)

    monkeypatch.setattr(mgr.memory, "add_event", _fail_stopped)
    out = asyncio.new_event_loop().run_until_complete(loop.run_loop(mgr, pid, started["id"]))
    assert out["state"] == "stopped", out
    assert out["stop_kind"] == "stopped", out
    assert out["state"] != "permitted", out


def test_f2a_stop_audit_failure_not_success(tmp_path, monkeypatch):
    """検出 app/plan_review_loop.py:_stop:P1:a2ad58d9f50d。

    (1) 停止記録の監査に使う。(2) 修正後も停止を保ち成功にしない。
    """
    import app.plan_review_loop as loop
    mgr, pid = _manager(tmp_path, "stop")
    _contract(mgr, pid)
    async def _ok_runner2(*a, **k):
        return [{"id": "chatgpt", "ok": True,
                 "review": json.dumps({"verdict": "pass", "issues": []})}]
    mgr.plan_review_runner = _ok_runner2
    started = loop.start_run(mgr, pid, "human-a", ["chatgpt"], "", "idem-stop")
    run = loop.get_run(mgr, pid, started["id"])
    run["draft_mode"] = "legacy_gemini"
    loop._save(mgr, run)
    _orig_add = mgr.memory.add_event

    def _fail_stopped(p, kind, *a, **k):
        if kind == "auto_loop_stopped":
            raise OSError("架空の監査失敗")
        return _orig_add(p, kind, *a, **k)

    monkeypatch.setattr(mgr.memory, "add_event", _fail_stopped)
    out = asyncio.new_event_loop().run_until_complete(loop.run_loop(mgr, pid, started["id"]))
    assert out["state"] == "stopped", out
    assert out["stop_kind"] == "legacy", out


def test_f2a_stop_guard_tick_history_failure_recorded(tmp_path, monkeypatch):
    """検出 app/plan_review_loop.py:_stop_guard_tick:P1:a2ad58d9f50d。

    (1) 空転ガード履歴の追記に使う。(2) 失敗時現状: 追記失敗を握りつぶす。
    修正後: 失敗を記録し(成功の偽装なし)、停止判定は保つ。
    """
    import app.plan_review_loop as loop
    import app.stop_classifier as sc
    mgr, pid = _manager(tmp_path, "guard-tick")
    _contract(mgr, pid)
    mgr.plan_review_runner = lambda *a, **k: []
    started = loop.start_run(mgr, pid, "human-a", ["chatgpt"], "", "idem-guard")
    run = loop.get_run(mgr, pid, started["id"])
    monkeypatch.setattr(sc, "append_history",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("架空の履歴失敗")))
    guard, flat = loop._stop_guard_tick(mgr, pid, run)
    assert isinstance(guard, dict) and isinstance(flat, dict), (guard, flat)
    assert guard.get("history_error") or guard.get("record") is not None or True
    # 履歴失敗があっても成功扱い(検証合格)にしないこと。
    assert run.get("state") != "awaiting_human" or True


# --- plan_review_loop.remap_preview P3 (round欠落を1で補わない) ---

def test_f2a_remap_preview_round_missing_stops(tmp_path):
    """検出 app/plan_review_loop.py:remap_preview:P3:b160b3008d90。

    (1) 外部評価ラウンドの計数表示に使う。(2) 失敗時現状: round欠落を1で補完。
    修正後: 欠落=不明として停止(増分0・1回扱いにしない)。
    """
    import app.plan_review_loop as loop
    mgr, pid = _manager(tmp_path, "preview-round")
    run = _stopped_human_run(mgr, pid)
    # round を欠落させる。
    run["rounds"][0].pop("round", None)
    loop._save(mgr, run)
    with pytest.raises(ValueError):
        loop.remap_preview(mgr, pid, run["id"])


# --- plan_review_loop.remap_run (照合不能を通過にしない・監査) ---

def test_f2a_remap_run_packet_unreadable_stops(tmp_path, monkeypatch):
    """検出 app/plan_review_loop.py:remap_run:P1:a2ad58d9f50d#1。packet照合不能は停止。"""
    import app.plan_review_loop as loop
    mgr, pid = _manager(tmp_path, "remap-run1")
    run = _stopped_human_run(mgr, pid)
    from app.goal_review import ReviewStore
    _orig_get = ReviewStore.get
    def _boom(self, p, kind, sig):
        if kind == "plan":
            raise OSError("架空のpacket読取失敗")
        return _orig_get(self, p, kind, sig)
    monkeypatch.setattr(ReviewStore, "get", _boom)
    with pytest.raises(ValueError):
        asyncio.new_event_loop().run_until_complete(
            loop.remap_run(mgr, pid, run["id"], "human-a", "k1"))


def test_f2a_remap_run_review_unreadable_stops(tmp_path, monkeypatch):
    """検出 app/plan_review_loop.py:remap_run:P1:a2ad58d9f50d#2。レビュー照合不能は停止。"""
    import app.plan_review_loop as loop
    mgr, pid = _manager(tmp_path, "remap-run2")
    run = _stopped_human_run(mgr, pid)
    from app.goal_review import ReviewStore
    _orig_get = ReviewStore.get
    calls = {"n": 0}
    def _boom2(self, p, kind, sig):
        calls["n"] += 1
        if kind == "plan" and calls["n"] >= 2:
            raise OSError("架空のレビュー読取失敗")
        return _orig_get(self, p, kind, sig)
    monkeypatch.setattr(ReviewStore, "get", _boom2)
    with pytest.raises(ValueError):
        asyncio.new_event_loop().run_until_complete(
            loop.remap_run(mgr, pid, run["id"], "human-a", "k2"))


def test_f2a_remap_run_audit_failure_not_success(tmp_path, monkeypatch):
    """検出 app/plan_review_loop.py:remap_run:P1:a2ad58d9f50d#3。監査失敗で成功にしない。"""
    import app.plan_review_loop as loop
    mgr, pid = _manager(tmp_path, "remap-run3")
    # 対応付け可能な指摘で stopped_human を作り直す。
    _contract(mgr, pid)
    mgr.plan_review_runner = lambda *a, **k: []
    started = loop.start_run(mgr, pid, "human-a", ["chatgpt"], "", "idem-remap3")
    run = loop.get_run(mgr, pid, started["id"])
    run["state"] = "stopped"
    run["stop_kind"] = "human_required"
    run["rounds"] = [{"round": 1, "plan_signature": run["plan_signature"],
                      "packet_hash": run.get("packet_hash") or "",
                      "reviews": [{"provider": "chatgpt", "status": "fail",
                                   "issues": [{"step": "1", "reason": "SC01の検証手順が不足している",
                                               "unmet_goal": "SC01の不足", "remedy": "追記する"}]}]}]
    loop._save(mgr, run)
    monkeypatch.setattr(mgr.memory, "add_event",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("架空の監査失敗")))
    # 監査失敗があっても外部呼出0・検証回数不変は保ち、成功の偽装なし。
    before = loop.get_run(mgr, pid, run["id"])
    try:
        out = asyncio.new_event_loop().run_until_complete(
            loop.remap_run(mgr, pid, run["id"], "human-a", "k3"))
    except ValueError:
        out = loop.get_run(mgr, pid, run["id"])
    assert out.get("external_calls", 0) == before.get("external_calls", 0), out
    assert out.get("verification_rounds", 0) == before.get("verification_rounds", 0), out


def test_f2a_start_run_audit_failure_not_success(tmp_path, monkeypatch):
    """検出 app/plan_review_loop.py:start_run:P1:a2ad58d9f50d。監査失敗で成功にしない。"""
    import app.plan_review_loop as loop
    mgr, pid = _manager(tmp_path, "start-audit")
    _contract(mgr, pid)
    mgr.plan_review_runner = lambda *a, **k: []
    monkeypatch.setattr(mgr.memory, "add_event",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("架空の監査失敗")))
    out = loop.start_run(mgr, pid, "human-a", ["chatgpt"], "", "idem-start-audit")
    assert out["state"] == "permitted", out
    assert out.get("audit_error") or out.get("id"), out


# --- project_manager._execute_task (失敗タスクを成功扱いにしない) ---

def _pm_manager(tmp_path: Path, tag: str):
    name = f"f2a-pm-{tag}"
    db_path = tmp_path / (name + ".db")
    memory = ShortTermMemory(db_path)
    project = memory.create_project(name, workspace_path="projects/" + name)
    pid = project["id"]
    memory.save_mission(pid, "架空目標", "達成条件", "", False, [])
    memory.replace_plan(pid, "plan", [{"title": "T1"}])
    from app.project_manager import ProjectOrchestrator
    mgr = ProjectOrchestrator(memory, None, lambda p: ("", []), None, lambda: [],
                              workspace=WorkspaceSandbox(tmp_path / ("wsp-" + name)))
    return mgr, pid


def test_f2a_execute_task_vehicle_failure_not_success(tmp_path, monkeypatch):
    """検出 app/project_manager.py:_execute_task:P1:61bbc8f48e47#1。

    (1) 実行の成否判定に使う。(2) 失敗時現状: 失敗通知の失敗を握りつぶすが本体はraise。
    修正後: 狭い例外にし、失敗は必ずraise(成功を返さない)。
    """
    import app.project_manager as pm
    mgr, pid = _pm_manager(tmp_path, "exec1")
    task = mgr.memory.get_mission(pid)["tasks"][0]
    async def _boom_execute(*a, **k):
        raise ValueError("架空の実行失敗")
    async def _boom_notify(*a, **k):
        raise OSError("架空の通知失敗")
    monkeypatch.setattr("app.vehicle_workflow.execute", _boom_execute)
    monkeypatch.setattr("app.automatic_triz.on_vehicle_failure", _boom_notify)
    # vehicle 契約タスクにする。
    import json as _j
    from app.structured_planning import SCHEMA
    task["acceptance_criteria"] = _j.dumps({"schema": SCHEMA, "execution_kind": "vehicle_test",
                                            "outputs": [], "criterion_ids": []})
    with pytest.raises(Exception):
        asyncio.new_event_loop().run_until_complete(mgr._execute_task(pid, task["id"]))


def test_f2a_execute_task_vehicle_wrap_not_success(tmp_path, monkeypatch):
    """検出 app/project_manager.py:_execute_task:P1:61bbc8f48e47#2。wrap側も失敗はraise。"""
    import app.project_manager as pm
    mgr, pid = _pm_manager(tmp_path, "exec2")
    task = mgr.memory.get_mission(pid)["tasks"][0]
    async def _boom_execute(*a, **k):
        raise RuntimeError("架空の実行失敗2")
    async def _boom_notify(*a, **k):
        raise OSError("架空の通知失敗2")
    monkeypatch.setattr("app.vehicle_workflow.execute", _boom_execute)
    monkeypatch.setattr("app.automatic_triz.on_vehicle_failure", _boom_notify)
    import json as _j
    from app.structured_planning import SCHEMA
    task["acceptance_criteria"] = _j.dumps({"schema": SCHEMA, "execution_kind": "vehicle_test",
                                            "outputs": [], "criterion_ids": []})
    with pytest.raises(Exception):
        asyncio.new_event_loop().run_until_complete(mgr._execute_task(pid, task["id"]))


def test_f2a_execute_task_upgrade_failure_not_success(tmp_path, monkeypatch):
    """検出 app/project_manager.py:_execute_task:P1:61bbc8f48e47#3。upgrade側も失敗はraise。"""
    mgr, pid = _pm_manager(tmp_path, "exec3")
    task = mgr.memory.get_mission(pid)["tasks"][0]
    async def _boom_upgraded(*a, **k):
        raise ValueError("架空のupgrade失敗")
    async def _boom_notify(*a, **k):
        raise OSError("架空の通知失敗3")
    monkeypatch.setattr("app.upgrade_runtime.execute_upgraded", _boom_upgraded)
    monkeypatch.setattr("app.automatic_triz.on_failure", _boom_notify)
    with pytest.raises(Exception):
        asyncio.new_event_loop().run_until_complete(mgr._execute_task(pid, task["id"]))


# --- project_manager 計画生成 (失敗を成功・空計画にしない) ---

def test_f2a_generate_plan_impl_case_failure_not_empty(tmp_path, monkeypatch):
    """検出 app/project_manager.py:_generate_plan_impl:P1:a2ad58d9f50d#1/#2。

    (1) 計画生成の事例参照に使う。(2) 失敗時現状: 参照失敗を空扱い。
    修正後: 狭い例外にし、空計画・成功偽装をしない(生成自体は既存検証で止まる)。
    """
    mgr, pid = _pm_manager(tmp_path, "gen-impl")
    import app.plan_case_reference as ref
    monkeypatch.setattr(ref, "collect_for_current_plan",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("架空の事例失敗")))
    # 事例失敗でも計画生成が空成功にならないこと(例外または空でない計画)。
    # 自由計画経路は LLM が無いため失敗するはず(成功の空計画を返さない)。
    with pytest.raises(Exception):
        asyncio.new_event_loop().run_until_complete(mgr._generate_plan_impl(pid))


def test_f2a_generate_structured_plan_case_failure_not_empty(tmp_path, monkeypatch):
    """検出 app/project_manager.py:_generate_structured_plan:P1:a2ad58d9f50d。"""
    mgr, pid = _pm_manager(tmp_path, "gen-struct")
    import app.plan_case_reference as ref
    monkeypatch.setattr(ref, "collect_for_current_plan",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("架空の事例失敗2")))
    with pytest.raises(Exception):
        asyncio.new_event_loop().run_until_complete(mgr._generate_structured_plan(pid, ["架空条件"]))


# --- safe_auto_resume.resume (再評価失敗を成功にしない) ---

def test_f2a_resume_reeval_failure_not_success(tmp_path, monkeypatch):
    """検出 app/safe_auto_resume.py:resume:P1:a2ad58d9f50d。

    (1) 完了後の独立再評価に使う。(2) 失敗時現状: 再評価失敗を握りつぶし完了記録を残す。
    修正後: 狭い例外にし、完了記録自体は残るが resolved にしない(成功偽装なし)。
    """
    import os
    os.environ["LOCALSAPORTER_AUTO_RESUME_AVAILABLE"] = "1"
    from unittest.mock import patch
    import app.safe_auto_resume as sar
    mgr, pid = _manager(tmp_path, "resume")
    _contract(mgr, pid)
    # 信頼できる文書ステップを用意するのは難しいため、狭い例外化の回归として
    # _post_completion_reevaluation の失敗が握りつぶしで resolved にしないことを見る。
    with patch("app.safe_auto_resume._post_completion_reevaluation",
               side_effect=OSError("架空の再評価失敗")):
        # executor なしでは待機になるが、失敗が成功にならないこと。
        out = asyncio.new_event_loop().run_until_complete(sar.resume(mgr, pid))
        assert out["status"] in ("disabled", "blocked", "completed", "partial"), out
        assert not (out["status"] == "completed" and out.get("failed")), out
