"""P1-2: 正本スナップショット保存のローカル再試行。疑似PJ・実関数・外部通信なし。

- 確定時に保存を失敗させる(ReviewStore.put を binding_snapshot のときだけ OSError
  にする)と save_failed になる。保存を直した後に retry_binding_snapshot_save を
  呼ぶと、正本が保存され(check_binding_snapshot_current が通り、confirmed に
  当該指摘が入る)、binding_snapshot_save_failed=False、行は confirmed、retried=True。
- 再試行が外部通信・外部AIの再送をしない(外部送信関数を差し替えて呼ばれたら失敗
  する形にして、呼出0件を確認)。external_calls が増えない。
- 保存がまだ失敗するときは、save_failed のまま・エラーコードが残る・
  snapshot_retry_count が増える・確定済みに見せない。1回の呼出で保存の試行は
  1回だけ(put の呼出数で確認)。
- 対象が無い(失敗していない)run に呼ぶと ValueError。
- 計画が変わった(署名変更)後の再試行は保存せず、古い確定を新しい正本へ入れない。
- 再試行後、P1-1の local_patch_preview.preview() が確定結合
  (binding_source="confirmed_snapshot")を使える(確定前は unbound)。
- 想定外の例外は握りつぶされず伝播する(例外タプルに含まれない型を差し替えで出して伝播を確認)。
- エンドポイント: 成功時200で retried が返る、対象なしは409。
- 修正前(再試行関数を一時的に外す、または retry_binding_snapshot_save の保存呼出を
  外す)で1が失敗することを実行して示す。示したら必ず元に戻し、戻したことを確認する。
"""
import asyncio
import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.experience_store import fingerprint


class Manager(SimpleNamespace):
    pass


@contextmanager
def _fail_binding_snapshot_put(exc=None, calls=None):
    """ReviewStore.put を binding_snapshot のときだけ失敗させる差し替え。

    クラス属性の原本関数を保存・復元する(束縛メソッドの保存・復元はしない)。
    calls を渡すと binding_snapshot への put 試行を記録する(試行回数の確認用)。
    """
    from app.goal_review import ReviewStore

    orig = ReviewStore.put
    boom = exc if exc is not None else OSError("pseudo store failure")

    def failing(self, pid, kind, signature, payload):
        if kind == "binding_snapshot":
            if calls is not None:
                calls.append((kind, signature))
            raise boom
        return orig(self, pid, kind, signature, payload)

    ReviewStore.put = failing
    try:
        yield
    finally:
        ReviewStore.put = orig


def _snapshot_tasks():
    tasks = []
    for i in range(6):
        key = "SC00" if i == 0 else f"SC{i:02d}"
        cids = [] if i == 0 else [f"SC{i:02d}"]
        tasks.append({
            "id": f"t{i}", "task_key": key, "title": key, "description": "pseudo step",
            "acceptance_criteria": json.dumps({
                "schema": "local-cowork-plan/v1", "criterion_ids": cids,
                "role": "preparation" if key == "SC00" else "execution",
                "internal_preparation_gate_ids": ["PREP00"] if key == "SC00" else [],
            }, ensure_ascii=False),
            "depends_on": [],
        })
    return tasks


def _manager(tmp_path):
    from app.memory.short_term import ShortTermMemory
    from app.workspace_files import WorkspaceSandbox

    mem = ShortTermMemory(tmp_path / "memory" / "conversations.db")
    pid = mem.create_project("p1-2-pseudo")["id"]
    mem.save_mission(pid, "pseudo goal\n1. pseudo item\n2. pseudo item two",
                     "pseudo goal\n1. pseudo item\n2. pseudo item two",
                     "", True, ["chatgpt"])
    ws = WorkspaceSandbox(tmp_path / "workspace")
    mgr = Manager(memory=mem, workspace=ws, planning_projects=set(), llm=None,
                  workers={}, _sync_memos=lambda _pid: None,
                  provider_statuses=lambda: [{"id": "chatgpt", "configured": True}],
                  plan_review_runner=None)
    live_tasks = []
    for i, row in enumerate(_snapshot_tasks()):
        task = dict(row)
        task["id"] = f"live-t{i}"
        task["description"] = "pseudo step"
        live_tasks.append(task)
    compiled_live = live_tasks[1:3]
    expected_versions = {t["task_key"] for t in compiled_live}
    mem.replace_plan(pid, "p1-2 pseudo plan", compiled_live)
    from app.goal_contract import activate as _activate

    activated = _activate(mgr, pid)
    assert expected_versions <= {c.get("criterion_id")
                                 for c in activated.get("criteria") or []}, activated
    return mgr, pid


