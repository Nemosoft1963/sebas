"""D1: レビュー指紋の単一化と診断コードの受入試験。外部AI・実通信は使わない。"""
import asyncio
import copy
import json

import pytest

import app.plan_feedback as fb
import app.plan_review_loop as loop
from app.experience_store import canonical, fingerprint


class Manager:
    def __init__(self, memory, workspace):
        self.memory = memory
        self.workspace = workspace
        self.planning_projects = set()
        self.llm = None
        self.workers = {}
        self._sync_memos = lambda pid: None
        self.provider_statuses = lambda: [{"id": "grok", "configured": True}]
        self.plan_review_runner = None


def _manager(tmp_path):
    from app.memory.short_term import ShortTermMemory
    from app.workspace_files import WorkspaceSandbox

    mem = ShortTermMemory(tmp_path / "memory" / "conversations.db")
    pid = mem.create_project("d1-pseudo")["id"]
    mem.save_mission(pid, "pseudo goal\n1. pseudo item",
                     "pseudo goal\n1. pseudo item", "", True, ["grok"])
    ws = WorkspaceSandbox(tmp_path / "workspace")
    mgr = Manager(mem, ws)
    from app.structured_planning import compile_plan, compile_task

    criteria = ["pseudo item"]
    task = compile_task(1, criteria[0], {"title": "pseudo doc",
        "scope": "pseudo整理", "headings": ["目的", "実施内容"],
        "depends_on": []}, [])
    compiled = compile_plan(criteria, [task], goal=mem.get_mission(pid)["goal"])
    mem.replace_plan(pid, "d1 pseudo plan", compiled["tasks"])
    from app.goal_contract import activate as _activate
    _activate(mgr, pid)
    return mgr, pid


def _reviews_one_ambiguous():
    # 1-2 は範囲指定で複数工程・複数達成条件にまたがり ambiguous になる。
    # step:99 は invalid_reference のため確定の受入には使わない。
    return [{"provider": "grok", "status": "fail", "issues": [
        {"step": "1-2", "unmet_goal": "unique one",
         "reason": "range refers to two steps", "remedy": "clarify",
         "severity": "major"},
    ]}]


def _run_with_stored_review(mgr, pid, reviews):
    from app.goal_review import ReviewStore

    _mission, snapshot, signature, contract = loop._current(mgr, pid)
    store = ReviewStore(mgr.memory.path)
    packet = {"pseudo": "packet"}
    # D1-2: 実走の不具合を再現する。保存行にだけ付随フィールドを足し、ラン内コピーには足さない。
    stored_reviews = copy.deepcopy(reviews)
    stored_reviews[0]["response_error"] = "stored-only metadata"
    stored_reviews[0]["issue_texts"] = ["stored-only text"]
    stored_reviews[0]["response"] = {"ok": True}
    store.put(pid, "plan", signature, {"packet": packet, "reviews": stored_reviews})
    packet_hash = fingerprint(packet)
    run = {
        "id": "run-d1", "project_id": pid, "state": "stopped",
        "stop_kind": "human_required", "plan_signature": signature,
        "goal_contract_hash": str(contract.get("content_hash") or ""),
        "packet_hash": packet_hash, "providers": ["grok"],
        "verification_rounds": 1, "external_calls": 1,
        "rounds": [{"round": 1, "plan_signature": signature,
                    "packet_hash": packet_hash, "providers": ["grok"],
                    "status": "fail", "reviews": copy.deepcopy(reviews)}],
        "issue_bindings": [], "manual_bindings": [],
        "created_at": 0.0, "updated_at": 0.0,
    }
    store.put(pid, "auto_loop", run["id"], run)
    return signature, contract, packet_hash


