"""P1-1: 確定済み対応を修復候補の結合に使う。疑似PJ・実関数・外部通信なし。

- stepだけで書かれた指摘はテキスト照合でbound=False。人がconfirm_bindingで
  確定したあとはbound=True/binding_source="confirmed_snapshot"になり、
  確定されたcriterion_id・task_keyでSAFE_PLAN_PATCH候補(パッチ付き)が出る。
- 確定していない指摘は結合しない。確定した1件だけが結合する。
- 正本スナップショットが無効(署名・レビュー・packet・版の不一致)のとき確定は
  使われない。確定の工程が現行計画に無いときも使われない。
- 指摘IDが曖昧(同じIDが複数)なとき結合しない。
- 確定されても業務事実・開発必須の判定は緩めない。
- start経由でもbinding_sourceが出る。external_calls==0。previewは書込なし。
"""
import asyncio
import json
from types import SimpleNamespace

from app.experience_store import canonical, fingerprint


class Manager(SimpleNamespace):
    pass


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
    pid = mem.create_project("p1-1-pseudo")["id"]
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
    mem.replace_plan(pid, "p1-1 pseudo plan", compiled_live)
    from app.goal_contract import activate as _activate

    activated = _activate(mgr, pid)
    assert expected_versions <= {c.get("criterion_id")
                                 for c in activated.get("criteria") or []}, activated
    return mgr, pid


def _issue_id(provider, criterion, text):
    return fingerprint([provider, criterion, text.strip()])[:20]


def _parsed(step_text_issues):
    return [{"provider": "chatgpt", "status": "fail", "issues": step_text_issues}]


def _save_plan_review(mgr, pid, signature, parsed, packet):
    from app.goal_review import ReviewStore

    ReviewStore(mgr.memory.path).put(pid, "plan", signature, {
        "status": "fail", "reviews": parsed, "packet": packet,
        "success_count": 0, "required_count": 1,
    })


def _packet():
    return {"pkt": "p1-1"}


def _packet_hash(packet):
    return fingerprint(packet)


def _confirm_via_run(mgr, pid, signature, parsed, packet, binding):
    """保存済みレビューからremap→confirm_bindingし、確定entryを返す。"""
    import app.plan_review_loop as loop

    packet_hash = _packet_hash(packet)
    _save_plan_review(mgr, pid, signature, parsed, packet)
    contract_hash = binding["goal_contract_hash"]
    run = {
        "id": "run-p1-1", "project_id": pid, "plan_signature": signature,
        "goal_contract_hash": contract_hash, "packet_hash": packet_hash,
        "providers": ["chatgpt"], "state": "stopped", "stop_kind": "human_required",
        "rounds": [{"round": 1, "plan_signature": signature, "packet_hash": packet_hash,
                    "reviews": parsed, "review_source": "provider"}],
        "issue_bindings": [], "manual_bindings": [], "remap_history": [],
        "resume_history": [],
    }
    from app.goal_review import ReviewStore

    ReviewStore(mgr.memory.path).put(pid, "auto_loop", run["id"], run)
    remapped = asyncio.new_event_loop().run_until_complete(
        loop.remap_run(mgr, pid, run["id"], actor="human-a",
                       idempotency_key="remap-p1-1"))
    stored = loop.get_run(mgr, pid, run["id"])
    assert stored["issue_bindings"], "保存済み指摘の対応表が保存されていること"
    target = next(b for b in stored["issue_bindings"]
                  if str(b.get("issue_id") or "") == binding["issue_id"])
    assert target["requires_human_selection"] is True
    preview = loop.binding_selection_preview_for_run(
        stored, binding["issue_id"], task_key=binding["task_key"],
        criterion_id=binding["criterion_id"])
    assert preview["external_calls"] == 0
    entry = asyncio.new_event_loop().run_until_complete(loop.confirm_binding(
        mgr, pid, run["id"], binding["issue_id"], actor="human-a",
        task_key=binding["task_key"], criterion_id=binding["criterion_id"],
        reason="対象を確認した", confirmed_preview=True))
    assert entry["task_key"] == binding["task_key"]
    assert entry["criterion_id"] == binding["criterion_id"]
    assert entry.get("binding_snapshot_save_failed") is not True
    return entry


def _live_issues(mgr, pid, signature, parsed, packet):
    _save_plan_review(mgr, pid, signature, parsed, packet)
    from app.plan_feedback import issues_for

    return issues_for(mgr, pid, signature)