def _parsed():
    return [{"provider": "chatgpt", "status": "fail", "issues": [
        {"step": "1", "unmet_goal": "pseudo method is thin (SC99)",
         "reason": "step one detail is thin", "remedy": "add step detail",
         "severity": "major"},
        {"step": "99", "unmet_goal": "other pseudo gap missing",
         "reason": "no such step pseudo detail", "remedy": "review pseudo target",
         "severity": "major"},
    ]}]


def _packet():
    return {"pkt": "p1-2"}


def _packet_hash(packet):
    return fingerprint(packet)


def _make_run(mgr, pid, signature, parsed, packet, goal_hash, run_id="run-p1-2"):
    """保存済みレビューから remap して対応表を作り、run を保存して返す。"""
    import app.plan_review_loop as loop
    from app.goal_review import ReviewStore

    packet_hash = _packet_hash(packet)
    ReviewStore(mgr.memory.path).put(pid, "plan", signature, {
        "status": "fail", "reviews": parsed, "packet": packet,
        "success_count": 0, "required_count": 1,
    })
    run = {
        "id": run_id, "project_id": pid, "plan_signature": signature,
        "goal_contract_hash": goal_hash, "packet_hash": packet_hash,
        "providers": ["chatgpt"], "state": "stopped", "stop_kind": "human_required",
        "rounds": [{"round": 1, "plan_signature": signature, "packet_hash": packet_hash,
                    "reviews": parsed, "review_source": "provider"}],
        "issue_bindings": [], "manual_bindings": [], "remap_history": [],
        "resume_history": [],
    }
    ReviewStore(mgr.memory.path).put(pid, "auto_loop", run["id"], run)
    remapped = asyncio.new_event_loop().run_until_complete(
        loop.remap_run(mgr, pid, run["id"], actor="human-a",
                       idempotency_key=f"remap-{run_id}"))
    assert remapped["issue_bindings"], "保存済み指摘の対応表が保存されていること"
    stored = loop.get_run(mgr, pid, run["id"])
    assert stored["issue_bindings"], "保存済み指摘の対応表が保存されていること"
    return loop, stored


def _confirm_params(loop, stored):
    first = next(b for b in stored["issue_bindings"]
                 if str(b.get("state") or "") == "ambiguous")
    return first