def test_d1_fingerprint_ignores_extra_fields_but_not_content():
    sig = "sig-d1"
    base = _reviews_one_ambiguous()
    variant = copy.deepcopy(base)
    # D1-1: issues外の付随フィールドの差だけを無視する。
    variant[0]["issue_texts"] = ["unique one"]
    variant[0]["response"] = {"ok": True}
    variant[0]["response_error"] = "boom"
    assert fb.review_fingerprint_of(sig, base) == fb.review_fingerprint_of(sig, variant)

    # D1-1: issues内の余分キーの変化は無視しない(指紋が変わる)。
    changed_extra = copy.deepcopy(base)
    changed_extra[0]["issues"][0]["extra"] = "付随"
    assert fb.review_fingerprint_of(sig, base) != fb.review_fingerprint_of(sig, changed_extra)

    changed = copy.deepcopy(base)
    changed[0]["issues"][0]["reason"] = "別の内容に変える"
    assert fb.review_fingerprint_of(sig, base) != fb.review_fingerprint_of(sig, changed)
    changed_status = copy.deepcopy(base)
    changed_status[0]["status"] = "pass"
    assert fb.review_fingerprint_of(sig, base) != fb.review_fingerprint_of(sig, changed_status)
    changed_provider = copy.deepcopy(base)
    changed_provider[0]["provider"] = "chatgpt"
    assert fb.review_fingerprint_of(sig, base) != fb.review_fingerprint_of(sig, changed_provider)

    # D1-1: dictでない行・issuesがlistでない行は空に潰さない。別の値なら別の指紋。
    broken_a = [{"provider": "grok", "status": "fail", "issues": [{"step": "1-2"}]}]
    broken_b = ["not-a-dict-row"]
    broken_c = [{"provider": "grok", "status": "fail", "issues": "not-a-list"}]
    broken_d = [{"provider": "grok", "status": "fail", "issues": [{"step": "different"}]}]
    assert fb.review_fingerprint_of(sig, broken_a) != fb.review_fingerprint_of(sig, broken_b)
    assert fb.review_fingerprint_of(sig, broken_a) != fb.review_fingerprint_of(sig, broken_c)
    assert fb.review_fingerprint_of(sig, broken_b) != fb.review_fingerprint_of(sig, broken_c)


def test_d1_confirm_then_remap_keeps_confirmation(tmp_path):
    mgr, pid = _manager(tmp_path)
    reviews = _reviews_one_ambiguous()
    signature, contract, packet_hash = _run_with_stored_review(mgr, pid, reviews)
    table = loop.build_issue_bindings(
        reviews, contract, loop._current(mgr, pid)[1],
        plan_signature=signature,
        goal_contract_hash=str(contract.get("content_hash") or ""),
        packet_hash=packet_hash)
    assert len(table["bindings"]) == 1
    assert table["bindings"][0]["state"] in ("ambiguous", "multiple_targets")
    run = loop.get_run(mgr, pid, "run-d1")
    run["issue_bindings"] = copy.deepcopy(table["bindings"])
    loop._save(mgr, run)

    candidates = table["bindings"][0].get("candidate_task_keys") or []
    criteria = table["bindings"][0].get("candidate_criterion_ids") or []
    task_key = str(candidates[0] if candidates else "")
    criterion_id = str(criteria[0] if criteria else "")

    async def _confirm():
        return await loop.confirm_binding(
            mgr, pid, "run-d1",
            table["bindings"][0]["issue_id"],
            actor="human-a", task_key=task_key,
            criterion_id=criterion_id, reason="人による対応先の確定",
            confirmed_preview=True)

    entry = asyncio.new_event_loop().run_until_complete(_confirm())
    assert entry.get("binding_snapshot_save_failed") is not True
    run = loop.get_run(mgr, pid, "run-d1")
    # D1-3: ゆるいアサーションを直す。成功時は空文字にする。
    assert run.get("binding_snapshot_save_failed") is False
    assert run.get("binding_snapshot_save_error") == ""
    assert run.get("binding_snapshot_save_error_code") == ""

    from app.goal_review import ReviewStore
    snap = ReviewStore(mgr.memory.path).get(pid, "binding_snapshot", signature)["snapshot"]
    assert snap.get("review_fingerprint_version") == fb.REVIEW_FINGERPRINT_VERSION
    assert snap.get("confirmed"), "確定が正本に入ること"

    remapped = asyncio.new_event_loop().run_until_complete(
        loop.remap_run(mgr, pid, "run-d1", actor="human-a",
                       idempotency_key="d1-remap"))
    assert remapped["external_calls"] == 1
    run2 = loop.get_run(mgr, pid, "run-d1")
    hit = [b for b in run2.get("issue_bindings") or []
           if str(b.get("issue_id") or "") == table["bindings"][0]["issue_id"]]
    assert hit and hit[0].get("selection_status") == "confirmed"
    unconfirmed = loop.unconfirmed_issue_ids(
        run2.get("issue_bindings") or [],
        [str(e.get("issue_id") or "") for e in (snap.get("confirmed") or [])])
    assert table["bindings"][0]["issue_id"] not in unconfirmed
    assert remapped.get("binding_snapshot_save_failed") is False