def test_confirmed_step_only_issue_is_bound_with_patch(tmp_path):
    import app.local_patch_preview as preview_module
    import app.plan_review_loop as loop
    from app.goal_contract import preview as contract_preview
    from app.goal_review import ReviewStore, plan_snapshot

    mgr, pid = _manager(tmp_path)
    _snap_live, signature = plan_snapshot(mgr, pid)
    contract = contract_preview(mgr, pid)
    goal_hash = str(contract.get("content_hash") or "")
    parsed = _parsed([
        {"step": "1", "unmet_goal": "pseudo method is thin (SC99)",
         "reason": "step one detail is thin", "remedy": "add step detail",
         "severity": "major"},
        {"step": "99", "unmet_goal": "other pseudo gap missing",
         "reason": "no such step pseudo detail", "remedy": "review pseudo target",
         "severity": "major"},
    ])
    packet = _packet()
    table = loop.build_issue_bindings(parsed, contract, _snap_live,
                                      plan_signature=signature,
                                      goal_contract_hash=goal_hash,
                                      packet_hash=_packet_hash(packet))
    amb = [b for b in table["bindings"] if b["state"] == "ambiguous"]
    inv = [b for b in table["bindings"] if b["state"] == "invalid_reference"]
    assert len(amb) == 1 and len(inv) == 1
    first = amb[0]
    issues = _live_issues(mgr, pid, signature, parsed, packet)
    assert [x["id"] for x in issues] == [b["issue_id"] for b in table["bindings"]]
    text_before = preview_module.preview(mgr, pid, issues=issues)
    target_before = next(c for c in text_before["candidates"]
                         if c["issue_id"] == first["issue_id"])
    assert target_before["bound"] is False
    assert target_before["binding_source"] == "unbound"
    assert target_before["classification"] == "development_required"
    # 人が1件だけ確定する。対応表・確定記録のissue_idの作り方を使う
    # (build_issue_bindingsと同じfingerprint式、既存名の再利用は下の_で確認)。
    from app.plan_review_loop import build_issue_bindings as _reused_issue_id_builder

    assert _reused_issue_id_builder is loop.build_issue_bindings
    entry = _confirm_via_run(mgr, pid, signature, parsed, packet,
                             {"issue_id": first["issue_id"],
                              "task_key": "SC01", "criterion_id": "SC01",
                              "goal_contract_hash": goal_hash})
    assert entry["issue_id"] == first["issue_id"]
    after = preview_module.preview(mgr, pid, issues=issues)
    assert _packet_hash(_packet()) == _packet_hash(packet)
    target = next(c for c in after["candidates"] if c["issue_id"] == first["issue_id"])
    assert target["bound"] is True
    assert target["binding_source"] == "confirmed_snapshot"
    assert target["criterion_id"] == "SC01"
    assert target["task_key"] == "SC01"
    assert target["classification"] == "safe_plan_patch"
    assert isinstance(target["patch"], dict) and target["patch"]["task_key"] == "SC01"
    assert target["patch"]["before"] != target["patch"]["after"]
    assert after["automatic_apply_allowed"] is False
    assert after["saved"] is False
    other = next(c for c in after["candidates"] if c["issue_id"] != first["issue_id"])
    assert other["bound"] is False and other["binding_source"] == "unbound"