def test_1_retry_saves_snapshot_after_failure(tmp_path):
    """受入1: 確定時保存失敗→save_failed。保存復旧後の再試行で正本保存・confirmed・retried。"""
    import app.plan_feedback as fb
    import app.plan_review_loop as loop
    from app.goal_contract import preview as contract_preview
    from app.goal_review import ReviewStore, plan_snapshot

    mgr, pid = _manager(tmp_path)
    snap_live, signature = plan_snapshot(mgr, pid)
    contract = contract_preview(mgr, pid)
    goal_hash = str(contract.get("content_hash") or "")
    parsed = _parsed()
    packet = _packet()
    loop_mod, stored = _make_run(mgr, pid, signature, parsed, packet, goal_hash)
    assert loop_mod is loop
    first = _confirm_params(loop, stored)

    with _fail_binding_snapshot_put():
        entry = asyncio.new_event_loop().run_until_complete(loop.confirm_binding(
            mgr, pid, stored["id"], first["issue_id"], actor="human-a",
            task_key="SC01", criterion_id="SC01",
            reason="対象を確認した", confirmed_preview=True))
    assert entry.get("binding_snapshot_save_failed") is True
    failed_run = loop.get_run(mgr, pid, stored["id"])
    assert failed_run.get("binding_snapshot_save_failed") is True
    failed_row = next(b for b in failed_run["issue_bindings"]
                      if str(b.get("issue_id") or "") == first["issue_id"])
    assert failed_row["selection_status"] == "save_failed"
    assert failed_run["manual_bindings"], "人の確定そのものは run に残ること"

    retried = asyncio.new_event_loop().run_until_complete(
        loop.retry_binding_snapshot_save(mgr, pid, stored["id"]))
    assert retried.get("retried") is True
    assert retried.get("saved") is True
    assert retried.get("attempts") == 1
    ok_run = loop.get_run(mgr, pid, stored["id"])
    assert ok_run.get("binding_snapshot_save_failed") is False
    assert ok_run.get("binding_snapshot_save_error") == ""
    assert ok_run.get("binding_snapshot_save_error_code") == ""
    assert ok_run.get("binding_snapshot_save_detail") == {}
    ok_row = next(b for b in ok_run["issue_bindings"]
                  if str(b.get("issue_id") or "") == first["issue_id"])
    assert ok_row["selection_status"] == "confirmed"
    manual = next(e for e in ok_run["manual_bindings"]
                  if str(e.get("issue_id") or "") == first["issue_id"])
    assert "binding_snapshot_save_failed" not in manual
    assert "binding_snapshot_save_error" not in manual

    store = ReviewStore(mgr.memory.path)
    saved = store.get(pid, "binding_snapshot", signature)["snapshot"]
    fb.check_binding_snapshot_current(
        saved, plan_signature=signature,
        goal_contract_hash=goal_hash,
        packet_hash=_packet_hash(packet),
        review_fingerprint=fb.review_fingerprint_of(signature, parsed),
        current_reviews=parsed)
    hit = fb.confirmed_target_for(saved, first["issue_id"])
    assert isinstance(hit, dict) and hit.get("issue_id") == first["issue_id"]
    assert int(ok_run.get("snapshot_retry_count") or 0) >= 1
    assert retried.get("snapshot_retry_count") == int(ok_run.get("snapshot_retry_count") or 0)


def test_2_retry_makes_no_external_calls(tmp_path, monkeypatch):
    """受入2: 再試行が外部通信・外部AIの再送をしない。external_calls が増えない。"""
    import app.plan_review_loop as loop
    from app.goal_contract import preview as contract_preview
    from app.goal_review import ReviewStore, plan_snapshot

    mgr, pid = _manager(tmp_path)
    _snap_live, signature = plan_snapshot(mgr, pid)
    contract = contract_preview(mgr, pid)
    goal_hash = str(contract.get("content_hash") or "")
    parsed = _parsed()
    packet = _packet()
    _, stored = _make_run(mgr, pid, signature, parsed, packet, goal_hash,
                          run_id="run-p1-2-ext")
    first = _confirm_params(loop, stored)

    with _fail_binding_snapshot_put():
        asyncio.new_event_loop().run_until_complete(loop.confirm_binding(
            mgr, pid, stored["id"], first["issue_id"], actor="human-a",
            task_key="SC01", criterion_id="SC01",
            reason="対象を確認した", confirmed_preview=True))
    before = loop.get_run(mgr, pid, stored["id"])
    external_before = int(before.get("external_calls") or 0)
    rounds_before = int(before.get("verification_rounds") or 0)

    calls = []

    async def _must_not_send(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("retry must not send external review")

    monkeypatch.setattr(mgr, "plan_review_runner", _must_not_send, raising=False)
    import app.external_ai as ext

    monkeypatch.setattr(ext, "call_provider_with_metadata",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("retry must not call provider")),
                        raising=True)

    retried = asyncio.new_event_loop().run_until_complete(
        loop.retry_binding_snapshot_save(mgr, pid, stored["id"]))
    assert retried.get("saved") is True
    assert calls == []
    after = loop.get_run(mgr, pid, stored["id"])
    assert int(after.get("external_calls") or 0) == external_before
    assert int(after.get("verification_rounds") or 0) == rounds_before


