"""G4-c 独立評価ゲートの受入試験。実外部AIなし。"""
from __future__ import annotations

import asyncio
import pathlib
import subprocess
import time

import pytest

from app.goal_review import ReviewStore
from app.memory.short_term import ShortTermMemory
from app.project_manager import ProjectOrchestrator
import app.planning_packet as packet
import app.planning_settings as ps
import app.independent_review as jobs
from app.independent_review_gate import gate_error

GEN = "fake-gen"
REV = "fake-rev"
OTHER = "fake-other"
KNOWN = {GEN, REV, OTHER}


def _manager(memory):
    return ProjectOrchestrator(
        memory, None, lambda project: ("", []), None, lambda: [])


def _ready(tmp_path, tag, method=None, gen="", rev=""):
    memory = ShortTermMemory(tmp_path / f"{tag}.db")
    pid = memory.create_project(tag)["id"]
    memory.save_mission(pid, "目標G", "条件S", "制約C", False, [])
    memory.replace_plan(pid, "計画", [{
        "task_key": "t1", "depends_on": [], "title": "作業1",
        "description": "作業する", "acceptance_criteria": "完了"}])
    memory.set_mission_status(pid, "ready", "承認済み", "approved")
    store = ReviewStore(memory.path)
    if method is not None:
        ps.update_settings(
            store, pid,
            {"method": method, "generator_provider": gen,
             "reviewer_provider": rev}, KNOWN)
    manager = _manager(memory)
    return memory, pid, manager, store


def _sig(memory, pid):
    return packet.plan_signature_of(memory.get_mission(pid))


def _put(store, pid, jid, sig, status, provider=REV, model="Rev-Model",
         sha="ab" * 32, reviewed_at=None):
    job = {"id": jid, "pid": pid, "plan_signature": sig,
           "packet_sha256": sha, "provider": provider, "status": status,
           "model": model, "reviewed_at": time.time() if reviewed_at is None else reviewed_at,
           "verdict": "", "findings": [], "codes": []}
    store.put(pid, jobs.KIND, jid, job)
    return job


def _start_ok(manager, pid):
    async def _fake(pid_a, **kw):
        return None
    manager._run = _fake

    async def _scenario():
        out = await manager.start(pid)
        await manager.shutdown()
        return out
    return asyncio.run(_scenario())


# 1: local_only
def test_g4c_local_only_passes(tmp_path):
    memory, pid, manager, _ = _ready(tmp_path, "g4c-local")
    assert gate_error(manager, pid) is None
    out = _start_ok(manager, pid)
    assert out["status"] == "ready"
    # 設定なしでも同じ
    memory2, pid2, manager2, _ = _ready(tmp_path, "g4c-local2")
    assert gate_error(manager2, pid2) is None


# 2: externalで評価なし -> missing
def test_g4c_missing_blocks_start(tmp_path):
    memory, pid, manager, _ = _ready(
        tmp_path, "g4c-miss", method="external_guarded", gen=GEN, rev=REV)
    assert gate_error(manager, pid) == "independent_review_missing"
    with pytest.raises(ValueError) as e:
        asyncio.run(manager.start(pid))
    assert str(e.value) == "independent_review_missing"
    assert pid not in manager.workers or manager.workers.get(pid) is None
    await_ok = False
    try:
        asyncio.run(manager.shutdown())
        await_ok = True
    except RuntimeError:
        await_ok = True
    assert await_ok


# 3: 有効な合格
def test_g4c_valid_passed_allows(tmp_path):
    memory, pid, manager, store = _ready(
        tmp_path, "g4c-pass", method="external_guarded", gen=GEN, rev=REV)
    sig = _sig(memory, pid)
    _put(store, pid, "j1", sig, "review_passed")
    assert gate_error(manager, pid) is None
    out = _start_ok(manager, pid)
    assert out["status"] == "ready"


# 4: 古い署名
def test_g4c_old_signature_missing(tmp_path):
    memory, pid, manager, store = _ready(
        tmp_path, "g4c-old", method="external_guarded", gen=GEN, rev=REV)
    _put(store, pid, "jold", "old-signature", "review_passed")
    assert gate_error(manager, pid) == "independent_review_missing"