def test_d1_old_snapshot_without_version_is_inherited_and_upgraded(tmp_path):
    mgr, pid = _manager(tmp_path)
    reviews = _reviews_one_ambiguous()
    signature, contract, packet_hash = _run_with_stored_review(mgr, pid, reviews)
    from app.goal_review import ReviewStore as _StoreForOld
    _stored_reviews_for_old = (_StoreForOld(mgr.memory.path).get(pid, "plan", signature) or {}).get("reviews") or []
    run_reviews_for_old = loop.get_run(mgr, pid, "run-d1")["rounds"][0]["reviews"]
    table = loop.build_issue_bindings(
        reviews, contract, loop._current(mgr, pid)[1],
        plan_signature=signature,
        goal_contract_hash=str(contract.get("content_hash") or ""),
        packet_hash=packet_hash)
    binding = table["bindings"][0]
    # D1-2/4: 保存行由来とラン内コピー由来で同じissuesなら同じ新指紋になる。
    assert fb.review_fingerprint_of(signature, _stored_reviews_for_old) == fb.review_fingerprint_of(
        signature, run_reviews_for_old) == fb.review_fingerprint_of(signature, reviews)
    new_fp = fb.review_fingerprint_of(signature, reviews)
    old_fp_saved_style = fb.legacy_review_fingerprint_of(signature, reviews)
    assert new_fp != old_fp_saved_style

    from app.goal_review import ReviewStore
    store = ReviewStore(mgr.memory.path)
    contract_hash = str(contract.get("content_hash") or "")
    # D1-4: 旧方式の正本の継承は両方式(保存行由来/ラン内コピー由来)で確認する。
    old_fp_saved_style_run = fb.legacy_review_fingerprint_of(signature, run_reviews_for_old)
    old_fp_saved_style_stored = fb.legacy_review_fingerprint_of(signature, _stored_reviews_for_old)
    for old_fp in (new_fp, old_fp_saved_style_run, old_fp_saved_style_stored):
        legacy_snapshot = {
            "version": fb.BINDING_SNAPSHOT_VERSION,
            "plan_signature": signature,
            "goal_contract_hash": contract_hash,
            "packet_hash": packet_hash,
            "review_fingerprint": old_fp,
            "bindings": [{"issue_id": binding["issue_id"], "state": binding["state"]}],
            "confirmed": [{"issue_id": binding["issue_id"], "task_key": "SC01",
                           "criterion_id": "SC01", "actor": "human-a",
                           "reason": "旧式の確定", "plan_signature": signature,
                           "goal_contract_hash": contract_hash,
                           "packet_hash": packet_hash,
                           "review_fingerprint": old_fp}],
        }
        # 旧形式(版なし)は現行データと一致すれば継承される。保存行由来とラン内コピー由来の両方を渡す。
        fb.check_binding_snapshot_current(
            legacy_snapshot, plan_signature=signature,
            goal_contract_hash=contract_hash, packet_hash=packet_hash,
            review_fingerprint=new_fp,
            current_reviews=[_stored_reviews_for_old, run_reviews_for_old],
            legacy_review_fingerprint=[old_fp_saved_style_run, old_fp_saved_style_stored])
        run = loop.get_run(mgr, pid, "run-d1")
        run["issue_bindings"] = copy.deepcopy(table["bindings"])
        run["manual_bindings"] = [dict(e) for e in legacy_snapshot["confirmed"]]
        loop._save(mgr, run)
        store.put(pid, "binding_snapshot", signature,
                  {"snapshot": legacy_snapshot, "saved_at": 0.0})

        _cands = table["bindings"][0].get("candidate_task_keys") or []
        _crits = table["bindings"][0].get("candidate_criterion_ids") or []
        _task = str(_cands[0] if _cands else "")
        _crit = str(_crits[0] if _crits else "")

        async def _confirm2(bid=binding["issue_id"], task_key=_task, criterion_id=_crit):
            return await loop.confirm_binding(
                mgr, pid, "run-d1", bid, actor="human-b",
                task_key=task_key, criterion_id=criterion_id,
                reason="旧正本の継承", confirmed_preview=True)

        entry = asyncio.new_event_loop().run_until_complete(_confirm2())
        assert entry.get("binding_snapshot_save_failed") is not True
        upgraded = store.get(pid, "binding_snapshot", signature)["snapshot"]
        assert upgraded.get("review_fingerprint_version") == fb.REVIEW_FINGERPRINT_VERSION

    # どちらにも一致しなければ拒否され、診断コード review_changed が残る。
    bad_snapshot = dict(legacy_snapshot)
    bad_snapshot["review_fingerprint"] = "deadbeef" * 8
    with pytest.raises(ValueError, match="評価結果"):
        fb.check_binding_snapshot_current(
            bad_snapshot, plan_signature=signature,
            goal_contract_hash=contract_hash, packet_hash=packet_hash,
            review_fingerprint=new_fp,
            current_reviews=[_stored_reviews_for_old, run_reviews_for_old],
            legacy_review_fingerprint=[old_fp_saved_style_run, old_fp_saved_style_stored])