def test_3_failure_keeps_save_failed_and_counts_retry_once(tmp_path):
    """受入3: 保存がまだ失敗→save_failed のまま・コード残存・回数加算・確定済みに見せない。1回だけ試行。"""
    import app.plan_review_loop as loop
    from app.goal_contract import preview as contract_preview
    from app.goal_review import ReviewStore, plan_snapshot

    mgr, pid = _manager(tmp_path)
    _snap_live, signature = plan_snapshot(mgr, pid)
    contract = contract_preview(mgr, pid)
    goal_hash = str(contract.get("content_hash") or "")
    parsed = _parsed()
    packet = _packet()
    _, stored = _make_run(mgr, pid, signature, parsed, packet, goal_hash,
                          run_id="run-p1-2-fail")
    first = _confirm_params(loop, stored)

    put_calls: list = []
    with _fail_binding_snapshot_put(calls=put_calls):
        asyncio.new_event_loop().run_until_complete(loop.confirm_binding(
            mgr, pid, stored["id"], first["issue_id"], actor="human-a",
            task_key="SC01", criterion_id="SC01",
            reason="対象を確認した", confirmed_preview=True))
        before_count = int(loop.get_run(mgr, pid, stored["id"]).get("snapshot_retry_count") or 0)
        put_calls.clear()
        retried = asyncio.new_event_loop().run_until_complete(
            loop.retry_binding_snapshot_save(mgr, pid, stored["id"]))
    assert retried.get("retried") is True
    assert retried.get("saved") is False
    assert retried.get("attempts") == 1
    assert len(put_calls) == 1, "1回の呼出で保存の試行は1回だけ"
    again = loop.get_run(mgr, pid, stored["id"])
    assert again.get("binding_snapshot_save_failed") is True
    assert str(again.get("binding_snapshot_save_error_code") or "") != ""
    assert int(again.get("snapshot_retry_count") or 0) == before_count + 1
    row = next(b for b in again["issue_bindings"]
               if str(b.get("issue_id") or "") == first["issue_id"])
    # 確定済みに見せない: save_failed のまま。
    assert row["selection_status"] == "save_failed"


def test_4_no_target_raises(tmp_path):
    """受入4: 対象が無い(失敗していない)run に呼ぶと ValueError。"""
    import app.plan_review_loop as loop
    from app.goal_contract import preview as contract_preview
    from app.goal_review import plan_snapshot

    mgr, pid = _manager(tmp_path)
    _snap_live, signature = plan_snapshot(mgr, pid)
    contract = contract_preview(mgr, pid)
    goal_hash = str(contract.get("content_hash") or "")
    parsed = _parsed()
    packet = _packet()
    _, stored = _make_run(mgr, pid, signature, parsed, packet, goal_hash,
                          run_id="run-p1-2-notarget")
    with pytest.raises(ValueError, match="再試行の対象がありません"):
        asyncio.new_event_loop().run_until_complete(
            loop.retry_binding_snapshot_save(mgr, pid, stored["id"]))