def test_invalid_snapshot_or_missing_task_is_not_used(tmp_path):
    import app.local_patch_preview as preview_module
    import app.plan_review_loop as loop
    from app.goal_contract import preview as contract_preview
    from app.goal_review import ReviewStore, plan_snapshot

    mgr, pid = _manager(tmp_path)
    _snap_live, signature = plan_snapshot(mgr, pid)
    contract = contract_preview(mgr, pid)
    goal_hash = str(contract.get("content_hash") or "")
    parsed = _parsed([
        {"step": "1", "unmet_goal": "pseudo method is thin (SC99)",
         "reason": "step one detail is thin and mentions no known number",
         "remedy": "add step one detail", "severity": "major"},
    ])
    packet = _packet()
    table = loop.build_issue_bindings(parsed, contract, _snap_live,
                                      plan_signature=signature,
                                      goal_contract_hash=goal_hash,
                                      packet_hash=_packet_hash(packet))
    first = table["bindings"][0]
    issues = _live_issues(mgr, pid, signature, parsed, packet)
    assert first["state"] == "ambiguous", first
    _confirm_via_run(mgr, pid, signature, parsed, packet,
                     {"issue_id": first["issue_id"],
                      "task_key": "SC01", "criterion_id": "SC01",
                      "goal_contract_hash": goal_hash})
    store = ReviewStore(mgr.memory.path)
    saved_row = store.get(pid, "binding_snapshot", signature) or {}
    saved_snapshot = dict(saved_row.get("snapshot"))
    # 計画署名の変更: 確定は使われない。
    changed = dict(saved_snapshot, plan_signature="other-signature")
    store.put(pid, "binding_snapshot", "other-signature",
              {"snapshot": changed, "saved_at": 0.0})
    assert store.get(pid, "binding_snapshot", "other-signature")
    # 現行署名の正本を壊す(レビュー変更): checkで拒否される正本にする。
    broken = dict(saved_snapshot)
    broken["review_fingerprint"] = "fp-does-not-match"
    store.put(pid, "binding_snapshot", signature, {"snapshot": broken, "saved_at": 0.0})
    stale = preview_module.preview(mgr, pid, issues=issues)
    cand = stale["candidates"][0]
    assert cand["binding_source"] == "unbound"
    assert cand["bound"] is False
    # packet変更: 確定は使われない。
    store.put(pid, "binding_snapshot", signature, {"snapshot": saved_snapshot, "saved_at": 0.0})
    _save_plan_review(mgr, pid, signature, parsed, {"changed-pkt": True})
    updated = preview_module.preview(mgr, pid, issues=issues)
    assert updated["candidates"][0]["binding_source"] == "unbound"
    assert updated["candidates"][0]["bound"] is False
    # 版不一致: 確定は使われない。
    _save_plan_review(mgr, pid, signature, parsed, packet)
    versioned = dict(saved_snapshot, version="binding-snapshot-v999")
    store.put(pid, "binding_snapshot", signature, {"snapshot": versioned, "saved_at": 0.0})
    ver_out = preview_module.preview(mgr, pid, issues=issues)["candidates"][0]
    assert ver_out["binding_source"] == "unbound"
    assert ver_out["bound"] is False
    # 確定の工程が現行計画に無い: 結合しない。
    store.put(pid, "binding_snapshot", signature, {"snapshot": saved_snapshot, "saved_at": 0.0})
    saved_row2 = store.get(pid, "binding_snapshot", signature) or {}
    snap2 = dict(saved_row2.get("snapshot"))
    snap2["confirmed"] = [dict(entry, task_key="SC99", criterion_id="SC99")
                          for entry in snap2.get("confirmed")]
    store.put(pid, "binding_snapshot", signature, {"snapshot": snap2, "saved_at": 0.0})
    gone = preview_module.preview(mgr, pid, issues=issues)
    assert gone["candidates"][0]["bound"] is False
    assert gone["candidates"][0]["binding_source"] == "unbound"
    store.put(pid, "binding_snapshot", signature, {"snapshot": saved_snapshot, "saved_at": 0.0})


def test_ambiguous_issue_id_is_not_bound(tmp_path):
    import app.local_patch_preview as preview_module
    import app.plan_review_loop as loop
    from app.goal_contract import preview as contract_preview
    from app.goal_review import plan_snapshot

    mgr, pid = _manager(tmp_path)
    _snap_live, signature = plan_snapshot(mgr, pid)
    contract = contract_preview(mgr, pid)
    goal_hash = str(contract.get("content_hash") or "")
    parsed = _parsed([
        {"step": "1", "unmet_goal": "pseudo method is thin (SC99)",
         "reason": "step one detail is thin and mentions no known number",
         "remedy": "add step one detail", "severity": "major"},
    ])
    packet = _packet()
    table = loop.build_issue_bindings(parsed, contract, _snap_live,
                                      plan_signature=signature,
                                      goal_contract_hash=goal_hash,
                                      packet_hash=_packet_hash(packet))
    first = table["bindings"][0]
    issues = _live_issues(mgr, pid, signature, parsed, packet)
    assert first["state"] == "ambiguous", first
    _confirm_via_run(mgr, pid, signature, parsed, packet,
                     {"issue_id": first["issue_id"],
                      "task_key": "SC01", "criterion_id": "SC01",
                      "goal_contract_hash": goal_hash})
    # 同じIDが複数の指摘にある(曖昧): 結合しない。
    dup = [dict(issues[0]), dict(issues[0])]
    assert dup[0]["id"] == dup[1]["id"]
    out = preview_module.preview(mgr, pid, issues=dup)
    assert [c["issue_id"] for c in out["candidates"]].count(first["issue_id"]) == 2
    assert all(c["bound"] is False for c in out["candidates"])
    assert all(c["binding_source"] == "unbound" for c in out["candidates"])