def _code_for(**kwargs):
    try:
        fb.check_binding_snapshot_current(
            kwargs.pop("snapshot"), plan_signature=kwargs.pop("plan_signature"),
            goal_contract_hash=kwargs.pop("goal_contract_hash"),
            packet_hash=kwargs.pop("packet_hash"),
            review_fingerprint=kwargs.pop("review_fingerprint"),
            current_reviews=kwargs.pop("current_reviews", None))
    except ValueError as exc:
        return str(getattr(exc, "code", "") or ""), str(exc)
    return "", ""


def test_d1_error_codes_and_no_secret_in_detail(tmp_path):
    secret_text = "連絡先 tanaka@example.com 電話03-1234-5678 sk-live-0123456789ABCDEF の指摘"
    reviews = [{"provider": "grok", "status": "fail", "issues": [
        {"step": "99", "unmet_goal": secret_text,
         "reason": "理由", "remedy": "対応", "severity": "major"}]}]
    sig = "sig-secret"
    packet_hash = "packet-secret"
    contract_hash = "contract-secret"
    new_fp = fb.review_fingerprint_of(sig, reviews)
    base = fb.build_binding_snapshot(
        bindings=[{"issue_id": "i1", "state": "ambiguous"}],
        plan_signature=sig, goal_contract_hash=contract_hash,
        packet_hash=packet_hash, review_fingerprint=new_fp, confirmed=[])

    cases = {
        "snapshot_missing": (None, sig, contract_hash, packet_hash, new_fp, "対応付けスナップショットを確認できません"),
        "snapshot_version": (dict(base, version="binding-snapshot-v0"), sig, contract_hash, packet_hash, new_fp, "版が一致しません"),
        "plan_signature_changed": (dict(base), "other", contract_hash, packet_hash, new_fp, "計画版"),
        "goal_contract_changed": (dict(base), sig, "other", packet_hash, new_fp, "目標契約"),
        "packet_changed": (dict(base), sig, contract_hash, "other", new_fp, "packet"),
        "review_changed": (dict(base), sig, contract_hash, packet_hash, "other", "評価結果"),
        "confirmed_entry_version": (dict(base, confirmed=[{"issue_id": "i1", "plan_signature": "other",
                                                          "goal_contract_hash": contract_hash,
                                                          "packet_hash": packet_hash}]), sig, contract_hash, packet_hash, new_fp, "確定済み"),
    }
    for expected_code, (snapshot, psig, chash, phash, rfp, msg) in cases.items():
        code, message = _code_for(snapshot=snapshot, plan_signature=psig,
                                  goal_contract_hash=chash, packet_hash=phash,
                                  review_fingerprint=rfp, current_reviews=reviews)
        assert code == expected_code, expected_code
        assert msg in message
    try:
        fb.check_binding_snapshot_current(None, plan_signature="", goal_contract_hash="",
                                          packet_hash="", review_fingerprint="")
    except ValueError as exc:
        # 型は ValueError のまま(サブクラスでコードを運ぶ)。既存の except ValueError を壊さない。
        assert isinstance(exc, ValueError)
        assert type(exc).__name__ == "BindingSnapshotError"
        assert str(getattr(exc, "code", "")) == "snapshot_missing"
    else:
        raise AssertionError("snapshot_missing must be rejected")
    detail = fb.binding_snapshot_error_detail(
        plan_signature=sig, packet_hash=packet_hash, review_fingerprint=new_fp)
    blob = json.dumps(detail, ensure_ascii=False)
    assert "tanaka@example.com" not in blob
    assert "03-1234-5678" not in blob
    assert "sk-live" not in blob
    assert detail["review_fingerprint"] == new_fp[:12]
    assert detail["packet_hash"] == packet_hash[:12]
    assert detail["plan_signature"] == sig[:12]