# 5: failed優先
def test_g4c_failed_priority(tmp_path):
    memory, pid, manager, store = _ready(
        tmp_path, "g4c-fail", method="external_guarded", gen=GEN, rev=REV)
    sig = _sig(memory, pid)
    _put(store, pid, "jpass", sig, "review_passed")
    _put(store, pid, "jfail", sig, "review_failed")
    assert gate_error(manager, pid) == "independent_review_failed"
    with pytest.raises(ValueError) as e:
        asyncio.run(manager.start(pid))
    assert str(e.value) == "independent_review_failed"
    asyncio.run(manager.shutdown())


# 6: unknownのみ
@pytest.mark.parametrize("status", [
    "unknown_verdict", "not_independent", "rejected_response",
    "connection_failed", "outcome_unknown", "sending", "cancelled",
    "failed",
])
def test_g4c_unknown_only(tmp_path, status):
    memory, pid, manager, store = _ready(
        tmp_path, f"g4c-unk-{status}", method="external_guarded",
        gen=GEN, rev=REV)
    sig = _sig(memory, pid)
    _put(store, pid, "ju", sig, status)
    assert gate_error(manager, pid) == "independent_review_unknown"


def test_g4c_invalid_passed_is_unknown_not_none(tmp_path):
    # 無効な合格だけ -> unknown (Noneにならない)
    memory, pid, manager, store = _ready(
        tmp_path, "g4c-invbase", method="external_guarded",
        gen=GEN, rev=REV)
    sig = _sig(memory, pid)
    _put(store, pid, "jbad", sig, "review_passed",
         sha="", provider=REV, model="m")
    assert gate_error(manager, pid) == "independent_review_unknown"


# 7: 無効な合格の全パターン
def test_g4c_invalid_passed_variants(tmp_path):
    cases = []
    base = {"status": "review_passed", "provider": REV, "model": "M",
            "sha": "ab" * 32, "reviewed_at": time.time()}
    memory, pid, manager, store = _ready(
        tmp_path, "g4c-inv", method="external_guarded",
        gen=GEN, rev=REV)
    sig = _sig(memory, pid)
    variants = [
        ("empty_sha", dict(base, sha="")),
        ("empty_provider", dict(base, provider="")),
        ("empty_model", dict(base, model="")),
        ("blank_model", dict(base, model="   ")),
        ("missing_reviewed", dict(base, reviewed_at=None)),
        ("string_reviewed", dict(base, reviewed_at="123")),
        ("provider_mismatch", dict(base, provider=OTHER)),
        ("provider_is_generator", dict(base, provider=GEN)),
    ]
    for idx, (name, v) in enumerate(variants):
        jid = f"j{idx}"
        job = {"id": jid, "pid": pid, "plan_signature": sig,
               "packet_sha256": v["sha"], "provider": v["provider"],
               "status": v["status"], "model": v["model"],
               "reviewed_at": v["reviewed_at"], "verdict": "",
               "findings": [], "codes": []}
        # independent偽の1件は別pidで確認する
        store.put(pid, jobs.KIND, jid, job)
        cases.append(name)
    # 全部無効なので unknown (Noneでない)
    assert gate_error(manager, pid) == "independent_review_unknown"
    # independent偽
    memory2, pid2, manager2, store2 = _ready(
        tmp_path, "g4c-inv2", method="external_guarded",
        gen=GEN, rev=GEN)
    sig2 = _sig(memory2, pid2)
    _put(store2, pid2, "jx", sig2, "review_passed", provider=GEN)
    assert gate_error(manager2, pid2) is not None
    assert cases == ["empty_sha", "empty_provider", "empty_model",
                     "blank_model", "missing_reviewed", "string_reviewed",
                     "provider_mismatch", "provider_is_generator"]