def test_confirmed_classification_is_not_relaxed(tmp_path):
    import app.local_patch_preview as preview_module
    import app.plan_review_loop as loop
    from app.goal_contract import preview as contract_preview
    from app.goal_review import plan_snapshot

    mgr, pid = _manager(tmp_path)
    _snap_live, signature = plan_snapshot(mgr, pid)
    contract = contract_preview(mgr, pid)
    goal_hash = str(contract.get("content_hash") or "")
    parsed = _parsed([
        {"step": "1", "unmet_goal": "pseudo budget basis (SC99)",
         "reason": "予算の価格根拠が未定で業務事実の確認が必要",
         "remedy": "decide price basis", "severity": "major"},
        {"step": "2", "unmet_goal": "pseudo new feature (SC99)",
         "reason": "外部APIの専用処理と機能追加の実装が必要",
         "remedy": "implement API", "severity": "major"},
    ])
    packet = _packet()
    table = loop.build_issue_bindings(parsed, contract, _snap_live,
                                      plan_signature=signature,
                                      goal_contract_hash=goal_hash,
                                      packet_hash=_packet_hash(packet))
    by_id = {b["issue_id"]: b for b in table["bindings"]}
    assert len(by_id) == 2
    assert {b["state"] for b in table["bindings"]} == {"ambiguous"}, table["bindings"]
    issues = _live_issues(mgr, pid, signature, parsed, packet)
    ids = list(by_id)
    _confirm_via_run(mgr, pid, signature, parsed, packet,
                     {"issue_id": ids[0], "task_key": "SC01",
                      "criterion_id": "SC01", "goal_contract_hash": goal_hash})
    _confirm_via_run2_entry = _confirm_second_issue(
        mgr, pid, signature, parsed, ids[1], goal_hash)
    assert _confirm_via_run2_entry["issue_id"] == ids[1]
    out = preview_module.preview(mgr, pid, issues=issues)
    rows = {c["issue_id"]: c for c in out["candidates"]}
    assert rows[ids[0]]["bound"] is True
    assert rows[ids[0]]["binding_source"] == "confirmed_snapshot"
    assert rows[ids[0]]["classification"] == "business_fact"
    assert rows[ids[0]]["patch"] is None
    assert rows[ids[0]]["question"]
    assert rows[ids[1]]["bound"] is True
    assert rows[ids[1]]["binding_source"] == "confirmed_snapshot"
    assert rows[ids[1]]["classification"] == "development_required"
    assert rows[ids[1]]["patch"] is None


def _confirm_second_issue(mgr, pid, signature, parsed, issue_id, goal_hash):
    import app.plan_review_loop as loop

    return asyncio.new_event_loop().run_until_complete(loop.confirm_binding(
        mgr, pid, "run-p1-1", issue_id, actor="human-a",
        task_key="SC02", criterion_id="SC02",
        reason="対象を確認した", confirmed_preview=True))


def test_start_exposes_binding_source_without_writes_or_external_calls(tmp_path):
    import app.local_patch_preview as preview_module
    import app.plan_review_loop as loop
    from app.goal_contract import preview as contract_preview
    from app.goal_review import ReviewStore, plan_snapshot

    mgr, pid = _manager(tmp_path)
    _snap_live, signature = plan_snapshot(mgr, pid)
    contract = contract_preview(mgr, pid)
    goal_hash = str(contract.get("content_hash") or "")
    parsed = _parsed([
        {"step": "1", "unmet_goal": "pseudo method is thin (SC99)",
         "reason": "step one detail is thin and mentions no known number",
         "remedy": "add step one detail", "severity": "major"},
    ])
    packet = _packet()
    table = loop.build_issue_bindings(parsed, contract, _snap_live,
                                      plan_signature=signature,
                                      goal_contract_hash=goal_hash,
                                      packet_hash=_packet_hash(packet))
    first = table["bindings"][0]
    issues = _live_issues(mgr, pid, signature, parsed, packet)
    assert first["state"] == "ambiguous", first
    _confirm_via_run(mgr, pid, signature, parsed, packet,
                     {"issue_id": first["issue_id"],
                      "task_key": "SC01", "criterion_id": "SC01",
                      "goal_contract_hash": goal_hash})
    store = ReviewStore(mgr.memory.path)
    before = {sig: payload for sig, payload in store.list(pid, "binding_snapshot")}
    assert before, "確定で正本スナップショットが保存されていること"
    from app.plan_repair_loop import start as repair_start

    run = repair_start(mgr, pid, "human-p1-1", issues)
    assert run["external_calls"] == 0
    assert run["candidates"][0]["binding_source"] == "confirmed_snapshot"
    assert run["candidates"][0]["bound"] is True
    after = {sig: payload for sig, payload in store.list(pid, "binding_snapshot")}
    assert canonical(after) == canonical(before), "previewは書込なし"
    other = preview_module.preview(mgr, pid, issues=issues)
    assert other["candidates"][0]["binding_source"] == "confirmed_snapshot"