def test_5_signature_change_does_not_save_old_confirmation(tmp_path):
    """受入5: 計画が変わった(署名変更)後の再試行は保存せず、古い確定を新しい正本へ入れない。"""
    import app.plan_feedback as fb
    import app.plan_review_loop as loop
    from app.goal_contract import preview as contract_preview
    from app.goal_review import ReviewStore, plan_snapshot

    mgr, pid = _manager(tmp_path)
    _snap_live, signature = plan_snapshot(mgr, pid)
    contract = contract_preview(mgr, pid)
    goal_hash = str(contract.get("content_hash") or "")
    parsed = _parsed()
    packet = _packet()
    _, stored = _make_run(mgr, pid, signature, parsed, packet, goal_hash,
                          run_id="run-p1-2-stale")
    first = _confirm_params(loop, stored)
    store = ReviewStore(mgr.memory.path)

    with _fail_binding_snapshot_put():
        asyncio.new_event_loop().run_until_complete(loop.confirm_binding(
            mgr, pid, stored["id"], first["issue_id"], actor="human-a",
            task_key="SC01", criterion_id="SC01",
            reason="対象を確認した", confirmed_preview=True))

    # 現行計画の署名を変える(別の工程構成で置換する)。
    other_tasks = []
    for i, row in enumerate(_snapshot_tasks()):
        task = dict(row)
        task["id"] = f"other-t{i}"
        task["title"] = f"other-{i}"
        other_tasks.append(task)
    mgr.memory.replace_plan(pid, "p1-2 changed plan", other_tasks[1:4])
    with pytest.raises(ValueError, match="409"):
        asyncio.new_event_loop().run_until_complete(
            loop.retry_binding_snapshot_save(mgr, pid, stored["id"]))
    # 古い確定を新しい正本へ入れない: 新署名の正本に当該確定が入っていないこと。
    _snap_new, new_signature = plan_snapshot(mgr, pid)
    assert new_signature != signature
    assert store.get(pid, "binding_snapshot", new_signature) in (None, {})
    saved_old = store.get(pid, "binding_snapshot", signature)
    assert saved_old in (None, {}) or not any(
        str(c.get("issue_id") or "") == first["issue_id"]
        for c in ((saved_old.get("snapshot") or {}).get("confirmed") or []))


def test_6_retry_enables_confirmed_snapshot_preview(tmp_path):
    """受入6: 再試行後、local_patch_preview が確定結合を使える(確定前は unbound)。"""
    import app.local_patch_preview as preview_module
    import app.plan_review_loop as loop
    from app.goal_contract import preview as contract_preview
    from app.goal_review import ReviewStore, plan_snapshot
    from app.plan_feedback import issues_for

    mgr, pid = _manager(tmp_path)
    _snap_live, signature = plan_snapshot(mgr, pid)
    contract = contract_preview(mgr, pid)
    goal_hash = str(contract.get("content_hash") or "")
    parsed = _parsed()
    packet = _packet()
    _, stored = _make_run(mgr, pid, signature, parsed, packet, goal_hash,
                          run_id="run-p1-2-preview")
    first = _confirm_params(loop, stored)
    issues = issues_for(mgr, pid, signature)

    before = preview_module.preview(mgr, pid, issues=issues)
    target_before = next(c for c in before["candidates"]
                         if c["issue_id"] == first["issue_id"])
    assert target_before["bound"] is False
    assert target_before["binding_source"] == "unbound"

    with _fail_binding_snapshot_put():
        asyncio.new_event_loop().run_until_complete(loop.confirm_binding(
            mgr, pid, stored["id"], first["issue_id"], actor="human-a",
            task_key="SC01", criterion_id="SC01",
            reason="対象を確認した", confirmed_preview=True))
    retried = asyncio.new_event_loop().run_until_complete(
        loop.retry_binding_snapshot_save(mgr, pid, stored["id"]))
    assert retried.get("saved") is True
    after = preview_module.preview(mgr, pid, issues=issues)
    target = next(c for c in after["candidates"] if c["issue_id"] == first["issue_id"])
    assert target["bound"] is True
    assert target["binding_source"] == "confirmed_snapshot"
    assert target["criterion_id"] == "SC01"
    assert target["task_key"] == "SC01"