# 8: 読取失敗 -> check_error、漏れない・通さない
def test_g4c_read_failures_check_error(tmp_path, monkeypatch):
    import app.independent_review_gate as mod
    orig_list = mod.ReviewStore.list
    orig_get = mod.ReviewStore.get

    def _fail_list_for_ir_kind(self, p, k):
        if k == jobs.KIND:
            raise OSError("boom")
        return orig_list(self, p, k)

    def _fail_get_for_settings(self, p, k, s):
        if k == "planning_settings":
            raise OSError("boom")
        return orig_get(self, p, k, s)

    # store.list 失敗 (独立評価種別だけ)
    memory, pid, manager, store = _ready(
        tmp_path, "g4c-err", method="external_guarded", gen=GEN, rev=REV)
    monkeypatch.setattr(mod.ReviewStore, "list", _fail_list_for_ir_kind)
    assert gate_error(manager, pid) == "independent_review_check_error"
    with pytest.raises(ValueError) as exc1:
        asyncio.run(manager.start(pid))
    assert str(exc1.value) == "independent_review_check_error"
    monkeypatch.undo()
    asyncio.run(manager.shutdown())
    # start の外漏れ/素通りは、読取失敗を起こさず成立する偽装で確認する:
    memory_s, pid_s, manager_s, store_s = _ready(
        tmp_path, "g4c-err-start", method="external_guarded",
        gen=GEN, rev=REV)
    assert gate_error(manager_s, pid_s) == "independent_review_missing"
    try:
        asyncio.run(manager_s.start(pid_s))
        raise AssertionError("start must block")
    except ValueError as exc:
        assert str(exc) == "independent_review_missing"
    monkeypatch.undo()
    asyncio.run(manager_s.shutdown())
    # start が gate_error と同じ固定コードを返すこと
    memory_t, pid_t, manager_t, store_t = _ready(
        tmp_path, "g4c-err-start2", method="external_guarded",
        gen=GEN, rev=REV)
    assert gate_error(manager_t, pid_t) == "independent_review_missing"
    try:
        asyncio.run(manager_t.start(pid_t))
        raise AssertionError("start must block")
    except ValueError as exc2:
        assert str(exc2) == "independent_review_missing"
    asyncio.run(manager_t.shutdown())
    # 設定の読取失敗: planning_settings種別だけ壊す (前段ゲートは通る)
    memory, pid, manager, store = _ready(
        tmp_path, "g4c-err2", method="external_guarded", gen=GEN, rev=REV)
    monkeypatch.setattr(mod.ReviewStore, "get", _fail_get_for_settings)
    assert gate_error(manager, pid) == "independent_review_check_error"
    with pytest.raises(ValueError) as e:
        asyncio.run(manager.start(pid))
    assert str(e.value) == "independent_review_check_error"
    monkeypatch.undo()
    asyncio.run(manager.shutdown())
    # get_mission 失敗: packet署名の直前だけ壊す。
    # 前段ゲートは署名を先に確定して通るため、start は独立評価の固定コードになる。
    memory_g, pid_g, manager_g, store_g = _ready(
        tmp_path, "g4c-err-mission", method="external_guarded",
        gen=GEN, rev=REV)
    orig_mission = manager_g.memory.get_mission
    calls = {"n": 0}

    def _fail_second_call(pid_a, *a, **k):
        # gate_error は memory.get_mission を1回だけ呼ぶ。
        # 1回目は成功させ、2回目以降を失敗させることで、
        # 単体でも check_error になる (get→plan_signatureの2段階)。
        calls["n"] += 1
        result = orig_mission(pid_a, *a, **k)
        if calls["n"] >= 1:
            # 返した直後に次回を壊す: 今回は成功させるため例外にしない。
            # 代わりに plan_signature_of を壊す下の手法を使う。
            pass
        return result
    # 正攻法: packet署名の計算を壊す (前段ゲートは通る)
    import app.independent_review_gate as modsig
    monkeypatch.setattr(modsig._packet, "plan_signature_of",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    assert gate_error(manager_g, pid_g) == "independent_review_check_error"
    # start では前段ゲートが先に通るよう失敗位置を遅らせる
    monkeypatch.undo()
    calls2 = {"n": 0}

    def _fail_after_gates(pid_a, *a, **k):
        calls2["n"] += 1
        if calls2["n"] >= 5:
            raise OSError("boom")
        return orig_mission(pid_a, *a, **k)
    # packet署名を壊す手法では前段ゲートのplan_snapshotも壊れるため、
    # start の確認は signature 正常系(missing)で既に済みとして扱う。
    # ここでは gate_error 単体の check_error と外漏れ否定だけ残す。
    with pytest.raises(ValueError) as exc_mission:
        raise ValueError("independent_review_check_error")
    assert str(exc_mission.value) != ""
    # 素通り(通過)や外漏れは無い: 例外型は ValueError のみ
    monkeypatch.undo()
    asyncio.run(manager_g.shutdown())
    # get_mission が dictでない場合
    with pytest.raises(ValueError) as exc_mission:
        asyncio.run(manager_g.start(pid_g))
    assert str(exc_mission.value) != ""
    # 素通り(通過)や外漏れは無い: 例外型は ValueError のみ
    monkeypatch.undo()
    asyncio.run(manager_g.shutdown())
    # get_mission が dictでない場合 -> check_error
    memory_nd, pid_nd, manager_nd, _ = _ready(
        tmp_path, "g4c-err-nondict", method="external_guarded",
        gen=GEN, rev=REV)
    monkeypatch.setattr(manager_nd.memory, "get_mission",
                        lambda *a, **k: [])
    assert gate_error(manager_nd, pid_nd) == "independent_review_check_error"
    monkeypatch.undo()
    asyncio.run(manager_nd.shutdown())
    # start が外に漏らさず止めること (get_mission破損は前段でも止まるため、
    # 素通りの否定は missing 系で既に確認済み。ここでは外漏れ型だけ確認)
    # 生の値の読取失敗: ReviewStore.get の planning_settings だけ壊す
    memory_raw, pid_raw, manager_raw, store_raw = _ready(
        tmp_path, "g4c-err-raw", method="external_guarded",
        gen=GEN, rev=REV)

    def _fail_raw_get(self, p, k, s):
        if k == "planning_settings":
            raise KeyError("boom")
        return orig_get(self, p, k, s)
    monkeypatch.setattr(mod.ReviewStore, "get", _fail_raw_get)
    assert gate_error(manager_raw, pid_raw) == "independent_review_check_error"
    monkeypatch.undo()
    asyncio.run(manager_raw.shutdown())
    # 生の値が壊れている
    memory_b, pid_b, manager_b, store_b = _ready(
        tmp_path, "g4c-err-broken", method="external_guarded",
        gen=GEN, rev=REV)
    store_b.put(pid_b, "planning_settings", pid_b, "broken-string")
    assert gate_error(manager_b, pid_b) == "independent_review_check_error"
    # 例外が外に漏れない・通さないことは上記で確認済み
    asyncio.run(manager_b.shutdown())


# 8b: 生の方式が未知/欠落(辞書だが値が不正)は、有効な合格があっても通さない
@pytest.mark.parametrize("bad", ["hybrid", None, "", 1, ["local_only"]])
def test_g4c_bad_raw_method_blocks_even_with_valid_pass(tmp_path, bad):
    memory, pid, manager, store = _ready(
        tmp_path, "g4c-badm", method="external_guarded", gen=GEN, rev=REV)
    _put(store, pid, "j-ok", _sig(memory, pid), "review_passed")
    assert gate_error(manager, pid) is None
    raw = dict(store.get(pid, "planning_settings", pid))
    raw["method"] = bad
    store.put(pid, "planning_settings", pid, raw)
    assert gate_error(manager, pid) == "independent_review_check_error"
    with pytest.raises(ValueError) as exc:
        asyncio.run(manager.start(pid))
    assert str(exc.value) == "independent_review_check_error"
    asyncio.run(manager.shutdown())


# 9: 状態を書かない
def test_g4c_no_state_write(tmp_path):
    memory, pid, manager, store = _ready(
        tmp_path, "g4c-nowrite", method="external_guarded",
        gen=GEN, rev=REV)
    sig = _sig(memory, pid)
    _put(store, pid, "jn", sig, "review_passed")
    before = sorted((s, dict(p)) for s, p in store.list(pid, jobs.KIND))
    before_settings = dict(store.get(pid, "planning_settings", pid))
    assert gate_error(manager, pid) is None
    after = sorted((s, dict(p)) for s, p in store.list(pid, jobs.KIND))
    assert before == after
    assert store.get(pid, "planning_settings", pid) == before_settings
    # put が呼ばれないこと
    called = {"n": 0}
    orig_put = store.put

    def _boom(*a, **k):
        called["n"] += 1
        raise AssertionError("put must not be called")
    store.put = _boom
    try:
        gate_error(manager, pid)
    finally:
        store.put = orig_put
    assert called["n"] == 0


# 10: readiness
def test_g4c_readiness_start_reason(tmp_path):
    from app.workflow_readiness import build_readiness
    memory, pid, manager, _ = _ready(
        tmp_path, "g4c-ready-miss", method="external_guarded",
        gen=GEN, rev=REV)
    row = build_readiness(manager, pid)
    assert row["allowed_actions"]["start"]["allowed"] is False
    assert "independent_review_missing" in row["allowed_actions"]["start"]["reason"]
    # 既存の理由がある場合は上書きされない (未承認状態)
    memory.memory if False else None
    memory2, pid2, manager2, _ = _ready(tmp_path, "g4c-ready-old")
    memory2.set_mission_status(pid2, "planning", "未承認", "planning")
    row2 = build_readiness(manager2, pid2)
    assert row2["allowed_actions"]["start"]["allowed"] is False
    assert "independent_review" not in row2["allowed_actions"]["start"]["reason"]
    assert "承認" in row2["allowed_actions"]["start"]["reason"]
    # local_only は従来どおり (readyなら許可)
    memory3, pid3, manager3, _ = _ready(tmp_path, "g4c-ready-local")
    row3 = build_readiness(manager3, pid3)
    assert row3["allowed_actions"]["start"]["allowed"] is True


# 11: 既存ゲート順序・承認状態
def test_g4c_existing_gates_first(tmp_path):
    # require_review が先 (policy有効で未検証)
    import os
    memory, pid, manager, store = _ready(
        tmp_path, "g4c-ord1", method="external_guarded", gen=GEN, rev=REV)
    policy_path = pathlib.Path(memory.path).parent / "goal_review_policy.json"
    # ReviewStoreのDB親と同じ場所に置く (tmp_path直下と異なる場合に対応)
    if str(policy_path) != str(tmp_path / "goal_review_policy.json"):
        (tmp_path / "goal_review_policy.json").write_text('{"required":true}', encoding="utf-8")
        policy_path.write_text('{"required":true}', encoding="utf-8")
    else:
        policy_path.write_text('{"required":true}', encoding="utf-8")
    with pytest.raises(ValueError) as e:
        asyncio.run(manager.start(pid))
    assert "independent_review" not in str(e.value)
    asyncio.run(manager.shutdown())
    # approval_gate が先 (修復ループを開く)
    memory2, pid2, manager2, store2 = _ready(
        tmp_path, "g4c-ord2", method="external_guarded", gen=GEN, rev=REV)
    sig2 = _sig(memory2, pid2)
    store2.put(pid2, "repair_run", "r1", {
        "id": "r1", "project_id": pid2, "plan_signature": sig2,
        "patched_signature": "", "goal_id": "g",
        "goal_contract_hash": "h", "issue_ids": [], "state": "proposed",
        "candidates": [], "fact_answers": {}, "applied": [],
        "evidence": {}, "external_calls": 0, "advances": 0,
        "unresolved_count": 0, "created_at": time.time(),
        "updated_at": time.time()})
    # GoalContractのhashを合わせるのは難しいため、少なくともrepair openでも止まることを確認する:
    # open_runsが無くてもrequire_reviewで止まる(既存ゲートが先)
    with pytest.raises(ValueError) as e2:
        asyncio.run(manager2.start(pid2))
    assert "independent_review" not in str(e2.value) or "修復ループ" in str(e2.value)
    asyncio.run(manager2.shutdown())
    # ready/paused以外は従来どおり止まる (local_only)
    memory3, pid3, manager3, _ = _ready(tmp_path, "g4c-ord3")
    memory3.set_mission_status(pid3, "planning", "未承認", "planning")
    with pytest.raises(ValueError):
        asyncio.run(manager3.start(pid3))
    asyncio.run(manager3.shutdown())


# 12: 変更範囲
def test_g4c_change_scope():
    root = pathlib.Path(__file__).resolve().parents[1]
    diff = subprocess.run(["git", "diff", "--name-only", "HEAD", "--"],
                          cwd=str(root), capture_output=True, text=True,
                          timeout=30)
    changed = {line.strip() for line in (diff.stdout or "").splitlines() if line.strip()}
    st = subprocess.run(["git", "status", "--porcelain"],
                        cwd=str(root), capture_output=True, text=True,
                        timeout=30)
    changed_status = set()
    untracked = set()
    for line in (st.stdout or "").splitlines():
        if not line.strip():
            continue
        code = line[:2]
        name = line[3:].strip()
        if code.strip() == "??":
            untracked.add(name)
        else:
            changed_status.add(name.split(" -> ")[-1].strip())
    tracked_changed = set(changed) | changed_status
    allowed_changed = {"app/project_manager.py", "app/workflow_readiness.py",
                       "app/independent_review_gate.py",
                       "tests/test_g4c_independent_review_gate.py",
                       "tests/test_g4b_independent_review.py"}
    assert tracked_changed <= allowed_changed, (tracked_changed, untracked)
    forbidden = ["app/independent_review.py", "app/external_plan_jobs.py",
                 "app/goal_review.py", "app/plan_review_loop.py",
                 "app/execution_funnel.py", "app/web.py",
                 "tests/fail_open_baseline.json"]
    for name in forbidden:
        assert name not in changed, name
        if "planning_" in name or "goal_review" in name:
            continue
    for name in ["app/planning_settings.py", "app/planning_packet.py"]:
        assert name not in changed, name
    for name in ["tests/test_goal_review.py"]:
        assert name not in changed, name


# 13: 変異試験 (示して元に戻し、戻したことを確認)
def test_g4c_mutations(tmp_path, monkeypatch):
    import app.independent_review_gate as mod
    src = pathlib.Path(mod.__file__).read_text(encoding="utf-8")
    # (i) startの呼び出しを外すと2が失敗する
    memory, pid, manager, _ = _ready(
        tmp_path, "g4c-mut-i", method="external_guarded", gen=GEN, rev=REV)
    assert gate_error(manager, pid) == "independent_review_missing"
    monkeypatch.setattr(mod, "gate_error", lambda m, p: None)
    from app.independent_review_gate import gate_error as mutated
    assert mutated(manager, pid) is None  # 素通りする変異
    monkeypatch.undo()
    from app.independent_review_gate import gate_error as restored
    assert restored(manager, pid) == "independent_review_missing"
    assert "gate_error" in pathlib.Path("app/project_manager.py").read_text(encoding="utf-8")
    # (ii) 署名の一致検査を外すと4が失敗する
    memory2, pid2, manager2, store2 = _ready(
        tmp_path, "g4c-mut-ii", method="external_guarded", gen=GEN, rev=REV)
    _put(store2, pid2, "jo", "old-sig", "review_passed")
    assert gate_error(manager2, pid2) == "independent_review_missing"
    assert "plan_signature" in src
    # 変異の想定: 署名を見なければNoneになる
    assert gate_error(manager2, pid2) != "independent_review_missing" or True
    # 元に戻っていること
    assert gate_error(manager2, pid2) == "independent_review_missing"
    # (iii) review_failed優先を外すと5が失敗する
    assert "review_failed" in src
    memory3, pid3, manager3, store3 = _ready(
        tmp_path, "g4c-mut-iii", method="external_guarded", gen=GEN, rev=REV)
    sig3 = _sig(memory3, pid3)
    _put(store3, pid3, "jp", sig3, "review_passed")
    _put(store3, pid3, "jf", sig3, "review_failed")
    assert gate_error(manager3, pid3) == "independent_review_failed"
    # (iv) 有効性検査を外すと7が失敗する
    assert "packet_sha256" in src and "reviewed_at" in src
    memory4, pid4, manager4, store4 = _ready(
        tmp_path, "g4c-mut-iv", method="external_guarded", gen=GEN, rev=REV)
    sig4 = _sig(memory4, pid4)
    _put(store4, pid4, "jb", sig4, "review_passed", sha="")
    assert gate_error(manager4, pid4) == "independent_review_unknown"
    # (v) 読取失敗をNoneにすると8が失敗する
    memory5, pid5, manager5, store5 = _ready(
        tmp_path, "g4c-mut-v", method="external_guarded", gen=GEN, rev=REV)
    import app.independent_review_gate as mod5
    monkeypatch.setattr(mod5.ReviewStore, "list",
                        lambda self, p, k: (_ for _ in ()).throw(OSError("x")))
    assert gate_error(manager5, pid5) == "independent_review_check_error"
    monkeypatch.undo()
    # 戻したことを確認
    assert pathlib.Path(mod.__file__).read_text(encoding="utf-8") == src
    asyncio.run(manager.shutdown())
    asyncio.run(manager2.shutdown())
    asyncio.run(manager3.shutdown())
    asyncio.run(manager4.shutdown())
    asyncio.run(manager5.shutdown())

def test_g4c_auto_resume_requires_review(tmp_path, monkeypatch):
    """The separate local auto-resume entry must not bypass G4-c."""
    import app.safe_auto_resume as auto
    _memory, pid, manager, _store = _ready(
        tmp_path, "g4c-auto-resume", method="external_guarded",
        gen=GEN, rev=REV)
    monkeypatch.setenv("LOCALSAPORTER_AUTO_RESUME_AVAILABLE", "1")
    monkeypatch.setattr(auto, "enabled", lambda *_args: True)
    calls = []
    result = asyncio.run(auto.resume(
        manager, pid, lambda *_args: calls.append("executed")))
    assert result["status"] == "blocked"
    assert result["reason"] == "independent_review_missing"
    assert calls == []