def test_d1_save_failure_records_code_and_short_detail(tmp_path):
    mgr, pid = _manager(tmp_path)
    secret_text = "連絡先 tanaka@example.com 電話03-1234-5678 sk-live-0123456789ABCDEF の指摘"
    reviews = [{"provider": "grok", "status": "fail", "issues": [
        {"step": "1-2", "unmet_goal": secret_text,
         "reason": "range refers to two steps", "remedy": "clarify",
         "severity": "major"}]}]
    signature, contract, packet_hash = _run_with_stored_review(mgr, pid, reviews)
    table = loop.build_issue_bindings(
        reviews, contract, loop._current(mgr, pid)[1],
        plan_signature=signature,
        goal_contract_hash=str(contract.get("content_hash") or ""),
        packet_hash=packet_hash)
    run = loop.get_run(mgr, pid, "run-d1")
    run["issue_bindings"] = copy.deepcopy(table["bindings"])
    loop._save(mgr, run)

    # 先に別のレビュー指紋を持つ正本を置いておき、confirm_binding の正本照合を失敗させる。
    from app.goal_review import ReviewStore
    store = ReviewStore(mgr.memory.path)
    bad_snap = fb.build_binding_snapshot(
        bindings=table["bindings"], plan_signature=signature,
        goal_contract_hash=str(contract.get("content_hash") or ""),
        packet_hash=packet_hash, review_fingerprint="f" * 64, confirmed=[])
    store.put(pid, "binding_snapshot", signature, {"snapshot": bad_snap, "saved_at": 0.0})

    candidates = table["bindings"][0].get("candidate_task_keys") or []
    criteria = table["bindings"][0].get("candidate_criterion_ids") or []
    task_key = str(candidates[0] if candidates else "")
    criterion_id = str(criteria[0] if criteria else "")

    async def _confirm():
        return await loop.confirm_binding(
            mgr, pid, "run-d1", table["bindings"][0]["issue_id"],
            actor="human-a", task_key=task_key, criterion_id=criterion_id,
            reason="人による対応先の確定", confirmed_preview=True)

    entry = asyncio.new_event_loop().run_until_complete(_confirm())
    assert entry.get("binding_snapshot_save_failed") is True
    assert entry.get("binding_snapshot_save_error") == "BindingSnapshotError"
    assert entry.get("binding_snapshot_save_error_code") == "review_changed"
    view = loop.public_view(loop.get_run(mgr, pid, "run-d1"))
    assert view["binding_snapshot_save_failed"] is True
    assert view["binding_snapshot_save_error"] == "BindingSnapshotError"
    assert view["binding_snapshot_save_error_code"] == "review_changed"
    detail = view["binding_snapshot_save_detail"]
    assert set(detail.keys()) == {"plan_signature", "packet_hash", "review_fingerprint"}
    assert all(len(str(v)) <= 12 for v in detail.values())
    blob = json.dumps({
        "error": view["binding_snapshot_save_error"],
        "code": view["binding_snapshot_save_error_code"],
        "detail": detail,
        "entry": entry,
    }, ensure_ascii=False)
    assert "tanaka@example.com" not in blob
    assert "03-1234-5678" not in blob
    assert "sk-live" not in blob


def test_d1_stale_confirmation_is_rejected(tmp_path):
    mgr, pid = _manager(tmp_path)
    reviews = _reviews_one_ambiguous()
    signature, contract, packet_hash = _run_with_stored_review(mgr, pid, reviews)
    contract_hash = str(contract.get("content_hash") or "")
    new_fp = fb.review_fingerprint_of(signature, reviews)
    snapshot = fb.build_binding_snapshot(
        bindings=[], plan_signature=signature,
        goal_contract_hash=contract_hash, packet_hash=packet_hash,
        review_fingerprint=new_fp,
        confirmed=[{"issue_id": "i1", "task_key": "SC01",
                    "plan_signature": signature,
                    "goal_contract_hash": contract_hash,
                    "packet_hash": packet_hash,
                    "review_fingerprint": new_fp}])
    changed = copy.deepcopy(reviews)
    changed[0]["issues"][0]["reason"] = "別の内容に変えた"
    changed_fp = fb.review_fingerprint_of(signature, changed)
    assert changed_fp != new_fp
    with pytest.raises(ValueError, match="評価結果"):
        fb.check_binding_snapshot_current(
            snapshot, plan_signature=signature,
            goal_contract_hash=contract_hash, packet_hash=packet_hash,
            review_fingerprint=changed_fp, current_reviews=changed)
    with pytest.raises(ValueError, match="計画版"):
        fb.check_binding_snapshot_current(
            snapshot, plan_signature="other",
            goal_contract_hash=contract_hash, packet_hash=packet_hash,
            review_fingerprint=new_fp, current_reviews=reviews)