def test_7_unexpected_error_propagates(tmp_path):
    """受入7: 想定外の例外は握りつぶされず伝播する。"""
    import app.plan_review_loop as loop
    from app.goal_contract import preview as contract_preview
    from app.goal_review import ReviewStore, plan_snapshot

    mgr, pid = _manager(tmp_path)
    _snap_live, signature = plan_snapshot(mgr, pid)
    contract = contract_preview(mgr, pid)
    goal_hash = str(contract.get("content_hash") or "")
    parsed = _parsed()
    packet = _packet()
    _, stored = _make_run(mgr, pid, signature, parsed, packet, goal_hash,
                          run_id="run-p1-2-unexpected")
    first = _confirm_params(loop, stored)

    with _fail_binding_snapshot_put():
        asyncio.new_event_loop().run_until_complete(loop.confirm_binding(
            mgr, pid, stored["id"], first["issue_id"], actor="human-a",
            task_key="SC01", criterion_id="SC01",
            reason="対象を確認した", confirmed_preview=True))

    class _UnexpectedBoom(Exception):
        pass

    with _fail_binding_snapshot_put(exc=_UnexpectedBoom("unexpected pseudo boom")):
        with pytest.raises(_UnexpectedBoom):
            asyncio.new_event_loop().run_until_complete(
                loop.retry_binding_snapshot_save(mgr, pid, stored["id"]))


def _web_client(tmp_path):
    import app.web as web

    memory_holder = {}
    real_memory = web.memory
    real_orchestrator = web.orchestrator

    from app.memory.short_term import ShortTermMemory

    mem = ShortTermMemory(tmp_path / "webmem" / "conversations.db")
    pid = mem.create_project("p1-2-web")["id"]
    # web.require_project は memory.get_project を使う。
    web.memory = mem
    mgr = SimpleNamespace(memory=mem)
    web.orchestrator = mgr
    memory_holder["real_memory"] = real_memory
    memory_holder["real_orchestrator"] = real_orchestrator
    client = TestClient(web.app)
    return web, mem, mgr, pid, client, memory_holder


def test_8_endpoint_success_and_no_target(tmp_path):
    """受入8: エンドポイント成功時200で retried、対象なしは409。payload不要。"""
    import app.plan_review_loop as loop
    import app.web as web
    from app.goal_contract import preview as contract_preview
    from app.goal_review import ReviewStore, plan_snapshot

    w, mem, mgr, _web_pid, client, holder = _web_client(tmp_path)
    try:
        from app.memory.short_term import ShortTermMemory  # noqa: F401 (参照用)
        from app.workspace_files import WorkspaceSandbox

        mem.save_mission(mem.list_projects()[0]["id"] if False else _web_pid,
                         "pseudo goal\n1. pseudo item\n2. pseudo item two",
                         "pseudo goal\n1. pseudo item\n2. pseudo item two",
                         "", True, ["chatgpt"])
        ws = WorkspaceSandbox(tmp_path / "webws")
        full = Manager(memory=mem, workspace=ws, planning_projects=set(), llm=None,
                       workers={}, _sync_memos=lambda _pid: None,
                       provider_statuses=lambda: [{"id": "chatgpt", "configured": True}],
                       plan_review_runner=None)
        live_tasks = []
        for i, row in enumerate(_snapshot_tasks()):
            task = dict(row)
            task["id"] = f"live-t{i}"
            task["description"] = "pseudo step"
            live_tasks.append(task)
        mem.replace_plan(_web_pid, "p1-2 web plan", live_tasks[1:3])
        from app.goal_contract import activate as _activate

        _activate(full, _web_pid)
        _snap_live, signature = plan_snapshot(full, _web_pid)
        contract = contract_preview(full, _web_pid)
        goal_hash = str(contract.get("content_hash") or "")
        parsed = _parsed()
        packet = _packet()
        packet_hash = _packet_hash(packet)
        ReviewStore(mem.path).put(_web_pid, "plan", signature, {
            "status": "fail", "reviews": parsed, "packet": packet,
            "success_count": 0, "required_count": 1,
        })
        run = {
            "id": "run-web-1", "project_id": _web_pid, "plan_signature": signature,
            "goal_contract_hash": goal_hash, "packet_hash": packet_hash,
            "providers": ["chatgpt"], "state": "stopped", "stop_kind": "human_required",
            "rounds": [{"round": 1, "plan_signature": signature, "packet_hash": packet_hash,
                        "reviews": parsed, "review_source": "provider"}],
            "issue_bindings": [], "manual_bindings": [], "remap_history": [],
            "resume_history": [],
        }
        ReviewStore(mem.path).put(_web_pid, "auto_loop", run["id"], run)
        w.orchestrator = full
        remapped = asyncio.new_event_loop().run_until_complete(
            loop.remap_run(full, _web_pid, run["id"], actor="human-a",
                           idempotency_key="remap-web-1"))
        assert remapped["issue_bindings"]
        first = next(b for b in loop.get_run(full, _web_pid, run["id"])["issue_bindings"]
                     if str(b.get("state") or "") == "ambiguous")
        with _fail_binding_snapshot_put():
            asyncio.new_event_loop().run_until_complete(loop.confirm_binding(
                full, _web_pid, run["id"], first["issue_id"], actor="human-a",
                task_key="SC01", criterion_id="SC01",
                reason="対象を確認した", confirmed_preview=True))

        url = (f"/api/projects/{_web_pid}/goal-review/auto-loop/"
               f"{run['id']}/binding-snapshot-retry")
        ok = client.post(url)
        assert ok.status_code == 200, ok.text
        body = ok.json()
        assert body.get("retried") is True
        assert body.get("saved") is True
        assert body.get("snapshot_retry_count", 0) >= 1

        # 対象なし(2回目は失敗状態が解消済み)は409。
        again = client.post(url)
        assert again.status_code == 409
    finally:
        web.memory = holder["real_memory"]
        web.orchestrator = holder["real_orchestrator"]