def test_d1_matrix_binding_uses_unified_fingerprint_and_confirmed(tmp_path):
    mgr, pid = _manager(tmp_path)
    reviews = _reviews_one_ambiguous()
    signature, contract, packet_hash = _run_with_stored_review(mgr, pid, reviews)
    snapshot = loop._current(mgr, pid)[1]
    table = loop.build_issue_bindings(
        reviews, contract, snapshot,
        plan_signature=signature,
        goal_contract_hash=str(contract.get("content_hash") or ""),
        packet_hash=packet_hash)
    run = loop.get_run(mgr, pid, "run-d1")
    run["issue_bindings"] = copy.deepcopy(table["bindings"])
    loop._save(mgr, run)

    candidates = table["bindings"][0].get("candidate_task_keys") or []
    criteria = table["bindings"][0].get("candidate_criterion_ids") or []
    task_key = str(candidates[0] if candidates else "")
    criterion_id = str(criteria[0] if criteria else "")

    async def _confirm():
        return await loop.confirm_binding(
            mgr, pid, "run-d1", table["bindings"][0]["issue_id"],
            actor="human-a", task_key=task_key, criterion_id=criterion_id,
            reason="人による対応先の確定", confirmed_preview=True)

    asyncio.new_event_loop().run_until_complete(_confirm())
    # ReviewStore 側に付随フィールドを追加しても _bind_issues_for_matrix が同じ指紋で照合を通し、
    # 確定済みの対応先(resolved)を返すことを確認する。
    from app.goal_review import ReviewStore
    store = ReviewStore(mgr.memory.path)
    stored_plan = store.get(pid, "plan", signature)
    stored_plan["reviews"][0]["response_error"] = "extra metadata"
    stored_plan["reviews"][0]["issue_texts"] = ["extra text"]
    store.put(pid, "plan", signature, stored_plan)

    issues = fb.issues_for(mgr, pid, signature)
    bound = fb._bind_issues_for_matrix(mgr, pid, signature, issues, snapshot)
    assert len(bound) == 1
    assert bound[0]["binding"]["state"] == "resolved"
    assert bound[0]["binding"]["target_task_keys"] == [task_key]
    assert bound[0]["binding"]["target_ids"] == [criterion_id]




def test_d1_old_run_with_changed_review_cannot_restore_confirmation():
    sig = "same-plan-signature"
    old_reviews = _reviews_one_ambiguous()
    changed_reviews = copy.deepcopy(old_reviews)
    changed_reviews[0]["issues"][0]["reason"] = "a different issue reason"
    old_fp = fb.legacy_review_fingerprint_of(sig, old_reviews)
    current_fp = fb.review_fingerprint_of(sig, changed_reviews)
    snapshot = {
        "version": fb.BINDING_SNAPSHOT_VERSION,
        "plan_signature": sig,
        "goal_contract_hash": "contract",
        "packet_hash": "packet",
        "review_fingerprint": old_fp,
        "bindings": [{"issue_id": "old-issue"}],
        "confirmed": [{"issue_id": "old-issue", "plan_signature": sig,
                       "goal_contract_hash": "contract", "packet_hash": "packet"}],
    }
    # 同じ計画署名の過去ランが残っても、現在のレビューと違えば確定を復活させない。
    with pytest.raises(fb.BindingSnapshotError) as error:
        fb.check_binding_snapshot_current(
            snapshot, plan_signature=sig, goal_contract_hash="contract",
            packet_hash="packet", review_fingerprint=current_fp,
            current_reviews=[changed_reviews, old_reviews],
            legacy_review_fingerprint=old_fp)
    assert error.value.code == "review_changed"


def test_d1_unknown_fingerprint_version_is_rejected_even_if_hash_matches():
    sig = "same-plan-signature"
    reviews = _reviews_one_ambiguous()
    current_fp = fb.review_fingerprint_of(sig, reviews)
    snapshot = fb.build_binding_snapshot(
        bindings=[], plan_signature=sig, goal_contract_hash="contract",
        packet_hash="packet", review_fingerprint=current_fp, confirmed=[])
    snapshot["review_fingerprint_version"] = "review-fingerprint-v999"
    with pytest.raises(fb.BindingSnapshotError) as error:
        fb.check_binding_snapshot_current(
            snapshot, plan_signature=sig, goal_contract_hash="contract",
            packet_hash="packet", review_fingerprint=current_fp,
            current_reviews=reviews)
    assert error.value.code == "snapshot_version"