def test_9_without_retry_save_does_not_recover(tmp_path, monkeypatch):
    """受入9: 修正前(再試行の保存呼出を外す)で受入1が失敗することを実行して示す。

    retry_binding_snapshot_save の保存呼出(_persist_confirmed_snapshot)を外した
    状態では、正本が保存されず saved=True にならないことを実行で示す。
    示したら必ず元に戻し、戻したことを確認する。
    """
    import app.plan_review_loop as loop
    from app.goal_contract import preview as contract_preview
    from app.goal_review import ReviewStore, plan_snapshot

    mgr, pid = _manager(tmp_path)
    _snap_live, signature = plan_snapshot(mgr, pid)
    contract = contract_preview(mgr, pid)
    goal_hash = str(contract.get("content_hash") or "")
    parsed = _parsed()
    packet = _packet()
    _, stored = _make_run(mgr, pid, signature, parsed, packet, goal_hash,
                          run_id="run-p1-2-prefix")
    first = _confirm_params(loop, stored)

    with _fail_binding_snapshot_put():
        asyncio.new_event_loop().run_until_complete(loop.confirm_binding(
            mgr, pid, stored["id"], first["issue_id"], actor="human-a",
            task_key="SC01", criterion_id="SC01",
            reason="対象を確認した", confirmed_preview=True))

    # 修正前: 保存呼出を外す(何も保存しない)状態で再試行しても正本は復旧しない。
    async def _no_save(*args, **kwargs):
        return {"saved": True}

    monkeypatch.setattr(loop, "_persist_confirmed_snapshot",
                        lambda *a, **k: {"saved": False, "error": "", "code": "", "detail": {}})
    broken = asyncio.new_event_loop().run_until_complete(
        loop.retry_binding_snapshot_save(mgr, pid, stored["id"]))
    assert broken.get("saved") is False, "保存呼出を外すと復旧しない(修正前の失敗を再現)"
    assert loop.get_run(mgr, pid, stored["id"]).get("binding_snapshot_save_failed") is True

    # 必ず元に戻す: monkeypatch を外した後は実関数が復旧すること。
    monkeypatch.undo()
    assert loop._persist_confirmed_snapshot is not None
    assert loop._persist_confirmed_snapshot.__name__ == "_persist_confirmed_snapshot"
    restored = asyncio.new_event_loop().run_until_complete(
        loop.retry_binding_snapshot_save(mgr, pid, stored["id"]))
    assert restored.get("saved") is True
    assert restored.get("retried") is True
    ok_run = loop.get_run(mgr, pid, stored["id"])
    assert ok_run.get("binding_snapshot_save_failed") is False
    assert loop._persist_confirmed_snapshot is not None, "戻したことを確認"
